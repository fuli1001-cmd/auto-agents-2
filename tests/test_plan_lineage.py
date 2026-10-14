import copy

import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.engine import STAGES, Replan
from auto_agents_2.models import Finding, ReviewResult


class ReachedDevelopment(Exception):
    pass


async def stop_before_development(*args):
    raise ReachedDevelopment()


def pending_run(tmp_path, agent):
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, agent)
    rid = workflow.store.admit(str(requirements), {})["id"]
    workflow._develop = stop_before_development
    return workflow, rid


@pytest.mark.asyncio
async def test_legacy_failure_feedback_does_not_hide_initial_origin_registration(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "plan":
                assert context["task_origins"] == {}
                assert context["task_budgets"] == {}
                assert "Replacement task T002" in context["feedback"]["reason"]
            if operation == "plan-review":
                # The reviewer must see the controller's current result, not the
                # empty mapping given to the planner before registration.
                assert context["task_origins"] == {"T001": "T001", "T002": "T002"}
                proof = context["task_origin_validation"]
                assert proof["status"] == "passed" and proof["registration"] == "initial"
                assert proof["prior_origins"] == {} and context["task_budgets"] == {}
                assert fresh
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "plan":
                result.tasks[0].id = "T001"
                result.tasks.append(result.tasks[0].model_copy(deep=True, update={"id": "T002", "depends_on": ["T001"]}))
            return result

    workflow, rid = pending_run(tmp_path, Agent())
    reason = "Replacement task T002 must retain an existing origin_id so budgets cannot reset"
    workflow.store.checkpoint(rid, "replan_feedback", {"stage": "plan", "reason": reason})
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    assert workflow.store.get(rid, "task_origins") == {"T001": "T001", "T002": "T002"}
    assert workflow.store.get(rid, "rounds/T001") is None
    assert workflow.store.get(rid, "rounds/T002") is None
    assert workflow.store.get(rid, "plan") is not None


@pytest.mark.asyncio
async def test_replan_supplies_full_open_findings_and_current_budget_evidence(tmp_path):
    finding = Finding(severity="blocking", location="plan.tasks[T1].checks",
                      description="Missing negative operand acceptance case",
                      evidence="The declared checks cover only positive operands",
                      resolution="Include a check asserting add(-2, -3) == -5")
    counters = {"round": 3, "stalled": 1, "failure": "historical failure"}

    class Agent(CodingAgent):
        reviews = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "plan" or operation == "plan-review":
                assert context["task_origins"] == {"T1": "T1"}
                assert context["task_budgets"] == {"T1": counters}
                if self.reviews:
                    issues = context["unresolved_issues"]
                    assert len(issues) == 1 and issues[0]["original"] == finding.model_dump()
                    assert issues[0]["observations"] and issues[0]["status"] == "open"
            if operation == "plan-review":
                assert context["task_origin_validation"]["registration"] == "existing"
                self.reviews += 1
                if self.reviews == 1:
                    return ReviewResult(approved=False, summary="Plan needs correction", findings=[finding])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent()
    workflow, rid = pending_run(tmp_path, agent)
    workflow.store.checkpoint(rid, "task_origins", {"T1": "T1"})
    workflow.store.checkpoint(rid, "rounds/T1", counters)
    with pytest.raises(Replan) as rejected:
        await workflow._execute(rid)
    assert finding.description in rejected.value.reason
    assert finding.evidence in rejected.value.reason and finding.resolution in rejected.value.reason
    ledger = copy.deepcopy(workflow.store.get(rid, "issues"))
    assert next(iter(ledger.values()))["status"] == "open"
    workflow.store.prepare_replan(rid, rejected.value.stage, rejected.value.reason)
    workflow.store.complete_replan(rid, STAGES[STAGES.index("plan"):], [])
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    assert agent.reviews == 2
    assert workflow.store.get(rid, "rounds/T1") == counters
    assert next(iter(workflow.store.get(rid, "issues").values()))["status"] == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [None, {"T1": "T1"}])
async def test_missing_origin_registry_with_existing_budget_is_not_initial_plan(tmp_path, registry):
    workflow, rid = pending_run(tmp_path, CodingAgent())
    if registry is not None:
        workflow.store.checkpoint(rid, "task_origins", registry)
    counters = {"round": 3, "stalled": 1}
    workflow.store.checkpoint(rid, "rounds/old-task", counters)
    with pytest.raises(Replan, match="origin history is missing"):
        await workflow._execute(rid)
    assert workflow.store.get(rid, "task_origins") == registry
    assert workflow.store.get(rid, "rounds/old-task") == counters
    assert workflow.store.get(rid, "plan") is None
