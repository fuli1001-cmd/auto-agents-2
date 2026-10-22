import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml
from test_installation_provenance import fake_installer
from test_manifest_references import plan
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.environment import EnvironmentManager, conda_toolchain_dependencies
from auto_agents_2.errors import ToolchainUnavailable
from auto_agents_2.faults import known_exception
from auto_agents_2.util import write_json


@pytest.mark.parametrize("dependencies,added", [
    (["conda-forge::nodejs==22.23.2"], ["python=3.11", "pip"]),
    ([], ["python=3.11", "pip"]),
    (["python=3.12"], ["pip"]),
    (["python >=3.12,<3.13", "pip >=24"], []),
    (["conda-forge/linux-64::python==3.12.8=fixture_0", "defaults::pip=24.3"], []),
    (["python[version='>=3.12,<3.13',build='fixture_*']", "pip[version='>=24']"], []),
    (["python_abi=3.12", "pip-tools"], ["python=3.11", "pip"]),
    (["pip"], ["python=3.11"]),
])
def test_missing_tools_preserve_named_constraints(dependencies, added):
    before = dependencies[:]
    assert conda_toolchain_dependencies(dependencies, "3.11") == added
    assert dependencies == before


@pytest.mark.parametrize("selection", ["auto", "selected"])
async def test_manifest_is_augmented_only_in_controller_copy(tmp_path, monkeypatch, selection):
    manager, _, calls = fake_installer(tmp_path, monkeypatch)
    filename = "environment.yml" if selection == "auto" else "toolchain.yml"
    original = ("channels:\n  - conda-forge\ndependencies:\n"
                "  - conda-forge::nodejs==22.23.2\n  - pip:\n    - app==1.0\n")
    (tmp_path / filename).write_text(original)
    command = manager._command
    documents = []

    async def inspect(argv, *args, **kwargs):
        if "create" in argv:
            selected = Path(argv[argv.index("-f") + 1])
            assert selected.is_relative_to(manager.root / "tmp")
            documents.append(yaml.safe_load(selected.read_text()))
        return await command(argv, *args, **kwargs)

    monkeypatch.setattr(manager, "_command", inspect)
    selected = filename if selection == "selected" else ""
    result = await manager.ensure(tmp_path, "react", selected)
    assert documents == [{"channels": ["conda-forge"],
                          "dependencies": ["conda-forge::nodejs==22.23.2", "python=3.11", "pip"]}]
    assert (tmp_path / filename).read_text() == original
    assert result.prefix.is_relative_to(manager.root / "environments")
    assert any("check" in argv for argv in calls)
    assert any("install" in argv and "--report" in argv for argv in calls)
    assert manager.valid(result.prefix, result.fingerprint)


async def test_explicit_python_and_pip_constraints_are_not_overridden(tmp_path, monkeypatch):
    manager, _, _ = fake_installer(tmp_path, monkeypatch)
    specs = ["conda-forge::python >=3.12,<3.13", "pip[version='>=24']", "nodejs==22.23.2"]
    original = yaml.safe_dump({"dependencies": specs})
    (tmp_path / "environment.yml").write_text(original)
    command = manager._command

    async def inspect(argv, *args, **kwargs):
        if "create" in argv:
            staged = yaml.safe_load(Path(argv[argv.index("-f") + 1]).read_text())
            assert staged["dependencies"] == specs
        return await command(argv, *args, **kwargs)

    monkeypatch.setattr(manager, "_command", inspect)
    await manager.ensure(tmp_path, "react")
    assert (tmp_path / "environment.yml").read_text() == original


async def test_added_tools_invalidate_old_resolution_without_rewriting_it(tmp_path, monkeypatch):
    manager, store, calls = fake_installer(tmp_path, monkeypatch)
    (tmp_path / "environment.yml").write_text("dependencies:\n  - nodejs==22.23.2\n")
    with monkeypatch.context() as old:
        old.setattr("auto_agents_2.environment.conda_toolchain_dependencies", lambda *args: [])
        previous = await manager.ensure(tmp_path, "react")
    old_ready = (previous.prefix / "aa2-ready.json").read_bytes()
    old_fingerprint = json.loads(old_ready)["fingerprint"]
    record = store.root / "environment-records" / old_fingerprint / "resolution.json"
    old_resolution = record.read_bytes()
    current = await manager.ensure(tmp_path, "react")
    assert current.prefix != previous.prefix
    assert json.loads((current.prefix / "aa2-ready.json").read_text())["fingerprint"] != old_fingerprint
    assert record.read_bytes() == old_resolution
    assert (previous.prefix / "aa2-ready.json").read_bytes() == old_ready
    creates = [argv for argv in calls if "create" in argv]
    assert len(creates) == 2 and all("--file" not in argv for argv in creates)
    assert await manager.ensure(tmp_path, "react") == current
    assert sum("create" in argv for argv in calls) == 2


@pytest.mark.parametrize("defect", ["missing", "nonexecutable"])
async def test_installer_missing_runtime_is_not_state_conflict_or_host_fallback(tmp_path, monkeypatch, defect):
    manager, store, calls = fake_installer(tmp_path, monkeypatch)
    (tmp_path / "environment.yml").write_text("dependencies:\n  - nodejs==22.23.2\n")
    command = manager._command

    async def broken(argv, *args, **kwargs):
        result = await command(argv, *args, **kwargs)
        if "create" in argv:
            python = Path(argv[argv.index("-p") + 1]) / "bin/python"
            if defect == "missing":
                python.unlink()
            else:
                python.chmod(0o644)
        return result

    monkeypatch.setattr(manager, "_command", broken)
    with pytest.raises(ToolchainUnavailable) as caught:
        await manager.ensure(tmp_path, "react")
    assert caught.value.tool == "project Python"
    assert known_exception(caught.value, "review").reason_code == "TOOLCHAIN_UNAVAILABLE"
    assert not any("-m" in argv for argv in calls)
    assert not list((store.root / "environments").glob("*/aa2-ready.json"))
    assert not list((store.root / "environment-records").glob("*/resolution.json"))
    assert list((store.root / "environments").glob("*"))  # Interrupted instance remains.


def offline_conda(tmp_path, config, *, omit_python=False):
    """A declared resolver fixture; actual venv/pip/probes and managed processes."""
    executable = tmp_path / "fixture-conda"
    trace = tmp_path / "conda-calls.jsonl"
    executable.write_text(f"#!{sys.executable}\n" + f"TRACE={str(trace)!r}\nOMIT={omit_python!r}\n" + r'''
import json, platform, re, sys, venv
from pathlib import Path
import yaml

args = sys.argv[1:]
prefix = Path(args[args.index("-p") + 1])
if "create" in args:
    if "-f" in args:
        doc = yaml.safe_load(Path(args[args.index("-f") + 1]).read_text())
        deps = doc["dependencies"]
    else:
        deps = args
    with Path(TRACE).open("a") as stream:
        stream.write(json.dumps(deps) + "\n")
    names = {re.split(r"[\s=<>!\[]", dep.split("::")[-1])[0] for dep in deps}
    (prefix / "bin").mkdir(exist_ok=True)
    if "python" in names and not OMIT:
        venv.EnvBuilder(with_pip="pip" in names, symlinks=True).create(prefix)
    metadata = prefix / "conda-meta"
    metadata.mkdir()
    (metadata / "nodejs.json").write_text(json.dumps({
        "name":"nodejs","version":"22.23.2","build":"fixture","depends":[],"constrains":[]}))
    print("Offline resolver fixture completed.")
elif "--explicit" in args:
    print("@EXPLICIT\nhttps://fixtures.example.test/nodejs-22.23.2-fixture.conda")
else:
    sys.exit(2)
''')
    executable.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["environment"]["conda"] = str(executable)
    write_json(config.path, body)
    return trace


@pytest.mark.parametrize("interrupt", [False, True])
async def test_engine_manifest_preparation_resume_evidence_and_reuse(tmp_path, monkeypatch, interrupt):
    workflow = engine(tmp_path, CodingAgent())
    trace = offline_conda(tmp_path, workflow.config)
    rid = workflow.store.admit("Node-only toolchain fixture", {})["id"]
    work = tmp_path / "work"
    work.mkdir()
    original = "channels:\n  - conda-forge\ndependencies:\n  - conda-forge::nodejs==22.23.2\n"
    (work / "environment.yml").write_text(original)
    proposal = plan(environment_file="environment.yml")
    workflow.store.checkpoints(rid, {"plan": proposal.model_dump(), "plan_revision/current": {"version": 1, "revision": 4},
                                   "rounds/T1": {"round": 5}, "recovery/budgets/diagnosis/T1": {"round": 9}})
    counters = {key: workflow.store.get(rid, key) for key in ("rounds/T1", "recovery/budgets/diagnosis/T1")}
    if interrupt:
        command = workflow.environments._command
        async def stop_after_install(argv, *args, **kwargs):
            result = await command(argv, *args, **kwargs)
            if "create" in argv:
                raise asyncio.CancelledError()
            return result
        monkeypatch.setattr(workflow.environments, "_command", stop_after_install)
        with pytest.raises(asyncio.CancelledError):
            await WorkflowEngine._environment(workflow, work, rid, proposal)
        retained = list((workflow.store.root / "environments").glob("*"))
        assert len(retained) == 1 and not (retained[0] / "aa2-ready.json").exists()
        workflow.environments = EnvironmentManager(workflow.config, workflow.sandbox)

    result = await WorkflowEngine._environment(workflow, work, rid, proposal)
    evidence = ArtifactStore(workflow.store.root).json(result.resolution)
    assert evidence["environment_hash"] == result.fingerprint
    assert evidence["python"]["runtime"]["python"]
    assert any(item["name"] == "pip" for item in evidence["python"]["packages"])
    assert evidence["conda_packages"][0]["name"] == "nodejs"
    assert not evidence["installation"]["pip_invoked"]  # No invented pip transaction.
    assert (work / "environment.yml").read_text() == original
    assert any(event["kind"] == "environment.ready" for event in workflow.store.events(rid))
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 4
    assert counters == {key: workflow.store.get(rid, key) for key in counters}
    repeated = await WorkflowEngine._environment(workflow, work, rid, proposal)
    assert repeated == result and len(trace.read_text().splitlines()) == (2 if interrupt else 1)
    if interrupt:
        assert retained[0].exists() and result.prefix != retained[0]
    assert not list((workflow.store.root / "processes").glob("*.json"))

