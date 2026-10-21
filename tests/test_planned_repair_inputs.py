"""Missing candidate inputs are not missing permission or fabricated source evidence."""
import asyncio
from copy import deepcopy

import pytest
from test_decisions import ApprovingAgent
from test_recovery_contracts import failure, plan
from test_recovery_policy import ready

from auto_agents_2.errors import AA2Error, ProtectedChange
from auto_agents_2.execution_stages import stage_contract
from auto_agents_2.models import Change
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.recovery import Diagnosis, RepairTarget, adjudicate, evidence_references
from auto_agents_2.recovery_identity import invocation_contract
from auto_agents_2.util import digest
from auto_agents_2.workspace import prepare_changes


def fixture():
    accepted = plan()
    task = accepted.tasks[2]
    task.allowed_paths = ['app.py', 'tests/contracts/', 'tests/test_new.py', 'unused.py', 'support/']
    task.checks[0].inputs = ['app.py', 'tests/contracts', 'tests/test_new.py']
    record = failure(evidence={'task': task.model_dump(), 'checks': [], 'review': None,
                              'environment_error': None, 'candidate_error': None})
    context = {'failure': record.model_dump(), 'plan': accepted.model_dump(),
               'project': {'inventory': [], 'files': []}, 'controller_stage_contract': stage_contract(),
               'allowed_actions': {'repair': {}, 'diagnose': {}}}
    target = RepairTarget(task_id='T3', paths=['app.py', 'tests/contracts/', 'tests/test_new.py'],
                          evidence_refs=['plan.tasks[2]', 'failure.evidence'],
                          reason='Generate the declared candidate and its independent regression expectations')
    diagnosis = Diagnosis(action='repair', task_ids=['T3'], reason=target.reason,
                          evidence_refs=target.evidence_refs, repair_targets=[target])
    return accepted, record, context, diagnosis


def assess(accepted, record, context, diagnosis):
    return adjudicate(record, plan=accepted, diagnosis=diagnosis, evidence_context=context)


@pytest.mark.parametrize('path', ['app.py', 'tests/contracts/', 'tests/contracts/new.json', 'tests/test_new.py'])
def test_planned_missing_input_can_be_proposed_without_minting_file_evidence(path):
    accepted, record, context, diagnosis = fixture()
    diagnosis.repair_targets[0].paths = [path]
    before = deepcopy((accepted.model_dump(), record.model_dump(), context))
    assert path not in evidence_references(record, context=context)
    result = assess(accepted, record, context, diagnosis)
    assert result.action == 'repair' and result.task_ids == ['T3'] and result.budget_owners == ['T3']
    assert result.repair_targets[0].paths == [path]
    assert (accepted.model_dump(), record.model_dump(), context) == before
    assert path not in evidence_references(record, context=context)


@pytest.mark.parametrize('ref', ['plan.tasks[2]', 'plan.tasks[2].checks', 'plan.tasks[2].checks[0]',
                               'failure.evidence.task', 'failure.evidence.task.checks[0].inputs'])
def test_planned_paths_require_a_citation_to_the_owning_contract(ref):
    accepted, record, context, diagnosis = fixture()
    diagnosis.repair_targets[0].evidence_refs = [ref]
    assert assess(accepted, record, context, diagnosis).action == 'repair'


@pytest.mark.parametrize('change', ['no_task', 'stale_task', 'stale_plan', 'no_plan', 'allowed_only',
                                  'other_task_reference', 'only_protocol', 'only_proposal', 'unknown_reference',
                                  'history_only', 'unrelated_input', 'not_in_scope', 'empty_reason', 'other_owner'])
def test_permission_or_claims_alone_do_not_authorize_missing_targets(change):
    accepted, record, context, diagnosis = fixture()
    target = diagnosis.repair_targets[0]
    if change == 'no_task':
        record.evidence.pop('task')
    elif change == 'stale_task':
        record.evidence['task']['acceptance'].append('old contract obligation')
    elif change == 'stale_plan':
        context['plan']['tasks'][2]['checks'][0]['expected_tests'] += 1
    elif change == 'no_plan':
        context.pop('plan')
        diagnosis.evidence_refs = ['failure.evidence']
        target.evidence_refs = ['failure.evidence.task']
    elif change == 'allowed_only':
        target.evidence_refs = ['plan.tasks[2].allowed_paths']
    elif change == 'other_task_reference':
        target.evidence_refs = ['plan.tasks[1]']
    elif change == 'only_protocol':
        target.evidence_refs = ['controller_stage_contract']
    elif change == 'only_proposal':
        context['failure']['proposal'] = {'changes': [{'path': 'app.py'}]}
        target.evidence_refs = ['failure.proposal.changes']
    elif change == 'unknown_reference':
        target.evidence_refs = ['not-supplied']
    elif change == 'history_only':
        context['source_history'] = {'status': 'available', 'scope': target.paths}
        target.evidence_refs = ['source_history']
    elif change == 'unrelated_input':
        target.paths = ['unused.py']  # Owned, but no declared verification input requires it.
    elif change == 'not_in_scope':
        accepted.tasks[2].checks[0].inputs.append('outside.py')
        record.evidence['task'] = accepted.tasks[2].model_dump()
        context['plan'] = accepted.model_dump()
        target.paths = ['outside.py']
    elif change == 'empty_reason':
        target.reason = '  '
    elif change == 'other_owner':
        target.task_id = 'T2'
        diagnosis.task_ids = ['T2']
        accepted.tasks[1].allowed_paths = ['app.py', 'tests/']
        accepted.tasks[1].checks[0].inputs = ['app.py', 'tests/']
        context['plan'] = accepted.model_dump()
        target.evidence_refs = ['plan.tasks[1]']
    with pytest.raises(AA2Error, match='outside the evidenced existing task scope'):
        assess(accepted, record, context, diagnosis)


@pytest.mark.parametrize('path', ['../outside.py', 'tests/../outside.py', '/tmp/outside.py', '.', '',
                                'tests//new.py', './app.py', 'tests/*.py', 'tests/?', 'tests/[abc]',
                                'C:/outside.py', 'tests\\new.py', '.aa2/state.sqlite', '.git/config',
                                '.codex/config.toml', '.env', 'private/.env', 'DESIGN.md',
                                'docs/aa2/plan.md', 'design/prototypes/index.html'])
def test_even_broad_plan_and_inputs_do_not_allow_unsafe_target_selectors(path):
    accepted, record, context, diagnosis = fixture()
    accepted.tasks[2].allowed_paths = ['*']
    accepted.tasks[2].checks[0].inputs = ['*']
    record.evidence['task'] = accepted.tasks[2].model_dump()
    context['plan'] = accepted.model_dump()
    diagnosis.repair_targets[0].paths = [path]
    with pytest.raises(AA2Error, match=r'T3.paths\[0\]'):
        assess(accepted, record, context, diagnosis)


def test_rejection_identifies_actual_unjustified_path_and_required_evidence():
    accepted, record, context, diagnosis = fixture()
    diagnosis.repair_targets[0].paths.append('unused.py')
    with pytest.raises(AA2Error, match=r"T3.paths\[3\] \('unused.py'\).*declared check inputs"):
        assess(accepted, record, context, diagnosis)


def test_current_file_repair_and_cross_task_proof_keep_legacy_meaning():
    accepted, record, context, diagnosis = fixture()
    accepted.tasks[1].allowed_paths = ['existing.py']
    context['plan'] = accepted.model_dump()
    record.evidence_refs.append('existing.py')
    diagnosis.task_ids = ['T2']
    diagnosis.repair_targets = [RepairTarget(task_id='T2', paths=['existing.py'],
                                          evidence_refs=['existing.py'], reason='Current source belongs to T2')]
    assert assess(accepted, record, context, diagnosis).budget_owners == ['T2']
    diagnosis.repair_targets[0].paths = ['app.py']
    with pytest.raises(AA2Error):
        assess(accepted, record, context, diagnosis)


def test_planned_diagnosis_does_not_authorize_test_changes_or_apply_any_file(tmp_path):
    accepted, record, context, diagnosis = fixture()
    assess(accepted, record, context, diagnosis)
    with pytest.raises(ProtectedChange):
        prepare_changes(tmp_path, [Change(path='tests/test_new.py', content='def test_value(): assert True\n')],
                        accepted.tasks[2].allowed_paths)
    assert not (tmp_path/'tests/test_new.py').exists()
    with pytest.raises(AA2Error):
        prepare_changes(tmp_path, [Change(path='outside.py', content='value = 1\n')], accepted.tasks[2].allowed_paths)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('stage,extra', [('recovery_diagnosis', {}), ('review', {'review_kind': 'recovery_diagnosis'})])
def test_real_and_legacy_diagnosis_prompts_explain_missing_candidate_semantics(stage, extra):
    prompt, _ = PromptCompiler().compile(stage, extra)
    assert 'missing planned inputs' in prompt and 'allowed_paths alone is insufficient' in prompt
    assert 'Other task targets require supplied current-file evidence' in prompt
    assert 'protected review and fresh checks' in prompt


async def test_planned_targets_cannot_reuse_a_permit_after_contract_or_source_changes(tmp_path):
    accepted, record, context, diagnosis = fixture()
    workflow, rid, work, _, _ = ready(tmp_path, record=record)
    workflow.store.checkpoint(rid, 'plan', accepted.model_dump())
    suggested = assess(accepted, record, context, diagnosis)
    active = workflow.store.get(rid, 'recovery/active')
    _, key = await workflow.recovery_policy.authorize(rid, record, active, work, accepted, suggested,
        operation='planned-input-repair', established=True)
    original = workflow.store.get(rid, key)
    assert original['task_scopes']['T3'] == accepted.tasks[2].allowed_paths
    altered = accepted.model_copy(deep=True)
    altered.tasks[2].checks[0].expected_tests += 1
    with pytest.raises(AA2Error, match='inputs changed'):
        await workflow.recovery_policy.authorize(rid, record, active, work, altered, suggested,
            operation='planned-input-repair', established=True)
    (work/'unrelated-user-file').write_text('preserve')
    with pytest.raises(AA2Error, match='inputs changed'):
        await workflow.recovery_policy.authorize(rid, record, active, work, accepted, suggested,
            operation='planned-input-repair', established=True)
    assert workflow.store.get(rid, key) == original
    assert (work/'unrelated-user-file').read_text() == 'preserve'


class PlannedRepairAgent(ApprovingAgent):
    def __init__(self):
        super().__init__()
        self.interrupted = False
        self.diagnosis_contexts = []

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if stage == 'recovery_diagnosis':
            self.diagnosis_contexts.append(deepcopy(context))
            if not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return fixture()[3]
        return await super().invoke(stage, context, schema, *args, **kwargs)


async def test_interrupted_diagnosis_resumes_same_frozen_operation_and_dispatches_unaccepted_candidate(tmp_path):
    accepted, record, _, _ = fixture()
    agent = PlannedRepairAgent()
    workflow, rid, _, _, _ = ready(tmp_path, agent=agent, record=record)
    workflow.store.checkpoint(rid, 'plan', accepted.model_dump())
    workflow.store.checkpoints(rid, {'recovery/budgets/diagnosis/T3': {'round': 18},
        'rounds/T3': {'round': 9, 'stalled': 0, 'failure': ''}})
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    assert active['diagnosis_round'] == 19
    operation = f'recovery/diagnose/{record.id}/19'
    key = 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', operation])
    frozen = workflow.store.get(rid, key)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, key) == frozen
    assert invocation_contract(agent.diagnosis_contexts[0]) == invocation_contract(agent.diagnosis_contexts[1])
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round'] == 19
    assert workflow.store.get(rid, 'rounds/T3')['round'] == 9
    assert workflow.store.get(rid, 'recovery/active') is None
    assert 'T3' not in workflow.store.get(rid, 'develop/done', [])
    saved = workflow.store.get(rid, 'develop/task/T3')
    assert saved['phase'] == 'rejected' and saved['recovery_permit']
    assert saved['feedback']['controller_recovery']['repair_targets'] == fixture()[3].model_dump()['repair_targets']
    assert not any(stage == 'implement' for stage, _ in agent.calls)
    assert not workflow.store.pending(rid)


@pytest.mark.parametrize('path', ['routes/[id]/page.tsx', 'tests/test_values[known].py'])
def test_known_literal_filenames_do_not_become_wildcard_selectors(path):
    accepted, record, context, diagnosis = fixture()
    accepted.tasks[2].allowed_paths.append(path)
    record.evidence_refs.append(path)
    diagnosis.repair_targets[0].paths = [path]
    diagnosis.repair_targets[0].evidence_refs = [path]
    assert assess(accepted, record, context, diagnosis).action == 'repair'


def test_same_task_repair_without_optional_path_detail_keeps_original_contract():
    accepted, record, context, diagnosis = fixture()
    diagnosis.repair_targets = []
    assert assess(accepted, record, context, diagnosis).action == 'repair'
