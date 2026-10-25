"""Inconclusive diagnoses reach action decisions without becoming human-only blocks."""

import asyncio

import pytest
from authority_review_fixtures import recorded_review
from test_decisions import ApprovingAgent, configure, setup
from test_recovery_contracts import failure, plan
from test_recovery_policy import ready

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import AA2Error, PostCommitError
from auto_agents_2.interaction import choice_spec
from auto_agents_2.recovery import Diagnosis, adjudicate, evidence_references
from auto_agents_2.recovery_identity import legacy_failure_records
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import tree_hash


def legacy_blocked_authorizer(workflow, monkeypatch):
    """Write an actual old unanswered binding; new code cannot authorize blocked."""
    original = workflow.recovery_policy.authorize
    async def authorize(rid, record, active, work, contract, suggested, **kwargs):
        if suggested.action != "blocked":
            return await original(rid, record, active, work, contract, suggested, **kwargs)
        cid = workflow.recovery_policy.cycle_id(record, active)
        suffix = digest([cid, kwargs["operation"]])
        tasks = [task for task in contract.tasks if not record.task_id or task.id == record.task_id or task.id in suggested.task_ids]
        binding = {"source_hash": tree_hash(work), "plan_hash": digest(contract.model_dump()),
            "scope_hash": digest([[task.model_dump() for task in tasks], record.evidence.get("allowed_paths", [])]),
            "candidates": digest({"blocked": suggested.model_dump()}),
            "failure": digest(legacy_failure_records(record.model_dump())[0])}
        workflow.store.checkpoint(rid, "recovery/authorization-inputs/" + suffix, binding)
        interaction = choice_spec(["授权", "暂停"], ["continue", "pause"], yes_no=True)
        interaction["pause_on"] = "n"
        await workflow._ask(rid, "recovery-cycle/" + suffix, "旧版恢复请求\n先执行：blocked", suffix, interaction)
        pytest.fail("The legacy fixture only writes a pending unanswered question")
    monkeypatch.setattr(workflow.recovery_policy, "authorize", authorize)
    return original


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
    original_authorize = legacy_blocked_authorizer(workflow, monkeypatch)
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
    monkeypatch.setattr(workflow.recovery_policy, "authorize", original_authorize)
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
    proposed = "failure.proposal.recovery_request"
    adjudicate(record, diagnosis=Diagnosis(action="recheck", reason="Inspect the proposed next step",
        evidence_refs=[known[0], proposed]), plan=plan(), evidence_context=context)
    with pytest.raises(AA2Error, match="requires controller evidence"):
        adjudicate(record, diagnosis=Diagnosis(action="recheck", reason="Unverified suggestion",
            evidence_refs=[proposed]), plan=plan(), evidence_context=context)
    for ref in ("plan.tasks[99]", "current_issues.unknown", "failure.proposal.unknown", "invented", "b" * 64):
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
        {"review": recorded_review(workflow, rid, {"approved": False, "summary": "Need startup evidence", "findings": []})}, ["value.py"])
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


async def pending_completed_diagnosis(tmp_path, monkeypatch):
    agent = InconclusiveAgent()
    workflow, rid, work, contract, record = ready(tmp_path, agent, proposal_failure())
    original_authorize = legacy_blocked_authorizer(workflow, monkeypatch)
    original = workflow.recovery._diagnosis_assessment
    monkeypatch.setattr(workflow.recovery, "_diagnosis_assessment",
                        lambda failure, decision: (decision.model_copy(update={"action": "blocked"}), False))
    async def leave_open(question):
        raise asyncio.CancelledError()
    workflow.question_handler = leave_open
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow.recovery_policy, "authorize", original_authorize)
    monkeypatch.setattr(workflow.recovery, "_diagnosis_assessment", original)
    async def no_question(*args, **kwargs):
        pytest.fail("The completed diagnosis should reach the automatic action decision")
    workflow.question_handler = no_question
    active = workflow.store.get(rid, "recovery/active")
    key = f"invocation/recovery/diagnose/{record.id}/{active['diagnosis_round']}"
    return workflow, rid, work, contract, active, key


@pytest.mark.parametrize("change", ["call_identity", "overwritten_response", "missing_invocation"])
async def test_completed_diagnosis_is_authoritative_across_full_resume(tmp_path, monkeypatch, change):
    workflow, rid, _, _, active, key = await pending_completed_diagnosis(tmp_path, monkeypatch)
    before = workflow.store.get(rid, active["diagnosis_permit"])
    replacement = Diagnosis(action="recheck", task_ids=["T3"], evidence_refs=["failure.evidence"],
                            reason="Regenerated prose and action must not replace a completed diagnosis.")
    if change == "call_identity":
        cfg = workflow.config.load().model_dump(mode="json")
        cfg["config_revision"] += 1
        write_json(workflow.config.path, cfg)
    elif change == "overwritten_response":
        saved = workflow.store.get(rid, key)
        workflow.store.checkpoint(rid, key, {**saved, "result": replacement.model_dump()})
    else:
        workflow.store.invalidate(rid, [key])
    invocation_before = workflow.store.get(rid, key)
    original = workflow.agent.invoke
    async def regenerated(stage, *args, **kwargs):
        if stage == "recovery_diagnosis":
            workflow.agent.calls.append((stage, True))
            return replacement
        return await original(stage, *args, **kwargs)
    workflow.agent.invoke = regenerated
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "develop/task/T3")["phase"] == "reverify"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1
    assert workflow.store.get(rid, active["diagnosis_permit"]) == before
    assert workflow.store.get(rid, key) == invocation_before
    assert workflow.agent.calls == [("recovery_diagnosis", True), ("decision", True)]
    assert not workflow.store.pending(rid)


@pytest.mark.parametrize("change", ["source", "plan", "failure", "scope", "cycle", "binding", "invalid_result"])
async def test_completed_diagnosis_cannot_bypass_changed_inputs_or_missing_proof(tmp_path, monkeypatch, change):
    workflow, rid, work, contract, active, _ = await pending_completed_diagnosis(tmp_path, monkeypatch)
    permit_key = active["diagnosis_permit"]
    permit = workflow.store.get(rid, permit_key)
    if change == "source":
        (work / "user-change.txt").write_text("preserve user changes")
    elif change == "plan":
        contract.tasks[2].description = "Changed accepted task contract"
        workflow.store.checkpoint(rid, "plan", contract.model_dump())
    elif change == "failure":
        active["failure"]["evidence"]["candidate_error"] = "New failure observation"
        workflow.store.checkpoint(rid, "recovery/active", active)
    elif change == "scope":
        permit["task_scopes"]["T3"].append("outside.py")
    elif change == "cycle":
        active["attempt"] += 1
        workflow.store.checkpoint(rid, "recovery/active", active)
    elif change == "binding":
        workflow.store.invalidate(rid, [permit_key.replace("/permits/", "/authorization-inputs/")])
    else:
        permit["outcome"] = {"status": "invalid"}
    if change in {"scope", "invalid_result"}:
        # Simulate disk corruption, bypassing the normal immutable receipt writer.
        import json
        with workflow.store.connect() as db:
            db.execute("UPDATE checkpoints SET data=? WHERE run_id=? AND key=?", (json.dumps(permit), rid, permit_key))
    async def no_call(*args, **kwargs):
        pytest.fail("Changed inputs require preserving the existing result, not another provider call")
    workflow.agent.invoke = no_call
    with pytest.raises(AA2Error):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active")
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1
    if change == "source":
        assert (work / "user-change.txt").read_text() == "preserve user changes"


async def test_unpublished_delivery_reuses_completed_diagnosis_after_call_identity_change(tmp_path, monkeypatch):
    workflow, rid = setup(tmp_path, InconclusiveAgent())
    (tmp_path / "value.py").write_text("value = 1\n")
    original = workflow.recovery_policy.authorize
    async def interrupt(*args, **kwargs):
        if kwargs["operation"].startswith("delivery-dispatch/"):
            raise PostCommitError("Interrupted after durable diagnosis, before action selection")
        return await original(*args, **kwargs)
    monkeypatch.setattr(workflow.recovery_policy, "authorize", interrupt)
    feedback = {"review": {"approved": False, "summary": "Need startup evidence", "findings": []}}
    with pytest.raises(PostCommitError):
        await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ["value.py"])
    monkeypatch.setattr(workflow.recovery_policy, "authorize", original)
    cfg = workflow.config.load().model_dump(mode="json")
    cfg["config_revision"] += 1
    write_json(workflow.config.path, cfg)
    assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ["value.py"]) == "recheck"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/delivery")["round"] == 1
    assert workflow.agent.calls == [("recovery_diagnosis", True), ("decision", True)]
