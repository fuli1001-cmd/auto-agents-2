"""Continuation decisions are evidence-bound, durable, and independent of old ceilings."""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from evidence_test_fixtures import seed_requirements
from test_decisions import ApprovingAgent, configure, setup
from test_recovery_contracts import failure, plan

from auto_agents_2.decisions import DecisionResult, FactAnswer
from auto_agents_2.environment import Environment
from auto_agents_2.errors import (
    AA2Error,
    PatchRepairExhausted,
    PostCommitError,
    RetryableInfrastructureError,
)
from auto_agents_2.interaction import choice_spec
from auto_agents_2.models import Change, CheckResult, Command, ImplementationResult, PlanResult, TaskSpec
from auto_agents_2.recovery import RecoveryNeeded, adjudicate
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


def ready(tmp_path, agent=None, record=None):
    workflow, rid = setup(tmp_path, agent)
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "integration", base)
    contract = plan()
    seed_requirements(workflow, rid, contract)
    workflow.store.checkpoint(rid, "plan", contract.model_dump())
    record = record or failure()
    workflow.recovery.submit(rid, record)
    return workflow, rid, work, contract, record


async def authorize(workflow, rid, work, contract, record, **kwargs):
    active = workflow.store.get(rid, "recovery/active")
    return await workflow.recovery_policy.authorize(rid, record, active, work, contract,
        adjudicate(record), operation="fixture", **kwargs)


async def test_legacy_ceiling_and_stall_cannot_prevent_first_diagnosis(tmp_path):
    workflow, rid, _, _, record = ready(tmp_path)
    configure(workflow, execution={"max_rounds": 1, "max_diagnosis_rounds": 1},
              automation={"max_implementation_rounds": 1, "max_diagnosis_rounds": 1})
    workflow.store.checkpoints(rid, {"rounds/T3": {"round": 20, "stalled": 19},
        "automation/progress/T3": {"missing": 2, "stalled": 19},
        "recovery/budgets/diagnosis/T3": {"round": 8}})
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 9
    assert workflow.store.get(rid, "rounds/T3")["round"] == 20
    assert workflow.store.get(rid, "automation/progress/T3") == {"missing": 2, "stalled": 19}
    saved = workflow.store.get(rid, "develop/task/T3")
    assert saved["phase"] == "rejected" and saved["recovery_permit"]
    assert not workflow.store.pending(rid)
    assert workflow.store.get(rid, f"recovery/completed/{record.id}/1")["actor"] == "rule"


async def test_new_observation_requires_new_decision_but_restart_reuses_permit(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    first, key = await authorize(workflow, rid, work, contract, record, force_decision=True)
    calls = len(workflow.agent.calls)
    assert first.action == "diagnose"
    assert (await authorize(workflow, rid, work, contract, record, force_decision=True))[1] == key
    assert len(workflow.agent.calls) == calls
    workflow.store.checkpoint(rid, "recovery/active", None)
    workflow.recovery.submit(rid, record)
    _, second = await authorize(workflow, rid, work, contract, record, force_decision=True)
    assert second != key and len(workflow.agent.calls) == calls + 1


async def test_committed_authorization_is_reused_after_postcommit_error(tmp_path, monkeypatch):
    workflow, rid, work, contract, record = ready(tmp_path)
    original = workflow.store.apply_decision
    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise PostCommitError("fixture after commit")
    monkeypatch.setattr(workflow.store, "apply_decision", interrupt)
    with pytest.raises(PostCommitError):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    monkeypatch.setattr(workflow.store, "apply_decision", original)
    await authorize(workflow, rid, work, contract, record, force_decision=True)
    assert len(workflow.agent.calls) == 1


async def test_changed_candidate_cannot_apply_model_authorization(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    original = workflow.agent.invoke
    async def drift(*args, **kwargs):
        result = await original(*args, **kwargs)
        (work / "user-change.txt").write_text("preserve this change")
        return result
    workflow.agent.invoke = drift
    with pytest.raises(AA2Error, match="changed"):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    assert (work / "user-change.txt").read_text() == "preserve this change"
    assert not workflow.store.recovery_history(rid, "T3")["recent"]


async def test_human_pause_racing_agent_approval_wins(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    progress = workflow.convergence.state(rid, "T3")
    key = "automation/human/" + digest(["T3", "diagnosis/1", progress])
    interaction = choice_spec(["continue", "pause"], ["continue", "pause"], yes_no=True)
    interaction["pause_on"] = "n"
    question = workflow.store.request(rid, key, "legacy", "legacy", interaction)
    original = workflow.agent.invoke
    async def pause(*args, **kwargs):
        result = await original(*args, **kwargs)
        workflow.store.answer(question["id"], "n")
        return result
    workflow.agent.invoke = pause
    with pytest.raises(AA2Error, match="authorization changed"):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    assert not workflow.store.recovery_history(rid, "T3")["recent"]
    assert workflow.store.question(question["id"])["answer"] == "n"


async def test_old_unanswered_stop_is_superseded_without_forging_answer(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    workflow.store.checkpoint(rid, "automation/progress/T3", {"missing": 2})
    key = "automation/human/" + digest(["T3", "diagnosis/1", {"missing": 2}])
    question = workflow.store.request(rid, key, "old stop")
    await authorize(workflow, rid, work, contract, record)
    saved = workflow.store.question(question["id"])
    assert saved["superseded"] and saved["answer"] is None


async def test_old_unanswered_implementation_budget_question_is_retired_on_effective_authorization(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    workflow.store.checkpoint(rid, "rounds/T3", {"round": 20})
    question = workflow.store.request(rid, "round-budget/T3/20/old-observation", "old round cap")
    await authorize(workflow, rid, work, contract, record)
    saved = workflow.store.question(question["id"])
    assert saved["superseded"] and saved["answer"] is None


async def test_old_answer_retains_single_action_scope(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    key = "automation/human/" + digest(["T3", "diagnosis/1", {}])
    interaction = choice_spec(["continue", "pause"], ["continue", "pause"], yes_no=True)
    interaction["pause_on"] = "n"
    question = workflow.store.request(rid, key, "legacy one diagnosis", "old", interaction)
    workflow.store.answer(question["id"], "y")
    _, permit_key = await authorize(workflow, rid, work, contract, record)
    permit = workflow.store.get(rid, permit_key)
    cycle = workflow.store.get(rid, "recovery/cycles/" + permit["cycle_id"])
    assert permit["actor"] == "human" and not cycle["human_grant"]
    assert workflow.store.question(question["id"])["answer"] == "y"


async def test_one_human_cycle_covers_diagnosis_and_scoped_dispatch(tmp_path):
    workflow, rid, _, _, record = ready(tmp_path)
    rule_key = "recovery/rules/" + digest(["T3", record.step, sorted(record.obligations), "diagnose"])
    workflow.store.checkpoint(rid, rule_key, {"previous": True})
    original = workflow.agent.invoke
    async def defer(stage, *args, **kwargs):
        if stage == "decision":
            return DecisionResult(action="ask_user", reason="User should choose whether to spend another attempt")
        return await original(stage, *args, **kwargs)
    workflow.agent.invoke = defer
    questions = []
    async def approve(question):
        questions.append(question)
        assert "本轮至多" in question["prompt"] and "T3" in question["prompt"]
        return "y"
    workflow.question_handler = approve
    await workflow.recovery.resume(rid)
    assert len(questions) == 1
    actions = workflow.store.recovery_history(rid, "T3")["recent"]
    assert [a["action"]["action"] for a in actions] == ["diagnose", "repair"]
    assert all(a["actor"] == "human" for a in actions)


async def test_repeated_identical_recheck_can_receive_a_fresh_model_permit(tmp_path):
    class RecheckingAgent(ApprovingAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "decision":
                self.calls.append((stage, True))
                return DecisionResult(action="approve", selected_action="recheck", reason="Retry a transient failure",
                    expected_result="A current valid report", evidence_refs=["failure"])
            if stage == "recovery_diagnosis":
                from auto_agents_2.recovery import Diagnosis
                return Diagnosis(action="recheck", task_ids=["T3"], reason="Transient report capture failure",
                                 evidence_refs=context["failure"]["evidence_refs"])
            return await super().invoke(stage, context, schema, *args, **kwargs)
    record = failure(kind="evidence", evidence={"checks": [{"argv": ["pytest"], "exit_code": 1,
        "failure_reasons": ["test_report_invalid"]}]})
    workflow, rid, _, _, _ = ready(tmp_path, RecheckingAgent(), record)
    await workflow.recovery.resume(rid)
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    assert not workflow.store.pending(rid)
    actions = workflow.store.recovery_history(rid, "T3")["recent"]
    rechecks = [a for a in actions if a["action"]["action"] == "recheck"]
    assert len(rechecks) == 2 and rechecks[-1]["actor"] == "agent"
    assert rechecks[0]["source_hash"] == rechecks[1]["source_hash"]


async def test_decision_failure_does_not_recursively_authorize_itself(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    calls = []
    async def invalid(*args, **kwargs):
        calls.append(args[0])
        return DecisionResult(action="approve", selected_action="invented", reason="invalid", evidence_refs=["failure"])
    workflow.agent.invoke = invalid
    async def pause(_):
        return "n"
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    assert calls == ["decision", "decision"]
    assert not workflow.store.recovery_history(rid, "T3")["recent"]


async def test_stalled_unrelated_task_does_not_block_technical_facts(tmp_path):
    workflow, rid, work, _, _ = ready(tmp_path)
    workflow.store.checkpoint(rid, "automation/progress/T2", {"missing": 100, "stalled": 100})
    async def answer(*args, **kwargs):
        return DecisionResult(action="answer", reason="Configured Python version", evidence_refs=["facts"],
                              answers=[FactAnswer(index=0, fact_ref="environment.python_version")])
    workflow.agent.invoke = answer
    result = await workflow.decisions.clarify(rid, "clarify/technical", ["Python version?"], "question", work)
    assert result == {0: workflow.config.load().environment.python_version}


def test_precheck_failure_is_not_missing_evidence_and_milestones_do_not_repeat(tmp_path):
    workflow, rid = setup(tmp_path)
    for attempt in (1, 2, 3):
        workflow.convergence.observe(rid, "T1", attempt, phase="environment")
    state = workflow.convergence.state(rid, "T1")
    assert state["missing"] == 0 and state["stage_failures"] == {"environment": 3}
    assert workflow.convergence.milestone(rid, "T1", "environment_ready", {"fingerprint": "verified"})
    assert not workflow.convergence.milestone(rid, "T1", "environment_ready", {"fingerprint": "new"})
    assert workflow.convergence.state(rid, "T1") == state


def test_v6_migration_preserves_counts_pause_and_answers(tmp_path):
    workflow, rid = setup(tmp_path)
    store = workflow.store
    store.checkpoints(rid, {"rounds/T1": {"round": 21, "stalled": 20}, "plan": plan().model_dump()})
    question = store.request(rid, "fixture", "answer")
    store.answer(question["id"], "y")
    store.update(rid, status="PAUSED")
    with store.connect() as db:
        db.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    store.migrate()
    store.migrate()
    assert store.run(rid)["status"] == "PAUSED"
    assert store.get(rid, "rounds/T1") == {"round": 21, "stalled": 20}
    assert store.question(question["id"])["answer"] == "y"
    assert store.get(rid, "automation/engineering/T1")["legacy_baseline"]
    with sqlite3.connect(store.root / "state-v6.backup.sqlite") as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "6"
    with store.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "8"


async def test_task_reaches_verified_result_after_twenty_failed_rounds(tmp_path):
    class PersistentAgent(ApprovingAgent):
        implementations = 0
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "implement":
                self.implementations += 1
                return ImplementationResult(summary="Next attempt", changes=[Change(path="value.py",
                    content=f"value = {self.implementations}\n")])
            if stage == "decision":
                self.calls.append((stage, True))
                return DecisionResult(action="approve", selected_action="repair", evidence_refs=["failure"],
                    reason="One more scoped implementation attempt", expected_result="The fixed assertion passes")
            return await super().invoke(stage, context, schema, *args, **kwargs)
    agent = PersistentAgent()
    workflow, rid = setup(tmp_path, agent)
    configure(workflow, execution={"max_rounds": 1}, automation={"max_implementation_rounds": 2})
    base = workflow.workspaces.committed_baseline(rid, "main")
    command = Command(argv=[sys.executable, "-B", "-c", "from value import value; assert value == 21"])
    task = TaskSpec(id="T1", title="value", description="reach 21", requirement_ids=["R1"],
                    allowed_paths=["value.py"], acceptance=["value equals 21"], checks=[command])
    contract = PlanResult(summary="value", document="value", tasks=[task], final_checks=[command])
    workflow.store.checkpoint(rid, "plan", contract.model_dump())
    workflow.workspaces.allocate(rid, "integration", base)
    for _ in range(22):
        try:
            commit = await workflow._task(rid, task, base, contract, {"specification": {}})
            break
        except RecoveryNeeded as exc:
            workflow.recovery.submit(rid, exc.failure)
            await workflow.recovery.resume(rid)
    else:
        pytest.fail("No verified result")
    assert commit and agent.implementations == 21
    assert workflow.store.get(rid, "rounds/T1")["round"] == 21
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["checks"][0]["exit_code"] == 0 and saved["review"]["approved"]
    assert not workflow.store.pending(rid)


async def test_environment_retry_exhaustion_is_a_decision_not_a_stop(tmp_path):
    workflow, rid, work, _, _ = ready(tmp_path)
    configure(workflow, execution={"infra_retries": 0})
    attempts = []
    async def environment(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise RetryableInfrastructureError("fixture transient connection failure")
        return Environment(Path(sys.prefix), "verified-environment")
    workflow._environment_once = environment
    workflow._environment = type(workflow)._environment.__get__(workflow)
    result = await workflow._environment(work, rid)
    assert result.fingerprint == "verified-environment" and len(attempts) == 2
    assert [stage for stage, _ in workflow.agent.calls] == ["decision"]
    assert not workflow.store.pending(rid)


async def test_exhausted_candidate_correction_returns_to_recovery_without_missing_check_count(tmp_path):
    workflow, rid = setup(tmp_path)
    base = workflow.workspaces.committed_baseline(rid, "main")
    command = Command(argv=[sys.executable, "-c", "assert True"])
    task = TaskSpec(id="T1", title="candidate", description="candidate", requirement_ids=["R1"],
                    allowed_paths=["calc.py", "tests"], acceptance=["valid candidate"], checks=[command])
    contract = PlanResult(summary="candidate", document="candidate", tasks=[task], final_checks=[command])
    async def exhausted(*args, **kwargs):
        raise PatchRepairExhausted("fixture replacement correction exhausted")
    workflow.patch_repair.prepare = exhausted
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, task, base, contract, {"specification": {}})
    assert caught.value.failure.kind == "proposal" and caught.value.failure.step == "candidate"
    progress = workflow.convergence.state(rid, "T1")
    assert progress["missing"] == 0 and progress["stage_failures"] == {"candidate": 1}


async def test_pending_human_authorization_cannot_follow_plan_drift(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    async def defer(*args, **kwargs):
        return DecisionResult(action="ask_user", reason="Need user information")
    workflow.agent.invoke = defer
    async def interrupt(question):
        raise asyncio.CancelledError()
    workflow.question_handler = interrupt
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    question = workflow.store.pending(rid)[0]
    workflow.store.answer(question["id"], "y")
    contract.tasks[2].allowed_paths.append("new-scope.py")
    workflow.store.checkpoint(rid, "plan", contract.model_dump())
    with pytest.raises(AA2Error, match="original inputs"):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    assert workflow.store.question(question["id"])["answer"] == "y"
    assert not workflow.store.recovery_history(rid, "T3")["recent"]


@pytest.mark.parametrize('grounded', [True, False])
async def test_unpublished_delivery_can_diagnose_beyond_legacy_limit(tmp_path, grounded):
    from authority_review_fixtures import recorded_review

    from auto_agents_2.faults import StateConflict
    workflow, rid = setup(tmp_path)
    (tmp_path / "value.py").write_text("value = 1\n")
    workflow.store.checkpoint(rid, "recovery/budgets/diagnosis/delivery", {"round": 20})
    review = {"approved": False, "summary": "candidate needs repair", "findings": []}
    if grounded:
        review = recorded_review(workflow, rid, review, work=tmp_path)
        result = await workflow.recovery.delivery_decision(rid, tmp_path, {"review": review}, ["value.py"])
        assert result == "repair"
    else:
        with pytest.raises(StateConflict, match='ungrounded review'):
            await workflow.recovery.delivery_decision(rid, tmp_path, {"review": review}, ["value.py"])
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/delivery")["round"] == 21
    assert not workflow.store.pending(rid)


async def test_user_edit_after_repair_dispatch_is_preserved(tmp_path):
    workflow, rid, work, contract, _ = ready(tmp_path)
    target = work / "part3.py"
    target.write_text("retained = True\n")
    workflow.store.checkpoint(rid, "develop/task/T3", {"phase": "rejected", "workspace": str(work),
        "source_hash": tree_hash(work)})
    await workflow.recovery.resume(rid)
    calls = len(workflow.agent.calls)
    target.write_text("user_edit = True\n")
    base = workflow.workspaces.committed_baseline(rid, "main")
    with pytest.raises(AA2Error, match="preserve user edits"):
        await workflow._task(rid, contract.tasks[2], base, contract, {"specification": {}})
    assert target.read_text() == "user_edit = True\n"
    assert len(workflow.agent.calls) == calls


async def test_unpublished_identical_delivery_recheck_requires_a_new_model_permit(tmp_path):
    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "decision":
                self.calls.append((stage, True))
                return DecisionResult(action="approve", selected_action="recheck", evidence_refs=["failure"],
                    reason="Try the same input again to distinguish a transient failure", expected_result="A valid report")
            if stage == "recovery_diagnosis":
                from auto_agents_2.recovery import Diagnosis
                return Diagnosis(action="recheck", reason="Repeat collection", evidence_refs=context["failure"]["evidence_refs"])
            return await super().invoke(stage, context, schema, *args, **kwargs)
    workflow, rid = setup(tmp_path, Agent())
    row = CheckResult(argv=["pytest"], cwd=".", exit_code=1, process_exit_code=0, duration=1,
        source_hash=tree_hash(tmp_path), environment_hash="fixture", output="fixture missing report",
        failure_reasons=["test_report_missing"])
    feedback = {"checks": [row.model_dump()]}
    assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ["value.py"], observation="first") == "recheck"
    first = workflow.store.get(rid, "recovery/delivery-recheck-active")
    workflow.recovery.start_delivery_recheck(rid, tmp_path)
    workflow.recovery.complete_delivery_recheck(rid, feedback)
    assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ["value.py"], observation="second") == "recheck"
    second = workflow.store.get(rid, "recovery/delivery-recheck-active")
    assert second != first and workflow.store.get(rid, first)["phase"] == "completed"
    permit = workflow.store.get(rid, workflow.store.get(rid, second)["permit"])
    assert permit["actor"] == "agent" and len(workflow.agent.calls) == 1
