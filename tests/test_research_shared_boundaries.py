"""Review regressions: exact dispatch boundaries and owned failure provenance."""
import asyncio
import errno
import socket
from pathlib import Path

import httpcore
import httpx
import pytest
from test_research_resolver import policy

from auto_agents_2 import network, research
from auto_agents_2.errors import (
    AA2Error,
    DocumentAcquisitionTimeout,
    ResearchResolverUnavailable,
)
from auto_agents_2.faults import StateConflict
from auto_agents_2.http_body import acquisition_deadline
from auto_agents_2.models import SourceEvidence
from auto_agents_2.network import PublicNetworkBackend
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import failure_observation
from auto_agents_2.research_batch import ResearchBatch, ResearchRequestNotExecuted, current_batch
from auto_agents_2.research_dns import BootstrapResolver, HTTPSJSONResolver
from auto_agents_2.util import digest

URL = "https://docs.example/ref"
IPS = ("93.184.216.34", "1.1.1.1")


@pytest.fixture(autouse=True)
def clean_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def test_source_imports_are_this_checkout():
    root = Path(__file__).resolve().parents[1] / "src/auto_agents_2"
    assert Path(research.__file__).resolve() == root / "research.py"
    assert Path(network.__file__).resolve() == root / "network.py"


def connection_error(kind):
    if kind == "proxy":
        return httpcore.ProxyError("EXAMPLE_PROXY_SECRET")
    error = httpcore.ConnectError("EXAMPLE_CONNECTION_SECRET")
    if kind != "unknown":
        error.__cause__ = OSError(errno.EACCES if kind == "permission" else errno.ECONNREFUSED,
                                  "EXAMPLE_OS_SECRET")
    return error


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("proxied,kind", [(False, "permission"), (False, "unknown"),
                                           (True, "permission"), (True, "unknown"), (True, "proxy")])
async def test_batch_first_nonautomatic_ip_stops_before_successful_second(monkeypatch, bootstrap, proxied, kind):
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    context = ResearchBatch(policy() if bootstrap else None)
    proxy = context.proxy_settings.route(httpcore.URL(URL).origin)
    resolver = BootstrapResolver("docs.example", IPS) if bootstrap else None
    backend = PublicNetworkBackend(proxy, resolver=resolver)
    failure = connection_error(kind)
    calls = []

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in IPS]

    async def dial(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise failure
        return httpcore.AsyncMockStream([])

    async def tunnel(*args, **kwargs):
        return await dial(*args)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(backend.backend, "connect_tcp", dial)
    monkeypatch.setattr(network, "open_proxy_tunnel", tunnel)
    token = current_batch.set(context)
    before = asyncio.all_tasks()
    try:
        with pytest.raises(type(failure)) as observed:
            await backend.connect_tcp("docs.example", 443, 1)
        assert observed.value is failure and len(calls) == 1
        with pytest.raises(ResearchRequestNotExecuted):
            context.before_connect(proxy, "docs.example", 443)
        assert asyncio.all_tasks() <= before
    finally:
        current_batch.reset(token)


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("proxied", [False, True])
async def test_batch_proven_transient_keeps_legal_second_ip_success(monkeypatch, bootstrap, proxied):
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    context = ResearchBatch(policy() if bootstrap else None)
    proxy = context.proxy_settings.route(httpcore.URL(URL).origin)
    backend = PublicNetworkBackend(proxy, resolver=BootstrapResolver("docs.example", IPS) if bootstrap else None)
    calls, success = [], httpcore.AsyncMockStream([])

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in IPS]

    async def dial(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise connection_error("refused")
        return success

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(backend.backend, "connect_tcp", dial)
    monkeypatch.setattr(network, "open_proxy_tunnel", dial)
    token = current_batch.set(context)
    try:
        assert await backend.connect_tcp("docs.example", 443, 1) is success
        assert len(calls) == 2 and context.route_failures == {}
    finally:
        current_batch.reset(token)


class RefusalStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"Fixture forbidden"

    async def aclose(self):
        await asyncio.Future()


def install_nested_refusal(monkeypatch, *, other="pending"):
    started, both = [], asyncio.Event()

    async def respond(request):
        kind = int(request.url.params["type"])
        started.append(kind)
        if len(started) == 2:
            both.set()
        await both.wait()
        if kind == 1:
            return httpx.Response(403, stream=RefusalStream())
        if other == "pending":
            await asyncio.Future()
        await asyncio.sleep(0)
        if other == "json":
            return httpx.Response(200, text="not JSON", headers={"content-type": "application/dns-json"})
        raise other

    async def forbidden(*args, **kwargs):
        pytest.fail("Offline regression must not open sockets")

    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    return started, both


@pytest.mark.parametrize("direct", [False, True])
async def test_outer_owned_document_deadline_preserves_this_capture_refusal(monkeypatch, direct):
    started, _ = install_nested_refusal(monkeypatch)
    monkeypatch.setattr(research, "acquisition_deadline",
        lambda timeout, label, *, source_url: acquisition_deadline(.04, label, source_url=source_url))
    service = ResearchService(None, resolver_config=policy())
    before = asyncio.all_tasks()
    with pytest.raises(ResearchResolverUnavailable) as observed:
        if direct:
            await service.capture(URL)
        else:
            await service.verify([SourceEvidence(url=URL, claim="Fixture")])
    assert sorted(started) == [1, 28] and observed.value.evidence["http_status"] == 403
    assert not failure_observation(observed.value)["automatic"] and asyncio.all_tasks() <= before
    if not direct:
        row = observed.value.acquisition_observations[0]
        assert row["status"] == "failed" and row["failure"]["diagnostic"]["http_status"] == 403


@pytest.mark.parametrize("kind", ["json", "runtime", "state"])
async def test_real_internal_failure_is_not_replaced_by_other_family_refusal(monkeypatch, kind):
    global_failure = RuntimeError("EXAMPLE_INTERNAL") if kind == "runtime" else StateConflict("Fixture state mismatch")
    started, _ = install_nested_refusal(monkeypatch, other="json" if kind == "json" else global_failure)
    before = asyncio.all_tasks()
    with pytest.raises(AA2Error if kind == "json" else type(global_failure)) as observed:
        await ResearchService(None, resolver_config=policy()).verify([SourceEvidence(url=URL, claim="Fixture")])
    if kind != "json":
        assert observed.value is global_failure
    else:
        assert type(observed.value) is AA2Error
    row = observed.value.acquisition_observations[0]
    assert row["failure"]["code"] == "UNCLASSIFIED_ACQUISITION_ERROR"
    assert any(item["diagnostic"].get("http_status") == 403 for item in row["observed_failures"])
    assert sorted(started) == [1, 28] and asyncio.all_tasks() <= before


async def test_real_caller_cancel_after_refusal_keeps_control_and_owned_facts(monkeypatch):
    _, both = install_nested_refusal(monkeypatch)
    observed_refusal = asyncio.Event()
    original = ResearchBatch.observe_resolver

    def observe(self, error, endpoint=None):
        original(self, error, endpoint)
        if error.evidence.get("http_status") == 403:
            observed_refusal.set()

    monkeypatch.setattr(ResearchBatch, "observe_resolver", observe)
    before = asyncio.all_tasks()
    task = asyncio.create_task(ResearchService(None, resolver_config=policy()).verify(
        [SourceEvidence(url=URL, claim="Fixture")]))
    await asyncio.wait_for(both.wait(), 1)
    await asyncio.wait_for(observed_refusal.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as observed:
        await asyncio.wait_for(task, 1)
    row = observed.value.acquisition_observations[0]
    assert row["status"] == "cancelled"
    assert any(item["diagnostic"].get("http_status") == 403 for item in row["observed_failures"])
    assert asyncio.all_tasks() <= before


async def test_same_service_parallel_verify_and_later_direct_capture_do_not_share_state(monkeypatch):
    from auto_agents_2.research_batch import current_capture
    service = ResearchService(None, resolver_config=policy())
    both_started, refused = asyncio.Event(), asyncio.Event()
    contexts, starts = {}, []
    failure = ResearchResolverUnavailable(
        endpoint_origin="https://resolver.example/", endpoint_url_hash="fixture",
        category="http_status", error_type="HTTPStatusError", status_code=403)

    async def capture(self, url, history):
        contexts[url] = current_batch.get()
        starts.append(url)
        if len(starts) == 2:
            both_started.set()
        if url.endswith("first"):
            await both_started.wait()
            history.record(failure)
            current_batch.get().observe_resolver(failure)
            refused.set()
            raise failure
        if url.endswith("second"):
            await refused.wait()
            assert history.failures == [] and current_capture.get() is history
            assert contexts[url].id != contexts["https://docs.example/first"].id
        else:
            assert current_batch.get() is None and history.failures == []
        return {"artifact": {"hash": digest(url)}, "excerpt": "Fixture", "final_url": url}

    monkeypatch.setattr(ResearchService, "_capture", capture)
    a = asyncio.create_task(service.verify([SourceEvidence(url="https://docs.example/first", claim="a")]))
    b = asyncio.create_task(service.verify([SourceEvidence(url="https://docs.example/second", claim="b")]))
    results = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), 1)
    assert results[0] is failure and isinstance(results[1], list)
    assert current_batch.get() is None and current_capture.get() is None
    assert (await service.capture("https://docs.example/direct"))["excerpt"] == "Fixture"
    assert current_capture.get() is None


async def test_multi_bootstrap_http_refusal_does_not_try_second_bootstrap(monkeypatch):
    config = policy()
    config.bootstrap_ips = ["8.8.8.8", "8.8.4.4"]
    context = ResearchBatch(config)
    calls = []

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            calls.append(host)
            return httpcore.AsyncMockStream([b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"])

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    token = current_batch.set(context)
    before = asyncio.all_tasks()
    try:
        with pytest.raises(ResearchResolverUnavailable) as observed:
            await HTTPSJSONResolver(config, context.proxy_settings).resolve("docs.example", 443, 1)
        assert observed.value.evidence["http_status"] == 403
        assert calls and set(calls) == {"8.8.8.8"} and asyncio.all_tasks() <= before
    finally:
        current_batch.reset(token)


async def test_real_proxy_refusal_freezes_before_close_and_before_another_connect(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    context = ResearchBatch(None)
    proxy = context.proxy_settings.route(httpcore.URL(URL).origin)
    closing, release = asyncio.Event(), asyncio.Event()
    dials = []

    class Stream(httpcore.AsyncMockStream):
        async def aclose(self):
            closing.set()
            await release.wait()
            await super().aclose()

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            dials.append((host, port))
            return Stream([b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"])

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in IPS]

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    token = current_batch.set(context)
    task = asyncio.create_task(PublicNetworkBackend(proxy).connect_tcp("docs.example", 443, 1))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        with pytest.raises(ResearchRequestNotExecuted):
            await PublicNetworkBackend(proxy).connect_tcp("other.example", 443, 1)
        assert dials == [("127.0.0.1", 7890)] and not task.done()
        release.set()
        with pytest.raises(httpcore.ProxyError):
            await asyncio.wait_for(task, 1)
        assert dials == [("127.0.0.1", 7890)]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        current_batch.reset(token)


async def test_sibling_timeout_cannot_borrow_another_capture_refusal(monkeypatch):
    starts, all_started = [], asyncio.Event()
    urls = ["https://first.example/ref", "https://second.example/ref"]

    async def respond(request):
        name, kind = request.url.params["name"], int(request.url.params["type"])
        starts.append((name, kind))
        if len(starts) == 4:
            all_started.set()
        await all_started.wait()
        if name == "first.example" and kind == 1:
            return httpx.Response(403, stream=RefusalStream())
        await asyncio.Future()

    async def forbidden(*args, **kwargs):
        pytest.fail("Offline test must never dial")

    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    monkeypatch.setattr(research, "acquisition_deadline",
        lambda timeout, label, *, source_url: acquisition_deadline(.04, label, source_url=source_url))
    before = asyncio.all_tasks()
    with pytest.raises(ResearchResolverUnavailable) as observed:
        await ResearchService(None, resolver_config=policy()).verify(
            [SourceEvidence(url=url, claim="Fixture") for url in urls])
    first, second = observed.value.acquisition_observations
    assert first["failure"]["diagnostic"]["http_status"] == 403
    assert second["failure"]["code"] == "DOCUMENT_ACQUISITION_TIMEOUT" and second["failure"]["automatic"]
    assert "observed_failures" not in second and asyncio.all_tasks() <= before


async def test_global_exception_durably_keeps_separate_refusal_fact(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.research_acquisition import PREFIX, ResearchAcquisitionRecovery
    from auto_agents_2.store import Store

    install_nested_refusal(monkeypatch, other="json")
    with pytest.raises(AA2Error) as observed:
        await ResearchService(None, resolver_config=policy()).verify([SourceEvidence(url=URL, claim="Fixture")])
    assert type(observed.value) is AA2Error
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    document = {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]}
    recovery.observe(rid, document, observed.value)
    record = store.get(rid, PREFIX + digest([rid, 0, recovery.binding(rid), document]))
    row = record["attempts"][0]["observations"][0]
    assert row["failure"]["code"] == "UNCLASSIFIED_ACQUISITION_ERROR"
    assert any(item["diagnostic"].get("http_status") == 403 for item in row["observed_failures"])
    assert "pending" not in record and recovery.active(rid) is None


@pytest.mark.parametrize("recording_failure", ["none", "journal", "audit"])
async def test_engine_cancel_records_refusal_without_replan_or_retry(tmp_path, monkeypatch, recording_failure):
    import json

    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.models import DocumentResult
    from auto_agents_2.research_acquisition import PREFIX, ResearchAcquisitionRecovery

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                return DocumentResult(summary="Fixture", document="Preserve fact",
                                      sources=[SourceEvidence(url=URL, claim="Fixture")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)

    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve all assertions.")
    workflow = engine(project, Agent())
    config = json.loads(workflow.config.path.read_text())
    config["research_resolver"] = policy().model_dump()
    workflow.config.path.write_text(json.dumps(config))
    install_nested_refusal(monkeypatch)
    seen = asyncio.Event()
    original = ResearchBatch.observe_resolver

    def observe(self, error, endpoint=None):
        original(self, error, endpoint)
        if error.evidence.get("http_status") == 403:
            seen.set()

    monkeypatch.setattr(ResearchBatch, "observe_resolver", observe)
    original_record = ResearchAcquisitionRecovery.observe
    original_event = workflow.store._event

    def record(self, rid, document, error, *, control=False):
        if control and recording_failure != "none":
            raise OSError("EXAMPLE_RECORDING_SECRET")
        return original_record(self, rid, document, error, control=control)

    def event(db, rid, kind, data):
        if kind == "research.acquisition_recording_failed" and recording_failure == "audit":
            raise ValueError("EXAMPLE_AUDIT_SECRET")
        return original_event(db, rid, kind, data)

    monkeypatch.setattr(ResearchAcquisitionRecovery, "observe", record)
    monkeypatch.setattr(workflow.store, "_event", event)
    before = asyncio.all_tasks()
    task = asyncio.create_task(workflow.start(str(request)))
    try:
        await asyncio.wait_for(seen.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as control_error:
            await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert asyncio.all_tasks() <= before
    rid = workflow.store.current()["id"]
    recovery = ResearchAcquisitionRecovery(workflow)
    candidate = workflow.store.get(rid, "candidate/provider_research")["document"]
    key = PREFIX + digest([rid, 0, recovery.binding(rid), candidate])
    record = workflow.store.get(rid, key)
    if recording_failure != "none":
        error = control_error.value
        details = error.acquisition_recording_failure
        assert details["error_type"] == "OSError" and details["original_error_type"] == "CancelledError"
        assert details["audit_recorded"] is (recording_failure == "journal")
        assert record["collecting"] and record["attempts"] == [] and not record.get("completed")
        assert record["documents"][URL]["outcomes"][-1]["status"] == "cancelled"
        assert any(item["diagnostic"].get("http_status") == 403
                   for item in error.acquisition_observations[0]["observed_failures"])
        assert "EXAMPLE_" not in str(error.__notes__) + str(details)
        with workflow.store.connect() as db:
            audits = db.execute("SELECT data FROM events WHERE run_id=? AND kind=?",
                                (rid, "research.acquisition_recording_failed")).fetchall()
        assert len(audits) == (1 if recording_failure == "journal" else 0)
        if audits:
            audit = json.loads(audits[0][0])
            assert audit["candidate_hash"] == digest(candidate)
            assert audit["observations"][0]["url"] == URL
        else:
            assert details["audit_error_type"] == "ValueError" and len(error.__notes__) == 2
        assert workflow.store.run(rid)["data"]["replans"] == 0
        assert workflow.store.run(rid)["status"] == "PAUSED"
        return
    row = record["attempts"][0]["observations"][0]
    assert row["status"] == "cancelled"
    assert any(item["diagnostic"].get("http_status") == 403 for item in row["observed_failures"])
    assert workflow.store.run(rid)["status"] == "PAUSED"
    assert workflow.store.run(rid)["data"]["replans"] == 0 and "pending" not in record

    # A later ordinary failure cannot turn that cancelled refusal into permission.
    later = DocumentAcquisitionTimeout("Fixture", 30, source_url=URL)
    later.acquisition_id = "fixture-later-attempt"
    later.acquisition_observations = [{"url": URL, "status": "failed", "failure": failure_observation(later)}]
    recovery.observe(rid, candidate, later)
    assert len(workflow.store.get(rid, key)["attempts"]) == 2
    assert "pending" not in workflow.store.get(rid, key) and recovery.active(rid) is None


async def test_outer_deadline_and_real_cancel_race_remains_cancelled(monkeypatch):
    starts, both = [], asyncio.Event()
    task = None

    class RacedClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"Fixture forbidden"
        async def aclose(self):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # The owned deadline has already cancelled the capture. Deliver
                # an independent caller cancellation before its manager exits.
                task.cancel()
                raise

    async def respond(request):
        kind = int(request.url.params["type"])
        starts.append(kind)
        if len(starts) == 2:
            both.set()
        await both.wait()
        if kind == 1:
            return httpx.Response(403, stream=RacedClose())
        await asyncio.Future()

    async def forbidden(*args, **kwargs):
        pytest.fail("Offline race test must never dial")

    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    monkeypatch.setattr(research, "acquisition_deadline",
        lambda timeout, label, *, source_url: acquisition_deadline(.03, label, source_url=source_url))
    before = asyncio.all_tasks()
    task = asyncio.create_task(ResearchService(None, resolver_config=policy()).capture(URL))
    with pytest.raises(asyncio.CancelledError) as observed:
        await asyncio.wait_for(task, 1)
    assert any(item["diagnostic"].get("http_status") == 403 for item in observed.value.observed_failures)
    assert current_batch.get() is None and asyncio.all_tasks() <= before


async def test_proxy_refusal_before_slow_close_survives_outer_owned_deadline(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    dials = []

    class Stream(httpcore.AsyncMockStream):
        async def aclose(self):
            await asyncio.Future()

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            dials.append((host, port))
            return Stream([b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"])

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in IPS]

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(research, "acquisition_deadline",
        lambda timeout, label, *, source_url: acquisition_deadline(.03, label, source_url=source_url))
    before = asyncio.all_tasks()
    with pytest.raises(AA2Error) as observed:
        await ResearchService(None).verify([SourceEvidence(url=URL, claim="Fixture")])
    assert observed.value.code == "DOCUMENT_ACQUISITION_UNAVAILABLE"
    assert observed.value.evidence["category"] == "proxy" and not failure_observation(observed.value)["automatic"]
    assert "http_status" not in observed.value.evidence
    assert dials == [("127.0.0.1", 7890)] and asyncio.all_tasks() <= before


def test_recording_failure_is_visible_without_turning_global_or_http_into_recovery(tmp_path):
    from types import SimpleNamespace

    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    from auto_agents_2.store import Store

    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    original = StateConflict("Fixture original global fault")
    original.acquisition_id = "fixture-global"
    original.acquisition_observations = [{"url": URL, "status": "failed", "failure": failure_observation(original)}]
    recovery.recording_failed(rid, {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]},
                              original, OSError("EXAMPLE_RECORDING_SECRET"))
    assert type(original) is StateConflict and original.acquisition_recording_failure["audit_recorded"]
    assert not failure_observation(original)["automatic"] and store.run(rid)["data"]["replans"] == 0
    request = httpx.Request("GET", URL)
    http_failure = research.DocumentAcquisitionUnavailable(URL, httpx.HTTPStatusError(
        "EXAMPLE_SOURCE_SECRET", request=request, response=httpx.Response(404, request=request)))
    assert failure_observation(http_failure)["automatic"]
    recovery.recording_failed(rid, {}, http_failure, OSError("EXAMPLE_RECORDING_SECRET"))
    assert not failure_observation(http_failure)["automatic"]
    assert "EXAMPLE_" not in str(original.__notes__) + str(http_failure.evidence)


def test_duplicate_observed_failure_preserves_legacy_no_progress_identity(tmp_path):
    import copy
    from types import SimpleNamespace

    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.engine import Replan
    from auto_agents_2.faults import HumanIntervention
    from auto_agents_2.research_acquisition import PREFIX, ResearchAcquisitionRecovery
    from auto_agents_2.store import Store

    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    document = {"sources": [SourceEvidence(url=URL, claim="Fixture").model_dump()]}
    request = httpx.Request("GET", URL)

    def error(identity, additional):
        result = research.DocumentAcquisitionUnavailable(URL, httpx.HTTPStatusError(
            "EXAMPLE", request=request, response=httpx.Response(404, request=request)))
        result.acquisition_id = identity
        row = {"url": URL, "status": "failed", "failure": failure_observation(result)}
        if additional:
            row["observed_failures"] = [copy.deepcopy(row["failure"])]
        result.acquisition_observations = [row]
        return result

    with pytest.raises(Replan):
        recovery.observe(rid, document, error("fixture-legacy", False))
    with pytest.raises(HumanIntervention):
        recovery.observe(rid, document, error("fixture-modern", True))
    record = store.get(rid, PREFIX + digest([rid, 0, recovery.binding(rid), document]))
    assert len(record["attempts"]) == 2
    assert record["attempts"][0]["facts_hash"] == record["attempts"][1]["facts_hash"]
    assert "observed_failures" not in record["attempts"][0]["observations"][0]
