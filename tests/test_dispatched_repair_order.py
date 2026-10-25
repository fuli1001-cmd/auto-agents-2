"""An authorized downstream repair must precede stale upstream re-verification."""
import asyncio
import copy
import json
import sys

import pytest
from authority_review_fixtures import authority_review
from evidence_test_fixtures import empty_contract
from test_workflow import CodingAgent, engine

from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, Command, ImplementationResult, PlanResult, TaskSpec
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.planning import ready_batch
from auto_agents_2.recovery import Diagnosis, RepairTarget
from auto_agents_2.task_repair_order import dispatched_repair
from auto_agents_2.util import digest
from auto_agents_2.workspace import git

COMPILE = Command(argv=[sys.executable, '-c',
    "from pathlib import Path; [compile(p.read_text(), str(p), 'exec') for p in Path('.').glob('*.py')]"],
    inputs=[])
CHILD_CHECK = Command(argv=[sys.executable, '-c',
    "from pathlib import Path; assert Path('child.py').read_text()"], inputs=['child.py'])


class RepairOrderAgent(CodingAgent):
    def __init__(self):
        super().__init__()
        self.implementations = []
        self.reviews = []
        self.patch_calls = 0
        self.diagnoses = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if schema is PlanPatch:
            self.patch_calls += 1
            plan = PlanResult.model_validate(context['plan'])
            child = plan.tasks[1].model_copy(update={
                'description': 'Preserve child and run its complete compile check',
                'checks': [CHILD_CHECK, COMPILE]})
            return PlanPatch(base_revision=context['base_revision'], failure_ids=[context['failure']['id']],
                reason='Add the existing final compile command at its repair acceptance gate',
                document='Append the final compile check; keep both tasks and every obligation.',
                upsert_tasks=[child], evidence_contract=empty_contract(context))
        if stage == 'plan':
            return PlanResult(summary='Parent and child', document='Compile all source at final acceptance.',
                evidence_contract=empty_contract(context), tasks=[
                    TaskSpec(id='P', origin_id='P', title='Parent', description='Implement parent',
                        requirement_ids=['R1'], allowed_paths=['parent.py'], acceptance=['sum'], checks=[COMPILE]),
                    TaskSpec(id='C', origin_id='C', title='Child', description='Implement child', depends_on=['P'],
                        requirement_ids=['R1'], allowed_paths=['child.py'], acceptance=['sum'], checks=[CHILD_CHECK])],
                final_checks=[COMPILE], runtime_probes=[Command(argv=[sys.executable, '-c',
                    'from parent import value; assert value == 1'])])
        if stage == 'implement':
            task = context['task']['id']
            self.implementations.append(task)
            if task == 'P':
                return ImplementationResult(summary='Parent', changes=[Change(path='parent.py', content='value = 1\n')])
            # A downstream type/syntax defect invisible to its first command.
            text = 'value = (\n' if self.implementations.count('C') == 1 else 'value = 2\n'
            return ImplementationResult(summary='Child or retained repair', changes=[Change(path='child.py', content=text)])
        if stage == 'recovery_diagnosis':
            f = context['failure']
            self.diagnoses.append((f['task_id'], f['kind']))
            if f['kind'] == 'review':
                return Diagnosis(action='plan_patch', task_ids=['C'], evidence_refs=['failure', 'plan'],
                    reason='Existing child checks cannot produce the required complete compile evidence')
            return Diagnosis(action='repair', task_ids=['C'], evidence_refs=['failure', 'child.py'],
                reason='The child owns the invalid source; preserve the accepted parent implementation',
                repair_targets=[RepairTarget(task_id='C', paths=['child.py'], evidence_refs=['child.py'],
                    reason='Correct the existing child source without changing checks')])
        if context.get('review_kind') == 'implementation':
            task = context['task']['id']
            self.reviews.append((task, context['source_hash']))
            if task == 'C' and self.implementations.count('C') >= 2 and not self.patch_calls:
                return authority_review(context, approved=False, summary='Actual compile evidence is absent')
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


@pytest.mark.asyncio
async def test_real_patch_repair_runs_before_stale_parent_and_delivers(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    request = tmp_path / 'request.md'
    request.write_text('Add two numbers')
    agent = RepairOrderAgent()
    workflow = engine(project, agent)
    result = await workflow.start(str(request))
    assert result['status'] == 'SUCCEEDED' and result['data']['outcome'] == 'DELIVERED'
    rid = result['id']
    assert agent.patch_calls == 1
    assert agent.implementations.count('P') == 1
    assert agent.implementations.count('C') == 3
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'develop/done') == ['C', 'P']
    assert workflow.store.get(rid, 'rounds/P')['round'] == 1
    assert workflow.store.get(rid, 'rounds/C')['round'] == 3
    assert (project / 'child.py').read_text() == 'value = 2\n'
    assert not workflow.store.pending(rid)
    for task in ['P', 'C']:
        saved = workflow.store.get(rid, 'develop/task/' + task)
        assert saved['review']['approved']
        assert all(c['exit_code'] == 0 for c in saved['checks'])


async def paused_at_repair(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    request = tmp_path / 'request.md'
    request.write_text('Add two numbers')
    agent = RepairOrderAgent()
    workflow = engine(project, agent)
    original = workflow._observed_task
    legacy_pass = False

    def first_old_schedule(engine, rid, plan, done, base):
        nonlocal legacy_pass
        if agent.patch_calls and not legacy_pass:
            # Reproduce the already-persisted failure created by the old scheduler,
            # using the real task/check/diagnosis path rather than forging its state.
            legacy_pass = True
            return None
        return dispatched_repair(engine, rid, plan, done, base)

    monkeypatch.setattr('auto_agents_2.task_repair_order.dispatched_repair', first_old_schedule)

    async def before_repair(rid, task, base, plan, context):
        if task.id == 'C' and agent.patch_calls:
            assert workflow.store.get(rid, 'task_state/P')['evidence'] == 'failed'
            assert workflow.store.get(rid, 'task_state/P')['implementation'] == 'accepted'
            raise asyncio.CancelledError()
        return await original(rid, task, base, plan, context)

    monkeypatch.setattr(workflow, '_observed_task', before_repair)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()['id']
    assert workflow.store.current()['status'] == 'PAUSED'
    assert agent.implementations == ['P', 'C', 'C']
    plan = PlanResult.model_validate(workflow.store.get(rid, 'plan'))
    integration = workflow.store.root / 'worktrees' / rid / 'integration'
    return workflow, rid, plan, git(integration, 'rev-parse', 'HEAD'), agent


async def test_dispatched_repair_resume_retains_budget_history_and_checks(tmp_path, monkeypatch):
    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    old = copy.deepcopy(workflow.store.get(rid, 'develop/task/P'))
    permit_key = workflow.store.get(rid, 'develop/task/C')['recovery_permit']
    permit = workflow.store.get(rid, permit_key)
    assert permit['phase'] == 'started' and permit['outcome']['tasks'] == {}
    selection = dispatched_repair(workflow, rid, plan, set(), base)
    assert selection and selection[0].id == 'C'
    assert workflow.store.get(rid, 'develop/task/P') == old  # Input proof is not acceptance.
    resumed = engine(workflow.project, agent)
    result = await resumed.start()
    assert result['data']['outcome'] == 'DELIVERED'
    assert agent.implementations == ['P', 'C', 'C', 'C']
    assert resumed.store.get(rid, 'rounds/P')['round'] == 1
    assert resumed.store.get(rid, 'rounds/C')['round'] == 3
    outcome = resumed.store.get(rid, permit_key)
    assert outcome['phase'] == 'completed' and outcome['outcome']['tasks']['C']['status'] == 'verified'
    assert outcome['action'] == permit['action'] and outcome['authority_sources'] == permit['authority_sources']
    assert all(c['exit_code'] == 0 for c in resumed.store.get(rid, 'develop/task/P')['checks'])


async def test_pause_after_repair_integration_does_not_repeat_repair_or_accept_old_parent(tmp_path, monkeypatch):
    project = tmp_path/'project'
    project.mkdir()
    request = tmp_path/'request.md'
    request.write_text('Add two numbers')
    agent = RepairOrderAgent()
    workflow = engine(project, agent)
    original = workflow.store.event

    def interrupt(rid, kind, data):
        original(rid, kind, data)
        if kind == 'task.integrated' and data['task'] == 'C' and agent.patch_calls:
            assert workflow.store.get(rid, 'develop/done') == ['C']
            assert workflow.store.get(rid, 'task_state/P')['evidence'] == 'stale'
            raise asyncio.CancelledError()

    monkeypatch.setattr(workflow.store, 'event', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()['id']
    implementations = list(agent.implementations)
    resumed = engine(project, agent)
    result = await resumed.start()
    assert result['data']['outcome'] == 'DELIVERED'
    assert agent.implementations == implementations == ['P', 'C', 'C', 'C']
    assert resumed.store.get(rid, 'develop/done') == ['C', 'P']
    assert resumed.store.get(rid, 'task_state/P')['evidence'] == 'valid'


@pytest.mark.parametrize('invalid', ['no_permit', 'proposal', 'reserved', 'completed', 'consumed',
    'wrong_action', 'wrong_scope', 'not_target', 'stale_authority', 'unaccepted', 'needs_repair',
    'missing_history', 'missing_receipt', 'source_mismatch', 'wrong_owner', 'unintegrated',
    'changed_task', 'changed_global', 'missing_plan', 'unbound_caller_plan', 'changed_user_input'])
async def test_repair_priority_requires_current_permit_and_accepted_input(tmp_path, monkeypatch, invalid):
    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    store = workflow.store
    child = store.get(rid, 'develop/task/C')
    permit_key = child['recovery_permit']
    permit = store.get(rid, permit_key)
    if invalid in {'no_permit', 'proposal'}:
        child.pop('recovery_permit')
        if invalid == 'proposal':
            child['feedback'] = {'controller_recovery': {'action': 'repair', 'task_ids': ['C']}}
        store.checkpoint(rid, 'develop/task/C', child)
    elif invalid in {'reserved', 'completed', 'consumed', 'wrong_action', 'wrong_scope', 'not_target', 'stale_authority'}:
        if invalid in {'reserved', 'completed'}:
            permit['phase'] = invalid
        elif invalid == 'consumed':
            permit['outcome']['tasks']['C'] = {'status': 'failed'}
        elif invalid == 'wrong_action':
            permit['action']['action'] = 'recheck'
        elif invalid == 'wrong_scope':
            permit['task_scopes']['C'] = ['parent.py']
        elif invalid == 'not_target':
            permit['action']['task_ids'] = ['P']
        else:
            permit['authority_sources']['plan'] = 'stale'
        store.checkpoint(rid, permit_key, permit)
    elif invalid in {'unaccepted', 'needs_repair'}:
        state = store.get(rid, 'task_state/P')
        state['implementation'] = 'pending' if invalid == 'unaccepted' else 'needs_repair'
        store.checkpoint(rid, 'task_state/P', state)
    elif invalid == 'changed_user_input':
        store.checkpoint(rid, 'capture', {'hash': 'different-user-input'})
        permit['authority_sources'] = workflow.authority.identity(rid)
        store.checkpoint(rid, permit_key, permit)
    elif invalid in {'changed_task', 'changed_global', 'unbound_caller_plan'}:
        plan = plan.model_copy(deep=True)
        if invalid == 'changed_task':
            plan.tasks[0].acceptance.append('a different contract')
        else:
            plan.requirements_file = 'new-requirements.txt'
        if invalid != 'unbound_caller_plan':
            store.checkpoint(rid, 'plan', plan.model_dump())
            permit['plan_hash'] = digest(plan.model_dump())
            permit['authority_sources'] = workflow.authority.identity(rid)
            store.checkpoint(rid, permit_key, permit)
    else:
        with store.connect() as db:
            rows = [(k, json.loads(v)) for k,v in db.execute('SELECT key,data FROM checkpoints WHERE run_id=?', (rid,))]
        for key, value in rows:
            if invalid == 'missing_receipt' and key.startswith('authority/reviews/') and value.get('operation') == 'develop/task/P/review':
                store.checkpoint(rid, key, None)
            if invalid == 'wrong_owner' and key.startswith('authority/reviews/') and value.get('operation') == 'develop/task/P/review':
                store.checkpoint(rid, key, {**value, 'operation': 'develop/task/C/review'})
            if key.startswith('plan_revision/history/'):
                if invalid == 'missing_plan':
                    value['plan']['document'] += '\nUnbound historical text'
                if invalid == 'missing_history':
                    value['tasks'].pop('develop/task/P', None)
                elif invalid in {'source_mismatch', 'unintegrated'}:
                    prior = value['tasks'].get('develop/task/P', {})
                    if prior.get('commit'):
                        prior['source_hash' if invalid == 'source_mismatch' else 'commit'] = '0'*64
                store.checkpoint(rid, key, value)
            if key.startswith('authority/task-history/P/'):
                if invalid in {'missing_history', 'source_mismatch', 'unintegrated'}:
                    store.checkpoint(rid, key, {})
        parent = store.get(rid, 'develop/task/P')
        if invalid == 'missing_history':
            # A model-supplied history claim cannot replace controller-owned slots.
            parent['feedback']['claimed_acceptance'] = parent.pop('previous_evidence', {})
            store.checkpoint(rid, 'develop/task/P', parent)
    before = {key: store.get(rid, key) for key in ['develop/done', 'rounds/P', 'rounds/C', 'task_state/P', permit_key]}
    if invalid == 'stale_authority':
        with pytest.raises(StateConflict):
            dispatched_repair(workflow, rid, plan, set(), base)
    else:
        assert dispatched_repair(workflow, rid, plan, set(), base) is None
    assert {key: store.get(rid, key) for key in before} == before


def test_normal_readiness_waits_for_all_ancestors_after_repair():
    p = TaskSpec(id='P', title='P', description='P', allowed_paths=['p'], acceptance=['P'],
                 requirement_ids=['R1'], checks=[COMPILE])
    c = p.model_copy(update={'id': 'C', 'depends_on': ['P'], 'allowed_paths': ['c']})
    d = p.model_copy(update={'id': 'D', 'depends_on': ['C'], 'allowed_paths': ['d']})
    assert [t.id for t in ready_batch([d,c,p], {'C'}, 2)] == ['P']
    assert [t.id for t in ready_batch([d,c,p], {'C','P'}, 2)] == ['D']


async def test_failed_prioritized_repair_does_not_clear_parent_or_accept_candidate(tmp_path, monkeypatch):
    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    original = agent.invoke

    async def failing(stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == 'recovery_diagnosis' and agent.implementations.count('C') == 3:
            raise asyncio.CancelledError()
        result = await original(stage, context, schema, workspace, run_id, operation, fresh)
        if stage == 'implement' and context['task']['id'] == 'C':
            result.changes[0].content = 'still_invalid = (\n'
        return result

    monkeypatch.setattr(agent, 'invoke', failing)
    resumed = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert resumed.store.get(rid, 'develop/done') == []
    assert resumed.store.get(rid, 'task_state/P')['evidence'] == 'failed'
    child = resumed.store.get(rid, 'develop/task/C')
    assert child['phase'] == 'rejected' and not child.get('commit')
    assert any(c['exit_code'] for c in child['checks'])
    permit = resumed.store.get(rid, child['recovery_permit'])
    assert permit['outcome']['tasks']['C']['status'] == 'failed'
    assert resumed.store.get(rid, 'rounds/P')['round'] == 1
    assert resumed.store.get(rid, 'rounds/C')['round'] == 3


@pytest.mark.parametrize('corrupt', [None, 'result_commit', 'result_source', 'result_evidence',
                                  'outcome_hash', 'review', 'accepted_state'])
async def test_pause_after_acceptance_before_integration_uses_only_verified_result(tmp_path, monkeypatch, corrupt):
    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    resumed = engine(workflow.project, agent)
    original = resumed._observed_task

    async def after_acceptance(*args):
        commit = await original(*args)
        if args[1].id == 'C':
            raise asyncio.CancelledError()
        return commit

    monkeypatch.setattr(resumed, '_observed_task', after_acceptance)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    saved = resumed.store.get(rid, 'develop/task/C')
    permit = resumed.store.get(rid, saved['recovery_permit'])
    assert saved['commit'] and permit['phase'] == 'completed'
    assert resumed.store.get(rid, 'develop/done') == []
    assert resumed.store.get(rid, 'task_state/P')['evidence'] == 'failed'
    if corrupt:
        if corrupt.startswith('result_'):
            field = {'result_commit': 'commit', 'result_source': 'source_hash', 'result_evidence': 'evidence'}[corrupt]
            permit['outcome']['tasks']['C'][field] = '0'*64
            permit['outcome_hash'] = digest(permit['outcome'])
            # Production Store already forbids rewriting a completed result.
            # Exercise that guard, then fault-inject the read boundary separately.
            with pytest.raises(StateConflict, match='cannot be overwritten'):
                resumed.store.checkpoint(rid, saved['recovery_permit'], permit)
        elif corrupt == 'outcome_hash':
            permit['outcome_hash'] = '0'*64
        elif corrupt == 'review':
            saved['review']['approved'] = False
            resumed.store.checkpoint(rid, 'develop/task/C', saved)
        else:
            state = resumed.store.get(rid, 'task_state/C')
            state['evidence'] = 'stale'
            resumed.store.checkpoint(rid, 'task_state/C', state)
        if corrupt.startswith('result_') or corrupt == 'outcome_hash':
            read = resumed.store.get
            assert read(rid, saved['recovery_permit']) != permit

            def corrupted_read(run_id, key, default=None):
                if run_id == rid and key == saved['recovery_permit']:
                    return copy.deepcopy(permit)
                return read(run_id, key, default)

            monkeypatch.setattr(resumed.store, 'get', corrupted_read)
        assert dispatched_repair(resumed, rid, plan, set(), base) is None
    else:
        choice = dispatched_repair(resumed, rid, plan, set(), base)
        assert choice[1]['mode'] == 'integrate_verified_result'
        final = engine(workflow.project, agent)
        result = await final.start()
        assert result['data']['outcome'] == 'DELIVERED'
        assert agent.implementations == ['P', 'C', 'C', 'C']
        assert final.store.get(rid, saved['recovery_permit']) == permit
        assert final.store.get(rid, 'rounds/C')['round'] == 3
        assert final.store.get(rid, 'develop/done') == ['C', 'P']


@pytest.mark.parametrize('boundary', ['task', 'state', 'permit', 'cycle', 'consumer'])
async def test_task_acceptance_and_recovery_result_rollback_together(tmp_path, monkeypatch, boundary):
    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    resumed = engine(workflow.project, agent)
    permit_key = resumed.store.get(rid, 'develop/task/C')['recovery_permit']
    before = resumed.store.get(rid, permit_key)
    target = {'task': 'develop/task/C', 'state': 'task_state/C', 'permit': permit_key,
              'cycle': 'recovery/cycles/' + before['cycle_id'],
              'consumer': 'develop/review-evidence/C'}[boundary]
    original = resumed.store._put
    accepting = interrupted = False
    prior_state = None

    def fail_write(db, run_id, key, value):
        nonlocal accepting, interrupted, prior_state
        if key == 'develop/task/C' and value.get('commit') and agent.patch_calls:
            accepting = True
            prior_state = resumed.store.get(rid, 'task_state/C')
        original(db, run_id, key, value)
        if accepting and not interrupted and key == target:
            interrupted = True
            # A persistence failure/process interruption inside this transaction,
            # not a claim that cooperative cancellation occurs between sync calls.
            raise asyncio.CancelledError()

    monkeypatch.setattr(resumed.store, '_put', fail_write)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert interrupted
    saved = resumed.store.get(rid, 'develop/task/C')
    assert not saved.get('commit')
    assert prior_state['implementation'] == 'in_progress'
    assert resumed.store.get(rid, 'task_state/C') == prior_state
    assert resumed.store.get(rid, permit_key) == before
    assert resumed.store.get(rid, 'develop/review-evidence/C')['phase'] == 'pending'
    assert resumed.store.get(rid, 'develop/done') == []
    assert resumed.store.get(rid, 'task_state/P')['evidence'] == 'failed'
    implementations = list(agent.implementations)
    final = engine(workflow.project, agent)
    result = await final.start()
    assert result['data']['outcome'] == 'DELIVERED'
    assert agent.implementations == implementations == ['P', 'C', 'C', 'C']
    completed = final.store.get(rid, permit_key)
    accepted = final.store.get(rid, 'develop/task/C')
    assert completed['phase'] == 'completed'
    assert completed['outcome']['tasks']['C'] == {
        'status': 'verified', 'commit': accepted['commit'],
        'source_hash': accepted['source_hash'], 'evidence': 'develop/task/C'}
    assert completed['outcome_hash'] == digest(completed['outcome'])
    assert final.store.get(rid, 'rounds/C')['round'] == 3
    assert final.store.get(rid, 'develop/done') == ['C', 'P']


async def test_legacy_accepted_commit_without_recovery_result_is_not_execution(tmp_path, monkeypatch):
    from auto_agents_2.review_evidence import ReviewEvidence

    workflow, rid, plan, base, agent = await paused_at_repair(tmp_path, monkeypatch)
    resumed = engine(workflow.project, agent)
    permit_key = resumed.store.get(rid, 'develop/task/C')['recovery_permit']
    permit = resumed.store.get(rid, permit_key)
    cycle_key = 'recovery/cycles/' + permit['cycle_id']
    original = ReviewEvidence.complete

    def old_split_write(self, run_id, key, checks, *, updates=None):
        if (key == 'develop/review-evidence/C' and agent.patch_calls
                and (updates or {}).get('develop/task/C', {}).get('commit')):
            # Reproduce the old two-transaction checkpoint using real acceptance
            # and review records in this isolated project, before the outcome write.
            original(self, run_id, key, checks, updates={
                k: v for k, v in updates.items() if k not in {permit_key, cycle_key}})
            raise asyncio.CancelledError()
        return original(self, run_id, key, checks, updates=updates)

    with monkeypatch.context() as patch:
        patch.setattr(ReviewEvidence, 'complete', old_split_write)
        with pytest.raises(asyncio.CancelledError):
            await resumed.start()
    saved = resumed.store.get(rid, 'develop/task/C')
    assert saved['commit'] and resumed.store.get(rid, permit_key) == permit
    assert permit['outcome']['tasks'] == {}
    keys = ['develop/task/C', 'task_state/C', 'develop/done', 'rounds/C', permit_key]
    before = {key: resumed.store.get(rid, key) for key in keys}
    assert dispatched_repair(resumed, rid, plan, set(), base) is None
    child = next(t for t in plan.tasks if t.id == 'C')
    with pytest.raises(StateConflict, match='recorded recovery outcome'):
        await resumed._task(rid, child, base, plan, {})
    assert {key: resumed.store.get(rid, key) for key in keys} == before
