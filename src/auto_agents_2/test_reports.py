"""Test-runner discovery and controller-owned JUnit reports."""
from __future__ import annotations

import json
import re
import shlex
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .errors import VerificationPreparationError
from .util import digest


def is_python(executable: str) -> bool:
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(executable).name))


def _python_module_target(argv: list[str]) -> tuple[str, int]:
    """Read interpreter options only; never mistake script/-c arguments for -m."""
    if not argv or not is_python(argv[0]):
        return "", 0
    index = 1
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg == "--check-hash-based-pycs":
            index += 1
            continue
        if not arg.startswith("-") or arg in {"-", "--"} or arg.startswith("--"):
            return "", 0
        flags = arg[1:]
        while flags:
            flag, flags = flags[0], flags[1:]
            if flag == "m":
                return (flags, index) if flags else ((argv[index], index + 1) if index < len(argv) else ("", 0))
            if flag in "chV?" or flag not in "bBdEiIOPqRsSuvxWX":
                return "", 0
            if flag in "WX":
                if not flags:
                    index += 1
                break
    return "", 0


def python_module(argv: list[str]) -> str:
    return _python_module_target(argv)[0]


def _literal_script(body: str) -> list[str]:
    """Accept literal shell words; reject operators, comments and expansions.

    Quotes and escapes matter: a quoted regex containing | or an in-word # is
    literal, whereas a shell comment would swallow all appended report options.
    """
    quote, escaped, word_start = "", False, True
    for index, char in enumerate(body):
        if escaped:
            if char == "\n":
                return []
            escaped, word_start = False, False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char in {"'", '"'} and (not quote or quote == char):
            quote = char if not quote else ""
            word_start = False
            continue
        if quote == "'":
            continue
        if char == "`" or (char == "$" and index + 1 < len(body)
                           and (body[index + 1].isalnum() or body[index + 1] in "_{(?#!$*@-")):
            return []
        if not quote:
            if char in ";&|<>\n" or char == "#" and word_start:
                return []
            word_start = char.isspace()
    return shlex.split(body)


def _npm_script(argv: list[str], cwd: Path) -> list[str]:
    if argv[1:2] in (["test"], ["t"]):
        script = "test"
    elif argv[1:2] in (["run"], ["run-script"]) and len(argv) > 2:
        script = argv[2]
    else:
        return []
    try:
        body = json.loads((cwd / "package.json").read_text())["scripts"][script]
        # Complex shell wrappers need an explicit direct runner command. Do not infer
        # a runner from a word in arbitrary shell text or forward options to a chain.
        if not isinstance(body, str):
            return []
        words = _literal_script(body)
        if not words or Path(words[0]).name not in {"vitest", "playwright"}:
            return []
        return words
    except (OSError, ValueError, KeyError, TypeError):
        return []


def test_runner(argv: list[str], cwd: Path) -> str:
    name = Path(argv[0]).name
    if name in {"pytest", "py.test"} or python_module(argv) == "pytest":
        return "pytest"
    if name == "vitest":
        return "vitest"
    if name == "playwright" and argv[1:2] == ["test"]:
        return "playwright"
    if name == "npm":
        words = _npm_script(argv, cwd)
        return test_runner(words, cwd) if words else ""
    return ""


def _options(args, values=(), booleans=()):
    """Extract only instrumentation options, retaining every other token in order."""
    remaining, found = [], {}
    index = 0
    while index < len(args):
        token = args[index]
        name, equals, value = token.partition("=")
        if "-p" in values and token.startswith("-p") and len(token) > 2 and not token.startswith("-p="):
            name, equals, value = "-p", "=", token[2:]
        index += 1
        if name in values:
            if not equals:
                if index == len(args) or args[index].startswith("--"):
                    raise VerificationPreparationError(f"Missing value for test option {name}")
                value = args[index]
                index += 1
        elif name in booleans:
            if name.startswith("--no-") and equals:
                raise VerificationPreparationError(f"Negated test option {name} does not accept a value")
            if not equals:
                value = "true"
                if not name.startswith("--no-") and index < len(args) and args[index] in {"true", "false"}:
                    value = args[index]
                    index += 1
            if value not in {"true", "false"}:
                raise VerificationPreparationError(f"Invalid boolean test option {name}")
        else:
            remaining.append(token)
            continue
        found.setdefault(name, []).append(value)
    return remaining, found


def _unique(values):
    return list(dict.fromkeys(values))


def _vitest_options(args, script, report):
    values = ("--reporter", "--reporters", "--outputFile", "--outputFile.junit")
    booleans = ("--run", "--no-run", "--watch", "-w", "--no-watch")
    kept, options = _options(args, values, booleans)
    _, fixed = _options(script, values, booleans)
    if fixed.get("--outputFile") or fixed.get("--outputFile.junit"):
        raise VerificationPreparationError(
            "Vitest npm script fixes a report output path; aa2 owns JUnit output. "
            "Use a direct Vitest command or remove the output option from the script through review.")
    for source in (fixed, options):
        if ("false" in source.get("--run", []) or "true" in source.get("--no-run", [])
                or "true" in source.get("--watch", []) or "true" in source.get("-w", [])
                or "false" in source.get("--no-watch", [])):
            raise VerificationPreparationError("Verification requires a non-watching Vitest invocation; conflicting run/watch options")
    fixed_run = fixed.get("--run", [])
    fixed_watch = fixed.get("--watch", []) + fixed.get("-w", []) + fixed.get("--no-watch", [])
    if len(fixed_run) > 1 or len(fixed_watch) > 1:
        raise VerificationPreparationError("Vitest npm script repeats a singleton run/watch option")
    combined = [*script, *args]
    if combined[:1] in (["watch"], ["dev"], ["bench"], ["list"]):
        raise VerificationPreparationError("Unsupported Vitest subcommand for test-case verification")
    # A script's run subcommand or boolean already establishes batch mode. Collapse
    # forwarded equivalents so npm lifecycle hooks and the original script survive.
    if not fixed_run and not fixed_watch and combined[:1] != ["run"]:
        kept.append("--run")
    reporters = _unique(fixed.get("--reporter", []) + fixed.get("--reporters", []))
    requested = options.get("--reporter", []) + options.get("--reporters", [])
    for reporter in _unique([*requested, *([] if reporters or requested else ["default"]), "junit"]):
        if reporter not in reporters:
            kept.append("--reporter=" + reporter)
    # A per-reporter path cannot be overwritten by an accompanying JSON reporter.
    kept.append(f"--outputFile.junit={report}")
    return kept


def _pytest_options(args, report):
    kept, options = _options(args, ("--junitxml", "--junit-xml", "-p"))
    plugins = options.get("-p", [])
    if "no:junitxml" in plugins or "no:_pytest.junitxml" in plugins:
        raise VerificationPreparationError("pytest JUnit plugin is disabled; controller-owned test evidence is required")
    for plugin in _unique(plugins):
        kept.extend(["-p", plugin])
    cache_options = {"--lf", "--last-failed", "--ff", "--failed-first", "--nf", "--new-first", "--cache-show", "--cache-clear", "--sw", "--stepwise"}
    if not {"cacheprovider", "no:cacheprovider"}.intersection(plugins) and not cache_options.intersection(kept):
        kept.extend(["-p", "no:cacheprovider"])
    kept.append(f"--junitxml={report}")
    return kept


def _playwright_options(args, script, report):
    kept, options = _options(args, ("--reporter", "--output"))
    _, fixed = _options(script, ("--reporter", "--output"))
    reporters = _unique([part for value in fixed.get("--reporter", []) + options.get("--reporter", [])
                         for part in value.split(",")])
    reporters = _unique([*(reporters or ["line"]), "junit"])
    # Playwright/Commander defines scalar options as last-value-wins; unlike
    # Vitest, a script's reporter/output option can be overridden by forwarded args.
    return [*kept, "--reporter=" + ",".join(reporters), f"--output={report.parent / 'playwright-results'}"]


def report_command(argv: list[str], runner: str, report: Path, *, cwd: Path | None = None) -> tuple[list[str], dict[str, str]]:
    if not runner:
        return list(argv), {}
    result = list(argv)
    script = []
    if Path(argv[0]).name == "npm":
        if cwd is None or not (script := _npm_script(argv, cwd)):
            raise VerificationPreparationError("Cannot inspect the npm test script before report instrumentation")
        script = script[1:]
        if "--" in script:
            raise VerificationPreparationError("An npm test script ends option parsing with --; use a direct runner command")
        if "--" not in result:
            result.append("--")
        start = result.index("--") + 1
    else:
        start = 1
        if runner == "pytest" and is_python(argv[0]):
            _, start = _python_module_target(argv)
    boundary = result.index("--", start) if "--" in result[start:] else len(result)
    args = result[start:boundary]
    variables = {}
    if runner == "pytest":
        args = _pytest_options(args, report)
    elif runner == "vitest":
        args = _vitest_options(args, script, report)
    elif runner == "playwright":
        args = _playwright_options(args, script, report)
        variables = {"PLAYWRIGHT_JUNIT_OUTPUT_FILE": str(report),
                     "PLAYWRIGHT_JUNIT_INCLUDE_PROJECT_IN_TEST_NAME": "1"}
    result[start:boundary] = args
    return result, variables


@dataclass
class TestCases:
    ids: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    skipped: int = 0
    failures: bool = False
    passed_count: int = 0


def read_junit(raw: bytes) -> TestCases:
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("JUnit report exceeds 32 MiB")
    root = ET.fromstring(raw)
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Expected a JUnit testsuite or testsuites")
    result = TestCases()
    suites = list(root.iter("testsuite"))
    for suite in [root, *suites]:
        if "tests" in suite.attrib and int(suite.get("tests")) != len(list(suite.iter("testcase"))):
            raise ValueError("JUnit testcase count does not match its declared total")
        # Some runners report suite/collection errors without a failing testcase.
        for key in ("failures", "errors"):
            count = int(suite.get(key, "0"))
            if count < 0:
                raise ValueError("Negative JUnit failure count")
            result.failures |= count > 0
        if suite.find("error") is not None or suite.find("failure") is not None:
            result.failures = True
    groups = defaultdict(list)
    for case in root.iter("testcase"):
        if not case.get("name"):
            raise ValueError("JUnit testcase has no name")
        name = case.get("classname", "") + "::" + case.get("name")
        failed = any(case.find(k) is not None for k in ("failure", "error"))
        status = case.get("status", "")
        if status not in {"", "run", "passed", "success", "completed", "notrun", "disabled", "skipped"}:
            raise ValueError("Unsupported JUnit testcase status")
        skipped = case.find("skipped") is not None or status in {"notrun", "disabled", "skipped"}
        result.failures |= failed
        result.skipped += int(skipped)
        success = not failed and not skipped
        result.passed_count += int(success)
        groups[name].append(success)
    totals_declared = "tests" in root.attrib or bool(suites) and all("tests" in s.attrib for s in suites)
    occurrences = Counter()
    for case in root.iter("testcase"):
        name = case.get("classname", "") + "::" + case.get("name")
        group = groups[name]
        occurrences[name] += 1
        if len(group) > 1:
            if not totals_declared:
                raise ValueError("Repeated JUnit names require declared testcase counts")
            # JUnit may omit parameter identities. Count real rows, but credit this
            # ambiguous group only if every member passes; reordering cannot turn
            # an old failed member into a gain. Cardinality changes change all IDs.
            name = f"junit-duplicate/{digest(name)}/{occurrences[name]}-of-{len(group)}"
        result.ids.append(name)
        if all(group):
            result.passed.append(name)
    if not result.ids:
        raise ValueError("JUnit report contains no testcases")
    return result
