"""Controller-owned issue identity, distinct from changing evidence observations."""
from __future__ import annotations

from .diagnostics import LOG_KINDS, references
from .evidence import assess_observation
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
                if issue["scope"] == scope and identity not in observed and issue.get("status") in {"open", "stale"}:
                    issue.update(status="closed", closure={"kind": "scoped_independent_review", "evidence": proof})
        self.store.checkpoint(rid, "issues", ledger)

    def unresolved(self, rid):
        return [value for value in self.store.get(rid, "issues", {}).values() if value.get("status") in {"open", "stale"}]

    def check_updates(self, rid, owner, commands, results, work, environment, *, previous=None):
        """Return one atomic update with per-obligation closure, never task acceptance."""
        ledger = previous if previous is not None else self.store.get(rid, "issues", {})
        scope = "checks/" + owner
        for issue in ledger.values():
            if issue.get("scope") == scope:
                issue["status"] = "stale"
        actual = {(c.cwd, tuple(c.argv), c.command_hash): c for c in results}
        summaries = []
        failed_in_batch, incomplete = set(), set()
        for command in commands:
            row = actual.get((command.cwd, tuple(command.argv), digest(command.model_dump())))
            obligation = digest([command.cwd, command.argv])
            assessment = assess_observation(command, row, work, environment, self.artifacts) if row else None
            valid = bool(assessment and assessment.valid)
            if not valid:
                incomplete.add(obligation)
                for issue in ledger.values():
                    if issue.get("scope") == scope and issue.get("obligation") == obligation:
                        issue["status"] = "stale"
            proof = {"command": command.model_dump(), "check": row.model_dump() if row else {},
                     "valid": valid, "reason": assessment.reason if assessment else "not executed"}

            def observe(kind, subject, satisfied):
                identity = digest([scope, kind, obligation, subject])
                issue = ledger.get(identity)
                if satisfied and (identity in failed_in_batch or obligation in incomplete):
                    return  # A weaker duplicate cannot hide an unmet contract in this batch.
                if not satisfied:
                    failed_in_batch.add(identity)
                if satisfied and not issue:
                    return
                if not issue:
                    issue = ledger[identity] = {"id": identity, "scope": scope, "owner": owner,
                        "source": "controller", "kind": kind, "obligation": obligation,
                        "subject": subject, "first_seen": now(), "observations": []}
                issue.update(status="closed" if satisfied else "open" if valid else "stale", evidence=proof)
                observation = digest(proof)
                if observation not in issue["observations"]:
                    issue["observations"].append(observation)
                if satisfied:
                    issue["closure"] = {"kind": "controller_evidence", "observation": observation}

            observe("evidence", "complete_current_observation", valid)
            if valid:
                observe("build" if command.outputs else "process", "exit_and_outputs", assessment.passed)
                observe("report", "machine_report", not row.test_runner or row.report_status == "valid")
                for case in row.test_ids:
                    observe("case", case, case in row.passed_test_ids)
                # Minimum *executed* coverage is independent of assertion failures.
                observe("coverage", "declared_test_count", len(row.test_ids) - row.skipped >= command.expected_tests)
            summaries.append({"cwd": command.cwd, "argv": command.argv, "valid": valid,
                "passed": bool(assessment and assessment.passed),
                "passed_tests": row.tests if valid else 0, "total_tests": len(row.test_ids) if valid else 0,
                "unpassed_test_ids": [c for c in row.test_ids if c not in row.passed_test_ids] if valid else [],
                "report": row.report if row else {}, "reason": proof["reason"]})
        return {"issues": ledger, "recovery/observations/" + owner: summaries}
