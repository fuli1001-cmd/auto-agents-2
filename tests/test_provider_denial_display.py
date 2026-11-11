"""Display-only refusal guidance cannot renew calls or rewrite old prerequisites."""
from types import SimpleNamespace

import pytest
from test_provider_failure_facts import observations, result
from test_provider_terminal_state import fixture_router

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.errors import ProviderError
from auto_agents_2.faults import known_exception
from auto_agents_2.models import ReviewResult
from auto_agents_2.provider_failure import COMMAND_DENIAL_DIAGNOSTIC, TOOL_DENIAL_DIAGNOSTIC
from auto_agents_2.store import Store
from auto_agents_2.util import digest


@pytest.mark.parametrize("action,expected", [
    ("command", COMMAND_DENIAL_DIAGNOSTIC),
    ("read_file", TOOL_DENIAL_DIAGNOSTIC),
    ("EXAMPLE_PRIVATE_ACTION", TOOL_DENIAL_DIAGNOSTIC),
])
async def test_retained_denial_hint_survives_budget_exhaustion_without_retry(tmp_path, action, expected):
    events = [result(denied_actions=[{"action": action, "display_name": "EXAMPLE_PRIVATE_DISPLAY"}]), result()]
    router, project, store, rid = fixture_router(tmp_path, events, kind="antigravity")
    before = None
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            await router.invoke("review", {}, ReviewResult, project, rid, "review")
        error = caught.value
        refs, _ = observations(store, rid)
        assert type(error) is ProviderError and not error.retryable
        assert error.evidence == {"provider_failures": refs}
        assert error.native_denial_diagnostic == expected
        boundary = known_exception(error, "prototype")
        ordinary = known_exception(ProviderError("Fixture", evidence=error.evidence), "prototype")
        assert boundary.reason_code == ordinary.reason_code == "PROVIDER_ERROR"
        assert boundary.requirement.model_dump() == ordinary.requirement.model_dump()
        assert expected in boundary.requirement.prompt()
        assert "检查项目配置和原生 provider 的认证、连接状态" not in boundary.requirement.prompt()
        assert "具体规则未明" in boundary.requirement.prompt()
        assert "EXAMPLE_" not in boundary.requirement.prompt()
        state = store.get(rid, "infra/review/antigravity")
        assert state["failures"] == 1 and state["grant"] == 0 and not state["pending"]
        if before is not None:
            assert state == before
        before = state
    assert len([e for e in store.events(rid) if e["kind"] == "agent.started"]) == 1
    assert store.get(rid, "provider-result/review") is None


async def test_free_text_permission_is_not_a_terminal_denial(tmp_path):
    router, project, _, rid = fixture_router(tmp_path,
        [result(status="ERROR", error="permission denied EXAMPLE_REASON")], kind="antigravity")
    with pytest.raises(ProviderError) as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert caught.value.native_denial_diagnostic == ""
    boundary = known_exception(caught.value, "prototype")
    assert "原生证据包含" not in boundary.requirement.prompt()


async def test_unavailable_diagnostic_does_not_replace_failure_or_grant_retry(tmp_path, monkeypatch):
    router, project, store, rid = fixture_router(tmp_path,
        [result(denied_actions=[{"action": "command"}])], kind="antigravity")
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    before = store.get(rid, "infra/review/antigravity")
    def unavailable(*args, **kwargs):
        raise OSError("EXAMPLE_PRIVATE_ERROR")
    monkeypatch.setattr(ArtifactStore, "json", unavailable)
    with pytest.raises(ProviderError, match="budget exhausted") as caught:
        await router.invoke("review", {}, ReviewResult, project, rid, "review")
    assert caught.value.native_denial_diagnostic == ""
    assert store.get(rid, "infra/review/antigravity") == before
    assert len([e for e in store.events(rid) if e["kind"] == "agent.started"]) == 1


@pytest.mark.parametrize("hint", ["EXAMPLE_PRIVATE_DIAGNOSTIC", {"untrusted": "value"}])
def test_arbitrary_diagnostic_cannot_enter_question(hint):
    error = ProviderError("Fixture")
    error.native_denial_diagnostic = hint
    assert known_exception(error, "prototype").requirement.prompt() == known_exception(
        ProviderError("Fixture"), "prototype").requirement.prompt()


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("answer", [None, "y"])
async def test_denial_display_preserves_prerequisite_identity_and_existing_prompt(tmp_path, retained, answer):
    store = Store(tmp_path)
    rid = store.admit("Display fixture", {})["id"]
    error = ProviderError("Fixture", evidence={"provider_failures": []})
    original = known_exception(error, "prototype")
    identity = digest([original.reason_code, original.phase, original.requirement.model_dump()])
    key = "prerequisite/" + identity + "/0"
    old = None
    if retained:
        old = store.request(rid, key, "Original exact prerequisite", identity)
        if answer:
            store.answer(old["id"], answer)
    error.native_denial_diagnostic = COMMAND_DENIAL_DIAGNOSTIC
    boundary = known_exception(error, "prototype")
    observed = []
    class StopAtQuestion(Exception):
        pass
    async def ask(run, question_key, prompt, artifact, interaction):
        observed.append(store.request(run, question_key, prompt, artifact, interaction))
        raise StopAtQuestion
    engine = SimpleNamespace(store=store, _ask=ask)
    with pytest.raises(StopAtQuestion):
        await WorkflowEngine._wait_prerequisite(engine, rid, boundary)
    q = observed[0]
    assert q["key"] == key and q["artifact_hash"] == identity
    if retained:
        assert q["id"] == old["id"] and q["prompt"] == old["prompt"] and q["answer"] == answer
    else:
        assert COMMAND_DENIAL_DIAGNOSTIC in q["prompt"] and q["answer"] is None
    assert store.get(rid, "prerequisite/" + identity, 0) == 0

    # A later read failure removes only the optional hint, not the saved wording.
    ordinary = known_exception(ProviderError("Fixture", evidence=error.evidence), "prototype")
    with pytest.raises(StopAtQuestion):
        await WorkflowEngine._wait_prerequisite(engine, rid, ordinary)
    later = observed[-1]
    assert later["id"] == q["id"] and later["prompt"] == q["prompt"]
    assert later["answer"] == q["answer"] and later["artifact_hash"] == identity
    assert store.get(rid, "prerequisite/" + identity, 0) == 0
