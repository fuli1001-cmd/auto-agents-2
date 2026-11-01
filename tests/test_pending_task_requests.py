"""Unadjudicated author requests survive siblings, cancellation and restart."""
import asyncio
import copy
from pathlib import Path

import pytest
from test_decisions import configure
from test_recovery_policy import ready
from test_task_feedback import InputAgent, bound_feedback
from test_workflow import engine

from auto_agents_2.faults import StateConflict
from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import Finding, ReviewResult
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.task_baselines import refresh_task_baseline
from auto_agents_2.task_batch import collect_batch
from auto_agents_2.workspace import git, tree_hash


def sibling_observation(workflow, rid):
    IssueLedger(workflow.store, workflow.artifacts).record(rid, "task/T1", ReviewResult(
        approved=False, summary="Sibling review completed", findings=[Finding(
            severity="major", location="part1.py", description="Sibling binding is incomplete",
            evidence="Independent T1 fixture observation",
            resolution="Repair T1 within its existing scope")]), {"task": {"id": "T1"}})


class ParallelRequests(InputAgent):
    def __init__(self, legacy):
        super().__init__()
        self.legacy = legacy
        self.started = set()
        self.barrier = asyncio.Event()
        self.results = {}

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if stage != "implement":
            return await super().invoke(stage, context, schema, *args, **kwargs)
        task_id = context["task"]["id"]
        self.started.add(task_id)
        if len(self.started) == 2:
            self.barrier.set()
        await asyncio.wait_for(self.barrier.wait(), 10)
        result = await super().invoke(stage, context, schema, *args, **kwargs)
        result.summary = f"Original {task_id} request, author call {self.author_calls}"
        if self.legacy:
            result.recovery_request = None
            result.replan_stage = "plan"
            result.replan_reason = "Request formal scope adjudication; no edits authorized"
        self.results.setdefault(task_id, result.model_dump())
        return result


@pytest.mark.parametrize("legacy", [False, True])
async def test_parallel_request_replays_original_after_sibling_and_restart_until_formal_repair(tmp_path, legacy):
    agent = ParallelRequests(legacy)
    workflow, rid, work, plan, _ = ready(tmp_path, agent)
    plan.tasks = [t.model_copy(update={"depends_on": []}) for t in plan.tasks[:2]]
    configure(workflow, execution={"max_parallel_tasks": 2})
    workflow.store.checkpoints(rid, {"recovery/active": None, "plan": plan.model_dump()})
    with pytest.raises(RecoveryNeeded) as first:
        await workflow._develop(rid, work, plan, {"specification": {}})
    assert first.value.failure.task_id == "T1" and agent.author_calls == 2
    task = plan.tasks[1]
    before = workflow.store.get(rid, "develop/task/T2")
    counters = workflow.store.get(rid, "rounds/T2")
    source = tree_hash(Path(before["workspace"]))
    sibling_observation(workflow, rid)
    resumed = engine(tmp_path, agent)
    with pytest.raises(RecoveryNeeded) as pending:
        await resumed._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    assert agent.author_calls == 2
    assert pending.value.failure.proposal == agent.results["T2"]
    assert pending.value.failure.evidence["author_input"] == before["author_input"]
    assert resumed.store.get(rid, "rounds/T2") == counters
    assert not resumed.store.get(rid, "develop/done")
    assert tree_hash(Path(before["workspace"])) == source
    assert resumed.store.get(rid, "plan") == plan.model_dump()
    # Only the existing decision/diagnosis/permit path can dispatch a new author.
    resumed.recovery.submit(rid, pending.value.failure)
    await resumed.recovery.resume(rid)
    dispatched = resumed.store.get(rid, "develop/task/T2")
    assert dispatched["phase"] == "rejected" and dispatched["recovery_permit"]
    assert resumed.store.get(rid, "recovery/active") is None
    with pytest.raises(RecoveryNeeded):
        await resumed._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    assert agent.author_calls == 3
    assert resumed.store.get(rid, "rounds/T2")["round"] == counters["round"] + 1
    assert agent.author["task"]["allowed_paths"] == task.allowed_paths
    assert tree_hash(Path(before["workspace"])) == source


def request_fixture(tmp_path):
    agent = InputAgent()
    workflow, rid, work, plan, _ = ready(tmp_path, agent)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["authority_sources"] = workflow.authority.identity(rid)
    workflow.store.checkpoints(rid, {"recovery/active": None, "develop/task/" + task.id: saved,
        "rounds/" + task.id: {"round": 7, "stalled": 0, "failure": ""}})
    return workflow, rid, work, plan, task, agent


@pytest.mark.parametrize("boundary", ["aggregation", "publication"])
async def test_completed_request_survives_pause_before_batch_failure_is_consumed(tmp_path, monkeypatch, boundary):
    workflow, rid, work, plan, task, agent = request_fixture(tmp_path)
    original = workflow._task_progress

    async def author():
        return await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})

    if boundary == "publication":
        def stop(*args, **kwargs):
            raise asyncio.CancelledError()
        monkeypatch.setattr(workflow, "_task_progress", stop)
        with pytest.raises(asyncio.CancelledError):
            await author()
        monkeypatch.setattr(workflow, "_task_progress", original)
    else:
        observed = asyncio.Event()
        async def completed_request():
            try:
                return await author()
            finally:
                observed.set()
        async def sibling_pause():
            await observed.wait()
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await collect_batch([completed_request(), sibling_pause()])
    sibling_observation(workflow, rid)
    resumed = engine(tmp_path, agent)
    with pytest.raises(RecoveryNeeded):
        await resumed._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    assert agent.author_calls == 1
    assert resumed.store.get(rid, "rounds/" + task.id)["round"] == 7
    assert not resumed.store.get(rid, "develop/done")
    assert not resumed.store.get(rid, "recovery/active")


async def test_pending_request_never_rebinds_to_unrecorded_source_edit(tmp_path):
    workflow, rid, work, plan, task, agent = request_fixture(tmp_path)
    with pytest.raises(RecoveryNeeded):
        await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    before = copy.deepcopy(workflow.store.get(rid, "develop/task/" + task.id))
    (work / "part3.py").write_text("# preserve user edit\n")
    with pytest.raises(StateConflict, match="source changed"):
        await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    assert agent.author_calls == 1
    assert workflow.store.get(rid, "develop/task/" + task.id)["author_input"] == before["author_input"]
    assert (work / "part3.py").read_text() == "# preserve user edit\n"
    assert not workflow.store.get(rid, "develop/done")


async def test_dependency_refresh_archives_pending_request_without_authorizing_it(tmp_path):
    workflow, rid, work, plan, task, agent = request_fixture(tmp_path)
    base = git(work, "rev-parse", "HEAD")
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, task, base, plan, {"specification": {}})
    saved = workflow.store.get(rid, "develop/task/" + task.id)
    dependency = workflow.workspaces.allocate(rid, "dependency-update", base)
    (dependency / "part1.py").write_text("# accepted dependency change\n")
    incoming = workflow.workspaces.checkpoint(dependency, f"refs/aa2/{rid}/dependency-fixture", "dependency fixture")
    updated, _ = refresh_task_baseline(workflow, rid, task, saved, work, incoming, 1)
    assert updated["phase"] == "rejected" and "implementation" not in updated
    assert updated["source_hash"] != saved["source_hash"]
    from auto_agents_2.util import digest
    journal = workflow.store.get(rid, f"recovery/task-baselines/{task.id}/" + digest([str(work), incoming])[:16])
    assert journal["previous_task"]["implementation"] == caught.value.failure.proposal
    assert updated["author_input"] == saved["author_input"]
    assert not workflow.recovery_policy.task_authorized(rid, updated, task)
    assert agent.author_calls == 1
