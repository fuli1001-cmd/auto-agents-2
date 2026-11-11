"""Application continuity for required DESIGN updates, never semantic acceptance."""
from __future__ import annotations

from .prototype_reentry import PrototypeReentry
from .util import digest


class PrototypeDesign:
    def __init__(self, engine):
        self.engine, self.store, self.artifacts = engine, engine.store, engine.artifacts

    def _record(self, rid, work, specification, pending):
        brief = self.store.get(rid, "ui_design/brief")
        rows = [row for row in pending.get("prepared", []) if row.get("path") == "DESIGN.md"]
        if (not brief or not pending.get("fingerprint") or len(rows) != 1
                or rows[0].get("operation") == "delete" or not rows[0].get("after")
                or rows[0]["after"] != digest(rows[0]["content"])
                or not any(row.get("path") == "DESIGN.md" for row in pending["document"]["files"])
                or self.store.get(rid, "clarify") != specification):
            return None
        return {"version": 1, "run_id": rid, "workspace": str(work),
                "specification": digest(specification), "brief": digest(brief),
                "candidate": digest(pending["document"]), "prepared": digest(pending["prepared"]),
                "identity": pending["identity"], "fingerprint": pending["fingerprint"],
                "design_after": rows[0]["after"]}

    def record_application(self, rid, work, specification, pending):
        # Called only after controller application, in the fingerprint checkpoint.
        # Never backfill a receipt from an old fingerprint or file existence.
        record = self._record(rid, work, specification, pending)
        path = work / "DESIGN.md"
        if record and not path.is_symlink() and path.is_file() and digest(path.read_bytes()) == record["design_after"]:
            pending["design_application"] = self.artifacts.put(record, "prototype-design-application")

    def synchronized(self, rid, work, specification):
        path = work / "DESIGN.md"
        if path.is_symlink() or not path.is_file():
            return False
        generation = self.store.run(rid)["data"].get("replans", 0) - 1
        reentry = PrototypeReentry(self.engine)
        feedback = self.store.get(rid, "replan_feedback", {})
        latest = self.store.get(rid, f"recovery/gate-history/{generation}", {})
        if not reentry._transition(rid, work, latest, generation, feedback):
            return False
        # Only completed, contiguous controller resets can carry application
        # evidence. Stop at the most recent DESIGN application, including legacy
        # or damaged records; an older receipt cannot conceal a later change.
        for index in range(generation, -1, -1):
            key = f"recovery/gate-history/{index}"
            history = self.store.get(rid, key, {})
            transition = history.get("pending", {})
            historical_feedback = {name: transition.get(name) for name in ("stage", "reason", "correction_request")}
            if not reentry._transition(rid, work, history, index, historical_feedback):
                return False
            pending = history.get("checkpoints", {}).get("prototype/pending", {})
            if not any(row.get("path") == "DESIGN.md" for row in pending.get("prepared", [])):
                continue
            reference = pending.get("design_application")
            expected = self._record(rid, work, specification, pending)
            if not reference or not expected:
                return False
            # Validate the complete original author receipt as well as the
            # content-addressed controller application, not arbitrary proposals.
            reentry._candidate(key, history, [])
            record = self.artifacts.json(reference)
            return record == expected and digest(path.read_bytes()) == record["design_after"]
        return False
