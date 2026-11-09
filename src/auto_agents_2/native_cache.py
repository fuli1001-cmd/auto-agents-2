"""Disposable discovery caches inside otherwise persistent native sessions.

Only the built-in, observed Codex layout has a contract. Session state, native
configuration and unknown caches are never this collector's deletion targets.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .artifacts import ArtifactStore
from .errors import AA2Error
from .safe_files import directory_fd
from .state_codec import checkpoint_query_db
from .util import digest, file_lock, now

VERSION = 1
LAYOUT = "codex-0.163.0-discovery-v1"
MAX_FILES = 256
MAX_CLASS_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
FILES = {
    "remote_plugin_catalog": re.compile(r"[0-9a-f]{16}\.json"),
    "codex_apps_tools": re.compile(r"[0-9a-f]{40}\.json"),
    "codex_apps_server_info": re.compile(r"[0-9a-f]{40}\.json"),
}


def observed_layout(transport, version, binary_hash):
    from .transports import CodexTransport
    if isinstance(version, bytes):
        version = version.decode("utf-8", errors="replace")
    if (type(transport) is CodexTransport and version.strip() == "codex-cli 0.163.0"
            and re.fullmatch(r"[0-9a-f]{64}", binary_hash)):
        return {"id": LAYOUT, "cli_version": "0.163.0", "binary_sha256": binary_hash}
    return None


def _directory(info):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise ValueError("Unowned or writable native cache directory")
    return [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)]


def _file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise ValueError("Native cache leaf is not an owned ordinary file")
    return [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode),
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def _cache(root, expected_root=None, expected_cache=None):
    with directory_fd(root) as parent:
        root_id = _directory(os.fstat(parent))
        if expected_root is not None and root_id != expected_root:
            raise ValueError("Native session root changed")
        fd = os.open("cache", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            cache_id = _directory(os.fstat(fd))
            if cache_id[0] != root_id[0] or expected_cache is not None and cache_id != expected_cache:
                raise ValueError("Native cache root changed")
            yield fd, root_id, cache_id
        finally:
            os.close(fd)


def _names(fd):
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= MAX_FILES:
                raise ValueError("Native cache inventory exceeds bound")
            names.append(entry.name)
    return names


def _inventory(parent, name, pattern):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        identity = _directory(os.fstat(fd))
        if identity[0] != os.fstat(parent).st_dev:
            raise ValueError("Native cache changed filesystem")
        files, total = {}, 0
        for leaf in _names(fd):
            if not pattern.fullmatch(leaf):
                raise ValueError("Unknown native cache layout")
            info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            files[leaf] = _file(info)
            total += info.st_size
            limit = 64 * 1024 * 1024 if name == "remote_plugin_catalog" else 32 * 1024 * 1024
            if info.st_size > limit or total > MAX_CLASS_BYTES:
                raise ValueError("Native cache size exceeds supported layout")
            if info.st_dev != identity[0]:
                raise ValueError("Native cache leaf changed filesystem")
        return {"directory": identity, "files": files}
    finally:
        os.close(fd)


def _read(db, rid):
    row = db.execute("SELECT * FROM resources WHERE id=? AND state='live' AND kind='session' AND length(data)<=?",
                     (rid, MAX_MANIFEST_BYTES + 8192)).fetchone()
    if not row:
        return None, None
    data = json.loads(row["data"])
    if not isinstance(data, dict):
        raise ValueError("Unknown session resource metadata")
    return dict(row), data


def _write(db, row, data, metadata, event=None):
    data = {**data, "native_cache": metadata}
    db.execute("UPDATE resources SET data=? WHERE id=?", (json.dumps(data), row["id"]))
    if event:
        info = {
            "resource": row["id"], "attempt": metadata["attempt"],
            "layout": metadata.get("layout"), "phase": metadata["phase"],
            "manifest": digest(metadata.get("caches", {})),
            **event,
        }
        db.execute("INSERT INTO events(time,run_id,kind,data) VALUES(?,?,?,?)",
                   (now(), row["run_id"], "resources.session_cache", json.dumps(info)))


def begin(manager, root, run_id, invocation, layout):
    """Invalidate any previous seal before authentication refresh or native resume."""
    with file_lock(manager.store.root / "gc.lock"):
        with directory_fd(root, create=True):
            pass
        resource = manager.register(root, "session", run_id)
        with directory_fd(root) as fd:
            root_id = _directory(os.fstat(fd))
        metadata = {
            "version": VERSION, "phase": "active", "root": root_id,
            "layout": layout, **invocation,
        }
        with manager.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, data = _read(db, resource)
            if row is None:
                raise ValueError("Native session resource disappeared")
            _write(db, row, data, metadata)
        return {"resource": resource, "metadata": metadata}


def seal(manager, handle, process):
    """Controller receipt after durable response and confirmed process-tree join.

    This is not a fabricated supervisor 'cleaned' receipt. The supervisor protocol
    remains authoritative; unsuccessful or interrupted attempts cannot reach here.
    """
    original = handle["metadata"]
    if ((original.get("layout") or {}).get("id") != LAYOUT
            or not process.cleanup_confirmed or process.returncode != 0
            or process.supervisor.returncode != 0):
        return
    with file_lock(manager.store.root / "gc.lock"):
        with manager.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, data = _read(db, handle["resource"])
            if not row or data.get("native_cache") != original:
                return
            response = manager.store.get(row["run_id"], "provider-result/" + original["operation"])
            session = manager.store.get(row["run_id"], original["session_key"])
            if (not response or response.get("attempt") != original["attempt"]
                    or not isinstance(session, str) or not session):
                return
            ArtifactStore(manager.store.root).bytes(response["artifact"])
            root = Path(row["path"])
            caches, retained = {}, []
            try:
                with _cache(root, original["root"]) as (parent, _, cache_id):
                    for name, pattern in FILES.items():
                        try:
                            caches[name] = _inventory(parent, name, pattern)
                        except FileNotFoundError:
                            continue
                        except (OSError, ValueError):
                            retained.append(name)
            except FileNotFoundError:
                cache_id = None
            except (OSError, ValueError):
                return  # Preserve an unknown root; no cache body is read.
            metadata = {
                **original, "phase": "sealed", "session": session,
                "artifact": response["artifact"], "cache_root": cache_id,
                "caches": caches, "retained_layouts": retained,
                "joined": {"authority": "controller_after_confirmed_join",
                           "pid": process.pid, "record": str(process.record),
                           "returncode": process.returncode, "supervisor_returncode": process.supervisor.returncode,
                           "cleanup_confirmed": True},
            }
            if len(json.dumps(metadata).encode()) > MAX_MANIFEST_BYTES:
                return
            _write(db, row, data, metadata, {"cache_classes": sorted(caches), "retained_layouts": retained})


def _checkpoint(db, run_id, key):
    row = checkpoint_query_db(db, 'SELECT slot_order AS data FROM checkpoint_records WHERE run_id=? AND key=?', (run_id, key), wire_index=0).fetchone()
    return json.loads(row[0]) if row else None


def _eligible(manager, db, row, metadata):
    if (metadata.get("version") != VERSION or metadata.get("phase") not in {"sealed", "collecting"}
            or not isinstance(metadata.get("layout"), dict) or metadata["layout"].get("id") != LAYOUT
            or metadata["layout"].get("cli_version") != "0.163.0"
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["layout"].get("binary_sha256", ""))
            or not isinstance(metadata.get("joined"), dict)
            or metadata["joined"].get("authority") != "controller_after_confirmed_join"
            or metadata["joined"].get("cleanup_confirmed") is not True
            or metadata["joined"].get("returncode") != 0
            or metadata["joined"].get("supervisor_returncode") != 0
            or not isinstance(metadata.get("caches"), dict)
            or not metadata["caches"] or set(metadata["caches"]) - FILES.keys()
            or len(json.dumps(metadata).encode()) > MAX_MANIFEST_BYTES):
        return False
    root = Path(row["path"])
    relative = root.relative_to(manager.store.root / "sessions")
    if (len(relative.parts) != 3 or relative.parts[0] != row["run_id"]
            or not re.fullmatch(r"[0-9a-f]{16}", relative.parts[1])
            or not re.fullmatch(r"[0-9a-f]{20}", relative.parts[2])):
        return False
    run_id, operation = row["run_id"], metadata["operation"]
    response = _checkpoint(db, run_id, "provider-result/" + operation)
    invocation = _checkpoint(db, run_id, "invocation/" + operation)
    infra = _checkpoint(db, run_id, f"infra/{operation}/{metadata['provider']}")
    completion = invocation
    if isinstance(invocation, dict) and "provider_receipt" in invocation:
        completion = invocation["provider_receipt"]
        if not isinstance(completion, dict) or completion.get("version") != 1:
            return False
    if (not isinstance(response, dict) or response.get("attempt") != metadata["attempt"]
            or response.get("artifact") != metadata["artifact"]
            or not isinstance(invocation, dict) or invocation.get("artifact") != metadata["artifact"]
            or not isinstance(completion, dict) or completion.get("artifact") != metadata["artifact"]
            or completion.get("attempt") != metadata["attempt"]
            or completion.get("identity") != metadata["input_identity"]
            or completion.get("cleanup_record") != metadata["joined"].get("record")
            or not isinstance(infra, dict) or infra.get("pending") is not False
            or _checkpoint(db, run_id, metadata["session_key"]) != metadata["session"]):
        return False
    # Only this known parent session's long-lived reference is exempted. Other
    # resources/leases, pending process records and delivery pins still protect.
    protected = manager.held_paths(db) + manager.process_paths()
    protected += [Path(r[0]) for r in db.execute("SELECT path FROM delivery_pins")]
    for other in db.execute("SELECT id,path,refs FROM resources WHERE state!='removed' AND id!=?", (row["id"],)):
        path = Path(other["path"])
        if other["refs"] > 0 or path.is_relative_to(root):
            protected.append(path)
    targets = [root / "cache" / name for name in metadata["caches"]]
    if any(p.is_relative_to(target) or target.is_relative_to(p) for p in protected for target in targets):
        return False
    ArtifactStore(manager.store.root).bytes(metadata["artifact"])
    return True


def _verify_inventory(parent, name, planned, *, replay):
    try:
        actual = _inventory(parent, name, FILES[name])
    except FileNotFoundError:
        if replay:
            return False
        raise ValueError("Sealed native cache unexpectedly missing") from None
    if actual["directory"] != planned["directory"]:
        raise ValueError("Sealed native cache directory changed")
    if (not replay and actual["files"] != planned["files"]
            or replay and any(planned["files"].get(k) != v for k, v in actual["files"].items())):
        raise ValueError("Sealed native cache inventory changed")
    return True


def _remove(root, metadata, progress):
    with _cache(root, metadata["root"], metadata["cache_root"]) as (parent, _, _):
        for name, planned in metadata["caches"].items():
            if not _verify_inventory(parent, name, planned, replay=True):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for leaf in _names(fd):
                    info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                    if _file(info) != planned["files"].get(leaf):
                        raise ValueError("Native cache leaf changed before unlink")
                    # Revalidate the path identities while retaining the descriptors.
                    with _cache(root, metadata["root"], metadata["cache_root"]) as (fresh, _, _):
                        if _directory(os.stat(name, dir_fd=fresh, follow_symlinks=False)) != planned["directory"]:
                            raise ValueError("Native cache path changed before unlink")
                    os.unlink(leaf, dir_fd=fd)
                    os.fsync(fd)
                    progress["files_removed"] += 1
                    progress["bytes"] += info.st_blocks * 512
                if _names(fd):
                    raise ValueError("Native cache gained unknown files")
                if _directory(os.stat(name, dir_fd=parent, follow_symlinks=False)) != planned["directory"]:
                    raise ValueError("Native cache directory changed before removal")
                os.rmdir(name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)


def collect(manager, *, dry_run=False):
    """Called only while ResourceManager holds gc.lock; no native payload reads."""
    result = {"files_removed": 0, "bytes": 0, "candidates": [], "errors": [], "retained": 0}
    with manager.store.connect() as db:
        resources = [r[0] for r in db.execute("SELECT id FROM resources WHERE kind='session' AND state='live'")]
    for resource in resources:
        progress = {"files_removed": 0, "bytes": 0}
        try:
            with manager.store.connect() as db:
                old, data = _read(db, resource)
            metadata = data.get("native_cache", {}) if data else {}
            if not isinstance(metadata, dict) or metadata.get("phase") not in {"sealed", "collecting"}:
                continue
            with manager.store.connect() as db:
                db.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
                row, data = _read(db, old["id"])
                if not row or data.get("native_cache") != metadata or not _eligible(manager, db, row, metadata):
                    result["retained"] += 1
                    continue
                root = Path(row["path"])
                with _cache(root, metadata["root"], metadata["cache_root"]) as (parent, _, _):
                    for name, planned in metadata["caches"].items():
                        if name not in FILES:
                            raise ValueError("Unknown native cache contract target")
                        _verify_inventory(parent, name, planned, replay=metadata["phase"] == "collecting")
                candidates = [str(root / "cache" / name) for name in metadata["caches"]]
                result["candidates"].extend(candidates)
                if dry_run:
                    continue
                if metadata["phase"] == "sealed":
                    metadata = {**metadata, "phase": "collecting"}
                    _write(db, row, data, metadata, {"cache_classes": sorted(metadata["caches"])})
            # Intent is durable before any unlink. A crash preserves the exact
            # allowed manifest; replay accepts only its remaining unchanged files.
            with manager.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row, data = _read(db, old["id"])
                if not row or data.get("native_cache") != metadata or not _eligible(manager, db, row, metadata):
                    result["retained"] += 1
                    continue
                _remove(Path(row["path"]), metadata, progress)
                _write(db, row, data, {**metadata, "phase": "collected", "caches": {},
                                     "collected_manifest": digest(metadata["caches"])},
                       {"manifest": digest(metadata["caches"]), **progress})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, AA2Error) as exc:
            # The durable intent and remaining cache stay available for retry.
            result["errors"].append({"resource": resource, "error_type": type(exc).__name__})
        finally:
            result["files_removed"] += progress["files_removed"]
            result["bytes"] += progress["bytes"]
    return result
