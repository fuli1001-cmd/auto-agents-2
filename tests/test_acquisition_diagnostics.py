import asyncio

import httpx
import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.faults import InternalWorkflowError
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
    assert stream.closed
    assert not list(artifacts.root.glob("*/*"))  # Partial input is not published as evidence.


@pytest.mark.parametrize("failure,expected", [(TimeoutError(), "TimeoutError"),
                                              (RuntimeError("  "), "RuntimeError"),
                                              (RuntimeError("EXAMPLE actionable message"), "RuntimeError")])
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
