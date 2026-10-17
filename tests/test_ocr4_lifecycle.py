import asyncio
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_recovery_contracts import failure, plan
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.build_publication import BuildPublication
from auto_agents_2.config import Cleanup
from auto_agents_2.errors import AA2Error, ConfigurationError, PolicyViolation, ProviderError
from auto_agents_2.logs import SanitizedLog
from auto_agents_2.models import CheckResult, Command, ReviewResult
from auto_agents_2.network import PreviewProxy
from auto_agents_2.process_ownership import verify_connection
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner, process_start
from auto_agents_2.store import Store
from auto_agents_2.task_batch import collect_batch
from auto_agents_2.util import digest, write_json
from auto_agents_2.verification import VerificationService


@pytest.mark.parametrize('cancel', [False, True])
async def test_attach_retry_preserves_paused_run_and_cancel_flag(tmp_path, cancel):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('fixture', {})['id']
    workflow.store.update(rid, status='PAUSED')
    workflow.store.checkpoint(rid, 'control/cancel', cancel)
    async def forbidden(*args):
        pytest.fail('Attached retry dispatched paused run')
    workflow._execute = forbidden
    result = await workflow.start(run_id=rid, allow_resume=False)
    assert result['status'] == 'PAUSED' and workflow.store.get(rid, 'control/cancel') is cancel


def test_held_diagnostic_root_retains_its_whole_dependency_closure(tmp_path):
    store = Store(tmp_path)
    old = store.admit('old', {})['id']
    artifacts = ArtifactStore(store.root)
    child = artifacts.put('required raw body', 'document')
    store.checkpoint(old, 'child', child)
    store.update(old, status='SUCCEEDED')
    with store.connect() as db:
        db.execute('DELETE FROM checkpoints WHERE run_id=?', (old,))
    current = store.admit('current', {})['id']
    parent = artifacts.put({'child': child}, 'dependency_log')
    resources = ResourceManager(store, Cleanup())
    resources.collect_artifacts()
    assert artifacts.bytes(child) == b'required raw body' and artifacts.bytes(parent)
    assert current


def build_fixture(tmp_path):
    store = Store(tmp_path)
    rid = store.admit('fixture', {})['id']
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    work = store.root / 'worktrees' / rid / 'integration'
    generated = store.root / 'tmp/build/generated'
    for path, content in ((work, 'old'), (generated, 'new')):
        (path / 'dist').mkdir(parents=True)
        (path / 'dist/value').write_text(content)
    resources.register(work, 'worktree', rid)
    resources.register(generated.parent, 'temporary', rid)
    return store, resources, work, generated


@pytest.mark.parametrize('phase', ['preparing', 'prepared'])
def test_build_preparation_failure_leaves_no_unreachable_holds(tmp_path, monkeypatch, phase):
    import auto_agents_2.build_publication as module
    store, resources, work, generated = build_fixture(tmp_path)
    original = module.write_json
    def failed(path, value):
        if value['phase'] == phase:
            raise OSError('journal fault')
        return original(path, value)
    monkeypatch.setattr(module, 'write_json', failed)
    with pytest.raises(OSError, match='journal fault'):
        BuildPublication(resources).publish(Command(argv=['build'], outputs=['dist']), work, generated)
    with store.connect() as db:
        assert not db.execute('SELECT 1 FROM resource_holds').fetchone()
    assert (work / 'dist/value').read_text() == 'old'


def test_pending_build_must_verify_durable_image_before_releasing_holds(tmp_path, monkeypatch):
    import auto_agents_2.build_publication as module
    store, resources, work, generated = build_fixture(tmp_path)
    publisher = BuildPublication(resources)
    original_release = publisher.release
    synced = []
    original_sync = module.sync_tree
    def sync(path):
        synced.append(path)
        return original_sync(path)
    monkeypatch.setattr(module, 'sync_tree', sync)
    monkeypatch.setattr(publisher, 'release', lambda *a: (_ for _ in ()).throw(OSError('release fault')))
    with pytest.raises(OSError, match='release fault'):
        publisher.publish(Command(argv=['build'], outputs=['dist']), work, generated)
    assert generated / 'dist/value' in synced and work / 'dist/value' in synced
    (work / 'dist/value').write_text('unexpected')
    with pytest.raises(AA2Error, match='changed'):
        BuildPublication(resources).recover(work)
    with store.connect() as db:
        assert db.execute('SELECT 1 FROM resource_holds').fetchone()
    (work / 'dist/value').write_text('new')
    monkeypatch.setattr(publisher, 'release', original_release)
    publisher.recover(work)
    (work / 'dist/value').write_text('legitimate subsequent edit')
    publisher.recover(work)
    assert (work / 'dist/value').read_text() == 'legitimate subsequent edit'


@pytest.mark.parametrize('cancel', [False, True])
async def test_startup_channel_failure_joins_cleanup_before_returning(tmp_path, monkeypatch, cancel):
    runner = SandboxRunner(tmp_path / '.aa2', _test_unconfined=True)
    entered = asyncio.Event()
    async def broken(**kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise OSError('channel failure')
    monkeypatch.setattr(asyncio, 'open_connection', broken)
    task = asyncio.create_task(runner.spawn([sys.executable, '-c', 'import time; time.sleep(60)'], tmp_path, dict(os.environ), []))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        await task
    assert not list((runner.root / 'processes').glob('*.json'))


async def test_slow_log_io_does_not_block_cancellation_or_release_live_spools(tmp_path, monkeypatch):
    store = Store(tmp_path)
    runner = SandboxRunner(store.root, _test_unconfined=True)
    service = VerificationService(store.root, runner)
    entered, release = threading.Event(), threading.Event()
    original = SanitizedLog.feed
    def slow(self, chunk):
        entered.set()
        assert release.wait(5)
        return original(self, chunk)
    monkeypatch.setattr(SanitizedLog, 'feed', slow)
    task = asyncio.create_task(service.run([Command(argv=[sys.executable, '-c',
        'import time; print("line", flush=True); time.sleep(60)'])], tmp_path, None))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(.01)
        task.cancel()
        async with asyncio.timeout(3):
            while list((store.root / 'processes').glob('*.json')):
                await asyncio.sleep(.01)
        assert not task.done()  # IO still owns its files, but no workload is live.
        assert list((store.root / 'tmp').glob('check-log*/*'))
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not list((store.root / 'tmp').glob('check-log*'))


async def test_cancellation_archive_failure_keeps_workspace_and_runs_other_cleanup(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('fixture', {})['id']
    work = workflow.store.root / 'worktrees' / rid / 'candidate'
    work.mkdir(parents=True)
    (work / 'unfinished').write_text('keep')
    workflow.resources.register(work, 'worktree', rid)
    async def execute(*args):
        workflow.store.checkpoint(rid, 'control/cancel', True)
        raise asyncio.CancelledError()
    workflow._execute = execute
    workflow.archive_workspaces = lambda *a: (_ for _ in ()).throw(OSError('archive fault'))
    calls = []
    original = workflow.resources.collect_artifacts_safely
    def collect():
        calls.append(True)
        return original()
    monkeypatch.setattr(workflow.resources, 'collect_artifacts_safely', collect)
    assert (await workflow.start(run_id=rid))['status'] == 'CANCELLED'
    assert (work / 'unfinished').read_text() == 'keep' and len(calls) >= 2
    workflow.resources.collect()
    assert work.exists()


async def test_plain_http_upstream_eof_finishes_proxy_without_client_eof():
    async def accept(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(accept, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    proxy = await PreviewProxy(f'http://127.0.0.1:{port}', []).start()
    reader, writer = await asyncio.open_connection('127.0.0.1', proxy.server.sockets[0].getsockname()[1])
    try:
        writer.write(f'GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).endswith(b'ok')
        await asyncio.sleep(.02)
        assert not proxy.tasks
    finally:
        writer.close()
        await proxy.close()
        server.close()
        await server.wait_closed()


async def test_preview_ownership_includes_children_created_by_worker_threads(tmp_path):
    runner = SandboxRunner(tmp_path / '.aa2', _test_unconfined=True)
    script = ('import threading,subprocess,sys,time\n'
              'def run():\n p=subprocess.Popen([sys.executable,"-u","-c",'
              '"import socket,time; s=socket.socket(); s.bind((\\\"127.0.0.1\\\",0)); s.listen(); print(s.getsockname()[1],flush=True); c,_=s.accept(); time.sleep(60)"]); p.wait()\n'
              't=threading.Thread(target=run); t.start(); t.join()')
    process = await runner.spawn([sys.executable, '-u', '-c', script], tmp_path, dict(os.environ), [])
    writer = None
    try:
        port = int(await asyncio.wait_for(process.stdout.readline(), 3))
        _, writer = await asyncio.open_connection('127.0.0.1', port)
        await verify_connection(process, writer)
    finally:
        if writer:
            writer.close()
        await runner.stop(process)


async def test_adopted_zombies_are_reaped_during_long_running_workload(tmp_path):
    runner = SandboxRunner(tmp_path / '.aa2', _test_unconfined=True)
    script = ('import os,time\n'
              'pid=os.fork()\n'
              'if pid==0:\n child=os.fork()\n if child==0: time.sleep(.05); os._exit(0)\n print(child,flush=True); os._exit(0)\n'
              'os.waitpid(pid,0); time.sleep(60)')
    process = await runner.spawn([sys.executable, '-u', '-c', script], tmp_path, dict(os.environ), [])
    try:
        child = int(await process.stdout.readline())
        async with asyncio.timeout(3):
            while Path(f'/proc/{child}').exists():
                await asyncio.sleep(.02)
        assert process.returncode is None
    finally:
        await runner.stop(process)


async def test_provider_success_survives_preference_persistence_failure(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    rid = store.admit('fixture', {})['id']
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    calls = []
    async def call(effective, *args):
        calls.append(effective['provider'])
        if len(calls) == 1:
            raise ProviderError('fixture failure')
        return ReviewResult(approved=True, summary='validated paid result')
    monkeypatch.setattr(router, '_call', call)
    monkeypatch.setattr(config, 'activate', lambda *a, **k: (_ for _ in ()).throw(ConfigurationError('write fault')))
    result = await router.invoke('review', {}, ReviewResult, tmp_path, rid, 'review')
    assert result.summary == 'validated paid result' and len(calls) == 2


def test_native_auth_rotation_refreshes_mirror_but_keeps_provider_refresh(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit('fixture', {})['id']
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'auth.json').write_text('{"token":"native-one"}')
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    effective = {**config.resolve('review', 'codex'), 'environment': {**os.environ, 'CODEX_HOME': str(native)}}
    mirror = router._session_overlay(effective, rid, 'same-operation')[0][0]
    (mirror / 'auth.json').write_text('{"token":"provider-refreshed"}')
    router._session_overlay(effective, rid, 'same-operation')
    assert 'provider-refreshed' in (mirror / 'auth.json').read_text()
    (native / 'auth.json').write_text('{"token":"native-two"}')
    router._session_overlay(effective, rid, 'same-operation')
    assert 'native-two' in (mirror / 'auth.json').read_text()
    assert 'native-two' not in json.dumps(store.get(rid, 'native-auth/' + digest(str(mirror))))


async def test_patch_grant_cannot_adopt_user_edits_as_its_baseline(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('fixture', {})['id']
    (tmp_path / 'base.py').write_text('baseline')
    baseline = workflow.workspaces.checkpoint(tmp_path, 'refs/aa2/base', 'baseline')
    work = workflow.workspaces.allocate(rid, 'integration', baseline)
    record = failure()
    active = {'attempt': 1, 'decision': {'reason': 'amend'}}
    async def grant(*args):
        (work / 'user.txt').write_text('retain this edit')
    workflow._grant_replan = grant
    with pytest.raises(AA2Error, match='baseline changed'):
        await workflow.recovery._patch(rid, active, record, plan(), work)
    assert (work / 'user.txt').read_text() == 'retain this edit'
    assert not workflow.agent.calls


async def test_recheck_reservation_replays_before_dispatch_and_not_after_unknown_completion(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('fixture', {})['id']
    feedback = {'checks': [CheckResult(argv=['pytest'], cwd='.', exit_code=1, duration=0, output='',
        source_hash='s', environment_hash='e', failure_reasons=['test_report_missing']).model_dump()]}
    for _ in range(2):
        assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ['part.py'], observation='same') == 'recheck'
    assert not workflow.agent.calls
    workflow.recovery.start_delivery_recheck(rid, tmp_path)
    with pytest.raises(AA2Error, match='Interrupted'):
        await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ['part.py'], observation='same')
    workflow.recovery.complete_delivery_recheck(rid, feedback)
    assert await workflow.recovery.delivery_decision(rid, tmp_path, feedback, ['part.py'], observation='same') == 'repair'


@pytest.mark.parametrize('dry_run', [False, True])
def test_resource_gc_collects_child_and_parent_in_one_pass(tmp_path, dry_run):
    store = Store(tmp_path)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    parent = store.root / 'tmp/parent'
    child = parent / 'child'
    child.mkdir(parents=True)
    (child / 'data').write_text('fixture')
    resources.register(parent, 'temporary', refs=0)
    resources.register(child, 'temporary', refs=0)
    result = resources.collect(dry_run=dry_run)
    assert result['candidates'] == [str(child), str(parent)]
    assert parent.exists() is dry_run


@pytest.mark.parametrize("receipt", [{"digest": "bad"}, {}, [], "malformed-json"])
def test_corrupt_receipt_protects_resources_without_blocking_other_delivery_cleanup(tmp_path, receipt):
    store = Store(tmp_path)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    old = store.admit('old', {})['id']
    retained = store.root / 'tmp/old'
    retained.mkdir(parents=True)
    resources.register(retained, 'temporary', old)
    artifact = ArtifactStore(store.root).put('keep damaged receipt evidence', 'check_log')
    store.checkpoint(old, 'delivery/receipt', receipt)
    if receipt == 'malformed-json':
        with store.connect() as db:
            db.execute("UPDATE checkpoints SET data='broken-json' WHERE run_id=? AND key='delivery/receipt'", (old,))
    store.update(old, status='SUCCEEDED', cleanup_pending=True)
    resources.recover_disposable()
    resources.finish_pending()
    assert retained.exists() and store.run(old)['data']['cleanup_pending']
    assert ArtifactStore(store.root).bytes(artifact) == b'keep damaged receipt evidence'
    assert store.admit('new', {})['id'] != old


def test_partial_schema_v5_is_repaired_without_changing_budgets(tmp_path):
    store = Store(tmp_path)
    rid = store.admit('fixture', {})['id']
    store.checkpoint(rid, 'rounds/T', {'round': 4})
    with store.connect() as db:
        db.execute('ALTER TABLE questions DROP COLUMN interaction')
        db.execute('ALTER TABLE questions DROP COLUMN superseded')
        db.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    reopened = Store(tmp_path)
    with reopened.connect() as db:
        assert 'interaction' not in {row[1] for row in db.execute('PRAGMA table_info(questions)')}
    reopened.migrate()
    assert reopened.get(rid, 'rounds/T') == {'round': 4}
    question = reopened.request(rid, 'test', 'Prompt', '')
    reopened.answer(question['id'], 'answer', '')
    with reopened.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '7'
    assert (reopened.root / 'state-v5.backup.sqlite').exists()


def test_failed_initial_schema_is_never_published_and_can_retry(tmp_path, monkeypatch):
    original = sqlite3.connect
    class Fault(sqlite3.Connection):
        def execute(self, sql, *args):
            if 'CREATE TABLE IF NOT EXISTS questions' in sql:
                raise sqlite3.OperationalError('fixture schema fault')
            return super().execute(sql, *args)
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: original(*a, **k, factory=Fault))
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path)
    assert not (tmp_path / '.aa2/state.sqlite').exists()
    monkeypatch.setattr(sqlite3, 'connect', original)
    store = Store(tmp_path)
    rid = store.admit('retry', {})['id']
    assert store.request(rid, 'q', 'Prompt', '')


@pytest.mark.parametrize('unknown', ['unreadable', 'empty'])
def test_unknown_process_identity_keeps_recovery_record(tmp_path, monkeypatch, unknown):
    runner = SandboxRunner(tmp_path / '.aa2')
    record = runner.root / 'processes/record.json'
    write_json(record, {'pid': os.getpid(), 'start': process_start(os.getpid()) if unknown == 'unreadable' else ''})
    original = Path.read_text
    def denied(path, *args, **kwargs):
        if str(path).startswith('/proc/'):
            raise PermissionError('fixture proc permission')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', denied)
    with pytest.raises(PolicyViolation):
        runner.reap_orphans()
    assert record.exists()


async def test_partial_batch_construction_cancels_and_joins_created_tasks():
    completed = []
    async def child():
        try:
            await asyncio.sleep(60)
        finally:
            completed.append(True)
    before = asyncio.all_tasks()
    def values():
        yield child()
        raise RuntimeError('enumeration fault')
    with pytest.raises(RuntimeError, match='enumeration'):
        await collect_batch(values())
    assert asyncio.all_tasks() == before


def test_supervisor_retries_inspection_and_individual_kill_failures_until_cleanup(monkeypatch):
    import auto_agents_2.process_supervisor as module
    reads, killed, records = [], [], []
    class FakePath:
        def __init__(self, value):
            self.value = value
        def read_text(self):
            if self.value == 'record':
                return '{"version":3}'
            if self.value.endswith('/stat'):
                return '1 (anchor) ' + ' '.join(['S', '0'] + ['0'] * 17 + ['123'])
            reads.append(True)
            if len(reads) == 1:
                raise PermissionError('transient inspection failure')
            return '42 43' if len(reads) < 225 else ''
    def kill(pid, sig):
        killed.append(pid)
        if pid == 42:
            raise PermissionError('one child is temporarily unkillable')
    def waitpid(*args):
        raise ChildProcessError()
    control = SimpleNamespace(set_inheritable=lambda *a: None, sendall=lambda *a: None, recv=lambda *a: b'', close=lambda: None)
    monkeypatch.setattr(module, 'Path', FakePath)
    monkeypatch.setattr(module, 'save', lambda path, data: records.append(dict(data)))
    monkeypatch.setattr(module, 'sys', SimpleNamespace(argv=['script', '4', 'record', 'command']))
    monkeypatch.setattr(module, 'socket', SimpleNamespace(socket=lambda **k: control))
    monkeypatch.setattr(module, 'ctypes', SimpleNamespace(CDLL=lambda *a, **k: SimpleNamespace(prctl=lambda *a: 0)))
    monkeypatch.setattr(module, 'os', SimpleNamespace(getpid=lambda: 1, close=lambda *a: None,
        killpg=lambda *a: None, kill=kill, waitpid=waitpid, WNOHANG=1))
    monkeypatch.setattr(module, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9, signal=lambda *a: None))
    monkeypatch.setattr(module, 'subprocess', SimpleNamespace(Popen=lambda *a, **k: SimpleNamespace(pid=42, returncode=0)))
    monkeypatch.setattr(module, 'select', SimpleNamespace(select=lambda *a: ([control], [], [])))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: len(reads), sleep=lambda *a: None))
    module.main()
    assert len(reads) == 225 and killed.count(43) > 200 and records[-1]['status'] == 'cleaned'


async def test_rejected_delivery_diagnosis_keeps_evidence_source_on_resume(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('fixture', {})['id']
    original = workflow.store.checkpoints
    async def diagnosis(*args, **kwargs):
        return Diagnosis(action='repair', evidence_refs=['invented'], reason='bad reference')
    workflow._invoke = diagnosis
    def interrupt(owner, updates, **kwargs):
        original(owner, updates, **kwargs)
        if any(key.startswith('recovery/diagnosis-rejections/') for key in updates):
            raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'checkpoints', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.delivery_decision(rid, tmp_path, {'review': 'failed'}, ['file.py'], observation='fixed')
    (tmp_path / 'user-edit.py').write_text('changed')
    monkeypatch.setattr(workflow.store, 'checkpoints', original)
    with pytest.raises(AA2Error, match='source changed'):
        await workflow.recovery.delivery_decision(rid, tmp_path, {'review': 'failed'}, ['file.py'], observation='fixed')


def test_unknown_cleanup_pins_resources_until_durable_confirmation(tmp_path):
    store = Store(tmp_path)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    root = store.root / 'attempts/fixture'
    root.mkdir(parents=True)
    resources.register(root, 'temporary', refs=0)
    record = store.root / 'processes/fixture.json'
    write_json(record, {'version': 3, 'status': 'running', 'paths': [str(root)], 'cwd': str(root), 'pid': 123, 'start': '456'})
    resources.collect()
    assert root.exists()
    runner = SandboxRunner(store.root)
    with pytest.raises(PolicyViolation, match='awaits process cleanup'):
        runner.confirm_record(str(record))
    write_json(record, {'version': 3, 'status': 'cleaned', 'paths': [str(root)], 'cwd': str(root), 'pid': 123, 'start': '456'})
    runner.confirm_record(str(record))
    resources.collect()
    assert not root.exists()


async def test_cancellation_during_started_handshake_joins_monitor_and_supervisor(tmp_path, monkeypatch):
    from auto_agents_2.managed_process import ManagedProcess
    runner = SandboxRunner(tmp_path / '.aa2', _test_unconfined=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ManagedProcess._monitor
    async def delayed(self, reader):
        entered.set()
        await release.wait()
        return await original(self, reader)
    monkeypatch.setattr(ManagedProcess, '_monitor', delayed)
    task = asyncio.create_task(runner.spawn([sys.executable, '-c', 'import time; time.sleep(60)'], tmp_path, dict(os.environ), []))
    await entered.wait()
    task.cancel()
    try:
        async with asyncio.timeout(3):
            while True:
                record = next((runner.root / 'processes').glob('*.json'))
                if json.loads(record.read_text()).get('status') == 'cleaned':
                    break
                await asyncio.sleep(.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((runner.root / 'processes').glob('*.json'))


async def test_resuming_delivery_repair_does_not_complete_recheck_with_old_feedback(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('fixture', {})['id']
    feedback = {'checks': [CheckResult(argv=['pytest'], cwd='.', exit_code=1, duration=0, output='',
        source_hash='s', environment_hash='e', failure_reasons=['test_report_missing']).model_dump()]}
    await workflow._delivery_repair(rid, tmp_path, feedback, ['part.py'], observation='fixed')
    workflow.recovery.start_delivery_recheck(rid, tmp_path)
    key = workflow.store.get(rid, 'recovery/delivery-recheck-active')
    with pytest.raises(AA2Error, match='Interrupted'):
        await workflow._delivery_repair(rid, tmp_path, feedback, ['part.py'], observation='fixed')
    assert workflow.store.get(rid, key)['phase'] == 'started'


async def test_persisted_provider_result_waits_for_cleanup_and_is_reused_without_another_call(tmp_path, monkeypatch):
    from auto_agents_2.engine import WorkflowEngine
    config, store = configured(tmp_path)
    binary = tmp_path / 'native-fixture'
    binary.write_text(f'#!{sys.executable}\n' + '''import json,sys
sys.stdin.read()
args=sys.argv[1:]
result={"approved":True,"summary":"preserved","findings":[]}
open(args[args.index("-o")+1],"w").write(json.dumps(result))
print(json.dumps({"type":"thread.started","thread_id":"fixture"}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":3}}))
''')
    binary.chmod(0o755)
    native = tmp_path / 'native'
    native.mkdir()
    body = config.load().model_dump(mode='json')
    spec = body['providers']['codex']
    spec.update(binary=str(binary), environment={'CODEX_HOME': str(native)})
    body.update(active_provider='codex', providers={'codex': spec})
    write_json(config.path, body)
    rid = store.admit('fixture', {})['id']
    runner = SandboxRunner(store.root, _test_unconfined=True)
    workflow = WorkflowEngine(tmp_path, sandbox=runner)
    processes = []
    original_spawn, original_stop = runner.spawn, runner.stop
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process
    async def unknown(*args):
        raise PolicyViolation('fixture unknown cleanup')
    monkeypatch.setattr(runner, 'spawn', spawn)
    monkeypatch.setattr(runner, 'stop', unknown)
    try:
        from auto_agents_2.errors import PostCommitError
        with pytest.raises(PostCommitError, match='result saved'):
            await workflow._invoke('review', {}, ReviewResult, tmp_path, rid, 'paid-result')
        assert store.usage_summary(rid)['total_tokens'] == 14
        saved = store.get(rid, 'invocation/paid-result')
        assert saved['result']['summary'] == 'preserved'
        assert Path(saved['cleanup_record']).exists()
        assert list((store.root / 'attempts' / rid).iterdir())
        with pytest.raises(PolicyViolation, match='awaits process cleanup'):
            await workflow._invoke('review', {}, ReviewResult, tmp_path, rid, 'paid-result')
    finally:
        monkeypatch.setattr(runner, 'stop', original_stop)
        for process in processes:
            await original_stop(process)
    result = await workflow._invoke('review', {}, ReviewResult, tmp_path, rid, 'paid-result')
    assert result.summary == 'preserved' and len(processes) == 1
    assert store.usage_summary(rid)['total_tokens'] == 14
