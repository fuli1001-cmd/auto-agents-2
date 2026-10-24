"""Page exceptions must not acquire the driver's navigation retry semantics."""
import sys
import time
from pathlib import Path

import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2 import design
from auto_agents_2.config import ConfigStore
from auto_agents_2.environment import Environment
from auto_agents_2.errors import PrototypeBrowserError
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize(('kind', 'phase'), [
    ('string-detach', 'text'), ('string-navigation', 'text'), ('string-navigation-dot', 'text'),
    ('error', 'text'), ('nameless-error', 'text'), ('undefined', 'text'), ('null', 'text'),
    ('number', 'text'), ('symbol', 'text'), ('hostile-object', 'text'), ('string-detach', 'layout'),
])
async def test_one_shot_page_exception_is_terminal_before_later_correct_text(browser_cache, kind, phase):
    from playwright.async_api import Error, async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content('<p id="status">Saved</p>')
            await page.evaluate('''({kind, phase}) => {
                const element = document.querySelector('#status');
                const nativeText = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText').get;
                const nativeRects = element.getClientRects;
                window.probeReads = 0;
                window.detailReads = 0;
                window.stringCoercions = 0;
                const failOnce = () => {
                    if (++window.probeReads !== 1) return;
                    if (kind === 'string-detach') throw 'Frame was detached';
                    if (kind === 'string-navigation') throw 'Execution context was destroyed, most likely because of a navigation';
                    if (kind === 'string-navigation-dot') throw 'Execution context was destroyed, most likely because of a navigation.';
                    if (kind === 'error') throw new Error('Frame was detached');
                    if (kind === 'nameless-error') { const error = new Error('Frame was detached'); error.name = ''; throw error; }
                    if (kind === 'undefined') throw undefined;
                    if (kind === 'null') throw null;
                    if (kind === 'number') throw 17;
                    if (kind === 'symbol') throw Symbol('Frame was detached');
                    if (kind === 'hostile-object') throw {
                        get message() { window.detailReads++; throw 'Frame was detached'; },
                        toString() { window.stringCoercions++; throw 'Frame was detached'; }
                    };
                };
                if (phase === 'text') Object.defineProperty(element, 'innerText', {
                    get() { failOnce(); return nativeText.call(this); }
                });
                else element.getClientRects = function() { failOnce(); return nativeRects.call(this); };
            }''', {'kind': kind, 'phase': phase})

            class RecordedPage:
                observations = 0

                def locator(self, selector):
                    locator = page.locator(selector)
                    owner = self

                    class RecordedLocator:
                        async def evaluate_all(self, expression, arg):
                            owner.observations += 1
                            return await locator.evaluate_all(expression, arg)

                    return RecordedLocator()

            observed = RecordedPage()
            action = BrowserAction(action='expect_text', selector='#status', value='Saved')
            completed = []
            start = time.monotonic()
            with pytest.raises(Error, match='expect_text page script failed'):
                await design.exercise(observed, [action], '/', completed)
            assert time.monotonic()-start < 3
            assert observed.observations == 1 and completed == []
            assert await page.evaluate('window.probeReads') == 1
            assert await page.evaluate('window.stringCoercions') == 0
            assert page.url == 'about:blank'
            # The later correct observation exists, but the failed action may not use it.
            assert await page.locator('#status').is_visible()
            assert await page.locator('#status').inner_text() == 'Saved'
        finally:
            await browser.close()


@pytest.mark.parametrize('entry', ['prototype', 'application'])
@pytest.mark.parametrize('throw_at', [1, 2], ids=['action', 'capture-terminal-recheck'])
async def test_real_preview_and_capture_keep_page_errors_as_failure(browser_cache, tmp_path, entry, throw_at):
    work = tmp_path/'page'
    work.mkdir()
    (work/'index.html').write_text('''<!doctype html><p id="status">Saved</p><script>
        const element = document.querySelector('#status');
        const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText').get;
        let reads = 0;
        Object.defineProperty(element, 'innerText', { get() {
            if (++reads === THROW_AT) throw 'Frame was detached';
            return original.call(this);
        }});
        </script>'''.replace('THROW_AT', str(throw_at)))
    action = BrowserAction(route='/index.html', action='expect_text', selector='#status', value='Saved')
    if entry == 'prototype':
        pending = design.inspect_prototype(work, tmp_path/'images', [action])
    else:
        ConfigStore(tmp_path).initialize()
        root = tmp_path/'.aa2'
        spec = PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
                           routes=['/index.html'], actions=[action])
        pending = design.inspect_application(spec, work, tmp_path/'images', Environment(Path(sys.prefix), 'fixture'),
                                             SandboxRunner(root, _test_unconfined=True), root)
    with pytest.raises(PrototypeBrowserError) as failed:
        await pending
    # The real preview entry stops at the first failed viewport.
    assert len(failed.value.results) == 1
    for row in failed.value.results:
        assert any('expect_text page script failed' in error for error in row['errors'])
        assert row['actions'] == ([] if throw_at == 1 else [action.model_dump()])
        if throw_at == 1:
            assert row['failed_action'] == action.model_dump()
        assert Path(row['screenshot']).is_file()
    if entry == 'application':
        assert all(r['cleanup'] == {'process_stopped': True, 'scratch_removed': True} for r in failed.value.results)
        assert not list((root/'processes').glob('*.json'))
        assert not list((root/'tmp').glob('preview-*'))
