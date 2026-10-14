"""Controller-owned issue identity, distinct from changing evidence observations."""
from __future__ import annotations

from .diagnostics import LOG_KINDS, references
from .util import digest, now


class IssueLedger:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def record(self, rid, scope, review, evidence):
        task = evidence.get("task", {})
        origin = task.get("origin_id") or task.get("id")
        if origin:
            scope = f"task/{origin}/{evidence.get('review_kind', 'implementation')}"
        ledger = self.store.get(rid, "issues", {})
        observed = set()
        proof = self.artifacts.put({"review": review.model_dump(), "context_identity": digest(evidence)}, "issue_observation")
        for finding in review.findings:
            body = finding.model_dump()
            identity = digest([scope, finding.severity, finding.location, finding.description, finding.resolution])
            observed.add(identity)
            issue = ledger.setdefault(identity, {"id": identity, "scope": scope, "original": body,
                                                  "first_seen": now(), "count": 0, "observations": []})
            logs = {ref["hash"]: ref for ref in issue.get("diagnostic_evidence", [])}
            logs.update({ref["hash"]: ref for ref in references(evidence) if ref.get("kind") in LOG_KINDS})
            issue["diagnostic_evidence"] = list(logs.values())
            issue.update(status="open", last_seen=now(), count=issue["count"] + 1)
            if proof not in issue["observations"]:
                issue["observations"].append(proof)
        if review.approved:
            for identity, issue in ledger.items():
                if issue["scope"] == scope and identity not in observed and issue.get("status") == "open":
                    issue.update(status="closed", closure={"kind": "scoped_independent_review", "evidence": proof})
        self.store.checkpoint(rid, "issues", ledger)

    def unresolved(self, rid):
        return [value for value in self.store.get(rid, "issues", {}).values() if value.get("status") == "open"]
