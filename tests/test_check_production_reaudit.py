"""Real reaudit and recovery consumers must not silently replace adopted closure."""
import asyncio
import sys
from pathlib import Path

import pytest
from evidence_test_fixtures import empty_contract
from test_check_production import plan_and_review
from test_workflow import CodingAgent, engine

from auto_agents_2.check_production import ACTIVE_KEY, REQUIRED_KEY, activation_updates, active_receipt
from auto_agents_2.environment import Environment
from auto_agents_2.evidence_admission import admission_context, admission_record, validate_admission_review
from auto_agents_2.models import CheckProducer, Command, EvidencePlanReview, PlanResult
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.recovery import Diagnosis, RecoveryNeeded
from auto_agents_2.util import digest


class ReauditAgent(CodingAgent):
    mode = 'command'
    reason = 'Independently bind this fixture obligation to its actual task check'
    final_files = False

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            return Diagnosis(action='plan_patch', task_ids=[], reason='Revise adopted closure through its owner',
                             evidence_refs=['failure'])
        if schema is PlanPatch:
            old = PlanResult.model_validate(context['plan'])
            return PlanPatch(base_revision=context['base_revision'], failure_ids=[context['failure']['id']],
                             reason='Keep the plan and formally adopt the corrected evidence binding',
                             document=old.document, evidence_contract=old.evidence_contract)
        result = await super().invoke(stage, context, schema, *args, **kwargs)
        if getattr(result, 'evidence_assessment', None):
            tasks = {t['id']: t for t in context['plan']['tasks']}
            for row in result.evidence_assessment.check_production.bindings:
                if row.target == 'acceptance':
                    row.kind, row.reason = self.mode, self.reason
                    row.producers = ([] if self.mode == 'review' else
                        [CheckProducer(index=0, command=Command.model_validate(tasks[row.task_id]['checks'][0]))])
                    row.attachment_formats = ['json'] if self.mode == 'attachments' else []
                elif self.final_files:
                    row.kind, row.attachment_formats = 'attachments', ['json']
        return result


async def accepted_plan(tmp_path, *, mode='attachments', no_tasks=False):
    workflow = engine(tmp_path, ReauditAgent())
    agent = workflow.agent
    agent.mode = mode
    rid = workflow.store.admit('reaudit fixture', {})['id']
    original = workflow.artifacts.put('Run the check and retain its required evidence.', 'original-input')
    workflow.store.checkpoints(rid, {'capture': {'artifact': original}, 'clarify': {
        'document': 'Run the check and retain its required evidence.', 'requirements': [
            {'id': 'R1', 'description': 'Declared check', 'acceptance': ['Actual evidence']}]} })
    plan, _, _ = plan_and_review()
    if no_tasks:
        plan.mode, plan.tasks = 'verification_only', []
    context = admission_context(lambda key: workflow.store.get(rid, key))
    plan.evidence_contract = empty_contract(context)
    context.update(plan=plan.model_dump(), review_kind='plan')
    review = await workflow._invoke('review', context, EvidencePlanReview, tmp_path, rid, 'plan-review', fresh=True)
    context = workflow.authority.context(rid, review, context)
    validate_admission_review(plan, review, context)
    receipt = admission_record(plan, review, context)
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), **activation_updates(receipt),
        **workflow.authority.acceptance(rid, 'plan', plan.model_dump(), review)})
    workflow.store.checkpoint(rid, 'authority/accepted/plan', {})
    return workflow, rid, plan, receipt


async def test_reaudit_cannot_downgrade_adopted_attachment_before_any_done_task(tmp_path):
    workflow, rid, plan, receipt = await accepted_plan(tmp_path)
    workflow.agent.mode = 'review'
    with pytest.raises(RecoveryNeeded) as error:
        await workflow.authority.reaudit_plan(rid, tmp_path, plan.model_dump(), {})
    assert error.value.failure.kind == 'contract'
    assert error.value.failure.step == 'check_production_mapping'
    assert workflow.store.get(rid, ACTIVE_KEY) == receipt
    assert not workflow.store.get(rid, 'authority/accepted/plan')
    env = Environment(Path(sys.prefix), 'fixture')
    commands = workflow._task_commands(rid, plan.tasks[0], tmp_path, plan)
    checks = await workflow._checks(rid, commands, tmp_path, env, plan.tasks[0])
    with pytest.raises(RecoveryNeeded) as missing_file:
        workflow._require_task_evidence(rid, plan.tasks[0], {}, commands, checks, tmp_path, env)
    assert missing_file.value.failure.kind == 'evidence'


async def test_reaudit_same_mapping_reason_only_update_is_legal(tmp_path):
    workflow, rid, plan, receipt = await accepted_plan(tmp_path, mode='command')
    workflow.agent.reason = 'Different explanation, same source/producer/obligation'
    workflow.store.checkpoint(rid, 'rounds/A', {'round': 7})
    await workflow.authority.reaudit_plan(rid, tmp_path, plan.model_dump(), {})
    assert workflow.authority.accepted(rid, 'plan', plan.model_dump())
    current = active_receipt(lambda key: workflow.store.get(rid, key))
    assert current['binding'] != receipt['binding']
    assert current['check_production']['bindings'][0]['producers'] == receipt['check_production']['bindings'][0]['producers']
    assert workflow.store.get(rid, 'rounds/A') == {'round': 7}
    assert workflow.store.get(rid, 'plan') == plan.model_dump()


@pytest.mark.parametrize('variant', ['no_tasks', 'legacy'])
async def test_reaudit_mapping_change_uses_revision_even_without_done_consumers(tmp_path, variant):
    workflow, rid, plan, receipt = await accepted_plan(tmp_path, mode='command', no_tasks=variant == 'no_tasks')
    if variant == 'no_tasks':
        workflow.agent.final_files = True
    else:
        workflow.store.checkpoints(rid, {ACTIVE_KEY: None, REQUIRED_KEY: None})
    before = workflow.store.get(rid, ACTIVE_KEY)
    with pytest.raises(RecoveryNeeded):
        await workflow.authority.reaudit_plan(rid, tmp_path, plan.model_dump(), {})
    assert workflow.store.get(rid, ACTIVE_KEY) == before
    assert workflow.store.get(rid, 'plan') == plan.model_dump()
    assert not workflow.store.get(rid, 'authority/accepted/plan')


async def test_accepted_unintegrated_consumer_requires_formal_patch_then_rechecks(tmp_path, monkeypatch):
    project = tmp_path/'project'
    project.mkdir()
    request = tmp_path/'request.md'
    request.write_text('Implement addition and retain its declared evidence.')
    agent = ReauditAgent()
    workflow = engine(project, agent)
    # Real command results plus deterministic archived bytes, not a browser claim.
    check = workflow.verifier._check
    async def attach(*args, **kwargs):
        result = await check(*args, **kwargs)
        result.attachments = [{'path': 'actual.json', 'artifact': workflow.artifacts.put(b'{"sum":5}', 'test_diagnostic')}]
        return result
    monkeypatch.setattr(workflow.verifier, '_check', attach)
    observed = workflow._observed_task
    async def stop_before_integration(*args):
        await observed(*args)
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow, '_observed_task', stop_before_integration)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    monkeypatch.setattr(workflow, '_observed_task', observed)
    rid = workflow.store.current()['id']
    plan = PlanResult.model_validate(workflow.store.get(rid, 'plan'))
    saved = workflow.store.get(rid, 'develop/task/T1')
    assert saved['commit']
    assert workflow.store.get(rid, 'task_state/T1')['implementation'] == 'accepted'
    assert workflow.store.get(rid, 'task_state/T1')['evidence'] == 'valid'
    assert not workflow.store.get(rid, 'develop/done')
    receipt = workflow.store.get(rid, ACTIVE_KEY)
    rounds = workflow.store.get(rid, 'rounds/T1')
    implementations = sum(stage == 'implement' for stage, _ in agent.calls)
    work = workflow.store.root/'worktrees'/rid/'integration'
    workflow.store.checkpoint(rid, 'authority/accepted/plan', {})
    agent.mode = 'attachments'
    with pytest.raises(RecoveryNeeded) as error:
        await workflow.authority.reaudit_plan(rid, work, plan.model_dump(), {})
    assert workflow.store.get(rid, ACTIVE_KEY) == receipt
    assert workflow.store.get(rid, 'develop/task/T1') == saved
    assert error.value.failure.obligations == ['sum', digest(plan.final_checks[0].model_dump())]
    gates, activations = [], []
    gate = workflow._require_task_evidence
    def capture_gate(*args, **kwargs):
        result = gate(*args, **kwargs)
        gates.append(result['check_production'])
        return result
    monkeypatch.setattr(workflow, '_require_task_evidence', capture_gate)
    activate = workflow.store.activate_plan_patch
    def capture_activation(*args, **kwargs):
        activate(*args, **kwargs)
        assert workflow.store.get(rid, 'develop/task/T1')['phase'] == 'reverify'
        assert workflow.store.get(rid, 'task_state/T1')['evidence'] == 'stale'
        assert workflow.store.get(rid, 'rounds/T1') == rounds
        activations.append(workflow.store.get(rid, 'plan_revision/current')['revision'])
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', capture_activation)
    # Resume through Engine so the paused run receives its normal lease/status;
    # directly invoking recovery.resume on PAUSED would correctly fail its CAS.
    result = await workflow.start()
    assert result['data']['outcome'] == 'DELIVERED'
    assert activations == [2] and gates and all(row['closed'][0]['attachment_hashes'] for row in gates)
    assert workflow.store.get(rid, 'task_state/T1')['evidence'] == 'valid'
    assert workflow.store.get(rid, 'rounds/T1') == rounds
    assert sum(stage == 'implement' for stage, _ in agent.calls) == implementations
