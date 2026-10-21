"""Recovery must use the same task build dependencies as actual verification."""
import copy

import pytest
from test_planned_repair_inputs import fixture

from auto_agents_2.builds import verification_commands
from auto_agents_2.errors import AA2Error
from auto_agents_2.models import CheckResult, Command
from auto_agents_2.recovery import adjudicate, check_failure, evidence_references


def build_case(tmp_path):
    plan, failure, context, diagnosis = fixture()
    task = plan.tasks[2]
    task.allowed_paths += ['frontend/index.html', 'frontend/src/main.tsx', 'other/index.html']
    task.checks[0].inputs = ['tests/test_new.py', 'frontend/dist']
    plan.runtime_prepare = [Command(argv=['npm', 'run', 'build'], cwd='frontend',
        inputs=['frontend/index.html', 'frontend/src'], outputs=['frontend/dist']),
        Command(argv=['npm', 'run', 'build'], cwd='other', inputs=['other/index.html'], outputs=['other/dist'])]
    (tmp_path / 'frontend').mkdir()
    (tmp_path / 'other').mkdir()
    failure.evidence['task'] = task.model_dump()
    context.update(plan=plan.model_dump(), failure=failure.model_dump())
    diagnosis.repair_targets[0].paths = ['frontend/index.html', 'frontend/src/main.tsx']
    return plan, failure, context, diagnosis


def assess(case, workspace):
    plan, failure, context, diagnosis = case
    return adjudicate(failure, plan=plan, diagnosis=diagnosis, evidence_context=context, workspace=workspace)


def test_check_failure_retains_exact_responsible_task_and_not_final_task(tmp_path):
    plan, _, _, _ = build_case(tmp_path)
    task = plan.tasks[2]
    commands = verification_commands(task.checks, tmp_path, plan.runtime_prepare)
    failure = check_failure(commands, [CheckResult(argv=commands[0].argv, cwd="frontend", exit_code=1, output="Missing index.html", duration=0.1, source_hash="source", environment_hash="environment")], task=task)
    assert failure.evidence['task'] == task.model_dump()
    assert failure.evidence['commands'] == [c.model_dump() for c in commands]
    task.acceptance.append('later modification')
    assert failure.evidence['task'] != task.model_dump()
    assert 'task' not in check_failure(commands, []).evidence


def test_required_build_inputs_are_planned_scope_without_fabricating_file_evidence(tmp_path):
    case = build_case(tmp_path)
    plan, failure, context, diagnosis = case
    before = copy.deepcopy((plan.model_dump(), failure.model_dump(), context))
    assert assess(case, tmp_path).repair_targets == diagnosis.repair_targets
    assert (plan.model_dump(), failure.model_dump(), context) == before
    assert not (tmp_path / 'frontend/index.html').exists()
    assert 'frontend/index.html' not in evidence_references(failure, context=context)


def test_transitive_build_input_and_merged_contract_use_shared_compiler(tmp_path):
    case = build_case(tmp_path)
    plan, failure, context, diagnosis = case
    task = plan.tasks[2]
    task.allowed_paths.append('assets/data.json')
    (tmp_path / 'assets').mkdir()
    plan.runtime_prepare[0].inputs.append('assets/dist')
    plan.runtime_prepare.append(Command(argv=['generate'], cwd='assets', inputs=['assets/data.json'], outputs=['assets/dist']))
    failure.evidence['task'] = task.model_dump()
    context.update(plan=plan.model_dump(), failure=failure.model_dump())
    diagnosis.repair_targets[0].paths = ['assets/data.json']
    assert assess(case, tmp_path).action == 'repair'


@pytest.mark.parametrize('change', ['unrelated_build', 'no_task_contract', 'stale_task', 'stale_plan',
                                 'allowed_only', 'outside_task', 'private', 'injected_failure_commands'])
def test_build_dependency_does_not_expand_task_permission_or_accept_unbound_claims(tmp_path, change):
    case = build_case(tmp_path)
    plan, failure, context, diagnosis = case
    target = diagnosis.repair_targets[0]
    if change == 'unrelated_build':
        target.paths = ['other/index.html']
    elif change == 'no_task_contract':
        failure.evidence.pop('task')
    elif change == 'stale_task':
        failure.evidence['task']['acceptance'].append('old obligation')
    elif change == 'stale_plan':
        context['plan']['runtime_prepare'][0]['inputs'].append('other/index.html')
    elif change == 'allowed_only':
        target.evidence_refs = ['plan.tasks[2].allowed_paths', 'plan.runtime_prepare[0]']
    elif change == 'outside_task':
        plan.tasks[2].allowed_paths.remove('frontend/index.html')
        failure.evidence['task'] = plan.tasks[2].model_dump()
        context['plan'] = plan.model_dump()
    elif change == 'private':
        plan.tasks[2].allowed_paths.append('.env')
        plan.runtime_prepare[0].inputs.append('.env')
        failure.evidence['task'] = plan.tasks[2].model_dump()
        context['plan'] = plan.model_dump()
        target.paths = ['.env']
    else:
        failure.evidence['commands'] = [plan.runtime_prepare[1].model_dump()]
        target.paths = ['other/index.html']
    with pytest.raises(AA2Error, match='outside the evidenced existing task scope'):
        assess(case, tmp_path)


def test_legacy_build_output_inference_matches_execution_without_running_build(tmp_path):
    case = build_case(tmp_path)
    plan, _, context, _ = case
    (tmp_path / 'frontend/package.json').write_text('{"devDependencies":{"vite":"1"}}')
    plan.runtime_prepare[0].outputs = []
    context['plan'] = plan.model_dump()
    assert assess(case, tmp_path).action == 'repair'


def test_no_workspace_cannot_infer_runtime_preparation_scope(tmp_path):
    case = build_case(tmp_path)
    with pytest.raises(AA2Error, match='outside the evidenced existing task scope'):
        assess(case, None)


async def test_real_recovery_dispatch_uses_selected_build_inputs(tmp_path):
    from test_recovery_policy import ready

    plan, failure, _, diagnosis = build_case(tmp_path)
    workflow, rid, work, _, _ = ready(tmp_path, record=failure)
    (work / 'frontend').mkdir(exist_ok=True)
    (work / 'other').mkdir(exist_ok=True)
    workflow.store.checkpoint(rid, 'plan', plan.model_dump())
    original = workflow.agent.invoke
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        if stage == 'recovery_diagnosis':
            assert context['failure']['evidence']['task'] == plan.tasks[2].model_dump()
            calls.append(stage)
            return diagnosis
        return await original(stage, context, *args, **kwargs)

    workflow.agent.invoke = invoke
    await workflow.recovery.resume(rid)
    assert calls == ['recovery_diagnosis']
    saved = workflow.store.get(rid, 'develop/task/T3')
    assert saved['feedback']['controller_recovery']['repair_targets'] == diagnosis.model_dump()['repair_targets']
    assert workflow.store.get(rid, 'recovery/active') is None
    assert not workflow.store.pending(rid)
    assert not (work / 'frontend/index.html').exists()
