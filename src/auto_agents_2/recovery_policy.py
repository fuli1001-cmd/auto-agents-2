"""Action authorization over facts; cumulative counts do not replace action availability."""
from __future__ import annotations

from typing import Literal

from .action_authority import STAGE_REENTRY_ACTIONS, route_action, transition_effects
from .decisions import DecisionRequest, DecisionResult, decision_request_identity
from .execution_stages import stage_contract
from .faults import HumanIntervention, HumanRequirement, StateConflict, route_failure
from .interaction import choice_spec
from .models import PlanResult, Record
from .preview_evidence import PreviewEvidence
from .recovery import (
    Diagnosis,
    RecoveryDecision,
    RecoveryProposal,
    check_rows,
    failure_summary,
    implemented_candidate,
)
from .recovery_identity import (
    authorization_binding,
    binding_matches_failure,
    failure_contract,
)
from .recovery_progress import RecoveryProgress
from .stage_responsibilities import ACTION_OWNERS
from .util import digest
from .workspace import tree_hash


class ActionPermit(Record):
    version: Literal[1, 2] = 2
    cycle_id: str
    owner: str
    action: dict
    actor: Literal["rule", "agent", "human"]
    source_hash: str
    plan_hash: str
    reason: str = ""
    expected_result: str = ""
    task_scopes: dict[str, list[str]] = {}
    prototype_binding: dict = {}
    preview_binding: dict = {}
    phase: Literal["reserved", "started", "completed"] = "reserved"
    outcome: dict = {}
    outcome_hash: str = ""
    authority_failure: dict = {}
    authority_sources: dict = {}
    authority_transition: dict = {}
    authority_patch: str = ""


class RecoveryCycle(Record):
    version: Literal[1, 2] = 2
    id: str
    owner: str
    failure_id: str
    observation: int
    phase: str = "reserved"
    permits: list[str] = []
    human_grant: dict = {}
    outcomes: dict = {}


class RecoveryPolicy:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.progress = RecoveryProgress(engine)

    @staticmethod
    def cycle_id(failure, active):
        return digest([failure.origin_id, failure.id, active["attempt"]])

    def candidates(self, failure, plan, suggested, *, include_declared_checks=False):
        """Freeze admissible actions; the model selects, it cannot supply new scopes."""
        choices = {suggested.action: suggested}
        if suggested.action in STAGE_REENTRY_ACTIONS | {"blocked"}:
            return choices
        common = dict(failure_id=failure.id, evidence_refs=failure.evidence_refs,
                      budget_owner=failure.origin_id, budget_owners=[failure.origin_id])
        tasks = [failure.task_id] if failure.task_id else []
        task = next((t for t in plan.tasks if t.id == failure.task_id), None) if plan else None
        choices.setdefault("diagnose", RecoveryDecision(action="diagnose", task_ids=tasks,
            reason="独立诊断当前故障及最小修复范围", **common))
        if (failure.evidence.get("checks") or failure.evidence.get("commands")
                or include_declared_checks and (task and task.checks or plan and not failure.task_id and plan.final_checks)):
            choices.setdefault("recheck", RecoveryDecision(action="recheck", task_ids=tasks,
                reason="重新执行现有检查并收集当前证据", **common))
        if task:
            choices.setdefault("repair", RecoveryDecision(action="repair", task_ids=tasks,
                reason="在责任任务已接受的路径范围内修复并验证", **common))
        if plan:
            choices.setdefault("plan_patch", RecoveryDecision(action="plan_patch", task_ids=tasks,
                reason="提出最小计划修订并经过契约校验及独立审查", **common))
        return choices

    def available_actions(self, rid, failure, work, plan, suggested, *, include_declared_checks=False):
        """Compute concrete choices eligible for authorization, not issued permissions."""
        choices = self.candidates(failure, plan, suggested, include_declared_checks=include_declared_checks)
        # A proposal cannot make an evidence-refresh action eligible by naming it.
        for action in ("prototype_recheck", "preview_recheck"):
            choices.pop(action, None)
        prototype_binding = preview_binding = None
        retained = self.store.get(rid, "develop/task/" + failure.task_id, {})
        if (plan and failure.task_id in {t.id for t in plan.tasks}
                and failure.kind in {"review", "evidence"}
                and retained.get("phase") in {"applied", "tested", "rejected", "reverify"}
                and implemented_candidate(retained) and not retained.get("commit")
                and suggested.action not in STAGE_REENTRY_ACTIONS | {"blocked"}
                and not (self.store.get(rid, "delivery/journal") or {}).get("publication_started")):
            prototype_binding = self.engine.approvals.recheck_binding(rid, work)
            if prototype_binding:
                choices.setdefault("prototype_recheck", RecoveryDecision(action="prototype_recheck",
                    failure_id=failure.id, task_ids=[failure.task_id], budget_owner=failure.origin_id,
                    budget_owners=[failure.origin_id], evidence_refs=failure.evidence_refs,
                    reason="重放已批准原型的既有动作、重新截图并独立复核，再恢复当前候选的任务验收；保留人工批准及计划"))
            preview_binding = PreviewEvidence(self.engine).binding(rid, work, plan)
            if preview_binding:
                choices.setdefault("preview_recheck", RecoveryDecision(action="preview_recheck",
                    failure_id=failure.id, task_ids=[failure.task_id], budget_owner=failure.origin_id,
                    budget_owners=[failure.origin_id], evidence_refs=failure.evidence_refs,
                    reason="读取归档生产预览合同并独立比较完整动作，核验构建后执行完整隔离预览及清理，再审查当前任务候选"))
        if (self.store.get(rid, "delivery/journal") or {}).get("publication_started"):
            for action in STAGE_REENTRY_ACTIONS | {"plan_patch"}:
                choices.pop(action, None)
        for action in self.progress.exclusions(rid, failure, work, plan):
            choices.pop(action, None)
        return choices, prototype_binding, preview_binding

    @staticmethod
    def proposal_actions(choices):
        """One projection for task/final/delivery diagnosis; no authorization fields."""
        return {name: RecoveryProposal(action=value.action, owner=ACTION_OWNERS[value.action],
            task_ids=value.task_ids, repair_targets=value.repair_targets, reason=value.reason).model_dump()
            for name, value in choices.items()}

    def diagnosis_actions(self, rid, failure, work, plan):
        suggested = RecoveryDecision(failure_id=failure.id, action="diagnose", task_ids=[failure.task_id] if failure.task_id else [],
            budget_owner=failure.origin_id, evidence_refs=failure.evidence_refs, reason="Assess current recovery actions")
        choices, _, _ = self.available_actions(rid, failure, work, plan, suggested, include_declared_checks=True)
        proposals = self.proposal_actions(choices)
        if not (self.store.get(rid, "delivery/journal") or {}).get("publication_started"):
            if plan and plan.tasks and failure.origin_id == "final" and not failure.task_id:
                # Final observations have no task owner yet. Advertise the existing
                # diagnosis contract, NOT an executable unscoped action: adjudicate
                # requires named tasks and evidenced, owned RepairTargets before
                # authorize can reserve a concrete repair. Keep candidates unchanged
                # so old questions/permits and decision-only choices retain scope.
                proposals["repair"] = RecoveryProposal(action="repair", owner=ACTION_OWNERS["repair"], reason=
                    "定位最终故障的既有责任任务；逐项提供 repair_targets 的公开路径和事实证据，"
                    "经范围校验及正式授权后修复并复验；空目标不授予执行权限").model_dump()
            # Re-entry still goes through route_action and its original human gates.
            for action in sorted(STAGE_REENTRY_ACTIONS):
                proposals[action] = RecoveryProposal(action=action, owner=ACTION_OWNERS[action],
                    task_ids=suggested.task_ids,
                    reason="Return to the owning stage without approving its outputs").model_dump()
        else:
            proposals.pop("plan_patch", None)
        return proposals

    def legacy_questions(self, rid, failure, active):
        progress = self.engine.convergence.state(rid, failure.origin_id)
        count = self.store.get(rid, "recovery/budgets/diagnosis/" + failure.origin_id, {"round": 0})["round"]
        actions = [f"diagnosis/{count + 1}", *[
            f"{action}/{failure.id}/{active['attempt']}" for action in ("recheck", "repair", "plan_patch")]]
        mapping = {"automation/human/" + digest([failure.origin_id, action, progress]):
                   ("diagnose" if action.startswith("diagnosis/") else action.split("/")[0]) for action in actions}
        mapping[f"diagnosis/{failure.id}/{count}"] = "diagnose"
        implementation_count = self.store.get(rid, "rounds/" + failure.origin_id, {}).get("round", 0)
        for key in self.store.decision_question_keys(rid, f"round-budget/{failure.origin_id}/{implementation_count}"):
            mapping[key] = "repair"
        pending = self.store.get(rid, "replanning/pending")
        if pending:
            mapping[f"replan-stalled/{pending['evaluation']}"] = (
                "plan_patch" if pending["stage"] == "plan" else pending["stage"])
        return {key: action for key, action in mapping.items()
                if not self.store.get(rid, "recovery/legacy-answers/" + digest(key))}

    async def authorize(self, rid, failure, active, work, plan, suggested, *, operation,
                        established=False, correction=False, force_decision=False, candidates=None, controller_owned=False,
                        diagnosis_assessment=False, prerequisite_resolved=False, supersede_keys=()):
        """Reserve a concrete action and its audit receipt in one CAS transaction."""
        if (not isinstance(suggested, RecoveryDecision) or candidates is not None and
                any(not isinstance(value, RecoveryDecision) for value in candidates.values())):
            raise StateConflict("Authorization requires validated recovery decisions, never diagnosis catalog proposals")
        if suggested.action == "blocked" and not route_failure(failure).human_requirement:
            suggested = suggested.model_copy(update={"action": "diagnose"})
            diagnosis_assessment = True
            candidates = None
        force_decision = force_decision or diagnosis_assessment
        cid = self.cycle_id(failure, active)
        if candidates is None:
            operation = self.progress.resume_operation(rid, failure, work, plan, cid, operation)
        cycle_key = "recovery/cycles/" + cid
        permit_key = "recovery/permits/" + digest([cid, operation])
        source, plan_hash = tree_hash(work), digest(plan.model_dump() if plan else None)
        prior = self.store.get(rid, permit_key)
        if prior:
            permit = ActionPermit.model_validate(prior)
            if permit.source_hash != source or permit.plan_hash != plan_hash:
                raise StateConflict("Recovery authorization inputs changed; preserve the candidate")
        cycle = RecoveryCycle.model_validate(self.store.get(rid, cycle_key) or
            RecoveryCycle(id=cid, owner=failure.origin_id, failure_id=failure.id,
                          observation=active["attempt"]).model_dump())
        question_key = "recovery-cycle/" + digest([cid, operation])
        binding_key = "recovery/authorization-inputs/" + digest([cid, operation])
        bound = self.store.get(rid, binding_key)
        legacy_blocked = suggested.model_copy(update={"action": "blocked"})
        remapped_diagnosis = (suggested.action == "diagnose" and force_decision and bound is not None
            and bound["candidates"] == digest({"blocked": legacy_blocked.model_dump()}))
        if remapped_diagnosis and (prior or any(q["answer"] is not None
                for q in self.store.decision_questions(rid, [question_key]))):
            # Existing grants/answers retain their old, narrow meaning. A later
            # observation can use the new policy; never enlarge a human's consent.
            suggested = legacy_blocked
            candidates = {"blocked": legacy_blocked}
        choices = candidates if candidates is not None else self.candidates(failure, plan, suggested,
            include_declared_checks=diagnosis_assessment)
        prototype_binding = preview_binding = None
        if candidates is None:
            choices, prototype_binding, preview_binding = self.available_actions(
                rid, failure, work, plan, suggested, include_declared_checks=diagnosis_assessment)
        if suggested.action == "prototype_recheck" and (
                not prototype_binding or suggested.task_ids != [failure.task_id]):
            raise StateConflict("Prototype evidence refresh requires matching human consent and the current task")
        if suggested.action == "preview_recheck" and (
                not preview_binding or suggested.task_ids != [failure.task_id]):
            raise StateConflict("Preview evidence refresh requires an accepted preview, human consent and the current task")
        exclusions = self.progress.exclusions(rid, failure, work, plan) if candidates is None else {}
        # An issued permit retains its exact scope; only a NEW selection excludes exhausted paths.
        if not prior and suggested.action in exclusions:
            if bound is not None:
                self.progress.stop(rid, failure, work, plan, cid, operation, exclusions,
                    "已保留的恢复请求指向同证据下已耗尽的动作；保留请求，先核对新的有效证据。")
            if not choices:
                self.progress.stop(rid, failure, work, plan, cid, operation, exclusions,
                    "相同证据下没有可执行的新恢复动作；现场保留，须补充有效证据或修复方案。")
            suggested = choices.get("repair") or choices.get("plan_patch") or next(iter(choices.values()))
            force_decision = True
        transition = suggested.action in STAGE_REENTRY_ACTIONS
        rule_key = "recovery/rules/" + digest([failure.origin_id, failure.step,
            sorted(failure.obligations), suggested.action])
        legacy = self.legacy_questions(rid, failure, active)
        question_keys = list(dict.fromkeys([*legacy, question_key, *supersede_keys]))
        questions = self.store.decision_questions(rid, question_keys)
        if any(q["key"] in supersede_keys and q["answer"] is not None for q in questions):
            raise StateConflict("Additional user input arrived for a technical recovery; preserve it for reassessment")
        answered = [q for q in questions if q["answer"] is not None]
        guards = self.engine.decisions.guards(rid, failure.origin_id,
            [cycle_key, permit_key, rule_key, "plan_revision/current", "automation/engineering/" + failure.origin_id])
        if transition or suggested.action == "plan_patch" or prototype_binding or preview_binding:
            guards["delivery/journal"] = self.store.get(rid, "delivery/journal")
        if prototype_binding:
            guards["prototype"] = self.store.get(rid, "prototype")
        cfg = self.engine.config.load().automation
        tasks = (plan.tasks if plan else [])
        scoped = [t for t in tasks if not failure.task_id or t.id == failure.task_id
                  or t.id in suggested.task_ids]
        paths = failure.evidence.get("allowed_paths", [])
        scope_hash = digest([[t.model_dump() for t in scoped], paths])
        binding = authorization_binding(failure, choices, source, plan_hash, scope_hash)
        binding_updates = {}
        expanded = False
        if bound and bound != binding:
            for additions in ({"preview_recheck"}, {"prototype_recheck"}, {"preview_recheck", "prototype_recheck"}):
                if not additions <= choices.keys() or suggested.action in additions:
                    continue
                previous_choices = {k: v for k, v in choices.items() if k not in additions}
                previous_binding = authorization_binding(failure, previous_choices, source, plan_hash, scope_hash)
                if bound != previous_binding:
                    continue
                if prior or answered:
                    # Recorded answers and issued actions retain their original authority.
                    choices, binding = previous_choices, previous_binding
                    if "prototype_recheck" in additions:
                        prototype_binding = None
                    if "preview_recheck" in additions:
                        preview_binding = None
                else:
                    expanded = True
                    binding_updates = {binding_key: binding,
                        "recovery/authorization-upgrades/" + digest([cid, operation, bound]): {
                            "previous": bound, "reason": "Offer controller-owned evidence refresh",
                            "added_actions": sorted(additions)}}
                break
        legacy_match = (bound is not None and bound.get("version", 1) == 1
            and all(bound.get(name) == binding[name] for name in ("source_hash", "plan_hash", "scope_hash"))
            and binding_matches_failure(bound, failure.model_dump())
            and (bound.get("candidates") == digest({key: value.model_dump() for key, value in choices.items()})
                 or remapped_diagnosis))
        if prior:
            # The issued action is authoritative. Reconstructed option wording or
            # expanded reference aliases cannot replace a durable authorization.
            if (not bound or not binding_matches_failure(bound, failure.model_dump())
                    or any(bound.get(name) != binding[name] for name in ("source_hash", "plan_hash", "scope_hash"))):
                raise StateConflict("Completed or reserved recovery inputs changed; preserve its receipt")
            self.validate_consumption(rid, permit_key)
            return RecoveryDecision.model_validate(permit.action), permit_key
        if bound is not None and bound != binding:
            if not legacy_match and not expanded:
                raise StateConflict("Pending recovery authorization no longer matches its original inputs")
            # A consumed/answered legacy grant keeps its exact old binding.
            if answered:
                binding = bound
            if not expanded:
                binding_updates = {binding_key: binding,
                    "recovery/authorization-upgrades/" + digest([cid, operation, bound]): {
                        "previous": bound, "reason": "Upgrade the verified observation/action contract without changing authority"}}
        if bound is None:
            self.store.checkpoint(rid, binding_key, binding)
        guards[binding_key] = bound if bound is not None else binding
        grant = dict(cycle.human_grant)
        slot = "diagnosis" if suggested.action == "diagnose" else "dispatch"
        human_cycle = (not transition and suggested.action not in {"prototype_recheck", "preview_recheck"} and grant.get("scope_hash") == scope_hash
                       and grant.get(slot, 0) > 0)
        routing = route_action(failure, suggested.action, enabled=cfg.enabled,
            rule=established or correction or not self.store.get(rid, rule_key),
            granted=human_cycle, answered=bool(answered), controller_owned=controller_owned,
            force_decision=force_decision,
            published=bool((self.store.get(rid, "delivery/journal") or {}).get("publication_started")),
            prerequisite_resolved=prerequisite_resolved)
        if routing.human_requirement:
            raise HumanIntervention(routing.human_requirement, reason_code=routing.reason_code, phase=failure.phase)
        rule = routing.route == "rule"
        evidence = {"controller_stage_contract": stage_contract(), "failure": failure.model_dump(), "progress": self.engine.convergence.state(rid, failure.origin_id),
            "engineering": self.store.get(rid, "automation/engineering/" + failure.origin_id, {}),
            "diagnosis_feedback": active.get("diagnosis_feedback"),
            "history": self.store.recovery_history(rid, failure.origin_id),
            "consumption": {"implementation": self.store.get(rid, "rounds/" + failure.origin_id, {}),
                "diagnosis": self.store.get(rid, "recovery/budgets/diagnosis/" + failure.origin_id, {}),
                "tokens": self.store.usage_summary(rid)}}
        if exclusions:
            evidence["excluded_actions"] = exclusions
        if prototype_binding:
            evidence["prototype_recheck"] = {"binding": prototype_binding,
                "previous_review": self.store.get(rid, "prototype_approval", {}).get("review"),
                "action": "Replay approved prototype actions and independently review fresh images; retain task candidate"}
        if preview_binding:
            evidence["preview_recheck"] = {"binding": preview_binding,
                "contracts": PreviewEvidence(self.engine).contracts(rid, plan),
                "action": "Independently compare the archived and current production preview contracts, then execute "
                          "all actions/viewports with verified build inputs in a fresh isolated instance and record cleanup. "
                          "Resume the retained task's review; production checks and prototype evidence are separate."}
        request = DecisionRequest(kind="recovery_action", owner=failure.origin_id,
            allowed_choices=["approve", "ask_user"], scope={"one_action": True, "cycle_id": cid,
                "tasks": [t.model_dump() for t in scoped],
                "allowed_paths": paths,
                "candidates": {key: value.model_dump() for key, value in choices.items()}},
            evidence=evidence, budget={"count_limits_enforced": False},
            state_identity=digest([guards, source, cfg.model_dump(), operation]))
        effects_plan = plan
        if transition and effects_plan is None and guards.get("plan"):
            effects_plan = PlanResult.model_validate(guards["plan"])
        effects = {key: transition_effects(key, effects_plan) for key in choices if key in STAGE_REENTRY_ACTIONS}
        if effects:
            request.scope["stage_transitions"] = effects
        snapshot_key = "recovery/request-snapshots/" + digest([cid, operation])
        snapshot = self.store.get(rid, snapshot_key)
        basis = decision_request_identity(request.model_copy(update={"state_identity": ""}))
        if snapshot and snapshot["basis"] == basis and snapshot["automation"] == cfg.model_dump():
            request = DecisionRequest.model_validate(snapshot["request"])
        elif not snapshot and bound is not None:
            # Version adapters locate only an equivalent latest snapshot. Never
            # select an earlier response because it happened to approve an action.
            for _, previous in self.store.decision_requests(rid):
                original = DecisionRequest.model_validate(previous["request"])
                if (original.scope.get("cycle_id") == cid
                        and decision_request_identity(original.model_copy(update={"state_identity": ""})) == basis):
                    request = original
                    break
        if snapshot and snapshot["request"] != request.model_dump():
            self.store.checkpoint(rid, snapshot_key + "/history/" + digest(snapshot), snapshot)
        self.store.checkpoint(rid, snapshot_key, {"basis": basis, "automation": cfg.model_dump(),
                                                 "request": request.model_dump(), "binding": binding})
        actor, selected, result_key = "rule", suggested.action, None
        result = DecisionResult(action="approve", reason=suggested.reason, evidence_refs=["failure"],
                                selected_action=selected, expected_result="执行后记录实际检查或诊断结果")
        manual = routing.route == "human" and not human_cycle or bool(answered)
        if suggested.action == "blocked" and not answered:
            requirement = routing.human_requirement or HumanRequirement(kind="state_conflict",
                reason=suggested.reason, action="核对当前门禁或恢复记录所需的外部条件。",
                resume_condition="控制器能形成可执行动作；确认不能授权 blocked。", evidence_refs=failure.evidence_refs)
            raise HumanIntervention(requirement, reason_code=requirement.kind, phase=failure.phase)
        self.store.event(rid, "recovery.routing", {"failure": failure.id, "action": suggested.action,
            **routing.model_dump()})
        legacy_answer = None
        answered_cycle = False
        if answered:
            # Persisted old answers authorize only their original action, never a whole cycle.
            old = self.store.question(answered[-1]["id"])
            await self.engine._ask(rid, old["key"], old["prompt"], old["artifact_hash"], old["interaction"])
            if old["key"] in legacy:
                selected = legacy[old["key"]]
                if selected not in choices:
                    raise StateConflict("The answered action no longer matches current recovery evidence")
                legacy_answer = old["key"]
            else:
                answered_cycle = True
        elif human_cycle:
            actor = "human"
            grant[slot] -= 1
        elif not manual and not rule:
            result_key, result = await self.engine.decisions.propose(rid, request, work)
            if tree_hash(work) != source or self.engine.config.load().automation != cfg:
                raise StateConflict("Recovery evidence or policy changed during decision; preserve the candidate")
            if result.action == "approve":
                actor, selected = "agent", result.selected_action
            else:
                manual = True
        if manual and exclusions and result_key is not None and not answered and not human_cycle:
            self.progress.stop(rid, failure, work, plan, cid, operation, exclusions,
                "同证据的无效恢复路径已停止；决策未找到有效替代动作：" + result.reason)
        if manual:
            if not legacy_answer:
                requirement = routing.human_requirement or HumanRequirement(
                    kind="authorization" if not cfg.enabled else "decision_unavailable",
                    reason=result.reason, action=f"决定是否执行 {suggested.action}，范围仅限下列已核对的任务与路径。",
                    resume_condition="取得对应范围的明确授权，且源码、计划及检查依据仍与本请求一致。",
                    evidence_refs=failure.evidence_refs)
                self.store.checkpoint(rid, "recovery/human-requirement", requirement.model_dump())
                self.store.record_fault(rid, {"failure": failure.id, "phase": failure.phase, "route": "human",
                    "reason_code": requirement.kind, "human_requirement": requirement.model_dump()})
                prompt = ("当前故障：\n" + failure_summary(check_rows(failure.evidence),
                        candidate_error=failure.evidence.get("candidate_error"),
                        validation_error=failure.evidence.get("validation_error"),
                        review=failure.evidence.get("review"), environment_error=failure.evidence.get("environment_error"))
                    + "\n恢复需要人工决定：" + requirement.prompt()
                    + ("\n原恢复请求：" + suggested.reason if result.reason != suggested.reason else "")
                    + "\n允许范围：" + ("; ".join(t.id + ": " + ", ".join(t.allowed_paths) for t in scoped)
                                               or ", ".join(paths) or "控制器声明的当前操作")
                    + "\n先执行：" + suggested.action
                    + ("\n本次仅重采已批准原型并独立复核，随后核验当前任务候选；不修改原型或重新批准设计。"
                       if suggested.action == "prototype_recheck" else
                       "\n本次比较归档合同、核验构建并执行完整隔离生产预览及清理，结果交回当前任务审查。"
                       if suggested.action == "preview_recheck" else
                       "\n本次仅批准返回所示阶段；其校验、独立审查及原型人审仍须执行。" if transition else
                       "\n本轮只允许重新执行当前已声明的操作并核验结果。" if candidates is not None else
                       "\n本轮至多一次诊断及一次格式修正、一次局部计划修订或每个所列任务一次范围内修复及验证。")
                    + "\n累计次数只记录，不因本次授权清零。")
                interaction = choice_spec(["授权展示的一轮恢复", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
                interaction["pause_on"] = "n"
                if not answered_cycle:
                    await self.engine._ask(rid, question_key, prompt, digest([request.model_dump(), prompt]), interaction)
                grant = {} if transition or suggested.action in {"prototype_recheck", "preview_recheck"} else {"scope_hash": scope_hash, "diagnosis": 2, "dispatch": 1}
                if grant:
                    grant[slot] -= 1
            actor = "human"
            result = DecisionResult(action="approve", reason="用户授权展示的恢复范围", evidence_refs=["failure"],
                selected_action=selected, expected_result="执行后验证实际结果")
        if tree_hash(work) != source or self.engine.config.load().automation != cfg:
            raise StateConflict("Recovery evidence or policy changed while authorizing; preserved candidate requires reassessment")
        choice = choices[selected]
        transition = (self.engine.authority.patch_transition(rid, active, failure)
                      if operation == "patch-followup" and self.store.get(rid, "authority/protocol") else {})
        self.engine.authority.require_failure(rid, failure, choice.action, transition=transition)
        if choice.action == "prototype_recheck" and (
                not prototype_binding or self.engine.approvals.recheck_binding(rid, work) != prototype_binding):
            raise StateConflict("Prototype consent changed during recovery authorization")
        if choice.action == "preview_recheck" and (
                not preview_binding or PreviewEvidence(self.engine).binding(rid, work, plan) != preview_binding):
            raise StateConflict("Preview contracts or consent changed during recovery authorization")
        permit = ActionPermit(cycle_id=cid, owner=failure.origin_id, action=choice.model_dump(),
                              actor=actor, source_hash=source, plan_hash=plan_hash,
                              authority_failure=failure.model_dump(),
                              authority_sources=self.engine.authority.identity(rid), authority_transition=transition,
                              reason=result.reason, expected_result=result.expected_result,
                              task_scopes={task.id: task.allowed_paths for task in scoped},
                              prototype_binding=prototype_binding if choice.action == "prototype_recheck" else {},
                              preview_binding=preview_binding if choice.action == "preview_recheck" else {})
        cycle.phase = "diagnosis" if choice.action == "diagnose" else "reserved"
        cycle.permits.append(permit_key)
        cycle.human_grant = grant
        updates = {permit_key: permit.model_dump(), cycle_key: cycle.model_dump(),
                   rule_key: {"permit": permit_key}, **binding_updates}
        if legacy_answer:
            updates["recovery/legacy-answers/" + digest(legacy_answer)] = {"permit": permit_key}
        # New answers may be ours; concurrent answers to other bound questions still win.
        current = self.store.decision_questions(rid, question_keys) if actor == "human" else questions
        own_key = legacy_answer or question_key if manual else None
        if actor == "human" and [q for q in current if q["key"] != own_key] != [
                q for q in questions if q["key"] != own_key]:
            raise StateConflict("A concurrent human answer superseded the proposed recovery")
        if not self.store.apply_decision(rid, "decisions/applied/" + digest([permit_key, request.model_dump()]),
                request.model_dump(), result.model_dump(), actor, guards, updates, current, question_keys,
                result_key=result_key, supersede_keys=[key for key in question_keys if key != own_key]):
            raise StateConflict("Recovery authorization changed; preserve the unapplied proposal")
        self.store.event(rid, "recovery.authorized", {"owner": failure.origin_id, "cycle": cid,
            "action": choice.action, "actor": actor, "reason": result.reason})
        self.store.record_fault(rid, {"failure": failure.id, "phase": failure.phase,
            "route": "human" if actor == "human" else "decision" if actor == "agent" else "rule",
            "reason_code": failure.reason_code, "permit": permit_key, "authorized": True})
        return choice, permit_key

    def validate_consumption(self, rid, key, *, expected=None):
        if not key:
            return
        permit = self.store.get(rid, key)
        if not permit:
            raise StateConflict("Dispatched action lost its authorization receipt")
        action = permit["action"]["action"]
        if expected is not None and action not in expected:
            raise StateConflict("Dispatched action belongs to another responsibility")
        if self.store.get(rid, "authority/protocol"):
            from .recovery import FailureRecord
            raw = permit.get("authority_failure")
            if not raw:
                cycle = self.store.get(rid, "recovery/cycles/" + permit["cycle_id"], {})
                raw = self.store.get(rid, f"recovery/failures/{cycle.get('failure_id')}/{cycle.get('observation')}")
            if not raw or raw.get("id") != permit["action"]["failure_id"]:
                raise StateConflict("Reserved action lost its authority observation; reassess before execution")
            if (action not in {"diagnose", "recheck", "prototype_recheck", "preview_recheck"}
                    and permit.get("authority_sources") != self.engine.authority.identity(rid)):
                raise StateConflict("Dispatched authority sources changed; reassessment is required before mutation")
            if permit.get("authority_patch"):
                self.engine.authority.validate_prepared_migration(rid, key, permit, FailureRecord.model_validate(raw))
            else:
                self.engine.authority.require_failure(rid, FailureRecord.model_validate(raw), action,
                                                      transition=permit.get("authority_transition"))
        return permit

    def adopt_prepared_authority(self, rid, active, failure, journal_key):
        key = active.get("permit")
        permit = self.store.get(rid, key, {}) if key else {}
        if permit.get("authority_sources") or not self.store.get(rid, "authority/protocol"):
            return
        migrated = {**permit, "authority_sources": self.engine.authority.identity(rid),
                    "authority_failure": failure.model_dump(), "authority_patch": journal_key}
        self.engine.authority.validate_prepared_migration(rid, key, migrated, failure)
        self.store.checkpoints(rid, {"authority/permit-history/" + digest(permit): permit, key: migrated})

    def start(self, rid, key):
        permit = self.validate_consumption(rid, key)
        if permit and permit["phase"] == "reserved":
            self.store.checkpoint(rid, key, {**permit, "phase": "started"})

    def completed_outcome(self, rid, key, *, action=None):
        """All action consumers use durable results before considering a new call."""
        if not key:
            return None
        saved = self.store.get(rid, key)
        if saved is None:
            raise StateConflict("Action authorization receipt is missing; preserve recovery state")
        if saved.get("phase") != "completed":
            return None
        if action and saved["action"].get("action") != action:
            raise StateConflict("Completed action does not match its reserved operation")
        outcome = saved.get("outcome", {})
        if saved.get("outcome_hash") and saved["outcome_hash"] != digest(outcome):
            raise StateConflict("Completed action result integrity mismatch")
        self.store.event(rid, "recovery.result_reused", {"permit": key, "action": saved["action"]["action"]})
        return outcome

    def completed_diagnosis(self, rid, key, failure, active, work, plan, *, authorization_failure=None):
        """A completed action is replayed from its bound outcome, not a transport cache."""
        if not key:
            return None  # Legacy or published-delivery calls without an action permit.
        outcome = self.completed_outcome(rid, key, action="diagnose")
        saved = self.store.get(rid, key)
        permit = ActionPermit.model_validate(saved)
        if permit.action.get("action") != "diagnose" or outcome is not None and permit.outcome.get("status") != "validated":
            raise StateConflict("Completed diagnosis has no validated outcome; preserve recovery state")
        binding = self.store.get(rid, key.replace("recovery/permits/", "recovery/authorization-inputs/", 1), {})
        source, plan_hash = tree_hash(work), digest(plan.model_dump() if plan else None)
        tasks = [task for task in plan.tasks if task.id in permit.task_scopes] if plan else []
        scope_hash = digest([[task.model_dump() for task in tasks], failure.evidence.get("allowed_paths", [])])
        failures = [failure.model_dump()]
        if authorization_failure is not None:
            # Delivery expands its evidence catalog after reserving diagnosis.
            # Both forms must describe exactly the same failure observations.
            if failure_contract(authorization_failure.model_dump()) != failure_contract(failure.model_dump()):
                raise StateConflict("Completed diagnosis failure changed; preserve its original outcome")
            failures.append(authorization_failure.model_dump())
        expected = {"source_hash": source, "plan_hash": plan_hash, "scope_hash": scope_hash}
        if (any(binding.get(field) != value for field, value in expected.items())
                or not any(binding_matches_failure(binding, row) for row in failures)
                or permit.source_hash != source or permit.plan_hash != plan_hash
                or permit.owner != failure.origin_id or permit.action.get("failure_id") != failure.id
                or permit.cycle_id != self.cycle_id(failure, active)
                or permit.task_scopes != {task.id: task.allowed_paths for task in tasks}):
            raise StateConflict("Completed diagnosis inputs changed; preserve its original outcome and candidate")
        if outcome is None:
            return None
        try:
            result = Diagnosis.model_validate(permit.outcome.get("diagnosis"))
        except ValueError:
            raise StateConflict("Completed diagnosis outcome is invalid; preserve recovery state") from None
        self.store.event(rid, "recovery.diagnosis_reused", {"owner": failure.origin_id, "permit": key})
        return result

    def complete(self, rid, key, outcome):
        if not key:
            return
        permit = self.store.get(rid, key)
        if permit and permit["phase"] == "completed":
            self.completed_outcome(rid, key)
            if permit.get("outcome") != outcome:
                raise StateConflict("Completed action outcome cannot be overwritten")
            return
        if permit and permit["phase"] != "completed":
            updates = {key: {**permit, "phase": "completed", "outcome": outcome, "outcome_hash": digest(outcome)}}
            if permit["action"]["action"] != "diagnose":
                cycle_key = "recovery/cycles/" + permit["cycle_id"]
                cycle = self.store.get(rid, cycle_key)
                if cycle:
                    updates[cycle_key] = {**cycle, "phase": "completed", "outcomes": outcome}
            self.store.checkpoints(rid, updates)

    def task_authorized(self, rid, saved, task):
        permit = self.store.get(rid, saved.get("recovery_permit", ""), {})
        allowed = (task.id in permit.get("outcome", {}).get("expected_tasks", [])
                and task.id not in permit.get("outcome", {}).get("tasks", {})
                and permit.get("task_scopes", {}).get(task.id) == task.allowed_paths)
        if allowed:
            self.validate_consumption(rid, saved["recovery_permit"], expected={"repair"})
        return allowed

    def require_task_outcome(self, rid, saved, task_id):
        """An accepted checkpoint cannot stand in for its missing action result."""
        key = saved.get("recovery_permit")
        if key:
            permit = self.store.get(rid, key, {})
            if not (permit or {}).get("outcome", {}).get("tasks", {}).get(task_id):
                raise StateConflict("Accepted task has no recorded recovery outcome; preserve it for reconciliation")

    def task_outcome(self, rid, saved, task_id, outcome):
        updates = self.task_outcome_updates(rid, saved, task_id, outcome)
        if updates:
            self.store.checkpoints(rid, updates)

    def task_outcome_updates(self, rid, saved, task_id, outcome):
        """Build the existing result updates for the consumer's atomic handoff."""
        key = saved.get("recovery_permit")
        if not key:
            return {}
        permit = self.store.get(rid, key)
        if not permit or task_id in permit.get("outcome", {}).get("tasks", {}):
            return {}
        results = {**permit.get("outcome", {}), "tasks": {
            **permit.get("outcome", {}).get("tasks", {}), task_id: outcome}}
        complete = set(results.get("expected_tasks", [])) <= results["tasks"].keys()
        updates = {key: {**permit, "phase": "completed" if complete else "started", "outcome": results,
                         "outcome_hash": digest(results) if complete else ""}}
        cycle_key = "recovery/cycles/" + permit["cycle_id"]
        cycle = self.store.get(rid, cycle_key)
        if cycle:
            updates[cycle_key] = {**cycle, "phase": "completed" if complete else "verifying", "outcomes": results}
        return updates

    def dispatch_updates(self, rid, active, targets):
        key = active.get("permit")
        permit = self.store.get(rid, key) if key else None
        if not permit:
            return {}
        if permit["phase"] in {"started", "completed"} and "expected_tasks" in permit.get("outcome", {}):
            if permit["outcome"]["expected_tasks"] != sorted(targets):
                raise StateConflict("Dispatched action targets changed; preserve its original receipt")
            if permit["phase"] == "completed":
                self.completed_outcome(rid, key)
            return {}
        cycle_key = "recovery/cycles/" + permit["cycle_id"]
        cycle = self.store.get(rid, cycle_key)
        updates = {key: {**permit, "phase": "started", "outcome": {"expected_tasks": sorted(targets), "tasks": {}}},
                   cycle_key: {**cycle, "phase": "verifying"}}
        if not targets:
            updates["recovery/final-action"] = key
        return updates

    def finish_cycle(self, rid, active, outcome):
        cid = digest([active["failure"]["origin_id"], active["failure"]["id"], active["attempt"]])
        key = "recovery/cycles/" + cid
        cycle = self.store.get(rid, key)
        if cycle:
            self.store.checkpoint(rid, key, {**cycle, "phase": "completed", "outcomes": outcome})
