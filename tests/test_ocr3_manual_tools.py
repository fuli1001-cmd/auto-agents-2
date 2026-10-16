import json
import sys
from pathlib import Path

import manual_command_matrix as matrix
import manual_test_reports as reports
import pytest
from manual_recovery_snapshot import snapshot, validate_copy
from test_ocr_manual_tools import paused_project

from auto_agents_2.config import Cleanup
from auto_agents_2.models import CheckResult
from auto_agents_2.resources import ResourceManager
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.workspace import WorkspaceManager


@pytest.mark.parametrize("kind", ["gitdir", "config", "alternates", "bare"])
def test_snapshot_rejects_every_unmanaged_nested_git_entrypoint(tmp_path, kind):
    nested = tmp_path / "nested"
    nested.mkdir()
    if kind == "gitdir":
        (nested / ".git").write_text("gitdir: /outside/private\n")
    elif kind == "bare":
        (nested / "HEAD").write_text("ref: refs/heads/main")
        (nested / "objects").mkdir()
    else:
        path = nested / (".git/config" if kind == "config" else ".git/objects/info/alternates")
        path.parent.mkdir(parents=True)
        path.write_text("[include]\npath=/outside/private\n" if kind == "config" else "/outside/objects\n")
    with pytest.raises(ValueError, match="Nested Git"):
        validate_copy(tmp_path)


def test_snapshot_still_supports_managed_worktrees(tmp_path):
    source, store, rid = paused_project(tmp_path)
    manager = WorkspaceManager(source)
    manager.initialize()
    (source / "source.py").write_text("fixture")
    base = manager.checkpoint(source, "refs/aa2/base", "base")
    work = manager.allocate(rid, "worker", base)
    store.checkpoint(rid, "develop/task/T", {"workspace": str(work)})
    target = tmp_path / "copy"
    snapshot(source, target)
    copied = Path(Store(target).get(rid, "develop/task/T")["workspace"])
    assert copied.is_relative_to(target) and (copied / "source.py").read_text() == "fixture"
    validate_copy(target)


def test_snapshot_preserves_held_build_recovery_and_rebinds_only_locators(tmp_path):
    source, store, rid = paused_project(tmp_path)
    resources = ResourceManager(store, Cleanup())
    backup = store.root / "recovery/build_fixture"
    backup.mkdir(parents=True)
    temporary = store.root / "tmp/held"
    temporary.mkdir(parents=True)
    (temporary / "staged").write_text("retained bytes")
    key = resources.register(backup, "build_recovery", rid)
    holds = [resources.hold(path, "fixture", rid) for path in (backup, temporary)]
    write_json(backup / "journal.json", {"workspace": str(source), "holds": holds, "resource": key,
        "phase": "prepared", "outputs": []})
    original = store.path.read_bytes()
    target = tmp_path / "copy"
    result = snapshot(source, target)
    copied = Store(target)
    assert Path(result["original_database"]).read_bytes() == original == store.path.read_bytes()
    with copied.connect() as db:
        assert all(Path(row[0]).is_relative_to(target) for row in db.execute("SELECT path FROM resource_holds"))
    assert (copied.root / "tmp/held/staged").read_text() == "retained bytes"
    assert json.loads((copied.root / "recovery/build_fixture/journal.json").read_text())["workspace"] == str(target)


def row(command, *, tests, skipped, process, reasons):
    return CheckResult(argv=command.argv, cwd=".", duration=0, source_hash="fixture", environment_hash="fixture",
        exit_code=int(bool(reasons)), process_exit_code=process, tests=tests, skipped=skipped,
        failure_reasons=reasons, report_status="valid", output="hook:pretest\nhook:posttest")


@pytest.mark.parametrize("fault", ["failed-as-skip", "skip-as-failed"])
async def test_manual_matrix_rejects_semantically_wrong_collector_rows(tmp_path, monkeypatch, fault):
    root, modules = tmp_path / "run", tmp_path / "modules"
    root.mkdir()
    for name in ("vitest", "playwright"):
        (modules / name).mkdir(parents=True)
        (modules / name / "package.json").write_text(json.dumps({"version": "fixture"}))
    monkeypatch.setattr(matrix.tempfile, "mkdtemp", lambda **kwargs: str(root))
    calls = []
    async def run(self, commands, *args):
        command = commands[0]
        index = len(calls)
        calls.append(command)
        if index == 12:
            return [row(command, tests=3, skipped=0, process=17, reasons=["process_failed"])]
        if index == 13:
            return [row(command, tests=0, skipped=1 if fault == "failed-as-skip" else 0,
                process=0 if fault == "failed-as-skip" else 1,
                reasons=["insufficient_test_cases"] if fault == "failed-as-skip" else
                ["process_failed", "test_report_failed", "insufficient_test_cases"])]
        if index == 14:
            return [row(command, tests=0, skipped=0, process=1,
                reasons=["process_failed", "test_report_failed", "insufficient_test_cases"])]
        return [row(command, tests=2 if index in {10, 11} else 3,
                    skipped=int(index == 10), process=0, reasons=[])]
    monkeypatch.setattr(matrix.VerificationService, "run", run)
    with pytest.raises(RuntimeError, match="classification"):
        await matrix.main(Path(sys.prefix), [modules])
    assert len(calls) == (14 if fault == "failed-as-skip" else 15)
    assert Store(root).current()["status"] == "PAUSED"


@pytest.mark.parametrize("fault", ["failed-as-skip", "skip-as-failed"])
async def test_manual_report_smoke_rejects_semantically_wrong_collector_rows(tmp_path, monkeypatch, fault):
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setattr(reports.tempfile, "mkdtemp", lambda **kwargs: str(root))
    async def install(*args, **kwargs):
        pass
    monkeypatch.setattr(reports.EnvironmentManager, "install_node", install)
    calls = []
    async def run(self, commands, *args):
        command = commands[0]
        index = len(calls)
        calls.append(command)
        if index == 0:
            return [row(command, tests=1, skipped=1, process=0, reasons=[])]
        if index == 1 and fault != "failed-as-skip":
            return [row(command, tests=0, skipped=1, process=1,
                reasons=["process_failed", "test_report_failed", "insufficient_test_cases"])]
        return [row(command, tests=0, skipped=2 if index == 1 else 1, process=0 if index == 1 else 1,
            reasons=["insufficient_test_cases"] if index == 1 else
            ["process_failed", "test_report_failed", "insufficient_test_cases"])]
    monkeypatch.setattr(reports.VerificationService, "run", run)
    with pytest.raises(RuntimeError, match="classification"):
        await reports.main(Path(sys.prefix))
    assert len(calls) == (2 if fault == "failed-as-skip" else 3)
    assert Store(root).current()["status"] == "PAUSED"
