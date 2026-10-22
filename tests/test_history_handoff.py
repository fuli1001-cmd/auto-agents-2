"""Explicit scope selection must preserve existing evidence and recovery identities."""
import copy

import pytest
from test_final_history_scope import history_fixture, plan_for
from test_recovery_policy import ready

from auto_agents_2.faults import StateConflict
from auto_agents_2.source_history import SourceHistory
from auto_agents_2.workspace import tree_hash


def history_records(workflow, rid):
    with workflow.store.connect() as db:
        return db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND "
                          "key LIKE 'review-evidence/source-history/%' ORDER BY key", (rid,)).fetchall()


def test_omitted_scope_cannot_silently_drop_planned_configuration(tmp_path):
    workflow, rid, _ = history_fixture(tmp_path)
    before = history_records(workflow, rid)
    with pytest.raises(TypeError, match="scopes"):
        SourceHistory(workflow).collect(rid, tmp_path)
    assert history_records(workflow, rid) == before


def test_missing_bound_plan_requires_an_explicit_no_plan_branch(tmp_path):
    workflow, rid, _ = history_fixture(tmp_path)
    before = history_records(workflow, rid)
    with pytest.raises(StateConflict, match="bound plan"):
        SourceHistory(workflow).collect_plan(rid, tmp_path, None)
    assert history_records(workflow, rid) == before


@pytest.mark.parametrize("targets", [[], ["missing"], ["T1", "missing"]])
def test_invalid_task_selection_fails_before_collecting_any_material(tmp_path, targets):
    workflow, rid, _ = history_fixture(tmp_path)
    before = history_records(workflow, rid)
    with pytest.raises(StateConflict, match="task selection"):
        SourceHistory(workflow).collect_plan(rid, tmp_path, plan_for(["frontend/vite.config.mjs"]),
                                            task_ids=targets)
    assert history_records(workflow, rid) == before


def test_explicit_entries_preserve_exact_old_records_and_task_scope_order(tmp_path):
    workflow, rid, _ = history_fixture(tmp_path)
    histories = SourceHistory(workflow)
    plan = plan_for(["support/config_helper.py", "frontend/vite.config.mjs"],
                    ["frontend/vitest.config.mjs", "frontend/vite.config.mjs"])
    before = copy.deepcopy(plan.model_dump())
    protected = histories.collect(rid, tmp_path, [])
    task = histories.collect(rid, tmp_path, plan.tasks[0].allowed_paths)
    selected = histories.collect(rid, tmp_path, sorted(plan.tasks[0].allowed_paths))
    all_scopes = sorted({path for t in plan.tasks for path in t.allowed_paths})
    whole = histories.collect(rid, tmp_path, all_scopes)
    originals = {row["artifact"]["hash"]: workflow.artifacts.bytes(row["artifact"])
                 for row in (protected, task, selected, whole)}
    assert histories.collect_protected(rid, tmp_path) == protected
    assert histories.collect_task(rid, tmp_path, plan.tasks[0]) == task
    assert histories.collect_plan(rid, tmp_path, plan, task_ids=["T1"]) == selected
    assert histories.collect_plan(rid, tmp_path, plan) == whole
    assert histories.collect_plan(rid, tmp_path, plan, task_ids=["T2", "T1", "T2"]) == whole
    assert histories.collect_plan(rid, tmp_path, plan_for()) == protected
    assert plan.model_dump() == before
    assert all(workflow.artifacts.bytes({"hash": h, "bytes": len(raw), "kind": "source_history"}) == raw
               for h, raw in originals.items())


@pytest.mark.parametrize("task_id", ["T3", None])
def test_progress_keeps_legacy_basis_and_exclusions_with_explicit_plan_selection(tmp_path, task_id):
    workflow, rid, work, plan, failure = ready(tmp_path)
    failure = failure.model_copy(update={"task_id": task_id})
    guard = workflow.recovery_policy.progress
    context = guard.inputs(rid, failure, work, plan)
    scopes = sorted({p for t in plan.tasks if not task_id or t.id == task_id for p in t.allowed_paths})
    legacy = copy.deepcopy(context)
    legacy["source_history"] = SourceHistory(workflow).collect(rid, work, scopes)
    assert legacy == context
    basis = guard.basis(legacy, tree_hash(work), "diagnose")
    for operation in ("original-one", "original-two"):
        guard.record(rid, failure.origin_id, "diagnose", basis, operation, {"status": "inconclusive"})
    assert guard.basis(guard.inputs(rid, failure, work, plan), tree_hash(work), "diagnose") == basis
    assert len(guard.exclusions(rid, failure, work, plan)["diagnose"]["operations"]) == 2


def test_progress_without_plan_preserves_protected_only_history(tmp_path):
    workflow, rid, work, _, failure = ready(tmp_path)
    context = workflow.recovery_policy.progress.inputs(rid, failure, work, None)
    assert context["plan"] is None
    assert context["source_history"] == SourceHistory(workflow).collect_protected(rid, work)
