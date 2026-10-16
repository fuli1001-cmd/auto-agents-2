"""Finish every owned cleanup action before propagating cancellation or errors."""
import asyncio
import inspect
import sys

from .managed_process import joined


async def finalize_async(*actions, original=None):
    original = original if original is not None else sys.exception()

    async def finish():
        failures = []
        for action in actions:
            try:
                result = action()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                failures.append(exc)
        if original is not None:
            for failure in failures:
                original.add_note("Cleanup also failed: " + type(failure).__name__)
        elif len(failures) == 1:
            raise failures[0]
        elif failures:
            raise BaseExceptionGroup("Owned cleanup failed", failures)

    try:
        await joined(asyncio.create_task(finish()))
    except BaseException as exc:
        if original is None:
            raise
        original.add_note("Cleanup also interrupted: " + type(exc).__name__)
