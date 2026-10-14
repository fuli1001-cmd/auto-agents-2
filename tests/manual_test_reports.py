"""Opt-in real Vitest/Playwright report checks; installs only in a temporary project.

Run with the aa2 Python environment, passing an existing project Conda prefix that
contains Node/npm. Evidence and fixture sources remain in the printed directory.
"""
import asyncio
import json
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


async def main(prefix):
    project = Path(tempfile.mkdtemp(prefix="aa2-real-test-reports-"))
    print(f"Fixture and evidence: {project}", flush=True)
    config = ConfigStore(project)
    config.initialize()
    store = Store(project)
    rid = store.admit("fixture://test-report-collectors", {})["id"]
    runner = SandboxRunner(store.root)
    environment = Environment(prefix.resolve(), "report-smoke-runtime")
    installer = EnvironmentManager(config, runner)
    verifier = VerificationService(store.root, runner)
    work = project / "workspace"
    work.mkdir()
    write_json(work / "package.json", {
        "name": "aa2-test-report-fixture", "version": "1.0.0", "private": True, "type": "module",
        "scripts": {"test": "vitest run --maxWorkers=1 --no-file-parallelism", "test:e2e": "playwright test --workers=1"},
        "devDependencies": {"vitest": "3.2.4", "@playwright/test": "1.55.0"},
    })
    (work / "vitest.config.js").write_text("export default {test: {include: ['unit.test.js']}};\n")
    (work / "playwright.config.js").write_text("export default {testMatch: 'browser.spec.js'};\n")
    observations = []
    try:
        await installer.install_node(work, environment)
        for name, script, filename, module in (("vitest", "test", "unit.test.js", "vitest"),
                                               ("playwright", "test:e2e", "browser.spec.js", "@playwright/test")):
            for scenario in ("passing", "failing", "skip_only"):
                skip = ".skip" if scenario == "skip_only" else ""
                expected = 99 if scenario == "failing" else 5
                callback = f"() => expect(2+3).toBe({expected})"
                imports = f"import {{test, expect}} from '{module}';\n"
                if name == "playwright":
                    imports += "import fs from 'node:fs/promises'; import path from 'node:path';\n"
                    callback = ("async ({}, info) => { const file = info.outputPath('diagnostic.txt'); "
                                "await fs.mkdir(path.dirname(file), {recursive:true}); "
                                "await fs.writeFile(file, 'fixture attachment'); "
                                "await info.attach('diagnostic', {path:file, contentType:'text/plain'}); "
                                f"expect(2+3).toBe({expected}); }}")
                (work / filename).write_text(imports +
                                            f"test{skip}('sum', {callback});\n"
                                            "test.skip('unimplemented', () => {});\n")
                command = Command(argv=["npm", "run", script], expected_tests=1)
                result = (await verifier.run([command], work, environment))[0]
                store.checkpoint(rid, f"checks/{name}/{scenario}", result.model_dump())
                observations.append({"runner": name, "scenario": scenario, **result.model_dump()})
                print(json.dumps({"runner": name, "scenario": scenario, "process_exit_code": result.process_exit_code,
                                  "exit_code": result.exit_code, "tests": result.tests, "skipped": result.skipped,
                                  "reasons": result.failure_reasons}), flush=True)
                assert result.report_status == "valid", result.output
                assert (result.exit_code == 0) == (scenario == "passing"), result.output
                assert result.tests == (1 if scenario == "passing" else 0)
                if name == "playwright" and scenario != "skip_only":
                    assert any(verifier.artifacts.bytes(a["artifact"]) == b"fixture attachment" for a in result.attachments)
        store.update(rid, status="SUCCEEDED")
    except BaseException:
        store.update(rid, status="PAUSED")
        raise
    finally:
        write_json(project / "results.json", observations)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
