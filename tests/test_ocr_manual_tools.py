import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from manual_command_matrix import main as command_matrix
from manual_node_browser import main as node_browser
from manual_recovery_snapshot import snapshot
from manual_research_presentation import RULES, Decision, Verdicts, citations_valid
from manual_support import finalize

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.config import Cleanup, ConfigStore
from auto_agents_2.resources import ResourceManager
from auto_agents_2.state_codec import checkpoint_query_db, put_wire_db
from auto_agents_2.store import Store
from auto_agents_2.util import digest


def paused_project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    ConfigStore(source).initialize()
    store = Store(source)
    rid = store.admit("fixture", {})["id"]
    store.update(rid, status="PAUSED")
    return source, store, rid


def test_snapshot_preserves_semantic_records_and_artifact_bytes(tmp_path):
    source, store, rid = paused_project(tmp_path)
    text = "Original approval referring to " + str(source)
    question = store.request(rid, "approval", text, "fixed-identity")
    store.answer(question["id"], text, "fixed-identity")
    artifact = ArtifactStore(store.root).put({"original": text})
    store.checkpoint(rid, "evidence", artifact)
    store.checkpoint(rid, "develop/task/T", {"workspace": str(source / "work"), "feedback": {"message": text}})
    store.update(rid, status="PAUSED")
    original_db = store.path.read_bytes()
    destination = tmp_path / "copy"
    result = snapshot(source, destination)
    copied = Store(destination)
    with copied.connect() as db:
        row = db.execute("SELECT prompt,answer,artifact_hash FROM questions").fetchone()
    assert tuple(row) == (text, text, "fixed-identity")
    assert ArtifactStore(copied.root).json(artifact) == {"original": text}
    assert copied.get(rid, "develop/task/T") == {"workspace": str(destination / "work"), "feedback": {"message": text}}
    assert Path(result["original_database"]).read_bytes() == original_db == store.path.read_bytes()
    assert result["live_writes"] == 0


def test_snapshot_rejects_copied_config_link_before_live_write(tmp_path):
    source, store, _ = paused_project(tmp_path)
    actual = source / "actual-config.json"
    original = json.dumps({"path": str(source)})
    actual.write_text(original)
    config = store.root / "config.json"
    config.unlink()
    config.symlink_to(actual)
    with pytest.raises(ValueError, match="symlink"):
        snapshot(source, tmp_path / "copy")
    assert actual.read_text() == original


@pytest.mark.parametrize("sidecar,symlink", [("original-state.sqlite", False), ("original-state.sqlite", True),
                                            ("snapshot.json", False), ("snapshot.json", True)])
def test_snapshot_reserves_sidecars_without_overwriting_existing_data(tmp_path, sidecar, symlink):
    source, _, _ = paused_project(tmp_path)
    target = tmp_path / "previous-data"
    target.write_text("preserve")
    occupied = tmp_path / ("copy-" + sidecar)
    if symlink:
        occupied.symlink_to(target)
    else:
        occupied.write_text("preserve")
    with pytest.raises(FileExistsError):
        snapshot(source, tmp_path / "copy")
    assert occupied.read_text() == "preserve" and target.read_text() == "preserve"
    assert not (tmp_path / "copy").exists()


def test_manual_finalization_runs_all_actions_and_retains_primary_error():
    called = []
    def failing():
        called.append("failed")
        raise OSError("disk full")
    with pytest.raises(ValueError, match="primary") as caught:
        try:
            raise ValueError("primary")
        finally:
            finalize(failing, lambda: called.append("cleaned"))
    assert called == ["failed", "cleaned"]
    assert caught.value.__notes__


async def test_empty_command_matrix_cannot_succeed(tmp_path, monkeypatch):
    monkeypatch.setattr("manual_command_matrix.tempfile.mkdtemp", lambda **kw: pytest.fail("empty matrix must fail before admission"))
    with pytest.raises(ValueError, match="At least one"):
        await command_matrix(tmp_path, [])


async def test_node_smoke_setup_failure_records_pause_and_failure(tmp_path, monkeypatch):
    project = tmp_path / "fixture"
    project.mkdir()
    monkeypatch.setattr("manual_node_browser.tempfile.mkdtemp", lambda **kw: str(project))
    with pytest.raises(FileNotFoundError):
        await node_browser(tmp_path / "prefix", tmp_path / "missing-modules")
    assert Store(project).current()["status"] == "PAUSED"
    assert json.loads((project / "failure.json").read_text())["passed"] is False


def test_manual_success_gates_survive_python_optimization(tmp_path):
    project, modules = tmp_path / "project", tmp_path / "modules"
    project.mkdir()
    modules.mkdir()
    script = r"""
import asyncio, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, 'tests')
import manual_node_browser as tool
from auto_agents_2.models import CheckResult
from auto_agents_2.store import Store
project, modules = Path(sys.argv[1]), Path(sys.argv[2])
tool.tempfile.mkdtemp = lambda **kw: str(project)
async def ready(*args): pass
async def fail(*args):
    return [CheckResult(argv=['test'], cwd='.', exit_code=1, process_exit_code=1, output='failed',
                        duration=0, source_hash='source', environment_hash='fixture')]
tool.EnvironmentManager.prepare_node_browsers = ready
tool.VerificationService = lambda *a: SimpleNamespace(run=fail)
try:
    asyncio.run(tool.main(Path(sys.prefix), modules))
except RuntimeError:
    if Store(project).current()['status'] != 'PAUSED':
        raise RuntimeError('Failure did not pause the run')
else:
    raise RuntimeError('Optimized manual script accepted failed evidence')
"""
    result = subprocess.run([sys.executable, "-O", "-c", script, str(project), str(modules)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads((project / "failure.json").read_text())["passed"] is False


def test_citation_oracle_requires_correct_version_and_exact_rule():
    rows = [Decision(claim_id=claim, supported=True, evidence=f"FIXTURE API VERSION {version}\n{rule}")
            for claim, (version, rule) in RULES.items()]
    answer = Verdicts(verdicts=rows)
    assert citations_valid(answer)
    for evidence in ("", "fabricated rule", "FIXTURE API VERSION 1\nMaximum page size: 200.",
                     "FIXTURE API VERSION 2\nMaximum page size: 200. All scopes are optional."):
        altered = answer.model_copy(deep=True)
        altered.verdicts[4].evidence = evidence
        assert not citations_valid(altered)


def test_snapshot_preserves_unmodified_wire_and_root_slots_when_rebinding_a_key(tmp_path):
    source, store, rid = paused_project(tmp_path)
    work = source / "work"
    work.mkdir()
    target = tmp_path / "copy"
    base = "frozen-base"
    key = "recovery/task-baselines/T/" + digest([str(work), base])[:16]
    new_key = "recovery/task-baselines/T/" + digest([str(target / "work"), base])[:16]
    feedback = "Original approval mentioning " + str(source)
    wires = {"z/non-whitelist": ' { "kept" : true }\n',
             "invocation/no-locator": ' { "result" : { "approved" : true } }\n',
             key: json.dumps({"workspace": str(work), "base": base, "feedback": feedback})}
    with store.connect() as db:
        for name, wire in wires.items():
            assert not db.execute("SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?", (rid, name)).fetchone()
            put_wire_db(db, rid, name, wire)
        before = [tuple(row) for row in checkpoint_query_db(db,
            "SELECT slot_order,key,slot_order AS data FROM checkpoint_records WHERE run_id=? ORDER BY slot_order",
            (rid,), wire_index=2)]
    original = store.path.read_bytes()
    result = snapshot(source, target)
    copied = Store(target)
    with copied.connect() as db:
        after = [tuple(row) for row in checkpoint_query_db(db,
            "SELECT slot_order,key,slot_order AS data FROM checkpoint_records WHERE run_id=? ORDER BY slot_order",
            (rid,), wire_index=2)]
    assert [(slot, name) for slot, name, _ in after] == [(slot, new_key if name == key else name) for slot, name, _ in before]
    after_wire = {name: wire for _, name, wire in after}
    assert key not in after_wire and new_key in after_wire
    assert after_wire["z/non-whitelist"] == wires["z/non-whitelist"]
    assert after_wire["invocation/no-locator"] == wires["invocation/no-locator"]
    assert json.loads(after_wire[new_key]) == {"workspace": str(target / "work"), "base": base, "feedback": feedback}
    assert Path(result["original_database"]).read_bytes() == original == store.path.read_bytes()


@pytest.mark.parametrize("damage", ["non-whitelist-bad-json", "key-collision"])
def test_snapshot_root_failure_rolls_back_all_target_metadata(tmp_path, damage):
    source, store, rid = paused_project(tmp_path)
    work = store.root / "worktrees" / "fixture"
    work.mkdir(parents=True)
    ResourceManager(store, Cleanup()).register(work, "temporary", rid)
    target = tmp_path / "copy"
    base = "frozen-base"
    key = "recovery/task-baselines/T/original"
    collision = "recovery/task-baselines/T/" + digest([str(target / work.relative_to(source)), base])[:16]
    wires = {"develop/task/T": json.dumps({"workspace": str(work), "feedback": str(source)})}
    if damage == "non-whitelist-bad-json":
        wires["outside/whitelist"] = '{"broken":'
    else:
        wires[key] = json.dumps({"workspace": str(work), "base": base})
        wires[collision] = ' { "preserve" : true } '
    with store.connect() as db:
        for name, wire in wires.items():
            assert not db.execute("SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?", (rid, name)).fetchone()
            put_wire_db(db, rid, name, wire)
        before = [tuple(row) for row in checkpoint_query_db(db,
            "SELECT slot_order,key,slot_order AS data FROM checkpoint_records WHERE run_id=? ORDER BY slot_order",
            (rid,), wire_index=2)]
        resources_before = [tuple(row) for row in db.execute("SELECT * FROM resources ORDER BY id")]
    original = store.path.read_bytes()
    if damage == "non-whitelist-bad-json":
        with pytest.raises(json.JSONDecodeError):
            snapshot(source, target)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            snapshot(source, target)
    copied = Store(target)
    with copied.connect() as db:
        after = [tuple(row) for row in checkpoint_query_db(db,
            "SELECT slot_order,key,slot_order AS data FROM checkpoint_records WHERE run_id=? ORDER BY slot_order",
            (rid,), wire_index=2)]
        assert [tuple(row) for row in db.execute("SELECT * FROM resources ORDER BY id")] == resources_before
    assert after == before
    assert store.path.read_bytes() == original
    assert (tmp_path / "copy-original-state.sqlite").read_bytes() == original
    assert (tmp_path / "copy-snapshot.json").read_bytes() == b""
