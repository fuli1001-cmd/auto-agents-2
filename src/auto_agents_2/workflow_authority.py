"""Controller adapters for the shared finite review-authority contract."""
from __future__ import annotations

from .errors import AA2Error, RecoveryPathExhausted
from .faults import HumanIntervention, HumanRequirement, StateConflict
from .models import ReviewResult
from .review_authority import (
    KINDS,
    InvalidAuthorityReview,
    dispute_targets,
    make_basis,
    review_schema,
    scope,
    source_identity,
    validate_review,
)
from .util import digest

REVIEW_CANDIDATES = {"coverage_review": "assessment", "research": "research", "design": "design",
                     "plan": "plan", "plan_patch": "plan", "test_changes": "candidate_changes"}


def review_candidate(kind, context):
    key = REVIEW_CANDIDATES.get(kind)
    if key and not isinstance(context.get(key), dict):
        raise InvalidAuthorityReview("missing_review_candidate", {"scope": kind, "required": key})
    return context[key] if key else None


class WorkflowAuthority:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def identity(self, rid):
        return source_identity(lambda name: self.store.authority_get(rid, name))

    def author_scope(self, rid, stage):
        correction = self.store.get(rid, "authority/correction")
        return {"version": 1, "owner": stage,
            "meaning": "Produce only this stage's owned proposal. Usage notes, research, suggested methods and review "
                       "instructions are not user statements, mandatory acceptance or permission. Technical selections "
                       "require independent adoption; do not label a chosen method as an explicit input requirement.",
            "correction": correction if correction and not correction.get("completed") and correction["owner"] == stage else None}

    def correction_evidence(self, rid, pending, correction):
        if not correction or correction.get("completed") or correction.get("reason") != pending["reason"]:
            return {}
        if correction["owner"] != pending["stage"] or correction["source_identity"] != self.identity(rid):
            raise StateConflict("Source correction identity changed before authorization")
        record = self.store.get(rid, correction["review_key"])
        if not record:
            raise StateConflict("Source correction lost its independent review")
        validate_review(record["review"], record["basis"])
        return {"authority_correction": correction, "review": record["review"]}

    @staticmethod
    def consumes(schema):
        nested = schema.model_fields.get("review")
        return (issubclass(schema, ReviewResult) or nested is not None
                and isinstance(nested.annotation, type) and issubclass(nested.annotation, ReviewResult))

    def prepare(self, rid, stage, context, schema, work, operation):
        if not self.consumes(schema):
            return context, schema
        kind = scope(stage, context, operation)
        if kind == "prototype":
            # Its finite, already-versioned protocol is the adapter for this
            # scope. Preserve existing matching human consent and observations.
            return context, schema
        if kind not in KINDS:
            raise InvalidAuthorityReview("unregistered_review_consumer", {"stage": stage, "kind": kind})
        review_candidate(kind, context)  # Reject incomplete controller input before a provider call.
        from .workspace import tree_hash
        capture = self.store.get(rid, "capture")
        original = ""
        if capture:
            original = self.engine.artifacts.read(capture["artifact"])
        elif kind != "doctor":
            raise InvalidAuthorityReview("missing_original_input", {})
        specification = self.store.get(rid, "clarify") or {}
        design = self.store.get(rid, "design")
        accepted = self.store.get(rid, "authority/accepted/design", {})
        choices = []
        if accepted and accepted.get("artifact_hash") == digest(design):
            record = self.store.get(rid, accepted["review_key"])
            if not record or record["review_hash"] != accepted["review_hash"]:
                raise StateConflict("Accepted technical choices have lost their review identity")
            assessment = validate_review(record["review"], record["basis"])
            choices = [c.model_dump() for c in assessment.technical_choices]
        basis = make_basis(kind, context, original=original, specification=specification,
            accepted_design=design if kind != "design" else None,
            accepted_plan=self.store.get(rid, "plan"), design_adoptions=choices,
            source_hash=tree_hash(work), source_identity=self.identity(rid),
            submitted_answers=self.store.submitted_answers(rid),
            correction=self.store.get(rid, "authority/correction"))
        return {**context, "controller_review_basis": basis}, review_schema(schema)

    def validate(self, rid, result, context):
        basis = context.get("controller_review_basis")
        assessment = validate_review(result, basis)
        if basis["source_identity"] != self.identity(rid):
            raise InvalidAuthorityReview("authority_sources_changed_during_review", result.model_dump())
        return assessment

    def record(self, rid, result, context, operation):
        basis = context.get("controller_review_basis")
        if not basis:
            prototype_basis = context.get("prototype_review_basis")
            if prototype_basis:
                from .prototype_review import validate_review as validate_prototype
                raw = result.model_dump()
                source = context["prototype_requirement_source"]
                validate_prototype(raw, prototype_basis, source)
                self.store.checkpoint(rid, "authority/prototype-reviews/" + digest(raw),
                    {"review": raw, "basis": prototype_basis, "source": source,
                     "source_identity": self.identity(rid)})
            return None
        assessment = self.validate(rid, result, context)
        raw = result.model_dump()
        review = raw if "approved" in raw else raw["review"]
        key = "authority/reviews/" + digest(review)
        record = {"version": 1, "basis": basis, "review": review,
                  "review_hash": digest(review), "operation": operation}
        candidate = review_candidate(basis["scope"], context)
        if basis["scope"] == "test_changes":
            record["reviewed_changes"] = candidate
        elif candidate is not None:
            record["owned_candidate"] = candidate
        old = self.store.get(rid, key)
        if old and old != record:
            # The same complete response includes the same basis hash; it cannot
            # silently be attached to a different review operation.
            if any(old.get(k) != record[k] for k in ("basis", "review", "review_hash")):
                raise StateConflict("Review authority receipt changed")
        else:
            self.store.checkpoint(rid, key, record)
        return key, assessment

    def context(self, rid, review, context):
        raw = review.model_dump() if hasattr(review, "model_dump") else review
        record = self.receipt(rid, raw)
        return {**context, "controller_review_basis": record["basis"]}

    def receipt(self, rid, raw):
        record = self.store.get(rid, "authority/reviews/" + digest(raw))
        if not record or record.get("review") != raw or record.get("review_hash") != digest(raw):
            raise StateConflict("Independent review has no matching controller authority receipt")
        validate_review(raw, record["basis"])
        return record

    def acceptance(self, rid, owner, document, review):
        """Return an update for the caller's existing atomic checkpoint batch."""
        raw = review.model_dump() if hasattr(review, "model_dump") else review
        key = "authority/reviews/" + digest(raw)
        record = self.store.get(rid, key)
        if not record or record["review"] != raw or not raw.get("approved"):
            raise StateConflict("Artifact acceptance requires its exact independent authority review")
        validate_review(raw, record["basis"])
        if record["basis"]["source_identity"] != self.identity(rid):
            raise StateConflict("Artifact authority sources changed before acceptance")
        updates = {"authority/accepted/" + owner: {"version": 1, "artifact_hash": digest(document),
                "review_hash": digest(raw), "review_key": key}}
        correction = record["basis"].get("correction")
        if correction:
            if correction != self.store.get(rid, "authority/correction"):
                raise StateConflict("Correction changed before its owner's acceptance")
            updates["authority/correction"] = {**correction, "completed": {
                "review_key": key, "artifact_hash": digest(document)}}
        return updates

    def accepted(self, rid, owner, document):
        accepted = self.store.get(rid, "authority/accepted/" + owner, {})
        if accepted.get("artifact_hash") != digest(document):
            return False
        record = self.store.get(rid, accepted.get("review_key", ""))
        if not record or accepted.get("review_hash") != record.get("review_hash"):
            return False
        return self.reusable(rid, record["review"], current=False) and record["review"]["approved"]

    async def reaudit_plan(self, rid, work, data, context):
        from .engine import Replan
        from .evidence_admission import (
            admission_context,
            validate_admission_review,
            validate_obligations,
        )
        from .models import EvidencePlanReview, PlanResult
        plan = PlanResult.model_validate(data)
        if plan.evidence_contract is None:
            raise Replan("plan", "Retain the legacy plan; formally map its evidence obligations before new acceptance.")
        context = {**context, **admission_context(lambda key: self.store.get(rid, key),
            shared_browsers=self.engine.config.load().shared_cache.enabled), "review_kind": "plan",
            "plan": data, "evidence_candidate_hash": digest(data)}
        context["unsupported_evidence_obligations"] = validate_obligations(plan, context)
        review = await self.engine._invoke("review", context, EvidencePlanReview, work, rid,
            "plan/authority-reaudit/" + digest(data), fresh=True)
        context = self.context(rid, review, context)
        validate_admission_review(plan, review, context)
        from .check_production import activation_updates, additional_affected
        from .evidence_admission import admission_record
        changed = additional_affected(plan, plan, context.get("accepted_check_production"),
                                      review.evidence_assessment.check_production)
        if changed & set(self.store.get(rid, "develop/done", [])):
            from .recovery import FailureRecord, RecoveryNeeded
            raise RecoveryNeeded(FailureRecord.create(kind="contract", component="plan_review", origin_id="final",
                step="check_production_mapping", evidence_refs=["plan"],
                evidence={"reason": "Changed closure mapping requires a formal PlanPatch with task invalidation",
                          "plan": data, "review": review.model_dump()}))
        record = admission_record(plan, review, context)
        self.store.checkpoints(rid, {**self.acceptance(rid, "plan", data, review), **activation_updates(record)})

    def superseded_capability_waits(self, rid):
        """Only an exact old admission observation can be retired by a new permit."""
        blocked = self.store.get(rid, "evidence_admission/blocked")
        if not blocked:
            return []
        from .evidence_admission import validate_admission_review
        from .models import PlanResult
        try:
            validate_admission_review(PlanResult.model_validate(blocked["plan"]),
                                      blocked["review"], blocked["context"])
        except HumanIntervention as exc:
            identity = digest([exc.reason_code, exc.phase, exc.requirement.model_dump()])
            return [q["key"] for q in self.store.pending(rid)
                    if q["answer"] is None and q["key"].startswith("prerequisite/" + identity + "/")]
        except (AA2Error, ValueError, KeyError):
            return []
        return []

    def reusable(self, rid, raw, *, current=True):
        if not isinstance(raw, dict) or not raw.get("authority"):
            return False
        record = self.store.get(rid, "authority/reviews/" + digest(raw))
        if not record or record.get("review") != raw:
            return False
        try:
            validate_review(raw, record["basis"])
        except InvalidAuthorityReview:
            return False
        return not current or record["basis"]["source_identity"] == self.identity(rid)

    def retain_task(self, rid, task, saved):
        """Retain implementation/history, invalidate only an unsupported verdict."""
        if not saved.get("commit") or not self.store.get(rid, "authority/protocol"):
            return saved
        if saved.get("review", {}).get("approved") and self.reusable(rid, saved["review"]):
            return saved
        key = "develop/task/" + task.id
        state = self.store.get(rid, "task_state/" + task.id, {"version": 1, "revision": 1})
        retained = {"phase": "reverify", "resume_base": saved["commit"], "previous_evidence": saved,
                    "feedback": {"authority_reassessment": "Retain accepted implementation; obtain a current grounded verdict."}}
        self.store.checkpoints(rid, {"authority/task-history/" + task.id + "/" + digest(saved): saved,
            key: retained, "task_state/" + task.id: {**state, "implementation": "accepted", "evidence": "stale"},
            "develop/done": [t for t in self.store.get(rid, "develop/done", []) if t != task.id]})
        return retained

    async def reaudit_task(self, rid, saved, task, work):
        """Adopt an untouched first proposal under today's formally accepted task.

        This is a new assessment, never a reconstructed historical permission.
        Partial writes and old recovery grants need reconciliation of their
        original inputs; hashes alone cannot recreate the original source text.
        """
        if (not self.store.get(rid, "authority/protocol") or saved.get("phase") != "implementing"
                or saved.get("authority_sources")):
            return saved
        from .models import ImplementationResult, PlanResult
        from .source_history import SourceHistory
        from .workspace import prepare_changes, tree_hash
        plan = PlanResult.model_validate(self.store.get(rid, "plan"))
        rounds = self.store.get(rid, "rounds/" + (task.origin_id or task.id), {})
        state = self.store.get(rid, "task_state/" + task.id, {})
        if (saved.get("recovery_permit") or rounds.get("round") != 1 or state.get("revision", 1) != 1
                or saved.get("source_hash") != tree_hash(work)
                or task.model_dump() not in [t.model_dump() for t in plan.tasks]
                or not self.accepted(rid, "plan", plan.model_dump())):
            raise StateConflict("Legacy implementing candidate needs its original input/authorization reconciled; "
                                "preserve partial writes, proposal, counters and consent")
        original = dict(saved)
        sources = self.identity(rid)
        if saved.get("implementation"):
            # Match this completed call, not any older presentation with the
            # same operation name. The root hash is of the full original task.
            operation = "develop/task/" + task.id
            call = self.store.get(rid, "invocation/" + operation, {})
            from .context_plan import CONTEXT_VERSION
            prefix = "presentation/" + operation + "/" + call.get("identity", "")
            proven = False
            for name in (prefix, prefix + "/" + CONTEXT_VERSION):
                retained = self.store.get(rid, name, {})
                if not retained or retained.get("identity") != call.get("identity"):
                    continue
                view = self.engine.artifacts.json(retained["artifact"])
                identities = {(row.get("source_ref"), row.get("identity"))
                    for row in view.get("selections", []) if row.get("presentation") != "not_selected"}
                proven |= (view.get("payload", {}).get("task") == task.model_dump()
                           or ("task", digest(task.model_dump())) in identities)
            if call.get("result") != saved["implementation"] or not proven:
                raise StateConflict("Legacy candidate lacks its exact completed call and original full task binding")
            change = ImplementationResult.model_validate(saved["implementation"])
            patch = prepare_changes(work, change.changes, task.allowed_paths, protected=False)
            if saved.get("patch") is not None and saved["patch"] != patch:
                raise StateConflict("Legacy candidate does not reproduce its original prepared preimage")
            context = {"review_kind": "test_changes", "task": task.model_dump(), "plan": plan.model_dump(),
                "candidate_changes": change.model_dump(), "project": self.engine.contexts.build(work),
                "source_history": SourceHistory(self.engine).collect_task(rid, work, task),
                "purpose": "Reassess this exact untouched initial candidate against the current accepted task. "
                    "Preserve business goals and existing assertions. This is not an old repair grant or task acceptance."}
            review = await self.engine._invoke("review", context, ReviewResult, work, rid,
                "authority/retained-candidate/" + task.id + "/" + digest(original), fresh=True)
            if not review.approved:
                from .recovery import FailureRecord, RecoveryNeeded
                # Retain the candidate as unapplied; normal diagnosis selects a
                # new, explicitly scoped action based on this fresh verdict.
                raise RecoveryNeeded(FailureRecord.create(kind="proposal", component="implementation",
                    origin_id=task.origin_id or task.id, task_id=task.id, step="candidate",
                    obligations=task.requirement_ids, evidence_refs=["develop/task/" + task.id],
                    evidence={"review": review.model_dump(), "task": task.model_dump()},
                    proposal=change.model_dump()))
            saved = {**saved, "patch": patch, "protected_review": review.model_dump(),
                     "protected_patch_hash": digest(patch)}
        if sources != self.identity(rid) or original["source_hash"] != tree_hash(work):
            raise StateConflict("Retained candidate inputs changed during its independent reassessment")
        saved = {**saved, "authority_sources": sources}
        self.store.checkpoints(rid, {"authority/task-history/" + task.id + "/" + digest(original): original,
                                    "develop/task/" + task.id: saved})
        return saved

    def prepared_task(self, rid, saved, task):
        """Validate the durable grant before an unapplied or partial batch resumes."""
        if not self.store.get(rid, "authority/protocol"):
            return
        if saved.get("authority_sources") != self.identity(rid):
            raise StateConflict("Prepared task requires current authority reassessment; preserve its patch and preimage")
        if saved.get("recovery_permit"):
            self.engine.recovery_policy.validate_consumption(rid, saved["recovery_permit"], expected={"repair"})
        from .workspace import protected_change_path
        if not any(protected_change_path(row["path"]) for row in saved["patch"]):
            return
        raw = saved.get("protected_review")
        if not raw or not raw.get("approved") or not self.reusable(rid, raw):
            raise StateConflict("Prepared protected changes require independent authority reassessment")
        receipt = self.receipt(rid, raw)
        if (receipt["basis"]["scope"] != "test_changes"
                or receipt.get("reviewed_changes") != saved.get("implementation")
                or receipt["basis"]["source_hash"] != saved.get("source_hash")
                or saved.get("protected_patch_hash") != digest(saved["patch"])):
            raise StateConflict("Prepared protected changes lost their exact reviewed candidate binding")

    async def reaudit_delivery(self, rid, journal):
        """A retained publication is not permission to reuse an ungrounded verdict."""
        if journal["phase"] in {"reintegrating", "repairing_runtime"}:
            # These phases retain the OLD candidate until existing merge/patch
            # reconciliation and _reintegrate_delivery's checks plus independent
            # current-authority review finish. The interim worktree is expected
            # to differ; it is not a stable candidate allowed to be published.
            return
        from pathlib import Path

        from .faults import ApprovalBoundary
        from .models import PlanResult
        from .source_history import SourceHistory
        from .workspace import git, legacy_tree_hash, tree_hash
        work = Path(journal["work"])
        before = tree_hash(work)
        # The delivery coordinator still owns legacy publication interpretation
        # and full revalidation. Do not mistake its old hash algorithm for drift
        # before it can run, and do not rewrite the journal as a side effect.
        known_source = before == journal["source_hash"] or (
            journal.get("identity_version") != 2 and legacy_tree_hash(work) == journal["source_hash"])
        if not known_source or git(work, "rev-parse", "HEAD") != journal["candidate"]:
            raise StateConflict("Pending delivery authority source changed; preserve published work")
        raw = self.store.get(rid, "verify/review")
        supplement = self.store.get(rid, "delivery/authority-review", {})
        if supplement.get("candidate") == journal["candidate"]:
            raw = supplement.get("review")
        if self.reusable(rid, raw):
            proof = self.receipt(rid, raw)
            if raw["approved"] and proof["basis"]["source_hash"] == before:
                return
        plan = PlanResult.model_validate(journal["plan"]) if journal.get("plan") else None
        history = SourceHistory(self.engine)
        context = {"project": self.engine.contexts.build(work), "plan": journal.get("plan"),
            "specification": self.store.get(rid, "clarify"), "previous_verdict": raw,
            "evidence": self.store.get(rid, "verify/checks", []),
            "implemented_visuals": self.store.get(rid, "verify/visuals", []),
            "source_history": history.collect_plan(rid, work, plan) if plan else history.collect_protected(rid, work),
            "controller_feedback": "Reassess the exact retained delivery under current requirement authority. "
                "Old approval is historical evidence, not a current verdict; runtime validation still follows."}
        review = await self.engine._invoke("verify", context, ReviewResult, work, rid,
            "delivery/authority-reaudit/" + journal["candidate"], fresh=True)
        if tree_hash(work) != before:
            raise StateConflict("Delivery changed during authority reassessment")
        self.store.checkpoint(rid, "delivery/authority-review", {
            "candidate": journal["candidate"], "source_hash": before, "review": review.model_dump()})
        if not review.approved:
            # Publication has already occurred: do not let a new review create
            # implicit write authority or mark completion. Existing delivery
            # recovery can assess the retained, grounded failure.
            raise ApprovalBoundary("Pending delivery requires resolution of its independently grounded review")

    def route_dispute(self, rid, result):
        raw = result.model_dump()
        raw = raw if "approved" in raw else raw.get("review", {})
        if raw.get("approved") or not raw.get("authority"):
            return
        proof = self.receipt(rid, raw)
        if proof["basis"]["source_identity"] != self.identity(rid):
            raise StateConflict("Source correction cannot consume a stale review")
        findings = [f for f in raw["authority"]["findings"] if f["kind"] == "source_dispute"]
        if not findings:
            return
        owners = {f["owner"] for f in findings}
        if len(owners) != 1 or not owners <= {"clarify", "provider_research", "prototype", "design", "plan"}:
            raise HumanIntervention(HumanRequirement(kind="external_input",
                reason="Independent review found a source dispute with more than one responsible stage.",
                action="Resolve the retained source mapping before choosing a correction stage.",
                resume_condition="A single source-bound correction target is independently established."),
                reason_code="requirement_source_unresolved", phase="review")
        owner = next(iter(owners))
        method_sources = {proof["basis"]["sources"][f["method_source"]].get("root", f["method_source"])
                          for f in findings}
        if len(method_sources) != 1:
            raise StateConflict("Mixed source owners require a single explicit correction mapping")
        method_source = next(iter(method_sources))
        previous_kind = proof["basis"]["sources"][method_source]["kind"]
        previous = (proof.get("owned_candidate") if method_source == "candidate"
                    else (proof["basis"].get("correction") or {}).get("previous")
                    if method_source == "previous_contract" else self.store.get(rid, owner))
        if previous is None:
            raise StateConflict("Source correction lost its responsible candidate")
        basis = proof["basis"]
        clauses = sorted({f["method_quote"] for f in findings})
        preserve = sorted(c["id"] for c in basis["criteria"]
                          if c["kind"] in {"submitted_requirement", "accepted_interpretation"})
        request = {"version": 1, "owner": owner, "previous": previous, "clauses": clauses,
            "mapped_clauses": dispute_targets(validate_review(raw, basis).findings, basis),
            "previous_kind": previous_kind,
            "preserve_ids": preserve, "review_key": "authority/reviews/" + digest(raw),
            "source_identity": basis["source_identity"], "source_hash": basis["source_hash"],
            "meaning": "Return to the responsible author for reviewed correction, not waived acceptance or permission."}
        if owner == "design" and method_source == "accepted_design":
            request["retained_choices"] = [c for c in basis["criteria"]
                if c["kind"] == "accepted_technical_contract" and c["owner"] == "design"
                and c["statement"] not in clauses]
        request["id"] = digest(request)
        # Rewording or renewed acceptance cannot create a fresh budget. New
        # submitted inputs or actually executed checks can justify reassessment;
        # this finite identity check does not claim semantic progress detection.
        previous_request = self.store.get(rid, "authority/correction") or {}
        facts = digest([basis["source_identity"]["capture"], basis["source_identity"].get("authority/submitted-answers"),
                        basis.get("correction_facts")])
        progress = (previous_request.get("lineage", previous_request.get("id"))
                    if previous_request.get("progress_facts") == facts else request["id"])
        request["lineage"] = progress
        request["progress_facts"] = facts
        key = "authority/no-progress/" + progress
        attempts = self.store.get(rid, key, [])
        if request["id"] not in attempts:
            if len(attempts) >= 2:
                raise RecoveryPathExhausted({owner: {"evidence": key}},
                    "Repeated source correction has no new relevant evidence; preserve the disputed contract for diagnosis.")
            self.store.checkpoint(rid, key, [*attempts, request["id"]])
        reason = "Source-bound correction " + request["id"] + ": " + raw["summary"]
        request["reason"] = reason
        self.store.checkpoints(rid, {"authority/corrections/" + request["id"]: request,
                                    "authority/correction": request})
        active = self.store.get(rid, "recovery/active")
        if active:
            # Retire the old operation, not its history. The new stage still
            # needs _grant_replan; an old plan-patch permit cannot authorize it.
            self.engine.recovery._finish(rid, active, {}, replan=(owner, reason))
        from .engine import Replan
        raise Replan(owner, reason, supersede_keys=self.superseded_capability_waits(rid))

    def patch_transition(self, rid, active, failure):
        """Only the controller's activated transaction can change a failure's parents."""
        key = f"plan_revision/journal/{failure.id}/{active['attempt']}"
        journal = self.store.get(rid, key, {})
        if journal.get("phase") != "activated" or journal.get("plan") != self.store.get(rid, "plan"):
            raise StateConflict("Plan follow-up lacks the exact activated transaction")
        raw = (journal.get("authority_reassessment") or journal).get("review")
        proof = self.receipt(rid, raw)
        before, after = proof["basis"]["source_identity"], self.identity(rid)
        reassessed_after = journal.get("authority_reassessment", {}).get("phase") == "activated"
        history = self.store.get(rid, f"plan_revision/history/{journal['base_revision']}", {})
        if (not raw.get("approved") or proof["basis"]["scope"] != "plan_patch"
                or proof["basis"]["candidate_hash"] != digest({k: journal[k] for k in ("plan", "patch", "previous_plan")})
                or any(before[k] != after[k] for k in before if k not in {"plan", "plan_revision/current"})
                or (before != after or history.get("plan") != journal["previous_plan"] if reassessed_after
                    else digest(journal["previous_plan"]) != before["plan"])
                or self.store.get(rid, "plan_revision/current", {}).get("revision") != journal["base_revision"] + 1):
            raise StateConflict("Plan follow-up has unrelated authority changes")
        return {"journal": key, "failure_id": failure.id, "before": before, "after": after,
                "review_hash": digest(raw), "reassessed_after": reassessed_after}

    def valid_transition(self, rid, failure, transition):
        if not transition or transition.get("failure_id") != failure.id:
            return False
        journal = self.store.get(rid, transition.get("journal", ""), {})
        raw = (journal.get("authority_reassessment") or journal).get("review")
        if (journal.get("phase") != "activated" or journal.get("plan") != self.store.get(rid, "plan")
                or transition.get("after") != self.identity(rid) or digest(raw) != transition.get("review_hash")):
            return False
        proof = self.receipt(rid, raw)
        before, after = proof["basis"]["source_identity"], transition["after"]
        reassessed_after = journal.get("authority_reassessment", {}).get("phase") == "activated"
        history = self.store.get(rid, f"plan_revision/history/{journal['base_revision']}", {})
        return (raw.get("approved") and proof["basis"]["scope"] == "plan_patch"
            and proof["basis"]["candidate_hash"] == digest({k: journal[k] for k in ("plan", "patch", "previous_plan")})
            and before == transition.get("before") and reassessed_after == transition.get("reassessed_after", False)
            and (before == after and history.get("plan") == journal["previous_plan"] if reassessed_after
                 else digest(journal["previous_plan"]) == before["plan"])
            and self.store.get(rid, "plan_revision/current", {}).get("revision") == journal["base_revision"] + 1
            and all(before[k] == after[k] for k in before if k not in {"plan", "plan_revision/current"}))

    def validate_prepared_migration(self, rid, permit_key, permit, failure):
        """Supplement an old grant with fresh review; never replace its frozen consent."""
        from .recovery_identity import action_contract, binding_matches_failure
        from .review_authority import validate_journal_authority
        from .stage_responsibilities import validate_correction_request
        journal = self.store.get(rid, permit.get("authority_patch", "")) or {}
        cycle = self.store.get(rid, "recovery/cycles/" + permit.get("cycle_id", "")) or {}
        binding = self.store.get(rid, (permit_key or "").replace("recovery/permits/", "recovery/authorization-inputs/", 1)) or {}
        suffix = (permit_key or "").removeprefix("recovery/permits/")
        snapshot = self.store.get(rid, "recovery/request-snapshots/" + suffix) or {}
        frozen = snapshot.get("request", {}).get("scope", {})
        tasks = journal.get("previous_plan", {}).get("tasks", [])
        scoped = [t for t in tasks if t["id"] in permit.get("task_scopes", {})]
        selected = frozen.get("candidates", {}).get("plan_patch", {})
        context = self.store.get(rid, f"recovery/patch-context/{failure.id}/{cycle.get('observation')}") or {}
        reassessment = journal.get("authority_reassessment", {})
        if (permit.get("action", {}).get("action") != "plan_patch" or permit["action"].get("failure_id") != failure.id
                or journal.get("phase") != "prepared" or permit.get("source_hash") != journal.get("source_hash")
                or permit.get("plan_hash") != digest(journal.get("previous_plan"))
                or permit_key not in cycle.get("permits", [])
                or not binding_matches_failure(binding, failure.model_dump())
                or binding.get("source_hash") != permit["source_hash"] or binding.get("plan_hash") != permit["plan_hash"]
                or permit.get("authority_sources") != self.identity(rid)
                or cycle.get("failure_id") != failure.id or cycle.get("owner") != failure.origin_id
                or cycle.get("id") != permit.get("cycle_id")
                or cycle.get("id") != digest([failure.origin_id, failure.id, cycle.get("observation")])
                or permit.get("owner") != failure.origin_id
                or permit.get("authority_patch") != f"plan_revision/journal/{failure.id}/{cycle.get('observation')}"
                or permit.get("task_scopes") != {t["id"]: t["allowed_paths"] for t in scoped}
                or binding.get("scope_hash") != digest([scoped, failure.evidence.get("allowed_paths", [])])
                or snapshot.get("binding") != binding or frozen.get("cycle_id") != cycle.get("id")
                or frozen.get("tasks") != scoped or frozen.get("allowed_paths") != failure.evidence.get("allowed_paths", [])
                or not selected or action_contract(selected) != action_contract(permit["action"])
                or action_contract(context.get("recovery_decision", {})) != action_contract(permit["action"])
                or reassessment.get("journal") != permit.get("authority_patch")
                or reassessment.get("permit") != permit_key or reassessment.get("phase") != "prepared"):
            raise StateConflict("Legacy prepared action lost its exact frozen authorization; preserve it for reassessment")
        expected_candidates = (digest({k: action_contract(v) for k, v in frozen["candidates"].items()})
                               if binding.get("version") == 2 else digest(frozen["candidates"]))
        if binding.get("candidates") != expected_candidates:
            raise StateConflict("Legacy prepared candidate choices no longer match their frozen authorization")
        validate_correction_request(journal.get("correction_request", {}),
                                    lambda key: self.store.get(rid, key), action="plan_patch")
        # Legacy contracts did not freeze separate submitted-answer identities.
        # Do not invent proof of which answers an old grant considered.
        if self.store.submitted_answers(rid):
            raise StateConflict("Legacy prepared consent lacks submitted-answer binding; retain it for reassessment")
        validate_journal_authority(journal, lambda name: self.store.authority_get(rid, name))

    def require_failure(self, rid, failure, action, *, transition=None):
        """Advice cannot become repair permission through an unvalidated review."""
        readonly = action in {"diagnose", "recheck", "prototype_recheck", "preview_recheck"}
        transitioned = action == "repair" and self.valid_transition(rid, failure, transition)
        if transition and not transitioned:
            raise StateConflict("Recovery follow-up lost its reviewed version transition")
        reviews = [failure.evidence.get(name) for name in ("review", "patch_review", "test_review")]
        for raw in reviews:
            if not isinstance(raw, dict) or "approved" not in raw:
                continue
            if raw.get("basis_hash") and raw.get("coverage"):
                # PrototypeReview uses its existing source-bound protocol. A
                # lookalike dictionary cannot bypass the shared consumer gate.
                from .prototype_review import validate_review as validate_prototype
                record = self.store.get(rid, "authority/prototype-reviews/" + digest(raw))
                if record and record.get("review") == raw:
                    validate_prototype(raw, record["basis"], record["source"])
                    if readonly or record.get("source_identity") == self.identity(rid):
                        continue
            if not self.reusable(rid, raw, current=False):
                if transitioned:
                    continue  # New independent old/new review, not rehabilitation of this historical verdict.
                if not readonly:
                    raise StateConflict("Legacy or ungrounded review needs reassessment before contract or source mutation")
                continue
            proof = self.receipt(rid, raw)
            if proof["basis"]["source_identity"] != self.identity(rid) and not readonly:
                if not transitioned or (not transition.get("reassessed_after")
                                         and proof["basis"]["source_identity"] != transition["before"]):
                    raise StateConflict("Review authority sources changed; reassessment is required before mutation")
            assessment = raw["authority"]
            owners = {item["owner"] for item in assessment["findings"] if item["kind"] == "source_dispute"}
            expected = {"plan_patch" if owner == "plan" else owner for owner in owners}
            if expected and action not in expected | {"diagnose", "recheck"} and not transitioned:
                raise StateConflict("Source dispute must return to its evidenced owner, not another repair scope")
