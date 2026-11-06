import asyncio
import json

import httpcore
import httpx
import pytest
from test_research_resolver import policy

from auto_agents_2.errors import ResearchResolverUnavailable
from auto_agents_2.faults import known_exception
from auto_agents_2.network import PublicNetworkBackend
from auto_agents_2.network_proxy import ProxySettings
from auto_agents_2.research_dns import HTTPSJSONResolver
from auto_agents_2.util import digest


@pytest.mark.parametrize("kind,category,status", [
    ("HTTPStatusError", "http_status", 403), ("HTTPStatusError", "http_status", 429),
    ("HTTPStatusError", "http_status", 503), ("ProxyError", "proxy", None),
    ("ConnectTimeout", "timeout", None), ("ReadTimeout", "timeout", None),
    ("ConnectError", "network", None), ("ReadError", "network", None),
    ("RemoteProtocolError", "protocol", None), ("HTTPError", "http", None),
])
async def test_http_failure_preserves_safe_type_and_routes_without_target_dial(monkeypatch, kind, category, status):
    query_urls = []
    selected = policy()
    selected.endpoint = "https://resolver.example/EXAMPLE_PRIVATE_ENDPOINT"
    def respond(request):
        query_urls.append(str(request.url))
        if status is not None:
            return httpx.Response(status, text="EXAMPLE_RESPONSE_SECRET")
        raise getattr(httpx, kind)("EXAMPLE_EXCEPTION_SECRET https://EXAMPLE_USER:EXAMPLE_PASSWORD@private.invalid/?token=EXAMPLE_TOKEN")
    monkeypatch.setattr("auto_agents_2.network.PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    async def forbidden(*args, **kwargs):
        pytest.fail("Resolver failure must never open a target socket or use system DNS")
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", forbidden)
    resolver = HTTPSJSONResolver(selected, ProxySettings(False))
    with pytest.raises(ResearchResolverUnavailable) as observed:
        await PublicNetworkBackend(resolver=resolver).connect_tcp("docs.example", 443, 1)
    error = observed.value
    expected = {"operation": "research_resolver", "category": category, "error_type": kind,
                "endpoint_origin": "https://resolver.example/", "endpoint_url_hash": digest(selected.endpoint)}
    if status is not None:
        expected["http_status"] = status
    assert error.evidence == expected and not resolver.connections
    boundary = known_exception(error, "provider_research")
    assert boundary.reason_code == error.code and boundary.requirement.kind == "external_input"
    assert boundary.requirement.evidence == expected
    public = str(error) + repr(error.__dict__) + boundary.requirement.model_dump_json()
    for marker in ("EXAMPLE_PRIVATE_ENDPOINT", "EXAMPLE_RESPONSE_SECRET", "EXAMPLE_EXCEPTION_SECRET", "EXAMPLE_USER", "EXAMPLE_PASSWORD", "EXAMPLE_TOKEN"):
        assert marker not in public
    assert query_urls and error.__cause__ is None and error.__suppress_context__


async def test_http_failure_cancels_the_other_family_before_any_target_connect(monkeypatch):
    sibling_started, sibling_drained = asyncio.Event(), asyncio.Event()
    async def respond(request):
        if request.url.params["type"] == "1":
            await sibling_started.wait()
            return httpx.Response(503, text="EXAMPLE_UNAVAILABLE")
        sibling_started.set()
        try:
            await asyncio.Future()
        finally:
            sibling_drained.set()
    monkeypatch.setattr("auto_agents_2.network.PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    async def forbidden(*args, **kwargs):
        pytest.fail("Failed A/AAAA union must never be dialled")
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    backend = PublicNetworkBackend(resolver=HTTPSJSONResolver(policy(), ProxySettings(False)))
    with pytest.raises(ResearchResolverUnavailable):
        await backend.connect_tcp("docs.example", 443, 1)
    assert sibling_drained.is_set()


@pytest.mark.parametrize("status", [403, 503])
async def test_resolver_failure_preserves_refusal_or_returns_to_research(tmp_path, monkeypatch, status):
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.models import DocumentResult, SourceEvidence
    from auto_agents_2.research_acquisition import CURRENT, ResearchRecoveryDocument

    class ResearchAgent(ApprovingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "provider_research":
                self.calls.append((stage, fresh))
                if status == 503 and "controller_research_acquisition" in context:
                    recovery = context["controller_research_acquisition"]
                    return ResearchRecoveryDocument(summary="Resolver still unavailable", document="Preserve requirements and original claims.",
                        sources=[], acquisition_resolution={"episode_id": recovery["episode_id"], "status": "blocked",
                            "claims": [{"claim_id": row["id"], "disposition": "blocked", "source_indexes": [],
                                "requirement_ids": ["R1"], "reason": "The necessary fact has no acquired authoritative evidence"}
                                for row in recovery["claims"]],
                            "missing_information": ["Working approved resolver route or supplied authoritative material"]})
                return DocumentResult(summary="research", document="# Research\nRetain candidate.",
                    sources=[SourceEvidence(url="https://docs.example/reference", claim="fixture")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    monkeypatch.setattr("auto_agents_2.network.PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(status, text="EXAMPLE_NOT_A_SUCCESS")))
    async def forbidden(*args, **kwargs):
        pytest.fail("The failing resolver must not dial a target")
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers.")
    agent = ResearchAgent()
    workflow = engine(project, agent)
    body = json.loads(workflow.config.path.read_text())
    body["research_resolver"] = policy().model_dump(mode="json")
    workflow.config.path.write_text(json.dumps(body))
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
    assert fault["route"] == "human"
    assert workflow.store.get(rid, "candidate/provider_research")
    assert workflow.store.get(rid, "provider_research") is None
    authors = [stage for stage, _ in agent.calls].count("provider_research")
    if status == 403:
        assert fault["reason_code"] == "RESEARCH_RESOLVER_UNAVAILABLE"
        assert fault["human_requirement"]["evidence"]["http_status"] == 403 and authors == 1
        assert not workflow.store.get(rid, CURRENT)
    else:
        assert fault["reason_code"] == "research_acquisition_blocked" and authors == 2
        record = workflow.store.get(rid, workflow.store.get(rid, CURRENT)["key"])
        failure = record["attempts"][0]["observations"][0]["failure"]
        assert failure["code"] == "RESEARCH_RESOLVER_UNAVAILABLE" and failure["diagnostic"]["http_status"] == 503
        assert not record.get("completed") and workflow.store.get(rid, "recovery/gate-history/0")
        assert "supplied authoritative material" in questions[-1]["prompt"]
    assert len(questions) == 1
