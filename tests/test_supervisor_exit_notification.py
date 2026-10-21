"""Cleanup requires both the durable receipt and the actual supervisor exit."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime import configured

from auto_agents_2 import managed_process
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import PolicyViolation
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_cleanup_survives_delayed_exit_notification(tmp_path, cancel):
    runner = SandboxRunner(tmp_path / "state", _test_unconfined=True)
    process = await runner.spawn([
        sys.executable, "-c", "import signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"],
        tmp_path, dict(os.environ), [tmp_path])
    assert await process.stdout.readline() == b"ready\n"
    loop = asyncio.get_running_loop()
    # Concurrent synchronous environment hashing can occupy the controller loop
    # past its polling deadline. The independent supervisor still reaps children.
    delayed = loop.call_later(.005, time.sleep, 6.5)
    stopping = asyncio.create_task(runner.stop(process))
    cancellation = loop.call_later(.05, stopping.cancel) if cancel else None
    try:
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await stopping
        assert process.cleanup_confirmed
        assert process.supervisor.returncode == 0
        assert not Path(f"/proc/{process.pid}").exists()
        assert not process.record.exists()
        await runner.stop(process)
    finally:
        delayed.cancel()
        if cancellation:
            cancellation.cancel()
        await asyncio.sleep(.1)
        await runner.stop(process)


async def test_environment_machine_command_keeps_output_after_delayed_cleanup(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    runner = SandboxRunner(store.root, _test_unconfined=True)
    manager = EnvironmentManager(config, runner)
    finish = managed_process.finish_supervisor
    observed = []

    async def delayed_finish(process, record):
        observed.append((process, record))
        # Delay before asyncio can deliver the exit notification for this stop.
        handle = asyncio.get_running_loop().call_soon(time.sleep, 6.5)
        try:
            return await finish(process, record)
        finally:
            handle.cancel()

    with monkeypatch.context() as patch:
        patch.setattr(managed_process, "finish_supervisor", delayed_finish)
        try:
            output = await manager._command(
                [sys.executable, "-I", "-c", "print('{\"packages\": []}')"],
                tmp_path, dict(os.environ), [], machine=True)
            assert json.loads(output) == {"packages": []}
            assert len(observed) == 1
            assert observed[0][0].returncode == 0
            assert not observed[0][1].exists()
        finally:
            # Reconcile any retained, already-cleaned fixture on an old version.
            await asyncio.sleep(.1)
            for process, record in observed:
                if record.exists():
                    await finish(process, record)
                    record.unlink()


async def test_startup_rollback_reconciles_delayed_supervisor_exit(tmp_path, monkeypatch):
    runner = SandboxRunner(tmp_path / "state", _test_unconfined=True)
    finish = managed_process.finish_supervisor
    observed = []

    async def failed_connection(*args, **kwargs):
        raise OSError("private channel setup failed")

    async def delayed_finish(process, record):
        observed.append((process, record))
        handle = asyncio.get_running_loop().call_soon(time.sleep, 6.5)
        try:
            return await finish(process, record)
        finally:
            handle.cancel()

    with monkeypatch.context() as patch:
        patch.setattr(managed_process.asyncio, "open_connection", failed_connection)
        patch.setattr(managed_process, "finish_supervisor", delayed_finish)
        try:
            with pytest.raises(OSError, match="private channel setup failed") as caught:
                await runner.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                                   tmp_path, dict(os.environ), [tmp_path])
            assert not getattr(caught.value, "__notes__", [])
            assert len(observed) == 1 and observed[0][0].returncode == 0
            assert not list((runner.root / "processes").glob("*.json"))
        finally:
            await asyncio.sleep(.1)
            for process, record in observed:
                if record.exists():
                    await finish(process, record)
                    record.unlink()


class PendingSupervisor:
    pid = 123
    returncode = None
    stdout = None
    stderr = None

    def __init__(self, outcome="exit", before_wait=None):
        self.outcome = outcome
        self.before_wait = before_wait
        self.waited = False

    async def wait(self):
        self.waited = True
        if self.before_wait:
            self.before_wait()
        if self.outcome == "pending":
            await asyncio.Future()
        if self.outcome == "exit":
            self.returncode = 0
        return self.returncode


@pytest.fixture
def expired_poll(tmp_path, monkeypatch):
    # Exercise the deadline boundaries without sleeping six seconds for every
    # invalid receipt. Keep a real bounded async wait for a supervisor that hangs.
    clock = iter([0, 7])

    async def bounded_wait(awaitable, timeout):
        return await asyncio.wait_for(awaitable, min(timeout, .02))

    monkeypatch.setattr(managed_process, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: next(clock)),
        sleep=asyncio.sleep, wait_for=bounded_wait))
    record = tmp_path / "process.json"
    write_json(record, {"version": 3, "status": "cleaned", "pid": 123,
                        "start": "456", "paths": [str(tmp_path)], "cwd": str(tmp_path)})
    return record


@pytest.mark.parametrize("damage", [
    "missing", "malformed", "non_object", "unknown_identity", "other_supervisor",
    "running", "starting", "legacy",
])
async def test_unconfirmed_receipt_cannot_extend_cleanup(expired_poll, damage):
    record = expired_poll
    data = json.loads(record.read_text())
    if damage == "missing":
        record.unlink()
    elif damage == "malformed":
        record.write_text("{")
    elif damage == "non_object":
        write_json(record, [])
    else:
        if damage == "unknown_identity":
            del data["pid"]
        elif damage == "other_supervisor":
            data["pid"] += 1
        elif damage == "legacy":
            data["version"] = 2
        else:
            data["status"] = damage
        write_json(record, data)
    before = record.read_bytes() if record.exists() else None
    process = PendingSupervisor()
    with pytest.raises(PolicyViolation):
        await managed_process.finish_supervisor(process, record)
    assert not process.waited
    assert (record.read_bytes() if record.exists() else None) == before


@pytest.mark.parametrize("outcome", ["pending", "no_exit"])
async def test_cleaned_receipt_never_substitutes_for_actual_exit(expired_poll, outcome):
    process = PendingSupervisor(outcome)
    before = expired_poll.read_bytes()
    with pytest.raises(PolicyViolation):
        await managed_process.finish_supervisor(process, expired_poll)
    assert process.waited and process.returncode is None
    assert expired_poll.read_bytes() == before


@pytest.mark.parametrize("change", ["running", "identity", "missing"])
async def test_cleanup_rechecks_receipt_after_exit(expired_poll, change):
    def changed():
        if change == "missing":
            expired_poll.unlink()
        else:
            data = json.loads(expired_poll.read_text())
            if change == "running":
                data["status"] = "running"
            else:
                data["pid"] += 1
            write_json(expired_poll, data)

    process = PendingSupervisor(before_wait=changed)
    with pytest.raises(PolicyViolation):
        await managed_process.finish_supervisor(process, expired_poll)
    assert process.waited and process.returncode == 0
    assert expired_poll.exists() == (change != "missing")


async def test_delayed_exit_is_confirmed_without_erasing_receipt(expired_poll):
    process = PendingSupervisor()
    before = expired_poll.read_bytes()
    await managed_process.finish_supervisor(process, expired_poll)
    assert process.returncode == 0 and process.waited
    # The caller owns final monitor reconciliation and removal, not this helper.
    assert expired_poll.read_bytes() == before
