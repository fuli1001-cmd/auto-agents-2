import json
import stat
import subprocess
import sys
from collections import namedtuple

import pytest

from auto_agents_2 import storage_space


def fixture_platform(monkeypatch):
    monkeypatch.setattr(storage_space.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(storage_space.platform, 'release', lambda: 'fixture-microsoft-WSL2')
    monkeypatch.setenv('WSL_DISTRO_NAME', 'fixture-distribution')
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(storage_space.shutil, 'disk_usage', lambda path: usage(100, 10, 90))


def test_failed_host_query_keeps_error_and_never_uses_guest_as_host(tmp_path, monkeypatch):
    fixture_platform(monkeypatch)
    calls = []
    state = tmp_path / '.aa2'
    state.mkdir(mode=0o700)
    detail = 'EXAMPLE_PRIVATE fixed path lookup failed'

    def fail(distribution):
        calls.append(distribution)
        raise subprocess.CalledProcessError(41, ['fixed-system-query'], stderr=detail)

    monkeypatch.setattr(storage_space, 'windows_backing_volume', fail)
    report = storage_space.observe(state)
    assert calls == ['fixture-distribution']
    assert [row['scope'] for row in report['volumes']] == ['filesystem']
    assert report['errors'] == [{'scope': 'wsl_backing', 'error_type': 'CalledProcessError'}]
    diagnostic = report['diagnostics'][0]
    assert diagnostic['returncode'] == 41 and diagnostic['retained']
    private = state / 'storage-diagnostics' / diagnostic['diagnostic_id']
    assert private.read_bytes() == detail.encode()
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    assert detail not in json.dumps(report)
    assert storage_space.boundary(report, 0, 'fixture') is not None


@pytest.mark.parametrize('stderr', ['x' * 20000, '\u754c' * 3000, b'\xff' * 10000], ids=['ascii', 'unicode', 'bytes'])
def test_host_error_detail_is_bounded_and_does_not_add_query(tmp_path, monkeypatch, stderr):
    fixture_platform(monkeypatch)
    calls = []
    state = tmp_path / '.aa2'
    state.mkdir(mode=0o700)

    def fail(distribution):
        calls.append(distribution)
        raise subprocess.CalledProcessError(42, ['fixed-system-query'], stderr=stderr)

    monkeypatch.setattr(storage_space, 'windows_backing_volume', fail)
    report = storage_space.observe(state)
    diagnostic = report['diagnostics'][0]
    raw = stderr if isinstance(stderr, bytes) else stderr.encode()
    assert len(calls) == 1 and diagnostic['returncode'] == 42
    private = state / 'storage-diagnostics' / diagnostic['diagnostic_id']
    assert private.read_bytes() == raw[:8192]
    assert diagnostic['stderr_bytes'] == len(raw) and diagnostic['retained_bytes'] == 8192
    assert diagnostic['truncated'] and len(diagnostic['stderr_sha256']) == 64
    assert 'stderr_excerpt' not in json.dumps(report)
    assert storage_space.boundary(report, 0, 'fixture') is not None


def test_injected_host_reader_does_not_hide_guest_failure(tmp_path, monkeypatch):
    fixture_platform(monkeypatch)
    calls = []

    def denied(path):
        raise PermissionError('fixture guest failure')

    monkeypatch.setattr(storage_space.shutil, 'disk_usage', denied)
    report = storage_space.observe(tmp_path, backing_volume_reader=lambda distro: calls.append(distro))
    assert calls == []
    assert report['errors'] == [{'scope': 'filesystem', 'error_type': 'PermissionError'}]
    assert storage_space.boundary(report, 0, 'fixture') is not None


@pytest.mark.asyncio
async def test_shared_engine_only_injects_host_reader(tmp_path, monkeypatch):
    from test_workflow import CodingAgent, engine

    fixture_platform(monkeypatch)
    guest_calls = []
    original_guest = storage_space.shutil.disk_usage

    def guest(path):
        guest_calls.append(path)
        return original_guest(path)

    def forbidden_host(distribution):
        raise AssertionError('ordinary workflow fixture queried actual host')

    monkeypatch.setattr(storage_space.shutil, 'disk_usage', guest)
    monkeypatch.setattr(storage_space, 'windows_backing_volume', forbidden_host)
    project = tmp_path / 'project'
    project.mkdir()
    workflow = engine(project, CodingAgent())
    report = await workflow.storage.sample(force=True)
    assert await workflow.storage.sample() == report
    assert guest_calls == [workflow.store.root]
    assert not report['errors']
    assert [row['scope'] for row in report['volumes']] == ['filesystem', 'wsl_backing']
    assert report['volumes'][1]['fixture'] is True
    assert report['volumes'][1]['actual_native_storage'] is False


def test_nonstate_observation_does_not_guess_a_private_log_root(tmp_path, monkeypatch):
    fixture_platform(monkeypatch)

    def fail(distribution):
        raise subprocess.CalledProcessError(43, ['fixed-system-query'], stderr='EXAMPLE_PRIVATE not retained')

    monkeypatch.setattr(storage_space, 'windows_backing_volume', fail)
    report = storage_space.observe(tmp_path)
    diagnostic = report['diagnostics'][0]
    assert diagnostic['returncode'] == 43 and not diagnostic['retained']
    assert diagnostic['retention_error_type'] == 'PrivateStateRootUnavailable'
    assert not list(tmp_path.iterdir())
    assert 'EXAMPLE_PRIVATE' not in json.dumps(report)
    assert storage_space.boundary(report, 0, 'fixture') is not None


def test_private_diagnostic_refuses_a_symlink_and_preserves_unknown(tmp_path, monkeypatch):
    fixture_platform(monkeypatch)
    state = tmp_path / '.aa2'
    state.mkdir(mode=0o700)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (state / 'storage-diagnostics').symlink_to(outside, target_is_directory=True)

    def fail(distribution):
        raise subprocess.CalledProcessError(44, ['fixed-system-query'], stderr='EXAMPLE_PRIVATE rejected link')

    monkeypatch.setattr(storage_space, 'windows_backing_volume', fail)
    report = storage_space.observe(state)
    assert not report['diagnostics'][0]['retained'] and not list(outside.iterdir())
    assert report['errors'] == [{'scope': 'wsl_backing', 'error_type': 'CalledProcessError'}]
    assert 'EXAMPLE_PRIVATE' not in json.dumps(report)
    assert storage_space.boundary(report, 0, 'fixture') is not None


def test_private_stderr_stays_outside_model_material_and_public_view(tmp_path, monkeypatch):
    from test_runtime import configured
    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.diagnostics import references
    from auto_agents_2.materials import bundle
    from auto_agents_2.readview import create_view
    from auto_agents_2.sandbox import SandboxRunner

    project = tmp_path / 'project'
    project.mkdir()
    _, store = configured(project)
    (project / 'source.py').write_text('answer = 42\n')
    fixture_platform(monkeypatch)
    detail = 'EXAMPLE_PRIVATE_STORAGE_STDERR'

    def fail(distribution):
        raise subprocess.CalledProcessError(45, ['fixed-system-query'], stderr=detail)

    monkeypatch.setattr(storage_space, 'windows_backing_volume', fail)
    report = storage_space.observe(store.root)
    private = store.root / 'storage-diagnostics' / report['diagnostics'][0]['diagnostic_id']
    assert private.read_text() == detail
    assert not list(references(report))
    artifacts = ArtifactStore(store.root)
    control = artifacts.put('public control material', 'context_material')
    prepared = ContextPlanner(artifacts).prepare('recovery_diagnosis', {
        'failure': {'kind': 'resource_limit', 'evidence': {'storage': report, 'control': control}}, 'project': {},
    }, project, 'storage-privacy')
    assert detail not in json.dumps(prepared.record())
    assert str(private) not in json.dumps(prepared.record())
    published = [path for path in artifacts.root.rglob('*') if path.is_file()]
    public = artifacts.root / control['hash'][:2] / control['hash']
    assert published and public in published
    assert all(detail.encode() not in path.read_bytes() for path in published)
    material = bundle('fixed prompt', {}, published, [], {})
    assert str(private) not in json.dumps(material) and str(public) in json.dumps(material)
    view = create_view(project, tmp_path / 'view')
    assert not (view / '.aa2').exists() and (view / 'source.py').exists()
    command = SandboxRunner(store.root, binary=sys.executable).command(
        [sys.executable, '-c', 'pass'], view, [], readable=published, hidden=[project])
    mask = max(index for index in range(len(command) - 1)
               if command[index:index + 2] == ['--tmpfs', str(store.root)])
    reopened = [command[index + 2] for index in range(mask + 1, len(command) - 2)
                if command[index] in {'--bind', '--ro-bind'}]
    assert str(public) in reopened
    assert all(not private.is_relative_to(type(private)(destination)) for destination in reopened)
