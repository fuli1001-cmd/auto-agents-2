"""Async subprocess facade with a durable lifecycle and private control channel."""
import asyncio
import json
import socket
import sys
from pathlib import Path

from .errors import PolicyViolation
from .process_records import read_record
from .util import identifier, write_json


async def joined(task):
    """Delay cancellation until an owned operation finishes; never abandon it."""
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
        except BaseException as exc:
            if cancelled:
                cancelled.add_note("Owned operation failed: " + type(exc).__name__)
                raise cancelled
            raise
    if cancelled:
        raise cancelled
    return result


class ManagedProcess:
    def __init__(self, process, reader, writer, record):
        self.supervisor = process
        self.pid = process.pid
        self.command_pid = None
        self.record = record
        self.start = json.loads(record.read_text()).get("start")
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self.returncode = None
        self.control = writer
        self.completed = asyncio.get_running_loop().create_future()
        self.started = asyncio.get_running_loop().create_future()
        self.cleanup_confirmed = False
        self.stop_task = None
        self.monitor = asyncio.create_task(self._monitor(reader))

    async def _monitor(self, reader):
        try:
            while line := await reader.readline():
                event = json.loads(line)
                if "started" in event:
                    self.command_pid = event["started"]
                    self.start = json.loads(self.record.read_text()).get("start")
                    if not self.started.done():
                        self.started.set_result(None)
                if "exited" in event:
                    self.returncode = event["exited"]
                    if not self.completed.done():
                        self.completed.set_result(self.returncode)
                if event.get("cleaned"):
                    self.cleanup_confirmed = True
        finally:
            if not self.started.done():
                self.started.set_exception(PolicyViolation("Process supervisor failed to start workload"))
            if not self.completed.done():
                self.returncode = -9
                self.completed.set_result(self.returncode)

    async def wait(self):
        return await asyncio.shield(self.completed)

    async def communicate(self, input=None):
        async def feed():
            try:
                if input:
                    self.stdin.write(input)
                    await self.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.stdin.close()
        _, stdout, stderr, _ = await asyncio.gather(feed(), self.stdout.read(), self.stderr.read(), self.wait())
        return stdout, stderr


def _supervisor_cleaned(process, record):
    data = read_record(record)
    return (data.get("version") == 3 and data.get("status") == "cleaned"
            and data.get("pid") == process.pid)


async def finish_supervisor(process, record):
    deadline = asyncio.get_running_loop().time() + 6
    while process.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    pending_exit = process.returncode is None
    if pending_exit and not _supervisor_cleaned(process, record):
        raise PolicyViolation("Child termination could not be confirmed; process recovery record retained")
    # A busy event loop can reach the deadline before delivering an exit already
    # observed by its child watcher. Only an owned durable cleanup receipt permits
    # reconciling that notification within the existing final wait; it cannot
    # substitute for the supervisor's actual exit.
    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.at_eof():
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError as exc:
        if pending_exit:
            raise PolicyViolation(
                "Child termination could not be confirmed; process recovery record retained") from exc
        raise
    if process.returncode is None:
        raise PolicyViolation("Child termination could not be confirmed; process recovery record retained")
    if not _supervisor_cleaned(process, record):
        raise PolicyViolation("Supervisor exited without cleanup confirmation; resources retained")


async def spawn_managed(command, cwd, env, state_root, paths):
    record = state_root / "processes" / (identifier("process") + ".json")
    write_json(record, {"version": 3, "status": "starting", "paths": [str(p) for p in paths], "cwd": str(cwd)})
    parent = child = process = managed = creation = None
    try:
        parent, child = socket.socketpair()
        coroutine = asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("process_supervisor.py")), str(child.fileno()), str(record), *command,
            cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, pass_fds=(child.fileno(),), limit=8 * 1024 * 1024)
        try:
            creation = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        process = await asyncio.shield(creation)
        child.close()
        reader, writer = await asyncio.open_connection(sock=parent)
        managed = ManagedProcess(process, reader, writer, record)
        await asyncio.shield(managed.started)
        return managed
    except BaseException as original:
        if creation is None:
            record.unlink(missing_ok=True)  # No subprocess creation was attempted.
            raise
        async def cleanup():
            nonlocal process
            try:
                process = process or await creation
            except BaseException:
                record.unlink(missing_ok=True)
                return
            finally:
                child.close()
            if managed:
                managed.control.close()
            else:
                parent.close()
            await finish_supervisor(process, record)
            if managed:
                try:
                    await managed.monitor
                finally:
                    if managed.started.done() and not managed.started.cancelled():
                        managed.started.exception()
            record.unlink(missing_ok=True)
        try:
            await joined(asyncio.create_task(cleanup()))
        except BaseException as exc:
            original.add_note("Startup cleanup failed; retain process records: " + type(exc).__name__)
        raise
    finally:
        if child is not None:
            child.close()
        if parent is not None and (process is None or managed is None):
            parent.close()
