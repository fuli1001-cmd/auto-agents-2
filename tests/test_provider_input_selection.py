from pathlib import Path

import pytest
from test_runtime import configured

from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.transports import CodexTransport
from auto_agents_2.util import digest


def test_only_selected_codex_profile_participates_in_hash_and_overlay(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    for name in ("config.toml", "auth.json", "balanced.config.toml", "max.config.toml", "unrelated.toml"):
        (native / name).write_text("synthetic fixture")
    effective = {"kind": "codex", "provider": "codex", "native_profile": "balanced",
                 "environment": {"CODEX_HOME": str(native)}, "fingerprint": "fixture"}
    monkeypatch.setattr(config, "resolve", lambda *args: effective)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    original_read, original_stat = Path.read_bytes, Path.stat
    def checked_read(path):
        assert path.name not in {"max.config.toml", "unrelated.toml"}
        return original_read(path)
    def checked_stat(path, *args, **kwargs):
        assert path.name not in {"max.config.toml", "unrelated.toml"}
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", checked_read)
    monkeypatch.setattr(Path, "stat", checked_stat)
    first = router.effective("review", "codex")["fingerprint"]
    from auto_agents_2.output_limit_runtime import native_inputs
    assert set(native_inputs(router, effective)) == {"config.toml", "auth.json", "balanced.config.toml"}
    rid = store.admit("fixture", {})["id"]
    mounts = router._session_overlay(effective, rid, "fixture")
    assert {path.name for path in mounts[0][0].iterdir()} >= {"config.toml", "auth.json", "balanced.config.toml"}
    assert not any(path.name in {"max.config.toml", "unrelated.toml"} for path in mounts[0][0].iterdir())
    (native / "balanced.config.toml").write_text("changed selected fixture")
    assert router.effective("review", "codex")["fingerprint"] != first


def test_identity_resolves_only_active_provider(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    names = []
    def effective(stage, candidate):
        names.append(candidate)
        assert candidate == config.load().active_provider
        return {"fingerprint": "fixture"}
    monkeypatch.setattr(router, "effective", effective)
    assert router.execution_identity("review") == digest(["selected-provider-v1", "codex", "fixture"])
    assert names == ["codex"]


@pytest.mark.parametrize("name", ["../max", "nested/profile", "..", "a\\b"])
def test_profile_selector_rejects_path_escape_without_stat(tmp_path, name, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid profile path must not be inspected")
    monkeypatch.setattr(Path, "is_file", forbidden)
    from auto_agents_2.errors import ConfigurationError
    with pytest.raises(ConfigurationError):
        CodexTransport().input_files({"environment": {"CODEX_HOME": str(tmp_path)}, "native_profile": name})


async def test_transport_fallback_completion_is_not_reused_as_active_provider(tmp_path, monkeypatch):
    from authority_review_fixtures import authority_review, seed_original

    from auto_agents_2.engine import WorkflowEngine
    from auto_agents_2.models import ReviewResult
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    workflow = WorkflowEngine(tmp_path)
    seed_original(workflow, rid)
    calls = []
    async def call(effective, stage, context, *args):
        calls.append(effective["provider"])
        return authority_review(context, approved=True, summary="fixture")
    monkeypatch.setattr(workflow.agent, "_call", call)
    await workflow._invoke("verify", {}, ReviewResult, tmp_path, rid, "cache")
    saved = store.get(rid, "invocation/cache")
    saved["provider"] = "antigravity"
    saved.pop("execution_provider", None)
    store.checkpoint(rid, "invocation/cache", saved)
    await workflow._invoke("verify", {}, ReviewResult, tmp_path, rid, "cache")
    assert calls == ["codex", "codex"]
