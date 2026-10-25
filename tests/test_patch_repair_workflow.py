import asyncio

import pytest
from authority_review_fixtures import authority_review, seed_original
from test_patch_repair import Corrector, replacement, setup
from test_workflow import engine

from auto_agents_2.engine import PROTOTYPE_BROWSER_VERSION
from auto_agents_2.models import DocumentResult, ImplementationResult, IntakeResult
from auto_agents_2.util import digest
from auto_agents_2.workspace import git


@pytest.mark.parametrize("approve", [True, False])
async def test_implementation_correction_still_requires_protected_test_review(tmp_path, approve):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests/test_calc.py"
    test.write_text("from calc import add\n\ndef test_sum():\n    assert add(2, 3) == 5\n")
    reviewed = []

    class Agent(Corrector):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement" and not context.get("patch_repair"):
                if reviewed and not approve:
                    raise asyncio.CancelledError
                return ImplementationResult(summary="addition", changes=[
                    replacement("calc.py", "return a - b", "return a + b"),
                    replacement("tests/test_calc.py", "assert add(2,3) == 5",
                                "assert add(2, 3) == 5\n    assert add(0, 0) == 0")])
            if context.get("review_kind") == "test_changes":
                candidate = context["candidate_changes"]
                assert candidate["changes"][1]["old_text"] == "assert add(2, 3) == 5"
                assert "assert add(0, 0)" not in (workspace / "tests/test_calc.py").read_text()
                assert "return a - b" in (workspace / "calc.py").read_text()
                reviewed.append(candidate)
                return authority_review(context, approved=approve, summary="fixture protected review")
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent([{"old_text": "assert add(2, 3) == 5", "content": "assert add(2, 3) == 5\n    assert add(0, 0) == 0"}])
    workflow = engine(tmp_path, agent)
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    if approve:
        result = await workflow.start(str(requirements))
        assert result["status"] == "SUCCEEDED"
        assert "assert add(0, 0)" in test.read_text()
    else:
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(str(requirements))
        assert "assert add(0, 0)" not in test.read_text()
        assert "return a - b" in (tmp_path / "calc.py").read_text()
    assert len(agent.repairs) == len(reviewed) == 1


async def test_readme_replacement_is_repaired_in_complete_workflow(tmp_path):
    (tmp_path / "README.md").write_text("Old documentation\n")

    class Agent(Corrector):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "readme" and not context.get("patch_repair"):
                return DocumentResult(summary="updated", document="# Done",
                    files=[replacement("README.md", "Old  documentation", "# Calculator\nRun pytest tests.")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent([{"old_text": "Old documentation", "content": "# Calculator\nRun pytest tests."}])
    workflow = engine(tmp_path, agent)
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED" and len(agent.repairs) == 1
    assert agent.repairs[0][0] == "readme"
    assert "# Calculator" in (tmp_path / "README.md").read_text()


async def test_legacy_prototype_candidate_repaired_before_browser_review_and_approval(tmp_path, monkeypatch):
    path = tmp_path / "design/prototypes/page.css"
    path.parent.mkdir(parents=True)
    path.write_text("width: minmax(0, 1fr);\n")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "minmax(0, 1fr);", "content": "1fr;"}])
    baseline = workflow.workspaces.checkpoint(tmp_path, f"refs/aa2/{rid}/base", "fixture")
    specification = IntakeResult(summary="fixture", document="# UI", frontend=True,
                                prototype_paths=["design/prototypes/page.css"])
    assessment = specification.model_dump()
    document = DocumentResult(summary="style", document="# Prototype",
        files=[replacement("design/prototypes/page.css", "minmax(0, 1fr)  ;", "1fr;")])
    workflow.store.checkpoints(rid, {"baseline": baseline, "prototype/pending": {
        "identity": digest([assessment, baseline, None]), "document": document.model_dump(),
        "design_candidates": [], "question_refresh_version": PROTOTYPE_BROWSER_VERSION}})
    gates = []

    async def browser(*args):
        assert path.read_text() == "width: 1fr;\n"
        gates.append("browser")
        return {"error": "", "results": []}

    async def approval(rid, work, proposed, review, visuals):
        assert review["approved"]
        assert proposed.files[0].old_text == "minmax(0, 1fr);"
        gates.append("human_approval")
        return {"status": "approved"}

    monkeypatch.setattr(workflow, "_prototype_browser_report", browser)
    monkeypatch.setattr(workflow, "_record_prototype_approval", approval)
    repaired, _ = await workflow._prepare_prototype(rid, tmp_path, specification, assessment, {}, "fixture")
    assert gates == ["browser", "human_approval"]
    assert ("review", True) in agent.calls
    assert len(agent.repairs) == 1 and repaired.files[0].old_text == "minmax(0, 1fr);"
    assert workflow.store.get(rid, "prototype/pending")["prepared"]
    # Resuming the applied patch uses its postimage, not the old anchor/model again.
    await workflow._prepare_prototype(rid, tmp_path, specification, assessment, {}, "fixture")
    assert len(agent.repairs) == 1


async def test_delivery_repair_uses_corrected_proposal_for_independent_review(tmp_path, monkeypatch):
    (tmp_path / "app.txt").write_text("old")
    reviewed = []

    class Agent(Corrector):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement" and not context.get("patch_repair"):
                return ImplementationResult(summary="delivery repair", changes=[replacement("app.txt", "old ", "new")])
            if context.get("review_kind") == "test_changes":
                assert context["candidate_changes"]["changes"][0]["old_text"] == "old"
                assert (workspace / "app.txt").read_text() == "old"
                reviewed.append(operation)
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent([{"old_text": "old", "content": "new"}])
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("fixture", workflow.config.load().model_dump(mode="json"))["id"]
    seed_original(workflow, rid)
    workflow.workspaces.checkpoint(tmp_path, f"refs/aa2/{rid}/base", "fixture")

    async def repair(*args, **kwargs):
        return "repair"

    monkeypatch.setattr(workflow.recovery, "delivery_decision", repair)
    await workflow._delivery_repair(rid, tmp_path, {"failure": "fixture"}, ["app.txt"])
    assert (tmp_path / "app.txt").read_text() == "new"
    assert len(reviewed) == len(agent.repairs) == 1
    pending = workflow.store.get(rid, "candidate/delivery-repair")
    assert pending["proposal"]["changes"][0]["old_text"] == "old"
    assert pending["commit"] == git(tmp_path, "rev-parse", "HEAD")
