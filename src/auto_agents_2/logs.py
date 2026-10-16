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
        try:
            os.chmod(path, 0o600)
        except BaseException:
            self.stream.close()
            raise
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
            self._check_size(line)
            self._write(line + "\n")
        self._check_size(self.pending)

    def _check_size(self, text):
        if len(text.encode("utf-8")) > self.max_record:
            raise AA2Error("Log record exceeds safe streaming capacity; capture is incomplete")

    def _write(self, text):
        clean = scrub_text(text, self.secrets)
        self.stream.write(clean.encode())
        self.tail = (self.tail + clean)[-25000:]

    def finish(self):
        self.pending += self.decoder.decode(b"", final=True)
        self._check_size(self.pending)
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
        logs, readers = [], []
        try:
            for name in ("stdout", "stderr"):
                log = AsyncLog(directory / name, secrets)
                logs.append(log)
                await log.open()
            async def drain(stream, log):
                while chunk := await stream.read(65536):
                    await log.feed(chunk)
                    if activity:
                        activity.output()
                await log.finish()
            readers = [asyncio.create_task(drain(process.stdout, logs[0])), asyncio.create_task(drain(process.stderr, logs[1]))]
            if process.stdin:
                process.stdin.close()
            await asyncio.gather(*readers, process.wait())
            refs = [await publish_log(artifacts, directory / name, "dependency_log", process) for name in ("stdout", "stderr")]
            return logs[0].tail.encode(), logs[1].tail.encode(), refs
        finally:
            import sys
            if sys.exception() is not None:
                try:
                    process.control.write_eof()
                except (AttributeError, OSError, RuntimeError):
                    pass
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            for log in logs:
                await log.close()


class AsyncLog:
    """One serialized worker owns all writes, finalization and publication."""
    def __init__(self, path, secrets=()):
        from concurrent.futures import ThreadPoolExecutor
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aa2-log")
        self.path, self.secrets = path, secrets
        self.log = None
        self.pending = set()

    async def call(self, function, *args):
        import asyncio
        future = asyncio.get_running_loop().run_in_executor(self.worker, function, *args)
        self.pending.add(future)
        future.add_done_callback(lambda done: (self.pending.discard(done), done.exception() if not done.cancelled() else None))
        # Keep pending work owned even when the reader is cancelled. The caller
        # can terminate the workload before waiting for slow storage to finish.
        return await asyncio.shield(future)

    async def open(self):
        def create():
            self.log = SanitizedLog(self.path, self.secrets)
        await self.call(create)
        return self

    async def feed(self, chunk):
        await self.call(self.log.feed, chunk)

    async def finish(self):
        await self.call(self.log.finish)

    @property
    def tail(self):
        return self.log.tail if self.log else ""

    @property
    def complete(self):
        return bool(self.log and self.log.complete)

    async def close(self):
        import asyncio

        from .managed_process import joined
        async def finish():
            await asyncio.gather(*self.pending, return_exceptions=True)
            if self.log:
                await self.call(self.log.close)
            self.worker.shutdown(wait=False)
        await joined(asyncio.create_task(finish()))


async def publish_log(artifacts, path, kind, process=None):
    import asyncio

    from .managed_process import joined
    task = asyncio.create_task(asyncio.to_thread(artifacts.put_file, path, kind))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if process is not None:
            try:
                process.control.write_eof()
            except (AttributeError, OSError, RuntimeError):
                pass
        await joined(task)
        raise
