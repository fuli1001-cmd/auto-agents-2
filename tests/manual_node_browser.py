"""Real project-local browser installation, headless input and screenshot capture.

Usage (aa2 Python): manual_node_browser.py CONDA_PREFIX NODE_MODULES
Copies dependencies into its own project; never changes the supplied installation.
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from auto_agents_2.config import ConfigStore
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.models import Command
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService

HTML = ('<!doctype html><html><body><button id="add">Add</button><output>0</output>'
        '<script>document.querySelector("#add").addEventListener("click",()=>{'
        'document.querySelector("output").textContent="1";});</script></body></html>')

TEST = """import {test,expect} from '@playwright/test';
import http from 'node:http';
test('real Chromium click and screenshot',async ({page}, info)=>{
  const server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end(HTML);});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.getByRole('button',{name:'Add'}).click();
    await expect(page.locator('output')).toHaveText('1');
    const file=info.outputPath('page.png');
    await page.screenshot({path:file});
    await info.attach('page',{path:file,contentType:'image/png'});
  } finally {await new Promise(resolve=>server.close(resolve));}
});
"""


async def main(prefix, modules):
    root = Path(tempfile.mkdtemp(prefix="aa2-node-browser-"))
    print(f"Evidence: {root}", flush=True)
    config = ConfigStore(root)
    config.initialize()
    store = Store(root)
    rid = store.admit("fixture://hermetic-browser", {})["id"]
    work = root / "workspace"
    work.mkdir()
    shutil.copytree(modules, work / "node_modules", symlinks=True,
                    ignore=shutil.ignore_patterns(".vite", ".vite-temp", ".cache", ".tmp"))
    write_json(work / "package.json", {"name": "aa2-node-browser", "type": "module",
                                      "scripts": {"test:e2e": "playwright test"}})
    (work / "playwright.config.mjs").write_text("export default {testMatch:'browser.spec.js',use:{headless:true}};\n")
    (work / "browser.spec.js").write_text("const HTML=" + json.dumps(HTML) + ";\n" + TEST)
    environment = Environment(prefix.resolve(), "browser-smoke-runtime")
    sandbox = SandboxRunner(store.root)
    try:
        await EnvironmentManager(config, sandbox).prepare_node_browsers(work, environment)
        service = VerificationService(store.root, sandbox)
        result = (await service.run([Command(argv=["npm", "run", "test:e2e", "--", "--workers=1"],
                                            expected_tests=1, timeout=120)], work, environment))[0]
        write_json(root / "results.json", result.model_dump())
        store.checkpoint(rid, "checks/browser", result.model_dump())
        assert result.exit_code == 0 and result.tests == 1, result.output
        screenshots = [a for a in result.attachments if a["path"].endswith("page.png")]
        assert screenshots and service.artifacts.bytes(screenshots[0]["artifact"]).startswith(b"\x89PNG")
        store.update(rid, status="SUCCEEDED")
        print("Passed: actual Chromium launch, HTTP page, ordinary click, assertion, archived screenshot", flush=True)
    except BaseException:
        store.update(rid, status="PAUSED")
        raise


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
