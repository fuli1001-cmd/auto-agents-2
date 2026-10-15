"""Copy a real paused/waiting project for opt-in recovery validation; no model calls.

Only infrastructure paths are relocated. Original SQLite bytes are retained outside
the copy. Approvals, budgets, source, refs, proposals and check outcomes are preserved.
Use the normal aa2 CLI on the printed project to perform the real recovery run.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from auto_agents_2.workspace import git, tree_hash


def snapshot(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if not destination.is_relative_to(Path(tempfile.gettempdir()).resolve()) or destination.exists():
        raise ValueError("Destination must be a new temporary project, outside the source")
    if destination.is_relative_to(source):
        raise ValueError("Snapshot cannot be nested in its source")
    destination.mkdir(parents=True)
    for path in source.iterdir():
        if path.name == ".aa2":
            continue
        subprocess.run(["cp", "-a", "--reflink=auto", str(path), str(destination / path.name)], check=True)
    state = destination / ".aa2"
    state.mkdir(mode=0o700)
    for name in ("artifacts", "worktrees", "cache", "environment-records", "node-records", "visuals", "config.json"):
        path = source / ".aa2" / name
        if path.exists():
            subprocess.run(["cp", "-a", "--reflink=auto", str(path), str(state / name)], check=True)
    database = source / ".aa2/state.sqlite"
    for _ in range(5):
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
            continue
        raw = database.read_bytes()
        if raw == database.read_bytes() and not any(Path(str(database) + s).exists() for s in ("-wal", "-journal")):
            break
    else:
        raise RuntimeError("Source is changing; pause it before taking a snapshot")
    original = destination.parent / (destination.name + "-original-state.sqlite")
    original.write_bytes(raw)
    (state / "state.sqlite").write_bytes(raw)

    def relocate(value):
        if isinstance(value, str):
            return value.replace(str(source), str(destination))
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, dict):
            return {relocate(k): relocate(v) for k, v in value.items()}
        return value

    with sqlite3.connect(state / "state.sqlite") as db:
        # Rewrite only string columns in the copy, leaving JSON numbers/booleans
        # and approval/source/evidence digests unchanged.
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            for row in db.execute(f'SELECT rowid,* FROM "{table}"').fetchall():
                updates = {column: value.replace(str(source), str(destination))
                           for column, value in zip(columns, row[1:])
                           if isinstance(value, str) and str(source) in value}
                if updates:
                    assignments = ','.join(f'"{column}"=?' for column in updates)
                    db.execute(f'UPDATE "{table}" SET {assignments} WHERE rowid=?', [*updates.values(), row[0]])
        rid = db.execute("SELECT id FROM runs ORDER BY created DESC LIMIT 1").fetchone()[0]
    for name in ("environment-records", "node-records"):
        for path in (state / name).rglob("*.json"):
            path.write_text(json.dumps(relocate(json.loads(path.read_text())), ensure_ascii=False))
    configuration = state / "config.json"
    configuration.write_text(json.dumps(relocate(json.loads(configuration.read_text())), ensure_ascii=False))
    pointers = list((destination / ".git/worktrees").glob("*/gitdir"))
    pointers += list((state / "worktrees").glob("*/*/.git"))
    for path in pointers:
        path.write_text(path.read_text().replace(str(source), str(destination)))
    for path in (state / "worktrees").glob("*/*"):
        if not (path / ".git").is_file():
            continue
        previous = source / path.relative_to(destination)
        if tree_hash(previous) != tree_hash(path) or git(previous, "rev-parse", "HEAD") != git(path, "rev-parse", "HEAD"):
            raise RuntimeError("Source worktree changed while copying; retain both snapshots and retry")
    for root, directories, files in os.walk(state / "worktrees"):
        for name in [*directories, *files]:
            path = Path(root) / name
            if path.is_symlink() and str(source) in os.readlink(path):
                raise RuntimeError("Copied symlink still addresses the live project")
    result = {"source": str(source), "project": str(destination), "run_id": rid,
              "original_database": str(original), "database_hash": hashlib.sha256(raw).hexdigest(),
              "source_hash": tree_hash(source), "live_writes": 0}
    (destination.parent / (destination.name + "-snapshot.json")).write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.destination), indent=2))
