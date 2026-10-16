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
        text = value if isinstance(value, (str, bytes)) else json.dumps(value, ensure_ascii=True, sort_keys=True)
        sha = digest(text)
        path = self.root / sha[:2] / sha
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT id FROM runs WHERE status NOT IN ('SUCCEEDED','CANCELLED')").fetchone()
            if not path.exists():
                atomic_write(path, text)
            elif digest(path.read_bytes()) != sha:
                raise ValueError("Existing artifact integrity check failed")
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
        with source.open("rb") as stream:
            return self.put_stream(stream, kind)

    def put_stream(self, incoming, kind="binary", *, max_bytes=None) -> dict:
        """Archive the already opened source without reopening an untrusted path."""
        from .workspace import file_digest
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.root) as captured:
            size = 0
            while chunk := incoming.read(min(1024 * 1024, max_bytes - size + 1) if max_bytes is not None else 1024 * 1024):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError("Artifact exceeds capture budget")
                captured.write(chunk)
            captured.seek(0)
            import hashlib
            sha = hashlib.file_digest(captured, "sha256").hexdigest()
            captured.seek(0)
            return self._publish_stream(captured, sha, kind, file_digest)

    def _publish_stream(self, incoming, sha, kind, file_digest):
        destination = self.root / sha[:2] / sha
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=destination.parent)
                try:
                    with os.fdopen(fd, "wb") as output:
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
            elif file_digest(destination) != sha:
                raise ValueError("Existing artifact integrity check failed")
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
        leases, queue, traversed, pinned = [], [(ref, True) for ref in references(value)], set(), set()
        retired_hashes = set()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            while queue:
                ref, declared = queue.pop()
                sha = ref["hash"]
                kinds = {row[0] for row in db.execute("SELECT kind FROM artifact_kinds WHERE hash=?", (sha,))}
                if not declared and not kinds and not db.execute("SELECT 1 FROM artifact_owners WHERE hash=?", (sha,)).fetchone():
                    continue  # Opaque research examples cannot invent required artifacts.
                retired = sha in retired_hashes
                if ref.get("kind") in LOG_KINDS or kinds & LOG_KINDS:
                    if sha not in pinned:
                        pinned.add(sha)
                        if (self.root / sha[:2] / sha).is_file():
                            self.bytes(ref)
                            lease = identifier("reader")
                            leases.append(lease)
                            db.execute("INSERT INTO diagnostic_holds VALUES(?,?,?,?)", (lease, sha, run_id, "reader"))
                            db.execute("DELETE FROM retired_diagnostics WHERE hash=?", (sha,))
                        elif not db.execute("SELECT 1 FROM retired_diagnostics WHERE hash=?", (sha,)).fetchone():
                            raise ValueError("Required diagnostic artifact is unavailable")
                        else:
                            retired = True
                            retired_hashes.add(sha)
                if retired and ref.get("kind") is not None and ref["kind"] not in LOG_KINDS:
                    raise ValueError("Required nondiagnostic artifact is unavailable")
                if sha in traversed or retired:
                    continue
                traversed.add(sha)
                edges = list(db.execute("SELECT child FROM artifact_edges WHERE parent=?", (sha,)))
                if not edges and kinds and kinds <= LOG_KINDS:
                    continue
                if not kinds and not edges and not ("kind" in ref and "bytes" in ref):
                    continue  # An inline content fingerprint is not an artifact reference.
                raw = self.bytes(ref)
                if raw.lstrip().startswith((b"{", b"[")) or edges:
                    try:
                        nested = json.loads(raw)
                    except (ValueError, UnicodeError):
                        if edges:
                            raise ValueError("Structured evidence dependencies cannot be verified") from None
                    else:
                        # Retention only. Provider exposure still requires registered edges.
                        queue.extend((child, bool(edges)) for child in references(nested))
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
