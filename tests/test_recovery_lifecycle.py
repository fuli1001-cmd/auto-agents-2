"""Recovery must advance from an old hold to a current, bounded decision."""
import asyncio
import sqlite3
import sys

import pytest
from test_build_contracts import blocked_workflow
from test_recovery_contracts import failure, plan, prepare_activation
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence import assess_observation
from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import Command
from auto_agents_2.recovery import Diagnosis, RecoveryNeeded, RepairTarget, adjudicate
from auto_agents_2.replanning import assess
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.workspace import tree_hash


async def test_old_plan_hold_does_not_block_current_diagnosis(tmp_path):
    workflow, rid, _, _, _, pending = await blocked_workflow(tmp_path, failing=True, proposed_changes=True)
    previous = workflow.store.get(rid, "develop/task/T004")
    workflow.store.checkpoint(rid, "develop/task/T004", {**previous, "checks": previous["feedback"]["checks"]})
    assert not await workflow._refresh_legacy_build_checks(rid)
    workflow._legacy_recovery(rid)
    calls = []

    async def invoke(stage, context, schema, workspace, run_id, operation, fresh=False):
        assert schema is Diagnosis and fresh
        calls.append(operation)
        return Diagnosis(action="repair", task_ids=["T004"],
                         evidence_refs=context["failure"]["evidence_refs"], reason="Actual assertion failed")

    async def unexpected_question(question):
        pytest.fail("An exhausted plan budget must not block a remaining diagnosis/repair budget")

    workflow.agent.invoke = invoke
    workflow.question_handler = unexpected_question
    await workflow.recovery.resume(rid)
    assert len(calls) == 1
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "develop/task/T004")["phase"] == "rejected"
    latest = workflow.store.get(rid, "develop/task/T004")["feedback"]["checks"]
    assert latest[0]["exit_code"] == 0 and latest[0]["generated_outputs"]
    assert workflow.store.get(rid, "develop/task/T004")["checks"] == latest
    assert len(latest) == 2 and latest[1]["report_status"] == "valid"
    assert workflow.store.get(rid, "rounds/T004")["round"] == 3
    assert workflow.store.get(rid, "replanning/progress")["stalled"] == pending["decision"]["stalled"] == 7
    assert workflow.store.get(rid, "develop/done") == ["T001", "T002", "T003"]
    assert not workflow.store.pending(rid)


async def test_partial_success_closes_only_proven_cases_and_never_accepts_task(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("partial", {})["id"]
    source = tmp_path / "values.py"
    source.write_text("a=0\nb=1\n")
    (tmp_path / "test_values.py").write_text("from values import a,b\ndef test_a(): assert a == 1\ndef test_b(): assert b == 1\n")
    command = Command(argv=[sys.executable, "-B", "-m", "pytest", "-q", "test_values.py"], expected_tests=2)
    environment = await workflow._environment()
    ledger = IssueLedger(workflow.store, workflow.artifacts)
    for code in ("a=0\nb=1\n", "a=1\nb=0\n"):
        source.write_text(code)
        rows = await workflow.verifier.run([command], tmp_path, environment)
        evidence = assess_observation(command, rows[0], tmp_path, environment, workflow.artifacts)
        assert evidence.valid and not evidence.passed
        workflow.store.checkpoints(rid, ledger.check_updates(rid, "T1", [command], rows, tmp_path, environment))
    issues = workflow.store.get(rid, "issues")
    assert next(i for i in issues.values() if i["subject"].endswith("::test_a"))["status"] == "closed"
    assert next(i for i in issues.values() if i["subject"].endswith("::test_b"))["status"] == "open"
    assert not workflow.store.get(rid, "develop/done")
    workflow.store.checkpoints(rid, ledger.check_updates(rid, "T1", [command], [], tmp_path, environment))
    assert next(i for i in workflow.store.get(rid, "issues").values() if i["subject"].endswith("::test_a"))["status"] == "stale"


async def test_diagnosis_budget_is_shared_across_different_failure_kinds(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    config = workflow.config.load().model_dump(mode="json")
    config["automation"]["enabled"] = False
    write_json(workflow.config.path, config)
    rid = workflow.store.admit("same origin", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoint(rid, "plan", plan().model_dump())
    calls = []

    async def invoke(stage, context, schema, workspace, run_id, operation, fresh=False):
        calls.append(operation)
        return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=context["failure"]["evidence_refs"], reason="scoped repair")

    workflow.agent.invoke = invoke
    for step in ("task", "checks"):
        workflow.recovery.submit(rid, failure(step=step))
        await workflow.recovery.resume(rid)
    assert len(calls) == 2
    workflow.recovery.submit(rid, failure(kind="environment", step="environment"))

    await workflow.recovery.resume(rid)
    assert len(calls) == 3
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 3
    assert not workflow.store.pending(rid)


def test_v2_migration_preserves_active_recovery_and_is_transactional(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("legacy", {})["id"]
    active = {"version": 1, "failure": failure().model_dump(), "attempt": 1, "diagnosis_round": 2}
    store.checkpoints(rid, {"plan": plan().model_dump(), "plan_revision/current": {"revision": 5},
        "task_state/T3": {"implementation": "needs_repair", "revision": 4}, "recovery/active": active,
        "recovery/failures/" + failure().id + "/1": failure().model_dump(),
        "recovery/diagnosis/" + failure().id: {"round": 2}, "rounds/T3": {"round": 4}})
    with store.connect() as db:
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        db.execute("CREATE TRIGGER fail_upgrade BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fault"):
        store.migrate()
    assert store.get(rid, "migration/v2") is None
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_upgrade")
    store.migrate()
    before = store.get(rid, "migration/v2")
    store.migrate()
    assert store.get(rid, "migration/v2") == before
    from auto_agents_2.recovery_identity import failure_contract
    upgraded = store.get(rid, "recovery/active")
    assert upgraded["version"] == 2 and upgraded["attempt"] == active["attempt"]
    assert upgraded["diagnosis_round"] == active["diagnosis_round"]
    assert failure_contract(upgraded["failure"]) == failure_contract(active["failure"])
    assert before["records"]["recovery/active"] == active
    assert store.get(rid, "task_state/T3")["revision"] == 4
    assert store.get(rid, "plan_revision/current")["revision"] == 5
    assert store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 2
    with sqlite3.connect(tmp_path / ".aa2/state-v2.backup.sqlite") as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"


def test_stale_scope_is_deferred_not_regressed_or_awarded_progress():
    original = {"prototype": {"criteria": {"approved": True}}, "tests": {"criteria": {"case": False}}}
    previous, _ = assess({}, original)
    state, decision = assess(previous, {"prototype": {"criteria": {}, "comparable": False},
                                       "tests": {"criteria": {"case": True}}})
    assert not decision["regressions"] and decision["deferred"] == ["prototype"]
    assert not decision["improved"] and state["stalled"] == 1


async def test_repeated_recheck_with_identical_inputs_requires_a_new_decision(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("repeat", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoint(rid, "plan", plan().model_dump())
    record = failure(kind="evidence")
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)

    async def invoke(stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == "decision":
            from auto_agents_2.decisions import DecisionResult
            return DecisionResult(action="ask_user", reason="Repeated identical inputs need user evidence")
        return Diagnosis(action="recheck", task_ids=["T3"], evidence_refs=record.evidence_refs, reason="try again")

    async def blocked(question):
        assert "Repeated identical inputs" in question["prompt"]
        raise asyncio.CancelledError()

    workflow.agent.invoke = invoke
    workflow.question_handler = blocked
    workflow.recovery.submit(rid, record)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active").get("decision") is None
    assert workflow.store.get(rid, "rounds/T3") is None


async def test_upstream_recovery_preserves_downstream_outstanding_repair(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("two repairs", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    downstream = failure(task_id="T4", origin_id="T4", evidence={"checks": [{"current_failure": True}]}).model_dump()
    workflow.store.checkpoints(rid, {"plan": plan().model_dump(),
        "task_state/T4": {"implementation": "needs_repair", "revision": 1},
        "develop/task/T4": {"phase": "rejected", "feedback": {
            "controller_recovery": {"action": "repair"}, "failure": downstream, "checks": [{"obsolete_failure": True}]}}})

    async def invoke(stage, context, schema, workspace, run_id, operation, fresh=False):
        return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=context["failure"]["evidence_refs"], reason="upstream defect")

    workflow.agent.invoke = invoke
    workflow.recovery.submit(rid, failure())
    await workflow.recovery.resume(rid)
    saved = workflow.store.get(rid, "develop/task/T4")
    assert saved["phase"] == "rejected"
    assert saved["feedback"]["failure"] == downstream
    assert saved["feedback"]["checks"] == [{"current_failure": True}]
    assert saved["feedback"]["upstream_recovery"]["task_ids"] == ["T3"]


async def test_retained_rejection_is_diagnosed_and_can_continue_past_old_limit(tmp_path):
    from test_verification_recovery import blocked_workflow as report_workflow
    workflow, rid, proposal, base, work, _ = await report_workflow(tmp_path, failing=True)
    config = workflow.config.load().model_dump(mode="json")
    config["automation"]["enabled"] = False
    write_json(workflow.config.path, config)
    rows = await workflow.verifier.run(proposal.tasks[0].checks, work, await workflow._environment())
    saved = {"phase": "rejected", "workspace": str(work), "checks": [r.model_dump() for r in rows]}
    workflow.store.checkpoints(rid, {"develop/task/T1": saved, "rounds/T1": {"round": 4, "stalled": 0}})
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert caught.value.failure.kind == "checks" and not workflow.agent.calls

    async def invoke(stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == "implement":
            raise RuntimeError("authorized implementation reached")
        return Diagnosis(action="repair", task_ids=["T1"], evidence_refs=context["failure"]["evidence_refs"], reason="verified arithmetic defect")

    workflow.agent.invoke = invoke
    workflow.recovery.submit(rid, caught.value.failure)
    await workflow.recovery.resume(rid)
    with pytest.raises(RuntimeError, match="authorized implementation reached"):
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert workflow.store.get(rid, "rounds/T1")["round"] == 5


async def test_final_candidate_refreshes_accepted_task_evidence_without_reopening(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("accepted", {})["id"]
    original = plan()
    original.tasks = original.tasks[:1]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_1.py").write_text("def test_case(): assert True\n")
    workflow.store.checkpoints(rid, {"plan": original.model_dump(), "develop/done": ["T1"],
        "rounds/T1": {"round": 4},
        "task_state/T1": {"implementation": "accepted", "evidence": "stale", "revision": 2}})
    environment = await workflow._environment()
    commands = original.tasks[0].checks
    rows = await workflow.verifier.run(commands, tmp_path, environment)
    workflow._replan_checks(rid, "final-checks", commands, rows, tmp_path, environment)
    assert workflow.store.get(rid, "task_state/T1") == {"implementation": "accepted", "evidence": "valid", "revision": 2}
    assert workflow.store.get(rid, "rounds/T1")["round"] == 4 and not workflow.agent.calls
    assert "issues" in workflow.store.delivery_evidence(rid)


def test_cross_task_diagnosis_cannot_borrow_another_tasks_write_scope():
    record = failure(task_id="T4", origin_id="T4", evidence_refs=["part1.py", "part3.py"])
    diagnosis = Diagnosis(action="repair", task_ids=["T3"], evidence_refs=["part1.py"], reason="claim ownership",
        repair_targets=[RepairTarget(task_id="T3", paths=["part1.py"], evidence_refs=["part1.py"], reason="wrong owner")])
    with pytest.raises(AA2Error, match="outside"):
        adjudicate(record, diagnosis=diagnosis, plan=plan())


@pytest.mark.parametrize("reverse", [False, True])
async def test_same_argv_distinct_contracts_keep_separate_evidence_and_strongest_obligation(tmp_path, reverse):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("distinct contracts", {})["id"]
    (tmp_path / "test_cases.py").write_text("def test_case(): assert True\n")
    command = Command(argv=[sys.executable, "-B", "-m", "pytest", "-q", "test_cases.py"], expected_tests=1)
    commands = [command, command.model_copy(update={"expected_tests": 2})]
    if reverse:
        commands.reverse()
    environment = await workflow._environment()
    rows = await workflow.verifier.run(commands, tmp_path, environment)
    updates = IssueLedger(workflow.store, workflow.artifacts).check_updates(rid, "T1", commands, rows, tmp_path, environment)
    assert all(row["valid"] for row in updates["recovery/observations/T1"])
    assert [row["passed"] for row in updates["recovery/observations/T1"]] == [bool(c.expected_tests == 1) for c in commands]
    assert next(i for i in updates["issues"].values() if i["kind"] == "coverage")["status"] == "open"


@pytest.mark.parametrize("user_change", [False, True])
async def test_resume_after_plan_materialization_accepts_only_the_owned_document_change(tmp_path, user_change):
    workflow = engine(tmp_path, CodingAgent())
    store, rid = prepare_activation(tmp_path)
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "integration", base)
    doc = work / "docs/aa2/plan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("four")
    before = tree_hash(work)
    record = failure(kind="contract", origin_id="plan", task_id="", evidence_refs=["plan"])
    key = f"plan_revision/journal/{record.id}/1"
    journal = store.get(rid, "plan_revision/journal/patch")
    store.checkpoint(rid, key, journal)
    store.activate_plan_patch(rid, record.id + "/1")
    doc.write_text("revised")
    decision = adjudicate(record, diagnosis=Diagnosis(action="plan_patch", reason="owned change", evidence_refs=["plan"]))
    store.checkpoint(rid, "recovery/active", {"version": 1, "failure": record.model_dump(), "attempt": 1,
        "evidence_source_hash": before, "decision": decision.model_dump()})
    if user_change:
        (work / "user.txt").write_text("preserve this")
        with pytest.raises(AA2Error, match="source changed"):
            await workflow.recovery.resume(rid)
        assert (work / "user.txt").read_text() == "preserve this"
    else:
        await workflow.recovery.resume(rid)
        assert store.get(rid, "recovery/active") is None
    assert store.get(rid, "plan_revision/current")["revision"] == 2
    assert not workflow.agent.calls
