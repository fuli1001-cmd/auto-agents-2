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


@pytest.mark.parametrize("entry", ["initial", "recheck"])
@pytest.mark.parametrize("interrupt_archive", [False, True])
async def test_superseded_review_remains_reachable_after_delivery_cleanup(tmp_path, monkeypatch, entry, interrupt_archive):
    import json

    from test_prototype_recovery import prototype_fixture

    from auto_agents_2.models import IntakeResult
    from auto_agents_2.util import digest
    from auto_agents_2.workspace import git

    if entry == "initial":
        workflow, rid, _, _, calls = prototype_fixture(tmp_path, monkeypatch)
        work = tmp_path
        spec = IntakeResult(summary="UI", document="UI", frontend=True)
        workflow.store.checkpoint(rid, "clarify", spec.model_dump())
        async def pause(question):
            raise asyncio.CancelledError()
        workflow.question_handler = pause
        with pytest.raises(asyncio.CancelledError):
            await workflow._prepare_prototype(rid, work, spec, spec.model_dump(), {}, "UI")
        key = "prototype/pending"
        async def resume():
            await workflow._prepare_prototype(rid, work, spec, spec.model_dump(), {}, "UI")
        async def approve(question):
            return "1"
        workflow.question_handler = approve
    else:
        workflow, rid, work, _, _, _, calls, _, _ = await ready(tmp_path, monkeypatch)
        checkpoint = workflow.store.checkpoint
        stopped = []
        def pause_after_review(run, key, value):
            checkpoint(run, key, value)
            if key.startswith("recovery/prototype-rechecks/") and "review" in value and not stopped:
                stopped.append(key)
                raise asyncio.CancelledError()
        monkeypatch.setattr(workflow.store, "checkpoint", pause_after_review)
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
        monkeypatch.setattr(workflow.store, "checkpoint", checkpoint)
        key = stopped[0]
        async def resume():
            await workflow.recovery.resume(rid)
    saved = workflow.store.get(rid, key)
    saved["review"] = {"approved": True, "summary": "Unique superseded legacy review " + entry, "findings": []}
    workflow.store.checkpoint(rid, key, saved)
    archive_hash = digest(json.dumps(saved, ensure_ascii=True, sort_keys=True))
    history_key = "recovery/prototype-review-history/" + archive_hash
    source = tree_hash(work)
    rounds = workflow.store.get(rid, "rounds/T1")
    checkpoint = workflow.store.checkpoint
    interrupted = []
    def stop_after_root(run, checkpoint_key, value):
        checkpoint(run, checkpoint_key, value)
        if interrupt_archive and checkpoint_key == history_key and not interrupted:
            interrupted.append(checkpoint_key)
            raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, "checkpoint", stop_after_root)
    if interrupt_archive:
        with pytest.raises(asyncio.CancelledError):
            await resume()
        assert workflow.store.get(rid, key)["review"] == saved["review"]
    await resume()
    assert tree_hash(work) == source and workflow.store.get(rid, "rounds/T1") == rounds
    human = workflow.store.get(rid, "prototype_approval")["human"]
    archive = workflow.artifacts.root/archive_hash[:2]/archive_hash
    assert archive.exists()
    # This is a synthetic completion receipt for the disposable fixture only.
    # Run real finish_delivery, including candidate compaction and artifact GC.
    workflow.store.checkpoint(rid, "candidate/cleanup-sentinel", {"present": True})
    receipt = {"outcome":"DELIVERED", "target":"main", "candidate":git(tmp_path,"rev-parse","HEAD"),
               "source_hash":source, "runtime":{"resources":[]}, "evidence":{}, "owned_refs":{}}
    receipt["digest"] = digest(receipt)
    workflow.store.complete_delivery(rid, receipt)
    workflow.resources.finish_delivery(rid)
    assert not workflow.store.run(rid)["data"]["cleanup_pending"]
    assert workflow.store.get(rid, "candidate/cleanup-sentinel") is None
    assert archive.exists(), "Normal successful cleanup must retain the superseded independent review"
    ref = workflow.store.get(rid, history_key)
    assert workflow.artifacts.json(ref) == saved
    report = workflow.artifacts.json(saved["report"]) if entry == "recheck" else {"results":saved["visuals"]}
    for row in report["results"]:
        image = row.get("image_artifact")
        if image:
            assert (workflow.artifacts.root/image["hash"][:2]/image["hash"]).is_file()
    assert workflow.store.get(rid, "prototype_approval")["human"] == human
    workflow.approvals.archive_review(rid, saved)
    with workflow.store.connect() as db:
        assert db.execute("SELECT count(*) FROM checkpoints WHERE run_id=? AND key=?",(rid,history_key)).fetchone()[0] == 1
