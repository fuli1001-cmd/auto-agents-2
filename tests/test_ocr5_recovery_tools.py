import asyncio

import pytest
from manual_recovery_snapshot import snapshot
from test_ocr4_workflow import (
    test_first_satisfied_frontend_gets_contracts_and_approval_without_implementation as satisfied_frontend,
)
from test_ocr_manual_tools import paused_project
from test_workflow import CodingAgent, engine

from auto_agents_2.collection import policy_identity
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.errors import AA2Error
from auto_agents_2.models import Command, PlanResult
from auto_agents_2.plan_revision import PlanPatch, apply_patch
from auto_agents_2.recovery import FailureRecord, adjudicate
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.workspace import WorkspaceManager, tree_hash


@pytest.mark.parametrize('target', ['state', 'controller.lock', 'gc.lock', 'artifact-gc.lock'])
def test_snapshot_rejects_source_links_before_any_lock_creation(tmp_path, target):
    source, store, _ = paused_project(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    if target == 'state':
        store.root.rename(outside / 'state')
        store.root.symlink_to(outside / 'state', target_is_directory=True)
    else:
        secret = outside / 'secret'
        secret.write_bytes(b'unchanged')
        (store.root / target).symlink_to(secret)
    before = {str(p): p.read_bytes() for p in outside.rglob('*') if p.is_file()}
    with pytest.raises((ValueError, OSError)):
        snapshot(source, tmp_path / 'copy')
    assert {str(p): p.read_bytes() for p in outside.rglob('*') if p.is_file()} == before
    assert not (store.root / 'controller.lock').exists() if target != 'controller.lock' else True


@pytest.mark.parametrize('area', ['artifacts', 'visuals', 'cache', 'git'])
@pytest.mark.parametrize('mutate', ['source', 'copy'])
def test_snapshot_certifies_every_copied_input(tmp_path, monkeypatch, area, mutate):
    import manual_recovery_snapshot as module
    source, store, _ = paused_project(tmp_path)
    WorkspaceManager(source).initialize()
    relative = '.git/description' if area == 'git' else '.aa2/' + area + '/fixture'
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('original')
    real = module.subprocess.run
    changed = []
    destination = tmp_path / 'copy'
    def copy(argv, *args, **kwargs):
        result = real(argv, *args, **kwargs)
        if argv[0] == 'cp' and (destination / relative).exists() and not changed:
            changed.append(True)
            (path if mutate == 'source' else destination / relative).write_text('corrupt')
        return result
    monkeypatch.setattr(module.subprocess, 'run', copy)
    with pytest.raises(RuntimeError, match='manifest|inputs|changed'):
        snapshot(source, destination)
    assert changed
    assert not (tmp_path / 'copy-snapshot.json').read_bytes()


def test_verification_only_amendment_requires_controller_coverage():
    original = PlanResult(mode='verification_only', summary='verify', document='before', tasks=[],
        final_checks=[Command(argv=['python', '-m', 'pytest'])])
    patch = PlanPatch(base_revision=1, failure_ids=['f'], reason='repair capture', document='after')
    with pytest.raises(AA2Error, match='controller-verified'):
        apply_patch(original, patch, 1, {'f'}, {})
    revised, affected, reopen = apply_patch(original, patch, 1, {'f'}, {}, coverage_verified=True)
    assert revised.mode == 'verification_only' and revised.tasks == [] and not affected and not reopen


async def test_verification_only_resume_renews_coverage_with_pending_flag(tmp_path, monkeypatch):
    checkpoint = WorkspaceManager.checkpoint
    start = WorkflowEngine.start
    interrupted = []
    def interrupt(self, path, ref, message, **kwargs):
        result = checkpoint(self, path, ref, message, **kwargs)
        if ref.endswith('/design') and not interrupted:
            interrupted.append(True)
            store = Store(self.project)
            rid = store.current()['id']
            store.checkpoint(rid, 'recovery/implementation_pending', True)
            raise asyncio.CancelledError()
        return result
    async def resume(self, *args, **kwargs):
        try:
            return await start(self, *args, **kwargs)
        except asyncio.CancelledError:
            return await start(self, run_id=self.store.current()['id'])
    monkeypatch.setattr(WorkspaceManager, 'checkpoint', interrupt)
    monkeypatch.setattr(WorkflowEngine, 'start', resume)
    await satisfied_frontend(tmp_path, monkeypatch)
    assert interrupted


async def test_capacity_pause_waits_for_changed_policy_without_spending_business_budget(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit('capacity', {})['id']
    workflow.store.checkpoint(rid, 'rounds/T', {'round': 4})
    row = {'failure_reasons': ['evidence_collection_failed'], 'capture_policy_hash': policy_identity(workflow.store.root),
           'source_hash': tree_hash(tmp_path)}
    questions = []
    async def answer(question):
        questions.append(question)
        assert workflow.store.run(rid)['status'] == 'WAITING_USER'
        if len(questions) == 2:
            data = workflow.config.load().model_dump(mode='json')
            data['attachment_capture']['max_file_bytes'] *= 2
            write_json(workflow.config.path, data)
        return 'continue'
    workflow.question_handler = answer
    assert await workflow.recovery._capture_retry(rid, [row], tmp_path)
    assert len(questions) == 2 and workflow.store.get(rid, 'rounds/T') == {'round': 4}
    failure = FailureRecord.create(kind='evidence', component='verification', origin_id='T', step='checks',
        obligations=['R'], evidence={'checks': [{**row, 'exit_code': 1}]})
    assert adjudicate(failure).action == 'blocked'


async def test_manual_delivery_attempts_remaining_teardown_after_release_error(tmp_path, monkeypatch):
    import manual_delivery

    from auto_agents_2.resources import ResourceManager
    primary = ValueError('environment fixture failure')
    calls = []
    async def fail(self, *args):
        (self.project / 'dist').mkdir()
        (self.project / 'dist/output').write_text('fixture')
        raise primary
    def release(*args):
        calls.append('release')
        raise OSError('release fixture')
    def collect(*args):
        calls.append('collect')
    monkeypatch.setattr(WorkflowEngine, '_environment', fail)
    monkeypatch.setattr(ResourceManager, 'release_run', release)
    monkeypatch.setattr(ResourceManager, 'collect', collect)
    with pytest.raises(ValueError) as caught:
        await manual_delivery.smoke(tmp_path, 'python')
    assert caught.value is primary and calls == ['release', 'collect']
    assert not (tmp_path / 'python/dist').exists() and primary.__notes__
