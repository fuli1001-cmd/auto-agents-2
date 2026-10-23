"""The observation is atomic; navigation is retried only within one shared deadline."""
import asyncio
import time

import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2 import design
from auto_agents_2.models import BrowserAction


@pytest.mark.parametrize(('mode', 'final_state'), [
    ('page-navigation', 'match'), ('frame-navigation', 'match'), ('frame-replacement', 'match'),
    ('frame-replacement', 'hidden'), ('frame-navigation', 'wrong'), ('frame-replacement', 'missing'),
])
async def test_inflight_real_navigation_resolves_the_locator_again(browser_cache, mode, final_state):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            async def document(route):
                body = '<p id="status">Saved</p>'
                if route.request.url.endswith('/new'):
                    if final_state == 'hidden':
                        body = '<p id="status" hidden>Saved</p>'
                    elif final_state == 'wrong':
                        body = '<p id="status">Waiting</p>'
                await route.fulfill(body=body, content_type='text/html')

            await page.route('http://navigation.test/**', document)
            entered, release = asyncio.Event(), asyncio.Event()

            async def gate(_):
                entered.set()
                await release.wait()

            await page.expose_binding('observationGate', gate)
            if mode == 'page-navigation':
                await page.goto('http://navigation.test/old')
                locator = page.locator('#status')
            else:
                await page.set_content('<iframe id="frame" src="http://navigation.test/old"></iframe>')
                locator = page.locator('#frame >> internal:control=enter-frame >> #status')

            class InFlightObservation:
                calls = 0

                async def evaluate_all(self, expression, arg):
                    self.calls += 1
                    if self.calls == 1:
                        expression = ('async (elements, arg) => { await window.observationGate(); return ('
                                      + expression + ')(elements,arg); }')
                    return await locator.evaluate_all(expression, arg)

            observed = InFlightObservation()
            task = asyncio.create_task(design._expect_rendered_text(observed, 'Saved'))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                if mode == 'page-navigation':
                    await page.goto('http://navigation.test/new')
                elif mode == 'frame-navigation':
                    await page.frames[1].goto('http://navigation.test/new')
                elif final_state == 'missing':
                    await page.evaluate("document.querySelector('iframe').remove()")
                else:
                    await page.evaluate('''document.querySelector('iframe').outerHTML =
                        '<iframe id="frame" src="http://navigation.test/new"></iframe>' ''')
                if final_state == 'match':
                    await task
                    assert await locator.is_visible()
                    assert await locator.inner_text() == 'Saved'
                else:
                    with pytest.raises(AssertionError, match='Expected visible rendered text'):
                        await task
                assert observed.calls >= 2
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            await browser.close()


@pytest.mark.parametrize('entry', ['prototype', 'application'])
async def test_real_preview_entry_retries_frame_replacement(browser_cache, tmp_path, monkeypatch, entry):
    """Real public entry + actual driver exception; the wrapper only synchronizes the race."""
    import sys
    from pathlib import Path

    from playwright.async_api import Locator

    from auto_agents_2.config import ConfigStore
    from auto_agents_2.environment import Environment
    from auto_agents_2.models import Command, PreviewSpec
    from auto_agents_2.sandbox import SandboxRunner

    work = tmp_path/'page'
    work.mkdir()
    (work/'index.html').write_text('<iframe id="frame" src="child.html"></iframe>')
    (work/'child.html').write_text('<p id="status">Saved</p>')
    action = BrowserAction(route='/index.html', action='expect_text',
                           selector='#frame >> internal:control=enter-frame >> #status', value='Saved')
    original = Locator.evaluate_all
    seen, errors = set(), []

    async def read_during_replacement(locator, expression, arg=None):
        page = locator.page
        if page in seen:
            return await original(locator, expression, arg)
        seen.add(page)
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(_):
            entered.set()
            await release.wait()

        await page.expose_binding('observationGate', gate)

        async def replace():
            await asyncio.wait_for(entered.wait(), 3)
            await page.evaluate('''document.querySelector('iframe').outerHTML =
                '<iframe id="frame" src="child.html?replacement"></iframe>' ''')

        replacement = asyncio.create_task(replace())
        try:
            return await original(locator, 'async (elements, arg) => { await window.observationGate(); return ('
                                  + expression + ')(elements,arg); }', arg)
        except Exception as exc:
            errors.append(str(exc))
            raise
        finally:
            release.set()
            await replacement

    monkeypatch.setattr(Locator, 'evaluate_all', read_during_replacement)
    if entry == 'prototype':
        rows = await design.inspect_prototype(work, tmp_path/'images', [action])
    else:
        ConfigStore(tmp_path).initialize()
        root = tmp_path/'.aa2'
        spec = PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
                           routes=['/index.html'], actions=[action])
        rows = await design.inspect_application(spec, work, tmp_path/'images', Environment(Path(sys.prefix), 'fixture'),
                                                SandboxRunner(root, _test_unconfined=True), root)
        assert all(r['cleanup'] == {'process_stopped': True, 'scratch_removed': True} for r in rows)
    # Static prototype also inspects child.html without actions.
    exercised = [r for r in rows if r['route'] == '/index.html']
    assert len(exercised) == 2 and all(r['actions'] == [action.model_dump()] and not r['errors'] for r in exercised)
    assert len(errors) == 2 and all('Frame was detached' in e or 'Execution context was destroyed' in e for e in errors)


@pytest.mark.parametrize('message', [
    'Execution context was destroyed, most likely because of a navigation',
    'Execution context was destroyed, most likely because of a navigation.',
    'Frame was detached',
])
async def test_only_canonical_transient_driver_errors_are_retried(message):
    from playwright.async_api import Error

    class Locator:
        calls = 0

        async def evaluate_all(self, expression, arg):
            self.calls += 1
            if self.calls == 1:
                raise Error('Locator.evaluate_all: '+message)
            return {'count': 1, 'visible': True, 'matches': True}

    locator = Locator()
    await design._expect_rendered_text(locator, 'Saved')
    assert locator.calls == 2


@pytest.mark.parametrize('kind', ['driver', 'script_like_navigation', 'script_like_detach', 'invalid_selector', 'timeout', 'python', 'python_timeout', 'cancel'])
async def test_terminal_errors_and_cancellation_keep_their_identity(kind):
    import builtins

    from playwright.async_api import Error, TimeoutError

    error = {
        'driver': Error('Locator.evaluate_all: Target page, context or browser has been closed'),
        'script_like_navigation': Error('Locator.evaluate_all: Error: Execution context was destroyed, most likely because of a navigation'),
        'script_like_detach': Error('Locator.evaluate_all: Error: Frame was detached'),
        'invalid_selector': Error('Locator.evaluate_all: Unexpected token in selector'),
        'timeout': TimeoutError('Locator.evaluate_all: Timeout 30000ms exceeded'),
        'python': ValueError('broken observation'),
        'python_timeout': builtins.TimeoutError('unrelated timeout'),
        'cancel': asyncio.CancelledError(),
    }[kind]

    class Locator:
        calls = 0

        async def evaluate_all(self, expression, arg):
            self.calls += 1
            raise error

    locator = Locator()
    with pytest.raises(type(error)) as caught:
        await design._expect_rendered_text(locator, 'Saved')
    assert caught.value is error and locator.calls == 1


async def test_persistent_navigation_uses_one_original_deadline():
    from playwright.async_api import Error

    class Locator:
        calls = 0

        async def evaluate_all(self, expression, arg):
            self.calls += 1
            raise Error('Locator.evaluate_all: Frame was detached')

    locator = Locator()
    start = time.monotonic()
    with pytest.raises(AssertionError, match='Frame was detached'):
        await design._expect_rendered_text(locator, 'Saved')
    assert 9.5 <= time.monotonic()-start < 14
    assert 2 <= locator.calls < 150


@pytest.mark.parametrize('kind', ['invalid_selector', 'script', 'page_closed', 'multiple', 'hidden', 'wrong', 'missing'])
async def test_real_browser_errors_and_nonmatches_do_not_pass(browser_cache, kind):
    from playwright.async_api import Error, async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            html = '<p id="status">Saved</p>'
            if kind == 'multiple':
                html += html
            elif kind == 'hidden':
                html = '<p id="status" hidden>Saved</p>'
            elif kind == 'wrong':
                html = '<p id="status">Waiting</p>'
            elif kind == 'missing':
                html = '<p>Saved</p>'
            await page.set_content(html)
            if kind == 'script':
                await page.evaluate('''Object.defineProperty(HTMLElement.prototype, 'innerText',
                    {get(){throw new Error('Execution context was destroyed, most likely because of a navigation')}})''')
            if kind == 'page_closed':
                await page.close()
            action = BrowserAction(action='expect_text', selector='css=[' if kind == 'invalid_selector' else '#status', value='Saved')
            completed = []
            start = time.monotonic()
            with pytest.raises(Error if kind in {'invalid_selector', 'script', 'page_closed'} else AssertionError):
                await design.exercise(page, [action], '/', completed)
            assert not completed
            if kind in {'invalid_selector', 'script', 'page_closed', 'multiple'}:
                assert time.monotonic()-start < 3
        finally:
            await browser.close()
