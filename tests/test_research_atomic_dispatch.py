"""Atomic recording and last-await CONNECT handoff regressions."""
import asyncio
import errno
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.engine import Replan
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import SourceEvidence
from auto_agents_2.network import PublicNetworkBackend
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import (
    CURRENT,
    PREFIX,
    ResearchAcquisitionRecovery,
    failure_observation,
)
from auto_agents_2.research_batch import ResearchBatch, ResearchRequestNotExecuted, current_batch
from auto_agents_2.research_dns import BootstrapResolver
from auto_agents_2.store import Store
from auto_agents_2.util import digest

URL = "https://docs.example/ref"


@pytest.fixture(autouse=True)
def clean_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def scenario(tmp_path):
    from auto_agents_2.errors import DocumentAcquisitionUnavailable
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    document = {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]}
    request = httpx.Request("GET", URL)
    error = DocumentAcquisitionUnavailable(URL, httpx.HTTPStatusError(
        "EXAMPLE_SOURCE_SECRET", request=request, response=httpx.Response(404, request=request)))
    error.acquisition_id = "fixture-attempt"
    error.acquisition_observations = [{"url": URL, "status": "failed", "failure": failure_observation(error)}]
    key = PREFIX + digest([rid, 0, recovery.binding(rid), document])
    return store, rid, recovery, document, error, key


@pytest.mark.parametrize("window", ["before_event_insert", "after_event_insert"])
def test_failed_required_event_rolls_back_new_pending_not_just_memory(tmp_path, monkeypatch, window):
    store, rid, recovery, document, error, key = scenario(tmp_path)
    original = store._event
    reached = []

    def event(db, run_id, kind, data):
        if kind == "research.acquisition_failed":
            # Both checkpoints were actually written in this same transaction.
            assert db.execute('SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?', (rid, key)).fetchone()
            assert db.execute('SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?', (rid, CURRENT)).fetchone()
            reached.append(True)
            if window == "after_event_insert":
                original(db, run_id, kind, data)
            raise OSError("EXAMPLE_EVENT_SECRET")
        return original(db, run_id, kind, data)

    monkeypatch.setattr(store, "_event", event)
    with pytest.raises(OSError) as failed:
        recovery.observe(rid, document, error)
    recovery.recording_failed(rid, document, error, failed.value)
    assert reached == [True] and error.acquisition_recording_failure["audit_recorded"]
    assert not failure_observation(error)["automatic"]
    assert store.get(rid, key) is None and store.get(rid, CURRENT) is None
    with pytest.raises(StateConflict, match="hard boundary"):
        recovery.prepare(rid)
    assert store.run(rid)["data"]["replans"] == 0
    events = store.events(rid)
    assert not any(row["kind"] == "research.acquisition_failed" for row in events)
    assert not any(row["kind"] == "checkpoint.saved" and row["data"]["key"] in {key, CURRENT}
                   for row in events)
    assert any(row["kind"] == "research.acquisition_recording_failed" for row in events)


def test_successful_required_event_and_pending_commit_together(tmp_path):
    store, rid, recovery, document, error, key = scenario(tmp_path)
    with pytest.raises(Replan):
        recovery.observe(rid, document, error)
    assert store.get(rid, key)["pending"]
    assert any(row["kind"] == "research.acquisition_failed" for row in store.events(rid))
    with pytest.raises(Replan):
        recovery.prepare(rid)


def test_existing_same_generation_pending_stops_before_any_new_collection(tmp_path):
    store, rid, recovery, document, error, key = scenario(tmp_path)
    with pytest.raises(Replan):
        recovery.observe(rid, document, error)
    before = store.get(rid, key)
    # Engine enters prepare before author/candidate/verify. It must consume this
    # original pending re-entry first, not append a conflicting new observation.
    with pytest.raises(Replan):
        recovery.prepare(rid)
    assert store.get(rid, key) == before and len(before["attempts"]) == 1


@pytest.mark.parametrize("stage", ["tcp", "tls"])
@pytest.mark.parametrize("close_error", [False, True])
async def test_proxy_connect_rechecks_route_after_last_await_and_closes_socket(monkeypatch, stage, close_error):
    scheme = "https" if stage == "tls" else "http"
    monkeypatch.setenv("HTTPS_PROXY", scheme + "://127.0.0.1:7890")
    context = ResearchBatch(None)
    proxy = context.proxy_settings.route(httpcore.URL(URL).origin)
    waiting, release = asyncio.Event(), asyncio.Event()
    writes, closed, tls = [], [], []

    class Stream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            writes.append(buffer)
        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append(True)
            if stage == "tls":
                waiting.set()
                await release.wait()
            return self
        async def aclose(self):
            closed.append(True)
            await super().aclose()
            if close_error:
                raise httpx.ReadTimeout("EXAMPLE_CLOSE_SECRET")

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            if stage == "tcp":
                waiting.set()
                await release.wait()
            return Stream([])

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    token = current_batch.set(context)
    before = asyncio.all_tasks()
    task = asyncio.create_task(PublicNetworkBackend(proxy, resolver=BootstrapResolver(
        "docs.example", ("93.184.216.34",))).connect_tcp("docs.example", 443, 1))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        context.freeze_connect(proxy, "sibling.example", 443, {
            "category": "network", "error_type": "ConnectError", "os_errno": errno.EACCES,
            "connection_scope": "shared_proxy"})
        release.set()
        with pytest.raises(ResearchRequestNotExecuted) as observed:
            await asyncio.wait_for(task, 1)
        assert writes == [] and closed == [True] and asyncio.all_tasks() <= before
        assert observed.value.evidence["blocked_by"]["diagnostic"]["hostname"] == "sibling.example"
        assert "http_status" not in observed.value.evidence
        assert bool(observed.value.evidence.get("cleanup_failure")) is close_error
        assert tls == ([True] if stage == "tls" else [])
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        current_batch.reset(token)


async def test_normal_source_403_does_not_fabricate_cleanup_failure(monkeypatch):
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport",
                        lambda: httpx.MockTransport(lambda request: httpx.Response(403)))
    from auto_agents_2.errors import DocumentAcquisitionUnavailable
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(None).verify([SourceEvidence(url=URL, claim="Fixture")])
    assert observed.value.evidence["http_status"] == 403 and "cleanup_failure" not in observed.value.evidence
    assert all("cleanup_failure" not in item["diagnostic"]
               for item in observed.value.acquisition_observations[0]["observed_failures"])


def test_optional_atomic_event_keeps_round_minimum_and_usage_semantics(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    usage = ("fixture-provider", "fixture-attempt", {"tokens": 1})
    store.checkpoints(rid, {"rounds/fixture": {"round": 3}}, usage=usage,
                      event=("fixture.required", {"value": 1}))
    store.checkpoints(rid, {"rounds/fixture": {"round": 1}}, usage=usage)
    assert store.get(rid, "rounds/fixture")["round"] == 3
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM usage WHERE run_id=?", (rid,)).fetchone()[0] == 1
    assert sum(row["kind"] == "fixture.required" for row in store.events(rid)) == 1


@pytest.mark.parametrize("kind", ["runtime", "state"])
async def test_proxy_cleanup_internal_fault_is_not_replaced_by_refusal(monkeypatch, kind):
    from auto_agents_2.faults import StateConflict
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    failure = RuntimeError("EXAMPLE_INTERNAL") if kind == "runtime" else StateConflict("Fixture cleanup state")

    class Stream(httpcore.AsyncMockStream):
        async def aclose(self):
            await super().aclose()
            raise failure

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            return Stream([b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"])

    async def resolve(host, port, **kwargs):
        import socket
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    before = asyncio.all_tasks()
    with pytest.raises(type(failure)) as observed:
        await ResearchService(None).verify([SourceEvidence(url=URL, claim="Fixture")])
    assert observed.value is failure and asyncio.all_tasks() <= before
    row = failure.acquisition_observations[0]
    assert row["failure"]["code"] == "UNCLASSIFIED_ACQUISITION_ERROR"
    assert any(item["diagnostic"]["category"] == "proxy" for item in row["observed_failures"])


async def test_engine_repeated_cancel_after_refusal_still_records_owned_facts(tmp_path, monkeypatch):
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.errors import DocumentAcquisitionUnavailable
    from auto_agents_2.models import DocumentResult

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                return DocumentResult(summary="Fixture", document="Preserve fact",
                                      sources=[SourceEvidence(url=URL, claim="Fixture")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)

    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def capture(self, url, history):
        request = httpx.Request("GET", url)
        history.record(DocumentAcquisitionUnavailable(url, httpx.HTTPStatusError(
            "EXAMPLE_SOURCE_SECRET", request=request, response=httpx.Response(403, request=request))))
        started.set()
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
    workflow = engine(project, Agent())
    before = asyncio.all_tasks()
    task = asyncio.create_task(workflow.start(str(request)))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as observed:
            await asyncio.wait_for(task, 5)
        assert observed.value.acquisition_id
        assert observed.value.acquisition_observations[0]["status"] == "cancelled"
        assert asyncio.all_tasks() <= before
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    rid = workflow.store.current()["id"]
    recovery = ResearchAcquisitionRecovery(workflow)
    document = workflow.store.get(rid, "candidate/provider_research")["document"]
    record = workflow.store.get(rid, PREFIX + digest([rid, 0, recovery.binding(rid), document]))
    row = record["attempts"][0]["observations"][0]
    assert any(item["diagnostic"].get("http_status") == 403 for item in row["observed_failures"])
    assert workflow.store.run(rid)["data"]["replans"] == 0 and "pending" not in record
    events = [row for row in workflow.store.events(rid) if row["kind"] == "research.acquisition_failed"]
    assert len(events) == 1 and events[0]["data"]["automatic"] is False


@pytest.mark.parametrize("recording", ["ledger", "audit"])
async def test_cancelled_refusal_blocks_normal_engine_restart_before_verify(tmp_path, monkeypatch, recording):
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.models import DocumentResult

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                return DocumentResult(summary="Fixture", document="Preserve fact",
                                      sources=[SourceEvidence(url=URL, claim="Fixture")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)

    closing = asyncio.Event()

    from auto_agents_2.errors import preserve_http_connection_failure

    def respond(request):
        error = httpx.ProxyError("EXAMPLE_SHARED_PROXY_AUTH")
        preserve_http_connection_failure(error, "public_connect", scope="shared_proxy", proxy_status=407)
        raise error

    class Transport(httpx.MockTransport):
        async def aclose(self):
            closing.set()
            await asyncio.Future()

    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: Transport(respond))
    original_record = ResearchAcquisitionRecovery.observe

    def record(self, rid, document, error, *, control=False):
        if control and recording == "audit":
            raise OSError("EXAMPLE_RECORDING_SECRET")
        return original_record(self, rid, document, error, control=control)

    monkeypatch.setattr(ResearchAcquisitionRecovery, "observe", record)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve assertions.")
    agent = Agent()
    workflow = engine(project, agent)
    first = asyncio.create_task(workflow.start(str(request)))
    try:
        await asyncio.wait_for(closing.wait(), 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 5)
    finally:
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    calls, questions = [], []

    async def forbidden(self, sources):
        calls.append([row.url for row in sources])
        raise RuntimeError("Fixture forbidden re-dispatch")

    async def pause(question):
        questions.append(question)
        raise asyncio.CancelledError()

    monkeypatch.setattr(ResearchService, "verify", forbidden)
    restarted = engine(project, agent)
    restarted.question_handler = pause
    outcome = None
    try:
        await restarted.start()
    except BaseException as error:
        outcome = error
    assert calls == [], f"Refusal was re-dispatched before a prerequisite: {calls}; outcome={type(outcome).__name__}"
    assert questions and questions[0]["key"].startswith("prerequisite/")
    assert isinstance(outcome, asyncio.CancelledError)
