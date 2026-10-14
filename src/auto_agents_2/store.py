from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .diagnostics import adopt_logs
from .errors import AA2Error, IncompleteIteration
from .interaction import validate_answer
from .replanning import assess
from .util import digest, identifier, now, redact

TERMINAL = ("SUCCEEDED", "CANCELLED")


class Store:
    def __init__(self, project: Path):
        self.root = project.resolve() / ".aa2"
        if self.root.is_symlink():
            raise AA2Error("Project state must be a local directory, not a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "state.sqlite"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('schema_version','1');
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
                created REAL NOT NULL, UNIQUE(run_id,key,artifact_hash));
            CREATE TABLE IF NOT EXISTS usage(
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                provider TEXT NOT NULL, attempt TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resources(
                id TEXT PRIMARY KEY, run_id TEXT, kind TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, refs INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                touched REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_pins(path TEXT PRIMARY KEY, run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifact_owners(hash TEXT NOT NULL, run_id TEXT NOT NULL,
                PRIMARY KEY(hash,run_id));
            CREATE TABLE IF NOT EXISTS artifact_edges(parent TEXT, child TEXT, PRIMARY KEY(parent,child));
            CREATE TABLE IF NOT EXISTS artifact_kinds(hash TEXT, kind TEXT, PRIMARY KEY(hash,kind));
            CREATE TABLE IF NOT EXISTS diagnostic_holds(
                id TEXT PRIMARY KEY, hash TEXT NOT NULL, run_id TEXT, purpose TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS retired_diagnostics(
                hash TEXT PRIMARY KEY, retired REAL NOT NULL, reason TEXT NOT NULL);
            """)
            if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "1":
                raise AA2Error("Unsupported state database version")
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
            if "interaction" not in columns:
                db.execute("ALTER TABLE questions ADD COLUMN interaction TEXT NOT NULL DEFAULT '{}'")
            if "superseded" not in columns:
                db.execute("ALTER TABLE questions ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

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
                   (rid, now(), kind, json.dumps(redact(data), ensure_ascii=False)))

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
                db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                           (rid, key, json.dumps(data, ensure_ascii=False)))
                adopt_logs(db, self.root, data)
                self._event(db, rid, "checkpoint.saved", {"key": key})

    def get(self, rid, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key=?", (rid, key)).fetchone()
            return json.loads(row[0]) if row else default

    def invalidate(self, rid, keys: list[str]):
        with self.connect() as db:
            for key in keys:
                db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR key LIKE ?)",
                           (rid, key, key + "/%"))
            self._event(db, rid, "checkpoints.invalidated", {"keys": keys})

    def prepare_replan(self, rid, stage, reason):
        """Journal the decision and counters together, before asking or invalidating."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
                           (rid, key, json.dumps(data, ensure_ascii=False)))
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
            for stage in stages:
                for prefix in ("candidate/", "invocation/", "provider-result/"):
                    db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR key LIKE ?)",
                               (rid, prefix + stage, prefix + stage + "/%"))
                db.execute("DELETE FROM checkpoints WHERE run_id=? AND (key=? OR key LIKE ?)",
                           (rid, stage, stage + "/%"))
            feedback = {"stage": pending["stage"], "reason": pending["reason"], "recovery_refs": recovery_refs}
            db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                       (rid, "replan_feedback", json.dumps(feedback, ensure_ascii=False)))
            db.execute("UPDATE runs SET status='RUNNING',stage=?,updated=?,data=? WHERE id=?",
                       (pending["stage"], now(), json.dumps(body), rid))
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key='replanning/pending'", (rid,))
            self._event(db, rid, "replan.completed", {"stage": pending["stage"], "replans": body["replans"],
                                                    "recovery_refs": recovery_refs, "granted": pending["granted"]})

    @staticmethod
    def _question(row):
        return {**dict(row), "interaction": json.loads(row["interaction"])}

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
            return [self._question(r) for r in db.execute(
                "SELECT * FROM questions WHERE run_id=? AND answer IS NULL AND superseded=0 ORDER BY created", (rid,))]

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

    def delivery_evidence(self, rid):
        result = {}
        with self.connect() as db:
            for row in db.execute("SELECT key,data FROM checkpoints WHERE run_id=?", (rid,)):
                key, value = row["key"], json.loads(row["data"])
                if key.startswith("develop/task/"):
                    result[key] = {k: value[k] for k in ("commit", "checks", "review", "source_hash") if k in value}
                elif key in {"capture", "clarify", "provider_research", "prototype", "prototype_approval", "design", "plan",
                             "coverage_checks", "verify/identity", "verify/checks", "verify/review", "verify/visuals"} or key.startswith("rounds/"):
                    result[key] = value
            result["approvals"] = [dict(r) for r in db.execute("SELECT key,artifact_hash,answer FROM questions WHERE run_id=? AND answer IS NOT NULL", (rid,))]
        return result
