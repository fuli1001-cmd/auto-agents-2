import sys
from pathlib import Path

import pytest
from test_runtime import configured
from typer.testing import CliRunner

from auto_agents_2.cli import app
from auto_agents_2.config import Config, ConfigStore, Profile, ProviderConfig
from auto_agents_2.context import ContextBuilder
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error, ConfigurationError, ProviderError
from auto_agents_2.materials import bundle
from auto_agents_2.profiles import ProjectProfile
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json


@pytest.mark.parametrize("document", ["dependencies: python", "dependencies: 123", "dependencies: [python, 123]",
    "[python, pip]", "null", "dependencies: [{pip: [123]}]"])
async def test_conda_shape_is_rejected_before_preparation(tmp_path, monkeypatch, document):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root))
    (tmp_path / "environment.yml").write_text(document)
    async def prepare(*args):
        pytest.fail("invalid manifest must not reach the installer")
    monkeypatch.setattr(manager, "_prepare", prepare)
    with pytest.raises(AA2Error, match="Invalid Conda"):
        await manager.ensure(tmp_path, "python")


@pytest.mark.parametrize("resolution", ["dep==1\n", "dep==2\n"])
async def test_reconstruction_constrains_pip_and_preserves_original_resolution(tmp_path, monkeypatch, resolution):
    config, store = configured(tmp_path)
    body = config.load().model_dump(mode="json")
    body["environment"]["conda"] = sys.executable
    write_json(config.path, body)
    manager = EnvironmentManager(config, SandboxRunner(store.root))
    prefix = store.root / "environments/new"
    records = store.root / "environment-records/fingerprint"
    records.mkdir(parents=True)
    (records / "conda.lock").write_text("@EXPLICIT\nfixture-conda-package\n")
    (records / "pip.lock").write_text("dep==1\n")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("--index-url https://packages.example/simple\ndep>=1\n")
    calls = []
    async def command(argv, *args, **kwargs):
        calls.append(argv)
        if "install" in argv and "-c" in argv:
            assert Path(argv[argv.index("-c") + 1]).read_text() == "dep==1\n"
        if "install" in argv:
            from auto_agents_2.artifacts import ArtifactStore
            from auto_agents_2.diagnostics import current_diagnostics
            write_json(Path(argv[argv.index("--report") + 1]), {"version": "1", "pip_version": "fixture", "install": [], "environment": {}})
            current_diagnostics.get().capture([ArtifactStore(store.root).put(text, "dependency_log") for text in ("fixture install", "")])
        if "create" in argv:
            (prefix / "bin").mkdir()
            (prefix / "bin/python").write_text("fixture")
            (prefix / "bin/python").chmod(0o755)
        if "-c" in argv and "import sys; print(sys.prefix)" in argv:
            return str(prefix)
        if "freeze" in argv:
            return resolution
        if "--explicit" in argv:
            return "@EXPLICIT\nfixture-conda-package\n"
        return ""
    monkeypatch.setattr(manager, "_command", command)
    if resolution == "dep==1\n":
        await manager._prepare(prefix, prefix / "aa2-ready.json", "fingerprint", "python", None, requirements, tmp_path, [], False)
        assert (prefix / "aa2-ready.json").is_file()
    else:
        with pytest.raises(AA2Error, match="differs"):
            await manager._prepare(prefix, prefix / "aa2-ready.json", "fingerprint", "python", None, requirements, tmp_path, [], False)
        assert not (prefix / "aa2-ready.json").exists()
    install = next(argv for argv in calls if "install" in argv)
    assert install[-2] == "-c" and Path(install[-1]).is_relative_to(store.root / "tmp")
    assert install[install.index("-r") + 1] == str(requirements)
    assert (records / "pip.lock").read_text() == "dep==1\n"


def node_fixture(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root))
    work = tmp_path / "work"
    work.mkdir()
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/npm").write_text("fixture")
    (work / "node_modules").mkdir()
    return manager, work, Environment(prefix, "fixture")


async def test_lock_resolution_stages_local_artifacts_and_workspace_manifests_invalidate_readiness(tmp_path, monkeypatch):
    manager, work, environment = node_fixture(tmp_path)
    (work / "vendor").mkdir()
    (work / "vendor/dep.tgz").write_bytes(b"local tarball fixture")
    write_json(work / "package.json", {"name": "fixture", "workspaces": ["packages/*"],
        "dependencies": {"dep": "file:vendor/dep.tgz"}})
    nested = work / "packages/app/package.json"
    write_json(nested, {"name": "app", "dependencies": {}})
    calls = []
    async def command(argv, cwd, *args, **kwargs):
        calls.append(argv)
        if "--package-lock-only" in argv:
            assert (cwd / "vendor/dep.tgz").read_bytes() == b"local tarball fixture"
            assert (cwd / "packages/app/package.json").is_file()
            write_json(cwd / "package-lock.json", {"lockfileVersion": 3})
        return ""
    monkeypatch.setattr(manager, "_command", command)
    await manager.install_node(work, environment)
    assert sum("ci" in c for c in calls) == 1
    await manager.install_node(work, environment)
    assert sum("ci" in c for c in calls) == 1
    write_json(nested, {"name": "app", "dependencies": {"changed": "1"}})
    await manager.install_node(work, environment)
    assert sum("ci" in c for c in calls) == 2
    (work / "vendor/dep.tgz").write_bytes(b"changed tarball")
    await manager.install_node(work, environment)
    assert sum("ci" in c for c in calls) == 3


async def test_local_npm_escape_is_rejected_before_resolution(tmp_path, monkeypatch):
    manager, work, environment = node_fixture(tmp_path)
    (tmp_path / "outside.tgz").write_bytes(b"outside")
    write_json(work / "package.json", {"dependencies": {"dep": "file:../outside.tgz"}})
    async def command(*args, **kwargs):
        pytest.fail("escaping dependency must not reach npm")
    monkeypatch.setattr(manager, "_command", command)
    with pytest.raises(AA2Error, match="Local npm"):
        await manager.install_node(work, environment)


@pytest.mark.parametrize("python", [True, False])
def test_generic_node_is_not_labelled_as_react(tmp_path, python):
    write_json(tmp_path / "package.json", {"dependencies": {"express": "1"}})
    if python:
        (tmp_path / "requirements.txt").write_text("")
    profile = ProjectProfile.detect(tmp_path)
    assert profile.name == ("python-node" if python else "node")
    assert not profile.has_frontend


def test_removed_path_does_not_resolve_inherited_provider_binary(tmp_path, monkeypatch):
    binary = tmp_path / "fixture-bin"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    config = ConfigStore(tmp_path)
    settings = Config(active_provider="fixture", providers={"fixture": ProviderConfig(kind="codex", binary="fixture-bin",
        environment={"PATH": None}, profile_map={"balanced": Profile()})})
    monkeypatch.setattr(config, "load", lambda: settings)
    with pytest.raises(ConfigurationError, match="executable not found"):
        config.resolve("review")


def test_native_profile_errors_are_normalized_without_secret_contents(tmp_path, monkeypatch):
    (tmp_path / "broken.config.toml").write_text('secret = ["private-unclosed')
    config = ConfigStore(tmp_path)
    settings = Config(active_provider="fixture", providers={"fixture": ProviderConfig(kind="codex", binary=sys.executable,
        environment={"CODEX_HOME": str(tmp_path)}, profile_map={"balanced": "broken"})})
    monkeypatch.setattr(config, "load", lambda: settings)
    with pytest.raises(ConfigurationError) as caught:
        config.resolve("review")
    assert "private-unclosed" not in str(caught.value)


@pytest.mark.parametrize("content", [None, "{invalid", "{}", '{"providers":[]}'])
def test_failed_configuration_import_never_initializes_defaults(tmp_path, content):
    file = tmp_path / "import.json"
    if content is not None:
        file.write_text(content)
    project = tmp_path / "project"
    result = CliRunner().invoke(app, ["init", "--project", str(project), "--from-config", str(file)])
    assert result.exit_code == 2
    assert not (project / ".aa2/config.json").exists()


def test_context_disappearance_and_unreadable_material_have_correct_boundaries(tmp_path, monkeypatch):
    missing = tmp_path / "gone.txt"
    monkeypatch.setattr("auto_agents_2.context.public_files", lambda *args: [missing])
    assert ContextBuilder().build(tmp_path)["files"] == []
    file = tmp_path / "attachment"
    file.write_text("fixture")
    original = Path.read_bytes
    def fail(path):
        if path == file:
            raise PermissionError("injected")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", fail)
    with pytest.raises(ProviderError, match="unreadable"):
        bundle("prompt", {}, [file], [], {})
