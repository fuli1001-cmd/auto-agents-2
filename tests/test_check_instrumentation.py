import shlex
import sys
from pathlib import Path

import pytest
from test_manifest_references import plan
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.environment import Environment
from auto_agents_2.errors import VerificationPreparationError
from auto_agents_2.models import Command
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.test_reports import read_junit, report_command
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService


@pytest.mark.parametrize("prefix,runner,args", [
    (["node", "node_modules/vitest/vitest.mjs"], "vitest", ["run", "src/core.test.ts", "--configLoader", "runner"]),
    (["node", "--enable-source-maps", "node_modules/@playwright/test/cli.js"], "playwright", ["test", "e2e/tasks.spec.ts"]),
    (["nodejs", "--", "node_modules/playwright/cli.js"], "playwright", ["test"]),
])
def test_node_package_entrypoints_keep_interpreter_and_selectors(tmp_path, prefix, runner, args):
    from auto_agents_2.test_reports import test_runner as discover
    original = [*prefix, *args]
    assert discover(original, tmp_path) == runner
    report = tmp_path / "private/junit.xml"
    instrumented, env = report_command(original, runner, report, cwd=tmp_path)
    assert instrumented[:len(prefix)] == prefix
    assert all(arg in instrumented for arg in args)
    assert report_command(instrumented, runner, report, cwd=tmp_path) == (instrumented, env)
    if runner == "vitest":
        assert "--reporter=junit" in instrumented
        assert f"--outputFile.junit={report}" in instrumented
        assert instrumented.count("run") == 1
    else:
        assert env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] == str(report)


@pytest.mark.parametrize("argv", [
    ["node", "-e", "console.log('vitest')", "node_modules/vitest/vitest.mjs"],
    ["node", "--eval=code", "node_modules/vitest/vitest.mjs"],
    ["node", "custom.mjs", "node_modules/vitest/vitest.mjs"],
    ["node", "vitest.mjs"], ["node", "node_modules/playwright/cli.js", "install"],
])
def test_node_discovery_does_not_infer_runner_from_arbitrary_code_or_arguments(tmp_path, argv):
    from auto_agents_2.test_reports import test_runner as discover
    assert discover(argv, tmp_path) == ""


def test_npm_node_wrapper_composes_reporting_after_the_script_entrypoint(tmp_path):
    from auto_agents_2.test_reports import test_runner as discover
    write_json(tmp_path / "package.json", {"scripts": {"test": "node --no-warnings node_modules/vitest/vitest.mjs run --configLoader runner"}})
    argv = ["npm", "run", "test", "--", "src/core.test.ts"]
    assert discover(argv, tmp_path) == "vitest"
    actual, _ = report_command(argv, "vitest", tmp_path / "report.xml", cwd=tmp_path)
    assert actual[:len(argv)] == argv
    assert "--run" not in actual and "--reporter=junit" in actual


@pytest.mark.parametrize("script", ["vitest", "vitest run", "vitest --run", "vitest --watch=false", "vitest --no-watch"])
@pytest.mark.parametrize("forwarded", [[], ["--run"], ["--run=true"], ["--run", "--run"], ["--watch=false"]])
def test_vitest_instrumentation_combines_script_and_forwarded_options_once(tmp_path, script, forwarded):
    package = {"scripts": {"pretest": "node pre.js", "test": script, "posttest": "node post.js"}}
    write_json(tmp_path / "package.json", package)
    original = ["npm", "run", "test", "--", *forwarded, "src/one.test.ts", "src/two.test.ts", "-t", "exact title", "--maxWorkers=1"]
    report = tmp_path / "private/junit.xml"
    argv, env = report_command(original, "vitest", report, cwd=tmp_path)
    assert report_command(argv, "vitest", report, cwd=tmp_path) == (argv, env)
    assert argv[:4] == original[:4] and original[4:4 + len(forwarded)] == forwarded
    combined = shlex.split(script)[1:] + argv[4:]
    assert sum(t.split("=")[0] == "--run" for t in combined) <= 1
    assert argv.count("src/one.test.ts") == argv.count("src/two.test.ts") == 1
    assert argv[argv.index("-t") + 1] == "exact title" and "--maxWorkers=1" in argv
    assert "--reporter=junit" in argv
    assert sum(t.startswith("--outputFile.junit=") for t in argv) == 1
    import json
    assert json.loads((tmp_path / "package.json").read_text()) == package


@pytest.mark.parametrize("argv,runner", [
    (["vitest", "--run", "--reporter=junit", "--reporter", "json", "--outputFile", "old.xml", "tests"], "vitest"),
    (["vitest", "run", "--outputFile.junit=old.xml", "--reporter=junit", "--", "--run"], "vitest"),
    (["python", "-X", "mpytest", "-B", "-m", "pytest", "-pno:cacheprovider", "--junit-xml", "old.xml", "--", "test_a.py"], "pytest"),
    (["playwright", "test", "--reporter", "list,junit", "--reporter=dot", "--output", "old", "--grep", "title", "--", "pattern"], "playwright"),
])
def test_instrumentation_is_idempotent_and_preserves_end_of_options(tmp_path, argv, runner):
    original = list(argv)
    first = report_command(argv, runner, tmp_path / "junit.xml")
    assert report_command(first[0], runner, tmp_path / "junit.xml") == first
    assert argv == original
    if "--" in argv:
        assert first[0][first[0].index("--"):] == argv[argv.index("--"):]
    if runner == "pytest":
        assert first[0][:6] == argv[:6]
        assert first[0].count("no:cacheprovider") == 1
        assert sum(x.startswith("--junitxml=") for x in first[0]) == 1
    elif runner == "playwright":
        assert first[0].count("--reporter=list,junit,dot") == 1
        assert first[0][first[0].index("--grep") + 1] == "title"
    else:
        assert first[0].count("--reporter=junit") == 1
        assert not any(x == "--outputFile" or x.startswith("--outputFile=") for x in first[0])


def test_script_reporter_and_npm_separator_are_preserved(tmp_path):
    write_json(tmp_path / "package.json", {"scripts": {"test": "vitest --run --reporter=junit"}})
    argv = ["npm", "test", "--", "--run", "--reporter=junit", "--reporter=dot", "--", "--run"]
    actual, _ = report_command(argv, "vitest", tmp_path / "junit.xml", cwd=tmp_path)
    assert actual.count("--reporter=junit") == 0  # The unchanged script owns this instance.
    assert actual.count("--run") == 1 and actual[-2:] == ["--", "--run"]


def test_negated_boolean_keeps_following_literal_filter(tmp_path):
    argv, _ = report_command(["vitest", "--no-watch", "false", "--", "true"], "vitest", tmp_path / "report.xml")
    assert "false" in argv and argv[-2:] == ["--", "true"]


@pytest.mark.parametrize("pattern", ['"one|two"', '"^title$"', "foo#bar", "'literal$VALUE'", r"one\|two"])
def test_quoted_npm_pattern_is_not_treated_as_a_shell_pipeline(tmp_path, pattern):
    from auto_agents_2.test_reports import test_runner as runner_for
    write_json(tmp_path / "package.json", {"scripts": {"test": f"vitest --run -t {pattern}"}})
    assert runner_for(["npm", "test"], tmp_path) == "vitest"
    argv, _ = report_command(["npm", "test"], "vitest", tmp_path / "report.xml", cwd=tmp_path)
    assert "--run" not in argv  # The existing script option is preserved.


@pytest.mark.parametrize("script", ['vitest # --run', 'vitest --run && echo ok', 'vitest --reporter="$REPORTER"'])
def test_shell_constructs_cannot_hide_or_redirect_controller_options(tmp_path, script):
    from auto_agents_2.test_reports import test_runner as runner_for
    write_json(tmp_path / "package.json", {"scripts": {"test": script}})
    assert runner_for(["npm", "test"], tmp_path) == ""


@pytest.mark.parametrize("script,args", [
    ("vitest", ["--watch"]), ("vitest", ["--run=false"]), ("vitest", ["--run", "--watch=true"]),
    ("vitest --watch", []), ("vitest --run --run", []), ("vitest --outputFile=public.xml", []),
    ("vitest --", []), ("vitest", ["--reporter"]), ("vitest", ["--no-run=false"]),
])
def test_unsafe_or_conflicting_instrumentation_stops_before_execution(tmp_path, script, args):
    write_json(tmp_path / "package.json", {"scripts": {"test": script}})
    with pytest.raises(VerificationPreparationError):
        report_command(["npm", "test", "--", *args], "vitest", tmp_path / "junit.xml", cwd=tmp_path)


async def test_preparation_error_preserves_applied_task_and_round_for_retry(tmp_path):
    from auto_agents_2.recovery import RecoveryNeeded
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("addition", {})["id"]
    proposal = plan()
    proposal.tasks[0].checks[0].argv.extend(["-p", "no:junitxml"])
    base = workflow.workspaces.committed_baseline(rid, "main")
    with pytest.raises(RecoveryNeeded) as raised:
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert raised.value.failure.component == "verification_preparation"
    assert "JUnit plugin" in raised.value.failure.evidence["error"]
    assert workflow.store.get(rid, "develop/task/T1")["phase"] == "applied"
    assert workflow.store.get(rid, "rounds/T1")["round"] == 1
    assert workflow.store.get(rid, "replanning/pending") is None
    assert not list((workflow.store.root / "processes").glob("*.json"))
    proposal.tasks[0].checks[0].argv = proposal.tasks[0].checks[0].argv[:-2]
    await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert sum(stage == "implement" for stage, _ in agent.calls) == 1
    assert workflow.store.get(rid, "rounds/T1")["round"] == 1


async def test_pytest_existing_report_and_plugin_options_use_only_private_report(tmp_path):
    _, store = configured(tmp_path)
    (tmp_path / "test_one.py").write_text("def test_one(): assert 1 + 1 == 2\n")
    public = tmp_path / "existing-report.xml"
    public.write_text("user-owned report")
    command = Command(argv=[sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "--junitxml", str(public), "--junit-xml=other.xml", "test_one.py"], expected_tests=1)
    result = (await VerificationService(store.root, SandboxRunner(store.root)).run(
        [command], tmp_path, Environment(Path(sys.prefix), "runtime")))[0]
    assert result.exit_code == 0 and result.tests == 1
    assert public.read_text() == "user-owned report" and not (tmp_path / "other.xml").exists()


@pytest.mark.parametrize("statuses", [(True, True), (True, False), (False, True)])
def test_repeated_parametrized_titles_have_conservative_group_identity(statuses):
    raw = "<testsuite name='parameters' tests='2'>" + "".join(
        "<testcase classname='suite' name='same'>" + ("" if ok else "<failure/>") + "</testcase>"
        for ok in statuses) + "</testsuite>"
    cases = read_junit(raw.encode())
    assert len(cases.ids) == len(set(cases.ids)) == 2
    assert cases.passed_count == sum(statuses)
    assert cases.passed == (cases.ids if all(statuses) else [])
    reordered = read_junit(raw.replace("<failure/>", "<skipped/>").encode())
    assert reordered.ids == cases.ids
    changed = read_junit(b"<testsuite tests='1'><testcase classname='suite' name='same'/></testsuite>")
    assert not set(changed.ids).intersection(cases.ids)
