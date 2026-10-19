import asyncio
from io import StringIO

import pytest
from rich.console import Console
from test_decisions import ApprovingAgent, configure
from test_recovery_policy import ready

from auto_agents_2.action_authority import STAGE_REENTRY_ACTIONS, route_action
from auto_agents_2.display import ProgressDisplay
from auto_agents_2.faults import HumanIntervention, StateConflict
from auto_agents_2.recovery import Diagnosis, RecoveryDecision


async def transition(workflow, rid, work, plan, failure, stage, **kwargs):
    suggested = RecoveryDecision(failure_id=failure.id, action=stage, task_ids=[failure.task_id],
        budget_owner=failure.origin_id, evidence_refs=failure.evidence_refs, reason="Re-enter the owning gate")
    return await workflow.recovery_policy.authorize(rid, failure, workflow.store.get(rid, "recovery/active"),
        work, plan, suggested, operation="stage-dispatch", established=True, **kwargs)


@pytest.mark.parametrize("stage", sorted(STAGE_REENTRY_ACTIONS))
async def test_technical_stage_transitions_use_a_decision_and_never_approve_outputs(tmp_path, stage):
    workflow, rid, work, plan, failure = ready(tmp_path)
    workflow.store.checkpoint(rid, "rounds/T3", {"round": 21})
    decision, key = await transition(workflow, rid, work, plan, failure, stage)
    assert decision.action == stage and workflow.store.get(rid, key)["actor"] == "agent"
    assert [stage for stage, _ in workflow.agent.calls] == ["decision"]
    request = workflow.store.decision_requests(rid)[0][1]["request"]
    effects = request["scope"]["stage_transitions"][stage]
    assert effects["invalidate_stages"][0] == stage and "no output is approved" in effects["authority"]
    assert effects["affected_tasks"] == ([] if stage == "readme" else [t.id for t in plan.tasks])
    assert workflow.store.get(rid, "rounds/T3") == {"round": 21}
    assert workflow.store.get(rid, "plan") == plan.model_dump()
    assert not workflow.store.get(rid, "prototype_approval") and not workflow.store.pending(rid)
    assert (await transition(workflow, rid, work, plan, failure, stage))[1] == key
    assert len(workflow.agent.calls) == 1


@pytest.mark.parametrize("answer", [None, "y", "n"])
async def test_pending_stage_question_is_reassessed_but_existing_answers_keep_their_meaning(tmp_path, answer):
    workflow, rid, work, plan, failure = ready(tmp_path)
    configure(workflow, automation={"enabled": False})
    async def pause(question):
        raise asyncio.CancelledError
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await transition(workflow, rid, work, plan, failure, "design")
    old = workflow.store.pending(rid)[0]
    if answer:
        workflow.store.answer(old["id"], answer)
    configure(workflow, automation={"enabled": True})
    if answer == "n":
        with pytest.raises(asyncio.CancelledError):
            await transition(workflow, rid, work, plan, failure, "design")
        assert not workflow.agent.calls
    else:
        _, key = await transition(workflow, rid, work, plan, failure, "design")
        assert workflow.store.get(rid, key)["actor"] == ("human" if answer else "agent")
        assert len(workflow.agent.calls) == (0 if answer else 1)
    saved = workflow.store.question(old["id"])
    assert saved["answer"] == answer
    if answer is None:
        assert saved["superseded"]


async def test_different_scope_human_grant_does_not_prevent_new_independent_decision(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path)
    active = workflow.store.get(rid, "recovery/active")
    cid = workflow.recovery_policy.cycle_id(failure, active)
    workflow.store.checkpoint(rid, "recovery/cycles/" + cid, {
        "id": cid, "owner": failure.origin_id, "failure_id": failure.id, "observation": 1,
        "human_grant": {"scope_hash": "old-narrow-scope", "dispatch": 1}})
    _, key = await transition(workflow, rid, work, plan, failure, "repair", force_decision=True)
    assert workflow.store.get(rid, key)["actor"] == "agent"


async def test_controller_transition_reports_accepted_plan_tasks_even_without_a_plan_argument(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path)
    await transition(workflow, rid, work, None, failure, "design", controller_owned=True, force_decision=True)
    request = workflow.store.decision_requests(rid)[0][1]["request"]
    assert request["scope"]["stage_transitions"]["design"]["affected_tasks"] == [t.id for t in plan.tasks]


async def test_completed_design_diagnosis_resumes_through_decision_without_another_diagnosis(tmp_path):
    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "recovery_diagnosis":
                self.calls.append((stage, True))
                return Diagnosis(action="design", task_ids=["T3"], evidence_refs=["failure.evidence"],
                                 reason="Reconcile the missing architecture in its owning gate")
            return await super().invoke(stage, context, *args, **kwargs)
    workflow, rid, work, plan, failure = ready(tmp_path, Agent())
    configure(workflow, automation={"enabled": False})
    async def pause(question):
        raise asyncio.CancelledError
    workflow.question_handler = pause
    workflow.store.checkpoint(rid, "rounds/T3", {"round": 12})
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    question = workflow.store.pending(rid)[0]
    configure(workflow, automation={"enabled": True})
    await workflow.recovery.resume(rid)
    assert [stage for stage, _ in workflow.agent.calls] == ["recovery_diagnosis", "decision"]
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1
    assert workflow.store.get(rid, "rounds/T3") == {"round": 12}
    assert workflow.store.get(rid, "recovery/gate-history/0")["checkpoints"]["plan"] == plan.model_dump()
    assert not workflow.store.pending(rid)
    assert workflow.store.question(question["id"])["answer"] is None


@pytest.mark.parametrize("stage", sorted(STAGE_REENTRY_ACTIONS | {"plan_patch"}))
async def test_published_journal_cannot_be_invalidated_by_automatic_stage_transition(tmp_path, stage):
    workflow, rid, work, plan, failure = ready(tmp_path)
    workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
    with pytest.raises(HumanIntervention) as caught:
        await transition(workflow, rid, work, plan, failure, stage)
    assert caught.value.reason_code == "published_gate_change" and not workflow.agent.calls


async def test_publication_race_cannot_commit_a_stage_transition_permit(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path)
    original = workflow.agent.invoke
    async def publish(*args, **kwargs):
        result = await original(*args, **kwargs)
        workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
        return result
    workflow.agent.invoke = publish
    with pytest.raises(StateConflict, match="authorization changed"):
        await transition(workflow, rid, work, plan, failure, "design")
    assert not workflow.store.recovery_history(rid, failure.origin_id)["recent"]


async def test_known_resource_prerequisite_cannot_be_overridden_by_a_model(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path)
    failure = failure.model_copy(update={"evidence": {"checks": [
        {"failure_reasons": ["evidence_collection_failed"], "exit_code": 1}]}})
    with pytest.raises(HumanIntervention) as caught:
        await transition(workflow, rid, work, plan, failure, "recheck")
    assert caught.value.requirement.kind == "resource_limit" and not workflow.agent.calls
    _, key = await transition(workflow, rid, work, plan, failure, "recheck", prerequisite_resolved=True)
    assert workflow.store.get(rid, key)["actor"] == "rule"


def test_controller_rules_still_work_when_model_decisions_are_disabled(tmp_path):
    _, _, _, _, failure = ready(tmp_path)
    assert route_action(failure, "design", enabled=False, rule=True, controller_owned=True).route == "rule"
    assert route_action(failure, "design", enabled=False, rule=True).reason_code == "automation_disabled"


def test_classification_does_not_claim_a_decision_agent_ran():
    output = StringIO()
    display = ProgressDisplay(Console(file=output, width=180), False)
    display.event({"kind": "recovery.classified", "time": 1, "data": {
        "route": "decision", "phase": "task", "reason_code": "proposal/candidate"}})
    assert "需决策评估" in output.getvalue() and "自动决策" not in output.getvalue()
    display.event({"kind": "recovery.routing", "time": 1, "data": {
        "route": "decision", "phase": "task", "action": "design"}})
    assert "交由决策代理" in output.getvalue()
    display.close()
