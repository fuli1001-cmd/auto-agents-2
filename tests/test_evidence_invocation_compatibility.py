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
