"""Browser feedback requires rendered text and visibility in the same observation."""
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

CASES = {
    'hidden_child_display': ('<span style="display:none">Invalid file</span>', '', False),
    'hidden_child_visibility': ('<span style="visibility:hidden">Invalid file</span>', '', False),
    'hidden_child_attribute': ('<span hidden>Invalid file</span>', '', False),
    'hidden_when_text_arrives': ('Waiting', "setTimeout(() => { e.style.display='none'; "
                                 "e.textContent='Invalid file'; }, 1000)", False),
    'visible_text_with_hidden_stale_child': (
        'Invalid file<span hidden>Obsolete message</span>', '', True),
    'visible_nested_text': ('<span>Invalid</span> <strong>file</strong>', '', True),
    'delayed_visible_text': ('Waiting', "e.style.display='none'; setTimeout(() => { "
                             "e.textContent='Invalid file'; e.style.display='block'; }, 100)", True),
    'replacement_visible_text': ('Waiting', "setTimeout(() => { const n=document.createElement('p'); "
                                 "n.id='error'; n.textContent='Invalid file'; e.replaceWith(n); }, 100)", True),
}


@pytest.mark.parametrize('entry', ['prototype', 'application'])
@pytest.mark.parametrize('case', CASES)
async def test_real_browser_text_observation(browser_cache, tmp_path, entry, case):
    content, transition, passes = CASES[case]
    page = tmp_path / 'page'
    page.mkdir()
    (page / 'index.html').write_text(
        '<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<button id="import">Import</button>'
        f'<p id="error" style="padding:8px">{content}</p>'
        '<script>document.querySelector("#import").onclick=() => {'
        f'const e=document.querySelector("#error"); {transition};'
        '};</script>')
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
    if passes:
        rows = await pending
        assert len(rows) == 2
        assert all(not row['errors'] and row['actions'] == [a.model_dump() for a in actions] for row in rows)
        assert all(Path(row['screenshot']).is_file() for row in rows)
    else:
        with pytest.raises(PrototypeBrowserError) as rejected:
            await pending
        row = rejected.value.results[0]
        assert row['actions'] == [actions[0].model_dump()]
        assert row['failed_action'] == actions[1].model_dump()
        assert row['errors'] and Path(row['screenshot']).is_file()
    if entry == 'application':
        assert not list((root / 'processes').glob('*.json'))
        assert not list((root / 'tmp').glob('preview-*'))
