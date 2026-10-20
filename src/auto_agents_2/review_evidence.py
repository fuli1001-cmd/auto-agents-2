"""Durable, bounded ownership between check publication and independent review."""
from __future__ import annotations

from contextvars import ContextVar

from .artifacts import ArtifactStore
from .diagnostics import references
from .faults import StateConflict
from .util import digest

review_consumer = ContextVar("review_consumer", default=None)


def consumer_key(task=None, phase="final"):
    if task is not None:
        return "develop/review-evidence/" + task.id
    if phase in {"final", "reuse"}:
        return "verify/review-evidence"
    if phase == "reintegration":
        return "delivery/review-evidence"
    return None


def is_review_consumer(key):
    return key in {"verify/review-evidence", "delivery/review-evidence"} or key.startswith("develop/review-evidence/")


class ReviewEvidence:
    def __init__(self, store):
        self.store, self.artifacts = store, ArtifactStore(store.root)

    def retain(self, rid, key, checks, *, updates=None):
        """Publish the next consumer atomically with the producer's completed batch.

        Reader holds bridge reacquisition of existing observations against concurrent
        collection. Deliberately retired attachments require a real new observation;
        missing or corrupt evidence still raises, never masquerades as retirement.
        """
        with self.artifacts.reading(checks, rid):
            if any(self.artifacts.released(ref) for ref in references(checks)
                   if ref.get("kind") == "test_diagnostic"):
                return False
            self.store.checkpoints(rid, {**(updates or {}), key: {
                "phase": "pending", "observation": digest(checks), "checks": checks}})
        return True

    def complete(self, rid, key, checks, *, updates=None):
        """Consume only this observation after its review/issue handoff is durable.

        Result updates and release share a transaction. A stale completion cannot
        release a replacement observation. Consumed records retain no artifact roots.
        """
        import json
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
            current = json.loads(row[0]) if row else {}
            if current and current.get("observation") != digest(checks):
                raise StateConflict("Review consumer observation changed; preserve the replacement evidence")
            for name, value in (updates or {}).items():
                self.store._put(db, rid, name, value)
            if current.get("phase") == "pending":
                self.store._put(db, rid, key, {"phase": "consumed", "observation": current["observation"]})
                self.store._event(db, rid, "review.evidence_consumed", {"consumer": key, "observation": current["observation"]})
