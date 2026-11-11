"""Synthetic native failures preserve facts without provider content or new calls."""
import json
from types import SimpleNamespace

import pytest
from test_provider_terminal_state import COMPLETE, MESSAGE, REVIEW, START, TURN, fixture_router

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import ProviderError
from auto_agents_2.faults import known_exception
from auto_agents_2.models import ReviewResult
from auto_agents_2.provider_failure import (
    CODES,
    HTTP_FIELDS,
    LIMIT,
    RETRY_FIELDS,
    ProviderFailureFacts,
    denied_action,
    native_error,
)
from auto_agents_2.util import digest


def result(**fields):
    return {"event": "result", "result": {"status": "SUCCESS", "structured_output": REVIEW, **fields}}


def observations(store, rid):
    refs = [row["data"]["artifact"] for row in store.events(rid) if row["kind"] == "agent.failure"]
    return refs, [ArtifactStore(store.root).json(ref) for ref in refs]


@pytest.mark.parametrize("target", [
    "https://EXAMPLE_USER:EXAMPLE_PASSWORD@EXAMPLE_HOST/EXAMPLE_PATH?x=EXAMPLE_QUERY#EXAMPLE_FRAGMENT",
    "/home/EXAMPLE_USER/EXAMPLE_SECRET/config.json",
    r"C:\\Users\\EXAMPLE_USER\\EXAMPLE_SECRET\\auth.json",
])
def test_structured_denial_has_known_tool_reason_and_only_opaque_target(target):
    denied = {"tool_name": "read_file", "args": {"path": target,
              "CommandLine": "EXAMPLE_COMMAND", "content": "EXAMPLE_CONTENT",
              "env": {"EXAMPLE_KEY": "EXAMPLE_TOKEN"}}, "reason": "POLICY_DENIED",
              "extra": "EXAMPLE_EXTENSION"}
    safe = denied_action(denied)
    assert safe["tool"]["value"] == "read_file" and safe["reason"]["value"] == "POLICY_DENIED"
    assert safe["targets"][0]["sha256"] and safe["targets"][0]["field"] == "path"
    assert safe["fingerprint"]["sha256"] == digest(
        json.dumps(denied, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    assert "EXAMPLE_" not in json.dumps(safe)


def test_unknown_or_free_text_does_not_become_a_tool_or_reason_code():
    for entry in ("EXAMPLE_TEXT", {"tool_name": "read_file EXAMPLE_SUFFIX",
                  "reason": "permission denied EXAMPLE_REASON", "args": "EXAMPLE_ARGS"}):
        safe = denied_action(entry)
        assert safe["tool"]["value"] == safe["reason"]["value"] == "unknown"
        assert "EXAMPLE_" not in json.dumps(safe)


@pytest.mark.parametrize("failure", [
    {"status": "ERROR", "error": {"code": "UNAVAILABLE", "message": "EXAMPLE_ERROR"}},
    {"denied_actions": [{"tool_name": "read_file", "args": {"path": "/EXAMPLE_PATH"},
                        "reason": "POLICY_DENIED"}]},
    {"denied_actions": ["run_command"]},
])
async def test_sticky_failure_evidence_survives_success_cleanup_restart_and_outer_fault(tmp_path, failure):
    router, project, store, rid = fixture_router(tmp_path, [result(**failure), result()], kind="antigravity")
    caught = []
    for _ in range(2):
        with pytest.raises(ProviderError) as error:
            await router.invoke("review", {}, ReviewResult, project, rid, "review")
        caught.append(error.value)
    refs, facts = observations(store, rid)
    assert len(facts) == 1 and facts[0]["terminal_failure_count"] == 1
    fact = facts[0]
    assert fact["category"] == "terminal_failure" and fact["last_terminal"]["status"]["value"] == "SUCCESS"
    assert fact["run_id"] == rid and fact["stage"] == fact["operation"] == "review"
    assert fact["attempt"] and fact["returncode"] == 0 and fact["result_seen"]
    assert fact["terminal_failures"][0]["denied_actions_present"] == bool(failure.get("denied_actions"))
    if failure.get("denied_actions"):
        assert fact["terminal_failures"][0]["denied_actions"][0]["tool"]["value"] in {"read_file", "run_command"}
        assert "result.denied_actions" in fact["error_sources"]
    else:
        assert fact["terminal_failures"][0]["error"]["code"]["value"] == "UNAVAILABLE"
    for error in caught:
        boundary = known_exception(error, "review")
        assert boundary.requirement.evidence == error.evidence == {"provider_failures": refs}
        assert boundary.requirement.evidence_refs == [ref["hash"] for ref in refs]
    assert "EXAMPLE_" not in json.dumps(fact)
    state = store.get(rid, "infra/review/antigravity")
    assert state["failures"] == 1 and state["grant"] == 0 and not state["pending"]
    assert store.get(rid, "provider-result/review") is None
    assert not list((store.root / "attempts" / rid).iterdir())


async def test_stderr_nonjson_and_returncode_are_distinct_content_free_facts(tmp_path):
    router, project, store, rid = fixture_router(
        tmp_path, [START, TURN], exit_code=7, stderr_text="EXAMPLE_STDERR https://x/?a=EXAMPLE_QUERY")
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    _, facts = observations(store, rid)
    assert facts[0]["returncode"] == 7 and facts[0]["category"] == "nonzero_exit"
    assert not facts[0]["result_seen"] and facts[0]["streams"]["stderr"]["bytes"] > 0
    assert facts[0]["streams"]["stderr"]["sha256"] and "stderr" in facts[0]["error_sources"]
    assert "EXAMPLE_" not in json.dumps(facts)


async def test_launch_failure_has_safe_errno_and_evidence(tmp_path, monkeypatch):
    router, project, store, rid = fixture_router(tmp_path, [])
    async def fail(*args, **kwargs):
        raise OSError(13, "EXAMPLE_OS_ERROR /EXAMPLE_PATH")
    monkeypatch.setattr(router.sandbox, "spawn", fail)
    with pytest.raises(ProviderError) as error:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    _, facts = observations(store, rid)
    assert facts[0]["category"] == "start_failed" and facts[0]["phase"] == "launch"
    assert facts[0]["os_errno"] == 13 and facts[0]["returncode"] is None
    assert error.value.evidence and "EXAMPLE_" not in json.dumps(facts)


async def test_success_does_not_publish_failure_facts(tmp_path):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, MESSAGE, COMPLETE])
    assert (await router.invoke("review", {}, ReviewResult, project, rid, "review")).approved is False
    assert observations(store, rid) == ([], [])


def test_bounded_projection_retains_first_denial_and_marks_unknowns():
    facts = ProviderFailureFacts("antigravity")
    for _ in range(LIMIT + 1):
        facts.event(result(status="EXAMPLE_STATUS", denied_actions=[
            {"tool_name": "EXAMPLE_TOOL", "reason": "EXAMPLE_REASON"}] * (LIMIT + 1)))
    record = facts.record(run_id="run", attempt="attempt", stage="review", operation="review",
                          provider="antigravity", input_identity="identity",
                          process=SimpleNamespace(returncode=0),
                          decoder=SimpleNamespace(finished=False, error="opaque"),
                          phase="terminal_validation", error=None)
    assert record["terminal_failure_count"] == LIMIT + 1
    assert len(record["terminal_failures"]) == LIMIT and record["terminal_failures_truncated"]
    assert record["terminal_failures"][0]["denied_actions_truncated"]
    assert record["last_terminal"]["status"]["value"] == "unknown"
    assert "EXAMPLE_" not in json.dumps(record)


async def test_malformed_result_is_seen_without_changing_decoder_rejection(tmp_path):
    router, project, store, rid = fixture_router(
        tmp_path, [{"event": "result", "result": "EXAMPLE_MALFORMED"}], kind="antigravity")
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    _, facts = observations(store, rid)
    assert facts[0]["result_seen"] and facts[0]["terminal_count"] == 0
    assert "result.structure" in facts[0]["error_sources"]
    assert "EXAMPLE_" not in json.dumps(facts)


async def test_projector_error_cannot_turn_valid_success_into_failure(tmp_path, monkeypatch):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, MESSAGE, COMPLETE])
    def fail(*args):
        raise ValueError("EXAMPLE_PROJECTION")
    monkeypatch.setattr(ProviderFailureFacts, "event", fail)
    assert (await router.invoke("review", {}, ReviewResult, project, rid, "review")).approved is False
    assert observations(store, rid) == ([], [])


async def test_nonobject_stdout_is_fingerprinted_without_its_text(tmp_path):
    router, project, store, rid = fixture_router(tmp_path, ["EXAMPLE_NON_OBJECT"])
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    _, facts = observations(store, rid)
    assert facts[0]["streams"]["stdout_non_json"]["bytes"] > 0
    assert "stdout_non_json" in facts[0]["error_sources"] and not facts[0]["result_seen"]
    assert "EXAMPLE_" not in json.dumps(facts)


async def test_observation_storage_failure_preserves_original_failure_and_cleanup(tmp_path, monkeypatch):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN], exit_code=7)
    put = ArtifactStore.put
    def fail(self, value, kind="document"):
        if kind == "provider_failure":
            raise OSError("EXAMPLE_CAPTURE")
        return put(self, value, kind)
    monkeypatch.setattr(ArtifactStore, "put", fail)
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert observations(store, rid) == ([], [])
    history = store.events(rid)
    finished = next(row["data"] for row in history if row["kind"] == "agent.finished")
    assert finished["outcome"] == "error" and finished["failure_observation_status"] == "unavailable"
    assert store.get(rid, "infra/review/codex")["failures"] == 1
    assert not list((store.root / "processes").glob("*.json"))
    assert not list((store.root / "attempts" / rid).iterdir())
    assert "EXAMPLE_" not in json.dumps(history)


async def test_retry_failure_refs_accumulate_without_more_calls_after_exhaustion(tmp_path):
    events = [result(status="ERROR", error={"code": "UNAVAILABLE", "message": "connection EXAMPLE_ERROR"})]
    router, project, store, rid = fixture_router(tmp_path, events, kind="antigravity", retries=1)
    for _ in range(2):
        with pytest.raises(ProviderError) as error:
            await router.invoke("review", {}, ReviewResult, project, rid, "review")
        refs, facts = observations(store, rid)
        assert len(facts) == 2 and error.value.evidence == {"provider_failures": refs}
    state = store.get(rid, "infra/review/antigravity")
    assert state["failures"] == 2 and state["grant"] == 0 and not state["pending"]
    assert state["provider_failure_refs"] == refs


@pytest.mark.parametrize("action,expected", [
    ("run_command", "run_command"),
    ("EXAMPLE_UNKNOWN_ACTION --token=EXAMPLE_TOKEN", "unknown"),
])
def test_headless_action_display_shape_is_separate_from_tool_and_reason(action, expected):
    display = "EXAMPLE_COMMAND https://EXAMPLE_USER:EXAMPLE_PASSWORD@private.invalid/EXAMPLE_PATH?token=EXAMPLE_QUERY"
    safe = denied_action({"action": action, "display_name": display})
    assert safe["wire_shape"] == "headless_action_display"
    assert safe["headless_action"]["value"] == expected
    assert safe["display_name"]["type"] == "str" and safe["display_name"]["sha256"]
    assert safe["fields_present"]["action"] and safe["fields_present"]["display_name"]
    assert not safe["fields_present"]["args"] and not safe["fields_present"]["reason"]
    assert safe["tool"]["value"] == safe["reason"]["value"] == "unknown"
    assert safe["tool"]["type"] == safe["arguments"]["type"] == safe["reason"]["type"] == "NoneType"
    assert safe["targets"] == []
    assert "EXAMPLE_" not in json.dumps(safe) and "private.invalid" not in json.dumps(safe)


def test_headless_presence_distinguishes_omitted_fields_from_explicit_null():
    omitted = denied_action({"display_name": ""})
    nulls = denied_action({"action": None, "display_name": None, "args": None, "reason": None})
    assert omitted["wire_shape"] == nulls["wire_shape"] == "headless_action_display"
    assert not omitted["fields_present"]["action"] and nulls["fields_present"]["action"]
    assert not omitted["fields_present"]["args"] and nulls["fields_present"]["args"]
    assert not omitted["fields_present"]["reason"] and nulls["fields_present"]["reason"]
    assert omitted["headless_action"]["type"] == nulls["headless_action"]["type"] == "NoneType"
    assert nulls["display_name"]["type"] == "NoneType"


@pytest.mark.parametrize("tool_key", ["tool_name", "toolName"])
def test_internal_protobuf_snake_and_camel_projection_remains_compatible(tool_key):
    safe = denied_action({tool_key: "read_file", "args": {"path": "/EXAMPLE_PRIVATE_PATH"},
                          "reason": "POLICY_DENIED"})
    assert safe["tool"]["value"] == "read_file" and safe["reason"]["value"] == "POLICY_DENIED"
    assert safe["arguments"]["type"] == "dict" and safe["targets"][0]["field"] == "path"
    assert safe["fields_present"][tool_key] and safe["fields_present"]["args"]
    assert not safe["fields_present"]["action"] and "wire_shape" not in safe
    assert "EXAMPLE_" not in json.dumps(safe)


async def test_headless_success_with_denial_still_fails_once_and_keeps_safe_action_facts(tmp_path):
    events = [result(denied_actions=[{"action": "read_file", "display_name": "EXAMPLE_PRIVATE_DISPLAY"}])]
    router, project, store, rid = fixture_router(tmp_path, events, kind="antigravity")
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    _, facts = observations(store, rid)
    assert len(facts) == 1 and facts[0]["returncode"] == 0
    assert facts[0]["last_terminal"]["status"]["value"] == "SUCCESS"
    denial = facts[0]["terminal_failures"][0]["denied_actions"][0]
    assert denial["headless_action"]["value"] == "read_file" and denial["fields_present"]["display_name"]
    assert denial["reason"]["value"] == "unknown" and denial["targets"] == []
    assert store.get(rid, "provider-result/review") is None
    assert store.get(rid, "infra/review/antigravity")["failures"] == 1
    assert len([row for row in store.events(rid) if row["kind"] == "agent.started"]) == 1
    assert "EXAMPLE_" not in json.dumps(facts)




@pytest.mark.parametrize("action", [
    "read_file", "write_file", "read_url", "execute_url", "command", "unsandboxed", "mcp",
])
def test_headless_permission_categories_are_not_invented_canonical_tools(action):
    safe = denied_action({"action": action, "display_name": "EXAMPLE_PRIVATE_DISPLAY"})
    assert safe["headless_action"]["value"] == action
    assert safe["tool"]["value"] == safe["reason"]["value"] == "unknown"
    assert "reason_signals" not in safe and safe["targets"] == []
    assert "EXAMPLE_" not in json.dumps(safe)


@pytest.mark.parametrize("action", [
    "command(EXAMPLE_PRIVATE_COMMAND)", "mcp(EXAMPLE_SERVER/EXAMPLE_TOOL)",
    "read_url(EXAMPLE_HOST)", "command EXAMPLE_TOKEN", "Command", ["command"],
])
def test_headless_action_resources_and_unknown_values_stay_opaque(action):
    safe = denied_action({"action": action})
    assert safe["headless_action"]["value"] == "unknown"
    assert "EXAMPLE_" not in json.dumps(safe)


def test_denial_reason_markers_are_not_promoted_to_an_exact_reason():
    reason = "PERMISSION_DENIED permission EXAMPLE_REASON /EXAMPLE_PATH --token=EXAMPLE_TOKEN"
    safe = denied_action({"reason": reason})
    assert safe["reason"]["value"] == "unknown"
    assert safe["reason_signals"]["public_codes"] == ["PERMISSION_DENIED"]
    assert safe["reason_signals"]["keyword_signals"] == ["permission"]
    assert safe["reason_signals"]["authority"] == "native_text_or_field_markers_only"
    assert "EXAMPLE_" not in json.dumps(safe)
    # Preserve exact-code priority even if a different free-text field looks informative.
    safe = denied_action({"reason_code": None, "reason": reason})
    assert safe["reason"]["value"] == "unknown" and not safe["reason_signals"]["public_codes"]


def tool_error_event(error=None, **step):
    return {"event": "step_update", "step_update": {
        "step_type": "tool", "tool_name": "run_command", "state": "DONE",
        "tool_info": {"name": "run_command", "error": error,
                      "parameters": {"CommandLine": "EXAMPLE_COMMAND --token=EXAMPLE_TOKEN"},
                      "output": "EXAMPLE_PRIVATE_OUTPUT"}, **step}}


def test_native_tool_error_identifiers_and_reason_are_independent_safe_observations():
    facts = ProviderFailureFacts("antigravity")
    facts.event(tool_error_event({"type": "PERMISSION_DENIED",
        "message": "access denied EXAMPLE_REASON https://EXAMPLE_USER:EXAMPLE_PASSWORD@EXAMPLE_HOST/EXAMPLE_PATH",
        "headers": {"Authorization": "EXAMPLE_TOKEN"}}, tool_name="read_file"))
    assert facts.tool_error_count == 1 and facts.failure_count == facts.terminal_count == 0
    item = facts.tool_errors[0]
    assert item["tool"]["value"] == "read_file"
    assert item["tool_info_name"]["value"] == "run_command"  # Do not silently reconcile a mismatch.
    assert item["error"]["code"]["value"] == "PERMISSION_DENIED"
    assert item["error"]["observed_signals"]["keyword_signals"] == ["permission"]
    assert facts.sources == {"step_update.tool_info.error"}
    assert "EXAMPLE_" not in json.dumps(facts.tool_errors)


@pytest.mark.parametrize("step", [None, [], "EXAMPLE_STEP", {},
    {"step_type": "agent_response", "tool_info": {"error": "EXAMPLE_ERROR"}},
    {"step_type": "tool", "tool_info": "EXAMPLE_INFO"},
    {"step_type": "tool", "tool_info": {}},
    {"step_type": "tool", "tool_info": {"error": None}},
])
def test_unrelated_or_absent_tool_error_is_not_a_failure_observation(step):
    facts = ProviderFailureFacts("antigravity")
    facts.event({"event": "step_update", "step_update": step})
    assert facts.tool_error_count == facts.error_count == facts.failure_count == 0
    assert not facts.sources


def test_tool_error_observations_are_bounded_count_events_and_leave_unknowns_opaque():
    facts = ProviderFailureFacts("antigravity")
    event = tool_error_event(tool_name="EXAMPLE_TOOL", tool_info={
        "name": "EXAMPLE_OTHER_TOOL", "error": {"type": "EXAMPLE_ERROR", "message": "EXAMPLE_MESSAGE",
        "payload": {"message": "PERMISSION_DENIED"}}})
    for _ in range(LIMIT + 1):
        facts.event(event)  # Repeated step updates are not distinct denied operations.
    record = facts.record(run_id="run", attempt="attempt", stage="review", operation="review",
                          provider="antigravity", input_identity="identity",
                          process=SimpleNamespace(returncode=0),
                          decoder=SimpleNamespace(finished=False, error="opaque"),
                          phase="terminal_validation", error=None)
    assert record["tool_error_count"] == LIMIT + 1
    assert len(record["tool_errors"]) == LIMIT and record["tool_errors_truncated"]
    assert record["terminal_failure_count"] == 0
    item = record["tool_errors"][0]
    assert item["tool"]["value"] == item["tool_info_name"]["value"] == item["error"]["code"]["value"] == "unknown"
    assert not item["error"]["observed_signals"]["public_codes"]
    assert "EXAMPLE_" not in json.dumps(record)
    codex = ProviderFailureFacts("codex")
    codex.event(event)
    assert codex.tool_error_count == 0


async def test_future_headless_denial_keeps_tool_error_evidence_without_retry_or_acceptance(tmp_path):
    events = [tool_error_event({"type": "PERMISSION_DENIED", "message": "EXAMPLE_REASON"}),
              result(denied_actions=[{"action": "command", "display_name": "EXAMPLE_DISPLAY"}]), result()]
    router, project, store, rid = fixture_router(tmp_path, events, kind="antigravity")
    for _ in range(2):
        with pytest.raises(ProviderError):
            await router.invoke("review", {}, ReviewResult, project, rid, "review")
    refs, facts = observations(store, rid)
    assert len(refs) == len(facts) == 1
    fact = facts[0]
    assert fact["tool_error_count"] == 1 and not fact["tool_errors_truncated"]
    assert fact["tool_errors"][0]["tool"]["value"] == "run_command"
    assert fact["tool_errors"][0]["error"]["code"]["value"] == "PERMISSION_DENIED"
    denial = fact["terminal_failures"][0]["denied_actions"][0]
    assert denial["headless_action"]["value"] == "command"
    assert denial["tool"]["value"] == denial["reason"]["value"] == "unknown"
    assert fact["last_terminal"]["status"]["value"] == "SUCCESS" and fact["terminal_failure_count"] == 1
    state = store.get(rid, "infra/review/antigravity")
    assert state["failures"] == 1 and state["grant"] == 0 and not state["pending"]
    assert store.get(rid, "provider-result/review") is None
    assert len([row for row in store.events(rid) if row["kind"] == "agent.started"]) == 1
    assert "EXAMPLE_" not in json.dumps(facts)


async def test_recovered_tool_error_does_not_change_success_acceptance(tmp_path):
    router, project, store, rid = fixture_router(tmp_path, [
        tool_error_event({"type": "NOT_FOUND", "message": "EXAMPLE_TOOL_ERROR"}), result()], kind="antigravity")
    assert (await router.invoke("review", {}, ReviewResult, project, rid, "review")).approved is False
    assert observations(store, rid) == ([], [])
    assert store.get(rid, "provider-result/review") is not None


def signals(value):
    return native_error(value)["observed_signals"]


@pytest.mark.parametrize("value,codes", [
    ("EXAMPLE_UNKNOWN_ERROR /EXAMPLE_PATH 429 EXAMPLE_TOKEN", []),
    ({"code": "EXAMPLE_CODE", "type": "UNAVAILABLE", "message": "EXAMPLE_MESSAGE"}, ["UNAVAILABLE"]),
    ({"message": {"message": "HTTP 429; Retry-After: 30"}, "status": 429,
      "headers": {"Retry-After": 30}, "payload": {"code": "RESOURCE_EXHAUSTED"}}, []),
    (["HTTP 429", "Retry-After: 30"], []), (None, []),
])
def test_unknown_native_error_stays_opaque_and_does_not_walk_nested_payloads(value, codes):
    safe = native_error(value)
    assert safe["code"]["value"] == "unknown"
    assert safe["fingerprint"]["sha256"] == digest(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    assert safe["observed_signals"] == {
        "authority": "native_text_or_field_markers_only", "public_codes": codes, "http_status_markers": [],
        "keyword_signals": [], "retry_after_markers": [], "truncated": False,
    }
    assert "EXAMPLE_" not in json.dumps(safe)


@pytest.mark.parametrize("message,expected", [
    ("Error generating content: [429 RESOURCE_EXHAUSTED] EXAMPLE_TOKEN", ["RESOURCE_EXHAUSTED"]),
    ("rpc error: code = UNAVAILABLE desc = EXAMPLE_SECRET", ["UNAVAILABLE"]),
    ('{"error":{"code":"PERMISSION_DENIED","message":"EXAMPLE_SECRET"}}', ["PERMISSION_DENIED"]),
    ("prefixRESOURCE_EXHAUSTED EXAMPLE_UNAVAILABLE RESOURCE_EXHAUSTED_suffix", []),
])
def test_embedded_public_codes_are_text_markers_not_promoted_native_codes(message, expected):
    safe = native_error(message)
    assert safe["code"]["value"] == "unknown"
    assert safe["observed_signals"]["public_codes"] == expected
    assert safe["observed_signals"]["http_status_markers"] == []
    assert "EXAMPLE_" not in json.dumps(safe)


def test_original_code_hierarchy_and_diagnostic_privacy_are_unchanged():
    message = ("HTTP 429: RESOURCE_EXHAUSTED rate limit capacity quota authentication permission timeout connection; "
               "Retry-After: 30; EXAMPLE_SECRET EXAMPLE_TOKEN "
               "https://EXAMPLE_USER:EXAMPLE_PASSWORD@EXAMPLE_HOST/EXAMPLE_PATH?token=EXAMPLE_QUERY "
               "run_command EXAMPLE_COMMAND /EXAMPLE_PRIVATE_PATH")
    for value in (message, {"code": "EXAMPLE_CODE", "type": "UNAVAILABLE", "message": message,
                            "headers": {"Authorization": "EXAMPLE_TOKEN"}, "env": {"EXAMPLE_KEY": "EXAMPLE_SECRET"}}):
        safe = native_error(value)
        observed = safe["observed_signals"]
        assert safe["code"]["value"] == "unknown"
        assert observed["public_codes"] == (["RESOURCE_EXHAUSTED"] if type(value) is str else
                                            ["RESOURCE_EXHAUSTED", "UNAVAILABLE"])
        assert observed["http_status_markers"] == [429]
        assert observed["keyword_signals"] == [
            "authentication", "capacity", "connection", "permission", "quota", "rate_limit", "timeout"]
        assert observed["retry_after_markers"] == [{"kind": "seconds", "value": 30}]
        assert "EXAMPLE_" not in json.dumps(safe)
        assert "run_command" not in json.dumps(safe) and "https://" not in json.dumps(safe)
        assert "root_cause" not in safe and "retryable" not in safe
    assert native_error({"code": "UNAVAILABLE", "type": "ERROR"})["code"]["value"] == "UNAVAILABLE"
    assert native_error({"type": "UNAVAILABLE"})["code"]["value"] == "UNAVAILABLE"
    assert native_error("UNAVAILABLE")["code"]["value"] == "UNAVAILABLE"


@pytest.mark.parametrize("message,status", [
    ("HTTP 429: EXAMPLE_SECRET", 429), ("HTTP/1.1 503 Service Unavailable", 503),
    ("http status code: 401", 401), ('{"status_code":429}', 429),
    ('{"http_status":"503"}', 503), ("statusCode=403", 403),
])
def test_only_explicit_http_text_markers_are_observed(message, status):
    assert signals(message)["http_status_markers"] == [status]


@pytest.mark.parametrize("key", HTTP_FIELDS)
@pytest.mark.parametrize("value", [429, "429"])
def test_shallow_http_status_fields_are_observed_as_markers_only(key, value):
    assert signals({key: value})["http_status_markers"] == [429]


@pytest.mark.parametrize("value", [
    "429", "Error code: 429", "exit 503", "/home/EXAMPLE_PATH/429/error", r"C:\EXAMPLE_PATH\503\error",
    "/tmp/HTTP 429/error", "/tmp/status_code:429/error", "HTTP 4290", "HTTP 429.json",
    "HTTP429", "status_code=429suffix", "httpStatus: 503.5", {"status_code": True},
    {"http_status": 600}, {"http_status": 99}, {"http_status": 429.0}, {"status": 429},
])
def test_plain_numbers_paths_and_invalid_values_do_not_become_http_statuses(value):
    assert signals(value)["http_status_markers"] == []


@pytest.mark.parametrize("key", RETRY_FIELDS)
@pytest.mark.parametrize("value,seconds", [(0, 0), (86400, 86400), ("00030", 30)])
def test_retry_fields_accept_only_bounded_whole_seconds(key, value, seconds):
    assert signals({key: value})["retry_after_markers"] == [{"kind": "seconds", "value": seconds}]


@pytest.mark.parametrize("message", [
    "Retry-After: 30", "retry-after: 30", '"retry_after": "30"', '{"retryAfterSeconds":30}',
    "HTTP 429; Retry-After: 30; EXAMPLE_SECRET", "Retry-After: 30\nEXAMPLE_SECRET",
    '{"retry_after":30, "status_code":429}', '{"retry_after":"30", "message":"EXAMPLE_SECRET"}',
])
def test_explicit_retry_text_field_normalizes_without_copying_message(message):
    assert signals(message)["retry_after_markers"] == [{"kind": "seconds", "value": 30}]
    assert "EXAMPLE_" not in json.dumps(signals(message))


@pytest.mark.parametrize("value,utc", [
    ("Wed, 11 Nov 2026 02:00:00 GMT", "2026-11-11T02:00:00Z"),
    ("2026-11-11T02:00:00Z", "2026-11-11T02:00:00Z"),
    ("2026-11-11T10:00:00+08:00", "2026-11-11T02:00:00Z"),
    ("2026-11-11T02:00:00.125-01:00", "2026-11-11T03:00:00.125000Z"),
])
def test_retry_dates_require_valid_timezone_and_normalize_to_utc(value, utc):
    expected = [{"kind": "utc_datetime", "value": utc}]
    assert signals({"Retry-After": value})["retry_after_markers"] == expected
    assert signals('Retry-After: "' + value + '"')["retry_after_markers"] == expected
    assert signals("Retry-After: " + value)["retry_after_markers"] == expected
    assert signals({"retry_after_seconds": value})["retry_after_markers"] == []
    assert signals('retryAfterSeconds: "' + value + '"')["retry_after_markers"] == []


@pytest.mark.parametrize("value", [
    -1, 86401, True, 1.5, None, {}, [], "+30", "-30", "30.0", "3e1", "30s", "30 seconds", "1000000",
    "2026-11-11T02:00:00", "2026-11-31T02:00:00Z", "2026-11-11T24:00:00Z",
    "2026-11-11T02:00:60Z", "2026-11-11T02:00:00+24:00", "2026-11-11T02:00:00+00:60",
    "2026-11-11T02:00:00-00:00", "2026-11-11T02:00:00Z/EXAMPLE_TOKEN",
    "Thu, 11 Nov 2026 02:00:00 GMT", "Wed, 11 Nov 2026 02:00:00 PST",
    "Wed, 11 Nov 2026 02:00:00", "Wed, 31 Nov 2026 02:00:00 GMT",
    "30 EXAMPLE_TOKEN", '30"EXAMPLE_SECRET',
    "30,000", "30, 000", "30 ,000", "30 , 000", "30,\t000", "30,\n000", "30\n,\n000",
    "30,\u00a0000", "30, １２３", '"30",000', '"30", 000',
])
def test_invalid_or_out_of_range_retry_values_are_not_partially_captured(value):
    assert signals({"Retry-After": value})["retry_after_markers"] == []
    assert signals("Retry-After: " + str(value))["retry_after_markers"] == []
    assert "EXAMPLE_" not in json.dumps(signals({"Retry-After": value}))


def test_new_signal_lists_are_bounded_deduplicated_and_explicitly_truncated():
    message = " ".join(sorted(CODES - {"SUCCESS"})) + "; " + "; ".join(
        f"HTTP {400 + i}; Retry-After: {i}" for i in range(LIMIT + 2))
    observed = signals(message)
    assert observed["truncated"]
    assert all(len(observed[key]) == LIMIT for key in ("public_codes", "http_status_markers", "retry_after_markers"))
    assert signals("HTTP 429; HTTP 429; Retry-After: 30; Retry-After: 30")["retry_after_markers"] == [
        {"kind": "seconds", "value": 30}]


async def test_native_markers_cannot_erase_terminal_failure_or_grant_a_retry(tmp_path):
    error_text = "HTTP 429: RESOURCE_EXHAUSTED; Retry-After: 30; EXAMPLE_SECRET"
    router, project, store, rid = fixture_router(
        tmp_path, [result(status="ERROR", error=error_text), result()], kind="antigravity")
    for _ in range(2):
        with pytest.raises(ProviderError):
            await router.invoke("review", {}, ReviewResult, project, rid, "review")
    _, facts = observations(store, rid)
    assert len(facts) == 1 and facts[0]["terminal_failure_count"] == 1
    assert facts[0]["last_terminal"]["status"]["value"] == "SUCCESS"
    observed_error = facts[0]["terminal_failures"][0]["error"]
    assert observed_error["code"]["value"] == "unknown"
    assert observed_error["observed_signals"]["http_status_markers"] == [429]
    assert observed_error["observed_signals"]["retry_after_markers"] == [{"kind": "seconds", "value": 30}]
    state = store.get(rid, "infra/review/antigravity")
    assert state["failures"] == 1 and state["grant"] == 0 and not state["pending"]
    assert store.get(rid, "provider-result/review") is None
    assert len([row for row in store.events(rid) if row["kind"] == "agent.started"]) == 1
    assert "EXAMPLE_" not in json.dumps(facts)
