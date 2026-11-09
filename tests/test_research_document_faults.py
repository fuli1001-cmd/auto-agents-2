import asyncio
import errno
import json
import socket
import ssl
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import (
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    ResearchDestinationRejected,
    ResearchResolverUnavailable,
)
from auto_agents_2.faults import known_exception
from auto_agents_2.research import ResearchService
from auto_agents_2.store import Store
from auto_agents_2.util import digest

URL = "https://docs.example/EXAMPLE_PRIVATE_PATH?section=EXAMPLE_QUERY"


class BrokenRead(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        yield b"partial"
        raise httpx.ReadError("EXAMPLE_RESPONSE_SECRET")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("kind,status", [
    ("ConnectError", None), ("ReadError", None), ("WriteError", None),
    ("HTTPStatusError", 403), ("HTTPStatusError", 429), ("HTTPStatusError", 503),
    ("ConnectTimeout", None), ("ReadTimeout", None), ("ProxyError", None),
    ("RemoteProtocolError", None), ("DecodingError", None), ("HTTPError", None),
])
async def test_document_http_errors_are_safe_prerequisites(tmp_path, monkeypatch, kind, status):
    stream = BrokenRead()
    def respond(request):
        if status is not None:
            return httpx.Response(status, text="EXAMPLE_RESPONSE_SECRET")
        if kind == "ReadError":
            return httpx.Response(200, stream=stream)
        raise getattr(httpx, kind)("EXAMPLE_ERROR_SECRET https://EXAMPLE_USER:EXAMPLE_PASSWORD@private.invalid/?token=EXAMPLE_TOKEN")
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    collected = []
    async def forbidden(*args, **kwargs):
        collected.append(True)
        pytest.fail("Failed/partial document input must never enter collection")
    monkeypatch.setattr("auto_agents_2.collection.run_collection", forbidden)
    artifacts = SimpleNamespace(store=SimpleNamespace(root=tmp_path))
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(artifacts).capture(URL)
    error = observed.value
    assert error.evidence["source_origin"] == "https://docs.example/"
    assert error.evidence["source_url_hash"] == digest(URL)
    assert error.evidence["error_type"] == kind and not collected
    if status is not None:
        assert error.evidence["http_status"] == status
    else:
        assert "http_status" not in error.evidence
    if kind == "ReadError":
        assert stream.closed
    boundary = known_exception(error, "provider_research")
    assert boundary.reason_code == error.code and boundary.requirement.kind == "external_input"
    public = str(error) + repr(error.__dict__) + boundary.requirement.model_dump_json()
    for marker in ("EXAMPLE_PRIVATE_PATH", "EXAMPLE_QUERY", "EXAMPLE_RESPONSE_SECRET", "EXAMPLE_ERROR_SECRET", "EXAMPLE_USER", "EXAMPLE_PASSWORD", "EXAMPLE_TOKEN"):
        assert marker not in public


@pytest.mark.parametrize("cause_kind", ["errno", "ssl"])
async def test_real_transport_mapping_preserves_numeric_causes_before_pool_clears_them(tmp_path, monkeypatch, cause_kind):
    from auto_agents_2.network import PublicHTTPTransport

    class FailingTLSStream(httpcore.AsyncMockStream):
        started = False

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            self.started = True
            assert server_hostname == "docs.example" and ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert ssl_context.check_hostname
            cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERT_SECRET")
            cause.verify_code = 20
            raise httpcore.ConnectError("EXAMPLE_CORE_TLS_SECRET") from cause

    stream = FailingTLSStream([])
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    async def dial(*args, **kwargs):
        if cause_kind == "ssl":
            return stream  # Real httpcore._connect must invoke start_tls next.
        cause = OSError(errno.ECONNREFUSED, "EXAMPLE_OS_SECRET")
        raise httpcore.ConnectError("EXAMPLE_CORE_TCP_SECRET") from cause
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: PublicHTTPTransport(trust_env=False))
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(SimpleNamespace(store=SimpleNamespace(root=tmp_path))).capture(URL)
    evidence = observed.value.evidence
    assert stream.started is (cause_kind == "ssl")
    assert evidence["error_type"] == "ConnectError" and evidence["category"] == "network"
    # httpcore1.0.9 re-raises the same error from None. Its trace has already
    # snapshotted these exact explicit causes, without retaining error text.
    assert evidence["os_errno"] == (1 if cause_kind == "ssl" else errno.ECONNREFUSED)
    assert evidence["connection_phase"] == ("origin_tls" if cause_kind == "ssl" else "public_connect")
    if cause_kind == "ssl":
        assert evidence["ssl_verify_code"] == 20
    else:
        assert "ssl_verify_code" not in evidence
    assert known_exception(observed.value, "provider_research").requirement.kind == "external_input"
    assert "EXAMPLE_" not in json.dumps(evidence) + str(observed.value)


def test_available_explicit_numeric_causes_are_retained_without_error_text():
    for cause_kind in ("errno", "ssl"):
        if cause_kind == "ssl":
            cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERT_SECRET")
            cause.verify_code = 20
        else:
            cause = OSError(errno.ECONNREFUSED, "EXAMPLE_OS_SECRET")
        core = httpcore.ConnectError("EXAMPLE_CORE_SECRET")
        core.__cause__ = cause
        mapped = httpx.ConnectError("EXAMPLE_HTTP_SECRET")
        mapped.__cause__ = core
        evidence = DocumentAcquisitionUnavailable(URL, mapped).evidence
        assert evidence["os_errno"] == (1 if cause_kind == "ssl" else errno.ECONNREFUSED)
        if cause_kind == "ssl":
            assert evidence["ssl_verify_code"] == 20
        assert "EXAMPLE_" not in json.dumps(evidence)


async def test_redirect_failure_binds_the_failed_hop_without_path_query(tmp_path, monkeypatch):
    destination = "https://other.example/EXAMPLE_TARGET_PATH?section=EXAMPLE_TARGET_QUERY"
    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": destination}) if len(requests) == 1 else httpx.Response(403)
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(SimpleNamespace(store=SimpleNamespace(root=tmp_path))).capture(URL)
    assert requests == [URL, destination]
    assert observed.value.evidence["source_origin"] == "https://other.example/"
    assert observed.value.evidence["source_url_hash"] == digest(destination) != digest(URL)
    assert "EXAMPLE_" not in json.dumps(observed.value.evidence)


async def test_redirect_close_failure_binds_old_attempt_not_unvalidated_location(tmp_path, monkeypatch):
    class BrokenClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

        async def aclose(self):
            raise httpx.CloseError("EXAMPLE_CLOSE_SECRET")

    requests = []
    def respond(request):
        requests.append(str(request.url))
        assert len(requests) == 1, "Unvalidated Location must never be requested"
        return httpx.Response(302, headers={"location": "https://EXAMPLE_USER:EXAMPLE_PASSWORD@unvisited.example/EXAMPLE_PATH"}, stream=BrokenClose())
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(SimpleNamespace(store=SimpleNamespace(root=tmp_path))).capture(URL)
    assert requests == [URL]
    assert observed.value.evidence["source_origin"] == "https://docs.example/"
    assert observed.value.evidence["source_url_hash"] == digest(URL)
    assert observed.value.evidence["error_type"] == "CloseError"
    assert "EXAMPLE_" not in json.dumps(observed.value.evidence) + str(observed.value)


@pytest.mark.parametrize("kind", ["resolver", "deadline", "destination", "cancel", "unknown"])
async def test_non_document_http_boundaries_keep_their_original_semantics(tmp_path, monkeypatch, kind):
    errors = {
        "resolver": ResearchResolverUnavailable(category="network", error_type="ConnectError", endpoint_origin="https://resolver.example/", endpoint_url_hash="EXAMPLE_HASH"),
        "deadline": DocumentAcquisitionTimeout("Research download", 30, source_url=URL),
        "destination": ResearchDestinationRejected("docs.example"),
        "cancel": asyncio.CancelledError(), "unknown": RuntimeError("EXAMPLE_PROGRAM_BUG"),
    }
    error = errors[kind]
    def respond(request):
        raise error
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    with pytest.raises(type(error)) as observed:
        await ResearchService(SimpleNamespace(store=SimpleNamespace(root=tmp_path))).capture(URL)
    assert observed.value is error
    if kind in {"unknown", "cancel"}:
        assert known_exception(error, "provider_research") is None
    else:
        assert known_exception(error, "provider_research").reason_code == error.code


@pytest.mark.parametrize("kind", ["http", "parser"])
async def test_collection_failures_stay_outside_the_http_boundary(tmp_path, monkeypatch, kind):
    error = httpx.ConnectError("EXAMPLE_COLLECTION_BUG") if kind == "http" else ValueError("EXAMPLE_PARSER_BUG")
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(lambda request: httpx.Response(200, text="complete document")))
    async def collect(*args, **kwargs):
        raise error
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    with pytest.raises(type(error)) as observed:
        await ResearchService(SimpleNamespace(store=SimpleNamespace(root=tmp_path))).capture(URL)
    assert observed.value is error and known_exception(error, "provider_research") is None


async def test_successful_capture_still_publishes_a_real_collected_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text="<h1>Fixture</h1><p>complete document</p>", headers={"content-type": "text/html"})))
    artifacts = ArtifactStore(Store(tmp_path).root)
    result = await ResearchService(artifacts).capture(URL)
    assert result["artifact"]["hash"] and "complete document" in result["excerpt"]
    assert "network_policy" not in result


@pytest.mark.parametrize("kind", ["connect", "read", "403"])
async def test_document_http_failure_keeps_original_claims_and_agent_blocks_necessary_gap(tmp_path, monkeypatch, kind):
    from test_workflow import CodingAgent, engine

    from auto_agents_2.models import DocumentResult, SourceEvidence
    from auto_agents_2.research_acquisition import (
        CONTEXT,
        ResearchAcquisitionRecovery,
        ResearchRecoveryDocument,
    )

    class ResearchAgent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                self.calls.append((stage, fresh))
                if CONTEXT in context:
                    recovery = context[CONTEXT]
                    return ResearchRecoveryDocument(summary="Necessary original fact is unavailable",
                        document="Preserve the original fact and requirement.", sources=[],
                        acquisition_resolution={"episode_id": recovery["episode_id"], "status": "blocked",
                            "claims": [{"claim_id": row["id"], "disposition": "blocked", "source_indexes": [],
                                "requirement_ids": ["R1"], "reason": "No independent authoritative replacement exists"}
                                for row in recovery["claims"]],
                            "missing_information": ["Authoritative material for the original required fact"]})
                return DocumentResult(summary="research", document="# Research\nRetain candidate.",
                    sources=[SourceEvidence(url=URL, claim="fixture")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    requests = []
    def respond(request):
        requests.append(str(request.url))
        if kind == "403":
            return httpx.Response(403, text="EXAMPLE_DENIED_BODY")
        raise (httpx.ConnectError if kind == "connect" else httpx.ReadError)("EXAMPLE_DOCUMENT_ERROR")
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers.")
    agent = ResearchAgent()
    workflow = engine(project, agent)
    questions = []
    async def pause(question):
        questions.append(question)
        assert question["key"].startswith("prerequisite/")
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    fault = workflow.store.get(rid, "recovery/current-fault")
    assert fault["route"] == "human" and fault["reason_code"] == "research_acquisition_blocked"
    record = ResearchAcquisitionRecovery(workflow).active(rid)
    assert record["attempts"][0]["observations"][0]["failure"]["diagnostic"]["source_url_hash"] == digest(URL)
    assert record["claims"][0]["source"]["claim"] == "fixture"
    assert workflow.store.get(rid, "candidate/provider_research") and workflow.store.get(rid, "provider_research") is None
    assert [stage for stage, _ in agent.calls].count("provider_research") == 2
    assert len(questions) == 1 and requests == [URL]
