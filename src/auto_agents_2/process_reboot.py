"""Explicit, single-record WSL recovery after an independently confirmed host reboot.

This is an operator entrypoint, never a resume heuristic or supervisor receipt.
"""
from __future__ import annotations

import base64
import ctypes
import fcntl
import json
import math
import os
import platform
import re
import sqlite3
import stat
import subprocess
import uuid
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from .errors import AA2Error, BusyProject, PolicyViolation
from .process_records import require_identity, valid_path
from .sandbox import process_start
from .util import digest

LIMIT = 256 * 1024
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
POWERSHELL = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
EVENT_NS = '{http://schemas.microsoft.com/win/2004/08/events/event}'
PROVIDER = 'Microsoft-Windows-Kernel-General'
PROVIDER_GUID = '{a68ca8b7-004f-d7b6-a698-07e2de0f1f5d}'
REQUEST_FIELDS = {
    'version', 'project', 'record', 'record_sha256', 'run_id', 'attempt_id',
    'started_event_seq', 'spawned_event_seq', 'events_sha256',
    'controller_record', 'controller_sha256', 'boot_id',
    'windows_computer', 'windows_event_record_id',
}


def reject(message):
    raise PolicyViolation('Host reboot reconciliation: ' + message)


def stamp(value):
    if not isinstance(value, str):
        reject('invalid UTC timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except ValueError:
        reject('invalid UTC timestamp')


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def directory(path):
    """Pin every path component without following a link."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        reject('absolute local paths without parent traversal are required')
    fd = os.open('/', DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def owned_file(folder, name, *, sync=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size > LIMIT):
            reject('evidence must be a bounded, owned, single-link regular file')
        raw = stream.read(LIMIT + 1)
        if sync:
            os.fsync(stream.fileno())
        if len(raw) > LIMIT or signature(before) != signature(os.fstat(stream.fileno())):
            reject('evidence changed while being read')
    return raw, signature(before)


def object_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        reject('JSON evidence must be an object')
    return value


def kernel_identity():
    release = platform.release()
    distro = os.environ.get('WSL_DISTRO_NAME', '')
    interop = os.environ.get('WSL_INTEROP', '')
    if (platform.system() != 'Linux' or 'microsoft' not in release.lower()
            or not distro or not re.fullmatch(r'/run/WSL/[0-9]+_interop', interop)
            or not stat.S_ISSOCK(os.stat(interop, follow_symlinks=False).st_mode)):
        reject('requires local WSL with its Windows interoperability socket')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if str(uuid.UUID(boot)) != boot:
        reject('invalid current Linux boot identity')
    return {'boot_id': boot, 'release': release, 'distribution': distro}


def windows_boot():
    # Fixed local executable and fixed script. No supplied command, XML file,
    # remote computer or localized event message can provide reboot authority.
    script = r"""
$ErrorActionPreference = 'Stop'
# WSL can inherit a PowerShell 7 module path from its caller. Use only the
# fixed Windows PowerShell executable's own modules, within this process.
$env:PSModulePath = $PSHOME + '\Modules'
Import-Module ($PSHOME + '\Modules\CimCmdlets\CimCmdlets.psd1') -ErrorAction Stop
Import-Module ($PSHOME + '\Modules\Microsoft.PowerShell.Diagnostics\Microsoft.PowerShell.Diagnostics.psd1') -ErrorAction Stop
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$rebootOs = Get-CimInstance -ClassName Win32_OperatingSystem
$rebootEvent = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=12} -MaxEvents 1
$rebootDistros = @(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss\*' | ForEach-Object { $_.DistributionName })
[pscustomobject]@{computer=$rebootOs.CSName; last_boot_utc=$rebootOs.LastBootUpTime.ToUniversalTime().ToString('o'); event_xml=$rebootEvent.ToXml(); distributions=$rebootDistros} | ConvertTo-Json -Depth 4 -Compress
"""
    result = subprocess.run(
        [POWERSHELL, '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
         '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
        capture_output=True, timeout=30, check=True)
    if len(result.stdout) > LIMIT:
        reject('Windows boot observation is oversized')
    return object_json(result.stdout.decode('utf-8-sig'))


def validate_boot(observed, request, kernel):
    if (set(observed) != {'computer', 'last_boot_utc', 'event_xml', 'distributions'}
            or observed['computer'] != request['windows_computer']
            or not isinstance(observed['distributions'], list)
            or kernel['distribution'] not in observed['distributions']):
        reject('Windows host or installed WSL distribution does not match')
    root = ElementTree.fromstring(observed['event_xml'])
    if root.tag != EVENT_NS + 'Event':
        reject('invalid Windows event envelope')
    def one(path):
        matches = root.findall('/'.join(EVENT_NS + part for part in path.split('/')))
        if len(matches) != 1:
            reject('missing or ambiguous Windows event field')
        return matches[0]
    provider = one('System/Provider')
    if (provider.attrib != {'Name': PROVIDER, 'Guid': PROVIDER_GUID}
            or one('System/EventID').text != '12'
            or one('System/Channel').text != 'System'
            or one('System/Computer').text != request['windows_computer']
            or one('System/EventRecordID').text != str(request['windows_event_record_id'])):
        reject('latest Windows startup event does not match the reviewed identity')
    fields = {}
    for item in one('EventData'):
        name = item.get('Name')
        if item.tag != EVENT_NS + 'Data' or not name or name in fields:
            reject('invalid Windows startup data')
        fields[name] = item.text
    if fields.get('BootMode') != '0':
        reject('unsupported Windows boot mode')
    boot = stamp(fields.get('StartTime'))
    created = stamp(one('System/TimeCreated').get('SystemTime'))
    if boot != stamp(observed['last_boot_utc']) or not boot <= created <= boot + 60:
        reject('Windows event and current OS startup do not agree')
    return boot


def request_data(raw, expected_hash, project):
    if not re.fullmatch('[0-9a-f]{64}', expected_hash) or digest(raw) != expected_hash:
        reject('reviewed request hash changed')
    request = object_json(raw)
    if set(request) != REQUEST_FIELDS or type(request['version']) is not int or request['version'] != 1:
        reject('unsupported request schema')
    if request['project'] != str(project):
        reject('request belongs to a different project')
    for field, pattern in {
        'record': r'process_[0-9a-f]{16}\.json', 'run_id': r'run_[0-9a-f]{16}',
        'attempt_id': r'attempt_[0-9a-f]{16}',
        'record_sha256': '[0-9a-f]{64}', 'events_sha256': '[0-9a-f]{64}',
        'controller_sha256': '[0-9a-f]{64}',
        'boot_id': '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        'windows_computer': r'[A-Za-z0-9][A-Za-z0-9_.-]{0,253}',
    }.items():
        if not isinstance(request[field], str) or not re.fullmatch(pattern, request[field]):
            reject('invalid request identity: ' + field)
    for field in ('started_event_seq', 'spawned_event_seq', 'windows_event_record_id'):
        if type(request[field]) is not int or request[field] <= 0:
            reject('invalid event sequence')
    if not valid_path(request['controller_record']):
        reject('invalid prior controller evidence path')
    return request


def events(state_fd, request):
    # Opening via the pinned directory keeps SQLite's DB/WAL lookup in that
    # directory. No Store construction, migration, run update or permit write.
    def database_identities():
        result = {}
        for name in ('state.sqlite', 'state.sqlite-wal', 'state.sqlite-shm', 'state.sqlite-journal'):
            try:
                info = os.stat(name, dir_fd=state_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name == 'state.sqlite':
                    raise
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                reject('unsafe state database or sidecar')
            result[name] = (info.st_dev, info.st_ino)
        return result
    before = database_identities()
    with closing(sqlite3.connect(f'file:/proc/self/fd/{state_fd}/state.sqlite?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows = [db.execute('SELECT seq,run_id,time,kind,data FROM events WHERE seq=?', (seq,)).fetchone()
                for seq in (request['started_event_seq'], request['spawned_event_seq'])]
        after = database_identities()
        if any(after.get(name) != identity for name, identity in before.items()):
            reject('database files changed while reading provider evidence')
    if any(row is None for row in rows):
        reject('missing original provider events')
    original = [dict(row) for row in rows]
    parsed = [{**row, 'data': object_json(row['data'])} for row in original]
    if digest(parsed) != request['events_sha256']:
        reject('original provider events changed')
    first, second = parsed
    if (first['kind'] != 'agent.started' or second['kind'] != 'agent.spawned'
            or first['seq'] >= second['seq']
            or any(row['run_id'] != request['run_id'] or row['data'].get('attempt') != request['attempt_id']
                   or not isinstance(row['time'], (int, float)) or not math.isfinite(row['time']) for row in parsed)
            or first['time'] > second['time']):
        reject('provider event ownership does not match')
    return original, parsed


def validate_record(raw, metadata, request, project, parsed, controller_raw, controller_metadata, boot):
    if digest(raw) != request['record_sha256'] or digest(controller_raw) != request['controller_sha256']:
        reject('reviewed process/controller bytes changed')
    record = object_json(raw)
    require_identity(record)
    attempt = project / '.aa2' / 'attempts' / request['run_id'] / request['attempt_id']
    paths = record.get('paths')
    if (record.get('version') != 3 or record.get('status') != 'running'
            or record.get('cwd') != str(attempt / 'workspace')
            or not isinstance(paths, list) or not paths or not all(valid_path(path) for path in paths)
            or str(attempt / 'workspace') not in paths or str(attempt / 'out') not in paths
            or parsed[1]['data'].get('pid') != record['pid']):
        reject('process record is not bound to the selected provider attempt')
    controller = object_json(controller_raw)
    argv = controller.get('argv')
    if (not isinstance(argv, list) or not all(isinstance(item, str) for item in argv)
            or argv.count('--project') != 1 or argv.count('--run') != 1
            or argv.count('auto_agents_2') != 1):
        reject('prior controller command is not a bound aa2 resume')
    try:
        if (argv[argv.index('--project') + 1] != str(project)
                or argv[argv.index('--run') + 1] != request['run_id']
                or argv[argv.index('auto_agents_2') - 1] != '-m'
                or argv[argv.index('auto_agents_2') + 1] != 'resume'):
            reject('prior controller belongs to another operation')
    except IndexError:
        reject('incomplete prior controller command')
    if not stamp(controller.get('started_utc')) <= parsed[0]['time'] <= parsed[1]['time'] < boot:
        reject('original controller/attempt did not precede the verified host reboot')
    if any(value / 1_000_000_000 >= boot for value in [*metadata[-2:], *controller_metadata[-2:]]):
        reject('process/controller evidence was changed after the verified reboot')
    identities = [{'pid': record['pid'], 'start': record['start']}]
    for name in ('runner', 'controller'):
        identity = {'pid': controller.get(name + '_pid'), 'start': str(controller.get(name + '_start_ticks', ''))}
        require_identity(identity)
        identities.append(identity)
    for identity in identities:
        if process_start(identity['pid']) == identity['start']:
            reject('a recorded process identity is still live')
    return record


def save_receipt(folder, name, raw):
    """No overwrite: publish a fully fsynced receipt, then fsync its directory."""
    temporary = '.writing-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # RENAME_NOREPLACE publishes atomically without an overwrite or a two-link
    # intermediate state. Unsupported kernels retain the active record.
    if rename(folder, os.fsencode(temporary), folder, os.fsencode(name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    os.fsync(folder)


def reconcile(project, request_path, expected_hash, *, confirm_local_host_reboot=False):
    """Archive verified reboot evidence and consume only its exact process record."""
    if not confirm_local_host_reboot:
        reject('explicit confirmation of this local host reboot is required')
    project, request_path = Path(project).absolute(), Path(request_path).absolute()
    consumed = False
    receipt_path = None
    try:
        with ExitStack() as stack:
            request_fd = stack.enter_context(directory(request_path.parent))
            request_raw, _ = owned_file(request_fd, request_path.name)
            request = request_data(request_raw, expected_hash, project)
            state = project / '.aa2'
            state_fd = stack.enter_context(directory(state))
            if os.fstat(state_fd).st_uid != os.getuid():
                reject('state directory is not owned by the operator')
            lock = os.open('controller.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=state_fd)
            stack.callback(os.close, lock)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                reject('unsafe controller lock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BusyProject('Project controller is active; reboot reconciliation refused') from None
            processes_fd = os.open('processes', DIRECTORY, dir_fd=state_fd)
            stack.callback(os.close, processes_fd)
            if os.fstat(processes_fd).st_uid != os.getuid():
                reject('process directory is not owned by the operator')
            controller_path = Path(request['controller_record'])
            controller_fd = stack.enter_context(directory(controller_path.parent))
            controller_raw, controller_metadata = owned_file(controller_fd, controller_path.name)
            kernel = kernel_identity()
            if kernel['boot_id'] != request['boot_id']:
                reject('current Linux boot changed')
            observed = windows_boot()
            boot = validate_boot(observed, request, kernel)
            original_events, parsed = events(state_fd, request)
            archive_name = 'process-reboot-recovery'
            try:
                os.mkdir(archive_name, mode=0o700, dir_fd=state_fd)
                os.fsync(state_fd)
            except FileExistsError:
                pass
            archive_fd = os.open(archive_name, DIRECTORY, dir_fd=state_fd)
            stack.callback(os.close, archive_fd)
            if os.fstat(archive_fd).st_uid != os.getuid():
                reject('reboot archive is not owned by the operator')
            receipt_name = expected_hash + '.json'
            receipt_path = state / archive_name / receipt_name
            try:
                previous_raw, _ = owned_file(archive_fd, receipt_name)
                previous = object_json(previous_raw)
            except FileNotFoundError:
                previous = None
            try:
                raw, metadata = owned_file(processes_fd, request['record'])
                absent = False
            except FileNotFoundError:
                if previous is None:
                    reject('process record is absent without an archived reboot proof')
                raw = base64.b64decode(previous['record_bytes'], validate=True)
                metadata = previous['record_metadata']
                absent = True
            validate_record(raw, metadata, request, project, parsed, controller_raw, controller_metadata, boot)
            receipt = {
                'version': 1, 'kind': 'verified_wsl_host_reboot',
                'request_sha256': expected_hash, 'request_bytes': base64.b64encode(request_raw).decode('ascii'),
                'record_sha256': digest(raw), 'record_bytes': base64.b64encode(raw).decode('ascii'),
                'record_metadata': metadata,
                'controller_sha256': digest(controller_raw),
                'controller_bytes': base64.b64encode(controller_raw).decode('ascii'),
                'controller_metadata': controller_metadata,
                'events': original_events, 'kernel': kernel, 'windows': observed,
                'windows_xml_sha256': digest(observed['event_xml']),
                'termination_basis': 'Operator-confirmed local host reboot, independently observed from Windows',
            }
            receipt_raw = (json.dumps(receipt, ensure_ascii=True, indent=2) + '\n').encode()
            if previous is not None:
                if previous_raw != receipt_raw:
                    reject('existing reboot archive conflicts; preserve both the record and evidence')
            else:
                save_receipt(archive_fd, receipt_name, receipt_raw)
            # A retry may encounter a complete receipt whose prior directory
            # fsync failed. Re-establish durability even for an exact replay.
            persisted, receipt_metadata = owned_file(archive_fd, receipt_name, sync=True)
            if persisted != receipt_raw:
                reject('reboot receipt changed during persistence')
            os.fsync(archive_fd)
            os.fsync(state_fd)
            # Re-observe all authority and pinned directory identities after the
            # durable archive, before the only destructive action.
            if (kernel_identity() != kernel or windows_boot() != observed
                    or events(state_fd, request)[0] != original_events
                    or owned_file(controller_fd, controller_path.name) != (controller_raw, controller_metadata)):
                reject('reboot or controller evidence changed before record consumption')
            with directory(state) as current_state:
                if os.fstat(current_state).st_ino != os.fstat(state_fd).st_ino or os.fstat(current_state).st_dev != os.fstat(state_fd).st_dev:
                    reject('project state directory changed')
            validate_record(raw, metadata, request, project, parsed, controller_raw, controller_metadata, boot)
            if kernel_identity() != kernel:
                reject('current Linux boot changed before consumption')
            current_archive = os.stat(archive_name, dir_fd=state_fd, follow_symlinks=False)
            if ((current_archive.st_dev, current_archive.st_ino) != (os.fstat(archive_fd).st_dev, os.fstat(archive_fd).st_ino)
                    or owned_file(archive_fd, receipt_name) != (receipt_raw, receipt_metadata)):
                reject('reboot archive changed before record consumption')
            current_lock = os.stat('controller.lock', dir_fd=state_fd, follow_symlinks=False)
            if (current_lock.st_dev, current_lock.st_ino) != (info.st_dev, info.st_ino):
                reject('controller lock changed before record consumption')
            current_processes = os.stat('processes', dir_fd=state_fd, follow_symlinks=False)
            if (current_processes.st_dev, current_processes.st_ino) != (os.fstat(processes_fd).st_dev, os.fstat(processes_fd).st_ino):
                reject('process directory changed')
            if absent:
                try:
                    os.stat(request['record'], dir_fd=processes_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return {'receipt': str(receipt_path), 'record': request['record'], 'already_absent': True}
                reject('process record reappeared during reconciliation')
            if owned_file(processes_fd, request['record']) != (raw, metadata):
                reject('process record changed before consumption')
            os.unlink(request['record'], dir_fd=processes_fd)
            consumed = True
            os.fsync(processes_fd)
            return {'receipt': str(receipt_path), 'record': request['record'], 'already_absent': False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, ElementTree.ParseError, subprocess.SubprocessError) as exc:
        if consumed:
            raise AA2Error('Process record was consumed, but directory durability is uncertain; '
                           f'original bytes and proof remain at {receipt_path}. Inspect before retrying.') from exc
        raise PolicyViolation('Host reboot reconciliation failed; process recovery record retained: '
                              + type(exc).__name__) from exc
