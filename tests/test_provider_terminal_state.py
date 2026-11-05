"""Codex terminal boundaries through the managed subprocess and strict parser."""
import json
import sys

import pytest
from test_runtime import configured

from auto_agents_2.errors import InvalidStructuredResponse, ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.providers import OUTPUT_LIMIT_DIAGNOSTIC, EventDecoder, ProviderRouter
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


def fixture_router(tmp_path, events, *, file_response=None, exit_code=0, kind="codex", retries=0, stderr_text=""):
    project = tmp_path / "project"
    project.mkdir()
    config, store = configured(project)
    native = tmp_path / "native"
    native.mkdir()
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\nEVENTS = {events!r}\n"
                      f"FILE_RESPONSE = {file_response!r}\nEXIT_CODE = {exit_code!r}\nSTDERR = {stderr_text!r}\n" + '''
import json, pathlib, sys
args = sys.argv[1:]
sys.stdin.read()
if FILE_RESPONSE is not None:
    pathlib.Path(args[args.index("-o") + 1]).write_text(FILE_RESPONSE)
for event in EVENTS:
    print(json.dumps(event), flush=True)
sys.stderr.write(STDERR)
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


OUTPUT_LIMIT_MESSAGE = "Incomplete response returned, reason: max_output_tokens"
OUTPUT_LIMIT = {"type": "turn.failed", "error": {
    "message": OUTPUT_LIMIT_MESSAGE, "codexErrorInfo": "other", "additionalDetails": None}}


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("details,attempts", [
    ("EXAMPLE_PRIVATE_DETAILS", 1),
    ("connection timeout EXAMPLE_PRIVATE_DETAILS", 2),
    ("context_window_exceeded connection EXAMPLE_PRIVATE_DETAILS", 1),
])
async def test_typed_output_limit_is_diagnostic_only_with_existing_retry_rules(tmp_path, exit_code, details, attempts):
    failure = {**OUTPUT_LIMIT, "error": {**OUTPUT_LIMIT["error"], "additionalDetails": details}}
    router, project, store, rid = fixture_router(
        tmp_path, [START, TURN, failure], file_response=TEXT, exit_code=exit_code, retries=1)
    with pytest.raises(ProviderError, match="max_output_tokens") as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert type(caught.value) is ProviderError
    assert store.get(rid, "provider-result/review") is None
    assert store.get(rid, "invocation/review") is None
    state = store.get(rid, "infra/review/codex")
    assert state["error_kind"] == "unavailable" and state["failures"] == attempts
    assert state["grant"] == 0 and not state["pending"]
    history = store.events(rid)
    assert len([event for event in history if event["kind"] == "agent.started"]) == attempts
    finished = [event["data"] for event in history if event["kind"] == "agent.finished"]
    assert len(finished) == attempts
    assert all(event["diagnostic"] == OUTPUT_LIMIT_DIAGNOSTIC and event["outcome"] == "error"
               and not event["complete"] for event in finished)
    assert "EXAMPLE_PRIVATE_DETAILS" not in str(caught.value) + json.dumps(history) + json.dumps(state)
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("error", [
    OUTPUT_LIMIT_MESSAGE,
    {"message": "prefix " + OUTPUT_LIMIT_MESSAGE},
    {"message": OUTPUT_LIMIT_MESSAGE + " suffix"},
    {"message": {"message": OUTPUT_LIMIT_MESSAGE}},
    {"details": {"message": OUTPUT_LIMIT_MESSAGE}},
])
def test_output_limit_requires_exact_typed_terminal_message(error):
    decoder = EventDecoder("codex")
    for event in (START, TURN, {"type": "turn.failed", "error": error}):
        decoder.feed(event)
    assert decoder.error and not decoder.finished and not decoder.failure_diagnostic


@pytest.mark.parametrize("events,stderr_text", [
    ([START, TURN, {"type": "item.completed", "item": {
        "type": "agent_message", "text": OUTPUT_LIMIT_MESSAGE}}], ""),
    ([START, TURN], OUTPUT_LIMIT_MESSAGE),
])
async def test_output_limit_text_outside_typed_error_has_no_terminal_label(tmp_path, events, stderr_text):
    router, project, store, rid = fixture_router(
        tmp_path, events, file_response=TEXT, stderr_text=stderr_text)
    with pytest.raises(ProviderError) as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert type(caught.value) is ProviderError and "max_output_tokens" not in str(caught.value)
    assert store.get(rid, "provider-result/review") is None
    finished = next(event["data"] for event in store.events(rid) if event["kind"] == "agent.finished")
    assert finished["diagnostic"] != OUTPUT_LIMIT_DIAGNOSTIC


async def test_output_limit_notification_can_complete_successfully(tmp_path):
    notice = {"type": "error", "error": OUTPUT_LIMIT["error"]}
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, notice, MESSAGE, COMPLETE])
    result = await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert result.model_dump(mode="json") == REVIEW
    assert store.get(rid, "infra/review/codex") == {"pending": False, "failures": 0, "grant": 0}
    finished = next(event["data"] for event in store.events(rid) if event["kind"] == "agent.finished")
    assert finished["outcome"] == "success" and finished["diagnostic"] == ""


@pytest.mark.parametrize("events", [
    [START, TURN, FAILED, OUTPUT_LIMIT],
    [START, TURN, TURN, OUTPUT_LIMIT],
    [START, TURN, OUTPUT_LIMIT, COMPLETE],
    [START, TURN, MESSAGE, COMPLETE, OUTPUT_LIMIT],
    [START, TURN, OUTPUT_LIMIT, ERROR],
])
async def test_output_limit_does_not_override_prior_or_contradictory_terminal_errors(tmp_path, events):
    decoder = EventDecoder("codex")
    for event in events:
        decoder.feed(event)
    assert decoder.error and not decoder.finished and not decoder.failure_diagnostic
    if events[2] == FAILED:
        assert "context_window_exceeded" in decoder.error
    router, project, store, rid = fixture_router(tmp_path, events, file_response=TEXT)
    with pytest.raises(ProviderError) as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert type(caught.value) is ProviderError
    assert store.get(rid, "provider-result/review") is None
    finished = next(event["data"] for event in store.events(rid) if event["kind"] == "agent.finished")
    assert finished["diagnostic"] != OUTPUT_LIMIT_DIAGNOSTIC


@pytest.mark.parametrize("legacy_checkpoint", [False, True])
async def test_output_limit_diagnostic_does_not_rewrite_exhausted_checkpoint_on_restart(tmp_path, legacy_checkpoint):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, OUTPUT_LIMIT], file_response=TEXT)
    key = "infra/review/codex"
    if legacy_checkpoint:
        store.checkpoint(rid, key, {"failures": 2, "grant": 0, "pending": False,
                                    "error_kind": "unavailable", "error_input": "legacy-input"})
    else:
        with pytest.raises(ProviderError, match="max_output_tokens"):
            await router.invoke("review", {}, ReviewResult, project, rid, "review")
    saved = store.get(rid, key)
    restarted = ProviderRouter(router.config, store, router.sandbox)
    with pytest.raises(ProviderError, match="persisted retry budget exhausted") as caught:
        await restarted.invoke("review", {}, ReviewResult, project, rid, "review")
    assert type(caught.value) is ProviderError and store.get(rid, key) == saved
    assert len([event for event in store.events(rid) if event["kind"] == "agent.started"]) == (0 if legacy_checkpoint else 1)
    assert store.get(rid, "provider-result/review") is None
