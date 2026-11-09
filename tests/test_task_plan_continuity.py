"""Accepted task evidence survives only a proven unrelated, reviewed amendment."""
from plan_proposal_fixtures import EvidencePlanPatch, proposal_response
import copy
import json
import sqlite3

import pytest
from authority_review_fixtures import authority_review
from evidence_test_fixtures import empty_contract, evidence_review, seed_check_production
from test_decisions import ApprovingAgent
from test_recovery_policy import ready

from auto_agents_2.models import ImplementationResult, PlanResult, ReviewResult
from auto_agents_2.plan_revision import PlanPatch, materialize_plan
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.state_codec import checkpoint_query_db
from auto_agents_2.util import digest
from auto_agents_2.workspace import git, tree_hash


class LocalPatchAgent(ApprovingAgent):
    async def invoke(self, stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            return Diagnosis(action="plan_patch", task_ids=["T3"], reason="Local task correction",
                             evidence_refs=["failure", "plan"])
        if schema in (PlanPatch, EvidencePlanPatch):
            previous = PlanResult.model_validate(context["plan"])
            task = previous.tasks[2].model_copy(deep=True)
            task.checks[0].expected_tests += 1
            return proposal_response(PlanPatch(base_revision=context["base_revision"], failure_ids=[context["failure"]["id"]],
                reason="Increase one task's existing check minimum", document="Reviewed local revision",
                upsert_tasks=[task], evidence_contract=empty_contract(context)), context, schema)
        if context.get("review_kind") == "plan_patch":
            return evidence_review(context)
        return await super().invoke(stage, context, schema, *args, **kwargs)


async def amended(tmp_path, input_alias=None):
    workflow, rid, work, plan, failure = ready(tmp_path, agent=LocalPatchAgent())
    for task in plan.tasks:
        task.checks[0].inputs = [f"part{task.id[1:]}.py", f"tests/test_{task.id[1:]}.py"]
        (work / f"part{task.id[1:]}.py").write_text("VALUE = 1\n")
        (work / "tests").mkdir(exist_ok=True)
        (work / f"tests/test_{task.id[1:]}.py").write_text("def test_value(): assert True\n")
    if input_alias is not None:
        plan.tasks[1].allowed_paths = ["part2.py"]
        plan.tasks[1].checks[0].inputs = [input_alias] if input_alias else []
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "authority/protocol": 1,
        "plan_revision/current": {"version": 1, "revision": 1},
        "task_origins": {t.id: t.origin_id for t in plan.tasks},
        "prototype_approval": {"unchanged": "human approval"}, "rounds/T1": {"round": 7}})
    seed_check_production(workflow, rid, plan)
    materialize_plan(workflow.store, rid, work, plan)
    commit = workflow.workspaces.checkpoint(work, "refs/aa2/fixture/accepted", "accepted fixture source")
    records = {}
    for task in plan.tasks[:2]:
        implementation = ImplementationResult(summary="Synthetic accepted task scenario").model_dump()
        checks = [{"argv": task.checks[0].argv, "exit_code": 0, "source_hash": tree_hash(work),
                   "command_hash": digest(task.checks[0].model_dump()), "tests": 1}]
        context = {"review_kind": "implementation", "task": task.model_dump(), "implementation": implementation,
                   "source_hash": tree_hash(work), "project": {"fixture": "actual committed source"}, "checks": checks}
        context, _ = workflow.authority.prepare(rid, "review", context, ReviewResult, work,
                                                "develop/task/" + task.id + "/review")
        review = authority_review(context)
        workflow.authority.record(rid, review, context, "develop/task/" + task.id + "/review")
        records[task.id] = {"phase": "tested", "workspace": str(work), "commit": commit,
            "source_hash": tree_hash(work), "implementation": implementation, "checks": checks,
            "review": review.model_dump(), "revision": 1}
        workflow.store.checkpoints(rid, {"develop/task/" + task.id: records[task.id],
            "task_state/" + task.id: {"version": 1, "revision": 1, "implementation": "accepted", "evidence": "valid"}})
    workflow.store.checkpoint(rid, "develop/done", ["T1", "T2"])
    await workflow.recovery.resume(rid)
    current = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    workflow.workspaces.checkpoint(work, "refs/aa2/fixture/amended", "materialized reviewed local plan")
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 2
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    return workflow, rid, work, current, records


async def test_develop_keeps_unaffected_acceptance_after_reviewed_local_patch(tmp_path, monkeypatch):
    workflow, rid, work, plan, records = await amended(tmp_path)
    started = []
    async def stop(run_id, task, *args):
        started.append(task.id)
        raise RuntimeError("stop before affected task execution")
    monkeypatch.setattr(workflow, "_observed_task", stop)
    with pytest.raises(RuntimeError, match="stop before"):
        await workflow._develop(rid, work, plan, {"specification": {}})
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    assert started == ["T3"]
    for tid, original in records.items():
        assert workflow.store.get(rid, "develop/task/" + tid) == original
    assert workflow.store.get(rid, "rounds/T1") == {"round": 7}
    assert workflow.store.get(rid, "prototype_approval") == {"unchanged": "human approval"}


def invalidate(workflow, rid, task, original):
    # Reproduce the exact old controller publication, preserving its immutable history.
    from auto_agents_2.task_plan_continuity import REASSESSMENT
    state = workflow.store.get(rid, "task_state/" + task.id)
    saved = {"phase": "reverify", "resume_base": original["commit"], "previous_evidence": original,
             "feedback": copy.deepcopy(REASSESSMENT)}
    workflow.store.checkpoints(rid, {"develop/task/" + task.id: saved,
        "task_state/" + task.id: {**state, "evidence": "stale"},
        "authority/task-history/" + task.id + "/" + digest(original): original,
        "develop/done": [t for t in workflow.store.get(rid, "develop/done") if t != task.id]})
    return saved


async def test_untouched_legacy_invalidation_recovers_atomically_and_idempotently(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path)
    task = plan.tasks[0]
    saved = invalidate(workflow, rid, task, records[task.id])
    before_rounds = workflow.store.get(rid, "rounds/T1")
    before_permits = {k: v for k, v in workflow.store.delivery_evidence(rid).items()
                      if k.startswith("recovery/permits/")}
    assert workflow.authority.retain_task(rid, task, saved, work=work) == records[task.id]
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    assert workflow.store.get(rid, "task_state/T1")["evidence"] == "valid"
    proof = workflow.store.get(rid, "authority/task-continuity/T1")
    assert proof["from"] != proof["to"] and proof["accepted_record_hash"] == digest(records[task.id])
    assert proof["accepted_source_hash"] != proof["current_source"]["source_hash"]
    assert workflow.authority.retain_task(rid, task, records[task.id], work=work) == records[task.id]
    assert len([e for e in workflow.store.events(rid) if e["kind"] == "task.authority_retained"]) == 1
    assert workflow.store.get(rid, "rounds/T1") == before_rounds
    assert {k: v for k, v in workflow.store.delivery_evidence(rid).items()
            if k.startswith("recovery/permits/")} == before_permits
    assert not workflow.authority.reusable(rid, records[task.id]["review"])  # Global authority remains strict.


@pytest.mark.parametrize("damage", ["capture", "answer", "approval", "receipt", "journal", "affected",
    "history", "contract", "global", "checks", "implementation", "source_hash", "commit", "unknown_inputs"])
async def test_damaged_or_changed_proof_keeps_original_reverify_gate(tmp_path, damage):
    workflow, rid, work, plan, records = await amended(tmp_path)
    task = plan.tasks[1]
    saved = copy.deepcopy(records[task.id])
    store = workflow.store
    if damage == "capture":
        store.checkpoint(rid, "capture", {"changed": "requirement source"})
    elif damage == "answer":
        q = store.request(rid, "design/0", "Scope", "binding")
        store.answer(q["id"], "new requested behavior", "binding")
    elif damage == "approval":
        store.checkpoint(rid, "prototype_approval", {"different": "binding"})
    elif damage == "receipt":
        key = "authority/reviews/" + digest(saved["review"])
        row = store.get(rid, key); row["review_hash"] = "corrupt"; store.checkpoint(rid, key, row)
    elif damage in {"journal", "affected"}:
        key = next(k for k in store.delivery_evidence(rid) if k.startswith("plan_revision/journal/"))
        row = store.get(rid, key)
        if damage == "journal": row["review"]["summary"] = "unbound edit"
        else: row["affected"].append(task.id)
        store.checkpoint(rid, key, row)
    elif damage == "history":
        row = store.get(rid, "plan_revision/history/1"); row["tasks"]["develop/done"] = []
        store.checkpoint(rid, "plan_revision/history/1", row)
    elif damage in {"contract", "global", "unknown_inputs"}:
        current = store.get(rid, "plan")
        if damage == "contract": current["tasks"][0]["description"] += " changed dependency"
        elif damage == "global": current["final_checks"][0]["expected_tests"] += 1
        else: current["tasks"][1]["checks"][0]["inputs"] = []
        store.checkpoint(rid, "plan", current)
        task = PlanResult.model_validate(current).tasks[1]
    elif damage == "checks": saved["checks"][0]["tests"] += 1
    elif damage == "implementation": saved["implementation"]["summary"] = "not reviewed"
    elif damage == "source_hash": saved["source_hash"] = "wrong-source"
    elif damage == "commit": saved["commit"] = git(work, "rev-parse", "HEAD")
    store.checkpoint(rid, "develop/task/" + task.id, saved)
    result = workflow.authority.retain_task(rid, task, saved, work=work)
    assert result["phase"] == "reverify"
    assert result["previous_evidence"] == saved
    assert task.id not in store.get(rid, "develop/done")
    assert store.get(rid, "authority/task-continuity/" + task.id) is None


@pytest.mark.parametrize("change", ["own", "dependency", "test_input", "untracked", "mode", "symlink", "unrelated"])
async def test_actual_source_and_dependency_content_controls_reuse(tmp_path, change):
    workflow, rid, work, plan, records = await amended(tmp_path)
    task = plan.tasks[1]
    target = {"own": "part2.py", "dependency": "part1.py", "test_input": "tests/test_2.py",
              "untracked": "unowned.py", "mode": "part2.py", "symlink": "alias.py", "unrelated": "part4.py"}[change]
    path = work / target
    if change == "mode": path.chmod(0o755)
    elif change == "symlink": path.symlink_to("part2.py")
    else: path.write_text("VALUE = 2\n")
    if change != "untracked":
        workflow.workspaces.checkpoint(work, "refs/aa2/fixture/changed", "fixture source change")
    result = workflow.authority.retain_task(rid, task, records[task.id], work=work)
    assert (result == records[task.id]) == (change == "unrelated")
    assert (task.id in workflow.store.get(rid, "develop/done")) == (change == "unrelated")


@pytest.mark.parametrize("mutation", ["new_work", "feedback", "revision"])
async def test_recovery_never_overwrites_new_work_or_other_invalidation(tmp_path, mutation):
    workflow, rid, work, plan, records = await amended(tmp_path)
    task = plan.tasks[0]
    saved = invalidate(workflow, rid, task, records[task.id])
    if mutation == "new_work": saved["observation_id"] = ["recheck", "new-failure", 1, task.id]
    elif mutation == "feedback": saved["feedback"]["user"] = "new correction"
    else:
        state = workflow.store.get(rid, "task_state/T1"); state["revision"] += 1
        workflow.store.checkpoint(rid, "task_state/T1", state)
    workflow.store.checkpoint(rid, "develop/task/T1", saved)
    assert workflow.authority.retain_task(rid, task, saved, work=work) == saved
    assert "T1" not in workflow.store.get(rid, "develop/done")
    assert workflow.store.get(rid, "authority/task-continuity/T1") is None


async def test_failed_publication_rolls_back_then_normal_retry_recovers(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path)
    saved = invalidate(workflow, rid, plan.tasks[0], records["T1"])
    with workflow.store.connect() as db:
        db.execute("CREATE TRIGGER fail_continuity BEFORE INSERT ON checkpoint_records WHEN NEW.key='develop/done' BEGIN SELECT RAISE(ABORT, 'injected continuity interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match="continuity interruption"):
        workflow.authority.retain_task(rid, plan.tasks[0], saved, work=work)
    assert workflow.store.get(rid, "develop/task/T1") == saved
    assert workflow.store.get(rid, "authority/task-continuity/T1") is None
    with workflow.store.connect() as db: db.execute("DROP TRIGGER fail_continuity")
    assert workflow.authority.retain_task(rid, plan.tasks[0], saved, work=work) == records["T1"]


async def test_stale_caller_cannot_overwrite_concurrent_task_state(tmp_path):
    from auto_agents_2.faults import StateConflict
    workflow, rid, work, plan, records = await amended(tmp_path)
    changed = {**records["T1"], "feedback": {"new": "work"}}
    workflow.store.checkpoint(rid, "develop/task/T1", changed)
    with pytest.raises(StateConflict, match="acceptance changed"):
        workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work)
    assert workflow.store.get(rid, "develop/task/T1") == changed


async def test_multiple_reviewed_local_patches_preserve_original_review_chain(tmp_path):
    from test_recovery_contracts import failure
    workflow, rid, work, plan, records = await amended(tmp_path)
    assert workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work) == records["T1"]
    workflow.recovery.submit(rid, failure(evidence={"next": "local amendment"}))
    await workflow.recovery.resume(rid)
    current = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    workflow.workspaces.checkpoint(work, "refs/aa2/fixture/second", "second local amendment")
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 3
    assert workflow.authority.retain_task(rid, current.tasks[0], records["T1"], work=work) == records["T1"]
    assert len(workflow.store.get(rid, "authority/task-continuity/T1")["transitions"]) == 2
    assert workflow.store.get(rid, "develop/task/T1") == records["T1"]


async def test_previously_retained_proof_cannot_mask_later_input_change(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path)
    task = plan.tasks[1]
    assert workflow.authority.retain_task(rid, task, records[task.id], work=work) == records[task.id]
    old = workflow.store.get(rid, "authority/task-continuity/" + task.id)
    (work / "part1.py").write_text("VALUE = 3\n")
    workflow.workspaces.checkpoint(work, "refs/aa2/fixture/later", "later dependency implementation")
    result = workflow.authority.retain_task(rid, task, records[task.id], work=work)
    assert result["phase"] == "reverify" and task.id not in workflow.store.get(rid, "develop/done")
    assert workflow.store.get(rid, "authority/task-continuity/" + task.id) == old  # Historical proof only.


@pytest.mark.parametrize("input_alias", ["./tests/test_2.py", "tests//test_2.py", "tests/test_2.py", "./", ".//"])
async def test_check_input_alias_outside_write_scope_cannot_hide_source_change(tmp_path, input_alias):
    workflow, rid, work, plan, records = await amended(tmp_path, input_alias)
    (work / "tests/test_2.py").write_text("def test_value(): assert False\n")
    workflow.workspaces.checkpoint(work, "refs/aa2/fixture/alias", "changed declared check input")
    result = workflow.authority.retain_task(rid, plan.tasks[1], records["T2"], work=work)
    assert result["phase"] == "reverify"
    assert "T2" not in workflow.store.get(rid, "develop/done")


async def test_unknown_input_boundary_in_original_contract_requires_reverification(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path, "")
    from auto_agents_2.task_plan_continuity import _chain
    with workflow.store.connect() as db:
        rows = {row["key"]: json.loads(row["data"]) for row in checkpoint_query_db(db, 'SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=?', (rid,), wire_index=1)}
    assert _chain(rows, workflow.authority.identity(rid), plan.tasks[1], records["T2"],
                  workflow.authority.receipt(rid, records["T2"]["review"])["basis"])
    result = workflow.authority.retain_task(rid, plan.tasks[1], records["T2"], work=work)
    assert result["phase"] == "reverify"
    assert workflow.store.get(rid, "authority/task-continuity/T2") is None


async def test_corrupted_intermediate_receipt_rejects_multihop_reuse(tmp_path):
    from test_recovery_contracts import failure
    workflow, rid, work, plan, records = await amended(tmp_path)
    first = workflow.store.get(rid, "authority/accepted/plan")["review_key"]
    assert workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work) == records["T1"]
    workflow.recovery.submit(rid, failure(evidence={"next": "local amendment"}))
    await workflow.recovery.resume(rid)
    current = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    workflow.workspaces.checkpoint(work, "refs/aa2/fixture/second", "second local amendment")
    receipt = workflow.store.get(rid, first); receipt["review_hash"] = "damaged intermediate receipt"
    workflow.store.checkpoint(rid, first, receipt)
    result = workflow.authority.retain_task(rid, current.tasks[0], records["T1"], work=work)
    assert result["phase"] == "reverify" and "T1" not in workflow.store.get(rid, "develop/done")
