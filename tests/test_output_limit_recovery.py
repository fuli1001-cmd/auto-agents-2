"""Explicit incident authority, native-call occupancy and fail-closed restart."""
import asyncio
import json
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_provider_terminal_state import COMPLETE, START, TURN, fixture_router

from auto_agents_2.errors import AA2Error, PostCommitError, RecoveryPathExhausted
from auto_agents_2.models import Change, Command, ImplementationResult, ReviewResult, TaskSpec
from auto_agents_2.output_limit_recovery import (
    CALL, CORRECTION, KEY, MESSAGE, Incident, ModelCall, authorize, prepare, raw_hash,
)
from auto_agents_2.providers import invocation_identity
from auto_agents_2.resources import ResourceManager
from auto_agents_2.store import Store
from auto_agents_2.util import digest, file_lock
from auto_agents_2.workspace import apply_prepared, prepare_changes, tree_hash


OP = "develop/task/T1"
RESULT = ImplementationResult(summary="complete fixture", changes=[Change(path="app.py", content="answer = 42\n")])
FAILED = {"type": "turn.failed", "error": {"message": MESSAGE, "codexErrorInfo": "other", "additionalDetails": None}}


def seed(router, project, store, rid, context_extra=None):
    """Historical native records are test data, never an authority shortcut."""
    binary = Path(router.config.resolve("implement", router.config.load().active_provider)["binary"])
    body = binary.read_text()
    if "incident-version-fixture" not in body:
        first, _, rest = body.partition("\n")
        binary.write_text(first + "\nimport sys\n# incident-version-fixture\n"
            "if sys.argv[1:] == ['--version']:\n    print('codex-cli 0.165.0')\n    raise SystemExit(0)\n" + rest)
    task = TaskSpec(id="T1", title="answer", description="Return 42", requirement_ids=["R1"],
                    allowed_paths=["app.py"], acceptance=["answer is 42"], checks=[Command(argv=["true"])])
    context = {"task": task.model_dump(), **(context_extra or {})}
    cfg = router.config.load()
    logical = invocation_identity("implement", context, ImplementationResult, project, cfg)
    execution = invocation_identity("implement", context, ImplementationResult, project, cfg,
                                    execution=router.execution_identity("implement"))
    store.update(rid, status="PAUSED", stage="implement")
    store.checkpoints(rid, {"plan": {"tasks": [task.model_dump()]}, OP: {
        "phase": "implementing", "workspace": str(project), "source_hash": tree_hash(project)},
        "rounds/T1": {"round": 28, "stalled": 11}, "develop/done": [],
        "infra/" + OP + "/codex": {"failures": cfg.execution.infra_retries + 1, "grant": 0,
            "pending": False, "error_kind": "unavailable", "error_input": logical},
        "materials/" + OP: {"original": "retained"}, "session/original": "original-thread"})
    store.event(rid, "agent.started", {"attempt": "original", "operation": OP})
    store.event(rid, "agent.finished", {"attempt": "original", "complete": False, "outcome": "error"})
    finished = store.events(rid)[-1]["seq"]
    native = store.root / "sessions" / rid / "original"
    native.mkdir(parents=True)
    resource = ResourceManager(store, cfg.cleanup).register(native, "session", rid)
    metadata = {"native_cache": {"phase": "active", "attempt": "original", "operation": OP,
                                "session_key": "session/original", "input_identity": execution}}
    with store.connect() as db:
        db.execute("UPDATE resources SET data=? WHERE id=?", (json.dumps(metadata), resource))
    with sqlite3.connect(native / "thread_history_1.sqlite") as db:
        db.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT,"
                   "started_at INTEGER,completed_at INTEGER,rollout_ordinal INTEGER,PRIMARY KEY(thread_id,turn_id))")
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)", ("original-thread", "original-turn", "failed",
                   json.dumps(FAILED["error"]), int(time.time()), int(time.time()), 10))
    effective = router.config.resolve("implement", "codex")
    with sqlite3.connect(native / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,model TEXT,reasoning_effort TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,?)", ("original-thread", effective["model"], effective["effort"]))
    question = store.request(rid, "provider/" + OP, "Provider failed", digest("old-gate"))
    fields = dict(run_id=rid, operation=OP, failed_attempt="original", question_id=question["id"],
                  resource_id=resource, thread_id="original-thread", turn_id="original-turn", finished_seq=finished,
                  authorization_reference="fixture explicit user approval")
    request = prepare(project, **fields)
    path = project.parent / "reviewed-request.json"
    path.write_text(json.dumps(request))
    return SimpleNamespace(router=router, project=project, store=store, rid=rid, context=context, task=task,
        request=request, fields=fields, path=path, sha=raw_hash(path.read_bytes()), native=native,
        incident=Incident(store, rid), engine=SimpleNamespace(config=router.config,
            authority=SimpleNamespace(identity=lambda _: {})))


def setup(tmp_path, *, events=None, response=None, retries=2):
    router, project, store, rid = fixture_router(tmp_path, events or [START, TURN, COMPLETE],
        file_response=response if response is not None else json.dumps(RESULT.model_dump()), retries=retries)
    return seed(router, project, store, rid)


def admit(f):
    return authorize(f.project, f.path, f.sha, confirm_one_call=True,
                     confirm_two_material_probes=bool(f.request.get("material_probe_authorization_reference")))


@asynccontextmanager
async def approve_runtime_messages(store, rid):
    """An operator submits the real bounded message; production never auto-approves."""
    from auto_agents_2.output_limit_runtime import publish_activation, quote_paths
    async def operator():
        while True:
            record = store.get(rid, KEY)
            for nonce, entry in (record or {}).get("runtime_quotes", {}).items():
                if not entry["consumed"] and not entry["revoked"]:
                    path, message = quote_paths(store, record["request"], nonce)
                    if path.is_file() and not message.exists():
                        publish_activation(store.root.parent, path, entry["sha256"], "fixture operator decision")
            await asyncio.sleep(0.02)
    task = asyncio.create_task(operator())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def double_launch(scope, router, context, schema, workspace, identity):
    """Only deterministic workflow transport doubles use a synthetic launch receipt."""
    from auto_agents_2.models import strict_schema
    from auto_agents_2.output_limit_runtime import VERSION, require_binding
    bound = require_binding(scope.incident)
    group = "implement" if scope.role == "correction" else "review"
    return {"version": VERSION, "runtime_nonce": bound["nonce"], "runtime_hash": digest(bound["runtime"]),
            "controller": bound["runtime"]["controller"], "profile": bound["runtime"]["profiles"][group],
            "role": scope.role, "stage": scope.stage, "operation": scope.operation,
            "process_record": str(scope.incident.store.root / "processes" / ("fixture-" + scope.role + ".json")),
            "logical_input": invocation_identity(scope.stage, context, schema, workspace, router.config.load()),
            "execution_input": identity, "schema_hash": digest(strict_schema(schema)), "source_hash": tree_hash(workspace)}


async def invoke(f, router=None):
    with file_lock(f.store.root / "controller.lock", blocking=False):
        async with approve_runtime_messages(f.store, f.rid):
            with f.incident.workflow_call(f.engine, "implement", f.context, ImplementationResult, f.project, OP):
                return await (router or f.router).invoke("implement", f.context, ImplementationResult, f.project, f.rid, OP)


def occupied(f):
    return f.store.get(f.rid, KEY)["calls"]


async def test_admission_is_exact_cas_and_replay_never_replenishes(tmp_path):
    f = setup(tmp_path)
    infra = f.store.get(f.rid, f.request["infra_key"])
    assert admit(f)["state"] == "active"
    with f.store.connect() as db:
        row = db.execute("SELECT answer,superseded FROM questions WHERE id=?", (f.fields["question_id"],)).fetchone()
    assert tuple(row) == (None, 1)
    assert f.store.get(f.rid, f.request["infra_key"]) == infra
    assert f.store.get(f.rid, "rounds/T1") == {"round": 28, "stalled": 11}
    await invoke(f)
    original_attempt = occupied(f)["correction"]["attempt"]
    assert admit(f)["replayed"] and occupied(f)["correction"]["attempt"] == original_attempt
    for _ in range(2):
        with pytest.raises(RecoveryPathExhausted):
            await invoke(f)
    assert occupied(f)["correction"]["attempt"] == original_attempt
    before = f.store.get(f.rid, KEY)
    f.incident.stop("another reason")
    assert f.store.get(f.rid, KEY) == before


def test_nonretryable_terminal_gate_need_not_exhaust_all_configured_retries(tmp_path):
    f = setup(tmp_path, retries=3)
    infra = {**f.store.get(f.rid, f.request["infra_key"]), "failures": 2}
    f.store.checkpoint(f.rid, f.request["infra_key"], infra)
    f.request = prepare(f.project, **f.fields)
    f.path.write_text(json.dumps(f.request))
    f.sha = raw_hash(f.path.read_bytes())
    assert admit(f)["state"] == "active"
    assert f.store.get(f.rid, f.request["infra_key"]) == infra


@pytest.mark.parametrize("failures", [0, True, -1])
def test_admission_requires_a_positive_integer_original_failure_count(tmp_path, failures):
    f = setup(tmp_path)
    f.store.checkpoint(f.rid, f.request["infra_key"], {
        **f.store.get(f.rid, f.request["infra_key"]), "failures": failures})
    with pytest.raises(AA2Error, match="original failed provider checkpoint"):
        prepare(f.project, **f.fields)
    assert f.store.get(f.rid, KEY) is None


@pytest.mark.parametrize("mutation", ["question", "other-question", "infra", "rounds", "source", "later-attempt", "later-turn", "mixed-error"])
def test_admission_rejects_stale_or_mixed_evidence_without_closing_gate(tmp_path, mutation):
    f = setup(tmp_path)
    if mutation == "question":
        with f.store.connect() as db:
            db.execute("UPDATE questions SET answer='n' WHERE id=?", (f.fields["question_id"],))
    elif mutation == "other-question":
        f.store.request(f.rid, "unrelated", "Separate decision", "other")
    elif mutation in {"infra", "rounds"}:
        key = f.request["infra_key"] if mutation == "infra" else "rounds/T1"
        f.store.checkpoint(f.rid, key, {**f.store.get(f.rid, key), "changed": True})
    elif mutation == "source":
        (f.project / "user.py").write_text("preserve = True")
    elif mutation == "later-attempt":
        f.store.event(f.rid, "agent.started", {"attempt": "later-auth-failure", "operation": OP})
    elif mutation == "later-turn":
        with sqlite3.connect(f.native / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)",
                       ("original-thread", "later", "inProgress", None, int(time.time()), None, 11))
    else:
        with sqlite3.connect(f.native / "thread_history_1.sqlite") as db:
            db.execute("UPDATE thread_turns SET error_json=?", (json.dumps({**FAILED["error"], "codexErrorInfo": "contextWindowExceeded"}),))
    with pytest.raises(AA2Error):
        admit(f)
    assert f.store.get(f.rid, KEY) is None
    with f.store.connect() as db:
        assert db.execute("SELECT superseded FROM questions WHERE id=?", (f.fields["question_id"],)).fetchone()[0] == 0


def test_admission_requires_confirmation_exact_bytes_and_free_controller_lock(tmp_path):
    f = setup(tmp_path)
    with pytest.raises(AA2Error):
        authorize(f.project, f.path, f.sha)
    with pytest.raises(AA2Error):
        authorize(f.project, f.path, "0" * 64, confirm_one_call=True)
    with file_lock(f.store.root / "controller.lock", blocking=False):
        with pytest.raises(AA2Error):
            admit(f)
    assert f.store.get(f.rid, KEY) is None


@pytest.mark.parametrize("mode", ["terminal", "transient", "schema", "spawn-uncertain"])
async def test_one_native_failure_stops_without_retry_fallback_or_new_grant(tmp_path, monkeypatch, mode):
    events = [START, TURN, FAILED] if mode == "terminal" else [START, TURN, COMPLETE]
    if mode == "transient":
        events = [START, TURN, {"type": "turn.failed", "error": {"message": "503 connection timeout"}}]
    f = setup(tmp_path, events=events, response="{" if mode == "schema" else None)
    admit(f)
    if mode == "spawn-uncertain":
        original_create = asyncio.create_subprocess_exec
        async def uncertain(*args, **kwargs):
            if str(Path(f.router.config.resolve("implement", "codex")["binary"])) in args and "--version" not in args:
                raise OSError("fixture lost spawn acknowledgement")
            return await original_create(*args, **kwargs)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", uncertain)
    infra = f.store.get(f.rid, f.request["infra_key"])
    with pytest.raises(RecoveryPathExhausted):
        await invoke(f)
    for _ in range(2):
        with pytest.raises(RecoveryPathExhausted):
            Incident(Store(f.project), f.rid).reconcile(f.router.sandbox)
    assert len(occupied(f)) == 1 and occupied(f)["correction"]["state"] == "claimed"
    assert f.store.get(f.rid, f.request["infra_key"]) == infra
    assert f.store.get(f.rid, "invocation/" + OP) is None
    events = f.store.events(f.rid)
    assert len([e for e in events if e["kind"] == "agent.started"]) == 2  # original + correction
    assert not any(e["kind"] == "agent.retrying" for e in events)
    assert f.store.get(f.rid, "materials/" + OP) == {"original": "retained"}
    assert f.store.get(f.rid, "session/original") == "original-thread"


async def test_success_and_postcommit_restart_reuse_exact_receipt_without_redispatch(tmp_path, monkeypatch):
    f = setup(tmp_path)
    admit(f)
    complete = f.store.complete_output_limit_call
    def interrupted(*args, **kwargs):
        raise OSError("fixture completion notification lost")
    monkeypatch.setattr(f.store, "complete_output_limit_call", interrupted)
    with pytest.raises(PostCommitError):
        await invoke(f)
    assert occupied(f)["correction"]["state"] == "claimed"
    monkeypatch.setattr(f.store, "complete_output_limit_call", complete)
    restarted = Incident(Store(f.project), f.rid)
    restarted.reconcile(f.router.sandbox)
    assert occupied(f)["correction"]["state"] == "completed"
    assert f.store.get(f.rid, "invocation/" + OP)["result"] == RESULT.model_dump()
    restarted.reconcile(f.router.sandbox)
    assert len([e for e in f.store.events(f.rid) if e["kind"] == "agent.started"]) == 2
    with pytest.raises(RecoveryPathExhausted):
        await invoke(f)
    assert len([e for e in f.store.events(f.rid) if e["kind"] == "agent.spawned"]) == 1


@pytest.mark.parametrize("drift", ["task", "source", "receipt", "cleanup", "cas"])
async def test_published_engine_result_never_overwrites_drift_or_unconfirmed_cleanup(tmp_path, monkeypatch, drift):
    from auto_agents_2.engine import WorkflowEngine
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, COMPLETE],
        file_response=json.dumps(RESULT.model_dump()), retries=2)
    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox)
    # Engine construction establishes its browser environment before the original
    # invocation is frozen, just as it does in the actual controller lifecycle.
    f = seed(router, project, store, rid)
    admit(f)
    async with approve_runtime_messages(f.store, f.rid):
        with file_lock(f.store.root / "controller.lock", blocking=False), f.incident.workflow_call(
                f.engine, "implement", f.context, ImplementationResult, f.project, OP):
            assert await workflow._invoke_once("implement", f.context, ImplementationResult, f.project, f.rid, OP,
                                               interactive_retry=False) == RESULT
    receipt = f.store.get(f.rid, "invocation/" + OP)
    assert receipt["provider_receipt"]["attempt"] == occupied(f)["correction"]["attempt"]
    assert not f.store.get(f.rid, OP).get("implementation")
    original = f.store.get(f.rid, f.request["infra_key"])
    if drift == "task":
        f.store.checkpoint(f.rid, OP, {**f.store.get(f.rid, OP),
            "implementation": RESULT.model_copy(update={"summary": "different candidate"}).model_dump()})
    elif drift == "source":
        (f.project / "user.py").write_text("preserve = True")
    elif drift == "receipt":
        receipt["provider_receipt"]["attempt"] = "another-attempt"
        f.store.checkpoint(f.rid, "invocation/" + OP, receipt)
    elif drift == "cleanup":
        def unconfirmed(*args):
            raise AA2Error("fixture cleanup not confirmed")
        monkeypatch.setattr(f.router.sandbox, "confirm_record", unconfirmed)
    else:
        restore = f.store.restore_output_limit_proposal
        def conflict(*args):
            f.store.checkpoint(f.rid, OP, {**f.store.get(f.rid, OP), "concurrent_change": "preserve"})
            return restore(*args)
        monkeypatch.setattr(f.store, "restore_output_limit_proposal", conflict)
    before = f.store.get(f.rid, OP)
    with pytest.raises(AA2Error):
        f.incident.reconcile(f.router.sandbox)
    expected = {**before, "concurrent_change": "preserve"} if drift == "cas" else before
    assert f.store.get(f.rid, OP) == expected
    assert f.store.get(f.rid, f.request["infra_key"]) == original
    assert len([e for e in f.store.events(f.rid) if e["kind"] == "agent.spawned"]) == 1
    assert not any(e["kind"] == "output_limit.proposal_restored" for e in f.store.events(f.rid))


async def test_confirmed_native_pause_is_spent_and_never_uses_cancellation_resume(tmp_path, monkeypatch):
    from test_provider_pause_accounting import cancel_when_ready, fixture
    router, project, store, rid, _, ready = fixture(tmp_path, monkeypatch)
    f = seed(router, project, store, rid)
    admit(f)
    await cancel_when_ready(invoke(f), ready)
    assert occupied(f)["correction"]["state"] == "claimed"
    with pytest.raises(RecoveryPathExhausted):
        Incident(Store(project), rid).reconcile(router.sandbox)
    assert len([e for e in store.events(rid) if e["kind"] == "agent.started"]) == 2
    assert not store.get(rid, f.request["infra_key"]).get("cancellation_resumes")


async def test_missing_material_capability_cannot_start_hidden_model_probe(tmp_path, monkeypatch):
    f = setup(tmp_path)
    admit(f)
    spawn = f.router.sandbox.spawn
    async def version(argv, *args, **kwargs):
        assert argv == [f.router.effective("implement", "codex")["binary"], "--version"]
        return await spawn([sys.executable, "-c", "print('codex-cli 0.165.0')"], *args, **kwargs)
    monkeypatch.setattr(f.router.sandbox, "spawn", version)
    async with approve_runtime_messages(f.store, f.rid):
        with file_lock(f.store.root / "controller.lock", blocking=False), f.incident.workflow_call(
                f.engine, "implement", f.context, ImplementationResult, f.project, OP):
            await CALL.get().ensure_runtime(f.router, f.context, ImplementationResult, f.project)
            with pytest.raises(RecoveryPathExhausted, match="material_read_capability"):
                await f.router._ensure_material_read(f.router.effective("implement", "codex"), f.project, f.rid, False)
    assert not occupied(f)
    assert len([e for e in f.store.events(f.rid) if e["kind"] == "agent.started"]) == 1


async def test_correction_prompt_and_prepared_postimage_are_bound(tmp_path, monkeypatch):
    f = setup(tmp_path)
    admit(f)
    compile_prompt, prompts = f.router.compiler.compile, []
    def capture(*args, **kwargs):
        result = compile_prompt(*args, **kwargs)
        prompts.append(result[0])
        return result
    monkeypatch.setattr(f.router.compiler, "compile", capture)
    assert await invoke(f) == RESULT
    with f.store.connect() as db:
        rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'materials/%/output-limit/%'", (f.rid,)).fetchall()
    assert len(rows) == len(prompts) == 1
    assert json.loads(rows[0][1])["prompt_hash"] == digest(prompts[0] + "\n\n" + CORRECTION)
    patch = prepare_changes(f.project, RESULT.changes, ["app.py"])
    f.incident.prepared("T1", f.project, patch)
    apply_prepared(f.project, patch)
    f.incident.applied("T1", f.project, patch)
    f.incident.applied("T1", f.project, patch)  # crash after applying, before phase checkpoint
    (f.project / "unrelated.py").write_text("user change")
    with pytest.raises(RecoveryPathExhausted):
        f.incident.prepared("T1", f.project, patch)


@pytest.mark.parametrize("state", ["exhausted", "pending", "question"])
async def test_acceptance_slot_cannot_grant_existing_provider_authority(tmp_path, state):
    f = setup(tmp_path)
    admit(f)
    await invoke(f)
    operation = OP + "/review"
    value = {"failures": 0, "grant": 0, "pending": False}
    if state == "exhausted":
        value["failures"] = 3
    elif state == "pending":
        value["pending"] = True
    else:
        f.store.request(f.rid, "provider/" + operation, "Review needs separate authority", "review")
    f.store.checkpoint(f.rid, "infra/" + operation + "/codex", value)
    token = CALL.set(ModelCall(f.incident, "implementation_review", "review", operation))
    try:
        with pytest.raises(AA2Error):
            await f.router.invoke("review", {}, ReviewResult, f.project, f.rid, operation)
    finally:
        CALL.reset(token)
    assert "implementation_review" not in occupied(f)
    assert f.store.get(f.rid, "infra/" + operation + "/codex") == value


@pytest.mark.parametrize("kind", ["schema", "source", "task", "operation", "budget", "nested-probe"])
async def test_runtime_drift_cannot_spend_the_correction(tmp_path, kind):
    f = setup(tmp_path)
    admit(f)
    if kind == "budget":
        f.store.checkpoint(f.rid, f.request["infra_key"], {"grant": 9})
    elif kind == "source":
        (f.project / "drift.py").write_text("unexpected = True")
    elif kind == "task":
        f.context["task"]["description"] = "changed obligation"
    with pytest.raises(RecoveryPathExhausted):
        with f.incident.workflow_call(f.engine, "implement", f.context,
                ReviewResult if kind == "schema" else ImplementationResult, f.project,
                OP + "/another" if kind == "operation" else OP):
            if kind == "nested-probe":
                await f.router._call(f.router.effective("implement", "codex"), "material_probe", {},
                                    ImplementationResult, f.project, f.rid, "hidden-probe", True)
            else:
                pytest.fail("drift must be rejected before provider entry")
    assert not occupied(f)


async def test_incident_blocks_generic_provider_entry_and_retry_grant(tmp_path):
    f = setup(tmp_path)
    admit(f)
    before = f.store.get(f.rid, f.request["infra_key"])
    with pytest.raises(RecoveryPathExhausted):
        await f.router.invoke("implement", f.context, ImplementationResult, f.project, f.rid, OP)
    with pytest.raises(RecoveryPathExhausted):
        f.router.grant_retry(f.rid, OP)
    assert f.store.get(f.rid, f.request["infra_key"]) == before and not occupied(f)


@pytest.mark.parametrize("method", ["_call", "_call_with_materials"])
async def test_low_level_entry_cannot_bypass_incident_claim(tmp_path, method):
    f = setup(tmp_path)
    admit(f)
    with pytest.raises(RecoveryPathExhausted):
        await getattr(f.router, method)(f.router.effective("implement", "codex"), "implement", f.context,
                                       ImplementationResult, f.project, f.rid, OP, True)
    assert not occupied(f)
    assert len([e for e in f.store.events(f.rid) if e["kind"] == "agent.started"]) == 1


@pytest.mark.parametrize("drift", ["schema", "kind", "candidate", "authority", "source"])
async def test_review_slot_requires_exact_controller_acceptance_binding(tmp_path, drift):
    from auto_agents_2.review_authority import review_schema
    f = setup(tmp_path)
    admit(f)
    await invoke(f)
    f.store.checkpoint(f.rid, OP, {**f.store.get(f.rid, OP), "implementation": RESULT.model_dump()})
    basis = {"version": 1, "authority": "normative_identity_only", "scope": "test_changes",
             "source_hash": tree_hash(f.project), "source_identity": {}}
    basis["hash"] = digest(basis)
    context = {**f.context, "review_kind": "test_changes", "controller_review_basis": basis,
               "candidate_changes": RESULT.model_dump()}
    # Match WorkflowAuthority.prepare's explicit revision=False call. The cache
    # distinguishes a default argument omitted by a caller from explicit False.
    controller_schema = review_schema(ReviewResult, False)
    with f.incident.workflow_call(f.engine, "review", context, controller_schema, f.project,
                                  OP + "/test-change-review", "test_changes"):
        assert CALL.get().role == "protected_review"
    if drift == "authority":
        basis["source_identity"] = {"different": True}
    if drift == "source":
        basis["source_hash"] = "stale"
    basis["hash"] = digest({key: value for key, value in basis.items() if key != "hash"})
    if drift == "kind":
        context["review_kind"] = "implementation"
    if drift == "candidate":
        context["candidate_changes"] = {**context["candidate_changes"], "summary": "other candidate"}
    reason = "protected_review_binding_changed" if drift == "candidate" else "non_acceptance_model_call"
    with pytest.raises(RecoveryPathExhausted, match=reason):
        with f.incident.workflow_call(f.engine, "review", context,
                ReviewResult if drift == "schema" else controller_schema, f.project,
                OP + "/test-change-review", "test_changes"):
            pytest.fail("unbound acceptance must not reach a provider")
    assert set(occupied(f)) == {"correction"}
