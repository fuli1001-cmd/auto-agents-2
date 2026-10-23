"""A plan amendment and its reviewer share the selected recovery objective and history."""
import asyncio
import copy

import pytest
from evidence_test_fixtures import empty_contract, evidence_review
from test_decisions import ApprovingAgent
from test_recovery_contracts import failure
from test_recovery_policy import ready
from test_workflow import engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import Change, ImplementationResult, PlanResult, ReviewResult
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.workspace import tree_hash

REASON = "Restore the original historical check minimum structurally; prose alone leaves the contract incomplete."
DESIGN = {"summary": "Accepted design", "document": "# Design\nRetain historical regressions and existing task boundaries."}


class HandoffAgent(ApprovingAgent):
    def __init__(self, assert_stage="plan", *, interrupt=False, reject=False):
        super().__init__()
        self.assert_stage, self.interrupt, self.reject = assert_stage, interrupt, reject
        self.views = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if schema is Diagnosis:
            return Diagnosis(action="plan_patch", task_ids=["T3"] if context["failure"]["task_id"] else [],
                             reason=REASON, evidence_refs=["source_history", "plan.tasks[2]"])
        if schema is PlanPatch or context.get("review_kind") == "plan_patch":
            view = ContextPlanner(self.workflow.artifacts).prepare(stage, context, workspace).payload
            self.views.append((stage, copy.deepcopy(view)))
            if stage == self.assert_stage or self.assert_stage == "both":
                assert view["recovery_decision"]["action"] == "plan_patch"
                assert view["recovery_decision"]["reason"] == REASON
                assert view["design"]["document"] == DESIGN["document"]
                manifest = self.workflow.artifacts.json(view["source_history"]["artifact"])
                original = next(p for p in manifest["proposals"] if p["task_id"] == "T3")
                assert original["status"] == "proposal_only"
                assert original["commands"][0]["expected_tests"] == 40
                assert original["content"] in view["evidence_materials"]
                assert self.workflow.artifacts.json(original["content"])["changes"][0]["content"] == "retained proposal\n"
                if context["failure"]["task_id"]:
                    assert not [p for p in manifest["proposals"] if p["task_id"] == "T1"]
                else:
                    assert any(p["task_id"] == "T1" for p in manifest["proposals"])
            if schema is PlanPatch:
                plan = PlanResult.model_validate(context["plan"])
                task = plan.tasks[2].model_copy(deep=True)
                task.checks[0].expected_tests = 40
                return PlanPatch(evidence_contract=empty_contract(context), base_revision=context["base_revision"], failure_ids=[context["failure"]["id"]],
                                 reason=REASON, document="structurally restored checks", upsert_tasks=[task])
            assert context["plan"]["tasks"][2]["checks"][0]["expected_tests"] == 40
            if self.interrupt:
                self.interrupt = False
                raise asyncio.CancelledError()
            return evidence_review(context, ReviewResult(approved=not self.reject, summary="Independent contract review"))
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


def setup_handoff(tmp_path, agent, *, final=False):
    record = failure(task_id="", origin_id="final", step="final") if final else failure()
    workflow, rid, work, plan, record = ready(tmp_path, agent=agent, record=record)
    agent.workflow = workflow
    historical = plan.model_copy(deep=True)
    historical.tasks[2].checks[0].expected_tests = 40
    proposed = {f"develop/task/{t.id}": {"phase": "rejected", "implementation": ImplementationResult(
        summary="Unapplied historical candidate", changes=[Change(path=f"part{t.id[1:]}.py", content="retained proposal\n")]).model_dump()}
        for t in [plan.tasks[0], plan.tasks[2]]}
    workflow.store.checkpoints(rid, {"design": DESIGN, "task_origins": {t.id: t.origin_id for t in plan.tasks},
        "recovery/gate-history/0": {"checkpoints": {"plan": historical.model_dump(), **proposed}}})
    return workflow, rid, work, plan, record


@pytest.mark.parametrize("stage", ["plan", "review"])
@pytest.mark.parametrize("final", [False, True])
async def test_author_and_reviewer_receive_objective_and_scoped_history(tmp_path, stage, final):
    agent = HandoffAgent(assert_stage=stage)
    workflow, rid, work, old, record = setup_handoff(tmp_path, agent, final=final)
    await workflow.recovery.resume(rid)
    plan = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    assert plan.tasks[2].checks[0].expected_tests == 40
    assert plan.tasks[:2] == old.tasks[:2] and plan.tasks[3] == old.tasks[3]
    assert plan.final_checks == old.final_checks and plan.runtime_probes == old.runtime_probes
    assert not workflow.store.get(rid, "develop/done") and not (work / "part3.py").exists()
    author, reviewer = [v for _, v in agent.views]
    for key in ("recovery_decision", "source_history", "design"):
        assert author[key] == reviewer[key]
    assert workflow.store.get(rid, f"recovery/completed/{record.id}/1")["decision"] == author["recovery_decision"]


async def test_rejected_patch_review_does_not_advance_plan_or_accept_tasks(tmp_path):
    agent = HandoffAgent(assert_stage="both", reject=True)
    workflow, rid, work, plan, record = setup_handoff(tmp_path, agent)
    source = tree_hash(work)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "plan") == plan.model_dump()
    assert tree_hash(work) == source and not workflow.store.get(rid, "develop/done")
    assert not workflow.store.get(rid, "plan_revision/current")
    assert workflow.store.get(rid, f"recovery/patch-rejection/{record.id}/1")["patch_review"]["approved"] is False


async def test_interrupted_review_reuses_the_frozen_author_evidence(tmp_path):
    agent = HandoffAgent(assert_stage="both", interrupt=True)
    workflow, rid, work, _, _ = setup_handoff(tmp_path, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    initial = [v for stage, v in agent.views if stage == "plan"][0]
    # An additional archive after generation cannot silently change the review's inputs.
    workflow.store.checkpoint(rid, "recovery/gate-history/99", {"checkpoints": {"develop/task/T3": {
        "implementation": ImplementationResult(summary="Later archive", changes=[Change(path="part3.py", content="later\n")]).model_dump()}}})
    workflow = engine(tmp_path, agent)
    agent.workflow = workflow
    await workflow.recovery.resume(rid)
    assert len([v for stage, v in agent.views if stage == "plan"]) == 1
    reviews = [v for stage, v in agent.views if stage == "review"]
    assert len(reviews) == 2
    assert all(v["source_history"] == initial["source_history"] for v in reviews)
    assert workflow.store.get(rid, "plan")["tasks"][2]["checks"][0]["expected_tests"] == 40
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1
    assert not workflow.store.get(rid, "develop/done")
