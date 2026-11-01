"""Current dependency evidence must not be replaced by older recovery failures."""
import asyncio
import copy

import pytest
from authority_review_fixtures import recorded_review
from test_diagnosis_handoff import RecheckDiagnosisAgent, initial_recheck
from test_recovery_policy import ready

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.recovery_handoff import diagnosis_handoff
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


def seed_result(workflow, rid, work, task_id='T2', *, source=None):
    source = source or tree_hash(work)
    log = workflow.artifacts.put('current dependency: 1 passed', 'check_log')
    report = workflow.artifacts.put('<testsuite tests="1" failures="0" errors="0"/>', 'junit')
    review = recorded_review(workflow, rid, {'approved': True,
        'summary': 'Latest independent dependency review passed.', 'findings': []}, work=work)
    saved = {'phase': 'tested', 'source_hash': source, 'commit': 'a' * 40, 'revision': 2,
        'checks': [{'argv': ['python', '-m', 'pytest', 'tests/test_2.py'], 'cwd': '.',
            'exit_code': 0, 'process_exit_code': 0, 'source_hash': source,
            'environment_hash': 'e' * 64, 'input_hash': 'i' * 64, 'command_hash': 'c' * 64,
            'report_status': 'valid', 'tests': 1, 'skipped': 0, 'logs': [log], 'report': report}],
        'review': review}
    workflow.store.checkpoints(rid, {'develop/task/' + task_id: saved,
        'task_state/' + task_id: {'version': 1, 'revision': 2, 'implementation': 'accepted', 'evidence': 'current'},
        'develop/done': sorted(set(workflow.store.get(rid, 'develop/done', [])) | {task_id})})
    return saved, log, report


async def test_real_diagnosis_includes_latest_dependency_checks_and_review_after_older_failure(tmp_path):
    workflow, rid, work, _, _, first, _ = await initial_recheck(tmp_path)
    saved, log, report = seed_result(workflow, rid, work)
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    context = workflow.agent.contexts[-1]
    current = context['current_task_results']
    assert current['source_hash'] == tree_hash(work)
    assert [row['task_id'] for row in current['tasks']] == ['T1', 'T2', 'T3']
    row = current['tasks'][1]
    assert row['done'] is True and row['state']['evidence'] == 'current'
    assert row['result']['source_relation'] == 'same'
    assert row['result']['checks'] == saved['checks']
    assert row['result']['review']['proposal'] == saved['review']
    assert row['result']['review']['receipt']['status'] == 'matching_record'
    assert row['result']['review']['receipt']['source_hash'] == saved['source_hash']
    # Presentation still delivers original reports and complete review, not an invented success summary.
    view = ContextPlanner(workflow.artifacts).prepare('recovery_diagnosis', context, work)
    shown = view.payload['current_task_results']['tasks'][1]['result']
    check = shown['checks'][0]
    assert workflow.artifacts.json(check['observation']['artifact']) == saved['checks'][0]
    assert shown['review']['proposal']['summary'] == saved['review']['summary']
    assert all(ref['hash'] in str(view.payload) for ref in [first, log, report])


def test_reopened_and_changed_source_are_observations_not_reaccepted_tasks(tmp_path):
    workflow, rid, work, _, failure = ready(tmp_path)
    saved, _, _ = seed_result(workflow, rid, work, source='b' * 64)
    pending = {'phase': 'reverify', 'previous_evidence': saved, 'revision': 3}
    workflow.store.checkpoints(rid, {'develop/task/T2': pending, 'develop/done': [],
        'task_state/T2': {'version': 1, 'revision': 3, 'implementation': 'accepted', 'evidence': 'stale'}})
    before = copy.deepcopy(pending)
    value = diagnosis_handoff(workflow, rid, failure, None, 'fresh', tree_hash(work))
    row = value['current_task_results']['tasks'][1]
    assert row['done'] is False and row['state']['evidence'] == 'stale'
    assert row['result']['source_relation'] == 'unknown'
    assert row['previous_result']['source_relation'] == 'different'
    assert row['previous_result']['status'] == 'historical_observation'
    assert row['previous_result']['checks'] == saved['checks']
    assert row['previous_result']['checkpoint'] == 'develop/task/T2.previous_evidence'
    assert workflow.store.get(rid, 'develop/task/T2') == before
    assert workflow.store.get(rid, 'develop/done') == []


def test_model_review_cannot_mint_facts_and_missing_receipt_cannot_claim_independence(tmp_path):
    workflow, rid, work, _, failure = ready(tmp_path)
    saved, _, _ = seed_result(workflow, rid, work)
    saved['review']['summary'] = 'unrecorded modified model claim'
    workflow.store.checkpoint(rid, 'develop/task/T2', saved)
    context = diagnosis_handoff(workflow, rid, failure, None, 'fresh', tree_hash(work))
    review = context['current_task_results']['tasks'][1]['result']['review']
    assert review['receipt']['status'] == 'missing'
    catalog = controller_context_catalog(context)
    base = 'current_task_results.tasks[1].result.review'
    assert base + '.proposal.approved' in catalog.proposals
    assert base + '.proposal.summary' not in catalog.factual
    assert base + '.receipt.status' in catalog.factual


async def test_frozen_current_results_survive_interrupt_without_following_mutated_checkpoints(tmp_path, monkeypatch):
    agent = RecheckDiagnosisAgent()
    workflow, rid, work, _, failure = ready(tmp_path, agent)
    saved, _, _ = seed_result(workflow, rid, work)
    invoke = agent.invoke
    async def cancel(stage, *args, **kwargs):
        if stage == 'recovery_diagnosis':
            raise asyncio.CancelledError()
        return await invoke(stage, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(agent, 'invoke', cancel)
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, 'recovery/active')
    operation = f"recovery/diagnose/{failure.id}/{active['diagnosis_round']}"
    key = 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', operation])
    frozen = workflow.store.get(rid, key)
    assert frozen['context']['current_task_results']['tasks'][1]['result']['checks'] == saved['checks']
    workflow.store.checkpoint(rid, 'develop/task/T2', {'phase': 'rejected', 'source_hash': 'f' * 64})
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, key) == frozen
    assert agent.contexts[-1]['current_task_results'] == frozen['context']['current_task_results']
    assert agent.diagnoses == 1


def test_legacy_frozen_absence_and_out_of_scope_tasks_remain_absent(tmp_path):
    workflow, rid, work, _, failure = ready(tmp_path)
    seed_result(workflow, rid, work, task_id='T4')  # Descendant, not a prerequisite of T3.
    value = diagnosis_handoff(workflow, rid, failure, None, 'fresh')
    assert all(row['task_id'] != 'T4' for row in value['current_task_results']['tasks'])
    operation = 'old-logical-call'
    workflow.store.checkpoint(rid, 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', operation]),
        {'source_hash': tree_hash(work), 'context': {'failure': failure.model_dump()}})
    assert diagnosis_handoff(workflow, rid, failure, None, operation) == {}
