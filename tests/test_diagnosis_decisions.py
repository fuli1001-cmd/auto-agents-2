"""Inconclusive diagnoses reach action decisions without becoming human-only blocks."""
import asyncio

import pytest
from test_decisions import ApprovingAgent, configure, setup
from test_recovery_contracts import failure, plan
from test_recovery_policy import ready

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import AA2Error, PostCommitError
from auto_agents_2.recovery import Diagnosis, adjudicate, evidence_references


class InconclusiveAgent(ApprovingAgent):
    def __init__(self, diagnosis="diagnose", actions=("recheck",)):
        super().__init__()
        self.diagnosis = diagnosis
        self.actions = iter(actions)

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if stage == "recovery_diagnosis":
            self.calls.append((stage, True))
            task = context["failure"]["task_id"]
            return Diagnosis(action=self.diagnosis, task_ids=[task] if task else [],
                evidence_refs=["failure.evidence"], reason="Need fresh startup evidence before selecting a repair.")
        if stage == "decision":
            self.calls.append((stage, True))
            action = next(self.actions)
            if action == "ask_user":
                return DecisionResult(action="ask_user", reason="A product choice needs user input.")
            assert action in context["decision_request"]["scope"]["candidates"]
            last = len(context["decision_request"]["evidence"]["history"]["recent"]) - 1
            return DecisionResult(action="approve", selected_action=action,
                reason="The next scoped action can produce the missing observation.",
                expected_result="Current startup and test results, without accepting the task.",
                evidence_refs=[f"history.recent[{last}]"])
        return await super().invoke(stage, context, schema, *args, **kwargs)


def proposal_failure():
    return failure(evidence={"checks": [], "review": None, "environment_error": None, "candidate_error": None,
                            "task": plan().tasks[2].model_dump()})


@pytest.mark.parametrize("action", ["diagnose", "blocked"])
async def test_inconclusive_diagnosis_uses_a_decision_and_declared_checks(tmp_path, action):
    agent = InconclusiveAgent(action)
    workflow, rid, _, _, record = ready(tmp_path, agent, proposal_failure())
    configure(workflow, execution={"max_diagnosis_rounds": 1}, automation={"max_diagnosis_rounds": 1})
    workflow.store.checkpoint(rid, "recovery/budgets/diagnosis/T3", {"round": 20})
    await workflow.recovery.resume(rid)
    assert agent.calls == [("recovery_diagnosis", True), ("decision", True)]
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 21
    assert not workflow.store.pending(rid) and workflow.store.get(rid, "recovery/active") is None
    completed = workflow.store.get(rid, f"recovery/completed/{record.id}/1")
    assert completed["decision"]["action"] == "recheck" and completed["actor"] == "agent"
    assert workflow.store.get(rid, "develop/task/T3")["phase"] == "reverify"
    assert "T3" not in workflow.store.get(rid, "develop/done", [])


async def test_a_new_diagnosis_requires_a_new_decision_and_reuses_its_reserved_permit(tmp_path):
    agent = InconclusiveAgent(actions=("diagnose", "recheck"))
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    assert active["selection"]["action"] == "diagnose"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 2
    assert agent.calls == [("recovery_diagnosis", True), ("decision", True)] * 2


async def test_inconclusive_decision_committed_before_interrupt_is_not_called_twice(tmp_path, monkeypatch):
    agent = InconclusiveAgent()
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    original = workflow._invoke
    async def interrupt(stage, *args, **kwargs):
        result = await original(stage, *args, **kwargs)
        if stage == "decision":
            raise PostCommitError("injected after committed decision")
        return result
    monkeypatch.setattr(workflow, "_invoke", interrupt)
    with pytest.raises(PostCommitError):
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow, "_invoke", original)
    await workflow.recovery.resume(rid)
    assert agent.calls == [("recovery_diagnosis", True), ("decision", True)]
    assert workflow.store.get(rid, "recovery/active") is None


async def test_decision_can_request_a_real_human_choice(tmp_path):
    agent = InconclusiveAgent(actions=("ask_user",))
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    async def leave_open(question):
        assert "A product choice needs user input" in question["prompt"]
        assert "先执行：diagnose" in question["prompt"] and "先执行：blocked" not in question["prompt"]
        raise asyncio.CancelledError()
    workflow.question_handler = leave_open
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    assert agent.calls == [("recovery_diagnosis", True), ("decision", True)]
    assert not any(a["actor"] == "human" for a in workflow.store.recovery_history(rid, "T3")["recent"])


@pytest.mark.parametrize("answer", [None, "y", "n", "source_drift", "race"])
async def test_old_blocked_question_is_reassessed_without_expanding_existing_consent(tmp_path, monkeypatch, answer):
    agent = InconclusiveAgent()
    workflow, rid, work, _, record = ready(tmp_path, agent, proposal_failure())
    original = workflow.recovery._diagnosis_assessment
    monkeypatch.setattr(workflow.recovery, "_diagnosis_assessment",
                        lambda failure, decision: (decision.model_copy(update={"action": "blocked"}), False))
    async def leave_open(question):
        assert "先执行：blocked" in question["prompt"]
        raise asyncio.CancelledError()
    workflow.question_handler = leave_open
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    q = workflow.store.pending(rid)[0]
    binding_key = q["key"].replace("recovery-cycle/", "recovery/authorization-inputs/")
    old_binding = workflow.store.get(rid, binding_key)
    monkeypatch.setattr(workflow.recovery, "_diagnosis_assessment", original)
    async def no_question(*args, **kwargs):
        pytest.fail("No new human question is required for the existing declared recheck")
    workflow.question_handler = no_question
    if answer in {"y", "n"}:
        workflow.store.answer(q["id"], answer)
    if answer == "source_drift":
        (work / "user.txt").write_text("preserved")
        with pytest.raises(AA2Error, match="source changed"):
            await workflow.recovery.resume(rid)
        assert (work / "user.txt").read_text() == "preserved"
    elif answer == "race":
        invoke = agent.invoke
        async def answer_while_deciding(stage, *args, **kwargs):
            result = await invoke(stage, *args, **kwargs)
            if stage == "decision":
                workflow.store.answer(q["id"], "n")
            return result
        agent.invoke = answer_while_deciding
        with pytest.raises(AA2Error, match="authorization changed"):
            await workflow.recovery.resume(rid)
    elif answer == "n":
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    else:
        await workflow.recovery.resume(rid)
        completed = workflow.store.get(rid, f"recovery/completed/{record.id}/1")
        if answer == "y":
            assert completed["decision"]["action"] == "blocked" and completed["actor"] == "human"
            assert workflow.store.get(rid, "recovery/active")["attempt"] == 2
        else:
            assert completed["decision"]["action"] == "recheck" and completed["actor"] == "agent"
            assert workflow.store.question(q["id"])["superseded"]
            assert workflow.store.question(q["id"])["answer"] is None
            assert workflow.store.get(rid, binding_key) != old_binding
    if answer is not None:
        assert workflow.store.get(rid, binding_key) == old_binding
    assert agent.calls.count(("recovery_diagnosis", True)) == 1
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1


def test_diagnosis_can_cite_the_actual_context_but_not_promote_proposals():
    record = proposal_failure()
    context = {"failure": record.model_dump(), "plan": plan().model_dump(),
               "current_issues": {"startup": {"evidence": {"hash": "a" * 64}}}}
    known = ["failure.evidence", "plan.tasks[2].checks[0]", "current_issues.startup.evidence", "a" * 64]
    decision = adjudicate(record, diagnosis=Diagnosis(action="diagnose", reason="Need a new observation",
        evidence_refs=known), plan=plan(), evidence_context=context)
    assert decision.action == "diagnose"
    context["failure"]["proposal"] = {"recovery_request": {"evidence_refs": ["invented"]}, "hash": "b" * 64}
    for ref in ("plan.tasks[99]", "current_issues.unknown", "failure.proposal.recovery_request", "invented", "b" * 64):
        with pytest.raises(AA2Error, match="invalid evidence_refs indexes: 0"):
            adjudicate(record, diagnosis=Diagnosis(action="recheck", reason="Invalid", evidence_refs=[ref]),
                       plan=plan(), evidence_context=context)
    # Expanding the contextual catalog does not rewrite the failure or its old bindings.
    assert "plan.tasks[2].checks[0]" not in evidence_references(record)


def test_controller_collection_limit_remains_a_human_boundary(tmp_path):
    workflow, _ = setup(tmp_path)
    record = failure(evidence={"checks": [{"failure_reasons": ["evidence_collection_failed"]}]})
    decision, assess = workflow.recovery._diagnosis_assessment(record, adjudicate(record))
    assert decision.action == "blocked" and not assess


@pytest.mark.parametrize("actions", [("recheck",), ("diagnose", "repair")])
async def test_unpublished_delivery_routes_inconclusive_diagnoses_through_decisions(tmp_path, actions):
    agent = InconclusiveAgent(actions=actions)
    workflow, rid = setup(tmp_path, agent)
    (tmp_path / "value.py").write_text("value = 1\n")
    result = await workflow.recovery.delivery_decision(rid, tmp_path,
        {"review": {"approved": False, "summary": "Need startup evidence", "findings": []}}, ["value.py"])
    assert result == actions[-1]
    assert not workflow.store.pending(rid)
    assert agent.calls == [("recovery_diagnosis", True), ("decision", True)] * len(actions)


async def test_published_delivery_keeps_its_existing_boundary(tmp_path):
    agent = InconclusiveAgent()
    workflow, rid = setup(tmp_path, agent)
    workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
    with pytest.raises(AA2Error, match="发布记录"):
        await workflow.recovery.delivery_decision(rid, tmp_path,
            {"review": {"approved": False, "summary": "Need startup evidence", "findings": []}}, ["value.py"])
    assert agent.calls == [("recovery_diagnosis", True)]
