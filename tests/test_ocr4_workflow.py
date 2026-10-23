import json
import sys
from pathlib import Path

import pytest
from browser_fixtures import prototype_rows
from evidence_test_fixtures import empty_contract
from manual_recovery_snapshot import snapshot
from test_ocr_manual_tools import paused_project
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import AA2Error
from auto_agents_2.models import Change, Command, DocumentResult, PlanResult, PreviewSpec, SourceEvidence
from auto_agents_2.planning import validate_plan
from auto_agents_2.util import write_json
from auto_agents_2.workspace import WorkspaceManager, git, isolated_git


async def test_first_satisfied_frontend_gets_contracts_and_approval_without_implementation(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('Keep the existing addition interface')
    (project / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
    (project / 'tests').mkdir()
    (project / 'tests/test_calc.py').write_text('from calc import add\ndef test_sum():\n    assert add(2, 3) == 5\n')
    class ExistingFrontend(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == 'prototype':
                self.calls.append((stage, fresh))
                return DocumentResult(summary='Existing screen contract', document='Prototype',
                    sources=[SourceEvidence(url='https://fixture.invalid/DESIGN.md', claim='style')],
                    files=[Change(path='design/prototypes/index.html', content='<h1>Addition</h1>'),
                           Change(path='DESIGN.md', content='Existing addition interface')])
            if stage == 'plan':
                self.calls.append((stage, fresh))
                assert context['planning_mode'] == 'verification_only' and context['coverage_verification']
                assert context['prototype_approval']['status'] == 'approved'
                return PlanResult(evidence_contract=empty_contract(context), mode='verification_only', summary='Verify existing application', document='Verification contract', tasks=[],
                    final_checks=[Command(argv=[sys.executable, '-m', 'pytest', '-q', 'tests'], expected_tests=1)],
                    runtime_probes=[Command(argv=[sys.executable, '-c', 'from calc import add; assert add(2,3)==5'])],
                    preview=PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}'])))
            assert stage != 'implement', 'Satisfied behaviour must not be reimplemented'
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == 'clarify':
                result.frontend, result.ui_changes = True, False
            return result
    agent = ExistingFrontend()
    workflow = engine(project, agent)
    workflow.workspaces.checkpoint(project, 'refs/aa2/existing', 'existing product')
    original_source = (project / 'calc.py').read_bytes()
    browser_calls, questions = [], []
    async def browser(*args):
        browser_calls.append(True)
        directory = args[0] if isinstance(args[0], Path) else args[1] / 'design/prototypes'
        actions = args[2] if isinstance(args[0], Path) and len(args) > 2 else []
        return prototype_rows(directory, actions, workflow.artifacts)
    async def installed(*args):
        pass
    async def candidates(*args):
        return [{'url': 'https://fixture.invalid/DESIGN.md', 'revision': 'fixture'}]
    async def research(*args):
        return [{'url': 'https://fixture.invalid/DESIGN.md', 'excerpt': 'Existing addition interface'}]
    async def approval(question):
        questions.append(question['key'])
        assert question['key'] == 'prototype-approval'
        return '1'
    workflow.question_handler = approval
    workflow._ensure_browser = installed
    monkeypatch.setattr('auto_agents_2.engine.DesignCatalog.candidates', candidates)
    monkeypatch.setattr('auto_agents_2.engine.ResearchService.verify', research)
    monkeypatch.setattr('auto_agents_2.engine.inspect_prototype', browser)
    monkeypatch.setattr('auto_agents_2.engine.inspect_application', browser)
    result = await workflow.start(str(requirements))
    assert result['status'] == 'SUCCEEDED' and result['data']['outcome'] == 'NO_CHANGES'
    assert questions == ['prototype-approval'] and len(browser_calls) >= 3
    assert (project / 'calc.py').read_bytes() == original_source
    assert workflow.store.get(result['id'], 'plan')['tasks'] == []
    assert all(stage != 'implement' for stage, _ in agent.calls)


def test_empty_plan_requires_mode_and_verified_coverage():
    with pytest.raises(ValueError):
        PlanResult(summary='x', document='x', tasks=[], final_checks=[Command(argv=['true'])])
    plan = PlanResult(mode='verification_only', summary='x', document='x', tasks=[], final_checks=[Command(argv=['true'])])
    with pytest.raises(AA2Error, match='controller-verified'):
        validate_plan(plan, set())
    with pytest.raises(AA2Error, match='controller-verified'):
        validate_plan(plan, {'unmet'}, coverage_verified=True)
    validate_plan(plan, set(), coverage_verified=True)


def test_snapshot_hashes_and_heads_ignore_inherited_git_environment(tmp_path, monkeypatch):
    source, store, rid = paused_project(tmp_path)
    manager = WorkspaceManager(source)
    manager.initialize()
    (source / 'source.py').write_text('original')
    base = manager.checkpoint(source, 'refs/aa2/base', 'base')
    worker = manager.allocate(rid, 'worker', base)
    (worker / 'dist').mkdir()
    (worker / 'dist/tracked.py').write_text('original')
    git(worker, 'add', '-f', 'dist/tracked.py')
    manager.checkpoint(worker, 'refs/aa2/worker', 'tracked output')
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    other = WorkspaceManager(unrelated)
    other.initialize()
    (unrelated / 'external').write_text('unrelated')
    other.checkpoint(unrelated, 'refs/aa2/other', 'other')
    monkeypatch.setenv('GIT_DIR', str(unrelated / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(unrelated))
    import manual_recovery_snapshot as module
    original = module.subprocess.run
    changed = []
    def copying(argv, *args, **kwargs):
        result = original(argv, *args, **kwargs)
        if argv[0] == 'cp' and str(store.root / 'worktrees') in argv and not changed:
            changed.append(True)
            (worker / 'dist/tracked.py').write_text('changed during copy')
            with isolated_git():
                manager.checkpoint(worker, 'refs/aa2/worker', 'user edit')
        return result
    monkeypatch.setattr(module.subprocess, 'run', copying)
    with pytest.raises(RuntimeError, match='worktree changed'):
        snapshot(source, tmp_path / 'copy')
    assert changed


@pytest.mark.parametrize('change_db', [False, True])
def test_snapshot_rejects_recovery_mutation_after_copy(tmp_path, monkeypatch, change_db):
    source, store, rid = paused_project(tmp_path)
    recovery = store.root / 'recovery/build_fixture/journal.json'
    write_json(recovery, {'workspace': str(source), 'phase': 'prepared', 'outputs': [], 'generation': 1})
    store.checkpoint(rid, 'generation', 1)
    import manual_recovery_snapshot as module
    original = module.subprocess.run
    changed = []
    def copying(argv, *args, **kwargs):
        result = original(argv, *args, **kwargs)
        if argv[0] == 'cp' and str(store.root / 'recovery') in argv and not changed:
            changed.append(True)
            data = json.loads(recovery.read_text())
            write_json(recovery, {**data, 'generation': 2})
            if change_db:
                store.checkpoint(rid, 'generation', 2)
        return result
    monkeypatch.setattr(module.subprocess, 'run', copying)
    with pytest.raises(RuntimeError, match='changed'):
        snapshot(source, tmp_path / 'copy')
    assert changed
