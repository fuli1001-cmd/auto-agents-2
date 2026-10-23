"""Final observations can propose owned task repairs, never unscoped permits."""
import asyncio
import copy

import pytest
from test_recovery_contracts import failure
from test_recovery_policy import ready

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import AA2Error, PostCommitError
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import PlanResult
from auto_agents_2.recovery import Diagnosis, RepairTarget, adjudicate
from auto_agents_2.workspace import tree_hash


def final_failure():
    return failure(kind="review", component="final_review", origin_id="final", task_id="",
        step="final_review", evidence_refs=["part3.py", "verify/review"],
        evidence={"review": {"approved": False, "summary": "Visible feedback missing"}})


def repair():
    return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=["failure.evidence.review", "part3.py"],
        reason="Fix the existing task's visible feedback", repair_targets=[RepairTarget(task_id="T3",
            paths=["part3.py"], evidence_refs=["part3.py"], reason="Current source belongs to T3")])


@pytest.mark.parametrize("rounds", [4, 21])
def test_final_diagnosis_can_propose_repair_without_an_executable_unscoped_choice(tmp_path, rounds):
    workflow, rid, work, plan, record = ready(tmp_path, record=final_failure())
    workflow.store.checkpoint(rid, "rounds/T3", {"round": rounds, "stalled": 0})
    policy = workflow.recovery_policy
    choices, _, _ = policy.available_actions(rid, record, work, plan, adjudicate(record))
    assert "repair" not in choices
    allowed = policy.diagnosis_actions(rid, record, work, plan)
    assert "repair" in allowed  # Red on the original final caller contract.
    assert allowed["repair"]["task_ids"] == [] and allowed["repair"]["repair_targets"] == []
    context = {"failure": record.model_dump(), "plan": plan.model_dump(), "allowed_actions": allowed}
    decision = adjudicate(record, plan=plan, diagnosis=repair(), evidence_context=context, workspace=work)
    executable, _, _ = policy.available_actions(rid, record, work, plan, decision)
    assert executable["repair"].task_ids == ["T3"]
    assert executable["repair"].repair_targets == repair().repair_targets
    assert executable["repair"].budget_owners == ["T3"]
    assert workflow.store.get(rid, "rounds/T3")["round"] == rounds


@pytest.mark.parametrize("boundary", ["published", "no_plan", "verification_only", "other_owner", "unknown_task"])
def test_final_proposal_entry_does_not_expand_other_boundaries(tmp_path, boundary):
    workflow, rid, work, plan, record = ready(tmp_path, record=final_failure())
    if boundary == "published":
        workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
    elif boundary == "no_plan":
        plan = None
    elif boundary == "verification_only":
        plan = PlanResult(summary="verify", document="verify", mode="verification_only", tasks=[], final_checks=plan.final_checks)
    elif boundary == "other_owner":
        record = record.model_copy(update={"origin_id": "delivery"})
    else:
        record = record.model_copy(update={"task_id": "unknown"})
    assert "repair" not in workflow.recovery_policy.diagnosis_actions(rid, record, work, plan)


@pytest.mark.parametrize("invalid", ["no_task", "no_targets", "unknown_task", "wrong_owner", "missing_path", "secret", "unknown_ref", "proposal_only"])
def test_final_repair_still_requires_owned_current_evidence(tmp_path, invalid):
    workflow, rid, work, plan, record = ready(tmp_path, record=final_failure())
    value = repair().model_dump()
    if invalid == "no_task":
        value.update(task_ids=[], repair_targets=[])
    elif invalid == "no_targets":
        value["repair_targets"] = []
    elif invalid == "unknown_task":
        value["task_ids"] = ["unknown"]
    elif invalid == "wrong_owner":
        value["repair_targets"][0]["paths"] = ["part1.py"]
    elif invalid == "missing_path":
        value["repair_targets"][0]["paths"] = ["tests/test_3.py"]
    elif invalid == "secret":
        value["repair_targets"][0]["paths"] = [".env"]
    elif invalid == "unknown_ref":
        value["repair_targets"][0]["evidence_refs"] = ["invented"]
    else:
        record = record.model_copy(update={"proposal": {"changes": [{"path": "part3.py", "content": "claim"}]}})
        value["repair_targets"][0]["evidence_refs"] = ["failure.proposal.changes"]
    context = {"failure": record.model_dump(), "plan": plan.model_dump(),
        "allowed_actions": workflow.recovery_policy.diagnosis_actions(rid, record, work, plan)}
    with pytest.raises(AA2Error):
        adjudicate(record, plan=plan, diagnosis=Diagnosis.model_validate(value), evidence_context=context, workspace=work)
    assert not workflow.store.get(rid, "recovery/implementation_pending")


async def test_old_pending_diagnosis_answer_is_preserved_then_new_call_can_target_repair(tmp_path, monkeypatch):
    workflow, rid, work, plan, record = ready(tmp_path, record=final_failure())
    workflow.store.checkpoints(rid, {"develop/done": [t.id for t in plan.tasks],
        "rounds/T3": {"round": 4, "stalled": 0}, "verify/identity": "old-verification"})
    policy = workflow.recovery_policy
    actual = policy.diagnosis_actions

    def old_actions(*args):
        return {k: v for k, v in actual(*args).items() if k != "repair"}

    monkeypatch.setattr(policy, "diagnosis_actions", old_actions)
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        calls.append((stage, copy.deepcopy(context)))
        if stage == "decision":
            return DecisionResult(action="ask_user", reason="No bounded repair is available", evidence_refs=["failure"])
        if "repair" not in context["allowed_actions"]:
            return Diagnosis(action="diagnose", task_ids=["T3"], evidence_refs=["failure"], reason="No repair entry")
        return repair()

    async def pending(question):
        raise asyncio.CancelledError()

    workflow.agent.invoke = invoke
    workflow.question_handler = pending
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    question = workflow.store.pending(rid)[0]
    active = workflow.store.get(rid, "recovery/active")
    old_permit_key = active["diagnosis_permit"]
    old_receipt = workflow.store.get(rid, old_permit_key)
    with workflow.store.connect() as db:
        frozen_keys = [r[0] for r in db.execute("SELECT key FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/invocation-inputs/%'", (rid,))]
    frozen = {k: workflow.store.get(rid, k) for k in frozen_keys}
    assert old_receipt["phase"] == "completed"
    assert len(calls) == 2
    monkeypatch.setattr(policy, "diagnosis_actions", actual)
    workflow.store.answer(question["id"], "y", question["artifact_hash"])
    await workflow.recovery.resume(rid)
    assert len(calls) == 2  # Old diagnosis/decision do not repeat after upgrade.
    await workflow.recovery.resume(rid)
    assert [c[0] for c in calls] == ["recovery_diagnosis", "decision", "recovery_diagnosis"]
    assert workflow.store.get(rid, old_permit_key) == old_receipt
    assert {k: workflow.store.get(rid, k) for k in frozen_keys} == frozen
    assert workflow.store.question(question["id"])["answer"] == "y"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/final")["round"] == 2
    assert workflow.store.get(rid, "rounds/T3")["round"] == 4
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    assert workflow.store.get(rid, "verify/identity") is None


async def test_real_final_review_repair_reexecutes_checks_and_waits_for_new_acceptance(tmp_path):
    from test_workflow import CodingAgent, engine

    from auto_agents_2.models import Change, Finding, ImplementationResult, ReviewResult

    class FinalAgent(CodingAgent):
        implementations = 0
        final_reviews = 0
        protection_reviews = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement":
                self.implementations += 1
                return ImplementationResult(summary="addition candidate", changes=[
                    Change(path="calc.py", content="def add(a,b):\n    return " + ("5\n" if self.implementations == 1 else "a + b\n")),
                    Change(path="tests/test_calc.py", content="from calc import add\ndef test_sum(): assert add(2,3)==5\n" +
                        ("def test_general(): assert add(1,1)==2\n" if self.implementations > 1 else ""))])
            if stage == "recovery_diagnosis":
                assert context["failure"]["origin_id"] == "final" and not context["failure"]["task_id"]
                assert "repair" in context["allowed_actions"]
                return Diagnosis(action="repair", task_ids=["T1"], evidence_refs=["calc.py", "failure.evidence.review"],
                    reason="General addition failed semantic review", repair_targets=[RepairTarget(task_id="T1",
                        paths=["calc.py", "tests/test_calc.py"], evidence_refs=["calc.py", "tests/test_calc.py"],
                        reason="Correct implementation and strengthen tests without removing original assertion")])
            if stage == "verify":
                self.final_reviews += 1
                if self.final_reviews == 1:
                    assert "return 5" in (workspace/"calc.py").read_text()
                    return ReviewResult(approved=False, summary="One passing example does not prove addition",
                        findings=[Finding(severity="major", location="calc.py:2", description="Constant result",
                            evidence="Source returns 5 for all inputs", resolution="Repair T1 and test another pair")])
                assert "a + b" in (workspace/"calc.py").read_text()
                rows = [r for r in context["evidence"] if r.get("tests")]
                assert rows and all(r["exit_code"] == 0 and r["tests"] == 2 for r in rows)
                assert self.protection_reviews >= 2  # Initial and repaired test proposals reviewed.
                return ReviewResult(approved=True, summary="New checks and source resolve the final finding")
            if context.get("review_kind") == "test_changes":
                self.protection_reviews += 1
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    project = tmp_path/"project"
    project.mkdir()
    requirements = tmp_path/"requirements.md"
    requirements.write_text("Add two numbers")
    agent = FinalAgent()
    workflow = engine(project, agent)
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED" and result["data"]["outcome"] == "DELIVERED"
    assert agent.implementations == 2 and agent.final_reviews == 2
    rid = result["id"]
    assert workflow.store.get(rid, "rounds/T1")["round"] == 2
    assert workflow.store.get(rid, "develop/done") == ["T1"]
    assert workflow.store.get(rid, "verify/review")["approved"] is True
    assert not workflow.store.pending(rid)
    assert workflow.store.get(rid, "recovery/active") is None


@pytest.mark.parametrize("drift", ["source", "plan"])
async def test_final_repair_cannot_reuse_a_permit_after_input_drift(tmp_path, drift):
    workflow, rid, work, plan, record = ready(tmp_path, record=final_failure())
    context = {"failure": record.model_dump(), "plan": plan.model_dump(),
        "allowed_actions": workflow.recovery_policy.diagnosis_actions(rid, record, work, plan)}
    decision = adjudicate(record, diagnosis=repair(), plan=plan, evidence_context=context, workspace=work)
    active = workflow.store.get(rid, "recovery/active")
    _, key = await workflow.recovery_policy.authorize(rid, record, active, work, plan, decision,
        operation="validated-final-repair", established=True)
    receipt = workflow.store.get(rid, key)
    if drift == "source":
        (work/"part3.py").write_text("user change\n")
    else:
        plan = plan.model_copy(update={"document": "changed plan"})
    with pytest.raises(StateConflict, match="inputs changed"):
        await workflow.recovery_policy.authorize(rid, record, active, work, plan, decision,
            operation="validated-final-repair", established=True)
    assert workflow.store.get(rid, key) == receipt


async def test_final_dispatch_postcommit_restart_does_not_reopen_or_charge_twice(tmp_path, monkeypatch):
    workflow, rid, work, plan, record = ready(tmp_path, record=final_failure())
    workflow.store.checkpoints(rid, {"develop/done": [t.id for t in plan.tasks],
        "rounds/T3": {"round": 4}, "verify/identity": "old",
        **{f"develop/task/{t.id}": {"commit": "old", "checks": []} for t in plan.tasks}})
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        calls.append(stage)
        assert "repair" in context["allowed_actions"]
        return repair()

    workflow.agent.invoke = invoke
    original = workflow.store.finish_recovery

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise PostCommitError("after final repair dispatch")

    monkeypatch.setattr(workflow.store, "finish_recovery", interrupt)
    with pytest.raises(PostCommitError):
        await workflow.recovery.resume(rid)
    before = {k: workflow.store.get(rid, k) for k in ("develop/task/T3", "develop/task/T4", "rounds/T3", "develop/done")}
    source = tree_hash(work)
    monkeypatch.setattr(workflow.store, "finish_recovery", original)
    await workflow.recovery.resume(rid)
    assert calls == ["recovery_diagnosis"]
    assert {k: workflow.store.get(rid, k) for k in before} == before
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/final")["round"] == 1
    assert before["rounds/T3"]["round"] == 4 and source == tree_hash(work)
    assert before["develop/done"] == ["T1", "T2"]
    assert before["develop/task/T3"]["previous_evidence"]["commit"] == "old"
    assert before["develop/task/T3"]["phase"] == "rejected"
    assert before["develop/task/T4"]["phase"] == "reverify"
    assert workflow.store.get(rid, "verify/identity") is None
