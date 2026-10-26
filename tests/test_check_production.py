"""Finite closure admission and real producer evidence, not natural-language inference."""
import copy
import json
import sys

import pytest
from evidence_test_fixtures import empty_contract, evidence_review
from test_evidence_admission import context
from test_workflow import CodingAgent, engine

from auto_agents_2.builds import verification_layout
from auto_agents_2.check_production import (
    ACTIVE_KEY,
    ProductionGap,
    activation_updates,
    active_receipt,
    additional_affected,
    observe,
    protocol,
    validate_mapping,
)
from auto_agents_2.environment import Environment
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence_admission import admission_record, validate_admission_review
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.models import (
    CheckProducer,
    CheckProductionAssessment,
    CheckProductionBinding,
    Command,
    PlanResult,
    TaskSpec,
)


def plan_and_review():
    ctx = context()
    command = Command(argv=[sys.executable, '-c', 'print("checked")'])
    plan = PlanResult(summary='scope', document='Scope fixture', evidence_contract=empty_contract(ctx),
        tasks=[TaskSpec(id='A', origin_id='A', title='A', description='A', requirement_ids=['R1'],
            allowed_paths=['app.py'], acceptance=['The declared check passes'], checks=[command])],
        final_checks=[Command(argv=[sys.executable, '-c', 'print("final only")'])], runtime_probes=[command])
    ctx.update(plan=plan.model_dump(), review_kind='plan')
    review = evidence_review(ctx)
    row = review.evidence_assessment.check_production.bindings[0]
    row.kind, row.producers = 'command', [CheckProducer(index=0, command=command)]
    return plan, review, ctx


@pytest.mark.parametrize('bad', ['missing', 'omitted', 'duplicate', 'value', 'task', 'index', 'producer_index',
    'same_argv_different_inputs', 'same_argv_lower_tests', 'future', 'final_as_task', 'review_with_files', 'foreign_protocol'])
def test_new_admission_rejects_invalid_mapping_even_when_review_approved(bad):
    plan, review, ctx = plan_and_review()
    assessment = review.evidence_assessment.check_production
    row = assessment.bindings[0]
    if bad == 'missing':
        review.evidence_assessment.check_production = None
    elif bad == 'omitted':
        assessment.bindings.pop()
    elif bad == 'duplicate':
        assessment.bindings.append(row.model_copy(deep=True))
    elif bad in {'value', 'task', 'index'}:
        setattr(row, {'value': 'value', 'task': 'task_id', 'index': 'index'}[bad], 99 if bad == 'index' else 'not-the-clause')
    elif bad == 'producer_index':
        row.producers[0].index = 77
    elif bad == 'same_argv_different_inputs':
        row.producers[0].command = row.producers[0].command.model_copy(update={'inputs': ['other.py']})
    elif bad == 'same_argv_lower_tests':
        row.producers[0].command = row.producers[0].command.model_copy(update={'expected_tests': 1})
    elif bad == 'future':
        row.producers = []
    elif bad == 'final_as_task':
        row.producers[0].command = plan.final_checks[0]
    elif bad == 'review_with_files':
        row.kind = 'review'
    else:
        ctx['controller_check_production']['version'] = 9
    with pytest.raises(AA2Error):
        validate_admission_review(plan, review, ctx)


def test_final_only_command_is_not_required_in_each_task_and_protocol_is_not_fact():
    plan, review, ctx = plan_and_review()
    validate_admission_review(plan, review, ctx)
    assert plan.final_checks[0] not in plan.tasks[0].checks
    refs = controller_context_catalog(ctx)
    assert protocol()['hash'] in refs.protocol and protocol()['hash'] not in refs.factual


def test_metadata_downgrade_needs_formal_correction_and_delta_invalidates_dependents():
    plan, review, ctx = plan_and_review()
    before = admission_record(plan, review, ctx)
    from auto_agents_2.check_production import accepted_projection
    previous = accepted_projection(before)
    changed = review.model_copy(deep=True)
    row = changed.evidence_assessment.check_production.bindings[0]
    row.kind, row.producers = 'review', []
    patch_context = {**ctx, 'previous_plan': plan.model_dump(), 'patch': {}, 'accepted_check_production': previous}
    with pytest.raises(AA2Error, match='downgrade'):
        validate_admission_review(plan, changed, patch_context)
    same = review.evidence_assessment.check_production.model_copy(deep=True)
    same.bindings[0].reason = 'Another explanation, no changed obligation'
    assert additional_affected(plan, plan, previous, same) == set()
    same.bindings[0].attachment_formats = ['json']
    assert additional_affected(plan, plan, previous, same) == {'A'}
    assert additional_affected(plan, plan, None, same) == {'A'}


def test_required_marker_prevents_missing_or_stale_receipt_legacy_fallback():
    plan, review, ctx = plan_and_review()
    record = admission_record(plan, review, ctx)
    rows = {'plan': plan.model_dump(), **activation_updates(record)}
    assert active_receipt(rows.get) == record
    rows[ACTIVE_KEY] = None
    with pytest.raises(AA2Error):
        active_receipt(rows.get)
    rows[ACTIVE_KEY] = copy.deepcopy(record)
    rows[ACTIVE_KEY]['candidate_hash'] = 'stale'
    with pytest.raises(AA2Error):
        active_receipt(rows.get)
    assert active_receipt({'plan': plan.model_dump()}.get) is None


def test_build_mapping_uses_the_same_merged_normalization_pass(tmp_path):
    first = Command(argv=['npm', 'run', 'build'], inputs=['src'], outputs=['dist'], timeout=60)
    second = first.model_copy(update={'inputs': ['styles'], 'timeout': 120})
    consumer = Command(argv=['node', 'probe.mjs'], inputs=['dist'])
    ordered, links, _ = verification_layout([first, consumer], tmp_path, [second])
    assert links[0] == ordered[0]
    assert ordered[0].inputs == ['src', 'styles'] and ordered[0].timeout == 120
    assert ordered[1] == consumer and links[1] == consumer


async def test_real_check_and_archive_bytes_close_only_the_bound_producer(tmp_path):
    plan, review, ctx = plan_and_review()
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('isolated closure', {})['id']
    row = review.evidence_assessment.check_production.bindings[0]
    row.kind, row.attachment_formats = 'attachments', ['json', 'png']
    record = admission_record(plan, review, ctx)
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), **activation_updates(record)})
    env = Environment(__import__('pathlib').Path(sys.prefix), 'fixture')
    commands = workflow._task_commands(rid, plan.tasks[0], tmp_path, plan)
    checks = await workflow._checks(rid, commands, tmp_path, env, plan.tasks[0])
    # Real command results + actual archived originals. Not a complete browser test.
    attachments = [
        {'path': 'misleading.bin', 'artifact': workflow.artifacts.put(b'{"actual":true}', 'test_diagnostic')},
        {'path': 'also.bin', 'artifact': workflow.artifacts.put(b'\x89PNG\r\n\x1a\nfixture', 'test_diagnostic')}]
    checks[0].attachments = attachments
    observation = observe(lambda key: workflow.store.get(rid, key), commands, checks, tmp_path, env,
                          workflow.artifacts, task=plan.tasks[0], phase='task')
    assert len(observation['closed'][0]['attachment_hashes']) == 2
    checks[0].attachments = [{'path': 'fake.png', 'artifact': workflow.artifacts.put(b'not a PNG', 'test_diagnostic')}]
    with pytest.raises(ProductionGap, match='not actually produced'):
        observe(lambda key: workflow.store.get(rid, key), commands, checks, tmp_path, env,
                workflow.artifacts, task=plan.tasks[0], phase='task')


@pytest.mark.parametrize('damage', ['missing', 'damaged', 'stale_plan'])
async def test_actual_engine_task_and_final_gates_preserve_receipt_state_conflict(tmp_path, damage):
    plan, review, ctx = plan_and_review()
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('isolated closure', {})['id']
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), **activation_updates(admission_record(plan, review, ctx))})
    env = Environment(__import__('pathlib').Path(sys.prefix), 'fixture')
    commands = workflow._task_commands(rid, plan.tasks[0], tmp_path, plan)
    checks = await workflow._checks(rid, commands, tmp_path, env, plan.tasks[0])
    observed = workflow._require_task_evidence(rid, plan.tasks[0], {}, commands, checks, tmp_path, env)
    assert observed['check_production']['closed'][0]['task_id'] == 'A'
    final_commands = workflow._candidate_checks(plan, tmp_path)
    final = await workflow._checks(rid, final_commands, tmp_path, env)
    assert workflow._require_candidate_evidence(rid, final_commands, final, tmp_path, env, phase='final')['check_production']
    receipt = workflow.store.get(rid, ACTIVE_KEY)
    if damage == 'missing':
        receipt = None
    elif damage == 'damaged':
        receipt['binding'] = 'damaged'
    else:
        from auto_agents_2.util import digest
        receipt['candidate_hash'] = 'different-plan'
        receipt['binding'] = digest({k: v for k, v in receipt.items() if k != 'binding'})
    workflow.store.checkpoints(rid, {ACTIVE_KEY: receipt, 'rounds/A': {'round': 4}})
    keys = [ACTIVE_KEY, 'plan', 'rounds/A', 'task_state/A', 'develop/task/A', 'verify/checks']
    before = {key: workflow.store.get(rid, key) for key in keys}
    from auto_agents_2.faults import StateConflict
    with pytest.raises(StateConflict):
        workflow._require_task_evidence(rid, plan.tasks[0], {}, commands, checks, tmp_path, env)
    with pytest.raises(StateConflict):
        workflow._require_candidate_evidence(rid, final_commands, final, tmp_path, env, phase='final')
    assert {key: workflow.store.get(rid, key) for key in keys} == before


async def test_actual_missing_attachment_remains_evidence_recovery_not_state_conflict(tmp_path):
    from auto_agents_2.recovery import RecoveryNeeded
    plan, review, ctx = plan_and_review()
    row = review.evidence_assessment.check_production.bindings[0]
    row.kind, row.attachment_formats = 'attachments', ['json']
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('actual missing file', {})['id']
    receipt = admission_record(plan, review, ctx)
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), 'rounds/A': {'round': 4}, **activation_updates(receipt)})
    env = Environment(__import__('pathlib').Path(sys.prefix), 'fixture')
    commands = workflow._task_commands(rid, plan.tasks[0], tmp_path, plan)
    checks = await workflow._checks(rid, commands, tmp_path, env, plan.tasks[0])
    with pytest.raises(RecoveryNeeded) as error:
        workflow._require_task_evidence(rid, plan.tasks[0], {}, commands, checks, tmp_path, env)
    assert error.value.failure.kind == 'evidence'
    assert workflow.store.get(rid, ACTIVE_KEY) == receipt
    assert workflow.store.get(rid, 'rounds/A') == {'round': 4}


def test_frozen_demo5_original_build_gap_and_legitimate_final_only_tsc():
    from pathlib import Path
    source = json.loads((Path(__file__).parent/'fixtures/demo5-check-production.json').read_text())
    original = PlanResult.model_validate(source['revision_1'])
    corrected = PlanResult.model_validate(source['revision_2'])
    assert '生产构建成功' in original.tasks[0].acceptance[8]
    build = corrected.tasks[0].checks[-1]
    assert build not in original.tasks[0].checks and build in original.runtime_prepare
    rows = []
    for task in original.tasks:
        rows.extend(CheckProductionBinding(target='acceptance', task_id=task.id, index=i, value=v,
            kind='review', reason='Archive replay: only the known build gap is classified here') for i,v in enumerate(task.acceptance))
    rows.extend(CheckProductionBinding(target='final_checks', index=i, value=c, kind='command',
        producers=[CheckProducer(index=i, command=c)], reason='Final producer') for i,c in enumerate(original.final_checks))
    # The final-only tsc is legal. This does not claim semantic completeness of the fixture mapping.
    mapping = CheckProductionAssessment(bindings=rows)
    # Existing structured attachment rows must cite their existing task producers.
    for obligation in original.evidence_contract.obligations:
        if obligation.capability == 'test_file_attachments' and obligation.target == 'acceptance':
            item = next(r for r in rows if r.task_id == obligation.task_id and r.index == obligation.index)
            task = next(t for t in original.tasks if t.id == obligation.task_id)
            item.kind, item.producers = 'attachments', [CheckProducer(index=0, command=task.checks[0])]
    validate_mapping(original, mapping)
    row = rows[8]
    row.kind, row.producers = 'build', [CheckProducer(index=3, command=build)]
    with pytest.raises(AA2Error, match='full command'):
        validate_mapping(original, mapping)
    validate_mapping(corrected, mapping)


@pytest.mark.parametrize('target', ['checks', 'final_checks', 'runtime_prepare', 'runtime_probes'])
async def test_existing_structured_file_obligation_requires_real_files_at_its_stage(tmp_path, target):
    from auto_agents_2.models import EvidenceObligation
    plan, review, ctx = plan_and_review()
    task = plan.tasks[0]
    if target == 'runtime_prepare':
        plan.runtime_prepare = [Command(argv=[sys.executable, '-c', 'print("prepare")'])]
    command = task.checks[0] if target == 'checks' else getattr(plan, target)[0]
    plan.evidence_contract.obligations = [EvidenceObligation(id='files', target=target,
        task_id=task.id if target == 'checks' else '', index=0, value=command,
        origin='model_derived', requirement_ids=['R1'], capability='test_file_attachments',
        capability_version=1, producer='VerificationService', production_stage='verification', required_stage='verification')]
    ctx['plan'] = plan.model_dump()
    review = evidence_review(ctx)
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('file evidence', {})['id']
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), **activation_updates(admission_record(plan, review, ctx))})
    env = Environment(__import__('pathlib').Path(sys.prefix), 'fixture')
    commands = workflow._task_commands(rid, task, tmp_path, plan) if target == 'checks' else workflow._candidate_checks(plan, tmp_path)
    checks = await workflow._checks(rid, commands, tmp_path, env, task if target == 'checks' else None)
    kwargs = {'task': task, 'phase': 'task'} if target == 'checks' else {'phase': 'final'}
    with pytest.raises(ProductionGap, match='not actually produced'):
        observe(lambda key: workflow.store.get(rid, key), commands, checks, tmp_path, env, workflow.artifacts, **kwargs)
    selected = next(i for i,c in enumerate(commands) if c == command)
    checks[selected].attachments = [{'path': 'actual.json', 'artifact': workflow.artifacts.put(b'{"actual":1}', 'test_diagnostic')}]
    observed = observe(lambda key: workflow.store.get(rid, key), commands, checks, tmp_path, env, workflow.artifacts, **kwargs)
    assert next(r for r in observed['closed'] if r.get('obligation_id') == 'files')['check_indexes'] == [selected]
    if target != 'checks':
        task_commands = workflow._task_commands(rid, task, tmp_path, plan)
        task_checks = await workflow._checks(rid, task_commands, tmp_path, env, task)
        task_observed = observe(lambda key: workflow.store.get(rid, key), task_commands, task_checks, tmp_path, env,
                                workflow.artifacts, task=task, phase='task')
        assert not any(r.get('obligation_id') == 'files' for r in task_observed['closed'])


async def test_real_report_and_attachment_collector_feed_engine_closure(tmp_path):
    # A deterministic local test runner exercises the real report/file collector;
    # these bytes do not claim to be a browser screenshot of a real application.
    runner = tmp_path/'playwright'
    runner.write_text(f'#!{sys.executable}\n' + """import os,pathlib
p=pathlib.Path(os.environ['PLAYWRIGHT_JUNIT_OUTPUT_FILE']); p.write_text('<testsuite><testcase name="ok"/></testsuite>')
d=p.parent/'playwright-results'; d.mkdir(); (d/'state.json').write_text('{"visible":true}')
""")
    runner.chmod(0o755)
    plan, review, ctx = plan_and_review()
    command = Command(argv=[str(runner), 'test'], expected_tests=1)
    plan.tasks[0].checks = [command]
    ctx['plan'] = plan.model_dump()
    review = evidence_review(ctx)
    row = review.evidence_assessment.check_production.bindings[0]
    row.kind, row.producers, row.attachment_formats = 'attachments', [CheckProducer(index=0, command=command)], ['json']
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('actual capture', {})['id']
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), **activation_updates(admission_record(plan, review, ctx))})
    env = Environment(__import__('pathlib').Path(sys.prefix), 'fixture')
    commands = workflow._task_commands(rid, plan.tasks[0], tmp_path, plan)
    checks = await workflow._checks(rid, commands, tmp_path, env, plan.tasks[0])
    assert checks[0].tests == 1 and checks[0].attachments
    observed = workflow._require_task_evidence(rid, plan.tasks[0], {}, commands, checks, tmp_path, env)
    assert observed['check_production']['closed'][0]['attachment_hashes']
    ref = checks[0].attachments[0]['artifact']
    assert json.loads(workflow.artifacts.bytes(ref)) == {'visible': True}
    original = workflow.artifacts.root/ref['hash'][:2]/ref['hash']
    original.write_bytes(b'corrupt')  # Isolated test fault, never a historical run.
    from auto_agents_2.recovery import RecoveryNeeded
    with pytest.raises(RecoveryNeeded):
        workflow._require_task_evidence(rid, plan.tasks[0], {}, commands, checks, tmp_path, env)


@pytest.mark.parametrize('approved', [False, True])
def test_missing_mapping_in_refusal_never_activates_or_masks_real_user_capability_gap(approved):
    from test_evidence_admission import candidate, reviewed

    from auto_agents_2.faults import HumanIntervention
    plan = candidate()
    plan.evidence_contract.obligations[0].origin = 'user'
    plan.evidence_contract.obligations[0].capability = 'effective_solver_configuration'
    review, ctx = reviewed(plan, unsupported_user_obligations=['env'], capabilities_sufficient=False)
    review.approved = approved
    review.evidence_assessment.check_production = None
    if approved:
        with pytest.raises(AA2Error, match='mapping is missing'):
            validate_admission_review(plan, review, ctx)
    else:
        with pytest.raises(HumanIntervention) as error:
            validate_admission_review(plan, review, ctx)
        assert error.value.reason_code == 'evidence_capability_unavailable'


async def test_actual_engine_returns_missing_producer_to_plan_then_closes_real_check(tmp_path):
    from authority_review_fixtures import authority_review
    class MissingBuild(CodingAgent):
        planned = 0
        rejected = False
        build = Command(argv=[sys.executable, '-c', 'print("actual build fixture")'])
        async def invoke(self, stage, ctx, schema, *args, **kwargs):
            result = await super().invoke(stage, ctx, schema, *args, **kwargs)
            if stage == 'plan':
                self.planned += 1
                result.tasks[0].acceptance.append('Run the declared build successfully before task acceptance')
                result.runtime_prepare.append(self.build)
                if self.planned > 1:
                    result.tasks[0].checks.append(self.build)
            if ctx.get('review_kind') == 'plan':
                actual = PlanResult.model_validate(ctx['plan'])
                complete = self.build in actual.tasks[0].checks
                if not complete:
                    self.rejected = True
                    result = evidence_review(ctx, authority_review(ctx, approved=False, summary='Required task build has no scheduled producer'))
                row = result.evidence_assessment.check_production.bindings[1]
                row.kind = 'build'
                row.producers = [CheckProducer(index=1, command=self.build)] if complete else []
            return result
    project = tmp_path/'project'
    project.mkdir()
    request = tmp_path/'request.md'
    request.write_text('Add numbers and run the declared build before task acceptance.')
    agent = MissingBuild()
    workflow = engine(project, agent)
    result = await workflow.start(str(request))
    assert result['data']['outcome'] == 'DELIVERED' and agent.rejected and agent.planned == 2
    rid = result['id']
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 1
    receipt = active_receipt(lambda key: workflow.store.get(rid, key))
    assert receipt['check_production']['bindings'][1]['kind'] == 'build'
    checks = workflow.store.get(rid, 'develop/task/T1')['checks']
    assert any(c['argv'] == agent.build.argv and 'actual build fixture' in c['output'] for c in checks)
