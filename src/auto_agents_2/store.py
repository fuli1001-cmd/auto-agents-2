from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .diagnostics import adopt_logs
from .errors import AA2Error, IncompleteIteration
from .interaction import validate_answer
from .recovery import implemented_candidate
from .replanning import assess
from .util import digest, file_lock, identifier, now, redact

TERMINAL = ("SUCCEEDED", "CANCELLED")
SCHEMA = """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('schema_version','8');
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished ON runs((1))
                WHERE status NOT IN ('SUCCEEDED','CANCELLED');
            CREATE TABLE IF NOT EXISTS checkpoints(
                run_id TEXT NOT NULL REFERENCES runs(id), key TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(run_id,key));
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, time REAL NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS questions(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), key TEXT NOT NULL,
                prompt TEXT NOT NULL, artifact_hash TEXT NOT NULL, answer TEXT,
                created REAL NOT NULL, interaction TEXT NOT NULL DEFAULT '{}',
                superseded INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id,key,artifact_hash));
            CREATE TABLE IF NOT EXISTS usage(
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                provider TEXT NOT NULL, attempt TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resources(
                id TEXT PRIMARY KEY, run_id TEXT, kind TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, refs INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                touched REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_pins(path TEXT PRIMARY KEY, run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resource_holds(
                id TEXT PRIMARY KEY, path TEXT NOT NULL, run_id TEXT, purpose TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifact_owners(hash TEXT NOT NULL, run_id TEXT NOT NULL,
                PRIMARY KEY(hash,run_id));
            CREATE TABLE IF NOT EXISTS artifact_edges(parent TEXT, child TEXT, PRIMARY KEY(parent,child));
            CREATE TABLE IF NOT EXISTS artifact_kinds(hash TEXT, kind TEXT, PRIMARY KEY(hash,kind));
            CREATE TABLE IF NOT EXISTS diagnostic_holds(
                id TEXT PRIMARY KEY, hash TEXT NOT NULL, run_id TEXT, purpose TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS retired_diagnostics(
                hash TEXT PRIMARY KEY, retired REAL NOT NULL, reason TEXT NOT NULL);
"""


class Store:
    def __init__(self, project: Path, *, readonly=False, _initialize=False):
        self.readonly = readonly
        self.root = project.resolve() / ".aa2"
        if self.root.is_symlink():
            raise AA2Error("Project state must be a local directory, not a symlink")
        self.path = self.root / "state.sqlite"
        if self.path.exists() and not _initialize:
            # Readers and engine construction must never perform a migration.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
                row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if meta else None
                version = row[0] if row else "0"
                if version not in {"0", "1", "2", "3", "4", "5", "6", "7", "8"}:
                    raise AA2Error("Unsupported state database version")
            return
        if readonly:
            raise AA2Error("Project state database does not exist")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with file_lock(self.root / "initialize.lock"):
            if self.path.exists():
                return
            temporary = self.root / "state.initializing.sqlite"
            temporary.unlink(missing_ok=True)
            try:
                with closing(sqlite3.connect(temporary)) as db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("BEGIN IMMEDIATE")
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.commit()
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
                fd = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def connect(self):
        db = (sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15) if self.readonly
              else sqlite3.connect(self.path, timeout=15))
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if not self.readonly:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def migrate(self):
        """Called only by run while holding controller.lock, before dispatch/GC."""
        if self.readonly:
            raise AA2Error("Read-only state cannot migrate")
        with self.connect() as db:
            meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if meta else None
            version = row[0] if row else "0"
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            import re
            required = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
            columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
            if version == "8" and required <= tables and {"interaction", "superseded"} <= columns:
                return
            backup = self.root / f"state-v{version if version != '0' else '1'}.backup.sqlite"
            if not backup.exists():
                temporary = backup.with_suffix(".tmp")
                with closing(sqlite3.connect(temporary)) as target:
                    db.backup(target)
                temporary.replace(backup)
                with backup.open("rb") as stream:
                    os.fsync(stream.fileno())
                directory = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            db.execute("BEGIN IMMEDIATE")
            # executescript would commit before running DDL; keep schema and data atomic.
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
            if "interaction" not in columns:
                db.execute("ALTER TABLE questions ADD COLUMN interaction TEXT NOT NULL DEFAULT '{}'")
            if "superseded" not in columns:
                db.execute("ALTER TABLE questions ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
            for row in db.execute("SELECT id FROM runs").fetchall():
                rid = row[0]
                records = {r[0]: json.loads(r[1]) for r in db.execute(
                    "SELECT key,data FROM checkpoints WHERE run_id=?", (rid,))}
                self._put(db, rid, f"migration/v{version if version != '0' else '1'}",
                          {"version": int(version), "records": records})
                # Raw v1 records and answered questions remain authoritative. The
                # active view is adapted; old hashes can still be verified exactly.
                active = records.get("recovery/active")
                if active:
                    from .recovery import FailureRecord
                    failure = FailureRecord.model_validate(active["failure"])
                    failure = failure.model_copy(update={"version": 2,
                        "phase": "task" if failure.task_id else "final",
                        "observation_id": failure.observation_id or digest([rid, failure.id, active["attempt"]])})
                    self._put(db, rid, "recovery/active", {**active, "version": 2, "failure": failure.model_dump()})
                self._put(db, rid, "migration/recovery-contracts", {"version": 2, "from_state": int(version),
                    "evidence_policy": "verification-v12", "reference_validation": 4})
                for task in (records.get("plan") or {}).get("tasks", []):
                    owner = task.get("origin_id") or task["id"]
                    key = "automation/engineering/" + owner
                    if key not in records:
                        self._put(db, rid, key, {"legacy_baseline": True, "achieved": {}})
                if "automation/engineering/final" not in records:
                    self._put(db, rid, "automation/engineering/final", {"legacy_baseline": True, "achieved": {}})
                if version in {"0", "1"}:
                    if records.get("plan"):
                        self._put(db, rid, "plan_revision/current", {"version": 1, "revision": 1})
                    done = records.get("develop/done", [])
                    for task in records.get("plan", {}).get("tasks", []):
                        self._put(db, rid, "task_state/" + task["id"], {
                            "version": 1, "revision": 1, "implementation": "accepted" if task["id"] in done else "pending",
                            "evidence": "stale" if task["id"] in done else "absent"})
                if version in {"3", "4", "5", "6", "7", "8"}:
                    continue  # v3 data semantics are already current; only its schema needs repair.
                owners = {}
                for key, value in records.items():
                    if key.startswith("recovery/failures/") and value:
                        owners[value["id"]] = value["origin_id"]
                    elif key.startswith("recovery/delivery/") and value.get("failure"):
                        owners[value["failure"]["id"]] = "delivery"
                budgets = {}
                for key, value in records.items():
                    if key.startswith("recovery/diagnosis/") and key.rsplit("/", 1)[-1] in owners:
                        owner = owners[key.rsplit("/", 1)[-1]]
                        budgets[owner] = budgets.get(owner, 0) + value.get("round", 0)
                for owner, count in budgets.items():
                    key = "recovery/budgets/diagnosis/" + owner
                    if key not in records:
                        self._put(db, rid, key, {"round": count})
                for key, value in records.items():
                    if key.startswith("replanning/evidence/") and key != "replanning/evidence/contracts":
                        self._put(db, rid, key, {**value, "comparable": False,
                                               "comparison_reason": "legacy evidence requires revalidation"})
            db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
            self._event(db, None, "state.migrated", {"version": 8, "backup": str(backup)})

    def finish_recovery(self, rid, updates, pending, *, replan=None, successor=None):
        """Dispatch and retire the obsolete question together; retain raw history."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, value in updates.items():
                self._put(db, rid, key, value)
            if pending:
                db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=?",
                           (rid, f"replan-stalled/{pending['evaluation']}"))
            if replan is not None:
                next_plan = self._prepare_replan(db, rid, *replan)
                self._put(db, rid, "replanning/pending", {**next_plan, "controller_owned": True})
            if successor is not None:
                count_key = "recovery/count/" + successor["id"]
                row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, count_key)).fetchone()
                count = (json.loads(row[0]) if row else 0) + 1
                self._put(db, rid, count_key, count)
                self._put(db, rid, "recovery/active", {"version": 1, "failure": successor, "attempt": count})
                self._put(db, rid, f"recovery/failures/{successor['id']}/{count}", successor)
                self._event(db, rid, "recovery.requested", {"failure": successor["id"], "owner": successor["origin_id"],
                    "component": successor["component"], "kind": successor["kind"]})
            self._event(db, rid, "recovery.dispatched", {"pending_issues_retained": True})

    def _put(self, db, rid, key, value):
        if key.startswith("verification/batches/"):
            from .faults import StateConflict
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and previous.get("phase") == "completed" and previous != value:
                raise StateConflict("Completed verification observation cannot be overwritten")
        if key.startswith("recovery/permits/"):
            from .faults import StateConflict
            from .recovery_identity import action_contract
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and previous.get("outcome_hash") and previous["outcome_hash"] != digest(previous.get("outcome", {})):
                raise StateConflict("Completed action result integrity mismatch")
            if previous and previous.get("phase") == "completed":
                if (not value or value.get("phase") != "completed" or value.get("outcome") != previous.get("outcome")
                        or action_contract(value.get("action", {})) != action_contract(previous.get("action", {}))
                        or any(value.get(name) != previous.get(name) for name in
                               ("source_hash", "plan_hash", "task_scopes", "prototype_binding", "cycle_id", "owner"))):
                    raise StateConflict("Completed action outcome cannot be overwritten")
            if value and value.get("phase") == "completed":
                value = {**value, "outcome_hash": digest(value.get("outcome", {}))}
        db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                   (rid, key, json.dumps(value, ensure_ascii=True)))
        adopt_logs(db, self.root, value)

    def prepare_recording_recovery(self, rid, key, record, guards, question_keys, questions, *,
                                   previous, observation_key, observation):
        """Keep the same failure/attempt; preparation does not answer or retire a wait."""
        from .faults import StateConflict
        from .recording_recovery import OWNER
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()[0] not in {'RUNNING', 'WAITING_USER'}:
                raise StateConflict('Recording admission requires the normal running recovery entry')
            for name, expected in {**guards, OWNER + record['task_id']: previous, observation_key: None}.items():
                row = db.execute('SELECT data FROM checkpoints WHERE run_id=? AND key=?', (rid, name)).fetchone()
                if (json.loads(row[0]) if row else None) != expected:
                    raise StateConflict('Recording admission inputs changed before publication')
            current = self._decision_questions(db, rid, question_keys)
            if current != questions or any(q['answer'] is not None for q in current):
                raise StateConflict('Recording admission cannot reinterpret an existing human answer')
            self._put(db, rid, key, record)
            self._put(db, rid, OWNER + record['task_id'], key)
            self._put(db, rid, observation_key, observation)
            self._event(db, rid, 'recording.diagnosis_prepared', {'receipt': key,
                'task_id': record['task_id'], 'failure': record['original_active']['failure']['id']})

    def activate_plan_patch(self, rid, journal_id):
        """CAS activation. Git/files are snapshotted before this transaction, never called atomic with it."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,data FROM checkpoints WHERE run_id=?", (rid,))}
            rows["authority/submitted-answers"] = self._submitted_answers(db, rid)
            key = "plan_revision/journal/" + journal_id
            journal = rows[key]
            if journal["phase"] == "activated":
                from .recording_recovery import validate_journal
                validate_journal(journal, rows.get, self.root)
                return
            revision = rows.get("plan_revision/current", {}).get("revision", 1)
            if revision != journal["base_revision"] or rows["plan"] != journal["previous_plan"]:
                raise AA2Error("Plan activation CAS failed; preserving proposal and recovery snapshots")
            from .plan_revision import validate_timing_journal
            validate_timing_journal(journal)
            from .recording_recovery import validate_journal
            validate_journal(journal, rows.get, self.root)
            from .contract_corrections import (
                legacy_contract_contexts,
                validate_contract_journal,
            )
            contexts = None
            if journal.get("version") == 2 and journal.get("patch", {}).get("contract_corrections"):
                from .artifacts import ArtifactStore
                contexts = legacy_contract_contexts(journal, journal_id, rows, ArtifactStore(self.root).json)
            validate_contract_journal(journal, rows.get, legacy_contexts=contexts)
            from .evidence_admission import (
                requires_admission,
                validate_admission_journal,
            )
            if requires_admission(journal):
                from .config import ConfigStore
                validate_admission_journal(journal, rows.get,
                    shared_browsers=ConfigStore(self.root.parent).load().shared_cache.enabled)
            if rows.get("authority/protocol") or journal.get("review", {}).get("authority"):
                from .review_authority import validate_journal_authority
                validate_journal_authority(journal, rows.get)
                review = (journal.get("authority_reassessment") or journal)["review"]
                review_key = "authority/reviews/" + digest(review)
                self._put(db, rid, "authority/accepted/plan", {"version": 1,
                    "artifact_hash": digest(journal["plan"]), "review_hash": digest(review), "review_key": review_key})
                correction = rows[review_key]["basis"].get("correction")
                if correction:
                    if correction != rows.get("authority/correction"):
                        raise AA2Error("Plan correction changed before activation")
                    self._put(db, rid, "authority/correction", {**correction, "completed": {
                        "review_key": review_key, "artifact_hash": digest(journal["plan"])}})
            self._put(db, rid, f"plan_revision/history/{revision}", {
                "plan": rows["plan"], "tasks": {k: v for k, v in rows.items() if k.startswith(("develop/", "task_state/"))}})
            origins = dict(rows.get("task_origins", {}))
            for task in journal["plan"]["tasks"]:
                origins[task["id"]] = task["origin_id"]
            self._put(db, rid, "task_origins", origins)
            self._put(db, rid, "plan", journal["plan"])
            if journal.get('recording'):
                proof = journal['recording']
                self._put(db, rid, 'recovery/recording/plans/' + proof['receipt_hash'] + '/' + digest(journal['plan']),
                          {'journal': key, 'review_hash': digest(journal['recording_review'])})
            from .check_production import activation_updates
            for name, value in activation_updates(journal.get("evidence_admission", {})).items():
                self._put(db, rid, name, value)
            self._put(db, rid, "plan_revision/current", {"version": 1, "revision": revision + 1})
            done = set(rows.get("develop/done", [])) - set(journal["affected"])
            done &= {t["id"] for t in journal["plan"]["tasks"]}
            self._put(db, rid, "develop/done", sorted(done))
            for task_id in set(journal["affected"]) | set(journal.get("patch", {}).get("remove_tasks", [])):
                db.execute("DELETE FROM checkpoints WHERE run_id=? AND key=?", (rid, "develop/review-evidence/" + task_id))
            for task_id in journal["affected"]:
                predecessor = task_id
                if task_id not in journal["reopen"] and task_id not in {t["id"] for t in journal["previous_plan"]["tasks"]}:
                    replaced = [t["id"] for t in journal["previous_plan"]["tasks"]
                                if t["id"] in journal.get("patch", {}).get("remove_tasks", []) and t["origin_id"] == origins[task_id]]
                    if len(replaced) == 1:
                        predecessor = replaced[0]
                accepted = predecessor in rows.get("develop/done", [])
                previous = rows.get("develop/task/" + predecessor, {})
                old = rows.get("task_state/" + task_id, {})
                state = {"version": 1, "revision": old.get("revision", 1) + 1,
                         "implementation": "needs_repair" if task_id in journal["reopen"] else (
                             "accepted" if accepted else "pending"),
                         "evidence": "stale"}
                self._put(db, rid, "task_state/" + task_id, state)
                retained = {}
                if task_id not in rows.get("develop/done", []) and journal.get("snapshots", {}).get(task_id):
                    retained["workspace"] = journal["snapshots"][task_id]["workspace"]
                elif task_id not in {t["id"] for t in journal["previous_plan"]["tasks"]}:
                    origin = origins[task_id]
                    inherited = [snapshot for old_id, snapshot in journal.get("snapshots", {}).items()
                                 if origins.get(old_id) == origin and old_id not in rows.get("develop/done", [])]
                    if len(inherited) > 1:
                        raise AA2Error("Multiple unfinished candidates need explicit reconciliation before splitting their origin")
                    if inherited:
                        retained["resume_base"] = inherited[0]["commit"]
                self._put(db, rid, "develop/task/" + task_id, {
                    **retained,
                    "phase": ("rejected" if task_id in journal["reopen"] else "reverify"
                              if implemented_candidate(previous, accepted) else "pending"),
                    "feedback": {"controller_recovery": journal["reason"]},
                    "previous_evidence": previous, "revision": state["revision"]})
            for prefix in ("readme", "candidate/readme", "verify"):
                db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR substr(key,1,?)=?)", (rid, prefix, len(prefix) + 1, prefix + "/"))
            self._put(db, rid, key, {**journal, "phase": "activated"})
            self._event(db, rid, "plan.patch_activated", {"revision": revision + 1, "affected": journal["affected"], "journal": key})

    def _run(self, row):
        if row is None:
            return None
        return {**dict(row), "data": json.loads(row["data"])}

    def current(self, unfinished: bool = False):
        with self.connect() as db:
            where = "WHERE status NOT IN ('SUCCEEDED','CANCELLED')" if unfinished else ""
            row = db.execute(f"SELECT * FROM runs {where} ORDER BY created DESC LIMIT 1").fetchone()
            return self._run(row)

    def run(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise AA2Error(f"Unknown run: {run_id}")
            return self._run(row)

    def admit(self, source: str, config: dict) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE status NOT IN ('SUCCEEDED','CANCELLED')").fetchone():
                raise IncompleteIteration("上一轮迭代尚未完成")
            rid = identifier("run")
            data = {"source": source, "config": redact(config), "outcome": "", "replans": 0}
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                       (rid, "RUNNING", "capture", now(), now(), json.dumps(data)))
            self._event(db, rid, "run.created", {"source": source})
        return self.run(rid)

    def _event(self, db, rid, kind, data):
        db.execute("INSERT INTO events(run_id,time,kind,data) VALUES(?,?,?,?)",
                   (rid, now(), kind, json.dumps(redact(data), ensure_ascii=True)))

    def event(self, rid, kind, data):
        with self.connect() as db:
            self._event(db, rid, kind, data)

    def update(self, rid: str, *, status=None, stage=None, **data):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            body = json.loads(row["data"])
            body.update(data)
            db.execute("UPDATE runs SET status=?,stage=?,updated=?,data=? WHERE id=?",
                       (status or row["status"], stage or row["stage"], now(), json.dumps(body), rid))
            self._event(db, rid, "run.updated", {"status": status, "stage": stage, **data})

    def record_fault(self, rid, record, *, pause=False):
        """Save the classification and retire generic legacy prompts atomically."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._put(db, rid, "recovery/current-fault", record)
            self._put(db, rid, "recovery/faults/" + record.get("incident_id", digest(record)), record)
            if pause or record.get("route") == "human":
                db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key LIKE 'incident/%' AND answer IS NULL", (rid,))
            if pause:
                body = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()[0])
                body.update(pause_reason="internal_error", incident_id=record["incident_id"])
                db.execute("UPDATE runs SET status='PAUSED',updated=?,data=? WHERE id=?", (now(), json.dumps(body), rid))
            self._event(db, rid, "workflow.internal_error" if pause else "recovery.classified", record)

    def checkpoint(self, rid, key, data):
        self.checkpoints(rid, {key: data})

    def checkpoints(self, rid, values, *, usage=None):
        """Publish related phases, round counters and evidence references atomically."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if usage:
                provider, attempt, body = usage
                db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?)",
                           (digest([provider, attempt, "invocation"]), rid, provider, attempt, json.dumps(body)))
            for key, data in values.items():
                if key.startswith("rounds/"):
                    existing = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
                    if existing:
                        data = {**data, "round": max(data.get("round", 0), json.loads(existing[0]).get("round", 0))}
                self._put(db, rid, key, data)
                self._event(db, rid, "checkpoint.saved", {"key": key})

    def get(self, rid, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
            return json.loads(row[0]) if row else default

    @staticmethod
    def _decision_questions(db, rid, keys):
        if not keys:
            return []
        slots = ",".join("?" for _ in keys)
        return [dict(row) for row in db.execute(
            f"SELECT id,key,artifact_hash,answer FROM questions WHERE run_id=? AND superseded=0 AND key IN ({slots}) ORDER BY id",
            (rid, *keys))]

    def decision_questions(self, rid, keys):
        with self.connect() as db:
            return self._decision_questions(db, rid, keys)

    def decision_requests(self, rid, state_identity=None):
        """Find original decision snapshots for the same guarded controller state."""
        with self.connect() as db:
            rows = [(row["key"], json.loads(row["data"])) for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'decisions/requests/%' ORDER BY rowid DESC",
                (rid,))]
        return [(key, value) for key, value in rows
                if state_identity is None or value.get("request", {}).get("state_identity") == state_identity]

    def decision_question_keys(self, rid, prefix):
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT key FROM questions WHERE run_id=? AND superseded=0 "
                "AND (key=? OR substr(key,1,?)=?) ORDER BY key",
                (rid, prefix, len(prefix) + 1, prefix + "/"))]

    def apply_decision(self, rid, receipt, request, result, actor, guards, updates, questions, question_keys,
                       *, result_key=None, answers=None, supersede_keys=None):
        """CAS the state, human answers, grant and audit receipt in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM checkpoints WHERE run_id=? AND key=?", (rid, receipt)).fetchone():
                return True
            status = db.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()[0]
            if status not in {"RUNNING", "WAITING_USER"}:
                return False
            for key, expected in guards.items():
                row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
                if (json.loads(row[0]) if row else None) != expected:
                    return False
            current = self._decision_questions(db, rid, question_keys)
            if current != questions or actor != "human" and any(q["answer"] is not None for q in current):
                return False
            for key, value in updates.items():
                self._put(db, rid, key, value)
            self._put(db, rid, receipt, {"actor": actor, "request": request, "result": result,
                "result_key": result_key, "answers": answers or {}, "applied": True})
            db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key LIKE 'incident/%' AND answer IS NULL", (rid,))
            if actor != "human" or supersede_keys is not None:
                for key in question_keys if supersede_keys is None else supersede_keys:
                    db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=? AND answer IS NULL", (rid, key))
            self._event(db, rid, "decision.applied", {"actor": actor, "kind": request["kind"],
                "owner": request["owner"], "action": result["action"], "budget": request.get("budget", {}),
                "reason": result["reason"]})
            return True

    def invalidate(self, rid, keys: list[str]):
        with self.connect() as db:
            for key in keys:
                db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR substr(key,1,?)=?)",
                           (rid, key, len(key) + 1, key + "/"))
            self._event(db, rid, "checkpoints.invalidated", {"keys": keys})

    def prepare_replan(self, rid, stage, reason):
        """Journal the decision and counters together, before asking or invalidating."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._prepare_replan(db, rid, stage, reason)

    def _prepare_replan(self, db, rid, stage, reason):
        rows = {r["key"]: json.loads(r["data"]) for r in db.execute(
            "SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'replanning/%'", (rid,))}
        if rows.get("replanning/pending"):
            return rows["replanning/pending"]
        observations = {key.removeprefix("replanning/evidence/"): value for key, value in rows.items()
                        if key.startswith("replanning/evidence/")}
        state, decision = assess(rows.get("replanning/progress", {}), observations)
        body = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()[0])
        pending = {"stage": stage, "reason": reason, "decision": decision,
                   "evaluation": state["evaluations"], "generation": body.get("replans", 0), "granted": False}
        for key, data in (("replanning/progress", state), ("replanning/pending", pending)):
            db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                       (rid, key, json.dumps(data, ensure_ascii=True)))
        self._event(db, rid, "replan.assessed", {"stage": stage, **decision})
        return pending

    def complete_replan(self, rid, stages, recovery_refs):
        """Finish a journalled rollback atomically; crash recovery cannot double count."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key='replanning/pending'", (rid,)).fetchone()
            if row is None:
                return
            pending = json.loads(row[0])
            body = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()[0])
            body["replans"] = pending["generation"] + 1
            records = {r["key"]: json.loads(r["data"]) for r in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=?", (rid,))}
            request = pending.get("correction_request")
            if request:
                from .stage_responsibilities import validate_correction_request
                validate_correction_request(request, records.get, action=(
                    "plan_patch" if pending["stage"] == "plan" else pending["stage"]))
            removed = {key: value for key, value in records.items() if any(
                key == prefix + stage or key.startswith(prefix + stage + "/")
                for stage in stages for prefix in ("", "candidate/", "invocation/", "provider-result/"))}
            self._put(db, rid, f"recovery/gate-history/{pending['generation']}", {
                "version": 1, "pending": pending, "checkpoints": removed, "recovery_refs": recovery_refs})
            if "plan" in removed:
                revision = records.get("plan_revision/current", {}).get("revision", 1)
                self._put(db, rid, f"plan_revision/history/{revision}", {
                    "plan": removed["plan"], "tasks": {k: v for k, v in records.items() if k.startswith(("develop/", "task_state/"))}})
                self._put(db, rid, "plan_revision/current", {"version": 1, "revision": revision + 1})
            for stage in stages:
                for prefix in ("candidate/", "invocation/", "provider-result/"):
                    db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR substr(key,1,?)=?)",
                               (rid, prefix + stage, len(prefix + stage) + 1, prefix + stage + "/"))
                db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR substr(key,1,?)=?)",
                           (rid, stage, len(stage) + 1, stage + "/"))
            feedback = {"stage": pending["stage"], "reason": pending["reason"], "recovery_refs": recovery_refs}
            if request:
                feedback.update(correction_request=request, preserved_specification=records.get("clarify"),
                                preserved_plan=records.get("plan"))
            if pending.get("verification_recheck"):
                feedback["verification_recheck"] = pending["verification_recheck"]
            db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                       (rid, "replan_feedback", json.dumps(feedback, ensure_ascii=True)))
            db.execute("UPDATE runs SET status='RUNNING',stage=?,updated=?,data=? WHERE id=?",
                       (pending["stage"], now(), json.dumps(body), rid))
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key='replanning/pending'", (rid,))
            self._event(db, rid, "replan.completed", {"stage": pending["stage"], "replans": body["replans"],
                                                    "recovery_refs": recovery_refs, "granted": pending["granted"]})

    def recover_verification_replan(self, rid, pending, task_key, previous, updated, recovery_key, record):
        """Replace obsolete checker feedback atomically, retaining budgets and history."""
        self.recover_task_replan(rid, pending, task_key, previous, updated, recovery_key, record, "verification.recovered")

    def recover_task_replan(self, rid, pending, task_key, previous, updated, recovery_key, record, event):
        """Reassess an unchanged proposal with repaired evidence, preserving all budgets."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, expected in (("replanning/pending", pending), (task_key, previous)):
                row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
                if row is None or json.loads(row[0]) != expected:
                    raise AA2Error("Task recovery state changed; preserve the candidate and retry")
            for key, value in ((recovery_key, {**record, "recovered": True}), (task_key, updated)):
                db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                           (rid, key, json.dumps(value, ensure_ascii=True)))
                adopt_logs(db, self.root, value)
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key='replanning/pending'", (rid,))
            db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=?",
                       (rid, f"replan-stalled/{pending['evaluation']}"))
            self._event(db, rid, event, {"task": task_key, "recovery": recovery_key})

    @staticmethod
    def _resolution_wait_questions(db, rid, keys):
        rows = []
        for key in keys:
            prefix = key + "/history/"
            rows.extend(dict(row) for row in db.execute(
                "SELECT id,key,artifact_hash,answer,superseded FROM questions WHERE run_id=? "
                "AND (key=? OR substr(key,1,?)=?) ORDER BY id", (rid, key, len(prefix), prefix)))
        return rows

    def resolution_wait_questions(self, rid, keys):
        with self.connect() as db:
            return self._resolution_wait_questions(db, rid, keys)

    def admit_resolution_diagnosis(self, rid, receipt, record, guards, updates, question_keys, questions):
        """CAS one original external wait into diagnosis; never answer a question."""
        from .faults import StateConflict
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            status = db.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()[0]
            if status not in {"RUNNING", "WAITING_USER"}:
                raise StateConflict("Resolution admission requires the normal running controller")
            for key, expected in guards.items():
                row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
                if (json.loads(row[0]) if row else None) != expected:
                    raise StateConflict("Resolution admission inputs changed; preserve the original wait")
            current = self._resolution_wait_questions(db, rid, question_keys)
            if current != questions or any(q["answer"] is not None for q in current):
                raise StateConflict("Resolution admission cannot replace a concurrent human answer")
            for key, value in updates.items():
                self._put(db, rid, key, value)
            self._put(db, rid, receipt, record)
            for key in question_keys:
                db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=? AND answer IS NULL",
                           (rid, key))
            self._event(db, rid, "environment.diagnosis_admitted", {
                "receipt": receipt, "observation": record["observation"],
                "failure": record["failure"]["id"], "initial_action": "diagnose",
                "questions": [q["id"] for q in questions]})

    def refresh_design_questions(self, rid, previous, updated, keys, questions, history_key, record):
        """Publish a reconciled design proposal without losing concurrent human answers."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key='candidate/design'", (rid,)).fetchone()
            current = self._decision_questions(db, rid, keys)
            fingerprint = digest(previous["document"])
            if (not row or json.loads(row[0]) != previous or current != questions
                    or any(q["answer"] is not None and q["artifact_hash"] == fingerprint for q in current)):
                from .faults import StateConflict
                raise StateConflict("Design questions changed during evidence refresh; preserve the proposal and human answers")
            self._put(db, rid, history_key, record)
            self._put(db, rid, "candidate/design", updated)
            for key in keys:
                db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=? AND artifact_hash=? AND answer IS NULL",
                           (rid, key, fingerprint))
            self._event(db, rid, "design.questions_refreshed", {"history": history_key})

    @staticmethod
    def _question(row):
        values = dict(row)
        return {**values, "interaction": json.loads(values.get("interaction", "{}")), "superseded": values.get("superseded", 0)}

    def question(self, qid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
            return self._question(row) if row else None

    def request(self, rid, key, prompt, artifact_hash="", interaction=None) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            qid = identifier("question")
            db.execute("INSERT OR IGNORE INTO questions(id,run_id,key,prompt,artifact_hash,answer,created,interaction) VALUES(?,?,?,?,?,NULL,?,?)",
                       (qid, rid, key, prompt, artifact_hash, now(), json.dumps(interaction or {})))
            # Refresh preview URLs after restart without losing question history.
            db.execute("UPDATE questions SET prompt=?,interaction=?,superseded=0 WHERE run_id=? AND key=? AND artifact_hash=? AND answer IS NULL",
                       (prompt, json.dumps(interaction or {}), rid, key, artifact_hash))
            db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=? AND artifact_hash!=? AND answer IS NULL",
                       (rid, key, artifact_hash))
            row = db.execute("SELECT * FROM questions WHERE run_id=? AND key=? AND artifact_hash=?",
                             (rid, key, artifact_hash)).fetchone()
            return self._question(row)

    def supersede_questions(self, rid, key, *, include_answered=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE questions SET superseded=1 WHERE run_id=? AND key=? AND answer IS NULL", (rid, key))
            if include_answered:
                db.execute("UPDATE questions SET superseded=1,key=key||'/history/'||id WHERE run_id=? AND key=? AND answer IS NOT NULL", (rid, key))
            self._event(db, rid, "question.superseded", {"key": key})

    def pending(self, rid):
        with self.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
            restriction = " AND superseded=0" if "superseded" in columns else ""
            return [self._question(r) for r in db.execute(
                "SELECT * FROM questions WHERE run_id=? AND answer IS NULL" + restriction + " ORDER BY created", (rid,))]

    @staticmethod
    def _submitted_answers(db, rid):
        """External requirements answers, never automatic decisions or approvals.

        Question prose is context, not a statement made by its respondent. Keep
        the original answer and binding; do not infer that a human wrote it.
        """
        import re
        rows = db.execute("SELECT * FROM questions WHERE run_id=? AND answer IS NOT NULL "
                          "AND superseded=0 ORDER BY created,id", (rid,))
        answers = []
        for row in rows:
            if not re.fullmatch(r"(?:(?:clarify/[^/]+|prototype-clarification|provider_research|design)/[0-9]+|prototype-feedback)", row["key"]):
                continue
            item = {k: row[k] for k in ("id", "key", "prompt", "answer", "artifact_hash")}
            item["raw_answer"] = row["answer"]
            item["interaction"] = interaction = json.loads(row["interaction"])
            if interaction.get("kind") == "choice":
                choices = [o for o in interaction["options"] if o["key"] == row["answer"]]
                if len(choices) != 1:
                    raise AA2Error("Submitted requirement answer lost its exact selected option")
                item["selected_option"] = selected = choices[0]
                item["answer"] = selected["label"] + ("\n" + selected["value"] if selected["value"] != selected["label"] else "")
            answers.append(item)
        return answers

    def submitted_answers(self, rid):
        with self.connect() as db:
            return self._submitted_answers(db, rid)

    def authority_get(self, rid, key):
        if key == "authority/submitted-answers":
            return self.submitted_answers(rid)
        return self.get(rid, key)

    def answer(self, qid, answer, artifact_hash: str | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
            if not row or row["answer"] is not None or row["superseded"]:
                raise AA2Error("问题不存在或已经处理")
            if artifact_hash is not None and row["artifact_hash"] != artifact_hash:
                raise AA2Error("审批版本不匹配")
            if row["key"] == "prototype-approval" and artifact_hash != row["artifact_hash"]:
                raise AA2Error("原型审核必须指定当前审批版本")
            answer = validate_answer(answer, json.loads(row["interaction"]))
            db.execute("UPDATE questions SET answer=? WHERE id=?", (answer, qid))
            self._event(db, row["run_id"], "question.answered", {"question_id": qid})

    def add_usage(self, rid, provider, attempt, event_id, usage):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?)",
                       (digest([provider, attempt, event_id]), rid, provider, attempt, json.dumps(usage)))

    def usage_summary(self, rid):
        result = {k: 0 for k in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "total_tokens")}
        result["complete"] = True
        with self.connect() as db:
            for row in db.execute("SELECT data FROM usage WHERE run_id=?", (rid,)):
                usage = json.loads(row[0])
                for key in result:
                    if key == "complete":
                        result[key] = result[key] and usage.get(key, False)
                    else:
                        result[key] += usage.get(key, 0)
            recorded = {r[0] for r in db.execute("SELECT DISTINCT attempt FROM usage WHERE run_id=?", (rid,))}
            started = {json.loads(r[0]).get("attempt") for r in db.execute(
                "SELECT data FROM events WHERE run_id=? AND kind='agent.started'", (rid,))}
            if started - recorded:
                result["complete"] = False
        return result

    def events(self, rid=None, after=0):
        with self.connect() as db:
            sql, params = "SELECT * FROM events WHERE seq>?", [after]
            if rid:
                sql += " AND run_id=?"
                params.append(rid)
            return [{**dict(r), "data": json.loads(r["data"])} for r in db.execute(sql + " ORDER BY seq", params)]

    def successful(self):
        with self.connect() as db:
            return [self._run(r) for r in db.execute("SELECT * FROM runs WHERE status='SUCCEEDED' ORDER BY created DESC")]

    def complete_delivery(self, rid, receipt):
        """Receipt, current runtime ownership and success are one durable transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            finished = db.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
            if finished and finished[0] == "SUCCEEDED":
                prior = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key='delivery/receipt'", (rid,)).fetchone()
                if not prior or json.loads(prior[0]) != receipt:
                    raise AA2Error("Completed delivery cannot be replaced by a different receipt")
                return
            row = db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()
            body = json.loads(row[0])
            body.update(outcome=receipt["outcome"], branch=receipt["target"],
                        delivery_commit=receipt["candidate"], verified_source_hash=receipt["source_hash"],
                        cleanup_pending=True, delivery_phase="complete")
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)", (rid, "delivery/receipt", json.dumps(receipt)))
            db.execute("INSERT OR REPLACE INTO meta VALUES('current_delivery',?)", (rid,))
            db.execute("DELETE FROM delivery_pins")
            db.executemany("INSERT INTO delivery_pins VALUES(?,?)", [(p, rid) for p in receipt["runtime"]["resources"]])
            db.execute("UPDATE runs SET status='SUCCEEDED',stage='deliver',updated=?,data=? WHERE id=?",
                       (now(), json.dumps(body), rid))
            self._event(db, rid, "delivery.completed", {"target": receipt["target"], "commit": receipt["candidate"]})

    def task_origin_state(self, rid):
        """Read the registry and consumed task budgets from one database snapshot."""
        with self.connect() as db:
            rows = {row["key"]: json.loads(row["data"]) for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=? AND (key='task_origins' OR key LIKE 'rounds/%')", (rid,))}
        # Delivery repair has a separate budget, unrelated to task lineage.
        return {"origins": rows.get("task_origins", {}),
                "budgets": {key.removeprefix("rounds/"): value for key, value in rows.items()
                            if key.startswith("rounds/") and key != "rounds/delivery"}}

    def recovery_budgets(self, rid):
        with self.connect() as db:
            return {row["key"]: json.loads(row["data"]) for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=? AND "
                "(key LIKE 'recovery/budgets/%' OR key LIKE 'rounds/%' OR key LIKE 'budget_grants/%' "
                "OR key LIKE 'automation/%' OR key LIKE 'decisions/applied/%' OR key LIKE 'recovery/diagnosis_grants/%' "
                "OR key LIKE 'recovery/cycles/%' OR key LIKE 'recovery/permits/%')", (rid,))}

    def recovery_history(self, rid, owner):
        """Bound the decision context without discarding durable attempts or outcomes."""
        with self.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/permits/%' ORDER BY rowid", (rid,))]
        rows = [row for row in rows if row.get("owner") == owner]
        counts = {}
        for row in rows:
            action = row["action"]["action"]
            counts[action] = counts.get(action, 0) + 1
        return {"total_actions": len(rows), "by_action": counts, "recent": rows[-16:],
                "older_actions": max(0, len(rows) - 16)}

    def delivery_evidence(self, rid):
        result = {}
        with self.connect() as db:
            for row in db.execute("SELECT key,data FROM checkpoints WHERE run_id=?", (rid,)):
                key, value = row["key"], json.loads(row["data"])
                if key.startswith("develop/task/"):
                    result[key] = {k: value[k] for k in ("commit", "checks", "review", "source_hash") if k in value}
                elif key in {"capture", "clarify", "provider_research", "prototype", "prototype_approval", "design", "design/review-receipt", "plan", "issues",
                             "coverage_checks", "verify/identity", "verify/checks", "verify/review", "verify/visuals"} or key.startswith(
                                 ("rounds/", "budget_grants/", "plan_revision/", "task_state/", "recovery/", "migration/", "automation/", "decisions/")):
                    result[key] = value
            result["approvals"] = [dict(r) for r in db.execute("SELECT key,artifact_hash,answer FROM questions WHERE run_id=? AND answer IS NOT NULL", (rid,))]
        return result
