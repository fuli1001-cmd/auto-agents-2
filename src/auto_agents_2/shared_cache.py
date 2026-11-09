"""Opt-in, same-user cache. Project state and installed environments stay private."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
from pathlib import Path

from .browser_sources import equivalent_plans
from .errors import BusyProject, PolicyViolation
from .safe_files import read_regular
from .util import async_file_lock, digest, file_lock, identifier, write_json
from .workspace import file_digest

DOWNLOAD_KINDS = {"npm", "pip", "conda"}
ID = re.compile(r"^[a-f0-9]{64}$")


def local_directory(path, *, create=False, private=False):
    path = Path(path).expanduser().absolute()
    if path != path.resolve():
        raise PolicyViolation("Shared cache paths must not traverse symlinks")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise PolicyViolation("Shared cache directory must be owned by this user and not writable by others")
    if private and info.st_mode & 0o077:
        raise PolicyViolation("Shared cache root must be private to this user (mode 0700)")
    return path


def read_json(path):
    return json.loads(read_regular(path, 32 * 1024 * 1024))


def manifest_tree(root):
    """Full content identity, including empty directories and bounded internal links."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PolicyViolation("Browser object must be a real directory")
    rows = []
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in sorted([*directories, *files]):
            path = Path(folder, name)
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise PolicyViolation("Browser object contains an unresolved link") from exc
                if Path(target).is_absolute() or not resolved.is_relative_to(root.resolve()):
                    raise PolicyViolation("Browser object link escapes its immutable directory")
                rows.append([relative, "link", target])
            elif stat.S_ISDIR(info.st_mode):
                rows.append([relative, "directory"])
            elif stat.S_ISREG(info.st_mode):
                rows.append([relative, "file", bool(info.st_mode & 0o111), info.st_size, file_digest(path)])
            else:
                raise PolicyViolation("Browser object contains a special file")
    return sorted(rows)


def browser_payload_spec(spec):
    """Only transport provenance may differ; all driver/ABI/platform inputs stay."""
    if not isinstance(spec, dict):
        raise ValueError("Browser specification must be a mapping")
    return {key: value for key, value in spec.items() if key != "download_configuration_hash"}


def immutable(root):
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(folder, name)
            if not path.is_symlink():
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        for name in directories:
            path = Path(folder, name)
            if not path.is_symlink():
                path.chmod(0o555)
    root.chmod(0o555)


def remove_object(path, root):
    """Only registered real object roots; unlink internal symlinks without following."""
    if path.parent != root / "objects" or not ID.fullmatch(path.name) or path.is_symlink():
        raise PolicyViolation("Refusing unregistered shared object removal")
    for folder, directories, _ in os.walk(path, followlinks=False):
        Path(folder).chmod(0o700)
        directories[:] = [name for name in directories if not Path(folder, name).is_symlink()]
    shutil.rmtree(path)


class SharedCache:
    def __init__(self, config):
        self.config = config
        settings = config.load().shared_cache
        self.enabled = settings.enabled
        self.root = Path(settings.root).expanduser() if settings.root else Path.home() / ".cache/auto-agents-2/v1"
        if self.enabled and (not self.root.is_absolute() or self.root == Path.home() or self.root == Path("/")
                or self.root.is_relative_to(config.project) or config.project.is_relative_to(self.root)):
            raise PolicyViolation("Shared cache requires a dedicated absolute directory outside the project")

    def initialize(self):
        if not self.enabled:
            raise PolicyViolation("Shared cache requires explicit configuration")
        local_directory(self.root, create=True, private=True)
        # A dedicated marker prevents GC from adopting an arbitrary existing
        # private directory. Initialization and concurrent first use are atomic.
        with file_lock(self.root / ".initialization.lock"):
            marker = self.root / "cache.json"
            expected = {"version": 1, "owner_uid": os.getuid(), "kind": "aa2-shared-dependencies"}
            if marker.exists():
                if read_json(marker) != expected:
                    raise PolicyViolation("Shared cache ownership marker is invalid")
            else:
                if any(p.name != ".initialization.lock" for p in self.root.iterdir()):
                    raise PolicyViolation("Refusing to adopt a nonempty unmanaged shared cache directory")
                write_json(marker, expected)
            for name in ("downloads", "objects", "index", "references", "locks", "staging"):
                local_directory(self.root / name, create=True)

    def download_path(self, kind):
        if not self.enabled or kind not in DOWNLOAD_KINDS:
            raise PolicyViolation("Shared download kind is not authorized")
        self.initialize()
        return local_directory(self.root / "downloads" / kind, create=True)

    def validate_writes(self, paths):
        if not self.enabled:
            raise PolicyViolation("Shared cache write mounts require explicit configuration")
        local_directory(self.root, private=True)
        for path in paths:
            path = Path(path)
            if path.parent != self.root / "downloads" or path.name not in DOWNLOAD_KINDS:
                raise PolicyViolation("Only registered download cache directories may be writable")
            local_directory(path)
        return [Path(p) for p in paths]

    @contextlib.asynccontextmanager
    async def downloads(self, kinds):
        if not self.enabled:
            yield {}
            return
        self.initialize()
        async with contextlib.AsyncExitStack() as stack:
            paths = {}
            for kind in sorted(set(kinds)):
                await stack.enter_async_context(async_file_lock(self.root / "locks" / f"download-{kind}.lock"))
                # GC may have removed an idle directory before we acquired the
                # lock; recreate it only while owning the same installer lock.
                paths[kind] = self.download_path(kind)
            try:
                yield paths
            finally:
                for kind in paths:
                    write_json(self.root / "index" / f"download-{kind}.json", {"touched": time.time()})

    def _object_metadata(self, object_id):
        if not isinstance(object_id, str) or not ID.fullmatch(object_id):
            raise PolicyViolation("Invalid browser object identity")
        path = self.root / "objects" / object_id
        local_directory(self.root, private=True)
        local_directory(path)
        record = read_json(path / "manifest.json")
        if (not isinstance(record, dict) or not isinstance(record.get("spec"), dict)
                or record.get("version") != 1 or record.get("object_id") != object_id
                or record.get("spec_key") != digest(record.get("spec"))):
            raise PolicyViolation("Browser object metadata is inconsistent")
        return record

    def object(self, object_id):
        record = self._object_metadata(object_id)
        path = self.root / "objects" / object_id
        for item in [path, *(path.rglob("*"))]:
            if not item.is_symlink() and item.stat().st_mode & 0o222:
                raise PolicyViolation("Shared browser object is no longer immutable")
        entries = manifest_tree(path / "browser")
        if entries != record.get("files") or digest(entries) != record.get("content_hash"):
            raise PolicyViolation("Shared browser content changed; retain the old object and reprepare")
        return {**record, "path": str(path / "browser")}

    def reference(self, record, consumer, run_id):
        project = str(self.config.project)
        key = digest([project, run_id, str(consumer), record["object_id"]])
        value = {"version": 1, "project": project, "run_id": run_id,
                 "consumer": str(consumer), "object_id": record["object_id"], "pending": True, "touched": time.time()}
        write_json(self.root / "references" / f"{key}.json", value)

    def activate(self, record, consumer, run_id):
        key = digest([str(self.config.project), run_id, str(consumer), record["object_id"]])
        with file_lock(self.root / "locks/control.lock"):
            path = self.root / "references" / f"{key}.json"
            value = read_json(path)
            value["pending"] = False
            write_json(path, value)

    def _valid_provenance(self, provenance, spec):
        return (isinstance(provenance, dict) and spec.get("version") == 1
                and provenance.get("version") == 2 and provenance.get("spec_key") == digest(spec)
                and isinstance(spec.get("download_configuration_hash"), str)
                and bool(ID.fullmatch(spec["download_configuration_hash"]))
                and provenance.get("download_configuration_hash") == spec["download_configuration_hash"])

    def _installation_witness(self, spec, record, provenance):
        return {"spec": spec, "provenance": provenance, "content_hash": record["content_hash"],
                "files_hash": digest(record["files"])}

    def _valid_witness(self, witness, record):
        return (isinstance(witness, dict) and isinstance(witness.get("spec"), dict)
                and self._valid_provenance(witness.get("provenance"), witness["spec"])
                and browser_payload_spec(witness["spec"]) == browser_payload_spec(record["spec"])
                and witness.get("content_hash") == record["content_hash"]
                and witness.get("files_hash") == digest(record["files"]))

    def _index_matches(self, index, spec, record):
        if not isinstance(index, dict) or index.get("object_id") != record["object_id"]:
            return False
        if record["spec_key"] == digest(spec):
            return True
        provenance, proof = index.get("provenance"), index.get("proof")
        if (index.get("version") != 2 or index.get("spec") != spec
                or index.get("content_hash") != record["content_hash"]
                or browser_payload_spec(record["spec"]) != browser_payload_spec(spec)
                or not self._valid_provenance(provenance, spec) or not isinstance(proof, dict)
                or proof.get("requested_spec_key") != digest(spec)
                or proof.get("object_id") != record["object_id"]
                or proof.get("object_spec_key") != record["spec_key"]
                or proof.get("content_hash") != record["content_hash"]
                or proof.get("provenance_hash") != digest(provenance)
                or not self._valid_witness(proof.get("installation"), record)):
            return False
        anchor = proof["installation"]
        if index.get("reuse") == "verified-content-v1":
            return anchor["spec"] == spec and anchor["provenance"] == provenance
        if index.get("reuse") == "official-download-plan-v1":
            return equivalent_plans(provenance.get("official_plan"), anchor["provenance"].get("official_plan"))
        return False

    def _source_witness(self, index, record):
        if record["spec_key"] == digest(index.get("spec", record["spec"])):
            provenance = record.get("provenance")
            if self._valid_provenance(provenance, record["spec"]):
                return self._installation_witness(record["spec"], record, provenance)
            return None  # A mutable index cannot repair missing immutable source evidence.
        proof = index.get("proof")
        witness = proof.get("installation") if isinstance(proof, dict) else None
        return witness if self._valid_witness(witness, record) else None

    def matches(self, spec, record):
        """Read-only binding comparison, including an already proved source alias."""
        if record["spec_key"] == digest(spec):
            return True
        try:
            index = read_json(self.root / "index" / f"browser-{digest(spec)}.json")
            return self._index_matches(index, spec, record)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _register_browser(self, spec, record, consumer, run_id, *, provenance=None, reuse=None, installation=None):
        index = {"version": 2, "object_id": record["object_id"], "spec": spec,
                 "content_hash": record["content_hash"], "touched": time.time(),
                 "provenance": provenance, "reuse": reuse}
        if reuse:
            index["proof"] = {"requested_spec_key": digest(spec), "object_id": record["object_id"],
                              "object_spec_key": record["spec_key"], "content_hash": record["content_hash"],
                              "provenance_hash": digest(provenance), "installation": installation}
            if not self._index_matches(index, spec, record):
                raise PolicyViolation("Browser source reuse proof is incomplete")
        self.reference(record, consumer, run_id)
        write_json(self.root / "index" / f"browser-{digest(spec)}.json", index)

    def lookup(self, spec, consumer, run_id, *, compatible=None, provenance=None):
        self.initialize()
        key = digest(spec)
        with file_lock(self.root / "locks/control.lock"):
            try:
                index = read_json(self.root / "index" / f"browser-{key}.json")
                record = self._object_metadata(index["object_id"])
                if not self._index_matches(index, spec, record):
                    raise PolicyViolation("Shared browser index belongs to another driver")
                record = self.object(record["object_id"])
            except (OSError, ValueError, KeyError, TypeError, PolicyViolation):
                record = None
            if record:
                aliased = record["spec_key"] != key
                evidence = index.get("provenance") if aliased else record.get("provenance")
                reuse = index.get("reuse") if aliased else None
                installation = index["proof"]["installation"] if aliased else None
                self._register_browser(spec, record, consumer, run_id,
                    provenance=evidence, reuse=reuse, installation=installation)
                return {**record, "hit": True, "requested_spec_key": key, "reuse": reuse}
            if compatible is None:
                return None
            # Read small registered metadata first. Never hash unrelated browser
            # payloads or accept an index alias without its source/content proof.
            for path in sorted((self.root / "index").glob("browser-*.json")):
                try:
                    index = read_json(path)
                    record = self._object_metadata(index["object_id"])
                    previous_spec = index.get("spec", record["spec"])
                    if (path.name != f"browser-{digest(previous_spec)}.json"
                            or not self._index_matches(index, previous_spec, record)
                            or browser_payload_spec(previous_spec) != browser_payload_spec(spec)):
                        continue
                    witness = self._source_witness(index, record)
                    if not witness or not compatible(witness["spec"], witness["provenance"]):
                        continue
                    record = self.object(record["object_id"])
                except (OSError, ValueError, KeyError, TypeError, PolicyViolation):
                    continue
                reuse = "official-download-plan-v1"
                self._register_browser(spec, record, consumer, run_id, provenance=provenance, reuse=reuse,
                                       installation=witness)
                return {**record, "hit": True, "requested_spec_key": key, "reuse": reuse}
            return None

    def publish(self, spec, source, consumer, run_id, *, provenance=None):
        self.initialize()
        before = manifest_tree(source)
        content_hash = digest(before)
        # The requested source has already produced this complete validated tree.
        # Equal full bytes/types/executable bits, plus every non-source identity
        # input, can reuse storage even when a custom mirror was requested. This
        # proof does not authorize a pre-download cross-mirror hit.
        with file_lock(self.root / "locks/control.lock"):
            for path in sorted((self.root / "objects").iterdir()):
                try:
                    record = self._object_metadata(path.name)
                    if (record.get("content_hash") != content_hash or record.get("files") != before
                            or browser_payload_spec(record["spec"]) != browser_payload_spec(spec)
                            or (record["spec_key"] != digest(spec) and not self._valid_provenance(provenance, spec))):
                        continue
                    record = self.object(path.name)
                except (OSError, ValueError, KeyError, TypeError, PolicyViolation):
                    continue
                if manifest_tree(source) != before:
                    raise PolicyViolation("Browser installation changed while comparing published content")
                reuse = "verified-content-v1"
                self._register_browser(spec, record, consumer, run_id, provenance=provenance, reuse=reuse,
                                       installation=self._installation_witness(spec, record, provenance))
                return {**record, "hit": True, "requested_spec_key": digest(spec), "reuse": reuse}
        object_id = digest([content_hash, identifier("browser")])
        stage = self.root / "staging" / object_id
        stage.mkdir(mode=0o700)
        shutil.copytree(source, stage / "browser", symlinks=True)
        copied = manifest_tree(stage / "browser")
        if before != copied or manifest_tree(source) != before:
            raise PolicyViolation("Browser installation changed while publishing; staging retained")
        record = {"version": 1, "object_id": object_id, "spec_key": digest(spec), "spec": spec,
                  "content_hash": content_hash, "files": before, "created": time.time(),
                  "logical_bytes": sum(row[3] for row in before if row[1] == "file"), "provenance": provenance}
        write_json(stage / "manifest.json", record)
        immutable(stage / "browser")
        (stage / "manifest.json").chmod(0o444)
        target = self.root / "objects" / object_id
        with file_lock(self.root / "locks/control.lock"):
            os.rename(stage, target)
            target.chmod(0o555)
            self._register_browser(spec, record, consumer, run_id, provenance=provenance)
        return {**record, "path": str(target / "browser"), "hit": False, "requested_spec_key": digest(spec)}

    def binding(self, workspace, record):
        return {"version": 1, "workspace": str(workspace), "object_id": record["object_id"],
                "content_hash": record["content_hash"], "spec_key": record["spec_key"], "path": record["path"]}

    def save_binding(self, workspace, record):
        path = self.config.project / ".aa2/browser-bindings" / (digest(str(workspace)) + ".json")
        write_json(path, self.binding(workspace, record))

    def browser_reads(self, workspace):
        if not self.enabled:
            return []
        selected = []
        for binding in (self.config.project / ".aa2/browser-bindings").glob("*.json"):
            if not ID.fullmatch(binding.stem):
                continue
            value = read_json(binding)
            work = Path(value["workspace"])
            state = self.config.project / ".aa2"
            if not work.is_dir() or (not workspace.is_relative_to(state) and work.is_relative_to(state)):
                continue
            # A nested check cwd may consume a binding above it; unrelated workspaces do not.
            if not (work == workspace or work.is_relative_to(workspace) or workspace.is_relative_to(work)):
                continue
            alias = work / "node_modules/playwright-core/.local-browsers"
            if not alias.exists() and not alias.is_symlink():
                # Removed packages grant no mount. A still-required browser will
                # fail readiness/checks and must be prepared normally again.
                continue
            if not alias.is_symlink() or str(alias.resolve()) != value["path"]:
                raise PolicyViolation("Project browser binding no longer matches its installed entry")
            record = self.object(value["object_id"])
            if any(value[key] != record[key] for key in ("path", "content_hash", "spec_key")):
                raise PolicyViolation("Project browser binding identity changed")
            selected.append(Path(record["path"]))
        return sorted(set(selected))

    def facts(self, workspace):
        facts = []
        for path in self.browser_reads(workspace):
            record = read_json(path.parent / "manifest.json")
            facts.append({"path": str(path), "object_id": record["object_id"],
                "content_hash": record["content_hash"], "spec_key": record["spec_key"],
                "driver_version": record["spec"]["driver_version"], "driver_hash": record["spec"]["driver_hash"],
                "families": record["spec"]["families"], "browser_manifest": record["spec"]["browsers"],
                "components": sorted({row[0].split("/")[0] for row in record["files"]})})
        return facts

    def _reference_live(self, reference):
        """Unknown, renamed, corrupt, paused and unconfirmed states remain pinned."""
        try:
            if reference.get("pending", True):
                return True
            project = Path(reference["project"])
            consumer = Path(reference["consumer"])
            if (not project.is_absolute() or project.resolve() != project
                    or not consumer.is_relative_to(project)):
                return True
            database = project / ".aa2/state.sqlite"
            if database.resolve() != database or not database.is_file():
                return True
            if any((project / ".aa2/processes").glob("*.json")):
                return True
            if consumer.is_symlink() and consumer.resolve() == self.root / "objects" / reference["object_id"] / "browser":
                return True
            if consumer == project / ".aa2/browser-bindings/controller.json" and consumer.is_file():
                binding = read_json(consumer)
                if binding.get("object_id") == reference["object_id"]:
                    return True
            if reference.get("run_id"):
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                    from .errors import AA2Error
                    from .state_connection import require_format
                    try:
                        require_format(connection)
                    except (AA2Error, sqlite3.Error):
                        return True  # unknown/old formats conservatively keep shared cache pinned
                    row = connection.execute("SELECT status FROM runs WHERE id=?", (reference["run_id"],)).fetchone()
                if not row or row[0] != "SUCCEEDED":
                    return True
            elif consumer.exists():
                return True
            return False
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return True

    def collect(self, *, dry_run=True, ttl_seconds=7 * 86400):
        self.initialize()
        result = {"root": str(self.root), "dry_run": dry_run, "removed": [], "eligible": [], "protected": [], "busy": []}
        with file_lock(self.root / "locks/control.lock"):
            referenced, unknown = set(), False
            for file in (self.root / "references").glob("*.json"):
                try:
                    reference = read_json(file)
                    if not ID.fullmatch(reference["object_id"]):
                        raise ValueError("Invalid reference")
                    if self._reference_live(reference):
                        referenced.add(reference["object_id"])
                    elif not dry_run:
                        file.unlink()
                except (OSError, ValueError, KeyError, TypeError):
                    unknown = True
            for path in (self.root / "objects").iterdir():
                if not ID.fullmatch(path.name) or path.is_symlink():
                    result["protected"].append(path.name)
                    continue
                try:
                    record = self.object(path.name)
                    expired = time.time() - record["created"] > ttl_seconds
                except (OSError, ValueError, KeyError, TypeError, PolicyViolation):
                    expired = False
                if unknown or path.name in referenced or not expired:
                    result["protected"].append(path.name)
                    continue
                result["eligible"].append(str(path))
                if not dry_run:
                    remove_object(path, self.root)
                    result["removed"].append(str(path))
            # Download caches are disposable only under their installer locks.
            for kind in sorted(DOWNLOAD_KINDS):
                path = self.root / "downloads" / kind
                if not path.exists():
                    continue
                try:
                    with file_lock(self.root / "locks" / f"download-{kind}.lock", blocking=False):
                        stamp = read_json(self.root / "index" / f"download-{kind}.json")["touched"]
                        if time.time() - stamp <= ttl_seconds:
                            continue
                        local_directory(path)
                        result["eligible"].append(str(path))
                        if not dry_run:
                            shutil.rmtree(path)
                            result["removed"].append(str(path))
                except BusyProject:
                    result["busy"].append(kind)
                except (OSError, ValueError, KeyError, TypeError, PolicyViolation):
                    result["protected"].append(kind)
        return result
