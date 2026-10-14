"""Test-runner discovery and controller-owned JUnit reports."""
from __future__ import annotations

import json
import re
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


def is_python(executable: str) -> bool:
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(executable).name))


def python_module(argv: list[str]) -> str:
    """Read interpreter options only; never mistake script/-c arguments for -m."""
    if not argv or not is_python(argv[0]):
        return ""
    index = 1
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg == "--check-hash-based-pycs":
            index += 1
            continue
        if not arg.startswith("-") or arg in {"-", "--"} or arg.startswith("--"):
            return ""
        flags = arg[1:]
        while flags:
            flag, flags = flags[0], flags[1:]
            if flag == "m":
                return flags or (argv[index] if index < len(argv) else "")
            if flag in "chV?" or flag not in "bBdEiIOPqRsSuvxWX":
                return ""
            if flag in "WX":
                if not flags:
                    index += 1
                break
    return ""


def test_runner(argv: list[str], cwd: Path) -> str:
    name = Path(argv[0]).name
    if name in {"pytest", "py.test"} or python_module(argv) == "pytest":
        return "pytest"
    if name == "vitest":
        return "vitest"
    if name == "playwright" and argv[1:2] == ["test"]:
        return "playwright"
    if name != "npm":
        return ""
    if argv[1:2] in (["test"], ["t"]):
        script = "test"
    elif argv[1:2] in (["run"], ["run-script"]) and len(argv) > 2:
        script = argv[2]
    else:
        return ""
    try:
        body = json.loads((cwd / "package.json").read_text())["scripts"][script]
        # Complex shell wrappers need an explicit direct runner command. Do not infer
        # a runner from a word in arbitrary shell text or forward options to a chain.
        if not isinstance(body, str) or any(c in body for c in ";&|<>`$\n"):
            return ""
        words = shlex.split(body)
        if not words or Path(words[0]).name not in {"vitest", "playwright"}:
            return ""
        return test_runner(words, cwd)
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def report_command(argv: list[str], runner: str, report: Path) -> tuple[list[str], dict[str, str]]:
    variables = {}
    if runner == "pytest":
        options = [f"--junitxml={report}", "-p", "no:cacheprovider"]
    elif runner == "vitest":
        options = ["--run", "--reporter=default", "--reporter=junit", f"--outputFile={report}"]
    elif runner == "playwright":
        # Playwright removes/recreates its output root. Put it below the writable
        # scratch mount instead of asking it to remove a read-only mount point.
        options = ["--reporter=line,junit", f"--output={report.parent / 'playwright-results'}"]
        variables = {"PLAYWRIGHT_JUNIT_OUTPUT_FILE": str(report),
                     "PLAYWRIGHT_JUNIT_INCLUDE_PROJECT_IN_TEST_NAME": "1"}
    else:
        return list(argv), variables
    result = list(argv)
    if Path(argv[0]).name == "npm":
        if "--" not in result:
            result.append("--")
        # npm consumes its first --; a subsequent -- belongs to the test runner.
        start = result.index("--") + 1
    else:
        start = 1
    boundary = result.index("--", start) if "--" in result[start:] else len(result)
    result[boundary:boundary] = options
    return result, variables


@dataclass
class TestCases:
    ids: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    skipped: int = 0
    failures: bool = False


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
    identities = set()
    for case in root.iter("testcase"):
        if not case.get("name"):
            raise ValueError("JUnit testcase has no name")
        name = case.get("classname", "") + "::" + case.get("name")
        if name in identities:
            raise ValueError("Duplicate JUnit testcase identity")
        identities.add(name)
        result.ids.append(name)
        failed = any(case.find(k) is not None for k in ("failure", "error"))
        status = case.get("status", "")
        if status not in {"", "run", "passed", "success", "completed", "notrun", "disabled", "skipped"}:
            raise ValueError("Unsupported JUnit testcase status")
        skipped = case.find("skipped") is not None or status in {"notrun", "disabled", "skipped"}
        result.failures |= failed
        result.skipped += int(skipped)
        if not failed and not skipped:
            result.passed.append(name)
    if not result.ids:
        raise ValueError("JUnit report contains no testcases")
    return result
