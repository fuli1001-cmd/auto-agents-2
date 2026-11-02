"""Route a rejected initial plan's bound upstream evidence gap through normal re-entry."""
from __future__ import annotations

import json
from types import SimpleNamespace

from .check_production import validate_review as validate_production_review
from .context_plan import CONTEXT_VERSION
from .evidence_admission import admission_context, validate_obligations
from .faults import StateConflict
from .models import EvidencePlanReview, PlanResult
from .stage_responsibilities import GATES, validate_correction_request
from .util import digest
from .workspace import tree_hash

PREFIX = "recovery/plan-review-upstream/"
REASON = "Upstream plan evidence gap "
RESEARCH = "recovery/plan-review-research-obligation"


class PlanReviewRecovery:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def owner(raw):
        if raw.get("approved") or not raw.get("authority"):
            return None
        findings = [row for row in raw["authority"]["findings"] if row["kind"] != "advisory"]
        upstream = [row for row in findings if row["kind"] == "evidence_gap" and row["owner"] in GATES[:4]]
        if not upstream:
            return None
        if len(upstream) != len(findings) or len({row["owner"] for row in upstream}) != 1:
            raise StateConflict("Plan evidence gaps have no single independently established upstream owner")
        return upstream[0]["owner"]

    def bindings(self, rid):
        # Shared authority identity omits research and prototype proposals.
        return {name: digest(self.store.get(rid, name)) for name in ("provider_research", "prototype")}

    def candidate_state(self, rid):
        return {name: digest(self.store.get(rid, name))
                for name in ("plan", "candidate/plan", "invocation/plan", "provider-result/plan")}

    def inspect(self, rid, work, raw, candidate):
        owner = self.owner(raw)
        if owner is None:
            return None
        proof = self.engine.authority.receipt(rid, raw)
        basis = proof["basis"]
        source = tree_hash(work)
        identity = self.engine.authority.identity(rid)
        if (self.store.get(rid, "plan") is not None or basis["scope"] != "plan"
                or proof["operation"] != "plan-review" or proof.get("owned_candidate") != candidate
                or basis["candidate_hash"] != digest({"plan": candidate})
                or basis["source_identity"] != identity or basis["source_hash"] != source
                or self.store.get(rid, "invocation/plan-review", {}).get("result") != raw):
            raise StateConflict("Upstream plan review no longer binds the current candidate and source")
        plan = PlanResult.model_validate(candidate)
        review = EvidencePlanReview.model_validate(raw)
        context = admission_context(lambda key: self.store.get(rid, key),
                                    shared_browsers=self.engine.config.load().shared_cache.enabled)
        assessment = review.evidence_assessment
        validate_production_review(plan, review, context)
        if (validate_obligations(plan, context) or basis["unsupported"]
                or assessment.unsupported_user_obligations
                or assessment.candidate_hash != digest(candidate)
                or assessment.catalog_hash != context["controller_evidence_capabilities"]["hash"]
                or basis["capability_hash"] != assessment.catalog_hash
                or assessment.source_binding != context["evidence_requirement_source"]["hash"]
                or not all((assessment.mapping_complete, assessment.origins_accurate,
                            assessment.capabilities_sufficient, assessment.requirements_preserved))):
            raise StateConflict("Upstream routing cannot replace unresolved plan admission or capability evidence")
        ledger = self.store.get(rid, "issues", {})
        issues = []
        for row in raw["authority"]["findings"]:
            if row["kind"] == "advisory":
                continue
            finding = raw["findings"][row["finding_index"]]
            iid = digest(["plan-review", *(finding[name] for name in
                         ("severity", "location", "description", "resolution"))])
            issue = ledger.get(iid, {})
            if (issue.get("status") != "open" or issue.get("original") != finding
                    or issue.get("scope") != "plan-review"
                    or issue.get("authority") != {"basis_hash": raw["authority"]["basis_hash"], **row}
                    or not any(self.engine.artifacts.json(ref).get("review") == raw
                               for ref in issue.get("observations", []))):
                raise StateConflict("Upstream plan review lost its exact open issue evidence")
            issues.append(iid)
        unresolved = {key for key, issue in ledger.items()
                      if issue.get("scope") == "plan-review" and issue.get("status") in {"open", "stale"}}
        if set(issues) != unresolved:
            raise StateConflict("Other unresolved plan findings prevent a single-owner recovery")
        return {"owner": owner, "review_key": "authority/reviews/" + digest(raw),
                "review_hash": digest(raw), "candidate_hash": digest(candidate), "source_hash": source,
                "authority_sources": identity, "bindings": self.bindings(rid), "issues": sorted(issues)}

    def prior_reentry(self, rid, work, history_key):
        generation = self.store.run(rid)["data"].get("replans", 0)
        history = self.store.get(rid, history_key, {})
        pending = history.get("pending", {})
        feedback = self.store.get(rid, "replan_feedback", {})
        request = pending.get("correction_request", {})
        if (history_key != f"recovery/gate-history/{generation - 1}" or history.get("version") != 1
                or pending.get("generation") != generation - 1 or pending.get("stage") != "plan"
                or not pending.get("granted") or feedback.get("stage") != "plan"
                or feedback.get("reason") != pending.get("reason")
                or feedback.get("correction_request") != request or request.get("reason") != pending.get("reason")
                or not request.get("permit") or pending.get("permit") != request["permit"]
                or pending.get("correction_source") != {"workspace": str(work), "hash": tree_hash(work)}):
            raise StateConflict("Legacy plan re-entry lost its latest source-bound correction receipt")
        validate_correction_request(request, lambda key: self.store.get(rid, key), action="plan_patch")
        permit = self.engine.recovery_policy.validate_consumption(rid, request["permit"], expected={"plan_patch"})
        if (permit.get("phase") != "completed" or permit.get("outcome") != {"status": "gate_reset", "stage": "plan"}
                or permit.get("outcome_hash") != digest(permit["outcome"])
                or permit.get("source_hash") != tree_hash(work) or permit.get("plan_hash") != digest(None)):
            raise StateConflict("Legacy plan permit is not the completed unchanged plan re-entry")
        archived = history.get("checkpoints", {})
        candidate = archived.get("candidate/plan", {}).get("document")
        invocation = archived.get("invocation/plan", {})
        completed = archived.get("provider-result/plan", {})
        if (candidate is None or invocation.get("result") != candidate
                or not invocation.get("artifact") or completed.get("artifact") != invocation["artifact"]
                or self.engine.artifacts.json(invocation["artifact"]) != candidate):
            raise StateConflict("Legacy plan re-entry lost its exact archived candidate")
        # The completed re-entry was created after the review. Only its exact
        # original invocation presentation can prove the review's upstream inputs.
        review_invocation = self.store.get(rid, "invocation/plan-review", {})
        identity = review_invocation.get("identity")
        presentation = self.store.get(rid, "presentation/plan-review/" + str(identity), {})
        if not identity or presentation.get("identity") != identity or not presentation.get("artifact"):
            raise StateConflict("Legacy plan review lost its frozen upstream input presentation")
        frozen = self.engine.artifacts.json(presentation["artifact"])
        selections = frozen.get("selections", [])
        # v8 adds citation aliases only. A completed v7 review still proves
        # its original upstream bindings; never regenerate historical evidence.
        if (frozen.get("version") not in {"context-v7", CONTEXT_VERSION} or frozen.get("purpose") != "plan"
                or any(not any(row.get("source_ref") == name and row.get("identity") == value
                               and row.get("presentation") != "not_selected" for row in selections)
                       for name, value in self.bindings(rid).items())):
            raise StateConflict("Legacy plan review upstream inputs changed or cannot be proven")
        return candidate

    def route(self, rid, work, candidate, raw, *, history_key=None, upstream_context=None):
        raw = raw.model_dump() if hasattr(raw, "model_dump") else raw
        checked = self.inspect(rid, work, raw, candidate)
        if checked is None:
            return
        if history_key is None and (upstream_context is None or
                {name: digest(upstream_context.get(name)) for name in checked["bindings"]} != checked["bindings"]):
            raise StateConflict("Plan review upstream inputs changed while awaiting the independent response")
        details = "".join(f"\n[{f['severity']}] {f['location']}: {f['description']}\n"
                          f"Evidence: {f['evidence']}\nResolution: {f['resolution']}" for f in raw["findings"])
        reason = REASON + checked["review_hash"] + ": " + raw["summary"] + details
        record = {"version": 1, "run_id": rid, "generation": self.store.run(rid)["data"].get("replans", 0),
                  "reason": reason, "candidate_state": self.candidate_state(rid),
                  "history_key": history_key, **checked}
        key = PREFIX + digest(reason)
        saved = self.store.get(rid, key)
        if saved is not None and saved != record:
            raise StateConflict("Upstream plan recovery evidence changed; preserve its original request")
        updates = {key: record} if saved is None else {}
        obligation = self.store.get(rid, RESEARCH)
        if checked["owner"] == "provider_research" and (not obligation or obligation.get("completed")):
            obligation = {"version": 1, "run_id": rid, "routing_key": key,
                          "previous_acceptance": self.store.get(rid, "authority/accepted/provider_research")}
            updates.update({RESEARCH: obligation, key + "/research-review": obligation})
        if updates:
            self.store.checkpoints(rid, updates)
        from .engine import Replan
        raise Replan(checked["owner"], reason, supersede_keys=["provider/plan"])

    def recover(self, rid, work):
        if any(self.store.get(rid, key) is not None for key in
               ("plan", "candidate/plan", "invocation/plan", "provider-result/plan")):
            return
        feedback = self.store.get(rid, "replan_feedback", {})
        raw = self.store.get(rid, "invocation/plan-review", {}).get("result", {})
        if feedback.get("stage") != "plan" or self.owner(raw) is None:
            return
        generation = self.store.run(rid)["data"].get("replans", 0)
        history_key = f"recovery/gate-history/{generation - 1}"
        candidate = self.prior_reentry(rid, work, history_key)
        self.route(rid, work, candidate, raw, history_key=history_key)

    @staticmethod
    def _invalidated(key, stage):
        return any(key == prefix + owner or key.startswith(prefix + owner + "/")
                   for owner in GATES[GATES.index(stage):]
                   for prefix in ("", "candidate/", "invocation/", "provider-result/"))

    def _completion_permit(self, rid, pending, work):
        evidence = self.evidence(rid, pending, work)
        if not evidence:
            return None
        key = pending.get("permit")
        permit = self.engine.recovery_policy.validate_consumption(rid, key, expected={pending["stage"]})
        bound = (permit or {}).get("authority_failure", {}).get("evidence", {})
        request = pending.get("correction_request", {})
        expected = {**bound.get("pending", {}), "granted": True, "permit": key,
                    "correction_request": request, "correction_source": pending.get("correction_source")}
        if (not key or pending != expected or pending.get("correction_permit") not in (None, key)
                or bound.get("plan_review_recovery") != evidence["plan_review_recovery"]
                or bound.get("review") != evidence["review"] or permit.get("phase") != "started"
                or permit.get("outcome") or permit.get("outcome_hash")
                or permit.get("source_hash") != tree_hash(work) or permit.get("plan_hash") != digest(None)
                or pending.get("correction_source") != {"workspace": str(work), "hash": tree_hash(work)}
                or request.get("reason") != pending["reason"] or request.get("permit") != key):
            raise StateConflict("Committed upstream re-entry lost its exact authorized request")
        validate_correction_request(request, lambda name: self.store.get(rid, name), action=pending["stage"])
        return evidence, permit

    def prepare_completion(self, rid, pending, work, recovery_refs):
        """Freeze this validated transition before the existing atomic gate reset."""
        checked = self._completion_permit(rid, pending, work)
        if checked is None:
            return
        evidence, permit = checked
        if self.store.get(rid, "replanning/pending") != pending:
            raise StateConflict("Upstream re-entry pending state changed before completion")
        with self.store.connect() as db:
            archived = {row["key"]: json.loads(row["data"]) for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=?", (rid,))
                if self._invalidated(row["key"], pending["stage"])}
        feedback = {"stage": pending["stage"], "reason": pending["reason"], "recovery_refs": recovery_refs,
                    "correction_request": pending["correction_request"],
                    "preserved_specification": self.store.get(rid, "clarify"),
                    "preserved_plan": self.store.get(rid, "plan")}
        if pending.get("verification_recheck"):
            feedback["verification_recheck"] = pending["verification_recheck"]
        prepared = {"version": 1, "run_id": rid,
            "history_key": f"recovery/gate-history/{pending['generation']}",
            "history_hash": digest({"version": 1, "pending": pending, "checkpoints": archived,
                                    "recovery_refs": recovery_refs}),
            "permit": pending["permit"], "permit_hash": digest(permit),
            "review_evidence_hash": digest(evidence),
            "feedback_before": self.store.get(rid, "replan_feedback"), "feedback_after": feedback}
        key = evidence["plan_review_recovery"]["checkpoint"] + "/completion"
        old = self.store.get(rid, key)
        if old is not None and old != prepared:
            raise StateConflict("Prepared upstream re-entry changed before its gate reset")
        if old is None:
            self.store.checkpoint(rid, key, prepared)

    def reconcile_completion(self, rid):
        """Finish only the receipt of an exactly proven, already committed reset."""
        run = self.store.run(rid)
        generation = run["data"].get("replans", 0) - 1
        history_key = f"recovery/gate-history/{generation}"
        history = self.store.get(rid, history_key, {}) or {}
        pending = history.get("pending", {})
        permit = self.store.get(rid, pending.get("permit", ""), {}) or {}
        bound = permit.get("authority_failure", {}).get("evidence", {}).get("plan_review_recovery", {})
        feedback = self.store.get(rid, "replan_feedback", {}) or {}
        key = bound.get("checkpoint")
        if not key:
            reason = next((reason for reason in (pending.get("reason"), feedback.get("reason"))
                           if isinstance(reason, str) and reason.startswith(REASON)), None)
            if reason is None:
                return  # Unrelated historical transitions keep their existing semantics.
            key = PREFIX + digest(reason)
        record = self.store.get(rid, key, {}) or {}
        prepared = self.store.get(rid, key + "/completion", {}) or {}
        permit = self.store.get(rid, prepared.get("permit", ""), {}) or {}
        if permit.get("phase") == "completed":
            return
        if (record.get("run_id") != rid or record.get("generation") != generation
                or key != PREFIX + digest(record.get("reason"))
                or prepared.get("version") != 1 or prepared.get("run_id") != rid
                or prepared.get("history_key") != history_key or prepared.get("history_hash") != digest(history)
                or prepared.get("permit") != pending.get("permit") or prepared.get("permit_hash") != digest(permit)
                or prepared.get("feedback_after") != feedback or run["stage"] != record.get("owner")
                or self.store.get(rid, "replanning/pending") is not None):
            raise StateConflict("Upstream completion does not match the latest committed gate reset")
        with self.store.connect() as db:
            if any(self._invalidated(row[0], pending["stage"]) for row in db.execute(
                    "SELECT key FROM checkpoints WHERE run_id=?", (rid,))):
                raise StateConflict("Upstream gate has already advanced beyond its uncompleted receipt")
        work = self.store.root / "worktrees" / rid / "integration"
        if not work.is_dir():
            work = self.engine.project
        # Only read the exact archived preimage; never restore it into the database.
        # Reuse all current review/legacy/authority validators against that preimage.
        from .recovery_policy import RecoveryPolicy
        from .workflow_authority import WorkflowAuthority
        archived = {**history["checkpoints"], "replan_feedback": prepared["feedback_before"]}
        def get(run_id, name, default=None):
            if run_id != rid:
                raise StateConflict("Upstream completion cannot read another run")
            return archived[name] if name in archived else self.store.get(rid, name, default)
        view = SimpleNamespace(root=self.store.root, get=get,
            run=lambda run_id: {**run, "data": {**run["data"], "replans": generation}},
            authority_get=lambda run_id, name: self.store.authority_get(run_id, name)
                if name == "authority/submitted-answers" else get(run_id, name))
        reader = SimpleNamespace(store=view, config=self.engine.config, artifacts=self.engine.artifacts)
        reader.authority = WorkflowAuthority(reader)
        reader.recovery_policy = RecoveryPolicy(reader)
        checked = PlanReviewRecovery(reader)._completion_permit(rid, pending, work)
        if checked is None or digest(checked[0]) != prepared["review_evidence_hash"]:
            raise StateConflict("Upstream completion lost its original verified review evidence")
        self.engine.recovery_policy.complete(rid, pending["permit"],
                                             {"status": "gate_reset", "stage": pending["stage"]})

    def requires_research_review(self, rid):
        # Rejection changes replan_feedback, but cannot discharge this obligation.
        obligation = self.store.get(rid, RESEARCH)
        if not obligation:
            return False
        record = self.store.get(rid, obligation.get("routing_key", ""), {})
        if (obligation.get("version") != 1 or obligation.get("run_id") != rid
                or record.get("run_id") != rid or record.get("owner") != "provider_research"
                or obligation["routing_key"] != PREFIX + digest(record.get("reason"))
                or self.store.get(rid, obligation["routing_key"] + "/research-review") != obligation):
            raise StateConflict("Research re-entry lost its upstream plan review obligation")
        # Persisting the route is not authorization. A crash before preparing or
        # consuming Replan must not make the old cached research discharge it.
        generation = record["generation"]
        history = self.store.get(rid, f"recovery/gate-history/{generation}")
        if history is None and self.store.run(rid)["data"].get("replans", 0) == generation:
            return False
        pending = (history or {}).get("pending", {})
        request = pending.get("correction_request", {})
        permit = self.store.get(rid, pending.get("permit", ""), {})
        from .stage_responsibilities import _permit_identity
        if (pending.get("generation") != generation or pending.get("stage") != "provider_research"
                or pending.get("reason") != record["reason"] or not pending.get("granted")
                or request.get("permit") != pending.get("permit")
                or request.get("action") != "provider_research" or request.get("owner") != "provider_research"
                or request.get("permit_hash") != _permit_identity(permit)
                or permit.get("action", {}).get("action") != "provider_research"
                or permit.get("source_hash") != record["source_hash"]
                or permit.get("phase") != "completed"
                or permit.get("outcome") != {"status": "gate_reset", "stage": "provider_research"}
                or permit.get("outcome_hash") != digest(permit["outcome"])):
            raise StateConflict("Research review obligation has no completed authorized upstream re-entry")
        completed = obligation.get("completed")
        if completed:
            accepted = self.store.get(rid, completed.get("review_key", ""), {})
            proof = self.engine.authority.receipt(rid, accepted.get("review", {}))
            if (not proof["review"]["approved"] or proof["basis"]["scope"] != "research"
                    or proof["review_hash"] != completed.get("review_hash")
                    or not proof["operation"].startswith("research-review/plan-gap/" + digest(obligation["routing_key"]) + "/")):
                raise StateConflict("Research review obligation lost its independent acceptance receipt")
            return False
        return True

    def research_operation(self, rid):
        if not self.requires_research_review(rid):
            return "research-review"
        obligation = self.store.get(rid, RESEARCH)
        generation = self.store.run(rid)["data"].get("replans", 0)
        return "research-review/plan-gap/" + digest(obligation["routing_key"]) + "/" + str(generation)

    def issue_scope(self, rid, operation):
        if not operation.startswith("research-review/plan-gap/"):
            return operation
        if operation != self.research_operation(rid):
            raise StateConflict("Research issue scope lost its current independent review obligation")
        # Calls and receipts remain generation-specific; defect closure belongs
        # to this one enduring research obligation across rejected generations.
        return operation.rsplit("/", 1)[0]

    def accept_research(self, rid, document, review):
        updates = self.engine.authority.acceptance(rid, "provider_research", document, review)
        if self.requires_research_review(rid):
            obligation = self.store.get(rid, RESEARCH)
            proof = self.engine.authority.receipt(rid, review.model_dump())
            if proof["operation"] != self.research_operation(rid):
                raise StateConflict("Research obligation requires its own independent review receipt")
            completed = {**obligation, "completed": updates["authority/accepted/provider_research"]}
            updates.update({RESEARCH: completed, obligation["routing_key"] + "/research-review": completed})
        return updates

    def evidence(self, rid, pending, work):
        reason = pending.get("reason")
        for permit_key in {pending.get("correction_permit"), pending.get("permit")} - {None}:
            permit = self.store.get(rid, permit_key, {})
            bound = permit.get("authority_failure", {}).get("evidence", {}).get("plan_review_recovery")
            if bound and (not reason or reason != bound.get("reason")):
                raise StateConflict("Upstream plan recovery permit lost its bound reason")
        if not reason:
            return {}  # Historical unrelated granted records may omit their reason.
        key = PREFIX + digest(reason)
        record = self.store.get(rid, key)
        if record is None:
            if pending["reason"].startswith(REASON):
                raise StateConflict("Upstream plan recovery lost its recorded review evidence")
            return {}
        if (record.get("version") != 1 or record.get("run_id") != rid
                or record.get("generation") != pending["generation"]
                or record.get("generation") != self.store.run(rid)["data"].get("replans", 0)
                or record.get("owner") != pending["stage"] or record.get("reason") != pending["reason"]
                or record.get("candidate_state") != self.candidate_state(rid)):
            raise StateConflict("Upstream plan recovery no longer matches its pending stage and candidate")
        proof = self.store.get(rid, record["review_key"], {})
        if not proof:
            raise StateConflict("Upstream plan recovery lost its independent review receipt")
        candidate = proof.get("owned_candidate")
        if record.get("history_key") and self.prior_reentry(rid, work, record["history_key"]) != candidate:
            raise StateConflict("Legacy plan recovery changed its archived candidate")
        checked = self.inspect(rid, work, proof.get("review", {}), candidate)
        if checked is None or any(record.get(name) != value for name, value in checked.items()):
            raise StateConflict("Upstream plan recovery review or source evidence changed")
        return {"plan_review_recovery": {"checkpoint": key, **record}, "review": proof["review"]}
