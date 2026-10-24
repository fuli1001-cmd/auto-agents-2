"""Screenshot collection must not edit field styles to normalize the caret."""
import html
import sys
from pathlib import Path

import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2 import design
from auto_agents_2.config import ConfigStore
from auto_agents_2.environment import Environment
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize(('entry', 'nested', 'field'), [
    ('prototype', False, 'input'), ('prototype', True, 'editable'),
    ('application', False, 'textarea'), ('application', True, 'textarea'),
])
async def test_real_preview_capture_keeps_field_markup_and_state(browser_cache, tmp_path, monkeypatch,
                                                                entry, nested, field):
    work = tmp_path/'page'
    work.mkdir()
    inline = ' style="caret-color:rgb(10,20,30) !important;color:rgb(40,50,60)"' if field == 'textarea' else ''
    control = {'input': f'<input id="edit"{inline}>', 'textarea': f'<textarea id="edit"{inline}></textarea>',
               'editable': '<div id="edit" contenteditable="true">seed</div>'}[field]
    body = f'''<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body {{margin:0;min-height:2100px}} #edit {{min-height:30px}}</style>
        {control}<p id="feedback">Measured</p><script>
        localStorage.setItem('original','preserved'); window.styleMutations=0;
        new MutationObserver(records => {{
            window.styleMutations += records.length;
            document.querySelector('#feedback').textContent='Invalidated';
        }}).observe(document.querySelector('#edit'),{{attributes:true,attributeFilter:['style']}});
        </script>'''
    page_html = (f'<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">'
                 f'<style>body{{margin:0;min-height:2100px}}iframe{{width:100%;height:1200px;border:0}}</style>'
                 f'<iframe id="frame" srcdoc="{html.escape(body, quote=True)}"></iframe>' if nested else body)
    (work/'index.html').write_text(page_html)
    prefix = '#frame >> internal:control=enter-frame >> ' if nested else ''
    actions = [BrowserAction(route='/index.html', action='fill', selector=prefix+'#edit', value='real input'),
               BrowserAction(route='/index.html', action='expect_text', selector=prefix+'#feedback', value='Measured')]
    original_capture = design.capture_page
    observed = []

    async def capture(page, image, row, actions):
        target = page.locator(prefix+'#edit')
        read = '''element => ({markup:element.ownerDocument.body.innerHTML,
            value:element.value ?? element.textContent, style:element.getAttribute('style'),
            caret:element.ownerDocument.defaultView.getComputedStyle(element).caretColor,
            focus:element.ownerDocument.activeElement===element,
            saved:element.ownerDocument.defaultView.localStorage.getItem('original'),
            mutations:element.ownerDocument.defaultView.styleMutations})'''
        before = await target.evaluate(read)
        await original_capture(page, image, row, actions)
        after = await target.evaluate(read)
        assert before == after
        assert after['mutations'] == 0 and after['value'] == 'real input' and after['focus']
        assert after['saved'] == 'preserved' and row['tiles']
        assert all(Path(tile['screenshot']).is_file() for tile in row['tiles'])
        observed.append(after)

    monkeypatch.setattr(design, 'capture_page', capture)
    if entry == 'prototype':
        rows = await design.inspect_prototype(work, tmp_path/'images', actions)
    else:
        ConfigStore(tmp_path).initialize()
        root = tmp_path/'.aa2'
        spec = PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
                           routes=['/index.html'], actions=actions)
        rows = await design.inspect_application(spec, work, tmp_path/'images', Environment(Path(sys.prefix), 'fixture'),
                                               SandboxRunner(root, _test_unconfined=True), root)
        assert not list((root/'processes').glob('*.json'))
    assert len(rows) == len(observed) == 2
    assert all(not row['errors'] and row['actions'] == [a.model_dump() for a in actions] for row in rows)


async def test_real_state_change_during_image_still_fails_terminal_check(browser_cache, tmp_path):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content('<input id="edit"><p id="status">Measured</p>')
            action = BrowserAction(action='expect_text', selector='#status', value='Measured')
            await design.exercise(page, [action], '/')
            class ChangedPage:
                def __getattr__(self, name):
                    return getattr(page, name)

                async def screenshot(self, **kwargs):
                    result = await page.screenshot(**kwargs)
                    await page.locator('#status').evaluate("e => e.textContent='Changed'")
                    return result
            row = {'route':'/', 'errors':[]}
            with pytest.raises(AssertionError, match='Expected visible rendered text'):
                await design.capture_page(ChangedPage(), tmp_path/'changed.png', row, [action])
            assert Path(row['screenshot']).is_file()
            assert await page.locator('#status').inner_text() == 'Changed'
        finally:
            await browser.close()


async def test_failure_diagnostic_image_does_not_add_caret_mutations(browser_cache, tmp_path):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content('<input id="edit"><p id="status">Failed</p>')
            await page.locator('#edit').fill('retain')
            before = await page.locator('body').inner_html()
            row = {'route':'/', 'errors':['original failure']}
            await design.capture_page(page, tmp_path/'failure.png', row,
                [BrowserAction(action='expect_text', selector='#status', value='Passed')])
            assert row['errors'] == ['original failure'] and row['tiles'] == []
            assert Path(row['screenshot']).is_file()
            assert await page.locator('body').inner_html() == before
            assert await page.locator('#edit').input_value() == 'retain'
        finally:
            await browser.close()
