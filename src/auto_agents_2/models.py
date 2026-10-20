from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Command(Record):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout: int = Field(default=300, ge=1, le=1800)
    expected_tests: int = Field(default=0, ge=0)
    inputs: list[str] = []
    outputs: list[str] = Field(default=[], description=(
        "Workspace-root-relative generated directories produced by this command, e.g. frontend/dist or frontend/.next. "
        "Only the command cwd's dist and .next directories are writable; declarations cannot grant source writes. "
        "Consumers must list required generated paths in inputs. Use [] for commands that do not build artifacts."))


class Change(Record):
    path: str
    operation: Literal["write", "replace", "delete"] = "write"
    content: str = ""
    old_text: str = ""


class Requirement(Record):
    id: str
    description: str
    acceptance: list[str] = Field(min_length=1)


class Coverage(Record):
    requirement_id: str
    status: Literal["satisfied", "changed", "regressed", "unknown"]
    existing_ids: list[str] = []
    evidence_paths: list[str] = []
    rationale: str


class UserQuestion(Record):
    prompt: str
    options: list[str] = []
    purpose: Literal["clarification", "prototype_approval"] = "clarification"


class IntakeResult(Record):
    summary: str
    document: str
    questions: list[str | UserQuestion] = []
    requirements: list[Requirement] = []
    coverage: list[Coverage] = []
    verification: list[Command] = []
    frontend: bool = False
    ui_changes: bool = True
    prototype_paths: list[str] = []


class SourceEvidence(Record):
    url: str
    claim: str
    version: str = ""
    validation: str = ""


class BrowserAction(Record):
    route: str = "/"
    action: Literal["click", "fill", "press", "check", "expect_visible", "expect_text"]
    selector: str = Field(min_length=1)
    value: str = ""


class DocumentResult(Record):
    summary: str
    document: str
    questions: list[str | UserQuestion] = []
    sources: list[SourceEvidence] = []
    files: list[Change] = []
    browser_actions: list[BrowserAction] = []


class TaskSpec(Record):
    id: str
    origin_id: str = ""
    title: str
    description: str
    requirement_ids: list[str] = Field(min_length=1)
    depends_on: list[str] = []
    allowed_paths: list[str] = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    checks: list[Command] = Field(min_length=1)


class PreviewSpec(Record):
    command: Command
    routes: list[str] = ["/"]
    actions: list[BrowserAction] = []

    @model_validator(mode="after")
    def action_coverage(self):
        if not self.routes or len(self.routes) != len(set(self.routes)):
            raise ValueError("Preview routes must be nonempty and unique")
        if any(action.route not in self.routes for action in self.actions):
            raise ValueError("Every preview action must name a declared route")
        return self


class PlanResult(Record):
    summary: str
    document: str
    mode: Literal["implementation", "verification_only"] = "implementation"
    tasks: list[TaskSpec]
    final_checks: list[Command] = Field(min_length=1)
    environment_file: str = Field(default="", description=(
        "Workspace-root-relative PATH to a Conda YAML file, e.g. environment.yml, or empty for discovery. "
        "Never put YAML contents here. The implementing task must create the declared file before environment preparation."))
    requirements_file: str = Field(default="", description=(
        "Workspace-root-relative PATH to a pip requirements file, e.g. requirements.txt, or empty for discovery. "
        "Never put package lists or file contents here. The implementing task must create the declared file."))
    preview: PreviewSpec | None = None
    runtime_prepare: list[Command] = []
    runtime_probes: list[Command] = []

    @model_validator(mode="after")
    def task_mode(self):
        if self.mode == "implementation" and not self.tasks:
            raise ValueError("Implementation plans require tasks")
        if self.mode == "verification_only" and self.tasks:
            raise ValueError("Verification-only plans cannot contain implementation tasks")
        return self


class RecoveryRequest(Record):
    reason: str
    suggested_action: Literal["diagnose", "recheck", "prototype_recheck", "repair", "plan_patch", "clarify", "provider_research", "prototype", "design", "readme"] = "diagnose"
    evidence_refs: list[str] = []


class ImplementationResult(Record):
    summary: str
    changes: list[Change] = []
    replan_stage: Literal["", "clarify", "provider_research", "prototype", "design", "plan"] = ""
    replan_reason: str = ""
    recovery_request: RecoveryRequest | None = None


class Finding(Record):
    severity: Literal["blocking", "major", "minor"]
    location: str
    description: str
    evidence: str
    resolution: str


class ReviewResult(Record):
    approved: bool
    summary: str
    findings: list[Finding] = []

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.approved and any(f.severity in {"blocking", "major"} for f in self.findings):
            raise ValueError("An approved review cannot contain blocking or major findings")
        return self


class CheckResult(Record):
    argv: list[str]
    cwd: str
    exit_code: int
    process_exit_code: int | None = None
    failure_reasons: list[str] = []
    test_runner: str = ""
    report_status: str = ""
    attachments: list[dict] = []
    capture_policy_hash: str = ""
    attachment_capture: dict = {}
    output: str
    duration: float
    source_hash: str
    environment_hash: str
    command_hash: str = ""
    effective_argv: list[str] = []
    checker_version: str = ""
    input_hash: str = ""
    runtime_config_hash: str = ""
    generated_outputs: dict[str, str] = {}
    logs: list[dict] = []
    tests: int = 0
    skipped: int = 0
    timed_out: bool = False
    test_ids: list[str] = []
    passed_test_ids: list[str] = []
    report: dict = {}


class Usage(Record):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    complete: bool = True


def strict_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def visit(item):
        if isinstance(item, dict):
            if item.get("type") == "object":
                if item.get("additionalProperties") not in (None, False):
                    from .errors import ConfigurationError
                    raise ConfigurationError("Strict provider schemas do not support arbitrary property maps")
                item["additionalProperties"] = False
                item["required"] = list(item.get("properties", {}))
            item.pop("default", None)
            for value in item.values():
                visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)

    visit(schema)
    return schema
