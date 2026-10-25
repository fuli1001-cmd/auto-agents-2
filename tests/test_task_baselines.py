import asyncio
import json
import sys
from pathlib import Path

import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import AA2Error
from auto_agents_2.models import Change, Command, ImplementationResult, PlanResult, TaskSpec
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.workspace import apply_prepared, git, prepare_changes, tree_hash


def setup_candidate(tmp_path, agent, conflict=False):
    from authority_review_fixtures import seed_original
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("retain candidate with updated dependencies", {})["id"]
    seed_original(workflow, rid)
    (tmp_path / "value.py").write_text("value = 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_value.py").write_text(
        f"from value import value\ndef test_value(): assert value == {30 if conflict else 20}\n")
    workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/base", "fixture baseline")
    base = workflow.workspaces.committed_baseline(rid, "main")
    worker = workflow.workspaces.allocate(rid, "retained", base)
    if conflict:
        (worker / "value.py").write_text("value = 10\n")
    else:
        (worker / "notes.txt").write_text("unfinished candidate\n")
    integration = workflow.workspaces.allocate(rid, "integration", base)
    (integration / "value.py").write_text("value = 20\n")
    incoming = workflow.workspaces.checkpoint(integration, f"refs/aa2/{rid}/parent-fix", "accepted dependency fix")
    task = TaskSpec(id="CHILD", origin_id="CHILD", title="retain work", description="Integrate the retained work with the accepted dependency fix",
                    requirement_ids=["R1"], allowed_paths=["value.py", "notes.txt", "tests/test_value.py"],
                    acceptance=["retained candidate and current dependency both apply"],
                    checks=[Command(argv=[sys.executable, "-m", "pytest", "tests/test_value.py"], expected_tests=1)])
    plan = PlanResult(summary="one task", document="one task", tasks=[task], final_checks=task.checks)
    workflow.store.checkpoints(rid, {"develop/task/CHILD": {"phase": "reverify", "workspace": str(worker), "source_hash": tree_hash(worker)},
                                    "rounds/CHILD": {"round": 1, "stalled": 0, "failure": ""}})
    return workflow, rid, task, plan, worker, incoming


async def test_pending_candidate_rechecks_against_new_dependency_without_reimplementation(tmp_path):
    agent = CodingAgent()
    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, agent)
    commit = await workflow._task(rid, task, incoming, plan, {"specification": {}})
    assert not any(stage == "implement" for stage, _ in agent.calls)
    assert (worker / "value.py").read_text() == "value = 20\n"
    assert (worker / "notes.txt").read_text() == "unfinished candidate\n"
    assert workflow.workspaces.is_ancestor(incoming, commit)
    assert workflow.store.get(rid, "rounds/CHILD")["round"] == 1
    assert workflow.store.get(rid, "develop/task/CHILD")["checks"][0]["tests"] == 1


async def test_git_infrastructure_failure_does_not_dispatch_source_repair(tmp_path, monkeypatch):
    agent = CodingAgent()
    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, agent)
    def unavailable(*args):
        raise AA2Error("Git lock unavailable")
    monkeypatch.setattr(workflow.workspaces, "integrate", unavailable)
    with pytest.raises(AA2Error, match="Git lock unavailable"):
        await workflow._task(rid, task, incoming, plan, {"specification": {}})
    assert not agent.calls and (worker / "notes.txt").read_text() == "unfinished candidate\n"
    assert workflow.store.get(rid, "rounds/CHILD")["round"] == 1


@pytest.mark.parametrize("user_edit", [False, True])
async def test_dependency_merge_recovers_after_git_before_sqlite_and_protects_user_edits(tmp_path, monkeypatch, user_edit):
    agent = CodingAgent()
    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, agent)
    save = workflow.store.checkpoints

    def interrupt(run_id, values, **kwargs):
        if any(key.startswith("recovery/task-baselines/") and value["phase"] == "merged" for key, value in values.items()):
            raise asyncio.CancelledError()
        return save(run_id, values, **kwargs)

    with monkeypatch.context() as patching:
        patching.setattr(workflow.store, "checkpoints", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await workflow._task(rid, task, incoming, plan, {"specification": {}})
    merged = git(worker, "rev-parse", "HEAD")
    assert workflow.workspaces.is_ancestor(incoming, merged)
    workflow = engine(tmp_path, agent)
    if user_edit:
        (worker / "notes.txt").write_text("user correction\n")
        with pytest.raises(AA2Error, match="unknown changes"):
            await workflow._task(rid, task, incoming, plan, {"specification": {}})
        assert (worker / "notes.txt").read_text() == "user correction\n"
        assert not agent.calls
    else:
        def repeated_merge(*args):
            raise AssertionError("Owned merge must not repeat after restart")
        monkeypatch.setattr(workflow.workspaces, "integrate", repeated_merge)
        await workflow._task(rid, task, incoming, plan, {"specification": {}})
        assert git(worker, "rev-list", "--count", "--merges", "HEAD") == "1"
    assert workflow.store.get(rid, "rounds/CHILD")["round"] == 1


async def test_conflict_keeps_old_candidate_and_repairs_on_current_dependency_base(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement":
                assert (workspace / "value.py").read_text() == "value = 20\n"
                assert context["feedback"]["baseline_refresh"]["retained_candidate"]
                retained = json.loads(Path(context["context_files"][0]).read_text())
                assert {row["path"]: row["content"] for row in retained["files"]}["value.py"] == "value = 10\n"
                return ImplementationResult(summary="Preserve current base 20 plus retained increment 10", changes=[
                    Change(path="value.py", operation="replace", old_text="value = 20\n", content="value = 30\n")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, Agent(), conflict=True)
    workflow.store.checkpoint(rid, "plan", plan.model_dump())
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, task, incoming, plan, {"specification": {}})
    assert caught.value.failure.kind == "integration"
    workflow.recovery.submit(rid, caught.value.failure)
    await workflow.recovery.resume(rid)
    commit = await workflow._task(rid, task, incoming, plan, {"specification": {}})
    saved = workflow.store.get(rid, "develop/task/CHILD")
    assert Path(saved["workspace"]) != worker
    assert (worker / "value.py").read_text() == "value = 10\n"
    assert workflow.workspaces.is_ancestor(incoming, commit)
    assert git(Path(saved["workspace"]), "show", "HEAD:value.py") == "value = 30"
    assert workflow.store.get(rid, "rounds/CHILD")["round"] == 2
    assert saved["checks"][0]["exit_code"] == 0


@pytest.mark.parametrize("user_edit", [False, True])
async def test_partial_patch_is_reconciled_before_dependency_refresh_without_overwriting_other_edits(tmp_path, user_edit):
    agent = CodingAgent()
    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, agent)
    task.allowed_paths.append("extra.txt")
    before = tree_hash(worker)
    proposal = ImplementationResult(summary="retained partial patch", changes=[
        Change(path="notes.txt", content="completed candidate\n"), Change(path="extra.txt", content="retained addition\n")])
    patch = prepare_changes(worker, proposal.changes, task.allowed_paths)
    apply_prepared(worker, patch[:1])
    workflow.store.checkpoint(rid, "develop/task/CHILD", {"phase": "implementing", "workspace": str(worker),
        "source_hash": before, "implementation": proposal.model_dump(), "patch": patch})
    if user_edit:
        (worker / "user-note.txt").write_text("preserve unrelated edit")
        with pytest.raises(AA2Error, match="outside its prepared patch"):
            await workflow._task(rid, task, incoming, plan, {"specification": {}})
        assert (worker / "user-note.txt").read_text() == "preserve unrelated edit"
        assert not (worker / "extra.txt").exists()
    else:
        await workflow._task(rid, task, incoming, plan, {"specification": {}})
        assert (worker / "value.py").read_text() == "value = 20\n"
        assert (worker / "notes.txt").read_text() == "completed candidate\n"
        assert (worker / "extra.txt").read_text() == "retained addition\n"
    assert not any(stage == "implement" for stage, _ in agent.calls)
    assert workflow.store.get(rid, "rounds/CHILD")["round"] == 1


@pytest.mark.parametrize("implementation,evidence,expected", [
    ("accepted", "failed", "reverify"), ("accepted", "stale", "reverify"),
    ("needs_repair", "failed", "rejected"), ("pending", "failed", "rejected"),
    ("in_progress", "failed", "rejected"), ("accepted", "valid", "rejected")])
def test_actual_merge_only_rechecks_rejected_accepted_implementation(tmp_path, implementation, evidence, expected):
    from auto_agents_2.task_baselines import refresh_task_baseline
    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, CodingAgent())
    saved = workflow.store.get(rid, "develop/task/CHILD")
    saved["phase"] = "rejected"
    workflow.store.checkpoints(rid, {"develop/task/CHILD": saved,
        "task_state/CHILD": {"version": 1, "revision": 1, "implementation": implementation, "evidence": evidence}})
    updated, _ = refresh_task_baseline(workflow, rid, task, saved, worker, incoming, 1)
    assert updated["phase"] == expected
    assert workflow.store.get(rid, "rounds/CHILD")["round"] == 1
    # Changing baseline is evidence for a new observation, never a successful check.
    assert "commit" not in updated and "checks" not in updated
    assert workflow.store.get(rid, "task_state/CHILD")["evidence"] == evidence


def test_rejected_accepted_implementation_does_not_recheck_unchanged_base(tmp_path):
    from auto_agents_2.task_baselines import refresh_task_baseline
    workflow, rid, task, plan, worker, incoming = setup_candidate(tmp_path, CodingAgent())
    saved = workflow.store.get(rid, "develop/task/CHILD")
    saved["phase"] = "rejected"
    workflow.store.checkpoint(rid, "task_state/CHILD", {"implementation": "accepted", "evidence": "failed"})
    before = git(worker, "rev-parse", "HEAD")
    updated, _ = refresh_task_baseline(workflow, rid, task, saved, worker, before, 1)
    assert updated == saved and updated["phase"] == "rejected"
