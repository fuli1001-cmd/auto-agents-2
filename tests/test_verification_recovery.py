import asyncio
import sqlite3

import pytest
from test_manifest_references import plan
from test_workflow import CodingAgent, engine

from auto_agents_2.models import ImplementationResult
from auto_agents_2.util import digest
from auto_agents_2.workspace import apply_prepared, prepare_changes, tree_hash


async def blocked_workflow(tmp_path, *, failing=False):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement":
                assert context["feedback"]["checks"][0]["tests"] == 1
                assert context["feedback"]["checks"][0]["process_exit_code"] == 0
                assert context["feedback"]["controller_recheck"]["previous_request"]["replan_reason"] == "recheck report"
                self.calls.append((stage, fresh))
                return ImplementationResult(summary="Existing implementation now has actual evidence", changes=[])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    workflow = engine(tmp_path, Agent())
    rid = workflow.store.admit("addition", {})["id"]
    proposal = plan()
    proposal.tasks[0].checks[0].argv.insert(1, "-B")
    workflow.store.checkpoint(rid, "plan", proposal.model_dump())
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "T1-p0", base)
    change = await CodingAgent().invoke("implement", {}, ImplementationResult, work, rid, "task")
    apply_prepared(work, prepare_changes(work, change.changes, proposal.tasks[0].allowed_paths, protected=False))
    if failing:
        (work / "calc.py").write_text("def add(a,b): return 0\n")
    old = (await workflow.verifier.run(proposal.tasks[0].checks, work, await workflow._environment()))[0]
    old = old.model_copy(update={"checker_version": "verification-v2", "process_exit_code": None,
                                 "exit_code": 1, "tests": 0, "report": {}, "test_ids": [], "passed_test_ids": []})
    saved = {"phase": "implementing", "feedback": {"checks": [old.model_dump()]}}
    response = ImplementationResult(summary="Missing evidence", changes=[], replan_stage="plan", replan_reason="recheck report")
    workflow.store.checkpoints(rid, {
        "develop/task/T1": saved, "rounds/T1": {"round": 2, "stalled": 1, "failure": "old"},
        "invocation/develop/task/T1": {"identity": "old", "result": response.model_dump()},
        "replanning/progress": {"tracked": {}, "evaluations": 4, "stalled": 3},
    })
    pending = workflow.store.prepare_replan(rid, "plan", response.replan_reason)
    workflow.store.request(rid, f"replan-stalled/{pending['evaluation']}", "stop", digest(pending))
    return workflow, rid, proposal, base, work, pending


@pytest.mark.asyncio
async def test_legacy_report_recovery_rechecks_without_resetting_budgets_or_skipping_review(tmp_path):
    workflow, rid, proposal, base, work, pending = await blocked_workflow(tmp_path)
    progress = workflow.store.get(rid, "replanning/progress")
    rounds = workflow.store.get(rid, "rounds/T1")
    source = tree_hash(work)
    assert await workflow._refresh_legacy_task_checks(rid)
    assert workflow.store.get(rid, "replanning/pending") is None
    assert workflow.store.get(rid, "rounds/T1") == rounds
    assert workflow.store.get(rid, "replanning/progress") == progress
    assert not workflow.store.pending(rid) and not workflow.agent.calls
    recovery = workflow.store.get(rid, "candidate/verification-recovery/T1/p0")
    assert recovery["pending"] == pending and recovery["checks"][0]["tests"] == 1
    assert tree_hash(work) == source and recovery["recovered"]
    await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert ("review", True) in workflow.agent.calls
    assert workflow.store.get(rid, "develop/task/T1")["commit"]
    assert workflow.store.get(rid, "rounds/T1") == rounds


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["actual_failure", "user_change", "other_replan"])
async def test_recovery_cannot_clear_unproven_or_unrelated_replan(tmp_path, condition):
    workflow, rid, _, _, work, pending = await blocked_workflow(tmp_path, failing=condition == "actual_failure")
    if condition == "user_change":
        (work / "user.txt").write_text("preserve user change")
    if condition == "other_replan":
        pending["reason"] = "unrelated design issue"
        workflow.store.checkpoint(rid, "replanning/pending", pending)
    assert not await workflow._refresh_legacy_task_checks(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert workflow.store.pending(rid)
    assert work.exists() and not workflow.agent.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["interrupt", "transaction"])
async def test_recovery_interruption_retries_without_losing_pending_state(tmp_path, fault):
    workflow, rid, _, _, _, pending = await blocked_workflow(tmp_path)
    run_checks = workflow.verifier.run

    async def interrupt(*args, **kwargs):
        raise asyncio.CancelledError()

    if fault == "interrupt":
        workflow.verifier.run = interrupt
        error = asyncio.CancelledError
    else:
        with workflow.store.connect() as db:
            db.execute("CREATE TRIGGER fail_recovery BEFORE DELETE ON checkpoints "
                       "WHEN OLD.key='replanning/pending' BEGIN SELECT RAISE(ABORT,'injected'); END")
        error = sqlite3.IntegrityError
    with pytest.raises(error):
        await workflow._refresh_legacy_task_checks(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert workflow.store.get(rid, "develop/task/T1")["feedback"]["checks"][0]["tests"] == 0
    workflow.verifier.run = run_checks
    if fault == "transaction":
        with workflow.store.connect() as db:
            db.execute("DROP TRIGGER fail_recovery")
    assert await workflow._refresh_legacy_task_checks(rid)
