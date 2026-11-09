from __future__ import annotations

import codecs
import hashlib
import json
import sqlite3

import pytest
from state_format_fixtures import LEGACY_SCHEMA8
from typer.testing import CliRunner

from auto_agents_2.cli import app
from auto_agents_2.config import ConfigStore
from auto_agents_2.errors import AA2Error
from auto_agents_2.state_codec import (
    checkpoint_get_db,
    checkpoint_keys_db,
    checkpoint_query_db,
    checkpoint_reader,
    checkpoint_rows_db,
    checkpoint_wire_bytes_db,
    checkpoint_wire_sha256_db,
    delete_query_db,
    put_wire_db,
    update_wire_db,
)
from auto_agents_2.state_connection import StateConnection, migration_gate, require_format
from auto_agents_2.state_wire import Integrity
from auto_agents_2.store import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    with result.connect() as db:
        db.execute("INSERT INTO runs VALUES('r','PAUSED','review',1,2,'{}')")
        db.execute("INSERT INTO runs VALUES('s','SUCCEEDED','done',1,2,'{}')")
    return result


def request_wire():
    permit = {"phase": "started", "token": "unknown", "x": "p" * 700}
    request = {"evidence": {"history": {"recent": [permit, permit]}}, "x": "q" * 700}
    return request, json.dumps(request, ensure_ascii=False, separators=(", ", ": "))


def test_known_wrappers_share_frozen_versions_without_top_level_hash_assumptions(store):
    request, wire = request_wire()
    wrappers = (
        ("recovery/request-snapshots/a", '{"meta":1,"request":' + wire + "}"),
        ("decisions/requests/a", '{"meta":2,"request":' + wire + "}"),
        ("decisions/applied/a", '{"meta":3,"request":' + wire + "}"),
        ("recovery/invocation-inputs/a", '{"context":{"decision_request":' + wire + "}}"),
    )
    with store.connect() as db:
        for key, raw in wrappers:
            put_wire_db(db, "r", key, raw)
        assert db.execute("SELECT count(*) FROM state_objects").fetchone()[0] == 2
        for key, raw in wrappers:
            assert checkpoint_wire_bytes_db(db, "r", key) == raw.encode()
            assert checkpoint_wire_sha256_db(db, "r", key) == hashlib.sha256(raw.encode()).hexdigest()
        assert checkpoint_get_db(db, "r", wrappers[0][0])["request"] == request
        put_wire_db(db, "r", "recovery/permits/current", json.dumps({"phase": "completed"}))
        assert (
            checkpoint_get_db(db, "r", wrappers[0][0])["request"]["evidence"]["history"]["recent"][0]["phase"]
            == "started"
        )
        # Same raw bytes in another run never merge scopes.
        put_wire_db(db, "s", wrappers[0][0], wrappers[0][1])
        assert db.execute("SELECT count(*) FROM state_objects").fetchone()[0] == 4


@pytest.mark.parametrize(
    "source_type,raw,error",
    [
        ("text", codecs.BOM_UTF8 + b'{"x":1}', json.JSONDecodeError),
        ("blob", codecs.BOM_UTF8 + b'{"x":1}', None),
        ("text", b'{"a":1,"a":2, "unknown":{"$ref":"business"}}', None),
        ("blob", b'{"x":"\xff"}', UnicodeError),
        ("text", b"not json", json.JSONDecodeError),
    ],
)
def test_wire_and_original_sqlite_decode_type(store, source_type, raw, error):
    with store.connect() as db:
        value = raw.decode() if source_type == "text" else raw
        put_wire_db(db, "r", "test", value)
        assert checkpoint_wire_bytes_db(db, "r", "test") == raw
        if error:
            with pytest.raises(error):
                checkpoint_get_db(db, "r", "test")
        else:
            assert checkpoint_get_db(db, "r", "test") == json.loads(value)


def test_keys_are_literal_no_body_and_no_sql_affinity_lookup(store, monkeypatch):
    with store.connect() as db:
        for key in ("x_%/a", "xyz/a", "1", "x_%/你"):
            put_wire_db(db, "r", key, "null")

        def no_body(*args, **kwargs):
            raise AssertionError("keys read a body")

        monkeypatch.setattr(StateConnection, "blobopen", no_body)
        assert checkpoint_keys_db(db, "r", prefix="x_%/") == ["x_%/a", "x_%/你"]
        assert checkpoint_get_db(db, "r", 1, "missing") == "missing"
        with pytest.raises(ValueError):
            checkpoint_rows_db(db, "r")


@pytest.mark.parametrize("finish", ["commit", "rollback", "sql_commit", "sql_rollback"])
def test_reader_rejects_new_read_only_transaction_on_same_connection_without_changes(store, finish):
    with store.connect() as db:
        db.execute("BEGIN")
        reader = checkpoint_reader(db)
        before = db.total_changes
        if finish.startswith("sql"):
            db.execute(finish[4:].upper())
        else:
            getattr(db, finish)()
        db.execute("BEGIN")
        assert db.total_changes == before and db.in_transaction
        with pytest.raises(Integrity, match="transaction"):
            reader.check()
        assert checkpoint_reader(db) is not reader


def test_same_connection_write_clears_all_raw_cache_and_get_has_no_mutable_alias(store):
    request, wire = request_wire()
    with store.connect() as db:
        put_wire_db(db, "r", "decisions/requests/a", '{"request":' + wire + "}")
        reader = checkpoint_reader(db)
        first = checkpoint_get_db(db, "r", "decisions/requests/a", reader=reader)
        first["request"]["evidence"]["history"]["recent"][0]["phase"] = "corrupt Python alias"
        assert checkpoint_get_db(db, "r", "decisions/requests/a", reader=reader)["request"] == request
        assert reader.cache
        put_wire_db(db, "r", "other", "1")
        reader.check()
        assert not reader.cache
        assert checkpoint_keys_db(db, "r") == ["decisions/requests/a", "other"]


@pytest.mark.parametrize("raw", ["null", '{"x":"' + "x" * 131072 + '"}'])
def test_active_iterator_rejects_after_invalidation_including_last_block(store, raw):
    with store.connect() as db:
        slot = put_wire_db(db, "r", "inline", raw)
        reader = checkpoint_reader(db)
        it = reader.iter_wire(slot)
        next(it)
        reader.invalidate()
        with pytest.raises(Integrity):
            next(it)
        db.rollback()  # clear the deliberate read-failure barrier


def test_private_gc_only_removes_unreachable_objects_and_preserves_shared_history(store):
    _, wire = request_wire()
    with store.connect() as db:
        for key in ("decisions/requests/a", "decisions/applied/a"):
            put_wire_db(db, "r", key, '{"request":' + wire + "}")
        delete_query_db(
            db,
            "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?",
            ("r", "decisions/requests/a"),
        )
        assert db.execute("SELECT count(*) FROM state_objects").fetchone()[0] == 2
        assert checkpoint_get_db(db, "r", "decisions/applied/a")["request"]
        delete_query_db(db, "SELECT slot_order FROM checkpoint_records WHERE run_id=?", ("r",))
        assert db.execute("SELECT count(*) FROM state_objects").fetchone()[0] == 0


def test_raw_guard_spelling_named_rows_and_update_missing_semantics(store):
    raw = '{"n": -0.0, "u":"\\u4f60", "unknown": [1,2]}'
    with store.connect() as db:
        slot = put_wire_db(db, "r", "guard", raw)
        rows = checkpoint_query_db(
            db, "SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=?", ("r",), wire_index=1
        )
        row = rows.fetchone()
        assert row["key"] == "guard" and row["data"] == raw
        assert dict(row)["data"] == raw
        assert update_wire_db(db, "r", "missing", "{}") == 0
        put_wire_db(db, "r", "guard", raw)
        assert db.execute("SELECT slot_order FROM checkpoint_records WHERE key='guard'").fetchone()[0] == slot
        assert checkpoint_wire_sha256_db(db, "r", "guard") == hashlib.sha256(raw.encode()).hexdigest()


def test_corrupt_object_read_cannot_be_caught_then_committed_as_success(store):
    _, wire = request_wire()
    with store.connect() as db:
        put_wire_db(db, "r", "decisions/requests/a", '{"request":' + wire + "}")
    with pytest.raises(Integrity):
        with store.connect() as db:
            oid = db.execute("SELECT object_id FROM state_objects WHERE kind='permit_snapshot'").fetchone()[0]
            db.execute("BEGIN IMMEDIATE")
            with db.blobopen("state_objects", "literal_body", oid) as body:
                body.write(b"!")
            try:
                checkpoint_get_db(db, "r", "decisions/requests/a")
            except AA2Error:
                pass  # a business fallback must not authorize this damaged transaction
            db.execute("UPDATE runs SET stage='should-rollback' WHERE id='r'")
    assert store.run("r")["stage"] == "review"


def test_old_plain_connection_cannot_write_any_business_table_or_read_fake_inline(store):
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.Error, match="no such function"):
            db.execute("UPDATE runs SET stage='bad' WHERE id='r'")
        with pytest.raises(sqlite3.Error, match="no such table"):
            db.execute("SELECT data FROM checkpoints")
        with pytest.raises(sqlite3.Error, match="no such function"):
            db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")


def test_migration_gate_blocks_respond_writer_connection(store):
    with migration_gate(store.root, exclusive=True):
        with pytest.raises(AA2Error, match="maintenance"):
            store.answer("unknown", "yes")


def test_schema8_rejected_before_provider_or_init_side_effects(tmp_path):
    ConfigStore(tmp_path).initialize()
    root = tmp_path / ".aa2"
    with sqlite3.connect(root / "state.sqlite") as db:
        db.executescript(LEGACY_SCHEMA8)
    config = ConfigStore(tmp_path).path
    before = config.read_bytes()
    runner = CliRunner()
    result = runner.invoke(app, ["provider", "use", "codex", "--project", str(tmp_path)])
    assert result.exit_code == 2 and "schema 9" in result.stdout
    assert config.read_bytes() == before
    result = runner.invoke(
        app, ["init", "--project", str(tmp_path), "--from-config", str(tmp_path / "missing")]
    )
    assert result.exit_code == 2 and "schema 9" in result.stdout
    assert config.read_bytes() == before


def test_cursor_lifecycle_and_unexpanded_sql_observation(store):
    with store.connect() as db:
        observed = []
        db.statement_observer = lambda sql, word: observed.append((sql, word))
        db.cursor().execute("BEGIN")
        reader = checkpoint_reader(db)
        db.cursor().execute("COMMIT")
        db.cursor().execute("BEGIN")
        with pytest.raises(Integrity):
            reader.check()
        payload = "x" * (2 * 1024**2)
        db.cursor().execute(
            "INSERT INTO events(run_id,time,kind,data) VALUES(?,?,?,?)", ("r", 1, "synthetic", payload)
        )
        assert all(len(sql) < 1024 for sql, _ in observed)
        assert all(payload not in sql for sql, _ in observed)
        with pytest.raises(AA2Error, match="tracked cursors"):
            db.cursor(factory=sqlite3.Cursor)
        with pytest.raises(AA2Error, match="script"):
            db.executescript("COMMIT; BEGIN;")


def test_maintenance_lock_refuses_symlink_without_touching_target(store, tmp_path):
    lock = store.root / "migration.lock"
    # The ordinary connection fixture already created the safe lock; replace only this
    # disposable synthetic lock to test refusal, never an external or production path.
    lock.rename(store.root / "migration.lock.retained")
    target = tmp_path / "sentinel"
    target.write_text("keep")
    lock.symlink_to(target)
    with pytest.raises(AA2Error, match="lock"):
        with store.connect():
            pass
    assert target.read_text() == "keep"


def test_initializer_race_rechecks_version_before_returning(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import auto_agents_2.store as store_module

    @contextmanager
    def competing_initializer(_path):
        with sqlite3.connect(tmp_path / ".aa2/state.sqlite") as db:
            db.executescript(LEGACY_SCHEMA8)
        yield

    monkeypatch.setattr(store_module, "file_lock", competing_initializer)
    with pytest.raises(AA2Error, match="schema 9"):
        Store(tmp_path)


def test_returned_cursor_and_executemany_track_transaction_lifecycle(store):
    with store.connect() as db:
        returned = db.execute("BEGIN")
        reader = checkpoint_reader(db)
        returned.execute("COMMIT")
        returned.executemany(
            "INSERT INTO events(run_id,time,kind,data) VALUES(?,?,?,?)",
            [("r", 1, "test", "{}"), ("r", 2, "test", "{}")],
        )
        with pytest.raises(Integrity):
            reader.check()
        with pytest.raises(AA2Error, match="script"):
            returned.executescript("COMMIT; BEGIN;")


@pytest.mark.parametrize("exceptional", [False, True])
def test_connection_context_exit_tracks_implicit_commit_and_rollback(store, exceptional):
    with store.connect() as db:
        try:
            with db:
                reader = checkpoint_reader(db)
                if exceptional:
                    raise ValueError("synthetic")
        except ValueError:
            pass
        db.execute("INSERT INTO events(run_id,time,kind,data) VALUES(?,?,?,?)", ("r", 1, "test", "{}"))
        assert db.in_transaction
        with pytest.raises(Integrity):
            reader.check()


@pytest.mark.parametrize("method", ["write", "setitem"])
def test_each_existing_writable_blob_mutation_invalidates_cached_closure(store, method):
    _, wire = request_wire()
    with store.connect() as db:
        put_wire_db(db, "r", "decisions/requests/a", '{"request":' + wire + "}")
    with pytest.raises(Integrity):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            oid = db.execute("SELECT object_id FROM state_objects WHERE kind='permit_snapshot'").fetchone()[0]
            with db.blobopen("state_objects", "literal_body", oid) as body:
                reader = checkpoint_reader(db)
                checkpoint_get_db(db, "r", "decisions/requests/a", reader=reader)
                assert reader.cache
                before = db.write_epoch
                if method == "write":
                    body.seek(0)
                    body.write(b"!")
                else:
                    body[0] = ord("!")
                assert db.write_epoch > before
                checkpoint_get_db(db, "r", "decisions/requests/a", reader=reader)


@pytest.mark.parametrize("operation", ["get", "wire", "sha", "query", "keys", "rows"])
def test_explicit_reader_cannot_expand_another_connection_slot(store, operation):
    from auto_agents_2.state_codec import checkpoint_query_db, checkpoint_rows_db

    with store.connect() as first, store.connect() as second:
        reader = checkpoint_reader(second)
        with pytest.raises(Integrity, match="different connection"):
            if operation == "get":
                checkpoint_get_db(first, "r", "missing", reader=reader)
            elif operation == "wire":
                checkpoint_wire_bytes_db(first, "r", "missing", reader=reader)
            elif operation == "sha":
                checkpoint_wire_sha256_db(first, "r", "missing", reader=reader)
            elif operation == "query":
                checkpoint_query_db(first, "SELECT slot_order AS data FROM checkpoint_records", reader=reader)
            elif operation == "keys":
                checkpoint_keys_db(first, "r", reader=reader)
            else:
                checkpoint_rows_db(first, "r", iterate_all=True, reader=reader)
        first.rollback()


def test_missing_and_invalid_key_do_not_hide_expired_explicit_reader(store):
    with store.connect() as db:
        reader = checkpoint_reader(db)
        db.commit()
        for key in ("missing", 1):
            with pytest.raises(Integrity):
                checkpoint_get_db(db, "r", key, "fallback", reader=reader)
            db.rollback()


@pytest.mark.parametrize("mutation", ["put", "delete"])
@pytest.mark.parametrize("finish", ["method", "sql_commit", "sql_end", "cursor_commit"])
def test_caught_storage_failure_cannot_commit_partial_mutation(store, mutation, finish):
    _, wire = request_wire()
    with store.connect() as db:
        put_wire_db(db, "r", "decisions/requests/a", '{"request":' + wire + "}")
    with pytest.raises(Integrity):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            oid = db.execute("SELECT object_id FROM state_objects WHERE kind='permit_snapshot'").fetchone()[0]
            with db.blobopen("state_objects", "literal_body", oid) as body:
                body[0] = ord("!")
            try:
                if mutation == "put":
                    put_wire_db(db, "r", "decisions/requests/a", '{"changed":true}')
                else:
                    delete_query_db(
                        db, "SELECT slot_order FROM checkpoint_records WHERE key=?", ("decisions/requests/a",)
                    )
            except AA2Error:
                pass
            db.execute("UPDATE runs SET stage='must-not-commit' WHERE id='r'")
            if finish == "method":
                db.commit()
            elif finish == "sql_commit":
                db.execute("COMMIT")
            elif finish == "sql_end":
                db.execute("END TRANSACTION")
            else:
                db.cursor().execute("COMMIT")
    assert store.run("r")["stage"] == "review"
    assert store.get("r", "decisions/requests/a")["request"]["evidence"]


def test_delete_validates_root_reference_lengths_before_any_deletion(store):
    _, wire = request_wire()
    with store.connect() as db:
        put_wire_db(db, "r", "decisions/requests/a", '{"request":' + wire + "}")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE state_checkpoint_refs SET raw_bytes=raw_bytes+1")
        with pytest.raises(Integrity):
            delete_query_db(db, "SELECT slot_order FROM checkpoint_records")
        assert db.execute("SELECT count(*) FROM checkpoint_records").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM state_objects").fetchone()[0] == 2
        db.rollback()


@pytest.mark.parametrize(
    "begin,finish",
    [
        ("BEGIN/**/", "COMMIT/**/"),
        ("BEGIN/**/TRANSACTION", "END/**/"),
        ("/*prefix*/ BEGIN --suffix\n", "/*prefix*/ COMMIT --suffix\n"),
        ("\ufeffBEGIN", "\ufeffEND"),
    ],
)
def test_adjacent_sql_comments_and_bom_cannot_hide_transaction_epoch(store, begin, finish):
    with store.connect() as db:
        db.execute(begin)
        reader = checkpoint_reader(db)
        db.execute(finish)
        db.execute(begin)
        with pytest.raises(Integrity):
            reader.check()
        db.rollback()


def test_sql_rollback_clears_poison_only_after_the_whole_transaction_ends(store):
    with store.connect() as db:
        slot = put_wire_db(db, "r", "plain", "{}")
    with store.connect() as db:
        db.execute("BEGIN")
        with db.blobopen("checkpoint_records", "literal_body", slot) as blob:
            blob[0] = ord("!")
        with pytest.raises(Integrity):
            checkpoint_get_db(db, "r", "plain")
        db.execute("ROLLBACK/**/")
        assert db._state_read_failure is None
        db.execute("UPDATE runs SET stage='clean-after-rollback' WHERE id='r'")
    assert store.run("r")["stage"] == "clean-after-rollback"
    assert store.get("r", "plain") == {}


@pytest.mark.parametrize("setter", ["isolation", "outer_release"])
def test_optional_implicit_commit_surface_cannot_bypass_poison(store, setter):
    with store.connect() as db:
        slot = put_wire_db(db, "r", "plain", "{}")
    with store.connect() as db:
        db.execute("SAVEPOINT outer_scope")
        with db.blobopen("checkpoint_records", "literal_body", slot) as blob:
            blob[0] = ord("!")
        with pytest.raises(Integrity):
            checkpoint_get_db(db, "r", "plain")
        db.execute("UPDATE runs SET stage='unsafe-commit' WHERE id='r'")
        with pytest.raises(AA2Error):
            if setter == "isolation":
                db.isolation_level = None
            else:
                db.execute("RELEASE/**/outer_scope")
        db.rollback()
    assert store.run("r")["stage"] == "review"


@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
def test_new_format_rejects_non_utf8_before_schema_creation_or_read(encoding):
    from auto_agents_2.state_format import create_schema

    with sqlite3.connect(":memory:", factory=StateConnection) as db:
        db.execute(f"PRAGMA encoding='{encoding}'")
        with pytest.raises(ValueError, match="UTF-8"):
            create_schema(db)
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        # Even forged format metadata cannot pass the normal read gate.
        db.execute("CREATE TABLE meta(key,value)")
        db.execute("INSERT INTO meta VALUES('schema_version','9')")
        with pytest.raises(AA2Error, match="UTF-8"):
            require_format(db)


def test_validation_before_mutation_does_not_parse_retained_bad_values(store):
    with store.connect() as db:
        for key, value in (("json", "!"), ("blob", b"\xff"), ("integer", 7), ("real", 2.0), ("null", None)):
            put_wire_db(db, "r", key, value)
        put_wire_db(db, "r", "json", "still not json")
        assert delete_query_db(db, "SELECT slot_order FROM checkpoint_records WHERE run_id=?", ("r",)) == 5


def test_reboot_entry_rejects_legacy_before_lock_or_request_read(tmp_path):
    from auto_agents_2.process_reboot import reconcile

    root = tmp_path / ".aa2"
    root.mkdir(mode=0o700)
    with sqlite3.connect(root / "state.sqlite") as db:
        db.executescript(LEGACY_SCHEMA8)
    before = (root / "state.sqlite").read_bytes()
    with pytest.raises(AA2Error, match="schema 9"):
        reconcile(tmp_path, tmp_path / "not-read.json", "synthetic", confirm_local_host_reboot=True)
    assert (root / "state.sqlite").read_bytes() == before
    assert not (root / "controller.lock").exists()
    assert not (root / "process-reboot-recovery").exists()


def test_connection_rejects_initial_autocommit_configuration():
    with pytest.raises(AA2Error, match="isolation_level"):
        sqlite3.connect(":memory:", factory=StateConnection, isolation_level=None)


_LEADING_EMPTY_PREFIXES = (
    ";",
    " ; /* empty */ ;\n\t",
    "\ufeff /* leading */ ;-- empty\n; ",
    " ;\ufeff/* empty */; ",
)


@pytest.mark.parametrize("prefix", _LEADING_EMPTY_PREFIXES)
@pytest.mark.parametrize("finish", ["COMMIT", "END"])
def test_leading_empty_statements_do_not_hide_epoch(store, prefix, finish):
    with store.connect() as db:
        db.execute(prefix + "BEGIN/**/")
        reader = checkpoint_reader(db)
        db.execute(prefix + finish + "/**/")
        db.execute(prefix + "BEGIN/**/")
        with pytest.raises(Integrity):
            reader.check()
        db.rollback()


@pytest.mark.parametrize("prefix", _LEADING_EMPTY_PREFIXES)
@pytest.mark.parametrize(
    "finish,api", [("COMMIT", "connection"), ("END", "returned"), ("RELEASE outer_scope", "cursor")]
)
def test_leading_empty_commit_aliases_cannot_persist_after_public_read_failure(store, prefix, finish, api):
    with store.connect() as db:
        slot = put_wire_db(db, "r", "plain", "{}")
    with pytest.raises(Integrity):
        with store.connect() as db:
            db.execute(prefix + ("SAVEPOINT outer_scope" if finish.startswith("RELEASE") else "BEGIN"))
            with db.blobopen("checkpoint_records", "literal_body", slot) as blob:
                blob[0] = ord("!")
            with pytest.raises(Integrity):
                checkpoint_get_db(db, "r", "plain")
            db.execute("UPDATE runs SET stage='unsafe-commit' WHERE id='r'")
            sql = prefix + finish + "/**/"
            if api == "connection":
                db.execute(sql)
            elif api == "returned":
                db.execute("SELECT 1").execute(sql)
            else:
                db.cursor().execute(sql)
    assert store.run("r")["stage"] == "review"
    assert store.get("r", "plain") == {}


@pytest.mark.parametrize("prefix", _LEADING_EMPTY_PREFIXES)
def test_leading_empty_whole_rollback_resets_poison_after_success(store, prefix):
    with store.connect() as db:
        slot = put_wire_db(db, "r", "plain", "{}")
    with store.connect() as db:
        db.execute(prefix + "BEGIN")
        with db.blobopen("checkpoint_records", "literal_body", slot) as blob:
            blob[0] = ord("!")
        with pytest.raises(Integrity):
            checkpoint_get_db(db, "r", "plain")
        db.execute(prefix + "ROLLBACK/**/")
        assert db._state_read_failure is None and not db.in_transaction
        assert checkpoint_get_db(db, "r", "plain") == {}
        db.execute("UPDATE runs SET stage='recovered' WHERE id='r'")
    assert store.run("r")["stage"] == "recovered"


@pytest.mark.parametrize("prefix", _LEADING_EMPTY_PREFIXES)
def test_leading_empty_rollback_to_does_not_clear_transaction_poison(store, prefix):
    with store.connect() as db:
        slot = put_wire_db(db, "r", "plain", "{}")
    with pytest.raises(Integrity):
        with store.connect() as db:
            db.execute(prefix + "SAVEPOINT outer_scope")
            db.execute(prefix + "SAVEPOINT inner_scope")
            with db.blobopen("checkpoint_records", "literal_body", slot) as blob:
                blob[0] = ord("!")
            with pytest.raises(Integrity):
                checkpoint_get_db(db, "r", "plain")
            db.execute(prefix + "ROLLBACK/**/ TO inner_scope")
            assert db._state_read_failure is not None and db.in_transaction
            db.execute("UPDATE runs SET stage='unsafe-commit' WHERE id='r'")
            db.execute(prefix + "RELEASE/**/ outer_scope")
    assert store.run("r")["stage"] == "review"
    assert store.get("r", "plain") == {}


@pytest.mark.parametrize("linked", ["state", "database"])
def test_reboot_no_link_format_precheck_keeps_policy_violation_and_record_bytes(tmp_path, linked):
    from auto_agents_2.errors import PolicyViolation
    from auto_agents_2.process_reboot import reconcile

    Store(tmp_path)
    state = tmp_path / ".aa2"
    processes = state / "processes"
    processes.mkdir(mode=0o700)
    record = processes / "synthetic.json"
    original = b'{"synthetic":"retain exactly"}'
    record.write_bytes(original)
    database = state / "state.sqlite"
    database_bytes = database.read_bytes()
    if linked == "state":
        saved = state.with_name(".aa2-original")
        state.rename(saved)
        state.symlink_to(saved, target_is_directory=True)
        actual_record = saved / "processes/synthetic.json"
        actual_database = saved / "state.sqlite"
    else:
        saved = database.with_name("state-original.sqlite")
        database.rename(saved)
        database.symlink_to(saved)
        actual_record = record
        actual_database = saved
    # Same policy-specific assertion as the existing no-link operator contract.
    with pytest.raises(PolicyViolation):
        reconcile(tmp_path, tmp_path / "must-not-read.json", "synthetic", confirm_local_host_reboot=True)
    assert actual_record.read_bytes() == original
    assert actual_database.read_bytes() == database_bytes
    assert not (actual_record.parent.parent / "controller.lock").exists()
    assert not (actual_record.parent.parent / "process-reboot-recovery").exists()


def test_schema9_ddl_is_unchanged_after_offline_tool_extraction():
    from auto_agents_2 import state_format

    assert not hasattr(state_format, "LEGACY_SCHEMA8")
    assert hashlib.sha256(state_format.SCHEMA.encode()).hexdigest() == (
        "59f1cc9333fdef0f6a0f3a406a69599d9c9c35b43180ce40f1d6d3867b4b329f"
    )


def test_normal_cli_has_no_oneoff_migration_command_or_modules():
    import importlib.util

    for name in ("state_migration", "state_inplace", "state_capacity"):
        assert importlib.util.find_spec("auto_agents_2." + name) is None
    result = CliRunner().invoke(app, ["migrate-state", "--help"])
    assert result.exit_code == 2
    assert "No such command" in result.output
