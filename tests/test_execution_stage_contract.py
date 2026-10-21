"""A timing correction preserves acceptance substance and uses the existing review gate."""
import asyncio
import copy

import pytest
from pydantic import ValidationError
from test_decisions import ApprovingAgent
from test_recovery_contracts import failure, plan
from test_recovery_policy import ready
from test_workflow import engine

from auto_agents_2.errors import AA2Error
from auto_agents_2.plan_revision import (
    PlanPatch,
    TimingCorrection,
    TimingPlanReview,
    TimingReview,
    apply_patch,
    validate_timing_review,
)
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.workspace import tree_hash

OLD = 'Before candidate generation obtain independent review of all historical assertions and the 11 regressions.'
NEW = 'Propose the candidate first; before application independently review all historical assertions and the 11 regressions.'


def fixture_patch():
    previous = plan()
    previous.tasks[2].acceptance = [OLD, 'Preserve all tests, configuration, collection and environment evidence.']
    task = previous.tasks[2].model_copy(deep=True)
    task.acceptance[0] = NEW
    task.description = 'Propose a candidate, then use the controller protected-change review before application.'
    correction = TimingCorrection(task_id='T3', acceptance_index=0, previous=OLD, replacement=NEW,
        producer='protected_review', reason='This review consumes the candidate; it cannot precede its generation.',
        evidence_refs=['plan.tasks[2].acceptance[0]'])
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason='Correct only review timing',
        document='Candidate, protected review, application, environment, checks, acceptance.',
        upsert_tasks=[task], timing_corrections=[correction])
    return previous, patch


def apply(previous, patch):
    return apply_patch(previous, patch, 1, {failure().id}, {t.id: t.origin_id for t in previous.tasks})


def review(**updates):
    row = TimingReview(task_id='T3', acceptance_index=0, timing_only=True, obligations_preserved=True,
        user_order_preserved=True, reason='Historical assertions and the 11 regressions remain mandatory before application.',
        evidence_refs=['previous_plan.tasks[2].acceptance[0]', 'specification'])
    return TimingPlanReview(approved=True, summary='Only the controller review timing changes',
                           timing_reviews=[row.model_copy(update=updates)])


def test_timing_correction_preserves_every_structured_contract_and_requires_explicit_mapping():
    previous, patch = fixture_patch()
    candidate, affected, reopened = apply(previous, patch)
    assert affected == {'T3', 'T4'} and reopened == {'T3'}
    assert candidate.tasks[2].acceptance == [NEW, previous.tasks[2].acceptance[1]]
    assert candidate.tasks[2].checks == previous.tasks[2].checks
    assert candidate.final_checks == previous.final_checks
    assert previous.tasks[2].acceptance[0] == OLD
    patch.timing_corrections = []
    with pytest.raises(AA2Error, match='accepted obligations'):
        apply(previous, patch)


@pytest.mark.parametrize('defect', ['stale', 'old_text', 'index', 'duplicate', 'unmapped', 'scope',
    'command', 'count', 'requirement', 'dependency', 'origin', 'remove', 'final', 'environment', 'other_task'])
def test_timing_correction_cannot_change_other_contracts(defect):
    previous, patch = fixture_patch()
    if defect == 'stale':
        patch.base_revision = 2
    elif defect == 'old_text':
        patch.timing_corrections[0].previous = 'different text'
    elif defect == 'index':
        patch.timing_corrections[0].acceptance_index = 99
    elif defect == 'duplicate':
        patch.timing_corrections.append(patch.timing_corrections[0])
    elif defect == 'unmapped':
        patch.upsert_tasks[0].acceptance[1] = 'Drop remaining tests'
    elif defect == 'scope':
        patch.upsert_tasks[0].allowed_paths.append('outside.py')
    elif defect == 'command':
        patch.upsert_tasks[0].checks[0].argv = ['true']
    elif defect == 'count':
        patch.upsert_tasks[0].checks[0].expected_tests = 0
    elif defect == 'requirement':
        patch.upsert_tasks[0].requirement_ids.append('R1')
    elif defect == 'dependency':
        patch.upsert_tasks[0].depends_on = []
    elif defect == 'origin':
        patch.upsert_tasks[0].origin_id = 'T2'
    elif defect == 'remove':
        patch.remove_tasks = ['T4']
    elif defect == 'final':
        patch.final_checks = []
    elif defect == 'environment':
        patch.environment_file = 'another.yml'
    else:
        patch.upsert_tasks.append(previous.tasks[0].model_copy(update={'description': 'unrelated change'}))
    with pytest.raises(AA2Error):
        apply(previous, patch)


def test_human_approval_is_not_a_correctable_downstream_producer():
    _, patch = fixture_patch()
    value = patch.timing_corrections[0].model_dump()
    value['producer'] = 'human_approval'
    with pytest.raises(ValidationError):
        TimingCorrection.model_validate(value)


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'unknown', 'weakening', 'user_order', 'substance', 'invented_ref', 'self_citation'])
def test_semantic_rejection_or_incomplete_review_cannot_approve_timing_change(defect):
    previous, patch = fixture_patch()
    candidate, _, _ = apply(previous, patch)
    verdict = review()
    context = {'previous_plan': previous.model_dump(), 'plan': candidate.model_dump(),
               'patch': patch.model_dump(), 'specification': {'requirements': ['retain assertions']}}
    if defect == 'missing':
        verdict.timing_reviews = []
    elif defect == 'duplicate':
        verdict.timing_reviews *= 2
    elif defect == 'unknown':
        verdict.timing_reviews[0].acceptance_index = 1
    elif defect == 'weakening':
        verdict.timing_reviews[0].obligations_preserved = False
    elif defect == 'user_order':
        verdict.timing_reviews[0].user_order_preserved = False
    elif defect == 'substance':
        verdict.timing_reviews[0].timing_only = False
    elif defect == 'invented_ref':
        verdict.timing_reviews[0].evidence_refs = ['invented']
    else:
        verdict.timing_reviews[0].evidence_refs = ['patch.timing_corrections[0].reason']
    with pytest.raises(AA2Error):
        validate_timing_review(patch, verdict, context)


@pytest.mark.parametrize('stage,context', [('plan', {}), ('plan', {'plan_patch': True}),
    ('review', {'review_kind': 'plan'}), ('review', {'review_kind': 'plan_patch'}),
    ('implement', {}), ('review', {'review_kind': 'test_changes'}),
    ('review', {}), ('recovery_diagnosis', {}), ('decision', {})])
def test_shared_phase_contract_reaches_every_confirmed_caller(stage, context):
    text, _ = PromptCompiler().compile(stage, context)
    assert 'controller_stage_contract' in text
    assert 'candidate_generation' in text and 'protected_review' in text
    assert 'Missing necessary source material' in text
    assert 'human approval' in text
    if stage == 'plan' and context.get('plan_patch'):
        assert 'timing_corrections' in text


class TimingAgent(ApprovingAgent):
    def __init__(self, verdict='approve', *, interrupt=False):
        super().__init__()
        self.verdict, self.interrupt = verdict, interrupt
        self.views = []

    async def invoke(self, stage, context, schema, workspace, rid, operation, fresh=False):
        if schema is Diagnosis:
            return Diagnosis(action='plan_patch', task_ids=['T3'], evidence_refs=['plan.tasks[2].acceptance'],
                             reason='Correct the candidate review timing without changing obligations.')
        if schema is PlanPatch:
            _, patch = fixture_patch()
            return patch
        if context.get('review_kind') == 'plan_patch':
            assert schema is TimingPlanReview
            self.views.append(copy.deepcopy(context))
            if self.interrupt:
                self.interrupt = False
                raise asyncio.CancelledError()
            if self.verdict == 'reject':
                return TimingPlanReview(approved=False, summary='The original user requires this order.')
            if self.verdict == 'weakening':
                return review(obligations_preserved=False)
            if self.verdict == 'empty':
                return TimingPlanReview(approved=True, summary='Unsupported blanket approval')
            return review()
        return await super().invoke(stage, context, schema, workspace, rid, operation, fresh)


def setup_timing(tmp_path, agent):
    workflow, rid, work, _, record = ready(tmp_path, agent)
    previous, _ = fixture_patch()
    workflow.store.checkpoints(rid, {'plan': previous.model_dump(),
        'task_origins': {t.id: t.origin_id for t in previous.tasks},
        'clarify': {'requirements': [{'id': 'R3', 'description': 'Keep historical tests', 'acceptance': ['Retain assertions']}]} })
    return workflow, rid, work, previous, record


@pytest.mark.parametrize('verdict', ['approve', 'reject', 'weakening', 'empty'])
async def test_real_recovery_path_activates_only_explicit_independent_timing_review(tmp_path, verdict):
    agent = TimingAgent(verdict)
    workflow, rid, work, previous, record = setup_timing(tmp_path, agent)
    before = tree_hash(work)
    await workflow.recovery.resume(rid)
    current = workflow.store.get(rid, 'plan')
    assert workflow.store.pending(rid) == []
    assert not workflow.store.get(rid, 'develop/done')
    if verdict == 'approve':
        assert current['tasks'][2]['acceptance'][0] == NEW
        assert current['tasks'][2]['checks'] == previous.model_dump()['tasks'][2]['checks']
        assert workflow.store.get(rid, 'plan_revision/history/1')['plan'] == previous.model_dump()
        journal = workflow.store.get(rid, f'plan_revision/journal/{record.id}/1')
        assert journal['phase'] == 'activated' and journal['timing_review_binding']
        assert workflow.store.get(rid, 'develop/task/T3')['phase'] == 'rejected'
    else:
        assert current == previous.model_dump()
        assert tree_hash(work) == before
        assert not workflow.store.get(rid, 'plan_revision/current')


async def test_interrupted_timing_review_replays_without_accepting_or_losing_original_plan(tmp_path):
    agent = TimingAgent(interrupt=True)
    workflow, rid, _, previous, _ = setup_timing(tmp_path, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan') == previous.model_dump()
    workflow = engine(tmp_path, agent)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan')['tasks'][2]['acceptance'][0] == NEW
    assert agent.views[0] == agent.views[1]


async def test_journal_activation_cannot_bypass_timing_review_binding(tmp_path, monkeypatch):
    agent = TimingAgent()
    workflow, rid, _, previous, record = setup_timing(tmp_path, agent)
    activate = workflow.store.activate_plan_patch
    def pause_before_activation(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', pause_before_activation)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    key = f'plan_revision/journal/{record.id}/1'
    journal = workflow.store.get(rid, key)
    journal['review']['timing_reviews'][0]['obligations_preserved'] = False
    workflow.store.checkpoint(rid, key, journal)
    with pytest.raises(AA2Error):
        activate(rid, f'{record.id}/1')
    assert workflow.store.get(rid, 'plan') == previous.model_dump()


@pytest.mark.parametrize('condition,flag', [
    ('Obtain the explicit user approval before proposing changes.', 'user_order_preserved'),
    ('Read the required historical source bodies before proposing changes.', 'timing_only'),
    ('Retain every regression assertion, not just the same number of tests.', 'obligations_preserved'),
])
def test_semantic_review_preserves_real_prerequisites_and_assertions(condition, flag):
    previous, patch = fixture_patch()
    previous.tasks[2].acceptance[0] = condition
    patch.timing_corrections[0].previous = condition
    candidate, _, _ = apply(previous, patch)
    verdict = review(**{flag: False})
    with pytest.raises(AA2Error):
        validate_timing_review(patch, verdict, {'previous_plan': previous.model_dump(),
            'plan': candidate.model_dump(), 'patch': patch.model_dump(),
            'specification': {'requirements': [condition]}})
    assert previous.tasks[2].acceptance[0] == condition


async def test_stale_reviewed_timing_journal_cannot_activate(tmp_path, monkeypatch):
    agent = TimingAgent()
    workflow, rid, _, previous, record = setup_timing(tmp_path, agent)
    activate = workflow.store.activate_plan_patch
    def pause(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', pause)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    workflow.store.checkpoint(rid, 'plan_revision/current', {'version': 1, 'revision': 2})
    with pytest.raises(AA2Error, match='CAS failed'):
        activate(rid, f'{record.id}/1')
    assert workflow.store.get(rid, 'plan') == previous.model_dump()


@pytest.mark.parametrize('stage,context', [('review', {'review_kind': 'prototype'}),
    ('review', {'review_kind': 'design'}), ('review', {'review_kind': 'research'})])
def test_task_phase_contract_does_not_change_unrelated_review_roles(stage, context):
    text, _ = PromptCompiler().compile(stage, context)
    assert 'controller_stage_contract' not in text
