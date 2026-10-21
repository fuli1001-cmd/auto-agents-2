"""Ordinary task retries must retain their unapplied proposals for review."""
import json

import pytest
from test_review_evidence import commit
from test_workflow import CodingAgent, engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, ImplementationResult
from auto_agents_2.source_history import SourceHistory
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash


def response_fixture(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('retain ordinary task proposals', {})['id']
    (tmp_path/'base.py').write_text('base = 1\n')
    commit(tmp_path)
    workflow.store.checkpoint(rid, 'baseline', git(tmp_path, 'rev-parse', 'HEAD'))
    proposal = ImplementationResult(summary='Original unapplied tests', changes=[
        Change(path='tests/test_runtime.py', content="def test_cleanup():\r\n    assert cleanup() == 'complete'\r\n"),
        Change(path='tools/preview.py', content=' def_placeholder\n')]).model_dump()
    ref = workflow.artifacts.put(proposal, 'implement')
    workflow.store.event(rid, 'artifact.created', {'stage': 'implement', 'operation': 'develop/task/T1', 'artifact': ref})
    return workflow, rid, proposal, ref


def collect(workflow, rid, tmp_path):
    result = SourceHistory(workflow).collect(rid, tmp_path, ['tools'])
    return result, workflow.artifacts.json(result['artifact'])


def test_prior_returned_candidate_survives_overwritten_task_slot_without_becoming_source(tmp_path):
    workflow, rid, proposal, ref = response_fixture(tmp_path)
    next_proposal = ImplementationResult(summary='Only fixes placeholder', changes=[
        Change(path='tools/preview.py', content='def main(): pass\n')]).model_dump()
    workflow.store.checkpoint(rid, 'develop/task/T1', {'phase': 'applied', 'implementation': next_proposal})
    before = tree_hash(tmp_path)
    result, report = collect(workflow, rid, tmp_path)
    assert len(report['proposals']) == 1
    row = report['proposals'][0]
    assert row['status'] == 'proposal_only' and row['recorded_phase'] == 'returned'
    assert row['recorded_source_hash'] is None and not row.get('approved')
    assert row['response_hash'] == ref['hash'] and row['proposal_hash'] == digest(proposal)
    assert row['origin'].startswith('events/') and row['origin'].endswith('/develop/task/T1')
    assert row['checks'] == row['commands'] == []
    assert workflow.artifacts.json(row['content'])['changes'] == proposal['changes']
    assert 'tests/test_runtime.py' not in report['current']
    assert not report['observations'] and tree_hash(tmp_path) == before
    assert not workflow.store.get(rid, 'develop/done')
    assert collect(workflow, rid, tmp_path)[0] == result


@pytest.mark.parametrize('stage,extra', [('implement', {}), ('review', {'review_kind':'test_changes'}),
    ('review', {'review_kind':'implementation'}), ('recovery_diagnosis', {}), ('plan', {'plan_patch':True}), ('verify', {})])
def test_history_body_is_mounted_for_each_shared_consumer(tmp_path, stage, extra):
    workflow, rid, original, ref = response_fixture(tmp_path)
    history, report = collect(workflow, rid, tmp_path)
    selected = report['proposals'][0]['content']
    view = ContextPlanner(workflow.artifacts).prepare(stage, {**extra, 'source_history':history}, tmp_path)
    mounted = {r['hash'] for r in view.payload['evidence_materials']}
    assert selected['hash'] in mounted and ref['hash'] not in mounted
    assert workflow.artifacts.json(selected)['changes'] == original['changes']


def test_returned_response_selection_is_run_stage_and_operation_scoped(tmp_path):
    workflow, rid, _, ref = response_fixture(tmp_path)
    hidden = workflow.artifacts.put(ImplementationResult(summary='Wrong producer', changes=[
        Change(path='tests/test_unrelated.py', content='UNRELATED_FIXTURE')]).model_dump(), 'implement')
    for stage, operation in [('review','develop/task/T2'), ('implement','prototype'),
                             ('implement','develop/task/T2/test-change-review'), ('implement','develop/task/T2/r2'),
                             ('implement','not-develop/task/T2')]:
        workflow.store.event(rid, 'artifact.created', {'stage':stage, 'operation':operation, 'artifact':hidden})
    workflow.store.event(rid, 'agent.output', {'stage':'implement','operation':'develop/task/T2','artifact':hidden})
    # A model claim nested in a controller record is not a returned task response.
    workflow.store.checkpoint(rid, 'recovery/diagnosis/fake', {'artifact':hidden, 'operation':'develop/task/T2'})
    other = 'unrelated_run'
    workflow.store.event(other, 'artifact.created', {'stage':'implement','operation':'develop/task/T2','artifact':hidden})
    _, report = collect(workflow, rid, tmp_path)
    assert [p['response_hash'] for p in report['proposals']] == [ref['hash']]


def test_exact_duplicates_do_not_grow_history_but_new_bytes_change_identity(tmp_path):
    workflow, rid, proposal, ref = response_fixture(tmp_path)
    first, _ = collect(workflow, rid, tmp_path)
    workflow.store.event(rid, 'artifact.created', {'stage':'implement','operation':'develop/task/T1','artifact':ref})
    assert collect(workflow, rid, tmp_path)[0] == first
    proposal['changes'][0]['content'] += 'assert new_regression()\n'
    changed = workflow.artifacts.put(proposal, 'implement')
    workflow.store.event(rid, 'artifact.created', {'stage':'implement','operation':'develop/task/T1','artifact':changed})
    second, report = collect(workflow, rid, tmp_path)
    assert second['artifact'] != first['artifact'] and second['source_hash'] == first['source_hash']
    assert len(report['proposals']) == 2


def test_private_paths_and_nested_references_never_reach_model(tmp_path):
    workflow, rid, proposal, _ = response_fixture(tmp_path)
    config = workflow.config.load().model_dump(mode='json')
    config['sensitive_paths'] = ['private/**']
    write_json(workflow.config.path, config)
    nested = workflow.artifacts.put('PRIVATE_REFERENCE_FIXTURE', 'private_fixture')
    proposal['changes'] += [Change(path=p, content='EXCLUDED_BODY_FIXTURE').model_dump() for p in
        ['.env','private/test_secret.py','.aa2/test_state.py','../test_out.py','unrelated.py']]
    proposal['changes'].append(Change(path='tests/test_ref.json', content=json.dumps({'report':nested})).model_dump())
    ref = workflow.artifacts.put(proposal, 'implement')
    workflow.store.event(rid, 'artifact.created', {'stage':'implement','operation':'develop/task/T1','artifact':ref})
    history, report = collect(workflow, rid, tmp_path)
    row = next(p for p in report['proposals'] if p['response_hash'] == ref['hash'])
    assert row['omitted_changes'] == 5
    assert 'EXCLUDED_BODY_FIXTURE' not in json.dumps(workflow.artifacts.json(row['content']))
    view = ContextPlanner(workflow.artifacts).prepare('review', {'source_history':history}, tmp_path)
    assert nested['hash'] not in {r['hash'] for r in view.payload['evidence_materials']}
    assert ref['hash'] not in {r['hash'] for r in view.payload['evidence_materials']}


@pytest.mark.parametrize('fault', ['missing','corrupt','wrong_length','wrong_kind','invalid_schema'])
def test_selected_response_integrity_failure_cannot_claim_complete_history(tmp_path, fault):
    workflow, rid, _, ref = response_fixture(tmp_path)
    path = workflow.artifacts.root/ref['hash'][:2]/ref['hash']
    if fault == 'missing':
        path.unlink()
    elif fault == 'corrupt':
        path.write_text('corrupted')
    elif fault == 'invalid_schema':
        ref = workflow.artifacts.put({'changes':[{'path':'tests/test_runtime.py','operation':'invalid'}]}, 'implement')
    else:
        ref = {**ref, **({'bytes':ref['bytes']+1} if fault == 'wrong_length' else {'kind':'review'})}
    if fault not in {'missing','corrupt'}:
        workflow.store.event(rid, 'artifact.created', {'stage':'implement','operation':'develop/task/T2','artifact':ref})
    with pytest.raises(StateConflict, match='task response'):
        collect(workflow, rid, tmp_path)
