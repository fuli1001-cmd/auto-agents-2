"""Owned, deadline-limited helpers for blocking evidence acquisition and publication."""
import asyncio
import json
import os
import sys

from .cleanup import finalize_async
from .config import AttachmentCapture, ConfigStore
from .errors import CollectionError
from .managed_process import spawn_managed
from .sandbox import SandboxRunner
from .util import digest


def capture_policy(root):
    config = ConfigStore(root.parent)
    policy = config.load().attachment_capture if config.path.exists() else AttachmentCapture()
    return policy.model_dump()


def policy_identity(root):
    return digest(["attachment-capture-v1", capture_policy(root)])


def capture_contract(root):
    """Actual per-command acquisition limits; not a new business obligation."""
    limits = capture_policy(root)
    return {"version": 1, "limits": limits,
        "policy_hash": digest(["attachment-capture-v1", limits]),
        "output_root": "controller-selected playwright-results for each command",
        "counting": "physical_paths_in_each_command_output_root",
        "duplicate_paths_count_separately": True,
        "scope": "All files under that root, including copies made by testInfo.attach(path). "
            "The count is not distinct contents, test cases, or only attached metadata.",
        "preserve": "Keep all accepted tests, assertions, viewports, required PNG, actual state JSON and "
            "download bytes. Recording budgets do not authorize dropping evidence, changing limits or "
            "overriding an adopted method. A changed accepted method needs its owner's formal correction.",
        "collection": "Write real required bytes under the controller output root. Memory-only attachment "
            "metadata is not proof of archived bytes. Complete acquisition is required before acceptance."}


async def run_collection(root, request, *, timeout=30, paths=()):
    """A helper process can be stopped even when a filesystem read or parser stalls."""
    process = await spawn_managed([sys.executable, "-I", "-m", "auto_agents_2.collection_worker", str(root)],
        root.parent, dict(os.environ), root, [root, *paths])
    async def exchange():
        async def send():
            process.stdin.write(json.dumps(request).encode())
            await process.stdin.drain()
            process.stdin.close()
        async def receive(stream, limit):
            body = bytearray()
            while chunk := await stream.read(65536):
                if len(body) + len(chunk) > limit:
                    raise CollectionError("Collection helper response exceeds limit")
                body.extend(chunk)
            return bytes(body)
        tasks = [asyncio.create_task(job) for job in (send(), receive(process.stdout, 32 * 1024**2),
                                                      receive(process.stderr, 65536), process.wait())]
        try:
            _, result, _, _ = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            reply = json.loads(result)
        except ValueError as exc:
            raise CollectionError("Collection helper did not publish a complete result") from exc
        if process.returncode or "error" in reply:
            raise CollectionError(reply.get("error", "Collection helper failed"), reply.get("details"))
        return reply["result"]
    operation = asyncio.create_task(exchange())
    async def stop_exchange():
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(operation)
    except TimeoutError as exc:
        raise CollectionError("Evidence acquisition exceeded its deadline", {"reason": "timeout", "timeout_seconds": timeout}) from exc
    finally:
        await finalize_async(lambda: SandboxRunner(root).stop(process), stop_exchange)
