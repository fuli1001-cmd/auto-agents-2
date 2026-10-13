import asyncio
import re

import httpx
import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.engine import Replan
from auto_agents_2.errors import AA2Error
from auto_agents_2.models import (
    BrowserAction,
    Change,
    DocumentResult,
    IntakeResult,
    ReviewResult,
    SourceEvidence,
)
from auto_agents_2.util import digest
from auto_agents_2.workspace import git


def prototype_fixture(tmp_path, monkeypatch, *, empty=False):
    document = DocumentResult(summary="prototype", document="Candidate",
        questions=["R008 要求先取得人工批准：请审阅原型，是否批准？"],
        sources=[SourceEvidence(url="https://fixture.invalid/DESIGN.md", claim="style")],
        files=[] if empty else [Change(path="design/prototypes/task/review.html", content="<h1>Review</h1>"),
                              Change(path="design/prototypes/task/index.html", content="<h1>Preview</h1>")])
    calls = []

    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            calls.append(stage)
            if stage == "prototype":
                assert "controller_feedback" in context
                return document.model_copy(update={"questions": [], "files": [
                    Change(path="design/prototypes/index.html", content="<h1>Generated</h1>")]})
            assert stage == "review" and fresh
            assert (workspace / "design/prototypes").is_dir()
            return ReviewResult(approved=True, summary="verified")

    workflow = engine(tmp_path, Agent())
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "baseline")
    rid = workflow.store.admit("prototype", {})["id"]
    workflow.store.checkpoint(rid, "baseline", git(tmp_path, "rev-parse", "HEAD"))
    artifact = workflow.artifacts.put(document.model_dump(), "prototype")
    workflow.store.event(rid, "artifact.created", {"operation": "prototype", "artifact": artifact})
    legacy = workflow.store.request(rid, "prototype", document.questions[0], digest(document.model_dump()))

    async def candidates(*args):
        return [{"url": "https://fixture.invalid/DESIGN.md"}]

    async def verify(*args):
        return [{"url": "https://fixture.invalid/DESIGN.md", "excerpt": "style"}]

    async def browser(*args):
        calls.append("browser")
        return [{"status": 200}]

    async def installed(*args):
        pass

    monkeypatch.setattr("auto_agents_2.engine.DesignCatalog.candidates", candidates)
    monkeypatch.setattr("auto_agents_2.engine.ResearchService.verify", verify)
    monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", browser)
    workflow._ensure_browser = installed
    return workflow, rid, document, legacy, calls


@pytest.mark.parametrize("empty", [False, True])
async def test_legacy_premature_question_restores_files_checks_and_live_preview(tmp_path, monkeypatch, empty):
    workflow, rid, document, legacy, calls = prototype_fixture(tmp_path, monkeypatch, empty=empty)

    async def approve(question):
        assert question["key"] == "prototype-approval"
        assert calls[-2:] == ["browser", "review"]
        assert "R008" not in question["prompt"]
        urls = re.findall(r"http://[^\s]+", question["prompt"])
        assert len(urls) == (1 if empty else 2)
        async with httpx.AsyncClient() as client:
            for url in urls:
                assert (await client.get(url)).status_code == 200
        return "1"

    workflow.question_handler = approve
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    returned, _ = await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert not returned.questions
    assert calls.count("prototype") == int(empty)
    assert workflow.store.get(rid, "prototype_approval")["hash"] == workflow._prototype_hash(tmp_path)
    with workflow.store.connect() as db:
        saved = db.execute("SELECT prompt,answer,superseded FROM questions WHERE id=?", (legacy["id"],)).fetchone()
        assert saved[:] == (document.questions[0], None, 1)


async def test_rejection_collects_multiline_feedback_without_approval(tmp_path, monkeypatch):
    workflow, rid, _, _, _ = prototype_fixture(tmp_path, monkeypatch)
    asked = []

    async def reject(question):
        asked.append(question["key"])
        return "2" if question["key"] == "prototype-approval" else "缩小间距\n按钮右对齐"

    workflow.question_handler = reject
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    with pytest.raises(Replan, match="缩小间距\n按钮右对齐"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert asked == ["prototype-approval", "prototype-feedback"]
    assert workflow.store.get(rid, "prototype_approval") is None


async def test_manual_changes_during_approval_cannot_be_approved(tmp_path, monkeypatch):
    workflow, rid, _, _, _ = prototype_fixture(tmp_path, monkeypatch)

    async def alter(question):
        (tmp_path / "design/prototypes/task/index.html").write_text("user edit")
        return "1"

    workflow.question_handler = alter
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    with pytest.raises(AA2Error, match="发生变化"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert workflow.store.get(rid, "prototype_approval") is None
    assert (tmp_path / "design/prototypes/task/index.html").read_text() == "user edit"


async def test_interrupted_application_recovers_prepared_candidate_without_overwriting_user_edit(tmp_path, monkeypatch):
    workflow, rid, _, _, _ = prototype_fixture(tmp_path, monkeypatch)
    from auto_agents_2.workspace import apply_prepared

    def interrupted(work, prepared):
        apply_prepared(work, prepared[:1])
        raise asyncio.CancelledError()

    monkeypatch.setattr("auto_agents_2.engine.apply_prepared", interrupted)
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    with pytest.raises(asyncio.CancelledError):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    user_file = tmp_path / "design/prototypes/task/review.html"
    user_file.write_text("user edits after interrupt")
    monkeypatch.setattr("auto_agents_2.engine.apply_prepared", apply_prepared)
    with pytest.raises(AA2Error, match="File changed"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert user_file.read_text() == "user edits after interrupt"
    assert workflow.store.get(rid, "prototype_approval") is None


@pytest.mark.parametrize("genuine_question", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_resume_refreshes_legacy_internal_question_with_actual_browser_report(
        tmp_path, monkeypatch, genuine_question, interrupted):
    workflow, rid, original, _, calls = prototype_fixture(tmp_path, monkeypatch)
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    page = tmp_path / "design/prototypes/task/index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<h1>Preview</h1>")
    document = original.model_copy(update={"files": [], "questions": ["界面语言？", "请提供控制器侧浏览器交互检查的实际报告"],
        "browser_actions": [BrowserAction(route="/index.html", action="expect_visible", selector="h1")]})
    identity = digest([spec.model_dump(), workflow.store.get(rid, "baseline"), None])
    workflow.store.checkpoint(rid, "prototype/pending", {"identity": identity,
        "document": document.model_dump(), "design_candidates": []})
    answered = workflow.store.request(rid, "prototype-clarification/0", document.questions[0], digest(document.model_dump()))
    workflow.store.answer(answered["id"], "中文")
    old = workflow.store.request(rid, "prototype-clarification/1", document.questions[1], digest(document.model_dump()))

    class Agent:
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "prototype":
                if fresh:
                    assert context["prototype_answer"] == [{"question": "界面语言？", "answer": "中文"}]
                else:
                    assert context["prototype_answer"] == [{"question": "是否需要导出？", "answer": "需要"}]
                report = context["controller_browser_report"]
                assert report["results"] == [{"status": 200}]
                assert report["source_hash"] == workflow._prototype_hash(tmp_path)
                return document.model_copy(update={"questions": ["是否需要导出？"] if genuine_question and fresh else []})
            return ReviewResult(approved=True, summary="verified")

    workflow.agent = Agent()

    async def approve(question):
        if question["key"].startswith("prototype-clarification/"):
            assert genuine_question and question["prompt"] == "是否需要导出？"
            return "需要"
        assert question["key"] == "prototype-approval"
        return "1"

    workflow.question_handler = approve
    if interrupted:
        supersede = workflow.store.supersede_questions

        def stop_before_superseding(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(workflow.store, "supersede_questions", stop_before_superseding)
        with pytest.raises(asyncio.CancelledError):
            await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
        monkeypatch.setattr(workflow.store, "supersede_questions", supersede)
    await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert page.read_text() == "<h1>Preview</h1>"
    assert calls.count("browser") == 2
    with workflow.store.connect() as db:
        assert db.execute("SELECT answer,superseded FROM questions WHERE id=?", (old["id"],)).fetchone()[:] == (None, 1)
        assert db.execute("SELECT answer FROM questions WHERE id=?", (answered["id"],)).fetchone()[0] == "中文"
    assert workflow.artifacts.json(workflow.store.get(rid, "prototype_browser_report"))["results"]


async def test_browser_failure_is_archived_and_returned_to_prototype_replan(tmp_path, monkeypatch):
    from auto_agents_2.errors import PrototypeBrowserError
    workflow, rid, _, _, _ = prototype_fixture(tmp_path, monkeypatch)
    rows = [{"route": "/task/index.html", "viewport": 1440, "status": 200, "actions": [],
             "errors": ["Missing button"], "failed_action": {"selector": "#add"}}]

    async def failed(*args):
        raise PrototypeBrowserError("Missing button", rows)

    monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", failed)
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    with pytest.raises(Replan, match="Missing button"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    report = workflow.artifacts.json(workflow.store.get(rid, "prototype_browser_report"))
    assert report["results"] == rows and report["error"] == "Missing button"
    assert "visuals" not in workflow.store.get(rid, "prototype/pending")
    assert workflow.store.get(rid, "prototype_approval") is None


async def test_resume_rechecks_old_screenshot_only_evidence(tmp_path, monkeypatch):
    workflow, rid, _, _, calls = prototype_fixture(tmp_path, monkeypatch)
    spec = IntakeResult(summary="ui", document="UI", frontend=True)

    async def interrupt(question):
        raise asyncio.CancelledError()

    workflow.question_handler = interrupt
    with pytest.raises(asyncio.CancelledError):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    pending = workflow.store.get(rid, "prototype/pending")
    pending.pop("browser_version")
    workflow.store.checkpoint(rid, "prototype/pending", pending)
    with pytest.raises(asyncio.CancelledError):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert calls.count("browser") == 2 and calls.count("review") == 2


async def test_resume_saved_candidate_with_multiple_replacements_without_regenerating(tmp_path, monkeypatch):
    workflow, rid, original, _, calls = prototype_fixture(tmp_path, monkeypatch)
    spec = IntakeResult(summary="ui", document="UI", frontend=True)
    directory = tmp_path / "design/prototypes/task"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<h1>Preview</h1>")
    (directory / "prototype.js").write_text("const a = 1; const b = 2;")
    document = original.model_copy(update={"questions": [], "files": [
        Change(path="design/prototypes/task/prototype.js", operation="replace", old_text="a = 1", content="a = 3"),
        Change(path="design/prototypes/task/prototype.js", operation="replace", old_text="b = 2", content="b = 4")]})
    workflow.store.checkpoint(rid, "prototype/pending", {
        "identity": digest([spec.model_dump(), workflow.store.get(rid, "baseline"), None]),
        "document": document.model_dump(), "design_candidates": []})

    async def approve(question):
        assert question["key"] == "prototype-approval"
        return "1"

    workflow.question_handler = approve
    await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert "prototype" not in calls
    assert calls == ["browser", "review"]
    assert (directory / "prototype.js").read_text() == "const a = 3; const b = 4;"
    pending = workflow.store.get(rid, "prototype/pending")
    assert len(pending["document"]["files"]) == 2 and len(pending["prepared"]) == 1
