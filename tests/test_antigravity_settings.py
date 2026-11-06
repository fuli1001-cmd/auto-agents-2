import json
import sys
import traceback

import pytest

from auto_agents_2.config import ConfigStore, validate_extra_args
from auto_agents_2.errors import ConfigurationError, ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.transports import AntigravityTransport
from auto_agents_2.util import write_json


def settings_config(project, args, location="provider"):
    config = ConfigStore(project)
    profile = {"model": "configured-model", "effort": "high"}
    provider = {
        "kind": "antigravity", "binary": sys.executable,
        "profile_map": {"max": profile}, "extra_args": [],
    }
    if location == "provider":
        provider["extra_args"] = args
    else:
        profile["extra_args"] = args
        if location == "profile_file":
            write_json(config.path.parent / "profile.json", profile)
            provider["profile_map"]["max"] = {"file": "profile.json"}
    write_json(config.path, {
        "active_provider": "billing-fixture", "providers": {"billing-fixture": provider},
    })
    return config


@pytest.mark.parametrize("location", ["provider", "inline_profile", "profile_file"])
@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("equals", [False, True])
def test_billing_settings_resolve_and_reach_transport(tmp_path, location, value, equals):
    payload = json.dumps({"useG1Credits": value})
    args = ["--settings=" + payload] if equals else ["--settings", payload]
    # Later model/effort overrides must still be parsed after the settings value.
    args += ["--model", "explicit-model", "--effort=medium"]
    config = settings_config(tmp_path, args, location)
    before = config.path.read_bytes()
    effective = config.resolve("prototype")
    assert effective["provider"] == "billing-fixture"
    assert effective["model"] == "explicit-model"
    assert effective["effort"] == "medium"
    assert effective["args"] == effective["extra_args"] == args
    transport = AntigravityTransport()
    for previous in (None, "fixture-session"):
        argv = transport.argv(effective, tmp_path, tmp_path / "schema.json",
                              tmp_path / "result.json", "prompt", previous, [])
        assert argv[-len(args):] == args
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert argv[argv.index("--json-schema") + 1] == str(tmp_path / "schema.json")
        assert ("--conversation" in argv) == bool(previous)
    assert config.path.read_bytes() == before


def test_billing_settings_change_execution_fingerprint_without_a_default(tmp_path):
    identities = []
    for args in ([], ["--settings", '{"useG1Credits":false}'],
                 ["--settings", '{"useG1Credits":true}']):
        effective = settings_config(tmp_path, args).resolve("prototype")
        assert effective["args"] == args
        identities.append(effective["fingerprint"])
    assert len(set(identities)) == 3


@pytest.mark.parametrize("kind", ["", "codex", "other"])
@pytest.mark.parametrize("equals", [False, True])
def test_session_settings_are_only_supported_by_antigravity(kind, equals):
    payload = '{"useG1Credits":false}'
    args = ["--settings=" + payload] if equals else ["--settings", payload]
    with pytest.raises(ConfigurationError):
        validate_extra_args(args, kind=kind)


@pytest.mark.parametrize("payload", [
    "", "/tmp/settings.json", "settings.json", "false", "null", "[]", "{}",
    '[["useG1Credits", false]]',
    '{"useG1Credits": "false"}', '{"useG1Credits": 0}', '{"useG1Credits": 1}',
    '{"useG1Credits": null}', '{"useG1Credits": []}', '{"useG1Credits": NaN}',
    '{"useG1Credits": {"useG1Credits": false}}',
    '{"toolPermission": "always"}',
    '{"useG1Credits": false, "toolPermission": "always"}',
    '{"useG1Credits": false, "useG1Credits": true}',
    '{"useG1Credits": false, "useG1Credits": false}',
    '{"useG1Credits": false} trailing',
])
def test_session_settings_reject_unsupported_shapes_and_values(payload):
    with pytest.raises(ConfigurationError):
        validate_extra_args(["--settings", payload], kind="antigravity")


@pytest.mark.parametrize("args", [
    ["--settings"], ["--settings="], ["--settings", "--model", "fixture"],
    ["--settings", '{"useG1Credits":false}', "--settings", '{"useG1Credits":true}'],
    ['--settings={"useG1Credits":false}', '--settings={"useG1Credits":false}'],
])
def test_session_settings_reject_missing_values_and_repeated_flags(args):
    with pytest.raises(ConfigurationError):
        validate_extra_args(args, kind="antigravity")


def test_settings_cannot_conflict_between_profile_and_provider(tmp_path):
    config = settings_config(tmp_path, ["--settings", '{"useG1Credits":false}'])
    body = config.load().model_dump(mode="json")
    body["providers"]["billing-fixture"]["profile_map"]["max"]["extra_args"] = [
        '--settings={"useG1Credits":true}',
    ]
    write_json(config.path, body)
    with pytest.raises(ConfigurationError):
        config.resolve("prototype")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    '{"useG1Credits":false,"toolPermission":"DO_NOT_LOG_FIXTURE"}',
    '{"useG1Credits":"DO_NOT_LOG_FIXTURE"}',
    '/tmp/DO_NOT_LOG_FIXTURE.json',
    'DO_NOT_LOG_FIXTURE invalid JSON',
])
async def test_invalid_settings_never_dispatch_or_expose_payload(tmp_path, monkeypatch, caplog, capsys, payload):
    config = settings_config(tmp_path, ["--settings", payload])
    store = Store(tmp_path)
    rid = store.admit("settings validation", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))

    async def unexpected_call(*args, **kwargs):
        pytest.fail("Invalid settings reached provider dispatch")

    monkeypatch.setattr(router, "_call", unexpected_call)
    with pytest.raises(ProviderError) as error:
        await router.invoke("prototype", {}, ReviewResult, tmp_path, rid, "settings")
    captured = capsys.readouterr()
    diagnostics = ("".join(traceback.format_exception(error.value)) + caplog.text
                   + captured.out + captured.err + json.dumps(store.events(rid)))
    assert "DO_NOT_LOG_FIXTURE" not in diagnostics
    assert "Unsupported Antigravity session settings" in str(error.value)
    assert not any(event["kind"] == "agent.spawned" for event in store.events(rid))
