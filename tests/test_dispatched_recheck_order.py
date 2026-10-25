"""Formal rechecks of retained repairs precede stale dependency evidence."""
import asyncio
import copy
from pathlib import Path

import pytest
from test_dispatched_repair_order import paused_at_repair
from test_workflow import engine

from auto_agents_2.faults import StateConflict
from auto_agents_2.models import ImplementationResult, RecoveryRequest
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.task_repair_order import dispatched_repair
from auto_agents_2.workspace import tree_hash


async def paused_at_recheck(tmp_path, monkeypatch):
    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    original = agent.invoke

    async def retained(stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == 'implement' and context['task']['id'] == 'C':
            agent.implementations.append('C')
            assert (workspace/'child.py').read_text() == 'value = 2\n'
            return ImplementationResult(summary='The retained fix needs fresh checks, no edits',
                recovery_request=RecoveryRequest(reason='Run the accepted complete compile contract',
                    suggested_action='recheck', evidence_refs=['verification_contract']))
        if stage == 'recovery_diagnosis' and context['failure']['kind'] == 'proposal':
            agent.diagnoses.append(('C', 'proposal'))
            return Diagnosis(action='recheck', task_ids=['C'], evidence_refs=['failure', 'plan'],
                reason='Recheck the retained source under its current unchanged checks')
        return await original(stage, context, schema, workspace, run_id, operation, fresh)

    monkeypatch.setattr(agent, 'invoke', retained)
    resumed = engine(workflow.project, agent)
    observe = resumed._observed_task
    scheduled = []

    async def before_recheck(run_id, task, *args):
        saved = resumed.store.get(run_id, 'develop/task/C')
        permit = resumed.store.get(run_id, saved.get('recovery_permit', ''), {})
        if permit.get('action', {}).get('action') == 'recheck':
            scheduled.append(task.id)
            raise asyncio.CancelledError()
        return await observe(run_id, task, *args)

    monkeypatch.setattr(resumed, '_observed_task', before_recheck)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    saved = resumed.store.get(rid, 'develop/task/C')
    permit = resumed.store.get(rid, saved['recovery_permit'])
    assert saved['phase'] == 'reverify' and not saved.get('patch')
    assert permit['phase'] == 'started' and permit['outcome']['tasks'] == {}
    assert permit['source_hash'] == saved['source_hash'] == tree_hash(Path(saved['workspace']))
    assert agent.implementations == ['P', 'C', 'C', 'C']
    return resumed, rid, plan, base, agent, scheduled


async def test_formal_recheck_runs_before_stale_parent_and_delivers(tmp_path, monkeypatch):
    workflow, rid, plan, base, agent, scheduled = await paused_at_recheck(tmp_path, monkeypatch)
    assert scheduled == ['C']  # Old repair-only scheduler selects P here.
    saved = workflow.store.get(rid, 'develop/task/C')
    permit_key = saved['recovery_permit']
    parent_before = workflow.store.get(rid, 'develop/task/P')
    choice = dispatched_repair(workflow, rid, plan, set(), base)
    assert choice[1]['mode'] == 'execute_dispatched_recheck'
    assert workflow.store.get(rid, 'develop/task/P') == parent_before
    assert workflow.store.get(rid, 'task_state/P')['evidence'] == 'failed'
    with pytest.raises(StateConflict, match='another responsibility'):
        workflow.recovery_policy.task_authorized(rid, saved, plan.tasks[1])
    implementations = list(agent.implementations)
    final = engine(workflow.project, agent)
    result = await final.start()
    assert result['data']['outcome'] == 'DELIVERED'
    assert agent.implementations == implementations
    assert final.store.get(rid, 'rounds/C')['round'] == 3
    assert final.store.get(rid, 'rounds/P')['round'] == 1
    assert final.store.get(rid, 'develop/done') == ['C', 'P']
    assert final.store.get(rid, permit_key)['outcome']['tasks']['C']['status'] == 'verified'
    for task in ['P', 'C']:
        accepted = final.store.get(rid, 'develop/task/' + task)
        assert accepted['review']['approved'] and all(c['exit_code'] == 0 for c in accepted['checks'])
    assert (workflow.project/'child.py').read_text() == 'value = 2\n'


@pytest.mark.parametrize('invalid', ['proposal_only', 'reserved', 'consumed', 'completed_empty',
    'rejected', 'implementing', 'patch', 'stale_revision', 'wrong_observation', 'wrong_source',
    'changed_source', 'missing_workspace', 'wrong_scope', 'stale_plan', 'stale_authority',
    'new_base', 'parent_needs_repair'])
async def test_recheck_priority_requires_exact_readonly_candidate(tmp_path, monkeypatch, invalid):
    workflow, rid, plan, base, agent, _ = await paused_at_recheck(tmp_path, monkeypatch)
    store = workflow.store
    saved = store.get(rid, 'develop/task/C')
    key = saved['recovery_permit']
    permit = store.get(rid, key)
    if invalid == 'proposal_only':
        saved.pop('recovery_permit')
    elif invalid in {'rejected', 'implementing'}:
        saved['phase'] = invalid
    elif invalid == 'patch':
        saved['patch'] = [{'path': 'child.py', 'content': 'value = 99\n'}]
    elif invalid == 'stale_revision':
        saved['revision'] += 1
    elif invalid == 'wrong_observation':
        saved['observation_id'][1] = 'unrelated_failure'
    elif invalid == 'wrong_source':
        saved['source_hash'] = '0'*64
    elif invalid == 'changed_source':
        (Path(saved['workspace'])/'child.py').write_text('value = 100\n')
    elif invalid == 'missing_workspace':
        saved['workspace'] = str(tmp_path/'missing')
    elif invalid == 'new_base':
        integration = store.root/'worktrees'/rid/'integration'
        parent = integration/'parent.py'
        parent.write_text(parent.read_text() + '# new dependency input\n')
        base = workflow.workspaces.checkpoint(integration, f'refs/aa2/{rid}/test-new-base',
                                             'Test new input after recheck dispatch')
    elif invalid == 'parent_needs_repair':
        state = store.get(rid, 'task_state/P')
        store.checkpoint(rid, 'task_state/P', {**state, 'implementation': 'needs_repair'})
    elif invalid == 'reserved':
        permit['phase'] = 'reserved'
    elif invalid == 'consumed':
        permit['outcome']['tasks']['C'] = {'status': 'failed'}
    elif invalid == 'completed_empty':
        permit['phase'] = 'completed'
    elif invalid == 'wrong_scope':
        permit['task_scopes']['C'] = ['parent.py']
    elif invalid == 'stale_plan':
        permit['plan_hash'] = '0'*64
    elif invalid == 'stale_authority':
        permit['authority_sources']['plan'] = 'stale'
    store.checkpoints(rid, {'develop/task/C': saved, key: permit})
    keys = ['develop/done', 'rounds/P', 'rounds/C', 'task_state/P', 'task_state/C', key]
    before = {k: store.get(rid, k) for k in keys}
    assert dispatched_repair(workflow, rid, plan, set(), base) is None
    assert {k: store.get(rid, k) for k in keys} == before


@pytest.mark.parametrize('boundary', ['applied', 'tested', 'accepted', 'integrated'])
async def test_recheck_interruption_resumes_without_implementation(tmp_path, monkeypatch, boundary):
    workflow, rid, plan, base, agent, _ = await paused_at_recheck(tmp_path, monkeypatch)
    resumed = engine(workflow.project, agent)
    key = resumed.store.get(rid, 'develop/task/C')['recovery_permit']
    observed = resumed._observed_task
    checkpoint = resumed.store.checkpoint
    event = resumed.store.event
    interrupted = False

    def pause_checkpoint(run_id, slot, value):
        nonlocal interrupted
        checkpoint(run_id, slot, value)
        if not interrupted and slot == 'develop/task/C' and value.get('phase') == boundary:
            interrupted = True
            raise asyncio.CancelledError()

    async def pause_accepted(*args):
        nonlocal interrupted
        commit = await observed(*args)
        if not interrupted and boundary == 'accepted' and args[1].id == 'C':
            interrupted = True
            raise asyncio.CancelledError()
        return commit

    def pause_integrated(run_id, kind, data):
        nonlocal interrupted
        event(run_id, kind, data)
        if not interrupted and boundary == 'integrated' and kind == 'task.integrated' and data['task'] == 'C':
            interrupted = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(resumed.store, 'checkpoint', pause_checkpoint)
    monkeypatch.setattr(resumed, '_observed_task', pause_accepted)
    monkeypatch.setattr(resumed.store, 'event', pause_integrated)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert interrupted
    prior = copy.deepcopy(resumed.store.get(rid, key))
    if boundary == 'accepted':
        assert prior['phase'] == 'completed'
        assert dispatched_repair(resumed, rid, plan, set(), base)[1]['mode'] == 'integrate_verified_result'
    implementations = list(agent.implementations)
    final = engine(workflow.project, agent)
    result = await final.start()
    assert result['data']['outcome'] == 'DELIVERED'
    assert agent.implementations == implementations
    assert final.store.get(rid, 'rounds/C')['round'] == 3
    assert final.store.get(rid, key)['phase'] == 'completed'
    if boundary in {'accepted', 'integrated'}:
        assert final.store.get(rid, key) == prior


async def test_failed_recheck_preserves_both_failures_without_edit_permission(tmp_path, monkeypatch):
    workflow, rid, plan, base, agent, _ = await paused_at_recheck(tmp_path, monkeypatch)
    resumed = engine(workflow.project, agent)
    key = resumed.store.get(rid, 'develop/task/C')['recovery_permit']
    original = resumed._checks
    original_invoke = agent.invoke

    async def fail_check(run_id, commands, work, environment, task=None):
        rows = await original(run_id, commands, work, environment, task)
        if task and task.id == 'C':
            # Fault injection after real commands to exercise failed result routing.
            rows[-1] = rows[-1].model_copy(update={'exit_code': 1})
        return rows

    async def stop_diagnosis(stage, *args, **kwargs):
        if stage == 'recovery_diagnosis':
            raise asyncio.CancelledError()
        return await original_invoke(stage, *args, **kwargs)

    monkeypatch.setattr(resumed, '_checks', fail_check)
    monkeypatch.setattr(agent, 'invoke', stop_diagnosis)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    saved = resumed.store.get(rid, 'develop/task/C')
    assert saved['phase'] == 'rejected' and not saved.get('commit')
    assert resumed.store.get(rid, key)['outcome']['tasks']['C']['status'] == 'failed'
    assert resumed.store.get(rid, 'develop/done') == []
    assert resumed.store.get(rid, 'task_state/P')['evidence'] == 'failed'
    assert resumed.store.get(rid, 'rounds/C')['round'] == 3
    assert not resumed.recovery_policy.task_authorized(rid, saved, plan.tasks[1])
