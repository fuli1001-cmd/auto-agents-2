"""Actual Engine receipts across the additive evidence-catalog upgrade."""
import asyncio
from copy import deepcopy

import pytest
from test_decisions import ApprovingAgent, configure, setup
from test_recovery_contracts import failure

from auto_agents_2.faults import StateConflict
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.util import digest


@pytest.mark.parametrize('old_stage', ['review', 'recovery_diagnosis'])
async def test_pre_catalog_completed_diagnosis_keeps_exact_original_receipt(tmp_path, old_stage):
    workflow, rid = setup(tmp_path)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    key = 'recovery/diagnose/legacy/1'
    # Bypass the newly added presentation helper to reproduce a real old input.
    first = await workflow._invoke_once(old_stage, context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, 'invocation/' + key)
    resumed = await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    assert resumed == first and len(workflow.agent.calls) == 1
    assert workflow.store.get(rid, 'invocation/' + key) == saved
    frozen = workflow.store.get(rid, 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', key]))
    if old_stage == 'recovery_diagnosis':
        assert frozen['context'] == context


@pytest.mark.parametrize('change', ['source', 'factual_input'])
async def test_catalog_compatibility_never_ignores_original_input_drift(tmp_path, change):
    workflow, rid = setup(tmp_path)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    key = 'recovery/diagnose/legacy-drift/1'
    await workflow._invoke_once('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, 'invocation/' + key)
    if change == 'source':
        (tmp_path / 'preserved-user-change.txt').write_text('Do not discard this new input')
    else:
        context = {**context, 'required_input': 'new factual requirement'}
    with pytest.raises(StateConflict, match='source changed'):
        await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    assert workflow.store.get(rid, 'invocation/' + key) == saved
    assert len(workflow.agent.calls) == 1


async def test_existing_catalog_is_frozen_for_completed_diagnosis_only(tmp_path):
    workflow, rid = setup(tmp_path)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    key = 'recovery/diagnose/new-catalog/1'
    first = await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    frozen_key = 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', key])
    frozen = workflow.store.get(rid, frozen_key)
    assert frozen['context']['controller_evidence_capabilities']['capabilities'][2]['available'] is False
    configure(workflow, shared_cache={'enabled': True})
    resumed = await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    assert resumed == first and len(workflow.agent.calls) == 1
    assert workflow.store.get(rid, frozen_key) == frozen
    next_key = 'recovery/diagnose/new-catalog/2'
    await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, next_key, fresh=True)
    next_frozen = workflow.store.get(rid, 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', next_key]))
    assert next_frozen['context']['controller_evidence_capabilities']['capabilities'][2]['available'] is True
    assert len(workflow.agent.calls) == 2


async def test_pre_catalog_interrupted_diagnosis_resumes_its_original_frozen_input(tmp_path):
    class Agent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.inputs = []

        async def invoke(self, stage, context, *args, **kwargs):
            self.inputs.append(deepcopy(context))
            if len(self.inputs) == 1:
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, *args, **kwargs)

    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    key = 'recovery/diagnose/legacy-interruption/1'
    with pytest.raises(asyncio.CancelledError):
        await workflow._invoke_once('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    frozen_key = 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', key])
    frozen = workflow.store.get(rid, frozen_key)
    await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    assert agent.inputs == [context, context]
    assert workflow.store.get(rid, frozen_key) == frozen


@pytest.mark.parametrize('with_catalog', [False, True])
async def test_pre_budget_review_receipt_migration_is_idempotent(tmp_path, with_catalog):
    from auto_agents_2.evidence_admission import capability_catalog

    workflow, rid = setup(tmp_path)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    if with_catalog:
        context['controller_evidence_capabilities'] = capability_catalog()
    key = 'recovery/diagnose/pre-budget-review/1'
    first = await workflow._invoke_once('review', context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, 'invocation/' + key)
    for _ in range(2):
        assert await workflow._invoke('recovery_diagnosis', context, Diagnosis,
                                      tmp_path, rid, key, fresh=True) == first
        assert workflow.store.get(rid, 'invocation/' + key) == saved
    assert len(workflow.agent.calls) == 1


@pytest.mark.parametrize('with_catalog,change', [
    (False, 'factual_input'), (True, 'source'), (False, 'schema'),
    (True, 'configuration'), (True, 'execution'),
])
async def test_pre_budget_review_never_ignores_bound_input_drift(tmp_path, monkeypatch, with_catalog, change):
    from auto_agents_2.evidence_admission import capability_catalog
    from auto_agents_2.providers import ProviderRouter

    workflow, rid = setup(tmp_path)
    agent = workflow.agent
    native = {'identity': 'native-before'}
    if change == 'execution':
        router = ProviderRouter(workflow.config, workflow.store, workflow.sandbox)
        monkeypatch.setattr(router, 'effective', lambda stage, candidate: {'fingerprint': native['identity']})
        monkeypatch.setattr(router, 'invoke', agent.invoke)
        workflow.agent = router
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    if with_catalog:
        context['controller_evidence_capabilities'] = capability_catalog()
    key = 'recovery/diagnose/pre-budget-drift/1'
    await workflow._invoke_once('review', context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, 'invocation/' + key)
    schema = Diagnosis
    if change == 'factual_input':
        context = {**context, 'required_input': 'new factual requirement'}
    elif change == 'source':
        (tmp_path / 'preserved-user-change.txt').write_text('Keep this new factual input')
    elif change == 'schema':
        class ChangedDiagnosis(Diagnosis):
            additional_protocol_field: bool = False
        schema = ChangedDiagnosis
    elif change == 'configuration':
        configure(workflow, execution={'infra_retries': workflow.config.load().execution.infra_retries + 1})
    else:
        native['identity'] = 'native-after'
    await workflow._invoke('recovery_diagnosis', context, schema, tmp_path, rid, key, fresh=True)
    assert len(agent.calls) == 2
    assert workflow.store.get(rid, 'invocation/' + key)['identity'] != saved['identity']
    assert workflow.artifacts.json(saved['artifact']) == saved['result']


@pytest.mark.parametrize('change', ['budget_value', 'budget_version', 'catalog', 'other_controller_field'])
async def test_legacy_review_does_not_strip_unknown_protocol_input(tmp_path, change):
    from auto_agents_2.collection import capture_contract
    from auto_agents_2.evidence_admission import capability_catalog

    workflow, rid = setup(tmp_path)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    key = 'recovery/diagnose/unknown-guidance/1'
    await workflow._invoke_once('review', context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, 'invocation/' + key)
    current = {**context, 'controller_evidence_capabilities': capability_catalog(),
               'controller_attachment_capture': capture_contract(workflow.store.root)}
    if change == 'budget_value':
        current['controller_attachment_capture']['limits']['max_files'] += 1
    elif change == 'budget_version':
        current['controller_attachment_capture']['version'] = 999
    elif change == 'catalog':
        current['controller_evidence_capabilities']['version'] = 999
    else:
        current['controller_additional_fact'] = {'required': True}
    # Directly exercise the identity consumer: _invoke would replace the budget
    # with the real controller contract and thereby hide this negative input.
    await workflow._invoke_once('recovery_diagnosis', current, Diagnosis, tmp_path, rid, key, fresh=True)
    assert len(workflow.agent.calls) == 2
    assert workflow.store.get(rid, 'invocation/' + key)['identity'] != saved['identity']


@pytest.mark.parametrize('stage', ['recovery_diagnosis', 'decision'])
@pytest.mark.parametrize('interrupted', [False, True])
async def test_native_recovery_operation_freezes_budget_but_new_operation_sees_current_policy(
        tmp_path, stage, interrupted):
    from auto_agents_2.decisions import DecisionResult

    class Agent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.inputs = []

        async def invoke(self, stage, context, *args, **kwargs):
            self.inputs.append(deepcopy(context))
            if interrupted and len(self.inputs) == 1:
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, *args, **kwargs)

    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    context = ({'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
               if stage == 'recovery_diagnosis' else {'decision_request': {'evidence': [], 'scope': {}}})
    schema = Diagnosis if stage == 'recovery_diagnosis' else DecisionResult
    key = 'recovery/budget-freeze/' + stage
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            await workflow._invoke(stage, context, schema, tmp_path, rid, key, fresh=True)
    else:
        await workflow._invoke(stage, context, schema, tmp_path, rid, key, fresh=True)
    frozen_key = 'recovery/invocation-inputs/' + digest([stage, key])
    frozen = workflow.store.get(rid, frozen_key)
    old_limit = frozen['context']['controller_attachment_capture']['limits']['max_files']
    configure(workflow, attachment_capture={'max_files': old_limit - 1})
    await workflow._invoke(stage, context, schema, tmp_path, rid, key, fresh=True)
    assert workflow.store.get(rid, frozen_key) == frozen
    assert len(agent.inputs) == (2 if interrupted else 1)
    assert all(c['controller_attachment_capture']['limits']['max_files'] == old_limit for c in agent.inputs)
    await workflow._invoke(stage, context, schema, tmp_path, rid, key + '/new', fresh=True)
    assert agent.inputs[-1]['controller_attachment_capture']['limits']['max_files'] == old_limit - 1


async def test_migrated_review_still_requires_unchanged_configuration(tmp_path):
    workflow, rid = setup(tmp_path)
    context = {'review_kind': 'recovery_diagnosis', 'failure': failure().model_dump()}
    key = 'recovery/diagnose/migrated-review-config/1'
    await workflow._invoke_once('review', context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, 'invocation/' + key)
    await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    assert len(workflow.agent.calls) == 1
    assert workflow.store.get(rid, 'invocation/' + key) == saved
    configure(workflow, execution={'infra_retries': workflow.config.load().execution.infra_retries + 1})
    await workflow._invoke('recovery_diagnosis', context, Diagnosis, tmp_path, rid, key, fresh=True)
    assert len(workflow.agent.calls) == 2
    assert workflow.store.get(rid, 'invocation/' + key)['identity'] != saved['identity']
