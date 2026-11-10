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


@pytest.mark.parametrize("shape", ["selected-profile", "base", "auth", "home"])
def test_selected_input_links_never_follow_an_alias(tmp_path, monkeypatch, shape):
    import os

    from auto_agents_2.errors import PolicyViolation
    from auto_agents_2.transports import native_input_bytes
    config, store = configured(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    target = tmp_path / "forbidden-target"
    target.write_text("synthetic unselected contents")
    profile = native / "deep.config.toml"
    profile.write_text("model='fixture'\nmodel_reasoning_effort='high'\n")
    name = {"selected-profile": "deep.config.toml", "base": "config.toml", "auth": "auth.json"}.get(shape)
    if name:
        path = native / name
        if path.exists():
            path.unlink()
        path.symlink_to(target)
    else:
        alias = tmp_path / "linked-home"
        alias.symlink_to(native, target_is_directory=True)
        native = alias
    body = config.load().model_dump(mode="json")
    body["providers"]["codex"]["environment"]["CODEX_HOME"] = str(native)
    body["providers"]["codex"]["profile_map"]["balanced"] = "deep"
    from auto_agents_2.util import write_json
    write_json(config.path, body)
    original_open = os.open
    def checked_open(path, flags, *args, **kwargs):
        assert str(path) != str(target) and str(path) != target.name
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", checked_open)
    if shape != "auth":
        with pytest.raises(PolicyViolation):
            config.resolve("review", "codex")
    with pytest.raises(PolicyViolation):
        CodexTransport().input_files({"environment": {"CODEX_HOME": str(native)}, "native_profile": "deep"})
    if name:
        with pytest.raises(PolicyViolation):
            native_input_bytes(native / name)


@pytest.mark.parametrize("name", ["deep.config.toml", "config.toml", "auth.json"])
def test_selected_native_hardlink_is_rejected_without_reading(tmp_path, monkeypatch, name):
    import os

    from auto_agents_2.errors import PolicyViolation
    from auto_agents_2.transports import native_input_bytes
    native = tmp_path / "native"
    native.mkdir()
    original = tmp_path / "unselected-original"
    original.write_text("synthetic fixture")
    alias = native / name
    os.link(original, alias)
    with pytest.raises(PolicyViolation):
        native_input_bytes(alias)
    with pytest.raises(PolicyViolation):
        CodexTransport().input_files({"environment": {"CODEX_HOME": str(native)}, "native_profile": "deep"})


async def test_unsafe_selected_input_stops_without_spawn_or_fallback(tmp_path, monkeypatch):
    from auto_agents_2.errors import PolicyViolation
    from auto_agents_2.models import ReviewResult
    config, store = configured(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    target = tmp_path / "unselected-target"
    target.write_text("synthetic fixture")
    (native / "auth.json").symlink_to(target)
    def resolve(stage, candidate):
        assert candidate == "codex", "Unsafe input must not trigger fallback resolution"
        return {"kind": "codex", "provider": "codex", "native_profile": "",
                "environment": {"CODEX_HOME": str(native)}, "fingerprint": "fixture"}
    monkeypatch.setattr(config, "resolve", resolve)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    async def forbidden(*args):
        pytest.fail("Unsafe input must not spawn any provider")
    monkeypatch.setattr(router, "_call", forbidden)
    rid = store.admit("fixture", {})["id"]
    with pytest.raises(PolicyViolation):
        await router.invoke("review", {}, ReviewResult, tmp_path, rid, "unsafe")


def test_selected_overlay_preserves_refresh_and_removes_only_owned_old_profile(tmp_path):
    config, store = configured(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    for name in ("config.toml", "auth.json", "deep.config.toml", "balanced.config.toml", "max.config.toml"):
        (native / name).write_text("synthetic " + name)
    effective = {"kind": "codex", "provider": "codex", "native_profile": "deep",
                 "environment": {"CODEX_HOME": str(native)}, "fingerprint": "fixture"}
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    rid = store.admit("fixture", {})["id"]
    mirror = router._session_overlay(effective, rid, "fixture")[0][0]
    (mirror / "auth.json").write_text("synthetic CLI refreshed auth")
    router._session_overlay(effective, rid, "fixture")
    assert (mirror / "auth.json").read_text() == "synthetic CLI refreshed auth"
    (native / "auth.json").write_text("synthetic source rotation")
    router._session_overlay(effective, rid, "fixture")
    assert (mirror / "auth.json").read_text() == "synthetic source rotation"
    effective["native_profile"] = "balanced"
    router._session_overlay(effective, rid, "fixture")
    assert not (mirror / "deep.config.toml").exists()
    assert (mirror / "balanced.config.toml").exists()
    assert (native / "deep.config.toml").read_text() == "synthetic deep.config.toml"
    assert (native / "max.config.toml").read_text() == "synthetic max.config.toml"
    assert not (mirror / "max.config.toml").exists()
