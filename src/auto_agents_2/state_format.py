"""Declared schema9 DDL and runtime guards; legacy conversion is external operator tooling."""

from __future__ import annotations

import re

from .state_connection import APPLICATION_ID, CODEC

SCHEMA = """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('schema_version','9');
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished ON runs((1))
                WHERE status NOT IN ('SUCCEEDED','CANCELLED');
\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20
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

CREATE TABLE state_format(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), codec TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('BUILDING','READY')), min_share_bytes INTEGER NOT NULL CHECK(min_share_bytes=512));
CREATE TABLE checkpoint_records(
    slot_order INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), key TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('text','blob','integer','real','null')),
    encoding TEXT NOT NULL CHECK(encoding IN ('wire-v1','opaque-sqlite')),
    literal_body BLOB NOT NULL, literal_bytes INTEGER NOT NULL CHECK(literal_bytes>=0),
    literal_sha TEXT NOT NULL, raw_sha TEXT, raw_bytes INTEGER,
    scalar_value, parse_status TEXT NOT NULL,
    UNIQUE(run_id,key), UNIQUE(slot_order,run_id));
CREATE TABLE state_objects(
    object_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL CHECK(kind IN ('request','permit_snapshot')),
    literal_body BLOB NOT NULL, literal_bytes INTEGER NOT NULL CHECK(literal_bytes>=0),
    literal_sha TEXT NOT NULL, raw_sha TEXT NOT NULL, raw_bytes INTEGER NOT NULL CHECK(raw_bytes>0),
    UNIQUE(run_id,kind,raw_sha), UNIQUE(object_id,run_id,kind));
CREATE TABLE state_checkpoint_refs(
    root_id INTEGER NOT NULL, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal=0),
    at INTEGER NOT NULL CHECK(at>=0), kind TEXT NOT NULL CHECK(kind='request'),
    raw_sha TEXT NOT NULL, raw_bytes INTEGER NOT NULL CHECK(raw_bytes>0),
    PRIMARY KEY(root_id,ordinal),
    FOREIGN KEY(root_id,run_id) REFERENCES checkpoint_records(slot_order,run_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id,kind,raw_sha) REFERENCES state_objects(run_id,kind,raw_sha));
CREATE INDEX state_checkpoint_children ON state_checkpoint_refs(run_id,kind,raw_sha);
CREATE TABLE state_object_refs(
    parent_id INTEGER NOT NULL, run_id TEXT NOT NULL,
    parent_kind TEXT NOT NULL DEFAULT 'request' CHECK(parent_kind='request'),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<511),
    at INTEGER NOT NULL CHECK(at>=0), kind TEXT NOT NULL CHECK(kind='permit_snapshot'),
    raw_sha TEXT NOT NULL, raw_bytes INTEGER NOT NULL CHECK(raw_bytes>0),
    PRIMARY KEY(parent_id,ordinal),
    FOREIGN KEY(parent_id,run_id,parent_kind) REFERENCES state_objects(object_id,run_id,kind) ON DELETE CASCADE,
    FOREIGN KEY(run_id,kind,raw_sha) REFERENCES state_objects(run_id,kind,raw_sha));
CREATE INDEX state_object_children ON state_object_refs(run_id,kind,raw_sha);
"""

TABLES = tuple(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)", SCHEMA))
PRIVATE_TABLES = {
    "state_format",
    "checkpoint_records",
    "state_objects",
    "state_checkpoint_refs",
    "state_object_refs",
}


def install_guards(db):
    for table in TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER guard_{table}_{operation.lower()} BEFORE {operation} ON {table}
                WHEN aa2_state_writer()!=9 BEGIN SELECT RAISE(ABORT,'schema 9 writer required'); END""")
    for operation in ("UPDATE", "DELETE"):
        db.execute(f"""CREATE TRIGGER immutable_format_{operation.lower()} BEFORE {operation} ON state_format
            BEGIN SELECT RAISE(ABORT,'state format metadata is immutable'); END""")
    db.execute("""CREATE TRIGGER immutable_version BEFORE UPDATE ON meta
        WHEN OLD.key='schema_version' AND (NEW.key!='schema_version' OR NEW.value!='9')
        BEGIN SELECT RAISE(ABORT,'state version is immutable'); END""")
    db.execute("""CREATE TRIGGER preserve_version BEFORE DELETE ON meta WHEN OLD.key='schema_version'
        BEGIN SELECT RAISE(ABORT,'state version is immutable'); END""")
    db.execute("""CREATE TRIGGER immutable_object_update BEFORE UPDATE ON state_objects
        BEGIN SELECT RAISE(ABORT,'state objects are immutable'); END""")
    db.execute("""CREATE TRIGGER object_delete_gate BEFORE DELETE ON state_objects WHEN aa2_state_object_gc()!=1
        BEGIN SELECT RAISE(ABORT,'private object GC required'); END""")
    db.execute("""CREATE TRIGGER object_refs_insert_gate BEFORE INSERT ON state_object_refs
        WHEN aa2_state_object_new(NEW.parent_id)!=1 BEGIN SELECT RAISE(ABORT,'object references are frozen'); END""")
    db.execute("""CREATE TRIGGER immutable_object_refs_update BEFORE UPDATE ON state_object_refs
        BEGIN SELECT RAISE(ABORT,'object references are immutable'); END""")
    db.execute("""CREATE TRIGGER object_refs_delete_gate BEFORE DELETE ON state_object_refs WHEN aa2_state_object_gc()!=1
        BEGIN SELECT RAISE(ABORT,'private object GC required'); END""")


def create_schema(db, *, status="READY", guards=True):
    if db.execute("PRAGMA encoding").fetchone()[0] != "UTF-8":
        raise ValueError("State format 9 requires UTF-8 encoding")
    if status not in {"READY", "BUILDING"}:
        raise ValueError("invalid schema lifecycle")
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT INTO state_format VALUES(1,?,?,512)", (CODEC, status))
    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    if guards:
        install_guards(db)
