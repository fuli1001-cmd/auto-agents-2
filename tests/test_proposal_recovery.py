import asyncio

import pytest
from test_decisions import ApprovingAgent, setup
from test_runtime import configured

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import InvalidStructuredResponse, PostCommitError, ProviderError
from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import (
    DocumentResult,
    Finding,
    ImplementationResult,
    IntakeResult,
    PlanResult,
    ReviewResult,
)
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json


class MalformedOnce(ApprovingAgent):
    def __init__(self):
        super().__init__()
        self.responses = []

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if stage != "decision":
            self.responses.append(operation)
            if len(self.responses) == 1:
                raise InvalidStructuredResponse("fixture schema failure")
            assert context["validation_feedback"]["code"] == "invalid_structured_response"
            if stage == "prototype":
                return DocumentResult(summary="prototype", document="Proposed prototype; approval still required")
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


@pytest.mark.parametrize("stage,schema", [
    ("clarify", IntakeResult), ("coverage_review", ReviewResult), ("provider_research", DocumentResult),
    ("prototype", DocumentResult), ("design", DocumentResult),
    ("plan", PlanResult), ("implement", ImplementationResult), ("review", ReviewResult),
    ("verify", ReviewResult), ("readme", DocumentResult)])
async def test_invalid_response_enters_action_decision_without_authentication_question(tmp_path, stage, schema):
    agent = MalformedOnce()
    workflow, rid = setup(tmp_path, agent)
    workflow.store.checkpoint(rid, "rounds/T1", {"round": 8})
    result = await workflow._invoke(stage, {}, schema, tmp_path, rid, "fixture", fresh=True)
    assert isinstance(result, schema) and len(agent.responses) == 2
    assert [s for s, _ in agent.calls].count("decision") == 1
    assert not workflow.store.pending(rid)
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert workflow.store.get(rid, "develop/done") is None
    request = workflow.store.decision_requests(rid)[0][1]["request"]
    assert request["evidence"]["failure"]["component"] == "provider_response"
    assert "no checks or file writes" in request["evidence"]["failure"]["evidence"]["response_contract"]["action_effect"]
    again = await workflow._invoke(stage, {}, schema, tmp_path, rid, "fixture", fresh=True)
    assert again == result and len(agent.responses) == 2


async def test_pause_and_resume_do_not_repeat_an_invalid_response(tmp_path):
    class Agent(MalformedOnce):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "decision":
                self.calls.append((stage, True))
                return DecisionResult(action="ask_user", reason="Choose whether to retry the supplied response contract")
            return await super().invoke(stage, *args, **kwargs)
    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    async def pause(question):
        assert "provider/" not in question["key"]
        raise asyncio.CancelledError
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow._invoke("design", {}, DocumentResult, tmp_path, rid, "fixture")
    assert len(agent.responses) == 1
    workflow.store.answer(workflow.store.pending(rid)[0]["id"], "y")
    result = await workflow._invoke("design", {}, DocumentResult, tmp_path, rid, "fixture")
    assert result.summary == "design" and len(agent.responses) == 2
    assert [s for s, _ in agent.calls].count("decision") == 1


async def test_format_recovery_preserves_review_scope_and_logical_result_checkpoint(tmp_path):
    agent = MalformedOnce()
    workflow, rid = setup(tmp_path, agent)
    context = {"review_kind": "plan"}
    ledger = IssueLedger(workflow.store, workflow.artifacts)
    ledger.record(rid, "plan-review", ReviewResult(approved=False, summary="Missing verification", findings=[Finding(
        severity="blocking", location="plan", description="Missing check", evidence="No test command",
        resolution="Declare the existing acceptance checks")]), context)
    result = await workflow._invoke("review", context, ReviewResult, tmp_path, rid, "plan-review", fresh=True)
    assert result.approved and not ledger.unresolved(rid)
    saved = workflow.store.get(rid, "invocation/plan-review")
    assert saved["result"] == result.model_dump() and saved["recovered_from"].startswith("plan-review/response-retry/")
    events = [e["data"] for e in workflow.store.events(rid) if e["kind"] == "artifact.created"]
    assert events[-1]["operation"] == "plan-review" and events[-1]["attempt_operation"] == saved["recovered_from"]


async def test_saved_response_is_reused_after_permit_completion_interrupts(tmp_path, monkeypatch):
    agent = MalformedOnce()
    workflow, rid = setup(tmp_path, agent)
    original = workflow.recovery_policy.complete
    def interrupt(rid, key, outcome):
        original(rid, key, outcome)
        if outcome.get("status") == "response_validated":
            raise PostCommitError("fixture committed action result")
    monkeypatch.setattr(workflow.recovery_policy, "complete", interrupt)
    with pytest.raises(PostCommitError):
        await workflow._invoke("design", {}, DocumentResult, tmp_path, rid, "fixture")
    monkeypatch.setattr(workflow.recovery_policy, "complete", original)
    await workflow._invoke("design", {}, DocumentResult, tmp_path, rid, "fixture")
    assert len(agent.responses) == 2
    assert [s for s, _ in agent.calls].count("decision") == 1


async def test_explicit_stage_reentry_cannot_reuse_a_previously_rejected_recovered_proposal(tmp_path):
    class Agent(ApprovingAgent):
        responses = 0
        async def invoke(self, stage, *args, **kwargs):
            if stage == "design":
                self.responses += 1
                if self.responses == 1:
                    raise InvalidStructuredResponse("fixture invalid schema")
                return DocumentResult(summary=f"candidate {self.responses}", document="architecture")
            return await super().invoke(stage, *args, **kwargs)
    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    first = await workflow._invoke("design", {}, DocumentResult, tmp_path, rid, "design")
    assert first.summary == "candidate 2"
    workflow.store.prepare_replan(rid, "design", "The valid response failed the independent architecture gate")
    workflow.store.complete_replan(rid, ["design", "plan", "develop", "readme", "verify"], [])
    second = await workflow._invoke("design", {}, DocumentResult, tmp_path, rid, "design")
    assert second.summary == "candidate 3" and agent.responses == 3


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("retries", [0, 3])
async def test_router_preserves_format_failure_type_including_exhausted_resume(tmp_path, mixed, retries):
    config, store = configured(tmp_path)
    body = config.load().model_dump(mode="json")
    body["execution"]["infra_retries"] = retries
    write_json(config.path, body)
    rid = store.admit("fixture", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    calls = []
    async def fail(effective, *args, **kwargs):
        calls.append(effective["provider"])
        if mixed and effective["provider"] == "codex":
            raise ProviderError("fixture provider unavailable")
        raise InvalidStructuredResponse("fixture schema failure")
    router._call = fail
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            await router.invoke("design", {}, DocumentResult, tmp_path, rid, "fixture")
        assert isinstance(caught.value, InvalidStructuredResponse) is not mixed
    assert len(calls) == len(config.load().providers) + int(mixed and retries > 0)
