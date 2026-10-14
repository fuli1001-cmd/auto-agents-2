"""Incremental UTF-8 decoding and sanitized managed logs, separate from machine reports."""
from __future__ import annotations

import codecs
import os
from pathlib import Path

from .errors import AA2Error
from .util import scrub_text


class SanitizedLog:
    def __init__(self, path: Path, secrets=(), max_record=1024 * 1024):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("wb")
        os.chmod(path, 0o600)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.secrets = list(secrets)
        self.max_record = max_record
        self.tail = ""
        self.complete = False

    def feed(self, chunk: bytes):
        self.pending += self.decoder.decode(chunk)
        # Retain complete records so URL credentials, bearer tokens and known values
        # split at arbitrary subprocess boundaries cannot leak to the spool.
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._write(line + "\n")
        if len(self.pending) > self.max_record:
            raise AA2Error("Log record exceeds safe streaming capacity; capture is incomplete")

    def _write(self, text):
        clean = scrub_text(text, self.secrets)
        self.stream.write(clean.encode())
        self.tail = (self.tail + clean)[-25000:]

    def finish(self):
        self.pending += self.decoder.decode(b"", final=True)
        self._write(self.pending)
        self.pending = ""
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.complete = True
        self.stream.close()

    def close(self):
        if not self.stream.closed:
            self.stream.close()


async def capture_diagnostics(process, resources, artifacts, secrets=()):
    import asyncio

    from .progress import current_activity
    activity = current_activity.get()
    with resources.temporary("diagnostic-log") as directory:
        logs = [SanitizedLog(directory / name, secrets) for name in ("stdout", "stderr")]
        async def drain(stream, log):
            while chunk := await stream.read(65536):
                log.feed(chunk)
                if activity:
                    activity.output()
            log.finish()
        readers = [asyncio.create_task(drain(process.stdout, logs[0])), asyncio.create_task(drain(process.stderr, logs[1]))]
        try:
            if process.stdin:
                process.stdin.close()
            await asyncio.gather(*readers, process.wait())
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            for log in logs:
                log.close()
        refs = [artifacts.put_file(directory / name, "dependency_log") for name in ("stdout", "stderr")]
        return logs[0].tail.encode(), logs[1].tail.encode(), refs
