"""Candidate source and preserved main-directory inputs have distinct identities."""
import asyncio
import sqlite3
from pathlib import Path

import pytest
from test_delivery import commit, fixture
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import PolicyViolation
from auto_agents_2.faults import StateConflict
from auto_agents_2.util import digest
from auto_agents_2.workspace import WorkspaceManager, source_manifest, source_manifest_hash, tree_hash


def published_fixture(tmp_path):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / '.gitignore').write_text('.aa2/\nlocal-state/\n')
    (tmp_path / 'app.py').write_text('value = 1\n')
    baseline = commit(tmp_path)
    state = tmp_path / 'local-state/input.bin'
    state.parent.mkdir()
    state.write_bytes(b'<PRESERVED_INPUT>')
    entries = source_manifest(tmp_path)
    original = source_manifest_hash(entries)
    snapshot = {'version': 1, 'commit': baseline, 'source_hash': original, 'entries': entries}
    work = manager.allocate('fixture', 'candidate', baseline)
    (work / 'app.py').write_text('value = 2\n')
    candidate = manager.checkpoint(work, 'refs/aa2/fixture/candidate', 'candidate')
    journal = {'work': str(work), 'candidate': candidate, 'source_hash': tree_hash(work)}
    manager.publish('main', baseline, candidate)
    return manager, journal, baseline, original, snapshot, state


@pytest.mark.parametrize('legacy', [False, True])
def test_preserved_inputs_require_admission_proof_and_remain_execution_inputs(tmp_path, legacy):
    manager, journal, baseline, original, snapshot, state = published_fixture(tmp_path)
    binding = manager.bind_published_inputs(journal, baseline, original, None if legacy else snapshot)
    assert binding['retained_paths'] == ['local-state/input.bin']
    assert binding['proof'] == ('reconstructed_admission' if legacy else 'admission_snapshot')
    assert manager.published_input_hash(journal, binding) == tree_hash(tmp_path)
    assert tree_hash(tmp_path) != journal['source_hash']
    before = tree_hash(tmp_path)
    state.write_bytes(b'<CHANGED_INPUT>')
    assert tree_hash(tmp_path) != before
    with pytest.raises(StateConflict, match='运行输入已变化'):
        manager.published_input_hash(journal, binding)
    assert state.read_bytes() == b'<CHANGED_INPUT>'


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('change', ['existing_input', 'new_ignored_input', 'tracked_source', 'candidate_source', 'wrong_baseline'])
def test_unknown_changes_cannot_be_reclassified_as_preserved_inputs(tmp_path, legacy, change):
    manager, journal, baseline, original, snapshot, state = published_fixture(tmp_path)
    if change == 'existing_input':
        state.write_bytes(b'<USER_EDIT>')
    elif change == 'new_ignored_input':
        (state.parent / 'new.sqlite3').write_bytes(b'<NEW_INPUT>')
    elif change == 'tracked_source':
        (tmp_path / 'app.py').write_text('value = 3\n')
    elif change == 'candidate_source':
        (Path(journal['work']) / 'app.py').write_text('value = 4\n')
    else:
        original = '0' * 64
    before = tree_hash(tmp_path)
    with pytest.raises(StateConflict):
        manager.bind_published_inputs(journal, baseline, original, None if legacy else snapshot)
    assert tree_hash(tmp_path) == before


@pytest.mark.parametrize('change', ['source', 'new_path', 'binding'])
def test_bound_publication_keeps_source_and_input_set_guards(tmp_path, change):
    manager, journal, baseline, original, snapshot, state = published_fixture(tmp_path)
    binding = manager.bind_published_inputs(journal, baseline, original, snapshot)
    if change == 'source':
        (tmp_path / 'app.py').write_text('value = 3\n')
    elif change == 'new_path':
        (state.parent / 'another.bin').write_bytes(b'<NEW_INPUT>')
    else:
        binding['source_paths'] = []
    with pytest.raises(StateConflict):
        manager.published_input_hash(journal, binding)


def test_admission_snapshot_survives_interruption_before_run_summary_update(tmp_path):
    from auto_agents_2.delivery import DeliveryCoordinator
    from auto_agents_2.store import Store
    manager, journal, baseline, _, snapshot, _ = published_fixture(tmp_path)
    store = Store(tmp_path)
    rid = store.admit('fixture', {})['id']
    store.checkpoints(rid, {'baseline': baseline, 'baseline/source_snapshot': snapshot})
    journal.update(phase='published', target='main')
    delivery = DeliveryCoordinator(store, manager, None, None, None, None)
    assert delivery.published_source(rid, journal) == tree_hash(tmp_path)
    assert journal['publication_inputs']['proof'] == 'admission_snapshot'


def test_new_verified_candidate_does_not_inherit_an_obsolete_input_binding(tmp_path):
    from auto_agents_2.delivery import DeliveryCoordinator
    from auto_agents_2.store import Store
    manager, journal, baseline, original, snapshot, state = published_fixture(tmp_path)
    previous = manager.bind_published_inputs(journal, baseline, original, snapshot)
    old = journal['candidate']
    work = Path(journal['work'])
    (work / 'app.py').write_text('value = 3\n')
    candidate = manager.checkpoint(work, 'refs/aa2/fixture/reverified', 'new verified candidate')
    manager.publish('main', old, candidate)
    state.unlink()  # An external change; the controller must not recreate removed input.
    journal.update(candidate=candidate, source_hash=tree_hash(work), publication_inputs=previous, phase='published')
    store = Store(tmp_path)
    rid = store.admit('fixture', {})['id']
    delivery = DeliveryCoordinator(store, manager, None, None, None, None)
    assert delivery.published_source(rid, journal) == journal['source_hash']
    assert 'publication_inputs' not in journal
    assert journal['publication_input_history'] == [previous]
    assert not state.exists()


def test_legacy_reconstruction_keeps_checkout_conversion_guards(tmp_path):
    manager, journal, baseline, original, _, _ = published_fixture(tmp_path)
    (tmp_path / '.git/info/attributes').write_text('app.py filter=unreviewed\n')
    with pytest.raises(PolicyViolation, match='Unsupported publication conversion'):
        manager.bind_published_inputs(journal, baseline, original)


@pytest.mark.parametrize('legacy', [False, True])
async def test_run_resumes_published_candidate_with_existing_database(tmp_path, legacy):
    project, requirements = fixture(tmp_path)
    agent = CodingAgent()
    workflow = engine(project, agent)
    ignored = project / '.gitignore'
    ignored.write_text((ignored.read_text() if ignored.exists() else '') + '\nbackend/data/\n')
    commit(project)
    database = project / 'backend/data/tasks.sqlite3'
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE fixture(value TEXT)')
        db.execute('INSERT INTO fixture VALUES(?)', ('<EXISTING_USER_DATA>',))
    original = digest(database.read_bytes())
    save = workflow.delivery.save
    def interrupt(rid, journal, phase=None):
        save(rid, journal, phase)
        if phase == 'published':
            if legacy:
                workflow.store.invalidate(rid, ['baseline/source_snapshot'])
            raise asyncio.CancelledError()
    workflow.delivery.save = interrupt
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    rid = workflow.store.current()['id']
    before = workflow.store.get(rid, 'delivery/journal')
    assert before['phase'] == 'published' and before['runtime_budget_used'] == 0
    assert tree_hash(project) != before['source_hash']
    implement_calls = [call for call in agent.calls if call[0] == 'implement']
    resumed = engine(project, agent)
    result = await resumed.start()
    assert result['status'] == 'SUCCEEDED'
    receipt = resumed.store.get(rid, 'delivery/receipt')
    assert receipt['source_hash'] == before['source_hash']
    assert receipt['runtime_source_hash'] == tree_hash(project)
    assert receipt['publication_inputs']['retained_paths'] == ['backend/data/tasks.sqlite3']
    assert receipt['runtime_budget_used'] == 1
    assert [call for call in agent.calls if call[0] == 'implement'] == implement_calls
    assert digest(database.read_bytes()) == original
    assert all(row['source_hash'] == receipt['runtime_source_hash'] for row in receipt['runtime']['evidence']['checks'])
