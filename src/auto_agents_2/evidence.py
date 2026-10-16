"""Central reuse policy: an input match permits reuse only of complete current evidence."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .builds import generated_entry, output_hashes, verification_commands
from .errors import AA2Error
from .util import contained, digest
from .workspace import input_entry, tree_hash

CHECKER_VERSION = "verification-v10"
NODE_RUNTIME_CACHES = {".cache", ".vite", ".vite-temp", ".tmp"}
RUNTIME_CACHES = {"__pycache__", ".pytest_cache", *NODE_RUNTIME_CACHES}


def input_identity(workspace, command):
    entries = [(p, generated_entry(contained(workspace, p), workspace)) for p in command.inputs]
    seen = set()
    def visit(directory):
        resolved = directory.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        modules = directory / "node_modules"
        if modules.exists() and any(p.name not in RUNTIME_CACHES for p in modules.iterdir()):
            entries.append((directory.relative_to(workspace).as_posix(), input_entry(modules, workspace, excluded=RUNTIME_CACHES)))
        for child in sorted(directory.iterdir()):
            if child.name not in {"node_modules", ".git", ".aa2", ".venv", ".conda-env", *RUNTIME_CACHES} and child.is_dir():
                visit(child)
    visit(workspace)
    return digest(sorted(entries))



def check_environment(workspace, command, environment, scratch):
    cwd = workspace if command.cwd == "." else contained(workspace, command.cwd)
    variables = environment.variables(scratch) if environment else {
        **os.environ, "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}
    variables["PYTHONPATH"] = os.pathsep.join([str(cwd / "src"), str(cwd), str(workspace / "src"), str(workspace)])
    return variables


def runtime_configuration(workspace, command, environment):
    # Account for every inherited execution variable without storing its contents.
    # A stable placeholder represents controller-owned temporary/cache locations.
    variables = check_environment(workspace, command, environment, Path("/__aa2_temporary_input__"))
    return digest(variables)


@dataclass(frozen=True)
class ReuseDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class EvidenceAssessment:
    valid: bool
    passed: bool
    reason: str


def assess_observation(command, result, workspace, environment, artifacts):
    """A trustworthy failing observation may close individual passing obligations."""
    invalid = {"execution_inputs_changed", "source_changed", "incomplete_log_capture",
               "test_report_missing", "test_report_invalid", "test_attachments_invalid", "evidence_collection_failed", "unsupported_test_runner"}
    try:
        return _assess_observation(command, result, workspace, environment, artifacts, invalid)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, AA2Error):
        return EvidenceAssessment(False, False, "evidence artifact or execution identity unavailable or changed")


def _assess_observation(command, result, workspace, environment, artifacts, invalid):
    if artifacts is None or not workspace.is_dir():
        return EvidenceAssessment(False, False, "artifact verification unavailable")
    if (result.checker_version != CHECKER_VERSION or result.source_hash != tree_hash(workspace)
            or result.environment_hash != (environment.fingerprint if environment else "host")
            or result.command_hash != digest(command.model_dump())
            or result.input_hash != input_identity(workspace, command)
            or result.runtime_config_hash != runtime_configuration(workspace, command, environment)
            or result.process_exit_code is None or result.timed_out
            or invalid.intersection(result.failure_reasons)):
        return EvidenceAssessment(False, False, "execution identity or complete evidence unavailable")
    from .collection import policy_identity
    if ((result.capture_policy_hash or result.test_runner == "playwright")
            and result.capture_policy_hash != policy_identity(artifacts.store.root)):
        return EvidenceAssessment(False, False, "attachment capture policy changed")
    report_failed = False
    for ref in result.logs:
        if not ref.get("capture_complete", True):
            return EvidenceAssessment(False, False, "incomplete log capture")
        if not artifacts.released(ref):
            artifacts.bytes(ref)
    if result.test_runner:
        from .test_reports import read_junit
        if result.report_status != "valid" or not result.report:
            return EvidenceAssessment(False, False, "machine report unavailable")
        cases = read_junit(artifacts.bytes(result.report))
        report_failed = cases.failures
        if (cases.ids != result.test_ids or cases.passed != result.passed_test_ids
                or cases.passed_count != result.tests or cases.skipped != result.skipped):
            return EvidenceAssessment(False, False, "machine report differs from observation")
    for attachment in result.attachments:
        if not artifacts.released(attachment["artifact"]):
            artifacts.bytes(attachment["artifact"])
    success = (not report_failed and not result.exit_code and not result.process_exit_code and not result.failure_reasons
               and result.tests >= max(command.expected_tests, int(bool(result.test_runner))))
    if success and result.generated_outputs != output_hashes(command, workspace):
        return EvidenceAssessment(False, False, "generated build outputs changed")
    return EvidenceAssessment(True, bool(success), "verified observation")


def reusable(commands, results, workspace: Path, environment, artifacts):
    try:
        commands = verification_commands(commands, workspace)
    except (OSError, ValueError, AA2Error):
        return ReuseDecision(False, "verification contract unavailable")
    if len(commands) != len(results) or not results:
        return ReuseDecision(False, "missing command evidence")
    for command, result in zip(commands, results):
        assessment = assess_observation(command, result, workspace, environment, artifacts)
        if not assessment.valid or not assessment.passed:
            return ReuseDecision(False, assessment.reason if not assessment.valid else "unsuccessful observation")
    return ReuseDecision(True, "complete matching controller evidence")


def invocation_identity(stage, context, schema, workspace, config, source_hash=None, execution=None):
    from .prompts import PROMPT_VERSION
    from .readview import VIEW_VERSION
    settings = config.model_dump(mode="json")
    if stage not in {"decision", "recovery_diagnosis"}:
        # Keep existing v4 worker receipts valid when only these defaults are added.
        settings.pop("automation", None)
        settings["efforts"].pop("decision", None)
        settings["efforts"].pop("recovery_diagnosis", None)
    return digest(["invocation-v4", PROMPT_VERSION, VIEW_VERSION, stage, context, schema.model_json_schema(),
                   source_hash or tree_hash(workspace), settings, execution])
