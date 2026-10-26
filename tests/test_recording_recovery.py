"""Production collection/recovery contract regressions; no real project is changed."""
import asyncio
import sys
from pathlib import Path

import pytest
from authority_review_fixtures import authority_review, seed_original
from evidence_test_fixtures import empty_contract, evidence_review, seed_check_production
from test_stage_contract_corrections import review_for
from test_workflow import CodingAgent, engine

from auto_agents_2 import recording_recovery as recording
from auto_agents_2.contract_corrections import ContractCorrection
from auto_agents_2.errors import AA2Error, PolicyViolation
from auto_agents_2.evidence_admission import admission_context
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import (
    Change,
    Command,
    EvidenceObligation,
    ImplementationResult,
    PlanResult,
    TaskSpec,
)
from auto_agents_2.plan_revision import PlanPatch, apply_patch
from auto_agents_2.recovery import Diagnosis, FailureRecord, RecoveryNeeded, check_failure
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, prepare_changes, tree_hash


class RecordingAgent(CodingAgent):
    def __init__(self):
        super().__init__()
        self.contexts = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        self.contexts.append((stage, context))
        if stage == 'implement':
            return ImplementationResult(summary='observe contract only')
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


async def captured_case(tmp_path):
    agent = RecordingAgent()
    workflow = engine(tmp_path, agent)
    config = workflow.config.load().model_dump(mode='json')
    config['attachment_capture']['max_files'] = 2
    write_json(workflow.config.path, config)
    rid = workflow.store.admit('recording scope', {})['id']
    seed_original(workflow, rid)
    base = workflow.workspaces.committed_baseline(rid, 'main')
    work = workflow.workspaces.allocate(rid, 'T1-r1', base)
    (work / 'tests').mkdir()
    runner = work / 'tests/playwright'
    runner.write_text(f'#!{sys.executable}\n' + '''import os,pathlib
p=pathlib.Path(os.environ['PLAYWRIGHT_JUNIT_OUTPUT_FILE'])
p.write_text('<testsuite><testcase name="ok"/></testsuite>')
d=p.parent/'playwright-results'; d.mkdir()
for i in range(3): (d/f'{i}.json').write_text('{"state":"actual"}')
''')
    runner.chmod(0o755)
    (work / 'src.py').write_text('BUSINESS = 1\n')
    command = Command(argv=[str(runner), 'test'], expected_tests=1, inputs=['tests/playwright'])
    task = TaskSpec(id='T1', origin_id='T1', title='test recording', description='retain all assertions',
                    requirement_ids=['R1'], allowed_paths=['src.py', 'tests'],
                    acceptance=['actual PNG and state JSON are required'], checks=[command])
    plan = PlanResult(summary='recording', document='# Plan', tasks=[task], final_checks=[command])
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), 'task_origins': {'T1': 'T1'},
        'task_state/T1': {'version': 1, 'revision': 1, 'implementation': 'in_progress', 'evidence': 'absent'},
        'rounds/T1': {'round': 1, 'stalled': 0}, 'plan_revision/current': {'revision': 1}})
    results = await workflow.verifier.run([command], work)
    assert results[0].failure_reasons == ['evidence_collection_failed']
    assert results[0].attachment_capture['reason'] == 'max_files'
    assert results[0].tests == 1 and results[0].process_exit_code == 0
    saved = {'phase': 'rejected', 'workspace': str(work), 'source_hash': tree_hash(work),
             'revision': 1, 'checks': [r.model_dump() for r in results]}
    workflow.store.checkpoint(rid, 'develop/task/T1', saved)
    failure = check_failure([command], results, task=task, evidence_ref='develop/task/T1')
    workflow.recovery.submit(rid, failure)
    return workflow, rid, plan, work, results


async def test_author_receives_actual_budget_and_physical_copy_counting(tmp_path):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    await workflow._invoke('implement', {'task': plan.tasks[0].model_dump()},
                           ImplementationResult, work, rid, 'recording-author')
    context = workflow.agent.contexts[-1][1]
    contract = context.get('controller_attachment_capture')
    assert contract and contract['limits']['max_files'] == 2
    assert contract['counting'] == 'physical_paths_in_each_command_output_root'
    assert contract['duplicate_paths_count_separately'] is True


async def test_verified_count_failure_reaches_read_only_diagnosis_before_any_edit(tmp_path):
    workflow, rid, _, _, _ = await captured_case(tmp_path)
    async def stop_at_diagnosis(context, *args):
        assert context['controller_recording_recovery']['initial_action'] == 'diagnose'
        assert not workflow.store.pending(rid)
        assert workflow.store.get(rid, 'rounds/T1')['round'] == 1
        raise StopAtDiagnosis()
    workflow.recovery._diagnose = stop_at_diagnosis
    import pytest
    with pytest.raises(StopAtDiagnosis):
        await workflow.recovery.resume(rid)


class StopAtDiagnosis(Exception):
    pass






async def admit(workflow, rid, plan, work):
    active = workflow.store.get(rid, 'recovery/active')
    failure = FailureRecord.model_validate(active['failure'])
    receipt = await recording.prepare(workflow, rid, active, failure, plan, work)
    return active, failure, receipt


async def test_old_question_conversion_is_atomic_unanswered_and_resume_keeps_counters(tmp_path):
    workflow, rid, plan, work, rows = await captured_case(tmp_path)
    # Exercise the former production wait, rather than manually inventing its key.
    async def pause(question):
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery._capture_retry(rid, [r.model_dump() for r in rows], work)
    question = workflow.store.pending(rid)[0]
    active, failure, receipt = await admit(workflow, rid, plan, work)
    assert receipt['question_keys'] == [question['key']]
    assert workflow.store.question(question['id'])['superseded'] == 0
    # Replay the interruption between preparation and permit publication.
    _, _, replayed = await admit(workflow, rid, plan, work)
    assert replayed == receipt
    async def stop(context, *args):
        saved = workflow.store.question(question['id'])
        assert saved['answer'] is None and saved['superseded'] == 1
        assert workflow.store.get(rid, 'rounds/T1') == {'round': 1, 'stalled': 0}
        assert workflow.store.get(rid, 'recovery/active')['attempt'] == active['attempt']
        assert context['controller_recording_recovery']['receipt_hash'] == receipt['hash']
        raise StopAtDiagnosis()
    workflow.recovery._diagnose = stop
    with pytest.raises(StopAtDiagnosis):
        await workflow.recovery.resume(rid)


@pytest.mark.parametrize('mutation', ['source', 'policy', 'plan-revision', 'task-revision', 'missing-artifact', 'corrupt-artifact', 'missing-hold', 'retained-bytes'])
async def test_prepared_receipt_revalidates_source_policy_and_retained_evidence(tmp_path, mutation):
    workflow, rid, plan, work, rows = await captured_case(tmp_path)
    await admit(workflow, rid, plan, work)
    row = rows[0].model_dump()
    if mutation == 'source':
        (work / 'src.py').write_text('BUSINESS = 2\n')
    elif mutation in {'plan-revision', 'task-revision'}:
        key = 'plan_revision/current' if mutation == 'plan-revision' else 'task_state/T1'
        state = workflow.store.get(rid, key)
        workflow.store.checkpoint(rid, key, {**state, 'revision': state['revision'] + 1})
    elif mutation == 'policy':
        config = workflow.config.load().model_dump(mode='json')
        config['attachment_capture']['max_files'] = 3
        write_json(workflow.config.path, config)
    elif mutation in {'missing-artifact', 'corrupt-artifact'}:
        sha = row['attachments'][0]['artifact']['hash']
        path = workflow.store.root / 'artifacts' / sha[:2] / sha
        if mutation == 'missing-artifact':
            path.unlink()  # Isolated test fixture only.
        else:
            path.write_bytes(b'changed')
    elif mutation == 'missing-hold':
        with workflow.store.connect() as db:
            db.execute('DELETE FROM resource_holds WHERE id=?', (row['attachment_capture']['hold'],))
    else:
        (Path(row['attachment_capture']['scratch']) / 'playwright-results' / '0.json').write_text('changed')
    with pytest.raises(StateConflict):
        await admit(workflow, rid, plan, work)


@pytest.mark.parametrize('reason', ['max_total_bytes', 'max_file_bytes', 'timeout', 'invalid_input'])
async def test_old_receipt_never_exempts_a_different_capture_prerequisite(tmp_path, reason):
    workflow, rid, plan, work, rows = await captured_case(tmp_path)
    _, _, original = await admit(workflow, rid, plan, work)
    changed = rows[0].model_copy(deep=True)
    changed.attachment_capture['reason'] = reason
    saved = workflow.store.get(rid, 'develop/task/T1')
    workflow.store.checkpoint(rid, 'develop/task/T1', {**saved, 'checks': [changed.model_dump()]})
    failure = check_failure(plan.tasks[0].checks, [changed], task=plan.tasks[0], evidence_ref='develop/task/T1')
    # Inject the next observation after the previous recovery cycle ended.
    workflow.store.checkpoint(rid, 'recovery/active', None)
    workflow.recovery.submit(rid, failure)
    active, _, result = await admit(workflow, rid, plan, work)
    assert result is None
    assert recording.current(workflow, rid, 'T1') == original  # Restriction survives; exemption does not.
    async def waiting(question):
        raise StopAtDiagnosis()
    workflow.question_handler = waiting
    with pytest.raises(StopAtDiagnosis):
        await workflow.recovery.resume(rid)
    assert workflow.store.pending(rid)[0]['key'].startswith('evidence-capture/')
    assert not any(stage == 'recovery_diagnosis' for stage, _ in workflow.agent.contexts)
    assert workflow.store.get(rid, 'recovery/active')['attempt'] == active['attempt']


@pytest.mark.parametrize('defect', ['missing-report', 'invalid-report', 'failed-test', 'changed-command', 'foreign-report', 'unsafe-remaining-path'])
async def test_count_admission_rejects_incomplete_or_untrusted_facts(tmp_path, defect):
    workflow, rid, plan, work, rows = await captured_case(tmp_path)
    row = rows[0].model_dump()
    if defect == 'missing-report':
        row['report'] = {}
    elif defect == 'invalid-report':
        row['report_status'] = 'invalid'
    elif defect == 'failed-test':
        row['process_exit_code'] = 1
    elif defect == 'changed-command':
        row['command_hash'] = 'a' * 64
    elif defect == 'foreign-report':
        with workflow.store.connect() as db:
            db.execute('DELETE FROM artifact_owners WHERE hash=? AND run_id=?', (row['report']['hash'], rid))
    else:
        (Path(row['attachment_capture']['scratch']) / 'playwright-results' / 'unsafe').symlink_to(work / 'src.py')
    active = workflow.store.get(rid, 'recovery/active')
    failure = FailureRecord.model_validate(active['failure'])
    failure.evidence['checks'] = [row]
    saved = workflow.store.get(rid, 'develop/task/T1')
    workflow.store.checkpoint(rid, 'develop/task/T1', {**saved, 'checks': [row]})
    assert await recording.prepare(workflow, rid, active, failure, plan, work) is None
    assert recording.current(workflow, rid, 'T1') is None


class RepairRecordingAgent(RecordingAgent):
    mutation = 'recording'
    preservation = True

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == 'implement':
            path = 'src.py' if self.mutation == 'product' else 'tests/new.py' if self.mutation == 'new' else 'tests/playwright'
            old = (workspace / path).read_text() if (workspace / path).exists() else ''
            content = old.replace('range(3)', 'range(2)') if self.mutation == 'recording' else old
            if self.mutation == 'new':
                content = '# new file\n'
            if self.mutation == 'product':
                content = 'BUSINESS = 2\n'
            return ImplementationResult(summary='fixture recording representation correction',
                changes=[Change(path=path, content=content)])
        if issubclass(schema, recording.RecordingReview):
            assert fresh and context['review_kind'] == 'test_changes'
            return recording.RecordingReview(review=authority_review(context),
                recording=recording.RecordingAssessment(
                    binding_hash=context['recording_review_binding'], recording_only=True,
                    assertions_preserved=self.preservation, test_execution_preserved=True,
                    viewports_preserved=True, required_files_and_contents_preserved=True,
                    adopted_methods_preserved=True, reason='Fixture removes redundant physical copy only'))
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


async def run_repair(workflow, rid, plan, work):
    await workflow.recovery.resume(rid)
    saved = workflow.store.get(rid, 'develop/task/T1')
    permit = workflow.store.get(rid, saved['recovery_permit'])
    assert permit['action']['action'] == 'repair'
    assert permit['recording'].get('diagnosis_permit')
    return await workflow._task(rid, plan.tasks[0], git(work, 'rev-parse', 'HEAD'), plan, {'specification': {'requirements': [{'id': 'R1', 'description': 'preserve behavior'}]}, 'design': {}})


async def test_real_engine_recording_repair_preserves_test_count_and_runs_fresh_evidence(tmp_path):
    workflow, rid, plan, work, old = await captured_case(tmp_path)
    workflow.agent = RepairRecordingAgent()
    outcome = await run_repair(workflow, rid, plan, work)
    saved = workflow.store.get(rid, 'develop/task/T1')
    assert saved['phase'] == 'tested' and saved['commit'] == outcome
    assert workflow.store.get(rid, 'task_state/T1')['implementation'] == 'accepted'
    assert saved['checks'][0]['tests'] == 1 == old[0].tests
    assert saved['checks'][0]['exit_code'] == 0
    assert len(saved['checks'][0]['attachments']) == 2
    assert saved['checks'][0]['source_hash'] != old[0].source_hash
    assert (work / 'src.py').read_text() == 'BUSINESS = 1\n'
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 2
    assert saved['recording_review']['recording']['assertions_preserved']
    assert outcome


@pytest.mark.parametrize('mutation,preservation', [('product', True), ('new', True), ('noop', True), ('recording', False)])
async def test_real_engine_rejects_recording_scope_or_semantic_weakening_before_apply(tmp_path, mutation, preservation):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    agent = RepairRecordingAgent()
    agent.mutation, agent.preservation = mutation, preservation
    workflow.agent = agent
    before = tree_hash(work)
    with pytest.raises(RecoveryNeeded):
        await run_repair(workflow, rid, plan, work)
    assert tree_hash(work) == before
    assert workflow.store.get(rid, 'develop/task/T1')['phase'] == 'rejected'


async def test_final_capture_does_not_acquire_task_recording_permission(tmp_path):
    workflow, rid, plan, work, rows = await captured_case(tmp_path)
    failure = check_failure(plan.final_checks, rows, evidence_ref='verify/checks')
    active = {'failure': failure.model_dump(), 'attempt': 1}
    assert not failure.task_id
    assert await recording.prepare(workflow, rid, active, failure, plan, work) is None


async def test_all_noop_prepared_correction_rejected(tmp_path):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    _, _, receipt = await admit(workflow, rid, plan, work)
    patch = prepare_changes(work, [Change(path='tests/playwright', content=(work/'tests/playwright').read_text())],
                            plan.tasks[0].allowed_paths, protected=False)
    assert patch  # A real prepared list can still represent no actual change.
    with pytest.raises(PolicyViolation):
        recording.check_changes(receipt, patch)





def recording_patch(plan, failure_id):
    task = next(t for t in plan.tasks if t.id == 'T1').model_copy(deep=True)
    before = task.acceptance[0]
    after = before + '; remove duplicate recording copies while retaining every required file and assertion'
    task.acceptance[0] = after
    contract = plan.evidence_contract.model_copy(deep=True)
    contract.obligations[0].value = after
    correction = ContractCorrection(target='acceptance', task_id='T1', index=0,
        previous=before, replacement=after, derived_decision='Duplicate file recording representation',
        reason='Physical copy count exceeds verified limit', evidence_refs=['failure'],
        preserved_obligations=['all PNG and state JSON originals, assertions, cases, viewports and adopted methods'])
    return PlanPatch(base_revision=1, failure_ids=[failure_id], reason='correct recording method only',
        document='# Corrected recording plan', upsert_tasks=[task], contract_corrections=[correction],
        evidence_contract=contract)


class RecordingPlanAgent(RepairRecordingAgent):
    adopted = True

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if schema is Diagnosis:
            return Diagnosis(action='plan_patch', task_ids=['T1'], reason='Correct the formally adopted recording expression',
                             evidence_refs=['failure'])
        if schema is PlanPatch:
            return recording_patch(PlanResult.model_validate(context['plan']), context['failure']['id'])
        if issubclass(schema, recording.RecordingPatchReview):
            inner = evidence_review(context, review_for(PlanPatch.model_validate(context['patch'])))
            return recording.RecordingPatchReview(review=inner, recording=recording.RecordingAssessment(
                binding_hash=context['recording_review_binding'], recording_only=True,
                assertions_preserved=True, test_execution_preserved=True, viewports_preserved=True,
                required_files_and_contents_preserved=True, adopted_methods_preserved=self.adopted,
                reason='Fixture allows duplicate recording correction; actual upstream methods must remain'))
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


async def formal_case(tmp_path):
    workflow, rid, plan, work, results = await captured_case(tmp_path)
    workflow.agent = RecordingPlanAgent()
    workflow.workspaces.allocate(rid, 'integration', git(work, 'rev-parse', 'HEAD'))
    workflow.store.checkpoint(rid, 'clarify', {'document': 'Preserve all required PNG and state JSON evidence',
        'requirements': [{'id': 'R1', 'description': 'Retain business behavior and evidence',
                          'acceptance': ['actual PNG and state JSON are required']}]})
    context = admission_context(lambda key: workflow.store.get(rid, key))
    plan.evidence_contract = empty_contract(context)
    plan.evidence_contract.obligations = [EvidenceObligation(id='E1', target='acceptance', task_id='T1',
        index=0, value=plan.tasks[0].acceptance[0], origin='user', requirement_ids=['R1'],
        capability='test_file_attachments', capability_version=1, producer='VerificationService',
        production_stage='verification', required_stage='verification')]
    prior = TaskSpec(id='T0', origin_id='T0', title='already accepted', description='unchanged predecessor',
        requirement_ids=['R1'], allowed_paths=['prior.py'], acceptance=['unrelated accepted behavior'],
        checks=plan.tasks[0].checks)
    plan.tasks[0].depends_on = ['T0']
    plan.tasks.insert(0, prior)
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), 'task_origins': {'T0': 'T0', 'T1': 'T1'},
        'develop/done': ['T0'], 'task_state/T0': {'version': 1, 'revision': 1, 'implementation': 'accepted', 'evidence': 'valid'},
        'rounds/T0': {'round': 4, 'stalled': 0}, 'prototype_approval': {'fixture_original_approval': 'retained'}})
    seed_check_production(workflow, rid, plan)
    failure = check_failure(plan.tasks[1].checks, results, task=plan.tasks[1], evidence_ref='develop/task/T1')
    workflow.store.checkpoint(rid, 'recovery/active', None)
    workflow.recovery.submit(rid, failure)
    return workflow, rid, plan, work, failure


async def test_recording_patch_activation_preserves_unaffected_acceptance_and_requires_real_reverification(tmp_path):
    workflow, rid, plan, work, failure = await formal_case(tmp_path)
    await workflow.recovery.resume(rid)
    current = workflow.store.get(rid, 'plan_revision/current')
    assert current['revision'] == 2
    assert workflow.store.get(rid, 'develop/done') == ['T0']
    assert workflow.store.get(rid, 'rounds/T0')['round'] == 4
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 1
    assert workflow.store.get(rid, 'task_state/T1')['implementation'] == 'needs_repair'
    assert workflow.store.get(rid, 'task_state/T1')['evidence'] == 'stale'
    assert workflow.store.get(rid, 'prototype_approval') == {'fixture_original_approval': 'retained'}
    saved = workflow.store.get(rid, 'develop/task/T1')
    permit = workflow.store.get(rid, saved['recovery_permit'])
    assert permit['action']['action'] == 'repair' and permit['recording']['patch_permit']
    journal = workflow.store.get(rid, permit['recording']['journal'])
    assert journal['affected'] == ['T1'] and journal['reopen'] == ['T1']
    assert journal['plan']['evidence_contract']['obligations'][0]['origin'] == 'user'
    workflow.store.activate_plan_patch(rid, permit['recording']['journal'].removeprefix('plan_revision/journal/'))
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2


async def test_recording_patch_cannot_override_adopted_upstream_method(tmp_path):
    workflow, rid, plan, work, failure = await formal_case(tmp_path)
    workflow.agent.adopted = False
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan') == plan.model_dump()
    assert workflow.store.get(rid, 'develop/done') == ['T0']
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 1


@pytest.mark.parametrize('boundary', ['prepared', 'activated'])
@pytest.mark.parametrize('damage', [None, 'missing-review', 'changed-review', 'policy'])
async def test_recording_patch_interruption_rechecks_proof_without_duplicate_activation(tmp_path, monkeypatch, boundary, damage):
    workflow, rid, plan, work, failure = await formal_case(tmp_path)
    activate = workflow.store.activate_plan_patch
    def interrupt(*args):
        if boundary == 'activated':
            activate(*args)
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    key = f"plan_revision/journal/{failure.id}/{active['attempt']}"
    journal = workflow.store.get(rid, key)
    assert journal['phase'] == boundary
    if damage == 'missing-review':
        journal.pop('recording_review')
    elif damage == 'changed-review':
        journal['recording_review']['recording']['binding_hash'] = 'a' * 64
    elif damage == 'policy':
        cfg = workflow.config.load().model_dump(mode='json')
        cfg['attachment_capture']['max_files'] = 3
        write_json(workflow.config.path, cfg)
    workflow.store.checkpoint(rid, key, journal)
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', activate)
    if damage:
        with pytest.raises(AA2Error):
            await workflow.recovery.resume(rid)
    else:
        await workflow.recovery.resume(rid)
        workflow.store.activate_plan_patch(rid, key.removeprefix('plan_revision/journal/'))
        assert workflow.store.get(rid, key)['phase'] == 'activated'
        assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 1
    assert workflow.store.get(rid, 'develop/done') == ['T0']


@pytest.mark.parametrize('scope', [False, True])
async def test_local_attachment_value_sync_requires_controller_recording_scope(tmp_path, scope):
    workflow, rid, plan, work, failure = await formal_case(tmp_path)
    _, _, record = await admit(workflow, rid, plan, work)
    patch = recording_patch(plan, failure.id)
    _, affected, reopen = apply_patch(plan, patch, 1, {failure.id}, {'T0': 'T0', 'T1': 'T1'},
                                      recording_scope=record if scope else None)
    assert affected == ({'T1'} if scope else {'T0', 'T1'})
    assert reopen == {'T1'}


@pytest.mark.parametrize('answered_after_preparation', [False, True])
async def test_existing_human_answer_is_never_reinterpreted_as_recording_permission(tmp_path, answered_after_preparation):
    workflow, rid, plan, work, rows = await captured_case(tmp_path)
    identity = digest([r.model_dump() for r in rows])
    question = workflow.store.request(rid, 'evidence-capture/' + identity + '/0', 'Existing resource question', identity)
    if answered_after_preparation:
        await admit(workflow, rid, plan, work)
    workflow.store.answer(question['id'], 'Do not change any recording method')
    if answered_after_preparation:
        with pytest.raises(StateConflict):
            await workflow.recovery.resume(rid)
    else:
        assert (await admit(workflow, rid, plan, work))[2] is None
    assert workflow.store.question(question['id'])['answer'] == 'Do not change any recording method'
    assert workflow.store.question(question['id'])['superseded'] == 0


async def test_rejected_semantic_recording_reason_reaches_next_real_diagnosis(tmp_path):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    workflow.agent = RepairRecordingAgent()
    workflow.agent.preservation = False
    with pytest.raises(RecoveryNeeded) as caught:
        await run_repair(workflow, rid, plan, work)
    failure = caught.value.failure
    assert failure.evidence['recording_review']['recording']['assertions_preserved'] is False
    reason = failure.evidence['recording_review']['recording']['reason']
    workflow.recovery.submit(rid, failure)
    async def stop(context, *args):
        assert context['failure']['evidence']['recording_review']['recording']['reason'] == reason
        raise StopAtDiagnosis()
    workflow.recovery._diagnose = stop
    with pytest.raises(StopAtDiagnosis):
        await workflow.recovery.resume(rid)


@pytest.mark.parametrize('damage', [None, 'review', 'patch', 'permit'])
async def test_prepared_recording_candidate_reentry_checks_scope_and_review(tmp_path, monkeypatch, damage):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    workflow.agent = RepairRecordingAgent()
    import auto_agents_2.engine as engine_module
    original = engine_module.apply_prepared
    def interrupt(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(engine_module, 'apply_prepared', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await run_repair(workflow, rid, plan, work)
    saved = workflow.store.get(rid, 'develop/task/T1')
    assert saved['patch'] and saved['recording_review']
    if damage == 'review':
        saved['recording_review']['recording']['binding_hash'] = '0' * 64
    elif damage == 'patch':
        saved['patch'][0]['path'] = 'src.py'
    elif damage == 'permit':
        permit = workflow.store.get(rid, saved['recovery_permit'])
        permit['recording'] = {}
        workflow.store.checkpoint(rid, saved['recovery_permit'], permit)
    workflow.store.checkpoint(rid, 'develop/task/T1', saved)
    monkeypatch.setattr(engine_module, 'apply_prepared', original)
    before = tree_hash(work)
    if damage:
        with pytest.raises(AA2Error):
            await workflow._task(rid, plan.tasks[0], git(work, 'rev-parse', 'HEAD'), plan, {'specification': {}})
        assert tree_hash(work) == before
    else:
        await workflow._task(rid, plan.tasks[0], git(work, 'rev-parse', 'HEAD'), plan, {'specification': {}})
        assert workflow.store.get(rid, 'task_state/T1')['implementation'] == 'accepted'
        assert workflow.store.get(rid, 'rounds/T1')['round'] == 2


@pytest.mark.parametrize('change', ['command', 'minimum', 'scope', 'obligation-id', 'capability', 'other-task', 'stale', 'drop-obligation'])
async def test_recording_plan_scope_cannot_change_other_contracts(tmp_path, change):
    workflow, rid, plan, work, failure = await formal_case(tmp_path)
    _, _, record = await admit(workflow, rid, plan, work)
    patch = recording_patch(plan, failure.id)
    if change == 'command':
        patch.upsert_tasks[0].checks[0].argv = ['true']
    elif change == 'minimum':
        patch.upsert_tasks[0].checks[0].expected_tests = 0
    elif change == 'scope':
        patch.upsert_tasks[0].allowed_paths.append('other.py')
    elif change == 'obligation-id':
        patch.evidence_contract.obligations[0].id = 'different'
    elif change == 'capability':
        patch.evidence_contract.obligations[0].capability = 'installed_dependency_snapshot'
    elif change == 'other-task':
        patch.upsert_tasks.append(plan.tasks[0])
    elif change == 'stale':
        patch.base_revision = 2
    else:
        patch.evidence_contract.obligations = []
    with pytest.raises(AA2Error):
        apply_patch(plan, patch, 1, {failure.id}, {'T0': 'T0', 'T1': 'T1'}, recording_scope=record)


@pytest.mark.parametrize('stage,kind', [('design', None), ('plan', None), ('implement', None),
                                     ('review', 'test_changes'), ('review', 'plan_patch'),
                                     ('recovery_diagnosis', None), ('verify', None)])
async def test_real_projection_preserves_policy_and_review_binding(tmp_path, stage, kind):
    from auto_agents_2.collection import capture_contract
    from auto_agents_2.context_plan import ContextPlanner
    workflow = engine(tmp_path, RecordingAgent())
    rid = workflow.store.admit('projection fixture', {})['id']
    contract = capture_contract(workflow.store.root)
    context = {'controller_attachment_capture': contract,
               'recording_review_binding': 'fixed-binding', 'recording_review_instruction': 'preserve all assertions'}
    if kind:
        context['review_kind'] = kind
    view = ContextPlanner(workflow.artifacts).prepare(stage, context, tmp_path, run_id=rid).payload
    assert view['controller_attachment_capture'] == contract
    assert view['recording_review_binding'] == 'fixed-binding'
    assert view['recording_review_instruction'] == 'preserve all assertions'


@pytest.mark.parametrize('schema', [recording.RecordingReview, recording.RecordingPatchReview])
def test_nested_recording_review_schema_requires_existing_authority_assessment(schema):
    from auto_agents_2.review_authority import review_schema
    augmented = review_schema(schema)
    inner = augmented.model_fields['review'].annotation
    assert inner.model_fields['authority'].is_required()
    assert 'recording' in augmented.model_fields


@pytest.mark.parametrize('target', ['diagnosis', 'repair'])
async def test_missing_capture_admission_cannot_downgrade_a_recording_permit(tmp_path, target):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    await workflow.recovery.resume(rid)
    repair_key = workflow.store.get(rid, 'develop/task/T1')['recovery_permit']
    repair = workflow.store.get(rid, repair_key)
    key = repair['recording']['diagnosis_permit'] if target == 'diagnosis' else repair_key
    permit = workflow.store.get(rid, key)
    permit['recording'].pop('observation')
    permit['recording'].pop('observation_hash')
    workflow.store.checkpoint(rid, key, permit)
    if target == 'diagnosis':
        repair['recording']['diagnosis_hash'] = digest(permit)
        workflow.store.checkpoint(rid, repair_key, repair)
    with pytest.raises(StateConflict):
        workflow.recovery_policy.validate_consumption(rid, repair_key, expected={'repair'})


async def test_receipt_reentry_inventory_uses_managed_cancellable_helper(tmp_path, monkeypatch):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    await admit(workflow, rid, plan, work)
    calls = []
    async def cancelled(root, request, **kwargs):
        calls.append((request, kwargs))
        raise asyncio.CancelledError()
    monkeypatch.setattr(recording, 'run_collection', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await admit(workflow, rid, plan, work)
    assert len(calls) == 1 and calls[0][0]['action'] == 'attachment_inventory'
    assert calls[0][1]['timeout'] == workflow.config.load().attachment_capture.timeout_seconds
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 1


async def test_missing_entire_recording_proof_cannot_downgrade_initial_diagnosis(tmp_path):
    workflow, rid, plan, work, _ = await captured_case(tmp_path)
    async def stop(*args):
        raise StopAtDiagnosis()
    workflow.recovery._diagnose = stop
    with pytest.raises(StopAtDiagnosis):
        await workflow.recovery.resume(rid)
    key = workflow.store.get(rid, 'recovery/active')['diagnosis_permit']
    permit = workflow.store.get(rid, key)
    permit['recording'] = {}
    workflow.store.checkpoint(rid, key, permit)
    with pytest.raises(StateConflict):
        workflow.recovery_policy.validate_consumption(rid, key, expected={'diagnose'})
