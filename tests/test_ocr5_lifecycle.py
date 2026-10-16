import asyncio
import os
import sys

import pytest
from test_ocr4_lifecycle import (
    test_persisted_provider_result_waits_for_cleanup_and_is_reused_without_another_call as preserved_result,
)
from test_recovery_contracts import test_local_patch_recovers_each_side_effect_boundary as patch_boundary

from auto_agents_2.cleanup import finalize_async
from auto_agents_2.errors import PolicyViolation
from auto_agents_2.logs import AsyncLog
from auto_agents_2.network import PreviewProxy
from auto_agents_2.providers import EventDecoder
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.workspace import WorkspaceManager


@pytest.mark.parametrize('kind', ['codex', 'antigravity'])
def test_stream_retains_only_latest_complete_response(kind):
    decoder = EventDecoder(kind)
    for i in range(1000):
        text = str(i) + 'x' * 1024
        event = ({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}} if kind == 'codex'
            else {'event': 'result', 'result': {'status': 'SUCCESS', 'response': text}})
        decoder.feed(event)
    assert decoder.messages == [text]


@pytest.mark.parametrize('cancel', [False, True])
async def test_every_cleanup_runs_and_primary_failure_survives(cancel):
    called = []
    original = asyncio.CancelledError() if cancel else ValueError('primary')
    async def broken():
        called.append('first')
        raise OSError('close')
    async def second():
        called.append('second')
    with pytest.raises(type(original)) as caught:
        try:
            raise original
        finally:
            await finalize_async(broken, second)
    assert caught.value is original and called == ['first', 'second'] and original.__notes__


async def test_log_close_failure_still_shuts_down_executor(tmp_path):
    log = await AsyncLog(tmp_path / 'log').open()
    original = log.log.close
    def broken():
        original()
        raise OSError('close fixture')
    log.log.close = broken
    with pytest.raises(OSError, match='close fixture'):
        await log.close()
    assert log.worker._shutdown and log.log.stream.closed


@pytest.mark.parametrize("boundary", ["socketpair", "create_task"])
async def test_startup_construction_failure_leaves_no_unknown_process_record(tmp_path, monkeypatch, boundary):
    import auto_agents_2.managed_process as module
    def broken(*args):
        raise OSError(boundary)
    with monkeypatch.context() as patch:
        patch.setattr(module.socket if boundary == 'socketpair' else module.asyncio, boundary, broken)
        with pytest.raises(OSError, match=boundary):
            await module.spawn_managed(['true'], tmp_path, dict(os.environ), tmp_path / '.aa2', [tmp_path])
    assert not list((tmp_path / '.aa2/processes').glob('*.json'))


async def test_failed_stop_closes_channel_and_can_be_retried(tmp_path, monkeypatch):
    import auto_agents_2.managed_process as module
    runner = SandboxRunner(tmp_path / '.aa2', _test_unconfined=True)
    process = await runner.spawn([sys.executable, '-c', 'import time; time.sleep(60)'], tmp_path, dict(os.environ), [])
    original = module.finish_supervisor
    async def fail(*args):
        raise PolicyViolation('cannot yet confirm')
    with monkeypatch.context() as patch:
        patch.setattr(module, 'finish_supervisor', fail)
        with pytest.raises(PolicyViolation):
            await runner.stop(process)
    assert process.control.is_closing() and process.record.exists()
    await runner.stop(process)
    assert process.cleanup_confirmed and not process.record.exists()
    assert original


async def test_descendant_receives_grace_after_parent_exits(tmp_path):
    runner = SandboxRunner(tmp_path / '.aa2', _test_unconfined=True)
    marker = tmp_path / 'flushed'
    worker = 'import time,pathlib; time.sleep(1); pathlib.Path(' + repr(str(marker)) + ').write_text("complete")'
    script = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(worker) + '],start_new_session=True)'
    process = await runner.spawn([sys.executable, '-c', script], tmp_path, dict(os.environ), [])
    await process.wait()
    await runner.stop(process)
    assert marker.read_text() == 'complete'


async def test_request_relay_error_closes_idle_response_relay(monkeypatch):
    class Reader:
        async def readuntil(self, *args):
            return b'POST http://127.0.0.1:1234/ HTTP/1.1\r\nContent-Length: 100\r\n\r\n'
        async def read(self, *args):
            raise ConnectionResetError('upload reset')
    class Writer:
        closed = False
        def write(self, *args):
            pass
        async def drain(self):
            pass
        def close(self):
            self.closed = True
        def can_write_eof(self):
            return False
    downstream, upstream = Writer(), Writer()
    idle = asyncio.StreamReader()
    async def connect(*args):
        return idle, upstream
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    await asyncio.wait_for(PreviewProxy('http://127.0.0.1:1234', []).forward(Reader(), downstream), 1)
    assert upstream.closed and downstream.closed


@pytest.mark.parametrize('failure', [OSError, ValueError])
async def test_committed_response_never_fails_over_on_postcommit_exception(tmp_path, monkeypatch, failure):
    # Exercise the existing real provider fixture with a second cleanup failure class.
    import auto_agents_2.sandbox as module
    original = module.SandboxRunner.stop
    async def intercept(self, process):
        return await original(self, process)
    # The fixture replaces stop with PolicyViolation; replace that injected exception
    # at its origin so the full router/cache/usage assertions run for OS/value faults.
    import test_ocr4_lifecycle as fixture
    class CleanupFailure(failure):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(fixture, 'PolicyViolation', CleanupFailure)
        # confirm_record remains a PolicyViolation, so accept either in fixture assertions.
        patch.setattr(module, 'PolicyViolation', CleanupFailure)
        await preserved_result(tmp_path, patch)
    assert intercept


@pytest.mark.parametrize('boundary', ['snapshot', 'review'])
async def test_patch_resume_reconciles_commit_on_integration_workspace(tmp_path, monkeypatch, boundary):
    # Same fault scenarios as the original recovery contract, with the retained task
    # physically sharing integration: its owned snapshot advances integration HEAD.
    original = WorkspaceManager.allocate
    integration = None
    def allocate(self, rid, name, base):
        nonlocal integration
        if name == 'T3-retained':
            return integration
        result = original(self, rid, name, base)
        if name == 'integration':
            integration = result
        return result
    monkeypatch.setattr(WorkspaceManager, 'allocate', allocate)
    await patch_boundary(tmp_path, monkeypatch, boundary)


@pytest.mark.parametrize('cancel', [False, True])
async def test_cli_shutdown_renderer_cannot_replace_primary(tmp_path, monkeypatch, cancel):
    from test_runtime import configured

    import auto_agents_2.cli as cli
    _, store = configured(tmp_path)
    store.admit('renderer', {})
    original = asyncio.CancelledError() if cancel else ValueError('workflow failure')
    class Engine:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self, *args, **kwargs):
            raise original
    async def render(store, stop, *args):
        await stop.wait()
        raise OSError('shutdown display failure')
    monkeypatch.setattr(cli, 'WorkflowEngine', Engine)
    monkeypatch.setattr(cli, 'render', render)
    with pytest.raises(type(original)) as caught:
        await cli.execute(tmp_path)
    assert caught.value is original and original.__notes__


async def test_prototype_cancellation_closes_context_before_browser(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import playwright.async_api

    from auto_agents_2.design import inspect_prototype
    closed = []
    (tmp_path / 'index.html').write_text('<h1>fixture</h1>')
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    page = SimpleNamespace(on=lambda *a: None, goto=cancelled)
    class Context:
        async def new_page(self):
            return page
        async def close(self):
            closed.append('context')
    class Browser:
        async def close(self):
            closed.append('browser')
    async def context(*args):
        return Context()
    async def launch():
        return Browser()
    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(playwright.async_api, 'async_playwright', Playwright)
    monkeypatch.setattr('auto_agents_2.design.preview_context', context)
    with pytest.raises(asyncio.CancelledError):
        await inspect_prototype(tmp_path, tmp_path / 'images')
    assert closed == ['context', 'browser']


@pytest.mark.parametrize('subsystem', ['diagnostics', 'verification'])
async def test_log_cleanup_attempts_both_streams_and_preserves_failure(tmp_path, monkeypatch, subsystem):
    from types import SimpleNamespace

    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.config import Cleanup
    from auto_agents_2.logs import capture_diagnostics
    from auto_agents_2.models import Command
    from auto_agents_2.resources import ResourceManager
    from auto_agents_2.store import Store
    from auto_agents_2.verification import VerificationService
    store = Store(tmp_path)
    resources = ResourceManager(store, Cleanup())
    closed = []
    primary = ValueError('primary spawn or process failure')
    class Log:
        def __init__(self, path, *args):
            self.path = path
        async def open(self):
            pass
        async def feed(self, *args):
            pass
        async def finish(self):
            pass
        async def close(self):
            closed.append(self.path.name)
            if len(closed) == 1:
                raise OSError('close failure')
    async def fail(*args, **kwargs):
        raise primary
    monkeypatch.setattr('auto_agents_2.logs.AsyncLog', Log)
    monkeypatch.setattr('auto_agents_2.verification.AsyncLog', Log)
    with pytest.raises(ValueError) as caught:
        if subsystem == 'verification':
            await VerificationService(store.root, SimpleNamespace(spawn=fail), resources).run(
                [Command(argv=[sys.executable, '-c', 'pass'])], tmp_path)
        else:
            stream = asyncio.StreamReader()
            stream.feed_eof()
            process = SimpleNamespace(stdout=stream, stderr=stream, stdin=None, wait=fail)
            await capture_diagnostics(process, resources, ArtifactStore(store.root))
    assert caught.value is primary and len(closed) == 2 and primary.__notes__
