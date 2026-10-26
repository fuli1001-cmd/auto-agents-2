"""Identical facts cannot fund unlimited read-only diagnoses or timing amendments."""
import copy

import pytest
from test_decisions import ApprovingAgent
from test_diagnosis_decisions import proposal_failure
from test_execution_stage_contract import TimingAgent, setup_timing
from test_recovery_policy import ready
from test_workflow import engine

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import RecoveryPathExhausted
from auto_agents_2.recovery import Diagnosis, adjudicate
from auto_agents_2.workspace import tree_hash


class LoopAgent(ApprovingAgent):
    def __init__(self, alternative='repair'):
        super().__init__()
        self.alternative = alternative
        self.diagnoses = 0
        self.options = []

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            self.diagnoses += 1
            return Diagnosis(action='diagnose', task_ids=['T3'], evidence_refs=['failure.evidence'],
                             reason=f'Different wording {self.diagnoses}, still awaiting the same future receipt.')
        if stage == 'decision':
            request = context['decision_request']
            choices = request['scope']['candidates']
            self.options.append(set(choices))
            if 'diagnose' in choices:
                selected = 'diagnose'
            elif self.alternative == 'ask_user':
                return DecisionResult(action='ask_user', reason='No useful permitted action could be identified.')
            else:
                selected = self.alternative
            return DecisionResult(action='approve', selected_action=selected, evidence_refs=['failure'],
                reason='Select a supplied executable action', expected_result='An actual observation or candidate')
        return await super().invoke(stage, context, schema, *args, **kwargs)


async def test_two_inconclusive_diagnoses_exclude_only_that_path_and_preserve_counts(tmp_path):
    agent = LoopAgent()
    workflow, rid, _, _, record = ready(tmp_path, agent, proposal_failure())
    workflow.store.checkpoint(rid, 'recovery/budgets/diagnosis/T3', {'round': 20})
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    assert agent.diagnoses == 2
    assert 'diagnose' in agent.options[0] and 'diagnose' not in agent.options[-1]
    assert {'repair', 'plan_patch'} <= agent.options[-1]
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round'] == 22
    assert workflow.store.get(rid, f'recovery/completed/{record.id}/1')['decision']['action'] == 'repair'
    assert not workflow.store.pending(rid) and not workflow.store.get(rid, 'develop/done')


async def test_unknown_blocker_stops_without_fabricating_a_human_permission_question(tmp_path):
    agent = LoopAgent('ask_user')
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    await workflow.recovery.resume(rid)
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    assert agent.diagnoses == 2 and not workflow.store.pending(rid)
    workflow = engine(tmp_path, agent)
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    assert agent.diagnoses == 2 and not workflow.store.pending(rid)


def test_progress_identity_ignores_wording_but_changes_for_actual_material(tmp_path):
    workflow, rid, work, plan, record = ready(tmp_path)
    guard = workflow.recovery_policy.progress
    context = guard.inputs(rid, record, work, plan)
    original = guard.basis(context, tree_hash(work), 'diagnose')
    changed = copy.deepcopy(context)
    changed['failure']['proposal'] = {'summary': 'Another wording'}
    changed['failure']['observation_id'] = 'new-attempt-not-new-proof'
    changed['failure']['evidence_refs'] = ['another citation']
    assert guard.basis(changed, tree_hash(work), 'diagnose') == original
    changed['source_history']['artifact']['hash'] = 'a' * 64
    assert guard.basis(changed, tree_hash(work), 'diagnose') != original
    for operation in ['one', 'one', 'two']:
        guard.record(rid, 'T3', 'diagnose', original, operation, {'status': 'inconclusive'})
    assert len(guard.exclusions(rid, record, work, plan)['diagnose']['operations']) == 2
    assert 'diagnose' not in workflow.recovery_policy.diagnosis_actions(rid, record, work, plan)
    changed_plan = plan.model_copy(deep=True)
    changed_plan.tasks[2].description += ' Evidence-backed changed contract'
    assert guard.exclusions(rid, record, work, changed_plan) == {}


async def test_reserved_diagnosis_permit_remains_bound_and_replayed(tmp_path):
    workflow, rid, work, plan, record = ready(tmp_path)
    policy = workflow.recovery_policy
    active = workflow.store.get(rid, 'recovery/active')
    first, key = await policy.authorize(rid, record, active, work, plan, adjudicate(record), operation='reserved')
    context = policy.progress.inputs(rid, record, work, plan)
    basis = policy.progress.basis(context, tree_hash(work), 'diagnose')
    for operation in ['one', 'two']:
        policy.progress.record(rid, 'T3', 'diagnose', basis, operation, {'status': 'inconclusive'})
    again, again_key = await policy.authorize(rid, record, active, work, plan, adjudicate(record), operation='reserved')
    assert again == first and again_key == key


async def test_two_rejected_timing_amendments_exclude_repetition_not_other_repairs(tmp_path):
    agent = TimingAgent('reject')
    workflow, rid, work, plan, record = setup_timing(tmp_path, agent)
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    exclusions = workflow.recovery_policy.progress.exclusions(rid, record, work, plan)
    assert 'plan_patch' in exclusions
    assert len(exclusions['plan_patch']['operations']) == 2
    choices = workflow.recovery_policy.diagnosis_actions(rid, record, work, plan)
    # Both rejected amendments followed an actual completed diagnosis on the same facts.
    assert 'plan_patch' not in choices and 'diagnose' not in choices
    assert {'recheck', 'repair'} <= choices.keys()
    assert len(exclusions['diagnose']['operations']) == 2
    assert workflow.store.get(rid, 'plan') == plan.model_dump()
    assert not workflow.store.pending(rid) and not workflow.store.get(rid, 'develop/done')


async def test_engine_pauses_exhausted_path_and_restart_does_not_call_provider(tmp_path):
    agent = LoopAgent('ask_user')
    workflow, rid, work, _, _ = ready(tmp_path, agent, proposal_failure())
    source = tree_hash(work)
    result = await workflow.start(run_id=rid)
    assert result['status'] == 'PAUSED'
    assert result['data']['last_error'] == 'RECOVERY_PATH_EXHAUSTED'
    assert agent.diagnoses == 2 and tree_hash(work) == source
    calls = len(agent.options)
    result = await workflow.start(run_id=rid)
    assert result['status'] == 'PAUSED'
    assert agent.diagnoses == 2 and len(agent.options) == calls
    assert not workflow.store.pending(rid)


async def test_real_new_archive_allows_fresh_selection_without_reusing_old_fallback(tmp_path):
    from auto_agents_2.models import Change, ImplementationResult

    agent = LoopAgent('ask_user')
    workflow, rid, work, plan, record = ready(tmp_path, agent, proposal_failure())
    await workflow.recovery.resume(rid)
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    source = tree_hash(work)
    workflow.store.checkpoint(rid, 'recovery/gate-history/99', {'checkpoints': {
        'plan': plan.model_dump(), 'develop/task/T3': {'phase': 'rejected',
            'implementation': ImplementationResult(summary='A newly available actual archive',
                changes=[Change(path='part3.py', content='historical_assertion = True\n')]).model_dump()}}})
    assert tree_hash(work) == source
    agent.alternative = 'repair'
    for _ in range(3):
        await workflow.recovery.resume(rid)
        if workflow.store.get(rid, 'recovery/active') is None:
            break
    assert agent.diagnoses == 4
    assert workflow.store.get(rid, f'recovery/completed/{record.id}/1')['decision']['action'] == 'repair'
    assert not workflow.store.pending(rid)


def test_stopped_selection_cannot_replace_an_existing_user_question(tmp_path):
    from auto_agents_2.faults import StateConflict
    from auto_agents_2.interaction import choice_spec
    from auto_agents_2.util import digest

    workflow, rid, work, plan, record = ready(tmp_path)
    guard = workflow.recovery_policy.progress
    cycle = workflow.recovery_policy.cycle_id(record, workflow.store.get(rid, 'recovery/active'))
    operation = 'fixture'
    with pytest.raises(RecoveryPathExhausted):
        guard.stop(rid, record, work, plan, cycle, operation, {'diagnose': {}}, 'No useful action')
    key = 'recovery-cycle/' + digest([cycle, operation])
    workflow.store.request(rid, key, 'Existing permission boundary', 'fingerprint',
        choice_spec(['continue', 'pause'], ['continue', 'pause'], yes_no=True))
    with pytest.raises(StateConflict):
        guard.resume_operation(rid, record, work, plan, cycle, operation)
    assert len(workflow.store.pending(rid)) == 1


async def test_disabled_automation_keeps_human_authorization_for_an_alternative(tmp_path):
    from test_decisions import configure

    workflow, rid, work, plan, record = ready(tmp_path)
    policy = workflow.recovery_policy
    context = policy.progress.inputs(rid, record, work, plan)
    basis = policy.progress.basis(context, tree_hash(work), 'diagnose')
    for operation in ['one', 'two']:
        policy.progress.record(rid, record.origin_id, 'diagnose', basis, operation, {'status': 'inconclusive'})
    configure(workflow, automation={'enabled': False})
    questions = []
    async def approve(question):
        questions.append(question)
        return 'y'
    workflow.question_handler = approve
    active = workflow.store.get(rid, 'recovery/active')
    suggested = adjudicate(record).model_copy(update={'action': 'repair'})
    decision, key = await policy.authorize(rid, record, active, work, plan, suggested,
        operation='alternative', force_decision=True)
    assert decision.action == 'repair' and len(questions) == 1
    assert workflow.store.get(rid, key)['actor'] == 'human'
    assert not workflow.agent.calls
    assert 'diagnose' not in policy.diagnosis_actions(rid, record, work, plan)
