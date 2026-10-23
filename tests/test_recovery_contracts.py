import copy
import sqlite3
import sys

import pytest
from evidence_test_fixtures import empty_contract, seed_requirements
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import AA2Error
from auto_agents_2.models import Command, ImplementationResult, PlanResult, RecoveryRequest, TaskSpec
from auto_agents_2.plan_revision import PlanPatch, apply_patch, materialize_plan
from auto_agents_2.planning import ready_batch
from auto_agents_2.recovery import (
    Diagnosis,
    FailureRecord,
    RepairTarget,
    adjudicate,
    evidence_references,
    request_from,
)
from auto_agents_2.store import Store
from auto_agents_2.workspace import git


def plan():
    tasks = [TaskSpec(id=f"T{i}", origin_id=f"T{i}", title=str(i), description=f"Implement {i}",
                      requirement_ids=[f"R{i}"], depends_on=[f"T{i-1}"] if i > 1 else [],
                      allowed_paths=[f"part{i}.py", f"tests/test_{i}.py"], acceptance=[f"accept {i}"],
                      checks=[Command(argv=[sys.executable, "-m", "pytest", f"tests/test_{i}.py"], expected_tests=1)])
             for i in range(1, 5)]
    return PlanResult(summary="four", document="four", tasks=tasks,
                      final_checks=[Command(argv=[sys.executable, "-m", "pytest", "tests"], expected_tests=4)],
                      runtime_probes=[Command(argv=[sys.executable, "-c", "import part1"])])


def failure(**changes):
    return FailureRecord.create(**{
        "kind": "proposal", "component": "implementation", "origin_id": "T3", "task_id": "T3",
        "step": "task", "obligations": ["R3"], "evidence_refs": ["develop/task/T3"], **changes})


def test_identity_excludes_prose_revision_and_renamed_task():
    first = failure(proposal={"replan_reason": "a"})
    second = failure(task_id="renamed", proposal={"replan_reason": "b"})
    assert first.id == second.id
    assert request_from(ImplementationResult(summary="x", replan_stage="plan", replan_reason="x")).suggested_action == "plan_patch"
    assert request_from(ImplementationResult(summary="x", recovery_request=RecoveryRequest(reason="a"))).suggested_action == "diagnose"


def test_split_tasks_cannot_race_the_same_budget_reservation():
    tasks = plan().tasks
    for task in tasks:
        task.depends_on = []
    tasks[1].origin_id = tasks[0].origin_id
    assert [t.id for t in ready_batch(tasks, set(), 4)] == ["T1", "T3", "T4"]


@pytest.mark.parametrize("stage,action", [("prototype", "gate"), ("plan", "diagnosis")])
def test_controller_gate_and_accepted_plan_defect_have_distinct_recovery_paths(tmp_path, stage, action):
    from auto_agents_2.engine import Replan
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("four", {})["id"]
    workflow.store.checkpoint(rid, "plan", plan().model_dump())
    workflow._controller_replan(rid, Replan(stage, "controller finding"))
    if action == "gate":
        assert workflow.store.get(rid, "replanning/pending")["controller_owned"]
        assert workflow.store.get(rid, "recovery/active") is None
    else:
        assert workflow.store.get(rid, "recovery/active")["failure"]["component"] == "controller"
        assert workflow.store.get(rid, "replanning/pending") is None


@pytest.mark.parametrize("kind,attempt,action", [("proposal", 0, "diagnose"), ("checks", 0, "diagnose"),
    ("evidence", 0, "recheck"), ("evidence", 1, "diagnose"), ("build", 0, "recheck"), ("environment", 0, "diagnose")])
def test_rules_never_treat_model_rollback_as_controller_decision(kind, attempt, action):
    assert adjudicate(failure(kind=kind), attempts=attempt).action == action


def test_collection_failure_overrides_model_plan_patch_advice_without_accepting_stdout():
    record = failure(proposal={"recovery_request": {"suggested_action": "plan_patch"}}, evidence={
        "feedback": {"checks": [{"exit_code": 1, "process_exit_code": 0, "tests": 0,
                                  "failure_reasons": ["unsupported_test_runner", "insufficient_test_cases"]}]}})
    assert adjudicate(record).action == "recheck"
    assert adjudicate(record, attempts=1).action == "diagnose"


def test_evidence_catalog_resolves_supplied_nested_records_hashes_and_public_files():
    record = failure(evidence={"feedback": {"checks": [{"logs": [{"hash": "a" * 64}]}]},
                               "execution_contract": {"commands": [{"argv": ["node", "runner"]}]}},
                     proposal={"evidence_refs": ["fabricated.py", "b" * 64]})
    refs = evidence_references(record, {"inventory": ["frontend/package.json"]})
    assert {"feedback.checks[0]", "execution_contract.commands[0]", "a" * 64, "frontend/package.json"} <= set(refs)
    assert "fabricated.py" not in refs and "b" * 64 not in refs
    record = record.model_copy(update={"evidence_refs": refs})
    decision = adjudicate(record, diagnosis=Diagnosis(action="plan_patch", reason="contract",
        evidence_refs=["feedback.checks[0]", "frontend/package.json", "a" * 64]), plan=plan())
    assert decision.action == "plan_patch"


def test_proposal_citations_cannot_supply_repair_evidence_or_expand_owned_paths():
    contract = plan()
    record = failure(proposal={"changes": [{"path": "outside.py", "content": "unreviewed"}]})
    record = record.model_copy(update={"evidence_refs": evidence_references(record, {"inventory": ["part1.py"]})})
    context = {"failure": record.model_dump(), "plan": contract.model_dump()}
    proposed = "failure.proposal.changes"
    diagnosis = Diagnosis(action="repair", task_ids=["T1"], reason="Evaluate a proposed repair within the existing scope",
        evidence_refs=["part1.py", proposed], repair_targets=[RepairTarget(task_id="T1", paths=["part1.py"],
            evidence_refs=["part1.py", proposed], reason="Current owned source and an unverified suggestion")])
    assert adjudicate(record, diagnosis=diagnosis, plan=contract, evidence_context=context).action == "repair"
    for update in ({"evidence_refs": [proposed]}, {"paths": ["outside.py"]}):
        altered = diagnosis.model_copy(update={"repair_targets": [diagnosis.repair_targets[0].model_copy(update=update)]})
        with pytest.raises(AA2Error, match="outside the evidenced existing task scope"):
            adjudicate(record, diagnosis=altered, plan=contract, evidence_context=context)


@pytest.mark.parametrize("tasks,refs", [(["unknown"], ["develop/task/T3"]), (["T2"], ["develop/task/T3"]),
                                       (["T3"], ["fabricated"]), ([], ["develop/task/T3"])])
def test_diagnosis_cannot_invent_evidence_or_widen_repair_scope(tasks, refs):
    with pytest.raises(AA2Error):
        adjudicate(failure(), diagnosis=Diagnosis(action="repair", task_ids=tasks, evidence_refs=refs, reason="fix"), plan=plan())


def patch_for(original, task=None, **kwargs):
    return PlanPatch(base_revision=1, failure_ids=[failure().id], reason="repair contract", document="revised",
                     upsert_tasks=[task or original.tasks[2].model_copy(deep=True, update={"description": "repair part3"})], **kwargs)


def apply(original, patch):
    return apply_patch(original, patch, 1, {failure().id}, {t.id: t.origin_id for t in original.tasks})


def test_patch_reopens_only_changed_implementation_and_rechecks_descendants():
    original = plan()
    candidate, affected, reopen = apply(original, patch_for(original))
    assert affected == {"T3", "T4"} and reopen == {"T3"}
    assert candidate.tasks[:2] == original.tasks[:2]
    changed = original.tasks[2].model_copy(deep=True)
    changed.checks[0].timeout += 1
    _, affected, reopen = apply(original, patch_for(original, changed))
    assert affected == {"T3", "T4"} and not reopen


@pytest.mark.parametrize("defect", ["stale", "cycle", "new_origin", "removed", "weakened", "target", "acceptance", "merge"])
def test_patch_rejects_invalid_or_budget_evading_changes(defect):
    original = plan()
    patch = patch_for(original)
    task = patch.upsert_tasks[0]
    if defect == "stale":
        patch.base_revision = 2
    elif defect == "cycle":
        task.depends_on = ["T4"]
    elif defect == "new_origin":
        task.origin_id = "free-budget"
    elif defect == "removed":
        patch.remove_tasks = ["T4"]
    elif defect == "weakened":
        task.checks[0].expected_tests = 0
    elif defect == "target":
        task.checks[0].argv[-1] = "tests/trivial.py"
    elif defect == "acceptance":
        task.acceptance = ["easier"]
    elif defect == "merge":
        task.requirement_ids += ["R4"]
        task.acceptance += ["accept 4"]
        task.checks += original.tasks[3].checks
        patch.remove_tasks = ["T4"]
    with pytest.raises(AA2Error):
        apply(original, patch)


def test_migration_is_explicit_idempotent_and_preserves_raw_unapplied_proposals(tmp_path):
    # Build the historical v1 schema, including its genuinely absent v5 table.
    from auto_agents_2.store import SCHEMA
    state = tmp_path / ".aa2"
    state.mkdir()
    with sqlite3.connect(state / "state.sqlite") as db:
        db.executescript(";".join(s for s in SCHEMA.replace("'schema_version','8'", "'schema_version','1'").split(";")
                                 if "resource_holds" not in s))
    store = Store(tmp_path)
    rid = store.admit("requirements", {})["id"]
    proposal = {"changes": [{"path": "README.md", "operation": "replace", "old_text": "old", "content": "unapplied"}],
                "replan_stage": "plan", "replan_reason": "build order"}
    store.checkpoints(rid, {"plan": plan().model_dump(), "develop/done": ["T1", "T2"],
        "invocation/develop/task/T3": {"result": proposal}, "rounds/T3": {"round": 3},
        "replanning/pending": {"stage": "plan", "decision": {"stalled": 7}}})
    before = Store(tmp_path, readonly=True).get(rid, "invocation/develop/task/T3")
    assert before["result"] == proposal
    with store.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    store.migrate()
    snapshot = copy.deepcopy(store.get(rid, "migration/v1"))
    assert snapshot["records"]["invocation/develop/task/T3"] == before
    assert store.get(rid, "rounds/T3") == {"round": 3}
    assert store.get(rid, "replanning/pending")["decision"]["stalled"] == 7
    assert store.get(rid, "task_state/T1") == {"version": 1, "revision": 1, "implementation": "accepted", "evidence": "stale"}
    store.migrate()
    assert store.get(rid, "migration/v1") == snapshot
    with sqlite3.connect(store.root / "state-v1.backup.sqlite") as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path, readonly=True).checkpoint(rid, "forbidden", {})


def prepare_activation(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("requirements", {})["id"]
    original = plan()
    patch = patch_for(original)
    candidate, affected, reopen = apply(original, patch)
    store.checkpoints(rid, {"plan": original.model_dump(), "develop/done": ["T1", "T2", "T3", "T4"],
        "task_origins": {t.id: t.origin_id for t in original.tasks}, "rounds/T3": {"round": 3},
        **{"develop/task/" + t.id: {"commit": t.id, "checks": [{"original": True}]} for t in original.tasks},
        "plan_revision/journal/patch": {"phase": "prepared", "base_revision": 1, "previous_plan": original.model_dump(),
            "plan": candidate.model_dump(), "affected": sorted(affected), "reopen": sorted(reopen), "reason": "fix"}})
    return store, rid


def test_activation_is_transactional_and_replay_does_not_charge_or_reopen_again(tmp_path):
    store, rid = prepare_activation(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_activation BEFORE INSERT ON checkpoints WHEN NEW.key='task_state/T4' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.activate_plan_patch(rid, "patch")
    assert store.get(rid, "develop/done") == ["T1", "T2", "T3", "T4"]
    assert not store.get(rid, "plan_revision/history/1")
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_activation")
    store.activate_plan_patch(rid, "patch")
    assert store.get(rid, "develop/done") == ["T1", "T2"]
    assert store.get(rid, "task_state/T3")["implementation"] == "needs_repair"
    assert store.get(rid, "task_state/T4")["implementation"] == "accepted"
    assert store.get(rid, "develop/task/T4")["phase"] == "reverify"
    assert store.get(rid, "rounds/T3") == {"round": 3}
    assert store.get(rid, "develop/task/T1")["commit"] == "T1"
    store.activate_plan_patch(rid, "patch")
    assert store.get(rid, "plan_revision/current")["revision"] == 2
    assert store.get(rid, "task_state/T3")["revision"] == 2
    assert "plan_revision/history/1" in store.delivery_evidence(rid)


def test_renaming_accepted_task_preserves_implementation_and_exhausted_budget(tmp_path):
    store, rid = prepare_activation(tmp_path)
    original = plan()
    renamed = original.tasks[2].model_copy(deep=True, update={"id": "T3_RENAMED"})
    child = original.tasks[3].model_copy(deep=True, update={"depends_on": [renamed.id]})
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason="stable task label", document="renamed",
                      upsert_tasks=[renamed, child], remove_tasks=["T3"])
    candidate, affected, reopen = apply(original, patch)
    assert not reopen
    store.checkpoints(rid, {"rounds/T3": {"round": 4}, "plan_revision/journal/rename": {
        "phase": "prepared", "base_revision": 1, "previous_plan": original.model_dump(), "plan": candidate.model_dump(),
        "patch": patch.model_dump(), "affected": sorted(affected), "reopen": sorted(reopen), "reason": patch.reason}})
    store.activate_plan_patch(rid, "rename")
    assert store.get(rid, "task_state/T3_RENAMED")["implementation"] == "accepted"
    assert store.get(rid, "develop/task/T3_RENAMED")["phase"] == "reverify"
    assert store.get(rid, "develop/task/T3_RENAMED")["previous_evidence"]["commit"] == "T3"
    assert store.get(rid, "rounds/T3") == {"round": 4}
    assert store.get(rid, "rounds/T3_RENAMED") is None


@pytest.mark.asyncio
async def test_reverification_never_dispatches_implementation_or_consumes_business_round(tmp_path):
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("addition", {})["id"]
    from auto_agents_2.evidence_admission import admission_context
    seed_requirements(workflow, rid, plan())
    context = admission_context(lambda key: workflow.store.get(rid, key))
    original = await agent.invoke("plan", context, PlanResult, tmp_path, rid, "plan")
    base = workflow.workspaces.committed_baseline(rid, "main")
    commit = await workflow._task(rid, original.tasks[0], base, original, {"specification": {}})
    saved = workflow.store.get(rid, "develop/task/T1")
    workflow.store.checkpoint(rid, "rounds/T1", {"round": 4, "stalled": 0, "failure": ""})
    workflow.store.checkpoint(rid, "develop/task/T1", {"phase": "reverify", "previous_evidence": saved})
    before = len([s for s, _ in agent.calls if s == "implement"])
    await workflow._task(rid, original.tasks[0], commit, original, {"specification": {}})
    assert len([s for s, _ in agent.calls if s == "implement"]) == before
    assert workflow.store.get(rid, "rounds/T1")["round"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("detected_by", ["T3", "T4"])
async def test_repair_t3_preserves_t1_t2_and_rechecks_t4_without_implementation(tmp_path, detected_by):
    from auto_agents_2.models import Change, ReviewResult

    class Agent(CodingAgent):
        implementations = []

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement":
                self.implementations.append(context["task"]["id"])
                return ImplementationResult(summary="repair part3", changes=[Change(path="part3.py", content="value = 3\n")])
            if schema is Diagnosis:
                return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=context["failure"]["evidence_refs"], reason="part3 regression",
                    repair_targets=[RepairTarget(task_id="T3", paths=["part3.py"], evidence_refs=["part3.py"], reason="owns implementation")])
            assert fresh
            return ReviewResult(approved=True, summary="actual task checks passed")

    agent = Agent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("four parts", {})["id"]
    original = plan()
    (tmp_path / "tests").mkdir()
    for i in range(1, 5):
        (tmp_path / f"part{i}.py").write_text(f"value = {9 if i == 3 else i}\n")
        (tmp_path / f"tests/test_{i}.py").write_text(f"from part{i} import value\ndef test_value(): assert value == {i}\n")
    workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/four-parts", "fixture: accepted baseline with later regression")
    base = workflow.workspaces.committed_baseline(rid, "main")
    integration = workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoints(rid, {"plan": original.model_dump(), "develop/done": ["T1", "T2", "T3", "T4"],
        **{f"develop/task/T{i}": {"commit": base, "checks": []} for i in range(1, 5)},
        **{f"task_state/T{i}": {"version": 1, "revision": 1, "implementation": "accepted", "evidence": "valid"} for i in range(1, 5)},
        **{f"rounds/T{i}": {"round": 1, "stalled": 0, "failure": ""} for i in range(1, 5)}})
    workflow.recovery.submit(rid, failure(kind="checks", task_id=detected_by, origin_id=detected_by))
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    await workflow._develop(rid, integration, original, {"specification": {}})
    assert agent.implementations == ["T3"]
    assert workflow.store.get(rid, "rounds/T3")["round"] == 2
    assert workflow.store.get(rid, "rounds/T4")["round"] == 1
    assert workflow.store.get(rid, "develop/task/T1")["commit"] == base
    assert workflow.store.get(rid, "develop/task/T4")["checks"][0]["tests"] == 1
    assert git(integration, "show", "HEAD:part3.py") == "value = 3"


def test_document_recovery_preserves_unrecognized_user_edit(tmp_path):
    store, rid = prepare_activation(tmp_path)
    path = tmp_path / "docs/aa2/plan.md"
    path.parent.mkdir(parents=True)
    path.write_text("four")
    store.activate_plan_patch(rid, "patch")
    candidate = PlanResult.model_validate(store.get(rid, "plan"))
    materialize_plan(store, rid, tmp_path, candidate)
    assert path.read_text() == "revised"
    path.write_text("user correction")
    with pytest.raises(AA2Error, match="user edits"):
        materialize_plan(store, rid, tmp_path, candidate)
    assert path.read_text() == "user correction"


@pytest.mark.asyncio
async def test_recheck_does_not_run_unimplemented_descendants_and_archives_candidate(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("four", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    previous = {"phase": "tested", "implementation": {"summary": "retained proposal", "changes": []},
                "checks": [{"exit_code": 1, "failure_reasons": ["unsupported_test_runner"]}]}
    workflow.store.checkpoints(rid, {"plan": plan().model_dump(), "develop/task/T3": previous})
    record = failure(kind="evidence")
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "develop/task/T3")["phase"] == "reverify"
    assert workflow.store.get(rid, "develop/task/T4") is None
    completed = workflow.store.get(rid, "recovery/completed/" + record.id + "/1")
    assert completed["previous_tasks"]["develop/task/T3"] == previous


def test_genuine_design_revision_archives_structured_plan_and_advances_version_once(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("four", {})["id"]
    original = plan().model_dump()
    proposal = {"recovery_request": {"suggested_action": "design", "reason": "constraint conflicts with acceptance"},
                "changes": [{"path": "README.md", "content": "not applied"}]}
    store.checkpoints(rid, {"plan": original, "plan_revision/current": {"version": 1, "revision": 2},
        "develop/done": ["T1", "T2"], "invocation/develop/task/T3": {"result": proposal},
        "rounds/T3": {"round": 3}, "prototype_approval": {"receipt": "retained"}})
    pending = store.prepare_replan(rid, "design", "Verified design conflict")
    store.complete_replan(rid, ["design", "plan", "develop", "readme", "verify"], ["refs/aa2/snapshot"])
    history = store.get(rid, "recovery/gate-history/0")
    assert history["pending"] == pending
    assert history["checkpoints"]["invocation/develop/task/T3"]["result"] == proposal
    assert history["checkpoints"]["develop/done"] == ["T1", "T2"]
    assert store.get(rid, "plan_revision/history/2")["plan"] == original
    assert store.get(rid, "plan_revision/current")["revision"] == 3
    assert store.get(rid, "plan") is None
    assert store.get(rid, "rounds/T3") == {"round": 3}
    assert store.get(rid, "prototype_approval") == {"receipt": "retained"}
    store.complete_replan(rid, ["design", "plan", "develop"], [])
    assert store.get(rid, "plan_revision/current")["revision"] == 3
    assert store.get(rid, "recovery/gate-history/0") == history


@pytest.mark.asyncio
async def test_diagnosis_reservation_survives_interruption_without_double_charge(tmp_path):
    import asyncio

    class Agent(CodingAgent):
        interrupted = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if schema is Diagnosis and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("four", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoint(rid, "plan", plan().model_dump())
    record = failure()
    workflow.recovery.submit(rid, record)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/diagnosis/" + record.id)["round"] == 1
    workflow = engine(tmp_path, agent)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/diagnosis/" + record.id)["round"] == 1
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "recovery/completed/" + record.id + "/1")


@pytest.mark.asyncio
async def test_invalid_diagnosis_is_returned_for_bounded_correction_without_generic_incident(tmp_path):
    class Agent(CodingAgent):
        attempts = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if schema is Diagnosis:
                self.attempts += 1
                if self.attempts == 1:
                    return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=["invented"], reason="invalid")
                assert "validation_error" in context["diagnosis_feedback"]
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("four", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoint(rid, "plan", plan().model_dump())
    record = failure()
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active")["diagnosis_feedback"]
    assert not workflow.store.pending(rid)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "recovery/diagnosis/" + record.id)["round"] == agent.attempts == 2


@pytest.mark.asyncio
async def test_delivery_repeated_collection_failure_requires_diagnosis_and_keeps_journal(tmp_path):
    from auto_agents_2.models import CheckResult
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("delivery", {})["id"]
    journal = {"phase": "published", "publication_started": True}
    workflow.store.checkpoint(rid, "delivery/journal", journal)
    result = CheckResult(argv=["pytest"], cwd=".", exit_code=1, duration=0, output="missing report",
                         source_hash="source", environment_hash="env", failure_reasons=["test_report_missing"])
    feedback = {"checks": [result.model_dump()]}
    assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ["part.py"]) == "recheck"
    assert not agent.calls
    workflow.recovery.start_delivery_recheck(rid, tmp_path)
    workflow.recovery.complete_delivery_recheck(rid, feedback)
    assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ["part.py"]) == "repair"
    assert agent.calls == [("recovery_diagnosis", True)]
    assert workflow.store.get(rid, "delivery/journal") == journal
    assert workflow.store.get(rid, "rounds/delivery") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["snapshot", "review", "journal", "activation", "document"])
async def test_local_patch_recovers_each_side_effect_boundary(tmp_path, monkeypatch, boundary):
    import asyncio

    class Agent(CodingAgent):
        interrupted = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if schema is PlanPatch:
                return patch_for(PlanResult.model_validate(context["plan"]), evidence_contract=empty_contract(context))
            if boundary == "review" and context.get("review_kind") == "plan_patch" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("four", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    integration = workflow.workspaces.allocate(rid, "integration", base)
    worker = workflow.workspaces.allocate(rid, "T3-retained", base)
    (worker / "part3.py").write_text("unfinished = True\n")
    original = plan()
    seed_requirements(workflow, rid, original)
    workflow.store.checkpoints(rid, {"plan": original.model_dump(), "develop/done": ["T1", "T2"],
        "task_origins": {t.id: t.origin_id for t in original.tasks}, "rounds/T3": {"round": 3},
        "develop/task/T3": {"workspace": str(worker), "phase": "implementing"}})
    record = failure()
    workflow.recovery.submit(rid, record)
    active = workflow.store.get(rid, "recovery/active")
    active["decision"] = {"version": 1, "failure_id": record.id, "action": "plan_patch", "task_ids": ["T3"],
                          "evidence_refs": record.evidence_refs, "reason": "repair contract", "budget_owner": "T3"}
    workflow.store.checkpoint(rid, "recovery/active", active)
    interrupted = False

    def inject(function, matches):
        def wrapped(*args, **kwargs):
            nonlocal interrupted
            result = function(*args, **kwargs)
            if not interrupted and matches(args):
                interrupted = True
                raise asyncio.CancelledError()
            return result
        return wrapped

    with monkeypatch.context() as patching:
        if boundary == "snapshot":
            patching.setattr(workflow.workspaces, "checkpoint", inject(workflow.workspaces.checkpoint, lambda a: "plan-patches" in a[1]))
        elif boundary == "journal":
            patching.setattr(workflow.store, "checkpoint", inject(workflow.store.checkpoint, lambda a: a[1].startswith("plan_revision/journal/")))
        elif boundary == "activation":
            patching.setattr(workflow.store, "activate_plan_patch", inject(workflow.store.activate_plan_patch, lambda a: True))
        elif boundary == "document":
            patching.setattr("auto_agents_2.recovery_controller.materialize_plan", inject(materialize_plan, lambda a: True))
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    workflow = engine(tmp_path, agent)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 2
    assert workflow.store.get(rid, "rounds/T3") == {"round": 3}
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    assert (worker / "part3.py").read_text() == "unfinished = True\n"
    assert workflow.store.get(rid, "develop/task/T3")["workspace"] == str(worker)
    materialize_plan(workflow.store, rid, integration, PlanResult.model_validate(workflow.store.get(rid, "plan")))
    assert (integration / "docs/aa2/plan.md").read_text() == "revised"
