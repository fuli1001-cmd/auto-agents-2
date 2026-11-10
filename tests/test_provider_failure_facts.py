"""Synthetic native failures preserve facts without provider content or new calls."""
import json
from types import SimpleNamespace

import pytest
from test_provider_terminal_state import COMPLETE, MESSAGE, REVIEW, START, TURN, fixture_router

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import ProviderError
from auto_agents_2.faults import known_exception
from auto_agents_2.models import ReviewResult
from auto_agents_2.provider_failure import LIMIT, ProviderFailureFacts, denied_action
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
