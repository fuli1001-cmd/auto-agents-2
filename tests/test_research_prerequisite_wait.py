"""Finite research wait ownership and actual normal-engine restart checks."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.engine import choice_spec
from auto_agents_2.errors import (
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    preserve_http_connection_failure,
)
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import DocumentResult, SourceEvidence
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import (
    CONSUMED,
    HARD,
    PREFIX,
    WAIT,
    ResearchAcquisitionRecovery,
    failure_observation,
)
from auto_agents_2.store import Store
from auto_agents_2.util import digest

URL = "https://docs.example/ref"


@pytest.fixture(autouse=True)
def clean_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def source_case(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    document = {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]}
    store.checkpoint(rid, "candidate/provider_research", {"document": document})
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    request = httpx.Request("GET", URL)
    error = DocumentAcquisitionUnavailable(URL, httpx.HTTPStatusError(
        "EXAMPLE", request=request, response=httpx.Response(403, request=request)))
    error.acquisition_id = "fixture-source-attempt"
    error.acquisition_dispatch = recovery.dispatch_binding(rid, document)
    error.acquisition_observations = [{"url": URL, "status": "failed", "failure": failure_observation(error)}]
    recovery.observe(rid, document, error)
    wait = store.get(rid, WAIT)
    key = "prerequisite/" + wait["identity"] + "/" + str(wait["attempt"])
    interaction = choice_spec(["已处理，重新核对", "暂停，稍后续跑"], ["retry", "pause"], yes_no=True)
    interaction["pause_on"] = "n"
    question = store.request(rid, key, "Fixture exact prerequisite", wait["identity"], interaction)
    return store, rid, recovery, document, wait, question


def test_exact_source_answer_consumption_clears_only_its_wait_and_counter(tmp_path):
    store, rid, recovery, _, wait, question = source_case(tmp_path)
    store.answer(question["id"], "y", wait["identity"])
    recovery.consume_wait(rid, wait, question["id"])
    assert store.get(rid, WAIT) is None
    assert store.get(rid, "prerequisite/" + wait["identity"]) == wait["attempt"] + 1
    assert store.get(rid, CONSUMED + wait["id"])["question_id"] == question["id"]
    assert recovery.prepare(rid) is None and store.run(rid)["data"]["replans"] == 0


@pytest.mark.parametrize("change", ["candidate", "generation", "capture", "clarify", "wait", "pause",
                                   "question", "superseded", "hard", "counter"])
def test_wait_answer_cas_rejects_state_or_question_drift(tmp_path, change):
    store, rid, recovery, document, wait, question = source_case(tmp_path)
    store.answer(question["id"], "n" if change == "pause" else "y", wait["identity"])
    selected = question["id"]
    if change == "candidate":
        store.checkpoint(rid, "candidate/provider_research", {"document": {**document, "summary": "Changed"}})
    elif change == "generation":
        store.update(rid, replans=1)
    elif change in {"capture", "clarify"}:
        store.checkpoint(rid, change, {"changed": True})
    elif change == "wait":
        store.checkpoint(rid, WAIT, {**wait, "id": "different-wait"})
    elif change == "question":
        other = store.request(rid, "unrelated/0", "Other", wait["identity"],
                              choice_spec(["retry", "pause"], ["retry", "pause"], yes_no=True))
        store.answer(other["id"], "y", wait["identity"])
        selected = other["id"]
    elif change == "superseded":
        store.supersede_questions(rid, question["key"], include_answered=True)
    elif change == "hard":
        store.checkpoint(rid, HARD, {"kind": "recording_error"})
    elif change == "counter":
        store.checkpoint(rid, "prerequisite/" + wait["identity"], 9)
    with pytest.raises(StateConflict):
        recovery.consume_wait(rid, wait, selected)
    assert store.get(rid, WAIT) is not None and store.get(rid, CONSUMED + wait["id"]) is None
    assert store.get(rid, "prerequisite/" + wait["identity"], 0) == (9 if change == "counter" else 0)


@pytest.mark.parametrize("kind", ["cancel", "transient", "other_blocked"])
def test_pure_control_or_transient_or_other_source_block_is_not_source_wait(tmp_path, kind):
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    document = {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]}
    store.checkpoint(rid, "candidate/provider_research", {"document": document})
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    error = asyncio.CancelledError()
    error.acquisition_id = "fixture-control"
    error.acquisition_dispatch = recovery.dispatch_binding(rid, document)
    if kind == "cancel":
        row = {"url": URL, "status": "cancelled"}
    elif kind == "transient":
        row = {"url": URL, "status": "failed",
               "failure": failure_observation(DocumentAcquisitionTimeout("Fixture", 30, source_url=URL))}
    else:
        row = {"url": URL, "status": "not_executed", "capture_started": False, "unit": "capture",
               "boundary": {"blocked_by": {"code": "DOCUMENT_ACQUISITION_UNAVAILABLE",
                                          "diagnostic": {"http_status": 403}}}}
    error.acquisition_observations = [row]
    recovery.observe(rid, document, error, control=True)
    assert store.get(rid, WAIT) is None and store.get(rid, HARD) is None and recovery.prepare(rid) is None


async def paused_engine(tmp_path, monkeypatch, *, normal=False, unknown=False):
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    class Agent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.research_reviews = 0
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                return DocumentResult(summary="Fixture", document="Preserve fixture fact",
                                      sources=[SourceEvidence(url=URL, claim="Fixture fact")])
            if stage == "review" and context.get("review_kind") == "research":
                self.research_reviews += 1
            if stage == "design":
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)

    state = {"mode": "bad", "block": not normal, "closing": asyncio.Event(), "requests": []}

    def respond(request):
        state["requests"].append(str(request.url))
        if state["mode"] == "good":
            return httpx.Response(200, text="Fixture fact")
        error = httpx.ConnectError("EXAMPLE_UNKNOWN") if unknown else httpx.ProxyError("EXAMPLE_PROXY_AUTH")
        preserve_http_connection_failure(error, "public_connect", scope="shared_proxy",
                                         proxy_status=None if unknown else 407)
        raise error

    class Transport(httpx.MockTransport):
        async def aclose(self):
            if state["block"]:
                state["closing"].set()
                await asyncio.Future()
            await super().aclose()

    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: Transport(respond))
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve assertions.")
    agent = Agent()
    workflow = engine(project, agent)
    questions = []

    async def pause(question):
        questions.append(question)
        raise asyncio.CancelledError()

    workflow.question_handler = pause
    task = asyncio.create_task(workflow.start(str(request)))
    try:
        if not normal:
            await asyncio.wait_for(state["closing"].wait(), 5)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    state["block"] = False
    rid = workflow.store.current()["id"]
    return workflow, agent, rid, state, questions


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("normal", [False, True])
async def test_source_wait_survives_normal_or_cancel_restart_before_verify(tmp_path, monkeypatch, normal, unknown):
    from test_workflow import engine
    workflow, agent, rid, _, original_questions = await paused_engine(tmp_path, monkeypatch, normal=normal, unknown=unknown)
    wait = workflow.store.get(rid, WAIT)
    assert wait["kind"] == "source_wait" and workflow.store.get(rid, HARD) is None
    calls, questions = [], []

    async def forbidden(self, sources):
        calls.append(True)
        raise RuntimeError("Fixture forbidden re-dispatch")

    async def pause(question):
        questions.append(question)
        raise asyncio.CancelledError()

    monkeypatch.setattr(ResearchService, "verify", forbidden)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await restarted.start()
    assert calls == [] and questions[0]["artifact_hash"] == wait["identity"]
    if original_questions:
        assert questions[0]["id"] == original_questions[0]["id"]
    assert workflow.store.get(rid, WAIT) == wait


async def test_real_matching_answer_rechecks_and_still_requires_independent_review(tmp_path, monkeypatch):
    from test_workflow import engine
    workflow, agent, rid, state, _ = await paused_engine(tmp_path, monkeypatch)
    wait = workflow.store.get(rid, WAIT)
    state["mode"] = "good"
    calls, questions = [], []
    original = ResearchService.verify

    async def verify(self, sources):
        calls.append(True)
        return await original(self, sources)

    async def answer(question):
        assert calls == []
        questions.append(question)
        return "y"

    monkeypatch.setattr(ResearchService, "verify", verify)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = answer
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(restarted.start(), 8)
    assert calls == [True] and len(questions) == 1 and agent.research_reviews == 1
    assert workflow.store.get(rid, WAIT) is None
    assert workflow.store.get(rid, "prerequisite/" + wait["identity"]) == 1
    assert workflow.store.get(rid, CONSUMED + wait["id"])["question_id"] == questions[0]["id"]
    assert workflow.store.get(rid, "provider_research")["verified_sources"]
    assert workflow.store.run(rid)["data"]["replans"] == 0


async def test_pause_and_old_positive_answer_do_not_release_later_refusal(tmp_path, monkeypatch):
    from test_workflow import engine
    workflow, agent, rid, _, _ = await paused_engine(tmp_path, monkeypatch)
    first = workflow.store.get(rid, WAIT)
    calls, questions = [], []
    original = ResearchService.verify

    async def verify(self, sources):
        calls.append(True)
        return await original(self, sources)

    async def answer(question):
        questions.append(question)
        return "y" if len(questions) == 1 else "n"

    monkeypatch.setattr(ResearchService, "verify", verify)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = answer
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(restarted.start(), 8)
    second = workflow.store.get(rid, WAIT)
    assert calls == [True] and second["id"] != first["id"] and second["identity"] != first["identity"]
    assert workflow.store.get(rid, "prerequisite/" + first["identity"]) == 1
    assert workflow.store.get(rid, "prerequisite/" + second["identity"], 0) == 0
    assert agent.research_reviews == 0
    calls.clear()

    async def pause(question):
        assert question["artifact_hash"] == second["identity"]
        raise asyncio.CancelledError()

    again = engine(workflow.project, agent)
    again.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await again.start()
    assert calls == [] and workflow.store.get(rid, WAIT) == second


async def test_recording_hardhold_does_not_clear_after_ordinary_network_yes(tmp_path, monkeypatch):
    from test_workflow import engine
    original_record = ResearchAcquisitionRecovery.observe

    def fail(self, rid, document, error, *, control=False):
        if control:
            raise OSError("EXAMPLE_RECORDING_SECRET")
        return original_record(self, rid, document, error, control=control)

    monkeypatch.setattr(ResearchAcquisitionRecovery, "observe", fail)
    workflow, agent, rid, _, _ = await paused_engine(tmp_path, monkeypatch)
    hard = workflow.store.get(rid, HARD)
    assert hard["kind"] == "recording_error" and hard["binding_status"] == "original_frozen"
    calls, questions = [], []

    async def forbidden(self, sources):
        calls.append(True)
        raise RuntimeError("Fixture forbidden dispatch")

    async def answer(question):
        questions.append(question)
        if len(questions) == 1:
            return "y"
        raise asyncio.CancelledError()

    monkeypatch.setattr(ResearchService, "verify", forbidden)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = answer
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(restarted.start(), 8)
    assert len(questions) == 2 and calls == []
    assert workflow.store.get(rid, HARD) == hard and workflow.store.get(rid, WAIT) is None
    assert not workflow.store.has_event(rid, "research.prerequisite_consumed")


def test_recording_hardhold_keeps_original_not_post_failure_binding(tmp_path):
    store, rid, recovery, document, wait, _ = source_case(tmp_path)
    original_stamp = wait["dispatch"]
    store.checkpoint(rid, "clarify", {"changed_after_dispatch": True})
    original = asyncio.CancelledError()
    original.acquisition_id = "fixture-frozen"
    original.acquisition_dispatch = original_stamp
    original.acquisition_observations = [{"url": URL, "status": "cancelled",
        "observed_failures": wait["observations"][0]["failures"]}]
    recovery.recording_failed(rid, document, original, StateConflict("Fixture original binding mismatch"))
    hard = store.get(rid, HARD)
    assert hard["dispatch"] == original_stamp
    assert hard["dispatch"]["binding"]["clarify"] != digest(store.get(rid, "clarify"))
    assert hard["error_type"] == "StateConflict"
    with pytest.raises(StateConflict):
        recovery.prepare(rid)


def test_legacy_recording_audit_without_original_binding_is_not_auto_bridged(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    document = {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]}
    store.checkpoint(rid, "candidate/provider_research", {"document": document})
    store.event(rid, "research.acquisition_recording_failed", {"candidate_hash": digest(document),
                "error_type": "OSError", "observations": []})
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    with pytest.raises(StateConflict, match="Historical research recording audit"):
        recovery.prepare(rid)


async def test_legacy_refusal_without_new_wait_is_hard_stopped_not_retagged(tmp_path, monkeypatch):
    from test_workflow import engine
    workflow, agent, rid, _, questions = await paused_engine(tmp_path, monkeypatch, normal=True)
    wait = workflow.store.get(rid, WAIT)
    original_question = questions[0]
    candidate = workflow.store.get(rid, "candidate/provider_research")["document"]
    key = PREFIX + digest([rid, 0, wait["dispatch"]["binding"], candidate])
    record = workflow.store.get(rid, key)
    # This private test fixture emulates the previously accepted ledger format;
    # actual observation, original candidate and question remain unchanged.
    for attempt in record["attempts"]:
        attempt.pop("dispatch", None)
    workflow.store.checkpoints(rid, {key: record, WAIT: None})
    calls, new_questions = [], []

    async def forbidden(self, sources):
        calls.append(True)
        raise RuntimeError("Fixture forbidden dispatch")

    async def pause(question):
        new_questions.append(question)
        raise asyncio.CancelledError()

    monkeypatch.setattr(ResearchService, "verify", forbidden)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await restarted.start()
    assert calls == [] and new_questions
    saved = workflow.store.question(original_question["id"])
    assert saved["artifact_hash"] == original_question["artifact_hash"] and not saved["superseded"]
    assert saved["answer"] is None and workflow.store.get(rid, WAIT) is None


@pytest.mark.parametrize("window", ["before_event", "after_event"])
def test_wait_consumption_event_failure_rolls_back_counter_receipt_and_wait(tmp_path, monkeypatch, window):
    store, rid, recovery, _, wait, question = source_case(tmp_path)
    store.answer(question["id"], "y", wait["identity"])
    original = store._event
    reached = []

    def event(db, run_id, kind, data):
        if kind == "research.prerequisite_consumed":
            # The confirmation writes occurred, but none may escape a failed
            # required event, even when that event was already inserted.
            reached.append(True)
            assert db.execute('SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?',
                              (rid, CONSUMED + wait["id"])).fetchone()
            if window == "after_event":
                original(db, run_id, kind, data)
            raise OSError("EXAMPLE_CONSUMPTION_SECRET")
        return original(db, run_id, kind, data)

    monkeypatch.setattr(store, "_event", event)
    with pytest.raises(OSError):
        recovery.consume_wait(rid, wait, question["id"])
    assert reached == [True] and store.get(rid, WAIT) == wait
    assert store.get(rid, "prerequisite/" + wait["identity"], 0) == wait["attempt"]
    assert store.get(rid, CONSUMED + wait["id"]) is None
    assert not store.has_event(rid, "research.prerequisite_consumed")
    assert store.question(question["id"])["answer"] == "y"


@pytest.mark.parametrize("kind", ["source", "state"])
async def test_double_recording_failure_stops_normal_engine_without_network_retry_question(tmp_path, monkeypatch, kind):
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.faults import InternalWorkflowError

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                return DocumentResult(summary="Fixture", document="Preserve fixture fact",
                                      sources=[SourceEvidence(url=URL, claim="Fixture fact")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)

    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve assertions.")
    workflow = engine(project, Agent())
    original_event = workflow.store._event
    failures, requests, questions = [], [], []

    def event(db, rid, kind, data):
        if kind in {"research.acquisition_failed", "research.acquisition_recording_failed"}:
            failures.append(kind)
            raise OSError("EXAMPLE_RECORDING_SECRET")
        return original_event(db, rid, kind, data)

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(404)

    async def forbidden(question):
        questions.append(question)
        pytest.fail("An unrecorded failure cannot become a retry question")

    monkeypatch.setattr(workflow.store, "_event", event)
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    if kind == "state":
        async def invalid_state(self, url, history):
            requests.append(url)
            raise StateConflict("Fixture capture consistency lost", phase="provider_research")
        monkeypatch.setattr(ResearchService, "_capture", invalid_state)
    workflow.question_handler = forbidden
    before = asyncio.all_tasks()
    with pytest.raises(InternalWorkflowError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    fault = workflow.store.get(rid, "recovery/current-fault")
    details = fault["research_recording_failure"]
    assert fault["route"] == "internal_error" and details["audit_recorded"] is False
    assert details["error_type"] == details["audit_error_type"] == "OSError"
    assert failures == ["research.acquisition_failed", "research.acquisition_recording_failed"]
    assert len(requests) == 1 and questions == [] and asyncio.all_tasks() <= before
    assert workflow.store.get(rid, HARD) is None and workflow.store.get(rid, WAIT) is None
    # There is deliberately no assertion of cross-restart durability: both
    # protective transactions failed. No automatic recovery authority was saved.
