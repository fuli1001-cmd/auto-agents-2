import os
import sys
from pathlib import Path

import pytest

from auto_agents_2.config import ConfigStore
from auto_agents_2.design import inspect_application, inspect_prototype
from auto_agents_2.environment import Environment
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
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


@pytest.mark.asyncio
async def test_prototype_cannot_call_external_service(browser_cache, tmp_path):
    from auto_agents_2.errors import AA2Error
    (tmp_path / "index.html").write_text('<script>fetch("https://example.invalid/charge-card").catch(()=>{});</script>')
    with pytest.raises(AA2Error, match="external request"):
        await inspect_prototype(tmp_path, tmp_path / "images")
