"""Durable recovery orchestration at a completed worker-batch boundary."""
from __future__ import annotations

from pathlib import Path

from .action_authority import STAGE_REENTRY_ACTIONS
from .builds import verification_commands
from .collection import policy_identity
from .design import PROTOTYPE_BROWSER_VERSION
from .errors import AA2Error, InvalidStructuredResponse
from .evidence import assess_observation
from .evidence_refs import controller_context_catalog
from .execution_stages import current_stage_contract, stage_contract
from .faults import HumanRequirement, ResourceBoundary, StateConflict, route_failure
from .interaction import choice_spec
from .models import CheckResult, ImplementationResult, PlanResult, ReviewResult
from .plan_revision import (
    PlanPatch,
    TimingPlanReview,
    apply_patch,
    descendants,
    materialize_plan,
    timing_review_binding,
    validate_timing_references,
    validate_timing_review,
)
from .recovery import (
    Diagnosis,
    FailureRecord,
    adjudicate,
    check_kind,
    check_rows,
    evidence_aliases,
    failure_summary,
    implemented_candidate,
)
from .source_history import SourceHistory
from .util import digest
from .workspace import git, prepared_before_hash, tree_hash


class RecoveryController:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def submit(self, rid, failure):
        active = self.store.get(rid, "recovery/active")
        if active:
            if active["failure"]["id"] != failure.id:
                raise StateConflict("Another recovery is pending; preserve both candidates")
            return
        history = self.store.get(rid, "recovery/count/" + failure.id, 0)
        failure = failure.model_copy(update={"version": 2,
            "observation_id": failure.observation_id or digest([rid, failure.id, history + 1])})
        self.store.checkpoints(rid, {
            "recovery/active": {"version": 2, "failure": failure.model_dump(), "attempt": history + 1},
            f"recovery/failures/{failure.id}/{history + 1}": failure.model_dump(),
            "recovery/count/" + failure.id: history + 1,
        })
        self.store.event(rid, "recovery.requested", {"failure": failure.id, "owner": failure.origin_id,
            "component": failure.component, "kind": failure.kind, "observation": failure.observation_id})
        self.store.record_fault(rid, {"failure": failure.id, "observation": failure.observation_id,
                                     **route_failure(failure).model_dump()})

    async def _capture_retry(self, rid, checks, work):
        failed = [row for row in checks if "evidence_collection_failed" in row.get("failure_reasons", [])]
        if not failed:
            return False
        identity = digest(failed)
        key = "evidence/capture-wait/" + identity
        requirement = HumanRequirement(kind="resource_limit", reason="测试附件未能完整归档，验收暂未通过。",
            action="调整 attachment_capture 上限或测试录制配置；候选代码、JUnit 和原始附件已保留。",
            resume_condition="采集配置或录制输入已变化；相同配置不会重复执行测试，也不会自动修改业务代码。")
        self.store.record_fault(rid, {"route": "human", "reason_code": "evidence_collection_failed", "phase": "verification",
                                     "human_requirement": requirement.model_dump()})
        while (all(row.get("capture_policy_hash") == policy_identity(self.store.root) for row in failed)
               and all(row.get("source_hash") == tree_hash(work) for row in failed)):
            count = self.store.get(rid, key, 0)
            await self.engine._ask(rid, f"evidence-capture/{identity}/{count}",
                requirement.prompt(),
                identity)
            self.store.checkpoint(rid, key, count + 1)
        return True

    async def delivery_decision(self, rid, work, feedback, scopes, *, observation=None):
        while True:
            action = await self._delivery_decision(rid, work, feedback, scopes, observation=observation)
            if action is not None:
                return action

    async def _delivery_decision(self, rid, work, feedback, scopes, *, observation=None):
        """Same fact policy, with delivery journal authority and its existing scope/budget."""
        capture_rows = check_rows(feedback)
        capture_retry = await self._capture_retry(rid, capture_rows, work)
        observation = digest([observation or tree_hash(work), feedback])
        key = "recovery/delivery/" + observation
        saved = self.store.get(rid, key)
        published = bool((self.store.get(rid, "delivery/journal", {}) or {}).get("publication_started"))
        policy, permit = self.engine.recovery_policy, None
        if saved and saved.get("decision", {}).get("action") == "repair":
            # The caller validates a partially applied repair against its saved
            # patch preimage. An already dispatched repair may legitimately edit.
            return saved["decision"]["action"]
        evidence_source = tree_hash(work)
        if saved and saved.get("evidence_source_hash", evidence_source) != evidence_source:
            raise StateConflict("Delivery recovery evidence source changed; preserve the candidate")
        if saved and saved.get("decision", {}).get("action") == "recheck" and saved.get("recheck_key"):
            reservation = self.store.get(rid, saved["recheck_key"], {})
            if reservation.get("phase") == "reserved" and reservation.get("observation") == observation:
                return "recheck"
            if reservation.get("phase") == "started":
                raise StateConflict("Interrupted delivery recheck lacks a completed observation; preserve its occupied reservation")
        evidence_ref = "recovery/delivery-evidence/" + observation
        self.store.checkpoint(rid, evidence_ref, feedback)
        kind = "integration" if feedback.get("conflicts") else "review"
        if capture_rows:
            kind = check_kind(capture_rows, default=kind)
        runtime_feedback = feedback.get("runtime_failure")
        runtime_feedback = runtime_feedback if isinstance(runtime_feedback, dict) else {}
        verification = feedback.get("verification_assessment", runtime_feedback.get("verification_assessment", {}))
        if verification.get("allowed") is False:
            kind = "evidence"
        checks = capture_rows
        recheck_identity = digest([tree_hash(work), sorted(scopes),
            [{k: row.get(k) for k in ("argv", "cwd", "command_hash", "environment_hash", "input_hash",
                                      "runtime_config_hash", "checker_version", "capture_policy_hash")} for row in checks],
            policy_identity(self.store.root)])
        recheck_key = "recovery/delivery-rechecks/" + recheck_identity
        already_rechecked = bool(self.store.get(rid, recheck_key))
        failure = FailureRecord.create(kind=kind, component="delivery", origin_id="delivery", step="delivery",
            phase="runtime" if published else "reintegration", observation_id=observation,
            obligations=sorted(scopes), evidence_refs=[evidence_ref], evidence={**feedback, "allowed_paths": scopes})
        count_key = "recovery/budgets/diagnosis/delivery"
        count = self.store.get(rid, count_key, self.store.get(rid, "recovery/diagnosis/" + failure.id, {"round": 0}))
        decision = adjudicate(failure, attempts=max(count["round"], int(bool(saved)), int(already_rechecked)))
        if capture_retry:
            decision = decision.model_copy(update={"action": "recheck", "reason": "Attachment capture configuration changed"})
        if feedback.get("conflicts"):
            decision = decision.model_copy(update={"action": "repair", "reason": "Repair recorded merge conflicts within controller scope"})
        active = None
        if not published:
            active_key = "recovery/delivery-policy/" + observation
            active = self.store.get(rid, active_key)
            previous_recheck = self.store.get(rid, (saved or {}).get("recheck_key", ""), {})
            if active is None or previous_recheck.get("phase") == "completed":
                number = self.store.get(rid, "recovery/delivery-policy-count", 0) + 1
                active = {"attempt": number, "failure": failure.model_dump()}
                self.store.checkpoints(rid, {active_key: active, "recovery/delivery-policy-count": number})
            active.update(diagnosis_feedback=(saved or {}).get("diagnosis_feedback"))
            if (saved or {}).get("selection"):
                from .recovery import RecoveryDecision
                decision = RecoveryDecision.model_validate(saved.pop("selection"))
                permit = saved.pop("selection_permit")
            elif not (saved or {}).get("round"):
                options = self._delivery_candidates(failure, decision)
                decision, permit = await policy.authorize(rid, failure, active, work, None, decision,
                    operation=f"delivery-choose/{count['round']}/{(saved or {}).get('diagnosis_errors', 0)}",
                    candidates=options, correction=(saved or {}).get("diagnosis_errors", 0) == 1,
                    prerequisite_resolved=capture_retry,
                    force_decision=(saved or {}).get("diagnosis_errors", 0) >= 2
                        or decision.action == "recheck" and already_rechecked)
            else:
                permit = saved.get("diagnosis_permit")
            policy.start(rid, permit)
        if decision.action == "diagnose":
            authorization_failure = failure
            project = self.engine.contexts.build(work)
            failure = failure.model_copy(update={"evidence_refs": evidence_aliases(failure, project)})
            saved = {**(saved or {}), "failure": failure.model_dump(), "evidence_source_hash": evidence_source}
            self.store.checkpoint(rid, key, saved)
            if not saved or not saved.get("round"):
                if published and (count["round"] >= self.engine.config.load().execution.max_diagnosis_rounds or saved.get("diagnosis_errors", 0) >= 2):
                    await self._grant(rid, count_key + f"/grant/{count['round']}",
                        "交付故障已达到诊断额度或诊断修正仍不合格。继续只增加一次独立诊断机会。\n" + self._summary(failure, saved))
                if tree_hash(work) != evidence_source:
                    raise StateConflict("Delivery recovery evidence source changed during diagnosis approval; preserve the candidate")
                count["round"] += 1
                saved = {**(saved or {}), "failure": failure.model_dump(), "round": count["round"],
                         "diagnosis_permit": permit}
                self.store.checkpoints(rid, {key: saved, count_key: count,
                    "recovery/diagnosis/" + failure.id: {"round": count["round"]}})
            diagnosis_context = {"review_kind": "recovery_diagnosis", "controller_stage_contract": stage_contract(),
                **self._approval(rid, work),
                "failure": failure.model_dump(), "project": project,
                "source_history": SourceHistory(self.engine).collect(rid, work, scopes),
                "diagnosis_feedback": saved.get("diagnosis_feedback"),
                "specification": self.store.get(rid, "clarify"), "delivery_journal": self.store.get(rid, "delivery/journal"),
                "constraint": "Delivery may only repair within supplied allowed_paths or recheck. Gate/plan changes require user reconciliation; never invalidate a published journal.",
                "allowed_actions": {name: action.model_dump() for name, action in
                    self._delivery_candidates(failure, adjudicate(failure), diagnosed=True).items()}}
            diagnosis = policy.completed_diagnosis(rid, permit, failure, active, work, None,
                authorization_failure=authorization_failure)
            schema_error = None
            if diagnosis is None:
                diagnosis, schema_error = await self._diagnose(diagnosis_context,
                    work, rid, f"recovery/diagnose/{failure.id}/{saved['round']}")
            if tree_hash(work) != evidence_source:
                raise StateConflict("Delivery recovery evidence source changed during diagnosis; preserve the candidate")
            try:
                if schema_error:
                    raise AA2Error(schema_error)
                decision = adjudicate(failure, diagnosis=diagnosis, evidence_context=diagnosis_context)
            except AA2Error as exc:
                rejection = {"diagnosis": diagnosis.model_dump() if diagnosis else None, "validation_error": str(exc)}
                self.store.checkpoints(rid, {key: {"failure": failure.model_dump(), "evidence_source_hash": evidence_source,
                    "diagnosis_feedback": rejection, "diagnosis_errors": saved.get("diagnosis_errors", 0) + 1},
                    f"recovery/diagnosis-rejections/{failure.id}/{saved['round']}": rejection})
                self._reject_diagnosis(rid, permit, rejection)
                return None
            policy.complete(rid, permit, {"status": "validated", "diagnosis": diagnosis.model_dump()})
            if not published:
                decision, assess = self._diagnosis_assessment(failure, decision)
                options = self._delivery_candidates(failure, decision, diagnosed=True)
                decision, permit = await policy.authorize(rid, failure, active, work, None, decision,
                    operation=f"delivery-dispatch/{saved['round']}", candidates=options, established=True,
                    diagnosis_assessment=assess, prerequisite_resolved=capture_retry,
                    force_decision=decision.action == "recheck" and already_rechecked)
                if decision.action == "diagnose":
                    saved.pop("round")
                    saved.update(selection=decision.model_dump(), selection_permit=permit)
                    self.store.checkpoint(rid, key, saved)
                    return None
        if decision.action not in {"repair", "recheck"}:
            self.store.checkpoint(rid, key, {"failure": failure.model_dump(), "evidence_source_hash": evidence_source, "blocked_decision": decision.model_dump()})
            raise StateConflict("交付恢复需要核对已保留的发布记录：" + decision.reason, phase="delivery")
        if tree_hash(work) != evidence_source:
            raise StateConflict("Delivery recovery evidence source changed before dispatch; preserve the candidate")
        updates = {key: {"failure": failure.model_dump(), "decision": decision.model_dump(),
                         "evidence_source_hash": evidence_source, "permit": permit}}
        if decision.action == "recheck":
            if already_rechecked and published:
                self.store.checkpoint(rid, key, {"failure": failure.model_dump(), "evidence_source_hash": evidence_source, "blocked_decision": decision.model_dump()})
                raise ResourceBoundary("同一交付输入已自动复验一次；保留证据并等待新的修复或输入",
                    action="核对发布日志并处理已记录的故障或检查输入。",
                    condition="控制器核验新的修复或输入；相同输入不重复消费发布后复验。", phase="runtime")
            if already_rechecked:
                recheck_key += "/" + digest(permit)
            updates[recheck_key] = {"observation": observation, "failure": failure.id, "phase": "reserved",
                                    "workspace": str(work), "source_hash": evidence_source, "permit": permit}
            updates[key]["recheck_key"] = recheck_key
            updates["recovery/delivery-recheck-active"] = recheck_key
        else:
            updates["recovery/delivery-action"] = permit
        self.store.checkpoints(rid, updates)
        policy.start(rid, permit)
        self.store.event(rid, "recovery.decided", decision.model_dump())
        return decision.action

    @staticmethod
    def _delivery_candidates(failure, suggested, *, diagnosed=False):
        from .recovery import RecoveryDecision
        if suggested.action not in {"diagnose", "repair", "recheck"}:
            return {suggested.action: suggested}
        choices = {suggested.action: suggested}
        for action in (("repair", "recheck") if diagnosed else ("diagnose", "repair", "recheck")):
            choices.setdefault(action, RecoveryDecision(failure_id=failure.id, action=action,
                budget_owner="delivery", evidence_refs=failure.evidence_refs,
                reason="在控制器提供的交付候选路径范围内处理当前故障"))
        return choices

    def complete_delivery_action(self, rid, permit, outcome):
        self.engine.recovery_policy.complete(rid, permit, outcome)
        record = self.store.get(rid, permit) if permit else None
        if record:
            key = "recovery/cycles/" + record["cycle_id"]
            cycle = self.store.get(rid, key)
            self.store.checkpoint(rid, key, {**cycle, "phase": "completed", "outcomes": outcome})

    def start_delivery_recheck(self, rid, work, *, completed_evidence=None):
        key = self.store.get(rid, "recovery/delivery-recheck-active")
        if not key:
            return
        saved = self.store.get(rid, key, {})
        if saved.get("phase") != "reserved":
            observation = self.store.get(rid, completed_evidence, {}) if completed_evidence else {}
            if (saved.get("phase") != "started" or observation.get("phase") != "completed"
                    or observation.get("source_hash") != tree_hash(work)):
                raise StateConflict("Interrupted delivery recheck has no durable completion; reservation remains occupied")
        if saved.get("workspace") != str(work) or saved.get("source_hash") != tree_hash(work):
            raise StateConflict("Reserved delivery recheck input changed; preserve recovery evidence")
        self.store.checkpoint(rid, key, {**saved, "phase": "started"})

    def complete_delivery_recheck(self, rid, outcome):
        key = self.store.get(rid, "recovery/delivery-recheck-active")
        if not key:
            return
        saved = self.store.get(rid, key, {})
        if saved.get("phase") == "started":
            self.store.checkpoints(rid, {key: {**saved, "phase": "completed", "outcome": outcome},
                                        "recovery/delivery-recheck-active": None})
            self.complete_delivery_action(rid, saved.get("permit"), {"status": "observed", "outcome": outcome})

    def _approval(self, rid, work):
        prototype = self.store.get(rid, "prototype", {})
        approval = self.engine.approvals.current(rid, work, prototype.get("browser_actions", []), PROTOTYPE_BROWSER_VERSION) if prototype else None
        return {"prototype_approval": self.engine.approvals.context(approval)} if approval else {}

    async def resume(self, rid):
        engine = self.engine
        active = self.store.get(rid, "recovery/active")
        if not active:
            return
        failure = FailureRecord.model_validate(active["failure"])
        plan_data = self.store.get(rid, "plan")
        plan = PlanResult.model_validate(plan_data) if plan_data else None
        integration = self.store.root / "worktrees" / rid / "integration"
        task_state = self.store.get(rid, "develop/task/" + failure.task_id, {})
        work = Path(task_state.get("workspace", integration))
        if not work.is_dir():
            work = integration
        if not work.is_dir():
            raise StateConflict("Recovery workspace missing; restore the recorded worktree before continuing")
        capture_retry = await self._capture_retry(rid, failure.evidence.get("checks", []), work)
        if capture_retry:
            active.pop("decision", None)
        if failure.component == "legacy_adapter" and failure.task_id and not active.get("evidence_reconciled"):
            task = next(t for t in plan.tasks if t.id == failure.task_id)
            commands = verification_commands(task.checks, work, plan.runtime_prepare)
            rows = [CheckResult.model_validate(c) for c in failure.evidence.get("checks", [])]
            source = tree_hash(work)
            if rows and any(c.source_hash != source for c in rows):
                raise StateConflict("Retained candidate changed since its recorded checks; preserve user changes before recovery")
            environment = await engine._environment(work, rid, plan)
            matching = {digest(c.model_dump()): c for c in commands}
            if (set(matching) != {row.command_hash for row in rows} or len(rows) != len(matching)
                    or any(row.command_hash not in matching or not assess_observation(
                        matching[row.command_hash], row, work, environment, engine.artifacts).valid for row in rows)):
                rows = await engine.verifier.run(commands, work, environment)
            engine._replan_checks(rid, "task/" + failure.origin_id, commands, rows, work, environment)
            failure.evidence["checks"] = [c.model_dump() for c in rows]
            active.update(failure=failure.model_dump(), evidence_reconciled=True, evidence_source_hash=source)
            self.store.checkpoints(rid, {"recovery/active": active,
                "develop/task/" + task.id: {**task_state, "workspace": str(work), "source_hash": source}})
        if active.get("evidence_source_hash") and active["evidence_source_hash"] != tree_hash(work):
            journal = self.store.get(rid, f"plan_revision/journal/{failure.id}/{active['attempt']}", {})
            document = work / "docs/aa2/plan.md"
            owned = False
            if (active.get("decision", {}).get("action") == "plan_patch" and journal.get("phase") == "activated"
                    and document.is_file() and not document.is_symlink()
                    and document.read_text() == journal["plan"]["document"]):
                preimages = ([journal["document_preimage"]] if "document_preimage" in journal else
                    [{"path": "docs/aa2/plan.md", "before": before, "mode": 0o644}
                     for before in (digest(journal["previous_plan"]["document"]), None)])
                owned = any(prepared_before_hash(work, [item]) == active["evidence_source_hash"] for item in preimages)
            if not owned:
                raise StateConflict("Recovery evidence source changed; preserve the candidate before reassessing the decision")
        # A legacy plan hold constrains a plan action, not diagnosis or scoped repair.
        # Keep it journalled until the actual action is known.
        # Pending legacy questions are retired atomically with an effective permit.
        from .recovery import RecoveryDecision
        policy = engine.recovery_policy
        project = engine.contexts.build(work)
        failure = failure.model_copy(update={"evidence_refs": evidence_aliases(failure, project)})
        failure_data = failure.model_dump() if failure.reason_code else failure.model_dump(exclude={"reason_code"})
        active["failure"] = failure_data
        active.setdefault("evidence_source_hash", tree_hash(work))
        self.store.checkpoint(rid, "recovery/active", active)
        budget_key = "recovery/budgets/diagnosis/" + failure.origin_id
        budget = self.store.get(rid, budget_key,
            self.store.get(rid, "recovery/diagnosis/" + failure.id, {"round": 0}))

        def recheck_identity():
            return digest([failure.origin_id, tree_hash(work), failure.evidence.get("commands"),
                [{k: c.get(k) for k in ("cwd", "argv", "environment_hash", "input_hash", "runtime_config_hash",
                    "checker_version", "capture_policy_hash")} for c in failure.evidence.get("checks", [])],
                policy_identity(self.store.root)])

        def repeated_recheck():
            previous = self.store.get(rid, "recovery/rechecks/" + recheck_identity())
            return bool(previous and (previous.get("failure"), previous.get("attempt")) !=
                        (failure.id, active["attempt"]))

        if active.get("decision"):
            decision = RecoveryDecision.model_validate(active["decision"])
            if not active.get("permit"):
                decision, active["permit"] = await policy.authorize(rid, failure, active, work, plan, decision,
                    operation="legacy-dispatch", established=True, prerequisite_resolved=capture_retry,
                    force_decision=decision.action == "recheck" and repeated_recheck())
        else:
            if active.get("selection"):
                decision = RecoveryDecision.model_validate(active.pop("selection"))
                permit = active.pop("selection_permit")
            elif active.get("diagnosis_round"):
                decision = adjudicate(failure).model_copy(update={"action": "diagnose"})
                permit = active.get("diagnosis_permit")
            else:
                suggested = adjudicate(failure, attempts=active["attempt"] - 1)
                if failure.kind == "integration" and failure.evidence.get("baseline_refresh", {}).get("retained_candidate"):
                    suggested = suggested.model_copy(update={"action": "repair", "reason":
                        "控制器已保留冲突候选，在当前依赖基线按原任务范围修复"})
                if capture_retry:
                    suggested = suggested.model_copy(update={"action": "recheck", "reason": "Attachment capture configuration changed"})
                decision, permit = await policy.authorize(rid, failure, active, work, plan, suggested,
                    operation=f"choose/{budget['round']}/{active.get('diagnosis_errors', 0)}",
                    correction=active.get("diagnosis_errors", 0) == 1, prerequisite_resolved=capture_retry,
                    force_decision=failure.component == "preview_verification" or active.get("diagnosis_errors", 0) >= 2
                        or suggested.action == "recheck" and repeated_recheck())
            if decision.action == "diagnose":
                if not active.get("diagnosis_round"):
                    if tree_hash(work) != active["evidence_source_hash"]:
                        raise StateConflict("Recovery evidence source changed during diagnosis approval; preserve the candidate")
                    budget["round"] += 1
                    active.update(diagnosis_round=budget["round"], diagnosis_permit=permit)
                    self.store.checkpoints(rid, {budget_key: budget, "recovery/active": active,
                        "recovery/diagnosis/" + failure.id: {"round": budget["round"]}})
                policy.start(rid, permit)
                diagnosis_context = {
                    "controller_stage_contract": stage_contract(),
                    "review_kind": "recovery_diagnosis", "failure": failure_data,
                    "plan": plan_data, "project": project,
                    "specification": self.store.get(rid, "clarify"), "design": self.store.get(rid, "design"),
                    "prototype_approval": self.store.get(rid, "prototype_approval"),
                    "task_budgets": self.store.task_origin_state(rid),
                    "diagnosis_feedback": active.get("diagnosis_feedback"),
                    "current_issues": self.store.get(rid, "issues", {}),
                    "observations": self.store.get(rid, "recovery/observations/" + failure.origin_id, []),
                    "allowed_actions": policy.diagnosis_actions(rid, failure, work, plan),
                }
                scopes = sorted({path for task in plan.tasks if not failure.task_id or task.id == failure.task_id
                                 for path in task.allowed_paths}) if plan else []
                diagnosis_context["source_history"] = SourceHistory(engine).collect(rid, work, scopes)
                from .preview_evidence import PreviewEvidence
                if plan and plan.preview:
                    diagnosis_context["preview_contracts"] = PreviewEvidence(engine).contracts(rid, plan)
                progress_operation = f"recovery/diagnose/{failure.id}/{active['diagnosis_round']}"
                progress_key = "recovery/diagnosis-progress-basis/" + progress_operation
                progress_basis = self.store.get(rid, progress_key)
                if progress_basis is None:
                    progress_basis = policy.progress.basis(diagnosis_context, tree_hash(work), "diagnose")
                    self.store.checkpoint(rid, progress_key, progress_basis)
                diagnosis = policy.completed_diagnosis(rid, permit, failure, active, work, plan)
                completed_diagnosis = diagnosis is not None
                schema_error = None
                if diagnosis is None:
                    diagnosis, schema_error = await self._diagnose(diagnosis_context,
                        work, rid, f"recovery/diagnose/{failure.id}/{active['diagnosis_round']}")
                if tree_hash(work) != active["evidence_source_hash"]:
                    raise StateConflict("Recovery evidence source changed during diagnosis; preserve the candidate")
                try:
                    if schema_error:
                        raise AA2Error(schema_error)
                    assessment_context = diagnosis_context
                    if completed_diagnosis and diagnosis.action in {"diagnose", "blocked"}:
                        # A validated inconclusive outcome remains a fact; it does not authorize another call.
                        assessment_context = {**diagnosis_context, "allowed_actions": {
                            **diagnosis_context["allowed_actions"], "diagnose": self.store.get(rid, permit)["action"]}}
                    suggested = adjudicate(failure, attempts=active["attempt"] - 1, diagnosis=diagnosis,
                        plan=plan, evidence_context=assessment_context)
                except AA2Error as exc:
                    rejection = {"diagnosis": diagnosis.model_dump() if diagnosis else None, "validation_error": str(exc)}
                    rejected_round = active.pop("diagnosis_round")
                    active["diagnosis_feedback"] = rejection
                    active["diagnosis_errors"] = active.get("diagnosis_errors", 0) + 1
                    self.store.checkpoints(rid, {"recovery/active": active,
                        f"recovery/diagnosis-rejections/{failure.id}/{rejected_round}": rejection})
                    self._reject_diagnosis(rid, permit, rejection)
                    return
                policy.complete(rid, permit, {"status": "validated", "diagnosis": diagnosis.model_dump()})
                if diagnosis.action in {"diagnose", "blocked"}:
                    policy.progress.record(rid, failure.origin_id, "diagnose", progress_basis, progress_operation,
                        {"status": "inconclusive", "evidence_ref": "invocation/" + progress_operation,
                         "diagnosis": diagnosis.model_dump()})
                active.pop("diagnosis_feedback", None)
                active.pop("diagnosis_errors", None)
                suggested, assess = self._diagnosis_assessment(failure, suggested)
                decision, permit = await policy.authorize(rid, failure, active, work, plan, suggested,
                    operation=f"dispatch/{active['diagnosis_round']}", established=True,
                    diagnosis_assessment=assess, prerequisite_resolved=capture_retry,
                    force_decision=suggested.action == "recheck" and repeated_recheck())
                if decision.action == "diagnose":
                    active.pop("diagnosis_round")
                    active.update(selection=decision.model_dump(), selection_permit=permit)
                    self.store.checkpoint(rid, "recovery/active", active)
                    return
            active["permit"] = permit
        active["decision"] = decision.model_dump()
        active["actor"] = (self.store.get(rid, active.get("permit", ""), {}) or {}).get("actor", "rule")
        self.store.checkpoint(rid, "recovery/active", active)
        policy.start(rid, active.get("permit"))
        if decision.action == "recheck":
            key = "recovery/rechecks/" + recheck_identity()
            reservation = {"failure": failure.id, "attempt": active["attempt"], "permit": active.get("permit")}
            updates = {"recovery/recheck-actions/" + digest([key, active.get("permit")]): reservation}
            if not self.store.get(rid, key):
                updates[key] = reservation
            active["recheck_reservation"] = key
            self.store.checkpoints(rid, {**updates, "recovery/active": active})
        self.store.event(rid, "recovery.decided", {**decision.model_dump(), "actor": active["actor"]})
        if decision.action == "blocked":
            self._finish(rid, active, {}, successor=failure.model_dump())
            return
        if decision.action == "prototype_recheck":
            from .prototype_recheck import recheck_prototype
            successor = await recheck_prototype(engine, rid, work, active["permit"], failure)
            if successor:
                self._finish(rid, active, {}, successor=successor.model_dump())
                return
            # Resume acceptance of the same candidate. Reuse production checks
            # only through the normal source/environment/report validity gate.
            key = "develop/task/" + failure.task_id
            saved = self.store.get(rid, key, {})
            refreshed = self.store.get(rid, "recovery/prototype-rechecks/" + digest(active["permit"]))
            updates = {key: {**saved, "phase": "tested", "recovery_permit": active["permit"],
                "implementation": ImplementationResult(summary="Retained candidate after prototype evidence refresh").model_dump(),
                "feedback": {**saved.get("feedback", {}), "controller_recovery": {**decision.model_dump(),
                    "prototype_browser_report": refreshed["report"], "independent_review": refreshed["review"]}}},
                "verify/identity": None}
            updates.update(policy.dispatch_updates(rid, active, [failure.task_id]))
            self._finish(rid, active, updates)
            return
        if decision.action == "preview_recheck":
            from .preview_evidence import PreviewEvidence
            successor = await PreviewEvidence(engine).recheck(rid, work, plan, active["permit"], failure)
            if successor:
                self._finish(rid, active, {}, successor=successor.model_dump())
                return
            key = "develop/task/" + failure.task_id
            saved = self.store.get(rid, key, {})
            refreshed = self.store.get(rid, "recovery/preview-rechecks/" + digest(active["permit"]))
            task = next(t for t in plan.tasks if t.id == failure.task_id)
            commands = {digest(c.model_dump()) for c in engine._task_commands(rid, task, work, plan)}
            updates = {key: {**saved, "phase": "tested", "recovery_permit": active["permit"],
                "implementation": ImplementationResult(summary="Retained candidate after production preview evidence collection").model_dump(),
                "preview_permit": active["permit"],
                "source_hash": refreshed["execution"]["source_hash"],
                "checks": [row for row in refreshed["checks"] if row["command_hash"] in commands],
                "feedback": {**saved.get("feedback", {}), "controller_recovery": {**decision.model_dump(),
                    "preview_report": refreshed["report"], "contract_comparison": refreshed["comparison"]}}},
                "verify/identity": None}
            updates.update(policy.dispatch_updates(rid, active, [failure.task_id]))
            self._finish(rid, active, updates)
            return
        if decision.action == "plan_patch":
            if not plan:
                raise AA2Error("No accepted plan exists to amend")
            if await self._patch(rid, active, failure, plan, integration):
                journal = self.store.get(rid, f"plan_revision/journal/{failure.id}/{active['attempt']}", {})
                active.setdefault("patch_permit", active.get("permit"))
                policy.complete(rid, active["patch_permit"], {"status": "applied", "plan_revision":
                    self.store.get(rid, "plan_revision/current", {}).get("revision")})
                # Renamed unfinished tasks and retained descendants also need a
                # permit after their baseline is refreshed, even without a new goal.
                targets = list(journal.get("affected", []))
                revised = PlanResult.model_validate(self.store.get(rid, "plan"))
                if targets:
                    continuation = RecoveryDecision(failure_id=failure.id, action="repair", task_ids=targets,
                        budget_owner=failure.origin_id, budget_owners=sorted({t.origin_id or t.id for t in revised.tasks if t.id in targets}),
                        evidence_refs=failure.evidence_refs, reason="按已通过独立审查的局部计划修订实施并复验")
                    _, active["permit"] = await policy.authorize(rid, failure, active, integration, revised, continuation,
                        operation="patch-followup", established=True, candidates={"repair": continuation},
                        prerequisite_resolved=capture_retry)
                    self.store.checkpoint(rid, "recovery/active", active)
                updates = {"recovery/implementation_pending": True}
                for task_id in targets:
                    key = "develop/task/" + task_id
                    saved = self.store.get(rid, key, {})
                    updates[key] = {**saved, "recovery_permit": active.get("permit")}
                if targets:
                    updates.update(policy.dispatch_updates(rid, active, targets))
                self._finish(rid, active, updates)
            return
        if decision.action in STAGE_REENTRY_ACTIONS:
            self._finish(rid, active, {}, replan=(decision.action, decision.reason))
            pending = self.store.get(rid, "replanning/pending")
            self.store.checkpoint(rid, "replanning/pending", {**pending, "granted": True})
            await engine._resume_replan(rid)
            return
        if decision.action not in {"repair", "recheck"}:
            raise AA2Error("Recovery has no executable action")
        if active.get("evidence_source_hash") and tree_hash(work) != active["evidence_source_hash"]:
            raise StateConflict("Recovery evidence source changed before dispatch; preserve the candidate")
        affected = descendants(plan, decision.task_ids) if plan else set()
        updates = {}
        done = set(self.store.get(rid, "develop/done", []))
        for task_id in affected:
            key = "develop/task/" + task_id
            saved = self.store.get(rid, key, {})
            state_key = "task_state/" + task_id
            state = self.store.get(rid, state_key, {"version": 1, "revision": 1})
            retained_repair = task_id not in decision.task_ids and state.get("implementation") == "needs_repair"
            repair = decision.action == "repair" and task_id in decision.task_ids or retained_repair
            accepted = task_id in done or bool(saved.get("commit"))
            if task_id not in decision.task_ids and not implemented_candidate(saved, accepted):
                continue  # Pending descendants still need their first implementation.
            state.update(implementation="needs_repair" if repair else ("accepted" if accepted else "pending"), evidence="stale")
            if accepted:
                state["revision"] += 1
                retained = {k: saved[k] for k in ("workspace", "source_hash", "recovery_ref", "baseline")
                            if k in saved} if task_id not in done else {}
                saved = {**retained, "previous_evidence": saved}
            feedback = dict(saved.get("feedback", {}))
            if retained_repair:
                feedback["upstream_recovery"] = decision.model_dump()
            else:
                feedback.update(controller_recovery=decision.model_dump(), failure=failure.model_dump())
            detection = feedback.get("failure", {})
            if detection.get("task_id") == task_id and detection.get("evidence", {}).get("checks"):
                feedback["checks"] = detection["evidence"]["checks"]
            updates[key] = {**saved, "phase": "rejected" if repair else "reverify", "revision": state["revision"],
                            "feedback": feedback,
                            "recovery_permit": saved.get("recovery_permit") if retained_repair else active.get("permit")}
            if not repair:
                updates[key]["observation_id"] = ["recheck", failure.id, active["attempt"], task_id]
            if detection.get("task_id") == task_id and detection.get("evidence", {}).get("checks"):
                updates[key]["checks"] = detection["evidence"]["checks"]
            # Never replay a proposal that requested recovery, including its unapplied changes.
            updates[key].pop("implementation", None)
            updates[key].pop("patch", None)
            updates[state_key] = state
        updates["develop/done"] = sorted(done - affected)
        updates["verify/identity"] = None
        if affected:
            updates["recovery/implementation_pending"] = True
        targets = [key.removeprefix("develop/task/") for key, value in updates.items()
                   if key.startswith("develop/task/") and value.get("recovery_permit") == active.get("permit")]
        updates.update(policy.dispatch_updates(rid, active, targets))
        self._finish(rid, active, updates)

    def _reject_diagnosis(self, rid, permit, rejection):
        policy = self.engine.recovery_policy
        if policy.completed_outcome(rid, permit, action="diagnose") is None:
            policy.complete(rid, permit, {"status": "invalid", "feedback": rejection})
        else:
            # Old versions validated schema/citations without checking action eligibility.
            # Keep that completed receipt immutable; the rejection is a new observation.
            self.store.event(rid, "recovery.diagnosis_rejected", {"permit": permit,
                "reason": rejection["validation_error"], "previous_outcome_preserved": True})

    async def _diagnose(self, context, work, rid, key):
        try:
            catalog = controller_context_catalog(context)
            return await self.engine._invoke("recovery_diagnosis", {**context,
                "evidence_reference_contract": catalog.contract(context)}, Diagnosis, work, rid, key, fresh=True), None
        except InvalidStructuredResponse:
            return None, "诊断返回格式不符合 Diagnosis schema；请按请求的完整结构修正。"

    @staticmethod
    def _diagnosis_assessment(failure, suggested):
        # An inconclusive model diagnosis cannot declare a human-only boundary.
        # Controller resource/collection guards remain authoritative.
        assess = suggested.action in {"diagnose", "blocked"} and adjudicate(failure).action != "blocked"
        if assess:
            suggested = suggested.model_copy(update={"action": "diagnose"})
        return suggested, assess

    @staticmethod
    def _summary(failure, active):
        evidence = failure.evidence
        feedback = evidence.get("feedback", {})
        return failure_summary(check_rows(evidence),
            candidate_error=evidence.get("candidate_error", feedback.get("candidate_error")),
            validation_error=evidence.get("validation_error", feedback.get("validation_error")),
            review=evidence.get("review", feedback.get("review")),
            environment_error=evidence.get("environment_error", feedback.get("environment_error")),
            diagnosis_feedback=active.get("diagnosis_feedback"))

    async def _grant(self, rid, key, reason):
        interaction = choice_spec(["授权这一次操作", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
        interaction["pause_on"] = "n"
        await self.engine._ask(rid, key, reason, digest([key, reason]), interaction)

    def _finish(self, rid, active, updates, *, replan=None, successor=None):
        # State transition and completion receipt share a SQLite transaction.
        pending = self.store.get(rid, "replanning/pending")
        permit_key = active.get("permit")
        permit = self.store.get(rid, permit_key) if permit_key else None
        if permit and (successor is not None or replan is not None):
            outcome = {"status": "rejected" if successor is not None else "gate_requested",
                       "evidence": "recovery/completed/" + active["failure"]["id"] + f"/{active['attempt']}"}
            cycle_key = "recovery/cycles/" + permit["cycle_id"]
            cycle = self.store.get(rid, cycle_key)
            updates = {**updates, permit_key: {**permit, "phase": "completed", "outcome": outcome},
                       cycle_key: {**cycle, "phase": "completed", "outcomes": outcome}}
        previous_tasks = {key: self.store.get(rid, key) for key in updates if key.startswith("develop/task/")}
        self.store.finish_recovery(rid, {**updates, "recovery/active": None,
            f"recovery/completed/{active['failure']['id']}/{active['attempt']}": {
                **active, "phase": "dispatched", "legacy_pending": pending, "previous_tasks": previous_tasks},
            "replanning/pending": None}, pending, replan=replan, successor=successor)

    async def _patch(self, rid, active, failure, plan, work):
        engine = self.engine
        identity = f"{failure.id}/{active['attempt']}"
        journal_key = "plan_revision/journal/" + identity
        journal = self.store.get(rid, journal_key)
        if journal:
            if journal["phase"] != "activated" and (tree_hash(work) != journal["source_hash"]
                    or git(work, "rev-parse", "HEAD") != journal["base_commit"]):
                raise StateConflict("Integration changed before patch activation; preserved journal requires reconciliation")
            if journal["phase"] != "activated":
                for snapshot in journal.get("snapshots", {}).values():
                    path = Path(snapshot["workspace"])
                    if not path.is_dir() or tree_hash(path) != snapshot["source_hash"]:
                        raise StateConflict("Retained candidate changed before activation; reconcile the saved snapshot first")
            self.store.activate_plan_patch(rid, identity)
            return True
        # Charge only an actual amendment, retaining the historical progress barrier.
        pending = self.store.prepare_replan(rid, "plan", active["decision"]["reason"])
        if pending["reason"] != active["decision"]["reason"]:
            pending = {**pending, "reason": active["decision"]["reason"], "legacy_request": pending,
                       "stage": "plan", "granted": False}
            self.store.checkpoint(rid, "replanning/pending", pending)
        if failure.evidence.get("checks"):
            pending["verification_recheck"] = {"checks": failure.evidence["checks"]}
            self.store.checkpoint(rid, "replanning/pending", pending)
        baseline_key = "recovery/patch-baseline/" + identity
        baseline = self.store.get(rid, baseline_key)
        if baseline is None:
            baseline = {"source_hash": tree_hash(work), "base_commit": git(work, "rev-parse", "HEAD")}
            self.store.checkpoint(rid, baseline_key, baseline)
        # Intent precedes every controller-owned Git mutation. Reconcile it before
        # comparing the baseline, including interruption immediately after commit.
        snapshot_key = "recovery/patch-snapshots/" + identity
        saved_snapshots = self.store.get(rid, snapshot_key, {})
        for snapshot in saved_snapshots.values():
            path = Path(snapshot["workspace"])
            if not snapshot.get("commit"):
                snapshot["commit"] = engine.workspaces.finish_checkpoint(path, snapshot["intent"])
            if path.resolve() == work.resolve():
                baseline["base_commit"] = snapshot["commit"]
        self.store.checkpoints(rid, {snapshot_key: saved_snapshots, baseline_key: baseline})
        await engine._grant_replan(rid, pending)
        revision = self.store.get(rid, "plan_revision/current", {}).get("revision", 1)
        def unchanged(proposal):
            if tree_hash(work) != baseline["source_hash"] or git(work, "rev-parse", "HEAD") != baseline["base_commit"]:
                self.store.checkpoint(rid, "recovery/patch-drift/" + identity, {**baseline, **proposal})
                raise StateConflict("Integration baseline changed during plan amendment; preserved proposal requires reconciliation")
        unchanged({"phase": "approved"})
        context_key = "recovery/patch-context/" + identity
        recovery_context = self.store.get(rid, context_key)
        if recovery_context is None:
            targets = set(active["decision"].get("task_ids", []))
            if failure.task_id:
                targets.add(failure.task_id)
            scopes = sorted({path for task in plan.tasks if not targets or task.id in targets
                             for path in task.allowed_paths})
            recovery_context = {"controller_stage_contract": stage_contract(), "recovery_decision": active["decision"], "design": self.store.get(rid, "design"),
                                "source_history": SourceHistory(engine).collect(rid, work, scopes)}
            # Author and reviewer must share the same selected recovery objective
            # and evidence, including after snapshot creation or interruption.
            self.store.checkpoint(rid, context_key, recovery_context)
        if ("controller_stage_contract" in recovery_context
                and not current_stage_contract(recovery_context["controller_stage_contract"])):
            raise StateConflict("Controller stage contract changed; preserve the frozen plan amendment for reconciliation")
        patch_context = {**recovery_context, "plan_patch": True, "base_revision": revision,
            "plan": plan.model_dump(), "failure": failure.model_dump(), "task_origins": self.store.task_origin_state(rid),
            "specification": self.store.get(rid, "clarify"), "project": engine.contexts.build(work)}
        patch_context["evidence_reference_contract"] = controller_context_catalog(
            patch_context, provenance={"patch": "proposal"}).contract(patch_context)
        progress_key = "recovery/patch-progress-basis/" + identity
        if self.store.get(rid, progress_key) is None:
            progress_context = {**patch_context, "prototype_approval": self.store.get(rid, "prototype_approval")}
            self.store.checkpoint(rid, progress_key, engine.recovery_policy.progress.basis(
                progress_context, tree_hash(work), "plan_patch"))
        patch = await engine._invoke("plan", patch_context, PlanPatch, work, rid, "recovery/patch/" + identity, fresh=True)
        unchanged({"patch": patch.model_dump()})
        try:
            validate_timing_references(patch, patch_context)
            coverage_verified = False
            if plan.mode == "verification_only":
                from .models import IntakeResult
                from .requirements import validate_coverage
                from .verification import passed
                assessment = IntakeResult.model_validate(self.store.get(rid, "clarify"))
                checks = assessment.verification or engine.config.load().verification
                environment = await engine._environment(work, rid, plan)
                evidence = await engine.verifier.run(checks, work, environment) if checks else []
                engine._replan_checks(rid, "coverage", checks, evidence, work, environment)
                satisfied, _ = validate_coverage(assessment, work)
                coverage_verified = satisfied and bool(evidence) and passed(evidence)
                self.store.checkpoint(rid, "coverage_checks", [r.model_dump() for r in evidence])
            candidate, affected, reopen = apply_patch(plan, patch, revision, {failure.id},
                self.store.get(rid, "task_origins", {}), coverage_verified=coverage_verified)
            for task in candidate.tasks:
                verification_commands(task.checks, work, candidate.runtime_prepare)
            verification_commands([*candidate.final_checks, *candidate.runtime_probes], work, candidate.runtime_prepare, prepare_all=True)
        except AA2Error as exc:
            self._reject_patch(rid, active, failure, identity, {"patch": patch.model_dump(), "validation_error": str(exc)})
            return False
        snapshots, candidates = {}, {}
        for task_id in affected | set(patch.remove_tasks):
            saved = self.store.get(rid, "develop/task/" + task_id, {})
            path = Path(saved["workspace"]) if saved.get("workspace") else None
            if path and path.is_dir():
                if saved.get("source_hash") and tree_hash(path) != saved["source_hash"]:
                    raise StateConflict("Task source changed before plan amendment; preserve user changes and reconcile first")
                ref = f"refs/aa2/{rid}/plan-patches/{revision}/{task_id}"
                snapshot = saved_snapshots.get(task_id)
                if snapshot is None:
                    snapshot = {"workspace": str(path), "ref": ref, "source_hash": tree_hash(path),
                                "intent": engine.workspaces.checkpoint_intent(path, ref)}
                    saved_snapshots[task_id] = snapshot
                    self.store.checkpoint(rid, snapshot_key, saved_snapshots)
                if not snapshot.get("commit"):
                    snapshot["commit"] = engine.workspaces.finish_checkpoint(path, snapshot["intent"])
                snapshots[task_id] = snapshot
                if path.resolve() == work.resolve():
                    baseline["base_commit"] = snapshot["commit"]
                self.store.checkpoints(rid, {snapshot_key: saved_snapshots, baseline_key: baseline})
                candidates[task_id] = {"state": saved, "project": engine.contexts.build(path)}
        review_context = {**recovery_context, "review_kind": "plan_patch", "previous_plan": plan.model_dump(),
            "patch": patch.model_dump(), "plan": candidate.model_dump(), "failure": failure.model_dump(),
            "retained_candidates": candidates,
            "affected": sorted(affected), "reopen": sorted(reopen), "specification": self.store.get(rid, "clarify"),
            "project": engine.contexts.build(work)}
        review_context["evidence_reference_contract"] = controller_context_catalog(
            review_context, provenance={"patch": "proposal", "plan": "proposal"}).contract(review_context)
        review = await engine._invoke("review", review_context, TimingPlanReview if patch.timing_corrections else ReviewResult,
            work, rid, "recovery/patch-review/" + identity, fresh=True)
        unchanged({"patch": patch.model_dump(), "review": review.model_dump()})
        if not review.approved:
            # Preserve both the rejected proposal and its review. A new bounded
            # diagnosis decides what to do; replaying this cached patch cannot help.
            self._reject_patch(rid, active, failure, identity, {"patch": patch.model_dump(), "patch_review": review.model_dump()})
            return False
        try:
            validate_timing_review(patch, review, review_context)
        except AA2Error as exc:
            self._reject_patch(rid, active, failure, identity, {"patch": patch.model_dump(),
                "patch_review": review.model_dump(), "validation_error": str(exc)})
            return False
        for task_id, snapshot in snapshots.items():
            if tree_hash(Path(snapshot["workspace"])) != snapshot["source_hash"]:
                raise StateConflict("Task source changed during plan review; preserve user changes before activation")
        document = work / "docs/aa2/plan.md"
        journal = {"version": 1, "phase": "prepared", "base_revision": revision, "previous_plan": plan.model_dump(),
            "plan": candidate.model_dump(), "patch": patch.model_dump(), "review": review.model_dump(),
            "affected": sorted(affected), "reopen": sorted(reopen), "reason": patch.reason,
            "snapshots": snapshots, **baseline,
            "document_preimage": {"path": "docs/aa2/plan.md", "before": digest(document.read_bytes()) if document.is_file() else None,
                                  "mode": document.stat().st_mode & 0o7777 if document.is_file() else 0o644}}
        if patch.timing_corrections:
            if "controller_stage_contract" in recovery_context:
                journal["controller_stage_contract"] = recovery_context["controller_stage_contract"]
            journal["timing_review_binding"] = timing_review_binding(
                journal["previous_plan"], journal["patch"], journal["plan"], journal["review"],
                journal.get("controller_stage_contract"))
        self.store.checkpoint(rid, journal_key, journal)
        self.store.activate_plan_patch(rid, identity)
        # A derived document is written only after activation. Startup materializes
        # it again from the accepted plan; the journal is the recovery authority.
        materialize_plan(self.store, rid, work, candidate)
        return True

    def _reject_patch(self, rid, active, failure, identity, evidence):
        key = "recovery/patch-rejection/" + identity
        self.store.checkpoint(rid, key, evidence)
        if evidence.get("patch", {}).get("timing_corrections"):
            basis = self.store.get(rid, "recovery/patch-progress-basis/" + identity)
            if basis:
                self.engine.recovery_policy.progress.record(rid, failure.origin_id, "plan_patch", basis,
                    "recovery/patch/" + identity, {"status": "rejected", "evidence_ref": key})
        successor = failure.model_copy(update={"evidence": {**failure.evidence, **evidence},
            "evidence_refs": [*failure.evidence_refs, key]})
        self._finish(rid, active, {}, successor=successor.model_dump())
