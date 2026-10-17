"""Realistic citations and durable recovery from the legacy root-only validator."""
import asyncio

import pytest
from test_decisions import setup
from test_recovery_contracts import failure
from test_recovery_policy import authorize, ready

from auto_agents_2.decisions import (
    LEGACY_FALLBACK_REASON,
    DecisionRequest,
    DecisionResult,
    DecisionService,
    DecisionValidationError,
)
from auto_agents_2.errors import AA2Error, InvalidStructuredResponse
from auto_agents_2.util import digest


def request():
    return DecisionRequest(kind="recovery_action", owner="T3", allowed_choices=["approve", "ask_user"],
        state_identity="same-controller-state", scope={"candidates": {"recheck": {"task_ids": ["T3"]}}},
        evidence={"failure": {"evidence_refs": ["develop/task/T3", "frontend/playwright.config.ts"],
            "evidence": {"commands": [{"argv": ["npm", "run", "test:e2e"]}], "checks": [
                {"process_exit_code": 0}, {"process_exit_code": 1, "report_status": "invalid",
                    "logs": [{"hash": "a" * 64}], "output": "fabricated-reference"}]},
            "proposal": {"hash": "b" * 64, "evidence_refs": ["invented"]}},
            "history": {"recent": [{"outcome": {"diagnosis": {
                "action": "recheck", "evidence_refs": ["unverified-citation"]}}}]},
            "consumption": {"implementation": {"round": 1}, "tokens": {"total_tokens": 100}}})


def approval(refs=None, **changes):
    return DecisionResult(**{"action": "approve", "selected_action": "recheck",
        "reason": "Startup failed before any business assertions; gather a fresh report.",
        "expected_result": "Record actual startup status, exit code and test report.",
        "evidence_refs": refs if refs is not None else ["failure.evidence.checks[1]"], **changes})


@pytest.mark.parametrize("refs", [
    ["failure"],
    ["failure.evidence.commands", "failure.evidence.checks[0]", "failure.evidence.checks[1]",
     "history.recent[0].outcome.diagnosis"],
    ["failure.evidence.commands", "failure.evidence.checks[1]", "a" * 64],
    ["develop/task/T3", "frontend/playwright.config.ts"],
])
def test_existing_nested_evidence_artifacts_and_controller_aliases_are_valid(refs):
    DecisionService.validate(request(), approval(refs))


@pytest.mark.parametrize("ref", [
    "failure.evidence.checks[2]", "failure.evidence.checks[-1]", "failure.evidence.checks[1].unknown",
    "failure.evidence.checks[1].logs[0].hash.extra", "history.recent[1].outcome.diagnosis",
    "fabricated-reference", "unverified-citation", "invented", "b" * 64, "c" * 64,
    "failure.proposal.hash", "scope.candidates.recheck", "../private",
])
def test_unknown_citations_and_proposal_content_do_not_become_evidence(ref):
    with pytest.raises(DecisionValidationError) as error:
        DecisionService.validate(request(), approval([ref]))
    assert error.value.feedback["code"] == "unknown_evidence"
    assert error.value.feedback["unresolved_indexes"] == [0]


@pytest.mark.asyncio
async def test_specific_feedback_allows_one_correction_to_use_existing_evidence(tmp_path):
    workflow, rid = setup(tmp_path)
    calls = []
    async def respond(stage, context, *args, **kwargs):
        assert stage == "decision"
        calls.append(context)
        if len(calls) == 1:
            return approval(["failure.evidence.checks[999]"])
        feedback = context["validation_feedback"]
        assert feedback["code"] == "unknown_evidence" and feedback["field"] == "evidence_refs"
        assert feedback["unresolved_indexes"] == [0] and "failure" in feedback["allowed_roots"]
        assert "[index]" in context["evidence_reference_contract"]["syntax"]
        return approval(["failure.evidence.checks[1]", "a" * 64])
    workflow.agent.invoke = respond
    key, result = await workflow.decisions.propose(rid, request(), tmp_path)
    assert result.action == "approve" and len(calls) == 2
    assert workflow.store.get(rid, key)["rejections"][0]["feedback"]["code"] == "unknown_evidence"
    assert (await workflow.decisions.propose(rid, request(), tmp_path))[1] == result
    assert len(calls) == 2 and not workflow.store.pending(rid)


@pytest.mark.parametrize("changes,code,field", [
    ({"action": "answer"}, "action_not_allowed", "action"),
    ({"selected_action": "invented"}, "unknown_candidate", "selected_action"),
    ({"expected_result": " "}, "expected_result_required", "expected_result"),
    ({"evidence_refs": []}, "evidence_required", "evidence_refs"),
])
def test_feedback_identifies_the_field_without_echoing_provider_values(changes, code, field):
    with pytest.raises(DecisionValidationError) as error:
        DecisionService.validate(request(), approval(**changes))
    assert error.value.feedback["code"] == code and error.value.feedback["field"] == field


def test_transport_and_schema_feedback_exclude_provider_text():
    text = "<PRIVATE_PROVIDER_OUTPUT_PLACEHOLDER>"
    feedback = DecisionService.validation_feedback(InvalidStructuredResponse(text))
    assert text not in str(feedback)
    try:
        DecisionResult.model_validate({"action": text, "reason": text, text: text})
    except ValueError as exc:
        feedback = DecisionService.validation_feedback(exc)
    assert feedback["code"] == "invalid_schema" and text not in str(feedback)


def legacy_request(workflow, rid, original, latest, **changes):
    key = "decisions/requests/" + digest(original.model_dump())
    saved = {"request": original.model_dump(), "attempts": 2, "pending": False,
        "error": "ValueError: invalid action, answer or evidence reference",
        "result": DecisionResult(action="ask_user", reason=LEGACY_FALLBACK_REASON).model_dump(), **changes}
    workflow.store.checkpoints(rid, {key: saved,
        f"invocation/{key}/1": {"result": approval().model_dump()},
        f"invocation/{key}/2": {"result": latest.model_dump()}})
    return key


@pytest.mark.asyncio
async def test_legacy_rejection_reuses_latest_response_despite_its_own_token_cost(tmp_path):
    workflow, rid = setup(tmp_path)
    original = request()
    key = legacy_request(workflow, rid, original, approval(["failure.evidence.checks[1]", "a" * 64]))
    current = request()
    current.evidence["consumption"]["tokens"]["total_tokens"] = 900
    result_key, result = await workflow.decisions.propose(rid, current, tmp_path)
    assert result_key == key and result.action == "approve"
    saved = workflow.store.get(rid, key)
    assert saved["attempts"] == 2 and saved["revalidation"]["valid"]
    assert saved["request"] == original.model_dump()
    assert saved["revalidation"]["previous_result"]["reason"] == LEGACY_FALLBACK_REASON
    assert (await workflow.decisions.propose(rid, current, tmp_path))[1] == result
    assert not workflow.agent.calls


@pytest.mark.asyncio
async def test_pending_legacy_response_keeps_its_original_invocation_context(tmp_path):
    workflow, rid = setup(tmp_path)
    original = request()
    key = "decisions/requests/" + digest(original.model_dump())
    context = {"decision_request": original.model_dump(), "validation_feedback": None}
    await workflow._invoke("decision", context, DecisionResult, tmp_path, rid, key + "/1", fresh=True)
    workflow.store.checkpoint(rid, key, {"request": original.model_dump(), "attempts": 1, "pending": True})
    current = request()
    current.evidence["consumption"]["tokens"]["total_tokens"] += 100
    result_key, result = await workflow.decisions.propose(rid, current, tmp_path)
    assert result_key == key and result.action == "approve"
    assert len(workflow.agent.calls) == 1
    assert workflow.store.get(rid, key)["attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("latest,changes", [
    (approval(["unknown"]), {}),
    (approval(selected_action="invented"), {}),
    (approval(expected_result=""), {}),
    (DecisionResult(action="ask_user", reason="Need a product choice"), {}),
    (approval(), {"failed": True}),
    (approval(), {"result": DecisionResult(action="ask_user", reason="Explicit model decision").model_dump()}),
])
async def test_revalidation_cannot_pick_an_earlier_approval_or_bypass_a_real_boundary(tmp_path, latest, changes):
    workflow, rid = setup(tmp_path)
    legacy_request(workflow, rid, request(), latest, **changes)
    _, result = await workflow.decisions.propose(rid, request(), tmp_path)
    assert result.action == "ask_user" and not workflow.agent.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["state", "evidence", "scope", "budget"])
async def test_changed_decision_basis_never_reuses_legacy_approval(tmp_path, change):
    workflow, rid = setup(tmp_path)
    old = legacy_request(workflow, rid, request(), approval())
    current = request()
    if change == "state":
        current.state_identity = "new-source-or-guards"
    elif change == "evidence":
        current.evidence["failure"]["evidence"]["checks"][1]["report_status"] = "valid"
    elif change == "scope":
        current.scope["candidates"] = {"diagnose": {"task_ids": ["T3"]}}
    else:
        current.evidence["consumption"]["implementation"]["round"] = 2
    new, _ = await workflow.decisions.propose(rid, current, tmp_path)
    assert new != old and len(workflow.agent.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, "y", "n", "source_drift"])
async def test_legacy_pending_recovery_resumes_without_calls_and_preserves_human_answers(tmp_path, monkeypatch, answer):
    record = failure(kind="evidence", evidence={"checks": [{"exit_code": 1,
        "process_exit_code": 1, "failure_reasons": ["test_report_invalid"]}]})
    workflow, rid, work, contract, record = ready(tmp_path, record=record)
    calls = []
    async def respond(stage, context, *args, **kwargs):
        calls.append(stage)
        return approval(["failure.evidence.checks[0]"])
    def old_validate(request, result):
        if not set(result.evidence_refs) <= request.evidence.keys():
            raise ValueError("Unknown evidence")
    async def leave_open(question):
        raise asyncio.CancelledError()
    workflow.agent.invoke = respond
    workflow.question_handler = leave_open
    monkeypatch.setattr(workflow.decisions, "validate", old_validate)
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, contract, record, force_decision=True)
    question = workflow.store.pending(rid)[0]
    states = workflow.store.delivery_evidence(rid)
    key, saved = next((k, v) for k, v in states.items() if k.startswith("decisions/requests/"))
    legacy_request(workflow, rid, DecisionRequest.model_validate(saved["request"]), approval(["failure.evidence.checks[0]"]))
    monkeypatch.setattr(workflow.decisions, "validate", DecisionService.validate)
    workflow.store.add_usage(rid, "fixture", "decision", "token-charge", {"total_tokens": 123, "complete": True})
    async def forbidden(*args, **kwargs):
        pytest.fail("An unchanged legacy recovery must not call a model or ask again")
    workflow.agent.invoke = workflow.question_handler = forbidden
    if answer in {"y", "n"}:
        workflow.store.answer(question["id"], answer)
    if answer == "source_drift":
        (work / "user.txt").write_text("Preserve user changes")
        with pytest.raises(AA2Error, match="original inputs"):
            await authorize(workflow, rid, work, contract, record, force_decision=True)
        assert (work / "user.txt").read_text() == "Preserve user changes"
    elif answer == "n":
        with pytest.raises(asyncio.CancelledError):
            await authorize(workflow, rid, work, contract, record, force_decision=True)
    else:
        action, permit = await authorize(workflow, rid, work, contract, record, force_decision=True)
        assert action.action == "recheck"
        assert workflow.store.get(rid, permit)["actor"] == ("human" if answer else "agent")
        assert not workflow.store.pending(rid)
        assert workflow.store.question(question["id"])["answer"] == answer
        if answer is None:
            assert workflow.store.question(question["id"])["superseded"]
            assert workflow.store.get(rid, key)["revalidation"]["valid"]
    assert calls == ["decision", "decision"]
