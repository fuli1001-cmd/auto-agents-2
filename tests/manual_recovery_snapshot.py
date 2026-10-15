"""Copy a real paused/waiting project for opt-in recovery validation; no model calls.

Only infrastructure paths are relocated. Original SQLite bytes are retained outside
the copy. Approvals, budgets, source, refs, proposals and check outcomes are preserved.
Use the normal aa2 CLI on the printed project to perform the real recovery run.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from auto_agents_2.util import digest
from auto_agents_2.workspace import git, tree_hash

# These are mutable controller addresses, never proposal text or signed evidence.
CHECKPOINT_PATHS = {
    "develop/task/*": ["workspace", "previous_evidence.workspace"],
    "recovery/task-baselines/*": ["workspace", "repair_workspace", "previous_task.workspace"],
    "plan_revision/journal/*": ["snapshots.*.workspace"],
    "delivery/journal": ["work", "environment.prefix"],
    "delivery/environment-recovery": ["work", "environment.prefix"],
    "candidate/verification-recovery/*": ["workspace"],
    "candidate/build-recovery/*": ["workspace"],
    "candidate/approval-recovery/*": ["workspace"],
    "prototype_approval": ["visuals.*.screenshot"],
    "candidate/prototype-approval-checks/*": ["visuals.*.screenshot"],
    "verify/visuals": ["*.screenshot"],
}


def validate_copy(root):
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in [*folders, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                if Path(os.readlink(path)).is_absolute() or not path.resolve().is_relative_to(root):
                    raise ValueError("Snapshot contains an external or absolute symlink; source remains untouched")
    if (root / ".git").is_file():
        raise ValueError("Snapshot source must own its Git metadata, not reference an external repository")
    alternates = root / ".git/objects/info/alternates"
    if alternates.exists() and alternates.read_text().strip():
        raise ValueError("External Git object databases cannot be safely relocated")
    variables = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    variables.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    for config in [root / ".git/config", root / ".git/config.worktree", *(root / ".git/worktrees").glob("*/config.worktree")]:
        if not config.exists():
            continue
        result = subprocess.run(["git", "config", "--file", str(config), "--no-includes", "--get-regexp",
                                 r"^(core\.worktree|include\.path|includeif\..*\.path)$"],
                                env=variables, capture_output=True)
        if result.returncode != 1:
            raise ValueError("Custom Git worktree/config indirection requires explicit snapshot support")


def safe_write(path, root, text):
    if not path.is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
        raise ValueError("Refusing to write through a copied symlink")
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as output:
        output.write(text)


def snapshot(source, destination):
    source = source.resolve()
    if (source / ".aa2").is_symlink():
        raise ValueError("Source state must be a local directory")
    # Do not resolve the final component: an occupied symlink is an occupied name.
    destination = destination.parent.resolve() / destination.name
    if (not destination.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or destination.exists() or destination.is_symlink() or destination.is_relative_to(source)):
        raise ValueError("Destination must be a new temporary project, outside the source")
    original = destination.parent / (destination.name + "-original-state.sqlite")
    manifest = destination.parent / (destination.name + "-snapshot.json")
    handles = []
    try:
        for path in (original, manifest):
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            handles.append((path, os.fdopen(fd, "wb")))
    except BaseException:
        for path, handle in handles:
            handle.close()
            path.unlink()  # Only the empty reservation created by this invocation.
        raise
    try:
        source_hash = tree_hash(source)
        destination.mkdir(mode=0o700)
        for path in source.iterdir():
            if path.name != ".aa2":
                subprocess.run(["cp", "-a", "--reflink=auto", str(path), str(destination / path.name)], check=True)
        state = destination / ".aa2"
        state.mkdir(mode=0o700)
        for name in ("artifacts", "worktrees", "cache", "environment-records", "node-records", "visuals", "config.json"):
            path = source / ".aa2" / name
            if path.exists() or path.is_symlink():
                subprocess.run(["cp", "-a", "--reflink=auto", str(path), str(state / name)], check=True)
        validate_copy(destination)  # Before opening any copied path for writing.
        database = source / ".aa2/state.sqlite"
        if database.is_symlink() or (source / ".aa2").is_symlink():
            raise ValueError("Source state must be a local regular database")
        for _ in range(5):
            if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
                continue
            raw = database.read_bytes()
            if raw == database.read_bytes() and not any(Path(str(database) + s).exists() for s in ("-wal", "-journal")):
                break
        else:
            raise RuntimeError("Source is changing; pause it before taking a snapshot")
        handles[0][1].write(raw)
        handles[0][1].flush()
        os.fsync(handles[0][1].fileno())
        with (state / "state.sqlite").open("xb") as output:
            output.write(raw)

        def relocate(value, *, external=False, relative_base=None):
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError("Unsupported infrastructure path shape")
            path = Path(value)
            if not path.is_absolute():
                if relative_base is None:
                    if external:
                        return value  # Executable names and profile selectors are not mutable locators.
                    raise ValueError("Mutable snapshot paths must be absolute")
                path = relative_base / path
            path = path.resolve()
            if path.is_relative_to(source):
                relocated = destination / path.relative_to(source)
            elif path.is_relative_to(destination):
                relocated = path
            elif external:
                return value
            else:
                raise ValueError("External mutable snapshot path cannot be safely relocated")
            if not relocated.resolve().is_relative_to(destination):
                raise ValueError("Mutable snapshot path escapes the recovery copy")
            return str(relocated)

        def field(value, pattern, *, external=False):
            first, *rest = pattern.split(".")
            if first == "*":
                values = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else []
                for child in values:
                    field(child, ".".join(rest), external=external)
            elif isinstance(value, dict) and first in value:
                if rest:
                    field(value[first], ".".join(rest), external=external)
                else:
                    value[first] = relocate(value[first], external=external)

        with closing(sqlite3.connect(state / "state.sqlite")) as db, db:
            current = db.execute("SELECT id,status FROM runs ORDER BY created DESC LIMIT 1").fetchone()
            if not current or current[1] not in {"PAUSED", "WAITING_USER"}:
                raise ValueError("Snapshot requires a paused or waiting run")
            rid = current[0]
            for table in ("resources", "delivery_pins"):
                for rowid, path in db.execute(f'SELECT rowid,path FROM "{table}"').fetchall():
                    db.execute(f'UPDATE "{table}" SET path=? WHERE rowid=?', (relocate(path), rowid))
            for owner, key, data in db.execute("SELECT run_id,key,data FROM checkpoints").fetchall():
                value = json.loads(data)
                fields = [p for pattern, paths in CHECKPOINT_PATHS.items() if fnmatch.fnmatchcase(key, pattern) for p in paths]
                if not fields:
                    continue
                for pattern in fields:
                    field(value, pattern)
                new_key = key
                if key.startswith("recovery/task-baselines/") and isinstance(value, dict) and "workspace" in value and "base" in value:
                    new_key = key.rsplit("/", 1)[0] + "/" + digest([value["workspace"], value["base"]])[:16]
                db.execute("UPDATE checkpoints SET key=?,data=? WHERE run_id=? AND key=?",
                           (new_key, json.dumps(value, ensure_ascii=False), owner, key))
        # Immutable artifact bodies, events, questions, receipts and run provenance
        # are never rewritten. Only an instance locator is mutable here.
        for path in (state / "environment-records").glob("*/instance.json"):
            value = json.loads(path.read_text())
            field(value, "prefix")
            safe_write(path, destination, json.dumps(value, ensure_ascii=False))
        configuration = state / "config.json"
        settings = json.loads(configuration.read_text())
        for pattern in ("providers.*.binary", "providers.*.environment.CODEX_HOME", "providers.*.environment.GEMINI_CLI_HOME",
                        "providers.*.profile_map.*.file", "environment.conda"):
            field(settings, pattern, external=True)
        safe_write(configuration, destination, json.dumps(settings, ensure_ascii=False))
        pointers = list((destination / ".git/worktrees").glob("*/gitdir"))
        pointers += list((state / "worktrees").glob("*/*/.git"))
        pointers += list((destination / ".git/worktrees").glob("*/commondir"))
        for path in pointers:
            text = path.read_text().strip()
            prefix = "gitdir: " if text.startswith("gitdir: ") else ""
            target = text.removeprefix(prefix)
            relocated = relocate(target, relative_base=source / path.parent.relative_to(destination))
            if not (path.parent / relocated).resolve().is_relative_to(destination):
                raise ValueError("Git pointer references an external repository")
            safe_write(path, destination, prefix + relocated + "\n")
        for path in (state / "worktrees").glob("*/*"):
            if not (path / ".git").is_file():
                continue
            previous = source / path.relative_to(destination)
            if tree_hash(previous) != tree_hash(path) or git(previous, "rev-parse", "HEAD") != git(path, "rev-parse", "HEAD"):
                raise RuntimeError("Source worktree changed while copying; retain both snapshots and retry")
        if database.read_bytes() != raw or any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise RuntimeError("Source changed during snapshot; copied project is not certified for recovery")
        validate_copy(destination)
        if tree_hash(source) != source_hash or tree_hash(destination) != source_hash:
            raise RuntimeError("Project source changed during snapshot; retain the copy for investigation")
        result = {"source": str(source), "project": str(destination), "run_id": rid,
                  "original_database": str(original), "database_hash": hashlib.sha256(raw).hexdigest(),
                  "source_hash": source_hash, "live_writes": 0}
        handles[1][1].write(json.dumps(result, indent=2).encode())
        handles[1][1].flush()
        os.fsync(handles[1][1].fileno())
        return result
    finally:
        for _, handle in handles:
            handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.destination), indent=2))
