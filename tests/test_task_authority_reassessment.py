"""A stale dispatch is reassessed, never silently rebound to a newer plan."""
from plan_proposal_fixtures import EvidencePlanPatch, proposal_response
import copy

import pytest
from evidence_test_fixtures import empty_contract, evidence_review
from test_decisions import ApprovingAgent
from test_recovery_contracts import failure
from test_recovery_policy import ready
from test_workflow import engine

from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, ImplementationResult, PlanResult
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.recovery import Diagnosis, RecoveryNeeded, adjudicate
from auto_agents_2.util import digest
from auto_agents_2.task_authority import plan_reassessment
from auto_agents_2.workspace import git, tree_hash


class ReassessmentAgent(ApprovingAgent):
    def __init__(self):
        super().__init__()
        self.patches = 0
        self.diagnoses = []
        self.extra_patch = False

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if schema is Diagnosis:
            self.diagnoses.append(copy.deepcopy(context))
            return Diagnosis(action="plan_patch" if self.patches < (3 if self.extra_patch else 2) else "repair",
                task_ids=[context["failure"]["task_id"]], evidence_refs=["failure", "plan"],
                reason="Assess the retained task against the current formally accepted contract")
        if schema in (PlanPatch, EvidencePlanPatch):
            self.patches += 1
            plan = PlanResult.model_validate(context["plan"])
            task = plan.tasks[2 if self.patches == 2 else 0].model_copy(deep=True)
            task.checks[0].expected_tests += 1
            return proposal_response(PlanPatch(base_revision=context["base_revision"], failure_ids=[context["failure"]["id"]],
                reason="Retain the existing target and increase its check minimum",
                document="Reviewed plan revision " + str(self.patches + 1), upsert_tasks=[task],
                evidence_contract=empty_contract(context)), context, schema)
        if stage == "implement":
            return ImplementationResult(summary="Implement the unchanged task", changes=[
                Change(path="part1.py", content="def add(a, b):\n    return a + b\n"),
                Change(path="tests/test_1.py", content="from part1 import add\ndef test_positive(): assert add(2,3) == 5\ndef test_negative(): assert add(-2,-3) == -5\n")])
        if context.get("review_kind") == "plan_patch":
            return evidence_review(context)
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


async def stale_dispatch(tmp_path):
    agent = ReassessmentAgent()
    workflow, rid, integration, plan, original = ready(tmp_path, agent=agent,
        record=failure(task_id="T1", origin_id="T1", obligations=["R1"]))
    worker = workflow.workspaces.allocate(rid, "T1-retained", git(integration, "rev-parse", "HEAD"))
    workflow.store.checkpoints(rid, {"authority/protocol": 1,
        "task_origins": {t.id: t.origin_id for t in plan.tasks},
        "plan_revision/current": {"version": 1, "revision": 1},
        "develop/task/T1": {"phase": "implementing", "workspace": str(worker), "source_hash": tree_hash(worker)},
        "rounds/T1": {"round": 7, "stalled": 2}, "prototype_approval": {"retained": "human binding"}})
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 2
    old_key = workflow.store.get(rid, "develop/task/T1")["recovery_permit"]
    old_permit = workflow.store.get(rid, old_key)
    workflow.recovery.submit(rid, failure())
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 3
    current = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    base = workflow.workspaces.checkpoint(integration, "refs/aa2/fixture/current", "current reviewed plan")
    return workflow, rid, integration, worker, current, base, old_key, old_permit


async def test_stale_dispatch_enters_fresh_diagnosis_without_consuming_old_permit(tmp_path):
    workflow, rid, integration, worker, plan, base, key, permit = await stale_dispatch(tmp_path)
    before_rounds = workflow.store.get(rid, "rounds/T1")
    before_diagnoses = len(workflow.agent.diagnoses)
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {}})
    record = caught.value.failure
    assert record.kind == "contract" and record.component == "task_authority"
    assert record.step == "authority_reassessment" and record.origin_id == "T1"
    assert record.evidence["checks"] == [] and "review" not in record.evidence
    proof = record.evidence["authority_reassessment"]
    assert proof["previous_permit"] == key and proof["previous_permit_hash"] == digest(permit)
    archived = workflow.artifacts.json(proof["history_artifact"])
    assert archived["historical_dispatch"]["permit"] == permit
    assert archived["transition"]["receipt"]["review"]["approved"]
    assert workflow.store.get(rid, key) == permit
    assert workflow.store.get(rid, "rounds/T1") == before_rounds
    assert workflow.store.get(rid, "prototype_approval") == {"retained": "human binding"}
    workflow.recovery.submit(rid, record)
    workflow = engine(workflow.project, workflow.agent)  # Resume the durable observation after interruption.
    await workflow.recovery.resume(rid)
    assert len(workflow.agent.diagnoses) == before_diagnoses + 1
    actual = workflow.agent.diagnoses[-1]["failure"]["evidence"]["authority_reassessment"]
    assert actual == proof
    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.diagnostics import references
    delivered = ContextPlanner(workflow.artifacts).prepare("recovery_diagnosis", workflow.agent.diagnoses[-1], worker)
    assert proof["history_artifact"]["hash"] in {r["hash"] for r in references(delivered.payload)}
    assert workflow.artifacts.json(proof["history_artifact"]) == archived
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["recovery_permit"] != key
    current_permit = workflow.store.get(rid, saved["recovery_permit"])
    assert current_permit["authority_sources"] == workflow.authority.identity(rid)
    assert current_permit["plan_hash"] == digest(plan.model_dump())
    assert workflow.recovery_policy.task_authorized(rid, saved, plan.tasks[0])
    assert workflow.store.get(rid, key) == permit
    assert workflow.store.get(rid, "rounds/T1") == before_rounds
    assert "T1" not in workflow.store.get(rid, "develop/done", [])
    assert not (worker / "part1.py").exists()

    commit = await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {}})
    verified = workflow.store.get(rid, "develop/task/T1")
    assert commit == verified["commit"] and verified["review"]["approved"]
    assert sum(row["tests"] for row in verified["checks"]) == 2
    assert all(row["exit_code"] == 0 for row in verified["checks"])
    assert workflow.store.get(rid, "rounds/T1")["round"] == before_rounds["round"] + 1
    assert workflow.store.get(rid, key) == permit
    await workflow.recovery.resume(rid)
    assert len(workflow.agent.diagnoses) == before_diagnoses + 1


async def observed(tmp_path):
    values = await stale_dispatch(tmp_path)
    workflow, rid, _, _, plan, base, _, _ = values
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {}})
    return values, caught.value.failure


@pytest.mark.parametrize("damage", ["capture", "clarify", "clarify_context", "design", "prototype_approval",
    "answer", "receipt", "old_transition", "frozen_request", "cycle", "history_patch", "baseline_patch",
    "completed", "outcome", "resolution", "recording", "global_contract", "task_contract", "missing_source"])
async def test_unknown_drift_or_retained_candidate_does_not_enter_reassessment(tmp_path, damage):
    values, record = await observed(tmp_path)
    workflow, rid, _, _, plan, _, key, permit = values
    saved = workflow.store.get(rid, "develop/task/T1")
    if damage in {"capture", "clarify", "clarify_context", "design", "prototype_approval"}:
        workflow.store.checkpoint(rid, damage, {"new_input": damage})
    elif damage == "missing_source":
        saved.pop("source_hash")
    elif damage == "answer":
        q = workflow.store.request(rid, "design/0", "Actual product change", "bound-answer")
        workflow.store.answer(q["id"], "new requirement", "bound-answer")
    elif damage == "receipt":
        receipt_key = workflow.store.get(rid, "authority/accepted/plan")["review_key"]
        receipt = workflow.store.get(rid, receipt_key)
        receipt["review_hash"] = "corrupt"
        workflow.store.checkpoint(rid, receipt_key, receipt)
    elif damage in {"old_transition", "completed", "outcome", "resolution", "recording"}:
        changed = copy.deepcopy(permit)
        if damage == "old_transition":
            changed["authority_transition"]["review_hash"] = "corrupt"
        elif damage == "completed":
            changed["phase"] = "completed"
        elif damage == "outcome":
            changed["outcome"].setdefault("tasks", {})["T1"] = {"status": "failed"}
        else:
            changed["resolution_diagnosis" if damage == "resolution" else "recording"] = {"preserve": "specialized restriction"}
        workflow.store.checkpoint(rid, key, changed)
    elif damage == "frozen_request":
        request_key = key.replace("recovery/permits/", "recovery/request-snapshots/")
        request = workflow.store.get(rid, request_key)
        request["request"]["scope"]["tasks"][0]["allowed_paths"].append("new-scope")
        workflow.store.checkpoint(rid, request_key, request)
    elif damage == "cycle":
        cycle_key = "recovery/cycles/" + permit["cycle_id"]
        cycle = workflow.store.get(rid, cycle_key)
        cycle["permits"] = []
        workflow.store.checkpoint(rid, cycle_key, cycle)
    elif damage == "history_patch":
        history_key = "plan_revision/history/2"
        history = workflow.store.get(rid, history_key)
        history["tasks"]["develop/task/T1"]["previous_evidence"]["patch"] = [{"path": "part1.py", "before": "original"}]
        workflow.store.checkpoint(rid, history_key, history)
    elif damage == "baseline_patch":
        baseline_key = record.evidence["authority_reassessment"]["retained_baselines"][0]["journal"]
        journal = workflow.store.get(rid, baseline_key)
        journal["previous_task"]["patch"] = [{"path": "part1.py", "before": "original"}]
        workflow.store.checkpoint(rid, baseline_key, journal)
    else:
        changed = plan.model_copy(deep=True)
        if damage == "global_contract":
            changed.runtime_probes[0].argv[-1] += "; print('changed')"
        else:
            changed.tasks[0].allowed_paths.append("new-scope")
        workflow.store.checkpoint(rid, "plan", changed.model_dump())
    before = workflow.store.get(rid, key)
    assert plan_reassessment(workflow, rid, plan.tasks[0], saved) is None
    assert workflow.store.get(rid, key) == before
    assert workflow.store.get(rid, "rounds/T1")["round"] == 7


@pytest.mark.parametrize("when", ["before_diagnosis", "after_diagnosis", "decision_wait"])
async def test_new_source_during_reassessment_never_authorizes_mutation(tmp_path, when):
    values, record = await observed(tmp_path)
    workflow, rid, _, worker, plan, _, old_key, permit = values
    workflow.recovery.submit(rid, record)
    original = workflow.agent.invoke
    async def changed(stage, *args, **kwargs):
        result = await original(stage, *args, **kwargs)
        if stage == ("decision" if when == "decision_wait" else "recovery_diagnosis"):
            workflow.store.checkpoint(rid, "design", {"new_input": "arrived during an authorized read"})
        return result
    if when == "before_diagnosis":
        workflow.store.checkpoint(rid, "design", {"new_input": "arrived before diagnosis"})
    else:
        workflow.agent.invoke = changed
    with pytest.raises(StateConflict, match="authority reassessment sources changed"):
        if when == "decision_wait":
            active = workflow.store.get(rid, "recovery/active")
            await workflow.recovery_policy.authorize(rid, record, active, worker, plan, adjudicate(record),
                operation="fixture-decision", force_decision=True)
        else:
            await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, old_key) == permit
    assert workflow.store.get(rid, "develop/task/T1")["recovery_permit"] == old_key
    assert workflow.store.get(rid, "rounds/T1")["round"] == 7


async def test_fresh_diagnosis_can_choose_reviewed_patch_then_followup(tmp_path):
    values, record = await observed(tmp_path)
    workflow, rid, _, _, _, _, key, permit = values
    workflow.agent.extra_patch = True
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 4
    assert workflow.store.get(rid, "recovery/active") is None
    saved = workflow.store.get(rid, "develop/task/T1")
    fresh = workflow.recovery_policy.validate_consumption(rid, saved["recovery_permit"], expected={"repair"})
    assert fresh["authority_sources"] == workflow.authority.identity(rid)
    assert fresh["authority_transition"]["before"] == record.evidence["authority_reassessment"]["current_sources"]
    assert workflow.store.get(rid, key) == permit
    assert workflow.store.get(rid, "rounds/T1")["round"] == 7


async def test_late_authority_change_is_not_adopted_into_new_permit(tmp_path, monkeypatch):
    values, record = await observed(tmp_path)
    workflow, rid, _, worker, plan, _, key, permit = values
    workflow.recovery.submit(rid, record)
    active = workflow.store.get(rid, "recovery/active")
    original = workflow.store.apply_decision
    def concurrent(*args, **kwargs):
        workflow.store.checkpoint(rid, "design", {"new_input": "arrived immediately before authorization commit"})
        return original(*args, **kwargs)
    monkeypatch.setattr(workflow.store, "apply_decision", concurrent)
    selected = adjudicate(record).model_copy(update={"action": "repair"})
    _, new_key = await workflow.recovery_policy.authorize(rid, record, active, worker, plan, selected,
        operation="fixture-late-cas", established=True, candidates={"repair": selected})
    fresh = workflow.store.get(rid, new_key)
    assert fresh["authority_sources"] == record.evidence["authority_reassessment"]["current_sources"]
    assert fresh["authority_sources"] != workflow.authority.identity(rid)
    with pytest.raises(StateConflict, match="authority sources changed"):
        workflow.recovery_policy.start(rid, new_key)
    assert workflow.store.get(rid, new_key)["phase"] == "reserved"
    assert workflow.store.get(rid, key) == permit
    assert workflow.store.get(rid, "rounds/T1")["round"] == 7


async def test_user_edit_after_observation_is_preserved_before_diagnosis(tmp_path):
    values, record = await observed(tmp_path)
    workflow, rid, _, worker, _, _, key, permit = values
    workflow.recovery.submit(rid, record)
    before = len(workflow.agent.diagnoses)
    (worker / "user-notes.txt").write_text("Keep this user change")
    with pytest.raises(StateConflict, match="authority reassessment workspace changed"):
        await workflow.recovery.resume(rid)
    assert (worker / "user-notes.txt").read_text() == "Keep this user change"
    assert len(workflow.agent.diagnoses) == before
    assert workflow.store.get(rid, key) == permit
    assert workflow.store.get(rid, "rounds/T1")["round"] == 7
