from __future__ import annotations

import asyncio
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .artifacts import ArtifactStore
from .builds import output_hashes, verification_commands
from .config import Cleanup
from .environment import Environment
from .errors import PolicyViolation, VerificationPreparationError
from .evidence import (
    CHECKER_VERSION,
    NODE_RUNTIME_CACHES,
    check_environment,
    input_identity,
    runtime_configuration,
)
from .logs import SanitizedLog
from .models import CheckResult, Command
from .progress import Activity
from .readview import sensitive_values
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .test_reports import is_python, read_junit, report_command, test_runner
from .util import SECRET, contained, digest, scrub_text
from .workspace import tree_hash


class VerificationService:
    def __init__(self, root: Path, sandbox: SandboxRunner, resources: ResourceManager | None = None):
        self.root, self.sandbox = root, sandbox
        self.resources = resources or ResourceManager(Store(root.parent), Cleanup())
        self.artifacts = ArtifactStore(root)

    async def run(self, commands: list[Command], workspace: Path, environment: Environment | None = None) -> list[CheckResult]:
        commands = verification_commands(commands, workspace)
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
            elif is_python(executable):
                argv[0] = str(environment.prefix / "bin/python")
            else:
                requested = argv[0]
                if "/" in requested and not Path(requested).is_absolute():
                    requested = str(cwd / requested)
                elif executable in {"vitest", "playwright"} and requested == executable:
                    local = cwd / "node_modules/.bin" / executable
                    if local.is_file():
                        requested = str(local)
                resolved = shutil.which(requested, path=env["PATH"])
                allowed = (environment.prefix.resolve(), workspace.resolve())
                if not resolved or not any(Path(resolved).absolute().is_relative_to(p) for p in allowed):
                    raise PolicyViolation("Verification executable is not owned by the selected environment or workspace")
                argv[0] = resolved
            if not Path(argv[0]).is_file():
                raise PolicyViolation("Verification interpreter is missing from the selected environment")
        runner = test_runner(command.argv, cwd)
        if executable == "npm" and command.expected_tests and not runner:
            raise VerificationPreparationError(
                "Unsupported npm test script for report collection; use a simple Vitest/Playwright script or direct runner command")
        report_file = scratch / "junit.xml"
        argv, report_env = report_command(argv, runner, report_file, cwd=cwd)
        env.update(report_env)
        generated = scratch / "generated"
        overlays = []
        for output in command.outputs:
            destination = contained(workspace, output)
            destination.mkdir(parents=True, exist_ok=True)
            staging = generated / output
            staging.mkdir(parents=True)
            overlays.append((staging, destination))
        outputs = [scratch, cwd / ".pytest_cache", cwd / ".next", cwd / "dist", cwd / "test-results",
                   *(cwd / "node_modules" / name for name in sorted(NODE_RUNTIME_CACHES))]
        outputs = [path for path in outputs if path not in {destination for _, destination in overlays}]
        start = time.monotonic()
        secrets = [v for k, v in env.items() if SECRET.search(k)] + sensitive_values(workspace)
        logs = [SanitizedLog(log_root / "stdout.log", secrets), SanitizedLog(log_root / "stderr.log", secrets)]
        try:
            process = await self.sandbox.spawn(argv, cwd, env, outputs,
                [workspace, environment.prefix] if environment else [workspace], overlays=overlays)
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
        test_ids, passed_test_ids, report = [], [], {}
        reasons = []
        report_status = "not_required" if not runner else "missing"
        if runner and report_file.is_file():
            report = self.artifacts.put_file(report_file, "junit")
            try:
                with report_file.open("rb") as stream:
                    cases = read_junit(stream.read(32 * 1024 * 1024 + 1))
                test_ids, passed_test_ids = cases.ids, cases.passed
                tests, skipped, report_status = cases.passed_count, cases.skipped, "valid"
                if cases.failures:
                    reasons.append("test_report_failed")
            except (ET.ParseError, ValueError) as exc:
                report_status = "invalid"
                output += "\nJUnit report rejected: " + scrub_text(str(exc), secrets)
        attachments = []
        attachment_root = scratch / "playwright-results"
        if runner == "playwright" and attachment_root.is_dir():
            for file in sorted(attachment_root.rglob("*")):
                if file.is_file() and not file.is_symlink():
                    attachments.append({"path": file.relative_to(attachment_root).as_posix(),
                                        "artifact": self.artifacts.put_file(file, "test_attachment")})
        if runner and report_status != "valid":
            reasons.append("test_report_" + report_status)
        if not runner and command.expected_tests:
            reasons.append("unsupported_test_runner")
        if tests < max(command.expected_tests, int(bool(runner))):
            reasons.append("insufficient_test_cases")
        if timed_out:
            reasons.append("timeout")
        if not all(log.complete for log in logs):
            reasons.append("incomplete_log_capture")
        if input_identity(workspace, command) != inputs_before:
            reasons.append("execution_inputs_changed")
        if tree_hash(workspace) != baseline:
            reasons.append("source_changed")
        if process.returncode != 0:
            reasons.append("process_failed")
        generated_outputs = {}
        if command.outputs and not reasons:
            try:
                generated_outputs = output_hashes(command, generated)
            except VerificationPreparationError as exc:
                reasons.append("build_outputs_missing_or_invalid")
                output += "\n" + str(exc)
        if command.outputs and not reasons:
            self._publish_outputs(command, workspace, generated, scratch / "previous-outputs")
        code = process.returncode if process.returncode is not None else -1
        if reasons:
            code = code or 1
            output += "\nController verification rejected: " + ", ".join(reasons) + "."
        return CheckResult(argv=command.argv, cwd=command.cwd, exit_code=code, output=output,
                           process_exit_code=process.returncode, failure_reasons=reasons,
                           test_runner=runner, report_status=report_status,
                           attachments=attachments,
                           duration=time.monotonic() - start, source_hash=baseline,
                           environment_hash=environment.fingerprint if environment else "host",
                           command_hash=digest(command.model_dump()),
                           effective_argv=argv, checker_version=CHECKER_VERSION, input_hash=inputs_before,
                           runtime_config_hash=runtime_config, logs=log_refs,
                           generated_outputs=generated_outputs,
                           tests=tests, skipped=skipped, timed_out=timed_out, test_ids=test_ids,
                           passed_test_ids=passed_test_ids, report=report)

    @staticmethod
    def _publish_outputs(command, workspace, generated, backup):
        """Publish only a successful, validated build; failed builds retain old files."""
        backup.mkdir()
        moved = []
        try:
            for index, output in enumerate(command.outputs):
                destination = contained(workspace, output)
                previous = backup / str(index)
                destination.rename(previous)
                moved.append((destination, previous, generated / output))
                (generated / output).rename(destination)
        except BaseException:
            for destination, previous, staged in reversed(moved):
                if destination.exists():
                    destination.rename(staged)
                previous.rename(destination)
            raise


def passed(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.exit_code == 0 and not r.timed_out and not r.failure_reasons
                                 and r.process_exit_code in {None, 0} for r in results)
