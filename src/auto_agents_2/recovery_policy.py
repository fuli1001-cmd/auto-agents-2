"""Action authorization over controller facts; counts inform decisions, never stop them."""
from __future__ import annotations

from typing import Literal

from .decisions import DecisionRequest, DecisionResult
from .errors import AA2Error
from .interaction import choice_spec
from .models import Record
from .recovery import RecoveryDecision, failure_summary
from .util import digest
from .workspace import tree_hash


class ActionPermit(Record):
    version: Literal[1] = 1
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


class RecoveryCycle(Record):
    version: Literal[1] = 1
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
        force_decision = force_decision or diagnosis_assessment
        cid = self.cycle_id(failure, active)
        cycle_key = "recovery/cycles/" + cid
        permit_key = "recovery/permits/" + digest([cid, operation])
        source, plan_hash = tree_hash(work), digest(plan.model_dump() if plan else None)
        prior = self.store.get(rid, permit_key)
        if prior:
            permit = ActionPermit.model_validate(prior)
            if permit.source_hash != source or permit.plan_hash != plan_hash:
                raise AA2Error("Recovery authorization inputs changed; preserve the candidate")
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
        binding = {"source_hash": source, "plan_hash": plan_hash, "scope_hash": scope_hash,
                   "candidates": digest({key: value.model_dump() for key, value in choices.items()}),
                   "failure": digest(failure.model_dump())}
        binding_updates = {}
        if bound is not None and bound != binding:
            if (not remapped_diagnosis
                    or {k: v for k, v in bound.items() if k != "candidates"}
                    != {k: v for k, v in binding.items() if k != "candidates"}):
                raise AA2Error("Pending recovery authorization no longer matches its original inputs")
            binding_updates = {binding_key: binding,
                "recovery/authorization-upgrades/" + digest([cid, operation, bound]): {
                    "previous": bound, "reason": "Reassess an inconclusive diagnosis instead of forcing a human block"}}
        if bound is None:
            self.store.checkpoint(rid, binding_key, binding)
        guards[binding_key] = bound if bound is not None else binding
        if prior:
            return RecoveryDecision.model_validate(permit.action), permit_key
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
        actor, selected, result_key = "rule", suggested.action, None
        result = DecisionResult(action="approve", reason=suggested.reason, evidence_refs=["failure"],
                                selected_action=selected, expected_result="执行后记录实际检查或诊断结果")
        manual = (hard_boundary or bool(answered) or bool(grant and grant.get("scope_hash") != scope_hash)
                  or not (rule or human_cycle or cfg.enabled))
        legacy_answer = None
        answered_cycle = False
        if answered:
            # Persisted old answers authorize only their original action, never a whole cycle.
            old = self.store.question(answered[-1]["id"])
            await self.engine._ask(rid, old["key"], old["prompt"], old["artifact_hash"], old["interaction"])
            if old["key"] in legacy:
                selected = legacy[old["key"]]
                if selected not in choices:
                    raise AA2Error("The answered action no longer matches current recovery evidence")
                legacy_answer = old["key"]
            else:
                answered_cycle = True
        elif human_cycle:
            actor = "human"
            grant[slot] -= 1
        elif not manual and not rule:
            result_key, result = await self.engine.decisions.propose(rid, request, work)
            if tree_hash(work) != source or self.engine.config.load().automation != cfg:
                raise AA2Error("Recovery evidence or policy changed during decision; preserve the candidate")
            if result.action == "approve":
                actor, selected = "agent", result.selected_action
            else:
                manual = True
        if manual:
            if not legacy_answer:
                prompt = ("当前故障：\n" + failure_summary(failure.evidence.get("checks", []),
                        review=failure.evidence.get("review"), environment_error=failure.evidence.get("environment_error"))
                    + "\n恢复需要人工决定：" + result.reason
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
            raise AA2Error("Recovery evidence or policy changed while authorizing; preserved candidate requires reassessment")
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
            raise AA2Error("A concurrent human answer superseded the proposed recovery")
        if not self.store.apply_decision(rid, "decisions/applied/" + digest([permit_key, request.model_dump()]),
                request.model_dump(), result.model_dump(), actor, guards, updates, current, question_keys,
                result_key=result_key, supersede_keys=[key for key in question_keys if key != own_key]):
            raise AA2Error("Recovery authorization changed; preserve the unapplied proposal")
        self.store.event(rid, "recovery.authorized", {"owner": failure.origin_id, "cycle": cid,
            "action": choice.action, "actor": actor, "reason": result.reason})
        return choice, permit_key

    def start(self, rid, key):
        if not key:
            return
        permit = self.store.get(rid, key)
        if permit and permit["phase"] == "reserved":
            self.store.checkpoint(rid, key, {**permit, "phase": "started"})

    def complete(self, rid, key, outcome):
        if not key:
            return
        permit = self.store.get(rid, key)
        if permit and permit["phase"] != "completed":
            updates = {key: {**permit, "phase": "completed", "outcome": outcome}}
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
        updates = {key: {**permit, "phase": "completed" if complete else "started", "outcome": results}}
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
