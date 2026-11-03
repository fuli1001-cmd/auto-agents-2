"""Host reboot proof is distinct from process cleanup and provider authority."""
import base64
import copy
import fcntl
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from auto_agents_2 import cli
from auto_agents_2 import process_reboot as recovery
from auto_agents_2.errors import AA2Error, BusyProject, PolicyViolation
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def boot_observation(boot):
    return {
        'computer': 'FIXTURE-HOST', 'last_boot_utc': utc(boot), 'distributions': ['Fixture-WSL'],
        'event_xml': f"""<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
<System><Provider Name="Microsoft-Windows-Kernel-General" Guid="{{a68ca8b7-004f-d7b6-a698-07e2de0f1f5d}}"/>
<EventID>12</EventID><Channel>System</Channel><Computer>FIXTURE-HOST</Computer>
<EventRecordID>42</EventRecordID><TimeCreated SystemTime="{utc(boot + .5)}"/></System>
<EventData><Data Name="StartTime">{utc(boot)}</Data><Data Name="BootMode">0</Data></EventData></Event>""",
    }


@pytest.fixture
def scene(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    state = project / '.aa2'
    processes = state / 'processes'
    processes.mkdir(parents=True)
    rid, aid = 'run_' + '1' * 16, 'attempt_' + '2' * 16
    record = processes / ('process_' + '3' * 16 + '.json')
    attempt = state / 'attempts' / rid / aid
    (attempt / 'workspace').mkdir(parents=True)
    (attempt / 'out').mkdir()
    (attempt / 'out' / 'retained').write_text('provider output remains protected by its original owners')
    document = {'version': 3, 'status': 'running', 'pid': 12345, 'start': '900000',
                'cwd': str(attempt / 'workspace'), 'paths': [str(attempt / 'workspace'), str(attempt / 'out')]}
    record.write_text(json.dumps(document, indent=3) + '\n')
    original = record.read_bytes()
    boot = time.time() + 60  # Synthetic host proof; fixture files predate it.
    controller = tmp_path / 'running.json'
    controller.write_text(json.dumps({
        'started_utc': utc(boot - 1000), 'runner_pid': 12343, 'runner_start_ticks': 800000,
        'controller_pid': 12344, 'controller_start_ticks': 800001,
        'argv': ['/fixture/python', '-B', '-m', 'auto_agents_2', 'resume', '--project', str(project), '--run', rid],
    }))
    events = [
        {'seq': 10, 'run_id': rid, 'time': boot - 100, 'kind': 'agent.started',
         'data': {'attempt': aid, 'stage': 'decision', 'operation': 'decisions/fixture/1'}},
        {'seq': 11, 'run_id': rid, 'time': boot - 99.9, 'kind': 'agent.spawned',
         'data': {'attempt': aid, 'pid': 12345}},
    ]
    database = state / 'state.sqlite'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY,run_id TEXT,time REAL,kind TEXT,data TEXT)')
        db.execute('CREATE TABLE checkpoints(key TEXT PRIMARY KEY,data TEXT)')
        db.execute('INSERT INTO checkpoints VALUES (?,?)', ('infra/fixture', '{"pending":true,"failures":5,"grant":12}'))
        for row in events:
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                       (row['seq'], row['run_id'], row['time'], row['kind'], json.dumps(row['data'])))
    observed = boot_observation(boot)
    kernel = {'boot_id': 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', 'release': 'fixture-microsoft-WSL2', 'distribution': 'Fixture-WSL'}
    request = {'version': 1, 'project': str(project), 'record': record.name,
               'record_sha256': digest(original), 'run_id': rid, 'attempt_id': aid,
               'started_event_seq': 10, 'spawned_event_seq': 11, 'events_sha256': digest(events),
               'controller_record': str(controller), 'controller_sha256': digest(controller.read_bytes()),
               'boot_id': kernel['boot_id'], 'windows_computer': 'FIXTURE-HOST', 'windows_event_record_id': 42}
    request_path = tmp_path / 'request.json'
    def save_request():
        request_path.write_text(json.dumps(request))
        return digest(request_path.read_bytes())
    expected = save_request()
    monkeypatch.setattr(recovery, 'kernel_identity', lambda: dict(kernel))
    monkeypatch.setattr(recovery, 'windows_boot', lambda: copy.deepcopy(observed))
    monkeypatch.setattr(recovery, 'process_start', lambda pid: '')
    return SimpleNamespace(**locals())


def apply(scene):
    return recovery.reconcile(scene.project, scene.request_path, scene.expected, confirm_local_host_reboot=True)


def test_verified_host_reboot_consumes_only_selected_record_and_is_repeatable(scene):
    other = scene.processes / 'process_4444444444444444.json'
    other.write_bytes(scene.original)
    before_database = scene.database.read_bytes()
    result = apply(scene)
    assert not scene.record.exists() and other.read_bytes() == scene.original
    assert scene.database.read_bytes() == before_database
    assert (scene.attempt / 'out/retained').is_file()
    receipt = json.loads(Path(result['receipt']).read_text())
    assert receipt['kind'] == 'verified_wsl_host_reboot'
    assert base64.b64decode(receipt['record_bytes']) == scene.original
    assert base64.b64decode(receipt['controller_bytes']) == scene.controller.read_bytes()
    assert receipt['windows_xml_sha256'] == digest(scene.observed['event_xml'])
    assert json.loads(base64.b64decode(receipt['record_bytes']))['status'] == 'running'
    previous = Path(result['receipt']).read_bytes()
    assert apply(scene)['already_absent'] is True
    assert Path(result['receipt']).read_bytes() == previous
    assert scene.database.read_bytes() == before_database


def test_normal_resume_still_refuses_legacy_record_without_receipt(scene, monkeypatch):
    monkeypatch.setattr('auto_agents_2.sandbox.process_start', lambda pid: '')
    with pytest.raises(PolicyViolation, match='lacks confirmation'):
        SandboxRunner(scene.state).reap_orphans()
    assert scene.record.read_bytes() == scene.original


def test_confirmation_and_exact_request_hash_are_required(scene):
    with pytest.raises(PolicyViolation, match='explicit confirmation'):
        recovery.reconcile(scene.project, scene.request_path, scene.expected)
    scene.request_path.write_text(scene.request_path.read_text() + ' ')
    with pytest.raises(PolicyViolation, match='request hash'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('damage', ['provider', 'guid', 'channel', 'computer', 'event', 'record', 'boot_mode',
                                   'duplicate_start', 'os_boot', 'distro', 'same_boot'])
def test_wrong_or_ambiguous_host_proof_never_consumes_record(scene, damage):
    replacements = {
        'provider': ('Microsoft-Windows-Kernel-General', 'Fixture-Provider'),
        'guid': ('a68ca8b7-004f-d7b6-a698-07e2de0f1f5d', '00000000-0000-0000-0000-000000000000'),
        'channel': ('<Channel>System</Channel>', '<Channel>Application</Channel>'),
        'computer': ('<Computer>FIXTURE-HOST</Computer>', '<Computer>OTHER</Computer>'),
        'event': ('<EventID>12</EventID>', '<EventID>13</EventID>'),
        'record': ('<EventRecordID>42</EventRecordID>', '<EventRecordID>41</EventRecordID>'),
        'boot_mode': ('<Data Name="BootMode">0</Data>', '<Data Name="BootMode">1</Data>'),
        'duplicate_start': ('</EventData>', '<Data Name="StartTime">' + utc(scene.boot) + '</Data></EventData>'),
    }
    if damage in replacements:
        scene.observed['event_xml'] = scene.observed['event_xml'].replace(*replacements[damage])
    elif damage == 'os_boot':
        scene.observed['last_boot_utc'] = utc(scene.boot - 1)
    elif damage == 'distro':
        scene.observed['distributions'] = ['Unrelated-WSL']
    else:
        scene.observed.update(boot_observation(scene.boot - 200))
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('damage', ['pid', 'attempt', 'run', 'time', 'controller', 'cwd', 'paths', 'record_hash'])
def test_original_controller_and_attempt_are_not_replaceable_by_assertions(scene, damage):
    if damage in {'pid', 'attempt', 'run', 'time'}:
        row = scene.events[1]
        if damage == 'pid':
            row['data']['pid'] += 1
        elif damage == 'attempt':
            row['data']['attempt'] = 'attempt_' + '5' * 16
        elif damage == 'run':
            row['run_id'] = 'run_' + '6' * 16
        else:
            row['time'] = scene.boot + 1
        with closing(sqlite3.connect(scene.database)) as db, db:
            db.execute('UPDATE events SET run_id=?,time=?,data=? WHERE seq=11',
                       (row['run_id'], row['time'], json.dumps(row['data'])))
        # Even a freshly reviewed hash cannot turn the wrong event into proof.
        scene.request['events_sha256'] = digest(scene.events)
    elif damage == 'controller':
        document = json.loads(scene.controller.read_text())
        document['argv'][-1] = 'run_' + '7' * 16
        scene.controller.write_text(json.dumps(document))
        scene.request['controller_sha256'] = digest(scene.controller.read_bytes())
    else:
        document = json.loads(scene.original)
        if damage == 'cwd':
            document['cwd'] = str(scene.project / 'elsewhere')
        elif damage == 'paths':
            document['paths'] = [str(scene.project)]
        else:
            document['start'] = '900001'
        scene.record.write_text(json.dumps(document))
        if damage != 'record_hash':
            scene.request['record_sha256'] = digest(scene.record.read_bytes())
    scene.expected = scene.save_request()
    before = scene.record.read_bytes()
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == before


@pytest.mark.parametrize('pid', [12343, 12344, 12345])
def test_any_matching_live_original_identity_blocks_without_signalling(scene, monkeypatch, pid):
    starts = {12343: '800000', 12344: '800001', 12345: '900000'}
    monkeypatch.setattr(recovery, 'process_start', lambda current: starts[pid] if current == pid else '')
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('reconciliation must never signal'))
    with pytest.raises(PolicyViolation, match='still live'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('boundary', ['kernel', 'windows', 'process'])
def test_unreadable_evidence_retains_record(scene, monkeypatch, boundary):
    def denied(*args):
        raise PermissionError('fixture denied')
    monkeypatch.setattr(recovery, {'kernel': 'kernel_identity', 'windows': 'windows_boot', 'process': 'process_start'}[boundary], denied)
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('target', ['record', 'controller', 'lock', 'processes', 'state', 'archive'])
def test_no_link_path_can_redirect_reconciliation(scene, target):
    path = {'record': scene.record, 'controller': scene.controller, 'lock': scene.state / 'controller.lock',
            'processes': scene.processes, 'state': scene.state, 'archive': scene.state / 'process-reboot-recovery'}[target]
    if target == 'lock':
        path.write_text('')
    if target == 'archive':
        path.mkdir()
    saved = path.with_name(path.name + '-original')
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=saved.is_dir())
    with pytest.raises(PolicyViolation):
        apply(scene)
    actual = saved if target == 'record' else scene.record
    assert actual.read_bytes() == scene.original


def test_controller_lock_prevents_reconciliation(scene):
    with (scene.state / 'controller.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BusyProject):
            apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('failure', ['write', 'fsync', 'conflict'])
def test_archive_failure_preserves_active_record(scene, monkeypatch, failure):
    if failure == 'conflict':
        archive = scene.state / 'process-reboot-recovery'
        archive.mkdir()
        (archive / (scene.expected + '.json')).write_text('{"version":1,"record_bytes":"conflict"}')
    else:
        def failed(*args, **kwargs):
            raise OSError('fixture archive failure')
        monkeypatch.setattr(recovery, 'save_receipt' if failure == 'write' else 'os',
                            failed if failure == 'write' else SimpleNamespace(**{**vars(os), 'fsync': failed}))
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('change', ['record', 'boot', 'events', 'controller', 'directory'])
def test_final_recheck_rejects_changes_after_durable_archive(scene, monkeypatch, change):
    save = recovery.save_receipt
    def changed(*args):
        save(*args)
        if change == 'record':
            scene.record.write_text(scene.record.read_text() + ' ')
        elif change == 'boot':
            scene.kernel['boot_id'] = 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb'
        elif change == 'events':
            with closing(sqlite3.connect(scene.database)) as db, db:
                db.execute('UPDATE events SET time=time+1 WHERE seq=11')
        elif change == 'controller':
            scene.controller.write_text(scene.controller.read_text() + ' ')
        else:
            scene.processes.rename(scene.state / 'old-processes')
            scene.processes.mkdir()
            scene.record.write_bytes(b'unrelated new record')
    monkeypatch.setattr(recovery, 'save_receipt', changed)
    with pytest.raises(PolicyViolation):
        apply(scene)
    if change == 'directory':
        assert scene.record.read_bytes() == b'unrelated new record'
        assert (scene.state / 'old-processes' / scene.record.name).read_bytes() == scene.original
    else:
        assert scene.record.exists()
    archive = json.loads(next((scene.state / 'process-reboot-recovery').glob('*.json')).read_text())
    assert base64.b64decode(archive['record_bytes']) == scene.original


def test_interruption_after_receipt_before_unlink_can_retry_without_overwriting(scene, monkeypatch):
    unlink = recovery.os.unlink
    def interrupted(name, *args, **kwargs):
        if name == scene.record.name:
            raise OSError('fixture pre-unlink interruption')
        return unlink(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, 'unlink', interrupted)
        with pytest.raises(PolicyViolation):
            apply(scene)
    assert scene.record.read_bytes() == scene.original
    archive = next((scene.state / 'process-reboot-recovery').glob('*.json'))
    prior = archive.read_bytes()
    apply(scene)
    assert not scene.record.exists() and archive.read_bytes() == prior


def test_post_unlink_fsync_failure_reports_consumed_state_and_retains_original(scene, monkeypatch):
    fsync = recovery.os.fsync
    inode = scene.processes.stat().st_ino
    def failed(fd):
        if os.fstat(fd).st_ino == inode:
            raise OSError('fixture directory sync failure')
        fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, 'fsync', failed)
        with pytest.raises(AA2Error, match='consumed.*uncertain'):
            apply(scene)
    assert not scene.record.exists()
    archive = json.loads(next((scene.state / 'process-reboot-recovery').glob('*.json')).read_text())
    assert base64.b64decode(archive['record_bytes']) == scene.original
    assert apply(scene)['already_absent'] is True


def test_cli_requires_explicit_confirmation_and_returns_receipt(scene):
    runner = CliRunner()
    args = ['reconcile-process-reboot', '--project', str(scene.project),
            '--request', str(scene.request_path), '--sha256', scene.expected]
    refused = runner.invoke(cli.app, args)
    assert refused.exit_code == 2 and scene.record.exists()
    accepted = runner.invoke(cli.app, [*args, '--confirm-local-host-reboot'])
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.output)['record'] == scene.record.name
    assert not scene.record.exists()


@pytest.mark.parametrize('sidecar', ['state.sqlite-wal', 'state.sqlite-shm', 'state.sqlite-journal'])
def test_database_sidecar_links_are_rejected(scene, sidecar):
    foreign = scene.tmp_path / 'foreign'
    foreign.write_text('retain')
    (scene.state / sidecar).symlink_to(foreign)
    with pytest.raises(PolicyViolation, match='sidecar'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original and foreign.read_text() == 'retain'


@pytest.mark.parametrize('change', ['receipt', 'archive_directory', 'lock'])
def test_archive_and_lock_are_rechecked_at_consumption(scene, monkeypatch, change):
    reads = 0
    def windows():
        nonlocal reads
        reads += 1
        if reads == 2:
            archive = scene.state / 'process-reboot-recovery'
            if change == 'receipt':
                next(archive.glob('*.json')).write_text('corrupted')
            elif change == 'archive_directory':
                archive.rename(scene.state / 'original-archive')
                archive.mkdir()
            else:
                lock = scene.state / 'controller.lock'
                lock.rename(scene.state / 'original-lock')
                lock.touch()
        return copy.deepcopy(scene.observed)
    monkeypatch.setattr(recovery, 'windows_boot', windows)
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('boundary', ['archive', 'state', 'receipt'])
def test_replayed_receipt_is_synced_before_record_consumption(scene, monkeypatch, boundary):
    unlink = recovery.os.unlink
    def interrupted(name, *args, **kwargs):
        if name == scene.record.name:
            raise OSError('interrupt after proof persistence')
        return unlink(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, 'unlink', interrupted)
        with pytest.raises(PolicyViolation):
            apply(scene)
    archive = scene.state / 'process-reboot-recovery'
    target = {'archive': archive, 'state': scene.state, 'receipt': next(archive.glob('*.json'))}[boundary]
    identity = (target.stat().st_dev, target.stat().st_ino)
    fsync = recovery.os.fsync
    def failed(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            raise OSError('replay durability failure')
        fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, 'fsync', failed)
        with pytest.raises(PolicyViolation):
            apply(scene)
    assert scene.record.read_bytes() == scene.original
    assert apply(scene)['already_absent'] is False


def test_last_identity_inspection_cannot_hide_record_drift(scene, monkeypatch):
    calls = 0
    def inspect(pid):
        nonlocal calls
        calls += 1
        if calls == 6:
            scene.record.write_text(scene.record.read_text() + ' ')
        return ''
    monkeypatch.setattr(recovery, 'process_start', inspect)
    with pytest.raises(PolicyViolation, match='record changed'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original + b' '


def test_live_wal_evidence_is_read_without_business_state_writes(scene):
    with closing(sqlite3.connect(scene.database)) as writer, writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('UPDATE events SET data=? WHERE seq=10',
                       (json.dumps({**scene.events[0]['data'], 'detail': 'committed only in WAL'}),))
        writer.commit()
        scene.events[0]['data']['detail'] = 'committed only in WAL'
        scene.request['events_sha256'] = digest(scene.events)
        scene.expected = scene.save_request()
        before = {name: (scene.state / name).read_bytes() for name in ('state.sqlite', 'state.sqlite-wal')}
        apply(scene)
        assert {name: (scene.state / name).read_bytes() for name in before} == before
        assert writer.execute('SELECT data FROM checkpoints').fetchone()[0] == '{"pending":true,"failures":5,"grant":12}'


def test_atomic_receipt_publication_can_resume_after_directory_sync_interruption(scene, monkeypatch):
    fsync = recovery.os.fsync
    failed = False
    def interrupted(fd):
        nonlocal failed
        archive = scene.state / 'process-reboot-recovery'
        if archive.exists() and os.fstat(fd).st_ino == archive.stat().st_ino and not failed:
            failed = True
            raise OSError('interrupted after atomic receipt publication')
        fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, 'fsync', interrupted)
        with pytest.raises(PolicyViolation):
            apply(scene)
    receipt = next((scene.state / 'process-reboot-recovery').glob('*.json'))
    assert receipt.stat().st_nlink == 1
    before = receipt.read_bytes()
    assert scene.record.read_bytes() == scene.original
    apply(scene)
    assert receipt.read_bytes() == before and not scene.record.exists()
