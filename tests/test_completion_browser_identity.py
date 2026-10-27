"""Actual Engine pending-delivery consumer must recheck the application protocol."""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from test_browser import browser_cache as browser_cache
from test_workflow import CodingAgent, engine

from auto_agents_2.environment import Environment
from auto_agents_2.faults import ResourceBoundary, StateConflict
from auto_agents_2.models import BrowserAction, Command, PlanResult, PreviewSpec
from auto_agents_2.workspace import git, tree_hash


def setup(tmp_path, monkeypatch, *, preview=True):
    project = tmp_path / 'project'
    project.mkdir()
    browser_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    workflow = engine(project, CodingAgent())
    if browser_path:
        monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', browser_path)
    (project / 'index.html').write_text('<!doctype html><p id="feedback">Saved</p>')
    git(project, 'add', 'index.html')
    git(project, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'fixture')
    commit = git(project, 'rev-parse', 'HEAD')
    rid = workflow.store.admit('fixture', {})['id']
    workflow.store.update(rid, status='RUNNING')
    plan = PlanResult(mode='verification_only', summary='fixture', document='', tasks=[],
        final_checks=[Command(argv=[sys.executable, '-c', "print('verified')"])],
        preview=PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
            actions=[BrowserAction(selector='#feedback', action='expect_text', value='Saved')]) if preview else None)
    journal = {'identity_version': 2, 'phase': 'runtime', 'target': 'main', 'old': commit, 'candidate': commit,
        'candidate_tree': git(project, 'rev-parse', 'HEAD^{tree}'), 'work': str(project), 'source_hash': tree_hash(project),
        'environment': {'prefix': sys.prefix, 'fingerprint': 'fixture'}, 'plan': plan.model_dump(),
        'reintegrations': 0, 'runtime_budget_used': 0, 'outcome': 'DELIVERED', 'validation': 'fixture'}
    from evidence_test_fixtures import seed_requirements
    seed_requirements(workflow, rid, plan)
    workflow.store.checkpoint(rid, 'delivery/journal', journal)
    workflow.store.checkpoint(rid, 'prototype_approval', {'human': {'answer': '1', 'artifact_hash': 'unchanged-human-binding'}})
    version, observed = {'value': '8:fixture-browser'}, {'prototype': 0, 'application': 0}
    monkeypatch.setattr(workflow, 'browser_check_version', lambda: version['value'])
    async def ready(*args, **kwargs):
        pass  # The test uses the existing interpreter and cached browser; no installation.
    async def prototype(*args):
        observed['prototype'] += 1
        return workflow.store.get(rid, 'prototype_approval')
    from auto_agents_2.engine import inspect_application
    async def application(*args, **kwargs):
        observed['application'] += 1
        return await inspect_application(*args, **kwargs)
    monkeypatch.setattr(workflow, '_ensure_browser', ready)
    monkeypatch.setattr(workflow.environments, 'prepare_delivery', ready)
    monkeypatch.setattr(workflow, '_ensure_prototype_approval', prototype)
    monkeypatch.setattr('auto_agents_2.engine.inspect_application', application)
    return workflow, rid, version, observed


async def prepare_final_observation(workflow, rid):
    """Create the real prerequisite evidence for a fixture starting at runtime.

    Runtime application counters intentionally exclude this earlier final check.
    Normal Engine reentry still invokes its independent authority reviewer.
    """
    from auto_agents_2.design import inspect_application
    journal = workflow.store.get(rid, 'delivery/journal')
    plan = PlanResult.model_validate(journal['plan'])
    work = Path(journal['work'])
    environment = Environment(Path(journal['environment']['prefix']), journal['environment']['fingerprint'])
    workflow.store.checkpoint(rid, 'plan', plan.model_dump())
    checks = await workflow._checks(rid, workflow._candidate_checks(plan, work), work, environment)
    assert checks and all(row.exit_code == 0 for row in checks)
    visuals = await inspect_application(plan.preview, work, workflow.store.root / 'visuals' / rid / 'final-fixture',
                                       environment, workflow.sandbox, workflow.store.root) if plan.preview else []
    workflow.store.checkpoints(rid, {'verify/identity': journal['validation'],
        'verify/checks': [row.model_dump() for row in checks], 'verify/visuals': workflow._archive_visuals(visuals)})


async def interrupted_completion(workflow, rid, monkeypatch):
    await prepare_final_observation(workflow, rid)
    def cut(*args):
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(workflow.store, 'complete_delivery', cut)
        with pytest.raises(asyncio.CancelledError):
            await workflow._execute(rid)
    journal = workflow.store.get(rid, 'delivery/journal')
    assert journal['phase'] == 'completion_pending'
    assert journal['runtime_budget_used'] == 1
    return journal


@pytest.mark.parametrize('change', ['protocol', 'missing', 'mismatched_evidence', 'unchanged'])
async def test_actual_engine_reentry_rechecks_application_not_just_prototype(browser_cache, tmp_path, monkeypatch, change):
    workflow, rid, version, calls = setup(tmp_path, monkeypatch)
    journal = await interrupted_completion(workflow, rid, monkeypatch)
    original = journal['completion_evidence']
    assert original['browser_version'] == version['value']
    assert calls == {'prototype': 1, 'application': 1}
    approval = workflow.store.get(rid, 'prototype_approval')
    if change == 'protocol':
        version['value'] = '9:fixture-browser'
        assert workflow.delivery.runtime_identity(journal) != journal['completion_inputs']
    elif change == 'missing':
        journal['completion_evidence'].pop('browser_version')
    elif change == 'mismatched_evidence':
        journal['completion_evidence']['browser_version'] = '6:old'
    workflow.store.checkpoint(rid, 'delivery/journal', journal)
    completing = []
    original_complete = workflow.store.complete_delivery
    def complete(run, receipt):
        # Inspect the durable pending history at the transaction boundary;
        # normal successful cleanup may then retire the active journal.
        completing.append(workflow.store.get(run, 'delivery/journal'))
        return original_complete(run, receipt)
    monkeypatch.setattr(workflow.store, 'complete_delivery', complete)
    result = await workflow._execute(rid)
    assert result['status'] == 'SUCCEEDED'
    receipt = workflow.store.get(rid, 'delivery/receipt')
    expected = 1 if change == 'unchanged' else 2
    assert calls == {'prototype': 2, 'application': expected}
    assert receipt['runtime_budget_used'] == expected
    assert receipt['candidate'] == journal['candidate'] == git(workflow.project, 'rev-parse', 'HEAD')
    assert receipt['runtime']['evidence']['browser_version'] == version['value']
    assert workflow.store.get(rid, 'prototype_approval') == approval
    assert len(completing) == 1
    history = completing[0].get('completion_history', [])
    assert len(history) == (0 if change == 'unchanged' else 1)
    if history:
        assert history[0]['completion_evidence'] == journal['completion_evidence']
        assert 'completion_receipt' in history[0]
    version['value'] = 'future-version'
    assert (await workflow._execute(rid))['status'] == 'SUCCEEDED'
    assert calls == {'prototype': 2, 'application': expected}  # Do not reopen success.


async def test_protocol_invalidation_preserves_exhausted_budget(browser_cache, tmp_path, monkeypatch):
    workflow, rid, version, calls = setup(tmp_path, monkeypatch)
    from auto_agents_2.util import write_json
    config = workflow.config.load().model_dump(mode='json')
    config['delivery']['max_runtime_retries'] = 0
    write_json(workflow.config.path, config)
    original = await interrupted_completion(workflow, rid, monkeypatch)
    version['value'] = '9:fixture-browser'
    with pytest.raises(ResourceBoundary):
        await workflow._execute(rid)
    journal = workflow.store.get(rid, 'delivery/journal')
    assert journal['phase'] == 'runtime' and journal['runtime_budget_used'] == 1
    assert journal['candidate'] == original['candidate']
    assert calls['application'] == 1 and workflow.store.run(rid)['status'] != 'SUCCEEDED'
    assert len(journal['completion_history']) == 1


async def test_no_preview_keeps_existing_completion_identity_and_reuse(tmp_path, monkeypatch):
    workflow, rid, version, calls = setup(tmp_path, monkeypatch, preview=False)
    original = await interrupted_completion(workflow, rid, monkeypatch)
    version['value'] = 'changed-but-no-browser'
    assert workflow.delivery.runtime_identity(original) == original['completion_inputs']
    assert (await workflow._execute(rid))['status'] == 'SUCCEEDED'
    assert workflow.store.get(rid, 'delivery/receipt')['runtime_budget_used'] == 1
    assert calls == {'prototype': 0, 'application': 0}


@pytest.mark.parametrize('identity', [None, '', False])
def test_preview_cannot_assume_a_missing_checker_identity(tmp_path, monkeypatch, identity):
    workflow, rid, _, _ = setup(tmp_path, monkeypatch)
    workflow.delivery.browser_version = lambda: identity
    with pytest.raises(StateConflict, match='unavailable'):
        workflow.delivery.runtime_identity(workflow.store.get(rid, 'delivery/journal'))


async def test_identity_change_during_application_check_retains_stale_observation(browser_cache, tmp_path, monkeypatch):
    workflow, rid, version, calls = setup(tmp_path, monkeypatch)
    from auto_agents_2.engine import inspect_application
    async def changed(*args, **kwargs):
        rows = await inspect_application(*args, **kwargs)
        version['value'] = '9:changed-during-check'
        return rows
    monkeypatch.setattr('auto_agents_2.engine.inspect_application', changed)
    journal = workflow.store.get(rid, 'delivery/journal')
    with pytest.raises(StateConflict, match='changed during'):
        await workflow._runtime_checks(rid, journal, Environment(Path(sys.prefix), 'fixture'))
    observation = workflow.store.get(rid, 'delivery/runtime-browser-stale')
    assert observation['browser_version'] == '8:fixture-browser' and observation['visuals']
    assert workflow.store.run(rid)['status'] != 'SUCCEEDED'


@pytest.mark.parametrize('defect', ['empty', 'both_fields', 'invalid_row', 'invalid_tiles', 'missing_tile'])
async def test_delivery_rejects_missing_required_final_visual_shape(browser_cache, tmp_path, monkeypatch, defect):
    workflow, rid, _, calls = setup(tmp_path, monkeypatch)
    await prepare_final_observation(workflow, rid)
    rows = workflow.store.get(rid, 'verify/visuals')
    assert rows and all(row.get('image_artifact') for row in rows)
    if defect == 'empty':
        rows = []
    elif defect == 'both_fields':
        rows[0].pop('screenshot')
        rows[0].pop('image_artifact')
    elif defect == 'invalid_row':
        rows[0] = None
    elif defect == 'invalid_tiles':
        rows[0]['tiles'] = {}
    else:
        rows[0]['tiles'] = [{'viewport': 320}]
    workflow.store.checkpoint(rid, 'verify/visuals', rows)
    before = workflow.store.get(rid, 'delivery/journal')
    with pytest.raises(StateConflict, match='Final delivery evidence'):
        await workflow._execute(rid)
    assert workflow.store.run(rid)['status'] != 'SUCCEEDED'
    assert workflow.store.get(rid, 'delivery/journal') == before
    assert calls['application'] == 0
