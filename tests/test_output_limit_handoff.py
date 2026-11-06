"""Explicit lifecycle handoff preserves rejected work and uses ordinary recovery."""
import asyncio
import json
from pathlib import Path

import pytest

from auto_agents_2.errors import AA2Error, BusyProject, RecoveryPathExhausted
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import ImplementationResult
from auto_agents_2.output_limit_recovery import (
    HANDOFF_HISTORY, HANDOFF_KEY, KEY, Incident, authorize_handoff, normal_recovery_handoff,
    prepare_handoff, raw_hash,
)
from auto_agents_2.util import digest, file_lock
from test_output_limit_recovery import OP, admit, invoke, setup


def stopped(tmp_path):
    f = setup(tmp_path)
    admit(f)
    f.incident.stop("task_failure_RecoveryNeeded")
    f.store.checkpoint(f.rid, OP, {**f.store.get(f.rid, OP), "phase": "rejected",
                                  "candidate_error": "fixture rejected candidate"})
    f.store.checkpoints(f.rid, {"recovery/budgets/diagnosis/T1": {"round": 18},
                              "recovery/count/fixture": 18})
    return f


def closed_repair(f):
    """Use the existing typed permission/outcome, as retained by a real failed task."""
    from auto_agents_2.recovery_policy import ActionPermit
    key, cycle_id = "recovery/permits/fixture-previous", "fixture-previous-cycle"
    outcome = {"expected_tasks": [f.task.id], "tasks": {f.task.id: {"status": "failed", "kind": "checks"}}}
    permit = ActionPermit(cycle_id=cycle_id, owner=f.task.origin_id or f.task.id,
        action={"action": "repair", "task_ids": [f.task.id]}, actor="rule", phase="completed",
        source_hash=f.store.get(f.rid, OP)["source_hash"], plan_hash=digest(f.store.get(f.rid, "plan")),
        task_scopes={f.task.id: f.task.allowed_paths}, outcome=outcome, outcome_hash=digest(outcome))
    f.store.checkpoints(f.rid, {key: permit.model_dump(),
        "recovery/cycles/" + cycle_id: {"phase": "completed", "permits": [key], "outcomes": outcome},
        OP: {**f.store.get(f.rid, OP), "recovery_permit": key}})
    return key, "recovery/cycles/" + cycle_id


def request_file(f):
    request = prepare_handoff(f.project, run_id=f.rid,
                              authorization_reference="fixture explicit withdrawal of the one-time restriction")
    path = f.project.parent / "normal-recovery-request.json"
    path.write_text(json.dumps(request))
    return request, path, raw_hash(path.read_bytes())


def publish(f, path, sha):
    return authorize_handoff(f.project, path, sha, confirm_normal_recovery=True)


def test_explicit_handoff_keeps_old_evidence_counters_and_pause_then_replays_once(tmp_path):
    f = stopped(tmp_path)
    prior_permit, prior_cycle = closed_repair(f)
    keys = [KEY, OP, "rounds/T1", "recovery/budgets/diagnosis/T1", "recovery/count/fixture", "develop/done",
            prior_permit, prior_cycle]
    before = {key: f.store.get(f.rid, key) for key in keys}
    run = f.store.run(f.rid)
    events = len(f.store.events(f.rid))
    request, path, sha = request_file(f)
    assert len(f.store.events(f.rid)) == events  # Preparing is read-only.
    with pytest.raises(AA2Error, match="Explicit confirmation"):
        authorize_handoff(f.project, path, sha)
    assert f.incident.active()
    result = publish(f, path, sha)
    assert result["execution_policy"] == "normal_recovery" and not result["replayed"]
    assert {key: f.store.get(f.rid, key) for key in keys} == before
    assert f.store.run(f.rid) == run
    assert not f.incident.active()
    assert f.store.get(f.rid, "recovery/active") is None
    receipt = normal_recovery_handoff(f.store, f.rid)
    archive = f.store.get(f.rid, receipt["archive_key"])
    assert archive["checkpoints"][OP] == before[OP]
    assert archive["checkpoints"][KEY] == before[KEY]
    assert archive["checkpoints"][prior_permit] == before[prior_permit]
    assert archive["checkpoints"][prior_cycle] == before[prior_cycle]
    assert archive["source_history"]["source_hash"] == request["source_hash"]
    manifest = f.router.store.root / "artifacts" / archive["source_history"]["artifact"]["hash"][:2] / archive["source_history"]["artifact"]["hash"]
    assert json.loads(manifest.read_text())["source_hash"] == request["source_hash"]
    after = len(f.store.events(f.rid))
    assert publish(f, path, sha)["replayed"]
    assert len(f.store.events(f.rid)) == after
    # Legal later work must not resurrect the old execution limit or refill it.
    f.store.checkpoint(f.rid, "rounds/T1", {"round": 29, "stalled": 12})
    f.store.checkpoint(f.rid, OP, {"phase": "implementing", "source_hash": "new-source"})
    (f.project / "later.py").write_text("value = 1\n")
    assert not Incident(f.store, f.rid).active()
    assert publish(f, path, sha)["replayed"]
    assert f.store.get(f.rid, KEY) == before[KEY]


@pytest.mark.parametrize("change", ["source", "counter", "question", "run", "config"])
def test_changed_request_cannot_release_the_stopped_incident(tmp_path, change):
    f = stopped(tmp_path)
    _, path, sha = request_file(f)
    if change == "source":
        (f.project / "new.py").write_text("changed\n")
    elif change == "counter":
        f.store.checkpoint(f.rid, "rounds/T1", {"round": 29, "stalled": 12})
    elif change == "question":
        f.store.request(f.rid, "fixture/new-question", "new external input", "fixture")
    elif change == "run":
        f.store.update(f.rid, status="RUNNING")
    else:
        body = f.router.config.load().model_dump(mode="json")
        body["config_revision"] += 1
        f.router.config.path.write_text(json.dumps(body))
    with pytest.raises(AA2Error):
        publish(f, path, sha)
    assert f.store.get(f.rid, HANDOFF_KEY) is None
    assert not [event for event in f.store.events(f.rid) if event["kind"] == "output_limit.handed_off"]
    assert f.incident.active()


def test_busy_controller_and_unresolved_call_do_not_handoff(tmp_path):
    f = stopped(tmp_path)
    _, path, sha = request_file(f)
    with file_lock(f.store.root / "controller.lock", blocking=False):
        with pytest.raises(BusyProject):
            publish(f, path, sha)
    record = f.store.get(f.rid, KEY)
    record["calls"]["correction"] = {"state": "claimed", "attempt": "uncertain"}
    f.store.checkpoint(f.rid, KEY, record)  # Corrupt/incomplete fixture; production never refills it.
    with pytest.raises(AA2Error, match="uncertain outcome"):
        prepare_handoff(f.project, run_id=f.rid, authorization_reference="fixture explicit decision")
    assert f.store.get(f.rid, HANDOFF_KEY) is None


@pytest.mark.parametrize("state", ["missing", "started", "no-outcome", "cycle-open"])
def test_unfinished_previous_repair_cannot_be_consumed_as_a_new_permission(tmp_path, state):
    f = stopped(tmp_path)
    key, cycle_key = closed_repair(f)
    with f.store.connect() as db:
        if state == "missing":
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key=?", (f.rid, key))
        else:
            target = cycle_key if state == "cycle-open" else key
            value = json.loads(db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (f.rid, target)).fetchone()[0])
            if state in {"started", "cycle-open"}:
                value["phase"] = "started"
            else:
                value["outcome"]["tasks"] = {}
                value["outcome_hash"] = digest(value["outcome"])
            db.execute("UPDATE checkpoints SET data=? WHERE run_id=? AND key=?", (json.dumps(value), f.rid, target))
    with pytest.raises(AA2Error, match="(repair permission|recovery cycle)"):
        request_file(f)
    assert f.store.get(f.rid, HANDOFF_KEY) is None


def test_handoff_and_archive_are_immutable_and_damaged_receipts_fail_closed(tmp_path):
    f = stopped(tmp_path)
    _, path, sha = request_file(f)
    publish(f, path, sha)
    receipt = f.store.get(f.rid, HANDOFF_KEY)
    archive = f.store.get(f.rid, receipt["archive_key"])
    for key, value in [(HANDOFF_KEY, {**receipt, "mode": "other"}),
                       (receipt["archive_key"], {**archive, "checkpoints": {}})]:
        with pytest.raises(StateConflict, match="immutable"):
            f.store.checkpoint(f.rid, key, value)
    for altered in [{**receipt, "mode": "other"}, {**receipt, "version": "unknown"},
                    {**receipt, "archive_sha256": "0" * 64},
                    {**receipt, "request": {**receipt["request"], "incident_request_hash": "0" * 64}}]:
        with f.store.connect() as db:
            db.execute("UPDATE checkpoints SET data=? WHERE run_id=? AND key=?", (json.dumps(altered), f.rid, HANDOFF_KEY))
        with pytest.raises(AA2Error, match="handoff is missing or changed"):
            f.incident.active()
    with f.store.connect() as db:
        db.execute("UPDATE checkpoints SET data=? WHERE run_id=? AND key=?", (json.dumps(receipt), f.rid, HANDOFF_KEY))
    assert not f.incident.active()
    other = json.loads(path.read_text())
    other["authorization_reference"] = "a different authorization"
    path.write_text(json.dumps(other))
    with pytest.raises(AA2Error, match="different normal recovery authorization"):
        publish(f, path, raw_hash(path.read_bytes()))


def test_cas_failure_does_not_publish_half_a_handoff(tmp_path, monkeypatch):
    from auto_agents_2.store import Store
    f = stopped(tmp_path)
    _, path, sha = request_file(f)
    original = Store._put
    def interrupted(self, db, rid, key, value):
        if key == HANDOFF_KEY:
            raise RuntimeError("fixture publication interruption")
        return original(self, db, rid, key, value)
    monkeypatch.setattr(Store, "_put", interrupted)
    with pytest.raises(RuntimeError, match="publication interruption"):
        publish(f, path, sha)
    assert f.store.get(f.rid, HANDOFF_KEY) is None
    with f.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id=? AND key LIKE ?",
                          (f.rid, HANDOFF_HISTORY + "%")).fetchone()[0] == 0
    assert f.incident.active()


@pytest.mark.parametrize("state", ["unknown-pending", "running-process", "invalid-process"])
def test_unknown_provider_or_cleanup_cannot_be_released(tmp_path, state):
    f = stopped(tmp_path)
    if state == "unknown-pending":
        f.store.checkpoint(f.rid, "infra/fixture/other/codex", {"pending": True})
    else:
        root = f.store.root / "processes"
        root.mkdir(exist_ok=True)
        value = {} if state == "invalid-process" else {"version": 3, "status": "running",
            "pid": 1, "start": "1", "cwd": str(f.project), "paths": [str(f.project)]}
        (root / "fixture.json").write_text(json.dumps(value))
    with pytest.raises(AA2Error, match="(uncertain pending|cleanup is unconfirmed)"):
        request_file(f)
    assert f.store.get(f.rid, HANDOFF_KEY) is None


@pytest.mark.parametrize("case", ["success", "different-provider", "conflicting-terminal", "late-spawn"])
def test_terminal_historical_pending_is_archived_without_erasure_and_rechecked(tmp_path, case):
    from auto_agents_2.artifacts import ArtifactStore
    f = stopped(tmp_path)
    operation, attempt = "fixture/prior-diagnosis", "fixture-completed"
    infra = "infra/" + operation + "/codex"
    result = {"action": "diagnose", "reason": "historical result"}
    artifact = ArtifactStore(f.store.root).put(result, "recovery_diagnosis")
    cleanup = f.store.root / "processes" / "fixture.json"
    f.store.checkpoints(f.rid, {infra: {"pending": True, "failures": 0, "grant": 0},
        "invocation/" + operation: {"attempt": attempt, "identity": "fixture-input", "result": result,
            "artifact": artifact, "cleanup_record": str(cleanup)},
        "provider-result/" + operation: {"attempt": attempt, "artifact": artifact}})
    provider = "different-provider" if case == "different-provider" else "codex"
    f.store.event(f.rid, "agent.started", {"operation": operation, "provider": provider, "attempt": attempt})
    if case == "conflicting-terminal":
        f.store.event(f.rid, "agent.finished", {"attempt": attempt, "complete": False, "outcome": "error"})
    f.store.event(f.rid, "agent.finished", {"attempt": attempt, "complete": True, "outcome": "success"})
    if case == "late-spawn":
        f.store.event(f.rid, "agent.spawned", {"attempt": attempt, "pid": 123})
    if case != "success":
        with pytest.raises(AA2Error, match="uncertain pending"):
            request_file(f)
        assert f.store.get(f.rid, HANDOFF_KEY) is None
        return
    request, path, sha = request_file(f)
    assert request["accounted_pending"][infra]["attempt"] == attempt
    # A newly created uncertain process invalidates the reviewed request.
    cleanup.parent.mkdir(exist_ok=True)
    cleanup.write_text(json.dumps({"version": 3, "status": "starting", "cwd": str(f.project), "paths": [str(f.project)]}))
    with pytest.raises(AA2Error, match="cleanup is unconfirmed"):
        publish(f, path, sha)
    cleanup.unlink()
    publish(f, path, sha)
    receipt = f.store.get(f.rid, HANDOFF_KEY)
    archive = f.store.get(f.rid, receipt["archive_key"])
    assert archive["checkpoints"][infra]["pending"] is True
    assert f.store.get(f.rid, infra)["pending"] is True


@pytest.mark.parametrize("race", ["pending", "process"])
def test_publication_rechecks_new_pending_and_cleanup_after_source_capture(tmp_path, monkeypatch, race):
    from auto_agents_2.source_history import SourceHistory
    f = stopped(tmp_path)
    _, path, sha = request_file(f)
    collect = SourceHistory.collect_task
    def capture(self, *args, **kwargs):
        history = collect(self, *args, **kwargs)
        if race == "pending":
            f.store.checkpoint(f.rid, "infra/new/provider/codex", {"pending": True})
        else:
            root = f.store.root / "processes"
            root.mkdir(exist_ok=True)
            (root / "new.json").write_text(json.dumps({"version": 3, "status": "starting",
                "cwd": str(f.project), "paths": [str(f.project)]}))
        return history
    monkeypatch.setattr(SourceHistory, "collect_task", capture)
    with pytest.raises(AA2Error):
        publish(f, path, sha)
    assert f.store.get(f.rid, HANDOFF_KEY) is None
    assert f.incident.active()


async def test_completed_native_call_is_archived_and_cannot_be_replayed_as_new_work(tmp_path):
    f = setup(tmp_path)
    admit(f)
    await invoke(f)
    old = f.store.get(f.rid, KEY)
    f.incident.stop("task_failure_RecoveryNeeded")
    f.store.checkpoint(f.rid, OP, {**f.store.get(f.rid, OP), "phase": "rejected",
                                  "candidate_error": "fixture rejects returned proposal"})
    _, path, sha = request_file(f)
    publish(f, path, sha)
    assert f.incident.replay_call("implement", f.context, ImplementationResult, f.project, OP, f.router.sandbox) is None
    assert f.store.get(f.rid, KEY)["calls"] == old["calls"]


async def test_handoff_resumes_through_real_checks_and_ordinary_recovery(tmp_path):
    from auto_agents_2.decisions import DecisionResult
    from test_output_limit_recovery import approve_runtime_messages
    from test_output_limit_workflow import IncidentAgent, attach_incident, native_correction_fixture
    from test_workflow import engine

    class Agent(IncidentAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "decision":
                request = context["decision_request"]
                selected = "diagnose" if "diagnose" in request["scope"]["candidates"] else next(iter(request["scope"]["candidates"]))
                return DecisionResult(action="approve", reason="fixture evidence-driven recovery", evidence_refs=["failure"],
                                      selected_action=selected, expected_result="repair and verify in the original scope")
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    agent = Agent("failed-check")
    workflow = engine(project, agent)
    agent.workflow = workflow
    native_correction_fixture(workflow, agent, tmp_path)
    agent.native = None  # Deterministic transport; actual pytest still executes.
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    rid = workflow.store.current(unfinished=True)["id"]
    attach_incident(workflow, agent, rid)
    async with approve_runtime_messages(workflow.store, rid):
        assert (await workflow.start())["status"] == "PAUSED"
    incident = workflow.store.get(rid, KEY)
    roles = list(agent.incident_roles)
    assert roles == ["correction", "protected_review"]
    from types import SimpleNamespace
    from auto_agents_2.models import TaskSpec
    fixture = SimpleNamespace(store=workflow.store, rid=rid,
        task=TaskSpec.model_validate(workflow.store.get(rid, "plan")["tasks"][0]))
    previous_key, previous_cycle = closed_repair(fixture)
    previous = workflow.store.get(rid, previous_key)
    family = digest([fixture.task.origin_id or fixture.task.id, "checks", sorted(fixture.task.requirement_ids)])
    workflow.store.checkpoints(rid, {"recovery/count/" + family: 18,
        "recovery/budgets/diagnosis/" + (fixture.task.origin_id or fixture.task.id): {"round": 18}})
    request = prepare_handoff(project, run_id=rid, authorization_reference="fixture return to ordinary recovery")
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(request))
    authorize_handoff(project, path, raw_hash(path.read_bytes()), confirm_normal_recovery=True)
    result = await workflow.start()
    assert result["status"] == "SUCCEEDED"
    assert workflow.store.get(rid, KEY) == incident
    assert workflow.store.get(rid, previous_key) == previous
    assert workflow.store.get(rid, "recovery/count/" + family) == 19
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/" + (fixture.task.origin_id or fixture.task.id))["round"] >= 18
    assert agent.incident_roles == roles
    assert workflow.store.get(rid, "rounds/T1")["round"] == 29
    assert workflow.store.get(rid, "task_state/T1")["evidence"] == "valid"
    events = workflow.store.events(rid)
    assert any(event["kind"] == "recovery.requested" for event in events)
    assert any(event["kind"] == "recovery.decided" and event["data"]["action"] == "repair" for event in events)
    assert "return a + b" in (project / "calc.py").read_text()
