from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .diagnostics import LOG_KINDS, references
from .util import atomic_write, digest, identifier


class ArtifactStore:
    """Immutable content-addressed objects; the controller alone publishes references."""

    def __init__(self, root: Path):
        self.root = root / "artifacts"
        from .store import Store
        self.store = Store(root.parent)

    def put(self, value: str | bytes | dict | list, kind: str = "document") -> dict:
        text = value if isinstance(value, (str, bytes)) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        sha = digest(text)
        path = self.root / sha[:2] / sha
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT id FROM runs WHERE status NOT IN ('SUCCEEDED','CANCELLED')").fetchone()
            if not path.exists():
                atomic_write(path, text)
            db.execute("CREATE TABLE IF NOT EXISTS artifact_edges(parent TEXT, child TEXT, PRIMARY KEY(parent,child))")
            def link(value):
                if isinstance(value, dict):
                    child = value.get("hash")
                    if isinstance(child, str) and len(child) == 64 and all(c in "0123456789abcdef" for c in child):
                        db.execute("INSERT OR IGNORE INTO artifact_edges VALUES(?,?)", (sha, child))
                    for item in value.values():
                        link(item)
                elif isinstance(value, list):
                    for item in value:
                        link(item)
            link(value)
            db.execute("INSERT OR IGNORE INTO artifact_owners VALUES(?,?)", (sha, run[0] if run else "shared"))
            ref = self._register(db, sha, kind, len(text if isinstance(text, bytes) else text.encode()), run)
        return ref

    def put_file(self, source: Path, kind="binary") -> dict:
        from .workspace import file_digest
        sha = file_digest(source)
        destination = self.root / sha[:2] / sha
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=destination.parent)
                try:
                    with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
                        shutil.copyfileobj(incoming, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    if file_digest(Path(temporary)) != sha:
                        raise ValueError("Artifact changed during capture")
                    os.replace(temporary, destination)
                    directory = os.open(destination.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            run = db.execute("SELECT id FROM runs WHERE status NOT IN ('SUCCEEDED','CANCELLED')").fetchone()
            db.execute("INSERT OR IGNORE INTO artifact_owners VALUES(?,?)", (sha, run[0] if run else "shared"))
            ref = self._register(db, sha, kind, destination.stat().st_size, run)
        return ref

    def _register(self, db, sha, kind, size, run):
        db.execute("INSERT OR IGNORE INTO artifact_kinds VALUES(?,?)", (sha, kind))
        db.execute("DELETE FROM retired_diagnostics WHERE hash=?", (sha,))
        ref = {"hash": sha, "kind": kind, "bytes": size}
        if kind in LOG_KINDS:
            lease = identifier("diagnostic")
            db.execute("INSERT INTO diagnostic_holds VALUES(?,?,?,?)", (lease, sha, run[0] if run else None, "producer"))
            ref["lease_id"] = lease
        return ref

    def released(self, ref):
        if ref.get("kind") not in LOG_KINDS:
            return False
        sha = ref["hash"]
        if (self.root / sha[:2] / sha).is_file():
            return False
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM retired_diagnostics WHERE hash=?", (sha,)).fetchone())

    @contextmanager
    def reading(self, value, run_id):
        """Pin a model invocation's evidence until its consumer finishes or is cancelled."""
        leases, queue, visited = [], list(references(value)), set()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            while queue:
                ref = queue.pop()
                sha = ref["hash"]
                if sha in visited:
                    continue
                visited.add(sha)
                if ref.get("kind") in LOG_KINDS:
                    if (self.root / sha[:2] / sha).is_file():
                        lease = identifier("reader")
                        leases.append(lease)
                        db.execute("INSERT INTO diagnostic_holds VALUES(?,?,?,?)", (lease, sha, run_id, "reader"))
                        db.execute("DELETE FROM retired_diagnostics WHERE hash=?", (sha,))
                elif db.execute("SELECT 1 FROM artifact_edges WHERE parent=?", (sha,)).fetchone():
                    try:
                        queue.extend(references(self.json(ref)))
                    except (OSError, ValueError, UnicodeError):
                        pass
        try:
            yield
        finally:
            with self.store.connect() as db:
                db.executemany("DELETE FROM diagnostic_holds WHERE id=?", [(lease,) for lease in leases])

    def read(self, ref: dict) -> str:
        sha = ref["hash"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid artifact address")
        data = (self.root / sha[:2] / sha).read_bytes()
        if digest(data) != sha:
            raise ValueError("Artifact integrity check failed")
        return data.decode()

    def bytes(self, ref: dict) -> bytes:
        sha = ref["hash"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid artifact address")
        data = (self.root / sha[:2] / sha).read_bytes()
        if digest(data) != sha:
            raise ValueError("Artifact integrity check failed")
        return data

    def json(self, ref: dict):
        return json.loads(self.read(ref))
