import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.collection import capture_policy, policy_identity, run_collection
from auto_agents_2.collection_worker import attachments
from auto_agents_2.config import Cleanup
from auto_agents_2.errors import AA2Error, CollectionError
from auto_agents_2.http_body import read_body
from auto_agents_2.models import Command
from auto_agents_2.requirements import capture
from auto_agents_2.resolution import load_resolution, save_resolution
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-32', 'utf-8-sig'])
async def test_local_document_bom_is_decoded_in_owned_worker(tmp_path, encoding):
    source = tmp_path / 'requirements.txt'
    source.write_bytes('需求：保留现场'.encode(encoding))
    store = Store(tmp_path)
    artifacts = ArtifactStore(store.root)
    result = await capture(str(source), artifacts)
    assert artifacts.read(result['artifact']) == '需求：保留现场'
    assert not list((store.root / 'processes').glob('*.json'))


@pytest.mark.parametrize('kind', ['fifo', 'symlink', 'invalid-encoding'])
async def test_local_special_inputs_fail_promptly_and_leave_no_worker(tmp_path, kind):
    source = tmp_path / 'requirements.txt'
    if kind == 'fifo':
        os.mkfifo(source)
    elif kind == 'symlink':
        target = tmp_path / 'outside'
        target.write_text('must not read')
        source.symlink_to(target)
    else:
        source.write_bytes(b'\xff\xfe\xff')
    store = Store(tmp_path)
    async with asyncio.timeout(5):
        with pytest.raises(AA2Error):
            await capture(str(source), ArtifactStore(store.root))
    assert not list((store.root / 'processes').glob('*.json'))


async def test_raw_deflate_with_zlib_looking_header_retries_from_raw_bytes():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'\x78\x9c\x00\x63\xff' + b'A' * 156 + b'\x01\x00\x00\xff\xff'
    response = httpx.Response(200, headers={'content-encoding': 'deflate'}, stream=Stream())
    assert await read_body(response) == b'A' * 156


def test_atomic_resolution_never_replays_partial_legacy_or_uncommitted_generation(tmp_path, monkeypatch):
    import auto_agents_2.resolution as module
    (tmp_path / 'pip.lock').write_text('stale==1\n')
    assert load_resolution(tmp_path) is None
    original = module.write_json
    def fail(*args):
        raise OSError('publish fault')
    monkeypatch.setattr(module, 'write_json', fail)
    with pytest.raises(OSError):
        save_resolution(tmp_path, 'fresh==2\n', '@EXPLICIT\npackage\n')
    assert load_resolution(tmp_path) is None
    monkeypatch.setattr(module, 'write_json', original)
    saved = save_resolution(tmp_path, 'fresh==2\n', '@EXPLICIT\npackage\n')
    assert load_resolution(tmp_path) == saved
    assert (tmp_path / 'pip.lock').read_text() == 'stale==1\n'
    data = json.loads((tmp_path / 'resolution.json').read_text())
    data['pip'] = 'tampered==3'
    write_json(tmp_path / 'resolution.json', data)
    with pytest.raises(AA2Error, match='damaged'):
        load_resolution(tmp_path)


@pytest.mark.parametrize('dimension', ['file', 'total', 'count'])
def test_attachment_budgets_reject_before_unbounded_archive(tmp_path, dimension):
    store = Store(tmp_path)
    folder = tmp_path / 'attachments'
    folder.mkdir()
    policy = {'max_file_bytes': 8, 'max_total_bytes': 12, 'max_files': 2}
    if dimension == 'file':
        with (folder / 'huge').open('wb') as file:
            file.truncate(8 * 1024**3)
    else:
        for index in range(2 if dimension == 'total' else 3):
            (folder / str(index)).write_bytes(b'a' * (7 if dimension == 'total' else 1))
    with pytest.raises(CollectionError) as caught:
        attachments(ArtifactStore(store.root), folder, policy)
    assert caught.value.details['reason'] == {'file': 'max_file_bytes', 'total': 'max_total_bytes', 'count': 'max_files'}[dimension]
    if dimension == 'file':
        assert caught.value.details['actual_file_bytes'] == 8 * 1024**3
        assert not list((store.root / 'artifacts').glob('*/*'))


async def test_collection_worker_deadline_keeps_event_loop_live_and_joins_process(tmp_path):
    store = Store(tmp_path)
    folder = tmp_path / 'attachments'
    folder.mkdir()
    ticks = []
    async def heartbeat():
        while True:
            ticks.append(True)
            await asyncio.sleep(.001)
    task = asyncio.create_task(heartbeat())
    try:
        with pytest.raises(CollectionError, match='deadline'):
            await run_collection(store.root, {'action': 'attachments', 'path': str(folder),
                'policy': capture_policy(store.root)}, timeout=.0001, paths=[folder])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert ticks and not list((store.root / 'processes').glob('*.json'))


async def test_failed_attachment_capture_keeps_scratch_and_runs_independent_check(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit('attachments', {})['id']
    body = config.load().model_dump(mode='json')
    body['attachment_capture'] = {'max_file_bytes': 8, 'max_total_bytes': 12, 'max_files': 2}
    write_json(config.path, body)
    runner = tmp_path / 'playwright'
    runner.write_text(f'#!{sys.executable}\n' + '''import os,pathlib
p=pathlib.Path(os.environ['PLAYWRIGHT_JUNIT_OUTPUT_FILE']); p.write_text('<testsuite><testcase name="ok"/></testsuite>')
d=p.parent/'playwright-results'; d.mkdir(); (d/'video.webm').write_bytes(b'x'*9)
''')
    runner.chmod(0o755)
    service = VerificationService(store.root, SandboxRunner(store.root, _test_unconfined=True))
    checks = [Command(argv=[str(runner), 'test'], expected_tests=1), Command(argv=[sys.executable, '-c', 'print("independent")'])]
    results = await service.run(checks, tmp_path)
    first, second = results
    assert first.failure_reasons == ['evidence_collection_failed'] and first.tests == 1
    assert first.capture_policy_hash == policy_identity(store.root)
    assert first.attachment_capture['actual_file_bytes'] == 9
    assert (Path(first.attachment_capture['scratch']) / 'playwright-results/video.webm').read_bytes() == b'x'*9
    assert second.exit_code == 0 and 'independent' in second.output
    assert store.get(rid, 'evidence/capture/' + Path(first.attachment_capture['scratch']).name)
    body['attachment_capture']['max_file_bytes'] = 16
    write_json(config.path, body)
    replacement = (await service.run(checks[:1], tmp_path))[0]
    assert replacement.exit_code == 0
    assert not Path(first.attachment_capture['scratch']).exists()


def test_optional_attachment_retirement_respects_open_issues_and_mixed_roles(tmp_path):
    store = Store(tmp_path)
    rid = store.admit('test', {})['id']
    artifacts = ArtifactStore(store.root)
    diagnostic = artifacts.put(b'diagnostic', 'test_diagnostic')
    legacy = artifacts.put(b'legacy', 'test_attachment')
    mixed = artifacts.put(b'mixed', 'test_diagnostic')
    required = artifacts.put(b'mixed', 'screenshot')
    store.checkpoint(rid, 'verify/checks', [diagnostic, legacy, mixed, required])
    store.checkpoint(rid, 'issues', {'failure': {'status': 'open', 'evidence': diagnostic}})
    resources = ResourceManager(store, Cleanup())
    resources.collect_artifacts()
    assert artifacts.bytes(diagnostic)
    store.checkpoint(rid, 'issues', {'failure': {'status': 'resolved', 'evidence': diagnostic}})
    resources.collect_artifacts()
    assert artifacts.released(diagnostic)
    assert artifacts.bytes(legacy) and artifacts.bytes(required)


def test_structured_result_keeps_core_but_releases_consumed_diagnostic(tmp_path):
    store = Store(tmp_path)
    rid = store.admit('structured', {})['id']
    artifacts = ArtifactStore(store.root)
    diagnostic = artifacts.put(b'video fixture', 'test_diagnostic')
    report = artifacts.put('<testsuite/>', 'junit')
    result = artifacts.put({'attachments': [{'artifact': diagnostic}], 'report': report}, 'check_result')
    store.checkpoint(rid, 'verify/checks', result)
    ResourceManager(store, Cleanup()).collect_artifacts()
    assert artifacts.bytes(result) and artifacts.bytes(report) and artifacts.released(diagnostic)


def test_completed_legacy_resolution_cannot_silently_resolve_missing_pair(tmp_path):
    (tmp_path / 'pip.lock').write_text('dep==1')
    write_json(tmp_path / 'instance.json', {'prefix': '/old/prefix'})
    with pytest.raises(AA2Error, match='damaged'):
        load_resolution(tmp_path)


def test_bounded_regular_read_checks_bytes_even_when_metadata_is_stale(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace

    import auto_agents_2.safe_files as safe
    source = tmp_path / 'document.txt'
    source.write_bytes(b'x' * 17)
    monkeypatch.setattr(safe.os, 'fstat', lambda fd: SimpleNamespace(st_size=1, st_mode=stat.S_IFREG | 0o600))
    with pytest.raises(ValueError, match='size'):
        safe.read_regular(source, 16)


def test_stream_growth_cannot_escape_attachment_budget(tmp_path):
    import io
    artifacts = ArtifactStore(Store(tmp_path).root)
    with pytest.raises(ValueError, match='budget'):
        artifacts.put_stream(io.BytesIO(b'x' * 17), 'test_diagnostic', max_bytes=16)
    assert not list(artifacts.root.glob('*/*'))
