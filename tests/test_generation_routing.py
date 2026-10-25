import pytest
from authority_review_fixtures import authority_review
from test_decisions import ApprovingAgent
from test_patch_repair import setup
from test_plan_lineage import pending_run
from test_workflow import CodingAgent, engine

from auto_agents_2.engine import PROTOTYPE_BROWSER_VERSION, Replan
from auto_agents_2.models import Change, DocumentResult, IntakeResult
from auto_agents_2.util import digest
from auto_agents_2.workspace import git


@pytest.mark.parametrize("status", ["unknown", "satisfied"])
async def test_missing_internal_coverage_evidence_returns_to_clarification_without_user_input(tmp_path, status):
    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            result = await super().invoke(stage, context, *args, **kwargs)
            if stage == "clarify":
                result.coverage[0].status = status
                result.coverage[0].evidence_paths = ["calc.py"]
                result.verification = []
            return result
    workflow, rid = pending_run(tmp_path, Agent())
    (workflow.project / "calc.py").write_text("def add(a, b): return a + b\n")
    git(workflow.project, "add", "calc.py")
    git(workflow.project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "existing behaviour")
    with pytest.raises(Replan) as caught:
        await workflow._execute(rid)
    assert caught.value.stage == "clarify"
    assert "verification" in workflow.store.get(rid, "clarify_context")["controller_feedback"] or status == "unknown"
    assert not workflow.store.pending(rid) and not workflow.store.get(rid, "plan")
    workflow._controller_replan(rid, caught.value)
    await workflow._resume_replan(rid)
    assert workflow.store.get(rid, "clarify") is None
    assert workflow.store.get(rid, "recovery/gate-history/0")["checkpoints"]["clarify"]["coverage"][0]["status"] == status


async def test_old_missing_evidence_question_is_retired_only_with_an_effective_permit(tmp_path):
    workflow, rid = pending_run(tmp_path, ApprovingAgent())
    assessment = {"coverage": "unknown"}
    old = workflow.store.request(rid, "unknown-coverage", "Provide code locations", digest(assessment))
    with pytest.raises(Replan) as caught:
        workflow._coverage_replan(rid, {}, assessment, "unknown-coverage", digest(assessment), "Inspect current code")
    assert not workflow.store.question(old["id"])["superseded"]
    workflow._controller_replan(rid, caught.value)
    await workflow._resume_replan(rid)
    assert workflow.store.question(old["id"])["superseded"]
    assert workflow.store.question(old["id"])["answer"] is None


async def test_old_evidence_answer_remains_input_not_an_action_authorization(tmp_path):
    workflow, rid = pending_run(tmp_path, ApprovingAgent())
    assessment = {"coverage": "unknown"}
    old = workflow.store.request(rid, "unknown-coverage", "Provide code locations", digest(assessment))
    workflow.store.answer(old["id"], "Inspect src/existing.py and preserve its behaviour")
    with pytest.raises(Replan) as caught:
        workflow._coverage_replan(rid, {}, assessment, "unknown-coverage", digest(assessment), "Inspect current code")
    assert not caught.value.supersede_keys
    assert workflow.store.get(rid, "clarify_context")["additional_evidence"] == workflow.store.question(old["id"])["answer"]


async def test_empty_acceptance_is_a_proposal_correction_not_an_internal_error(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "clarify":
                return IntakeResult(summary="incomplete", document="Missing acceptance")
            return await super().invoke(stage, *args, **kwargs)
    workflow, rid = pending_run(tmp_path, Agent())
    with pytest.raises(Replan, match="acceptance criteria"):
        await workflow._execute(rid)
    assert workflow.store.get(rid, "capture")
    assert "no acceptance criteria" in workflow.store.get(rid, "clarify_context")["controller_feedback"]


async def test_clarification_batch_exhaustion_preserves_feedback_for_recovery(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "coverage_review":
                return authority_review(args[0], approved=False, summary="Preserve the requested acceptance criteria")
            return await super().invoke(stage, *args, **kwargs)
    workflow, rid = pending_run(tmp_path, Agent())
    with pytest.raises(Replan, match="batch ended"):
        await workflow._execute(rid)
    assert workflow.store.get(rid, "clarify_context")["review_feedback"]["approved"] is False
    assert not workflow.store.pending(rid)


async def test_readme_rejection_retries_documentation_without_replanning_accepted_implementation(tmp_path):
    class Agent(CodingAgent):
        documents = 0
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "readme":
                self.documents += 1
                if self.documents == 1:
                    return DocumentResult(summary="invalid", document="documentation", files=[
                        Change(path="docs/aa2/design.md", content="unauthorized rewrite")])
            return await super().invoke(stage, context, *args, **kwargs)
    agent = Agent()
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, agent)
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED" and agent.documents == 2
    assert [s for s, _ in agent.calls].count("plan") == 1
    assert [s for s, _ in agent.calls].count("implement") == 1
    assert workflow.store.get(result["id"], "rounds/T1")["round"] == 1
    archived = workflow.store.get(result["id"], "recovery/gate-history/0")["checkpoints"]
    assert "candidate/readme" in archived and "plan" not in archived and "develop/done" not in archived
    assert "unauthorized rewrite" not in (project / "docs/aa2/design.md").read_text()


async def test_out_of_scope_prototype_is_a_rejected_proposal_not_a_human_state_conflict(tmp_path):
    workflow, agent, rid = setup(tmp_path, [])
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "baseline")
    baseline = workflow.workspaces.checkpoint(tmp_path, f"refs/aa2/{rid}/base", "fixture")
    specification = IntakeResult(summary="fixture", document="# UI", frontend=True)
    assessment = specification.model_dump()
    proposal = DocumentResult(summary="bad scope", document="# Prototype", files=[Change(path="calc.py", content="not a prototype")])
    workflow.store.checkpoints(rid, {"baseline": baseline, "prototype/pending": {
        "identity": digest([assessment, baseline, None]), "document": proposal.model_dump(),
        "design_candidates": [], "question_refresh_version": PROTOTYPE_BROWSER_VERSION}})
    with pytest.raises(Replan, match="Out-of-scope") as caught:
        await workflow._prepare_prototype(rid, tmp_path, specification, assessment, {}, "fixture")
    assert caught.value.stage == "prototype" and not (tmp_path / "calc.py").exists()
    assert not agent.calls
