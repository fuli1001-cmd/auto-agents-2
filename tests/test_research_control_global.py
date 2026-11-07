"""Global failures stay hard boundaries when later cancellation becomes primary."""
import asyncio

import httpx
import pytest

from auto_agents_2.errors import AA2Error, DocumentAcquisitionUnavailable
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import DocumentResult, SourceEvidence
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import HARD, WAIT

GLOBAL_URL = "https://global.example/ref"
SIBLING_URL = "https://sibling.example/ref"


@pytest.mark.parametrize("kind", [StateConflict, RuntimeError, AA2Error])
@pytest.mark.parametrize("refusal", [False, True])
async def test_observed_global_failure_survives_later_cancel_and_network_yes(tmp_path, monkeypatch, refusal, kind):
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                return DocumentResult(summary="Fixture", document="Preserve fixture facts",
                    sources=[SourceEvidence(url=url, claim="Fixture") for url in (GLOBAL_URL, SIBLING_URL)])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)

    observed, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = kind("Fixture original controller inconsistency")

    async def capture(self, url, history):
        if url == GLOBAL_URL:
            await observed.wait()
            raise failure
        if refusal:
            request = httpx.Request("GET", url)
            history.record(DocumentAcquisitionUnavailable(url, httpx.HTTPStatusError(
                "EXAMPLE_REFUSAL", request=request, response=httpx.Response(403, request=request))))
        observed.set()
        try:
            async with asyncio.timeout(1):
                await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr(ResearchService, "_capture", capture)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve assertions.")
    agent = Agent()
    workflow = engine(project, agent)
    before = asyncio.all_tasks()
    task = asyncio.create_task(workflow.start(str(request)))
    try:
        # Real verify has observed the global failure and cancelled its sibling.
        # A later caller cancellation is delayed by joined until cleanup drains.
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, 5)
        report = cancelled.value.acquisition_observations
        assert report[0]["status"] == "failed"
        assert report[0]["failure"]["code"] == "UNCLASSIFIED_ACQUISITION_ERROR"
        assert report[1]["status"] == "cancelled"
        assert bool(report[1].get("observed_failures")) is refusal
        assert asyncio.all_tasks() <= before
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    rid = workflow.store.current()["id"]
    hard = workflow.store.get(rid, HARD)
    assert hard is not None, "An already observed global failure cannot become a source-only wait"
    assert report[0]["global_error_type"] == kind.__name__
    assert hard["error_type"] == kind.__name__ and hard["original_error_type"] == "CancelledError"
    assert hard["global_failures"] == [{"url": GLOBAL_URL, "error_type": kind.__name__}]
    assert hard["observations"] == report and workflow.store.get(rid, WAIT) is None
    calls, questions = [], []

    async def forbidden(self, sources):
        calls.append(True)
        pytest.fail("A network answer cannot release an observed global consistency failure")

    async def answer(question):
        questions.append(question)
        if len(questions) == 1:
            return "y"
        raise asyncio.CancelledError()

    monkeypatch.setattr(ResearchService, "verify", forbidden)
    restarted = engine(project, agent)
    restarted.question_handler = answer
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(restarted.start(), 8)
    assert len(questions) == 2 and calls == [] and workflow.store.get(rid, HARD) == hard
    assert workflow.store.get(rid, WAIT) is None and asyncio.all_tasks() <= before


@pytest.mark.parametrize("stage", ["capture", "request"])
async def test_blocked_sentinel_does_not_manufacture_observed_global_failure(tmp_path, monkeypatch, stage):
    from types import SimpleNamespace

    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    from auto_agents_2.research_batch import ResearchBatch, ResearchRequestNotExecuted, current_batch
    from auto_agents_2.store import Store

    request = httpx.Request("GET", SIBLING_URL)
    # This is another source's boundary context, not an actual failure of this
    # capture. Neither admission window may invent global or source provenance.
    other = DocumentAcquisitionUnavailable(SIBLING_URL, httpx.HTTPStatusError(
        "EXAMPLE_OTHER_SOURCE", request=request, response=httpx.Response(403, request=request)))

    def blocked(batch, operation):
        raise ResearchRequestNotExecuted(batch, operation, other)

    if stage == "capture":
        monkeypatch.setattr(ResearchBatch, "before_capture", lambda self, url: blocked(self, "capture_dispatch"))
    else:
        async def capture(self, url, history):
            blocked(current_batch.get(), "resolver_request")
        monkeypatch.setattr(ResearchService, "_capture", capture)

    with pytest.raises(ResearchRequestNotExecuted) as result:
        await ResearchService(None).verify([SourceEvidence(url=GLOBAL_URL, claim="Fixture")])
    row = result.value.acquisition_observations[0]
    assert row["status"] == "not_executed" and "global_error_type" not in row
    assert row["capture_started"] is (stage == "request")
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    document = {"sources": [SourceEvidence(url=GLOBAL_URL, claim="Fixture").model_dump()]}
    store.checkpoint(rid, "candidate/provider_research", {"document": document})
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    result.value.acquisition_dispatch = recovery.dispatch_binding(rid, document)
    recovery.observe(rid, document, result.value)
    assert store.get(rid, HARD) is None and store.get(rid, WAIT) is None
    assert recovery.global_failures(result.value.acquisition_observations) == []
