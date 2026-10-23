"""Batch failures retain independent waits; a retry answer grants no repair scope."""
import asyncio
import sys

import pytest
from test_recovery_policy import ready
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import DependencyResolutionError, EnvironmentCommandError
from auto_agents_2.faults import HumanIntervention, StateConflict
from auto_agents_2.installation_failure import command_failure
from auto_agents_2.models import Change, Command, ImplementationResult, TaskSpec
from auto_agents_2.task_batch import collect_batch
from auto_agents_2.util import digest


class ParallelAgent(CodingAgent):
    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == 'implement' and context['task']['id'] == 'T2':
            self.calls.append((stage, fresh))
            return ImplementationResult(summary='independent increment', changes=[
                Change(path='inc.py', content='def inc(value): return value + 1\n'),
                Change(path='tests/test_inc.py', content='from inc import inc\ndef test_inc(): assert inc(2) == 3\n')])
        result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
        if stage == 'plan':
            first = result.tasks[0]
            first.allowed_paths = ['calc.py', 'tests/test_calc.py']
            first.checks = [Command(argv=[sys.executable, '-m', 'pytest', '-q', 'tests/test_calc.py'], expected_tests=1)]
            result.tasks.append(TaskSpec(id='T2', title='increment', description='independent increment',
                requirement_ids=['R1'], allowed_paths=['inc.py', 'tests/test_inc.py'], acceptance=['2 + 1 == 3'],
                checks=[Command(argv=[sys.executable, '-m', 'pytest', '-q', 'tests/test_inc.py'], expected_tests=1)]))
            result.final_checks = [Command(argv=[sys.executable, '-m', 'pytest', '-q', 'tests'], expected_tests=2)]
        return result


@pytest.mark.parametrize('interrupt_after_ack', [False, True])
async def test_real_engine_two_commands_wait_independently_before_retry(tmp_path, interrupt_after_ack):
    project = tmp_path / 'project'
    project.mkdir()
    workflow = engine(project, ParallelAgent())
    config = workflow.config.load().model_dump(mode='json')
    config['execution']['max_parallel_tasks'] = 2
    from auto_agents_2.util import write_json
    write_json(workflow.config.path, config)
    request = tmp_path / 'request.md'
    request.write_text('Addition and independent increment')
    original_environment = workflow._environment
    barrier, calls = asyncio.Event(), []
    failing = True

    async def environment(work, *args, **kwargs):
        nonlocal failing
        task_id = work.name.split('-')[0]
        if task_id in {'T1', 'T2'}:
            calls.append(task_id)
        if failing:
            if len(calls) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 10)
            # Actual manager capture and process exit, but no network or install.
            await workflow.environments._command([sys.executable, '-c',
                "import sys; print('original unknown ' + sys.argv[1]); sys.exit(41)", task_id], work, {}, [work])
        return await original_environment(work, *args, **kwargs)

    async def pause(question):
        raise asyncio.CancelledError()

    workflow._environment = environment
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()['id']
    queued = workflow.store.get(rid, 'environment/prerequisite-queue')
    assert len(queued) == 2
    by_task = {item['requirement']['evidence']['controller_task']['task_id']: item for item in queued.values()}
    assert set(by_task) == {'T1', 'T2'}
    plan_hash = digest(workflow.store.get(rid, 'plan'))
    rounds = {task: workflow.store.get(rid, 'rounds/' + task) for task in by_task}
    for task, item in by_task.items():
        evidence = item['requirement']['evidence']
        assert evidence['classification'] == 'unknown' and evidence['exit_code'] == 41
        assert evidence['controller_task']['plan_hash'] == plan_hash
        assert evidence['controller_task']['source_hash'] == workflow.store.get(rid, 'develop/task/' + task)['source_hash']
        assert any('original unknown ' + task in workflow.artifacts.read(ref) for ref in evidence['logs'])
        assert workflow.store.get(rid, 'develop/task/' + task)['phase'] == 'applied'
    question = workflow.store.pending(rid)[0]
    assert 'Task T1:' in question['prompt']
    workflow.store.answer(question['id'], 'y', question['artifact_hash'])
    checkpoint = workflow.store.checkpoints

    def cut_after_consuming_answer(rid, values, **kwargs):
        checkpoint(rid, values, **kwargs)
        if values.get('prerequisite/' + by_task['T1']['identity']) == 1:
            raise asyncio.CancelledError()

    if interrupt_after_ack:
        workflow.store.checkpoints = cut_after_consuming_answer
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    workflow.store.checkpoints = checkpoint
    if interrupt_after_ack:
        assert workflow.store.get(rid, 'environment/prerequisite-wait') is None
        with pytest.raises(asyncio.CancelledError):
            await workflow.start()
    assert sorted(calls) == ['T1', 'T2']  # Answering T1 cannot rerun either original wait.
    second = workflow.store.pending(rid)[0]
    assert 'Task T2:' in second['prompt']
    assert second['artifact_hash'] == by_task['T2']['identity']
    assert workflow.store.get(rid, 'prerequisite/' + by_task['T1']['identity']) == 1
    assert workflow.store.get(rid, 'prerequisite/' + by_task['T2']['identity']) is None
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    assert workflow.store.pending(rid) == [second] and sorted(calls) == ['T1', 'T2']
    for task in by_task:
        assert workflow.store.get(rid, 'rounds/' + task) == rounds[task]
    workflow.store.answer(second['id'], 'y', second['artifact_hash'])
    failing = False
    result = await workflow.start()
    assert result['status'] == 'SUCCEEDED' and workflow.store.get(rid, 'develop/done') == ['T1', 'T2']
    assert workflow.store.get(rid, 'environment/prerequisite-queue') == {}
    for task, item in by_task.items():
        assert workflow.store.get(rid, 'prerequisite/' + item['identity']) == 1
        assert workflow.store.get(rid, 'environment/prerequisite-observations/' + item['identity']) == item
        assert workflow.store.get(rid, 'rounds/' + task) == rounds[task]
    assert not workflow.store.get(rid, 'recovery/active')
    assert not [stage for stage, _ in workflow.agent.calls if stage == 'decision']


async def inject(workflow, rid, plan, task, error):
    async def fail(*args):
        raise error
    workflow._task = fail
    return await workflow._observed_task(rid, task, 'original-base', plan, {})


async def test_successful_sibling_is_integrated_while_failed_task_keeps_its_wait(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    workflow = engine(project, ParallelAgent())
    from auto_agents_2.util import write_json
    config = workflow.config.load().model_dump(mode='json')
    config['execution']['max_parallel_tasks'] = 2
    write_json(workflow.config.path, config)
    request = tmp_path / 'request.md'
    request.write_text('Addition and independent increment')
    original_environment = workflow._environment
    failures = []

    async def environment(work, *args):
        if work.name.startswith('T2-'):
            failures.append(True)
            await workflow.environments._command([sys.executable, '-c', "raise SystemExit(41)"], work, {}, [work])
        return await original_environment(work, *args)

    async def pause(question):
        raise asyncio.CancelledError()

    workflow._environment, workflow.question_handler = environment, pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()['id']
    assert workflow.store.get(rid, 'develop/done') == ['T1']
    queued = workflow.store.get(rid, 'environment/prerequisite-queue')
    assert len(queued) == 1
    question = workflow.store.pending(rid)[0]
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    assert failures == [True] and workflow.store.pending(rid) == [question]
    workflow.store.answer(question['id'], 'y', question['artifact_hash'])
    workflow._environment = original_environment
    assert (await workflow.start())['status'] == 'SUCCEEDED'
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 1


async def test_equal_command_facts_in_different_tasks_keep_distinct_questions(tmp_path):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[0]
    second = task.model_copy(update={'id': 'T2', 'origin_id': 'T2'})
    evidence = command_failure(['same'], work, outcome='nonzero_exit', exit_code=7)
    error = EnvironmentCommandError('same unknown failure', evidence=evidence)
    for current in [task, second]:
        with pytest.raises(HumanIntervention):
            await inject(workflow, rid, plan, current, error)
    queued = workflow.store.get(rid, 'environment/prerequisite-queue')
    assert len(queued) == 2
    assert {item['requirement']['evidence']['controller_task']['task_id'] for item in queued.values()} == {task.id, 'T2'}
    assert 'controller_task' not in error.evidence  # Original command fact is not mutated.


@pytest.mark.parametrize('kind', ['declared', 'internal', 'legacy'])
async def test_wrapper_does_not_change_other_failure_routes(tmp_path, kind):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[0]
    evidence = command_failure(['same'], work, outcome='nonzero_exit', exit_code=7)
    error = {'declared': DependencyResolutionError('declared', evidence=evidence),
             'internal': TypeError('broken invariant'), 'legacy': EnvironmentCommandError('legacy')}[kind]
    with pytest.raises(type(error)) as caught:
        await inject(workflow, rid, plan, task, error)
    assert caught.value is error
    assert workflow.store.get(rid, 'environment/prerequisite-queue') is None


async def test_observation_survives_batch_cancellation_before_aggregation(tmp_path):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[0]
    evidence = command_failure(['unknown'], work, outcome='nonzero_exit', exit_code=7)
    entered = asyncio.Event()

    async def fail(*args):
        entered.set()
        raise EnvironmentCommandError('unknown', evidence=evidence)

    async def cancel():
        await entered.wait()
        raise asyncio.CancelledError()

    workflow._task = fail
    with pytest.raises(asyncio.CancelledError):
        await collect_batch([workflow._observed_task(rid, task, 'base', plan, {}), cancel()])
    queued = workflow.store.get(rid, 'environment/prerequisite-queue')
    assert len(queued) == 1
    assert next(iter(queued.values()))['requirement']['evidence']['exit_code'] == 7


@pytest.mark.parametrize('corruption', ['identity', 'body', 'shape'])
async def test_corrupt_pending_set_never_retries_or_consumes_answer(tmp_path, corruption):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[0]
    evidence = command_failure(['unknown'], work, outcome='nonzero_exit', exit_code=7)
    with pytest.raises(HumanIntervention):
        await inject(workflow, rid, plan, task, EnvironmentCommandError('unknown', evidence=evidence))
    queued = workflow.store.get(rid, 'environment/prerequisite-queue')
    identity = next(iter(queued))
    if corruption == 'identity':
        queued['unrelated'] = queued.pop(identity)
    elif corruption == 'body':
        queued[identity]['requirement']['evidence']['exit_code'] = 0
    else:
        queued = ['not a pending map']
    workflow.store.checkpoint(rid, 'environment/prerequisite-queue', queued)
    with pytest.raises(StateConflict):
        await workflow._resume_environment_waits(rid)
    assert workflow.store.get(rid, 'prerequisite/' + identity) is None
    assert not workflow.store.pending(rid)
