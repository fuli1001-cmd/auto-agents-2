import asyncio
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from test_environment_resolution_evidence import installed as installation_fixture
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.diagnostics import DiagnosticOperation, current_diagnostics
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error
from auto_agents_2.installation_evidence import (
    RECORD,
    REPORT,
    has_installation_record,
    installation_record,
    report_document,
    save_installation,
)
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json

installed = installation_fixture


def report():
    return {"version": "1", "pip_version": "25.2", "install": [{
        "metadata": {"name": "app", "version": "1.0", "requires_dist": ["leaf>=2"]},
        "download_info": {"url": "https://packages.example.test/app-1.0-py3-none-any.whl",
                          "archive_info": {"hashes": {"sha256": "a" * 64}}},
        "requested": True, "requested_extras": ["test"], "is_direct": False, "is_yanked": False}],
        "environment": {"python_full_version": "3.13.0", "sys_platform": "linux", "platform_machine": "x86_64"}}


def captured(manager, prefix, path, document=None, stdout="Installed app-1.0\n"):
    path.write_text(json.dumps(document if document is not None else report()))
    artifacts = ArtifactStore(manager.root)
    logs = [artifacts.put(stdout, "dependency_log"), artifacts.put("", "dependency_log")]
    return save_installation(manager, prefix, path, logs)


def bind(manager, env, capture_hash):
    path = env.prefix / "aa2-ready.json"
    ready = json.loads(path.read_text())
    ready.update(installation_hash=capture_hash, integrity=manager.integrity(env.prefix))
    write_json(path, ready)
    return Environment(env.prefix, manager.installed_identity(env.prefix))


async def test_installation_facts_bind_to_actual_packages_and_survive_material_selection(tmp_path, installed):
    manager, env, metadata, _ = installed
    metadata["runtime"]["machine"] = "x86_64"
    capture_hash = captured(manager, env.prefix, tmp_path / "report.json")
    env = bind(manager, env, capture_hash)
    enriched = await manager.with_resolution(tmp_path, env)
    artifacts = ArtifactStore(manager.root)
    facts = artifacts.json(enriched.resolution)
    install = facts["installation"]
    assert install["status"] == "recorded" and install["report"] == report()
    assert install["transcript"] == {"stdout": "Installed app-1.0\n", "stderr": ""}
    assert "not a lock or compatibility approval" in install["coverage"]
    assert facts["environment_hash"] == env.fingerprint and has_installation_record(env.prefix)
    for stage, content in (("review", {"checks": [{"environment_resolution": enriched.resolution}]}),
                           ("verify", {"evidence": [{"environment_resolution": enriched.resolution}]}),
                           ("recovery_diagnosis", {"failure": {"evidence": {"checks": [{"environment_resolution": enriched.resolution}]}}})):
        prepared = ContextPlanner(artifacts).prepare(stage, content, tmp_path)
        assert enriched.resolution["hash"] in json.dumps(prepared.payload)
        assert artifacts.json(enriched.resolution)["installation"] == install


async def test_legacy_capture_never_invents_installation_history(tmp_path, installed):
    manager, env, _, calls = installed
    before = manager.integrity(env.prefix)
    facts = ArtifactStore(manager.root).json((await manager.with_resolution(tmp_path, env)).resolution)
    assert facts["installation"]["status"] == "not_recorded"
    assert "report" not in facts["installation"] and not has_installation_record(env.prefix)
    assert manager.integrity(env.prefix) == before and len(calls) == 1


@pytest.mark.parametrize("damage", ["report", "stdout", "missing_report", "symlink", "wrong_package", "wrong_runtime", "record_hash"])
async def test_invalid_installation_evidence_cannot_publish_resolution(tmp_path, installed, damage):
    manager, env, metadata, _ = installed
    metadata["runtime"]["machine"] = "x86_64"
    document = report()
    if damage == "wrong_package":
        document["install"][0]["metadata"]["version"] = "9.0"
    if damage == "wrong_runtime":
        document["environment"]["python_full_version"] = "3.11.0"
    capture_hash = captured(manager, env.prefix, tmp_path / "report.json", document)
    if damage == "report":
        (env.prefix / REPORT).write_text("{}")
    elif damage == "stdout":
        (env.prefix / "aa2-pip-stdout.log").write_text("changed")
    elif damage == "missing_report":
        (env.prefix / REPORT).unlink()
    elif damage == "symlink":
        (env.prefix / REPORT).unlink()
        (env.prefix / REPORT).symlink_to(tmp_path / "report.json")
    elif damage == "record_hash":
        capture_hash = "b" * 64
    # Recalculate the outer tree to exercise the separate evidence-binding checks.
    env = bind(manager, env, capture_hash)
    with pytest.raises(AA2Error, match="resolution evidence unavailable"):
        await manager.with_resolution(tmp_path, env)
    with manager.store.connect() as db:
        assert not db.execute("SELECT 1 FROM artifact_kinds WHERE kind='environment_resolution'").fetchone()


@pytest.mark.parametrize("invalid", [None, [], {}, {"version": "99"}, {"install": []}])
def test_unsupported_reports_rejected_without_ready(tmp_path, installed, invalid):
    manager, env, _, _ = installed
    with pytest.raises(AA2Error, match="Installation provenance capture failed"):
        captured(manager, env.prefix, tmp_path / "bad.json", invalid if invalid is not None else [])
    assert not (env.prefix / RECORD).exists()


def test_complete_transcript_required_not_output_tail(tmp_path, installed):
    manager, env, _, _ = installed
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report()))
    artifact = ArtifactStore(manager.root).put("tail only", "dependency_log")
    with pytest.raises(AA2Error, match="provenance capture failed"):
        save_installation(manager, env.prefix, path, [artifact])
    with pytest.raises(AA2Error, match="provenance capture failed"):
        save_installation(manager, env.prefix, path, [{**artifact, "capture_complete": False}, artifact])
    assert not (env.prefix / RECORD).exists()


async def test_install_report_and_transcript_are_redacted_together(tmp_path, installed, monkeypatch):
    manager, env, metadata, _ = installed
    metadata["runtime"]["machine"] = "x86_64"
    monkeypatch.setenv("API_KEY", "fixture-private-install-value")
    (tmp_path / ".env").write_text('CAPTURE_SOURCE="fixture-protected-install-value"\n')
    document = report()
    document["install"][0]["metadata"]["summary"] = "fixture-private-install-value fixture-protected-install-value"
    document["install"][0]["download_info"]["url"] = "https://user:fixture-password@packages.example.test/app.whl?token=fixture-token"
    capture_hash = captured(manager, env.prefix, tmp_path / "report.json", document,
                            "fixture-private-install-value\n")
    env = bind(manager, env, capture_hash)
    facts = ArtifactStore(manager.root).json((await manager.with_resolution(tmp_path, env)).resolution)
    text = json.dumps(facts)
    assert facts["redacted"] and facts["installation"]["redacted"]
    retained = "\n".join((env.prefix / name).read_text() for name in (RECORD, REPORT, "aa2-pip-stdout.log", "aa2-pip-stderr.log"))
    assert all(secret not in text + retained for secret in ("fixture-password", "fixture-token",
               "fixture-private-install-value", "fixture-protected-install-value"))
    assert facts["installation"]["report"]["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"] == "a" * 64


def fake_installer(tmp_path, monkeypatch, *, missing_report=False, fail_install=False):
    config, store = configured(tmp_path)
    body = config.load().model_dump(mode="json")
    body["environment"]["conda"] = sys.executable
    write_json(config.path, body)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    calls = []

    async def command(argv, workspace, env, writes, reads=(), **options):
        calls.append(argv)
        if "create" in argv:
            prefix = Path(argv[argv.index("-p") + 1])
            (prefix / "bin").mkdir()
            (prefix / "bin/python").write_text("fixture interpreter")
            (prefix / "bin/python").chmod(0o755)
        elif "install" in argv:
            assert "--report" in argv and "--verbose" in argv
            assert "--dry-run" not in argv and "--ignore-installed" not in argv
            op = current_diagnostics.get()
            op.capture([ArtifactStore(store.root).put("fixture installer output\n", "dependency_log"),
                        ArtifactStore(store.root).put("", "dependency_log")])
            if fail_install:
                raise AA2Error("fixture install failure")
            if not missing_report:
                write_json(Path(argv[argv.index("--report") + 1]), report())
        elif "import sys; print(sys.prefix)" in argv:
            return str(Path(argv[0]).parent.parent)
        elif "freeze" in argv:
            return "app==1.0\n"
        elif "--explicit" in argv:
            return "@EXPLICIT\nfixture-conda-artifact\n"
        return ""

    monkeypatch.setattr(manager, "_command", command)
    return manager, store, calls


@pytest.mark.parametrize("private", [False, True])
async def test_new_and_private_preparations_capture_and_reuse(tmp_path, monkeypatch, private):
    manager, store, calls = fake_installer(tmp_path, monkeypatch)
    (tmp_path / "requirements.txt").write_text("-e .\n" if private else "app==1.0\n")
    if private:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="app"\nversion="1.0"\n')
    first, second = await asyncio.gather(manager.ensure(tmp_path, "python"), manager.ensure(tmp_path, "python"))
    assert first == second and sum("install" in argv for argv in calls) == 1
    ready = json.loads((first.prefix / "aa2-ready.json").read_text())
    facts = installation_record(first.prefix, ready["installation_hash"])
    assert ready["private"] == private and facts["report"] == report()
    assert facts["transcript"]["stdout"] == "fixture installer output\n"
    assert has_installation_record(first.prefix)
    assert (first.prefix / "aa2-source").is_dir() == private


async def test_legacy_upgrade_uses_saved_resolution_and_preserves_old_prefix(tmp_path, monkeypatch):
    manager, store, calls = fake_installer(tmp_path, monkeypatch)
    (tmp_path / "requirements.txt").write_text("app>=1\n")
    first = await manager.ensure(tmp_path, "python")
    ready = json.loads((first.prefix / "aa2-ready.json").read_text())
    ready.pop("installation_hash")
    for name in (RECORD, REPORT, "aa2-pip-stdout.log", "aa2-pip-stderr.log"):
        (first.prefix / name).unlink()
    ready["integrity"] = manager.integrity(first.prefix)
    write_json(first.prefix / "aa2-ready.json", ready)
    original_identity = manager.installed_identity(first.prefix)
    record = store.root / "environment-records" / ready["fingerprint"] / "resolution.json"
    lock_before = record.read_bytes()
    second = await manager.ensure(tmp_path, "python")
    assert second.prefix != first.prefix and second.fingerprint != original_identity
    assert manager.valid(first.prefix, original_identity) and manager.integrity(first.prefix) == ready["integrity"]
    assert record.read_bytes() == lock_before
    create = [argv for argv in calls if "create" in argv]
    assert "--file" not in create[0] and "--file" in create[1]
    install = [argv for argv in calls if "install" in argv]
    assert "-c" not in install[0] and "-c" in install[1]
    assert await manager.ensure(tmp_path, "python") == second


@pytest.mark.parametrize("mode", ["missing_report", "failed_install"])
async def test_incomplete_install_never_publishes_ready_or_resolution(tmp_path, monkeypatch, mode):
    manager, store, _ = fake_installer(tmp_path, monkeypatch, missing_report=mode == "missing_report", fail_install=mode == "failed_install")
    (tmp_path / "requirements.txt").write_text("app==1.0\n")
    rid = store.admit("installation evidence fixture", {})["id"]
    with pytest.raises(AA2Error):
        await manager.ensure(tmp_path, "python")
    assert not list((store.root / "environments").glob("*/aa2-ready.json"))
    assert not list((store.root / "environment-records").glob("*/resolution.json"))
    with store.connect() as db:
        assert db.execute("SELECT 1 FROM checkpoints WHERE run_id=? AND key LIKE 'environment/logs/%'", (rid,)).fetchone()


async def test_conda_only_record_does_not_claim_a_pip_install(tmp_path, monkeypatch):
    manager, _, calls = fake_installer(tmp_path, monkeypatch)
    env = await manager.ensure(tmp_path, "python")
    ready = json.loads((env.prefix / "aa2-ready.json").read_text())
    record = installation_record(env.prefix, ready["installation_hash"])
    assert not record["pip_invoked"] and "report" not in record and "transcript" not in record
    assert not any("install" in argv for argv in calls)


@pytest.mark.parametrize("kind", ["wheel", "source", "source_with_build_dependencies"])
async def test_real_offline_pip_records_selected_artifact_and_actual_build_output(tmp_path, kind):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    work, prefix, target = tmp_path / "work", tmp_path / "evidence-prefix", tmp_path / "installed"
    work.mkdir()
    prefix.mkdir()
    filename = "capture_fixture-1.0-py3-none-any.whl"
    contents = {"capture_fixture.py": "VALUE=42\n",
                "capture_fixture-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: capture-fixture\nVersion: 1.0\n",
                "capture_fixture-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                "capture_fixture-1.0.dist-info/RECORD": ""}
    if kind == "wheel":
        selected = work / filename
        with zipfile.ZipFile(selected, "w") as archive:
            for name, body in contents.items():
                archive.writestr(name, body)
    else:
        selected = work / "source"
        selected.mkdir()
        requires = []
        if kind == "source_with_build_dependencies":
            requires = ["capture-build-dep==1.0"]
            for name, version, dependency in (("capture_build_dep", "1.0", "Requires-Dist: capture-build-leaf==2.0\n"),
                                               ("capture_build_leaf", "2.0", "")):
                with zipfile.ZipFile(work / f"{name}-{version}-py3-none-any.whl", "w") as archive:
                    archive.writestr(f"{name}.py", f"VERSION={version!r}\n")
                    archive.writestr(f"{name}-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{dependency}")
                    archive.writestr(f"{name}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                    archive.writestr(f"{name}-{version}.dist-info/RECORD", "")
        (selected / "pyproject.toml").write_text(f'[build-system]\nrequires={json.dumps(requires)}\nbuild-backend="backend"\nbackend-path=["."]\n')
        backend = "import pathlib, zipfile\n" + f"CONTENTS={contents!r}\nNAME={filename!r}\n"
        if kind == "source":
            backend += 'print("fixture transcript start:" + "x" * 30000 + ":fixture transcript end")\n'
        if requires:
            backend += 'import capture_build_dep, capture_build_leaf\nprint("fixture actual build dependencies:", capture_build_dep.VERSION, capture_build_leaf.VERSION)\n'
        backend += 'def get_requires_for_build_wheel(config_settings=None):\n print("fixture actual build requirements: []")\n return []\n'
        backend += 'def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n print("fixture actual wheel build")\n with zipfile.ZipFile(pathlib.Path(wheel_directory)/NAME,"w") as a:\n  for n,b in CONTENTS.items(): a.writestr(n,b)\n return NAME\n'
        (selected / "backend.py").write_text(backend)
    report_path = tmp_path / "actual-report.json"
    env = {**os.environ, "PIP_CONFIG_FILE": os.devnull, "PIP_CACHE_DIR": str(tmp_path / "cache"), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    with DiagnosticOperation(store, manager.resources, "offline-install") as operation:
        await manager._command([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile",
            "--find-links", str(work), "--target", str(target), "--report", str(report_path), "--verbose", str(selected)], work, env, [tmp_path], [Path(sys.prefix)], timeout=60)
        capture_hash = save_installation(manager, prefix, report_path, operation.logs)
    # The successful diagnostic operation may now collect its logs. The prefix copy remains complete.
    captured_record = installation_record(prefix, capture_hash)
    real_report = captured_record["report"]
    assert report_document(report_path.read_bytes()) == real_report
    assert real_report["install"][0]["metadata"]["name"] == "capture-fixture"
    assert (target / "capture_fixture.py").read_text() == "VALUE=42\n"
    transcript = "\n".join(captured_record["transcript"].values())
    if kind == "wheel":
        assert real_report["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"] == digest(selected.read_bytes())
        assert "Building wheel" not in transcript
    else:
        assert "fixture actual build requirements: []" in transcript
        assert "fixture actual wheel build" in transcript
        assert real_report["install"][0]["download_info"]["dir_info"] == {}
        assert "does not inventory isolated build environments" in captured_record["coverage"]
        if kind == "source":
            assert "fixture transcript start:" + "x" * 30000 + ":fixture transcript end" in transcript

        if kind == "source_with_build_dependencies":
            assert "fixture actual build dependencies: 1.0 2.0" in transcript
            assert "capture-build-dep-1.0" in transcript and "capture-build-leaf-2.0" in transcript
            assert len(real_report["install"]) == 1  # Build dependencies are evidenced by the transcript, not the report.
