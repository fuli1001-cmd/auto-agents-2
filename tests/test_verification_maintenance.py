"""Periodic full graph GC must yield to complete formal verification batches."""
import asyncio
import sys

import pytest
from test_waiting_maintenance import GIB, logs, observation, setup, stop
from test_workflow import CodingAgent, engine

from auto_agents_2.faults import ResourceBoundary
from auto_agents_2.models import Command


def collection_cycles(workflow, monkeypatch):
    cycles = asyncio.Queue()
    collect = workflow.resources.collect

    def collect_resources():
        result = collect()
        cycles.put_nowait(None)
        return result

    monkeypatch.setattr(workflow.resources, "collect", collect_resources)
    return cycles


async def next_cycle(cycles):
    # The maintenance turn completes its synchronous GC decision before yielding.
    await asyncio.wait_for(cycles.get(), 10)


async def test_active_batch_keeps_artifacts_but_retains_space_and_resource_maintenance(tmp_path, monkeypatch):
    workflow, store, rid = setup(tmp_path)
    old, current = logs(workflow, rid)
    preserved = {"recovery/count/fixture": 7, "authority/fixture": {"approved": True}}
    store.checkpoints(rid, preserved)
    disposable = store.root / "tmp" / "disposable"
    disposable.mkdir(parents=True)
    workflow.resources.register(disposable, "temporary", rid, refs=0)
    held = store.root / "tmp" / "held"
    held.mkdir()
    workflow.resources.register(held, "temporary", rid, refs=0)
    workflow.resources.hold(held, "pending consumer", rid)
    entered, finish = asyncio.Event(), asyncio.Event()
    free = 20 * GIB
    commands = [Command(argv=[sys.executable, "-c", "pass"], timeout=420)]
    before = [c.model_dump() for c in commands]
    expected = [{"fixture": "original result"}]

    async def sample(**kwargs):
        return observation(free)

    async def verify(actual, work, environment):
        assert actual is commands and work == tmp_path and environment is None
        entered.set()
        await finish.wait()
        return expected

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.verifier, "run", verify)
    cycles = collection_cycles(workflow, monkeypatch)
    batch = asyncio.create_task(workflow._run_checks(rid, commands, tmp_path, None))
    maintenance = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        free = 4 * GIB
        maintenance = asyncio.create_task(workflow._collect_periodically(rid, asyncio.current_task()))
        await next_cycle(cycles)
        assert not workflow.artifacts.released(old)
        assert workflow.artifacts.read(current) == "current required observation"
        assert not disposable.exists() and held.exists()
        assert not batch.done()
        assert store.get(rid, "resources/storage")["minimum_free_bytes"] == 5 * GIB
        with store.connect() as db:
            count = db.execute("SELECT count(*) FROM events WHERE run_id=? AND kind='resources.space_low'",
                               (rid,)).fetchone()[0]
        assert count == 1
        free = 20 * GIB
        finish.set()
        assert await asyncio.wait_for(batch, 10) is expected
        await next_cycle(cycles)
        assert workflow.artifacts.released(old)
        assert workflow.artifacts.read(current)
        assert held.exists()
        assert [c.model_dump() for c in commands] == before
        assert {key: store.get(rid, key) for key in preserved} == preserved
    finally:
        finish.set()
        await stop(batch, *([maintenance] if maintenance is not None else []))


async def test_batch_entering_during_maintenance_observation_is_protected(tmp_path, monkeypatch):
    workflow, store, rid = setup(tmp_path)
    old, current = logs(workflow, rid)
    probing, release_probe, entered, finish = (asyncio.Event() for _ in range(4))
    calls = 0

    async def sample(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            probing.set()
            await release_probe.wait()
        return observation()

    async def verify(*args):
        entered.set()
        await finish.wait()
        return []

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.verifier, "run", verify)
    cycles = collection_cycles(workflow, monkeypatch)
    maintenance = asyncio.create_task(workflow._collect_periodically(rid, asyncio.current_task()))
    batch = None
    try:
        await asyncio.wait_for(probing.wait(), 10)
        batch = asyncio.create_task(workflow._run_checks(rid, [], tmp_path, None))
        await asyncio.wait_for(entered.wait(), 10)
        release_probe.set()
        await next_cycle(cycles)
        assert not workflow.artifacts.released(old)
        assert workflow.artifacts.read(current)
        finish.set()
        await asyncio.wait_for(batch, 10)
        await next_cycle(cycles)
        assert workflow.artifacts.released(old)
    finally:
        release_probe.set()
        finish.set()
        await stop(maintenance, *([batch] if batch is not None else []))


@pytest.mark.parametrize("nested", [False, True])
async def test_last_overlapping_or_nested_batch_releases_periodic_gc(tmp_path, monkeypatch, nested):
    workflow, store, rid = setup(tmp_path)
    old, current = logs(workflow, rid)
    outer_commands, inner_commands = ["outer"], ["inner"]
    outer_entered, inner_entered, inner_done, finish_inner, finish_outer = (
        asyncio.Event() for _ in range(5)
    )

    async def sample(**kwargs):
        return observation()

    async def verify(commands, work, environment):
        if commands is outer_commands:
            outer_entered.set()
            if nested:
                assert await workflow._run_checks(rid, inner_commands, work, environment) == ["inner-result"]
                inner_done.set()
            await finish_outer.wait()
            return ["outer-result"]
        assert commands is inner_commands
        inner_entered.set()
        await finish_inner.wait()
        return ["inner-result"]

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.verifier, "run", verify)
    cycles = collection_cycles(workflow, monkeypatch)
    outer = asyncio.create_task(workflow._run_checks(rid, outer_commands, tmp_path, None))
    inner = maintenance = None
    try:
        await asyncio.wait_for(outer_entered.wait(), 10)
        if not nested:
            inner = asyncio.create_task(workflow._run_checks(rid, inner_commands, tmp_path, None))
        await asyncio.wait_for(inner_entered.wait(), 10)
        maintenance = asyncio.create_task(workflow._collect_periodically(rid, asyncio.current_task()))
        await next_cycle(cycles)
        assert not workflow.artifacts.released(old)
        finish_inner.set()
        if nested:
            await asyncio.wait_for(inner_done.wait(), 10)
        else:
            assert await asyncio.wait_for(inner, 10) == ["inner-result"]
        await next_cycle(cycles)
        assert not outer.done()
        assert not workflow.artifacts.released(old)
        finish_outer.set()
        assert await asyncio.wait_for(outer, 10) == ["outer-result"]
        await next_cycle(cycles)
        assert workflow.artifacts.released(old)
        assert workflow.artifacts.read(current)
    finally:
        finish_inner.set()
        finish_outer.set()
        await stop(outer, *([inner] if inner is not None else []),
                   *([maintenance] if maintenance is not None else []))


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_batch_keeps_window_until_verifier_cleanup_finishes(tmp_path, monkeypatch, cancel):
    workflow, store, rid = setup(tmp_path)
    old, current = logs(workflow, rid)
    entered, fail, cleaning, cleaned = (asyncio.Event() for _ in range(4))

    async def sample(**kwargs):
        return observation()

    async def verify(*args):
        entered.set()
        try:
            await fail.wait()
            raise RuntimeError("fixture verification failure")
        finally:
            cleaning.set()
            await cleaned.wait()

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.verifier, "run", verify)
    cycles = collection_cycles(workflow, monkeypatch)
    batch = asyncio.create_task(workflow._run_checks(rid, [], tmp_path, None))
    maintenance = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if cancel:
            batch.cancel()
        else:
            fail.set()
        await asyncio.wait_for(cleaning.wait(), 10)
        maintenance = asyncio.create_task(workflow._collect_periodically(rid, asyncio.current_task()))
        await next_cycle(cycles)
        assert not batch.done()
        assert not workflow.artifacts.released(old)
        cleaned.set()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await asyncio.wait_for(batch, 10)
        await next_cycle(cycles)
        assert workflow.artifacts.released(old)
        assert workflow.artifacts.read(current)
    finally:
        cleaned.set()
        fail.set()
        await stop(batch, *([maintenance] if maintenance is not None else []))


async def test_rejected_space_boundary_does_not_leave_gc_deferred(tmp_path, monkeypatch):
    workflow, store, rid = setup(tmp_path)
    old, current = logs(workflow, rid)
    probing, release_probe = asyncio.Event(), asyncio.Event()
    calls = 0

    async def sample(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            probing.set()
            await release_probe.wait()
        return observation(4 * GIB)

    async def forbidden_verify(*args):
        pytest.fail("storage rejection must precede verifier dispatch")

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.verifier, "run", forbidden_verify)
    cycles = collection_cycles(workflow, monkeypatch)
    batch = asyncio.create_task(workflow._run_checks(rid, [], tmp_path, None))
    maintenance = None
    try:
        await asyncio.wait_for(probing.wait(), 10)
        maintenance = asyncio.create_task(workflow._collect_periodically(rid, asyncio.current_task()))
        await next_cycle(cycles)
        assert not workflow.artifacts.released(old)
        release_probe.set()
        with pytest.raises(ResourceBoundary):
            await asyncio.wait_for(batch, 10)
        # Boundary pressure collection is ordinary resource GC, not a periodic turn.
        while not cycles.empty():
            cycles.get_nowait()
        await next_cycle(cycles)
        assert workflow.artifacts.released(old)
        assert workflow.artifacts.read(current)
    finally:
        release_probe.set()
        await stop(batch, *([maintenance] if maintenance is not None else []))


async def test_controller_pause_preserves_interrupted_batch_and_final_gc(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("verification pause fixture", {})["id"]
    commands = [Command(argv=[sys.executable, "-c", "pass"], timeout=420)]
    before = [c.model_dump() for c in commands]
    preserved = {"recovery/count/fixture": 7, "authority/fixture": {"approved": True}}
    workflow.store.checkpoints(rid, preserved)
    entered, block = asyncio.Event(), asyncio.Event()
    saved, collected_states = {}, []
    collect = workflow.resources.collect_artifacts_safely

    def collect_artifacts():
        collected_states.append(workflow.store.run(rid)["status"])
        return collect()

    async def sample(**kwargs):
        return observation()

    async def check(*args):
        entered.set()
        await block.wait()

    async def execute(run_id):
        saved["old"], saved["current"] = logs(workflow, run_id)
        return await workflow._checks(run_id, commands, tmp_path, None)

    monkeypatch.setattr(workflow, "_execute", execute)
    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.verifier, "_check", check)
    monkeypatch.setattr(workflow.resources, "collect_artifacts_safely", collect_artifacts)
    task = asyncio.create_task(workflow.start(run_id=rid))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert workflow.store.run(rid)["status"] == "PAUSED"
        assert collected_states[-1] == "PAUSED"
        assert workflow.artifacts.released(saved["old"])
        assert workflow.artifacts.read(saved["current"])
        with workflow.store.connect() as db:
            keys = [r[0] for r in db.execute(
                "SELECT key FROM checkpoints WHERE run_id=? AND key LIKE 'verification/batches/%'", (rid,))]
        assert len(keys) == 1
        batch = workflow.store.get(rid, keys[0])
        assert batch["phase"] == "interrupted"
        assert batch["checks"] == [] and batch["commands"] == before
        assert {key: workflow.store.get(rid, key) for key in preserved} == preserved
    finally:
        await stop(task)
