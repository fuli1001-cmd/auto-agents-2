"""Observations for display only. These events never advance workflow gates."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .errors import AA2Error
from .util import identifier

current_activity = ContextVar("aa2_activity", default=None)


async def capture_output(process, *, machine_limit=32 * 1024 * 1024):
    """Drain both streams, observing bytes without publishing their contents."""
    activity = current_activity.get()

    async def drain(stream):
        data = bytearray()
        while chunk := await stream.read(65536):
            data.extend(chunk)
            if len(data) > machine_limit:
                raise AA2Error("Machine output exceeds supported parsing capacity; capture is incomplete")
            if activity:
                activity.output()
        return bytes(data)

    readers = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
    try:
        if process.stdin:
            process.stdin.close()
        stdout, stderr, _ = await asyncio.gather(*readers, process.wait())
        return stdout, stderr
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


@contextmanager
def observe_activity(store, run_id, label):
    activity = Activity(store, run_id, label)
    token = current_activity.set(activity)
    try:
        yield activity
    except (asyncio.CancelledError, KeyboardInterrupt):
        activity.finish("interrupted")
        raise
    except BaseException:
        activity.finish("error")
        raise
    else:
        activity.finish()
    finally:
        current_activity.reset(token)


class Activity:
    def __init__(self, store, run_id, label, *, total=None):
        self.store, self.run_id = store, run_id
        self.id = identifier("activity")
        self.last_published = 0.0
        self.last_output = None
        self.store.event(run_id, "activity.started", {"activity": self.id, "label": label, "total": total})

    def output(self):
        self.last_output = time.time()
        if time.monotonic() - self.last_published >= 1:
            self.last_published = time.monotonic()
            self.store.event(self.run_id, "activity.output", {"activity": self.id, "last_output": self.last_output})

    def progress(self, done, total):
        self.store.event(self.run_id, "activity.progress", {"activity": self.id, "done": done, "total": total,
                                                          "last_output": self.last_output})

    def finish(self, outcome="success"):
        self.store.event(self.run_id, "activity.finished", {"activity": self.id, "outcome": outcome,
                                                          "last_output": self.last_output})
