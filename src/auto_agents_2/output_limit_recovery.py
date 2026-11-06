"""Explicit one-incident correction; no automatic output-limit retry authority."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .config import ConfigStore
from .errors import AA2Error, PostCommitError, RecoveryPathExhausted
from .models import ImplementationResult, ReviewResult, strict_schema
from .store import Store
from .util import digest, file_lock
from .workspace import tree_hash

VERSION = "output-limit-once-v2"
KEY = "recovery/output-limit-once"
HANDOFF_KEY = "recovery/output-limit-handoff"
HANDOFF_VERSION = "output-limit-handoff-v1"
HANDOFF_HISTORY = "recovery/gate-history/output-limit-handoff/"
CALL = ContextVar("output_limit_model_call", default=None)
MESSAGE = "Incomplete response returned, reason: max_output_tokens"
CORRECTION = (
    "This is one explicitly authorized correction of an incomplete output-limit response. "
    "Return one complete JSON object satisfying every required schema field, with all objects and arrays closed. "
    "Use compact JSON syntax: do not generate padding spaces, repeated blank lines, or repeated output. "
    "Preserve every necessary space and newline INSIDE content and old_text strings, including exact replace anchors. "
    "Keep the summary concise and omit unchanged files. Prefer small, unique, exact replace changes when appropriate; "
    "write a complete file only when required. Do not truncate code, use placeholders or unsupported body references, "
    "omit tests, weaken acceptance, alter approved behavior, or write directly to the source workspace. "
    "If a complete valid implementation cannot be supplied, return the existing recovery_request form with the "
    "unchanged obligations and no partial changes. The controller will preserve it and stop for a separate decision."
)


def raw_hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def code_identity():
    root = Path(__file__).resolve().parent
    return digest({path.name: raw_hash(path.read_bytes()) for path in sorted(root.glob("*.py"))})


def latest_failure(db, request):
    """Bounded primary-key ranges; old failures cannot replace a newer error gate."""
    finished_seq = request["finished_seq"]
    latest = db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    if latest - finished_seq > 20000:
        raise AA2Error("Failure event window is too old for this one-incident admission")
    row = db.execute("SELECT seq,data,time FROM events WHERE seq>? AND seq<=? AND run_id=? AND kind='agent.started' "
        "AND json_extract(data,'$.operation')=? ORDER BY seq DESC LIMIT 1",
        (max(0, finished_seq - 20000), latest, request["run_id"], request["operation"])).fetchone()
    if not row or row[0] >= finished_seq or json.loads(row[1]).get("attempt") != request["failed_attempt"]:
        raise AA2Error("The typed failure is not this operation's latest attempt")
    later = db.execute("SELECT seq FROM events WHERE seq>? AND seq<=? AND run_id=? "
        "AND kind IN ('agent.finished','agent.spawned') AND json_extract(data,'$.attempt')=? LIMIT 1",
        (finished_seq, latest, request["run_id"], request["failed_attempt"])).fetchone()
    if later:
        raise AA2Error("Attempt events contradict the selected terminal failure")
    return {"started_seq": row[0], "started_sha256": raw_hash(row[1]), "started_at": row[2]}


def _native_evidence(store, request):
    with store.connect() as db:
        history = latest_failure(db, request)
        row = db.execute("SELECT path,state,data FROM resources WHERE id=? AND run_id=?",
                         (request["resource_id"], request["run_id"])).fetchone()
        finished = db.execute("SELECT kind,data,time FROM events WHERE seq=? AND run_id=?",
                              (request["finished_seq"], request["run_id"])).fetchone()
    if not row or not finished or finished[0] != "agent.finished":
        raise AA2Error("Original provider failure evidence is unavailable")
    event = json.loads(finished[1])
    metadata = json.loads(row["data"]).get("native_cache", {})
    if (row["state"] != "live" or metadata.get("phase") != "active"
            or metadata.get("attempt") != request["failed_attempt"]
            or metadata.get("operation") != request["operation"]
            or event.get("attempt") != request["failed_attempt"]
            or event.get("complete") is not False or event.get("outcome") != "error"
            or store.get(request["run_id"], metadata.get("session_key", "")) != request["thread_id"]):
        raise AA2Error("Original terminal/session evidence does not match this incident")
    native = Path(row["path"])
    if not native.resolve().is_relative_to(store.root / "sessions" / request["run_id"]):
        raise AA2Error("Native evidence must belong to this run")
    with sqlite3.connect((native / "thread_history_1.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        size = db.execute("SELECT length(CAST(error_json AS BLOB)) FROM thread_turns WHERE thread_id=? AND turn_id=?",
                          (request["thread_id"], request["turn_id"])).fetchone()
        if not size or size[0] is None or size[0] > 1024:
            raise AA2Error("Missing or oversized typed terminal error")
        status, error, started, completed = db.execute(
            "SELECT status,error_json,started_at,completed_at FROM thread_turns WHERE thread_id=? AND turn_id=?",
            (request["thread_id"], request["turn_id"])).fetchone()
        latest_turn = db.execute("SELECT turn_id,status FROM thread_turns WHERE thread_id=? "
                                 "ORDER BY rollout_ordinal DESC LIMIT 1", (request["thread_id"],)).fetchone()
        if latest_turn != (request["turn_id"], "failed"):
            raise AA2Error("The typed failure is not the current native turn")
        if (type(started) is not int or type(completed) is not int
                or started < int(history["started_at"]) - 1 or completed > int(finished[2]) + 1):
            raise AA2Error("Native terminal turn does not belong to the selected attempt's time interval")
    typed = json.loads(error)
    if (status != "failed" or not isinstance(typed, dict) or typed.get("message") != MESSAGE
            or typed.get("codexErrorInfo") != "other" or typed.get("additionalDetails") not in (None, "")):
        raise AA2Error("This is not the exact supported terminal output-limit failure")
    with sqlite3.connect((native / "state_5.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        model = db.execute("SELECT model,reasoning_effort FROM threads WHERE id=?", (request["thread_id"],)).fetchone()
    if not model:
        raise AA2Error("Original native model identity is missing")
    return {**history, "error_sha256": raw_hash(error), "execution_input": metadata["input_identity"],
            "resource_data_sha256": raw_hash(row["data"]), "finished_sha256": raw_hash(finished[1]),
            "native_path": str(native), "model": model[0], "effort": model[1]}


def prepare(project, *, run_id, operation, failed_attempt, question_id, resource_id, thread_id,
            turn_id, finished_seq, authorization_reference, material_probe_authorization_reference=None):
    """Produce a reviewable request from current facts; this creates no authority."""
    store = Store(project, readonly=True)
    run = store.run(run_id)
    if not run or run["status"] not in {"WAITING_USER", "PAUSED"} or run["stage"] != "implement":
        raise AA2Error("Correction requires a paused implementation incident")
    if not operation.startswith("develop/task/") or "/" in operation[len("develop/task/"):]:
        raise AA2Error("Only a single ordinary task implementation is supported")
    task_id = operation[len("develop/task/"):]
    tasks = [task for task in store.get(run_id, "plan", {}).get("tasks", []) if task.get("id") == task_id]
    if len(tasks) != 1:
        raise AA2Error("The original task specification is no longer uniquely present")
    origin_id = tasks[0].get("origin_id") or task_id
    saved = store.get(run_id, operation, {})
    if saved.get("phase") != "implementing" or saved.get("implementation") or saved.get("patch"):
        raise AA2Error("The original task must still be awaiting its implementation proposal")
    work = Path(saved["workspace"])
    source = tree_hash(work)
    if source != saved.get("source_hash") or task_id in store.get(run_id, "develop/done", []):
        raise AA2Error("Task source or acceptance changed since the failed call")
    config = ConfigStore(project).load()
    provider = config.active_provider
    effective = ConfigStore(project).resolve("implement", provider)
    if effective["kind"] != "codex":
        raise AA2Error("This explicit correction supports only Codex typed terminal failures")
    infra_key = f"infra/{operation}/{provider}"
    infra = store.get(run_id, infra_key, {})
    if (infra.get("pending") or infra.get("error_kind") != "unavailable"
            or not infra.get("error_input") or infra.get("grant") != 0
            or type(infra.get("failures")) is not int or infra["failures"] < 1):
        raise AA2Error("The original failed provider checkpoint changed")
    request = {"version": VERSION, "run_id": run_id, "operation": operation, "task_id": task_id,
        "origin_id": origin_id, "task_spec_hash": digest(tasks[0]),
        "round": store.get(run_id, "rounds/" + origin_id, {}).get("round"),
        "stage": "implement", "provider": provider, "failed_attempt": failed_attempt,
        "question_id": question_id, "resource_id": resource_id, "thread_id": thread_id,
        "turn_id": turn_id, "finished_seq": finished_seq, "authorization_reference": authorization_reference,
        "workspace": str(work), "source_hash": source, "logical_input": infra["error_input"],
        "schema_hash": digest(strict_schema(ImplementationResult)), "config_hash": digest(config.model_dump(mode="json")),
        "code_identity": code_identity(), "correction_hash": digest(CORRECTION), "infra_key": infra_key}
    if material_probe_authorization_reference is not None:
        if not isinstance(material_probe_authorization_reference, str) or not material_probe_authorization_reference.strip():
            raise AA2Error("Material probe expansion needs its separate explicit authorization reference")
        request["material_probe_authorization_reference"] = material_probe_authorization_reference
    request.update(_native_evidence(store, request))
    if effective["model"] != request["model"] or effective["effort"] != request["effort"]:
        raise AA2Error("Correction cannot change the original model or reasoning effort")
    with store.connect() as db:
        question = db.execute("SELECT id,key,artifact_hash,answer,superseded FROM questions WHERE id=? AND run_id=?",
                              (question_id, run_id)).fetchone()
        if (not question or question["key"] != "provider/" + operation or question["answer"] is not None
                or question["superseded"]):
            raise AA2Error("The exact unanswered provider question is no longer current")
        request["question"] = dict(question)
        keys = [infra_key, operation, "plan", "plan_revision/current", "task_state/" + task_id,
                "develop/done", "rounds/" + origin_id, "recovery/active", "replanning/pending", "control/cancel"]
        request["guards"] = {}
        for key in keys:
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (run_id, key)).fetchone()
            request["guards"][key] = raw_hash(row[0]) if row else None
            if key in {"recovery/active", "replanning/pending", "control/cancel"} and row and json.loads(row[0]):
                raise AA2Error("Another recovery or control transition is pending")
    if not isinstance(authorization_reference, str) or not authorization_reference.strip():
        raise AA2Error("An explicit authorization reference is required")
    return request


def authorize(project, path, expected_sha256, *, confirm_one_call=False, confirm_two_material_probes=False):
    if not confirm_one_call:
        raise AA2Error("Explicit confirmation of exactly one correction invocation is required")
    from .safe_files import read_regular
    try:
        raw = read_regular(Path(path), 65536)
    except (OSError, ValueError) as error:
        raise AA2Error("Correction request must be a bounded regular file") from error
    if len(raw) > 65536 or raw_hash(raw) != expected_sha256:
        raise AA2Error("Correction request bytes do not match the reviewed SHA256")
    request = json.loads(raw)
    if request.get("material_probe_authorization_reference") and not confirm_two_material_probes:
        raise AA2Error("Explicit confirmation of at most two conditional material probes is required")
    store = Store(project)
    with file_lock(store.root / "controller.lock", blocking=False):
        existing = store.get(request["run_id"], KEY)
        if existing:
            if existing.get("request") != request:
                raise AA2Error("This incident already has a different correction authorization")
            return {"request_hash": digest(request), "state": existing["state"], "replayed": True}
        fields = ["run_id", "operation", "failed_attempt", "question_id", "resource_id", "thread_id", "turn_id",
                  "finished_seq", "authorization_reference"]
        if "material_probe_authorization_reference" in request:
            fields.append("material_probe_authorization_reference")
        current = prepare(project, **{field: request[field] for field in fields})
        if current != request:
            raise AA2Error("Correction evidence, source, configuration or code changed after review")
        store.authorize_output_limit(request)
    return {"request_hash": digest(request), "state": "active", "replayed": False}


def normal_recovery_handoff(store, rid, record=None):
    """A new explicit lifecycle receipt retires execution limits, never the old evidence."""
    with store.connect() as db:
        row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, HANDOFF_KEY)).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(row[0])
            request = receipt["request"]
            parent = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, KEY)).fetchone()
            request_hash = digest(request)
            archive_key = HANDOFF_HISTORY + request_hash
            archive = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, archive_key)).fetchone()
            parent_record = json.loads(parent[0]) if parent else None
            if (receipt["version"] != HANDOFF_VERSION or receipt["mode"] != "normal_recovery"
                    or request["version"] != HANDOFF_VERSION or request["run_id"] != rid
                    or not isinstance(request["authorization_reference"], str) or not request["authorization_reference"].strip()
                    or receipt["request_hash"] != request_hash or receipt["archive_key"] != archive_key
                    or not parent_record or parent_record.get("state") != "stopped"
                    or record is not None and record != parent_record
                    or raw_hash(parent[0]) != request["guards"][KEY]
                    or digest(parent_record["request"]) != request["incident_request_hash"]
                    or not archive or raw_hash(archive[0]) != receipt["archive_sha256"]):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as error:
            raise AA2Error("Normal recovery handoff is missing or changed; preserve the stopped incident") from error
    return receipt


def _handoff_calls(store, rid, record):
    """Check only committed old outcomes; do not reconcile, clean up or redispatch."""
    from .artifacts import ArtifactStore
    from .capabilities import ReadChallengeResult, confirm_read
    from .output_limit_runtime import verify_historical_launch
    from .process_records import read_record

    artifacts = ArtifactStore(store.root)
    for role, claim in record.get("calls", {}).items():
        probe = role.startswith("material_probe_")
        if claim.get("state") != ("proof_confirmed" if probe else "completed"):
            raise AA2Error("Original invocation has an uncertain outcome; reconcile it before handoff")
        verify_historical_launch(record, role, claim)
        saved = store.get(rid, "invocation/" + claim["operation"], {})
        result = store.get(rid, "provider-result/" + claim["operation"], {})
        receipt = saved if probe else saved.get("provider_receipt", saved)
        if (receipt.get("attempt") != claim["attempt"] or result.get("attempt") != claim["attempt"]
                or receipt.get("identity") != claim["input_identity"]
                or receipt.get("incident_launch") != claim["launch"]
                or not saved.get("artifact") or saved["artifact"] != result.get("artifact")
                or saved["artifact"] != receipt.get("artifact")
                or claim.get("result_hash") != digest(saved.get("result"))
                or artifacts.json(saved["artifact"]) != saved.get("result")
                or claim["launch"].get("process_record") != receipt.get("cleanup_record")):
            raise AA2Error("Original invocation receipt changed; preserve it before handoff")
        cleanup = Path(receipt["cleanup_record"])
        if cleanup.parent != store.root / "processes" or cleanup.is_symlink():
            raise AA2Error("Original invocation cleanup path is not owned by this run")
        if cleanup.exists() and read_record(cleanup).get("status") != "cleaned":
            raise AA2Error("Original invocation cleanup is unconfirmed; preserve it before handoff")
        if probe:
            challenge = claim["challenge"]
            expected = (artifacts.bytes(challenge["text_ref"]).decode("utf-8"), "")
            proof = confirm_read(ReadChallengeResult.model_validate(saved["result"]), expected)
            if digest(expected) != challenge["expected_identity"] or proof != claim.get("proof"):
                raise AA2Error("Original material proof changed; preserve it before handoff")


def _handoff_processes(store):
    """Observe ownership records without stopping processes or consuming records."""
    from .process_records import read_record
    root = store.root / "processes"
    if root.is_symlink() or root.exists() and not root.is_dir():
        raise AA2Error("Process ownership directory is unsafe; reconcile it before handoff")
    records = {}
    for path in sorted(root.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError()
            before = path.read_bytes()
            if read_record(path).get("status") != "cleaned" or path.read_bytes() != before:
                raise ValueError()
        except (OSError, ValueError, AA2Error) as error:
            raise AA2Error("Process cleanup is unconfirmed; reconcile it before handoff") from error
        records[path.name] = raw_hash(before)
    return records


def _handoff_pending(store, rid, db):
    """A committed terminal result accounts for a stale marker, never an unknown call."""
    from .artifacts import ArtifactStore
    artifacts = ArtifactStore(store.root)
    accounted = {}
    for row in db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'infra/%' "
                          "AND json_extract(data,'$.pending')=1 ORDER BY key", (rid,)):
        operation, provider = row[0][len("infra/"):].rsplit("/", 1)
        values = {}
        for prefix in ("invocation/", "provider-result/"):
            value = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, prefix + operation)).fetchone()
            values[prefix] = json.loads(value[0]) if value else {}
        saved, result = values["invocation/"], values["provider-result/"]
        attempt = saved.get("attempt")
        started = db.execute("SELECT seq,data FROM events WHERE run_id=? AND kind='agent.started' "
            "AND json_extract(data,'$.operation')=? ORDER BY seq DESC LIMIT 1", (rid, operation)).fetchone()
        terminal = db.execute("SELECT seq,data,kind FROM events WHERE run_id=? AND kind IN ('agent.started','agent.spawned','agent.finished') "
            "AND json_extract(data,'$.attempt')=? ORDER BY seq DESC LIMIT 1", (rid, attempt)).fetchone()
        terminals = db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='agent.finished' "
            "AND json_extract(data,'$.attempt')=?", (rid, attempt)).fetchone()[0]
        cleanup = Path(saved.get("cleanup_record", "."))
        if (not attempt or not saved.get("identity") or not saved.get("artifact")
                or result.get("attempt") != attempt or result.get("artifact") != saved["artifact"]
                or "result" not in saved or artifacts.json(saved["artifact"]) != saved["result"]
                or not started or json.loads(started[1]).get("attempt") != attempt
                or json.loads(started[1]).get("provider") != provider
                or not terminal or terminal[0] <= started[0]
                or terminal[2] != "agent.finished" or terminals != 1
                or json.loads(terminal[1]).get("complete") is not True
                or json.loads(terminal[1]).get("outcome") != "success"
                or cleanup.parent != store.root / "processes" or cleanup.is_symlink()):
            raise AA2Error("A provider invocation has an uncertain pending outcome; reconcile it before handoff")
        accounted[row[0]] = {"operation": operation, "provider": provider, "attempt": attempt,
            "started_seq": started[0], "started_sha256": raw_hash(started[1]),
            "finished_seq": terminal[0], "finished_sha256": raw_hash(terminal[1])}
    return accounted


def prepare_handoff(project, *, run_id, authorization_reference):
    """Describe the exact paused candidate; preparation makes no execution or state change."""
    if not isinstance(authorization_reference, str) or not authorization_reference.strip():
        raise AA2Error("Explicit authorization to return to normal recovery is required")
    store = Store(Path(project), readonly=True)
    record = store.get(run_id, KEY)
    if not isinstance(record, dict) or record.get("state") != "stopped":
        raise AA2Error("Normal recovery handoff requires a stopped output-limit incident")
    if normal_recovery_handoff(store, run_id, record):
        raise AA2Error("This incident already has a normal recovery handoff")
    run = store.run(run_id)
    original = record["request"]
    task_key = original["operation"]
    saved = store.get(run_id, task_key, {})
    work = Path(original["workspace"])
    plan = store.get(run_id, "plan", {})
    tasks = [task for task in plan.get("tasks", []) if task.get("id") == original["task_id"]]
    if (run["status"] != "PAUSED" or saved.get("workspace") != str(work)
            or work.is_symlink() or work.absolute() != work.resolve() or not work.is_dir()
            or saved.get("phase") != "rejected" or saved.get("commit")
            or len(tasks) != 1 or digest(tasks[0]) != original["task_spec_hash"]
            or saved.get("source_hash") != tree_hash(work)
            or saved["source_hash"] != record.get("applied_source", original["source_hash"])
            or store.get(run_id, "recovery/active") or store.get(run_id, "replanning/pending")
            or store.get(run_id, "control/cancel")):
        raise AA2Error("Stopped task, source or recovery state changed; preserve it before handoff")
    permit_key = saved.get("recovery_permit")
    cycle_key = None
    if permit_key:
        permit = store.get(run_id, permit_key, {})
        outcome = permit.get("outcome", {})
        if (not permit_key.startswith("recovery/permits/") or permit.get("phase") != "completed"
                or permit.get("owner") != original["origin_id"]
                or permit.get("action", {}).get("action") != "repair"
                or permit.get("task_scopes", {}).get(original["task_id"]) != tasks[0]["allowed_paths"]
                or outcome.get("tasks", {}).get(original["task_id"], {}).get("status") != "failed"
                or permit.get("outcome_hash") != digest(outcome) or not permit.get("cycle_id")):
            raise AA2Error("Original repair permission has no closed failed task outcome; reconcile it before handoff")
        cycle_key = "recovery/cycles/" + permit["cycle_id"]
        cycle = store.get(run_id, cycle_key, {})
        if cycle.get("phase") != "completed" or permit_key not in cycle.get("permits", []):
            raise AA2Error("Original recovery cycle is incomplete; preserve it before handoff")
    _handoff_calls(store, run_id, record)
    keys = {KEY, task_key, original["infra_key"], "plan", "plan_revision/current", "task_origins",
            "task_state/" + original["task_id"], "rounds/" + original["origin_id"], "develop/done",
            "recovery/active", "replanning/pending", "control/cancel", "capture", "clarify", "clarify_context",
            "design", "design/review-receipt", "prototype", "prototype_approval", "authority/protocol"}
    if permit_key:
        keys.update({permit_key, cycle_key})
    with store.connect() as db:
        if db.execute("SELECT 1 FROM questions WHERE run_id=? AND answer IS NULL AND superseded=0", (run_id,)).fetchone():
            raise AA2Error("An unanswered question requires its own decision before handoff")
        pending = _handoff_pending(store, run_id, db)
        processes = _handoff_processes(store)
        for key, evidence in pending.items():
            keys.update({key, "invocation/" + evidence["operation"], "provider-result/" + evidence["operation"]})
        keys.update(row[0] for row in db.execute("SELECT key FROM checkpoints WHERE run_id=? AND "
            "(key LIKE 'recovery/count/%' OR key LIKE 'recovery/budgets/%' OR key LIKE 'automation/%' "
            "OR key LIKE 'budget_grants/%' OR key LIKE 'recovery/diagnosis_grants/%')", (run_id,)))
        for claim in record.get("calls", {}).values():
            keys.update({"invocation/" + claim["operation"], "provider-result/" + claim["operation"]})
        guards = {}
        for key in sorted(keys):
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (run_id, key)).fetchone()
            guards[key] = raw_hash(row[0]) if row else None
        run_row = db.execute("SELECT status,stage,data FROM runs WHERE id=?", (run_id,)).fetchone()
        answers_hash = digest(store._submitted_answers(db, run_id))
    return {"version": HANDOFF_VERSION, "run_id": run_id, "authorization_reference": authorization_reference,
            "incident_request_hash": digest(original), "workspace": str(work), "source_hash": saved["source_hash"],
            "task_id": original["task_id"], "operation": task_key, "task_hash": digest(tasks[0]),
            "run": {"status": run_row[0], "stage": run_row[1], "data_sha256": raw_hash(run_row[2])},
            "answers_hash": answers_hash, "config_hash": digest(ConfigStore(Path(project)).load().model_dump(mode="json")),
            "code_identity": code_identity(), "guards": guards,
            "accounted_pending": pending, "process_records": processes}


def authorize_handoff(project, path, expected_sha256, *, confirm_normal_recovery=False):
    """Retain the stopped episode and authorize the existing recovery policy, without a call cap."""
    if not confirm_normal_recovery:
        raise AA2Error("Explicit confirmation of normal recovery without the one-time restriction is required")
    from .safe_files import read_regular
    try:
        raw = read_regular(Path(path), 65536)
    except (OSError, ValueError) as error:
        raise AA2Error("Handoff request must be a bounded regular file") from error
    if raw_hash(raw) != expected_sha256:
        raise AA2Error("Handoff request bytes do not match the reviewed SHA256")
    try:
        request = json.loads(raw)
        if (not isinstance(request, dict) or request.get("version") != HANDOFF_VERSION
                or not isinstance(request.get("run_id"), str)
                or not isinstance(request.get("authorization_reference"), str)):
            raise ValueError()
    except (ValueError, TypeError) as error:
        raise AA2Error("Handoff request is not a complete reviewed request") from error
    store = Store(Path(project))
    with file_lock(store.root / "controller.lock", blocking=False):
        existing = normal_recovery_handoff(store, request["run_id"])
        if existing:
            if existing["request"] != request:
                raise AA2Error("This incident already has a different normal recovery authorization")
            return {"request_hash": digest(request), "incident_state": "stopped", "execution_policy": "normal_recovery", "replayed": True}
        current = prepare_handoff(project, run_id=request["run_id"],
                                  authorization_reference=request["authorization_reference"])
        if current != request:
            raise AA2Error("Handoff inputs changed after review; preserve the current candidate")
        from types import SimpleNamespace
        from .artifacts import ArtifactStore
        from .models import TaskSpec
        from .source_history import SourceHistory
        task = next(task for task in store.get(request["run_id"], "plan")["tasks"] if task["id"] == request["task_id"])
        history = SourceHistory(SimpleNamespace(store=store, config=ConfigStore(Path(project)),
            artifacts=ArtifactStore(store.root))).collect_task(request["run_id"], Path(request["workspace"]), TaskSpec.model_validate(task))
        store.handoff_output_limit(request, history)
    return {"request_hash": digest(request), "incident_state": "stopped", "execution_policy": "normal_recovery", "replayed": False}


class Incident:
    def __init__(self, store, rid):
        self.store, self.rid = store, rid

    def record(self):
        return self.store.get(self.rid, KEY)

    def active(self):
        record = self.record()
        if normal_recovery_handoff(self.store, self.rid, record):
            return False
        return bool(record and record["state"] != "accepted")

    def stop(self, reason):
        record = self.record()
        if record and record["state"] == "stopped":
            reason = record["stop_reason"]
        elif record and record["state"] == "active":
            self.store.update_output_limit(self.rid, digest(record["request"]),
                                           {"state": "stopped", "stop_reason": reason})
        return RecoveryPathExhausted({KEY: {"reason": reason}},
            "The one-time output-limit correction has stopped; preserve its evidence and do not redispatch (" + reason + ")")

    def pause(self, reason):
        error = self.stop(reason)
        self.store.checkpoint(self.rid, "recovery/path-exhausted", {"reason": str(error), "excluded_actions": error.evidence})
        self.store.event(self.rid, "recovery.path_exhausted", {"reason": str(error)})
        self.store.update(self.rid, status="PAUSED", pause_reason=str(error), last_error=error.code)
        return self.store.run(self.rid)

    def require_active(self):
        record = self.record()
        if not record or record["state"] != "active":
            raise self.stop("incident_not_active")
        if record["request"]["code_identity"] != code_identity():
            raise self.stop("implementation_source_changed")
        request = record["request"]
        fixed = {request["infra_key"], "plan", "plan_revision/current",
                 "recovery/active", "replanning/pending", "control/cancel"}
        with self.store.connect() as db:
            for key in fixed:
                row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (self.rid, key)).fetchone()
                if (raw_hash(row[0]) if row else None) != request["guards"][key]:
                    raise self.stop("original_authority_or_budget_changed")
        if self.store.get(self.rid, "rounds/" + request["origin_id"], {}).get("round") != request["round"]:
            raise self.stop("original_round_changed")
        return record

    def priority(self, tasks, done):
        if not self.active():
            return None
        record = self.require_active()
        task_id = record["request"]["task_id"]
        if task_id in done:
            self.integrated(task_id)
            return None
        selected = [task for task in tasks if task.id == task_id]
        if len(selected) != 1:
            raise self.stop("task_scope_changed")
        return selected

    def reconcile(self, sandbox):
        record = self.require_active()
        request = record["request"]
        correction = None
        for role, claim in record["calls"].items():
            from .output_limit_runtime import verify_historical_launch
            verify_historical_launch(record, role, claim)
            if role.startswith("material_probe_"):
                self.reconcile_probe(role, sandbox)
                continue
            saved = self.store.get(self.rid, "invocation/" + claim["operation"], {})
            result = self.store.get(self.rid, "provider-result/" + claim["operation"], {})
            receipt = saved.get("provider_receipt", saved)
            if (not isinstance(receipt, dict)
                    or "provider_receipt" in saved and receipt.get("version") != 1
                    or receipt.get("attempt") != claim["attempt"] or result.get("attempt") != claim["attempt"]
                    or receipt.get("identity") != claim["input_identity"] or not saved.get("result")
                    or not saved.get("artifact") or saved["artifact"] != result.get("artifact")
                    or saved["artifact"] != receipt.get("artifact") or not receipt.get("cleanup_record")
                    or receipt.get("incident_launch") != claim.get("launch")
                    or claim.get("launch", {}).get("execution_input") != claim["input_identity"]
                    or claim["launch"].get("process_record") != receipt.get("cleanup_record")
                    or claim.get("result_hash") not in {None, digest(saved["result"])}):
                raise self.stop("claimed_call_without_committed_result")
            from .artifacts import ArtifactStore
            if ArtifactStore(self.store.root).json(saved["artifact"]) != saved["result"]:
                raise self.stop("committed_result_artifact_changed")
            sandbox.confirm_record(receipt["cleanup_record"])
            if claim["state"] != "completed":
                self.store.complete_output_limit_call(self.rid, digest(request), role, claim["attempt"],
                                                      digest(saved["result"]))
            if role == "correction":
                correction = saved, result
        if correction is not None:
            self._restore_proposal(*correction)

    def reconcile_probe(self, role, sandbox):
        from .artifacts import ArtifactStore
        from .capabilities import ReadChallengeResult, confirm_read
        record = self.require_active()
        claim = record["calls"][role]
        from .output_limit_runtime import verify_historical_launch
        verify_historical_launch(record, role, claim)
        operation = claim["operation"]
        saved = self.store.get(self.rid, "invocation/" + operation, {})
        result = self.store.get(self.rid, "provider-result/" + operation, {})
        if (saved.get("attempt") != claim["attempt"] or result.get("attempt") != claim["attempt"]
                or saved.get("identity") != claim["input_identity"]
                or saved.get("incident_launch") != claim["launch"] or not saved.get("artifact")
                or claim["launch"].get("process_record") != saved.get("cleanup_record")
                or saved["artifact"] != result.get("artifact") or not saved.get("cleanup_record")
                or claim.get("result_hash") not in {None, digest(saved.get("result"))}):
            raise self.stop("probe_without_committed_native_result")
        artifacts = ArtifactStore(self.store.root)
        if artifacts.json(saved["artifact"]) != saved.get("result"):
            raise self.stop("probe_result_artifact_changed")
        sandbox.confirm_record(saved["cleanup_record"])
        challenge = claim["challenge"]
        expected = (artifacts.bytes(challenge["text_ref"]).decode("utf-8"), "")
        if digest(expected) != challenge["expected_identity"] or challenge.get("image_read") is not False:
            raise self.stop("probe_challenge_changed")
        proof = confirm_read(ReadChallengeResult.model_validate(saved["result"]), expected)
        key = "material-capability/" + claim["capability_key"]
        if claim["state"] == "proof_confirmed":
            if claim["proof"] != proof or self.store.get(self.rid, key) != claim["capability_record"]:
                raise self.stop("committed_material_proof_changed")
            return
        if claim["state"] == "claimed":
            self.store.complete_output_limit_call(self.rid, digest(record["request"]), role, claim["attempt"],
                                                  digest(saved["result"]))
        keys = ["invocation/" + operation, "provider-result/" + operation, key]
        with self.store.connect() as db:
            rows = {name: db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?",
                                    (self.rid, name)).fetchone() for name in keys}
        expected_rows = {name: raw_hash(row[0]) if row else None for name, row in rows.items()}
        if expected_rows[key] != challenge["capability_before_sha256"]:
            raise self.stop("probe_capability_changed_before_confirmation")
        self.store.confirm_output_limit_probe(self.rid, digest(record["request"]), role, claim["attempt"],
                                             proof, expected_rows)

    def replay_call(self, stage, context, schema, work, operation, sandbox):
        """Historical result reuse does not require equality to a new process profile."""
        if not self.active():
            return None
        from .providers import invocation_identity
        record = self.require_active()
        suffix = {record["request"]["operation"]: "correction",
                  record["request"]["operation"] + "/test-change-review": "protected_review",
                  record["request"]["operation"] + "/review": "implementation_review"}
        role = suffix.get(operation)
        if role not in record["calls"]:
            return None
        self.reconcile(sandbox)
        claim = self.require_active()["calls"][role]
        launch = claim["launch"]
        if (claim["state"] != "completed" or launch["stage"] != stage
                or launch["schema_hash"] != digest(strict_schema(schema))
                or launch["source_hash"] != tree_hash(work)
                or launch["logical_input"] != invocation_identity(stage, context, schema, work,
                                                                  ConfigStore(self.store.root.parent).load())):
            raise self.stop("historical_business_binding_changed")
        saved = self.store.get(self.rid, "invocation/" + operation)
        return schema.model_validate(saved["result"])

    def _restore_proposal(self, verified_invocation, verified_result):
        """Finish publication of the same proposal before rebuilding task history."""
        record = self.require_active()
        request, claim = record["request"], record["calls"]["correction"]
        with self.store.connect() as db:
            keys = [request["operation"], "invocation/" + request["operation"],
                    "provider-result/" + request["operation"]]
            rows = {key: db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?",
                                   (self.rid, key)).fetchone() for key in keys}
        if any(row is None for row in rows.values()):
            raise self.stop("correction_publication_missing")
        task, invocation, result = (json.loads(rows[key][0]) for key in keys)
        if invocation != verified_invocation or result != verified_result:
            raise self.stop("correction_receipt_changed_after_verification")
        proposal = ImplementationResult.model_validate(invocation["result"]).model_dump(mode="json")
        if (claim["state"] != "completed" or claim["input_identity"] != claim["launch"]["execution_input"]
                or request["schema_hash"] != digest(strict_schema(ImplementationResult))
                or digest(proposal) != claim["result_hash"] or proposal != invocation["result"]
                or digest(ConfigStore(self.store.root.parent).load().model_dump(mode="json")) != request["config_hash"]):
            raise self.stop("correction_publication_binding_changed")
        if task.get("implementation"):
            if task["implementation"] != proposal:
                raise self.stop("retained_correction_proposal_changed")
            return
        if (task.get("phase") != "implementing" or task.get("patch") is not None or task.get("commit")
                or task.get("workspace") != request["workspace"] or task.get("source_hash") != request["source_hash"]
                or tree_hash(Path(request["workspace"])) != request["source_hash"]):
            raise self.stop("correction_publication_task_changed")
        self.store.restore_output_limit_proposal(self.rid, digest(request),
            {key: raw_hash(rows[key][0]) for key in keys}, proposal)

    @contextmanager
    def task(self, task_id, sandbox):
        if not self.active():
            yield
            return
        record = self.require_active()
        if record["request"]["task_id"] != task_id:
            raise self.stop("another_task_before_acceptance")
        self.reconcile(sandbox)
        try:
            yield
        except (PostCommitError, RecoveryPathExhausted):
            raise
        except Exception as error:
            raise self.stop("task_failure_" + type(error).__name__) from error

    @contextmanager
    def workflow_call(self, engine, stage, context, schema, work, operation, acceptance=None):
        record = self.require_active()
        request = record["request"]
        if digest(engine.config.load().model_dump(mode="json")) != request["config_hash"]:
            raise self.stop("configuration_changed")
        if digest(context.get("task")) != request["task_spec_hash"]:
            raise self.stop("task_specification_changed")
        if stage == "implement" and operation == request["operation"] and acceptance is None:
            role = "correction"
            if schema is not ImplementationResult or tree_hash(work) != request["source_hash"]:
                raise self.stop("correction_source_or_schema_changed")
        else:
            from .recording_recovery import RecordingReview
            from .review_authority import review_schema, valid_basis
            role = {"test_changes": "protected_review", "implementation": "implementation_review"}.get(acceptance)
            suffix = {"protected_review": "/test-change-review", "implementation_review": "/review"}.get(role)
            saved = self.store.get(self.rid, request["operation"], {})
            correction = record["calls"].get("correction", {})
            expected_schema = review_schema(RecordingReview if acceptance == "test_changes"
                                            and context.get("recording_review_binding") else ReviewResult, False)
            basis = context.get("controller_review_basis")
            if (not role or stage != "review" or operation != request["operation"] + suffix
                    or schema is not expected_schema or not valid_basis(basis) or basis["scope"] != acceptance
                    or basis["source_hash"] != tree_hash(work) or basis["source_identity"] != engine.authority.identity(self.rid)
                    or context.get("review_kind") != acceptance or context.get("task", {}).get("id") != request["task_id"]
                    or correction.get("state") != "completed" or not saved.get("implementation")
                    or digest(saved["implementation"]) != correction.get("result_hash")):
                raise self.stop("non_acceptance_model_call")
            if acceptance == "test_changes":
                if (saved.get("phase") != "implementing" or tree_hash(work) != request["source_hash"]
                        or context.get("candidate_changes") != saved["implementation"]):
                    raise self.stop("protected_review_binding_changed")
            elif (saved.get("phase") != "tested" or not record.get("applied_source")
                    or tree_hash(work) != record["applied_source"] or context.get("source_hash") != record["applied_source"]
                    or context.get("implementation") != saved["implementation"] or context.get("checks") != saved.get("checks")):
                raise self.stop("implementation_review_binding_changed")
        scope = ModelCall(self, role, stage, operation)
        scope.task_checkpoint = digest(self.store.get(self.rid, request["operation"]))
        expected_authority = digest(engine.authority.identity(self.rid))
        scope.authority_recheck = lambda: digest(engine.authority.identity(self.rid)) == expected_authority
        token = CALL.set(scope)
        try:
            yield
        except (PostCommitError, RecoveryPathExhausted):
            raise
        except Exception as error:
            raise self.stop("model_call_failure_" + type(error).__name__) from error
        finally:
            CALL.reset(token)

    def prepared(self, task_id, work, patch):
        if not self.active():
            return
        record = self.require_active()
        from .workspace import prepared_before_hash, validate_prepared
        validate_prepared(work, patch)
        if (record["request"]["task_id"] != task_id
                or prepared_before_hash(work, patch) != record["request"]["source_hash"]
                or record.get("prepared_patch") not in {None, digest(patch)}):
            raise self.stop("prepared_patch_changed")
        self.store.update_output_limit(self.rid, digest(record["request"]), {"prepared_patch": digest(patch)})

    def applied(self, task_id, work, patch):
        if not self.active():
            return
        self.prepared(task_id, work, patch)
        record = self.require_active()
        from .workspace import write_path
        for item in patch:
            path, _ = write_path(work, item["path"])
            current = digest(path.read_bytes()) if path.is_file() else None
            if current != item["after"]:
                raise self.stop("applied_patch_postimage_changed")
        self.store.update_output_limit(self.rid, digest(record["request"]), {"applied_source": tree_hash(work)})

    def integrated(self, task_id):
        if not self.active():
            return
        record = self.require_active()
        saved = self.store.get(self.rid, record["request"]["operation"], {})
        state = self.store.get(self.rid, "task_state/" + task_id, {})
        if (task_id != record["request"]["task_id"] or task_id not in self.store.get(self.rid, "develop/done", [])
                or not saved.get("commit") or state.get("implementation") != "accepted" or state.get("evidence") != "valid"
                or record["calls"].get("implementation_review", {}).get("state") != "completed"):
            raise self.stop("integration_acceptance_not_proven")
        self.store.update_output_limit(self.rid, digest(record["request"]), {"state": "accepted", "commit": saved["commit"]})


class ModelCall:
    def __init__(self, incident, role, stage, operation):
        self.incident, self.role, self.stage, self.operation = incident, role, stage, operation
        self.attempt = None
        self.challenge = None
        self.business_binding = None
        self.task_checkpoint = None
        self.authority_recheck = None

    def recheck_business(self):
        record = self.incident.require_active()
        if (self.task_checkpoint is not None and self.task_checkpoint != digest(
                self.incident.store.get(self.incident.rid, record["request"]["operation"]))
                or self.authority_recheck is not None and not self.authority_recheck()):
            raise self.incident.stop("business_authority_or_checkpoint_changed")

    async def ensure_runtime(self, router, context, schema, workspace):
        from .output_limit_runtime import ensure_runtime
        from .providers import invocation_identity
        self.business_binding = {"logical_input": invocation_identity(self.stage, context, schema, workspace,
                                                                      router.config.load()),
                                 "schema_hash": digest(strict_schema(schema)), "source_hash": tree_hash(workspace)}
        await ensure_runtime(self, router, context, schema, workspace)

    async def material_probe(self, router, eff, workspace, capability, image_read):
        from .artifacts import ArtifactStore
        from .capabilities import ReadChallengeResult, challenge_files
        from .output_limit_runtime import require_binding
        from .resources import ResourceManager
        record = self.incident.require_active()
        self.recheck_business()
        group = "implement" if self.role == "correction" else "review"
        role = "material_probe_" + group
        bound = require_binding(self.incident)
        if (image_read or self.role not in {"correction", "protected_review", "implementation_review"}
                or not record["limits"].get(role)
                or bound["runtime"]["profiles"][group]["capability_key"] != capability):
            raise self.incident.stop("material_read_capability_not_previously_certified")
        if role in record["calls"]:
            raise self.incident.stop("material_probe_already_occupied")
        key = "material-capability/" + capability
        with self.incident.store.connect() as db:
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (self.incident.rid, key)).fetchone()
        before = raw_hash(row[0]) if row else None
        artifacts = ArtifactStore(self.incident.store.root)
        resources = ResourceManager(self.incident.store, router.config.load().cleanup)
        with resources.temporary("read-challenge") as directory:
            context, expected = await challenge_files(artifacts, directory, False)
            text_path = Path(context["context_files"][0])
            scope = ModelCall(self.incident, role, "material_probe", "material-probe/" + capability)
            scope.task_checkpoint, scope.authority_recheck = self.task_checkpoint, self.authority_recheck
            scope.challenge = {"text_ref": {"hash": text_path.name, "kind": "read_challenge",
                                           "bytes": text_path.stat().st_size},
                               "expected_identity": digest(expected), "capability_key": capability,
                               "capability_before_sha256": before, "parent_role": self.role, "image_read": False}
            token = CALL.set(scope)
            try:
                await router._call(eff, "material_probe", context, ReadChallengeResult, workspace,
                                   self.incident.rid, scope.operation, True)
                self.incident.reconcile_probe(role, router.sandbox)
            finally:
                CALL.reset(token)

    def entry(self, stage, operation):
        if stage != self.stage or operation != self.operation:
            raise self.incident.stop("nested_or_recovery_model_call")
        record = self.incident.require_active()
        if self.role in record["calls"]:
            raise self.incident.stop("model_call_already_occupied")

    def claim(self, attempt, input_identity, launch):
        self.entry(self.stage, self.operation)
        self.recheck_business()
        record = self.incident.require_active()
        from .output_limit_runtime import verify_launch
        verify_launch(self.incident, launch)
        if self.business_binding is not None and any(launch.get(key) != value for key, value in self.business_binding.items()):
            raise self.incident.stop("business_input_changed_before_claim")
        self.incident.store.claim_output_limit_call(self.incident.rid, digest(record["request"]), self.role,
                                                   self.operation, attempt, input_identity, launch, self.challenge)
        self.attempt = attempt

    def complete(self, result):
        if self.attempt is None:
            raise self.incident.stop("model_result_without_claim")
        record = self.incident.require_active()
        self.incident.store.complete_output_limit_call(self.incident.rid, digest(record["request"]), self.role,
                                                      self.attempt, digest(result.model_dump(mode="json")))


def current_call(store, rid, stage, operation):
    scope = CALL.get()
    if scope is None and Incident(store, rid).active():
        raise Incident(store, rid).stop("native_call_without_incident_scope")
    if scope is not None:
        if scope.incident.rid != rid or scope.incident.store.root != store.root:
            raise scope.incident.stop("model_call_run_changed")
        scope.entry(stage, operation)
    return scope
