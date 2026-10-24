import asyncio
import copy

import pytest
from prototype_review_fixtures import prototype_verdict
from test_preview_evidence_recovery import ready

from auto_agents_2.faults import StateConflict
from auto_agents_2.models import ReviewResult
from auto_agents_2.preview_evidence import PreviewEvidence
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


async def reserved_preview(tmp_path, monkeypatch, *, started=False):
    workflow, rid, work, plan, _, failure, calls, browsers, _ = await ready(tmp_path, monkeypatch)
    checkpoint = workflow.store.checkpoint
    reserved = []

    def interrupt(run, key, value):
        checkpoint(run, key, value)
        if key == "recovery/active" and value.get("decision", {}).get("action") == "preview_recheck" and not reserved:
            reserved.append(value["permit"])
            raise asyncio.CancelledError()

    monkeypatch.setattr(workflow.store, "checkpoint", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow.store, "checkpoint", checkpoint)
    permit = reserved[0]
    assert workflow.store.get(rid, permit)["phase"] == "reserved"
    if started:
        workflow.recovery_policy.start(rid, permit)
    approval = workflow.store.get(rid, "prototype_approval")
    modern = copy.deepcopy(approval["review"])
    # The permit never bound the review schema. This is its real serialized
    # pre-authority state; the independent probe also issues it with old code.
    approval["review"] = ReviewResult(approved=True, summary="Legacy prototype review").model_dump()
    workflow.store.checkpoint(rid, "prototype_approval", approval)
    return workflow, rid, work, plan, failure, calls, browsers, permit, modern


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("interruption", [None, "before", "after"])
async def test_reserved_legacy_preview_retires_then_separately_authorizes_prototype_review(
        tmp_path, monkeypatch, started, interruption):
    workflow, rid, work, plan, failure, calls, browsers, permit_key, _ = await reserved_preview(
        tmp_path, monkeypatch, started=started)
    old_permit = workflow.store.get(rid, permit_key)
    actions = workflow.store.get(rid, "prototype")["browser_actions"]
    human = workflow.approvals.human(rid, work, actions)
    original_task = workflow.store.get(rid, "develop/task/T1")
    source = tree_hash(work)
    keys = ["rounds/T1", "recovery/budgets/diagnosis/T1", "develop/done", "recovery/count/" + failure.id]
    retained = {key: workflow.store.get(rid, key) for key in keys}
    finish = workflow.store.finish_recovery

    def interrupt(*args, **kwargs):
        if interruption == "before":
            raise asyncio.CancelledError()
        value = finish(*args, **kwargs)
        if interruption == "after":
            raise asyncio.CancelledError()
        return value

    monkeypatch.setattr(workflow.store, "finish_recovery", interrupt)
    if interruption:
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    else:
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow.store, "finish_recovery", finish)
    if interruption == "before":
        await workflow.recovery.resume(rid)
    successor = workflow.store.get(rid, "recovery/active")
    assert successor["failure"]["component"] == "prototype_verification"
    assert successor["failure"]["kind"] == "review"
    assert successor["failure"]["evidence"]["execution_status"] == "not_started"
    assert successor["failure"]["evidence"]["previous_failure"]["id"] == failure.id
    assert successor["attempt"] == 1
    assert not successor.get("permit") and not successor.get("decision")
    completed = workflow.store.get(rid, permit_key)
    assert completed["phase"] == "completed" and completed["outcome"]["status"] == "rejected"
    assert {k: completed[k] for k in old_permit if k not in {"phase", "outcome", "outcome_hash"}} == {
        k: v for k, v in old_permit.items() if k not in {"phase", "outcome", "outcome_hash"}}
    assert workflow.store.get(rid, "develop/task/T1") == original_task
    assert not browsers and [s for s, _ in calls] == ["decision"]
    assert not workflow.store.pending(rid)
    assert not PreviewEvidence(workflow).binding(rid, work, plan)

    refreshed = []

    async def refresh(stage, context, *args, **kwargs):
        refreshed.append((stage, copy.deepcopy(context)))
        if stage == "recovery_diagnosis":
            assert context["failure"]["id"] == successor["failure"]["id"]
            assert context["failure"]["evidence"]["execution_status"] == "not_started"
            assert "prototype_recheck" in context["allowed_actions"]
            assert "preview_recheck" not in context["allowed_actions"]
            return Diagnosis(action="prototype_recheck", task_ids=["T1"], evidence_refs=["failure"],
                reason="The existing automatic review uses the obsolete protocol; recheck under unchanged consent")
        assert stage == "review" and context["review_kind"] == "prototype"
        assert context["browser_evidence"]
        return prototype_verdict(context)

    workflow.agent.invoke = refresh
    await workflow.recovery.resume(rid)
    assert [s for s, _ in refreshed] == ["recovery_diagnosis", "review"]
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, permit_key) == completed
    new_permit = workflow.store.get(rid, "develop/task/T1")["recovery_permit"]
    assert new_permit != permit_key
    receipt = workflow.store.get(rid, new_permit)
    assert receipt["action"]["action"] == "prototype_recheck"
    assert receipt["actor"] == "rule" and receipt["source_hash"] == source
    assert receipt["plan_hash"] == digest(plan.model_dump())
    assert receipt["task_scopes"] == old_permit["task_scopes"]
    assert receipt["prototype_binding"]["human"] == human
    assert PreviewEvidence(workflow).binding(rid, work, plan)
    assert workflow.approvals.human(rid, work, actions) == human
    assert tree_hash(work) == source and workflow.store.get(rid, "plan") == plan.model_dump()
    expected = {**retained, "recovery/budgets/diagnosis/T1": {"round": retained["recovery/budgets/diagnosis/T1"]["round"] + 1}}
    assert {key: workflow.store.get(rid, key) for key in keys} == expected
    assert workflow.store.get(rid, "develop/task/T1")["checks"] == original_task["checks"]
    assert not browsers and not workflow.store.pending(rid)


@pytest.mark.parametrize("drift", ["source", "plan", "human", "history", "modern_stale", "legacy_rejected", "journal", "comparison_call"])
async def test_review_upgrade_never_masks_changed_or_occupied_preview(tmp_path, monkeypatch, drift):
    workflow, rid, work, _, failure, calls, browsers, permit, modern = await reserved_preview(tmp_path, monkeypatch)
    key = "recovery/preview-rechecks/" + digest(permit)
    if drift == "source":
        (work / "calc.py").write_text("# user change\n")
    elif drift == "plan":
        plan = workflow.store.get(rid, "plan")
        plan["document"] += "\nChanged contract"
        workflow.store.checkpoint(rid, "plan", plan)
    elif drift == "human":
        workflow.store.supersede_questions(rid, "prototype-approval", include_answered=True)
    elif drift == "history":
        history = workflow.store.get(rid, "plan_revision/history/5")
        history["plan"]["preview"]["actions"].pop()
        workflow.store.checkpoint(rid, "plan_revision/history/5", history)
    elif drift in {"modern_stale", "legacy_rejected"}:
        approval = workflow.store.get(rid, "prototype_approval")
        if drift == "modern_stale":
            modern["basis_hash"] = "stale"
            approval["review"] = modern
        else:
            approval["review"]["approved"] = False
        workflow.store.checkpoint(rid, "prototype_approval", approval)
    elif drift == "journal":
        workflow.store.checkpoint(rid, key, {"phase": "comparing", "binding": workflow.store.get(rid, permit)["preview_binding"]})
    else:
        workflow.store.checkpoint(rid, "invocation/" + key + "/comparison", {"phase": "started"})
    with pytest.raises(StateConflict):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active")["failure"]["id"] == failure.id
    assert workflow.store.get(rid, permit)["phase"] != "completed"
    assert not browsers and [s for s, _ in calls] == ["decision"]


async def test_review_upgrade_cannot_replay_an_unknown_browser_execution(tmp_path, monkeypatch):
    workflow, rid, _, _, _, failure, _, browsers, _ = await ready(tmp_path, monkeypatch)

    async def interrupted(*args):
        browsers.append("started")
        raise asyncio.CancelledError()

    monkeypatch.setattr("auto_agents_2.preview_evidence.inspect_application", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    key = "recovery/preview-rechecks/" + digest(active["permit"])
    journal = workflow.store.get(rid, key)
    assert journal["phase"] == "started" and journal["execution"]
    approval = workflow.store.get(rid, "prototype_approval")
    approval["review"] = ReviewResult(approved=True, summary="Legacy review").model_dump()
    workflow.store.checkpoint(rid, "prototype_approval", approval)
    with pytest.raises(StateConflict):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, key) == journal
    assert workflow.store.get(rid, "recovery/active")["failure"]["id"] == failure.id
    assert workflow.store.get(rid, active["permit"])["phase"] == "started"
    assert browsers == ["started"]


async def test_legacy_preview_never_substitutes_for_denied_new_prototype_authorization(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, _, browsers, permit, _ = await reserved_preview(tmp_path, monkeypatch)
    await workflow.recovery.resume(rid)
    successor = workflow.store.get(rid, "recovery/active")
    old = workflow.store.get(rid, permit)
    authorize = workflow.recovery_policy.authorize
    calls = []

    async def gate(run, failure, active, workspace, contract, suggested, **kwargs):
        if suggested.action == "prototype_recheck":
            assert failure.id == successor["failure"]["id"]
            assert active.get("permit") is None
            raise StateConflict("New prototype authorization declined by current gate")
        return await authorize(run, failure, active, workspace, contract, suggested, **kwargs)

    async def invoke(stage, context, *args, **kwargs):
        calls.append(stage)
        if stage == "recovery_diagnosis":
            return Diagnosis(action="prototype_recheck", task_ids=["T1"], evidence_refs=["failure"],
                reason="Obsolete automatic review needs its own evidence refresh")
        assert stage == "review" and context["review_kind"] == "prototype"
        return prototype_verdict(context)

    workflow.agent.invoke = invoke
    monkeypatch.setattr(workflow.recovery_policy, "authorize", gate)
    with pytest.raises(StateConflict, match="New prototype authorization declined"):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    assert active["failure"]["id"] == successor["failure"]["id"]
    assert active.get("permit") is None
    assert calls == ["recovery_diagnosis"] and not browsers
    assert workflow.store.get(rid, permit) == old
    assert PreviewEvidence(workflow).binding(rid, work, plan) is None
    monkeypatch.setattr(workflow.recovery_policy, "authorize", authorize)
    await workflow.recovery.resume(rid)
    assert calls == ["recovery_diagnosis", "review"]
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, permit) == old
    assert PreviewEvidence(workflow).binding(rid, work, plan)
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T1")["round"] == 7
