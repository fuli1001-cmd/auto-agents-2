"""Finite, independently reviewed check-to-obligation bindings; never execution authority."""
from __future__ import annotations

import json

from .builds import verification_layout
from .errors import AA2Error
from .models import CheckProductionAssessment, PlanResult
from .util import digest

PROTOCOL_KEY = "controller_check_production"
ACTIVE_KEY = "evidence_admission/active"
REQUIRED_KEY = "evidence_admission/check_production_required"


def protocol():
    body = {"version": 1, "producer": "VerificationService",
        "targets": ["acceptance", "final_checks"],
        "kinds": ["build", "typecheck", "tests", "attachments", "command", "review"],
        "attachment_formats": ["png", "json"],
        "scope": "Map every existing task acceptance clause and final command, by zero-based index and exact value. "
            "Task acceptance producers are that task's checks; final_checks producers are the exact final command. "
            "A preparation being available does not schedule it at a task gate. Add a genuinely required producer "
            "to task checks through the normal plan, never add obligations in a review. Preserve final-only scope.",
        "semantics": "The independent reviewer classifies existing obligations. Review-only is for semantic/source/environment "
            "assessment, never a substitute for a required build, typecheck, tests or files. A mixed clause can cite several "
            "full commands. Attachment formats require actual archived bytes; file presence does not prove business semantics.",
        "revalidation": "A change to an accepted binding invalidates its consumer and dependency successors. "
            "Final binding changes invalidate all tasks conservatively. This grants no implementation permission."}
    return {**body, "hash": digest(body)}


def current_protocol(value):
    return value == protocol()


def _value(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _targets(plan):
    return {
        **{("acceptance", task.id, i): value for task in plan.tasks for i, value in enumerate(task.acceptance)},
        **{("final_checks", "", i): command.model_dump() for i, command in enumerate(plan.final_checks)}}


def validate_mapping(plan, assessment):
    if assessment is None:
        raise AA2Error("Check production mapping is missing; correct the plan/review, not its obligations")
    expected, seen = _targets(plan), set()
    tasks = {task.id: task for task in plan.tasks}
    for row in assessment.bindings:
        key = row.target, row.task_id, row.index
        if key in seen or key not in expected or _value(row.value) != expected[key]:
            raise AA2Error("Check production binding has a duplicate, foreign or stale obligation")
        seen.add(key)
        commands = tasks[row.task_id].checks if row.target == "acceptance" else plan.final_checks
        if row.kind == "review":
            if row.target != "acceptance" or row.producers or row.attachment_formats:
                raise AA2Error("Review-only binding cannot claim command or attachment production")
        elif not row.producers:
            raise AA2Error("Command-backed obligation has no producer in its acceptance phase")
        indexes = []
        for producer in row.producers:
            if producer.index >= len(commands) or producer.command != commands[producer.index]:
                raise AA2Error("Check producer must match the full command at the declared stage/index")
            indexes.append(producer.index)
        if len(indexes) != len(set(indexes)):
            raise AA2Error("Duplicate check producer")
        if row.target == "final_checks" and indexes != [row.index]:
            raise AA2Error("Final check must close with its own complete command")
        if len(row.attachment_formats) != len(set(row.attachment_formats)):
            raise AA2Error("Duplicate required attachment format")
    if seen != set(expected):
        raise AA2Error("Check production mapping must cover every task acceptance and final command")
    # This already structured capability is an explicit file-evidence obligation;
    # it cannot be reclassified as prose-only by the new reviewer metadata.
    for obligation in plan.evidence_contract.obligations if plan.evidence_contract else []:
        if obligation.capability == "test_file_attachments" and obligation.target == "acceptance":
            row = next(r for r in assessment.bindings if (r.target, r.task_id, r.index) ==
                       (obligation.target, obligation.task_id, obligation.index))
            if row.kind == "review":
                raise AA2Error("Declared test file obligation requires a real check producer")


def validate_receipt(record, plan):
    if not isinstance(record, dict) or record.get("binding") != digest({k: v for k, v in record.items() if k != "binding"}):
        raise AA2Error("Check production admission receipt is missing or invalid")
    if (record.get("candidate_hash") != digest(plan.model_dump()) or not record.get("review_hash")
            or not current_protocol(record.get("context", {}).get(PROTOCOL_KEY))):
        raise AA2Error("Check production receipt belongs to another plan or protocol")
    assessment = CheckProductionAssessment.model_validate(record.get("check_production"))
    validate_mapping(plan, assessment)
    return assessment


def active_receipt(get, plan=None):
    record = get(ACTIVE_KEY)
    if not record and not get(REQUIRED_KEY):
        return None  # Historic accepted plan; never manufacture a new-protocol approval.
    try:
        plan = plan or PlanResult.model_validate(get("plan"))
        validate_receipt(record, plan)
    except (AA2Error, ValueError) as exc:
        from .faults import StateConflict
        raise StateConflict("Accepted check production receipt is missing, damaged or bound to a different plan/protocol",
                            evidence_refs=[ACTIVE_KEY, REQUIRED_KEY, "plan"]) from exc
    return record


def accepted_projection(record):
    # Bounded snapshot: never recursively embed earlier admission contexts.
    if record is None:
        return None
    return {"candidate_hash": record["candidate_hash"], "check_production": record["check_production"],
            "protocol_hash": protocol()["hash"], "admission_binding": record["binding"]}


def prior_assessment(record, plan):
    if (record.get("candidate_hash") != digest(plan.model_dump()) or record.get("protocol_hash") != protocol()["hash"]
            or not record.get("admission_binding")):
        raise AA2Error("Prior check production projection is stale")
    result = CheckProductionAssessment.model_validate(record.get("check_production"))
    validate_mapping(plan, result)
    return result


def activation_updates(record):
    if record.get("check_production") is None:
        return {}
    return {ACTIVE_KEY: record, REQUIRED_KEY: {"version": 1}}


def _semantic(row):
    return {key: value for key, value in row.model_dump().items() if key != "reason"}


def preserve_mapping(previous, candidate, old_record, assessment, patch):
    if not old_record:
        return
    before = prior_assessment(old_record, previous)
    corrections = {(r["target"], r.get("task_id", ""), r["index"]) for r in patch.get("contract_corrections", [])}
    corrections |= {("acceptance", r["task_id"], r["acceptance_index"]) for r in patch.get("timing_corrections", [])}
    old_tasks = {t.id: t for t in previous.tasks}
    new_tasks = {t.id: t for t in candidate.tasks}
    for row in before.bindings:
        if row.kind == "review" or (row.target, row.task_id, row.index) in corrections:
            continue
        replacements = [new for new in assessment.bindings if new.target == row.target and _value(new.value) == _value(row.value)
            and (not row.task_id or new.task_id in new_tasks and
                 (new_tasks[new.task_id].origin_id or new.task_id) == (old_tasks[row.task_id].origin_id or row.task_id))]
        if not any(new.kind != "review" and set(row.attachment_formats) <= set(new.attachment_formats) for new in replacements):
            raise AA2Error("Check production downgrade requires the existing formal clause correction, not review metadata")


def validate_review(plan, verdict, context):
    declared = context.get(PROTOCOL_KEY)
    assessment = verdict.evidence_assessment.check_production
    if declared is None:
        if assessment is not None:
            raise AA2Error("Check production mapping has no frozen protocol")
        return
    if not current_protocol(declared):
        raise AA2Error("Check production review protocol is stale")
    if not verdict.approved:
        # A refusal may report an absent producer/mapping. It is archived and
        # routed by the existing admission/source gate, never activated here.
        return
    validate_mapping(plan, assessment)
    if context.get("previous_plan"):
        previous = PlanResult.model_validate(context["previous_plan"])
        preserve_mapping(previous, plan, context.get("accepted_check_production"), assessment, context.get("patch", {}))


def additional_affected(previous, candidate, old_record, assessment):
    """Same deterministic metadata delta at preparation and transactional activation."""
    if assessment is None:
        return set()
    from .plan_revision import descendants
    if not old_record:
        return {t.id for t in candidate.tasks}  # Explicit migration, not old evidence relabeling.
    before = prior_assessment(old_record, previous)
    old = {(r.target, r.task_id, r.index): _semantic(r) for r in before.bindings}
    new = {(r.target, r.task_id, r.index): _semantic(r) for r in assessment.bindings}
    delta = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    if any(k[0] == "final_checks" for k in delta):
        return {t.id for t in candidate.tasks}
    changed = {k[1] for k in delta}
    return (descendants(previous, changed) | descendants(candidate, changed)) & {t.id for t in candidate.tasks}


def journal_affected(previous, candidate, journal):
    """One metadata-delta contract for all prepared-journal validators."""
    production = journal.get("review", {}).get("evidence_assessment", {}).get("check_production")
    if production is None:
        return set()
    return additional_affected(previous, candidate,
        journal.get("evidence_admission", {}).get("context", {}).get("accepted_check_production"),
        CheckProductionAssessment.model_validate(production))


class ProductionGap(AA2Error):
    def __init__(self, message, *, kind="contract"):
        super().__init__(message)
        self.kind = kind


def observe(get, commands, results, work, environment, artifacts, *, task=None, phase="final"):
    """Called only after the existing full reusable() gate, never a replacement for it."""
    if task is None and phase not in {"final", "reuse", "reintegration"}:
        return None
    try:
        record = active_receipt(get)
        if record is None:
            return None
        plan = PlanResult.model_validate(get("plan"))
        assessment = validate_receipt(record, plan)
        offsets = {}
        if task:
            current = next((t for t in plan.tasks if t.id == task.id), None)
            if current != task:
                raise AA2Error("Check production task differs from the active plan")
            raw = current.checks
            offsets[task.id] = 0
        else:
            raw = list(plan.final_checks)
            for current in plan.tasks:
                offsets[current.id] = len(raw)
                raw.extend(current.checks)
            raw.extend(plan.runtime_probes)
        scheduled, resolved, preparations = verification_layout(raw, work, plan.runtime_prepare, prepare_all=task is None)
        if scheduled != commands or len(results) != len(scheduled):
            raise AA2Error("Check production execution list differs from the complete scheduled contract")
        positions = {digest(c.model_dump()): i for i, c in enumerate(scheduled)}
        closed = []
        file_obligations = {(r.task_id, r.index) for r in plan.evidence_contract.obligations
            if r.target == "acceptance" and r.capability == "test_file_attachments"} if plan.evidence_contract else set()
        for row in assessment.bindings:
            if task and (row.target != "acceptance" or row.task_id != task.id):
                continue
            if row.kind == "review":
                continue  # This gate does not prove semantic acceptance.
            selected = [positions[digest(resolved[(offsets[row.task_id] if row.task_id else 0) + p.index].model_dump())]
                        for p in row.producers]
            required_files = bool(row.attachment_formats) or row.kind == "attachments" or (row.task_id, row.index) in file_obligations
            identities = attachment_evidence(results, selected, artifacts, row.attachment_formats) if required_files else []
            closed.append({"target": row.target, "task_id": row.task_id, "index": row.index,
                "check_indexes": selected, "command_hashes": [results[i].command_hash for i in selected],
                "attachment_hashes": sorted(set(identities))})
        # Existing structured file obligations on executable commands also require
        # actual files. Only commands scheduled at this gate are in scope; a final
        # probe/preparation never becomes a new per-task obligation.
        for obligation in plan.evidence_contract.obligations if plan.evidence_contract else []:
            if obligation.capability != "test_file_attachments" or obligation.target == "acceptance":
                continue
            target = obligation.target
            if target == "checks":
                if task and obligation.task_id != task.id:
                    continue
                command = resolved[offsets[obligation.task_id] + obligation.index]
            elif task:
                continue
            elif target == "final_checks":
                command = resolved[obligation.index]
            elif target == "runtime_probes":
                command = resolved[len(raw) - len(plan.runtime_probes) + obligation.index]
            else:
                command = preparations[obligation.index]
            selected = [positions[digest(command.model_dump())]]
            identities = attachment_evidence(results, selected, artifacts, [])
            closed.append({"obligation_id": obligation.id, "target": target,
                "task_id": obligation.task_id, "index": obligation.index,
                "check_indexes": selected, "command_hashes": [results[i].command_hash for i in selected],
                "attachment_hashes": identities})
        return {"version": 1, "admission_binding": record["binding"], "plan_hash": record["candidate_hash"],
            "checks_hash": digest([r.model_dump() for r in results]), "source_hash": results[0].source_hash,
            "environment_hash": environment.fingerprint, "closed": closed,
            "scope": "Actual producer evidence at this gate; semantic/source review remains required."}
    except ProductionGap:
        raise
    except (KeyError, ValueError, AA2Error) as exc:
        raise ProductionGap(str(exc)) from exc


def attachment_evidence(results, selected, artifacts, required_formats):
    formats, identities = set(), set()
    for i in selected:
        for ref in results[i].attachments:
            try:
                payload = artifacts.bytes(ref["artifact"])
            except (KeyError, OSError, ValueError, AA2Error) as exc:
                raise ProductionGap("Required check attachment original is unavailable", kind="evidence") from exc
            if payload.startswith(b"\x89PNG\r\n\x1a\n"):
                formats.add("png")
            try:
                json.loads(payload)
                formats.add("json")
            except (ValueError, UnicodeDecodeError):
                pass
            identities.add(ref["artifact"]["hash"])
    if not identities or not set(required_formats) <= formats:
        raise ProductionGap("Required check attachments were not actually produced in the supported formats", kind="evidence")
    return sorted(identities)
