"""Continuity reads only its complete proof, on the caller's real transaction."""
from __future__ import annotations

import copy
import json
import sqlite3
from collections import Counter

import pytest
from test_recovery_contracts import failure
from test_task_contract_continuity import local_timeout_patch
from test_task_plan_continuity import amended, invalidate

from auto_agents_2 import state_codec
from auto_agents_2 import task_plan_continuity as continuity
from auto_agents_2.errors import AA2Error
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import PlanResult
from auto_agents_2.state_codec import CheckpointReader, checkpoint_get_db, put_wire_db
from auto_agents_2.state_connection import StateConnection
from auto_agents_2.state_wire import Integrity
from auto_agents_2.store import Store
from auto_agents_2.util import digest


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    with result.connect() as db:
        db.execute("INSERT INTO runs VALUES('r','PAUSED','review',1,2,'{}')")
    return result


def snapshot(store):
    # Public synthetic fixture only. Compare exact durable rows without decoding.
    tables = ("checkpoint_records", "state_objects", "state_checkpoint_refs",
              "state_object_refs", "events", "runs", "questions", "sqlite_sequence")
    with store.connect() as db:
        return {name: [tuple(row) for row in db.execute("SELECT * FROM " + name + " ORDER BY rowid")]
                for name in tables}


def journal_key(store, rid):
    with store.connect() as db:
        return store.checkpoint_keys_db(db, rid, prefix="plan_revision/journal/", order="rowid")[0]


def test_view_keeps_null_missing_key_types_and_one_decode_per_key(store, monkeypatch):
    counts = Counter()
    get = store.checkpoint_get_db

    def counted(db, rid, key, default=None, **kwargs):
        counts[key] += 1
        return get(db, rid, key, default, **kwargs)

    monkeypatch.setattr(store, "checkpoint_get_db", counted)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for key, value in (("present", '{"x":1}'), ("null", "null"), ("1", '"text-key"')):
            put_wire_db(db, "r", key, value)
        rows = continuity._CheckpointView(store, db, "r")
        assert rows.get("present") is rows["present"]
        assert rows.get("null", "not-null") is None and rows["null"] is None
        assert rows.get("absent", 1) == 1 and rows.get("absent", 2) == 2
        with pytest.raises(KeyError, match="absent"):
            rows["absent"]
        assert rows.get(1, "missing") == "missing"
        assert rows["1"] == "text-key"
        for operation in (lambda: rows.get([]), lambda: rows[{}]):
            with pytest.raises(TypeError) as caught:
                operation()
            assert not isinstance(caught.value, continuity._StateReadFailure)
        assert counts == {"present": 1, "null": 1, "absent": 1, 1: 1, "1": 1}


def test_view_uses_same_connection_and_uncommitted_state_and_answers(store):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        put_wire_db(db, "r", "uncommitted", '{"version":2}')
        db.execute("INSERT INTO questions(id,run_id,key,prompt,artifact_hash,answer,created,interaction) "
                   "VALUES('q','r','design/1','A public fixture question','fixture','answer',1,'{}')")
        rows = continuity._CheckpointView(store, db, "r")
        assert rows.reader.db is db
        assert rows["uncommitted"] == {"version": 2}
        assert rows["authority/submitted-answers"][0]["answer"] == "answer"
        with store.connect() as other:
            assert checkpoint_get_db(other, "r", "uncommitted", "absent") == "absent"
            assert store._submitted_answers(other, "r") == []
        db.rollback()
    assert store.get("r", "uncommitted") is None


def test_write_epoch_clears_values_missing_answers_and_journal_keys(store, monkeypatch):
    keys_calls = []
    keys = store.checkpoint_keys_db

    def counted(*args, **kwargs):
        keys_calls.append(kwargs)
        return keys(*args, **kwargs)

    monkeypatch.setattr(store, "checkpoint_keys_db", counted)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        put_wire_db(db, "r", "value", "1")
        put_wire_db(db, "r", "plan_revision/journal/z", '{"n":1}')
        rows = continuity._CheckpointView(store, db, "r")
        assert rows["value"] == 1 and rows.get("missing") is None
        assert rows["authority/submitted-answers"] == []
        assert list(rows.journal_items()) == [("plan_revision/journal/z", {"n": 1})]
        assert list(rows.journal_items()) == [("plan_revision/journal/z", {"n": 1})]
        assert len(keys_calls) == 1
        put_wire_db(db, "r", "value", "2")
        put_wire_db(db, "r", "missing", "3")
        put_wire_db(db, "r", "plan_revision/journal/a", '{"n":2}')
        db.execute("INSERT INTO questions(id,run_id,key,prompt,artifact_hash,answer,created,interaction) "
                   "VALUES('q','r','design/1','Fixture','fixture','new answer',1,'{}')")
        assert rows["value"] == 2 and rows["missing"] == 3
        assert rows["authority/submitted-answers"][0]["answer"] == "new answer"
        assert list(rows.journal_items()) == [("plan_revision/journal/z", {"n": 1}),
                                              ("plan_revision/journal/a", {"n": 2})]
        assert len(keys_calls) == 2
        assert all(call["reader"] is rows.reader and call["order"] == "rowid" for call in keys_calls)


@pytest.mark.parametrize("ending", ["commit", "rollback", "sql_commit", "sql_rollback"])
def test_old_view_rejects_every_cached_missing_and_keys_path_after_transaction(store, ending):
    with store.connect() as db:
        put_wire_db(db, "r", "null", "null")
    with store.connect() as db:
        db.execute("BEGIN")
        rows = continuity._CheckpointView(store, db, "r")
        assert rows["null"] is None and rows.get("missing") is None
        assert list(rows.journal_items()) == []
        if ending.startswith("sql_"):
            db.execute(ending.removeprefix("sql_").upper())
        else:
            getattr(db, ending)()
        db.execute("BEGIN")
        for operation in (lambda: rows.get("null"), lambda: rows["null"],
                          lambda: rows.get("missing", 9), lambda: rows.get(1), rows.journal_items):
            with pytest.raises(continuity._StateReadFailure) as caught:
                operation()
            assert isinstance(caught.value.error, Integrity)
        fresh = continuity._CheckpointView(store, db, "r")
        assert fresh.reader is not rows.reader and fresh["null"] is None


def test_journal_literal_prefix_and_row_order_do_not_read_similar_names(store):
    with store.connect() as db:
        for key, raw in (("planXrevision/journal/trap", "not json"),
                         ("plan_revision/journal/z_%", '{"n":1}'),
                         ("plan_revision/journal/a", '{"n":2}')):
            put_wire_db(db, "r", key, raw)
        rows = continuity._CheckpointView(store, db, "r")
        assert list(rows.journal_items()) == [("plan_revision/journal/z_%", {"n": 1}),
                                              ("plan_revision/journal/a", {"n": 2})]
        assert "planXrevision/journal/trap" not in rows.values


async def test_full_multihop_namespace_and_dict_chain_remain_equivalent(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path)
    workflow.recovery.submit(rid, failure(evidence={"next": "second unrelated amendment"}))
    await workflow.recovery.resume(rid)
    plan = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    workflow.workspaces.checkpoint(work, "refs/aa2/fixture/lazy-second", "second reviewed local amendment")
    with workflow.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        view = continuity._CheckpointView(workflow.store, db, rid)
        rows = {key: workflow.store.checkpoint_get_db(db, rid, key)
                for key in workflow.store.checkpoint_keys_db(db, rid)}
        rows["authority/submitted-answers"] = workflow.store._submitted_answers(db, rid)
        identity = continuity.source_identity(view.get)
        basis = rows["authority/reviews/" + digest(records["T1"]["review"])]["basis"]
        eager = continuity._chain(rows, identity, plan.tasks[0], records["T1"], basis)
        lazy = continuity._chain(view, identity, plan.tasks[0], records["T1"], basis,
                                 journal_items=view.journal_items)
        assert lazy == eager and len(lazy[0]) == 2
    assert workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work) == records["T1"]
    assert len(workflow.store.get(rid, "authority/task-continuity/T1")["transitions"]) == 2


@pytest.mark.parametrize("extra", ["duplicate", "malformed_unmatched"])
async def test_complete_journal_cannot_stop_at_first_matching_row(tmp_path, extra):
    workflow, rid, work, plan, records = await amended(tmp_path)
    key = journal_key(workflow.store, rid)
    with workflow.store.connect() as db:
        value = json.dumps(workflow.store.checkpoint_get_db(db, rid, key)) if extra == "duplicate" else "{"
        put_wire_db(db, rid, "plan_revision/journal/late_%_extra", value)
    before = snapshot(workflow.store)
    if extra == "duplicate":
        assert continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work) is None
    else:
        with pytest.raises(json.JSONDecodeError):
            continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work)
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("required", ["capture", "plan", "revision", "review", "journal", "history", "proof"])
async def test_required_malformed_json_raises_without_reverify_or_persistent_change(tmp_path, required):
    workflow, rid, work, plan, records = await amended(tmp_path)
    keys = {"capture": "capture", "plan": "plan", "revision": "plan_revision/current",
            "review": "authority/reviews/" + digest(records["T1"]["review"]),
            "journal": journal_key(workflow.store, rid), "history": "plan_revision/history/1",
            "proof": "authority/task-continuity/T1"}
    with workflow.store.connect() as db:
        put_wire_db(db, rid, keys[required], "{")
    before = snapshot(workflow.store)
    with pytest.raises(json.JSONDecodeError):
        workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work)
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("raw,expected", [(b'{"x":"\xff"}', UnicodeDecodeError), (3, TypeError)])
async def test_required_blob_encoding_and_opaque_scalar_errors_keep_original_types(tmp_path, raw, expected):
    workflow, rid, work, plan, records = await amended(tmp_path)
    with workflow.store.connect() as db:
        put_wire_db(db, rid, journal_key(workflow.store, rid), raw)
    before = snapshot(workflow.store)
    with pytest.raises(expected):
        workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work)
    assert snapshot(workflow.store) == before


async def test_actual_read_integrity_failure_is_not_business_reverify(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path)
    key = journal_key(workflow.store, rid)
    with workflow.store.connect() as db:
        slot = db.execute("SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?", (rid, key)).fetchone()[0]
        with db.blobopen("checkpoint_records", "literal_body", slot) as body:
            body.write(b"!")
    before = snapshot(workflow.store)
    with pytest.raises(Integrity):
        workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work)
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("damage", ["missing", "scope", "body"])
def test_required_shared_object_corruption_cannot_become_missing_or_business_fallback(store, damage):
    request = {"evidence": {"history": {"recent": [{"phase": "started", "x": "p" * 700}]}},
               "x": "q" * 700}
    key = "decisions/requests/required"
    with store.connect() as db:
        # Deliberately model an out-of-band damaged public fixture. Normal writes
        # correctly reject broken references; no product connection is changed.
        if damage in {"missing", "scope"}:
            db.execute("PRAGMA foreign_keys=OFF")
        slot = put_wire_db(db, "r", key, json.dumps({"request": request}))
        if damage == "missing":
            db.execute("UPDATE state_checkpoint_refs SET raw_sha=? WHERE root_id=?", ("0" * 64, slot))
        elif damage == "scope":
            db.execute("UPDATE state_checkpoint_refs SET run_id='another-run' WHERE root_id=?", (slot,))
        else:
            oid = db.execute("SELECT object_id FROM state_objects WHERE kind='permit_snapshot'").fetchone()[0]
            with db.blobopen("state_objects", "literal_body", oid) as body:
                body.write(b"!")
    before = snapshot(store)
    with pytest.raises(continuity._StateReadFailure) as caught:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = continuity._CheckpointView(store, db, "r")
            rows.get(key, "must-not-return-default")
    assert isinstance(caught.value.error, Integrity)
    assert snapshot(store) == before


@pytest.mark.parametrize("kind", ["aa2", "key", "type", "value", "attribute", "os", "sqlite", "json", "integrity"])
async def test_read_boundary_preserves_same_exception_and_cause_even_inside_proof(tmp_path, monkeypatch, kind):
    workflow, rid, work, plan, records = await amended(tmp_path)
    errors = {"aa2": AA2Error("fixture read"), "key": KeyError("fixture read"),
              "type": TypeError("fixture read"), "value": ValueError("fixture read"),
              "attribute": AttributeError("fixture read"), "os": OSError("fixture read"),
              "sqlite": sqlite3.OperationalError("fixture read"),
              "json": json.JSONDecodeError("fixture read", "{", 1), "integrity": Integrity("fixture read")}
    error = errors[kind]
    cause = RuntimeError("original fixture cause")
    error.__cause__ = cause
    key = journal_key(workflow.store, rid)
    get = workflow.store.checkpoint_get_db

    def failed(db, run_id, selected, *args, **kwargs):
        if selected == key:
            raise error
        return get(db, run_id, selected, *args, **kwargs)

    before = snapshot(workflow.store)
    monkeypatch.setattr(workflow.store, "checkpoint_get_db", failed)
    with pytest.raises(type(error)) as caught:
        continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work)
    assert caught.value is error and caught.value.__cause__ is cause
    frames = []
    trace = caught.value.__traceback__
    while trace:
        frames.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert "failed" in frames
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("late", [False, True])
async def test_keys_read_or_iterator_failure_escapes_proof_catch(tmp_path, monkeypatch, late):
    workflow, rid, work, plan, records = await amended(tmp_path)
    error = ValueError("fixture keys read")

    def failed(*args, **kwargs):
        if late:
            def broken():
                yield "plan_revision/journal/first"
                raise error
            return broken()
        raise error

    before = snapshot(workflow.store)
    monkeypatch.setattr(workflow.store, "checkpoint_keys_db", failed)
    with pytest.raises(ValueError) as caught:
        continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work)
    assert caught.value is error
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("required", ["review", "journal", "history"])
async def test_legal_json_wrong_business_shape_still_uses_original_reverify(tmp_path, required):
    workflow, rid, work, plan, records = await amended(tmp_path)
    keys = {"review": "authority/reviews/" + digest(records["T1"]["review"]),
            "journal": journal_key(workflow.store, rid), "history": "plan_revision/history/1"}
    with workflow.store.connect() as db:
        put_wire_db(db, rid, keys[required], "[]")
    before = snapshot(workflow.store)
    assert continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work) is None
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("stage", ["proof", "record", "state", "done", "event"])
async def test_every_publication_stage_rolls_back_and_normal_retry_recovers(tmp_path, monkeypatch, stage):
    workflow, rid, work, plan, records = await amended(tmp_path)
    saved = invalidate(workflow, rid, plan.tasks[0], records["T1"])
    keys = {"proof": "authority/task-continuity/T1", "record": "develop/task/T1",
            "state": "task_state/T1", "done": "develop/done"}
    put, event = workflow.store._put, workflow.store._event
    before = snapshot(workflow.store)

    def failed_put(db, run_id, key, value):
        put(db, run_id, key, value)
        if key == keys.get(stage):
            raise sqlite3.OperationalError("fixture interrupted publication")

    def failed_event(*args):
        event(*args)
        raise sqlite3.OperationalError("fixture interrupted publication")

    monkeypatch.setattr(workflow.store, "_put", failed_put)
    if stage == "event":
        monkeypatch.setattr(workflow.store, "_event", failed_event)
    with pytest.raises(sqlite3.OperationalError, match="interrupted publication"):
        continuity.retain(workflow, rid, plan.tasks[0], saved, work)
    assert snapshot(workflow.store) == before
    monkeypatch.setattr(workflow.store, "_put", put)
    monkeypatch.setattr(workflow.store, "_event", event)
    assert workflow.authority.retain_task(rid, plan.tasks[0], saved, work=work) == records["T1"]
    assert workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work) == records["T1"]
    assert len([e for e in workflow.store.events(rid) if e["kind"] == "task.authority_retained"]) == 1
    assert workflow.store.get(rid, "rounds/T1") == {"round": 7}


async def test_stale_saved_record_keeps_existing_cas(tmp_path):
    workflow, rid, work, plan, records = await amended(tmp_path)
    changed = {**records["T1"], "feedback": {"new": "work"}}
    workflow.store.checkpoint(rid, "develop/task/T1", changed)
    before = snapshot(workflow.store)
    with pytest.raises(StateConflict, match="acceptance changed"):
        continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work)
    assert snapshot(workflow.store) == before


@pytest.mark.parametrize("contract", [False, True])
async def test_unrelated_large_slots_and_shared_wrappers_add_no_reads_or_decodes(tmp_path, monkeypatch, contract):
    if contract:
        local_timeout_patch(monkeypatch)
    workflow, rid, work, plan, records = await amended(tmp_path)
    assert continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work) == records["T1"]
    decoded, roots, objects, statements = [], [], [], []
    loads, wire, blob, before_sql = json.loads, CheckpointReader.wire_value, StateConnection.blobopen, StateConnection._before_sql

    def counted_loads(value, *args, **kwargs):
        decoded.append(len(value.encode("utf-8")) if isinstance(value, str) else len(value))
        return loads(value, *args, **kwargs)

    def counted_wire(reader, slot):
        roots.append(slot)
        return wire(reader, slot)

    def counted_blob(db, table, column, row, *args, **kwargs):
        if table == "state_objects":
            objects.append(row)
        return blob(db, table, column, row, *args, **kwargs)

    def counted_sql(db, statement):
        statements.append(statement)
        return before_sql(db, statement)

    def observe():
        decoded.clear()
        roots.clear()
        objects.clear()
        statements.clear()
        with monkeypatch.context() as patch:
            patch.setattr(json, "loads", counted_loads)
            patch.setattr(CheckpointReader, "wire_value", counted_wire)
            patch.setattr(StateConnection, "blobopen", counted_blob)
            patch.setattr(StateConnection, "_before_sql", counted_sql)
            assert continuity.retain(workflow, rid, plan.tasks[0], records["T1"], work) == records["T1"]
        return copy.deepcopy((decoded, roots, objects, statements))

    baseline = observe()
    request = {"evidence": {"history": {"recent": [{"phase": "started", "x": "p" * 1024}]}}, "large": "q" * 8192}
    wrappers = {"recovery/request-snapshots/lazy-unused": {"request": request},
                "decisions/requests/lazy-unused": {"request": request},
                "decisions/applied/lazy-unused": {"request": request},
                "recovery/invocation-inputs/lazy-unused": {"context": {"decision_request": request}}}
    with workflow.store.connect() as db:
        for key, value in wrappers.items():
            put_wire_db(db, rid, key, json.dumps(value))
        put_wire_db(db, rid, "unused/large", json.dumps({"large": "z" * (4 * 1024**2 + 1)}))
        put_wire_db(db, rid, "unused/malformed", "not json")
        put_wire_db(db, rid, "planXrevision/journal/unused", "not json")
        unused = {row[0] for row in db.execute("SELECT slot_order FROM checkpoint_records WHERE run_id=? AND "
                                             "(key LIKE '%lazy-unused' OR key LIKE 'unused/%' OR key LIKE 'planXrevision/%')", (rid,))}
        assert db.execute("SELECT count(*) FROM state_objects WHERE run_id=?", (rid,)).fetchone()[0] >= 2
    after = observe()
    assert after == baseline
    assert not unused.intersection(after[1]) and not after[2]
    assert max(Counter(after[1]).values()) == 1
    assert not any("SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=?" in sql for sql in after[3])
    assert sum(after[0]) < 4 * 1024**2
    assert state_codec.json.loads is json.loads
