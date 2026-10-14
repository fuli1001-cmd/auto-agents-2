import sys

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.engine import STAGES, Replan
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import AA2Error
from auto_agents_2.models import Command, PlanResult, TaskSpec
from auto_agents_2.planning import validate_plan
from auto_agents_2.sandbox import SandboxRunner


def plan(**fields):
    check = Command(argv=[sys.executable, "-m", "pytest", "-q", "tests"], expected_tests=1)
    task = TaskSpec(id="T1", title="addition", description="implement addition", requirement_ids=["R1"],
                    allowed_paths=["calc.py", "tests"], acceptance=["sum"], checks=[check])
    return PlanResult(summary="plan", document="plan", tasks=[task], final_checks=[check],
                      runtime_probes=[Command(argv=[sys.executable, "-c", "import calc"])], **fields)


@pytest.mark.parametrize("field,value", [
    ("environment_file", "name: example\ndependencies:\n  - python=3.12\n"),
    ("requirements_file", "fastapi>=0.115,<1\npytest>=8,<9\n"),
    ("environment_file", "/tmp/environment.yml"),
    ("requirements_file", "../requirements.txt"),
])
def test_plan_rejects_manifest_contents_and_external_paths(field, value):
    with pytest.raises(AA2Error, match=field + ".*relative file path"):
        validate_plan(plan(**{field: value}), {"R1"})


def test_plan_accepts_relative_manifests_before_implementation_creates_them():
    validate_plan(plan(environment_file="config/environment.yml", requirements_file="dependencies/dev.in"), {"R1"})
    validate_plan(plan(), {"R1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["environment_file", "requirements_file"])
async def test_environment_rejects_inline_selector_without_installing_or_echoing_contents(tmp_path, field):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    invoked = False

    async def prepare(*args):
        nonlocal invoked
        invoked = True
        raise AssertionError("Must validate selectors before preparing an environment")

    manager._prepare = prepare
    body = "private diagnostic fixture\nnot a filename\n"
    with pytest.raises(AA2Error, match=field + ".*relative file path") as caught:
        await manager.ensure(tmp_path, "python", **{field: body})
    assert body not in str(caught.value) and not invoked


@pytest.mark.asyncio
async def test_invalid_plan_selector_replans_before_review_and_development(tmp_path):
    class Agent(CodingAgent):
        corrected = False
        reviews = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if operation == "plan-review":
                self.reviews += 1
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "plan":
                if self.corrected:
                    assert "environment_file" in context["feedback"]["reason"]
                    result.environment_file = "environment.yml"
                else:
                    result.environment_file = "dependencies:\n  - python=3.12\n"
            return result

    class ReachedDevelopment(Exception):
        pass

    async def stop(*args):
        raise ReachedDevelopment()

    agent = Agent()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, agent)
    workflow._develop = stop
    rid = workflow.store.admit(str(requirements), {})["id"]
    with pytest.raises(Replan, match="environment_file") as failure:
        await workflow._execute(rid)
    assert agent.reviews == 0 and workflow.store.get(rid, "plan") is None
    workflow.store.prepare_replan(rid, failure.value.stage, failure.value.reason)
    workflow.store.complete_replan(rid, STAGES[STAGES.index("plan"):], [])
    agent.corrected = True
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    assert agent.reviews == 1


@pytest.mark.asyncio
async def test_repairing_saved_selector_resumes_applied_task_without_new_implementation(tmp_path):
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("addition", {})["id"]
    proposal = plan(environment_file="dependencies:\n  - python=3.12\n")
    workflow.store.checkpoint(rid, "plan", proposal.model_dump())
    prepare = workflow._environment

    async def validate_environment(work, run_id, current_plan):
        validate_plan(current_plan, {"R1"})
        return await prepare(work, run_id, current_plan)

    workflow._environment = validate_environment
    base = workflow.workspaces.committed_baseline(rid, "main")
    context = {"specification": {"description": "addition"}}
    with pytest.raises(AA2Error, match="environment_file"):
        await workflow._task(rid, proposal.tasks[0], base, proposal, context)
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["phase"] == "applied"
    counters = workflow.store.get(rid, "rounds/T1")
    corrected = proposal.model_copy(update={"environment_file": "environment.yml"})
    workflow.store.checkpoint(rid, "plan", corrected.model_dump())
    await workflow._task(rid, corrected.tasks[0], base, corrected, context)
    assert sum(stage == "implement" for stage, _ in agent.calls) == 1
    assert workflow.store.get(rid, "rounds/T1") == counters
    assert workflow.store.get(rid, "develop/task/T1")["commit"]
