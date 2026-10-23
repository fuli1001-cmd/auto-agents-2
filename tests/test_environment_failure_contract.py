"""External installation observations are not internal defects or repair permits."""
import asyncio
import sys

import pytest
from test_installation_failure_evidence import CRASH, DEBUG, fixture
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import AA2Error, DependencyResolutionError, RetryableInfrastructureError
from auto_agents_2.faults import known_exception
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize('message', ['npm error code ENOTCACHED', 'npm error code EACCES',
                                    'npm error code EINTEGRITY', 'npm error unknown vendor failure'])
@pytest.mark.parametrize('locked', [False, True])
async def test_unknown_npm_exit_preserves_observation_without_manifest_repair_authority(tmp_path, message, locked):
    manager, store, rid, work, environment = fixture(tmp_path, locked=locked)
    npm = environment.prefix / 'bin/npm'
    npm.write_text(npm.read_text().replace(repr(CRASH), repr(message)))
    before = {p.name: p.read_bytes() for p in work.iterdir()}
    with pytest.raises(AA2Error) as caught:
        await manager.install_node(work, environment)
    error = caught.value
    assert error.code == 'ENVIRONMENT_COMMAND_ERROR'
    assert not isinstance(error, (DependencyResolutionError, RetryableInfrastructureError))
    assert error.evidence['version'] == 2
    assert error.evidence['outcome'] == 'nonzero_exit' and error.evidence['exit_code'] == 1
    assert error.evidence['classification'] == 'unknown'
    assert error.evidence['capture_status'] == 'complete'
    assert error.evidence['runtime_identity'] == environment.fingerprint
    assert error.evidence['npm_debug']['status'] == 'captured'
    artifacts = ArtifactStore(store.root)
    assert ''.join(artifacts.read(r) for r in error.evidence['npm_debug']['logs']) == DEBUG
    boundary = known_exception(error, 'verify')
    assert boundary.requirement.kind == 'external_input'
    assert boundary.requirement.evidence == error.evidence
    assert set(boundary.requirement.evidence_refs) >= {r['hash'] for r in error.evidence['logs']}
    assert {p.name: p.read_bytes() for p in work.iterdir()} == before
    assert not list((store.root / 'node-records').glob('*.json'))


async def test_transport_error_has_same_owned_debug_without_changing_retry_kind(tmp_path):
    manager, store, rid, work, environment = fixture(tmp_path)
    npm = environment.prefix / 'bin/npm'
    npm.write_text(npm.read_text().replace(repr(CRASH), repr('npm error code ECONNRESET')))
    with pytest.raises(RetryableInfrastructureError) as caught:
        await manager.install_node(work, environment)
    assert not isinstance(caught.value, DependencyResolutionError)
    assert caught.value.evidence['classification'] == 'transient_transport'
    assert caught.value.evidence['npm_debug']['status'] == 'captured'


async def test_machine_failure_keeps_sanitized_original_but_success_stays_machine_output(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    argv = [sys.executable, '-c', "import sys; print('EARLY_WITNESS'); print('x'*40000); "
            "print('fixture-secret',file=sys.stderr); sys.exit(9)"]
    with pytest.raises(AA2Error) as caught:
        await manager._command(argv, tmp_path, {'API_KEY': 'fixture-secret'}, [tmp_path], machine=True)
    error = caught.value
    assert error.code == 'ENVIRONMENT_COMMAND_ERROR'
    body = ''.join(ArtifactStore(store.root).read(r) for r in error.evidence['logs'])
    assert 'EARLY_WITNESS' in body and 'fixture-secret' not in body
    assert 'fixture-secret' not in str(error.evidence)
    assert error.evidence['capture_status'] == 'complete'
    assert await manager._command([sys.executable, '-c', "print('{\"ok\":true}')"],
                                  tmp_path, {}, [tmp_path], machine=True) == '{"ok":true}\n'


async def test_timeout_is_incomplete_observation_and_cancel_is_not_failure(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    argv = [sys.executable, '-c', 'import time; print("partial",flush=True); time.sleep(60)']
    with pytest.raises(RetryableInfrastructureError) as caught:
        await manager._command(argv, tmp_path, {}, [tmp_path], timeout=.1)
    assert caught.value.evidence['outcome'] == 'timed_out'
    assert caught.value.evidence['exit_code'] is None
    assert caught.value.evidence['capture_status'] == 'incomplete'
    running = asyncio.create_task(manager._command(argv, tmp_path, {}, [tmp_path], timeout=90))
    await asyncio.sleep(.1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert known_exception(TypeError('internal invariant'), 'verify') is None
    assert known_exception(AA2Error('internal invariant'), 'verify') is None


def test_empty_legacy_prerequisite_identity_and_nested_serialization_are_unchanged():
    from auto_agents_2.faults import HumanRequirement, RecoveryRoute
    original = {'kind': 'external_input', 'reason': 'legacy', 'action': 'check',
                'resume_condition': 'verified', 'evidence_refs': []}
    requirement = HumanRequirement.model_validate(original)
    assert requirement.model_dump() == original
    assert RecoveryRoute(route='human', reason_code='legacy', phase='task',
                         human_requirement=requirement).model_dump()['human_requirement'] == original


@pytest.mark.parametrize('error_kind', ['internal', 'policy', 'os'])
async def test_spawn_boundary_does_not_reclassify_internal_or_policy_errors(tmp_path, error_kind):
    from auto_agents_2.errors import EnvironmentCommandError, PolicyViolation
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    error = {'internal': TypeError('invariant'), 'policy': PolicyViolation('outside scope'),
             'os': FileNotFoundError('missing executable')}[error_kind]
    async def spawn(*args, **kwargs):
        raise error
    manager.sandbox.spawn = spawn
    with pytest.raises(EnvironmentCommandError if error_kind == 'os' else type(error)) as caught:
        await manager._command(['fixture'], tmp_path, {}, [tmp_path])
    if error_kind == 'os':
        assert caught.value.evidence['outcome'] == 'start_failed'
        assert caught.value.evidence['capture_status'] == 'not_started'
    else:
        assert caught.value is error


async def test_real_environment_retry_decision_receives_observation_and_retains_binding(tmp_path):
    from pathlib import Path

    from test_recovery_policy import configure, ready

    from auto_agents_2.engine import WorkflowEngine
    from auto_agents_2.environment import Environment
    from auto_agents_2.installation_failure import command_failure
    from auto_agents_2.util import digest
    from auto_agents_2.workspace import tree_hash

    workflow, rid, work, plan, _ = ready(tmp_path)
    configure(workflow, execution={'infra_retries': 0})
    # A real command produces the evidence, then the actual Engine retry and
    # decision caller must transport it without another fabricated log fixture.
    manager = workflow.environments
    with pytest.raises(RetryableInfrastructureError) as observed:
        await manager._command([sys.executable, '-c', "import sys; print('ECONNRESET'); sys.exit(1)"],
                               work, {}, [work])
    observation = observed.value.evidence
    calls, decisions = [], []
    actual_invoke = workflow.agent.invoke
    async def invoke(stage, context, *args, **kwargs):
        if stage == 'decision':
            request = context['decision_request']
            last = request['evidence']['failure']['evidence']['last_failure']
            assert last['environment_failure'] == observation
            assert any('ECONNRESET' in workflow.artifacts.read(ref) for ref in observation['logs'])
            decisions.append(request)
        return await actual_invoke(stage, context, *args, **kwargs)
    async def once(*args):
        calls.append(True)
        if len(calls) == 1:
            raise observed.value
        return Environment(Path(sys.prefix), 'verified')
    workflow.agent.invoke = invoke
    workflow._environment_once = once
    workflow._environment = WorkflowEngine._environment.__get__(workflow)
    result = await workflow._environment(work, rid, plan)
    assert result.fingerprint == 'verified' and len(decisions) == 1 and len(calls) == 2
    key = 'infrastructure/environment/' + digest([str(work), tree_hash(work)])
    assert workflow.store.get(rid, key) == 1
    assert command_failure(['fixture'], work, outcome='nonzero_exit', exit_code=1)['classification'] == 'unknown'


async def test_actual_prerequisite_saves_full_unknown_failure_and_does_not_auto_retry(tmp_path):
    from test_recovery_policy import ready

    from auto_agents_2.errors import Waiting
    workflow, rid, work, _, _ = ready(tmp_path)
    async def leave_pending(question):
        raise Waiting('fixture leaves the real question unanswered')
    workflow.question_handler = leave_pending
    with pytest.raises(AA2Error) as observed:
        await workflow.environments._command(
            [sys.executable, '-c', "import sys; print('unknown vendor fact'); sys.exit(19)"], work, {}, [work])
    boundary = known_exception(observed.value, 'verify')
    assert boundary is not None
    with pytest.raises(Waiting):
        await workflow._wait_prerequisite(rid, boundary)
    pending = workflow.store.pending(rid)
    assert len(pending) == 1
    original = workflow.store.get(rid, 'recovery/current-fault')
    assert original['route'] == 'human'
    evidence = original['human_requirement']['evidence']
    assert evidence == observed.value.evidence
    assert any('unknown vendor fact' in workflow.artifacts.read(ref) for ref in evidence['logs'])
    with pytest.raises(Waiting):
        await workflow._wait_prerequisite(rid, boundary)
    assert workflow.store.pending(rid) == pending
    assert not any(e['kind'] == 'workflow.internal_error' for e in workflow.store.events(rid))


@pytest.mark.parametrize('reply', [None, 'n'])
async def test_engine_start_preserves_unknown_failure_through_real_pause_and_reentry(tmp_path, reply):
    from test_workflow import CodingAgent, engine

    project = tmp_path / 'project'
    project.mkdir()
    workflow = engine(project, CodingAgent())
    actual_environment = workflow._environment
    commands, questions = [], []
    request = tmp_path / 'request.md'
    request.write_text('Add numbers')
    async def fail_environment(work, *args, **kwargs):
        commands.append(True)
        await workflow.environments._command(
            [sys.executable, '-c', "import sys; print('ENOTCACHED original observation'); sys.exit(1)"],
            work, {}, [work])
    async def pause_at_question(question):
        questions.append(question)
        if reply and len(questions) == 1:
            return reply
        raise asyncio.CancelledError()
    workflow._environment = fail_environment
    workflow.question_handler = pause_at_question
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    run = workflow.store.current()
    rid = run['id']
    assert run['status'] == 'PAUSED'
    pending = workflow.store.pending(rid)
    assert len(pending) == (0 if reply else 1)
    original = workflow.store.get(rid, 'environment/prerequisite-wait')
    rounds = workflow.store.get(rid, 'rounds/T1')
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    current = workflow.store.pending(rid)
    assert len(current) == 1 and (reply or current == pending)
    assert workflow.store.get(rid, 'environment/prerequisite-wait') == original
    assert len(commands) == 1
    assert workflow.store.get(rid, 'rounds/T1') == rounds
    assert not any(e['kind'] == 'workflow.internal_error' for e in workflow.store.events(rid))
    fault = workflow.store.get(rid, 'recovery/current-fault')
    assert fault['reason_code'] == 'ENVIRONMENT_COMMAND_ERROR'
    assert fault['human_requirement']['evidence']['exit_code'] == 1
    # The original formal answer only enables another real stage check. It
    # cannot set a passing environment or task result by itself.
    workflow.store.answer(current[0]['id'], 'y', current[0]['artifact_hash'])
    workflow._environment = actual_environment
    result = await workflow.start()
    assert result['status'] == 'SUCCEEDED'
    assert workflow.store.get(rid, 'environment/prerequisite-wait') is None
    assert workflow.store.get(rid, 'rounds/T1') == rounds
    assert workflow.store.get(rid, 'develop/done') == ['T1']
    assert workflow.store.get(rid, 'verify/review')['approved']


@pytest.mark.parametrize('code', [1, 0])
async def test_prototype_installer_observations_preserve_existing_retry_policy(tmp_path, code):
    manager, store, rid, work, _ = fixture(tmp_path)
    actual_spawn = manager.sandbox.spawn
    async def substitute(argv, *args, **kwargs):
        assert argv[1:] == ['-m', 'playwright', 'install', 'chromium']
        return await actual_spawn([sys.executable, '-c',
            f"import sys; print('BROWSER_INSTALL_WITNESS'); sys.exit({code})"], *args, **kwargs)
    manager.sandbox.spawn = substitute
    cache, scratch = tmp_path / 'browser-cache', tmp_path / 'scratch'
    cache.mkdir()
    scratch.mkdir()
    with pytest.raises(RetryableInfrastructureError) as caught:
        await manager.install_browser(work, cache, scratch)
    evidence = caught.value.evidence
    assert evidence['outcome'] == ('nonzero_exit' if code else 'postcondition_failed')
    assert evidence['exit_code'] == code
    assert any('BROWSER_INSTALL_WITNESS' in ArtifactStore(store.root).read(ref) for ref in evidence['logs'])
