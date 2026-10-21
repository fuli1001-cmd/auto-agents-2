import asyncio
import sys

import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.config import Cleanup
from auto_agents_2.diagnostics import DiagnosticOperation
from auto_agents_2.issues import IssueLedger
from auto_agents_2.logs import capture_diagnostics
from auto_agents_2.models import Finding, ReviewResult
from auto_agents_2.resources import ResourceManager
from auto_agents_2.store import Store
from auto_agents_2.util import digest


def setup(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    artifacts = ArtifactStore(store.root)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    return store, rid, artifacts, resources


def test_log_producer_checkpoint_consumer_and_final_receipt(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("full diagnostic", "check_log")
    report = artifacts.put("<testsuite tests='1'/>", "junit")
    resources.collect_artifacts()
    assert artifacts.read(log) == "full diagnostic"  # Producer not yet published.
    receipt = {"checks": [{"exit_code": 0, "logs": [log], "report": report}]}
    receipt["digest"] = digest(receipt)
    store.checkpoint(rid, "delivery/receipt", receipt)
    resources.collect_artifacts()
    assert artifacts.read(log)
    store.update(rid, status="SUCCEEDED")
    resources.collect_artifacts()
    assert artifacts.released(log)
    assert artifacts.read(report)
    assert store.get(rid, "delivery/receipt") == receipt  # No rewriting signed historical records.


def test_overwritten_logs_reclaimed_during_unfinished_run(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    first = artifacts.put("old resolved attempt", "check_log")
    store.checkpoint(rid, "checks", {"logs": [first]})
    second = artifacts.put("current failure", "check_log")
    store.checkpoint(rid, "checks", {"logs": [second]})
    resources.collect_artifacts()
    assert artifacts.released(first)
    assert artifacts.read(second)


@pytest.mark.parametrize("status", ["PAUSED", "WAITING_USER"])
def test_waiting_and_paused_consumers_keep_logs(tmp_path, status):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("awaiting decision", "check_log")
    store.checkpoint(rid, "checks", {"logs": [log]})
    store.update(rid, status=status)
    resources.collect_artifacts()
    assert artifacts.read(log)


@pytest.mark.parametrize("issue_status", ["open", "stale"])
def test_open_issues_and_explicit_recovery_outlive_cancellation(tmp_path, issue_status):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("failed assertion details", "check_log")
    other = artifacts.put("no remaining consumer", "dependency_log")
    recovery = artifacts.put("required recovery log", "check_log")
    ledger = IssueLedger(store, artifacts)
    finding = Finding(severity="major", location="a.py", description="wrong", evidence="actual check", resolution="fix")
    ledger.record(rid, "scope", ReviewResult(approved=False, summary="failed", findings=[finding]), {"checks": [{"logs": [log]}]})
    issues = store.get(rid, "issues")
    for issue in issues.values():
        issue["status"] = issue_status
    store.checkpoint(rid, "issues", issues)
    store.checkpoint(rid, "recovery/required/operator", {"logs": [recovery]})
    store.checkpoint(rid, "checks", {"logs": [other]})
    store.update(rid, status="CANCELLED")
    resources.collect_artifacts()
    assert artifacts.released(other)
    assert artifacts.read(log) and artifacts.read(recovery)
    ledger.record(rid, "scope", ReviewResult(approved=True, summary="resolved"), {})
    store.invalidate(rid, ["recovery/required/operator"])
    resources.collect_artifacts()
    assert artifacts.released(log) and artifacts.released(recovery)


def test_same_bytes_required_elsewhere_and_shared_across_runs(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("shared content", "check_log")
    mandatory = artifacts.put("shared content", "required_evidence")
    store.checkpoint(rid, "proof", {"body": mandatory, "logs": [log]})
    store.update(rid, status="SUCCEEDED")
    resources.collect_artifacts()
    assert artifacts.read(log)
    second = store.admit("second", {})["id"]
    shared = artifacts.put("shared content", "check_log")
    store.checkpoint(second, "checking", {"logs": [shared]})
    store.invalidate(rid, ["proof"])
    resources.collect_artifacts()
    assert artifacts.read(shared)


def test_reader_pins_terminal_log_until_actual_reader_finishes(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("read during cleanup", "check_log")
    parent = artifacts.put({"nested": {"logs": [log]}})
    store.checkpoint(rid, "evidence", parent)
    store.update(rid, status="SUCCEEDED")
    with artifacts.reading({"evidence": parent}, rid):
        resources.collect_artifacts()
        assert artifacts.read(log)
    resources.collect_artifacts()
    assert artifacts.released(log)
    assert artifacts.json(parent)["nested"]["logs"][0]["hash"] == log["hash"]


def test_retirement_is_recoverable_after_unlink_failure(tmp_path, monkeypatch):
    from pathlib import Path
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("to retire", "check_log")
    store.checkpoint(rid, "checks", {"logs": [log]})
    store.update(rid, status="SUCCEEDED")
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path.name == log["hash"]:
            raise OSError("injected unlink failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError, match="injected"):
        resources.collect_artifacts()
    assert artifacts.read(log) and not artifacts.released(log)
    monkeypatch.setattr(Path, "unlink", original)
    resources.collect_artifacts()
    assert artifacts.released(log)


def test_missing_unreleased_file_is_not_reported_as_expired(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("accidental loss", "check_log")
    store.checkpoint(rid, "checks", {"logs": [log]})
    (artifacts.root / log["hash"][:2] / log["hash"]).unlink()
    assert not artifacts.released(log)
    store.update(rid, status="SUCCEEDED")
    resources.collect_artifacts()
    assert not artifacts.released(log)


def test_installer_keeps_logs_through_postconditions_then_releases(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    with pytest.raises(ValueError):
        with DiagnosticOperation(store, resources, "install") as operation:
            log = artifacts.put("install returned zero but validation failed", "dependency_log")
            operation.capture([log])
            resources.collect_artifacts()
            assert artifacts.read(log)
            raise ValueError("postcondition failed")
    resources.collect_artifacts()
    assert artifacts.read(log)
    with DiagnosticOperation(store, resources, "install"):
        pass  # The retry validated all postconditions.
    assert artifacts.released(log)
    assert store.current()["status"] == "RUNNING"


def test_recovery_adopts_abandoned_producer_without_approving_check(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("published file before checkpoint crash", "check_log")
    resources.recover_disposable()
    rows = store.get(rid, "diagnostics/recovered/" + log["lease_id"])
    assert "never acceptance evidence" in rows["outcome"]
    assert artifacts.read(log)
    store.update(rid, status="CANCELLED")
    resources.collect_artifacts()
    assert artifacts.released(log)


@pytest.mark.asyncio
async def test_successful_installer_scope_cleans_spool_and_archived_body(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    with DiagnosticOperation(store, resources, "install") as operation:
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('installation diagnostic')",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, _, logs = await capture_diagnostics(process, resources, artifacts)
        operation.capture(logs)
        assert artifacts.read(logs[0]) == "installation diagnostic\n"
        assert not list((store.root / "tmp").glob("diagnostic-log_*"))
    assert all(artifacts.released(ref) for ref in logs)


def test_cleanup_preserves_mandatory_refs_in_legacy_bundle(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("optional diagnostic", "check_log")
    report = artifacts.put("actual required report")
    parent = artifacts.put({"logs": [log], "report": report})
    with store.connect() as db:
        db.execute("DELETE FROM artifact_edges")
    store.checkpoint(rid, "legacy", parent)
    store.update(rid, status="SUCCEEDED")
    resources.collect_artifacts()
    assert artifacts.released(log) and artifacts.read(report)


def test_new_checkpoint_between_retirement_and_unlink_keeps_log(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("reacquired during collection", "check_log")
    store.checkpoint(rid, "checks", {"logs": [log]})
    store.update(rid, status="SUCCEEDED")
    next_run = store.admit("next consumer", {})["id"]
    original, connections = store.connect, 0

    @contextmanager
    def interleaved():
        nonlocal connections
        connections += 1
        first = connections == 1
        with original() as db:
            yield db
        if first:
            store.checkpoint(next_run, "checks", {"logs": [log]})

    monkeypatch.setattr(store, "connect", interleaved)
    resources.collect_artifacts()
    assert artifacts.read(log) == "reacquired during collection"
    assert not artifacts.released(log)


@pytest.mark.asyncio
async def test_retired_diagnostic_does_not_replace_required_report(tmp_path):
    from pathlib import Path

    from auto_agents_2.environment import Environment
    from auto_agents_2.evidence import reusable
    from auto_agents_2.models import Command
    from auto_agents_2.sandbox import SandboxRunner
    from auto_agents_2.verification import VerificationService

    store, rid, artifacts, resources = setup(tmp_path)
    (tmp_path / "test_value.py").write_text("def test_value():\n    assert 2 + 2 == 4\n")
    command = Command(argv=[sys.executable, "-m", "pytest", "-q", "test_value.py"])
    environment = Environment(Path(sys.prefix), "fixture")
    checks = await VerificationService(store.root, SandboxRunner(store.root), resources).run(
        [command], tmp_path, environment)
    assert checks[0].exit_code == 0 and checks[0].tests == 1
    receipt = {"checks": [check.model_dump() for check in checks]}
    store.checkpoint(rid, "delivery/receipt", {**receipt, "digest": digest(receipt)})
    store.update(rid, status="SUCCEEDED")
    resources.collect_artifacts()
    assert all(artifacts.released(ref) for ref in checks[0].logs)
    assert reusable([command], checks, tmp_path, environment, artifacts).allowed
    report = checks[0].report
    (artifacts.root / report["hash"][:2] / report["hash"]).unlink()
    assert not reusable([command], checks, tmp_path, environment, artifacts).allowed


@pytest.mark.asyncio
async def test_model_view_marks_retired_nested_logs_and_rejects_accidental_loss(tmp_path):
    from test_runtime import configured

    from auto_agents_2.errors import ProviderError
    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.sandbox import SandboxRunner

    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    artifacts = ArtifactStore(store.root)
    resources = ResourceManager(store, config.load().cleanup)
    log = artifacts.put("optional finished check diagnostic", "check_log")
    report = artifacts.put("mandatory structured evidence")
    parent = artifacts.put({"logs": [log], "report": report})
    receipt = {"evidence": parent}
    store.checkpoint(rid, "delivery/receipt", {**receipt, "digest": digest(receipt)})
    store.update(rid, status="SUCCEEDED")
    resources.collect_artifacts()
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))

    class Inspected(Exception):
        pass

    class InspectCompiler:
        def compile(self, stage, context, *args):
            assert context["released_diagnostics"][0]["hash"] == log["hash"]
            assert context["released_diagnostics"][0]["availability"] == "released"
            assert "location" not in context["released_diagnostics"][0]
            assert not any(path.endswith(log["hash"]) for path in context["context_files"])
            # Generated mount paths are now exposed once through evidence locations.
            assert any(ref["hash"] == report["hash"] and ref["location"].endswith(report["hash"])
                       for ref in context["evidence_materials"])
            assert not any(ref.get("location", "").endswith(log["hash"])
                           for ref in context["evidence_materials"])
            raise Inspected()

    router.compiler = InspectCompiler()
    with pytest.raises(Inspected):
        await router._call(config.resolve("review"), "review", {"receipt": parent}, ReviewResult,
                           tmp_path, rid, "inspect", True)
    assert artifacts.json(parent) == {"logs": [log], "report": report}
    missing = artifacts.put("unexpectedly lost", "check_log")
    (artifacts.root / missing["hash"][:2] / missing["hash"]).unlink()
    with pytest.raises(ProviderError, match="unavailable"):
        await router._call(config.resolve("review"), "review", {"logs": [missing]}, ReviewResult,
                           tmp_path, rid, "missing", True)
