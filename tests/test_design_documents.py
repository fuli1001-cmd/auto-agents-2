import asyncio

import pytest
from authority_review_fixtures import authority_review, seed_original
from evidence_test_fixtures import seed_requirements
from test_patch_repair import Corrector, replacement, setup
from test_plan_lineage import ReachedDevelopment, pending_run
from test_recovery_contracts import plan as source_plan
from test_workflow import CodingAgent, engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.design_documents import DesignDocuments
from auto_agents_2.engine import STAGES, Replan
from auto_agents_2.errors import AA2Error, PolicyViolation
from auto_agents_2.evidence_admission import admission_context
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, DocumentResult, Finding
from auto_agents_2.planning import validate_plan
from auto_agents_2.recovery import failure_summary
from auto_agents_2.workspace import apply_prepared, git


def proposal():
    return DocumentResult(summary="increment", document="# Separate proposal summary", files=[
        replacement("docs/aa2/design.md", "# Existing  architecture", "# Existing architecture\nSee design-feature.md"),
        Change(path="docs/aa2/design-feature.md", content="# Feature\nPreserve the approved interface and open evidence issue.")])


def baseline(work):
    (work / "docs/aa2").mkdir(parents=True, exist_ok=True)
    (work / "docs/aa2/design.md").write_text("# Existing architecture\nPreserve previous obligations.\n")


class DesignAgent(Corrector):
    def __init__(self):
        super().__init__([{"old_text": "# Existing architecture",
                          "content": "# Existing architecture\nSee design-feature.md"}])
        self.reviews = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == "design" and not context.get("patch_repair"):
            return proposal()
        if context.get("review_kind") == "design":
            assert fresh
            assert "Preserve previous obligations" in context["design"]["document"]
            assert "See design-feature.md" in (workspace / "docs/aa2/design.md").read_text()
            assert (workspace / "docs/aa2/design-feature.md").is_file()
            assert "docs/aa2/design-feature.md" in context["project"]["inventory"]
            self.reviews.append(operation)
        if stage == "plan":
            assert self.reviews == ["design/review"]
            assert "docs/aa2/design-feature.md" in context["project"]["inventory"]
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


async def test_design_files_corrected_applied_reviewed_and_visible_before_planning(tmp_path):
    agent = DesignAgent()
    workflow, rid = pending_run(tmp_path, agent)
    baseline(workflow.project)
    git(workflow.project, "add", "docs")
    git(workflow.project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "architecture")
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    pending = workflow.store.get(rid, "candidate/design")
    assert pending["review"]["approved"]
    assert len(agent.repairs) == len(agent.reviews) == 1
    assert pending["original"] == proposal().model_dump()
    assert workflow.store.get(rid, "design")["document"].startswith("# Existing architecture")
    # Accepted design receipts verify only their document images once later stages run.
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    assert len(agent.reviews) == 1


@pytest.mark.parametrize("drift", [None, "docs/aa2/design.md", "user.txt"])
async def test_partial_application_resumes_original_images_and_preserves_user_changes(tmp_path, monkeypatch, drift):
    agent = DesignAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("fixture", {})["id"]
    seed_original(workflow, rid)
    baseline(tmp_path)
    service = DesignDocuments(workflow)

    def interrupt(work, prepared):
        apply_prepared(work, prepared[:1])
        raise asyncio.CancelledError

    monkeypatch.setattr("auto_agents_2.design_documents.apply_prepared", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await service.prepare(rid, tmp_path, proposal(), {})
    assert not (tmp_path / "docs/aa2/design-feature.md").exists()
    if drift:
        (tmp_path / drift).write_text("user edit")
    monkeypatch.setattr("auto_agents_2.design_documents.apply_prepared", apply_prepared)
    restarted = DesignDocuments(engine(tmp_path, agent))
    if drift:
        with pytest.raises(StateConflict):
            await restarted.prepare(rid, tmp_path, proposal(), {})
        assert (tmp_path / drift).read_text() == "user edit"
        assert not agent.reviews
    else:
        result, review = await restarted.prepare(rid, tmp_path, proposal(), {})
        assert review.approved and "See design-feature.md" in result.document
        assert len(agent.reviews) == 1
    assert len(agent.repairs) == 1


async def test_saved_design_review_reused_after_interruption(tmp_path, monkeypatch):
    workflow, agent, rid = setup(tmp_path, [])
    seed_original(workflow, rid)
    document = DocumentResult(summary="design", document="# Architecture")
    original = workflow.store.checkpoint

    def interrupt(rid, key, value):
        original(rid, key, value)
        if key == "candidate/design" and "review" in value:
            raise asyncio.CancelledError

    monkeypatch.setattr(workflow.store, "checkpoint", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await DesignDocuments(workflow).prepare(rid, tmp_path, document, {})
    calls = len(agent.calls)
    result, review = await DesignDocuments(engine(tmp_path, agent)).prepare(rid, tmp_path, document, {})
    assert result.document == "# Architecture" and review.approved
    assert len(agent.calls) == calls


async def test_saved_proposal_does_not_overwrite_edits_before_preparation(tmp_path, monkeypatch):
    workflow, rid = pending_run(tmp_path, CodingAgent())
    original = DesignDocuments.prepare

    async def interrupt(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(DesignDocuments, "prepare", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow._execute(rid)
    pending = workflow.store.get(rid, "candidate/design")
    assert pending["proposal_source_hash"] and "prepared" not in pending
    work = workflow.store.root / "worktrees" / rid / "integration"
    (work / "docs/aa2/design.md").write_text("user architecture edit")
    monkeypatch.setattr(DesignDocuments, "prepare", original)
    with pytest.raises(StateConflict, match="after proposal generation"):
        await workflow._execute(rid)
    assert (work / "docs/aa2/design.md").read_text() == "user architecture edit"
    assert workflow.store.get(rid, "candidate/design") == pending


@pytest.mark.parametrize("path", ["src/app.py", "docs/aa2/plan.md", "docs/aa2/environments/lock.md",
    "docs/requirements/spec.md", "DESIGN.md", "design/prototypes/index.html", "docs/aa2/design/run.py"])
async def test_design_cannot_write_other_gate_or_implementation_files(tmp_path, path):
    workflow, agent, rid = setup(tmp_path, [])
    document = DocumentResult(summary="invalid", document="# Architecture", files=[Change(path=path, content="unauthorized")])
    with pytest.raises(PolicyViolation):
        await DesignDocuments(workflow).prepare(rid, tmp_path, document, {})
    assert not agent.calls and not (tmp_path / path).exists()
    assert not (tmp_path / "docs/aa2/design.md").exists()


async def test_design_review_rejection_blocks_planning_and_retains_candidate(tmp_path):
    class Rejecting(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if context.get("review_kind") == "design":
                return authority_review(context, approved=False, summary="Missing interface", findings=[Finding(
                    severity="blocking", location="docs/aa2/design.md", description="Undefined data invariant",
                    evidence="No invariant", resolution="Specify the invariant before planning")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    workflow, rid = pending_run(tmp_path, Rejecting())
    with pytest.raises(Replan, match="Specify the invariant") as exc:
        await workflow._execute(rid)
    assert exc.value.stage == "design"
    assert workflow.store.get(rid, "plan") is None
    assert not workflow.store.get(rid, "design")
    assert workflow.store.get(rid, "candidate/design")["review"]["approved"] is False


async def test_review_cannot_accept_changed_design_inputs(tmp_path):
    workflow, _, rid = setup(tmp_path, [])
    seed_original(workflow, rid)
    original = workflow.agent.invoke

    async def drift(*args, **kwargs):
        result = await original(*args, **kwargs)
        (tmp_path / "docs/aa2/design.md").write_text("user edit during review")
        return result

    workflow.agent.invoke = drift
    with pytest.raises(StateConflict):
        await DesignDocuments(workflow).prepare(rid, tmp_path, DocumentResult(summary="design", document="# Architecture"), {})
    assert "review" not in workflow.store.get(rid, "candidate/design")
    assert (tmp_path / "docs/aa2/design.md").read_text() == "user edit during review"


async def test_legacy_unapplied_design_returns_to_owning_gate_with_history(tmp_path):
    workflow, rid = pending_run(tmp_path, CodingAgent())
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    old_plan = workflow.store.get(rid, "plan")
    legacy = proposal().model_dump()
    workflow.store.checkpoints(rid, {"design": legacy, "candidate/design": {"document": legacy},
        "rounds/T1": {"round": 7}, "prototype_approval": {"status": "approved", "question_id": "fixture-human"}})
    with pytest.raises(Replan, match="Legacy design") as exc:
        await workflow._execute(rid)
    assert exc.value.stage == "design"
    workflow.store.prepare_replan(rid, "design", exc.value.reason)
    workflow.store.complete_replan(rid, STAGES[STAGES.index("design"):], [])
    archived = workflow.store.get(rid, "recovery/gate-history/0")["checkpoints"]
    assert archived["design"] == legacy and archived["plan"] == old_plan
    assert workflow.store.get(rid, "rounds/T1") == {"round": 7}
    assert workflow.store.get(rid, "prototype_approval")["question_id"] == "fixture-human"


@pytest.mark.parametrize("path", ["docs/aa2/design-feature.md", "docs/aa2", "./docs/aa2/design/*.md",
    "docs/requirements/spec.md", "design/prototypes/page.html", "DESIGN.md"])
async def test_planning_rejects_stage_owned_implementation_paths(tmp_path, path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("Fixture original path-protection requirements", {})["id"]
    seed_requirements(workflow, rid, source_plan())
    context = admission_context(lambda key: workflow.store.get(rid, key))
    plan = await CodingAgent().invoke("plan", context, None, tmp_path, rid, "fixture")
    plan.tasks[0].allowed_paths.append(path)
    with pytest.raises(AA2Error, match="stage-owned"):
        validate_plan(plan, {"R1"})
    plan.tasks[0].allowed_paths.remove(path)
    plan.tasks[0].acceptance.append("Verify existing architecture in " + path)
    validate_plan(plan, {"R1"})


def test_design_review_keeps_actual_design_and_proposal_rejections_are_visible(tmp_path):
    workflow, _, _ = setup(tmp_path, [])
    view = ContextPlanner(workflow.artifacts).prepare("review", {
        "review_kind": "design", "design": {"document": "required architecture", "files": []},
        "specification": {"requirements": []}}, tmp_path)
    assert view.payload["design"]["document"] == "required architecture"
    message = failure_summary([], candidate_error="Accepted contracts require a stage replan: docs/aa2/design-feature.md",
                              validation_error="Plan patch removes accepted obligations")
    assert message.startswith("候选补丁未应用：")
    assert "恢复提案未通过校验：Plan patch removes accepted obligations" in message
    assert "没有已执行" in message
