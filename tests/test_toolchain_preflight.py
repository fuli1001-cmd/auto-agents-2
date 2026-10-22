import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2 import toolchain
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import AA2Error, ToolchainUnavailable
from auto_agents_2.faults import known_exception
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.toolchain import ToolchainPreflight, interpreter_requirement, version_probe
from auto_agents_2.util import write_json


def executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


def test_missing_conda_is_a_concrete_external_prerequisite(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ToolchainUnavailable) as caught:
        version_probe("conda", "conda")
    assert caught.value.status == "missing"
    boundary = known_exception(caught.value, "develop")
    assert boundary.reason_code == "TOOLCHAIN_UNAVAILABLE"
    assert boundary.requirement.kind == "external_input"
    assert known_exception(AA2Error("unknown defect"), "develop") is None


@pytest.mark.parametrize("exit_code,output", [(1, "conda 1.2.3"), (0, "no version")])
def test_failed_version_probe_does_not_publish_raw_diagnostics(tmp_path, exit_code, output):
    binary = executable(tmp_path / "probe", f"import sys\nprint({output!r})\nprint('EXAMPLE_PRIVATE_VALUE',file=sys.stderr)\nsys.exit({exit_code})")
    with pytest.raises(ToolchainUnavailable) as caught:
        version_probe("conda", binary)
    assert "EXAMPLE_PRIVATE_VALUE" not in str(caught.value)


def test_version_report_retains_only_path_and_version(tmp_path):
    binary = executable(tmp_path / "probe", "print('conda 25.7.0 EXAMPLE_PRIVATE_VALUE')")
    assert version_probe("conda", binary) == {"path": binary, "version": "25.7.0"}


def test_probe_timeout_is_not_an_unbounded_or_model_retry(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(args[0], 15, output=b"EXAMPLE_PRIVATE_VALUE")
    monkeypatch.setattr(toolchain.subprocess, "run", timeout)
    with pytest.raises(ToolchainUnavailable) as caught:
        version_probe("python", sys.executable)
    assert caught.value.status == "probe_failed"
    assert "EXAMPLE_PRIVATE_VALUE" not in str(caught.value)


@pytest.mark.parametrize("shebang", ["#!/usr/bin/env node", "#!/usr/bin/env -S node --no-warnings"])
def test_provider_node_shebang_uses_effective_provider_path(tmp_path, shebang):
    wrapper = tmp_path / "provider"
    wrapper.write_text(shebang + "\n")
    with pytest.raises(ToolchainUnavailable, match="provider interpreter"):
        interpreter_requirement(wrapper, {"PATH": str(tmp_path)})
    node = Path(executable(tmp_path / "node", "print('fixture')"))
    assert interpreter_requirement(wrapper, {"PATH": str(tmp_path)}) == str(node)


def test_native_provider_does_not_get_guessed_interpreter_requirement(tmp_path):
    binary = tmp_path / "provider"
    binary.write_bytes(b"\x7fELF\0fixture")
    assert interpreter_requirement(binary, {"PATH": ""}) is None


def test_inspect_records_real_controller_and_never_exposes_provider_environment(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    runner = SandboxRunner(store.root, _test_unconfined=True)
    monkeypatch.setattr(toolchain, "version_probe", lambda tool, binary: {"path": "/tools/" + tool, "version": "1.2.3"})
    monkeypatch.setattr(config, "resolve", lambda *args: {"binary": sys.executable,
        "environment": {"PATH": "", "EXAMPLE_API_KEY": "EXAMPLE_PRIVATE_VALUE"}})
    report = ToolchainPreflight(config, runner).inspect()
    assert report["ok"]
    assert report["checks"]["controller_python"]["prefix"] == sys.prefix
    assert "EXAMPLE_PRIVATE_VALUE" not in json.dumps(report)
    assert "EXAMPLE_API_KEY" not in json.dumps(report)


async def test_missing_conda_is_checked_before_prefix_mutation(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    settings = config.load().model_dump(mode="json")
    settings["environment"]["conda"] = "definitely-missing-conda-fixture"
    write_json(config.path, settings)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    prefix = store.root / "environments" / "retained"
    prefix.mkdir(parents=True)
    original = prefix / "keep"
    original.write_text("previous evidence")
    with pytest.raises(ToolchainUnavailable):
        await manager._prepare(prefix, prefix / "aa2-ready.json", "fixture", "python", None, None, tmp_path, [], False)
    assert original.read_text() == "previous evidence"
    assert not (prefix / "aa2-ready.json").exists()


async def test_engine_rechecks_precondition_after_answer_and_resumes_without_reset(tmp_path):
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("fixture", {})["id"]
    workflow.store.checkpoint(rid, "rounds/T1", {"round": 7})
    observations, questions = [], []
    restored = False

    async def check():
        observations.append(restored)
        if not restored:
            raise ToolchainUnavailable("conda", "missing", "activate the selected toolchain")
        return {"ok": True, "checks": {"conda": {"ok": True}}}

    async def question(request):
        questions.append(request)
        return "y" if len(questions) == 1 else "n"

    async def execute(run_id):
        assert run_id == rid and restored
        workflow.store.update(rid, status="SUCCEEDED")
        return workflow.store.run(rid)

    workflow.toolchain.check = check
    workflow.question_handler = question
    workflow._execute = execute
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(run_id=rid)
    assert observations == [False, False]
    assert len(questions) == 2 and not agent.calls
    assert workflow.store.run(rid)["status"] == "PAUSED"
    assert workflow.store.get(rid, "rounds/T1") == {"round": 7}
    restored = True
    result = await workflow.start(run_id=rid)
    assert result["status"] == "SUCCEEDED"
    assert workflow.store.get(rid, "rounds/T1") == {"round": 7}
    assert workflow.store.get(rid, "environment/preflight")["ok"]


def test_doctor_failed_toolchain_never_dispatches_live_model(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from auto_agents_2.cli import app
    from auto_agents_2.providers import ProviderRouter
    configured(tmp_path)
    monkeypatch.setattr(ToolchainPreflight, "inspect", lambda self: {"ok": False,
        "checks": {"sandbox": {"ok": True}, "conda": {"ok": False}}, "optional": {}})
    async def unexpected(*args, **kwargs):
        pytest.fail("A failed preflight must not dispatch a paid model request")
    monkeypatch.setattr(ProviderRouter, "invoke", unexpected)
    result = CliRunner().invoke(app, ["doctor", "--live", "--project", str(tmp_path)])
    assert result.exit_code != 0
    assert "Toolchain preflight" in result.output
