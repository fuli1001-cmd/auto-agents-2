"""Waiting for external input must leave the artifact writer lock available."""
import asyncio
from types import SimpleNamespace

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.store import Store
from auto_agents_2.util import write_json


GIB = 1024 ** 3


def setup(project):
    config, store = configured(project)
    settings = config.load().model_dump(mode="json")
    settings["cleanup"].update(interval_seconds=1, min_free_bytes=5 * GIB)
    settings["execution"]["wait_poll_seconds"] = 0.01
    write_json(config.path, settings)
    workflow = WorkflowEngine(project, agent=SimpleNamespace())
    rid = store.admit("waiting maintenance fixture", {})["id"]
    store.update(rid, status="RUNNING")
    return workflow, store, rid


def observation(free=20 * GIB):
    return {
        "version": 1, "observed_at": 123.0, "platform": "wsl",
        "volumes": [{"scope": "wsl_backing", "name": "host:E",
                     "free": free, "total": 100 * GIB}],
        "errors": [],
    }


def logs(workflow, rid):
    old = workflow.artifacts.put("previous resolved observation", "check_log")
    workflow.store.checkpoint(rid, "checks", {"logs": [old]})
    current = workflow.artifacts.put("current required observation", "check_log")
    workflow.store.checkpoint(rid, "checks", {"logs": [current]})
    return old, current


async def stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_waiting_response_preserves_evidence_and_resumes_collection(tmp_path, monkeypatch):
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
    cycles = [asyncio.Event(), asyncio.Event()]
    observed = []

    async def sample(**kwargs):
        observed.append(kwargs)
        cycles[min(len(observed), 2) - 1].set()
        return observation(4 * GIB)

    monkeypatch.setattr(workflow.storage, "sample", sample)
    question = asyncio.create_task(workflow._ask(rid, "technical-fixture", "Keep the original decision?"))
    await asyncio.sleep(0)
    assert store.run(rid)["status"] == "WAITING_USER"
    maintenance = asyncio.create_task(workflow._collect_periodically(rid, question))
    try:
        await asyncio.wait_for(cycles[0].wait(), 10)
        # The observed maintenance turn finishes synchronously before yielding.
        assert not workflow.artifacts.released(old)
        assert workflow.artifacts.read(current) == "current required observation"
        assert not disposable.exists() and held.exists()
        pending = store.pending(rid)
        assert len(pending) == 1 and pending[0]["answer"] is None
        external = Store(tmp_path)
        await asyncio.wait_for(asyncio.to_thread(external.answer, pending[0]["id"], "continue"), 3)
        assert await asyncio.wait_for(question, 3) == "continue"
        assert store.run(rid)["status"] == "RUNNING"
        await asyncio.wait_for(cycles[1].wait(), 10)
        assert workflow.artifacts.released(old)
        assert workflow.artifacts.read(current) == "current required observation"
        assert held.exists()
        assert store.question(pending[0]["id"])["answer"] == "continue"
        assert {key: store.get(rid, key) for key in preserved} == preserved
        assert store.get(rid, "resources/storage")["minimum_free_bytes"] == 5 * GIB
        with store.connect() as db:
            warnings = db.execute("SELECT data FROM events WHERE run_id=? AND kind='resources.space_low'",
                                  (rid,)).fetchall()
        assert len(warnings) == 1
        assert all(row == {"force": True} for row in observed)
    finally:
        await stop(maintenance, question)


async def test_waiting_during_host_observation_defers_full_scan(tmp_path, monkeypatch):
    workflow, store, rid = setup(tmp_path)
    old, current = logs(workflow, rid)
    probing, release, collected = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def sample(**kwargs):
        probing.set()
        await release.wait()
        return observation()

    collect = workflow.resources.collect

    def collect_resources():
        result = collect()
        collected.set()
        return result

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.resources, "collect", collect_resources)
    maintenance = asyncio.create_task(workflow._collect_periodically(rid, asyncio.current_task()))
    question = None
    try:
        await asyncio.wait_for(probing.wait(), 10)
        assert store.run(rid)["status"] == "RUNNING"
        question = asyncio.create_task(workflow._ask(rid, "during-observation", "Waiting for input"))
        await asyncio.sleep(0)
        assert store.run(rid)["status"] == "WAITING_USER"
        release.set()
        await asyncio.wait_for(collected.wait(), 10)
        assert not workflow.artifacts.released(old)
        assert workflow.artifacts.read(current)
        assert store.pending(rid)[0]["answer"] is None
    finally:
        release.set()
        await stop(maintenance, *([question] if question is not None else []))


async def test_pausing_waiting_controller_keeps_final_gc_and_unanswered_request(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("waiting pause fixture", {})["id"]
    preserved = {"recovery/count/fixture": 7, "authority/fixture": {"approved": True}}
    workflow.store.checkpoints(rid, preserved)
    waiting, block = asyncio.Event(), asyncio.Event()
    saved = {}
    collected_states = []
    collect = workflow.resources.collect_artifacts_safely

    def collect_artifacts():
        collected_states.append(workflow.store.run(rid)["status"])
        return collect()

    async def sample(**kwargs):
        return observation()

    async def respond(question):
        waiting.set()
        await block.wait()

    async def execute(run_id):
        saved["old"], saved["current"] = logs(workflow, run_id)
        return await workflow._ask(run_id, "pause-fixture", "Keep this unanswered request")

    workflow.question_handler = respond
    monkeypatch.setattr(workflow, "_execute", execute)
    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.resources, "collect_artifacts_safely", collect_artifacts)
    task = asyncio.create_task(workflow.start(run_id=rid))
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        assert workflow.store.run(rid)["status"] == "WAITING_USER"
        pending = workflow.store.pending(rid)[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert workflow.store.run(rid)["status"] == "PAUSED"
        assert collected_states[-1] == "PAUSED"
        assert workflow.artifacts.released(saved["old"])
        assert workflow.artifacts.read(saved["current"])
        assert workflow.store.question(pending["id"])["answer"] is None
        assert {key: workflow.store.get(rid, key) for key in preserved} == preserved
    finally:
        await stop(task)
