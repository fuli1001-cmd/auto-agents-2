from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .artifacts import ArtifactStore
from .config import Cleanup
from .environment import Environment
from .errors import PolicyViolation
from .models import CheckResult, Command
from .progress import Activity
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import contained, digest
from .workspace import tree_hash


class VerificationService:
    def __init__(self, root: Path, sandbox: SandboxRunner, resources: ResourceManager | None = None):
        self.root, self.sandbox = root, sandbox
        self.resources = resources or ResourceManager(Store(root.parent), Cleanup())
        self.artifacts = ArtifactStore(root)

    async def run(self, commands: list[Command], workspace: Path, environment: Environment | None = None) -> list[CheckResult]:
        results = []
        baseline = tree_hash(workspace)
        store = self.resources.store
        run = store.current(unfinished=True)
        activity = Activity(store, run["id"] if run else None, f"执行检查（{workspace.name}）", total=len(commands))
        try:
            for command in commands:
                with self.resources.temporary("check") as scratch:
                    result = await self._check(command, workspace, environment, baseline, scratch, activity)
                    results.append(result)
                activity.progress(len(results), len(commands))
                if result.exit_code:
                    break
        except asyncio.CancelledError:
            activity.finish("interrupted")
            raise
        except Exception:
            activity.finish("error")
            raise
        activity.finish("success" if passed(results) else "error")
        return results

    async def _check(self, command, workspace, environment, baseline, scratch, activity=None):
        cwd = workspace if command.cwd == "." else contained(workspace, command.cwd)
        executable = Path(command.argv[0]).name
        if executable in {"sh", "bash", "zsh", "sudo", "conda", "pip", "pip3", "curl", "wget"}:
            raise PolicyViolation("Verification requires a direct test/build executable")
        if executable in {"npm", "pnpm", "yarn"} and len(command.argv) > 1 and command.argv[1] in {"install", "add", "update", "remove", "ci", "exec"}:
            raise PolicyViolation("Dependency installation belongs to EnvironmentManager")
        env = environment.variables(scratch) if environment else {**os.environ, "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}
        env["PYTHONPATH"] = os.pathsep.join([str(cwd / "src"), str(cwd), str(workspace / "src"), str(workspace)])
        argv = list(command.argv)
        pytest = executable in {"pytest", "py.test"} or argv[1:3] == ["-m", "pytest"]
        report_file = scratch / "junit.xml"
        if pytest:
            argv += [f"--junitxml={report_file}", "-p", "no:cacheprovider"]
        outputs = [scratch, cwd / ".pytest_cache", cwd / ".next", cwd / "dist", cwd / "test-results",
                   cwd / "node_modules/.vite", cwd / "node_modules/.cache", cwd / "node_modules/.tmp"]
        start = time.monotonic()
        process = await self.sandbox.spawn(argv, cwd, env, outputs, [environment.prefix] if environment else [])
        tails = [bytearray(), bytearray()]

        async def drain(stream, tail):
            while chunk := await stream.read(8192):
                if activity:
                    activity.output()
                tail.extend(chunk)
                del tail[:-25000]

        readers = [asyncio.create_task(drain(process.stdout, tails[0])), asyncio.create_task(drain(process.stderr, tails[1]))]
        timed_out = False
        try:
            async with asyncio.timeout(command.timeout):
                await process.wait()
        except TimeoutError:
            timed_out = True
        finally:
            await self.sandbox.stop(process)
            await asyncio.gather(*readers, return_exceptions=True)
        output = b"".join(tails).decode(errors="replace")
        tests = sum(int(n) for n in re.findall(r"(\d+) passed", output))
        skipped = sum(int(n) for n in re.findall(r"(\d+) skipped", output))
        test_ids, passed_test_ids, report, report_valid = [], [], {}, not pytest
        if pytest and report_file.is_file():
            try:
                raw = report_file.read_text()
                cases = ET.fromstring(raw).findall(".//testcase")
                tests = sum(not any(case.find(k) is not None for k in ("failure", "error", "skipped")) for case in cases)
                skipped = sum(case.find("skipped") is not None for case in cases)
                test_ids = [case.get("classname", "") + "::" + case.get("name", "") for case in cases]
                passed_test_ids = [name for name, case in zip(test_ids, cases)
                                   if not any(case.find(k) is not None for k in ("failure", "error", "skipped"))]
                report = self.artifacts.put(raw)
                report_valid = bool(cases) and not any(case.find(k) is not None for case in cases for k in ("failure", "error"))
            except (ET.ParseError, UnicodeError):
                output += "\nInvalid controller-owned JUnit report."
        code = process.returncode if process.returncode is not None else -1
        if timed_out or tree_hash(workspace) != baseline or tests < max(command.expected_tests, int(pytest)) or not report_valid:
            code = code or 1
        return CheckResult(argv=command.argv, cwd=command.cwd, exit_code=code, output=output,
                           duration=time.monotonic() - start, source_hash=baseline,
                           environment_hash=environment.fingerprint if environment else "host",
                           command_hash=digest(command.model_dump()),
                           tests=tests, skipped=skipped, timed_out=timed_out, test_ids=test_ids,
                           passed_test_ids=passed_test_ids, report=report)


def passed(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.exit_code == 0 and not r.timed_out for r in results)
