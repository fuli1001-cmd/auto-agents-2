import asyncio
import sys

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error, DependencyResolutionError, RetryableInfrastructureError
from auto_agents_2.faults import known_exception
from auto_agents_2.models import Change, ImplementationResult
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json

CRASH = "npm error Cannot read properties of null (reading 'edgesOut')"


def installer(root, message=CRASH, code=1):
    binary = root / 'bin/npm'
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f'#!{sys.executable}\nimport sys\nprint({message!r}, file=sys.stderr)\nsys.exit({code})\n')
    binary.chmod(0o755)
    return Environment(root, 'fixture-node')


@pytest.mark.parametrize('locked', [False, True])
@pytest.mark.parametrize('shared', [False, True])
@pytest.mark.parametrize('message,expected', [
    (CRASH, 'DEPENDENCY_SOLVER_ERROR'),
    ('npm error code ERESOLVE\nnpm error unable to resolve dependency tree', 'DEPENDENCY_RESOLUTION_ERROR'),
    ('npm ERR! code ETARGET\nnpm ERR! No matching version', 'DEPENDENCY_RESOLUTION_ERROR'),
])
async def test_npm_resolution_failure_retains_inputs_logs_and_no_readiness(tmp_path, locked, shared, message, expected):
    project = tmp_path / 'project'
    project.mkdir()
    config, store = configured(project)
    if shared:
        body = config.load().model_dump(mode='json')
        body['shared_cache'] = {'enabled': True, 'root': str(tmp_path / 'shared')}
        write_json(config.path, body)
    rid = store.admit('npm fixture', {})['id']
    work = tmp_path / 'work'
    work.mkdir()
    write_json(work / 'package.json', {'name': 'fixture', 'version': '1.0.0'})
    lock = work / 'package-lock.json'
    if locked:
        write_json(lock, {'lockfileVersion': 3, 'packages': {}})
    before = {p.name: p.read_bytes() for p in work.iterdir()}
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    with pytest.raises(DependencyResolutionError) as caught:
        await manager.install_node(work, installer(tmp_path / 'runtime', message))
    assert caught.value.code == expected
    assert message in str(caught.value)
    if expected == 'DEPENDENCY_SOLVER_ERROR':
        assert 'not proof' in str(caught.value)
    assert {p.name: p.read_bytes() for p in work.iterdir()} == before
    assert not list((store.root / 'node-records').glob('*.json'))
    with store.connect() as db:
        rows = db.execute("SELECT key FROM checkpoints WHERE run_id=? AND key LIKE 'environment/logs/%'", (rid,)).fetchall()
    logs = [ref for row in rows for ref in store.get(rid, row[0])['logs']]
    assert any(message in ArtifactStore(store.root).read(ref) for ref in logs)


@pytest.mark.parametrize('message,expected', [
    ('npm error code ECONNRESET', RetryableInfrastructureError),
    ('npm error code EACCES', AA2Error),
    ('npm error code E401', AA2Error),
    ('npm error code EINTEGRITY', AA2Error),
    ('npm error code ENOSPC', AA2Error),
    ('npm error unknown vendor failure', AA2Error),
    (CRASH + '\nnpm error code EACCES', AA2Error),
    ('npm error code ERESOLVE\nnpm error code E401', AA2Error),
    (CRASH + '\nnpm error code ECONNRESET', RetryableInfrastructureError),
    ('npm warn example: ' + CRASH, AA2Error),
    ("npm error Cannot read properties of null (reading 'other')", AA2Error),
])
async def test_other_npm_failures_are_not_declared_manifest_defects(tmp_path, message, expected):
    config, store = configured(tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    write_json(work / 'package.json', {'name': 'fixture'})
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    with pytest.raises(expected) as caught:
        await manager.install_node(work, installer(tmp_path / 'runtime', message))
    assert type(caught.value) is expected


async def test_solver_text_requires_installer_context_and_failed_exit(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    environment = installer(tmp_path / 'runtime')
    binary = str(environment.prefix / 'bin/npm')
    with pytest.raises(AA2Error) as caught:
        await manager._command([binary], tmp_path, environment.variables(tmp_path), [tmp_path])
    assert type(caught.value) is AA2Error
    installer(environment.prefix, code=0)
    await manager._command([binary], tmp_path, environment.variables(tmp_path), [tmp_path], npm_resolution=True)


def test_solver_failure_outside_task_does_not_become_internal_incident():
    from auto_agents_2.errors import DependencySolverError
    for error in [DependencySolverError(CRASH), DependencyResolutionError('ERESOLVE')]:
        boundary = known_exception(error, 'verify')
        assert boundary.requirement.kind == 'external_input'
        assert boundary.reason_code == error.code and boundary.phase == 'verify'
    assert known_exception(AA2Error(CRASH), 'verify') is None
    assert known_exception(TypeError("Cannot read properties of null (reading 'edgesOut')"), 'verify') is None


@pytest.mark.parametrize('interrupt', [False, True])
async def test_engine_routes_real_npm_failure_to_diagnosis_and_preserves_resume(tmp_path, interrupt):
    from test_manifest_references import plan

    class Agent(CodingAgent):
        attempts = 0
        stopped = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == 'implement':
                if self.attempts:
                    assert 'edgesOut' in context['feedback']['environment_error']
                    if interrupt and not self.stopped:
                        self.stopped = True
                        raise asyncio.CancelledError()
                    self.attempts += 1
                    return ImplementationResult(summary='scope-limited fixture repair', changes=[
                        Change(path='package.json', content='{"name":"fixture","version":"1.0.1"}')])
                self.attempts += 1
                result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
                result.changes.append(Change(path='package.json', content='{"name":"fixture","version":"1.0.0"}'))
                return result
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Agent()
    workflow = engine(tmp_path, agent)
    runtime = installer(workflow.store.root / 'environments/fixture-runtime')
    (runtime.prefix / 'bin/python').symlink_to(sys.executable)
    (runtime.prefix / 'bin/npm').write_text(f'#!{sys.executable}\n' + '''import json, sys
from pathlib import Path
if json.loads(Path('package.json').read_text())['version']=='1.0.0':
    print("npm error Cannot read properties of null (reading 'edgesOut')", file=sys.stderr)
    sys.exit(1)
if '--package-lock-only' in sys.argv:
    Path('package-lock.json').write_text('{"lockfileVersion":3,"packages":{}}')
else:
    Path('node_modules').mkdir(exist_ok=True)
''')

    def bind(workflow):
        workflow._environment = WorkflowEngine._environment.__get__(workflow)
        async def ensure(*args):
            return runtime
        async def resolution(work, environment):
            return environment
        workflow.environments.ensure = ensure
        workflow.environments.with_resolution = resolution

    bind(workflow)
    rid = workflow.store.admit('addition', {})['id']
    proposal = plan()
    proposal.tasks[0].allowed_paths.extend(['package.json', 'package-lock.json'])
    base = workflow.workspaces.committed_baseline(rid, 'main')
    context = {'specification': {'description': 'addition'}}
    workflow.store.checkpoint(rid, 'plan', proposal.model_dump())
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, proposal.tasks[0], base, proposal, context)
    assert caught.value.failure.kind == 'environment'
    assert not caught.value.failure.evidence['checks']
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 1
    failure_key = 'candidate/environment-failures/T1/p0/r1'
    retained = workflow.store.get(rid, failure_key)
    assert retained['patch'] and retained['implementation'] and not retained.get('commit')
    workflow.recovery.submit(rid, caught.value.failure)
    await workflow.recovery.resume(rid)
    if interrupt:
        with pytest.raises(asyncio.CancelledError):
            await workflow._task(rid, proposal.tasks[0], base, proposal, context)
        workflow = engine(tmp_path, agent)
        bind(workflow)
    await workflow._task(rid, proposal.tasks[0], base, proposal, context)
    saved = workflow.store.get(rid, 'develop/task/T1')
    assert saved['commit'] and saved['checks'][0]['tests'] > 0 and saved['checks'][0]['exit_code'] == 0
    assert workflow.store.get(rid, failure_key) == retained
    assert workflow.store.get(rid, 'rounds/T1')['round'] == agent.attempts == 2
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T1')['round'] == 1


@pytest.mark.parametrize('locked', [False, True])
async def test_frozen_install_never_synthesizes_or_changes_a_lock(tmp_path, locked):
    config, store = configured(tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    write_json(work / 'package.json', {'name': 'fixture'})
    lock = work / 'package-lock.json'
    if locked:
        write_json(lock, {'lockfileVersion': 3, 'packages': {}})
    before = lock.read_bytes() if locked else None
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    with pytest.raises(AA2Error) as caught:
        await manager.install_node(work, installer(tmp_path / 'runtime'), frozen=True)
    assert (lock.read_bytes() if lock.exists() else None) == before
    assert isinstance(caught.value, DependencyResolutionError) == locked
    assert not list((store.root / 'node-records').glob('*.json'))


async def test_npm_solver_error_keeps_sanitized_diagnostics(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit('npm diagnostic redaction', {})['id']
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    message = CRASH + '\nhttps://user:fixture-password@example.test/?token=fixture-token\nfixture-secret-value'
    environment = installer(tmp_path / 'runtime', message)
    env = {**environment.variables(tmp_path), 'API_KEY': 'fixture-secret-value'}
    with pytest.raises(DependencyResolutionError) as caught:
        await manager._command([str(environment.prefix / 'bin/npm')], tmp_path, env, [tmp_path], npm_resolution=True)
    with store.connect() as db:
        keys = db.execute("SELECT key FROM checkpoints WHERE run_id=? AND key LIKE 'environment/logs/%'", (rid,)).fetchall()
    archived = ''.join(ArtifactStore(store.root).read(ref) for key in keys for ref in store.get(rid, key[0])['logs'])
    assert CRASH in archived
    for secret in ('fixture-password', 'fixture-token', 'fixture-secret-value'):
        assert secret not in str(caught.value) and secret not in archived
