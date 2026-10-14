import os
from pathlib import Path

import pytest
from test_runtime import configured

from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json


def setup_installer(tmp_path):
    config, store = configured(tmp_path)
    store.admit("node browser runtime", {})
    work = tmp_path / "frontend"
    work.mkdir()
    write_json(work / "package.json", {"devDependencies": {"@playwright/test": "1.58.2"}})
    write_json(work / "package-lock.json", {"lockfileVersion": 3})
    prefix = tmp_path / "runtime"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/npm").touch()
    (prefix / "bin/node").touch()
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    return manager, Environment(prefix, "runtime"), work


@pytest.mark.asyncio
async def test_browser_installation_is_part_of_node_readiness_and_integrity(tmp_path, monkeypatch):
    manager, environment, work = setup_installer(tmp_path)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "unrelated-browser-cache"))
    calls = []
    browser = work / "node_modules/playwright-core/.local-browsers/chromium-test/chrome"

    async def command(argv, workspace, env, writes, reads=(), **kwargs):
        calls.append(argv)
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == "0"
        assert work / "node_modules" in writes
        if argv[1] == "ci":
            cli = work / "node_modules/playwright/cli.js"
            cli.parent.mkdir(parents=True, exist_ok=True)
            cli.write_text("locked project CLI")
        else:
            assert argv[:3] == [str(environment.prefix / "bin/node"), str(work / "node_modules/playwright/cli.js"), "install"]
            browser.parent.mkdir(parents=True, exist_ok=True)
            browser.write_text("verified browser binary")

    manager._command = command
    await manager.install_node(work, environment)
    assert len(calls) == 2 and calls[-1][3:] == ["chromium"]
    await manager.install_node(work, environment)
    assert len(calls) == 2
    browser.write_text("corrupt browser binary")
    await manager.install_node(work, environment)
    assert len(calls) == 4 and browser.read_text() == "verified browser binary"
    settings = manager.config.load().model_dump(mode="json")
    settings["environment"]["playwright_browsers"] = ["chromium", "firefox"]
    write_json(manager.config.path, settings)
    await manager.install_node(work, environment)
    assert calls[-1][3:] == ["chromium", "firefox"]
    assert not (tmp_path / "unrelated-browser-cache").exists()


@pytest.mark.asyncio
async def test_failed_browser_install_never_publishes_a_ready_node_record(tmp_path):
    manager, environment, work = setup_installer(tmp_path)
    (work / "user.ts").write_text("preserve source")
    marker = manager.root / "node-records" / (digest(str(work)) + ".json")

    async def command(argv, *args, **kwargs):
        if argv[1] == "ci":
            cli = work / "node_modules/playwright/cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("CLI")
        else:
            raise AA2Error("browser download failed")

    manager._command = command
    with pytest.raises(AA2Error, match="browser download failed"):
        await manager.install_node(work, environment)
    assert not marker.exists() and (work / "user.ts").read_text() == "preserve source"


async def test_projects_without_playwright_do_not_install_browsers(tmp_path):
    manager, environment, work = setup_installer(tmp_path)

    async def unexpected(*args, **kwargs):
        pytest.fail("No browser installer should run")

    manager._command = unexpected
    await manager.prepare_node_browsers(work, environment)
    assert environment.variables(Path("/scratch"))["PLAYWRIGHT_BROWSERS_PATH"] == "0"


def test_headless_runtime_does_not_inherit_or_modify_host_desktop_session(tmp_path, monkeypatch):
    values = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/mnt/wslg/runtime-dir"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    variables = Environment(tmp_path / "runtime", "fixture").variables(tmp_path / "scratch")
    assert not values.keys() & variables.keys()
    assert all(os.environ[name] == value for name, value in values.items())
    assert variables["PLAYWRIGHT_BROWSERS_PATH"] == "0"
