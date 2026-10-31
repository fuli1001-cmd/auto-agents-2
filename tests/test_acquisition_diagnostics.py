import asyncio

import httpx
import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import AA2Error, ResearchDestinationRejected
from auto_agents_2.faults import InternalWorkflowError, known_exception
from auto_agents_2.store import Store


@pytest.mark.parametrize("entrypoint", ["research", "requirements"])
async def test_download_timeout_identifies_operation_without_url_details(tmp_path, monkeypatch, entrypoint):
    from auto_agents_2 import requirements, research

    class StalledStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"partial document"
            await asyncio.Future()

        async def aclose(self):
            self.closed = True

    stream = StalledStream()
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))))
    module = research if entrypoint == "research" else requirements
    monkeypatch.setattr(module, "ACQUISITION_TIMEOUT", 0.02)
    artifacts = ArtifactStore(Store(tmp_path).root)
    url = "https://docs.example/EXAMPLE_PRIVATE_PATH?section=EXAMPLE_QUERY"
    with pytest.raises(TimeoutError) as failure:
        if entrypoint == "research":
            await research.ResearchService(artifacts).capture(url)
        else:
            await requirements.capture(url, artifacts)
    message = str(failure.value)
    assert f"{entrypoint.capitalize()} document download for docs.example" in message
    assert "0.02s" in message and "HTTP_PROXY/HTTPS_PROXY" in message
    assert "EXAMPLE_PRIVATE_PATH" not in message and "EXAMPLE_QUERY" not in message
    boundary = known_exception(failure.value, entrypoint)
    assert boundary.reason_code == "DOCUMENT_ACQUISITION_TIMEOUT"
    assert boundary.requirement.evidence["source_url_hash"]
    assert "EXAMPLE_PRIVATE_PATH" not in boundary.requirement.model_dump_json()
    assert "EXAMPLE_QUERY" not in boundary.requirement.model_dump_json()
    assert stream.closed
    assert not list(artifacts.root.glob("*/*"))  # Partial input is not published as evidence.


@pytest.mark.parametrize("failure,expected", [(TimeoutError(), "TimeoutError"),
                                              (RuntimeError("  "), "RuntimeError"),
                                              (RuntimeError("EXAMPLE actionable message"), "RuntimeError"),
                                              (AA2Error("Research destination must resolve exclusively to public addresses"),
                                               "AA2Error")])
async def test_unknown_incident_exits_with_a_reason_without_generic_question(tmp_path, failure, expected):
    workflow = engine(tmp_path, CodingAgent())

    async def fail(rid):
        raise failure

    async def pause(question):
        pytest.fail("Unknown internal exceptions must not create an authorization prompt")

    workflow._execute = fail
    workflow.question_handler = pause
    with pytest.raises(InternalWorkflowError, match=expected):
        await workflow.start("fixture")
    run = workflow.store.current()
    assert run["status"] == "PAUSED"
    assert not workflow.store.pending(run["id"])
    assert workflow.store.get(run["id"], "recovery/current-fault")["exception_type"] == expected


async def test_research_download_timeout_waits_and_resumes_retained_candidate(tmp_path, monkeypatch):
    from auto_agents_2 import research
    from auto_agents_2.models import DocumentResult, SourceEvidence
    from auto_agents_2.util import digest

    class ResearchAgent(CodingAgent):
        research_reviews = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                self.calls.append((stage, fresh))
                return DocumentResult(summary="research", document="# Research\nRetain this candidate.",
                    sources=[SourceEvidence(url="https://docs.example/reference", claim="fixture claim")])
            if context.get("review_kind") == "research":
                self.research_reviews += 1
                rows = context["retrieved_sources"]
                assert len(rows) == 1 and rows[0]["retrieval"]["artifact"]["hash"]
                assert "complete reference" in rows[0]["excerpt"]
            if stage == "design":
                assert self.research_reviews == 1
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    class StalledStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"partial reference"
            await asyncio.Future()

        async def aclose(self):
            self.closed = True

    stalled = True
    streams = []
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if stalled:
            stream = StalledStream()
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, text="complete reference", headers={"content-type": "text/plain"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(research, "ACQUISITION_TIMEOUT", 0.02)
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
        assert "HTTP_PROXY/HTTPS_PROXY" in question["prompt"]
        raise asyncio.CancelledError()

    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    candidate = workflow.store.get(rid, "candidate/provider_research")
    clarification = workflow.store.get(rid, "clarify")
    replans = workflow.store.run(rid)["data"].get("replans", 0)
    fault = workflow.store.get(rid, "recovery/current-fault")
    assert fault["route"] == "human" and fault["reason_code"] == "DOCUMENT_ACQUISITION_TIMEOUT"
    assert fault["human_requirement"]["evidence"]["source_url_hash"] == digest("https://docs.example/reference")
    assert workflow.store.get(rid, "provider_research") is None
    assert agent.research_reviews == 0 and all(stream.closed for stream in streams)

    # An answer is not successful retrieval: the still-broken source must block again.
    workflow.store.answer(questions[-1]["id"], "y", questions[-1]["artifact_hash"])
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(run_id=rid)
    assert len(questions) == 2 and questions[0]["id"] != questions[1]["id"]
    assert len(requests) >= 2 and all(stream.closed for stream in streams)
    assert workflow.store.get(rid, "provider_research") is None and agent.research_reviews == 0

    workflow.store.answer(questions[-1]["id"], "y", questions[-1]["artifact_hash"])
    stalled = False
    monkeypatch.setattr(research, "ACQUISITION_TIMEOUT", 30)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(run_id=rid)
    accepted = workflow.store.get(rid, "provider_research")
    assert accepted["verified_sources"][0]["retrieval"]["artifact"]["hash"]
    assert workflow.authority.accepted(rid, "provider_research", accepted)
    assert workflow.store.get(rid, "candidate/provider_research") == candidate
    assert workflow.store.get(rid, "clarify") == clarification
    assert workflow.store.run(rid)["data"].get("replans", 0) == replans
    assert [stage for stage, _ in agent.calls].count("clarify") == 1
    assert [stage for stage, _ in agent.calls].count("provider_research") == 1
    assert agent.research_reviews == 1


@pytest.mark.parametrize("failure", [TimeoutError("unrelated timeout"), asyncio.CancelledError()])
async def test_acquisition_deadline_preserves_unrelated_timeout_and_cancellation(failure):
    from auto_agents_2.errors import DocumentAcquisitionTimeout
    from auto_agents_2.faults import known_exception
    from auto_agents_2.http_body import acquisition_deadline

    with pytest.raises(type(failure)) as observed:
        async with acquisition_deadline(30, "Research document download for docs.example",
                                        source_url="https://docs.example/reference"):
            raise failure
    assert observed.value is failure
    assert not isinstance(observed.value, DocumentAcquisitionTimeout)
    assert known_exception(observed.value, "provider_research") is None


async def test_research_redirect_rejection_binds_failed_hop_without_url_details(tmp_path, monkeypatch):
    from test_research_proxy import RecordingStream, install_network

    from auto_agents_2 import research
    from auto_agents_2.util import digest

    original = "https://docs.example/EXAMPLE_ORIGINAL_PATH?section=EXAMPLE_ORIGINAL_QUERY"
    destination = "https://redirect.example/EXAMPLE_PRIVATE_PATH?section=EXAMPLE_QUERY"
    stream = RecordingStream([("HTTP/1.1 302 Found\r\nLocation: " + destination +
                               "\r\nContent-Length: 0\r\n\r\n").encode()])
    lookups, connections = install_network(monkeypatch, stream, {"redirect.example": ["198.18.0.22"]})
    transport = research.PublicHTTPTransport
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda: transport(trust_env=False))
    artifacts = ArtifactStore(Store(tmp_path).root)
    with pytest.raises(ResearchDestinationRejected) as failure:
        await research.ResearchService(artifacts).capture(original)
    boundary = known_exception(failure.value, "provider_research")
    assert boundary.requirement.evidence["hostname"] == "redirect.example"
    assert boundary.requirement.evidence["source_url_hash"] == digest(destination) != digest(original)
    assert "EXAMPLE_" not in boundary.requirement.model_dump_json()
    assert lookups == ["docs.example", "redirect.example"] and connections == [("93.184.216.34", 443)]
    assert stream.closed and not list(artifacts.root.glob("*/*"))


@pytest.mark.parametrize("denied", [[], ["198.18.0.22"], ["2001:2::22"], ["10.0.0.1"]])
async def test_research_dns_rejection_waits_and_rechecks_retained_candidate(tmp_path, monkeypatch, denied):
    from test_research_proxy import RecordingStream, install_network

    from auto_agents_2 import research
    from auto_agents_2.models import DocumentResult, SourceEvidence
    from auto_agents_2.util import digest

    source_url = "https://docs.example/EXAMPLE_PRIVATE_PATH?section=EXAMPLE_QUERY"
    class ResearchAgent(CodingAgent):
        research_reviews = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                self.calls.append((stage, fresh))
                return DocumentResult(summary="research", document="# Research\nRetain this candidate.",
                    sources=[SourceEvidence(url=source_url, claim="fixture claim")])
            if context.get("review_kind") == "research":
                self.research_reviews += 1
                rows = context["retrieved_sources"]
                assert len(rows) == 1 and rows[0]["retrieval"]["artifact"]["hash"]
                assert "complete reference" in rows[0]["excerpt"]
            if stage == "design":
                assert self.research_reviews == 1
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    addresses = {"docs.example": denied}
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Length: 18\r\nContent-Type: text/plain\r\n\r\ncomplete reference"])
    lookups, connections = install_network(monkeypatch, stream, addresses)
    transport = research.PublicHTTPTransport
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda: transport(trust_env=False))
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
        assert "DNS" in question["prompt"] and "EXAMPLE_" not in question["prompt"]
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    candidate = workflow.store.get(rid, "candidate/provider_research")
    clarification = workflow.store.get(rid, "clarify")
    replans = workflow.store.run(rid)["data"].get("replans", 0)
    fault = workflow.store.get(rid, "recovery/current-fault")
    assert fault["route"] == "human" and fault["reason_code"] == "RESEARCH_DESTINATION_REJECTED"
    assert fault["human_requirement"]["kind"] == "external_input"
    assert fault["human_requirement"]["evidence"] == {"operation": "research_connect",
        "hostname": "docs.example", "resolution": "non_public" if denied else "empty",
        "source_url_hash": digest(source_url)}
    assert workflow.store.get(rid, "provider_research") is None and agent.research_reviews == 0
    assert candidate["document"]["sources"][0]["url"] == source_url  # Candidate retains the original source.
    assert connections == [] and lookups == ["docs.example"]

    # Answering y cannot turn the unchanged DNS rejection into successful retrieval.
    workflow.store.answer(questions[-1]["id"], "y", questions[-1]["artifact_hash"])
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(run_id=rid)
    assert len(questions) == 2 and questions[0]["id"] != questions[1]["id"]
    # Resume observes the failure before consuming the saved y; the existing
    # prerequisite loop may then recheck it once more before asking again.
    rejected_lookups = len(lookups)
    assert rejected_lookups > 1 and lookups == ["docs.example"] * rejected_lookups and connections == []
    assert workflow.store.get(rid, "provider_research") is None and agent.research_reviews == 0
    assert workflow.store.get(rid, "candidate/provider_research") == candidate
    assert workflow.store.run(rid)["data"].get("replans", 0) == replans

    workflow.store.answer(questions[-1]["id"], "y", questions[-1]["artifact_hash"])
    addresses["docs.example"] = ["93.184.216.34"]  # A changed test fixture, never a production DNS setting.
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(run_id=rid)
    accepted = workflow.store.get(rid, "provider_research")
    assert accepted["verified_sources"][0]["retrieval"]["artifact"]["hash"]
    assert workflow.authority.accepted(rid, "provider_research", accepted)
    assert workflow.store.get(rid, "candidate/provider_research") == candidate
    assert workflow.store.get(rid, "clarify") == clarification
    assert workflow.store.run(rid)["data"].get("replans", 0) == replans
    assert [stage for stage, _ in agent.calls].count("clarify") == 1
    assert [stage for stage, _ in agent.calls].count("provider_research") == 1
    assert agent.research_reviews == 1
    assert lookups == ["docs.example"] * (rejected_lookups + 1) and connections == [("93.184.216.34", 443)]
    assert stream.closed
