"""Diagnostic consumers and availability, separate from mandatory verification reports."""
from __future__ import annotations

import json
import sqlite3
from contextvars import ContextVar

from .util import digest

LOG_KINDS = frozenset({"check_log", "dependency_log"})
current_diagnostics = ContextVar("aa2_diagnostic_operation", default=None)


def references(value):
    if isinstance(value, dict):
        sha = value.get("hash")
        if isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
            yield value
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def adopt_logs(db, root, value):
    """Transfer producer holds to a durable checkpoint in the caller's transaction."""
    queue, visited = list(references(value)), set()
    while queue:
        ref = queue.pop()
        sha = ref["hash"]
        if (root / "artifacts" / sha[:2] / sha).is_file():
            db.execute("DELETE FROM retired_diagnostics WHERE hash=?", (sha,))
        if ref.get("kind") in LOG_KINDS:
            if ref.get("lease_id"):
                db.execute("DELETE FROM diagnostic_holds WHERE id=? AND hash=? AND purpose='producer'",
                           (ref["lease_id"], sha))
        elif sha not in visited:
            visited.add(sha)
            if (db.execute("SELECT 1 FROM artifact_edges WHERE parent=?", (sha,)).fetchone()
                    or db.execute("SELECT 1 FROM artifact_owners WHERE hash=?", (sha,)).fetchone()):
                try:
                    data = (root / "artifacts" / sha[:2] / sha).read_bytes()
                    if digest(data) == sha:
                        queue.extend(references(json.loads(data)))
                except (OSError, ValueError, UnicodeError):
                    pass


class DiagnosticOperation:
    """Installer logs are needed through postcondition checks, not just exit code zero."""

    def __init__(self, store, resources, scope):
        self.store, self.resources = store, resources
        run = store.current(unfinished=True)
        self.run_id = run["id"] if run else None
        self.key = "environment/logs/" + digest(scope)
        self.logs = store.get(self.run_id, self.key, {}).get("logs", []) if self.run_id else []

    def __enter__(self):
        self.token = current_diagnostics.set(self)
        return self

    def capture(self, refs):
        self.logs.extend(refs)
        if self.run_id:
            self.store.checkpoint(self.run_id, self.key, {"logs": self.logs, "phase": "validating"})

    def __exit__(self, kind, value, traceback):
        current_diagnostics.reset(self.token)
        if self.run_id:
            if kind is None:
                self.store.invalidate(self.run_id, [self.key])
            else:
                try:
                    self.store.checkpoint(self.run_id, self.key, {"logs": self.logs, "phase": "incomplete"})
                except (OSError, sqlite3.Error):
                    value.add_note("Diagnostic checkpoint could not be persisted; original failure retained")
        elif kind is None:
            # Standalone installer use has no checkpoint consumer.
            with self.store.connect() as db:
                for ref in self.logs:
                    db.execute("DELETE FROM diagnostic_holds WHERE id=?", (ref.get("lease_id", ""),))
        if kind is None:
            self.resources.collect_artifacts_safely()
