import asyncio
import copy
import os
import sys

import pytest
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import DependencyResolutionError
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json

CRASH = "npm error Cannot read properties of null (reading 'edgesOut')"
DEBUG = "0 info using npm@10.9.9\n1 info using node@v22.23.4\n2 silly fetch manifest peer@4.1.11\n3 verbose stack ROOT_CAUSE_WITNESS\n"


def fixture(tmp_path, *, locked=False, shared=False, debug=DEBUG):
    project = tmp_path / 'project'
    project.mkdir()
    config, store = configured(project)
    if shared:
        body = config.load().model_dump(mode='json')
        body['shared_cache'] = {'enabled': True, 'root': str(tmp_path / 'shared')}
        write_json(config.path, body)
    rid = store.admit('installer evidence', {})['id']
    work = tmp_path / 'work'
    work.mkdir()
    write_json(work / 'package.json', {'name': 'fixture'})
    if locked:
        write_json(work / 'package-lock.json', {'lockfileVersion': 3, 'packages': {}})
    prefix = tmp_path / 'runtime'
    binary = prefix / 'bin/npm'
    binary.parent.mkdir(parents=True)
    binary.write_text(f'#!{sys.executable}\n' + f'''
import os,sys
from pathlib import Path
p = Path(os.environ.get('npm_config_logs_dir', os.environ['HOME'] + '/uncollected'))
p.mkdir(parents=True, exist_ok=True)
(p/'current-debug-0.log').write_text({debug!r})
print({CRASH!r}, file=sys.stderr)
sys.exit(1)
''')
    binary.chmod(0o755)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    return manager, store, rid, work, Environment(prefix, 'fixture-environment')


@pytest.mark.parametrize('locked', [False, True])
@pytest.mark.parametrize('shared', [False, True])
async def test_failed_installer_hands_actual_debug_and_streams_to_caller(tmp_path, locked, shared):
    manager, store, rid, work, environment = fixture(tmp_path, locked=locked, shared=shared)
    before = {p.name:p.read_bytes() for p in work.iterdir()}
    with pytest.raises(DependencyResolutionError) as caught:
        await manager.install_node(work, environment)
    evidence = caught.value.evidence
    assert evidence['version'] == 2 and evidence['exit_code'] == 1
    assert evidence['authority'] == 'external_command_observation' and evidence['capture_status'] == 'complete'
    assert evidence['runtime_identity'] == environment.fingerprint
    assert evidence['npm_debug']['status'] == 'captured'
    assert evidence['argv'][1] == ('ci' if locked else 'install')
    artifacts = ArtifactStore(store.root)
    assert any(CRASH in artifacts.read(ref) for ref in evidence['logs'])
    assert ''.join(artifacts.read(ref) for ref in evidence['npm_debug']['logs']) == DEBUG
    assert {p.name:p.read_bytes() for p in work.iterdir()} == before
    assert not list((store.root/'node-records').glob('*.json'))


@pytest.mark.parametrize('damage', ['missing', 'symlink', 'directory', 'fifo', 'hardlink', 'oversize', 'invalid_utf8', 'too_many', 'linked_parent'])
def test_debug_capture_does_not_follow_unsafe_paths_or_claim_completeness(tmp_path, damage, monkeypatch):
    from auto_agents_2 import installation_failure as capture
    config, store = configured(tmp_path)
    artifacts = ArtifactStore(store.root)
    logs = tmp_path/'logs'
    logs.mkdir()
    secret = tmp_path/'private'
    secret.write_text('PRIVATE_FILE_MUST_NOT_APPEAR')
    target = logs/'current-debug-0.log'
    if damage == 'symlink':
        target.symlink_to(secret)
    if damage == 'directory':
        target.mkdir()
    if damage == 'fifo':
        os.mkfifo(target)
    if damage == 'hardlink':
        os.link(secret, target)
    if damage == 'oversize':
        target.write_text(DEBUG)
        monkeypatch.setattr(capture, 'MAX_LOG_BYTES', 10)
    if damage == 'invalid_utf8':
        target.write_bytes(b'\xff')
    if damage == 'too_many':
        for i in range(17):
            (logs/f'current-debug-{i}.log').touch()
    if damage == 'linked_parent':
        link = tmp_path/'link'
        link.symlink_to(logs, target_is_directory=True)
        logs = link
    result = capture.npm_debug_logs(logs, artifacts)
    assert not result['logs']
    assert result['status'] == ('not_recorded' if damage=='missing' else 'unavailable' if damage=='too_many' else 'incomplete')
    assert secret.read_text() == 'PRIVATE_FILE_MUST_NOT_APPEAR'


def test_debug_capture_filters_credentials_and_never_reads_a_printed_host_path(tmp_path):
    from auto_agents_2.installation_failure import npm_debug_logs
    _, store = configured(tmp_path)
    logs = tmp_path/'logs'
    logs.mkdir()
    secret = tmp_path/'host-config'
    secret.write_text('HOST_SECRET_MUST_NOT_APPEAR')
    text = DEBUG + (f'complete log: {secret}\nhttps://fixture:fixture-password@example.test/?token=fixture-query\n'
                   'Authorization: Bearer fixture-bearer\n_authToken=fixture-auth\nfixture-env-secret\n'
                   'npm_012345678901234567890123456789012345\n')
    (logs/'current-debug-0.log').write_text(text)
    (logs/'auth.json').write_text('IGNORED_CONFIG_MUST_NOT_APPEAR')
    artifacts = ArtifactStore(store.root)
    result = npm_debug_logs(logs, artifacts, ['fixture-env-secret'])
    archived = ''.join(artifacts.read(ref) for ref in result['logs'])
    assert result['status'] == 'captured' and DEBUG in archived
    for value in ('HOST_SECRET_MUST_NOT_APPEAR', 'fixture-password', 'fixture-query', 'fixture-bearer',
                  'fixture-auth', 'fixture-env-secret', 'npm_012345678901234567890123456789012345', 'IGNORED_CONFIG_MUST_NOT_APPEAR'):
        assert value not in archived


def test_debug_total_limit_never_truncates_or_calls_partial_complete(tmp_path, monkeypatch):
    from auto_agents_2 import installation_failure as capture
    _, store = configured(tmp_path)
    artifacts = ArtifactStore(store.root)
    logs = tmp_path/'logs'
    logs.mkdir()
    for i in range(2):
        (logs/f'current-debug-{i}.log').write_text(DEBUG)
    monkeypatch.setattr(capture, 'MAX_TOTAL_BYTES', len(DEBUG.encode())+1)
    result = capture.npm_debug_logs(logs, artifacts)
    assert result['status'] == 'incomplete' and len(result['logs']) == 1
    assert artifacts.read(result['logs'][0]) == DEBUG


def test_old_progress_identity_preserved_and_new_diagnostics_are_facts_not_leases():
    from auto_agents_2.recovery_progress import RecoveryProgress
    context = {'failure': {'id':'failure','evidence':{'environment_error':CRASH}}}
    old = RecoveryProgress.basis(context, 'source', 'diagnose')
    context['failure']['evidence']['environment_failure'] = {}
    assert RecoveryProgress.basis(context, 'source', 'diagnose') == old
    context['failure']['evidence']['environment_failure'] = {'version':1,'logs':[
        {'hash':'a'*64,'kind':'dependency_log','bytes':100,'lease_id':'first'}]}
    observed = RecoveryProgress.basis(context, 'source', 'diagnose')
    assert observed != old
    context['failure']['evidence']['environment_failure']['logs'][0]['lease_id'] = 'second'
    assert RecoveryProgress.basis(context, 'source', 'diagnose') == observed


async def test_failed_command_keeps_full_streams_and_only_its_own_debug(tmp_path):
    manager, store, rid, work, environment = fixture(tmp_path)
    npm = environment.prefix/'bin/npm'
    original = npm.read_text()
    npm.write_text(original.replace('print(', "print('EARLY_LOG_WITNESS' + 'x'*30000)\nprint(", 1))
    with pytest.raises(DependencyResolutionError) as first:
        await manager.install_node(work, environment)
    artifacts = ArtifactStore(store.root)
    assert 'EARLY_LOG_WITNESS' not in str(first.value)
    assert any('EARLY_LOG_WITNESS' in artifacts.read(r) for r in first.value.evidence['logs'])
    npm.write_text(original.replace('ROOT_CAUSE_WITNESS','SECOND_ATTEMPT_WITNESS'))
    with pytest.raises(DependencyResolutionError) as second:
        await manager.install_node(work, environment)
    body = ''.join(artifacts.read(r) for r in second.value.evidence['npm_debug']['logs'])
    assert 'SECOND_ATTEMPT_WITNESS' in body and 'ROOT_CAUSE_WITNESS' not in body
    assert any('ROOT_CAUSE_WITNESS' in artifacts.read(r) for r in first.value.evidence['npm_debug']['logs'])


async def test_logs_disabled_is_not_silently_overridden_or_claimed_complete(tmp_path):
    manager, store, rid, work, environment = fixture(tmp_path)
    async def command(argv, workspace, env, *args, **kwargs):
        assert env['npm_config_logs_max'] == '0'
        assert env['npm_config_logs_dir'] != '/untrusted/shared'
        raise DependencyResolutionError('ETARGET', evidence={'version':1,'logs':[]})
    manager._command = command
    with pytest.raises(DependencyResolutionError) as caught:
        await manager._npm_command(['npm','ci'],work,
            {'npm_config_logs_max':'0','npm_config_logs_dir':'/untrusted/shared'}, [], [], environment)
    assert caught.value.evidence['npm_debug']['status'] == 'not_recorded'


@pytest.mark.parametrize('damage', ['missing','corrupt'])
async def test_failed_install_artifact_integrity_is_required_before_dispatch(tmp_path, damage):
    from test_provider_prompt_delivery import fixture_router

    from auto_agents_2.errors import ProviderError
    from auto_agents_2.models import ReviewResult

    manager, store, rid, work, environment = fixture(tmp_path)
    with pytest.raises(DependencyResolutionError) as caught:
        await manager.install_node(work, environment)
    # The actual installer-produced ref is the input. The fake provider must
    # never receive a missing/corrupt body, irrespective of a valid short error.
    router_root = tmp_path/'router'
    router_root.mkdir()
    router, project, other_store, other_run = fixture_router(router_root)
    ref = caught.value.evidence['npm_debug']['logs'][0]
    body = ArtifactStore(store.root).read(ref)
    copied = ArtifactStore(other_store.root).put(body, 'dependency_log')
    path = other_store.root/'artifacts'/copied['hash'][:2]/copied['hash']
    if damage == 'missing':
        path.unlink()
    else:
        path.write_text('corrupt')
    with pytest.raises(ProviderError, match='unavailable'):
        await router.invoke('review', {'feedback':{'environment_failure':{'logs':[copied]}}},
                            ReviewResult, project, other_run, 'bad-diagnostic',fresh=True)
    assert not any(e['kind']=='agent.started' for e in other_store.events(other_run))


@pytest.mark.parametrize('interrupt', [False, True])
async def test_real_engine_failure_diagnosis_mount_and_repair_resume(tmp_path, interrupt):
    from test_manifest_references import plan
    from test_workflow import CodingAgent, engine

    from auto_agents_2.engine import WorkflowEngine
    from auto_agents_2.models import Change, ImplementationResult
    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.recovery import RecoveryNeeded
    from auto_agents_2.workspace import tree_hash

    class Agent(CodingAgent):
        attempts = 0
        stopped = False
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == 'recovery_diagnosis':
                detail = context['failure']['evidence']['environment_failure']
                assert detail['source_hash'] == tree_hash(workspace)
                assert detail['runtime_identity'] == 'fixture-environment'
                assert detail['npm_debug']['status'] == 'captured'
                self.diagnosis_context = copy.deepcopy(context)
                return await self.router.invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)
            if stage == 'implement':
                if self.attempts:
                    detail = context['feedback']['environment_failure']
                    assert any('ROOT_CAUSE_WITNESS' in workflow.artifacts.read(r) for r in detail['npm_debug']['logs'])
                    if interrupt and not self.stopped:
                        self.stopped = True
                        raise asyncio.CancelledError()
                    self.attempts += 1
                    return ImplementationResult(summary='fixture causal correction', changes=[
                        Change(path='package.json', content='{"name":"fixture","version":"1.0.1"}')])
                self.attempts += 1
                result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
                result.changes.append(Change(path='package.json', content='{"name":"fixture","version":"1.0.0"}'))
                return result
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    project = tmp_path/'project'
    project.mkdir()
    agent = Agent()
    workflow = engine(project, agent)
    binary = tmp_path/'provider'
    binary.write_text(f'#!{sys.executable}\n' + '''
import hashlib,json,pathlib,sys
args=sys.argv[1:]
if args==['--version']:
    print('fixture 1');sys.exit(0)
schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
text=sys.stdin.read()
context=json.loads(text.split('<context>\\n',1)[1].split('\\n</context>',1)[0])
if 'text_token' in schema['properties']:
    result={'text_token':pathlib.Path(context['context_files'][0]).read_text(),'image_token':''}
else:
    refs=[]
    def visit(v):
        if isinstance(v,dict):
            if v.get('kind')=='dependency_log' and 'location' in v:refs.append(v)
            for x in v.values():visit(x)
        elif isinstance(v,list):
            for x in v:visit(x)
    visit(context)
    if isinstance(context.get('evidence_materials'),dict):
        visit(json.loads(pathlib.Path(context['evidence_materials']['artifact']['location']).read_text()))
    bodies=[]
    for ref in refs:
        p=pathlib.Path(ref['location']);raw=p.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==ref['hash']
        bodies.append(raw.decode())
        try:p.write_text('forbidden')
        except OSError:pass
        else:raise AssertionError('Diagnostic must be read-only')
    assert any('ROOT_CAUSE_WITNESS' in x for x in bodies)
    result={'action':'repair','task_ids':['T1'],'evidence_refs':['failure.evidence.environment_failure'],
            'reason':'Observed fixture resolver failure; scope remains unchanged','repair_targets':[]}
pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
''')
    binary.chmod(0o755)
    native = tmp_path/'native'
    native.mkdir()
    body = workflow.config.load().model_dump(mode='json')
    body['providers'] = {'codex':body['providers']['codex']}
    body['providers']['codex'].update(binary=str(binary),environment={'CODEX_HOME':str(native)})
    body['providers']['codex']['profile_map'] = {key:{'model':'test'} for key in ('balanced','deep','max')}
    write_json(workflow.config.path, body)
    agent.router = ProviderRouter(workflow.config, workflow.store, SandboxRunner(workflow.store.root))
    runtime = Environment(workflow.store.root/'environments/fixture-runtime','fixture-environment')
    (runtime.prefix/'bin').mkdir(parents=True)
    (runtime.prefix/'bin/python').symlink_to(sys.executable)
    npm = runtime.prefix/'bin/npm'
    npm.write_text(f'#!{sys.executable}\n' + f'''
import json,os,sys
from pathlib import Path
if json.loads(Path('package.json').read_text())['version']=='1.0.0':
    d=Path(os.environ['npm_config_logs_dir']);d.mkdir(exist_ok=True)
    (d/'current-debug-0.log').write_text({DEBUG!r})
    print({CRASH!r},file=sys.stderr);sys.exit(1)
if '--package-lock-only' in sys.argv:Path('package-lock.json').write_text('{{"lockfileVersion":3,"packages":{{}}}}')
else:Path('node_modules').mkdir(exist_ok=True)
''')
    npm.chmod(0o755)
    def bind(e):
        e._environment = WorkflowEngine._environment.__get__(e)
        async def ensure(*args): return runtime
        async def resolution(work, environment): return environment
        e.environments.ensure = ensure
        e.environments.with_resolution = resolution
    bind(workflow)
    rid = workflow.store.admit('addition',{})['id']
    proposal = plan()
    proposal.tasks[0].allowed_paths.extend(['package.json','package-lock.json'])
    base = workflow.workspaces.committed_baseline(rid,'main')
    context = {'specification':{'description':'addition'}}
    workflow.store.checkpoint(rid,'plan',proposal.model_dump())
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid,proposal.tasks[0],base,proposal,context)
    retained = workflow.store.get(rid,'candidate/environment-failures/T1/p0/r1')
    workflow.recovery.submit(rid,caught.value.failure)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid,'materials/recovery/diagnose/'+caught.value.failure.id+'/1')['materials']
    if interrupt:
        with pytest.raises(asyncio.CancelledError):
            await workflow._task(rid,proposal.tasks[0],base,proposal,context)
        workflow = engine(project,agent)
        bind(workflow)
    await workflow._task(rid,proposal.tasks[0],base,proposal,context)
    assert workflow.store.get(rid,'candidate/environment-failures/T1/p0/r1') == retained
    saved = workflow.store.get(rid,'develop/task/T1')
    assert saved['commit'] and saved['checks'][0]['exit_code']==0
    assert workflow.store.get(rid,'rounds/T1')['round']==2
    assert workflow.store.get(rid,'recovery/budgets/diagnosis/T1')['round']==1
