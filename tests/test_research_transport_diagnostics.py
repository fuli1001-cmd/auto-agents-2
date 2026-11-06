import asyncio
import errno
import json
import socket
import ssl
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from auto_agents_2.errors import (
    DocumentAcquisitionUnavailable,
    ResearchResolverUnavailable,
    http_failure_details,
    preserve_http_connection_failure,
)
from auto_agents_2.network import PublicHTTPTransport
from auto_agents_2.network_proxy import ProxySettings
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import failure_observation
from auto_agents_2.research_dns import HTTPSJSONResolver
from auto_agents_2.util import digest
from httpcore._backends.anyio import AnyIOStream
from test_research_resolver import policy


async def public_resolution(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class CertificateFailure(httpcore.AsyncMockStream):
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
        cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERT_PRIVATE_MESSAGE")
        cause.verify_code = 20
        raise httpcore.ConnectError("EXAMPLE_TLS_PRIVATE_MESSAGE") from cause


@pytest.mark.parametrize("cause_kind", ["none", "implicit", "permission", "refused"])
async def test_real_pool_keeps_unknown_and_permission_boundaries(monkeypatch, cause_kind):
    calls = []

    async def dial(*args, **kwargs):
        calls.append(True)
        error = httpcore.ConnectError("EXAMPLE_PRIVATE_MESSAGE")
        if cause_kind == "implicit":
            try:
                raise OSError(errno.ECONNREFUSED, "EXAMPLE_UNRELATED_CONTEXT")
            except OSError:
                raise error from None
        if cause_kind != "none":
            code = errno.EACCES if cause_kind == "permission" else errno.ECONNREFUSED
            raise error from OSError(code, "EXAMPLE_OS_PRIVATE_MESSAGE")
        raise error from None

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_resolution)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=False), trust_env=False) as client:
        with pytest.raises(httpx.ConnectError) as observed:
            await client.get("https://docs.example/EXAMPLE_PATH?token=EXAMPLE_TOKEN")
    failure = DocumentAcquisitionUnavailable("https://docs.example/", observed.value)
    diagnostic = failure_observation(failure)
    assert len(calls) == 1 and failure.evidence["connection_phase"] == "public_connect"
    assert diagnostic["automatic"] is (cause_kind == "refused")
    if cause_kind in {"none", "implicit"}:
        assert "os_errno" not in failure.evidence and diagnostic["classification"] == "unknown"
    assert "EXAMPLE_" not in json.dumps(failure.evidence) + str(failure)


async def test_concurrent_real_pool_failures_and_existing_trace_are_isolated(monkeypatch):
    both_started, starts = asyncio.Event(), []

    async def dial(self, host, port, *args, **kwargs):
        starts.append(port)
        if len(starts) == 2:
            both_started.set()
        await both_started.wait()
        if port == 443:
            return CertificateFailure([])
        raise httpcore.ConnectError("EXAMPLE_TCP_SECRET") from OSError(errno.ECONNREFUSED, "EXAMPLE_OS_SECRET")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_resolution)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    traced = []

    async def previous(name, info):
        traced.append(name)
        if name == "connection.start_tls.failed":
            assert info["exception"].__cause__.verify_code == 20

    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=False), trust_env=False) as client:
        tls_request = client.build_request("GET", "https://docs.example/", extensions={"trace": previous})
        tcp_request = client.build_request("GET", "http://other.example/")
        tls, tcp = await asyncio.gather(client.send(tls_request), client.send(tcp_request), return_exceptions=True)
    assert isinstance(tls, httpx.ConnectError) and isinstance(tcp, httpx.ConnectError)
    assert tls_request.extensions["trace"] is previous and "trace" not in tcp_request.extensions
    tls_details, tcp_details = http_failure_details(tls), http_failure_details(tcp)
    assert tls_details["connection_phase"] == "origin_tls" and tls_details["ssl_verify_code"] == 20
    assert tls_details["os_errno"] == 1 and tcp_details["os_errno"] == errno.ECONNREFUSED
    assert tcp_details["connection_phase"] == "public_connect" and "ssl_verify_code" not in tcp_details
    assert "connection.connect_tcp.complete" in traced and "connection.start_tls.failed" in traced
    assert not failure_observation(DocumentAcquisitionUnavailable("https://docs.example/", tls))["automatic"]


async def test_real_resolver_mapping_retains_certificate_failure_and_drains_queries(monkeypatch):
    streams = []

    async def dial(*args, **kwargs):
        stream = CertificateFailure([])
        streams.append(stream)
        return stream

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    selected = policy()
    resolver = HTTPSJSONResolver(selected, ProxySettings(False))
    tasks_before = asyncio.all_tasks()
    with pytest.raises(ResearchResolverUnavailable) as observed:
        await resolver.resolve("docs.example", 443, 1)
    assert observed.value.evidence["connection_phase"] == "origin_tls"
    assert observed.value.evidence["ssl_verify_code"] == 20
    assert observed.value.evidence["os_errno"] == 1 and not resolver.connections
    assert asyncio.all_tasks() <= tasks_before
    assert streams and "EXAMPLE_" not in json.dumps(observed.value.evidence) + str(observed.value)


async def test_redirect_failure_binds_current_hop_with_real_transport(tmp_path, monkeypatch):
    calls = []
    destination = "https://other.example/EXAMPLE_HOP?section=EXAMPLE_QUERY"
    first = httpcore.AsyncMockStream([
        ("HTTP/1.1 302 Found\r\nLocation: " + destination + "\r\nContent-Length: 0\r\n\r\n").encode()
    ])

    async def dial(*args, **kwargs):
        calls.append(True)
        return first if len(calls) == 1 else CertificateFailure([])

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_resolution)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: PublicHTTPTransport(trust_env=False))
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(SimpleNamespace(store=SimpleNamespace(root=tmp_path))).capture("http://docs.example/")
    assert len(calls) == 2
    assert observed.value.evidence["source_origin"] == "https://other.example/"
    assert observed.value.evidence["source_url_hash"] == digest(destination)
    assert observed.value.evidence["connection_phase"] == "origin_tls"
    assert observed.value.evidence["ssl_verify_code"] == 20
    assert "EXAMPLE_" not in json.dumps(observed.value.evidence) + str(observed.value)


async def test_real_pool_cancellation_is_not_a_connection_failure(monkeypatch):
    started, drained = asyncio.Event(), asyncio.Event()

    async def dial(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            drained.set()

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_resolution)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=False), trust_env=False) as client:
        task = asyncio.create_task(client.get("https://docs.example/"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as observed:
            await task
    assert drained.is_set() and not hasattr(observed.value, "_aa2_http_connection_failure")


def test_connection_snapshot_cannot_be_rebound_to_an_unrelated_error():
    original = httpcore.ConnectError("EXAMPLE_ORIGINAL_SECRET")
    original.__cause__ = OSError(errno.ECONNREFUSED, "EXAMPLE_OS_SECRET")
    preserve_http_connection_failure(original, "public_connect")
    other = httpx.ConnectError("EXAMPLE_OTHER_SECRET")
    other._aa2_http_connection_failure = original._aa2_http_connection_failure
    assert http_failure_details(other) == {"category": "network", "error_type": "ConnectError"}
    original.__cause__ = None
    mapped = httpx.ConnectError("EXAMPLE_MAPPED_SECRET")
    mapped.__cause__ = original
    assert http_failure_details(mapped)["os_errno"] == errno.ECONNREFUSED
    assert "EXAMPLE_" not in json.dumps(http_failure_details(mapped))


@pytest.mark.parametrize("first", ["certificate", "unknown", "permission", "refused"])
@pytest.mark.parametrize("last", ["refused", "timeout"])
async def test_all_address_failures_cannot_hide_an_earlier_boundary(monkeypatch, first, last):
    calls = []

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ("93.184.216.34", "1.1.1.1")]

    async def tunnel(*args, **kwargs):
        calls.append(True)
        kind = first if len(calls) == 1 else last
        if kind == "timeout":
            raise httpcore.ConnectTimeout("EXAMPLE_TIMEOUT_SECRET")
        error = httpcore.ConnectError("EXAMPLE_CONNECTION_SECRET")
        if kind == "certificate":
            cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERT_SECRET")
            cause.verify_code = 20
            raise error from cause
        if kind == "unknown":
            raise error from None
        code = errno.EACCES if kind == "permission" else errno.ECONNREFUSED
        raise error from OSError(code, "EXAMPLE_OS_SECRET")

    monkeypatch.setenv("HTTPS_PROXY", "https://EXAMPLE_PROXY_USER:EXAMPLE_PROXY_PASSWORD@127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr("auto_agents_2.network.open_proxy_tunnel", tunnel)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False) as client:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)) as observed:
            await client.get("https://docs.example/")
    failure = DocumentAcquisitionUnavailable("https://docs.example/", observed.value)
    assert len(calls) == 2
    assert len(failure.evidence["connection_failures"]) == 2
    assert failure_observation(failure)["automatic"] is (first == "refused")
    if first == "certificate":
        assert failure.evidence["ssl_verify_code"] == 20
    if first == "unknown":
        assert "os_errno" not in failure.evidence
    assert "EXAMPLE_" not in json.dumps(failure.evidence) + str(failure)


async def test_late_certificate_boundary_survives_bounded_attempt_summary(monkeypatch):
    calls = []

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"93.184.216.{i}", port)) for i in range(1, 21)]

    async def tunnel(*args, **kwargs):
        calls.append(True)
        error = httpcore.ConnectError("EXAMPLE_CONNECTION_SECRET")
        if len(calls) == 18:
            cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERT_SECRET")
            cause.verify_code = 20
            raise error from cause
        raise error from OSError(errno.ECONNREFUSED, "EXAMPLE_OS_SECRET")

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr("auto_agents_2.network.open_proxy_tunnel", tunnel)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False) as client:
        with pytest.raises(httpx.ConnectError) as observed:
            await client.get("https://docs.example/")
    failure = DocumentAcquisitionUnavailable("https://docs.example/", observed.value)
    assert len(calls) == failure.evidence["connection_failure_count"] == 20
    assert failure.evidence["ssl_verify_code"] == 20 and not failure_observation(failure)["automatic"]
    summary = failure.evidence["connection_failures"]
    assert failure.evidence["connection_failures_truncated"] is True
    assert len(summary) == 16 and summary[-2]["attempt"] == 18 and summary[-1]["attempt"] == 20
    assert len(json.dumps(failure.evidence)) < 8192 and "EXAMPLE_" not in json.dumps(failure.evidence)


async def test_success_after_an_address_failure_keeps_existing_success_behavior(monkeypatch):
    calls = []

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ("93.184.216.34", "1.1.1.1")]

    async def dial(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise httpcore.ConnectError("EXAMPLE_UNKNOWN") from None
        return httpcore.AsyncMockStream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=False), trust_env=False) as client:
        response = await client.get("https://docs.example/")
    assert len(calls) == 2 and response.status_code == 200 and response.text == "ok"


@pytest.mark.parametrize("cleanup", ["reset", "timeout"])
async def test_real_anyio_tls_cleanup_cannot_convert_a_hidden_certificate_error_to_recovery(monkeypatch, cleanup):
    import anyio.streams.tls

    class RawStream:
        closed = False

        async def aclose(self):
            self.closed = True
            if cleanup == "timeout":
                raise TimeoutError("EXAMPLE_CLOSE_TIMEOUT")
            raise OSError(errno.ECONNRESET, "EXAMPLE_CLOSE_SECRET")

    raw = RawStream()

    async def wrap(*args, **kwargs):
        cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERT_SECRET")
        cause.verify_code = 20
        raise cause

    async def dial(*args, **kwargs):
        return AnyIOStream(raw)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_resolution)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    monkeypatch.setattr(anyio.streams.tls.TLSStream, "wrap", wrap)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=False), trust_env=False) as client:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)) as observed:
            await client.get("https://docs.example/")
    failure = DocumentAcquisitionUnavailable("https://docs.example/", observed.value)
    assert raw.closed and failure.evidence["connection_phase"] == "origin_tls"
    assert failure.evidence["connection_context_uncertain"] is True
    assert "ssl_verify_code" not in failure.evidence  # Not observed at the trace; never invented.
    assert failure_observation(failure)["classification"] == "unknown"
    assert not failure_observation(failure)["automatic"]
    assert "EXAMPLE_" not in json.dumps(failure.evidence) + str(failure)


async def test_existing_trace_still_requires_a_coroutine(monkeypatch):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_resolution)
    called = []

    def previous(name, info):
        called.append(name)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=False), trust_env=False) as client:
        with pytest.raises(TypeError, match="coroutine"):
            await client.get("https://docs.example/", extensions={"trace": previous})
    assert called == ["connection.connect_tcp.started"]


@pytest.mark.parametrize("field,value", [("phase", []), ("numeric", []), ("uncertain", None)])
def test_malformed_controller_snapshot_cannot_break_failure_diagnostics(field, value):
    error = httpx.ConnectError("EXAMPLE_SECRET")
    preserve_http_connection_failure(error, "public_connect")
    setattr(error._aa2_http_connection_failure, field, value)
    assert http_failure_details(error) == {"category": "network", "error_type": "ConnectError"}
