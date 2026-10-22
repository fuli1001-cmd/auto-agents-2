"""Bounded correction through real recovery/activation and ownership entrypoints."""
import asyncio
import copy

import pytest
from test_decisions import ApprovingAgent
from test_recovery_contracts import failure, plan
from test_recovery_policy import ready
from test_workflow import engine

from auto_agents_2.contract_corrections import (
    ContractCorrection,
    ContractPlanReview,
    ContractReview,
    contract_review_binding,
    validate_contract_references,
)
from auto_agents_2.errors import AA2Error, PolicyViolation
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, Command, PlanResult
from auto_agents_2.plan_revision import PlanPatch, apply_patch
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.stage_responsibilities import (
    correction_contract,
    correction_request,
    validate_correction_request,
    validate_stage_writes,
)
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


def fixture_patch(original=None, *, target='checks'):
    original = original or plan()
    old = original.tasks[2]
    task = old.model_copy(deep=True)
    if target == 'acceptance':
        before = task.acceptance[0]
        after = before + '; use the evidenced compatible implementation strategy'
        task.acceptance[0] = after
    else:
        before = task.checks[0].model_copy(deep=True)
        after = before.model_copy(update={'argv': [*before.argv, '--strict-markers']})
        task.checks[0] = after
    correction = ContractCorrection(target=target, task_id='T3', index=0, previous=before, replacement=after,
        reason='Actual failed derived technical decision', derived_decision='Planner selected a broken expression',
        preserved_obligations=['Original business assertions and historical collection'], evidence_refs=['failure'])
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason='Correct the owner contract',
        document='Corrected plan', upsert_tasks=[task], contract_corrections=[correction])
    return original, patch


def apply(original, patch):
    return apply_patch(original, patch, 1, {failure().id}, {t.id: t.origin_id for t in original.tasks})


def review_for(patch, *, approve=True, flag=None):
    rows = []
    for item in patch.contract_corrections:
        fields = dict(model_derived_only=True, user_constraints_preserved=True, assertions_preserved=True,
                      collection_preserved=True, dependency_scope_preserved=True, scope_minimal=True)
        if flag:
            fields[flag] = False
        rows.append(ContractReview(target=item.target, task_id=item.task_id, index=item.index,
            reason='Compared complete old/new expressions against original user text and observed failure',
            evidence_refs=['previous_plan', 'original_requirements', 'failure'], **fields))
    return ContractPlanReview(approved=approve, summary='Independent semantic assessment', contract_reviews=rows)


@pytest.mark.parametrize('target', ['checks', 'acceptance'])
def test_mapped_correction_admits_semantic_review_but_ordinary_patch_still_rejects(target):
    original, patch = fixture_patch(target=target)
    ordinary = patch.model_copy(update={'contract_corrections': []})
    with pytest.raises(AA2Error, match='removes'):
        apply(original, ordinary)
    candidate, affected, reopen = apply(original, patch)
    assert candidate.tasks[:2] == original.tasks[:2]
    assert affected == {'T3', 'T4'} and reopen == {'T3'}
    assert candidate.final_checks == original.final_checks


@pytest.mark.parametrize('bad', ['minimum', 'input', 'output', 'cwd', 'unmapped', 'origin', 'requirements',
                                  'dependency', 'scope', 'wildcard', 'unrelated_path', 'stale', 'duplicate', 'missing', 'remove'])
def test_structural_weakening_and_stale_mappings_are_rejected(bad):
    original, patch = fixture_patch()
    before, after = original.tasks[2].checks[0], patch.upsert_tasks[0].checks[0]
    row = patch.contract_corrections[0]
    if bad == 'minimum':
        after.expected_tests = 0
    elif bad == 'input':
        before.inputs = ['tests/test_3.py']
        row.previous = before.model_copy(deep=True)
    elif bad == 'output':
        before.outputs = ['dist']
        row.previous = before.model_copy(deep=True)
    elif bad == 'cwd':
        after.cwd = 'other'
    elif bad == 'unmapped':
        patch.upsert_tasks[0].acceptance = ['weaker']
    elif bad == 'origin':
        patch.upsert_tasks[0].origin_id = 'T1'
    elif bad == 'requirements':
        patch.upsert_tasks[0].requirement_ids = ['R1']
    elif bad == 'dependency':
        patch.upsert_tasks[0].depends_on = []
    elif bad == 'scope':
        patch.upsert_tasks[0].allowed_paths = ['other.py']
    elif bad == 'wildcard':
        patch.upsert_tasks[0].allowed_paths += ['**']
    elif bad == 'unrelated_path':
        patch.upsert_tasks[0].allowed_paths += ['unrelated.py']
    elif bad == 'stale':
        row.previous = Command(argv=['different'])
    elif bad == 'duplicate':
        patch.contract_corrections.append(row)
    elif bad == 'missing':
        patch.upsert_tasks = []
    elif bad == 'remove':
        patch.remove_tasks = ['T2']
    with pytest.raises(AA2Error):
        apply(original, patch)


class CorrectionAgent(ApprovingAgent):
    def __init__(self, *, approve=True, flag=None, interrupt=False, weaken=False):
        super().__init__()
        self.approve, self.flag, self.interrupt, self.weaken = approve, flag, interrupt, weaken
        self.views = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if schema is Diagnosis:
            return Diagnosis(action='plan_patch', task_ids=['T3'], reason='Correct the owning plan expression',
                             evidence_refs=['failure'])
        if schema is PlanPatch:
            self.views.append((stage, copy.deepcopy(context)))
            old = PlanResult.model_validate(context['plan'])
            _, patch = fixture_patch(old)
            patch.base_revision = context['base_revision']
            patch.failure_ids = [context['failure']['id']]
            if self.weaken:
                # Same declared test minimum, but the command hides actual assertions.
                new = patch.upsert_tasks[0].checks[0].model_copy(update={'argv': ['python', '-c', 'print(1)']})
                patch.upsert_tasks[0].checks[0] = new
                patch.contract_corrections[0].replacement = new
            return patch
        if schema is ContractPlanReview:
            self.views.append((stage, copy.deepcopy(context)))
            assert context['original_requirements'] == 'Keep the business assertions; choose a compatible tool version.'
            assert context['correction_request']['owner'] == 'plan'
            assert context['correction_request']['bindings']['plan'] == digest(context['previous_plan'])
            if self.interrupt:
                self.interrupt = False
                raise asyncio.CancelledError()
            return review_for(PlanPatch.model_validate(context['patch']), approve=self.approve,
                              flag='assertions_preserved' if self.weaken else self.flag)
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


def setup_correction(tmp_path, **kwargs):
    agent = CorrectionAgent(**kwargs)
    workflow, rid, work, original, record = ready(tmp_path, agent)
    artifact = workflow.artifacts.put('Keep the business assertions; choose a compatible tool version.', 'original-input')
    workflow.store.checkpoints(rid, {'capture': {'artifact': artifact},
        'clarify': {'requirements': [{'id': 'R3', 'acceptance': ['Keep all original assertions']}],
                    'document': 'Original goals are authoritative'},
        'task_origins': {t.id: t.origin_id for t in original.tasks},
        'rounds/T3': {'round': 17}, 'develop/done': ['T1', 'T2', 'T3', 'T4']})
    return workflow, rid, work, original, record, agent


async def test_real_recovery_route_review_activation_invalidates_only_affected_tasks(tmp_path):
    workflow, rid, work, original, record, agent = setup_correction(tmp_path)
    await workflow.recovery.resume(rid)
    assert [stage for stage, _ in agent.views] == ['plan', 'review']
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'develop/done') == ['T1', 'T2']
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'task_state/T3')['implementation'] == 'needs_repair'
    assert workflow.store.get(rid, 'task_state/T4')['evidence'] == 'stale'
    assert workflow.store.get(rid, 'plan_revision/history/1')['plan'] == original.model_dump()
    journal = workflow.store.get(rid, f'plan_revision/journal/{record.id}/1')
    assert journal['contract_review_binding'] == contract_review_binding(journal)
    assert journal['phase'] == 'activated'
    workflow.store.activate_plan_patch(rid, f'{record.id}/1')
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert not (work / 'part3.py').exists()  # Approval did not implement or certify the task.


@pytest.mark.parametrize('flag', ['model_derived_only', 'user_constraints_preserved', 'assertions_preserved',
                                 'collection_preserved', 'dependency_scope_preserved', 'scope_minimal'])
async def test_independent_semantic_rejection_keeps_old_plan_and_counters(tmp_path, flag):
    workflow, rid, work, original, record, _ = setup_correction(tmp_path, flag=flag)
    source = tree_hash(work)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan') == original.model_dump() and tree_hash(work) == source
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, f'recovery/patch-rejection/{record.id}/1')['validation_error']


async def test_equal_test_counts_cannot_substitute_for_semantic_preservation(tmp_path):
    workflow, rid, _, old, record, _ = setup_correction(tmp_path, weaken=True)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan') == old.model_dump()
    assert workflow.store.get(rid, f'recovery/patch-rejection/{record.id}/1')


async def test_interrupted_review_reuses_request_old_plan_and_original_input(tmp_path):
    workflow, rid, _, _, _, agent = setup_correction(tmp_path, interrupt=True)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    frozen = copy.deepcopy(agent.views[0][1])
    workflow = engine(tmp_path, agent)
    await workflow.recovery.resume(rid)
    assert len([s for s, _ in agent.views if s == 'plan']) == 1
    assert all(view['correction_request'] == frozen['correction_request'] for _, view in agent.views)
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round'] == 1


@pytest.mark.parametrize('change', ['revision', 'capture', 'owner', 'review', 'candidate', 'mapping', 'omitted_mapping'])
async def test_prepared_activation_revalidates_all_bindings(tmp_path, monkeypatch, change):
    workflow, rid, _, _, record, _ = setup_correction(tmp_path)
    original_activate = workflow.store.activate_plan_patch
    def interrupt(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    key = f'plan_revision/journal/{record.id}/1'
    journal = workflow.store.get(rid, key)
    if change == 'revision':
        workflow.store.checkpoint(rid, 'plan_revision/current', {'revision': 99})
    elif change == 'capture':
        workflow.store.checkpoint(rid, 'capture', {'artifact': {'hash': 'different'}})
    elif change == 'owner':
        journal['correction_request']['owner'] = 'develop'
    elif change == 'review':
        journal['review']['contract_reviews'] = []
    elif change == 'candidate':
        journal['plan']['tasks'][2]['checks'][0]['expected_tests'] = 0
    elif change == 'omitted_mapping':
        journal['patch']['contract_corrections'] = []
    elif change == 'mapping':
        journal['patch']['contract_corrections'][0]['previous']['argv'] = ['different']
    workflow.store.checkpoint(rid, key, journal)
    with pytest.raises(AA2Error):
        original_activate(rid, f'{record.id}/1')
    assert workflow.store.get(rid, key)['phase'] == 'prepared'


@pytest.mark.parametrize('owner,path', [('develop', 'docs/aa2/plan.md'), ('readme', 'src/app.py'),
    ('design', 'DESIGN.md'), ('prototype', 'docs/aa2/design.md'), ('review', 'part.py'),
    ('environment', 'tests/test_app.py'), ('checks', 'package.json')])
def test_wrong_owner_cannot_write_even_with_path_permission(owner, path):
    with pytest.raises(PolicyViolation):
        validate_stage_writes(owner, [Change(path=path, content='wrong owner')], [path])


async def test_common_patch_preparation_enforces_owner_before_any_application(tmp_path):
    workflow, rid, work, _, _ = ready(tmp_path)
    with pytest.raises(PolicyViolation, match='own'):
        await workflow.patch_repair.prepare(rid, work, [Change(path='DESIGN.md', content='x')], ['DESIGN.md'],
            stage='implement', context={}, operation='wrong-owner', contracts=True)
    assert not (work / 'DESIGN.md').exists()
    _, prepared = await workflow.patch_repair.prepare(rid, work, [Change(path='DESIGN.md', content='x')], ['DESIGN.md'],
        stage='implement', context={}, operation='controller-prototype-owner', contracts=True, artifact_owner='prototype')
    assert prepared and not (work / 'DESIGN.md').exists()


def test_request_and_protocol_do_not_grant_permissions_or_project_facts(tmp_path):
    workflow, rid, _, _, _ = ready(tmp_path)
    request = correction_request(workflow.store, rid, action='design', reason='Architecture defect', evidence_refs=['failure'])
    assert request['owner'] == 'design' and request['handler'] == 'stage_reentry'
    validate_correction_request(request, lambda key: workflow.store.get(rid, key), action='design')
    request['owner'] = 'develop'
    with pytest.raises(StateConflict):
        validate_correction_request(request, lambda key: workflow.store.get(rid, key))
    protocol = correction_contract()
    catalog = controller_context_catalog({'controller_correction_contract': protocol})
    assert protocol['hash'] in catalog.protocol and not catalog.factual
    _, patch = fixture_patch()
    with pytest.raises(AA2Error):
        validate_contract_references(patch, {'controller_correction_contract': protocol})


@pytest.mark.parametrize('stage', ['clarify', 'provider_research', 'prototype', 'design', 'readme'])
async def test_real_stage_reentry_retains_owner_request_and_old_contracts(tmp_path, stage):
    class ReentryAgent(ApprovingAgent):
        async def invoke(self, name, context, schema, *args, **kwargs):
            if schema is Diagnosis:
                return Diagnosis(action=stage, task_ids=['T3'], reason='Evidence of an owning-stage defect',
                                 evidence_refs=['failure'])
            return await super().invoke(name, context, schema, *args, **kwargs)
    workflow, rid, _, old, _ = ready(tmp_path, ReentryAgent())
    workflow.store.checkpoint(rid, 'rounds/T3', {'round': 21})
    await workflow.recovery.resume(rid)
    feedback = workflow.store.get(rid, 'replan_feedback')
    assert feedback['correction_request']['owner'] == stage
    assert feedback['preserved_plan'] == old.model_dump()
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 21}
    assert workflow.store.get(rid, 'plan') == (old.model_dump() if stage == 'readme' else None)
    assert not workflow.store.get(rid, 'prototype_approval')


async def test_two_rejected_contract_amendments_exclude_same_evidence_path(tmp_path):
    workflow, rid, work, old, record, _ = setup_correction(tmp_path, approve=False)
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    exclusions = workflow.recovery_policy.progress.exclusions(rid, record, work, old)
    assert len(exclusions['plan_patch']['operations']) == 2
    assert 'plan_patch' not in workflow.recovery_policy.diagnosis_actions(rid, record, work, old)
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'plan') == old.model_dump()
    changed = record.model_copy(deep=True)
    changed.evidence['environment_error'] = 'New verified resolution observation'
    assert 'plan_patch' not in workflow.recovery_policy.progress.exclusions(rid, changed, work, old)


def test_old_declared_check_input_can_be_added_only_through_reviewed_correction():
    original = plan()
    original.tasks[2].checks[0].inputs = ['toolchain.lock']
    original, patch = fixture_patch(original)
    patch.upsert_tasks[0].allowed_paths += ['toolchain.lock']
    candidate, _, reopen = apply(original, patch)
    assert candidate.tasks[2].allowed_paths[-1] == 'toolchain.lock' and 'T3' in reopen


@pytest.mark.parametrize('target', ['final_checks', 'runtime_prepare', 'runtime_probes'])
def test_final_and_prepare_contracts_use_same_mapping_and_preserve_other_commands(target):
    original = plan()
    if target == 'runtime_prepare':
        original.runtime_prepare = [Command(argv=['node', 'build.js'], outputs=['dist'], inputs=['package.json'])]
    previous = getattr(original, target)[0]
    replacement = previous.model_copy(update={'argv': [*previous.argv, '--strict']})
    corrected = [replacement, *getattr(original, target)[1:]]
    mapping = ContractCorrection(target=target, index=0, previous=previous, replacement=replacement,
        reason='Observed derived tooling error', derived_decision='Planner expression',
        preserved_obligations=['Original application assertions and build outputs'], evidence_refs=['failure'])
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason='Fix command expression',
        document=original.document, contract_corrections=[mapping], **{target: corrected})
    candidate, affected, reopen = apply(original, patch)
    assert affected == {t.id for t in original.tasks} and not reopen
    assert candidate.tasks == original.tasks
    assert getattr(candidate, target)[0] == replacement


async def test_activation_commit_interruption_does_not_reinvoke_or_double_increment(tmp_path, monkeypatch):
    workflow, rid, _, _, record, agent = setup_correction(tmp_path)
    activate = workflow.store.activate_plan_patch
    def interrupt_after_commit(*args):
        activate(*args)
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', interrupt_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    workflow = engine(tmp_path, agent)
    await workflow.recovery.resume(rid)
    assert [stage for stage, _ in agent.views] == ['plan', 'review']
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, f'recovery/completed/{record.id}/1')


async def test_changed_original_text_and_protocol_only_citations_cannot_activate(tmp_path):
    workflow, rid, _, _, record, _ = setup_correction(tmp_path)
    original_invoke = workflow.agent.invoke
    async def invalid_reference(stage, context, schema, *args, **kwargs):
        result = await original_invoke(stage, context, schema, *args, **kwargs)
        if schema is PlanPatch:
            result.contract_corrections[0].evidence_refs = ['controller_correction_contract']
        return result
    workflow.agent.invoke = invalid_reference
    await workflow.recovery.resume(rid)
    assert not workflow.store.get(rid, 'plan_revision/current')
    assert 'evidence' in workflow.store.get(rid, f'recovery/patch-rejection/{record.id}/1')['validation_error']


async def test_coarse_reentry_source_drift_preserves_pending_request(tmp_path, monkeypatch):
    workflow, rid, work, _, _ = ready(tmp_path)
    from auto_agents_2.engine import Replan
    workflow._controller_replan(rid, Replan('design', 'Bound architecture correction'))
    original_archive = workflow.archive_workers
    def stop_before_archive(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow, 'archive_workers', stop_before_archive)
    with pytest.raises(asyncio.CancelledError):
        await workflow._resume_replan(rid)
    pending = workflow.store.get(rid, 'replanning/pending')
    assert pending['correction_request']['owner'] == 'design'
    (work / 'user-change.txt').write_text('preserve user edit')
    monkeypatch.setattr(workflow, 'archive_workers', original_archive)
    with pytest.raises(StateConflict, match='source changed'):
        await workflow._resume_replan(rid)
    assert workflow.store.get(rid, 'replanning/pending') == pending
    assert (work / 'user-change.txt').read_text() == 'preserve user edit'


@pytest.mark.parametrize('drift', [False, True])
async def test_verification_only_correction_requires_fresh_bound_coverage(tmp_path, monkeypatch, drift):
    import sys

    from auto_agents_2.models import Coverage, IntakeResult, Requirement
    command = Command(argv=[sys.executable, '-c', 'from calc import add; assert add(2,3)==5'])
    original = PlanResult(mode='verification_only', summary='Verify existing code', document='original',
                          tasks=[], final_checks=[command], runtime_probes=[command])
    replacement = command.model_copy(update={'argv': [sys.executable, '-c',
        'from calc import add; assert add(2, 3) == 5']})
    class VerificationAgent(ApprovingAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if schema is Diagnosis:
                return Diagnosis(action='plan_patch', task_ids=[], evidence_refs=['failure'], reason='Fix final expression')
            if schema is PlanPatch:
                row = ContractCorrection(target='final_checks', index=0, previous=command, replacement=replacement,
                    reason='Equivalent expression fixture', derived_decision='Plan expression',
                    preserved_obligations=['Addition assertion'], evidence_refs=['failure'])
                return PlanPatch(base_revision=1, failure_ids=[context['failure']['id']], reason='Correct expression',
                    document='reviewed expression', final_checks=[replacement], contract_corrections=[row])
            if schema is ContractPlanReview:
                return review_for(PlanPatch.model_validate(context['patch']))
            return await super().invoke(stage, context, schema, *args, **kwargs)
    record = failure(task_id='', origin_id='final', step='final')
    workflow, rid, work, _, _ = ready(tmp_path, VerificationAgent(), record=record)
    (work / 'calc.py').write_text('def add(a,b): return a+b\n')
    artifact = workflow.artifacts.put('Preserve addition behavior.', 'original')
    intake = IntakeResult(summary='addition', document='Preserve addition',
        requirements=[Requirement(id='R1', description='sum', acceptance=['2+3==5'])],
        coverage=[Coverage(requirement_id='R1', status='satisfied', evidence_paths=['calc.py'], rationale='Existing source')],
        verification=[command])
    workflow.store.checkpoints(rid, {'plan': original.model_dump(), 'capture': {'artifact': artifact},
                                   'clarify': intake.model_dump()})
    activate = workflow.store.activate_plan_patch
    if drift:
        def changed(*args):
            workflow.store.checkpoint(rid, 'coverage_checks', [])
            return activate(*args)
        monkeypatch.setattr(workflow.store, 'activate_plan_patch', changed)
        with pytest.raises(AA2Error, match='coverage proof'):
            await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, 'plan') == original.model_dump()
    else:
        await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
        journal = workflow.store.get(rid, f'plan_revision/journal/{record.id}/1')
        assert journal['contract_coverage']['verified']
        assert workflow.store.get(rid, 'coverage_checks')[0]['process_exit_code'] == 0
        assert not workflow.store.get(rid, 'develop/done')


async def test_first_rejected_plan_reenters_author_but_accepted_plan_cannot_reset(tmp_path):
    from auto_agents_2.engine import Replan
    workflow, rid, _, old, _ = ready(tmp_path)
    workflow.store.checkpoint(rid, 'plan', None)
    workflow._controller_replan(rid, Replan('plan', 'Initial plan failed structural checks'))
    await workflow._resume_replan(rid)
    request = workflow.store.get(rid, 'replan_feedback')['correction_request']
    assert request['owner'] == 'plan' and request['handler'] == 'stage_reentry'
    assert request['invalidate_stages'] == ['plan', 'develop', 'readme', 'verify', 'deliver']
    workflow.store.checkpoint(rid, 'plan', old.model_dump())
    pending = workflow.store.prepare_replan(rid, 'plan', 'Attempt to bypass accepted obligations')
    workflow.store.checkpoint(rid, 'replanning/pending', {**pending, 'granted': True})
    with pytest.raises(StateConflict, match='must use PlanPatch'):
        await workflow._resume_replan(rid)
    assert workflow.store.get(rid, 'plan') == old.model_dump()


@pytest.mark.parametrize('change', ['source', 'consent', 'plan', 'published_evidence', 'permit'])
async def test_published_prototype_refresh_still_rejects_real_input_drift(tmp_path, monkeypatch, change):
    from test_prototype_evidence_recovery import ready as prototype_ready

    workflow, rid, work, _, _, _, calls, browsers, _ = await prototype_ready(tmp_path, monkeypatch)
    checkpoint = workflow.store.checkpoint
    def stop_after_publication(run_id, key, data):
        checkpoint(run_id, key, data)
        if key.startswith('recovery/prototype-rechecks/') and data.get('phase') == 'verified':
            raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'checkpoint', stop_after_publication)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow.store, 'checkpoint', checkpoint)
    previous_task = workflow.store.get(rid, 'develop/task/T1')
    if change == 'source':
        (work / 'user-edit.txt').write_text('retain user edit')
    elif change == 'consent':
        workflow.store.supersede_questions(rid, 'prototype-approval', include_answered=True)
    elif change == 'plan':
        current = workflow.store.get(rid, 'plan')
        workflow.store.checkpoint(rid, 'plan', {**current, 'document': 'different approved contract'})
    elif change == 'published_evidence':
        current = workflow.store.get(rid, 'prototype_approval')
        workflow.store.checkpoint(rid, 'prototype_approval', {**current, 'visuals': []})
    else:
        permit_key = workflow.store.get(rid, 'recovery/active')['permit']
        permit = workflow.store.get(rid, permit_key)
        permit['prototype_binding']['identity'] = 'different'
        workflow.store.checkpoint(rid, permit_key, permit)
    with pytest.raises(StateConflict):
        await workflow.recovery.resume(rid)
    assert len(browsers) == 1 and [stage for stage, _ in calls] == ['decision', 'review']
    assert workflow.store.get(rid, 'develop/task/T1') == previous_task
    assert workflow.store.get(rid, 'rounds/T1') == {'round': 8, 'stalled': 3}
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T1') == {'round': 6}


def test_other_corrections_still_bind_complete_prototype_approval(tmp_path):
    workflow, rid, _, _, _ = ready(tmp_path)
    workflow.store.checkpoint(rid, 'prototype_approval', {'review': {'approved': True}, 'visuals': ['old']})
    request = correction_request(workflow.store, rid, action='plan_patch', reason='Derived check defect',
                                 evidence_refs=['failure'])
    workflow.store.checkpoint(rid, 'prototype_approval', {'review': {'approved': True}, 'visuals': ['new']})
    with pytest.raises(StateConflict, match='inputs changed'):
        validate_correction_request(request, lambda key: workflow.store.get(rid, key))
