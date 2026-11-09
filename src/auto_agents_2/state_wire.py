"""Controller-private finite wire scanning. No business JSON references are interpreted.

Ported from the independently reviewed r2 file prototype, SHA a0285178ac389dab750ea34ac9705e312e51ce3c0563aac0777f2bcd4ed7f7be.
Only known request/history paths are selected; raw JSON spelling is preserved.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import pathlib
import re
from types import SimpleNamespace

from .errors import AA2Error
from .util import digest

CHUNK = 64 * 1024
MAX_SELECTED_SPANS = 512
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_OBJECTS = 512
MAX_TOTAL_REFS = 512
MAX_WIRE_BYTES = 256 * 1024**2
STRING_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
HEX = frozenset(b"0123456789abcdefABCDEF")
PREFIXES = ("recovery/request-snapshots/", "decisions/requests/", "decisions/applied/")


class JSONSyntax(ValueError):
    pass


class Admission(AA2Error):
    pass


class Integrity(AA2Error):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_wire(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()


logical_sha = digest


def typed_load(raw, source_type):
    if source_type == "text":
        return json.loads(raw.decode("utf-8"))
    if source_type == "blob":
        return json.loads(raw)
    raise Admission("unsupported SQLite data type")


def byte_hash(path):
    h = hashlib.sha256()
    with wire_source(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


class MemorySource:
    def __init__(self, raw):
        if not isinstance(raw, bytes):
            raise TypeError("wire source must be bytes")
        self.raw = raw

    def open(self, mode="rb"):
        if mode != "rb":
            raise ValueError("wire sources are read-only")
        return io.BytesIO(self.raw)

    def stat(self):
        return SimpleNamespace(st_size=len(self.raw))


class BlobSource:
    def __init__(self, db, table, column, rowid):
        self.db, self.table, self.column, self.rowid = db, table, column, rowid
        with self.open() as stream:
            self.size = len(stream)

    def open(self, mode="rb"):
        if mode != "rb":
            raise ValueError("wire sources are read-only")
        return self.db.blobopen(self.table, self.column, self.rowid, readonly=True)

    def stat(self):
        return SimpleNamespace(st_size=self.size)


def wire_source(source):
    if isinstance(source, (MemorySource, BlobSource)):
        return source
    return pathlib.Path(source)


class Input:
    def __init__(self, path, source_type, chunk=CHUNK):
        self.stream = wire_source(path).open("rb")
        self.buf = b""
        self.pos = 0
        self.base = 0
        self.eof = False
        self.active = []
        self.chunk = chunk
        self.whole = hashlib.sha256()
        self.read_bytes = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")(
            "strict" if source_type == "text" else "surrogatepass"
        )

    def fill(self):
        if self.pos < len(self.buf):
            return True
        if self.eof:
            return False
        self.base += len(self.buf)
        self.pos = 0
        self.buf = self.stream.read(self.chunk)
        self.decoder.decode(self.buf, final=not self.buf)
        self.whole.update(self.buf)
        self.read_bytes += len(self.buf)
        if not self.buf:
            self.eof = True
            return False
        return True

    def peek(self):
        return self.buf[self.pos] if self.fill() else None

    def tell(self):
        return self.base + self.pos

    def advance(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise AssertionError("buffer boundary")
        if self.active:
            part = memoryview(self.buf)[self.pos : self.pos + n]
            for capture in self.active:
                capture["hasher"].update(part)
        self.pos += n

    def one(self):
        value = self.peek()
        if value is None:
            raise JSONSyntax("unexpected EOF")
        self.advance(1)
        return value

    def ws(self):
        while self.peek() in (32, 9, 10, 13):
            self.advance(1)

    def close(self):
        self.stream.close()


class Scanner:
    """Only container stack, bounded key sets, and finite selected spans. Never values AST."""

    def __init__(
        self,
        path,
        key,
        source_type,
        *,
        chunk=CHUNK,
        max_depth=128,
        max_key_bytes=65536,
        max_live_key_bytes=4 * 1024**2,
        max_live_keys=32768,
        max_spans=512,
        max_wire=256 * 1024**2,
    ):
        self.path = wire_source(path)
        self.source_type = source_type
        if source_type not in {"text", "blob"}:
            raise Admission("unsupported SQLite data type")
        if type(max_spans) is not int or not 1 <= max_spans <= MAX_SELECTED_SPANS:
            raise Admission("selected span parameter limit")
        if type(max_wire) is not int or not 0 <= max_wire <= MAX_WIRE_BYTES:
            raise Admission("wire limit parameter budget")
        if self.path.stat().st_size > max_wire:
            raise Admission("wire byte admission limit")
        self.request_path = (
            ("request",)
            if key.startswith(PREFIXES)
            else ("context", "decision_request")
            if key.startswith("recovery/invocation-inputs/")
            else None
        )
        self.r = Input(path, source_type, chunk)
        self.max_depth = max_depth
        self.max_key_bytes = max_key_bytes
        self.max_live_key_bytes = max_live_key_bytes
        self.max_live_keys = max_live_keys
        self.max_spans = max_spans
        self.live_key_bytes = 0
        self.live_keys = 0
        self.duplicate = False
        self.spans = []
        self.max_seen_depth = 0

    def string(self, capture=False):
        if self.r.one() != 34:
            raise JSONSyntax("string expected")
        pieces = [b'"'] if capture else None
        count = 1

        def take(n):
            nonlocal count
            if capture:
                count += n
                if count > self.max_key_bytes:
                    raise Admission("key byte admission limit")
                pieces.append(self.r.buf[self.r.pos : self.r.pos + n])
            self.r.advance(n)

        while True:
            if not self.r.fill():
                raise JSONSyntax("unterminated string")
            match = STRING_SPECIAL.search(self.r.buf, self.r.pos)
            end = match.start() if match else len(self.r.buf)
            take(end - self.r.pos)
            if match is None:
                continue
            value = self.r.buf[self.r.pos]
            if value == 34:
                take(1)
                return b"".join(pieces) if capture else None
            if value < 32:
                raise JSONSyntax("unescaped control character")
            take(1)  # backslash
            escape = self.r.peek()
            if escape is None:
                raise JSONSyntax("unterminated escape")
            if escape in b'"\\/bfnrt':
                take(1)
            elif escape == 117:
                take(1)
                for _ in range(4):
                    if self.r.peek() not in HEX:
                        raise JSONSyntax("invalid Unicode escape")
                    take(1)
            else:
                raise JSONSyntax("invalid escape")

    def literal(self, text):
        for value in text:
            if self.r.one() != value:
                raise JSONSyntax("invalid literal")

    def number(self):
        digits = 0
        if self.r.peek() == 45:
            self.r.advance(1)
            if self.r.peek() == 73:
                self.literal(b"Infinity")
                return
        value = self.r.peek()
        if value == 48:
            self.r.advance(1)
        elif value is not None and 49 <= value <= 57:
            while self.r.peek() is not None and 48 <= self.r.peek() <= 57:
                self.r.advance(1)
                digits += 1
                if digits > 8192:
                    raise Admission("number token admission limit")
        else:
            raise JSONSyntax("invalid integer")
        if self.r.peek() == 46:
            self.r.advance(1)
            if self.r.peek() is None or not 48 <= self.r.peek() <= 57:
                raise JSONSyntax("invalid fraction")
            while self.r.peek() is not None and 48 <= self.r.peek() <= 57:
                self.r.advance(1)
        if self.r.peek() in (69, 101):
            self.r.advance(1)
            if self.r.peek() in (43, 45):
                self.r.advance(1)
            if self.r.peek() is None or not 48 <= self.r.peek() <= 57:
                raise JSONSyntax("invalid exponent")
            while self.r.peek() is not None and 48 <= self.r.peek() <= 57:
                self.r.advance(1)

    def capture_kind(self, path, token):
        if token != 123 or self.request_path is None:
            return None
        if path == self.request_path:
            return "request"
        prefix = self.request_path + ("evidence", "history", "recent")
        if len(path) == len(prefix) + 1 and path[:-1] == prefix and type(path[-1]) is int:
            return "permit_snapshot"
        return None

    def value(self, path=(), depth=0):
        self.r.ws()
        token = self.r.peek()
        kind = self.capture_kind(path, token)
        capture = None
        if kind:
            if len(self.spans) + len(self.r.active) >= self.max_spans:
                raise Admission("selected span admission limit")
            capture = {"start": self.r.tell(), "kind": kind, "path": path, "hasher": hashlib.sha256()}
            self.r.active.append(capture)
        if token in (123, 91):
            if depth >= self.max_depth:
                raise Admission("nesting depth admission limit")
            self.max_seen_depth = max(self.max_seen_depth, depth + 1)
        if token == 123:
            self.r.advance(1)
            self.r.ws()
            seen = set()
            cost = 0
            try:
                if self.r.peek() == 125:
                    self.r.advance(1)
                else:
                    while True:
                        self.r.ws()
                        if self.r.peek() != 34:
                            raise JSONSyntax("object key expected")
                        raw = self.string(True)
                        key = json.loads(
                            raw.decode("utf-8", "strict" if self.source_type == "text" else "surrogatepass")
                        )
                        if key in seen:
                            self.duplicate = True
                        else:
                            size = len(raw) + 64
                            seen.add(key)
                            cost += size
                            self.live_keys += 1
                            self.live_key_bytes += size
                            if (
                                self.live_keys > self.max_live_keys
                                or self.live_key_bytes > self.max_live_key_bytes
                            ):
                                raise Admission("live key-set admission limit")
                        self.r.ws()
                        if self.r.one() != 58:
                            raise JSONSyntax("colon expected")
                        self.value(path + (key,), depth + 1)
                        self.r.ws()
                        separator = self.r.one()
                        if separator == 125:
                            break
                        if separator != 44:
                            raise JSONSyntax("object separator expected")
            finally:
                self.live_keys -= len(seen)
                self.live_key_bytes -= cost
        elif token == 91:
            self.r.advance(1)
            self.r.ws()
            index = 0
            if self.r.peek() == 93:
                self.r.advance(1)
            else:
                while True:
                    self.value(path + (index,), depth + 1)
                    index += 1
                    self.r.ws()
                    separator = self.r.one()
                    if separator == 93:
                        break
                    if separator != 44:
                        raise JSONSyntax("array separator expected")
        elif token == 34:
            self.string()
        elif token == 116:
            self.literal(b"true")
        elif token == 102:
            self.literal(b"false")
        elif token == 110:
            self.literal(b"null")
        elif token == 78:
            self.literal(b"NaN")
        elif token == 73:
            self.literal(b"Infinity")
        elif token == 45 or token is not None and 48 <= token <= 57:
            self.number()
        else:
            raise JSONSyntax("JSON value expected")
        if capture:
            if self.r.active.pop() is not capture:
                raise AssertionError("capture stack")
            self.spans.append(
                {
                    "start": capture["start"],
                    "end": self.r.tell(),
                    "kind": kind,
                    "sha": capture["hasher"].hexdigest(),
                    "path": path,
                }
            )

    def scan(self):
        try:
            if self.source_type == "blob":
                with self.path.open("rb") as prefix_stream:
                    prefix = prefix_stream.read(4)
                encoding = json.detect_encoding(prefix)
                if encoding != "utf-8" and encoding != "utf-8-sig":
                    return {"status": "non_utf8_blob_inline", "spans": []}
                if prefix.startswith(codecs.BOM_UTF8):
                    for _ in range(3):
                        self.r.one()
            self.value()
            self.r.ws()
            if self.r.peek() is not None:
                raise JSONSyntax("trailing data")
            spans = [] if self.duplicate else self.spans
            return {
                "status": "duplicate_inline" if self.duplicate else "valid",
                "spans": spans,
                "wire_sha": self.r.whole.hexdigest(),
                "wire_bytes": self.r.read_bytes,
                "max_depth": self.max_seen_depth,
                "duplicate": self.duplicate,
                "utf8_validation_complete": self.source_type == "text" and self.r.eof,
            }
        finally:
            self.r.close()


def range_blocks(path, start, end):
    with wire_source(path).open("rb") as stream:
        stream.seek(start)
        remaining = end - start
        while remaining:
            block = stream.read(min(CHUNK, remaining))
            if not block:
                raise Integrity("short source range")
            remaining -= len(block)
            yield block


def validate_complete_text_utf8(path):
    """Encoding proof for all remaining TEXT after lexical fallback; no large value DOM."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    whole = hashlib.sha256()
    size = 0
    with wire_source(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            decoder.decode(block, final=False)
            whole.update(block)
            size += len(block)
    decoder.decode(b"", final=True)
    return {"utf8_validation_complete": True, "wire_sha": whole.hexdigest(), "wire_bytes": size}


def scan_wire(source, key, source_type):
    """No DOM. Invalid JSON is retained opaque; invalid SQLite TEXT encoding stops."""
    if source_type not in {"text", "blob"}:
        raise Admission("unsupported wire source type")
    try:
        result = Scanner(source, key, source_type).scan()
    except Admission:
        raise
    except UnicodeDecodeError as exc:
        if source_type == "text":
            raise Admission("Invalid UTF-8 TEXT cannot represent the original SQLite str") from exc
        result = {"status": "opaque_invalid_blob", "spans": [], "parse_error": type(exc).__name__}
    except JSONSyntax as exc:
        result = {"status": "opaque_invalid_json", "spans": [], "parse_error": str(exc)}
        if source_type == "text":
            try:
                result.update(validate_complete_text_utf8(source))
            except UnicodeDecodeError as encoding_error:
                raise Admission("Invalid UTF-8 TEXT after JSON fallback") from encoding_error
    if source_type == "text" and result.get("utf8_validation_complete") is not True:
        raise Admission("Complete TEXT UTF-8 validation not established")
    result.setdefault("wire_bytes", wire_source(source).stat().st_size)
    if "wire_sha" not in result:
        result["wire_sha"] = byte_hash(source)
    return result
