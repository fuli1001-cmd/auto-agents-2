from __future__ import annotations

import asyncio
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .artifacts import ArtifactStore
from .config import Cleanup
from .environment import Environment
from .errors import PolicyViolation
from .evidence import check_environment, input_identity, runtime_configuration
from .logs import SanitizedLog
from .models import CheckResult, Command
from .progress import Activity
from .readview import sensitive_values
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import SECRET, contained, digest
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
                with self.resources.temporary("check") as scratch, self.resources.temporary("check-log") as log_root:
                    result = await self._check(command, workspace, environment, baseline, scratch, activity, log_root)
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

    async def _check(self, command, workspace, environment, baseline, scratch, activity=None, log_root=None):
        inputs_before = input_identity(workspace, command)
        cwd = workspace if command.cwd == "." else contained(workspace, command.cwd)
        executable = Path(command.argv[0]).name
        if executable in {"sh", "bash", "zsh", "sudo", "conda", "pip", "pip3", "curl", "wget"}:
            raise PolicyViolation("Verification requires a direct test/build executable")
        if executable in {"npm", "pnpm", "yarn"} and len(command.argv) > 1 and command.argv[1] in {"install", "add", "update", "remove", "ci", "exec"}:
            raise PolicyViolation("Dependency installation belongs to EnvironmentManager")
        env = check_environment(workspace, command, environment, scratch)
        runtime_config = runtime_configuration(workspace, command, environment)
        for declared in command.inputs:
            contained(workspace, declared)
        argv = list(command.argv)
        if environment:
            if executable in {"pytest", "py.test"}:
                argv = [str(environment.prefix / "bin/python"), "-m", "pytest", *argv[1:]]
            elif executable in {"python", "python3"}:
                argv[0] = str(environment.prefix / "bin/python")
            else:
                resolved = shutil.which(argv[0], path=env["PATH"])
                allowed = (environment.prefix.resolve(), workspace.resolve())
                if not resolved or not any(Path(resolved).absolute().is_relative_to(p) for p in allowed):
                    raise PolicyViolation("Verification executable is not owned by the selected environment or workspace")
                argv[0] = resolved
            if not Path(argv[0]).is_file():
                raise PolicyViolation("Verification interpreter is missing from the selected environment")
        pytest = executable in {"pytest", "py.test"} or argv[1:3] == ["-m", "pytest"]
        report_file = scratch / "junit.xml"
        if pytest:
            argv += [f"--junitxml={report_file}", "-p", "no:cacheprovider"]
        outputs = [scratch, cwd / ".pytest_cache", cwd / ".next", cwd / "dist", cwd / "test-results",
                   cwd / "node_modules/.vite", cwd / "node_modules/.cache", cwd / "node_modules/.tmp"]
        start = time.monotonic()
        secrets = [v for k, v in env.items() if SECRET.search(k)] + sensitive_values(workspace)
        logs = [SanitizedLog(log_root / "stdout.log", secrets), SanitizedLog(log_root / "stderr.log", secrets)]
        try:
            process = await self.sandbox.spawn(argv, cwd, env, outputs, [workspace, environment.prefix] if environment else [workspace])
        except BaseException:
            for log in logs:
                log.close()
            raise

        async def drain(stream, log):
            while chunk := await stream.read(8192):
                if activity:
                    activity.output()
                log.feed(chunk)
            log.finish()

        readers = [asyncio.create_task(drain(process.stdout, logs[0])), asyncio.create_task(drain(process.stderr, logs[1]))]
        timed_out = False
        try:
            async with asyncio.timeout(command.timeout):
                await asyncio.gather(process.wait(), *readers)
        except TimeoutError:
            timed_out = True
        finally:
            await self.sandbox.stop(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            outcomes = await asyncio.gather(*readers, return_exceptions=True)
            for log in logs:
                log.close()
        if not timed_out:
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        log_refs = [self.artifacts.put_file(log_root / name, "check_log") for name in ("stdout.log", "stderr.log")]
        for ref, log in zip(log_refs, logs):
            ref["capture_complete"] = log.complete
        output = "".join(log.tail for log in logs)
        # Stdout is diagnostic text, not controller-owned test-case evidence.
        tests, skipped = 0, 0
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
        if timed_out or input_identity(workspace, command) != inputs_before or tree_hash(workspace) != baseline or tests < max(command.expected_tests, int(pytest)) or not report_valid:
            code = code or 1
        return CheckResult(argv=command.argv, cwd=command.cwd, exit_code=code, output=output,
                           duration=time.monotonic() - start, source_hash=baseline,
                           environment_hash=environment.fingerprint if environment else "host",
                           command_hash=digest(command.model_dump()),
                           effective_argv=argv, checker_version="verification-v2", input_hash=inputs_before,
                           runtime_config_hash=runtime_config, logs=log_refs,
                           tests=tests, skipped=skipped, timed_out=timed_out, test_ids=test_ids,
                           passed_test_ids=passed_test_ids, report=report)


def passed(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.exit_code == 0 and not r.timed_out for r in results)
