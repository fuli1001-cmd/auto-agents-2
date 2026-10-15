from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .errors import BusyProject, PolicyViolation


def now() -> float:
    return time.time()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def digest(value: bytes | str | Any) -> str:
    if not isinstance(value, (bytes, str)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def atomic_write(path: Path, value: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value.encode() if isinstance(value, str) else value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise BusyProject("项目已有控制进程正在运行") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def contained(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise PolicyViolation(f"Invalid relative path: {relative}")
    result = root.joinpath(rel)
    if not result.resolve().is_relative_to(root.resolve()):
        raise PolicyViolation(f"Path escapes workspace: {relative}")
    return result


@contextlib.asynccontextmanager
async def async_file_lock(path: Path):
    while True:
        lock = file_lock(path, blocking=False)
        try:
            lock.__enter__()
            break
        except BusyProject:
            await asyncio.sleep(0.1)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


SECRET = re.compile(r"(api.?key|password|secret|token|authorization|credential)", re.I)


def redact(value: Any, key: str = "") -> Any:
    if SECRET.search(key):
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        result, hidden = [], False
        for item in value:
            if hidden:
                result.append("<redacted>")
                hidden = False
            elif isinstance(item, str) and item.startswith("-") and SECRET.search(item):
                result.append(item.split("=", 1)[0] + "=<redacted>" if "=" in item else item)
                hidden = "=" not in item
            else:
                result.append(redact(item))
        return result
    return value


def scrub_text(text: str, secrets: list[str] | None = None) -> str:
    # Match against the original text: replacing a short value must not expose
    # the suffix of a longer or partially overlapping credential.
    values = {part for secret in secrets or [] if secret
              for part in (secret, *secret.splitlines()) if part}
    spans = []
    for value in values:
        start = text.find(value)
        while start >= 0:
            spans.append((start, start + len(value)))
            start = text.find(value, start + 1)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + "<redacted>" + text[end:]
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1<redacted>", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1<redacted>@", text)
    text = re.sub(r"(?i)([?&](?:api[_-]?key|token|password|secret|access_token)=)[^&\s]+", r"\1<redacted>", text)
    return text
