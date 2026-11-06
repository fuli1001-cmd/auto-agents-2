"""Positive interop evidence can recheck capacity, never stand in for it."""

import asyncio
import copy
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from test_runtime import configured
from test_storage_space import GIB, fake_platform, observation

from auto_agents_2 import storage_space
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.faults import ResourceBoundary
from auto_agents_2.util import write_json

STDERR = "<3>WSL (29113 - ) ERROR: UtilAcceptVsock:271: accept4 failed 110\n"


@pytest.mark.parametrize(
    "stdout,stderr,code,allowed",
    [
        ("", STDERR, 1, True),
        (b"", STDERR.encode(), 1, True),
        (None, STDERR, 1, False),
        ("partial", STDERR, 1, False),
        ("", STDERR + "Access denied\n", 1, False),
        ("", STDERR, 2, False),
        ("", STDERR, True, False),
        ("", STDERR.replace("110", "13"), 1, False),
        ("", "EXAMPLE_PRIVATE unrecognized failure", 1, False),
        ("", STDERR + "\udcff", 1, False),
    ],
)
def test_only_complete_positive_fixed_query_evidence_is_retryable(stdout, stderr, code, allowed):
    error = subprocess.CalledProcessError(code, ["fixed-system-query"], output=stdout, stderr=stderr)
    assert storage_space.interop_timeout(error) is allowed


def test_observation_keeps_private_stderr_and_safe_classification(tmp_path, monkeypatch):
    fake_platform(monkeypatch, distribution="Fixture-Distro")
    state = tmp_path / ".aa2"
    state.mkdir(mode=0o700)
    calls = []

    def fail(distribution):
        calls.append(distribution)
        raise subprocess.CalledProcessError(1, ["fixed-system-query"], output="", stderr=STDERR)

    report = storage_space.observe(state, backing_volume_reader=fail)
    assert calls == ["Fixture-Distro"]
    assert report["errors"] == [
        {"scope": "wsl_backing", "error_type": "CalledProcessError", "reason_code": "wsl_interop_timeout"}
    ]
    assert STDERR not in json.dumps(report)
    assert (state / "storage-diagnostics" / report["diagnostics"][0]["diagnostic_id"]).read_text() == STDERR
    assert storage_space.boundary(report, 5 * GIB, "verification")


def transient():
    report = observation()
    report["volumes"] = report["volumes"][:1]
    report["errors"] = [
        {"scope": "wsl_backing", "error_type": "CalledProcessError", "reason_code": "wsl_interop_timeout"}
    ]
    return report


def setup(tmp_path, monkeypatch, reports):
    config, store = configured(tmp_path)
    settings = config.load().model_dump(mode="json")
    settings["cleanup"]["min_free_bytes"] = 5 * GIB
    write_json(config.path, settings)
    workflow = WorkflowEngine(tmp_path, agent=SimpleNamespace())
    rid = store.admit("interop recovery fixture", {})["id"]
    calls = []

    def observe(path):
        calls.append(path)
        return copy.deepcopy(reports[min(len(calls) - 1, len(reports) - 1)])

    monkeypatch.setattr(storage_space, "observe", observe)
    monkeypatch.setattr(workflow.resources, "collect", lambda: None)
    preserved = {
        "budget/original": {"calls": 7},
        "prototype_approval": {"approved": True, "hash": "original"},
    }
    store.checkpoints(rid, preserved)
    return workflow, store, rid, calls, preserved


async def test_one_transient_failure_recovers_only_after_fresh_real_volume_proof(tmp_path, monkeypatch):
    first = transient()
    second = observation()
    workflow, store, rid, calls, preserved = setup(tmp_path, monkeypatch, [first, second])
    await workflow._storage_boundary(rid, "verification")
    assert len(calls) == 2
    assert store.get(rid, "resources/storage")["volumes"] == second["volumes"]
    assert {key: store.get(rid, key) for key in preserved} == preserved
    with store.connect() as db:
        snapshots = {
            row[0]: json.loads(row[1])
            for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'resources/storage-rechecks/%'",
                (rid,),
            )
        }
        assert db.execute("SELECT COUNT(*) FROM questions WHERE run_id=?", (rid,)).fetchone()[0] == 0
    assert len(snapshots) == 2
    original = next(value for key, value in snapshots.items() if not key.endswith("/result"))
    result = next(value for key, value in snapshots.items() if key.endswith("/result"))
    assert original["observation"] == first and result["observation"] == second
    assert result["outcome"] == "ready" and not result["no_progress"]


@pytest.mark.parametrize("later", ["same", "unknown", "low"])
async def test_recheck_cannot_bypass_persistent_unknown_or_low_capacity(tmp_path, monkeypatch, later):
    second = (
        transient()
        if later == "same"
        else observation(error="PermissionError")
        if later == "unknown"
        else observation(host=GIB)
    )
    workflow, store, rid, calls, preserved = setup(tmp_path, monkeypatch, [transient(), second])
    with pytest.raises(ResourceBoundary) as stopped:
        await workflow._storage_boundary(rid, "verification")
    assert len(calls) == (3 if later == "low" else 2), (
        "Only the existing actual-low cleanup adds its normal final check"
    )
    assert {key: store.get(rid, key) for key in preserved} == preserved
    assert stopped.value.requirement.evidence["minimum_free_bytes"] == 5 * GIB
    if later == "same":
        assert "仍无进展" in stopped.value.requirement.reason


@pytest.mark.parametrize("cause", ["PermissionError", "ValueError", "CalledProcessError"])
async def test_unknown_or_denied_queries_do_not_gain_a_recheck(tmp_path, monkeypatch, cause):
    workflow, store, rid, calls, _ = setup(tmp_path, monkeypatch, [observation(error=cause)])
    with pytest.raises(ResourceBoundary):
        await workflow._storage_boundary(rid, "verification")
    assert len(calls) == 1


async def test_known_low_guest_space_prevents_an_unnecessary_interop_recheck(tmp_path, monkeypatch):
    report = transient()
    report["volumes"][0]["free"] = GIB
    workflow, store, rid, calls, _ = setup(tmp_path, monkeypatch, [report])
    with pytest.raises(ResourceBoundary):
        await workflow._storage_boundary(rid, "verification")
    assert len(calls) == 1


async def test_failure_to_preserve_first_observation_prevents_requery(tmp_path, monkeypatch):
    workflow, store, rid, calls, _ = setup(tmp_path, monkeypatch, [transient(), observation()])
    original = workflow.store.checkpoint

    def fail(rid, key, value):
        if key.startswith("resources/storage-rechecks/"):
            raise OSError("fixture audit unavailable")
        return original(rid, key, value)

    monkeypatch.setattr(workflow.store, "checkpoint", fail)
    with pytest.raises(OSError, match="audit unavailable"):
        await workflow._storage_boundary(rid, "verification")
    assert len(calls) == 1


async def test_concurrent_rechecks_share_only_a_fresh_later_observation(tmp_path, monkeypatch):
    calls = []

    def observe(path):
        calls.append(path)
        return transient() if len(calls) == 1 else observation()

    monkeypatch.setattr(storage_space, "observe", observe)
    monitor = storage_space.StorageMonitor(tmp_path)
    first = await monitor.sample(force=True)
    results = await asyncio.gather(
        monitor.sample(force=True, previous=first), monitor.sample(force=True, previous=first)
    )
    assert len(calls) == 2 and results[0] is results[1]
    monitor._at = time.monotonic() - 6
    await monitor.sample(force=True, previous=first)
    assert len(calls) == 3, "A later but expired healthy report cannot authorize dispatch"


async def test_cancelled_recheck_joins_worker_and_preserves_unknown(tmp_path, monkeypatch):
    workflow, store, rid, calls, _ = setup(tmp_path, monkeypatch, [transient()])
    entered, finish, left = threading.Event(), threading.Event(), threading.Event()

    def observe(path):
        calls.append(path)
        if len(calls) == 1:
            return transient()
        entered.set()
        try:
            assert finish.wait(5)
            return observation()
        finally:
            left.set()

    monkeypatch.setattr(storage_space, "observe", observe)
    task = asyncio.create_task(workflow._storage_boundary(rid, "verification"))

    async def wait_entered():
        while not entered.is_set():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_entered(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not left.is_set()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert left.is_set() and len(calls) == 2
    assert store.get(rid, "resources/storage")["errors"][0]["reason_code"] == "wsl_interop_timeout"
