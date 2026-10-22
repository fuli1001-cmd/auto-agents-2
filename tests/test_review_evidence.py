import asyncio
import copy
import json

import pytest
from test_plan_lineage import ReachedDevelopment, pending_run
from test_workflow import CodingAgent, engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.design_documents import DesignDocuments, DesignQuestions
from auto_agents_2.engine import STAGES
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, DocumentResult, UserQuestion
from auto_agents_2.source_history import SourceHistory
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash


def commit(work, title="fixture"):
    git(work, "add", "--all")
    return git(work, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", title)


async def reviewed(workflow, rid, work):
    result = DocumentResult(summary="architecture", document="# Architecture\nSee design-pagination.md", files=[
        Change(path="docs/aa2/design-pagination.md", content="# Pagination\nKeep the original acceptance obligations.\n")])
    await DesignDocuments(workflow).prepare(rid, work, result, {})
    workflow.store.checkpoint(rid, "design", result.model_dump())
    return result


async def test_architecture_receipt_binds_full_document_set_and_raw_review(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("architecture", {})["id"]
    await reviewed(workflow, rid, tmp_path)
    receipt = DesignDocuments(workflow).context(rid, tmp_path)
    assert receipt["status"] == "approved"
    assert set(receipt["documents"]) == {"docs/aa2/design.md", "docs/aa2/design-pagination.md"}
    assert receipt["independent_review"] == workflow.artifacts.json(receipt["review_artifact"])
    for path, binding in receipt["documents"].items():
        assert binding["hash"] == digest((tmp_path / path).read_bytes())
    view = ContextPlanner(workflow.artifacts).prepare("review", {
        "review_kind": "implementation", "design_review": receipt}, tmp_path)
    assert view.payload["design_review"]["status"] == "approved"
    (tmp_path / "docs/aa2/design-pagination.md").write_text("Changed companion")
    assert DesignDocuments(workflow).context(rid, tmp_path)["status"] == "unverified"
    with pytest.raises(StateConflict, match="Architecture documents"):
        workflow._task_design_context(rid, tmp_path)
    with pytest.raises(StateConflict):
        DesignDocuments(workflow).verify(tmp_path, workflow.store.get(rid, "candidate/design"))


async def test_saved_verdict_cannot_advance_design_when_raw_review_is_lost(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("lost raw review", {})["id"]
    document = await reviewed(workflow, rid, tmp_path)
    pending = workflow.store.get(rid, "candidate/design")
    ref = pending["review_artifact"]
    (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).unlink()
    with pytest.raises(StateConflict, match="review artifact"):
        await DesignDocuments(workflow).prepare(rid, tmp_path, document, {})
    assert workflow.store.get(rid, "candidate/design") == pending


@pytest.mark.parametrize("damage", [None, "bytes", "verdict", "documents", "specification"])
async def test_archived_legacy_decision_requires_actual_matching_evidence(tmp_path, damage):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("legacy receipt", {})["id"]
    await reviewed(workflow, rid, tmp_path)
    original = workflow.store.get(rid, "candidate/design")
    ref = original["review_artifact"]
    legacy = {k: v for k, v in original.items() if k not in {"review_documents", "review_specification", "review_artifact"}}
    workflow.store.checkpoint(rid, "candidate/design", legacy)
    workflow.store.prepare_replan(rid, "design", "Provide the original independent decision")
    workflow.store.complete_replan(rid, STAGES[STAGES.index("design"):], [])
    if damage == "bytes":
        (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("invalid artifact")
    elif damage == "verdict":
        history = workflow.store.get(rid, "recovery/gate-history/0")
        history["checkpoints"]["candidate/design"]["review"]["summary"] = "Invented decision"
        workflow.store.checkpoint(rid, "recovery/gate-history/0", history)
    elif damage == "documents":
        (tmp_path / "docs/aa2/design-pagination.md").write_text("User edit")
    elif damage == "specification":
        workflow.store.checkpoint(rid, "clarify", {"document": "Changed requirements"})
    evidence = DesignDocuments(workflow).evidence(rid, tmp_path)
    assert bool(evidence["records"]) == (damage in {None, "specification"})
    if evidence["records"]:
        assert evidence["records"][0]["origin"] == "recovery/gate-history/0"
        assert evidence["records"][0]["review_artifact"] == ref
    # A recovered historical decision is inspectable evidence, not an automatic
    # approval of a new specification or a replacement for the current design gate.
    assert DesignDocuments(workflow).context(rid, tmp_path)["status"] == "unverified"


def history_fixture(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("history", {})["id"]
    (tmp_path / "frontend/src/test").mkdir(parents=True)
    test = tmp_path / "frontend/src/App.test.tsx"
    setup = tmp_path / "frontend/src/test/setup.ts"
    test.write_text("click();\nexpect(button).toHaveFocus();\n")
    setup.write_bytes(b"setup();\r\n")
    (tmp_path / "frontend/src/format_test.py").write_text("# Protected historical test outside a tests directory\n")
    (tmp_path / ".env").write_text("EXAMPLE_SECRET_PLACEHOLDER=DO_NOT_ADMIT_THIS_FIXTURE\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/test_config.py").write_text("CUSTOM_PROTECTED_PATH_PLACEHOLDER")
    (tmp_path / "frontend/src/test/link.ts").symlink_to(tmp_path / ".env")
    commit(tmp_path, "baseline")
    base = git(tmp_path, "rev-parse", "HEAD")
    workflow.store.checkpoint(rid, "baseline", base)
    test.write_text("await act(async () => click());\nexpect(button).toHaveFocus();\n")
    commit(tmp_path, "retained candidate")
    ref = f"refs/aa2/{rid}/replans/0/T007"
    git(tmp_path, "update-ref", ref, "HEAD")
    workflow.store.checkpoint(rid, "replan_feedback", {"recovery_refs": [ref]})
    test.write_text("await act(async () => click());\nexpect(button).toHaveFocus();\n// current candidate\n")
    config = workflow.config.load().model_dump(mode="json")
    config["sensitive_paths"] = ["private/**"]
    write_json(workflow.config.path, config)
    return workflow, rid, base, ref


def test_history_preserves_complete_bytes_diffs_and_filters_sensitive_paths(tmp_path, monkeypatch):
    workflow, rid, base, ref = history_fixture(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-git"))
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend/src", "private"])
    assert result["status"] == "available" and result["anchors"]["baseline"] == base
    report = workflow.artifacts.json(result["artifact"])
    assert report["source_hash"] == tree_hash(tmp_path)
    assert {s["anchor"] for s in report["snapshots"]} == {"baseline", ref}
    for snapshot in [report["current"], *[s["files"] for s in report["snapshots"]]]:
        assert "frontend/src/format_test.py" in snapshot
        assert "frontend/src/test/link.ts" not in snapshot and ".env" not in snapshot
        assert not any(p.startswith("private/") for p in snapshot)
        assert workflow.artifacts.bytes(snapshot["frontend/src/test/setup.ts"]["content"]) == b"setup();\r\n"
    baseline = next(s for s in report["snapshots"] if s["anchor"] == "baseline")
    changed = next(c for c in baseline["changes_to_current"] if c["path"] == "frontend/src/App.test.tsx")
    assert workflow.artifacts.bytes(changed["before"]["content"]) == b"click();\nexpect(button).toHaveFocus();\n"
    patch = workflow.artifacts.bytes(changed["diff"]).decode()
    assert "+await act(async () => click());" in patch and " expect(button).toHaveFocus();" in patch
    view = ContextPlanner(workflow.artifacts).prepare("review", {"review_kind": "implementation", "source_history": result}, tmp_path)
    mounted = {ref["hash"] for ref in view.payload["evidence_materials"]}
    assert {changed["before"]["content"]["hash"], changed["after"]["content"]["hash"], changed["diff"]["hash"]} <= mounted
    assert SourceHistory(workflow).collect(rid, tmp_path, ["frontend/src", "private"]) == result
    (tmp_path / "frontend/src/App.test.tsx").write_text("new edit")
    assert SourceHistory(workflow).collect_protected(rid, tmp_path)["source_hash"] != result["source_hash"]


def test_source_file_contents_cannot_expand_artifact_admission(tmp_path):
    workflow, rid, _, _ = history_fixture(tmp_path)
    unrelated = workflow.artifacts.put("UNRELATED_CONTROLLER_RECORD_PLACEHOLDER", "private_fixture")
    content = {"report": unrelated}
    source = workflow.artifacts.put(content, "fixture_json")  # Existing edges must not turn source text into authority.
    path = tmp_path / "frontend/src/test/reference.json"
    path.write_text(json.dumps(content, ensure_ascii=True, sort_keys=True))
    history = SourceHistory(workflow).collect_protected(rid, tmp_path)
    view = ContextPlanner(workflow.artifacts).prepare("review", {"review_kind": "implementation", "source_history": history}, tmp_path)
    mounted = {ref["hash"] for ref in view.payload["evidence_materials"]}
    assert source["hash"] in mounted and unrelated["hash"] not in mounted


def test_missing_history_is_explicit_and_deleted_public_tests_remain_auditable(tmp_path):
    workflow, rid, _, ref = history_fixture(tmp_path)
    (tmp_path / "frontend/src/App.test.tsx").unlink()
    git(tmp_path, "update-ref", "-d", ref)
    result = SourceHistory(workflow).collect_protected(rid, tmp_path)
    assert result["status"] == "incomplete" and result["missing"] == [{"anchor": ref, "status": "missing"}]
    report = workflow.artifacts.json(result["artifact"])
    change = next(c for c in report["snapshots"][0]["changes_to_current"] if c["path"] == "frontend/src/App.test.tsx")
    assert change["after"] is None and workflow.artifacts.bytes(change["before"]["content"])


async def test_task_review_receives_applied_candidate_history_and_architecture_receipt(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "review" and context.get("review_kind", "implementation") == "implementation":
                assert context["design_review"]["status"] == "approved"
                assert context["source_history"]["source_hash"] == context["source_hash"]
                report = workflow.artifacts.json(context["source_history"]["artifact"])
                assert "tests/test_calc.py" in report["current"]
                assert report["snapshots"]
            return await super().invoke(stage, context, *args, **kwargs)

    workflow = engine(tmp_path, Agent())
    rid = workflow.store.admit("task receipt", {})["id"]
    await reviewed(workflow, rid, tmp_path)
    commit(tmp_path, "reviewed architecture")
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.store.checkpoint(rid, "baseline", base)
    contract = await CodingAgent().invoke("plan", {}, None, tmp_path, rid, "plan")
    await workflow._task(rid, contract.tasks[0], base, contract, {"specification": {}})


async def test_final_review_receives_current_public_comparisons_and_design_receipt(tmp_path):
    observed = []
    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "verify":
                assert context["design_review"]["status"] == "approved"
                assert context["source_history"]["source_hash"] == tree_hash(workspace)
                report = workflow.artifacts.json(context["source_history"]["artifact"])
                assert "tests/test_calc.py" in report["current"] and report["snapshots"]
                observed.append(operation)
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Add two numbers")
    workflow = engine(project, Agent())
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED" and observed == ["final-review"]
    rid = result["id"]
    assert workflow.store.get(rid, "candidate/design") is None
    receipt = DesignDocuments(workflow).context(rid, project)
    assert receipt["status"] == "approved" and receipt["origin"] == "design/review-receipt"
    assert workflow.artifacts.json(receipt["review_artifact"])["approved"]
    assert "design/review-receipt" in workflow.store.get(rid, "delivery/receipt")["evidence"]


@pytest.mark.parametrize("historical", [False, True])
async def test_controller_review_questions_reach_design_review_without_asking_user(tmp_path, historical):
    class Agent(CodingAgent):
        def __init__(self):
            super().__init__()
            self.refreshes = 0
            self.reviewed = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "design":
                if "controller_feedback" in context:
                    self.refreshes += 1
                    assert context["design_review_evidence"]["records"] if historical else not context["design_review_evidence"]["records"]
                    assert context["source_history"]["artifact"]
                    return DesignQuestions(summary="Submit unchanged current architecture for review")
                return DocumentResult(summary="Need controller evidence", document=(workspace / "docs/aa2/design.md").read_text(),
                    questions=[UserQuestion(prompt="请控制器提供当前架构原始评审；如无决定请执行评审"),
                               UserQuestion(prompt="请控制器提供历史测试基线")])
            if stage == "review" and context.get("review_kind") == "design":
                assert context["design_documents"] and fresh
                self.reviewed = True
            if stage == "plan":
                assert self.reviewed and context["design_review"]["status"] == "approved"
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent()
    workflow, rid = pending_run(tmp_path, agent)
    work = workflow.project
    if historical:
        await reviewed(workflow, rid, work)
        workflow.store.prepare_replan(rid, "design", "Missing original evidence")
        workflow.store.complete_replan(rid, STAGES[STAGES.index("design"):], [])
    else:
        (work / "docs/aa2").mkdir(parents=True)
        (work / "docs/aa2/design.md").write_text("# Current architecture")
    commit(work)
    with pytest.raises(ReachedDevelopment):
        await workflow._execute(rid)
    assert agent.refreshes == 1 and agent.reviewed and not workflow.store.pending(rid)


@pytest.mark.parametrize("boundary", ["after_commit", "after_response", "human_race"])
async def test_question_refresh_resumes_durably_and_preserves_human_answers(tmp_path, monkeypatch, boundary):
    workflow, rid, _, _ = history_fixture(tmp_path)
    document = DocumentResult(summary="pending", document="# Architecture", questions=["Provide controller review"],
        files=[Change(path="docs/aa2/design-feature.md", content="Keep this exact candidate.\n")])
    pending = {"document": document.model_dump(), "proposal_source_hash": tree_hash(tmp_path)}
    workflow.store.checkpoint(rid, "candidate/design", pending)
    question = workflow.store.request(rid, "design/0", document.questions[0], digest(document.model_dump()))
    calls = []
    async def answer(stage, context, *args, **kwargs):
        calls.append(stage)
        if boundary == "human_race":
            workflow.store.answer(question["id"], "Preserve this human answer")
        return DesignQuestions(summary="Controller evidence resolves this question")
    workflow.agent.invoke = answer
    publish, invoke = workflow.store.refresh_design_questions, workflow._invoke
    def interrupted(*args):
        publish(*args)
        raise asyncio.CancelledError()
    async def after_response(*args, **kwargs):
        await invoke(*args, **kwargs)
        raise asyncio.CancelledError()
    if boundary == "after_commit":
        monkeypatch.setattr(workflow.store, "refresh_design_questions", interrupted)
    elif boundary == "after_response":
        monkeypatch.setattr(workflow, "_invoke", after_response)
    service = DesignDocuments(workflow)
    with pytest.raises(StateConflict if boundary == "human_race" else asyncio.CancelledError):
        await service.refresh_questions(rid, tmp_path, document, {})
    if boundary == "human_race":
        assert workflow.store.get(rid, "candidate/design") == pending
        assert workflow.store.question(question["id"])["answer"] == "Preserve this human answer"
        assert not workflow.store.question(question["id"])["superseded"]
        return
    monkeypatch.setattr(workflow.store, "refresh_design_questions", publish)
    monkeypatch.setattr(workflow, "_invoke", invoke)
    current = DocumentResult.model_validate(workflow.store.get(rid, "candidate/design")["document"])
    refreshed = await service.refresh_questions(rid, tmp_path, current, {})
    assert refreshed.model_dump() == {**document.model_dump(), "questions": []}
    assert not (tmp_path / "docs/aa2/design-feature.md").exists()
    assert calls == ["design"]
    assert workflow.store.question(question["id"])["superseded"]
    assert workflow.store.question(question["id"])["answer"] is None


async def test_genuine_product_question_and_recorded_answer_are_preserved(tmp_path):
    workflow, rid, _, _ = history_fixture(tmp_path)
    document = DocumentResult(summary="choice", document="# Architecture", questions=["Should exports include archived tasks?"])
    original = copy.deepcopy(document.model_dump())
    workflow.store.checkpoint(rid, "candidate/design", {"document": original, "proposal_source_hash": tree_hash(tmp_path)})
    calls = []
    async def answer(*args, **kwargs):
        calls.append(1)
        return DesignQuestions(summary="Product choice remains with the user", questions=document.questions)
    workflow.agent.invoke = answer
    refreshed = await DesignDocuments(workflow).refresh_questions(rid, tmp_path, document, {})
    assert refreshed.questions == document.questions and calls == [1]
    question = workflow.store.request(rid, "design/0", document.questions[0], digest(original))
    workflow.store.answer(question["id"], "Include archived tasks")
    workflow.store.checkpoint(rid, "candidate/design", {"document": original, "proposal_source_hash": tree_hash(tmp_path)})
    assert await DesignDocuments(workflow).refresh_questions(rid, tmp_path, document, {}) == document
    assert calls == [1] and workflow.store.question(question["id"])["answer"] == "Include archived tasks"
