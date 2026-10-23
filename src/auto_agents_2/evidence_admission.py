"""Finite controller evidence capabilities, not a language for business assertions.

The controller checks declared mappings. Independent review still has to detect
omitted mappings, misattributed requirements and prose outside a capability.
"""
from __future__ import annotations

from .errors import AA2Error
from .faults import HumanIntervention, HumanRequirement
from .models import EvidenceContract, EvidencePlanReview, PlanResult
from .util import digest


def capability_catalog(*, shared_browsers=False):
    capabilities = [
        {"id": "installed_dependency_snapshot", "version": 1, "producer": "EnvironmentManager",
         "stage": "environment", "available": True,
         "observes": "Selected runtime and installed package identities, dependencies and actual installation provenance status.",
         "limits": "Not effective solver settings, future installability or guaranteed provenance when not_recorded."},
        {"id": "dependency_locks", "version": 1, "producer": "EnvironmentManager",
         "stage": "environment", "available": True,
         "observes": "Recorded pip/Conda installed locks and their hashes, bound to the prepared environment.",
         "limits": "Does not edit product lockfiles or collect global pins, channel priority or merged solver configuration."},
        {"id": "shared_product_browser_identity", "version": 1, "producer": "EnvironmentManager",
         "stage": "environment", "available": bool(shared_browsers),
         "observes": "Verified shared browser artifact identity and selected product driver binding when installed.",
         "limits": "Requires shared caching and a selected product browser. Prototype browser is not product evidence."},
        {"id": "test_file_attachments", "version": 1, "producer": "VerificationService",
         "stage": "verification", "available": True,
         "observes": "Files actually collected under the configured attachment policy, with source and content identity.",
         "limits": "Memory-only body, paths, counts or capture completion alone do not prove required content."},
    ]
    body = {"version": 1, "capabilities": capabilities,
            "not_collected": ["effective_solver_configuration"],
            "authority": "Controller protocol only. Availability is not successful production or acceptance."}
    return {**body, "hash": digest(body)}


def current_catalog(value):
    return any(value == capability_catalog(shared_browsers=enabled) for enabled in (False, True))


def requirement_source(get):
    capture, specification = get("capture"), get("clarify")
    if not capture or not specification or not capture.get("artifact", {}).get("hash"):
        raise AA2Error("Evidence admission requires the retained original capture and specification")
    body = {"version": 1, "capture_hash": capture["artifact"]["hash"],
            "specification_hash": digest(specification),
            "requirement_ids": sorted(r["id"] for r in specification.get("requirements", []))}
    return {**body, "hash": digest(body)}


def current_requirement_source(value):
    if not isinstance(value, dict):
        return False
    body = {key: item for key, item in value.items() if key != "hash"}
    return (set(body) == {"version", "capture_hash", "specification_hash", "requirement_ids"}
            and body["version"] == 1 and isinstance(body["requirement_ids"], list)
            and digest(body) == value.get("hash"))


def admission_context(get, *, shared_browsers=False):
    return {"controller_evidence_capabilities": capability_catalog(shared_browsers=shared_browsers),
            "evidence_requirement_source": requirement_source(get)}


def empty_contract(context):
    """Explicit no-controller-evidence mapping; never a production default."""
    return EvidenceContract(catalog_hash=context["controller_evidence_capabilities"]["hash"],
                            source_binding=context["evidence_requirement_source"]["hash"], obligations=[])


def _value(plan, row):
    if row.target in {"acceptance", "checks"}:
        tasks = {task.id: task for task in plan.tasks}
        if row.task_id not in tasks:
            raise AA2Error("Evidence obligation names an unknown task")
        values = getattr(tasks[row.task_id], row.target)
    else:
        if row.task_id:
            raise AA2Error("Global evidence obligation cannot name a task")
        values = getattr(plan, row.target)
    if row.index >= len(values):
        raise AA2Error("Evidence obligation target index is out of range")
    value = values[row.index]
    return value.model_dump() if hasattr(value, "model_dump") else value


def validate_obligations(plan, context):
    """Reject stale mappings; return unsupported declarations for source review.

    Unknown capabilities remain proposals and cannot be accepted. Allowing their
    independent source review is necessary to distinguish a real user blocker
    from a model-derived bad technical choice; it is not a waiver.
    """
    contract = plan.evidence_contract
    catalog, source = (context.get(k) for k in
                       ("controller_evidence_capabilities", "evidence_requirement_source"))
    if contract is None:
        raise AA2Error("New plan candidates require an explicit evidence_contract; legacy approval is not new admission")
    if (not current_catalog(catalog) or not current_requirement_source(source)
            or contract.catalog_hash != catalog["hash"] or contract.source_binding != source["hash"]):
        raise AA2Error("Evidence contract has a missing or stale capability/source binding")
    capabilities = {row["id"]: row for row in catalog["capabilities"]}
    stages = {name: i for i, name in enumerate(("plan", "implementation", "environment", "verification", "final_review"))}
    seen, targets, unsupported = set(), set(), []
    for row in contract.obligations:
        value = row.value.model_dump() if hasattr(row.value, "model_dump") else row.value
        target = (row.target, row.task_id, row.index, row.capability)
        if row.id in seen or target in targets or value != _value(plan, row):
            raise AA2Error("Evidence obligation has a duplicate identity or stale target/value")
        seen.add(row.id)
        targets.add(target)
        if not set(row.requirement_ids) <= set(source["requirement_ids"]):
            raise AA2Error("Evidence obligation cites unknown original requirement IDs")
        if row.task_id:
            task = next(task for task in plan.tasks if task.id == row.task_id)
            if not set(row.requirement_ids) <= set(task.requirement_ids):
                raise AA2Error("Evidence obligation source is outside its task's declared requirement coverage")
        cap = capabilities.get(row.capability)
        if (cap is None or not cap["available"] or row.capability_version != cap["version"]
                or row.producer != cap["producer"] or row.production_stage != cap["stage"]
                or stages[row.required_stage] < stages[row.production_stage]):
            unsupported.append(row.id)
    return unsupported


def preserve_obligations(previous, candidate, patch):
    """Metadata must not grant a second route for removing accepted obligations."""
    before = previous.evidence_contract
    if before is None:
        return
    after = candidate.evidence_contract
    if after is None:
        raise AA2Error("Evidence contract cannot be downgraded to legacy format")
    new = {r.id: r for r in after.obligations}
    corrections = {(r.target, r.task_id, r.index) for r in patch.contract_corrections}
    corrections |= {("acceptance", r.task_id, r.acceptance_index) for r in patch.timing_corrections}
    for row in before.obligations:
        replacement = new.get(row.id)
        if replacement == row:
            continue
        if (row.target, row.task_id, row.index) not in corrections:
            raise AA2Error("Evidence obligation change requires a formal mapped contract/timing correction")
        if row.origin != "model_derived" and (replacement is None or replacement.origin != row.origin
                or replacement.requirement_ids != row.requirement_ids):
            raise AA2Error("Evidence correction cannot remove or reattribute a user/unknown obligation")


def validate_admission_review(plan, review, context, *, allow_user_blocker=True):
    data = review.model_dump() if hasattr(review, "model_dump") else review
    # Composite patch reviews also carry separately validated timing/contract judgments.
    verdict = EvidencePlanReview.model_validate({k: v for k, v in data.items()
                                               if k not in {"timing_reviews", "contract_reviews"}})
    assessment = verdict.evidence_assessment
    unsupported = validate_obligations(plan, context)
    if (assessment.candidate_hash != digest(plan.model_dump())
            or assessment.catalog_hash != context["controller_evidence_capabilities"]["hash"]
            or assessment.source_binding != context["evidence_requirement_source"]["hash"]):
        raise AA2Error("Evidence review is not bound to this exact candidate, source and capability version")
    confirmed = set(assessment.unsupported_user_obligations)
    user_rows = {r.id for r in plan.evidence_contract.obligations if r.origin == "user"}
    if not confirmed <= set(unsupported) & user_rows:
        raise AA2Error("Evidence review claims an unsupported user obligation without its exact source mapping")
    if confirmed and assessment.origins_accurate and assessment.mapping_complete and allow_user_blocker:
        raise HumanIntervention(HumanRequirement(kind="external_input",
            reason="A source-reviewed user evidence requirement has no supported producer: " + ", ".join(sorted(confirmed)),
            action="Resolve the missing capability or explicitly clarify the original requirement; preserve the candidate and source.",
            resume_condition="Recheck the actual capability and source through plan admission. Confirmation cannot waive this gate.",
            evidence_refs=["capture", "clarify"]), reason_code="evidence_capability_unavailable", phase="plan")
    if (unsupported or not verdict.approved or confirmed or not all((assessment.mapping_complete,
            assessment.origins_accurate, assessment.capabilities_sufficient, assessment.requirements_preserved))):
        raise AA2Error("Evidence admission rejected: unsupported declarations or incomplete independent semantic review; "
                       "preserve user requirements and formally correct model-derived clauses. Unsupported: " + ", ".join(unsupported))


def admission_record(plan, review, context, *, previous=None, patch=None, affected=(), reopen=(), coverage=None):
    body = {"version": 1, "context": {k: context[k] for k in
            ("controller_evidence_capabilities", "evidence_requirement_source")},
            "candidate_hash": digest(plan.model_dump()), "review_hash": digest(review.model_dump()),
            "previous_hash": digest(previous.model_dump()) if previous else None,
            "patch_hash": digest(patch.model_dump()) if patch else None,
            "affected": sorted(affected), "reopen": sorted(reopen), "coverage": coverage}
    return {**body, "binding": digest(body)}


def requires_admission(journal):
    return ("evidence_admission" in journal or "evidence_assessment" in journal.get("review", {})
            or any("evidence_contract" in journal.get(key, {}) for key in ("plan", "previous_plan", "patch")))


def validate_admission_journal(journal, get, *, shared_browsers=False):
    candidate_data, patch_data = journal["plan"], journal["patch"]
    record = journal.get("evidence_admission")
    if not requires_admission(journal):
        return  # Existing prepared legacy records retain their original strict validators.
    if not record or not candidate_data.get("evidence_contract") or not patch_data.get("evidence_contract"):
        raise AA2Error("Prepared evidence contract is missing its admission record or patch contract")
    from .plan_revision import EvidencePatchReview, PlanPatch, apply_patch, valid_coverage_receipt
    context = admission_context(get, shared_browsers=shared_browsers)
    if record.get("context") != context:
        raise AA2Error("Prepared evidence admission source/capability context is stale")
    previous = PlanResult.model_validate(journal["previous_plan"])
    patch = PlanPatch.model_validate(patch_data)
    coverage = record.get("coverage") if previous.mode == "verification_only" else None
    coverage_verified = False
    if previous.mode == "verification_only":
        coverage_verified = valid_coverage_receipt(journal, get, coverage or {})
        if not coverage_verified:
            raise AA2Error("Evidence admission coverage proof changed before activation")
    candidate, affected, reopen = apply_patch(previous, patch, journal["base_revision"], set(patch.failure_ids),
        {t.id: t.origin_id for t in previous.tasks}, coverage_verified=coverage_verified)
    review = EvidencePatchReview.model_validate(journal["review"])
    validate_admission_review(candidate, review, context, allow_user_blocker=False)
    expected = admission_record(candidate, review, context, previous=previous, patch=patch,
                                affected=affected, reopen=reopen, coverage=coverage)
    if (record != expected or candidate.model_dump() != candidate_data
            or set(journal["affected"]) != affected or set(journal["reopen"]) != reopen):
        raise AA2Error("Prepared evidence admission does not match its reviewed candidate and invalidation")


def evidence_instructions(purpose):
    if purpose not in {"design", "plan", "plan_patch", "implement", "implementation", "test_changes", "verify", "recovery_diagnosis", "decision"}:
        return ""
    return ("\nFinite evidence admission: use controller_evidence_capabilities when supplied. New PlanResult/PlanPatch must "
            "include a complete evidence_contract with the supplied catalog/source hashes, mapping every controller evidence "
            "obligation to its exact clause or full command. An explicit empty list requires independent completeness review; "
            "it cannot hide a user requirement. Unknown capability must be declared, never invented or silently dropped. "
            "Do not mislabel a requirement as model-derived. Review must bind candidate_hash to the supplied candidate digest, "
            "check mapping completeness, origins, capability sufficiency and substantive preservation. Capability availability "
            "does not prove installability or successful evidence collection. EnvironmentManager consumes frozen product locks; "
            "the task author proposes lock edits through protected review.\n"
            "Playwright required file evidence: write PNG/JSON to testInfo.outputPath(...), then await testInfo.attach(name, "
            "{path, contentType}). Use the controller-selected output directory. Memory-only body attachments do not guarantee "
            "archived originals; verify actual captured content, not counts/complete status. Never attach credentials/private data.")
