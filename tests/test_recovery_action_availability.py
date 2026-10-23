import copy

import pytest
from test_diagnosis_decisions import proposal_failure
from test_preview_evidence_recovery import ready as preview_ready
from test_recovery_policy import ready

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.errors import AA2Error
from auto_agents_2.faults import StateConflict
from auto_agents_2.recovery import Diagnosis, adjudicate
from auto_agents_2.workspace import tree_hash


@pytest.mark.parametrize("action", ["prototype_recheck", "preview_recheck"])
async def test_ineligible_diagnosis_is_corrected_without_human_or_evidence_bypass(tmp_path, action):
    workflow, rid, work, plan, failure = ready(tmp_path, record=proposal_failure())
    source = tree_hash(work)
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        assert stage == "recovery_diagnosis"
        calls.append(copy.deepcopy(context))
        assert action not in context["allowed_actions"]
        assert "recheck" in context["allowed_actions"]
        if len(calls) > 1:
            assert "unavailable" in context["diagnosis_feedback"]["validation_error"]
        return Diagnosis(action=action if len(calls) == 1 else "recheck", task_ids=[failure.task_id],
            evidence_refs=["failure.evidence"], reason="Collect declared evidence")

    workflow.agent.invoke = invoke
    await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    assert active["diagnosis_errors"] == 1 and "diagnosis_round" not in active
    assert not workflow.store.pending(rid)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    saved = workflow.store.get(rid, "develop/task/" + failure.task_id)
    assert saved["phase"] == "reverify" and not saved.get("checks")
    assert tree_hash(work) == source
    assert len(calls) == 2 and not workflow.store.pending(rid)


async def test_completed_old_ineligible_diagnosis_keeps_its_receipt_and_gets_new_validation(tmp_path, monkeypatch):
    import auto_agents_2.recovery_controller as module

    workflow, rid, work, plan, failure = ready(tmp_path, record=proposal_failure())
    original = module.adjudicate
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        calls.append(stage)
        return Diagnosis(action="preview_recheck" if len(calls) == 1 else "recheck",
            task_ids=[failure.task_id], evidence_refs=["failure.evidence"], reason="Collect evidence")

    def old_adjudicate(*args, **kwargs):
        if kwargs.get("evidence_context"):
            kwargs["evidence_context"] = {k: v for k, v in kwargs["evidence_context"].items() if k != "allowed_actions"}
        return original(*args, **kwargs)

    workflow.agent.invoke = invoke
    monkeypatch.setattr(module, "adjudicate", old_adjudicate)
    with pytest.raises(StateConflict, match="accepted preview"):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    permit_key = active["diagnosis_permit"]
    receipt = workflow.store.get(rid, permit_key)
    assert receipt["phase"] == "completed" and receipt["outcome"]["status"] == "validated"
    monkeypatch.setattr(module, "adjudicate", original)
    await workflow.recovery.resume(rid)
    assert calls == ["recovery_diagnosis"]
    assert workflow.store.get(rid, permit_key) == receipt
    assert workflow.store.get(rid, "recovery/active")["diagnosis_errors"] == 1
    await workflow.recovery.resume(rid)
    assert calls == ["recovery_diagnosis", "recovery_diagnosis"]
    assert workflow.store.get(rid, permit_key) == receipt
    assert workflow.store.get(rid, "recovery/active") is None
    assert not workflow.store.pending(rid)


async def test_diagnosis_and_authorization_share_preview_prerequisites(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, failure, _, _, _ = await preview_ready(tmp_path, monkeypatch)
    policy = workflow.recovery_policy
    allowed = policy.diagnosis_actions(rid, failure, work, plan)
    assert {"prototype_recheck", "preview_recheck"} <= allowed.keys()
    choices, _, _ = policy.available_actions(rid, failure, work, plan, adjudicate(failure), include_declared_checks=True)
    expected = policy.proposal_actions(choices)
    assert all(allowed[key] == expected[key] for key in choices if key != "diagnose")
    assert all(row["authority"] == "proposal_only" for row in allowed.values())
    view = ContextPlanner(workflow.artifacts).prepare("recovery_diagnosis", {"allowed_actions": allowed}, work)
    assert view.payload["allowed_actions"] == allowed
    workflow.store.checkpoint(rid, "develop/task/T1", {"phase": "implementing", "workspace": str(work)})
    allowed = policy.diagnosis_actions(rid, failure, work, plan)
    assert "preview_recheck" not in allowed and "prototype_recheck" not in allowed
    workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
    allowed = policy.diagnosis_actions(rid, failure, work, plan)
    assert not {"plan_patch", "design", "prototype", "preview_recheck"} & allowed.keys()


def test_diagnosis_cannot_select_unknown_action_or_refresh_a_different_task():
    from test_recovery_contracts import failure, plan

    record, contract = failure(), plan()
    context = {"failure": record.model_dump(), "allowed_actions": {"preview_recheck": {}}}
    with pytest.raises(AA2Error, match="current retained task"):
        adjudicate(record, plan=contract, evidence_context=context,
            diagnosis=Diagnosis(action="preview_recheck", task_ids=["T1"], evidence_refs=["failure"], reason="Wrong task"))
