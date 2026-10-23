"""Finite text semantics: rendered HTML, native value labels and laid-out SVG text."""
import sys
from pathlib import Path

import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2.config import ConfigStore
from auto_agents_2.design import exercise, inspect_application, inspect_prototype
from auto_agents_2.environment import Environment
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.sandbox import SandboxRunner

POSITIVES = {
    'button': '<input id="feedback" type="button" value="Saved">',
    'submit': '<input id="feedback" type="submit" value="Saved">',
    'reset': '<input id="feedback" type="reset" value="Saved">',
    'svg': '<svg width="260" height="60"><text id="feedback" x="4" y="30">Saved</text></svg>',
}


@pytest.mark.parametrize('entry', ['prototype', 'application'])
@pytest.mark.parametrize('kind', POSITIVES)
async def test_native_labels_and_svg_through_real_preview_entries(browser_cache, tmp_path, entry, kind):
    work = tmp_path / 'page'
    work.mkdir()
    (work / 'index.html').write_text('<!doctype html><meta charset="utf-8">' + POSITIVES[kind])
    actions = [BrowserAction(action='expect_text', selector='#feedback', value='Saved')]
    if entry == 'prototype':
        rows = await inspect_prototype(work, tmp_path / 'images', actions)
    else:
        ConfigStore(tmp_path).initialize()
        root = tmp_path / '.aa2'
        spec = PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
                           actions=actions)
        rows = await inspect_application(spec, work, tmp_path / 'images', Environment(Path(sys.prefix), 'fixture'),
                                         SandboxRunner(root, _test_unconfined=True), root)
        assert not list((root / 'processes').glob('*.json'))
    assert len(rows) == 2 and all(not r['errors'] and len(r['actions']) == 1 for r in rows)
    assert all(Path(r['screenshot']).is_file() for r in rows)


CASES = [
    ('svg_hidden_display', '<svg><text id="x" x="4" y="30">Waiting<tspan style="display:none">Saved</tspan></text></svg>', 'Saved', False),
    ('svg_hidden_visibility', '<svg><text id="x" x="4" y="30">Waiting<tspan style="visibility:hidden">Saved</tspan></text></svg>', 'Saved', False),
    ('svg_visible_and_hidden', '<svg><text id="x" x="4" y="30">Sa<tspan hidden style="display:none">old</tspan><tspan>ved</tspan></text></svg>', 'Saved', True),
    ('svg_title_not_rendered', '<svg id="x" width="100" height="80"><title>Saved</title><rect width="10" height="10"/></svg>', 'Saved', False),
    ('contents_visible', '<div id="x" style="display:contents">Saved<span hidden>old</span></div>', 'Saved', True),
    ('contents_hidden', '<div id="x" style="display:contents"><span hidden>Saved</span><span style="display:inline-block;width:10px;height:10px"></span></div>', 'Saved', False),
    ('unicode_whitespace', '<p id="x">  错误&nbsp; \n 文件\t café e\u0301 👩\u200d💻 العربية 漢字  </p>', '错误 文件 café e\u0301 👩\u200d💻 العربية 漢字', True),
    ('native_button_hidden', '<input id="x" type="button" style="visibility:hidden" value="Saved">', 'Saved', False),
    ('input_text_is_not_label', '<input id="x" type="text" value="Saved">', 'Saved', False),
    ('input_hide_on_change', '<input id="x" type="submit" value="Waiting"><script>setTimeout(()=>{x.value="Saved";x.style.display="none"},100)</script>', 'Saved', False),
    ('replace_element_category', '<input id="x" type="button" value="Waiting"><script>setTimeout(()=>{x.outerHTML="<p id=x>Saved</p>"},100)</script>', 'Saved', True),
]


@pytest.mark.parametrize(('name', 'html', 'expected', 'passes'), CASES, ids=[c[0] for c in CASES])
async def test_current_element_observation_uses_only_its_visible_text(browser_cache, name, html, expected, passes):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<!doctype html><meta charset="utf-8">' + html)
            action = BrowserAction(action='expect_text', selector='#x', value=expected)
            completed = []
            if passes:
                await exercise(page, [action], '/', completed)
                assert completed == [action.model_dump()]
            else:
                with pytest.raises(AssertionError):
                    await exercise(page, [action], '/', completed)
                assert completed == []
        finally:
            await browser.close()
