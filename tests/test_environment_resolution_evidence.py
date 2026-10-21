import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.environment_evidence import PYTHON_METADATA
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence import assess_observation
from auto_agents_2.models import Command
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json
from auto_agents_2.verification import VerificationService


@pytest.fixture
def installed(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    prefix = store.root / 'environments/installed'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / 'bin/python').write_text('fixture interpreter')
    (prefix / 'aa2-pip.lock').write_text('app==1.0\nleaf==2.0\n')
    (prefix / 'aa2-conda.lock').write_text('@EXPLICIT\nhttps://packages.example.test/python-3.13-0.conda\n')
    write_json(prefix / 'conda-meta/python.json', {
        'name': 'python', 'version': '3.13.0', 'build': '0',
        'depends': ['openssl >=3'], 'constrains': ['python_abi 3.13.*']})
    ready = {'fingerprint': 'inputs', 'private': False,
             'pip_hash': digest((prefix / 'aa2-pip.lock').read_bytes()),
             'conda_hash': digest((prefix / 'aa2-conda.lock').read_bytes()),
             'integrity': manager.integrity(prefix)}
    write_json(prefix / 'aa2-ready.json', ready)
    snapshot = {'prefix': str(prefix), 'runtime': {'python': '3.13.0', 'platform': 'linux'}, 'packages': [
        {'name': 'app', 'version': '1.0', 'requires_python': '>=3.11',
         'requires_dist': ['leaf>=2; python_version >= "3.11"'], 'provides_extra': ['test']},
        {'name': 'leaf', 'version': '2.0', 'requires_python': '>=3.10',
         'requires_dist': [], 'provides_extra': []}]}
    calls = []

    async def command(argv, workspace, env, writes, reads, **options):
        calls.append(argv)
        assert argv == [str(prefix / 'bin/python'), '-I', '-c', PYTHON_METADATA]
        assert reads == [prefix] and prefix not in writes and options['machine']
        return json.dumps(snapshot)

    manager._command = command
    environment = Environment(prefix, manager.installed_identity(prefix))
    return manager, environment, snapshot, calls


@pytest.mark.parametrize('private', [False, True])
async def test_complete_resolution_is_bound_and_reusable_without_installing(tmp_path, installed, private):
    manager, env, snapshot, calls = installed
    ready_path = env.prefix / 'aa2-ready.json'
    ready = json.loads(ready_path.read_text())
    ready['private'] = private
    write_json(ready_path, ready)
    before = manager.integrity(env.prefix)
    enriched = await manager.with_resolution(tmp_path, env)
    ref = enriched.resolution
    data = json.loads(ArtifactStore(manager.root).bytes(ref))
    assert ref['kind'] == 'environment_resolution'
    assert data['environment_hash'] == enriched.fingerprint == env.fingerprint
    assert data['pip_lock'] == (env.prefix / 'aa2-pip.lock').read_text()
    assert data['conda_explicit_lock'] == (env.prefix / 'aa2-conda.lock').read_text()
    assert data['resolution_hash'] == digest([data['pip_lock'], data['conda_explicit_lock']])
    assert data['python'] == {k: v for k, v in snapshot.items() if k != 'prefix'}
    assert data['conda_packages'][0]['depends'] == ['openssl >=3']
    assert data['conda_packages'][0]['constrains'] == ['python_abi 3.13.*']
    assert not data['redacted']
    assert (await manager.with_resolution(tmp_path, env)).resolution == ref
    assert len(calls) == 2 and manager.integrity(env.prefix) == before
    assert not (manager.root / 'environment-records').exists()


@pytest.mark.parametrize('damage', ['pip', 'conda', 'ready_hash', 'metadata', 'missing_metadata', 'link', 'identity'])
async def test_missing_or_inconsistent_evidence_never_publishes_success(tmp_path, installed, damage):
    manager, env, _, _ = installed
    if damage in {'pip', 'conda'}:
        (env.prefix / f'aa2-{damage}.lock').write_text('corrupted')
    elif damage == 'ready_hash':
        path = env.prefix / 'aa2-ready.json'
        ready = json.loads(path.read_text())
        ready['pip_hash'] = 'invalid'
        write_json(path, ready)
    elif damage == 'metadata':
        write_json(env.prefix / 'conda-meta/python.json', {'name': 'python', 'version': '3.13', 'build': '0', 'depends': 'not a list'})
    elif damage == 'missing_metadata':
        (env.prefix / 'conda-meta/python.json').unlink()
    elif damage == 'link':
        target = tmp_path / 'outside.lock'
        target.write_text('outside must not become evidence')
        (env.prefix / 'aa2-pip.lock').unlink()
        (env.prefix / 'aa2-pip.lock').symlink_to(target)
    else:
        env = Environment(env.prefix, 'another-environment')
    with pytest.raises(AA2Error, match='resolution evidence unavailable'):
        await manager.with_resolution(tmp_path, env)
    assert not list((manager.root / 'artifacts').glob('*/*'))
    assert env.prefix.exists()


@pytest.mark.parametrize('invalid', [[], {}, {'packages': []}, {'packages': [None]}, {'packages': [{'name': None}]}])
async def test_incomplete_python_metadata_is_not_a_resolution_record(tmp_path, installed, invalid):
    manager, env, _, _ = installed

    async def command(*args, **kwargs):
        return json.dumps(invalid)

    manager._command = command
    with pytest.raises(AA2Error, match='resolution evidence unavailable'):
        await manager.with_resolution(tmp_path, env)
    assert not list((manager.root / 'artifacts').glob('*/*'))


async def test_drift_during_capture_preserves_environment_and_rejects_evidence(tmp_path, installed):
    manager, env, snapshot, _ = installed

    async def command(*args, **kwargs):
        (env.prefix / 'changed-by-other-owner').write_text('preserve')
        return json.dumps(snapshot)

    manager._command = command
    with pytest.raises(AA2Error, match='resolution evidence unavailable'):
        await manager.with_resolution(tmp_path, env)
    assert (env.prefix / 'changed-by-other-owner').read_text() == 'preserve'


async def test_resolution_redacts_credentials_without_losing_dependency_edges(tmp_path, installed, monkeypatch):
    manager, env, snapshot, _ = installed
    monkeypatch.setenv('API_KEY', 'fixture-private-value')
    snapshot['packages'][0]['requires_dist'].append(
        'private @ https://user:fixture-password@packages.example.test/pkg?token=fixture-token')
    snapshot['packages'][0]['provides_extra'].append('fixture-private-value')
    data = json.loads(ArtifactStore(manager.root).bytes((await manager.with_resolution(tmp_path, env)).resolution))
    text = json.dumps(data)
    assert data['redacted']
    assert all(secret not in text for secret in ('fixture-password', 'fixture-token', 'fixture-private-value'))
    assert data['python']['packages'][0]['requires_dist'][0] == 'leaf>=2; python_version >= "3.11"'


def test_metadata_probe_reads_real_selected_interpreter_without_importing_packages():
    result = subprocess.run([sys.executable, '-I', '-c', PYTHON_METADATA], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    packages = {item['name'].lower(): item for item in data['packages']}
    assert packages['pytest']['version'] and packages['pytest']['requires_python']
    assert packages['pydantic']['requires_dist']
    assert data['runtime']['python'].startswith(f'{sys.version_info.major}.{sys.version_info.minor}.')


async def observation(tmp_path):
    config, store = configured(tmp_path)
    store.admit('resolution evidence fixture', {})
    artifacts = ArtifactStore(store.root)
    ref = artifacts.put({'version': 1, 'environment_hash': 'fixture-installed', 'pip_lock': 'leaf==2.0\n'}, 'environment_resolution')
    env = Environment(Path(sys.prefix), 'fixture-installed', ref)
    command = Command(argv=[sys.executable, '-c', "print('real check')"])
    service = VerificationService(store.root, SandboxRunner(store.root, _test_unconfined=True))
    result = (await service.run([command], tmp_path, env))[0]
    return store, artifacts, env, command, result


@pytest.mark.parametrize('damage', ['none', 'missing', 'old_policy', 'wrong_binding', 'removed_artifact'])
async def test_checks_bind_and_validate_environment_resolution(tmp_path, damage):
    store, artifacts, env, command, row = await observation(tmp_path)
    assert row.environment_resolution == env.resolution and row.exit_code == 0
    if damage == 'missing':
        row = row.model_copy(update={'environment_resolution': {}})
    elif damage == 'old_policy':
        row = row.model_copy(update={'checker_version': 'verification-v12'})
    elif damage == 'wrong_binding':
        ref = artifacts.put({'version': 1, 'environment_hash': 'different'}, 'environment_resolution')
        env = Environment(env.prefix, env.fingerprint, ref)
        row = row.model_copy(update={'environment_resolution': ref})
    elif damage == 'removed_artifact':
        ref = row.environment_resolution
        (artifacts.root / ref['hash'][:2] / ref['hash']).unlink()
    verdict = assess_observation(command, row, tmp_path, env, artifacts)
    assert verdict.valid == verdict.passed == (damage == 'none')


@pytest.mark.parametrize('stage,field', [('review', 'checks'), ('verify', 'evidence'), ('recovery_diagnosis', 'failure')])
async def test_resolution_survives_task_final_and_recovery_material_selection(tmp_path, stage, field):
    store, artifacts, env, command, row = await observation(tmp_path)
    content = [row.model_dump()]
    if stage == 'recovery_diagnosis':
        content = {'evidence': {'checks': content}}
    prepared = ContextPlanner(artifacts).prepare(stage, {field: content}, tmp_path, run_id=store.current()['id'])
    selected = prepared.payload[field]
    if stage == 'recovery_diagnosis':
        selected = selected['evidence']['checks']
    ref = selected[0]['environment_resolution']
    assert ref == env.resolution
    assert json.loads(artifacts.bytes(ref))['pip_lock'] == 'leaf==2.0\n'


async def test_workflow_task_final_and_no_change_reviews_receive_resolution(tmp_path):
    class Reviewer(CodingAgent):
        reviewed = []

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            for row in context.get('checks', context.get('evidence', [])):
                assert row['environment_resolution']['kind'] == 'environment_resolution'
                self.reviewed.append(operation)
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Reviewer()
    project = tmp_path / 'project'
    project.mkdir()
    workflow = engine(project, agent)
    ref = workflow.artifacts.put({'version': 1, 'environment_hash': 'test-environment', 'pip_lock': 'fixture==1\n'}, 'environment_resolution')

    async def environment(*args, **kwargs):
        return Environment(Path(sys.prefix), 'test-environment', ref)

    workflow._environment = environment
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('Add two numbers')
    assert (await workflow.start(str(requirements)))['status'] == 'SUCCEEDED'
    assert 'develop/task/T1/review' in agent.reviewed and 'final-review' in agent.reviewed
    receipt = workflow.store.get(workflow.store.current()['id'], 'delivery/receipt')
    assert receipt['runtime']['resolution'] == ref
    assert all(row['environment_resolution'] == ref for row in receipt['runtime']['evidence']['checks'])
    agent.reviewed.clear()
    assert (await workflow.start(str(requirements)))['data']['outcome'] == 'NO_CHANGES'
    assert agent.reviewed


async def test_environment_preparation_supplies_resolution_before_ready(tmp_path, installed):
    manager, env, _, _ = installed
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('environment evidence', {})['id']
    workflow.environments = manager

    async def ensure(*args, **kwargs):
        return env

    manager.ensure = ensure
    actual = await workflow._environment_once(tmp_path, rid)
    assert actual.resolution['kind'] == 'environment_resolution'
    assert json.loads(workflow.artifacts.bytes(actual.resolution))['environment_hash'] == env.fingerprint


async def test_metadata_probe_failure_preserves_ready_environment(tmp_path, installed):
    manager, env, _, _ = installed
    before = manager.integrity(env.prefix)

    async def command(*args, **kwargs):
        raise AA2Error('probe unavailable')

    manager._command = command
    with pytest.raises(AA2Error, match='probe unavailable'):
        await manager.with_resolution(tmp_path, env)
    assert manager.integrity(env.prefix) == before
    assert not list((manager.root / 'artifacts').glob('*/*'))


async def test_archived_recovery_observation_mounts_its_resolution(tmp_path):
    store, artifacts, env, command, row = await observation(tmp_path)
    prepared = ContextPlanner(artifacts).prepare('recovery_diagnosis',
        {'observations': [{'checks': [row.model_dump()]}]}, tmp_path,
        run_id=store.current()['id'])
    assert prepared.payload['observations']['artifact']['kind'] == 'context_material'
    assert env.resolution in prepared.payload['evidence_materials']
    assert json.loads(artifacts.bytes(env.resolution))['environment_hash'] == row.environment_hash
