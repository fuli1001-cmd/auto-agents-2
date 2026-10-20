import asyncio
import json
import sqlite3
from contextlib import contextmanager

import pytest
from test_delivery import commit, fixture
from test_diagnostic_lifecycle import setup
from test_workflow import CodingAgent, engine

from auto_agents_2.artifact_gc import recover_diagnostic_holds
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.faults import StateConflict
from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import CheckResult, Command, Finding, ReviewResult, TaskSpec
from auto_agents_2.review_evidence import ReviewEvidence, consumer_key, is_review_consumer, review_consumer
from auto_agents_2.verification import VerificationRefreshNeeded, verification_operation


def observation(artifacts, text="explicitly synthetic review screenshot fixture"):
    image = artifacts.put(text, "test_diagnostic")
    report = artifacts.put('<testsuite tests="1"/>', "junit")
    return [{"attachments": [{"path": "required-view.png", "artifact": image}], "report": report}], image


@pytest.mark.parametrize("key", ["develop/review-evidence/T1", "verify/review-evidence", "delivery/review-evidence"])
def test_pending_review_survives_collection_then_consumption_releases_it(tmp_path, key):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    owners = ReviewEvidence(store)
    assert owners.retain(rid, key, checks, updates={"verification/batches/fixture": {"phase": "completed", "checks": checks}})
    resources.collect_artifacts()
    assert artifacts.bytes(image)
    owners.complete(rid, key, checks, updates={"verify/review": {"approved": True}})
    resources.collect_artifacts()
    assert artifacts.released(image)
    assert artifacts.read(checks[0]["report"])
    assert store.get(rid, key) == {"phase": "consumed", "observation": store.get(rid, key)["observation"]}


@pytest.mark.parametrize("status", ["RUNNING", "PAUSED", "WAITING_USER", "CANCELLED", "SUCCEEDED"])
def test_crash_recovery_preserves_pending_consumers_until_terminal(tmp_path, status):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    ReviewEvidence(store).retain(rid, consumer_key(), checks)
    store.update(rid, status=status)
    recover_diagnostic_holds(store)
    resources.collect_artifacts()
    assert artifacts.released(image) == (status in {"CANCELLED", "SUCCEEDED"})
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM diagnostic_holds").fetchone()


@pytest.mark.parametrize("prefix", ["develop", "verify", "delivery"])
def test_abandoned_stage_does_not_leave_a_consumer(tmp_path, prefix):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    key = prefix + "/review-evidence" + ("/T1" if prefix == "develop" else "")
    ReviewEvidence(store).retain(rid, key, checks)
    store.invalidate(rid, [prefix])
    resources.collect_artifacts()
    assert artifacts.released(image)


def test_replacement_is_bounded_and_stale_completion_cannot_release_new_observation(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    owners, key = ReviewEvidence(store), consumer_key()
    old, old_image = observation(artifacts, "synthetic old observation")
    owners.retain(rid, key, old)
    new, new_image = observation(artifacts, "synthetic replacement observation")
    owners.retain(rid, key, new)
    with pytest.raises(StateConflict, match="observation changed"):
        owners.complete(rid, key, old, updates={"verify/review": {"stale": True}})
    assert store.get(rid, "verify/review") is None
    resources.collect_artifacts()
    assert artifacts.released(old_image) and artifacts.bytes(new_image)
    owners.complete(rid, key, new)
    resources.collect_artifacts()
    assert artifacts.released(new_image)


@pytest.mark.parametrize("transition", ["publish", "consume"])
def test_transaction_failure_keeps_previous_owner_and_result(tmp_path, transition):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    owners, key = ReviewEvidence(store), consumer_key()
    if transition == "consume":
        owners.retain(rid, key, checks)
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_consumer BEFORE INSERT ON checkpoints "
                   "WHEN NEW.key='verify/review-evidence' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        if transition == "publish":
            owners.retain(rid, key, checks, updates={"verification/batches/fixture": {"phase": "completed", "checks": checks}})
        else:
            owners.complete(rid, key, checks, updates={"verify/review": {"approved": True}})
    assert store.get(rid, "verification/batches/fixture") is None
    assert store.get(rid, "verify/review") is None
    resources.collect_artifacts()
    assert artifacts.bytes(image)  # Producer or durable pending consumer still owns it.
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_consumer")
    if transition == "publish":
        owners.retain(rid, key, checks)
    owners.complete(rid, key, checks, updates={"verify/review": {"approved": True}})
    resources.collect_artifacts()
    assert artifacts.released(image)


def test_consumer_acquired_between_gc_mark_and_unlink_cancels_retirement(tmp_path, monkeypatch):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    store.checkpoint(rid, "verify/checks", checks)
    original, entered = store.connect, False

    @contextmanager
    def interleaved():
        nonlocal entered
        first = not entered
        entered = True
        with original() as db:
            yield db
        if first:
            assert ReviewEvidence(store).retain(rid, consumer_key(), checks)

    monkeypatch.setattr(store, "connect", interleaved)
    resources.collect_artifacts()
    assert artifacts.bytes(image)
    ReviewEvidence(store).complete(rid, consumer_key(), checks)
    resources.collect_artifacts()
    assert artifacts.released(image)


def test_rejected_review_transfers_evidence_to_issue_until_resolution(tmp_path):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    owners = ReviewEvidence(store)
    owners.retain(rid, consumer_key(), checks)
    ledger = IssueLedger(store, artifacts)
    review = ReviewResult(approved=False, summary="fixture rejection", findings=[Finding(
        severity="major", location="view", description="layout failed", evidence="screenshot", resolution="repair layout")])
    ledger.record(rid, "final-review", review, {"checks": checks})
    owners.complete(rid, consumer_key(), checks, updates={"verify/review": review.model_dump()})
    resources.collect_artifacts()
    assert artifacts.bytes(image)
    ledger.record(rid, "final-review", ReviewResult(approved=True, summary="resolved"), {})
    resources.collect_artifacts()
    assert artifacts.released(image)


@pytest.mark.parametrize("loss", ["retired", "missing", "corrupt"])
def test_reacquisition_requires_available_evidence_without_disguising_corruption(tmp_path, loss):
    store, rid, artifacts, resources = setup(tmp_path)
    checks, image = observation(artifacts)
    store.checkpoint(rid, "verify/checks", checks)
    path = artifacts.root / image["hash"][:2] / image["hash"]
    if loss == "retired":
        resources.collect_artifacts()
        assert not ReviewEvidence(store).retain(rid, consumer_key(), checks)
    else:
        if loss == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt synthetic fixture")
        with pytest.raises(ValueError):
            ReviewEvidence(store).retain(rid, consumer_key(), checks)
    assert store.get(rid, consumer_key()) is None


def instrument_checks(workflow, monkeypatch, captures):
    original = workflow.verifier._check

    async def checked(*args, **kwargs):
        result = await original(*args, **kwargs)
        key = review_consumer.get()
        if key:
            ref = workflow.artifacts.put("synthetic view " + str(len(captures)), "test_diagnostic")
            captures.append(ref)
            result = result.model_copy(update={"attachments": [{"path": "required.png", "artifact": ref}]})
        return result

    published = workflow.verifier._publish_batch

    def collect_after_publish(rid, key, batch):
        published(rid, key, batch)
        workflow.resources.collect_artifacts()
        for check in batch["checks"]:
            for attachment in check["attachments"]:
                assert workflow.artifacts.bytes(attachment["artifact"])

    capture_recovery = workflow.verifier._release_resolved_capture

    def collect_during_capture_recovery(rid, result):
        # Mirrors the real capture-recovery publisher that adopts the producer hold.
        workflow.store.checkpoint(rid, "evidence/capture/fixture", {"resolved_by": result.model_dump()})
        workflow.resources.collect_artifacts()
        for attachment in result.attachments:
            assert workflow.artifacts.bytes(attachment["artifact"])
        capture_recovery(rid, result)

    monkeypatch.setattr(workflow.verifier, "_check", checked)
    monkeypatch.setattr(workflow.verifier, "_publish_batch", collect_after_publish)
    monkeypatch.setattr(workflow.verifier, "_release_resolved_capture", collect_during_capture_recovery)


def reviewing_agent(project, captures, operations, *, advance=False):
    class Reviewing(CodingAgent):
        advanced = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            rows = context.get("checks") or context.get("evidence")
            if stage in {"review", "verify"} and isinstance(rows, list):
                workflow.resources.collect_artifacts()
                for row in rows:
                    for attachment in row.get("attachments", []):
                        assert workflow.artifacts.bytes(attachment["artifact"])
                operations.append(operation)
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if advance and operation == "final-review" and not self.advanced:
                self.advanced = True
                (project / "user.txt").write_text("concurrent work")
                commit(project)
            return result

    workflow = engine(project, Reviewing())
    return workflow


async def test_workflow_handoff_task_final_reuse_and_reintegration(tmp_path, monkeypatch):
    project, requirements = fixture(tmp_path)
    captures, operations = [], []
    workflow = reviewing_agent(project, captures, operations, advance=True)
    instrument_checks(workflow, monkeypatch, captures)
    (project / "initial.txt").write_text("initial")
    commit(project)
    first = await workflow.start(str(requirements))
    assert first["status"] == "SUCCEEDED"
    assert {"develop/task/T1/review", "final-review", "reintegration-review/1"} <= set(operations)
    assert all(workflow.artifacts.released(ref) for ref in captures)
    second = await workflow.start(str(requirements))
    assert second["status"] == "SUCCEEDED" and second["data"]["outcome"] == "NO_CHANGES"
    assert "no-change-review" in operations
    assert all(workflow.artifacts.released(ref) for ref in captures)
    for rid in (first["id"], second["id"]):
        with workflow.store.connect() as db:
            rows = list(db.execute("SELECT key,data FROM checkpoints WHERE run_id=?", (rid,)))
        assert all(json.loads(row["data"])["phase"] == "consumed" for row in rows if is_review_consumer(row["key"]))
    assert review_consumer.get() is None and verification_operation.get() is None


@pytest.mark.parametrize("legacy_defaults", [False, True])
async def test_review_cancel_and_restart_preserves_exact_checks_until_consumed(tmp_path, monkeypatch, legacy_defaults):
    project, requirements = fixture(tmp_path)
    captures, operations = [], []
    workflow = reviewing_agent(project, captures, operations)
    workflow.agent.interrupt_review = True
    instrument_checks(workflow, monkeypatch, captures)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    run = workflow.store.current()
    assert run["status"] == "PAUSED"
    saved = workflow.store.get(run["id"], "develop/task/T1")["checks"]
    if legacy_defaults:
        prior = workflow.store.get(run["id"], "develop/task/T1")
        for row in prior["checks"]:
            row.pop("timed_out")
        workflow.store.checkpoint(run["id"], "develop/task/T1", prior)
    recover_diagnostic_holds(workflow.store)
    workflow.resources.collect_artifacts()
    assert all(workflow.artifacts.bytes(ref) for ref in captures)
    resumed = reviewing_agent(project, captures, operations)
    instrument_checks(resumed, monkeypatch, captures)
    result = await resumed.start()
    assert result["status"] == "SUCCEEDED"
    assert [CheckResult.model_validate(row).model_dump() for row in
            resumed.store.get(run["id"], "develop/task/T1")["checks"]] == saved
    assert all(resumed.artifacts.released(ref) for ref in captures)


async def test_completed_legacy_batch_requires_real_refresh_for_new_review(tmp_path, monkeypatch):
    project, _ = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    ref = workflow.artifacts.put("synthetic legacy screenshot", "test_diagnostic")
    calls = []

    async def checked(command, *args):
        calls.append(command)
        return CheckResult(argv=command.argv, cwd=".", exit_code=0, output="fixture", duration=0,
            source_hash=args[2], environment_hash="host", attachments=[{"path": "view.png", "artifact": ref}])

    monkeypatch.setattr(workflow.verifier, "_check", checked)
    command = Command(argv=["true"])
    token = verification_operation.set("legacy")
    try:
        checks = await workflow.verifier.run([command], project)
        workflow.resources.collect_artifacts()
        assert workflow.artifacts.released(ref)
        consumer = review_consumer.set(consumer_key())
        try:
            with pytest.raises(VerificationRefreshNeeded, match="review_evidence_released"):
                await workflow.verifier.run([command], project)
        finally:
            review_consumer.reset(consumer)
        assert len(calls) == 1
        assert workflow.store.get(rid, "verification/batches/legacy")["checks"] == [c.model_dump() for c in checks]
    finally:
        verification_operation.reset(token)


@pytest.mark.parametrize("interruption", [asyncio.CancelledError, RuntimeError])
async def test_partial_check_cancellation_retains_progress_without_replaying_batch(tmp_path, monkeypatch, interruption):
    project, _ = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    ref = workflow.artifacts.put("synthetic partial screenshot", "test_diagnostic")
    calls = []

    async def checked(command, *args):
        calls.append(command)
        if len(calls) == 2:
            raise interruption()
        return CheckResult(argv=command.argv, cwd=".", exit_code=0, output="fixture", duration=0,
            source_hash=args[2], environment_hash="host", attachments=[{"path": "view.png", "artifact": ref}])

    monkeypatch.setattr(workflow.verifier, "_check", checked)
    commands = [Command(argv=["true"]), Command(argv=["false"])]
    token, consumer = verification_operation.set("partial"), review_consumer.set(consumer_key())
    try:
        with pytest.raises(interruption):
            await workflow.verifier.run(commands, project)
        recover_diagnostic_holds(workflow.store)
        workflow.resources.collect_artifacts()
        assert workflow.artifacts.bytes(ref)
        cancelled = interruption is asyncio.CancelledError
        assert workflow.store.get(rid, "verification/batches/partial")["phase"] == ("interrupted" if cancelled else "started")
        with pytest.raises(VerificationRefreshNeeded if cancelled else StateConflict):
            await workflow.verifier.run(commands, project)
        assert len(calls) == 2
        workflow.store.update(rid, status="CANCELLED")
        workflow.resources.collect_artifacts()
        assert workflow.artifacts.released(ref)
    finally:
        verification_operation.reset(token)
        review_consumer.reset(consumer)


def test_runtime_does_not_claim_a_model_review_consumer():
    task = TaskSpec(id="T1", title="fixture", description="fixture", requirement_ids=["R1"],
                    allowed_paths=["source.py"], acceptance=["fixture"], checks=[Command(argv=["true"])])
    assert consumer_key(task) == "develop/review-evidence/T1"
    assert consumer_key(phase="reuse") == consumer_key()
    assert consumer_key(phase="runtime") is None


@pytest.mark.parametrize("rename", [False, True])
def test_plan_replacement_releases_only_affected_task_consumers(tmp_path, rename):
    from test_recovery_contracts import apply, failure, plan, prepare_activation

    from auto_agents_2.config import Cleanup
    from auto_agents_2.plan_revision import PlanPatch
    from auto_agents_2.resources import ResourceManager

    store, rid = prepare_activation(tmp_path)
    artifacts, owners = ArtifactStore(store.root), ReviewEvidence(store)
    refs = {}
    for task_id in ("T1", "T3", "T4"):
        checks, refs[task_id] = observation(artifacts, "synthetic plan task " + task_id)
        owners.retain(rid, "develop/review-evidence/" + task_id, checks)
    if rename:
        original = plan()
        renamed = original.tasks[2].model_copy(deep=True, update={"id": "T3_RENAMED"})
        child = original.tasks[3].model_copy(deep=True, update={"depends_on": [renamed.id]})
        patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason="fixture rename", document="renamed",
                          upsert_tasks=[renamed, child], remove_tasks=["T3"])
        candidate, affected, reopen = apply(original, patch)
        store.checkpoint(rid, "plan_revision/journal/rename", {
            "phase": "prepared", "base_revision": 1, "previous_plan": original.model_dump(), "plan": candidate.model_dump(),
            "patch": patch.model_dump(), "affected": sorted(affected), "reopen": sorted(reopen), "reason": patch.reason})
    store.activate_plan_patch(rid, "rename" if rename else "patch")
    ResourceManager(store, Cleanup(min_free_bytes=0)).collect_artifacts()
    assert artifacts.bytes(refs["T1"])
    assert artifacts.released(refs["T3"]) and artifacts.released(refs["T4"])
