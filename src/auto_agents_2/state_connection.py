"""Finite schema-9 connection lifecycle. Old-format data is not read here."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .errors import AA2Error

APPLICATION_ID = 0x41413239
SCHEMA_VERSION = "9"
CODEC = "limited-wire-v1"
MIGRATION_INTENT = "state-migration.json"


@contextmanager
def migration_gate(root: Path, *, exclusive=False):
    """Only current normal entry points obey this gate; legacy processes must exit."""
    path = root / "migration.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise AA2Error("Unsafe state maintenance lock") from exc
    with os.fdopen(fd, "a+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise AA2Error("Unsafe state maintenance lock")
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AA2Error("State maintenance is active; do not start another writer") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def require_format(db, *, allow_building=False):
    if db.execute("PRAGMA encoding").fetchone()[0] != "UTF-8":
        raise AA2Error("State format 9 requires UTF-8 database encoding; no implicit transcoding")
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not version or version[0] != SCHEMA_VERSION:
        raise AA2Error(
            "State schema 9 is required. Existing state needs separately supplied offline conversion; no automatic upgrade."
        )
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise AA2Error("State database application/codec identity mismatch")
    row = db.execute("SELECT codec,status,min_share_bytes FROM state_format WHERE singleton=1").fetchone()
    if (
        not row
        or row[0] != CODEC
        or row[1] not in ({"READY", "BUILDING"} if allow_building else {"READY"})
        or row[2] != 512
    ):
        raise AA2Error(
            "State format is incomplete or unsupported; preserve all files and finish offline validation"
        )


class StateConnection(sqlite3.Connection):
    """Explicit transaction epochs, including two read-only BEGIN scopes on one connection."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.isolation_level is None:
            super().close()
            raise AA2Error("Autocommit isolation_level=None is not supported by checkpoint connections")
        self.transaction_epoch = 0
        self.write_epoch = 0
        self._checkpoint_reader = None
        self._state_object_new = set()
        self._state_object_gc = False
        self._state_read_failure = None
        self.create_function("aa2_state_writer", 0, lambda: 9)
        self.create_function("aa2_state_object_new", 1, lambda oid: int(oid in self._state_object_new))
        self.create_function("aa2_state_object_gc", 0, lambda: int(self._state_object_gc))
        self.statement_observer = None

    def _before_sql(self, sql):
        # Conservative for unclassified statements/CTEs. SELECT does not share decoded objects.
        # SQLite accepts leading empty statements as trivia, including ; /*...*/ ;.
        # Comments may also follow a keyword immediately: BEGIN/**/, END/**/.
        match = re.match(r"^(?:[\s;]|\ufeff|--[^\n]*(?:\n|$)|/\*.*?\*/)*([A-Za-z]+)\b", sql, flags=re.S)
        word = match[1].upper() if match else ""
        if word in {"COMMIT", "END", "RELEASE"} and self._state_read_failure is not None:
            failure = self._state_read_failure
            self.rollback()
            raise failure
        if word in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
            self.transaction_epoch += 1
            self._state_object_new.clear()
        elif (
            word in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
            and not self.in_transaction
            and self.isolation_level is not None
        ):
            self.transaction_epoch += 1
        if word not in {"SELECT", "EXPLAIN"}:
            self.write_epoch += 1
        if self.statement_observer is not None:
            self.statement_observer(sql, word)
        return word

    @property
    def isolation_level(self):
        return sqlite3.Connection.isolation_level.__get__(self, type(self))

    @isolation_level.setter
    def isolation_level(self, value):
        raise AA2Error("Changing isolation_level is unsupported; use tracked explicit transactions")

    def cursor(self, factory=None):
        if factory is not None and not issubclass(factory, StateCursor):
            raise AA2Error("Checkpoint connections require tracked cursors")
        return super().cursor(factory=factory or StateCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script):
        raise AA2Error("Implicit script transaction changes are not supported; execute statements explicitly")

    def commit(self):
        self._before_sql("COMMIT")
        try:
            return super().commit()
        finally:
            # Even a no-op commit ends a caller's explicit reader lifecycle.
            self.transaction_epoch += 1
            self._state_object_new.clear()

    def rollback(self):
        self._before_sql("ROLLBACK")
        try:
            return super().rollback()
        finally:
            self._state_read_failure = None
            self.transaction_epoch += 1
            self._state_object_new.clear()

    def __enter__(self):
        super().__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        else:
            self.rollback()
        return False

    def close(self):
        self.transaction_epoch += 1
        self.write_epoch += 1
        super().close()

    def blobopen(self, table, column, row, /, *, readonly=False, name="main"):
        if not readonly:
            self.write_epoch += 1
        blob = super().blobopen(table, column, row, readonly=readonly, name=name)
        return blob if readonly else StateBlob(self, blob)


class StateCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        word = self.connection._before_sql(sql)
        result = super().execute(sql, parameters)
        if word == "ROLLBACK" and not self.connection.in_transaction:
            self.connection._state_read_failure = None
        return result

    def executemany(self, sql, parameters):
        self.connection._before_sql(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql_script):
        raise AA2Error("Implicit script transaction changes are not supported; execute statements explicitly")


class StateBlob:
    """Only the documented sqlite3.Blob API, with every mutation tracked."""

    def __init__(self, connection, blob):
        self.connection, self.blob = connection, blob

    def __len__(self):
        return len(self.blob)

    def __getitem__(self, key):
        return self.blob[key]

    def __setitem__(self, key, value):
        self.connection.write_epoch += 1
        self.blob[key] = value

    def write(self, data):
        self.connection.write_epoch += 1
        return self.blob.write(data)

    def read(self, length=-1):
        return self.blob.read(length)

    def seek(self, offset, origin=0):
        return self.blob.seek(offset, origin)

    def tell(self):
        return self.blob.tell()

    def close(self):
        return self.blob.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def check_project_format(project: Path, *, allow_missing=False):
    """Read-only, before configuration/provider/workspace side effects."""
    root = project.resolve() / ".aa2"
    if root.is_symlink():
        raise AA2Error("Project state must not be a symlink")
    if (root / MIGRATION_INTENT).exists():
        raise AA2Error("Offline state migration is incomplete; preserve its files")
    path = root / "state.sqlite"
    if not path.exists():
        if allow_missing:
            return False
        raise AA2Error("Project state does not exist; run aa2 init first")
    if path.is_symlink():
        raise AA2Error("Project state database must not be a symlink")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            require_format(db)
    except sqlite3.Error as exc:
        raise AA2Error("State format is unreadable; preserve it for offline diagnosis") from exc
    return True
