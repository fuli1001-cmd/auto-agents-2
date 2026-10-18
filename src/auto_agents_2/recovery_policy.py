"""Action authorization over controller facts; counts inform decisions, never stop them."""
from __future__ import annotations

from typing import Literal

from .decisions import DecisionRequest, DecisionResult, decision_request_identity
from .faults import HumanIntervention, HumanRequirement, StateConflict, route_failure
from .interaction import choice_spec
from .models import Record
from .recovery import Diagnosis, RecoveryDecision, check_rows, failure_summary
from .recovery_identity import authorization_binding, binding_matches_failure, failure_contract
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
    phase: Literal["reserved", "started", "completed"] = "reserved"
    outcome: dict = {}
    outcome_hash: str = ""


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

    @staticmethod
    def cycle_id(failure, active):
        return digest([failure.origin_id, failure.id, active["attempt"]])

    def candidates(self, failure, plan, suggested, *, include_declared_checks=False):
        """Freeze admissible actions; the model selects, it cannot supply new scopes."""
        choices = {suggested.action: suggested}
        if suggested.action in {"clarify", "provider_research", "prototype", "design", "blocked"}:
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
                        diagnosis_assessment=False):
        """Reserve a concrete action and its audit receipt in one CAS transaction."""
        if suggested.action == "blocked" and not route_failure(failure).human_requirement:
            suggested = suggested.model_copy(update={"action": "diagnose"})
            diagnosis_assessment = True
            candidates = None
        force_decision = force_decision or diagnosis_assessment
        cid = self.cycle_id(failure, active)
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
        hard_boundary = (not controller_owned and
                         suggested.action in {"clarify", "provider_research", "prototype", "design", "blocked"})
        rule_key = "recovery/rules/" + digest([failure.origin_id, failure.step,
            sorted(failure.obligations), suggested.action])
        legacy = self.legacy_questions(rid, failure, active)
        question_keys = [*legacy, question_key]
        questions = self.store.decision_questions(rid, question_keys)
        answered = [q for q in questions if q["answer"] is not None]
        guards = self.engine.decisions.guards(rid, failure.origin_id,
            [cycle_key, permit_key, rule_key, "plan_revision/current", "automation/engineering/" + failure.origin_id])
        cfg = self.engine.config.load().automation
        tasks = (plan.tasks if plan else [])
        scoped = [t for t in tasks if not failure.task_id or t.id == failure.task_id
                  or t.id in suggested.task_ids]
        paths = failure.evidence.get("allowed_paths", [])
        scope_hash = digest([[t.model_dump() for t in scoped], paths])
        binding = authorization_binding(failure, choices, source, plan_hash, scope_hash)
        binding_updates = {}
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
            return RecoveryDecision.model_validate(permit.action), permit_key
        if bound is not None and bound != binding:
            if not legacy_match:
                raise StateConflict("Pending recovery authorization no longer matches its original inputs")
            # A consumed/answered legacy grant keeps its exact old binding.
            if answered:
                binding = bound
            binding_updates = {binding_key: binding,
                "recovery/authorization-upgrades/" + digest([cid, operation, bound]): {
                    "previous": bound, "reason": "Upgrade the verified observation/action contract without changing authority"}}
        if bound is None:
            self.store.checkpoint(rid, binding_key, binding)
        guards[binding_key] = bound if bound is not None else binding
        grant = dict(cycle.human_grant)
        slot = "diagnosis" if suggested.action == "diagnose" else "dispatch"
        human_cycle = (not hard_boundary and grant.get("scope_hash") == scope_hash
                       and grant.get(slot, 0) > 0)
        rule = (not hard_boundary and not force_decision and
                (established or correction or not self.store.get(rid, rule_key)))
        evidence = {"failure": failure.model_dump(), "progress": self.engine.convergence.state(rid, failure.origin_id),
            "engineering": self.store.get(rid, "automation/engineering/" + failure.origin_id, {}),
            "diagnosis_feedback": active.get("diagnosis_feedback"),
            "history": self.store.recovery_history(rid, failure.origin_id),
            "consumption": {"implementation": self.store.get(rid, "rounds/" + failure.origin_id, {}),
                "diagnosis": self.store.get(rid, "recovery/budgets/diagnosis/" + failure.origin_id, {}),
                "tokens": self.store.usage_summary(rid)}}
        request = DecisionRequest(kind="recovery_action", owner=failure.origin_id,
            allowed_choices=["approve", "ask_user"], scope={"one_action": True, "cycle_id": cid,
                "tasks": [t.model_dump() for t in scoped],
                "allowed_paths": paths,
                "candidates": {key: value.model_dump() for key, value in choices.items()}},
            evidence=evidence, budget={"count_limits_enforced": False},
            state_identity=digest([guards, source, cfg.model_dump(), operation]))
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
        manual = (hard_boundary or bool(answered) or bool(grant and grant.get("scope_hash") != scope_hash)
                  or not (rule or human_cycle or cfg.enabled))
        routing = route_failure(failure, repeated=not rule, diagnosis_inconclusive=diagnosis_assessment)
        if suggested.action == "blocked" and not answered:
            requirement = routing.human_requirement or HumanRequirement(kind="state_conflict",
                reason=suggested.reason, action="核对当前门禁或恢复记录所需的外部条件。",
                resume_condition="控制器能形成可执行动作；确认不能授权 blocked。", evidence_refs=failure.evidence_refs)
            raise HumanIntervention(requirement, reason_code=requirement.kind, phase=failure.phase)
        self.store.event(rid, "recovery.classified", {"failure": failure.id, "observation": failure.observation_id,
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
        if manual:
            if not legacy_answer:
                requirement = routing.human_requirement or HumanRequirement(
                    kind="authorization" if hard_boundary or not cfg.enabled else "decision_unavailable",
                    reason=result.reason, action=f"决定是否执行 {suggested.action}，范围仅限下列已核对的任务与路径。",
                    resume_condition="取得对应范围的明确授权，且源码、计划及检查依据仍与本请求一致。",
                    evidence_refs=failure.evidence_refs)
                self.store.checkpoint(rid, "recovery/human-requirement", requirement.model_dump())
                self.store.record_fault(rid, {"failure": failure.id, "phase": failure.phase, "route": "human",
                    "reason_code": requirement.kind, "human_requirement": requirement.model_dump()})
                prompt = ("当前故障：\n" + failure_summary(check_rows(failure.evidence),
                        review=failure.evidence.get("review"), environment_error=failure.evidence.get("environment_error"))
                    + "\n恢复需要人工决定：" + requirement.prompt()
                    + ("\n原恢复请求：" + suggested.reason if result.reason != suggested.reason else "")
                    + "\n允许范围：" + ("; ".join(t.id + ": " + ", ".join(t.allowed_paths) for t in scoped)
                                               or ", ".join(paths) or "控制器声明的当前操作")
                    + "\n先执行：" + suggested.action
                    + ("\n本次仅批准上述门禁或资源核对动作。" if hard_boundary else
                       "\n本轮只允许重新执行当前已声明的操作并核验结果。" if candidates is not None else
                       "\n本轮至多一次诊断及一次格式修正、一次局部计划修订或每个所列任务一次范围内修复及验证。")
                    + "\n累计次数只记录，不因本次授权清零。")
                interaction = choice_spec(["授权展示的一轮恢复", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
                interaction["pause_on"] = "n"
                if not answered_cycle:
                    await self.engine._ask(rid, question_key, prompt, digest([request.model_dump(), prompt]), interaction)
                grant = {} if hard_boundary else {"scope_hash": scope_hash, "diagnosis": 2, "dispatch": 1}
                if grant:
                    grant[slot] -= 1
            actor = "human"
            result = DecisionResult(action="approve", reason="用户授权展示的恢复范围", evidence_refs=["failure"],
                selected_action=selected, expected_result="执行后验证实际结果")
        if tree_hash(work) != source or self.engine.config.load().automation != cfg:
            raise StateConflict("Recovery evidence or policy changed while authorizing; preserved candidate requires reassessment")
        choice = choices[selected]
        permit = ActionPermit(cycle_id=cid, owner=failure.origin_id, action=choice.model_dump(),
                              actor=actor, source_hash=source, plan_hash=plan_hash,
                              reason=result.reason, expected_result=result.expected_result,
                              task_scopes={task.id: task.allowed_paths for task in scoped})
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

    def start(self, rid, key):
        if not key:
            return
        permit = self.store.get(rid, key)
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
        return (task.id in permit.get("outcome", {}).get("expected_tasks", [])
                and task.id not in permit.get("outcome", {}).get("tasks", {})
                and permit.get("task_scopes", {}).get(task.id) == task.allowed_paths)

    def task_outcome(self, rid, saved, task_id, outcome):
        key = saved.get("recovery_permit")
        if not key:
            return
        permit = self.store.get(rid, key)
        if not permit or task_id in permit.get("outcome", {}).get("tasks", {}):
            return
        results = {**permit.get("outcome", {}), "tasks": {
            **permit.get("outcome", {}).get("tasks", {}), task_id: outcome}}
        complete = set(results.get("expected_tasks", [])) <= results["tasks"].keys()
        updates = {key: {**permit, "phase": "completed" if complete else "started", "outcome": results,
                         "outcome_hash": digest(results) if complete else ""}}
        cycle_key = "recovery/cycles/" + permit["cycle_id"]
        cycle = self.store.get(rid, cycle_key)
        if cycle:
            updates[cycle_key] = {**cycle, "phase": "completed" if complete else "verifying", "outcomes": results}
        self.store.checkpoints(rid, updates)

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
