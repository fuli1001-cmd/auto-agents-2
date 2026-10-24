"""Prototype review authority effects across recovery and delivery lifecycles."""
import asyncio

import pytest
from prototype_review_fixtures import prototype_verdict
from test_prototype_evidence_recovery import ready

from auto_agents_2.prototype_review import PrototypeFinding
from auto_agents_2.workspace import tree_hash


@pytest.mark.parametrize("boundary", [None, "before", "after"])
async def test_recheck_clarification_completes_old_action_before_new_authorization(tmp_path, monkeypatch, boundary):
    workflow, rid, work, _, _, failure, _, browsers, old = await ready(tmp_path, monkeypatch)
    original = "Prototype approval requires a state snapshot; clarify its format."
    workflow.store.checkpoint(rid, "capture", {"artifact": workflow.artifacts.put(original, "requirements")})
    invoke = workflow.agent.invoke
    reviews = []

    async def review_gap(stage, context, *args, **kwargs):
        if stage == "review" and context.get("review_kind") == "prototype":
            reviews.append(context)
            finding = PrototypeFinding(severity="major", location="original requirement", description="Format is ambiguous",
                evidence="The original requirement does not define the snapshot format", resolution="Clarify that format",
                criterion_id="user:original", kind="evidence_gap", observation="Screenshots exist but required format is ambiguous",
                evidence_refs=["browser", "requirements"], owner="clarify", requested_capability="none",
                source_quote="Prototype approval requires a state snapshot")
            return prototype_verdict(context, approved=False, findings=[finding])
        return await invoke(stage, context, *args, **kwargs)

    monkeypatch.setattr(workflow.agent, "invoke", review_gap)
    before = {key: workflow.store.get(rid, key) for key in (
        "rounds/T1", "recovery/budgets/diagnosis/T1", "plan", "develop/done", "develop/task/T1")}
    source = tree_hash(work)
    active = workflow.store.get(rid, "recovery/active")
    finish = workflow.store.finish_recovery
    interrupted = []

    def interrupted_finish(*args, **kwargs):
        if boundary and not interrupted and kwargs.get("replan"):
            interrupted.append(boundary)
            if boundary == "after":
                finish(*args, **kwargs)
            raise asyncio.CancelledError()
        return finish(*args, **kwargs)

    monkeypatch.setattr(workflow.store, "finish_recovery", interrupted_finish)
    if boundary:
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
        assert interrupted == [boundary]
    else:
        await workflow.recovery.resume(rid)
    # Production prioritizes recovery before replan dispatch. This must now
    # terminate the old action, including a crash before or after its transaction.
    if workflow.store.get(rid, "recovery/active"):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    pending = workflow.store.get(rid, "replanning/pending")
    assert pending["stage"] == "clarify" and pending["controller_owned"] and not pending["granted"]
    completed = workflow.store.get(rid, f"recovery/completed/{failure.id}/{active['attempt']}")
    permit = workflow.store.get(rid, completed["permit"])
    assert permit["phase"] == "completed" and permit["outcome"]["status"] == "gate_requested"
    assert workflow.store.get(rid, "recovery/cycles/" + permit["cycle_id"])["phase"] == "completed"
    assert len(reviews) == len(browsers) == 1
    assert tree_hash(work) == source and workflow.store.get(rid, "prototype_approval") == old
    assert {key: workflow.store.get(rid, key) for key in before} == before
    assert workflow.store.get(rid, "recovery/count/" + failure.id) == active["attempt"]
    authorization = []

    class AwaitNewAuthorization(Exception):
        pass

    async def require_new_permission(*args, **kwargs):
        authorization.append(kwargs)
        raise AwaitNewAuthorization()

    monkeypatch.setattr(workflow.recovery_policy, "authorize", require_new_permission)
    with pytest.raises(AwaitNewAuthorization):
        await workflow._resume_replan(rid)
    assert len(authorization) == 1 and set(authorization[0]["candidates"]) == {"clarify"}
    assert authorization[0]["controller_owned"] and not workflow.store.get(rid, "replanning/pending")["granted"]
