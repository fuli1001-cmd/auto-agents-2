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

    def object(self, object_id):
        if not isinstance(object_id, str) or not ID.fullmatch(object_id):
            raise PolicyViolation("Invalid browser object identity")
        path = self.root / "objects" / object_id
        local_directory(self.root, private=True)
        local_directory(path)
        record = read_json(path / "manifest.json")
        if (record.get("version") != 1 or record.get("object_id") != object_id
                or record.get("spec_key") != digest(record.get("spec"))):
            raise PolicyViolation("Browser object metadata is inconsistent")
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

    def lookup(self, spec, consumer, run_id):
        self.initialize()
        key = digest(spec)
        with file_lock(self.root / "locks/control.lock"):
            try:
                index = read_json(self.root / "index" / f"browser-{key}.json")
                record = self.object(index["object_id"])
                if record["spec_key"] != key:
                    raise PolicyViolation("Shared browser index belongs to another driver")
            except (OSError, ValueError, KeyError, TypeError, PolicyViolation):
                return None
            self.reference(record, consumer, run_id)
            write_json(self.root / "index" / f"browser-{key}.json", {"object_id": record["object_id"], "touched": time.time()})
            return {**record, "hit": True}

    def publish(self, spec, source, consumer, run_id):
        self.initialize()
        before = manifest_tree(source)
        content_hash = digest(before)
        object_id = digest([content_hash, identifier("browser")])
        stage = self.root / "staging" / object_id
        stage.mkdir(mode=0o700)
        shutil.copytree(source, stage / "browser", symlinks=True)
        copied = manifest_tree(stage / "browser")
        if before != copied or manifest_tree(source) != before:
            raise PolicyViolation("Browser installation changed while publishing; staging retained")
        record = {"version": 1, "object_id": object_id, "spec_key": digest(spec), "spec": spec,
                  "content_hash": content_hash, "files": before, "created": time.time(),
                  "logical_bytes": sum(row[3] for row in before if row[1] == "file")}
        write_json(stage / "manifest.json", record)
        immutable(stage / "browser")
        (stage / "manifest.json").chmod(0o444)
        target = self.root / "objects" / object_id
        with file_lock(self.root / "locks/control.lock"):
            os.rename(stage, target)
            target.chmod(0o555)
            self.reference(record, consumer, run_id)
            write_json(self.root / "index" / f"browser-{record['spec_key']}.json",
                       {"object_id": object_id, "touched": time.time()})
        return {**record, "path": str(target / "browser"), "hit": False}

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
