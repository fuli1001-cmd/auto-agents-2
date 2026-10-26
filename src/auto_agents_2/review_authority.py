"""Finite normative identities shared by workflow review consumers.

This validates provenance and effects, not the meaning of arbitrary prose. A
genuine source quotation still needs independent semantic review and execution.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, create_model

from .errors import AA2Error, InvalidStructuredResponse
from .models import Record, ReviewResult
from .util import digest

VERSION = 1
# Existing validation semantics, also disclosed to the response producer.
FINDING_COVERAGE = {"defect": "violated", "evidence_gap": "unverified", "source_dispute": "unverified"}
GOAL_KINDS = {"submitted_requirement", "accepted_interpretation"}
OWNERS = Literal["clarify", "provider_research", "prototype", "design", "plan", "develop", "checks", "controller"]
KINDS = {
    "coverage_review": ("clarify", "Interpret submitted goals without inventing user statements or consent. Preserve every original obligation; assess actual existing coverage."),
    "research": ("provider_research", "Verify research claims and citations. Research is advice or evidence, never permission or a new product requirement."),
    "design": ("design", "Review architecture and its selected technical methods against submitted goals. Explicitly adopt bounded technical choices; a method is not a user statement or an execution capability."),
    "plan": ("plan", "Review complete task/check contracts, genuine capability prerequisites and preservation of goals. A declared origin or requirement ID does not prove a method was requested."),
    "plan_patch": ("plan", "Review the exact old/new contract, preserved obligations, affected tasks and evidence. Never escape a rejected amendment by relabeling user requirements."),
    "implementation": ("develop", "Assess the actual scoped candidate against accepted obligations and fresh evidence. Green tests alone do not establish visible behavior or complete semantics."),
    "test_changes": ("develop", "Preserve substantive existing assertions and collection behavior. Review proposed test changes before application; counts or prose cannot establish equivalence."),
    "preview_contract": ("plan", "Preserve all accepted preview operations and assertions using the existing exact mapping. Recommendations cannot replace accepted obligations."),
    "verify": ("develop", "Independently assess the complete current candidate, real checks and browser observations against all accepted obligations. Advice cannot invent acceptance requirements."),
    "doctor": ("controller", "Report diagnostic observations only. This review cannot accept a project, change its contracts or grant execution permission."),
}


class CriterionAssessment(Record):
    criterion_id: str = Field(description="Exact controller_review_basis.criteria ID, including its prefix.")
    status: Literal["satisfied", "violated", "unverified", "later_stage"] = Field(
        description="Use response_contract.finding_coverage for blocking findings; every unresolved criterion needs a mapped finding.")
    reason: str = Field(min_length=1)


class FindingAuthority(Record):
    finding_index: int = Field(ge=0)
    criterion_id: str = Field(description="Exact controller_review_basis.criteria ID, not a bare specification ID.")
    kind: Literal["defect", "evidence_gap", "advisory", "source_dispute"]
    owner: OWNERS
    source_quote: str = ""
    observation_refs: list[str] = Field(min_length=1,
        description="Exact keys of controller_review_basis.observations only; material source_ref/path/hash is not this namespace.")
    method_source: str = Field(default="",
        description="For source_dispute, exact key of controller_review_basis.sources; otherwise empty. Not a material source_ref.")
    method_quote: str = ""
    rationale: str = Field(min_length=1)


class TechnicalChoice(Record):
    id: str = Field(min_length=1)
    clause: str = Field(min_length=1)
    goal_ids: list[str] = Field(min_length=1,
        description="Exact goal criterion IDs listed in response_contract.goal_ids, including requirement: prefixes.")
    method_origin: Literal["derived_choice", "explicit_input_method"]
    input_quote: str = ""
    method_source: str = "input"
    feasibility: Literal["supported", "not_yet_verified", "unavailable"]
    evidence_refs: list[str] = Field(default_factory=list,
        description="Exact keys of controller_review_basis.observations; no material paths or inferred evidence.")
    reason: str = Field(min_length=1)


class ObligationSource(Record):
    obligation_id: str = Field(description="Exact entry of controller_review_basis.unsupported for THIS review, not historical feedback.")
    goal_ids: list[str] = Field(min_length=1,
        description="Exact goal criterion IDs in response_contract.goal_ids; also included in preserve_ids.")
    method_source: str
    method_quote: str = Field(min_length=1)
    classification: Literal["explicit_input_method", "necessary_technical_condition", "derived_choice", "unresolved"]
    disposition: Literal["genuine_gap", "correct_method", "unresolved"]
    owner: OWNERS
    input_quote: str = ""
    preserve_ids: list[str] = Field(min_length=1,
        description="Exact controller criterion IDs, including prefixes; preserve every referenced goal.")
    reason: str = Field(min_length=1)


class AmendmentAssessment(Record):
    source: str
    old_quote: str = Field(min_length=1)
    new_quote: str = Field(min_length=1)
    preserve_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class AuthorityAssessment(Record):
    version: Literal[1] = VERSION
    basis_hash: str
    candidate_hash: str
    coverage: list[CriterionAssessment] = Field(description="Assess each controller criterion exactly once; no omitted or invented IDs.")
    findings: list[FindingAuthority] = []
    technical_choices: list[TechnicalChoice] = []
    choices_complete: bool
    obligations: list[ObligationSource] = Field(default_factory=list,
        description="Exactly one row per current basis.unsupported ID; [] when unsupported is empty, even if historical feedback lists obligations.")
    amendments: list[AmendmentAssessment] = []
    requirements_preserved: bool
    explanation: str = Field(min_length=1)


class InvalidAuthorityReview(InvalidStructuredResponse):
    def __init__(self, code, response):
        self.details = {"code": code, "contract": "review_authority", "version": VERSION}
        self.response = response
        super().__init__("Review authority validation failed: " + code)


def scope(stage, context, operation=""):
    if operation == "doctor":
        return "doctor"
    if stage in {"coverage_review", "verify"}:
        return stage
    return context.get("review_kind") if stage == "review" else None


@lru_cache(maxsize=16)
def review_schema(schema):
    """New invocations require the record; old persisted models remain readable."""
    if issubclass(schema, ReviewResult):
        return create_model("Authority" + schema.__name__, __base__=schema,
                            authority=(AuthorityAssessment, ...))
    if ("review" in schema.model_fields and isinstance(schema.model_fields["review"].annotation, type)
            and issubclass(schema.model_fields["review"].annotation, ReviewResult)):
        return create_model("Authority" + schema.__name__, __base__=schema,
                            review=(review_schema(schema.model_fields["review"].annotation), ...))
    return schema


def candidate_data(kind, context):
    names = {
        "coverage_review": ("assessment",), "research": ("research",),
        "design": ("design", "design_documents"), "plan": ("plan",),
        "plan_patch": ("plan", "patch", "previous_plan"),
        "implementation": ("task", "implementation", "source_hash", "project"),
        "test_changes": ("candidate_changes", "project", "purpose"),
        "preview_contract": ("preview_contracts",),
        "verify": ("plan", "project", "incoming"), "doctor": ("project",),
    }
    return {name: context[name] for name in names[kind] if name in context}


def source_text(value):
    """Readable quotation view; never a structured value or target identity."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(source_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(source_text(v) for v in value)
    return str(value)


def plan_clause_sources(root, plan, kind):
    """Expose the existing finite correction targets without flattening identity."""
    result = {}

    def add(target, task_id, values):
        for index, value in enumerate(values):
            binding = {"target": target, "task_id": task_id, "index": index,
                       "value_hash": digest(value)}
            key = root + ":clause:" + digest([target, task_id, index])
            result[key] = {"kind": kind, "owner": "plan", "root": root,
                           "text": source_text(value), "value": value, "contract_target": binding}

    for task in plan.get("tasks", []):
        for target in ("acceptance", "checks"):
            add(target, task["id"], task.get(target, []))
    for target in ("final_checks", "runtime_prepare", "runtime_probes"):
        add(target, "", plan.get(target, []))
    return result


def dispute_targets(findings, basis):
    targets = {}
    for finding in findings:
        if finding.kind != "source_dispute":
            continue
        source = basis["sources"][finding.method_source]
        if source.get("contract_target"):
            value = {"source": finding.method_source, **source["contract_target"]}
            targets[digest(value)] = value
    return [targets[key] for key in sorted(targets)]


def response_contract(criteria, sources, observations, unsupported):
    """Describe this finite output interface; never authorize or rewrite a verdict."""
    return {
        "version": 1,
        "criterion_ids": [c["id"] for c in criteria],
        "goal_ids": [c["id"] for c in criteria if c["kind"] in GOAL_KINDS],
        "source_ids": sorted(sources),
        "observation_ids": sorted(observations),
        "unsupported_obligation_ids": list(unsupported),
        "finding_coverage": dict(FINDING_COVERAGE),
        "rules": [
            "These exact IDs apply inside authority only. Material source_ref/material_id paths locate readable "
            "content but do not become source_ids or observation_ids. Never drop a criterion prefix.",
            "coverage contains every criterion_id exactly once. findings maps every top-level finding by its "
            "zero-based index exactly once. Non-advisory findings must be major/blocking; advisory is minor.",
            "Each non-advisory finding's criterion coverage equals finding_coverage[kind]. The set of all "
            "violated/unverified criteria equals the set mapped by non-advisory findings; no unpaired coverage "
            "or findings. If one issue affects multiple criteria, give each unresolved criterion its own mapped finding.",
            "approved is true only with no blocking findings, requirements_preserved=true and choices_complete=true. "
            "A stage criterion cannot be later_stage; final verify cannot defer any criterion.",
            "Product defects/evidence gaps quote the actual criterion statement. A general stage role can ground "
            "only a source_dispute (except doctor), not an invented product obligation.",
            "source_dispute uses an exact sources key and a nonempty method_quote from that source's text, "
            "with its actual owner. A contract_target source needs its complete text. Other kinds leave method_source "
            "and method_quote empty. Explicit input/answer methods require clarification, not a technical waiver.",
            "technical_choices are for design only: unique IDs, exact clauses in sources.candidate.text, goal_ids "
            "from the goal list and evidence_refs from the observation list. Explicit input methods must quote "
            "the submitted input/answer source. not_yet_verified is not execution success; unavailable cannot be adopted.",
            "obligations maps exactly unsupported_obligation_ids once each; it is [] when that list is empty. "
            "Historical blocked_admission is background, not a new current obligation. goal_ids are a subset "
            "of preserve_ids; preserve_ids use criterion_ids. method_quote must occur in the selected source.",
            "correct_method is a derived_choice at the selected proposal/technical source's owner and requires "
            "a source_dispute with exactly the same owner, method_source and method_quote. A genuine_gap needs "
            "explicit_input_method or necessary_technical_condition. Unknown/derived methods do not self-authorize waiting.",
            "amendments is [] without a controller correction. Approval of a correction requires every old clause "
            "mapped once from previous_contract to exact new candidate text, preserving all requested criterion IDs.",
            "Identity is not semantic entailment, acceptance, successful execution or permission. Retain real goals "
            "and uncertainty; never fabricate references or verdicts merely to satisfy this format.",
        ],
    }


def make_basis(kind, context, *, original, specification, accepted_design=None,
               accepted_plan=None, design_adoptions=None, source_hash="", source_identity=None,
               correction=None, submitted_answers=()):
    """All arguments labelled accepted come from controller state, not proposals."""
    if kind not in KINDS:
        raise ValueError("Unregistered authority consumer: " + str(kind))
    owner, statement = KINDS[kind]
    if correction and correction.get("completed"):
        correction = None
    sources = {
        "input": {"kind": "submitted_input", "owner": "clarify", "text": original,
                  "authorship": "Not established by a captured file; never claim verbatim personal authorship."},
        "specification": {"kind": "accepted_interpretation", "owner": "clarify", "text": source_text(specification)},
    }
    criteria = [{"id": "stage:" + kind, "kind": "controller_rule", "owner": owner,
                 "source": "controller", "statement": statement}]
    if original:
        criteria.append({"id": "input:goals", "kind": "submitted_requirement", "owner": "clarify",
                         "source": "input", "statement": original})
    for answer in submitted_answers:
        key = "answer:" + answer["id"]
        sources[key] = {"kind": "submitted_answer", "owner": "clarify", "text": answer["answer"],
                        "question_context": answer["prompt"], "binding": answer["artifact_hash"],
                        "answer_record": answer,
                        "authorship": "Externally submitted answer; not proof of personal human authorship."}
        criteria.append({"id": key, "kind": "submitted_requirement", "owner": "clarify",
                         "source": key, "statement": answer["answer"]})
    for requirement in (specification or {}).get("requirements", []):
        criteria.append({"id": "requirement:" + requirement["id"], "kind": "accepted_interpretation",
                         "owner": "clarify", "source": "specification", "statement": source_text(requirement)})
    if accepted_design:
        sources["accepted_design"] = {"kind": "accepted_technical_contract", "owner": "design",
                                      "text": source_text(accepted_design)}
        # Legacy architecture remains available, explicitly not original input.
        # New adoptions are the only normative selection from a new design.
        for choice in design_adoptions or []:
            criteria.append({"id": "design:" + choice["id"], "kind": "accepted_technical_contract",
                             "owner": "design", "source": "accepted_design", "statement": choice["clause"],
                             "goal_ids": choice["goal_ids"]})
    if accepted_plan:
        sources["accepted_plan"] = {"kind": "accepted_technical_contract", "owner": "plan", "text": source_text(accepted_plan)}
        for task in accepted_plan.get("tasks", []):
            criteria.append({"id": "task:" + task["id"], "kind": "accepted_technical_contract",
                             "owner": "plan", "source": "accepted_plan", "statement": source_text(task)})
        for name in ("final_checks", "runtime_prepare", "runtime_probes", "preview"):
            if accepted_plan.get(name):
                criteria.append({"id": "plan:" + name, "kind": "accepted_technical_contract",
                                 "owner": "plan", "source": "accepted_plan", "statement": source_text(accepted_plan[name])})
    candidate = candidate_data(kind, context)
    sources["candidate"] = {"kind": "proposal", "owner": owner, "text": source_text(candidate)}
    if correction and correction.get("owner") == owner:
        sources["previous_contract"] = {"kind": correction.get("previous_kind", "proposal"), "owner": owner,
                                        "text": source_text(correction["previous"])}
        for choice in correction.get("retained_choices", []):
            if choice["id"] not in {c["id"] for c in criteria}:
                criteria.append({**choice, "source": "previous_contract"})
    for root, plan in (("accepted_plan", accepted_plan),
                       ("candidate", context.get("plan") if kind in {"plan", "plan_patch"} else None),
                       ("previous_contract", correction.get("previous") if correction and owner == "plan" else None)):
        if plan and root in sources:
            sources.update(plan_clause_sources(root, plan, sources[root]["kind"]))
    observations = {name: digest(value) for name, value in context.items() if name in {
        "project", "implementation", "candidate_changes", "checks", "evidence", "browser_evidence",
        "implemented_visuals", "verification_observation", "source_history", "retrieved_sources",
        "design_documents", "preview_contracts", "retained_candidates", "failure", "assessment"}}
    observations["candidate"] = digest(candidate)
    from .recovery import check_rows
    execution = check_rows(context)
    if isinstance(context.get("evidence"), list):
        execution += [row for row in context["evidence"] if isinstance(row, dict)]
    if isinstance(context.get("failure"), dict):
        execution += check_rows(context["failure"].get("evidence", {}))
    # Only controller execution records count. Prose, candidate hashes,
    # another approval and report timestamps cannot reset a correction chain.
    progress_facts = [{k: row.get(k) for k in ("command_hash", "input_hash", "environment_hash",
        "exit_code", "process_exit_code", "tests", "failure_reasons")}
        for row in execution if row.get("command_hash") and row.get("input_hash")]
    for value in sources.values():
        value["hash"] = digest(value["text"])
    body = {"version": VERSION, "scope": kind, "authority": "normative_identity_only",
            "source_identity": source_identity, "source_hash": source_hash,
            "candidate_hash": digest(candidate), "criteria": criteria, "sources": sources,
            "observations": observations,
            "correction_facts": digest(progress_facts),
            "unsupported": list(context.get("unsupported_evidence_obligations", [])),
            "capability_hash": (context.get("controller_evidence_capabilities") or {}).get("hash"),
            "correction": correction if correction and correction.get("owner") == owner else None,
            "meaning": "References establish identity, not entailment, successful execution or permission."}
    body["response_contract"] = response_contract(criteria, sources, observations, body["unsupported"])
    return {**body, "hash": digest(body)}


def valid_basis(basis):
    return (isinstance(basis, dict) and basis.get("version") == VERSION
            and basis.get("authority") == "normative_identity_only" and basis.get("scope") in KINDS
            and basis.get("hash") == digest({k: v for k, v in basis.items() if k != "hash"}))


def validate_review(value, basis):
    raw = value.model_dump() if hasattr(value, "model_dump") else value
    if isinstance(raw, dict) and "review" in raw and "approved" not in raw:
        raw = raw["review"]

    def fail(code):
        raise InvalidAuthorityReview(code, raw)

    if not valid_basis(basis):
        fail("invalid_controller_basis")
    try:
        review = ReviewResult.model_validate({k: v for k, v in raw.items() if k in {"approved", "summary", "findings", "authority"}})
        assessment = AuthorityAssessment.model_validate(raw.get("authority"))
    except (ValidationError, TypeError, AttributeError):
        fail("required_authority_fields")
    if assessment.basis_hash != basis["hash"] or assessment.candidate_hash != basis["candidate_hash"]:
        fail("stale_review_binding")
    criteria = {c["id"]: c for c in basis["criteria"]}
    coverage = {r.criterion_id: r for r in assessment.coverage}
    if len(coverage) != len(assessment.coverage) or set(coverage) != set(criteria):
        fail("incomplete_or_duplicate_coverage")
    if any(row.status == "later_stage" and (key.startswith("stage:") or basis["scope"] == "verify")
           for key, row in coverage.items()):
        fail("current_gate_cannot_be_deferred")
    if review.approved and (not assessment.requirements_preserved or not assessment.choices_complete
                           or any(r.status in {"violated", "unverified"} for r in coverage.values())):
        fail("approval_requires_complete_assessment")
    findings = {f.finding_index: f for f in assessment.findings}
    if len(findings) != len(assessment.findings) or set(findings) != set(range(len(review.findings))):
        fail("unmapped_review_findings")
    blocking = set()
    for index, finding in enumerate(review.findings):
        proof = findings[index]
        criterion = criteria.get(proof.criterion_id)
        if criterion is None:
            fail("unknown_normative_criterion")
        if not set(proof.observation_refs) <= set(basis["observations"]):
            fail("unknown_observation_reference")
        if proof.kind == "advisory":
            if finding.severity != "minor":
                fail("advice_cannot_block")
            continue
        if finding.severity not in {"blocking", "major"}:
            fail("unresolved_finding_must_block")
        if (criterion["kind"] == "controller_rule" and proof.kind != "source_dispute"
                and basis["scope"] != "doctor"):
            fail("general_stage_role_cannot_invent_product_obligation")
        expected = FINDING_COVERAGE[proof.kind]
        if coverage[proof.criterion_id].status != expected:
            fail("finding_coverage_disagreement")
        blocking.add(proof.criterion_id)
        if criterion["kind"] != "controller_rule" and (
                not proof.source_quote.strip() or proof.source_quote not in criterion["statement"]):
            fail("missing_exact_normative_source")
        if proof.kind == "source_dispute":
            method = basis["sources"].get(proof.method_source)
            if (not method or method["kind"] not in {"proposal", "accepted_technical_contract", "accepted_interpretation"}
                    or proof.owner != method["owner"] or not proof.method_quote.strip()
                    or proof.method_quote not in method["text"]):
                fail("invalid_source_dispute_owner_or_method")
            if method.get("contract_target") and proof.method_quote != method["text"]:
                fail("incomplete_contract_clause_quote")
            if any(proof.method_quote in source["text"] for source in basis["sources"].values()
                   if source["kind"] in {"submitted_input", "submitted_answer"}):
                fail("explicit_input_clause_requires_clarification_not_technical_waiver")
        elif proof.method_source or proof.method_quote:
            fail("method_dispute_must_be_explicit")
    unresolved = {k for k, row in coverage.items() if row.status in {"violated", "unverified"}}
    if unresolved != blocking or review.approved == bool(blocking):
        fail("verdict_requires_grounded_findings")
    ids = set()
    goal_ids = {k for k, c in criteria.items() if c["kind"] in GOAL_KINDS}
    for choice in assessment.technical_choices:
        if (basis["scope"] != "design" or choice.id in ids or choice.clause not in basis["sources"]["candidate"]["text"]
                or not set(choice.goal_ids) <= goal_ids):
            fail("invalid_technical_adoption")
        ids.add(choice.id)
        source = basis["sources"].get(choice.method_source, {})
        if choice.method_origin == "explicit_input_method" and (
                source.get("kind") not in {"submitted_input", "submitted_answer"}
                or not choice.input_quote.strip() or choice.input_quote not in source["text"]):
            fail("method_does_not_quote_submitted_input")
        if not set(choice.evidence_refs) <= set(basis["observations"]):
            fail("adoption_invents_execution_evidence")
        if review.approved and choice.feasibility == "unavailable":
            fail("unavailable_method_cannot_be_adopted")
    obligations = {r.obligation_id: r for r in assessment.obligations}
    if len(obligations) != len(assessment.obligations) or set(obligations) != set(basis["unsupported"]):
        fail("unmapped_unsupported_obligations")
    for obligation in obligations.values():
        method = basis["sources"].get(obligation.method_source)
        if (not set(obligation.goal_ids) <= goal_ids or not set(obligation.goal_ids) <= set(obligation.preserve_ids)
                or not set(obligation.preserve_ids) <= set(criteria) or not method
                or obligation.method_quote not in method["text"]):
            fail("invalid_obligation_source_or_preservation")
        if obligation.classification == "explicit_input_method" and (
                method["kind"] not in {"submitted_input", "submitted_answer"}
                or obligation.method_quote != obligation.input_quote or not obligation.input_quote.strip()):
            fail("unsupported_method_not_in_original_input")
        if obligation.disposition == "correct_method" and (
                obligation.classification != "derived_choice" or method["kind"] not in {"proposal", "accepted_technical_contract"}
                or obligation.owner != method["owner"]):
            fail("correction_has_wrong_source_or_owner")
        if obligation.disposition == "correct_method" and not any(
                f.kind == "source_dispute" and f.owner == obligation.owner
                and f.method_source == obligation.method_source and f.method_quote == obligation.method_quote
                for f in findings.values()):
            fail("correction_requires_mapped_blocking_finding")
        if obligation.disposition == "genuine_gap" and obligation.classification not in {
                "explicit_input_method", "necessary_technical_condition"}:
            fail("unknown_or_derived_method_cannot_self_authorize_wait")
        if review.approved:
            fail("unsupported_obligation_cannot_pass")
    correction = basis.get("correction")
    if assessment.amendments and not correction:
        fail("amendment_has_no_controller_request")
    if correction and review.approved:
        wanted = set(correction["clauses"])
        actual = [a.old_quote for a in assessment.amendments]
        if len(actual) != len(set(actual)) or set(actual) != wanted:
            fail("correction_requires_complete_old_new_mapping")
        for row in assessment.amendments:
            if (row.source != "previous_contract" or row.old_quote not in basis["sources"][row.source]["text"]
                    or row.new_quote not in basis["sources"]["candidate"]["text"]
                    or not set(correction["preserve_ids"]) <= set(row.preserve_ids)
                    or not set(row.preserve_ids) <= set(criteria)):
                fail("invalid_amendment_source_or_preservation")
    return assessment


def review_current(value, basis):
    try:
        validate_review(value, basis)
        return True
    except InvalidAuthorityReview:
        return False


def source_identity(get):
    return {**{name: digest(get(name)) for name in (
        "capture", "clarify", "clarify_context", "design", "plan", "plan_revision/current", "prototype_approval")},
        "authority/submitted-answers": digest(get("authority/submitted-answers") or [])}


def plan_source_correction(get, previous):
    """Only a current controller-recorded dispute can reopen a mislabeled method.

    This allows preparing a proposal, never activating it without its own
    independent old/new preservation review and the existing transaction gates.
    """
    request = get("authority/correction")
    if not request or request.get("completed") or request.get("owner") != "plan":
        return None
    if request.get("source_identity") != source_identity(get):
        raise AA2Error("Plan source correction is not bound to this source version")
    receipt = get(request.get("review_key", ""))
    if not receipt or receipt.get("review_hash") != digest(receipt.get("review")):
        raise AA2Error("Plan source correction lost its independent source assessment")
    if receipt["basis"]["source_identity"] != request["source_identity"]:
        raise AA2Error("Plan source correction cites a different source version")
    assessment = validate_review(receipt["review"], receipt["basis"])
    findings = [r for r in assessment.findings if r.kind == "source_dispute" and r.owner == "plan"]
    sources = receipt["basis"]["sources"]
    roots = {sources[r.method_source].get("root", r.method_source) for r in findings}
    if len(roots) != 1:
        raise AA2Error("Plan source correction has no single responsible source")
    root = next(iter(roots))
    original = (previous.model_dump() if root == "accepted_plan" else receipt.get("owned_candidate")
                if root == "candidate" else (receipt["basis"].get("correction") or {}).get("previous")
                if root == "previous_contract" else None)
    kind = sources[root]["kind"]
    if original is None or request.get("previous") != original or request.get("previous_kind") != kind:
        raise AA2Error("Plan source correction changed its original source identity")
    quotes = {r.method_quote for r in findings}
    if receipt["review"]["approved"] or set(request["clauses"]) != quotes:
        raise AA2Error("Plan source correction has no exact disputed clauses")
    if request.get("mapped_clauses", []) != dispute_targets(findings, receipt["basis"]):
        raise AA2Error("Plan source correction changed its exact target or structured value")
    if kind == "proposal":
        # Author feedback and the required old/new review remain in the shared
        # correction context. An unaccepted candidate grants no exception to
        # preserving the actually accepted plan's evidence obligations.
        return None
    if kind != "accepted_technical_contract" or original != previous.model_dump():
        raise AA2Error("Plan source correction is not bound to this accepted version")
    return request


def validate_journal_authority(journal, get):
    """Validate inside the existing activation transaction, before any write."""
    supplementary = journal.get("authority_reassessment")
    raw = supplementary["review"] if supplementary else journal["review"]
    basis = supplementary["basis"] if supplementary else journal.get("evidence_admission", {}).get("authority")
    assessment = validate_review(raw, basis)
    receipt = get("authority/reviews/" + digest(raw))
    candidate = {name: journal[name] for name in ("plan", "patch", "previous_plan")}
    if (not raw["approved"] or receipt is None or receipt.get("review") != raw
            or receipt.get("basis") != basis or receipt.get("review_hash") != digest(raw)
            or basis["source_identity"] != source_identity(get)
            or basis["candidate_hash"] != digest(candidate)
            or basis["source_hash"] != journal["source_hash"]):
        raise AA2Error("Prepared review authority lost its exact source, version, candidate or independent receipt")
    return assessment


def instructions():
    return """
Shared review authority: controller_review_basis contains source identities, limited standards and observations.
Its response_contract describes the exact authority output namespace and field relationships. Follow it for
both direct and nested review results. Material source_ref/material_id instructions concern reading and
prose citations, not authority IDs. In authority, use only the exact listed criterion/source/observation keys,
including prefixes. A source_dispute maps to unverified, not violated. Each unresolved coverage item needs
its own mapped non-advisory finding. obligations is exactly the CURRENT unsupported list (empty means []),
not historical feedback. No caller or reviewer may infer a new obligation from feedback alone.
Submitted input is authoritative for this task but is NOT proof that a human personally wrote every sentence.
An accepted interpretation or technical contract is not a verbatim user instruction. Candidate documents,
research recommendations and previous resolution text cannot create normative criteria or permissions.
Return authority bound to the exact basis/candidate hashes. Assess every criterion once. Map every finding
by index to a standard, exact source quote, actual observation references, kind and owner. Advisory is minor
and cannot reject. A source_dispute identifies the actual proposed/accepted technical method and its owner;
it does not erase the goal. Preserve real behavior, checks and history. A real evidence gap still blocks.
For an existing plan acceptance/check/prepare/probe clause, select its exact controller source entry with
contract_target and quote its complete text. Its target, index and full value hash bind the proposed correction;
readable text alone is not a command identity. A root-plan quote cannot authorize reattributing a different clause.
For design, explicitly list the selected technical_choices (exact candidate clauses and goal IDs); choices_complete
asserts you inspected the whole candidate for binding methods, not merely its title. Recommendations not adopted
remain advice. not_yet_verified is not success: the selected method must still be feasible within known capabilities.
Never adopt a known unavailable derived method as an unavoidable user condition. Legitimate technical dependencies
remain binding after formal adoption and can change only at their owner. Approval grants no execution permission.
For EACH unsupported evidence obligation explain the result goal, method source/quote, classification and disposition.
An explicit_input_method must quote submitted input or a controller-provided answer source itself
(method_source=input or answer:<id> and method_quote=input_quote). Question prose is context, not the respondent's
statement; automatic clarification decisions are not externally submitted requirements. Short ambiguous answers
need semantic clarification, never expansion into a new exact method merely by quoting the model's question.
Do not use a quote about the goal to relabel a design's separately chosen method as explicit input.
Separate explicit input methods from necessary technical conditions and optional derived choices. A real quotation
does not itself prove entailment: independently justify why the condition is necessary and why alternatives do not
meet the goal. Unknown facts remain unresolved. Do not equate a finite evidence collector catalog with all OS/test
execution capabilities. Preserve final JSON, real file roundtrips, cancellation and visible behavior as applicable.
No field proves arbitrary semantic equivalence. Invalid grounding returns to this reviewer; it never approves a product.
For product defects or evidence gaps cite an actual submitted/adopted criterion and quote its relevant clause.
The general stage role is not a substitute product requirement; only explicit source disputes may use it.
When correction is supplied, review the previous contract and proposed replacement together. Approval requires exact
old/new clause mappings for every disputed method and preservation of every retained goal; preserve undisputed
obligations too. A new wording, matching count, or author claim is not proof. An unresolved dispute must still reject.
"""
