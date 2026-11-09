"""Private limited root -> request -> frozen-permit storage. Never model material."""

from __future__ import annotations

import collections
import hashlib
import json
import sqlite3
from functools import wraps

from .state_connection import StateConnection
from .state_wire import (
    MAX_SELECTED_SPANS,
    MAX_WIRE_BYTES,
    Integrity,
    MemorySource,
    range_blocks,
    scan_wire,
)

MIN_SHARE = 512
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _transaction(db, *, write=False):
    if not isinstance(db, StateConnection):
        raise Integrity("Checkpoint access requires a schema-9 connection")
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")


def _address(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class CheckpointReader:
    """Wire cache only. Decoded values are independent; explicit transaction epoch is mandatory."""

    def __init__(self, db, *, cache_bytes=1024**2, max_entry=65536):
        _transaction(db)
        if type(cache_bytes) is not int or not 0 <= cache_bytes <= 1024**2:
            raise Integrity("Checkpoint cache budget")
        self.db = db
        self.epoch = db.transaction_epoch
        self.write_epoch = db.write_epoch
        self.cache_limit = cache_bytes
        self.max_entry = min(max_entry, cache_bytes)
        self.cache = collections.OrderedDict()
        self.cache_size = 0
        self.closed = False

    def invalidate(self):
        self.closed = True
        self.cache.clear()
        self.cache_size = 0

    def check(self):
        if self.closed or not self.db.in_transaction or self.epoch != self.db.transaction_epoch:
            raise Integrity("Checkpoint reader transaction has ended")
        if self.write_epoch != self.db.write_epoch:
            self.cache.clear()
            self.cache_size = 0
            self.write_epoch = self.db.write_epoch

    def _live_stream(self, epoch, write_epoch):
        self.check()
        if (epoch, write_epoch) != (self.db.transaction_epoch, self.db.write_epoch):
            raise Integrity("Checkpoint stream invalidated by a transaction or write")

    def _descriptor(self, table, rowid, expected_kind, rid, expected_sha=None):
        self.check()
        if table == "checkpoint_records":
            row = self.db.execute(
                """SELECT slot_order,run_id,source_type,encoding,literal_bytes,
                literal_sha,raw_sha,raw_bytes,typeof(literal_body),scalar_value
                FROM checkpoint_records WHERE slot_order=?""",
                (rowid,),
            ).fetchone()
            refs_sql = """SELECT ordinal,at,kind,raw_sha,raw_bytes,run_id FROM state_checkpoint_refs
                WHERE root_id=? ORDER BY ordinal LIMIT 513"""
            kind = "root"
        else:
            row = self.db.execute(
                """SELECT object_id,run_id,'blob','wire-v1',literal_bytes,
                literal_sha,raw_sha,raw_bytes,typeof(literal_body),NULL,kind
                FROM state_objects WHERE object_id=?""",
                (rowid,),
            ).fetchone()
            refs_sql = """SELECT ordinal,at,kind,raw_sha,raw_bytes,run_id FROM state_object_refs
                WHERE parent_id=? ORDER BY ordinal LIMIT 513"""
            kind = row[10] if row else None
        if row is None or row[1] != rid or kind != expected_kind:
            raise Integrity("Checkpoint object scope/role mismatch")
        desc = dict(
            id=row[0],
            rid=row[1],
            source_type=row[2],
            encoding=row[3],
            body_bytes=row[4],
            body_sha=row[5],
            raw_sha=row[6],
            raw_bytes=row[7],
            body_type=row[8],
            scalar=row[9],
            kind=kind,
            table=table,
        )
        if desc["encoding"] == "opaque-sqlite":
            if kind != "root" or desc["source_type"] not in {"integer", "real", "null"}:
                raise Integrity("Opaque scalar type mismatch")
            if (
                desc["body_bytes"] != 0
                or desc["body_sha"] != EMPTY_SHA
                or desc["raw_sha"] is not None
                or desc["raw_bytes"] is not None
            ):
                raise Integrity("Opaque scalar metadata mismatch")
            expected = {"integer": "integer", "real": "real", "null": "null"}[desc["source_type"]]
            actual = self.db.execute(
                "SELECT typeof(scalar_value) FROM checkpoint_records WHERE slot_order=?", (rowid,)
            ).fetchone()[0]
            if actual != expected:
                raise Integrity("Opaque scalar SQLite type changed")
        elif desc["encoding"] != "wire-v1" or desc["source_type"] not in {"text", "blob"}:
            raise Integrity("Checkpoint codec/source type mismatch")
        if (
            desc["body_type"] != "blob"
            or type(desc["body_bytes"]) is not int
            or desc["body_bytes"] < 0
            or not _address(desc["body_sha"])
        ):
            raise Integrity("Checkpoint literal metadata mismatch")
        refs = self.db.execute(refs_sql, (rowid,)).fetchall()
        limit = {"root": 1, "request": MAX_SELECTED_SPANS - 1, "permit_snapshot": 0}[kind]
        if len(refs) > limit:
            raise Integrity("Checkpoint role reference budget")
        children = []
        total = desc["body_bytes"]
        previous = 0
        for ordinal, at, child_kind, raw_sha, raw_bytes, child_rid in refs:
            allowed = "request" if kind == "root" else "permit_snapshot"
            if ordinal != len(children) or child_rid != rid or child_kind != allowed or not _address(raw_sha):
                raise Integrity("Checkpoint typed/scoped reference mismatch")
            if type(at) is not int or not previous <= at <= desc["body_bytes"]:
                raise Integrity("Checkpoint reference offset")
            if type(raw_bytes) is not int or not 0 < raw_bytes <= MAX_WIRE_BYTES:
                raise Integrity("Checkpoint positive reference length")
            previous = at
            total += raw_bytes
            children.append(dict(at=at, kind=child_kind, raw_sha=raw_sha, raw_bytes=raw_bytes))
        desc["refs"] = children
        if desc["encoding"] == "opaque-sqlite":
            if children:
                raise Integrity("Opaque scalar has references")
            with self.db.blobopen(table, "literal_body", rowid, readonly=True) as body:
                if len(body):
                    raise Integrity("Opaque scalar has unexpected wire bytes")
        elif (
            not _address(desc["raw_sha"])
            or expected_sha is not None
            and desc["raw_sha"] != expected_sha
            or type(desc["raw_bytes"]) is not int
            or not 0 <= desc["raw_bytes"] <= MAX_WIRE_BYTES
            or kind != "root"
            and desc["raw_bytes"] == 0
            or total != desc["raw_bytes"]
        ):
            raise Integrity("Checkpoint index/ref/SHA/length binding")
        return desc

    def _object_unchecked(self, rid, ref, epoch, write_epoch):
        self._live_stream(epoch, write_epoch)
        row = self.db.execute(
            "SELECT object_id FROM state_objects WHERE run_id=? AND kind=? AND raw_sha=?",
            (rid, ref["kind"], ref["raw_sha"]),
        ).fetchone()
        if row is None:
            raise Integrity("Missing private checkpoint object")
        desc = self._descriptor("state_objects", row[0], ref["kind"], rid, ref["raw_sha"])
        if desc["raw_bytes"] != ref["raw_bytes"]:
            raise Integrity("Checkpoint reference/object length mismatch")
        identity = (rid, ref["kind"], ref["raw_sha"])
        # Identity/descriptor checks deliberately precede the cache hit.
        if identity in self.cache:
            value = self.cache[identity]
            if len(value) != ref["raw_bytes"]:
                raise Integrity("Checkpoint cached length mismatch")
            self.cache.move_to_end(identity)
            self._live_stream(epoch, write_epoch)
            yield value
            self._live_stream(epoch, write_epoch)
            return
        stream = self._chunks(desc, epoch, write_epoch)
        if ref["raw_bytes"] <= self.max_entry:
            value = b"".join(stream)
            self._live_stream(epoch, write_epoch)
            while self.cache and (self.cache_size + len(value) > self.cache_limit or len(self.cache) >= 256):
                _, old = self.cache.popitem(last=False)
                self.cache_size -= len(old)
            self.cache[identity] = value
            self.cache_size += len(value)
            yield value
            self._live_stream(epoch, write_epoch)
        else:
            yield from stream

    def _object(self, rid, ref, epoch, write_epoch):
        try:
            yield from self._object_unchecked(rid, ref, epoch, write_epoch)
        except (Integrity, sqlite3.Error, UnicodeError) as exc:
            self.db._state_read_failure = exc
            raise

    def _chunks(self, desc, epoch, write_epoch):
        self._live_stream(epoch, write_epoch)
        full = hashlib.sha256()
        literal = hashlib.sha256()
        yielded = 0
        literal_count = 0
        cursor = 0

        def checked(block, is_literal):
            nonlocal yielded, literal_count
            self._live_stream(epoch, write_epoch)
            full.update(block)
            yielded += len(block)
            if is_literal:
                literal.update(block)
                literal_count += len(block)
            return block

        with self.db.blobopen(desc["table"], "literal_body", desc["id"], readonly=True) as body:
            if len(body) != desc["body_bytes"]:
                raise Integrity("Checkpoint literal length mismatch")
            for ref in desc["refs"]:
                remaining = ref["at"] - cursor
                while remaining:
                    self._live_stream(epoch, write_epoch)
                    block = body.read(min(65536, remaining))
                    if not block:
                        raise Integrity("Short checkpoint literal")
                    remaining -= len(block)
                    cursor += len(block)
                    yield checked(block, True)
                    self._live_stream(epoch, write_epoch)
                for block in self._object(desc["rid"], ref, epoch, write_epoch):
                    yield checked(block, False)
                    self._live_stream(epoch, write_epoch)
            while True:
                self._live_stream(epoch, write_epoch)
                block = body.read(65536)
                if not block:
                    break
                yield checked(block, True)
                self._live_stream(epoch, write_epoch)
        if literal_count != desc["body_bytes"] or literal.hexdigest() != desc["body_sha"]:
            raise Integrity("Checkpoint literal integrity mismatch")
        if yielded != desc["raw_bytes"] or full.hexdigest() != desc["raw_sha"]:
            raise Integrity("Checkpoint reconstructed wire integrity mismatch")

    def _iter_wire(self, slot_order):
        self.check()
        row = self.db.execute(
            "SELECT run_id FROM checkpoint_records WHERE slot_order=?", (slot_order,)
        ).fetchone()
        if row is None:
            raise KeyError(slot_order)
        desc = self._descriptor("checkpoint_records", slot_order, "root", row[0])
        if desc["encoding"] != "wire-v1":
            raise TypeError("Original SQLite scalar does not have TEXT/BLOB wire bytes")
        epoch, write_epoch = self.epoch, self.db.write_epoch
        try:
            yield from self._chunks(desc, epoch, write_epoch)
        except Integrity as exc:
            self.db._state_read_failure = exc
            raise

    def _wire_value(self, slot_order):
        self.check()
        row = self.db.execute(
            "SELECT run_id FROM checkpoint_records WHERE slot_order=?", (slot_order,)
        ).fetchone()
        if row is None:
            raise KeyError(slot_order)
        desc = self._descriptor("checkpoint_records", slot_order, "root", row[0])
        if desc["encoding"] == "opaque-sqlite":
            return desc["scalar"]
        raw = b"".join(self.iter_wire(slot_order))
        return raw.decode("utf-8") if desc["source_type"] == "text" else raw

    def iter_wire(self, slot_order):
        try:
            yield from self._iter_wire(slot_order)
        except (Integrity, sqlite3.Error, UnicodeError) as exc:
            self.db._state_read_failure = exc
            raise

    def wire_value(self, slot_order):
        try:
            return self._wire_value(slot_order)
        except (Integrity, sqlite3.Error, UnicodeError) as exc:
            self.db._state_read_failure = exc
            raise


def checkpoint_reader(db):
    _transaction(db)
    reader = db._checkpoint_reader
    if reader is None or reader.closed or reader.epoch != db.transaction_epoch:
        reader = CheckpointReader(db)
        db._checkpoint_reader = reader
    reader.check()
    return reader


class CheckpointRow:
    """sqlite.Row-compatible named/indexed reads without exposing storage envelopes."""

    def __init__(self, values, keys):
        self.values, self.names = tuple(values), tuple(keys)

    def __len__(self):
        return len(self.values)

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, key):
        return self.values[self.names.index(key) if isinstance(key, str) else key]

    def keys(self):
        return list(self.names)


class CheckpointRows:
    def __init__(self, cursor, reader, wire_index):
        self.cursor, self.reader, self.wire_index = cursor, reader, wire_index

    def _row(self, row):
        if row is None:
            return None
        values = list(row)
        values[self.wire_index] = self.reader.wire_value(values[self.wire_index])
        return CheckpointRow(values, [d[0] for d in self.cursor.description])

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return list(self)

    def fetchmany(self, size=None):
        rows = self.cursor.fetchmany() if size is None else self.cursor.fetchmany(size)
        return [self._row(row) for row in rows]

    def __iter__(self):
        for row in self.cursor:
            yield self._row(row)

    def close(self):
        self.cursor.close()


def _bound_reader(db, reader=None):
    if reader is None:
        return checkpoint_reader(db)
    try:
        if not isinstance(reader, CheckpointReader) or reader.db is not db:
            raise Integrity("Checkpoint reader belongs to a different connection")
        reader.check()
        return reader
    except Integrity as exc:
        if isinstance(db, StateConnection):
            db._state_read_failure = exc
        raise


def checkpoint_query_db(db, sql, parameters=(), *, wire_index=0, reader=None):
    """Internal source-selected header SQL; callers explicitly project slot_order AS data."""
    reader = _bound_reader(db, reader)
    return CheckpointRows(db.execute(sql, parameters), reader, wire_index)


def checkpoint_keys_db(db, rid, *, prefix=None, order="key", reader=None):
    if reader is not None:
        _bound_reader(db, reader)
    _transaction(db)
    if not isinstance(rid, str):
        raise TypeError("run_id must be a string")
    orders = {"key": "key", "rowid": "slot_order", "rowid_desc": "slot_order DESC"}
    if order not in orders:
        raise ValueError("unsupported checkpoint key order")
    sql, args = "SELECT key FROM checkpoint_records WHERE run_id=?", [rid]
    if prefix is not None:
        if not isinstance(prefix, str):
            raise TypeError("checkpoint prefix must be a string")
        raw = prefix.encode("utf-8")
        sql += " AND substr(CAST(key AS BLOB),1,?)=?"
        args.extend((len(raw), raw))
    return [row[0] for row in db.execute(sql + " ORDER BY " + orders[order], args)]


def checkpoint_get_db(db, rid, key, default=None, *, reader=None):
    if reader is not None:
        reader = _bound_reader(db, reader)
    if not isinstance(rid, str) or not isinstance(key, str):
        return default
    reader = _bound_reader(db, reader)
    row = db.execute(
        "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?", (rid, key)
    ).fetchone()
    return json.loads(reader.wire_value(row[0])) if row else default


def checkpoint_wire_bytes_db(db, rid, key, *, reader=None):
    reader = _bound_reader(db, reader)
    row = db.execute(
        "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?", (rid, key)
    ).fetchone()
    if row is None:
        raise KeyError(key)
    return b"".join(reader.iter_wire(row[0]))


def checkpoint_wire_sha256_db(db, rid, key, *, reader=None):
    reader = _bound_reader(db, reader)
    row = db.execute(
        "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?", (rid, key)
    ).fetchone()
    if row is None:
        raise KeyError(key)
    value = hashlib.sha256()
    for block in reader.iter_wire(row[0]):
        value.update(block)
    return value.hexdigest()


def _validate_root(db, slot_order):
    reader = checkpoint_reader(db)
    row = db.execute("SELECT encoding FROM checkpoint_records WHERE slot_order=?", (slot_order,)).fetchone()
    if row is None:
        raise Integrity("Missing checkpoint selected for mutation")
    if row[0] == "opaque-sqlite":
        reader.wire_value(slot_order)
    else:
        for _ in reader.iter_wire(slot_order):
            pass


def _literal_plan(source, start, end, children):
    ranges, refs, cursor, count = [], [], start, 0
    sha = hashlib.sha256()
    for child in sorted(children, key=lambda item: item["start"]):
        if not cursor <= child["start"] < child["end"] <= end:
            raise Integrity("Selected source bounds overlap")
        ranges.append((cursor, child["start"]))
        for block in range_blocks(source, cursor, child["start"]):
            sha.update(block)
            count += len(block)
        refs.append(
            dict(at=count, kind=child["kind"], raw_sha=child["sha"], raw_bytes=child["end"] - child["start"])
        )
        cursor = child["end"]
    ranges.append((cursor, end))
    for block in range_blocks(source, cursor, end):
        sha.update(block)
        count += len(block)
    return ranges, refs, count, sha.hexdigest()


def _write_literal(db, table, rowid, source, ranges):
    with db.blobopen(table, "literal_body", rowid, readonly=False) as target:
        for start, end in ranges:
            for block in range_blocks(source, start, end):
                target.write(block)
                ledger = getattr(db, "_migration_ledger", None)
                if ledger is not None:
                    ledger.observe_write(len(block))


def _object(db, rid, source, span, children):
    kind, address, size = span["kind"], span["sha"], span["end"] - span["start"]
    if kind not in {"request", "permit_snapshot"} or not 0 < size <= MAX_WIRE_BYTES:
        raise Integrity("Selected object role/length")
    row = db.execute(
        "SELECT object_id FROM state_objects WHERE run_id=? AND kind=? AND raw_sha=?", (rid, kind, address)
    ).fetchone()
    if row:
        reader = checkpoint_reader(db)
        ref = dict(kind=kind, raw_sha=address, raw_bytes=size)
        # Full consumption verifies bytes and all frozen children; INSERT OR IGNORE is not proof.
        for _ in reader._object(rid, ref, reader.epoch, db.write_epoch):
            pass
        return row[0]
    ranges, refs, body_size, body_sha = _literal_plan(source, span["start"], span["end"], children)
    if len(refs) > (511 if kind == "request" else 0):
        raise Integrity("Selected object reference budget")
    ledger = getattr(db, "_migration_ledger", None)
    if ledger is not None:
        ledger.probe(next_statement_bytes=body_size)
    cursor = db.execute(
        """INSERT INTO state_objects(run_id,kind,literal_body,literal_bytes,literal_sha,raw_sha,raw_bytes)
        VALUES(?,?,zeroblob(?),?,?,?,?)""",
        (rid, kind, body_size, body_size, body_sha, address, size),
    )
    oid = cursor.lastrowid
    _write_literal(db, "state_objects", oid, source, ranges)
    db._state_object_new.add(oid)
    try:
        for ordinal, ref in enumerate(refs):
            db.execute(
                """INSERT INTO state_object_refs(parent_id,run_id,ordinal,at,kind,raw_sha,raw_bytes)
                VALUES(?,?,?,?,?,?,?)""",
                (oid, rid, ordinal, ref["at"], ref["kind"], ref["raw_sha"], ref["raw_bytes"]),
            )
    finally:
        db._state_object_new.discard(oid)
    return oid


def _collect_removed(db, rid, previous):
    """Only unreachable private objects. Logical history/ArtifactStore retention is unchanged."""
    for kind, address in set(previous):
        row = db.execute(
            "SELECT object_id,raw_bytes FROM state_objects WHERE run_id=? AND kind=? AND raw_sha=?",
            (rid, kind, address),
        ).fetchone()
        if row is None:
            raise Integrity("Missing removed-root object")
        if db.execute(
            "SELECT 1 FROM state_checkpoint_refs WHERE run_id=? AND kind=? AND raw_sha=? LIMIT 1",
            (rid, kind, address),
        ).fetchone():
            continue
        reader = checkpoint_reader(db)
        for _ in reader._object(
            rid, dict(kind=kind, raw_sha=address, raw_bytes=row[1]), reader.epoch, db.write_epoch
        ):
            pass
        children = db.execute(
            "SELECT kind,raw_sha FROM state_object_refs WHERE parent_id=?", (row[0],)
        ).fetchall()
        db._state_object_gc = True
        try:
            db.execute("DELETE FROM state_objects WHERE object_id=?", (row[0],))
            for child_kind, child_sha in children:
                if not db.execute(
                    "SELECT 1 FROM state_object_refs WHERE run_id=? AND kind=? AND raw_sha=? LIMIT 1",
                    (rid, child_kind, child_sha),
                ).fetchone():
                    db.execute(
                        "DELETE FROM state_objects WHERE run_id=? AND kind=? AND raw_sha=?",
                        (rid, child_kind, child_sha),
                    )
        finally:
            db._state_object_gc = False


def _poison_mutation(function):
    @wraps(function)
    def guarded(db, *args, **kwargs):
        try:
            return function(db, *args, **kwargs)
        except (Integrity, sqlite3.Error) as exc:
            if isinstance(db, StateConnection):
                db._state_read_failure = exc
            raise

    return guarded


@_poison_mutation
def put_wire_db(db, rid, key, value=None, *, source=None, source_type=None, slot_order=None, replace=False):
    _transaction(db, write=True)
    if not isinstance(rid, str) or not isinstance(key, str):
        raise TypeError("Checkpoint run and key must be strings")
    old = db.execute(
        "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?", (rid, key)
    ).fetchone()
    if old is not None:
        _validate_root(db, old[0])
    old_refs = (
        db.execute("SELECT kind,raw_sha FROM state_checkpoint_refs WHERE root_id=?", (old[0],)).fetchall()
        if old
        else []
    )
    native_replace = bool(replace and old is not None)
    scalar = None
    if source is None:
        if isinstance(value, str):
            source_type, source = "text", MemorySource(value.encode("utf-8"))
        elif isinstance(value, bytes):
            source_type, source = "blob", MemorySource(value)
        else:
            scalar = value
            source_type = (
                "null"
                if value is None
                else "integer"
                if type(value) is int
                else "real"
                if type(value) is float
                else None
            )
            if source_type is None:
                raise TypeError("Unsupported original SQLite checkpoint value")
    if source is not None:
        if source_type not in {"text", "blob"}:
            raise Integrity("Wire source type required")
        scanned = scan_wire(source, key, source_type)
        request = [
            s for s in scanned["spans"] if s["kind"] == "request" and s["end"] - s["start"] >= MIN_SHARE
        ]
        if len(request) > 1:
            raise Integrity("Ambiguous selected request")
        root_children = []
        if request:
            req = request[0]
            permits = [
                s
                for s in scanned["spans"]
                if s["kind"] == "permit_snapshot"
                and req["start"] <= s["start"] < s["end"] <= req["end"]
                and s["end"] - s["start"] >= MIN_SHARE
            ]
            for permit in permits:
                _object(db, rid, source, permit, [])
            _object(db, rid, source, req, permits)
            root_children = [req]
        ranges, refs, body_size, body_sha = _literal_plan(source, 0, scanned["wire_bytes"], root_children)
        encoding, status, raw_sha, raw_bytes = (
            "wire-v1",
            scanned["status"],
            scanned["wire_sha"],
            scanned["wire_bytes"],
        )
    else:
        ranges, refs, body_size, body_sha = [], [], 0, EMPTY_SHA
        encoding, status, raw_sha, raw_bytes = "opaque-sqlite", "opaque_sqlite_scalar", None, None
    # Native REPLACE allocates its new rowid before deleting the conflicting row.
    actual_order = None if native_replace else old[0] if old else slot_order
    ledger = getattr(db, "_migration_ledger", None)
    if ledger is not None:
        ledger.probe(next_statement_bytes=body_size)
    verb = "INSERT OR REPLACE" if native_replace else "INSERT"
    statement = f"""{verb} INTO checkpoint_records(slot_order,run_id,key,source_type,encoding,literal_body,
        literal_bytes,literal_sha,raw_sha,raw_bytes,scalar_value,parse_status)
        VALUES(?,?,?,?,?,zeroblob(?),?,?,?,?,?,?)"""
    if not native_replace:
        statement += """ ON CONFLICT(run_id,key) DO UPDATE SET source_type=excluded.source_type,encoding=excluded.encoding,
        literal_body=excluded.literal_body,literal_bytes=excluded.literal_bytes,literal_sha=excluded.literal_sha,
        raw_sha=excluded.raw_sha,raw_bytes=excluded.raw_bytes,scalar_value=excluded.scalar_value,parse_status=excluded.parse_status"""
    cursor = db.execute(
        statement,
        (
            actual_order,
            rid,
            key,
            source_type,
            encoding,
            body_size,
            body_size,
            body_sha,
            raw_sha,
            raw_bytes,
            scalar,
            status,
        ),
    )
    oid = cursor.lastrowid if native_replace or old is None else old[0]
    if native_replace:
        # Normally already removed by ON DELETE CASCADE. Explicit cleanup also keeps
        # the private reference set correct for a controlled connection with FK off.
        db.execute("DELETE FROM state_checkpoint_refs WHERE root_id=?", (old[0],))
    db.execute("DELETE FROM state_checkpoint_refs WHERE root_id=?", (oid,))
    if source is not None:
        _write_literal(db, "checkpoint_records", oid, source, ranges)
    for ordinal, ref in enumerate(refs):
        db.execute(
            """INSERT INTO state_checkpoint_refs(root_id,run_id,ordinal,at,kind,raw_sha,raw_bytes)
            VALUES(?,?,?,?,?,?,?)""",
            (oid, rid, ordinal, ref["at"], ref["kind"], ref["raw_sha"], ref["raw_bytes"]),
        )
    previous = [
        (row[0], row[1])
        for row in old_refs
        if (row[0], row[1]) not in {(r["kind"], r["raw_sha"]) for r in refs}
    ]
    _collect_removed(db, rid, previous)
    return oid


@_poison_mutation
def delete_query_db(db, sql, parameters=()):
    """Source-selected header query, never body JSON or caller-provided SQL."""
    _transaction(db, write=True)
    rows = db.execute(sql, parameters).fetchall()
    for row in rows:
        _validate_root(db, row[0])
    for row in rows:
        slot = row[0]
        root = db.execute("SELECT run_id FROM checkpoint_records WHERE slot_order=?", (slot,)).fetchone()
        if root is None:
            continue
        refs = db.execute(
            "SELECT kind,raw_sha FROM state_checkpoint_refs WHERE root_id=?", (slot,)
        ).fetchall()
        db.execute("DELETE FROM checkpoint_records WHERE slot_order=?", (slot,))
        _collect_removed(db, root[0], refs)
    return len(rows)


def checkpoint_rows_db(db, rid, *, keys=None, prefix=None, iterate_all=False, reader=None):
    """Explicit selected logical rows; the default cannot accidentally expand a whole run."""
    selected = sum((keys is not None, prefix is not None, iterate_all))
    if selected != 1:
        raise ValueError("Select exact keys, literal prefix, or explicit iterate_all")
    reader = _bound_reader(db, reader)
    available = checkpoint_keys_db(db, rid, prefix=prefix, reader=reader)
    if keys is not None:
        wanted = {key for key in keys if isinstance(key, str)}
        available = [key for key in available if key in wanted]
    return [(key, checkpoint_get_db(db, rid, key, reader=reader)) for key in available]


@_poison_mutation
def update_wire_db(db, rid, key, value):
    """Preserve the old UPDATE-only behavior for delivery cleanup; missing slots stay missing."""
    _transaction(db, write=True)
    if not db.execute("SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?", (rid, key)).fetchone():
        return 0
    put_wire_db(db, rid, key, value)
    return 1
