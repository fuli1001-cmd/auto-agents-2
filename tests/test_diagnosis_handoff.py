"""A suggested action is not execution progress or a replacement for the authorized inquiry."""
import asyncio
import copy

import pytest
from test_diagnosis_decisions import proposal_failure
from test_recovery_no_progress import LoopAgent
from test_recovery_policy import ready

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.decisions import DecisionResult
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


class RecheckDiagnosisAgent(LoopAgent):
    def __init__(self, alternative='repair'):
        super().__init__(alternative)
        self.contexts = []

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            self.diagnoses += 1
            self.contexts.append(copy.deepcopy(context))
            return Diagnosis(action='recheck', task_ids=['T3'], evidence_refs=['failure.evidence'],
                             reason='Inspect the same source again; no repair has been executed.')
        if stage == 'decision':
            choices = context['decision_request']['scope']['candidates']
            self.options.append(set(choices))
            selected = 'diagnose' if 'diagnose' in choices else self.alternative
            if selected == 'ask_user':
                return DecisionResult(action='ask_user', reason='No useful remaining action.')
            return DecisionResult(action='approve', selected_action=selected,
                evidence_refs=['failure.evidence'], reason='Compare the two retained installation logs.',
                expected_result='Identify the actual failed edge; do not infer a version conflict.')
        return await super().invoke(stage, context, schema, *args, **kwargs)


async def initial_recheck(tmp_path, agent=None, logs=('first installation log', 'second installation log')):
    agent = agent or RecheckDiagnosisAgent()
    workflow, rid, work, contract, record = ready(tmp_path, agent, proposal_failure())
    log = workflow.artifacts.put(logs[0], 'check_log')
    record = record.model_copy(deep=True)
    record.evidence['environment_failure'] = {'logs': [log], 'runtime_identity': {'node': '22'},
                                              'npm_debug': {'status': 'complete'}}
    # Replace the unexecuted fixture admission with the first actual observation.
    workflow.store.checkpoint(rid, 'recovery/active', None)
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'recovery/active') is None
    second = workflow.artifacts.put(logs[1], 'check_log')
    record.evidence['environment_failure']['logs'] = [second]
    workflow.recovery.submit(rid, record)
    return workflow, rid, work, contract, record, log, second


async def test_recheck_advice_then_diagnose_decision_counts_actual_completed_diagnoses(tmp_path):
    workflow, rid, work, _, record, _, _ = await initial_recheck(tmp_path)
    source = tree_hash(work)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'recovery/active')['selection']['action'] == 'diagnose'
    await workflow.recovery.resume(rid)
    assert 'diagnose' not in workflow.agent.options[-1]
    assert workflow.store.get(rid, 'recovery/active') is None
    history = workflow.store.recovery_history(rid, 'T3')
    assert history['recent'][-1]['action']['action'] == 'repair'
    assert workflow.agent.diagnoses == 3
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round'] == 3
    assert not workflow.store.get(rid, 'develop/done') and tree_hash(work) == source


async def test_actual_engine_handoff_contains_directed_goal_and_prior_log_material(tmp_path):
    workflow, rid, work, _, _, first, second = await initial_recheck(tmp_path)
    await workflow.recovery.resume(rid)
    permit = workflow.store.get(rid, 'recovery/active')['selection_permit']
    await workflow.recovery.resume(rid)
    context = workflow.agent.contexts[-1]
    request = context['diagnosis_request']
    assert request['permit'] == permit
    assert request['proposal']['expected_result'] == 'Identify the actual failed edge; do not infer a version conflict.'
    assert request['proposal']['reason'] == 'Compare the two retained installation logs.'
    assert context['history']['recent']
    observations = context['previous_failure_observations']['recent']
    assert any(first['hash'] == log['hash'] for row in observations
               for log in row['failure']['evidence'].get('environment_failure', {}).get('logs', []))
    prepared = ContextPlanner(workflow.artifacts).prepare('recovery_diagnosis', context, work)
    assert 'history' in prepared.payload and 'diagnosis_request' in prepared.payload
    material = str(prepared.payload)
    assert first['hash'] in material and second['hash'] in material
    assert workflow.artifacts.read(first) == 'first installation log'
    assert workflow.artifacts.read(second) == 'second installation log'


@pytest.mark.parametrize('replacement', ['repair', 'ask_user'])
async def test_same_facts_stop_or_repair_instead_of_buying_more_diagnoses(tmp_path, replacement):
    from auto_agents_2.errors import RecoveryPathExhausted

    workflow, rid, _, _, _, _, _ = await initial_recheck(tmp_path, RecheckDiagnosisAgent(replacement))
    await workflow.recovery.resume(rid)
    if replacement == 'ask_user':
        with pytest.raises(RecoveryPathExhausted):
            await workflow.recovery.resume(rid)
        with pytest.raises(RecoveryPathExhausted):
            await workflow.recovery.resume(rid)
    else:
        await workflow.recovery.resume(rid)
    assert workflow.agent.diagnoses == 3 and not workflow.store.pending(rid)


async def test_new_actual_log_gets_new_basis_without_resetting_consumption(tmp_path):
    workflow, rid, _, _, failure, _, _ = await initial_recheck(tmp_path)
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'recovery/active') is None
    before = workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round']
    failure = failure.model_copy(deep=True)
    third = workflow.artifacts.put('genuinely new installation observation', 'check_log')
    failure.evidence['environment_failure']['logs'] = [third]
    workflow.recovery.submit(rid, failure)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'recovery/active')['selection']['action'] == 'diagnose'
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'recovery/active') is None
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round'] == before + 2
    assert workflow.agent.diagnoses == 5
    with workflow.store.connect() as db:
        ledgers = list(db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/no-progress/diagnose/%'", (rid,)))
    import json
    assert sorted(len(json.loads(row[0])['operations']) for row in ledgers) == [1, 2, 2]


@pytest.mark.parametrize('point', ['before_complete', 'after_complete', 'after_ledger'])
async def test_new_completion_ledger_survives_interrupt_without_duplicate_call(tmp_path, monkeypatch, point):
    from auto_agents_2.errors import PostCommitError

    agent = RecheckDiagnosisAgent()
    workflow, rid, _, _, _ = ready(tmp_path, agent, proposal_failure())
    complete = workflow.recovery_policy.complete
    record = workflow.recovery_policy.progress.record
    def interrupt_complete(rid, key, outcome):
        if outcome.get('status') == 'validated':
            if point == 'before_complete':
                raise PostCommitError('after enrollment')
            complete(rid, key, outcome)
            raise PostCommitError('after completion')
        return complete(rid, key, outcome)
    def interrupt_record(*args, **kwargs):
        record(*args, **kwargs)
        raise PostCommitError('after ledger')
    with monkeypatch.context() as patch:
        if point == 'after_ledger':
            patch.setattr(workflow.recovery_policy.progress, 'record', interrupt_record)
        else:
            patch.setattr(workflow.recovery_policy, 'complete', interrupt_complete)
        with pytest.raises(PostCommitError):
            await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    operation = f"recovery/diagnose/{active['failure']['id']}/{active['diagnosis_round']}"
    basis = workflow.store.get(rid, 'recovery/diagnosis-progress-basis/' + operation)
    await workflow.recovery.resume(rid)
    ledger = workflow.store.get(rid, 'recovery/no-progress/diagnose/' + basis)
    assert list(ledger['operations']) == [operation]
    assert agent.diagnoses == 1 and workflow.store.get(rid, 'recovery/active') is None
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3') == {'round': 1}
    events = [e for e in workflow.store.events(rid) if e['kind'] == 'recovery.no_progress']
    assert len(events) == 1


async def test_legacy_completed_recheck_is_not_retroactively_counted(tmp_path, monkeypatch):
    from auto_agents_2.errors import PostCommitError

    workflow, rid, _, _, _ = ready(tmp_path, RecheckDiagnosisAgent(), proposal_failure())
    authorize = workflow.recovery_policy.authorize
    async def interrupt(rid, failure, active, work, plan, suggested, **kwargs):
        if kwargs['operation'].startswith('dispatch/'):
            raise PostCommitError('old completion before dispatch')
        return await authorize(rid, failure, active, work, plan, suggested, **kwargs)
    with monkeypatch.context() as patch:
        # Exercise the old producer: no new fields or enrollment existed.
        patch.setattr('auto_agents_2.recovery_controller.diagnosis_handoff', lambda *args: {})
        patch.setattr(workflow.recovery_policy.progress, 'enroll_diagnosis', lambda *args, **kw: None)
        patch.setattr(workflow.recovery_policy, 'authorize', interrupt)
        with pytest.raises(PostCommitError):
            await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    receipt = workflow.store.get(rid, active['diagnosis_permit'])
    assert receipt['phase'] == 'completed'
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, active['diagnosis_permit']) == receipt
    assert workflow.agent.diagnoses == 1
    assert not [e for e in workflow.store.events(rid) if e['kind'] == 'recovery.no_progress']


@pytest.mark.parametrize('legacy', [False, True])
async def test_interrupted_frozen_input_keeps_original_handoff_and_counts_once(tmp_path, monkeypatch, legacy):
    workflow, rid, _, _, _ = ready(tmp_path, RecheckDiagnosisAgent(), proposal_failure())
    invoke = workflow.agent.invoke
    async def cancel(stage, *args, **kwargs):
        if stage == 'recovery_diagnosis':
            raise asyncio.CancelledError()
        return await invoke(stage, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(workflow.agent, 'invoke', cancel)
        if legacy:
            patch.setattr('auto_agents_2.recovery_controller.diagnosis_handoff', lambda *args: {})
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    operation = f"recovery/diagnose/{active['failure']['id']}/{active['diagnosis_round']}"
    key = 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', operation])
    frozen = workflow.store.get(rid, key)
    assert ('diagnosis_request' in frozen['context']) != legacy
    assert not workflow.store.get(rid, 'recovery/diagnosis-progress-enrollment/' + operation)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, key) == frozen
    assert ('diagnosis_request' in workflow.agent.contexts[-1]) != legacy
    assert workflow.agent.diagnoses == 1
    assert len([e for e in workflow.store.events(rid) if e['kind'] == 'recovery.no_progress']) == 1


def test_historical_scope_and_proposal_hashes_do_not_expand_materials(tmp_path):
    from auto_agents_2.evidence_refs import controller_context_catalog
    from auto_agents_2.recovery_handoff import diagnosis_handoff

    workflow, rid, work, _, failure = ready(tmp_path)
    log = workflow.artifacts.put('admitted old log', 'check_log')
    fake = {'hash': 'f' * 64, 'kind': 'document', 'bytes': 99}
    historical = failure.model_dump() | {'observation_id': 'old', 'proposal': {'attachment': fake}}
    historical['evidence'] = {'environment_failure': {'logs': [log]}, 'model_attachment': fake}
    prefix = 'recovery/failures/' + failure.id + '/'
    workflow.store.checkpoints(rid, {
        prefix+'accepted': historical,
        prefix+'wrong-task': historical | {'task_id': 'T4'},
        prefix+'wrong-owner': historical | {'origin_id': 'T4'},
        prefix+'wrong-id': historical | {'id': 'f' * 64}})
    # A model's claimed hash in text or proposal is not a controller attachment.
    context = {'failure': failure.model_dump(), **diagnosis_handoff(workflow, rid, failure, None, 'published-fixture')}
    rows = context['previous_failure_observations']['recent']
    assert [r['checkpoint'] for r in rows if r['failure'].get('observation_id') == 'old'] == [prefix+'accepted']
    assert all('proposal' not in r['failure'] for r in rows)
    prepared = ContextPlanner(workflow.artifacts).prepare('recovery_diagnosis', context, work)
    assert log['hash'] in str(prepared.payload) and fake['hash'] not in str(prepared.payload)
    context['diagnosis_request']['proposal'] = {'attachment': fake}
    catalog = controller_context_catalog(context)
    assert fake['hash'] not in catalog.controller
    assert 'diagnosis_request.proposal' in catalog.proposals


@pytest.mark.parametrize('field,value', [('owner', 'other'), ('basis', 'wrong'), ('outcome_hash', 'wrong')])
async def test_enrollment_drift_is_rejected_before_new_selection(tmp_path, monkeypatch, field, value):
    from auto_agents_2.errors import PostCommitError
    from auto_agents_2.faults import StateConflict

    workflow, rid, _, _, _ = ready(tmp_path, RecheckDiagnosisAgent(), proposal_failure())
    complete = workflow.recovery_policy.complete
    def interrupt(rid, key, outcome):
        complete(rid, key, outcome)
        raise PostCommitError('after completion')
    with monkeypatch.context() as patch:
        patch.setattr(workflow.recovery_policy, 'complete', interrupt)
        with pytest.raises(PostCommitError):
            await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    operation = f"recovery/diagnose/{active['failure']['id']}/{active['diagnosis_round']}"
    key = 'recovery/diagnosis-progress-enrollment/' + operation
    workflow.store.checkpoint(rid, key, workflow.store.get(rid, key) | {field: value})
    with pytest.raises(StateConflict, match='enrollment'):
        await workflow.recovery.resume(rid)
    assert workflow.agent.diagnoses == 1 and not workflow.agent.options


async def test_prepublication_explicit_candidates_share_factual_progress_basis(tmp_path):
    from authority_review_fixtures import recorded_review
    from test_decisions import setup
    from test_diagnosis_decisions import InconclusiveAgent

    class DeliveryAgent(InconclusiveAgent):
        def __init__(self):
            super().__init__(actions=('diagnose', 'repair'))
            self.options = []
            self.contexts = []

        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == 'decision':
                self.options.append(set(context['decision_request']['scope']['candidates']))
            if stage == 'recovery_diagnosis':
                self.contexts.append(copy.deepcopy(context))
            return await super().invoke(stage, context, schema, *args, **kwargs)

    agent = DeliveryAgent()
    workflow, rid = setup(tmp_path, agent)
    result = await workflow.recovery.delivery_decision(rid, tmp_path,
        {'review': recorded_review(workflow, rid, {'approved': False, 'summary': 'Need observation', 'findings': []})},
        ['value.py'])
    assert result == 'repair' and 'diagnose' in agent.options[0] and 'diagnose' not in agent.options[-1]
    assert agent.contexts[-1]['diagnosis_request']['proposal']['expected_result']
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/delivery') == {'round': 2}


@pytest.mark.parametrize('published', [False, True])
@pytest.mark.parametrize('envelope', ['feedback', 'runtime_failure'])
def test_delivery_history_preserves_machine_envelopes_and_deduplicates_observation(tmp_path, published, envelope):
    from test_decisions import setup

    from auto_agents_2.recovery import FailureRecord
    from auto_agents_2.recovery_handoff import diagnosis_handoff

    workflow, rid = setup(tmp_path)
    log = workflow.artifacts.put('original delivery check log', 'check_log')
    record = FailureRecord.create(kind='checks', component='delivery', origin_id='delivery', step='delivery',
        observation_id='current', obligations=['value.py'], evidence={})
    old = record.model_dump() | {'observation_id': 'previous', 'evidence': {envelope: {
        'checks': [{'argv': ['tsc'], 'exit_code': 2, 'logs': [log]}],
        'proposal_attachment': {'hash': 'e' * 64, 'kind': 'document', 'bytes': 3}}}}
    workflow.store.checkpoint(rid, 'recovery/delivery/previous', {'failure': old})
    if not published:
        workflow.store.checkpoint(rid, 'recovery/delivery-policy/previous', {'failure': old, 'attempt': 1})
    result = diagnosis_handoff(workflow, rid, record, None, 'new')
    previous = result['previous_failure_observations']
    assert previous['total'] == 1 and len(previous['recent']) == 1
    observed = previous['recent'][0]['failure']['evidence'][envelope]
    assert observed == {'checks': [{'argv': ['tsc'], 'exit_code': 2, 'logs': [log]}]}
    view = ContextPlanner(workflow.artifacts).prepare('recovery_diagnosis', {'failure': record.model_dump(), **result}, tmp_path)
    assert log['hash'] in str(view.payload) and 'e' * 64 not in str(view.payload)


@pytest.mark.parametrize('reference', [
    'history.recent[0].action.reason', 'history.recent[0].action.repair_targets[0].reason',
    'excluded_actions.diagnose.operations.saved/operation.diagnosis.reason'])
def test_copied_model_rationales_cannot_supply_factual_evidence(reference):
    from auto_agents_2.errors import AA2Error
    from auto_agents_2.recovery import adjudicate

    failure = proposal_failure()
    context = {'failure': failure.model_dump(), 'history': {'recent': [{'action': {'action': 'diagnose',
        'reason': 'unproved', 'repair_targets': [{'reason': 'unproved'}]}}]},
        'excluded_actions': {'diagnose': {'operations': {'saved/operation': {
            'status': 'completed_without_execution', 'diagnosis': {'reason': 'unproved'}}}}}}
    with pytest.raises(AA2Error, match='requires controller evidence'):
        adjudicate(failure, diagnosis=Diagnosis(action='diagnose', reason='unproved', evidence_refs=[reference]),
                   evidence_context=context)
    assert adjudicate(failure, diagnosis=Diagnosis(action='diagnose', reason='actual action record',
        evidence_refs=['history.recent[0].action.action']), evidence_context=context).action == 'diagnose'
