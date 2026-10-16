"""Async subprocess facade with a private supervisor control channel."""
import asyncio
import socket
import sys
from pathlib import Path


class ManagedProcess:
    def __init__(self, process, reader, writer):
        self.supervisor = process
        self.pid = process.pid
        self.command_pid = None
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self.returncode = None
        self.control = writer
        self.completed = asyncio.get_running_loop().create_future()
        self.started = asyncio.get_running_loop().create_future()
        self.stop_task = None
        self.monitor = asyncio.create_task(self._monitor(reader))

    async def _monitor(self, reader):
        import json
        try:
            while line := await reader.readline():
                event = json.loads(line)
                if "started" in event:
                    self.command_pid = event["started"]
                    if not self.started.done():
                        self.started.set_result(None)
                if "exited" in event:
                    self.returncode = event["exited"]
                    if not self.completed.done():
                        self.completed.set_result(self.returncode)
        finally:
            if not self.started.done():
                self.started.set_exception(RuntimeError("Process supervisor failed to start workload"))
            if not self.completed.done():
                # A killed workload cannot report before the anchor's final kill.
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


async def spawn_managed(command, cwd, env):
    parent, child = socket.socketpair()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("process_supervisor.py")), str(child.fileno()), *command,
            cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, pass_fds=(child.fileno(),), limit=8 * 1024 * 1024)
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    try:
        reader, writer = await asyncio.open_connection(sock=parent)
    except BaseException:
        parent.close()  # EOF asks the live anchor to clean up, without PID signalling.
        raise
    managed = ManagedProcess(process, reader, writer)
    try:
        await asyncio.shield(managed.started)
    except BaseException:
        writer.close()
        raise
    return managed
