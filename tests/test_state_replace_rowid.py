from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from auto_agents_2.state_codec import checkpoint_query_db, put_wire_db
from auto_agents_2.state_wire import Integrity
from auto_agents_2.store import Store

REPLACED = "decisions/requests/replace"
OTHER = "decisions/requests/other"


def wire(label):
    permit = {"phase": "completed", "unknown": label + "p" * 700}
    request = {"evidence": {"history": {"recent": [permit, permit]}}, "unknown": label + "q" * 700}
    return json.dumps({"request": request})


def native_rows(db):
    return db.execute("SELECT rowid,key,data FROM checkpoints ORDER BY rowid").fetchall()


def core_rows(db):
    return [
        tuple(row)
        for row in checkpoint_query_db(
            db,
            "SELECT slot_order,key,slot_order AS data FROM checkpoint_records ORDER BY slot_order",
            wire_index=2,
        )
    ]


def initial_rows(highest, shared):
    data = [(OTHER, wire("old") if shared else wire("other")), (REPLACED, wire("old"))]
    return data if highest else list(reversed(data))


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    with result.connect() as db:
        db.execute("INSERT INTO runs VALUES('r','PAUSED','review',1,2,'{}')")
    return result


@pytest.mark.parametrize("highest", [False, True])
@pytest.mark.parametrize("same", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_native_rowid_replace_oracle_preserves_raw_rows_and_independent_references(
    store, highest, same, shared
):
    rows = initial_rows(highest, shared)
    replacement = wire("old") if same else wire("new")
    # Exact old physical operation as an independent SQLite oracle, not runtime compatibility.
    with closing(sqlite3.connect(":memory:")) as native:
        native.execute(
            "CREATE TABLE checkpoints(run_id TEXT NOT NULL,key TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(run_id,key))"
        )
        for key, raw in rows:
            native.execute("INSERT INTO checkpoints VALUES('r',?,?)", (key, raw))
        with store.connect() as db:
            for key, raw in rows:
                put_wire_db(db, "r", key, raw)
            assert core_rows(db) == native_rows(native)
            native.execute("INSERT OR REPLACE INTO checkpoints VALUES('r',?,?)", (REPLACED, replacement))
            put_wire_db(db, "r", REPLACED, replacement, replace=True)
            assert core_rows(db) == native_rows(native)
            logical = [json.loads(raw)["request"] for _, _, raw in native_rows(native)]
            expected_requests = {json.dumps(value) for value in logical}
            expected_permits = {
                json.dumps(p) for value in logical for p in value["evidence"]["history"]["recent"]
            }
            counts = dict(db.execute("SELECT kind,count(*) FROM state_objects GROUP BY kind"))
            assert counts == {"request": len(expected_requests), "permit_snapshot": len(expected_permits)}
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("highest", [False, True])
def test_ordinary_upsert_keeps_its_original_slot_oracle(store, highest):
    rows = initial_rows(highest, False)
    with closing(sqlite3.connect(":memory:")) as native:
        native.execute("CREATE TABLE checkpoints(run_id TEXT,key TEXT,data TEXT,PRIMARY KEY(run_id,key))")
        for key, raw in rows:
            native.execute("INSERT INTO checkpoints VALUES('r',?,?)", (key, raw))
        with store.connect() as db:
            for key, raw in rows:
                put_wire_db(db, "r", key, raw)
            native.execute(
                "INSERT INTO checkpoints VALUES('r',?,?) ON CONFLICT(run_id,key) DO UPDATE SET data=excluded.data",
                (REPLACED, wire("new")),
            )
            put_wire_db(db, "r", REPLACED, wire("new"))
            assert core_rows(db) == native_rows(native)


@pytest.mark.parametrize("fault", ["root", "reference", "gc"])
@pytest.mark.parametrize("caught", [False, True])
def test_replace_failure_preserves_native_oracle_and_new_reference_atomicity(store, fault, caught):
    rows = initial_rows(True, False)
    with closing(sqlite3.connect(":memory:")) as native:
        native.execute("CREATE TABLE checkpoints(run_id TEXT,key TEXT,data TEXT,PRIMARY KEY(run_id,key))")
        for key, raw in rows:
            native.execute("INSERT INTO checkpoints VALUES('r',?,?)", (key, raw))
        native.commit()
        before = native_rows(native)
        native.execute(
            "CREATE TRIGGER injected AFTER INSERT ON checkpoints WHEN NEW.key='decisions/requests/replace' BEGIN SELECT RAISE(ABORT,'replacement fault'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            native.execute("INSERT OR REPLACE INTO checkpoints VALUES('r',?,?)", (REPLACED, wire("new")))
        native.rollback()
        assert native_rows(native) == before
        with store.connect() as db:
            for key, raw in rows:
                put_wire_db(db, "r", key, raw)
        with store.connect() as db:
            if fault == "root":
                db.execute(
                    "CREATE TRIGGER injected AFTER INSERT ON checkpoint_records WHEN NEW.key='decisions/requests/replace' BEGIN SELECT RAISE(ABORT,'replacement fault'); END"
                )
            elif fault == "reference":
                db.execute(
                    "CREATE TRIGGER injected AFTER INSERT ON state_checkpoint_refs WHEN (SELECT key FROM checkpoint_records WHERE slot_order=NEW.root_id)='decisions/requests/replace' BEGIN SELECT RAISE(ABORT,'replacement fault'); END"
                )
            else:
                db.execute(
                    "CREATE TRIGGER injected AFTER DELETE ON state_objects BEGIN SELECT RAISE(ABORT,'replacement fault'); END"
                )
        with pytest.raises(sqlite3.IntegrityError):
            with store.connect() as db:
                if caught:
                    with pytest.raises(sqlite3.IntegrityError):
                        put_wire_db(db, "r", REPLACED, wire("new"), replace=True)
                    db.execute("UPDATE runs SET stage='unsafe-commit' WHERE id='r'")
                    db.execute("COMMIT")
                else:
                    put_wire_db(db, "r", REPLACED, wire("new"), replace=True)
        with store.connect() as db:
            assert core_rows(db) == native_rows(native)
            assert dict(db.execute("SELECT kind,count(*) FROM state_objects GROUP BY kind")) == {
                "request": 2,
                "permit_snapshot": 2,
            }
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.run("r")["stage"] == "review"


def test_replace_validates_old_root_and_poison_before_replacing(store):
    with store.connect() as db:
        slot = put_wire_db(db, "r", REPLACED, wire("old"))
    with pytest.raises(Integrity):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE state_checkpoint_refs SET raw_bytes=raw_bytes+1 WHERE root_id=?", (slot,))
            with pytest.raises(Integrity):
                put_wire_db(db, "r", REPLACED, wire("new"), replace=True)
            assert (
                db.execute("SELECT slot_order FROM checkpoint_records WHERE key=?", (REPLACED,)).fetchone()[0]
                == slot
            )
            db.execute("UPDATE runs SET stage='unsafe-commit' WHERE id='r'")
    assert store.run("r")["stage"] == "review"
    assert store.get("r", REPLACED) == json.loads(wire("old"))
