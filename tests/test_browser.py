import base64
import os
import struct
import sys
from pathlib import Path

import pytest

from auto_agents_2.config import ConfigStore
from auto_agents_2.design import inspect_application, inspect_prototype, prototype_routes
from auto_agents_2.environment import Environment
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.replanning import browser_criteria
from auto_agents_2.sandbox import SandboxRunner


@pytest.fixture
def browser_cache(monkeypatch):
    pytest.importorskip("playwright")
    cache = Path(os.environ.get("AA2_BROWSER_CACHE", Path(__file__).parents[1] / ".cache/browsers"))
    if not list(cache.glob("chromium-*")):
        pytest.skip("Install Chromium into AA2_BROWSER_CACHE to run real browser tests")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))


@pytest.mark.asyncio
async def test_static_interaction_and_implemented_preview(browser_cache, tmp_path):
    directory = tmp_path / "pages"
    directory.mkdir()
    (directory / "index.html").write_text('''<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><button id="increment" onclick="document.getElementById('count').textContent='1'">Add</button><p id="count">0</p></body></html>''')
    actions = [BrowserAction(route="/index.html", action="click", selector="#increment"),
               BrowserAction(route="/index.html", action="expect_text", selector="#count", value="1")]
    prototype = await inspect_prototype(directory, tmp_path / "prototype-images", actions)
    assert len(prototype) == 2 and all(len(r["actions"]) == 2 for r in prototype)
    assert all(Path(r["screenshot"]).is_file() for r in prototype)
    ConfigStore(tmp_path).initialize()
    root = tmp_path / ".aa2"
    runner = SandboxRunner(root, _test_unconfined=True)
    spec = PreviewSpec(command=Command(argv=[sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"]),
                       routes=["/index.html"], actions=actions)
    actual = await inspect_application(spec, directory, tmp_path / "actual-images", Environment(Path(sys.prefix), "fixture"), runner, root)
    assert len(actual) == 2 and all(r["status"] == 200 and len(r["actions"]) == 2 for r in actual)
    assert not list((root / "processes").glob("*.json"))
    assert not list((root / "tmp").glob("preview-*"))


async def test_backend_preview_reads_sibling_build_inside_real_sandbox(browser_cache, tmp_path, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    ConfigStore(tmp_path).initialize()
    root = tmp_path / ".aa2"
    work = root / "worktrees/preview/integration"
    (work / "backend").mkdir(parents=True)
    (work / "frontend/dist").mkdir(parents=True)
    (work / "frontend/dist/index.html").write_text('<h1 id="status">Built application</h1>')
    (work / "backend/server.py").write_text(
        "import sys\nfrom pathlib import Path\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  data=(Path(__file__).parents[1]/'frontend/dist/index.html').read_bytes()\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(data)\n"
        "HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()\n")
    spec = PreviewSpec(command=Command(argv=[sys.executable, "server.py", "{port}"], cwd="backend",
                                      inputs=["frontend/dist"]),
                       actions=[BrowserAction(action="expect_text", selector="#status", value="Built application")])
    rows = await inspect_application(spec, work, tmp_path / "images", Environment(Path(sys.prefix), "fixture"),
                                     SandboxRunner(root), root)
    assert len(rows) == 2 and all(r["status"] == 200 and len(r["actions"]) == 1 for r in rows)
    assert not list((root / "processes").glob("*.json"))


@pytest.mark.asyncio
async def test_prototype_cannot_call_external_service(browser_cache, tmp_path):
    from auto_agents_2.errors import AA2Error
    (tmp_path / "index.html").write_text('<script>fetch("https://example.invalid/charge-card").catch(()=>{});</script>')
    with pytest.raises(AA2Error, match="external request"):
        await inspect_prototype(tmp_path, tmp_path / "images")


async def test_nested_prototype_query_scenarios_execute_all_actions(browser_cache, tmp_path):
    directory = tmp_path / "pages"
    (directory / "task").mkdir(parents=True)
    (directory / "task/index.html").write_text('''<button id="add" onclick="
        localStorage.setItem('used','yes');document.querySelector('#result').textContent='done'">Add</button>
        <p id="result"></p><script>
        document.querySelector('#result').textContent = localStorage.getItem('used') ||
            new URLSearchParams(location.search).get('scene') || 'default';
        </script>''')
    actions = []
    for route, initial in (("/index.html", "default"), ("/index.html?scene=empty", "empty"),
                           ("/task/index.html?scene=error", "error")):
        actions.extend([BrowserAction(route=route, action="expect_text", selector="#result", value=initial),
                        BrowserAction(route=route, action="click", selector="#add"),
                        BrowserAction(route=route, action="expect_text", selector="#result", value="done")])
    rows = await inspect_prototype(directory, tmp_path / "images", actions)
    assert len(rows) == 6
    assert all(r["resolved_route"].startswith("/task/index.html") for r in rows)
    assert all(len(r["actions"]) == 3 for r in rows)
    assert len({r["screenshot"] for r in rows}) == 6
    assert all(browser_criteria(actions, rows).values())


@pytest.mark.parametrize("route", ["/missing.html", "/index.html", "https://example.com/index.html",
                                   "//example.com/index.html", "/%2e%2e/index.html", "/a/../b/index.html"])
def test_unmatched_ambiguous_and_nonlocal_routes_are_rejected(tmp_path, route):
    from auto_agents_2.errors import AA2Error
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "index.html").write_text("<p>test</p>")
    with pytest.raises(AA2Error):
        prototype_routes(tmp_path, [BrowserAction(route=route, action="expect_visible", selector="p")])
    assert prototype_routes(tmp_path, [BrowserAction(route="/a/index.html", action="expect_visible", selector="p")])[0] == (
        "/a/index.html", "/a/index.html")


async def test_failed_interaction_keeps_completed_prefix_and_failure(browser_cache, tmp_path):
    from auto_agents_2.errors import PrototypeBrowserError
    (tmp_path / "index.html").write_text('<button id="ok">OK</button>')
    actions = [BrowserAction(route="/index.html", action="click", selector="#ok"),
               BrowserAction(route="/index.html", action="click", selector="[")]
    with pytest.raises(PrototypeBrowserError) as failed:
        await inspect_prototype(tmp_path, tmp_path / "images", actions)
    row = failed.value.results[0]
    assert row["actions"] == [actions[0].model_dump()]
    assert row["failed_action"] == actions[1].model_dump()
    assert row["errors"] and Path(row["screenshot"]).exists()


async def test_capture_preserves_responsive_end_state_and_collects_content_below_fold(browser_cache, tmp_path, monkeypatch):
    from auto_agents_2.design import capture_page
    (tmp_path / "index.html").write_text('''<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body {margin:0; height:2100px; background:lime}
        #bottom {position:absolute; top:1850px; left:0; width:100%; height:100px; background:blue}</style>
        <button id="next" onclick="state.textContent='page 2'; window.armed=true">Next</button>
        <p id="state">page 1</p><div id="bottom"></div>
        <script>window.resizes=0;
        addEventListener('resize', () => {if(window.armed) {resizes++; state.textContent='wrong page'}});
        matchMedia('(max-width:600px)').addEventListener('change', () => {
            if(window.armed) state.textContent='wrong page';
        });</script>''')

    async def checked(page, path, row, actions):
        await capture_page(page, path, row, actions)
        assert await page.locator("#state").inner_text() == "page 2"
        assert await page.evaluate("resizes") == 0
        assert await page.evaluate("scrollY") == row["capture_origin"]["y"]
        tile = row["tiles"][-1]
        raw = Path(tile["screenshot"]).read_bytes()
        assert struct.unpack(">II", raw[16:24]) == (row["viewport"], row["viewport_height"])
        # Prove that the unmodified image actually renders content below the first
        # viewport, rather than padding an incomplete capture to full-page size.
        pixel = await page.evaluate('''async ({data,y}) => {
            const image = new Image(); image.src = 'data:image/png;base64,' + data; await image.decode();
            const canvas = document.createElement('canvas'); canvas.width=image.width; canvas.height=image.height;
            const ctx=canvas.getContext('2d'); ctx.drawImage(image,0,0);
            return [...ctx.getImageData(20,y,1,1).data];
        }''', {"data": base64.b64encode(raw).decode(), "y": 1900 - tile["y"]})
        assert pixel == [0, 0, 255, 255]

    monkeypatch.setattr("auto_agents_2.design.capture_page", checked)
    actions = [BrowserAction(route="/index.html", action="click", selector="#next"),
               BrowserAction(route="/index.html", action="expect_text", selector="#state", value="page 2")]
    rows = await inspect_prototype(tmp_path, tmp_path / "images", actions)
    assert len(rows) == 2 and all(row["tiles"] and not row["errors"] for row in rows)
