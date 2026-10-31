"""Real held-pointer evidence through the finite controller action protocol."""
import asyncio
import copy
import json
import struct
import sys
from pathlib import Path

import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2 import design
from auto_agents_2.config import ConfigStore
from auto_agents_2.environment import Environment
from auto_agents_2.errors import PrototypeBrowserError
from auto_agents_2.models import BrowserAction, Command, DocumentResult, PreviewSpec
from auto_agents_2.sandbox import SandboxRunner

PAGE = """<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body {margin:0;min-height:2100px} #target {position:fixed;left:40px;top:40px;
width:160px;height:80px;border:0;background:rgb(230,230,230)}
#target:active {background:rgb(160,30,20)} #stats {position:fixed;top:140px}</style>
<button id="target" data-original>Hold</button><p id="stats">0/0/false</p>
<script>window.events={downs:0,clicks:0,trusted:false};
target.addEventListener('mousedown',e=>{events.downs++;events.trusted=e.isTrusted;show()});
target.addEventListener('click',()=>{events.clicks++;show()});
function show(){stats.textContent=[events.downs,events.clicks,events.trusted].join('/')}</script>"""


def action(kind, selector='#target', route='/', value=''):
    return BrowserAction(action=kind, selector=selector, route=route, value=value)


def test_mouse_down_is_supported_by_both_proposal_shapes():
    sequence = [action('mouse_down'), action('expect_visible', '#target:active')]
    proposal = DocumentResult(summary='Held state', document='Complete proposal', browser_actions=sequence)
    preview = PreviewSpec(command=Command(argv=['preview', '{port}']), actions=sequence)
    assert proposal.browser_actions == preview.actions == sequence


async def observation(page):
    return await page.locator('#target').evaluate("e => ({active:e.matches(':active'), ...window.events})")


async def pixel(page, image, x=50, y=50):
    # Decode the actual emitted PNG on a separate blank page; do not touch the
    # inspected application's DOM or styles to manufacture the expected image.
    import base64
    context = await page.context.browser.new_context()
    try:
        reader = await context.new_page()
        return await reader.evaluate("""async ({data,x,y}) => {
            const img=new Image(); img.src='data:image/png;base64,'+data; await img.decode();
            const canvas=document.createElement('canvas'); canvas.width=img.width;canvas.height=img.height;
            const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);return [...ctx.getImageData(x,y,1,1).data];
        }""", {'data':base64.b64encode(Path(image).read_bytes()).decode(),'x':x,'y':y})
    finally:
        await context.close()


async def test_real_held_input_is_not_a_released_click(browser_cache, tmp_path):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width':390,'height':844})
            await page.set_content(PAGE)
            await design.exercise(page, [action('click')], '/')
            assert await observation(page) == {'active':False,'downs':1,'clicks':1,'trusted':True}
            await page.screenshot(path=str(tmp_path/'clicked.png'))
            assert await pixel(page, tmp_path/'clicked.png') == [230,230,230,255]
            # Persisted/in-memory objects can reach the executor independently of
            # response validation. This isolates its real input capability too.
            held = BrowserAction.model_construct(route='/', action='mouse_down', selector='#target', value='')
            sequence = [held, action('expect_visible', '#target:active'),
                        action('expect_text', '#stats', value='2/1/true')]
            completed = await design.exercise(page, sequence, '/')
            before = await observation(page)
            row = {'route':'/','errors':[],'actions':completed}
            await design.capture_page(page, tmp_path/'held.png', row, sequence)
            assert before == await observation(page) == {'active':True,'downs':2,'clicks':1,'trusted':True}
            assert row['tiles'] and row['actions'] == [a.model_dump() for a in sequence]
            for image in [row, *row['tiles']]:
                assert await pixel(page, image['screenshot']) == [160,30,20,255]
            (tmp_path/'observations.json').write_text(json.dumps({'before':before,'row':row}))
        finally:
            await browser.close()


@pytest.mark.parametrize('following', ['mouse_down', 'click', 'fill', 'press', 'check'])
@pytest.mark.parametrize('shape', ['prototype', 'application'])
def test_interaction_after_down_is_rejected_at_each_model_entry(following, shape):
    from pydantic import ValidationError
    sequence = [action('mouse_down'), action('expect_visible', '#target:active'), action(following)]
    with pytest.raises(ValidationError, match='after mouse_down'):
        if shape == 'prototype':
            DocumentResult(summary='Invalid sequence', document='Fixture', browser_actions=sequence)
        else:
            PreviewSpec(command=Command(argv=['preview', '{port}']), actions=sequence)


@pytest.mark.parametrize('following', ['mouse_down', 'click', 'fill', 'press', 'check'])
async def test_executor_rejects_invalid_sequence_before_any_input(following):
    class NoInput:
        def locator(self, selector):
            pytest.fail('Invalid sequence must be rejected before any interaction')
    with pytest.raises(ValueError, match='after mouse_down'):
        await design.exercise(NoInput(), [action('mouse_down'), action(following)], '/')


def test_terminal_rules_are_per_route_and_leave_old_sequences_unchanged():
    old = [action('fill', '#field', value='text'), action('press', '#field', value='Tab'),
           action('check', '#check'), action('click'), action('expect_text', '#status', value='done')]
    sequence = [*old, action('mouse_down'), action('click', route='/other'),
                action('expect_visible', '#target:active'), action('mouse_down', route='/other'),
                action('expect_visible', '#target:active', route='/other')]
    assert DocumentResult(summary='Two scenarios', document='Complete', browser_actions=sequence).browser_actions == sequence
    assert PreviewSpec(command=Command(argv=['preview', '{port}']), routes=['/', '/other'], actions=sequence).actions == sequence
    assert DocumentResult(summary='Legacy', document='Unchanged', browser_actions=old).model_dump()['browser_actions'] == [a.model_dump() for a in old]


async def run_entry(entry, work, output, actions, tmp_path):
    if entry == 'prototype':
        return await design.inspect_prototype(work, output, actions)
    ConfigStore(tmp_path).initialize()
    root = tmp_path/'.aa2'
    spec = PreviewSpec(command=Command(argv=[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']),
                       routes=list(dict.fromkeys(a.route for a in actions)), actions=actions)
    return await design.inspect_application(spec, work, output, Environment(Path(sys.prefix), 'fixture'),
                                            SandboxRunner(root, _test_unconfined=True), root)


@pytest.mark.parametrize('entry', ['prototype', 'application'])
async def test_real_routes_and_viewports_keep_png_state_without_repeated_down(browser_cache, tmp_path, monkeypatch, entry):
    work = tmp_path/'pages'
    work.mkdir()
    sequence = []
    for route in ('/index.html', '/next.html'):
        (work/route.lstrip('/')).write_text(PAGE)
        sequence.extend([action('expect_visible', '#target:not(:active)', route),
                         action('expect_text', '#stats', route, '0/0/false'),
                         action('mouse_down', route=route), action('expect_visible', '#target:active', route),
                         action('expect_text', '#stats', route, '1/0/true')])
    capture = design.capture_page
    observed, closed = [], []

    async def inspect(page, image, row, actions):
        page.context.on('close', lambda: closed.append((row['route'], row['viewport'])))
        assert await observation(page) == {'active':True,'downs':1,'clicks':0,'trusted':True}
        await capture(page, image, row, actions)
        after = await observation(page)
        assert after == {'active':True,'downs':1,'clicks':0,'trusted':True}
        assert row['tiles'] and await page.evaluate('scrollY') == row['capture_origin']['y']
        for item in [row, row['tiles'][-1]]:
            raw = Path(item['screenshot']).read_bytes()
            assert struct.unpack('>II', raw[16:24]) == (row['viewport'], row['viewport_height'])
            assert await pixel(page, item['screenshot']) == [160,30,20,255]
        observed.append({'route':row['route'],'viewport':row['viewport'],**after})

    monkeypatch.setattr(design, 'capture_page', inspect)
    rows = await run_entry(entry, work, tmp_path/'images', sequence, tmp_path)
    assert len(rows) == len(observed) == len(closed) == 4
    assert set(closed) == {(route, width) for route in ('/index.html', '/next.html') for width in (1440,390)}
    for row in rows:
        assert not row['errors'] and row['actions'] == [a.model_dump() for a in sequence if a.route == row['route']]
        if entry == 'application':
            assert row['cleanup'] == {'process_stopped':True,'scratch_removed':True}
    assert not list((tmp_path/'.aa2/processes').glob('*.json'))
    assert not list((tmp_path/'.aa2/tmp').glob('preview-*'))
    (tmp_path/'observations.json').write_text(json.dumps(observed))


@pytest.mark.parametrize('entry', ['prototype', 'application'])
@pytest.mark.parametrize('fault', ['error', 'cancel'])
async def test_held_pointer_is_closed_on_capture_failure_or_cancel(browser_cache, tmp_path, monkeypatch, entry, fault):
    work = tmp_path/'pages'
    work.mkdir()
    (work/'index.html').write_text(PAGE)
    sequence = [action('mouse_down', route='/index.html'), action('expect_visible', '#target:active', '/index.html')]
    closed = []

    async def fail(page, image, row, actions):
        assert await observation(page) == {'active':True,'downs':1,'clicks':0,'trusted':True}
        page.context.on('close', lambda: closed.append(True))
        if fault == 'cancel':
            raise asyncio.CancelledError('fixture cancellation after genuine down')
        raise RuntimeError('fixture capture failure after genuine down')

    monkeypatch.setattr(design, 'capture_page', fail)
    with pytest.raises(asyncio.CancelledError if fault == 'cancel' else PrototypeBrowserError):
        await run_entry(entry, work, tmp_path/'images', sequence, tmp_path)
    assert closed == [True]
    assert not list((tmp_path/'.aa2/processes').glob('*.json'))
    assert not list((tmp_path/'.aa2/tmp').glob('preview-*'))


@pytest.mark.parametrize('change', ['move', 'replace', 'cover'])
async def test_hover_hit_target_change_requires_real_active_assertion(browser_cache, tmp_path, monkeypatch, change):
    from playwright.async_api import TimeoutError as BrowserTimeout
    from playwright.async_api import async_playwright
    behavior = {
        'move': "target.style.left='230px'",
        'replace': "const copy=target.cloneNode(true);copy.removeAttribute('data-original');target.replaceWith(copy)",
        'cover': "const cover=document.createElement('div');cover.style='position:fixed;left:40px;top:40px;width:160px;height:80px;z-index:5';document.body.append(cover)",
    }[change]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width':390,'height':844})
            await page.set_content(PAGE+f"<script>window.changeAfterHover=()=>{{{behavior}}}</script>")
            real_down = page.mouse.down

            async def changed_target(**kwargs):
                # Fault injection at the two public calls' actual boundary:
                # hover has passed its own actionability checks, then the page
                # changes before the real trusted down. The production driver
                # does not evaluate this fixture function or alter the DOM.
                assert await page.locator('#target[data-original]:hover').count() == 1
                await page.evaluate('changeAfterHover()')
                await real_down(**kwargs)

            monkeypatch.setattr(page.mouse, 'down', changed_target)
            held = action('mouse_down', '#target[data-original]')
            completed = await design.exercise(page, [held], '/')
            assert completed == [held.model_dump()]
            assert await page.locator('#target[data-original]:active').count() == 0
            row = {'route':'/','errors':[],'actions':completed}
            terminal = action('expect_visible', '#target[data-original]:active')
            with pytest.raises(BrowserTimeout):
                await design.capture_page(page, tmp_path/'changed.png', row, [held, terminal])
            assert Path(row['screenshot']).is_file() and not row['tiles']
            assert await page.evaluate('events.clicks') == 0
        finally:
            await browser.close()


async def test_scroll_state_loss_fails_without_replaying_down_or_losing_first_png(browser_cache, tmp_path):
    from playwright.async_api import TimeoutError as BrowserTimeout
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width':390,'height':844})
            await page.set_content(PAGE+"<script>addEventListener('scroll',()=>target.remove(),{once:true})</script>")
            sequence = [action('mouse_down'), action('expect_visible', '#target:active')]
            completed = await design.exercise(page, sequence, '/')
            row = {'route':'/','errors':[],'actions':completed}
            with pytest.raises(BrowserTimeout):
                await design.capture_page(page, tmp_path/'initial.png', row, sequence)
            assert await pixel(page, row['screenshot']) == [160,30,20,255]
            assert not row['tiles']
            assert await page.evaluate('events') == {'downs':1,'clicks':0,'trusted':True}
            assert await page.evaluate('scrollY') == row['capture_origin']['y']
        finally:
            await browser.close()


async def test_version_upgrade_rechecks_automation_and_never_rebinds_changed_actions(tmp_path, monkeypatch):
    from test_prototype_approvals import approval_fixture

    from auto_agents_2.approvals import prototype_identity
    from auto_agents_2.prototype_contract import prototype_contract
    from auto_agents_2.prototype_review import valid_basis
    from auto_agents_2.util import digest

    workflow, rid, document, question, calls, browsers = approval_fixture(tmp_path, monkeypatch, legacy=False)
    original = workflow.store.get(rid, 'prototype_approval')
    actions = document.model_dump()['browser_actions']
    human = workflow.approvals.human(rid, tmp_path, actions)
    assert design.PROTOTYPE_BROWSER_VERSION == 13
    assert 'mouse_down' in prototype_contract()['actions']
    assert prototype_contract()['hash'] != '67466f6c4e9c784732d40a0151d75b38600ee0e3930b9b362c03fdca11bd5a61'
    basis, _ = workflow.approvals.review_inputs(rid, tmp_path, actions, original['visuals'])
    assert valid_basis(basis)
    old_basis = copy.deepcopy(basis)
    old_basis['protocol_hash'] = '67466f6c4e9c784732d40a0151d75b38600ee0e3930b9b362c03fdca11bd5a61'
    old_basis['hash'] = digest({k:v for k,v in old_basis.items() if k != 'hash'})
    assert not valid_basis(old_basis)
    old = copy.deepcopy(original)
    old['browser_version'] = 12
    workflow.store.checkpoint(rid, 'prototype_approval', old)
    assert workflow.approvals.current(rid, tmp_path, actions, workflow.browser_check_version()) is None
    assert workflow.approvals.human(rid, tmp_path, actions) == human
    refreshed = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert refreshed['human']['question_id'] == question['id'] and len(browsers) == len(calls) == 1
    assert workflow.store.question(question['id'])['answer'] == '1'
    changed = [*actions, action('mouse_down', 'h1', '/index.html').model_dump(),
               action('expect_visible', 'h1:active', '/index.html').model_dump()]
    assert prototype_identity(tmp_path, changed)['identity'] != original['identity']
    assert workflow.approvals.human(rid, tmp_path, changed) is None
    assert workflow.store.question(question['id'])['answer'] == '1'
