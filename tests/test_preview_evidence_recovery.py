import asyncio
import copy
import os
import sys

import pytest
from browser_fixtures import PNG
from test_browser import browser_cache as browser_cache
from test_prototype_evidence_recovery import ready as prototype_ready

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import AA2Error, PrototypeBrowserError
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import BrowserAction, Command, PreviewSpec, ReviewResult
from auto_agents_2.preview_evidence import ActionMapping, PreviewComparison, PreviewEvidence
from auto_agents_2.recovery import Diagnosis, RecoveryNeeded, adjudicate
from auto_agents_2.workspace import tree_hash


async def ready(tmp_path, monkeypatch):
    workflow, rid, work, plan, base, failure, _, _, approval = await prototype_ready(
        tmp_path, monkeypatch, legacy_review=False)
    actions = workflow.store.get(rid, "prototype")["browser_actions"]
    assert workflow.approvals.current(rid, work, actions, workflow.browser_check_version())
    previous = PreviewSpec(command=Command(argv=["python", "-m", "preview", "{port}"], inputs=["dist"]),
        actions=[BrowserAction(action="click", selector="#add"),
                 BrowserAction(action="expect_text", selector="#count", value="1"),
                 BrowserAction(action="click", selector="#delete")])
    plan.preview = previous
    history = {"plan": plan.model_dump(), "tasks": {}}
    plan.preview = previous.model_copy(update={"actions": [*previous.actions,
        BrowserAction(action="expect_text", selector="#count", value="0")]})
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "plan_revision/history/5": history,
                                    "plan_revision/history/6": {"plan": plan.model_dump()}})
    (work / "dist").mkdir()
    (work / "dist/index.html").write_text("fixture build")
    calls, browsers = [], []
    image = workflow.artifacts.put(PNG, "screenshot")

    async def browser(spec, *args):
        browsers.append(spec.model_dump())
        return [{"route": "/", "viewport": w, "viewport_height": h, "status": 200,
                 "errors": [], "actions": [a.model_dump() for a in spec.actions], "image_artifact": image,
                 "cleanup": {"process_stopped": True, "scratch_removed": True}}
                for w, h in ((1440, 900), (390, 844))]

    monkeypatch.setattr("auto_agents_2.preview_evidence.inspect_application", browser)

    class Agent:
        choice = "preview_recheck"
        approved = True
        missing_mapping = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            calls.append((stage, copy.deepcopy(context)))
            if stage == "decision":
                if self.choice == "ask_user":
                    return DecisionResult(action="ask_user", reason="Original preview contract or controller entry required")
                candidate = context["decision_request"]["evidence"]["preview_recheck"]
                assert candidate["contracts"]["previous"]["origin"] == "plan_revision/history/5"
                return DecisionResult(action="approve", selected_action=self.choice, evidence_refs=["preview_recheck"],
                    reason="Collect missing production preview evidence", expected_result="Complete preview and cleanup")
            if stage == "recovery_diagnosis":
                return Diagnosis(action="recheck", task_ids=["T1"], evidence_refs=["failure"], reason="Existing checks")
            assert stage == "review" and fresh
            if context.get("review_kind") == "preview_contract":
                contracts = context["preview_contracts"]
                assert contracts["previous"]["preview"] == previous.model_dump()
                assert len(contracts["current"]["preview"]["actions"]) == 4
                return PreviewComparison(review=ReviewResult(approved=self.approved, summary="Every original obligation retained"),
                    mappings=[ActionMapping(previous_index=i, current_indexes=[i], reason="Original action retained")
                              for i in range(2 if self.missing_mapping else 3)])
            assert context["preview_evidence"]["comparison"]["review"]["approved"]
            assert context["preview_evidence"]["execution"]["source_hash"] == tree_hash(work)
            return ReviewResult(approved=True, summary="Full preview evidence independently reviewed")

    workflow.agent = Agent()
    return workflow, rid, work, plan, base, failure, calls, browsers, approval


@pytest.mark.parametrize("review_approved", [True, False])
async def test_legacy_prototype_review_must_be_refreshed_before_preview(tmp_path, monkeypatch, review_approved):
    workflow, rid, work, plan, _, failure, calls, browsers, old = await prototype_ready(tmp_path, monkeypatch)
    plan.preview = PreviewSpec(command=Command(argv=["python", "-m", "preview", "{port}"]),
        actions=[BrowserAction(action="expect_text", selector="#count", value="0")])
    workflow.store.checkpoint(rid, "plan", plan.model_dump())
    service = PreviewEvidence(workflow)
    actions = workflow.store.get(rid, "prototype")["browser_actions"]
    human = workflow.approvals.human(rid, work, actions)
    source = tree_hash(work)
    original_task = workflow.store.get(rid, "develop/task/T1")
    counters = {key: workflow.store.get(rid, key) for key in ("rounds/T1", "recovery/budgets/diagnosis/T1")}
    assert human and service.binding(rid, work, plan) is None
    allowed = workflow.recovery_policy.diagnosis_actions(rid, failure, work, plan)
    assert "prototype_recheck" in allowed and "preview_recheck" not in allowed
    with pytest.raises(AA2Error, match="人工审批证据"):
        workflow._task_approval_context(rid, work, {"specification": {"frontend": True}})

    async def no_application_preview(*args, **kwargs):
        raise AssertionError("Refreshing a prototype cannot execute an application preview")

    monkeypatch.setattr("auto_agents_2.preview_evidence.inspect_application", no_application_preview)
    workflow.agent.approved = review_approved
    await workflow.recovery.resume(rid)
    assert [stage for stage, _ in calls] == ["decision", "review"] and len(browsers) == 1
    assert workflow.approvals.human(rid, work, actions) == human
    assert tree_hash(work) == source and workflow.store.get(rid, "plan") == plan.model_dump()
    assert {key: workflow.store.get(rid, key) for key in counters} == counters
    assert workflow.store.get(rid, "develop/task/T1")["checks"] == original_task["checks"]
    assert not workflow.store.pending(rid)
    if review_approved:
        assert service.binding(rid, work, plan)
        assert "preview_recheck" in workflow.recovery_policy.diagnosis_actions(rid, failure, work, plan)
        assert workflow.store.get(rid, "recovery/active") is None
        assert workflow.store.get(rid, "develop/task/T1")["phase"] == "tested"
        assert workflow._task_approval_context(rid, work, {"specification": {"frontend": True}})
    else:
        assert service.binding(rid, work, plan) is None
        assert workflow.store.get(rid, "prototype_approval") == old
        assert workflow.store.get(rid, "develop/task/T1") == original_task
        assert workflow.store.get(rid, "recovery/active")["failure"]["component"] == "prototype_verification"


async def test_collects_historical_contract_and_preview_before_accepting_retained_task(tmp_path, monkeypatch):
    workflow, rid, work, plan, base, _, calls, browsers, approval = await ready(tmp_path, monkeypatch)
    source = tree_hash(work)
    counters = workflow.store.get(rid, "rounds/T1")
    checks = workflow.store.get(rid, "develop/task/T1")["checks"]
    await workflow.recovery.resume(rid)
    assert [s for s, _ in calls] == ["decision", "review"]
    assert len(browsers) == 1
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "develop/task/T1")["phase"] == "tested"
    assert workflow.store.get(rid, "develop/task/T1")["checks"] == checks
    assert workflow.store.get(rid, "develop/done") == ["accepted-task"]
    assert workflow.store.get(rid, "rounds/T1") == counters
    assert workflow.store.get(rid, "prototype_approval") == approval
    assert tree_hash(work) == source
    await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {"frontend": True}})
    assert [s for s, _ in calls] == ["decision", "review", "review"]
    assert workflow.store.get(rid, "develop/task/T1")["commit"]


@pytest.mark.parametrize("boundary", ["comparison", "report", "dispatch"])
async def test_saved_comparison_and_browser_results_are_reused_after_interruption(tmp_path, monkeypatch, boundary):
    workflow, rid, _, _, _, _, calls, browsers, _ = await ready(tmp_path, monkeypatch)
    checkpoint, finish = workflow.store.checkpoint, workflow.store.finish_recovery
    stopped = False

    def interrupt(rid, key, value):
        nonlocal stopped
        checkpoint(rid, key, value)
        if key.startswith("recovery/preview-rechecks/") and boundary in value and not stopped:
            stopped = True
            raise asyncio.CancelledError()

    def dispatch(*args, **kwargs):
        if boundary == "dispatch":
            raise asyncio.CancelledError()
        return finish(*args, **kwargs)

    monkeypatch.setattr(workflow.store, "checkpoint", interrupt)
    monkeypatch.setattr(workflow.store, "finish_recovery", dispatch)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow.store, "checkpoint", checkpoint)
    monkeypatch.setattr(workflow.store, "finish_recovery", finish)
    await workflow.recovery.resume(rid)
    assert len(browsers) == 1 and [s for s, _ in calls] == ["decision", "review"]
    assert workflow.store.get(rid, "recovery/active") is None


@pytest.mark.parametrize("fault", ["rejected", "mapping", "browser", "cleanup", "viewport", "actions"])
async def test_incomplete_comparison_or_execution_cannot_advance_task(tmp_path, monkeypatch, fault):
    workflow, rid, _, _, _, _, _, browsers, _ = await ready(tmp_path, monkeypatch)
    original = workflow.store.get(rid, "develop/task/T1")
    workflow.agent.approved = fault != "rejected"
    workflow.agent.missing_mapping = fault == "mapping"
    import auto_agents_2.preview_evidence as module
    browser = module.inspect_application

    async def broken(*args):
        rows = await browser(*args)
        if fault == "browser":
            raise PrototypeBrowserError("injected failure", rows)
        if fault == "cleanup":
            rows[0]["cleanup"]["scratch_removed"] = False
        if fault == "viewport":
            rows[0]["viewport_height"] = 720
        if fault == "actions":
            rows[0]["actions"].pop()
        return rows

    monkeypatch.setattr(module, "inspect_application", broken)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "develop/task/T1") == original
    assert workflow.store.get(rid, "recovery/active")["failure"]["component"] == "preview_verification"
    assert len(browsers) == int(fault not in {"rejected", "mapping"})
    assert workflow.store.get(rid, "preview-evidence/current/T1") is None


@pytest.mark.parametrize("change", ["source", "history", "consent", "published"])
async def test_inputs_changed_after_permit_cannot_execute_preview(tmp_path, monkeypatch, change):
    workflow, rid, work, _, _, _, _, browsers, _ = await ready(tmp_path, monkeypatch)
    start = workflow.recovery_policy.start

    def drift(rid, key):
        start(rid, key)
        if change == "source":
            (work / "user.txt").write_text("preserve")
        elif change == "history":
            history = workflow.store.get(rid, "plan_revision/history/5")
            history["plan"]["preview"]["actions"].pop()
            workflow.store.checkpoint(rid, "plan_revision/history/5", history)
        elif change == "consent":
            workflow.store.supersede_questions(rid, "prototype-approval", include_answered=True)
        else:
            workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})

    monkeypatch.setattr(workflow.recovery_policy, "start", drift)
    with pytest.raises(StateConflict, match="inputs or human consent changed"):
        await workflow.recovery.resume(rid)
    assert not browsers and workflow.store.get(rid, "recovery/active")


async def test_completed_preview_becomes_unavailable_when_build_changes(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, _, _, _, _ = await ready(tmp_path, monkeypatch)
    await workflow.recovery.resume(rid)
    service = PreviewEvidence(workflow)
    environment = await workflow._environment(work, rid, plan)
    assert service.context(rid, work, plan, environment, task_id="T1")
    (work / "dist/index.html").write_text("changed build")
    assert not service.context(rid, work, plan, environment, task_id="T1")


async def test_task_review_cannot_accept_a_preview_with_changed_inputs(tmp_path, monkeypatch):
    workflow, rid, work, plan, base, _, _, _, _ = await ready(tmp_path, monkeypatch)
    await workflow.recovery.resume(rid)
    invoke = workflow.agent.invoke

    async def drift(stage, context, *args, **kwargs):
        result = await invoke(stage, context, *args, **kwargs)
        if stage == "review":
            (work / "dist/index.html").write_text("changed during review")
        return result

    workflow.agent.invoke = drift
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {"frontend": True}})
    assert caught.value.failure.step == "preview_stale"
    assert not workflow.store.get(rid, "develop/task/T1").get("commit")


async def test_failed_preview_restarts_whole_contract_under_a_new_permit(tmp_path, monkeypatch):
    workflow, rid, _, _, _, _, _, browsers, _ = await ready(tmp_path, monkeypatch)
    import auto_agents_2.preview_evidence as module
    browser = module.inspect_application

    async def failing(*args):
        rows = await browser(*args)
        raise PrototypeBrowserError("failure after a write", rows)

    monkeypatch.setattr(module, "inspect_application", failing)
    await workflow.recovery.resume(rid)
    history = workflow.store.recovery_history(rid, "T1")["recent"]
    first_permit = history[-1]["action"]
    assert first_permit["action"] == "preview_recheck"
    monkeypatch.setattr(module, "inspect_application", browser)
    invoke = workflow.agent.invoke

    async def decide(stage, context, *args, **kwargs):
        if stage == "recovery_diagnosis":
            return Diagnosis(action="preview_recheck", task_ids=["T1"], evidence_refs=["failure"],
                             reason="Retry the complete isolated preview after confirmed cleanup")
        return await invoke(stage, context, *args, **kwargs)

    workflow.agent.invoke = decide
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert len(browsers) == 2 and browsers[0] == browsers[1]
    with workflow.store.connect() as db:
        rows = db.execute("SELECT key FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/preview-rechecks/%'", (rid,)).fetchall()
    assert len(rows) == 2
    records = [workflow.store.get(rid, row[0]) for row in rows]
    assert {record["phase"] for record in records} == {"recorded", "completed"}


async def test_real_browser_preview_runs_before_task_acceptance(browser_cache, tmp_path, monkeypatch):
    from auto_agents_2.design import inspect_application
    cache = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    workflow, rid, work, plan, base, _, _, _, _ = await ready(tmp_path, monkeypatch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", cache)
    (work / "site").mkdir()
    (work / "site/index.html").write_text('''<!doctype html><html><body>
<button id="add" onclick="document.getElementById('count').textContent='1'">Add</button>
<button id="delete" onclick="document.getElementById('count').textContent='0'">Delete</button>
<p id="count">0</p></body></html>''')
    plan.preview.command.argv = [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"]
    plan.preview.command.cwd = "site"
    plan.preview.command.inputs = ["site"]
    old = workflow.store.get(rid, "plan_revision/history/5")
    old["plan"]["preview"]["command"] = plan.preview.command.model_dump()
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "plan_revision/history/5": old,
                                    "plan_revision/history/6": {"plan": plan.model_dump()}})
    monkeypatch.setattr("auto_agents_2.preview_evidence.inspect_application", inspect_application)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    environment = await workflow._environment(work, rid, plan)
    receipt = PreviewEvidence(workflow).context(rid, work, plan, environment, task_id="T1")
    report = workflow.artifacts.json(receipt["preview_evidence"]["report"])
    assert len(report["results"]) == 2
    assert all(len(row["actions"]) == 4 for row in report["results"])
    assert not list((workflow.store.root / "processes").glob("*.json"))
    assert not list((workflow.store.root / "tmp").glob("preview-*"))
    await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {"frontend": True}})
    assert workflow.store.get(rid, "develop/task/T1")["commit"]


async def test_unknown_browser_completion_is_not_replayed_on_resume(tmp_path, monkeypatch):
    workflow, rid, _, _, _, _, _, browsers, _ = await ready(tmp_path, monkeypatch)

    async def interrupted(*args):
        browsers.append("started")
        raise asyncio.CancelledError()

    monkeypatch.setattr("auto_agents_2.preview_evidence.inspect_application", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    with pytest.raises(StateConflict, match="no durable completion"):
        await workflow.recovery.resume(rid)
    assert browsers == ["started"]


@pytest.mark.parametrize("answer", [None, "y", "n", "race"])
async def test_old_pending_request_offers_preview_without_expanding_old_answers(tmp_path, monkeypatch, answer):
    workflow, rid, _, _, _, _, calls, browsers, _ = await ready(tmp_path, monkeypatch)
    binding = PreviewEvidence.binding
    monkeypatch.setattr(PreviewEvidence, "binding", lambda *a: None)
    workflow.agent.choice = "ask_user"

    async def leave_open(question):
        raise asyncio.CancelledError()

    workflow.question_handler = leave_open
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    question = workflow.store.pending(rid)[0]
    old_key = question["key"].replace("recovery-cycle/", "recovery/authorization-inputs/")
    old_binding = workflow.store.get(rid, old_key)
    monkeypatch.setattr(PreviewEvidence, "binding", binding)
    workflow.agent.choice = "preview_recheck"
    if answer in {"y", "n"}:
        workflow.store.answer(question["id"], answer)
    invoke = workflow.agent.invoke

    async def racing(stage, *args, **kwargs):
        result = await invoke(stage, *args, **kwargs)
        if stage == "decision" and answer == "race":
            workflow.store.answer(question["id"], "n")
        return result

    workflow.agent.invoke = racing
    if answer == "race":
        with pytest.raises(StateConflict, match="authorization changed"):
            await workflow.recovery.resume(rid)
    elif answer == "n":
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    else:
        await workflow.recovery.resume(rid)
    if answer is None:
        assert len(browsers) == 1
        assert [s for s, _ in calls] == ["decision", "decision", "review"]
        assert workflow.store.question(question["id"])["superseded"]
        assert workflow.store.question(question["id"])["answer"] is None
    else:
        assert not browsers and workflow.store.get(rid, old_key) == old_binding


async def test_context_selection_admits_full_contracts_and_preview_attachments(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, failure, _, _, _ = await ready(tmp_path, monkeypatch)
    contracts = PreviewEvidence(workflow).contracts(rid, plan)
    view = ContextPlanner(workflow.artifacts).prepare("review", {
        "review_kind": "preview_contract", "preview_contracts": contracts}, work)
    assert view.payload["preview_contracts"]["previous"]["preview"]["actions"] == contracts["previous"]["preview"]["actions"]
    await workflow.recovery.resume(rid)
    environment = await workflow._environment(work, rid, plan)
    context = PreviewEvidence(workflow).context(rid, work, plan, environment, task_id="T1")
    view = ContextPlanner(workflow.artifacts).prepare("review", context, work)
    assert view.payload["preview_evidence"]["report"] == context["preview_evidence"]["report"]
    assert any(ref["kind"] == "screenshot" for ref in view.payload["evidence_materials"])


async def test_preview_is_not_offered_without_accepted_plan_preview(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, failure, _, _, _ = await ready(tmp_path, monkeypatch)
    plan.preview = None
    workflow.store.checkpoint(rid, "plan", plan.model_dump())

    async def choose(stage, context, *args, **kwargs):
        assert "preview_recheck" not in context["decision_request"]["scope"]["candidates"]
        return DecisionResult(action="approve", selected_action="recheck", evidence_refs=["failure"],
                              reason="Declared checks", expected_result="Current checks")

    workflow.agent.invoke = choose
    await workflow.recovery_policy.authorize(rid, failure, workflow.store.get(rid, "recovery/active"), work, plan,
                                             adjudicate(failure), operation="eligibility", force_decision=True)


@pytest.mark.parametrize("stage", ["implement", "review", "recovery_diagnosis", "verify"])
async def test_preview_contracts_reach_all_controller_consumers(tmp_path, monkeypatch, stage):
    workflow, rid, work, plan, _, _, _, _, _ = await ready(tmp_path, monkeypatch)
    contracts = PreviewEvidence(workflow).contracts(rid, plan)
    view = ContextPlanner(workflow.artifacts).prepare(stage, {"preview_contracts": contracts}, work)
    assert view.payload["preview_contracts"] == contracts
    assert not any(row["source_ref"] == "preview_contracts" and row["presentation"] == "not_selected"
                   for row in view.selections)
