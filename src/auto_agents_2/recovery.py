"""Recovery facts and controller policy. A model recommendation cannot advance a gate."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .errors import AA2Error
from .evidence_refs import controller_context_catalog, supplied_references
from .models import Command, Record, RecoveryRequest
from .util import digest

Action = Literal["diagnose", "recheck", "repair", "plan_patch", "clarify", "provider_research", "prototype", "design", "readme", "blocked"]
COLLECTION_FAILURES = {"test_report_missing", "test_report_invalid", "incomplete_log_capture",
                       "source_changed", "execution_inputs_changed", "test_attachments_invalid", "evidence_collection_failed", "unsupported_test_runner"}


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
    version: Literal[1, 2] = 2
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
    reason_code: str = ""
    phase: str = "recovery"
    observation_id: str = ""

    @classmethod
    def create(cls, *, kind, component, origin_id, step, obligations, **kwargs):
        # No model prose, current plan revision, task name or fluctuating output.
        identity = digest([origin_id, step, sorted(obligations)])
        kwargs.setdefault("reason_code", kind + "/" + step)
        kwargs.setdefault("phase", "task" if kwargs.get("task_id") else
                          "runtime" if step.startswith("runtime") else
                          "reuse" if step.startswith("reuse") else
                          "reintegration" if step.startswith("reintegration") else
                          "delivery" if origin_id == "delivery" else "final" if origin_id == "final" else "generation")
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
    rows = check_rows(failure.evidence)
    return [{"argv": row.get("argv"), "process_exit_code": row.get("process_exit_code"),
             "exit_code": row.get("exit_code"), "report_status": row.get("report_status"),
             "unpassed_test_ids": [case for case in row.get("test_ids", []) if case not in row.get("passed_test_ids", [])],
             "failure_reasons": row.get("failure_reasons", [])}
            for row in rows if row.get("exit_code") or row.get("failure_reasons")]


def check_rows(evidence):
    """Current checks have one meaning in task, legacy and delivery envelopes."""
    if not isinstance(evidence, dict):
        return []
    if "checks" in evidence:
        return evidence["checks"] or []
    for name in ("runtime_failure", "feedback"):
        nested = evidence.get(name)
        if isinstance(nested, dict) and "checks" in nested:
            return nested["checks"] or []
    return []


def check_kind(rows, *, default="checks"):
    """Classify observed failures without manufacturing CheckResult proof for legacy errors."""
    reasons = {reason for row in rows for reason in row.get("failure_reasons", [])}
    if "build_outputs_missing_or_invalid" in reasons:
        return "build"
    if reasons & COLLECTION_FAILURES:
        return "evidence"
    if any(row.get("exit_code") or row.get("process_exit_code") or row.get("timed_out") or row.get("failure_reasons") for row in rows):
        return "checks"
    return default


def observation_identity(checks, review=None, environment_error=None):
    """Distinguish current failures without timestamps, log prose or new budget IDs."""
    return digest({"checks": [{k: row.get(k) for k in (
        "cwd", "argv", "source_hash", "environment_hash", "command_hash", "input_hash", "checker_version",
        "process_exit_code", "failure_reasons", "test_ids", "passed_test_ids", "capture_policy_hash")} for row in checks],
        "review": review, "environment_error": environment_error})


def evidence_references(failure, project=None, *, context=None):
    """Index supplied controller data, never endorse a model's requested citations."""
    refs = set(evidence_aliases(failure, project)) | supplied_references(failure.evidence, prefixes=("", "failure.evidence."))
    if context is not None:
        refs.update(supplied_references(context))
    return sorted(refs)


def evidence_aliases(failure, project=None):
    """Persist only explicit aliases; derive structural paths when validating citations.

    Existing records may contain an expanded legacy catalog. Keep those aliases
    intact so saved requests, permits and citations retain their original meaning.
    """
    return sorted(set(failure.evidence_refs) | set((project or {}).get("inventory", [])))


def implemented_candidate(saved, accepted=False):
    return bool(accepted or saved.get("commit") or saved.get("phase") in {"applied", "tested"}
                or saved.get("checks") or saved.get("feedback", {}).get("checks"))


def check_failure(commands, results, *, task=None, step="checks", evidence_ref=""):
    kind = check_kind([result.model_dump() for result in results])
    return FailureRecord.create(
        kind=kind, component="verification", origin_id=(task.origin_id or task.id) if task else "final",
        task_id=task.id if task else "", step=step,
        obligations=task.requirement_ids if task else [digest([c.cwd, c.argv]) for c in commands],
        evidence_refs=[evidence_ref] if evidence_ref else [],
        evidence={"commands": [c.model_dump() for c in commands], "checks": [r.model_dump() for r in results]})


def adjudicate(failure: FailureRecord, *, attempts=0, diagnosis=None, plan=None, evidence_context=None):
    """Facts first; ambiguous or repeated collection failures require independent diagnosis."""
    action, reason = "diagnose", "需要独立诊断故障归属和最小修复范围"
    tasks = [failure.task_id] if failure.task_id else []
    refs = failure.evidence_refs
    rows = current_failures(failure)
    collection_failure = any(COLLECTION_FAILURES.intersection(row["failure_reasons"]) for row in rows)
    if (failure.kind in {"evidence", "build"} or collection_failure) and attempts == 0:
        action, reason = "recheck", "重新准备声明的构建产物并采集原始检查证据"
    if any("evidence_collection_failed" in row["failure_reasons"] for row in rows):
        return RecoveryDecision(failure_id=failure.id, action="blocked", task_ids=tasks, evidence_refs=refs,
            reason="附件证据采集失败；调整采集上限或测试录制配置后复验，保留候选代码和原始现场", budget_owner=failure.origin_id)
    if diagnosis:
        if len({target.task_id for target in diagnosis.repair_targets}) != len(diagnosis.repair_targets):
            raise AA2Error("Duplicate repair-target task IDs")
        catalog = controller_context_catalog(evidence_context or {"failure": failure.model_dump()},
                                             aliases=evidence_references(failure))
        supplied = catalog.controller
        known_refs = supplied | catalog.proposals
        resolved = [catalog.resolve(ref) for ref in diagnosis.evidence_refs]
        missing = [i for i, ref in enumerate(resolved) if ref is None]
        if missing:
            raise AA2Error("Diagnosis cites evidence not supplied by the controller; invalid evidence_refs indexes: "
                + ", ".join(map(str, missing)) + ". Cite an existing controller-context path, supplied artifact hash "
                "or failure.evidence_refs entry. Existing proposal paths describe unverified suggestions only.")
        if not supplied.intersection(resolved):
            raise AA2Error("Diagnosis requires controller evidence; unverified proposals cannot establish results")
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
            target_refs = {catalog.resolve(ref) for ref in target.evidence_refs}
            if (not target_refs <= known_refs or not supplied.intersection(target_refs)
                    or not all(path in evidence_references(failure) and path_allowed(path, known[task_id].allowed_paths)
                               for path in target.paths)):
                raise AA2Error("Repair target is outside the evidenced existing task scope; amend the plan if necessary")
        if diagnosis.action == "repair" and set(diagnosis.task_ids) - {failure.task_id} and failure.origin_id != "delivery":
            if set(targets) != set(diagnosis.task_ids):
                raise AA2Error("Cross-task repair requires evidence and owned paths for each existing task")
        action = diagnosis.action
        tasks, refs, reason = diagnosis.task_ids, diagnosis.evidence_refs, diagnosis.reason
    origins = {t.id: t.origin_id or t.id for t in plan.tasks} if plan else {}
    owners = sorted({origins.get(t, failure.origin_id) for t in tasks}) if action == "repair" and tasks else [failure.origin_id]
    return RecoveryDecision(failure_id=failure.id, action=action, task_ids=tasks,
                            evidence_refs=refs, reason=reason, budget_owner=failure.origin_id,
                            budget_owners=owners,
                            repair_targets=diagnosis.repair_targets if diagnosis and action == "repair" else [])


def failure_summary(checks, *, review=None, environment_error=None, diagnosis_feedback=None, budget=None,
                    candidate_error=None, validation_error=None):
    """Describe the current blocker, including a rejected review with green checks."""
    lines = []
    if candidate_error:
        lines.append("候选补丁未应用：" + str(candidate_error))
    if validation_error:
        lines.append("恢复提案未通过校验：" + str(validation_error))
    if not checks:
        lines.append("当前没有已执行的检查记录；不能据此判断检查通过或失败。")
    for row in checks:
        reasons = row.get("failure_reasons", [])
        state = ("尚未执行（前置检查阻塞）" if row.get("report_status") == "not_executed" or {"not_executed", "prerequisite_failed"}.intersection(reasons) else
                 "证据无效或采集不完整" if row.get("process_exit_code", row.get("exit_code")) is None else
                 "证据无效或采集不完整" if COLLECTION_FAILURES.intersection(reasons) else
                 "检查失败" if row.get("exit_code") or reasons or row.get("timed_out") else "检查通过")
        failed = [c for c in row.get("test_ids", []) if c not in row.get("passed_test_ids", [])]
        lines.append(f"{row.get('cwd', '.')}：{' '.join(row.get('argv', []))}；"
                     f"{state}；退出码 {row.get('process_exit_code', row.get('exit_code'))}"
                     + (f"；通过 {len(row.get('passed_test_ids', []))}/{len(row['test_ids'])}" if row.get("test_ids") else ""))
        lines.extend("  " + name for name in failed[:6])
        if reasons:
            lines.append("  " + ", ".join(reasons))
        if row.get("report") and (failed or reasons):
            lines.append("  报告：" + row["report"]["hash"])
    if review and not review.get("approved"):
        lines.append("独立审查未通过：" + review.get("summary", "仍有待解决的问题"))
        lines.extend(f"  {f.get('location', '')}：{f.get('description', '')}" for f in review.get("findings", [])[:6])
    if environment_error:
        lines.append("环境准备失败：" + str(environment_error))
    if diagnosis_feedback:
        lines.append("上一次诊断结果未通过格式/范围校验：" + diagnosis_feedback.get("validation_error", "未知校验错误"))
    if budget:
        lines.append("额度阻塞：" + str(budget))
    return "\n".join(lines)
