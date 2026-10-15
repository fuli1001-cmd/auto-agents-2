import asyncio
import copy
import sqlite3

import pytest
from browser_fixtures import PNG
from test_manifest_references import plan
from test_workflow import CodingAgent, engine

from auto_agents_2.approvals import APPROVAL_CONTEXT_VERSION, prototype_identity
from auto_agents_2.design import PROTOTYPE_BROWSER_VERSION
from auto_agents_2.errors import AA2Error
from auto_agents_2.interaction import choice_spec
from auto_agents_2.models import (
    BrowserAction,
    DocumentResult,
    ImplementationResult,
    IntakeResult,
    ReviewResult,
)
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.util import digest
from auto_agents_2.workspace import git, tree_hash


def approval_fixture(tmp_path, monkeypatch, *, legacy=True, answer="1"):
    contexts, browser_calls = [], []

    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            contexts.append((stage, copy.deepcopy(context)))
            if stage == "review" and context.get("review_kind") == "prototype":
                return ReviewResult(approved=True, summary="Browser evidence independently reviewed")
            if stage in {"implement", "review"}:
                receipt = context["prototype_approval"]
                assert receipt["status"] == "approved" and receipt["human"]["decision"] == "approve"
                assert "待人工批准" in (workspace / "DESIGN.md").read_text()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    workflow = engine(tmp_path, Agent())
    rid = workflow.store.admit("ui", {})["id"]
    page = tmp_path / "design/prototypes/index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<h1>Tasks</h1>")
    (tmp_path / "DESIGN.md").write_text("状态：待人工批准。人工确认后才能开发。")
    git(tmp_path, "add", "DESIGN.md", "design")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "prototype")
    document = DocumentResult(summary="Tasks UI", document="UI contract", browser_actions=[
        BrowserAction(route="/index.html", action="expect_text", selector="h1", value="Tasks")])
    actions = [a.model_dump() for a in document.browser_actions]
    visuals = [{"route": "/index.html", "viewport": width, "status": 200, "errors": [], "actions": actions}
               for width in (1440, 390)]
    image = workflow.artifacts.put(PNG, "screenshot")
    for row in visuals:
        row["image_artifact"] = image
    identity = prototype_identity(tmp_path, actions)
    question = workflow.store.request(rid, "prototype-approval", "Approve the displayed prototype",
        identity["legacy_hash"] if legacy else identity["identity"],
        choice_spec(["通过，继续开发", "修改"], ["approve", "reject"]))
    if answer:
        workflow.store.answer(question["id"], answer, question["artifact_hash"])
    review = ReviewResult(approved=True, summary="Reviewed, not human approval").model_dump()
    approval = {"hash": identity["legacy_hash"], "visuals": visuals, "review": review} if legacy else {
        **identity, "visuals": visuals, "review": review, "browser_version": PROTOTYPE_BROWSER_VERSION}
    workflow.store.checkpoints(rid, {"prototype": document.model_dump(), "prototype_approval": approval,
        "clarify": IntakeResult(summary="UI", document="UI", frontend=True).model_dump()})

    async def browser(*args):
        browser_calls.append(args)
        return copy.deepcopy(visuals)

    async def installed(*args):
        pass

    monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", browser)
    workflow._ensure_browser = installed
    return workflow, rid, document, question, contexts, browser_calls


@pytest.mark.parametrize("legacy", [True, False])
async def test_explicit_consent_migrates_and_reuses_without_reasking(tmp_path, monkeypatch, legacy):
    workflow, rid, document, question, contexts, browser_calls = approval_fixture(tmp_path, monkeypatch, legacy=legacy)
    original = workflow.store.get(rid, "prototype_approval")
    before = tree_hash(tmp_path)
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert receipt["human"]["question_id"] == question["id"]
    assert receipt["human"]["answer"] == "1"
    assert receipt["hash"] == workflow._prototype_hash(tmp_path)
    assert (receipt["hash"] != original["hash"]) == legacy
    assert receipt["identity"] == prototype_identity(tmp_path, document.model_dump()["browser_actions"])["identity"]
    assert tree_hash(tmp_path) == before
    assert len(browser_calls) == int(legacy)
    assert len(contexts) == int(legacy)
    assert workflow.store.get(rid, "candidate/prototype-approval-history/" + digest(original)) == original
    assert await workflow._ensure_prototype_approval(rid, tmp_path) == receipt
    assert len(browser_calls) == int(legacy)


@pytest.mark.parametrize("case", ["no_answer", "reject", "superseded", "wrong_key", "wrong_version",
                                 "content_changed", "actions_changed", "partial_actions", "symlink"])
def test_legacy_record_never_substitutes_for_matching_human_consent(tmp_path, monkeypatch, case):
    workflow, rid, document, question, _, _ = approval_fixture(tmp_path, monkeypatch,
        answer=None if case == "no_answer" else "2" if case == "reject" else "1")
    if case in {"superseded", "wrong_key", "wrong_version"}:
        column, value = {"superseded": ("superseded", 1), "wrong_key": ("key", "prototype"),
                         "wrong_version": ("artifact_hash", "other")}[case]
        with workflow.store.connect() as db:
            db.execute(f"UPDATE questions SET {column}=? WHERE id=?", (value, question["id"]))
    if case == "content_changed":
        (tmp_path / "design/prototypes/index.html").write_text("<h1>Changed</h1>")
    if case == "actions_changed":
        document.browser_actions[0].value = "Changed"
    if case == "partial_actions":
        record = workflow.store.get(rid, "prototype_approval")
        record["visuals"][0]["actions"] = []
        workflow.store.checkpoint(rid, "prototype_approval", record)
    if case == "symlink":
        # Same bytes and legacy digest do not prove the old link target identity.
        page = tmp_path / "design/prototypes/index.html"
        (tmp_path / "external.html").write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(tmp_path / "external.html")
    assert workflow.approvals.human(rid, tmp_path, document.model_dump()["browser_actions"]) is None


async def test_review_only_record_blocks_task_before_round_or_model_call(tmp_path, monkeypatch):
    workflow, rid, _, _, contexts, _ = approval_fixture(tmp_path, monkeypatch, legacy=False, answer=None)
    proposal = plan()
    base = workflow.workspaces.committed_baseline(rid, "main")
    with pytest.raises(AA2Error, match="人工审批证据"):
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {"frontend": True}})
    assert not contexts and workflow.store.get(rid, "rounds/T1") is None
    assert workflow.store.get(rid, "develop/task/T1") is None
    assert workflow.store.pending(rid)[0]["answer"] is None


@pytest.mark.parametrize("mutation", ["content", "actions", "question", "image"])
async def test_published_receipt_is_revalidated_at_dispatch(tmp_path, monkeypatch, mutation):
    workflow, rid, _, question, contexts, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    await workflow._ensure_prototype_approval(rid, tmp_path)
    if mutation == "content":
        (tmp_path / "DESIGN.md").write_text("Another design")
    elif mutation == "actions":
        prototype = workflow.store.get(rid, "prototype")
        prototype["browser_actions"][0]["value"] = "Another interaction"
        workflow.store.checkpoint(rid, "prototype", prototype)
    elif mutation == "question":
        with workflow.store.connect() as db:
            db.execute("UPDATE questions SET superseded=1 WHERE id=?", (question["id"],))
    else:
        receipt = workflow.store.get(rid, "prototype_approval")
        image = workflow.artifacts.put(b"screenshot", "screenshot")
        receipt["visuals"][0]["image_artifact"] = image
        workflow.store.checkpoint(rid, "prototype_approval", receipt)
        (workflow.artifacts.root / image["hash"][:2] / image["hash"]).unlink()
    with pytest.raises(AA2Error, match="人工审批证据"):
        workflow._task_approval_context(rid, tmp_path, {"specification": {"frontend": True}})
    assert not contexts


def blocked_approval_workflow(tmp_path, monkeypatch):
    workflow, rid, document, question, contexts, browser_calls = approval_fixture(tmp_path, monkeypatch)
    proposal = plan()
    workflow.store.checkpoint(rid, "plan", proposal.model_dump())
    base = workflow.workspaces.committed_baseline(rid, "main")
    integration = workflow.workspaces.allocate(rid, "integration", base)
    work = workflow.workspaces.allocate(rid, "T1-p0", base)
    response = ImplementationResult(summary="Need consent", changes=[], replan_stage="prototype",
                                    replan_reason="Missing human approval; also reassess the entire previous request")
    workflow.store.checkpoints(rid, {"develop/done": ["accepted-task"],
        "develop/task/accepted-task": {"commit": base}, "develop/task/T1": {"phase": "implementing", "feedback": {}},
        "invocation/develop/task/T1": {"identity": "old", "result": response.model_dump()},
        "rounds/T1": {"round": 1, "stalled": 0, "failure": ""},
        "replanning/progress": {"tracked": {}, "evaluations": 6, "stalled": 5}})
    pending = workflow.store.prepare_replan(rid, "prototype", response.replan_reason)
    workflow.store.request(rid, f"replan-stalled/{pending['evaluation']}", "stop", digest(pending))
    return workflow, rid, proposal, base, integration, work, pending, contexts, browser_calls


async def test_recover_old_task_preserves_completed_work_counters_and_full_request(tmp_path, monkeypatch):
    workflow, rid, proposal, base, integration, work, pending, contexts, browser_calls = blocked_approval_workflow(tmp_path, monkeypatch)
    progress, rounds = workflow.store.get(rid, "replanning/progress"), workflow.store.get(rid, "rounds/T1")
    before = tree_hash(work), tree_hash(integration), git(tmp_path, "show-ref")
    assert await workflow._refresh_legacy_approval_context(rid)
    assert workflow.store.get(rid, "replanning/pending") is None
    assert workflow.store.get(rid, "replanning/progress") == progress
    assert workflow.store.get(rid, "rounds/T1") == rounds
    assert workflow.store.get(rid, "develop/done") == ["accepted-task"]
    assert workflow.store.get(rid, "develop/task/accepted-task") == {"commit": base}
    assert workflow.store.get(rid, "plan") == proposal.model_dump()
    assert (tree_hash(work), tree_hash(integration), git(tmp_path, "show-ref")) == before
    recovery = workflow.store.get(rid, "candidate/approval-recovery/T1/p0")
    assert recovery["pending"] == pending and recovery["recovered"]
    assert len(browser_calls) == 1 and len(contexts) == 1
    assert not workflow.store.pending(rid)
    await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {"frontend": True}})
    task_contexts = [c for s, c in contexts if s == "implement" or c.get("review_kind") != "prototype"]
    assert len(task_contexts) >= 2
    assert all(c["prototype_approval"]["human"]["decision"] == "approve" for c in task_contexts)
    assert task_contexts[0]["feedback"]["controller_approval_recheck"]["previous_request"]["replan_reason"] == pending["reason"]
    assert workflow.store.get(rid, "develop/task/T1")["commit"]
    assert workflow.store.get(rid, "rounds/T1") == rounds


@pytest.mark.parametrize("condition", ["missing_consent", "changed_prototype", "unrelated", "new_context", "applied"])
async def test_ineligible_prototype_replan_is_preserved(tmp_path, monkeypatch, condition):
    workflow, rid, _, _, _, work, pending, contexts, browser_calls = blocked_approval_workflow(tmp_path, monkeypatch)
    if condition == "missing_consent":
        with workflow.store.connect() as db:
            db.execute("UPDATE questions SET superseded=1 WHERE key='prototype-approval'")
    elif condition == "changed_prototype":
        (work / "DESIGN.md").write_text("preserve user edit")
    elif condition == "unrelated":
        pending["reason"] = "Other replan"
        workflow.store.checkpoint(rid, "replanning/pending", pending)
    else:
        saved = workflow.store.get(rid, "develop/task/T1")
        saved.update({"approval_context_version": APPROVAL_CONTEXT_VERSION} if condition == "new_context" else {"patch": ["applied"]})
        workflow.store.checkpoint(rid, "develop/task/T1", saved)
    assert not await workflow._refresh_legacy_approval_context(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert not contexts and not browser_calls


@pytest.mark.parametrize("fault", ["browser_interrupt", "transaction"])
async def test_approval_recovery_is_restartable_and_atomic(tmp_path, monkeypatch, fault):
    workflow, rid, _, _, _, _, pending, _, _ = blocked_approval_workflow(tmp_path, monkeypatch)
    original = workflow.store.get(rid, "develop/task/T1")
    browser = workflow._prototype_browser_report

    async def interrupt(*args):
        raise asyncio.CancelledError()

    if fault == "browser_interrupt":
        workflow._prototype_browser_report = interrupt
        error = asyncio.CancelledError
    else:
        with workflow.store.connect() as db:
            db.execute("CREATE TRIGGER fail_recovery BEFORE DELETE ON checkpoints "
                       "WHEN OLD.key='replanning/pending' BEGIN SELECT RAISE(ABORT,'injected'); END")
        error = sqlite3.IntegrityError
    with pytest.raises(error):
        await workflow._refresh_legacy_approval_context(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert workflow.store.get(rid, "develop/task/T1") == original
    workflow._prototype_browser_report = browser
    if fault == "transaction":
        with workflow.store.connect() as db:
            db.execute("DROP TRIGGER fail_recovery")
    assert await workflow._refresh_legacy_approval_context(rid)


async def test_new_model_replan_after_receipt_is_not_suppressed(tmp_path, monkeypatch):
    workflow, rid, proposal, base, _, _, _, _, _ = blocked_approval_workflow(tmp_path, monkeypatch)
    assert await workflow._refresh_legacy_approval_context(rid)

    async def still_blocked(*args, **kwargs):
        return ImplementationResult(summary="Real design issue", changes=[], replan_stage="prototype", replan_reason="Still needs redesign")

    workflow.agent.invoke = still_blocked
    with pytest.raises(RecoveryNeeded) as raised:
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {"frontend": True}})
    assert raised.value.failure.proposal["replan_reason"] == "Still needs redesign"
    pending = workflow.store.prepare_replan(rid, "prototype", "Still needs redesign")
    assert not await workflow._refresh_legacy_approval_context(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending


@pytest.mark.parametrize("status", ["SUCCEEDED", "CANCELLED"])
async def test_historical_reuse_preserves_origin_and_requires_successful_run(tmp_path, monkeypatch, status):
    workflow, prior, document, question, _, _ = approval_fixture(tmp_path, monkeypatch)
    approval = workflow.store.get(prior, "prototype_approval")
    workflow.store.update(prior, status=status)
    rid = workflow.store.admit("later iteration", {})["id"]
    workflow.store.checkpoints(rid, {"prototype": document.model_dump(), "prototype_approval": approval})
    if status == "CANCELLED":
        assert workflow.approvals.human(rid, tmp_path, document.model_dump()["browser_actions"]) is None
        return
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert receipt["human"]["run_id"] == prior
    assert receipt["human"]["question_id"] == question["id"]
    assert await workflow._ensure_prototype_approval(rid, tmp_path) == receipt


async def test_cached_prototype_without_consent_waits_and_resumes_verified_evidence(tmp_path, monkeypatch):
    workflow, rid, _, _, contexts, browser_calls = approval_fixture(tmp_path, monkeypatch, answer=None)
    asked = []

    async def interrupt(question):
        asked.append(question["id"])
        assert question["key"] == "prototype-approval" and "http://127.0.0.1:" in question["prompt"]
        raise asyncio.CancelledError()

    workflow.question_handler = interrupt
    with pytest.raises(asyncio.CancelledError):
        await workflow._ensure_prototype_approval(rid, tmp_path)
    assert len(browser_calls) == 1 and len(contexts) == 1
    assert workflow.store.get(rid, "rounds/T1") is None

    async def approve(question):
        assert question["id"] == asked[0]
        return "1"

    workflow.question_handler = approve
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert receipt["human"]["question_id"] == asked[0]
    assert len(browser_calls) == 1 and len(contexts) == 1


@pytest.mark.parametrize("failed_gate", ["browser", "review"])
async def test_legacy_consent_cannot_bypass_fresh_automated_gates(tmp_path, monkeypatch, failed_gate):
    workflow, rid, _, _, _, _, pending, _, _ = blocked_approval_workflow(tmp_path, monkeypatch)
    if failed_gate == "browser":
        from auto_agents_2.errors import PrototypeBrowserError

        async def failed(*args):
            raise PrototypeBrowserError("Interaction failed", [{"status": 200, "errors": ["wrong state"]}])

        monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", failed)
    else:
        async def rejected(*args, **kwargs):
            return ReviewResult(approved=False, summary="Interaction failed")

        workflow.agent.invoke = rejected
    with pytest.raises(AA2Error, match="历史原型审批复核未通过.*Interaction failed"):
        await workflow._refresh_legacy_approval_context(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert workflow.store.get(rid, "develop/task/T1") == {"phase": "implementing", "feedback": {}}
    assert workflow.store.get(rid, "candidate/approval-recovery/T1/p0")["pending"] == pending


async def test_delivery_repair_supplies_approval_to_implementation_and_review(tmp_path, monkeypatch):
    workflow, rid, _, question, contexts, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    await workflow._ensure_prototype_approval(rid, tmp_path)
    await workflow._delivery_repair(rid, tmp_path, {"runtime_failure": "repair"}, ["calc.py", "tests"])
    assert [stage for stage, _ in contexts] == ["review", "implement", "review"]
    assert contexts[0][1]["review_kind"] == "recovery_diagnosis"
    assert all(c["prototype_approval"]["human"]["question_id"] == question["id"] for _, c in contexts)
