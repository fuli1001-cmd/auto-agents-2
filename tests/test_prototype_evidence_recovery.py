import asyncio
import copy

import pytest
from prototype_review_fixtures import prototype_verdict
from test_manifest_references import plan
from test_prototype_approvals import approval_fixture

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import AA2Error, PrototypeBrowserError
from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import Finding, ReviewResult
from auto_agents_2.recovery import Diagnosis, FailureRecord, adjudicate
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


async def ready(tmp_path, monkeypatch):
    workflow, rid, document, question, _, browser_calls = approval_fixture(tmp_path, monkeypatch, legacy=False)
    contract = plan()
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "integration", base)
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "tests").mkdir()
    (work / "tests/test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n")
    environment = await workflow._environment(work, rid)
    checks = await workflow.verifier.run(contract.tasks[0].checks, work, environment)
    finding = Finding(severity="minor", location="prototype screenshot", description="Image differs from assertions",
                      evidence="Original prototype image", resolution="Recapture and independently review the final state")
    old = workflow.store.get(rid, "prototype_approval")
    old["review"] = ReviewResult(approved=True, summary="One open evidence concern", findings=[finding]).model_dump()
    review = ReviewResult(approved=False, summary="Prototype evidence prerequisite remains open",
                         findings=[finding.model_copy(update={"severity": "major"})])
    task = {"phase": "rejected", "workspace": str(work), "source_hash": tree_hash(work),
            "checks": [c.model_dump() for c in checks], "review": review.model_dump()}
    workflow.store.checkpoints(rid, {"plan": contract.model_dump(), "prototype_approval": old,
        "develop/task/T1": task, "develop/done": ["accepted-task"],
        "develop/task/accepted-task": {"commit": base}, "rounds/T1": {"round": 8, "stalled": 3},
        "recovery/budgets/diagnosis/T1": {"round": 6}})
    ledger = IssueLedger(workflow.store, workflow.artifacts)
    ledger.record(rid, "prototype-review", ReviewResult.model_validate(old["review"]), {})
    ledger.record(rid, "develop/task/T1/review", review, {"task": contract.tasks[0].model_dump()})
    failure = FailureRecord.create(kind="review", component="review", origin_id="T1", task_id="T1",
        step="review", obligations=["R1"], evidence_refs=["develop/task/T1"],
        evidence={"task": contract.tasks[0].model_dump(), "checks": task["checks"], "review": review.model_dump()})
    workflow.recovery.submit(rid, failure)
    workflow.store.checkpoint(rid, "recovery/rules/" + digest(["T1", "review", ["R1"], "diagnose"]), {"seen": True})
    calls = []

    class Agent:
        choice = "prototype_recheck"
        approved = True

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            calls.append((stage, copy.deepcopy(context)))
            if stage == "decision":
                if self.choice == "ask_user":
                    return DecisionResult(action="ask_user", reason="No useful action in the old candidates")
                assert "prototype_recheck" in context["decision_request"]["scope"]["candidates"]
                assert context["decision_request"]["evidence"]["prototype_recheck"]["binding"]["human"]["question_id"] == question["id"]
                return DecisionResult(action="approve", selected_action="prototype_recheck", evidence_refs=["failure"],
                    reason="Refresh the missing prerequisite", expected_result="Fresh prototype evidence and independent review")
            assert stage == "review" and fresh
            if context.get("review_kind") == "prototype":
                assert browser_calls and context["browser_evidence"]
                assert context["unresolved_issues"]
                assert context["feedback"]["previous_approval"] == old
                return prototype_verdict(context, approved=self.approved, summary="Fresh images reviewed")
            assert context["prototype_approval"]["human"]["question_id"] == question["id"]
            return ReviewResult(approved=True, summary="Retained implementation and fresh prerequisite verified")

    workflow.agent = Agent()
    return workflow, rid, work, contract, base, failure, calls, browser_calls, old


async def test_refreshes_prototype_before_reviewing_same_candidate_without_implementation(tmp_path, monkeypatch):
    workflow, rid, work, contract, base, failure, calls, browsers, old = await ready(tmp_path, monkeypatch)
    source = tree_hash(work)
    original_task = workflow.store.get(rid, "develop/task/T1")
    await workflow.recovery.resume(rid)
    assert len(browsers) == 1 and [s for s, _ in calls] == ["decision", "review"]
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "develop/task/T1")["phase"] == "tested"
    assert workflow.store.get(rid, "develop/task/T1")["checks"] == original_task["checks"]
    assert workflow.store.get(rid, "plan") == contract.model_dump()
    assert workflow.store.get(rid, "develop/done") == ["accepted-task"]
    assert tree_hash(work) == source
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8, "stalled": 3}
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T1") == {"round": 6}
    assert workflow.store.get(rid, "candidate/prototype-approval-history/" + digest(old)) == old
    issues = IssueLedger(workflow.store, workflow.artifacts).unresolved(rid)
    assert issues and all(i["scope"] == "task/T1/implementation" for i in issues)
    assert not workflow.store.pending(rid)
    await workflow._task(rid, contract.tasks[0], base, contract, {"specification": {"frontend": True}})
    assert [s for s, _ in calls] == ["decision", "review", "review"]
    assert not IssueLedger(workflow.store, workflow.artifacts).unresolved(rid)
    action = workflow.store.recovery_history(rid, "T1")["recent"][-1]
    assert action["phase"] == "completed" and action["outcome"]["tasks"]["T1"]["status"] == "verified"


@pytest.mark.parametrize("boundary", ["report", "review", "published", "dispatch"])
async def test_interruption_reuses_saved_browser_and_review_results(tmp_path, monkeypatch, boundary):
    workflow, rid, work, _, _, _, calls, browsers, _ = await ready(tmp_path, monkeypatch)
    checkpoint, finish = workflow.store.checkpoint, workflow.store.finish_recovery
    stopped = False

    def interrupt(rid, key, data):
        nonlocal stopped
        checkpoint(rid, key, data)
        matches = (key.startswith("recovery/prototype-rechecks/") and
            (boundary == "report" and "report" in data or boundary == "review" and "review" in data
             or boundary == "published" and data.get("phase") == "verified"))
        if matches and not stopped:
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
    prior_issues = workflow.store.get(rid, "issues")
    monkeypatch.setattr(workflow.store, "checkpoint", checkpoint)
    monkeypatch.setattr(workflow.store, "finish_recovery", finish)
    await workflow.recovery.resume(rid)
    assert len(browsers) == 1 and [s for s, _ in calls] == ["decision", "review"]
    assert workflow.store.get(rid, "develop/task/T1")["source_hash"] == tree_hash(work)
    assert workflow.store.get(rid, "recovery/active") is None
    if boundary in {"published", "dispatch"}:
        assert workflow.store.get(rid, "issues") == prior_issues


@pytest.mark.parametrize("failure_kind", ["browser", "review"])
async def test_failed_prototype_gate_retains_candidate_and_cannot_advance_task(tmp_path, monkeypatch, failure_kind):
    workflow, rid, _, _, _, _, calls, _, old = await ready(tmp_path, monkeypatch)
    previous = workflow.store.get(rid, "develop/task/T1")
    if failure_kind == "browser":
        async def failed(*args):
            raise PrototypeBrowserError("injected browser failure", [])
        monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", failed)
    else:
        workflow.agent.approved = False
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "develop/task/T1") == previous
    assert workflow.store.get(rid, "prototype_approval") == old
    assert workflow.store.get(rid, "recovery/active")["failure"]["component"] == "prototype_verification"
    assert all(c.get("review_kind") == "prototype" for s, c in calls if s == "review")
    assert any(i["scope"] == "prototype-review" for i in IssueLedger(workflow.store, workflow.artifacts).unresolved(rid))


async def test_unanswered_old_recovery_question_offers_new_action_without_reusing_old_denial(tmp_path, monkeypatch):
    workflow, rid, _, _, _, _, calls, browsers, _ = await ready(tmp_path, monkeypatch)
    original = workflow.approvals.recheck_binding
    monkeypatch.setattr(workflow.approvals, "recheck_binding", lambda *a: None)
    workflow.agent.choice = "ask_user"

    async def pause(question):
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    question = workflow.store.pending(rid)[0]
    monkeypatch.setattr(workflow.approvals, "recheck_binding", original)
    workflow.agent.choice = "prototype_recheck"
    await workflow.recovery.resume(rid)
    assert len(browsers) == 1 and [s for s, _ in calls] == ["decision", "decision", "review"]
    assert workflow.store.question(question["id"])["answer"] is None
    assert workflow.store.question(question["id"])["superseded"]
    assert not workflow.store.pending(rid)


@pytest.mark.parametrize("answered", [True, False])
async def test_old_human_answer_keeps_authority_and_wins_a_race_with_upgrade(tmp_path, monkeypatch, answered):
    workflow, rid, _, _, _, _, _, browsers, _ = await ready(tmp_path, monkeypatch)
    binding = workflow.approvals.recheck_binding
    monkeypatch.setattr(workflow.approvals, "recheck_binding", lambda *a: None)
    workflow.agent.choice = "ask_user"

    async def pause(question):
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    question = workflow.store.pending(rid)[0]
    monkeypatch.setattr(workflow.approvals, "recheck_binding", binding)
    workflow.agent.choice = "prototype_recheck"
    invoke = workflow.agent.invoke

    async def racing(stage, context, *args, **kwargs):
        if stage == "recovery_diagnosis":
            return Diagnosis(action="recheck", task_ids=["T1"], evidence_refs=["failure"], reason="Original scoped checks")
        if stage == "decision" and not answered:
            workflow.store.answer(question["id"], "n")
        return await invoke(stage, context, *args, **kwargs)

    workflow.agent.invoke = racing
    if answered:
        workflow.store.answer(question["id"], "y")
        await workflow.recovery.resume(rid)
        history = workflow.store.recovery_history(rid, "T1")["recent"]
        assert [item["action"]["action"] for item in history] == ["diagnose", "recheck"]
        assert all(item["actor"] == "human" for item in history)
    else:
        with pytest.raises(AA2Error, match="authorization changed"):
            await workflow.recovery.resume(rid)
        assert workflow.store.question(question["id"])["answer"] == "n"
    assert not browsers


@pytest.mark.parametrize("missing", ["consent", "candidate", "published"])
async def test_refresh_not_offered_without_verified_prerequisites(tmp_path, monkeypatch, missing):
    workflow, rid, work, contract, _, failure, _, browsers, _ = await ready(tmp_path, monkeypatch)
    if missing == "consent":
        workflow.store.supersede_questions(rid, "prototype-approval", include_answered=True)
    elif missing == "candidate":
        saved = workflow.store.get(rid, "develop/task/T1")
        workflow.store.checkpoint(rid, "develop/task/T1", {**saved, "phase": "implementing"})
    else:
        workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})

    async def choose(stage, context, *args, **kwargs):
        assert "prototype_recheck" not in context["decision_request"]["scope"]["candidates"]
        return DecisionResult(action="approve", selected_action="recheck", evidence_refs=["failure"],
                              reason="Existing checks", expected_result="Current checks")

    workflow.agent.invoke = choose
    decision, _ = await workflow.recovery_policy.authorize(rid, failure, workflow.store.get(rid, "recovery/active"),
        work, contract, adjudicate(failure), operation="eligibility", force_decision=True)
    assert decision.action == "recheck" and not browsers


@pytest.mark.parametrize("change", ["source", "consent", "actions", "plan", "published"])
async def test_changed_inputs_after_authorization_block_recapture(tmp_path, monkeypatch, change):
    workflow, rid, work, _, _, _, _, browsers, _ = await ready(tmp_path, monkeypatch)
    start = workflow.recovery_policy.start

    def drift(rid, key):
        start(rid, key)
        if change == "source":
            (work / "user-edit.txt").write_text("preserve")
        elif change == "consent":
            workflow.store.supersede_questions(rid, "prototype-approval", include_answered=True)
        elif change == "actions":
            prototype = workflow.store.get(rid, "prototype")
            prototype["browser_actions"][0]["value"] = "different"
            workflow.store.checkpoint(rid, "prototype", prototype)
        elif change == "plan":
            contract = workflow.store.get(rid, "plan")
            contract["document"] = "different"
            workflow.store.checkpoint(rid, "plan", contract)
        else:
            workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})

    monkeypatch.setattr(workflow.recovery_policy, "start", drift)
    with pytest.raises(AA2Error, match="inputs or human consent changed"):
        await workflow.recovery.resume(rid)
    assert not browsers and workflow.store.get(rid, "recovery/active")
    assert workflow.store.get(rid, "develop/task/T1")["phase"] == "rejected"
