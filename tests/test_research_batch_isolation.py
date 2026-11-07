"""Offline batch-isolation counterexamples; no provider calls or live network."""
import asyncio
import itertools
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from auto_agents_2 import research
from auto_agents_2.errors import (
    AA2Error,
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    ResearchDestinationRejected,
    ResearchResolverUnavailable,
)
from auto_agents_2.faults import StateConflict
from auto_agents_2.http_body import acquisition_deadline
from auto_agents_2.models import SourceEvidence
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import failure_observation
from auto_agents_2.research_batch import ResearchBatch, ResearchRequestNotExecuted, current_batch
from auto_agents_2.util import digest

URLS = [f"https://docs{index}.example.test/reference" for index in range(6)]


def source(url):
    return SourceEvidence(url=url, claim="Fixture claim")


def captured(url):
    return {"artifact": {"hash": digest(url), "kind": "fixture", "bytes": 12},
            "excerpt": "Fixture content", "url": url, "final_url": url}


def status_error(url, status):
    response = httpx.Response(status, request=httpx.Request("GET", url))
    return DocumentAcquisitionUnavailable(url, httpx.HTTPStatusError(
        "EXAMPLE_PRIVATE_MESSAGE", request=response.request, response=response))


def unknown(url):
    return DocumentAcquisitionUnavailable(url, httpx.ConnectError("EXAMPLE_UNKNOWN_MESSAGE"))


async def drain(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["unknown", "403"])
async def test_early_boundary_waits_for_sibling_success_and_failure(monkeypatch, kind):
    all_started, release = asyncio.Event(), asyncio.Event()
    starts, finishes = [], []
    failure = unknown(URLS[0]) if kind == "unknown" else status_error(URLS[0], 403)

    async def capture(self, url):
        starts.append(url)
        if len(starts) == 3:
            all_started.set()
        if url == URLS[0]:
            await all_started.wait()
            raise failure
        try:
            await release.wait()
            if url == URLS[2]:
                raise status_error(url, 404)
            return captured(url)
        finally:
            finishes.append(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    batch = asyncio.create_task(ResearchService(None).verify([source(url) for url in URLS[:3]]))
    try:
        await asyncio.wait_for(all_started.wait(), 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not batch.done() and finishes == []
        release.set()
        with pytest.raises(DocumentAcquisitionUnavailable) as observed:
            await asyncio.wait_for(batch, 1)
        assert observed.value is failure
        rows = failure.acquisition_observations
        assert [row["url"] for row in rows] == URLS[:3]
        assert [row["status"] for row in rows] == ["failed", "retrieved", "failed"]
        assert rows[1]["retrieval"]["artifact"]["hash"] == digest(URLS[1])
        assert "excerpt" not in rows[1]["retrieval"]
        assert set(finishes) == set(URLS[1:3]) and failure.acquisition_id
        assert "EXAMPLE_" not in str(rows)
    finally:
        release.set()
        await drain(batch)


@pytest.mark.parametrize("order", list(itertools.permutations(("404", "unknown", "security", "permission"))))
async def test_failure_priority_is_stable_not_completion_order(monkeypatch, order):
    errors = {"404": status_error(URLS[0], 404), "unknown": unknown(URLS[0]),
              "security": ResearchDestinationRejected("docs.example.test"),
              "permission": status_error(URLS[0], 403)}
    release_first, all_started = asyncio.Event(), asyncio.Event()
    starts = []

    async def capture(self, url):
        index = URLS.index(url)
        starts.append(url)
        if len(starts) == 4:
            all_started.set()
        await all_started.wait()
        if index == 0:
            await release_first.wait()
        elif index == 3:
            release_first.set()
        raise errors[order[index]]

    monkeypatch.setattr(ResearchService, "capture", capture)
    expected = next(errors[kind] for kind in order if kind in {"security", "permission"})
    with pytest.raises(type(expected)) as observed:
        await asyncio.wait_for(ResearchService(None).verify([source(url) for url in URLS[:4]]), 1)
    assert observed.value is expected
    rows = expected.acquisition_observations
    assert [row["url"] for row in rows] == URLS[:4]
    assert all(row["status"] == "failed" for row in rows)


@pytest.mark.parametrize("order", [("timeout", "unknown"), ("unknown", "timeout"),
                                  ("404", "timeout"), ("timeout", "404")])
async def test_unknown_precedes_ordinary_failures_and_ordinary_order_stays_stable(monkeypatch, order):
    errors = {"404": status_error(URLS[0], 404), "unknown": unknown(URLS[0]),
              "timeout": DocumentAcquisitionTimeout("Fixture", 30, source_url=URLS[0])}

    async def capture(self, url):
        raise errors[order[URLS.index(url)]]

    monkeypatch.setattr(ResearchService, "capture", capture)
    expected = errors["unknown"] if "unknown" in order else errors[order[0]]
    with pytest.raises(type(expected)) as observed:
        await ResearchService(None).verify([source(url) for url in URLS[:2]])
    assert observed.value is expected and len(expected.acquisition_observations) == 2


async def test_four_capture_limit_and_admitted_queue_continue_after_failure(monkeypatch):
    four_started, release = asyncio.Event(), asyncio.Event()
    starts, active, peak = [], 0, 0
    failure = status_error(URLS[0], 403)

    async def capture(self, url):
        nonlocal active, peak
        starts.append(url)
        active += 1
        peak = max(peak, active)
        if len(starts) == 4:
            four_started.set()
        try:
            await release.wait()
            if url == URLS[0]:
                raise failure
            return captured(url)
        finally:
            active -= 1

    monkeypatch.setattr(ResearchService, "capture", capture)
    batch = asyncio.create_task(ResearchService(None).verify([source(url) for url in URLS]))
    try:
        await asyncio.wait_for(four_started.wait(), 1)
        assert starts == URLS[:4] and active == peak == 4
        release.set()
        with pytest.raises(DocumentAcquisitionUnavailable) as observed:
            await asyncio.wait_for(batch, 1)
        assert observed.value is failure and starts == URLS and peak == 4 and active == 0
        assert [row["status"] for row in failure.acquisition_observations] == ["failed"] + ["retrieved"] * 5
    finally:
        release.set()
        await drain(batch)


async def test_repeated_caller_cancellation_waits_for_all_sibling_cleanup(monkeypatch):
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts, cleanups, finished = [], [], []
    before = asyncio.all_tasks()

    async def capture(self, url):
        starts.append(url)
        if len(starts) == 2:
            started.set()
        try:
            await asyncio.Future()
        finally:
            cleanups.append(url)
            if len(cleanups) == 2:
                cleaning.set()
            await release.wait()
            finished.append(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    batch = asyncio.create_task(ResearchService(None).verify([source(url) for url in URLS[:2]]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        batch.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        batch.cancel()
        await asyncio.sleep(0)
        assert not batch.done() and finished == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(batch, 1)
        assert set(finished) == set(URLS[:2]) and asyncio.all_tasks() <= before
    finally:
        release.set()
        await drain(batch)


async def test_non_exception_control_boundary_cancels_and_drains_siblings(monkeypatch):
    class StopBatch(BaseException):
        pass

    started, cleaned = asyncio.Event(), asyncio.Event()
    control = StopBatch("Fixture control")

    async def capture(self, url):
        if url == URLS[0]:
            await started.wait()
            raise control
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    monkeypatch.setattr(ResearchService, "capture", capture)
    before = asyncio.all_tasks()
    with pytest.raises(StopBatch) as observed:
        await ResearchService(None).verify([source(url) for url in URLS[:2]])
    assert observed.value is control and cleaned.is_set()
    assert control.acquisition_observations[0]["status"] == "failed"
    assert control.acquisition_observations[1]["status"] == "cancelled" and asyncio.all_tasks() <= before


async def test_original_url_admission_still_precedes_all_captures(monkeypatch):
    starts = []

    async def capture(self, url):
        starts.append(url)
        return captured(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    with pytest.raises(AA2Error, match="public"):
        await ResearchService(None).verify([source(URLS[0]), source("http://127.0.0.1/private")])
    assert starts == []


async def test_previous_success_is_not_reused_to_clear_later_unknown_failure(monkeypatch):
    calls = []
    failure = unknown(URLS[0])

    async def capture(self, url):
        calls.append(url)
        if len(calls) == 1:
            return captured(url)
        raise failure

    monkeypatch.setattr(ResearchService, "capture", capture)
    service = ResearchService(None)
    assert (await service.verify([source(URLS[0])]))[0]["retrieval"]["artifact"]["hash"] == digest(URLS[0])
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await service.verify([source(URLS[0])])
    assert observed.value is failure and calls == [URLS[0]] * 2
    assert failure.acquisition_observations[0]["status"] == "failed"


class StalledBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.Future()
        yield b""


async def test_each_http_deadline_starts_after_queue_not_at_batch_start(monkeypatch):
    starts = []

    def deadline(timeout, label, *, source_url):
        starts.append(asyncio.get_running_loop().time())
        return acquisition_deadline(.03, label, source_url=source_url)

    monkeypatch.setattr(research, "acquisition_deadline", deadline)
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, stream=StalledBody())))
    before = asyncio.all_tasks()
    with pytest.raises(DocumentAcquisitionTimeout) as observed:
        await ResearchService(None).verify([source(url) for url in URLS[:5]])
    rows = observed.value.acquisition_observations
    assert len(starts) == 5 and starts[4] - starts[0] >= .02
    assert [row["status"] for row in rows] == ["failed"] * 5
    assert all(row["failure"]["diagnostic"]["timeout_seconds"] == .03 for row in rows)
    assert asyncio.all_tasks() <= before


async def test_slow_refusal_cleanup_keeps_strong_boundary_and_sibling_success(monkeypatch):
    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"Fixture forbidden document"

        async def aclose(self):
            await asyncio.Future()

    def respond(request):
        if str(request.url) == URLS[0]:
            return httpx.Response(403, stream=SlowClose())
        if str(request.url) == URLS[1]:
            return httpx.Response(200, stream=StalledBody())
        return httpx.Response(200, text="Fixture success")

    async def collect(root, request, **kwargs):
        return captured(request["source"])

    monkeypatch.setattr(research, "acquisition_deadline",
        lambda timeout, label, *, source_url: acquisition_deadline(.03, label, source_url=source_url))
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    artifacts = SimpleNamespace(store=SimpleNamespace(root=None))
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(artifacts).verify([source(url) for url in URLS[:3]])
    rows = observed.value.acquisition_observations
    assert observed.value.evidence["http_status"] == 403 and observed.value.evidence["cleanup_failure"]
    assert [row["status"] for row in rows] == ["failed", "failed", "retrieved"]
    assert rows[0]["failure"]["classification"] == "permission" and not rows[0]["failure"]["automatic"]
    assert rows[1]["failure"]["code"] == "DOCUMENT_ACQUISITION_TIMEOUT"


@pytest.mark.parametrize("kind", ["state", "internal"])
async def test_shared_or_unclassified_failure_stops_running_and_queued_sources(monkeypatch, kind):
    errors = {"state": StateConflict("Fixture shared-state mismatch"),
              "internal": ValueError("Fixture artifact integrity failure")}
    failure = errors[kind]
    four_started, trigger = asyncio.Event(), asyncio.Event()
    starts, cleaned = [], []

    async def capture(self, url):
        starts.append(url)
        if len(starts) == 4:
            four_started.set()
        if url == URLS[0]:
            await trigger.wait()
            raise failure
        try:
            await asyncio.Future()
        finally:
            cleaned.append(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    before = asyncio.all_tasks()
    batch = asyncio.create_task(ResearchService(None).verify([source(url) for url in URLS]))
    try:
        await asyncio.wait_for(four_started.wait(), 1)
        trigger.set()
        with pytest.raises(type(failure)) as observed:
            await asyncio.wait_for(batch, 1)
        assert observed.value is failure and starts == URLS[:4]
        assert set(cleaned) == set(URLS[1:4]) and asyncio.all_tasks() <= before
        assert [row["status"] for row in failure.acquisition_observations] == ["failed"] + ["cancelled"] * 5
    finally:
        trigger.set()
        await drain(batch)


async def test_known_transient_resolver_failure_still_waits_for_independent_success(monkeypatch):
    failure = ResearchResolverUnavailable(
        endpoint_origin="https://resolver.example/", endpoint_url_hash="fixture",
        category="timeout", error_type="ConnectTimeout")

    async def capture(self, url):
        if url == URLS[0]:
            raise failure
        await asyncio.sleep(0)
        return captured(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    with pytest.raises(ResearchResolverUnavailable) as observed:
        await ResearchService(None).verify([source(url) for url in URLS[:2]])
    assert observed.value is failure
    assert [row["status"] for row in failure.acquisition_observations] == ["failed", "retrieved"]


@pytest.fixture(autouse=True)
def no_proxy_environment(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def test_import_path_matches_this_checkout():
    expected = Path(__file__).resolve().parents[1] / "src/auto_agents_2/research.py"
    assert Path(research.__file__).resolve() == expected


@pytest.mark.parametrize("kind", ["unknown", "permission"])
async def test_shared_resolver_failure_finishes_inflight_and_blocks_queued_capture(monkeypatch, kind):
    from test_research_resolver import policy

    four_started, trigger, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts, finishes = [], []
    failure = ResearchResolverUnavailable(
        endpoint_origin="https://resolver.example/", endpoint_url_hash="fixture",
        category="network" if kind == "unknown" else "http_status",
        error_type="ConnectError" if kind == "unknown" else "HTTPStatusError",
        status_code=None if kind == "unknown" else 403)

    async def capture(self, url):
        starts.append(url)
        if len(starts) == 4:
            four_started.set()
        if url == URLS[0]:
            await trigger.wait()
            raise failure
        await release.wait()
        finishes.append(url)
        return captured(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    before = asyncio.all_tasks()
    batch = asyncio.create_task(ResearchService(None, resolver_config=policy()).verify(
        [source(url) for url in URLS]))
    try:
        await asyncio.wait_for(four_started.wait(), 1)
        trigger.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not batch.done() and finishes == [] and starts == URLS[:4]
        release.set()
        with pytest.raises(ResearchResolverUnavailable) as observed:
            await asyncio.wait_for(batch, 1)
        assert observed.value is failure
        rows = failure.acquisition_observations
        assert [row["status"] for row in rows] == ["failed"] + ["retrieved"] * 3 + ["not_executed"] * 2
        for row in rows[4:]:
            assert not row["capture_started"] and row["unit"] == "capture"
            assert row["boundary"]["operation"] == "capture_dispatch" and "failure" not in row
        assert rows[4]["boundary"]["context_id"] == rows[5]["boundary"]["context_id"]
        assert set(finishes) == set(URLS[1:4]) and asyncio.all_tasks() <= before
    finally:
        trigger.set()
        release.set()
        await drain(batch)


@pytest.mark.parametrize("phase", ["proxy_error", "uncertain_public_connect"])
async def test_shared_proxy_boundary_stops_same_route_but_not_direct_sources(monkeypatch, phase):
    monkeypatch.setenv("HTTPS_PROXY", "http://EXAMPLE_USER:EXAMPLE_PASSWORD@127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "docs5.example.test")
    cause = httpx.ProxyError("EXAMPLE_PROXY_SECRET") if phase == "proxy_error" else httpx.ConnectError(
        "EXAMPLE_PUBLIC_CONNECT_SECRET")
    failure = DocumentAcquisitionUnavailable(URLS[0], cause)
    if phase == "uncertain_public_connect":
        failure.evidence["connection_phase"] = "public_connect"
        failure.evidence["connection_context_uncertain"] = True
    four_started, trigger, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts = []

    async def capture(self, url):
        starts.append(url)
        if len(starts) == 4:
            four_started.set()
        if url == URLS[0]:
            await trigger.wait()
            raise failure
        await release.wait()
        return captured(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    batch = asyncio.create_task(ResearchService(None).verify([source(url) for url in URLS]))
    try:
        await asyncio.wait_for(four_started.wait(), 1)
        trigger.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert URLS[4] not in starts
        release.set()
        with pytest.raises(DocumentAcquisitionUnavailable) as observed:
            await asyncio.wait_for(batch, 1)
        assert observed.value is failure and set(starts) == set(URLS[:4] + URLS[5:])
        rows = failure.acquisition_observations
        assert [row["status"] for row in rows] == ["failed"] + ["retrieved"] * 3 + ["not_executed", "retrieved"]
        assert rows[4]["boundary"]["operation"] == "proxy_request" and "http_status" not in failure.evidence
        assert "EXAMPLE_" not in str(rows)
    finally:
        trigger.set()
        release.set()
        await drain(batch)


async def test_batch_freezes_proxy_snapshot_and_new_batch_does_not_inherit_stop(monkeypatch):
    import httpcore
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    first = ResearchBatch(None)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")
    failure = DocumentAcquisitionUnavailable(URLS[0], httpx.ProxyError("EXAMPLE_PROXY_SECRET"))
    first.observe_connection(failure, URLS[0])
    with pytest.raises(ResearchRequestNotExecuted):
        first.before_capture(URLS[0])
    second = ResearchBatch(None)
    second.before_capture(URLS[0])
    assert first.id != second.id
    assert first.proxy_settings.route(httpcore.URL(URLS[0]).origin).url.port == 7890
    assert second.proxy_settings.route(httpcore.URL(URLS[0]).origin).url.port == 7891


async def test_real_resolver_guard_keeps_dispatched_siblings_and_stops_new_dns(monkeypatch):
    import httpcore
    from test_research_resolver import answer, policy

    from auto_agents_2 import network

    all_dns_started, release_failure, release_good = asyncio.Event(), asyncio.Event(), asyncio.Event()
    queries, drained = [], []

    async def respond(request):
        name, kind = request.url.params["name"], int(request.url.params["type"])
        queries.append((name, kind))
        if len(queries) == 8:
            all_dns_started.set()
        if name == "docs0.example.test":
            if kind == 1:
                await release_failure.wait()
                return httpx.Response(403)
            try:
                await asyncio.Future()
            finally:
                drained.append(name)
        await release_good.wait()
        return httpx.Response(200, json=answer(kind, hostname=name))

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            assert host == "93.184.216.34" and port == 443
            return httpcore.AsyncMockStream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])

    async def collect(root, request, **kwargs):
        return captured(request["source"])

    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    artifacts = SimpleNamespace(store=SimpleNamespace(root=None))
    before = asyncio.all_tasks()
    batch = asyncio.create_task(ResearchService(artifacts, resolver_config=policy()).verify(
        [source(url) for url in URLS]))
    try:
        await asyncio.wait_for(all_dns_started.wait(), 1)
        release_failure.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not batch.done()
        release_good.set()
        with pytest.raises(ResearchResolverUnavailable) as observed:
            await asyncio.wait_for(batch, 1)
        rows = observed.value.acquisition_observations
        assert [row["status"] for row in rows] == ["failed"] + ["retrieved"] * 3 + ["not_executed"] * 2
        assert len(queries) == 8 and drained == ["docs0.example.test"]
        assert observed.value.evidence["http_status"] == 403 and asyncio.all_tasks() <= before
    finally:
        release_failure.set()
        release_good.set()
        await drain(batch)


@pytest.mark.parametrize("same_origin", [False, True])
async def test_real_redirect_guard_blocks_new_resolution_before_target_request(monkeypatch, same_origin):
    import httpcore
    from test_research_resolver import answer, policy

    from auto_agents_2 import network
    from auto_agents_2.research_batch import ResearchBatch

    target_started, shared_failure = asyncio.Event(), asyncio.Event()
    redirect_url = URLS[1].removesuffix("/reference") + "/next" if same_origin else "https://redirect.example.test/next"
    queries, target_requests = [], []
    original_observe = ResearchBatch.observe_resolver

    def observe(self, error, endpoint=None):
        original_observe(self, error, endpoint)
        if not failure_observation(error)["automatic"]:
            shared_failure.set()

    async def respond(request):
        name, kind = request.url.params["name"], int(request.url.params["type"])
        queries.append((name, kind))
        if name == "docs0.example.test":
            await target_started.wait()
            return httpx.Response(403)
        return httpx.Response(200, json=answer(kind, hostname=name))

    class RedirectStream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            if buffer.startswith(b"GET "):
                target_requests.append(buffer.split(b" ", 2)[1].decode())
                target_started.set()
        async def read(self, max_bytes, timeout=None):
            await shared_failure.wait()
            return await super().read(max_bytes, timeout)

    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            assert host == "93.184.216.34" and port == 443
            return RedirectStream([("HTTP/1.1 302 Found\r\nLocation: " + redirect_url +
                                    "\r\nConnection: close\r\nContent-Length: 0\r\n\r\n").encode()])

    async def forbidden(*args, **kwargs):
        pytest.fail("An incomplete redirect capture cannot publish evidence")

    monkeypatch.setattr(ResearchBatch, "observe_resolver", observe)
    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", forbidden)
    artifacts = SimpleNamespace(store=SimpleNamespace(root=None))
    with pytest.raises(ResearchResolverUnavailable) as observed:
        await asyncio.wait_for(ResearchService(artifacts, resolver_config=policy()).verify(
            [source(url) for url in URLS[:2]]), 1)
    row = observed.value.acquisition_observations[1]
    assert row["status"] == "not_executed" and row["capture_started"] and row["unit"] == "pending_request"
    assert row["boundary"]["operation"] == "resolver_request"
    assert row["boundary"]["blocked_request_url_hash"] == digest(redirect_url)
    assert row["boundary"]["prior_response_headers"] == [{"url_hash": digest(URLS[1]), "http_status": 302}]
    assert target_requests == ["/reference"]
    assert all(name in {"docs0.example.test", "docs1.example.test"} for name, _ in queries)
    assert sum(name == "docs1.example.test" for name, _ in queries) == 2


async def test_resolver_refusal_is_frozen_before_slow_cleanup(monkeypatch):
    from test_research_resolver import policy

    from auto_agents_2 import network
    from auto_agents_2.research_dns import HTTPSJSONResolver

    class Refusal(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"Fixture forbidden"
        async def aclose(self):
            await asyncio.Future()

    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(403, stream=Refusal())

    context = ResearchBatch(policy())
    token = current_batch.set(context)
    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    before = asyncio.all_tasks()
    try:
        with pytest.raises(ResearchResolverUnavailable) as observed:
            await HTTPSJSONResolver(policy(), context.proxy_settings).resolve("docs.example.test", 443, .03)
        assert observed.value is context.resolver_failure and observed.value.evidence["http_status"] == 403
        count = len(calls)
        with pytest.raises(ResearchRequestNotExecuted):
            await HTTPSJSONResolver(policy(), context.proxy_settings).resolve("new.example.test", 443, .03)
        assert len(calls) == count and asyncio.all_tasks() <= before
    finally:
        current_batch.reset(token)


def test_not_executed_ledger_retains_refusal_and_does_not_mint_progress(tmp_path):
    import copy

    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.research_acquisition import PREFIX, ResearchAcquisitionRecovery
    from auto_agents_2.store import Store
    from auto_agents_2.util import identifier

    store = Store(tmp_path)
    rid = store.admit("Fixture source", {})["id"]
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=ArtifactStore(store.root)))
    document = {"summary": "Fixture", "document": "Two facts", "sources": [source(url).model_dump() for url in URLS[:2]]}
    failure = unknown(URLS[0])
    blocked = ResearchRequestNotExecuted(ResearchBatch(None), "capture_dispatch", failure)
    report = [{"url": URLS[0], "status": "failed",
               "failure": failure_observation(failure)},
              {"url": URLS[1], "status": "not_executed", "capture_started": False, "unit": "capture",
               "boundary": blocked.evidence}]
    failure.acquisition_id = identifier("research-acquisition")
    failure.acquisition_observations = report
    recovery.observe(rid, document, failure)
    key = PREFIX + digest([rid, 0, recovery.binding(rid), document])
    first = store.get(rid, key)
    assert first["attempts"][0]["observations"] == report and "pending" not in first

    # A new opaque context ID and attempt ID cannot turn the same facts into progress.
    repeated = unknown(URLS[0])
    repeated.acquisition_id = identifier("research-acquisition")
    repeated.acquisition_observations = copy.deepcopy(report)
    repeated.acquisition_observations[1]["boundary"]["context_id"] = "different-fixture-context"
    recovery.observe(rid, document, repeated)
    attempts = store.get(rid, key)["attempts"]
    assert attempts[0]["facts_hash"] == attempts[1]["facts_hash"]
    assert attempts[0]["observations"] == report

    # A later ordinary timeout plus a new sibling retrieval cannot clear the original refusal.
    later = DocumentAcquisitionTimeout("Fixture", 30, source_url=URLS[0])
    later.acquisition_id = identifier("research-acquisition")
    later.acquisition_observations = [
        {"url": URLS[0], "status": "failed",
         "failure": failure_observation(later)},
        {"url": URLS[1], "status": "retrieved", "retrieval": captured(URLS[1])}]
    recovery.observe(rid, document, later)
    assert len(store.get(rid, key)["attempts"]) == 3 and "pending" not in store.get(rid, key)
    assert recovery.active(rid) is None and store.run(rid)["data"]["replans"] == 0


@pytest.mark.parametrize("phase", [None, "public_connect", "origin_tls"])
async def test_unknown_connect_needs_proven_source_scope_before_new_route_dispatch(monkeypatch, phase):
    failure = unknown(URLS[0])
    if phase is not None:
        failure.evidence["connection_phase"] = phase
    four_started, trigger, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts = []

    async def capture(self, url):
        starts.append(url)
        if len(starts) == 4:
            four_started.set()
        if url == URLS[0]:
            await trigger.wait()
            raise failure
        await release.wait()
        return captured(url)

    monkeypatch.setattr(ResearchService, "capture", capture)
    batch = asyncio.create_task(ResearchService(None).verify([source(url) for url in URLS]))
    try:
        await asyncio.wait_for(four_started.wait(), 1)
        trigger.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(DocumentAcquisitionUnavailable) as observed:
            await asyncio.wait_for(batch, 1)
        assert observed.value is failure
        rows = failure.acquisition_observations
        if phase == "origin_tls":
            assert starts == URLS and all(row["status"] == "retrieved" for row in rows[1:])
        else:
            assert starts == URLS[:4]
            assert [row["status"] for row in rows] == ["failed"] + ["retrieved"] * 3 + ["not_executed"] * 2
            assert rows[4]["boundary"]["operation"] == "public_connect"
    finally:
        trigger.set()
        release.set()
        await drain(batch)


async def test_parallel_and_later_verify_have_independent_contexts(monkeypatch):
    from test_research_resolver import policy

    service_a, service_b = ResearchService(None, resolver_config=policy()), ResearchService(None, resolver_config=policy())
    both_a_started, failed_a, release_a = asyncio.Event(), asyncio.Event(), asyncio.Event()
    a_starts, context_ids = [], {}
    failure = ResearchResolverUnavailable(
        endpoint_origin="https://resolver.example/", endpoint_url_hash="fixture",
        category="network", error_type="ConnectError")
    original_observe = ResearchBatch.observe_resolver

    def observe(self, error, endpoint=None):
        original_observe(self, error, endpoint)
        if error is failure:
            failed_a.set()

    async def capture(self, url):
        context_ids.setdefault(self, set()).add(current_batch.get().id)
        if self is service_a:
            a_starts.append(url)
            if len(a_starts) == 2:
                both_a_started.set()
            if url == URLS[0]:
                await both_a_started.wait()
                raise failure
            await release_a.wait()
        return captured(url)

    monkeypatch.setattr(ResearchBatch, "observe_resolver", observe)
    monkeypatch.setattr(ResearchService, "capture", capture)
    batch_a = asyncio.create_task(service_a.verify([source(url) for url in URLS[:2]]))
    try:
        await asyncio.wait_for(failed_a.wait(), 1)
        assert not batch_a.done()
        assert len(await service_b.verify([source(url) for url in URLS])) == 6
        assert context_ids[service_a].isdisjoint(context_ids[service_b])
        assert current_batch.get() is None
        assert len(await service_b.verify([source(URLS[0])])) == 1 and len(context_ids[service_b]) == 2
        release_a.set()
        with pytest.raises(ResearchResolverUnavailable) as observed:
            await asyncio.wait_for(batch_a, 1)
        assert observed.value is failure and current_batch.get() is None
    finally:
        release_a.set()
        await drain(batch_a)


async def test_already_observed_other_dns_family_refusal_beats_first_transient(monkeypatch):
    from test_research_resolver import policy

    from auto_agents_2 import network
    from auto_agents_2.research_dns import HTTPSJSONResolver

    both_started = asyncio.Event()
    starts = []

    class DelayedClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"Fixture"
        async def aclose(self):
            await asyncio.sleep(0)

    async def respond(request):
        kind = int(request.url.params["type"])
        starts.append(kind)
        if len(starts) == 2:
            both_started.set()
        await both_started.wait()
        return httpx.Response(503 if kind == 1 else 403, stream=DelayedClose())

    monkeypatch.setattr(network, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    with pytest.raises(ResearchResolverUnavailable) as observed:
        await HTTPSJSONResolver(policy(), ResearchBatch(policy()).proxy_settings).resolve("docs.example.test", 443, 1)
    assert sorted(starts) == [1, 28] and observed.value.evidence["http_status"] == 403


def test_effective_http_https_and_no_proxy_routes_remain_distinct(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")
    monkeypatch.setenv("NO_PROXY", "docs5.example.test")
    context = ResearchBatch(None)
    http_url = URLS[0].replace("https:", "http:")
    failure = DocumentAcquisitionUnavailable(http_url, httpx.ProxyError("EXAMPLE_PROXY_SECRET"))
    context.observe_connection(failure, http_url)
    with pytest.raises(ResearchRequestNotExecuted) as observed:
        context.before_capture(URLS[1].replace("https:", "http:"))
    assert observed.value.evidence["operation"] == "proxy_request"
    context.before_capture(URLS[1])
    context.before_capture(URLS[5].replace("https:", "http:"))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7892")
    with pytest.raises(ResearchRequestNotExecuted):
        context.before_capture(http_url)
