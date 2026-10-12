import asyncio
import sys
from pathlib import Path

import pytest

from auto_agents_2.config import ConfigStore
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.environment import Environment
from auto_agents_2.models import (
    Change,
    Command,
    Coverage,
    DocumentResult,
    Finding,
    ImplementationResult,
    IntakeResult,
    PlanResult,
    PreviewSpec,
    Requirement,
    ReviewResult,
    SourceEvidence,
    TaskSpec,
)
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.workspace import WorkspaceManager, apply_prepared, git, prepare_changes


class CodingAgent:
    def __init__(self):
        self.calls = []
        self.interrupt_review = False

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        self.calls.append((stage, fresh))
        check = Command(argv=[sys.executable, "-m", "pytest", "-q", "tests"], expected_tests=1)
        if stage == "clarify":
            implemented = (workspace / "calc.py").is_file() and "return a + b" in (workspace / "calc.py").read_text()
            return IntakeResult(summary="addition", document="# Addition\nReturn the sum of both inputs.",
                                requirements=[Requirement(id="R1", description="addition", acceptance=["2 + 3 == 5"])],
                                coverage=[Coverage(requirement_id="R1", status="satisfied" if implemented else "changed",
                                                   evidence_paths=["calc.py"] if implemented else [], rationale="source inspected")],
                                verification=[check] if implemented else [])
        if stage in {"provider_research", "design"}:
            return DocumentResult(summary=stage, document=f"# {stage}\nNo external services required.")
        if stage == "plan":
            return PlanResult(summary="one task", document="# Plan", tasks=[TaskSpec(
                id="T1", title="addition", description="implement addition", requirement_ids=["R1"],
                allowed_paths=["calc.py", "tests"], acceptance=["sum"], checks=[check])], final_checks=[check])
        if stage == "implement":
            return ImplementationResult(summary="implemented", changes=[
                Change(path="calc.py", content="def add(a, b):\n    return a + b\n"),
                Change(path="tests/test_calc.py", content="from calc import add\n\ndef test_sum():\n    assert add(2, 3) == 5\n"),
            ])
        if stage == "readme":
            return DocumentResult(summary="documented", document="# Done", files=[Change(path="README.md", content="# Calculator\nRun pytest tests.\n")])
        if self.interrupt_review and operation.endswith("/review"):
            self.interrupt_review = False
            raise asyncio.CancelledError()
        assert fresh
        return ReviewResult(approved=True, summary="verified")


def engine(project, agent):
    config = ConfigStore(project)
    config.initialize()
    body = config.load().model_dump(mode="json")
    body["cleanup"]["min_free_bytes"] = 0
    write_json(config.path, body)
    WorkspaceManager(project).initialize()
    sandbox = SandboxRunner(project / ".aa2", _test_unconfined=True)
    result = WorkflowEngine(project, agent=agent, sandbox=sandbox)

    async def environment(*args, **kwargs):
        return Environment(Path(sys.prefix), "test-environment")

    result._environment = environment
    return result


@pytest.mark.asyncio
async def test_complete_delivery_same_input_and_semantic_noop(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    agent = CodingAgent()
    workflow = engine(project, agent)
    first = await workflow.start(str(requirements))
    assert first["status"] == "SUCCEEDED"
    assert first["data"]["outcome"] == "DELIVERED"
    assert "return a + b" in git(project, "show", first["data"]["delivery_commit"] + ":calc.py")
    calls = len(agent.calls)
    second = await engine(project, agent).start(str(requirements))
    assert second["data"]["outcome"] == "NO_CHANGES"
    assert len(agent.calls) == calls
    requirements.write_text("Compute the sum of two inputs")
    third = await engine(project, agent).start(str(requirements))
    assert third["data"]["outcome"] == "NO_CHANGES"
    assert [stage for stage, _ in agent.calls[calls:]] == ["clarify", "coverage_review"]
    assert not list((project / ".aa2/worktrees").glob("*/*"))


@pytest.mark.asyncio
async def test_resume_during_review_reuses_implementation_and_round(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add numbers")
    agent = CodingAgent()
    agent.interrupt_review = True
    workflow = engine(project, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    interrupted = Store(project).current()
    assert interrupted["status"] == "PAUSED"
    before = len([s for s, _ in agent.calls if s == "implement"])
    result = await engine(project, agent).start()
    assert result["status"] == "SUCCEEDED"
    assert len([s for s, _ in agent.calls if s == "implement"]) == before
    assert Store(project).get(result["id"], "rounds/T1")["round"] == 1


def test_interrupted_patch_application_is_idempotent(tmp_path):
    (tmp_path / "one.py").write_text("old")
    (tmp_path / "two.py").write_text("old")
    patch = prepare_changes(tmp_path, [Change(path="one.py", operation="replace", old_text="old", content="new"),
                                       Change(path="two.py", operation="replace", old_text="old", content="new")], ["*.py"])
    apply_prepared(tmp_path, patch[:1])
    apply_prepared(tmp_path, patch)
    assert (tmp_path / "one.py").read_text() == "new"
    assert (tmp_path / "two.py").read_text() == "new"


@pytest.mark.asyncio
async def test_reviewer_rejects_test_weakening_before_any_candidate_is_applied(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "tests").mkdir()
    test_content = "from calc import add\n\ndef test_sum():\n    assert add(2, 3) == 5\n"
    (project / "tests/test_calc.py").write_text(test_content)
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Implement addition and preserve the existing acceptance test")

    class MisguidedAgent(CodingAgent):
        attempts = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement":
                self.attempts += 1
                assert (workspace / "tests/test_calc.py").read_text() == test_content
                if self.attempts == 1:
                    return ImplementationResult(summary="wrong proposal", changes=[
                        Change(path="calc.py", content="def add(a,b): return 0\n"),
                        Change(path="tests/test_calc.py", content="def test_sum(): assert True\n")])
            if operation.endswith("test-change-review") and self.attempts == 1:
                assert fresh and not (workspace / "calc.py").exists()
                return ReviewResult(approved=False, summary="assertion weakened", findings=[Finding(
                    severity="blocking", location="tests/test_calc.py", description="Removes the required sum assertion",
                    evidence="proposed assert True", resolution="Preserve the existing acceptance test and fix implementation")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = MisguidedAgent()
    result = await engine(project, agent).start(str(requirements))
    assert result["data"]["outcome"] == "DELIVERED" and agent.attempts == 2
    assert git(project, "show", result["data"]["delivery_commit"] + ":tests/test_calc.py").strip() == test_content.strip()
    assert Store(project).get(result["id"], "rounds/T1")["round"] == 2


@pytest.mark.asyncio
async def test_prototype_waits_for_versioned_human_approval_and_external_reply(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Addition with a frontend")

    class FrontendAgent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "prototype":
                self.calls.append((stage, fresh))
                return DocumentResult(summary="prototype", document="Static counter", sources=[SourceEvidence(url="https://fixture.invalid/DESIGN.md", claim="style")],
                                      files=[Change(path="design/prototypes/index.html", content="<h1>Counter</h1>"),
                                             Change(path="DESIGN.md", content="Simple monochrome counter")])
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "clarify":
                result.frontend = True
            if stage == "plan":
                result.preview = PreviewSpec(command=Command(argv=[sys.executable, "-m", "http.server", "{port}"]))
            return result

    async def candidates(*args):
        return [{"url": "https://fixture.invalid/DESIGN.md", "revision": "fixture"}]

    async def browser(*args):
        return [{"status": 200, "viewport": 390}]

    async def research(*args):
        return [{"url": "https://fixture.invalid/DESIGN.md", "excerpt": "Simple monochrome counter"}]

    monkeypatch.setattr("auto_agents_2.engine.DesignCatalog.candidates", candidates)
    monkeypatch.setattr("auto_agents_2.engine.ResearchService.verify", research)
    monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", browser)
    monkeypatch.setattr("auto_agents_2.engine.inspect_application", browser)
    agent = FrontendAgent()
    workflow = engine(project, agent)
    workflow._ensure_browser = browser
    question_visible = asyncio.Event()

    async def unanswered_tty(question):
        question_visible.set()
        await asyncio.Event().wait()

    workflow.question_handler = unanswered_tty
    running = asyncio.create_task(workflow.start(str(requirements)))
    try:
        await asyncio.wait_for(question_visible.wait(), 10)
        rid = workflow.store.current()["id"]
        question = workflow.store.pending(rid)[0]
        assert question["key"] == "prototype-approval" and question["artifact_hash"]
        assert "design" not in [stage for stage, _ in agent.calls]
        from auto_agents_2.errors import AA2Error
        with pytest.raises(AA2Error, match="版本"):
            workflow.store.answer(question["id"], "approve", "wrong-digest")
        workflow.store.answer(question["id"], "approve", question["artifact_hash"])
        result = await asyncio.wait_for(running, 15)
        assert result["status"] == "SUCCEEDED"
        assert workflow.store.get(rid, "prototype_approval")["hash"] == question["artifact_hash"]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_iteration_only_plans_and_changes_the_gap(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add numbers")
    first = await engine(project, CodingAgent()).start(str(requirements))
    previous_code = git(project, "show", first["data"]["delivery_commit"] + ":calc.py")
    requirements.write_text("Keep addition; also implement doubling")

    class IncrementalAgent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "clarify":
                result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
                result.requirements.append(Requirement(id="R2", description="double", acceptance=["double(3) == 6"]))
                result.coverage.append(Coverage(requirement_id="R2", status="changed", rationale="double.py is missing"))
                return result
            if stage == "plan":
                assert context["specification"]["coverage"][0]["status"] == "satisfied"
                check = Command(argv=[sys.executable, "-m", "pytest", "-q", "tests"], expected_tests=2)
                return PlanResult(summary="delta", document="Only implement double", tasks=[TaskSpec(
                    id="T2", title="double", description="Double an input", requirement_ids=["R2"], allowed_paths=["double.py", "tests/test_double.py"],
                    acceptance=["double(3)==6"], checks=[check])], final_checks=[check])
            if stage == "implement":
                assert context["task"]["requirement_ids"] == ["R2"]
                return ImplementationResult(summary="double", changes=[Change(path="double.py", content="def double(x): return x * 2\n"),
                     Change(path="tests/test_double.py", content="from double import double\ndef test_double(): assert double(3)==6\n")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    result = await engine(project, IncrementalAgent()).start(str(requirements))
    assert result["data"]["outcome"] == "DELIVERED"
    assert git(project, "show", result["data"]["delivery_commit"] + ":calc.py") == previous_code
    assert [t["requirement_ids"] for t in Store(project).get(result["id"], "plan")["tasks"]] == [["R2"]]


@pytest.mark.asyncio
async def test_replan_renaming_preserves_budget_and_reclaims_previous_worker(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Addition")

    class ReplanningAgent(CodingAgent):
        replanned = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement" and not self.replanned:
                self.replanned = True
                return ImplementationResult(summary="Task needs a clarified contract", replan_stage="plan", replan_reason="Make the public import explicit")
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "plan" and self.replanned:
                result.tasks[0].id = "T1_REVISED"
                result.tasks[0].origin_id = "T1"
                assert context["feedback"]["recovery_refs"]
                assert not list((project / ".aa2/worktrees" / run_id).glob("T1-p*"))
            return result

    workflow = engine(project, ReplanningAgent())
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED"
    assert workflow.store.get(result["id"], "rounds/T1")["round"] == 2
    assert workflow.store.get(result["id"], "rounds/T1_REVISED") is None
