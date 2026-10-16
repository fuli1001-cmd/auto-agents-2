"""Collect recoverable failures, but promptly propagate a task's pause/cancellation."""
import asyncio


async def collect_batch(coroutines):
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    pending = set(tasks)
    try:
        while pending:
            completed, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(task.cancelled() for task in completed):
                raise asyncio.CancelledError()
        return [task.exception() or task.result() for task in tasks]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
