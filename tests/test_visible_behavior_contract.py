"""Correct text in a hidden element is not observed user feedback."""
import sys
from pathlib import Path

import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2.config import ConfigStore
from auto_agents_2.design import inspect_application, inspect_prototype
from auto_agents_2.environment import Environment
from auto_agents_2.errors import PrototypeBrowserError
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize(('entry', 'style'), [
    ('prototype', 'display:none'), ('application', 'visibility:hidden'), ('application', 'hidden'),
    ('prototype', ''), ('application', ''),
])
async def test_real_browser_feedback_must_be_visible_at_both_callers(browser_cache, tmp_path, entry, style):
    page = tmp_path / 'page'
    page.mkdir()
    attributes = 'hidden' if style == 'hidden' else f'style="{style}"'
    (page / 'index.html').write_text(
        '<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<button id="import" onclick="document.querySelector(\'#error\').textContent=\'Invalid file\'">Import</button>'
        f'<p id="error" {attributes}></p>')
    actions = [BrowserAction(route='/index.html', action='click', selector='#import'),
               BrowserAction(route='/index.html', action='expect_text', selector='#error', value='Invalid file')]
    output = tmp_path / 'images'
    if entry == 'prototype':
        pending = inspect_prototype(page, output, actions)
    else:
        ConfigStore(tmp_path).initialize()
        root = tmp_path / '.aa2'
        runner = SandboxRunner(root, _test_unconfined=True)
        spec = PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
                           routes=['/index.html'], actions=actions)
        pending = inspect_application(spec, page, output, Environment(Path(sys.prefix), 'fixture'), runner, root)
    if style:
        with pytest.raises(PrototypeBrowserError) as rejected:
            await pending
        row = rejected.value.results[0]
        assert row['actions'] == [actions[0].model_dump()]
        assert row['failed_action'] == actions[1].model_dump()
        assert row['errors'] and Path(row['screenshot']).is_file()
    else:
        rows = await pending
        assert len(rows) == 2 and all(row['actions'] == [a.model_dump() for a in actions] for row in rows)
        assert all(not row['errors'] and Path(row['screenshot']).is_file() for row in rows)
    if entry == 'application':
        assert not list((root / 'processes').glob('*.json'))
        assert not list((root / 'tmp').glob('preview-*'))


@pytest.mark.parametrize('old_version', [6, 7, 8, 9, 10])
async def test_previous_text_only_browser_evidence_rechecks_without_replacing_human_consent(
        tmp_path, monkeypatch, old_version):
    from test_prototype_approvals import approval_fixture

    from auto_agents_2.util import digest
    from auto_agents_2.workspace import tree_hash
    workflow, rid, document, question, contexts, browser_calls = approval_fixture(tmp_path, monkeypatch, legacy=False)
    await workflow._ensure_prototype_approval(rid, tmp_path)
    old = workflow.store.get(rid, 'prototype_approval')
    old['browser_version'] = old_version  # All earlier visible-text semantics require fresh automation.
    workflow.store.checkpoint(rid, 'prototype_approval', old)
    before = tree_hash(tmp_path)
    assert workflow.approvals.current(rid, tmp_path, document.model_dump()['browser_actions'],
                                     workflow.browser_check_version()) is None
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert receipt['browser_version'] > old_version and len(browser_calls) == 1
    assert receipt['human']['question_id'] == question['id'] and receipt['human']['answer'] == '1'
    assert workflow.store.get(rid, 'candidate/prototype-approval-history/' + digest(old)) == old
    assert tree_hash(tmp_path) == before and not workflow.store.pending(rid)
    assert len(contexts) == 1 and contexts[0][1]['review_kind'] == 'prototype'


@pytest.mark.parametrize(('stage', 'context'), [
    ('plan', {}), ('plan', {'plan_patch': True}), ('implement', {}),
    ('review', {'review_kind': 'plan'}), ('review', {'review_kind': 'plan_patch'}),
    ('review', {'review_kind': 'test_changes'}), ('review', {'review_kind': 'implementation'}),
    ('review', {'review_kind': 'preview_contract'}), ('verify', {}),
])
def test_real_prompt_compiler_delivers_behavior_guidance_to_authors_and_reviewers(stage, context):
    from auto_agents_2.prompts import PromptCompiler
    prompt, _ = PromptCompiler().compile(stage, context)
    assert 'DOM text presence' in prompt and 'both visibility and the expected message' in prompt
    assert 'all required fields' in prompt and 'existing data remains unchanged' in prompt
    assert 'do not add unrequested features' in prompt
