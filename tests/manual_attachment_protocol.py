"""Opt-in real attachment protocol replay using existing exact driver/runtime files.

Does not install packages. Copies the small existing Node modules into a new
isolated fixture, then uses the normal verified shared-browser acquisition path.
Run with aa2's Python and arguments: existing-workspace existing-conda-prefix.
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
from auto_agents_2.util import digest, write_json
from auto_agents_2.verification import VerificationService


async def main(existing, prefix):
    project = Path(tempfile.mkdtemp(prefix="aa2-attachment-protocol-"))
    config = ConfigStore(project)
    config.initialize()
    settings = config.load().model_dump(mode="json")
    settings["shared_cache"]["enabled"] = True
    settings["cleanup"]["min_free_bytes"] = 0
    write_json(config.path, settings)
    store = Store(project)
    rid = store.admit("fixture://attachment-protocol", {})["id"]
    runner = SandboxRunner(store.root)
    manager = EnvironmentManager(config, runner)
    ready = json.loads((prefix / "aa2-ready.json").read_text())
    environment = Environment(prefix, ready["fingerprint"])
    work = project / "workspace"
    work.mkdir()
    # Read existing package bytes; never write/link mutable files back to a demo.
    shutil.copytree(existing / "node_modules", work / "node_modules", symlinks=True,
                    ignore=shutil.ignore_patterns('.local-browsers'))
    package = json.loads((existing / "package.json").read_text())
    package["scripts"] = {"test": "playwright test --workers=1"}
    write_json(work / "package.json", package)
    shutil.copyfile(existing / "package-lock.json", work / "package-lock.json")
    (work / "playwright.config.cjs").write_text("module.exports = {testMatch: 'protocol.spec.cjs'};\n")
    print(f"Fixture: {project}", flush=True)
    observations = []
    try:
        async def no_install(*args, **kwargs):
            raise AssertionError('This replay requires an existing verified cache hit; no download or installation allowed')
        manager._command = no_install
        await manager.prepare_node_browsers(work, environment)
        facts = manager.shared.facts(work)
        assert facts, "No verified shared-browser identity"
        verifier = VerificationService(store.root, runner)
        for mode in ("body", "file"):
            (work / "protocol.spec.cjs").write_text("""
const {test, expect} = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
test('required attachment originals', async ({page}, info) => {
  await page.setContent('<button>Retained evidence</button>');
  await expect(page.getByRole('button')).toHaveText('Retained evidence');
  const png = await page.screenshot();
  const report = Buffer.from(JSON.stringify({protocol:'outputPath-v1',assertion:'button text verified'}));
  for (const [name,body,contentType] of [['screen.png',png,'image/png'],['assertions.json',report,'application/json']]) {
    if (MODE === 'body') {
      await info.attach(name, {body,contentType});
    } else {
      const target = info.outputPath(name);
      await fs.mkdir(path.dirname(target), {recursive:true});
      await fs.writeFile(target, body);
      await info.attach(name, {path:target,contentType});
    }
  }
});
""".replace("MODE", json.dumps(mode)))
            result = (await verifier.run([Command(argv=["npm", "test"], expected_tests=1)], work, environment))[0]
            store.checkpoint(rid, "checks/" + mode, result.model_dump())
            assert result.exit_code == 0 and result.tests == 1, result.output
            bodies = [verifier.artifacts.bytes(row["artifact"]) for row in result.attachments]
            has_png = any(value.startswith(b"\x89PNG\r\n\x1a\n") for value in bodies)
            has_json = any(value == b'{"protocol":"outputPath-v1","assertion":"button text verified"}' for value in bodies)
            assert (has_png, has_json) == ((False, False) if mode == "body" else (True, True))
            observations.append({"mode": mode, "captured_png": has_png, "captured_assertion_json": has_json,
                "captured_hashes": [digest(value) for value in bodies], "check": result.model_dump()})
            print(json.dumps({"mode": mode, "tests": result.tests, "captured_png": has_png,
                              "captured_assertion_json": has_json}), flush=True)
        store.update(rid, status="SUCCEEDED")
    except BaseException:
        store.update(rid, status="PAUSED")
        raise
    finally:
        write_json(project / "protocol-results.json", {"fixture": str(project), "source_workspace": str(existing),
            "runtime_prefix": str(prefix), "observations": observations})


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
