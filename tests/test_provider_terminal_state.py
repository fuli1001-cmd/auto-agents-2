"""Codex terminal boundaries through the managed subprocess and strict parser."""
import json
import sys

import pytest
from test_runtime import configured

from auto_agents_2.errors import InvalidStructuredResponse, ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.providers import EventDecoder, ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.transports import AntigravityTransport
from auto_agents_2.util import write_json

REVIEW = {
    "approved": False, "summary": "Fixture review rejected",
    "findings": [
        {"severity": "major", "location": f"fixture-{i}", "description": f"Unresolved issue {i}",
         "evidence": "Fixture observation", "resolution": "Repair and verify"}
        for i in range(4)
    ],
}
TEXT = json.dumps(REVIEW)
START = {"type": "thread.started", "thread_id": "fixture-session"}
TURN = {"type": "turn.started"}
MESSAGE = {"type": "item.completed", "item": {"type": "agent_message", "text": TEXT}}
COMPLETE = {"type": "turn.completed", "usage": {
    "input_tokens": 11, "output_tokens": 3, "cached_input_tokens": 7, "reasoning_output_tokens": 2}}
ERROR = {"type": "error", "message": "fixture stream error EXAMPLE_PLACEHOLDER"}
FAILED = {"type": "turn.failed", "error": {"message": "context_window_exceeded EXAMPLE_PLACEHOLDER"}}


def fixture_router(tmp_path, events, *, file_response=None, exit_code=0, kind="codex", retries=0):
    project = tmp_path / "project"
    project.mkdir()
    config, store = configured(project)
    native = tmp_path / "native"
    native.mkdir()
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\nEVENTS = {events!r}\n"
                      f"FILE_RESPONSE = {file_response!r}\nEXIT_CODE = {exit_code!r}\n" + '''
import json, pathlib, sys
args = sys.argv[1:]
sys.stdin.read()
if FILE_RESPONSE is not None:
    pathlib.Path(args[args.index("-o") + 1]).write_text(FILE_RESPONSE)
for event in EVENTS:
    print(json.dumps(event), flush=True)
sys.exit(EXIT_CODE)
''')
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["providers"] = {kind: body["providers"][kind]}
    body["active_provider"] = kind
    body["providers"][kind].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    body["execution"]["infra_retries"] = retries
    write_json(config.path, body)
    rid = store.admit("terminal fixture", {})["id"]
    transports = {}
    if kind == "antigravity":
        adapter = AntigravityTransport()
        adapter.native_home = lambda _: native
        transports[kind] = adapter
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True),
                            transports=transports)
    return router, project, store, rid


@pytest.mark.parametrize("response_source", ["stream", "file"])
async def test_recovered_stream_error_returns_rejected_review_once_with_usage(tmp_path, response_source):
    events = [START, TURN, ERROR, ERROR]
    if response_source == "stream":
        events.append(MESSAGE)
    events.append(COMPLETE)
    router, project, store, rid = fixture_router(
        tmp_path, events, file_response=TEXT if response_source == "file" else None, retries=1)
    store.checkpoint(rid, "infra/review/codex", {"pending": False, "failures": 1, "grant": 2})
    result = await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert result.model_dump(mode="json") == REVIEW
    assert not result.approved and len(result.findings) == 4
    state = store.get(rid, "infra/review/codex")
    assert state == {"pending": False, "failures": 1, "grant": 2}
    receipt = store.get(rid, "provider-result/review")
    assert receipt and store.get(rid, "invocation/review")["result"] == REVIEW
    assert receipt["usage"]["total_tokens"] == 14
    assert receipt["usage"]["cached_tokens"] == 7 and receipt["usage"]["reasoning_tokens"] == 2
    assert store.usage_summary(rid)["total_tokens"] == 14
    events = store.events(rid)
    assert len([e for e in events if e["kind"] == "agent.started"]) == 1
    assert not any(e["kind"] == "agent.retrying" for e in events)
    finished = next(e["data"] for e in events if e["kind"] == "agent.finished")
    assert finished["outcome"] == "success" and finished["diagnostic"] == ""
    assert "EXAMPLE_PLACEHOLDER" not in json.dumps(events)
    assert not list((store.root / "processes").glob("*.json"))
    assert not list((store.root / "attempts" / rid).iterdir())


@pytest.mark.parametrize("events,exit_code", [
    pytest.param([START, TURN, ERROR, MESSAGE], 0, id="missing-terminal"),
    pytest.param([START, TURN, ERROR, MESSAGE, COMPLETE], 7, id="nonzero-exit"),
    pytest.param([START, TURN, FAILED], 0, id="terminal-failure-with-valid-file"),
    pytest.param([START, TURN, FAILED, MESSAGE, COMPLETE], 0, id="failure-then-complete"),
    pytest.param([START, TURN, {"type": "turn.failed", "error": ""}, COMPLETE], 0, id="empty-terminal-failure"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, FAILED], 0, id="complete-then-failure"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, ERROR], 0, id="complete-then-error"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, ERROR, COMPLETE], 0, id="complete-error-complete"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, TURN], 0, id="new-turn-after-complete"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, TURN, COMPLETE], 0, id="second-turn-old-file"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, START, COMPLETE], 0, id="replayed-thread"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, MESSAGE], 0, id="message-after-complete"),
    pytest.param([START, TURN, MESSAGE, COMPLETE, COMPLETE], 0, id="duplicate-complete"),
    pytest.param([START, TURN, TURN, MESSAGE, COMPLETE], 0, id="duplicate-turn-start"),
    pytest.param([START, MESSAGE, TURN, COMPLETE], 0, id="old-message-before-turn"),
])
async def test_invalid_terminal_sequence_never_accepts_valid_response(tmp_path, events, exit_code):
    router, project, store, rid = fixture_router(tmp_path, events, file_response=TEXT, exit_code=exit_code)
    with pytest.raises(ProviderError) as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert not isinstance(caught.value, InvalidStructuredResponse)
    assert store.get(rid, "provider-result/review") is None
    assert store.get(rid, "invocation/review") is None
    state = store.get(rid, "infra/review/codex")
    assert state["failures"] == 1 and state["error_kind"] == "unavailable" and not state["pending"]
    expected_tokens = sum(14 for e in events if e["type"] == "turn.completed")
    assert store.usage_summary(rid)["total_tokens"] == expected_tokens
    history = store.events(rid)
    assert len([e for e in history if e["kind"] == "agent.started"]) == 1
    assert "EXAMPLE_PLACEHOLDER" not in str(caught.value) + json.dumps(history)
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("response", [
    pytest.param(None, id="missing"),
    pytest.param("", id="empty"),
    pytest.param("{", id="malformed-json"),
    pytest.param(json.dumps({"approved": False, "summary": "missing findings"}), id="missing-required"),
    pytest.param(json.dumps({**REVIEW, "approved": "false"}), id="no-bool-coercion"),
    pytest.param(json.dumps({**REVIEW, "extra": True}), id="extra-field"),
])
async def test_recovered_error_still_requires_strict_final_response(tmp_path, response):
    events = [START, TURN, ERROR, COMPLETE]
    router, project, store, rid = fixture_router(tmp_path, events, file_response=response, retries=1)
    with pytest.raises(InvalidStructuredResponse):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    with pytest.raises(InvalidStructuredResponse, match="saved response"):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert store.get(rid, "provider-result/review") is None
    state = store.get(rid, "infra/review/codex")
    assert state["failures"] == 1 and state["error_kind"] == "invalid_structured_response"
    assert len([e for e in store.events(rid) if e["kind"] == "agent.started"]) == 1
    assert store.usage_summary(rid)["total_tokens"] == 14


def test_stream_diagnostic_survives_recovery_without_erasing_terminal_failure():
    decoder = EventDecoder("codex")
    for event in (START, TURN, ERROR, MESSAGE, COMPLETE):
        decoder.feed(event)
    assert decoder.finished and not decoder.error
    assert decoder.stream_error == ERROR["message"]
    failed = EventDecoder("codex")
    for event in (START, TURN, ERROR, FAILED, ERROR, COMPLETE):
        failed.feed(event)
    assert failed.error and "context_window_exceeded" in failed.error
    assert not failed.finished and failed.usage.total_tokens == 14


@pytest.mark.parametrize("failure", [
    {"status": "ERROR", "error": "fixture terminal failure"},
    {"status": "SUCCESS", "denied_actions": ["fixture denied action"]},
])
async def test_antigravity_terminal_failure_cannot_be_recovered_by_later_valid_result(tmp_path, failure):
    def result(fields, total):
        return {"event": "result", "result": {
            "status": "SUCCESS", "structured_output": REVIEW,
            "usage": {"input_tokens": total - 3, "output_tokens": 3, "total_tokens": total}, **fields}}
    events = [result(failure, 14), result({}, 20)]
    router, project, store, rid = fixture_router(tmp_path, events, kind="antigravity")
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert store.get(rid, "provider-result/review") is None
    assert store.get(rid, "infra/review/antigravity")["failures"] == 1
    assert store.usage_summary(rid)["total_tokens"] == 20


@pytest.mark.parametrize("message,diagnostic,attempts", [
    ("context_window_exceeded connection timeout", "provider 会话超过模型上下文容量", 1),
    ("model_not_found connection timeout", "provider 所选模型不可用或不受支持", 1),
    ("connection timeout", "provider 连接异常", 2),
])
async def test_unrecovered_stream_diagnostic_preserves_retry_classification(tmp_path, message, diagnostic, attempts):
    events = [START, TURN, {"type": "error", "message": message + " EXAMPLE_PLACEHOLDER"}, COMPLETE]
    router, project, store, rid = fixture_router(tmp_path, events, file_response=TEXT, exit_code=1, retries=1)
    with pytest.raises(ProviderError, match=diagnostic) as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    history = store.events(rid)
    assert len([e for e in history if e["kind"] == "agent.started"]) == attempts
    assert all(e["data"]["diagnostic"] == diagnostic for e in history if e["kind"] == "agent.finished")
    assert store.get(rid, "provider-result/review") is None
    assert store.get(rid, "infra/review/codex")["failures"] == attempts
    assert store.usage_summary(rid)["total_tokens"] == 14 * attempts
    assert "EXAMPLE_PLACEHOLDER" not in str(caught.value) + json.dumps(history)


@pytest.mark.parametrize("event", [{"type": "error"}, {"type": "error", "message": ""}])
def test_stream_error_without_text_is_still_a_known_error(event):
    decoder = EventDecoder("codex")
    decoder.feed(event)
    assert decoder.stream_error and not decoder.finished
    decoder.feed(COMPLETE)
    assert decoder.finished and not decoder.error and decoder.stream_error
