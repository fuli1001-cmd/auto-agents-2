"""Rejected completed diagnoses consume the same bounded factual path."""
import json

import pytest
from authority_review_fixtures import recorded_review
from test_decisions import setup
from test_diagnosis_decisions import proposal_failure
from test_recovery_no_progress import LoopAgent
from test_recovery_policy import ready

from auto_agents_2.errors import InvalidStructuredResponse, PostCommitError, RecoveryPathExhausted
from auto_agents_2.recovery import Diagnosis

class InvalidAgent(LoopAgent):
    def __init__(self, semantic=False, alternative="repair"):
        super().__init__(alternative)
        self.semantic = semantic

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            self.diagnoses += 1
            if self.semantic:
                return Diagnosis(action="repair", task_ids=["T3"],
                    evidence_refs=["missing.source"], reason="Invalid factual reference")
            raise InvalidStructuredResponse("not safe to echo arbitrary model output")
        return await super().invoke(stage, context, schema, *args, **kwargs)

def ledgers(workflow, rid):
    with workflow.store.connect() as db:
        return [json.loads(r[0]) for r in db.execute(
            "select data from checkpoints where run_id=? and key like 'recovery/no-progress/diagnose/%'", (rid,))]

@pytest.mark.parametrize("semantic", [False, True])
async def test_two_rejected_completed_diagnoses_exclude_repetition(tmp_path, semantic):
    agent = InvalidAgent(semantic)
    workflow, rid, work, plan, failure = ready(tmp_path, agent, proposal_failure())
    workflow.store.checkpoint(rid, "recovery/budgets/diagnosis/T3", {"round": 20})
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    rows = ledgers(workflow, rid)
    assert len(rows) == 1 and len(rows[0]["operations"]) == 2
    for op, result in rows[0]["operations"].items():
        assert result["status"] == "rejected"
        assert result["evidence_ref"].startswith("recovery/diagnosis-rejections/")
        assert workflow.store.get(rid, result["evidence_ref"])
    await workflow.recovery.resume(rid)
    assert agent.diagnoses == 2 and "diagnose" not in agent.options[-1]
    assert workflow.store.get(rid, f"recovery/completed/{failure.id}/1")["decision"]["action"] == "repair"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3") == {"round": 22}
    assert not workflow.store.pending(rid)

async def test_same_rejections_stop_when_no_alternative_is_selected(tmp_path):
    agent = InvalidAgent(alternative="ask_user")
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    assert agent.diagnoses == 2 and not workflow.store.pending(rid)

@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("point", ["before", "after"])
async def test_atomic_rejection_survives_interrupt_and_does_not_rerun_completed_call(tmp_path, monkeypatch, point, semantic):
    agent = InvalidAgent(semantic)
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    original = workflow.store.checkpoints
    def interrupt(run, values, **kwargs):
        if any(k.startswith("recovery/diagnosis-rejections/") for k in values):
            if point == "after":
                original(run, values, **kwargs)
            raise PostCommitError("injected rejection handoff boundary")
        return original(run, values, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(workflow.store, "checkpoints", interrupt)
        with pytest.raises(PostCommitError):
            await workflow.recovery.resume(rid)
    assert agent.diagnoses == 1
    if point == "before":
        assert ledgers(workflow, rid) == []
        await workflow.recovery.resume(rid)
        assert agent.diagnoses == 1
    rows = ledgers(workflow, rid)
    assert len(rows) == 1 and len(rows[0]["operations"]) == 1
    op = next(iter(rows[0]["operations"]))
    active = workflow.store.get(rid, "recovery/active")
    assert "diagnosis_round" not in active
    permit = workflow.store.get(rid, active["diagnosis_permit"])
    assert permit["phase"] == "completed" and permit["outcome"]["status"] == "invalid"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3") == {"round": 1}

async def test_unpublished_delivery_rejections_share_the_same_guard(tmp_path):
    agent = InvalidAgent()
    workflow, rid = setup(tmp_path, agent)
    (tmp_path / "value.py").write_text("value = 1\n")
    feedback = {"review": recorded_review(workflow, rid, {
        "approved": False, "summary": "Need current evidence", "findings": []})}
    assert await workflow.recovery._delivery_decision(rid, tmp_path, feedback, ["value.py"]) is None
    assert await workflow.recovery._delivery_decision(rid, tmp_path, feedback, ["value.py"]) is None
    assert len(ledgers(workflow, rid)[0]["operations"]) == 2
    assert await workflow.recovery._delivery_decision(rid, tmp_path, feedback, ["value.py"]) == "repair"
    assert agent.diagnoses == 2 and "diagnose" not in agent.options[-1]

async def test_rejections_across_cycles_keep_same_factual_budget(tmp_path):
    agent = InvalidAgent()
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    workflow.recovery._finish(rid, active, {}, successor=active["failure"])
    await workflow.recovery.resume(rid)
    rows = ledgers(workflow, rid)
    assert len(rows) == 1 and len(rows[0]["operations"]) == 2
    await workflow.recovery.resume(rid)
    assert agent.diagnoses == 2


async def test_published_delivery_does_not_gain_unpublished_progress_policy(tmp_path):
    agent = InvalidAgent()
    workflow, rid = setup(tmp_path, agent)
    workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
    before = workflow.store.get(rid, "delivery/journal")
    assert await workflow.recovery._delivery_decision(rid, tmp_path, {"review": "failed"}, ["value.py"]) is None
    assert agent.diagnoses == 1 and ledgers(workflow, rid) == []
    assert workflow.store.get(rid, "delivery/journal") == before

@pytest.mark.parametrize("damage", [None, "source", "missing_rejection", "receipt_hash", "frozen_inputs"])
async def test_legacy_invalid_receipt_is_reused_without_backfill_or_new_call(tmp_path, monkeypatch, damage):
    from auto_agents_2.errors import AA2Error
    from auto_agents_2.util import digest
    agent = InvalidAgent()
    workflow, rid, work, _, _ = ready(tmp_path, agent, proposal_failure())
    captured = {}
    def old_completion(run, permit, rejection, **kwargs):
        captured.update(permit=permit, rejection=kwargs["evidence_ref"], operation=kwargs["operation"])
        workflow.recovery_policy.complete(run, permit, {"status": "invalid", "feedback": rejection})
        workflow.store.checkpoint(run, kwargs["evidence_ref"], rejection)
        raise PostCommitError("legacy completion before handoff; no enrollment")
    with monkeypatch.context() as patch:
        patch.setattr(workflow.recovery, "_reject_diagnosis", old_completion)
        with pytest.raises(PostCommitError):
            await workflow.recovery.resume(rid)
    receipt = workflow.store.get(rid, captured["permit"])
    if damage == "source":
        (work / "user-change.txt").write_text("preserve")
    elif damage == "missing_rejection":
        workflow.store.invalidate(rid, [captured["rejection"]])
    elif damage == "receipt_hash":
        # Store writes recompute completed outcome hashes. Inject a corrupt read,
        # leaving the immutable real fixture receipt and writer protection intact.
        original_get = workflow.store.get
        def corrupt_read(run, key, default=None):
            value = original_get(run, key, default)
            return {**value, "outcome_hash": "bad"} if key == captured["permit"] else value
        monkeypatch.setattr(workflow.store, "get", corrupt_read)
    elif damage == "frozen_inputs":
        key = "recovery/invocation-inputs/" + digest(["recovery_diagnosis", captured["operation"]])
        frozen = workflow.store.get(rid, key)
        workflow.store.checkpoint(rid, key, {**frozen, "context": {**frozen["context"], "failure": {}}})
    if damage:
        with pytest.raises(AA2Error):
            await workflow.recovery.resume(rid)
    else:
        await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, captured["permit"]) == receipt
        assert "diagnosis_round" not in workflow.store.get(rid, "recovery/active")
    assert agent.diagnoses == 1 and ledgers(workflow, rid) == []
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3") == {"round": 1}

async def test_a_later_eligibility_rejection_cannot_replace_validated_receipt_or_count(tmp_path, monkeypatch):
    from test_diagnosis_decisions import pending_completed_diagnosis
    from auto_agents_2.errors import AA2Error
    workflow, rid, _, _, active, _ = await pending_completed_diagnosis(tmp_path, monkeypatch)
    receipt = workflow.store.get(rid, active["diagnosis_permit"])
    before = ledgers(workflow, rid)
    import auto_agents_2.recovery_controller as module
    original = module.adjudicate
    def reject(*args, **kwargs):
        if kwargs.get("diagnosis") is not None:
            raise AA2Error("current eligibility rejected the retained diagnosis")
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "adjudicate", reject)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, active["diagnosis_permit"]) == receipt
    assert ledgers(workflow, rid) == before
