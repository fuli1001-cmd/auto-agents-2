"""Recovery facts and controller policy. A model recommendation cannot advance a gate."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from .errors import AA2Error
from .models import Command, Record, RecoveryRequest
from .util import digest

Action = Literal["diagnose", "recheck", "repair", "plan_patch", "clarify", "provider_research", "prototype", "design", "blocked"]
COLLECTION_FAILURES = {"test_report_missing", "test_report_invalid", "incomplete_log_capture",
                       "source_changed", "execution_inputs_changed", "test_attachments_invalid", "unsupported_test_runner"}


class ExecutionContract(Record):
    version: Literal[1] = 1
    plan_revision: int = 1
    task_revision: int = 1
    task_id: str = ""
    origin_id: str = ""
    allowed_paths: list[str] = []
    approval: dict = {}
    commands: list[Command] = []


class FailureRecord(Record):
    version: Literal[1] = 1
    id: str
    kind: Literal["proposal", "checks", "evidence", "build", "environment", "review", "integration", "contract"]
    component: str
    origin_id: str
    task_id: str = ""
    step: str
    obligations: list[str]
    evidence_refs: list[str] = []
    evidence: dict = {}
    proposal: dict = {}

    @classmethod
    def create(cls, *, kind, component, origin_id, step, obligations, **kwargs):
        # No model prose, current plan revision, task name or fluctuating output.
        identity = digest([origin_id, step, sorted(obligations)])
        return cls(id=identity, kind=kind, component=component, origin_id=origin_id,
                   step=step, obligations=obligations, **kwargs)


class RepairTarget(Record):
    task_id: str
    paths: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    reason: str


class RecoveryDecision(Record):
    version: Literal[1] = 1
    failure_id: str
    action: Action
    task_ids: list[str] = []
    evidence_refs: list[str] = []
    reason: str
    budget_owner: str
    budget_owners: list[str] = []
    repair_targets: list[RepairTarget] = []


class Diagnosis(Record):
    action: Action
    task_ids: list[str] = []
    evidence_refs: list[str] = Field(min_length=1)
    reason: str
    repair_targets: list[RepairTarget] = []


class RecoveryNeeded(AA2Error):
    def __init__(self, failure: FailureRecord):
        self.failure = failure
        super().__init__(f"{failure.component}: {failure.kind} ({failure.id[:12]})")


def request_from(proposal):
    if proposal.recovery_request:
        return proposal.recovery_request
    if proposal.replan_stage:
        return RecoveryRequest(reason=proposal.replan_reason, suggested_action=(
            "plan_patch" if proposal.replan_stage == "plan" else proposal.replan_stage))
    return None


def current_failures(failure):
    rows = failure.evidence.get("checks", failure.evidence.get("feedback", {}).get("checks", []))
    return [{"argv": row.get("argv"), "process_exit_code": row.get("process_exit_code"),
             "exit_code": row.get("exit_code"), "report_status": row.get("report_status"),
             "unpassed_test_ids": [case for case in row.get("test_ids", []) if case not in row.get("passed_test_ids", [])],
             "failure_reasons": row.get("failure_reasons", [])}
            for row in rows if row.get("exit_code") or row.get("failure_reasons")]


def observation_identity(checks, review=None, environment_error=None):
    """Distinguish current failures without timestamps, log prose or new budget IDs."""
    return digest({"checks": [{k: row.get(k) for k in (
        "cwd", "argv", "source_hash", "environment_hash", "command_hash", "input_hash", "checker_version",
        "process_exit_code", "failure_reasons", "test_ids", "passed_test_ids")} for row in checks],
        "review": review, "environment_error": environment_error})


def evidence_references(failure, project=None):
    """Index supplied controller data, never endorse a model's requested citations."""
    refs = set(failure.evidence_refs)

    def visit(value, path):
        if path:
            refs.update((path, "failure.evidence." + path))
        if isinstance(value, dict):
            if isinstance(value.get("hash"), str) and re.fullmatch(r"[a-f0-9]{64}", value["hash"]):
                refs.add(value["hash"])
            for key, item in value.items():
                if key not in {"proposal", "recovery_request", "original_proposal"}:
                    visit(item, path + "." + key if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(failure.evidence, "")
    refs.update((project or {}).get("inventory", []))
    return sorted(refs)


def implemented_candidate(saved, accepted=False):
    return bool(accepted or saved.get("commit") or saved.get("phase") in {"applied", "tested"}
                or saved.get("checks") or saved.get("feedback", {}).get("checks"))


def check_failure(commands, results, *, task=None, step="checks", evidence_ref=""):
    failed = [r for r in results if r.exit_code or r.failure_reasons or r.timed_out]
    reasons = {reason for r in failed for reason in r.failure_reasons}
    kind = "checks"
    if reasons & COLLECTION_FAILURES:
        kind = "evidence"
    if reasons & {"build_outputs_missing_or_invalid"}:
        kind = "build"
    return FailureRecord.create(
        kind=kind, component="verification", origin_id=(task.origin_id or task.id) if task else "final",
        task_id=task.id if task else "", step=step,
        obligations=task.requirement_ids if task else [digest([c.cwd, c.argv]) for c in commands],
        evidence_refs=[evidence_ref] if evidence_ref else [],
        evidence={"commands": [c.model_dump() for c in commands], "checks": [r.model_dump() for r in results]})


def adjudicate(failure: FailureRecord, *, attempts=0, diagnosis=None, plan=None):
    """Facts first; ambiguous or repeated collection failures require independent diagnosis."""
    action, reason = "diagnose", "需要独立诊断故障归属和最小修复范围"
    tasks = [failure.task_id] if failure.task_id else []
    refs = failure.evidence_refs
    rows = current_failures(failure)
    collection_failure = any(COLLECTION_FAILURES.intersection(row["failure_reasons"]) for row in rows)
    if (failure.kind in {"evidence", "build"} or collection_failure) and attempts == 0:
        action, reason = "recheck", "重新准备声明的构建产物并采集原始检查证据"
    if diagnosis:
        if len({target.task_id for target in diagnosis.repair_targets}) != len(diagnosis.repair_targets):
            raise AA2Error("Duplicate repair-target task IDs")
        if not set(diagnosis.evidence_refs) <= set(evidence_references(failure)):
            raise AA2Error("Diagnosis cites evidence not supplied by the controller")
        known = {t.id: t for t in plan.tasks} if plan else {}
        if not set(diagnosis.task_ids) <= known.keys():
            raise AA2Error("Diagnosis names unknown tasks")
        if diagnosis.action == "repair" and not diagnosis.task_ids and failure.origin_id != "delivery":
            raise AA2Error("A repair diagnosis must identify existing task scopes")
        from .workspace import path_allowed
        targets = {target.task_id: target for target in diagnosis.repair_targets}
        if failure.origin_id == "delivery" and targets:
            raise AA2Error("Delivery repair does not support task repair targets")
        for task_id, target in targets.items():
            if task_id not in diagnosis.task_ids or task_id not in known:
                raise AA2Error("Repair target must belong to a named existing task")
            if (not set(target.evidence_refs) <= set(evidence_references(failure))
                    or not all(path in evidence_references(failure) and path_allowed(path, known[task_id].allowed_paths)
                               for path in target.paths)):
                raise AA2Error("Repair target is outside the evidenced existing task scope; amend the plan if necessary")
        if diagnosis.action == "repair" and set(diagnosis.task_ids) - {failure.task_id} and failure.origin_id != "delivery":
            if set(targets) != set(diagnosis.task_ids):
                raise AA2Error("Cross-task repair requires evidence and owned paths for each existing task")
        action = "blocked" if diagnosis.action == "diagnose" else diagnosis.action
        tasks, refs, reason = diagnosis.task_ids, diagnosis.evidence_refs, diagnosis.reason
    origins = {t.id: t.origin_id or t.id for t in plan.tasks} if plan else {}
    owners = sorted({origins.get(t, failure.origin_id) for t in tasks}) if action == "repair" and tasks else [failure.origin_id]
    return RecoveryDecision(failure_id=failure.id, action=action, task_ids=tasks,
                            evidence_refs=refs, reason=reason, budget_owner=failure.origin_id,
                            budget_owners=owners,
                            repair_targets=diagnosis.repair_targets if diagnosis and action == "repair" else [])


def failure_summary(checks):
    """A short actionable summary; raw reports remain in status/evidence."""
    lines = []
    for row in checks:
        if not row.get("exit_code") and not row.get("failure_reasons"):
            continue
        failed = [c for c in row.get("test_ids", []) if c not in row.get("passed_test_ids", [])]
        lines.append(f"{row.get('cwd', '.')}：{' '.join(row.get('argv', []))}；"
                     f"退出码 {row.get('process_exit_code')}；通过 {row.get('tests', 0)}/{len(row.get('test_ids', []))}")
        lines.extend("  " + name for name in failed[:6])
        if not failed:
            lines.append("  " + ", ".join(row.get("failure_reasons", [])))
        if row.get("report"):
            lines.append("  报告：" + row["report"]["hash"])
    return "\n".join(lines) or "当前尚无有效失败报告，需要重新核验。"
