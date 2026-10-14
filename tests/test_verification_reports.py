import sys
from pathlib import Path

import pytest
from test_runtime import configured

from auto_agents_2.environment import Environment
from auto_agents_2.evidence import CHECKER_VERSION, reusable
from auto_agents_2.models import Command
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.test_reports import python_module, read_junit, report_command
from auto_agents_2.test_reports import test_runner as runner_for
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService, passed


@pytest.mark.parametrize("options", [
    ["-B", "-m", "pytest"], ["-Bu", "-W", "error", "-X", "dev", "-m", "pytest"],
    ["-I", "-Wignore", "-Xdev", "-mpytest"], ["-Bmpytest"],
    ["--check-hash-based-pycs", "never", "-m", "pytest"],
])
def test_python_options_are_parsed_before_the_module(options):
    assert python_module(["/env/bin/python3.12", *options, "-q"]) == "pytest"


@pytest.mark.parametrize("argv", [
    ["python", "script.py", "-m", "pytest"], ["python", "-c", "pass", "-m", "pytest"],
    ["python", "--", "-m", "pytest"], ["python", "-W", "-m", "pytest"],
    ["node", "-m", "pytest"], ["python", "-X"], ["python", "-m"],
])
def test_python_script_arguments_cannot_impersonate_pytest(argv):
    assert python_module(argv) == ""


@pytest.mark.parametrize("script,runner", [
    ("vitest run", "vitest"), ("playwright test", "playwright"),
    ("vitest run && echo complete", ""), ("echo vitest", ""), ("node runner.js", ""),
])
def test_npm_runner_is_read_from_the_actual_script(tmp_path, script, runner):
    write_json(tmp_path / "package.json", {"scripts": {"test": script, "test:e2e": script}})
    for argv in (["npm", "test"], ["npm", "run", "test:e2e", "--", "selected.spec.ts"]):
        assert runner_for(argv, tmp_path) == runner
        effective, variables = report_command(argv, runner, tmp_path / "junit.xml", cwd=tmp_path)
        if runner:
            assert effective[effective.index("--") + 1:]
            assert "--reporter=line,junit" in effective if runner == "playwright" else "--reporter=junit" in effective
            if runner == "playwright":
                assert variables["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] == str(tmp_path / "junit.xml")


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["python", "-B", "-m", "pytest"], ["python3.11", "-W", "ignore", "-X", "dev", "-m", "pytest"],
    [sys.executable, "-I", "-m", "pytest"], ["python", "-Bmpytest"], ["pytest"],
])
async def test_flagged_pytest_captures_cases_in_the_selected_environment(tmp_path, argv):
    _, store = configured(tmp_path)
    (tmp_path / "test_sample.py").write_text("def test_pass(): assert 2 + 3 == 5\n")
    environment = Environment(Path(sys.prefix), "selected-env")
    service = VerificationService(store.root, SandboxRunner(store.root))
    command = Command(argv=[*argv, "-q", "--", "test_sample.py"], expected_tests=1)
    result = (await service.run([command], tmp_path, environment))[0]
    assert passed([result]) and result.process_exit_code == 0 and result.tests == 1
    assert result.report_status == "valid" and result.test_ids == ["test_sample::test_pass"]
    assert result.effective_argv[0] == str(environment.prefix / "bin/python")
    assert result.checker_version == CHECKER_VERSION and result.report
    assert reusable([command], [result], tmp_path, environment, service.artifacts).allowed
    obsolete = result.model_copy(update={"checker_version": "verification-v2"})
    assert not reusable([command], [obsolete], tmp_path, environment, service.artifacts).allowed
    missing = result.model_copy(update={"report": {}})
    assert not reusable([command], [missing], tmp_path, environment, service.artifacts).allowed
    damaged_attachment = result.model_copy(update={"attachments": [{"path": "trace.zip", "artifact": {}}]})
    assert not reusable([command], [damaged_attachment], tmp_path, environment, service.artifacts).allowed


@pytest.mark.parametrize("body", [
    b"<testsuite/>", b"<report><testcase name='invented'/></report>",
    b"<testsuite tests='2'><testcase name='only-one'/></testsuite>",
    b"<testsuite><testcase name='same'/><testcase name='same'/></testsuite>",
    b"<testsuite errors='-1'><testcase name='a'/></testsuite>",
])
def test_incomplete_or_inconsistent_reports_are_not_passing_evidence(body):
    with pytest.raises(ValueError):
        read_junit(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("report,raw_code,reason,status", [
    (None, 0, "test_report_missing", "missing"),
    ("<testsuite", 0, "test_report_invalid", "invalid"),
    ("<testsuite><testcase name='a'><skipped/></testcase></testsuite>", 0, "insufficient_test_cases", "valid"),
    ("<testsuite errors='1'><testcase name='a'/></testsuite>", 0, "test_report_failed", "valid"),
    ("<testsuite><testcase name='a'/></testsuite>", 7, "process_failed", "valid"),
])
async def test_raw_process_status_is_preserved_when_evidence_gate_rejects(tmp_path, report, raw_code, reason, status):
    _, store = configured(tmp_path)
    executable = tmp_path / "pytest"
    executable.write_text(f"#!{sys.executable}\nimport pathlib,sys\n"
                          "path=next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--junitxml='))\n"
                          f"body={report!r}\n"
                          "if body is not None: pathlib.Path(path).write_text(body)\n"
                          f"print('171 passed')\nsys.exit({raw_code})\n")
    executable.chmod(0o755)
    service = VerificationService(store.root, SandboxRunner(store.root))
    result = (await service.run([Command(argv=[str(executable)], expected_tests=1)], tmp_path))[0]
    assert result.process_exit_code == raw_code and result.exit_code != 0
    assert result.report_status == status and reason in result.failure_reasons
    assert reason in result.output and not passed([result])
    if report is not None:
        assert service.artifacts.read(result.report) == report


async def test_unsupported_test_wrapper_reports_the_collection_gap(tmp_path):
    _, store = configured(tmp_path)
    service = VerificationService(store.root, SandboxRunner(store.root))
    result = (await service.run([Command(argv=[sys.executable, "-c", "print('171 passed')"],
                                        expected_tests=1)], tmp_path))[0]
    assert result.process_exit_code == 0 and result.tests == 0 and result.exit_code == 1
    assert "unsupported_test_runner" in result.failure_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("executable", ["vitest", "./node_modules/.bin/vitest"])
async def test_direct_node_runner_uses_workspace_binary_and_generated_cache_only(tmp_path, executable):
    _, store = configured(tmp_path)
    work = tmp_path / "frontend"
    binary = work / "node_modules/.bin/vitest"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{sys.executable}\nimport pathlib,sys\n"
                      "cache=pathlib.Path('node_modules/.vite-temp')\n"
                      "cache.mkdir(exist_ok=True)\n(cache/'config.mjs').write_text('generated')\n"
                      "path=next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--outputFile.junit='))\n"
                      "pathlib.Path(path).write_text(\"<testsuite><testcase name='observed'/></testsuite>\")\n")
    binary.chmod(0o755)
    write_json(work / "package.json", {"name": "fixture"})
    service = VerificationService(store.root, SandboxRunner(store.root))
    environment = Environment(Path(sys.prefix), "runtime")
    command = Command(argv=[executable, "run"], cwd="frontend", expected_tests=1)
    result = (await service.run([command], tmp_path, environment))[0]
    assert result.exit_code == 0 and result.tests == 1
    assert (work / "node_modules/.vite-temp/config.mjs").is_file()
    assert reusable([command], [result], tmp_path, environment, service.artifacts).allowed


async def test_playwright_can_recreate_output_and_attachments_survive_scratch_cleanup(tmp_path):
    _, store = configured(tmp_path)
    binary = tmp_path / "node_modules/.bin/playwright"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{sys.executable}\nimport os,pathlib,shutil,sys\n"
                      "out=pathlib.Path(next(a.split('=',1)[1] for a in sys.argv if a.startswith('--output=')))\n"
                      "out.mkdir()\nshutil.rmtree(out)\nout.mkdir()\n"
                      "(out/'trace.zip').write_bytes(b'captured failure trace')\n"
                      "pathlib.Path(os.environ['PLAYWRIGHT_JUNIT_OUTPUT_FILE']).write_text(\""
                      "<testsuite><testcase name='failure'><failure/></testcase></testsuite>\")\n"
                      "sys.exit(1)\n")
    binary.chmod(0o755)
    service = VerificationService(store.root, SandboxRunner(store.root))
    result = (await service.run([Command(argv=["playwright", "test"], expected_tests=1)], tmp_path,
                                Environment(Path(sys.prefix), "runtime")))[0]
    assert result.process_exit_code == 1 and "test_report_failed" in result.failure_reasons
    assert result.attachments[0]["path"] == "trace.zip"
    assert service.artifacts.bytes(result.attachments[0]["artifact"]) == b"captured failure trace"
    assert not list((store.root / "tmp").glob("check_*"))
