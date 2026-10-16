"""Real argument-composition regression matrix using already installed packages.

Run with the aa2 Python: manual_command_matrix.py CONDA_PREFIX NODE_MODULES [...].
Dependencies are copied with symlinks intact into disposable, sandboxed projects.
No provider is invoked and no input project, manifest or environment is modified.
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from manual_support import finalize

from auto_agents_2.environment import Environment
from auto_agents_2.models import Command
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService


async def main(prefix, installations):
    if not installations:
        raise ValueError("At least one NODE_MODULES installation is required")
    root = Path(tempfile.mkdtemp(prefix="aa2-command-matrix-"))
    print(f"Evidence: {root}", flush=True)
    store = Store(root)
    rid = store.admit("fixture://command-instrumentation", {})["id"]
    environment = Environment(prefix.resolve(), "argument-matrix-runtime")
    verifier = VerificationService(store.root, SandboxRunner(store.root))
    observations = []
    try:
        for index, modules in enumerate(installations):
            work = root / f"workspace-{index}"
            work.mkdir()
            shutil.copytree(modules, work / "node_modules", symlinks=True,
                            ignore=shutil.ignore_patterns(".vite", ".vite-temp", ".cache", ".tmp"))
            versions = {name: json.loads((work / "node_modules" / name / "package.json").read_text())["version"]
                        for name in ("vitest", "playwright")}
            (work / "vitest.config.js").write_text("export default {test:{include:['*.test.js']}};\n")
            (work / "playwright.config.js").write_text("export default {testMatch:'browser.spec.js'};\n")
            base_tests = ("import {test,expect} from 'vitest';\n"
                          "test.each([1,2])('same title', value=>expect(value).toBeGreaterThan(0));\n")
            npm_test = ("test('npm context',()=>{"
                        "expect(process.env.npm_lifecycle_event).toBe('test');"
                        "expect(process.env.npm_package_name).toBe('aa2-command-matrix'); });\n")
            (work / "other.test.js").write_text("import {test,expect} from 'vitest'; test('must not select',()=>expect(1).toBe(2));\n")
            (work / "skipped.test.js").write_text("import {test} from 'vitest'; test.skip('unimplemented',()=>{});\n")
            (work / "browser.spec.js").write_text("import {test,expect} from '@playwright/test';\n"
                                                  "test('selection',()=>expect(2+3).toBe(5));\n"
                                                  "test('excluded',()=>expect(1).toBe(2));\n")
            (work / "hook.mjs").write_text("console.log('hook:'+process.env.npm_lifecycle_event); "
                                          "if(process.argv[2]==='fail') process.exit(17);\n")
            (work / "user-report.xml").write_text("preserve existing report")
            cases = [
                ("bare", "vitest", [], 3, True),
                ("declared-run", "vitest", ["--run"], 3, True),
                ("run-subcommand", "vitest run", ["--run"], 3, True),
                ("script-run-flag", "vitest --run", ["--run"], 3, True),
                ("watch-false", "vitest --watch=false", ["--run"], 3, True),
                ("no-watch", "vitest --no-watch", [], 3, True),
                ("repeat-run", "vitest", ["--run", "--run=true"], 3, True),
                ("reporter-existing", "vitest --reporter=junit", ["--run", "--reporter=junit", "--reporter=json"], 3, True),
                ("report-path-existing", "vitest", ["--run", "--outputFile", "user-report.xml"], 3, True),
                ("report-map-existing", "vitest", ["--run", "--outputFile.junit=user-report.xml"], 3, True),
                ("name-filter", "vitest", ["--run", "-t", "same title"], 2, True),
                ("direct", "vitest", ["run", "--run"], 2, True),
                ("failed-hook", "vitest", ["--run"], 3, False),
                ("failed-test", "vitest", ["--run"], 0, False),
                ("skip-only", "vitest", ["--run"], 0, False),
            ]
            for label, script, args, expected, accepted in cases:
                (work / "selected.test.js").write_text(base_tests + ("" if label == "direct" else npm_test))
                package = {"name": "aa2-command-matrix", "private": True, "type": "module",
                           "scripts": {"pretest": "node hook.mjs", "test": script,
                                       "posttest": "node hook.mjs" + (" fail" if label == "failed-hook" else "")}}
                write_json(work / "package.json", package)
                file = "other.test.js" if label == "failed-test" else "skipped.test.js" if label == "skip-only" else "selected.test.js"
                argv = (["./node_modules/.bin/vitest", *args, file] if label == "direct" else
                        ["npm", "run", "test", "--", *args, file])
                result = (await verifier.run([Command(argv=argv, expected_tests=max(1, expected), timeout=60)], work, environment))[0]
                observations.append({"case": label, "versions": versions, **result.model_dump()})
                write_json(root / "results.json", observations)
                store.checkpoint(rid, f"check/{index}/{label}", result.model_dump())
                print(versions, label, result.exit_code, result.tests, result.failure_reasons, flush=True)
                if not (result.report_status == "valid" and result.tests == expected):
                    raise RuntimeError(result.output)
                if not ((result.exit_code == 0) == accepted):
                    raise RuntimeError(result.output)
                if label != "direct":
                    if "hook:pretest" not in result.output:
                        raise RuntimeError('Manual verification failed')
                    if accepted or label == "failed-hook":
                        if "hook:posttest" not in result.output:
                            raise RuntimeError('Manual verification failed')
                if label == "failed-hook":
                    if not (result.process_exit_code == 17 and "process_failed" in result.failure_reasons):
                        raise RuntimeError('Manual verification failed')
                expected_reasons = ({"process_failed"} if label == "failed-hook" else
                                    {"process_failed", "test_report_failed", "insufficient_test_cases"} if label == "failed-test" else
                                    {"insufficient_test_cases"} if label == "skip-only" else set())
                if (type(result.process_exit_code) is not int or set(result.failure_reasons) != expected_reasons
                        or result.skipped != (1 if label in {"name-filter", "skip-only"} else 0)
                        or (result.process_exit_code != 0) != (label in {"failed-test", "failed-hook"})):
                    raise RuntimeError("Manual report classification failed")
                if not ((work / "user-report.xml").read_text() == "preserve existing report"):
                    raise RuntimeError('Manual verification failed')
            write_json(work / "package.json", {"name": "aa2-command-matrix", "type": "module", "scripts": {
                "test": "playwright test --reporter=list --output=test-results/original"}})
            for mode in ("npm", "direct"):
                argv = ["npm", "test", "--"] if mode == "npm" else ["./node_modules/.bin/playwright", "test"]
                argv += ["--reporter=dot", "--reporter", "junit", "--output", "test-results/requested",
                         "--grep", "selection$", "--workers=1"]
                result = (await verifier.run([Command(argv=argv, expected_tests=1, timeout=60)], work, environment))[0]
                observations.append({"case": "playwright-" + mode, "versions": versions, **result.model_dump()})
                write_json(root / "results.json", observations)
                store.checkpoint(rid, f"check/{index}/playwright-{mode}", result.model_dump())
                print(versions, "playwright-" + mode, result.exit_code, result.tests, flush=True)
                if not (result.exit_code == 0 and result.tests == 1):
                    raise RuntimeError(result.output)
        store.update(rid, status="SUCCEEDED")
    except BaseException:
        store.update(rid, status="PAUSED")
        raise
    finally:
        finalize(lambda: write_json(root / "results.json", observations))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]))
