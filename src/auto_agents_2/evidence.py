"""Central reuse policy: an input match permits reuse only of complete current evidence."""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .builds import generated_entry, output_hashes, verification_commands
from .errors import AA2Error
from .util import contained, digest
from .workspace import input_entry, tree_hash

CHECKER_VERSION = "verification-v13"
NODE_RUNTIME_CACHES = {".cache", ".vite", ".vite-temp", ".tmp"}
RUNTIME_CACHES = {"__pycache__", ".pytest_cache", *NODE_RUNTIME_CACHES}


def input_identity(workspace, command):
    dependencies = []
    seen = set()
    def visit(directory):
        resolved = directory.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        modules = directory / "node_modules"
        if modules.exists() and any(p.name not in RUNTIME_CACHES for p in modules.iterdir()):
            dependencies.append((directory.relative_to(workspace).as_posix(), modules))
        for child in sorted(directory.iterdir()):
            if child.name not in {"node_modules", ".git", ".aa2", ".venv", ".conda-env", *RUNTIME_CACHES} and child.is_dir():
                visit(child)
    visit(workspace)
    # Explicit inputs (including ancestors/aliases of node_modules) and automatic
    # dependency discovery must describe the same immutable content. Only cache
    # roots are mutable; package source with a cache-like name is still hashed.
    omitted = {base / name for _, modules in dependencies for base in (modules, modules.resolve())
               for name in RUNTIME_CACHES}
    entries = [(p, generated_entry(contained(workspace, p), workspace, omitted=omitted)) for p in command.inputs]
    entries.extend((name, input_entry(modules, workspace, excluded=set(), omitted=omitted))
                   for name, modules in dependencies)
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
class EvidenceIssue:
    code: str
    command_index: int
    cwd: str
    argv: tuple[str, ...]
    category: str

    def record(self):
        return {"code": self.code, "command_index": self.command_index, "cwd": self.cwd,
                "argv": list(self.argv), "category": self.category}


@dataclass(frozen=True)
class ReuseDecision:
    allowed: bool
    reason: str
    issues: tuple[EvidenceIssue, ...] = ()

    def record(self):
        return {"allowed": self.allowed, "reason": self.reason, "issues": [issue.record() for issue in self.issues]}


@dataclass(frozen=True)
class EvidenceAssessment:
    valid: bool
    passed: bool
    reason: str
    code: str = "verified"

    @property
    def verdict(self):
        return "unknown" if not self.valid else "passed" if self.passed else "failed"


def assess_observation(command, result, workspace, environment, artifacts):
    """A trustworthy failing observation may close individual passing obligations."""
    invalid = {"execution_inputs_changed", "source_changed", "incomplete_log_capture",
               "test_report_missing", "test_report_invalid", "test_attachments_invalid", "evidence_collection_failed", "unsupported_test_runner"}
    try:
        return _assess_observation(command, result, workspace, environment, artifacts, invalid)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, AA2Error):
        return EvidenceAssessment(False, False, "evidence artifact or execution identity unavailable or changed", "evidence_unavailable")


def _assess_observation(command, result, workspace, environment, artifacts, invalid):
    if artifacts is None or not workspace.is_dir():
        return EvidenceAssessment(False, False, "artifact verification unavailable", "evidence_unavailable")
    identities = (
        (result.source_hash, lambda: tree_hash(workspace), "verified source changed", "source_changed"),
        (result.checker_version, lambda: CHECKER_VERSION, "verification policy changed", "policy_changed"),
        (result.environment_hash, lambda: environment.fingerprint if environment else "host", "verification environment changed", "environment_changed"),
        (result.command_hash, lambda: digest(command.model_dump()), "verification command changed", "command_changed"),
        (result.input_hash, lambda: input_identity(workspace, command), "declared inputs or installed dependencies changed", "inputs_changed"),
        (result.runtime_config_hash, lambda: runtime_configuration(workspace, command, environment), "execution environment configuration changed", "runtime_configuration_changed"),
    )
    for previous, current, reason, code in identities:
        if previous != current():
            return EvidenceAssessment(False, False, reason, code)
    if result.process_exit_code is None or result.timed_out or invalid.intersection(result.failure_reasons):
        code = ("source_changed" if "source_changed" in result.failure_reasons else
                "execution_timeout" if result.timed_out else
                "inputs_changed" if "execution_inputs_changed" in result.failure_reasons else
                "evidence_collection_failed" if "evidence_collection_failed" in result.failure_reasons else "execution_incomplete")
        return EvidenceAssessment(False, False, "complete execution evidence unavailable", code)
    from .collection import policy_identity
    if ((result.capture_policy_hash or result.test_runner == "playwright")
            and result.capture_policy_hash != policy_identity(artifacts.store.root)):
        return EvidenceAssessment(False, False, "attachment capture policy changed", "capture_policy_changed")
    if environment and environment.resolution and result.environment_resolution != environment.resolution:
        return EvidenceAssessment(False, False, "environment resolution evidence changed or missing", "environment_resolution_changed")
    if result.environment_resolution:
        resolution = json.loads(artifacts.bytes(result.environment_resolution))
        if (not isinstance(resolution, dict) or resolution.get("version") != 1
                or resolution.get("environment_hash") != result.environment_hash):
            return EvidenceAssessment(False, False, "resolution belongs to another environment", "environment_resolution_changed")
    report_failed = False
    for ref in result.logs:
        if not ref.get("capture_complete", True):
            return EvidenceAssessment(False, False, "incomplete log capture", "log_incomplete")
        if not artifacts.released(ref):
            artifacts.bytes(ref)
    if result.test_runner:
        from .test_reports import read_junit
        if result.report_status != "valid" or not result.report:
            return EvidenceAssessment(False, False, "machine report unavailable", "report_unavailable")
        cases = read_junit(artifacts.bytes(result.report))
        report_failed = cases.failures
        if (cases.ids != result.test_ids or cases.passed != result.passed_test_ids
                or cases.passed_count != result.tests or cases.skipped != result.skipped):
            return EvidenceAssessment(False, False, "machine report differs from observation", "report_mismatch")
    for attachment in result.attachments:
        if not artifacts.released(attachment["artifact"]):
            artifacts.bytes(attachment["artifact"])
    success = (not report_failed and not result.exit_code and not result.process_exit_code and not result.failure_reasons
               and result.tests >= max(command.expected_tests, int(bool(result.test_runner))))
    if success:
        try:
            outputs = output_hashes(command, workspace)
        except (OSError, ValueError, AA2Error):
            return EvidenceAssessment(False, False, "generated build outputs unavailable", "outputs_unavailable")
        if result.generated_outputs != outputs:
            return EvidenceAssessment(False, False, "generated build outputs changed", "outputs_changed")
    return EvidenceAssessment(True, bool(success), "verified observation", "verified" if success else "check_failed")


def reusable(commands, results, workspace: Path, environment, artifacts):
    try:
        commands = verification_commands(commands, workspace)
    except (OSError, ValueError, AA2Error):
        return ReuseDecision(False, "verification contract unavailable",
                             (EvidenceIssue("contract_unavailable", -1, ".", (), "contract"),))
    if len(commands) != len(results) or not results:
        return ReuseDecision(False, "missing command evidence",
                             (EvidenceIssue("missing_observation", -1, ".", (), "evidence"),))
    issues, reasons = [], []
    for index, (command, result) in enumerate(zip(commands, results)):
        assessment = assess_observation(command, result, workspace, environment, artifacts)
        if not assessment.valid or not assessment.passed:
            reason = assessment.reason if not assessment.valid else "unsuccessful observation"
            reasons.append(f"{command.cwd}: {' '.join(command.argv)}: {reason}")
            category = "state_conflict" if assessment.code == "source_changed" else "checks" if assessment.valid else "evidence"
            issues.append(EvidenceIssue(assessment.code, index, command.cwd, tuple(command.argv), category))
    if issues:
        return ReuseDecision(False, "; ".join(reasons), tuple(issues))
    return ReuseDecision(True, "complete matching controller evidence")


def invocation_identity(stage, context, schema, workspace, config, source_hash=None, execution=None):
    from .prompts import INVOCATION_CONTRACT_VERSION
    from .readview import VIEW_VERSION
    settings = config.model_dump(mode="json")
    if stage not in {"decision", "recovery_diagnosis"}:
        # Keep existing v4 worker receipts valid when only these defaults are added.
        settings.pop("automation", None)
        settings["efforts"].pop("decision", None)
        settings["efforts"].pop("recovery_diagnosis", None)
    return digest(["invocation-v4", INVOCATION_CONTRACT_VERSION, VIEW_VERSION, stage, context, schema.model_json_schema(),
                   source_hash or tree_hash(workspace), settings, execution])
