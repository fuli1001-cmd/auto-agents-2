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


@pytest.fixture(params=["ticks", "legacy"])
def scene(tmp_path, monkeypatch, request):
    launcher_shape = request.param
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
    launcher = {
        'started_utc': utc(boot - 1000), 'runner_pid': 12343,
        'controller_pid': 12344,
        'argv': ['/fixture/python', '-B', '-m', 'auto_agents_2', 'resume', '--project', str(project), '--run', rid],
    }
    if launcher_shape == 'ticks':
        launcher.update(runner_start_ticks=800000, controller_start_ticks=800001)
    controller.write_text(json.dumps(launcher))
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



def revise_launcher(scene, change):
    launcher = json.loads(scene.controller.read_text())
    change(launcher)
    scene.controller.write_text(json.dumps(launcher))
    scene.request['controller_sha256'] = digest(scene.controller.read_bytes())
    scene.expected = scene.save_request()


@pytest.mark.parametrize('name', ['runner', 'controller'])
@pytest.mark.parametrize('pid', [None, True, False, 0, -1, '12343', 12343.0])
def test_invalid_launcher_pid_never_uses_legacy_fallback(scene, name, pid):
    revise_launcher(scene, lambda launcher: launcher.update({name + '_pid': pid}))
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('name', ['runner', 'controller'])
@pytest.mark.parametrize('damage', ['missing', None, False, '', 'invalid', 1.5, [], {}])
def test_partial_or_malformed_ticks_never_use_legacy_fallback(scene, name, damage):
    def corrupt(launcher):
        launcher.update(runner_start_ticks=800000, controller_start_ticks=800001)
        if damage == 'missing':
            del launcher[name + '_start_ticks']
        else:
            launcher[name + '_start_ticks'] = damage
    revise_launcher(scene, corrupt)
    with pytest.raises(PolicyViolation):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('pid', [12343, 12344])
def test_reused_launcher_pid_requires_original_ticks(scene, monkeypatch, pid):
    monkeypatch.setattr(recovery, 'process_start', lambda current: '999999' if current == pid else '')
    if scene.launcher_shape == 'legacy':
        with pytest.raises(PolicyViolation, match='still live'):
            apply(scene)
        assert scene.record.read_bytes() == scene.original
    else:
        assert apply(scene)['already_absent'] is False


@pytest.mark.parametrize('pid', [12343, 12344])
def test_unreadable_launcher_identity_retains_record(scene, monkeypatch, pid):
    def inspect(current):
        if current == pid:
            raise PolicyViolation('Process identity cannot be inspected; preserving recovery state')
        return ''
    monkeypatch.setattr(recovery, 'process_start', inspect)
    with pytest.raises(PolicyViolation, match='cannot be inspected'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original


@pytest.mark.parametrize('pid,start', [(12343, '800000'), (12344, '800001')])
def test_launcher_becoming_live_after_archive_retains_record(scene, monkeypatch, pid, start):
    save = recovery.save_receipt
    def published(*args):
        save(*args)
        monkeypatch.setattr(recovery, 'process_start', lambda current: start if current == pid else '')
    monkeypatch.setattr(recovery, 'save_receipt', published)
    with pytest.raises(PolicyViolation, match='still live'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original
    receipt = json.loads(next((scene.state / 'process-reboot-recovery').glob('*.json')).read_text())
    assert base64.b64decode(receipt['record_bytes']) == scene.original
    assert base64.b64decode(receipt['controller_bytes']) == scene.controller.read_bytes()


def test_preexisting_version_one_receipt_replays_without_schema_migration(scene):
    # Independently encode the existing v1 format, rather than using a receipt
    # just emitted by the implementation under test as its compatibility oracle.
    with closing(sqlite3.connect(scene.database)) as db:
        db.row_factory = sqlite3.Row
        original_events = [dict(row) for row in db.execute(
            'SELECT seq,run_id,time,kind,data FROM events ORDER BY seq')]
    def metadata(path):
        info = path.stat()
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    def encoded(raw):
        return base64.b64encode(raw).decode('ascii')
    receipt = {
        'version': 1, 'kind': 'verified_wsl_host_reboot',
        'request_sha256': scene.expected, 'request_bytes': encoded(scene.request_path.read_bytes()),
        'record_sha256': digest(scene.original), 'record_bytes': encoded(scene.original),
        'record_metadata': metadata(scene.record),
        'controller_sha256': digest(scene.controller.read_bytes()),
        'controller_bytes': encoded(scene.controller.read_bytes()),
        'controller_metadata': metadata(scene.controller),
        'events': original_events, 'kernel': scene.kernel, 'windows': scene.observed,
        'windows_xml_sha256': digest(scene.observed['event_xml']),
        'termination_basis': 'Operator-confirmed local host reboot, independently observed from Windows',
    }
    archive = scene.state / 'process-reboot-recovery'
    archive.mkdir()
    archived = archive / (scene.expected + '.json')
    prior = (json.dumps(receipt, ensure_ascii=True, indent=2) + '\n').encode()
    archived.write_bytes(prior)
    scene.record.unlink()
    assert apply(scene)['already_absent'] is True
    assert archived.read_bytes() == prior



def large_leaf_record(scene):
    document = json.loads(scene.original)
    leaf = str(scene.attempt / 'artifacts' / ('a' * 64))
    base = json.dumps(document).encode()
    count = (812319 - len(base)) // (len(leaf) + 11) + 1
    document['paths'].extend(f'{leaf}-{index:06d}' for index in range(count))
    raw = json.dumps(document).encode()
    assert 812319 <= len(raw) <= recovery.PROCESS_RECORD_LIMIT
    scene.record.write_bytes(raw)
    scene.original = raw
    scene.request['record_sha256'] = digest(raw)
    scene.expected = scene.save_request()
    return raw


def test_realistic_large_leaf_pin_record_archives_rechecks_and_replays(scene):
    raw = large_leaf_record(scene)
    before_database = scene.database.read_bytes()
    result = apply(scene)
    receipt = Path(result['receipt'])
    archived = receipt.read_bytes()
    assert recovery.LIMIT < len(archived) <= recovery.RECEIPT_LIMIT
    assert base64.b64decode(json.loads(archived)['record_bytes']) == raw
    assert not scene.record.exists()
    assert apply(scene)['already_absent'] is True
    assert receipt.read_bytes() == archived
    assert scene.database.read_bytes() == before_database


def test_large_record_drift_at_final_identity_check_is_retained(scene, monkeypatch):
    raw = large_leaf_record(scene)
    calls = 0
    def inspect(pid):
        nonlocal calls
        calls += 1
        if calls == 6:
            scene.record.write_bytes(raw + b' ')
        return ''
    monkeypatch.setattr(recovery, 'process_start', inspect)
    with pytest.raises(PolicyViolation, match='record changed'):
        apply(scene)
    assert scene.record.read_bytes() == raw + b' '


@pytest.mark.parametrize('limit', [recovery.LIMIT, recovery.PROCESS_RECORD_LIMIT, recovery.RECEIPT_LIMIT])
def test_evidence_bounds_accept_exact_size_and_reject_one_more(tmp_path, limit):
    path = tmp_path / 'evidence'
    path.write_bytes(b'x' * limit)
    with recovery.directory(tmp_path) as folder:
        assert recovery.owned_file(folder, path.name, limit=limit)[0] == b'x' * limit
        with path.open('ab') as stream:
            stream.write(b'x')
        with pytest.raises(PolicyViolation, match='bounded'):
            recovery.owned_file(folder, path.name, limit=limit)


@pytest.mark.parametrize('limit', [recovery.LIMIT, recovery.PROCESS_RECORD_LIMIT, recovery.RECEIPT_LIMIT])
def test_growing_evidence_during_bounded_read_is_rejected(tmp_path, monkeypatch, limit):
    path = tmp_path / 'evidence'
    path.write_bytes(b'x' * limit)
    identity = path.stat().st_ino
    original = recovery.os.fstat
    changed = False
    def inspect(fd):
        nonlocal changed
        info = original(fd)
        if info.st_ino == identity and not changed:
            changed = True
            with path.open('ab') as stream:
                stream.write(b'x')
        return info
    with recovery.directory(tmp_path) as folder:
        monkeypatch.setattr(recovery.os, 'fstat', inspect)
        with pytest.raises(PolicyViolation, match='changed'):
            recovery.owned_file(folder, path.name, limit=limit)


@pytest.mark.parametrize('damage', ['hardlink', 'symlink', 'owner'])
def test_large_record_limit_does_not_relax_file_ownership(tmp_path, monkeypatch, damage):
    path = tmp_path / 'evidence'
    path.write_bytes(b'x' * (recovery.LIMIT + 1))
    if damage == 'hardlink':
        os.link(path, tmp_path / 'other-link')
    elif damage == 'symlink':
        original = tmp_path / 'original'
        path.rename(original)
        path.symlink_to(original)
    else:
        original = recovery.os.fstat
        identity = path.stat().st_ino
        def other_owner(fd):
            info = original(fd)
            if info.st_ino == identity:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                                       st_nlink=info.st_nlink, st_size=info.st_size)
            return info
        monkeypatch.setattr(recovery.os, 'fstat', other_owner)
    with recovery.directory(tmp_path) as folder:
        with pytest.raises((OSError, PolicyViolation)):
            recovery.owned_file(folder, path.name, limit=recovery.PROCESS_RECORD_LIMIT)
    assert (tmp_path / 'original' if damage == 'symlink' else path).read_bytes() == b'x' * (recovery.LIMIT + 1)


def test_base64_amplification_is_rejected_before_receipt_publication(scene, monkeypatch):
    raw = large_leaf_record(scene)
    # An individually admissible input must not publish an unreadable envelope.
    envelope = len(raw) + 100
    assert envelope < len(base64.b64encode(raw))
    monkeypatch.setattr(recovery, 'RECEIPT_LIMIT', envelope)
    monkeypatch.setattr(recovery, 'save_receipt', lambda *args: pytest.fail('oversized receipt must not be published'))
    with pytest.raises(PolicyViolation, match='archive exceeds'):
        apply(scene)
    assert scene.record.read_bytes() == raw
    assert not list((scene.state / 'process-reboot-recovery').iterdir())


def test_absent_record_replay_enforces_decoded_record_bound(scene):
    archive = scene.state / 'process-reboot-recovery'
    archive.mkdir()
    path = archive / (scene.expected + '.json')
    value = {'record_bytes': base64.b64encode(b'x' * (recovery.PROCESS_RECORD_LIMIT + 1)).decode(),
             'record_metadata': recovery.signature(scene.record.stat())}
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert len(before) < recovery.RECEIPT_LIMIT
    scene.record.unlink()
    with pytest.raises(PolicyViolation, match='record size bound'):
        apply(scene)
    assert path.read_bytes() == before and not scene.record.exists()


def test_provider_event_limit_counts_utf8_bytes_before_materialization(scene):
    row = scene.events[0]
    row['data']['padding'] = '\u00e9' * (recovery.LIMIT // 2)
    raw = json.dumps(row['data'], ensure_ascii=False)
    assert len(raw) < recovery.LIMIT < len(raw.encode())
    with closing(sqlite3.connect(scene.database)) as db, db:
        db.execute('UPDATE events SET data=? WHERE seq=10', (raw,))
    scene.request['events_sha256'] = digest(scene.events)
    scene.expected = scene.save_request()
    with pytest.raises(PolicyViolation, match='provider event exceeds'):
        apply(scene)
    assert scene.record.read_bytes() == scene.original



@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
@pytest.mark.parametrize('oversized', [False, True])
def test_original_event_utf8_bound_survives_sqlite_text_encodings(scene, encoding, oversized):
    with closing(sqlite3.connect(scene.database)) as db:
        script = '\n'.join(db.iterdump())
    scene.database.unlink()
    row = scene.events[0]
    row['data']['padding'] = '\u6c49' * (recovery.LIMIT // 2) if oversized else 'x' * (200 * 1024)
    raw = json.dumps(row['data'], ensure_ascii=False)
    with closing(sqlite3.connect(scene.database)) as db, db:
        db.execute(f"PRAGMA encoding='{encoding}'")
        db.executescript(script)
        db.execute('UPDATE events SET data=? WHERE seq=10', (raw,))
        stored = db.execute('SELECT length(CAST(data AS BLOB)) FROM events WHERE seq=10').fetchone()[0]
        assert recovery.LIMIT < stored <= 2 * recovery.LIMIT
    scene.request['events_sha256'] = digest(scene.events)
    scene.expected = scene.save_request()
    if oversized:
        assert len(raw.encode()) > recovery.LIMIT
        with pytest.raises(PolicyViolation, match='provider event exceeds'):
            apply(scene)
        assert scene.record.read_bytes() == scene.original
    else:
        assert len(raw.encode()) < recovery.LIMIT
        result = apply(scene)
        archived = Path(result['receipt']).read_bytes()
        assert apply(scene)['already_absent'] is True
        assert Path(result['receipt']).read_bytes() == archived
