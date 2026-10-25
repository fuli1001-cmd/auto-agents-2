"""Local plan amendments and conservative task invalidation, without file dependency graphs."""
from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import Field, model_serializer

from .contract_corrections import (
    ContractCorrection,
    ContractPlanReview,
    validate_contract_structure,
)
from .errors import AA2Error
from .evidence_refs import controller_context_catalog
from .execution_stages import TimingProducer, current_stage_contract
from .models import (
    Command,
    EvidenceContract,
    EvidencePlanReview,
    PlanResult,
    PreviewSpec,
    Record,
    ReviewResult,
    TaskSpec,
)
from .planning import validate_plan
from .util import atomic_write, digest


def materialize_plan(store, rid, work, plan):
    path = work / "docs/aa2/plan.md"
    revision = store.get(rid, "plan_revision/current", {}).get("revision", 1)
    prior = store.get(rid, f"plan_revision/history/{revision - 1}", {}).get("plan", {}).get("document")
    if path.exists() and path.read_text() not in {plan.document, prior}:
        raise AA2Error("Plan document changed outside activation; preserve user edits and reconcile first")
    atomic_write(path, plan.document)


class TimingCorrection(Record):
    task_id: str = Field(min_length=1)
    acceptance_index: int = Field(ge=0)
    previous: str = Field(min_length=1)
    replacement: str = Field(min_length=1)
    producer: TimingProducer
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class TimingReview(Record):
    task_id: str
    acceptance_index: int = Field(ge=0)
    timing_only: bool
    obligations_preserved: bool
    user_order_preserved: bool
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class TimingPlanReview(ReviewResult):
    timing_reviews: list[TimingReview] = []


class EvidencePatchReview(EvidencePlanReview, TimingPlanReview, ContractPlanReview):
    pass


def _timing_acceptance(plan, patch):
    """Check explicit mappings and exact structural invariants, not prose equivalence."""
    if not patch.timing_corrections:
        return {}
    old = {t.id: t for t in plan.tasks}
    updates = {t.id: t for t in patch.upsert_tasks}
    expected, seen = {}, set()
    for correction in patch.timing_corrections:
        key = (correction.task_id, correction.acceptance_index)
        task = old.get(correction.task_id)
        if (key in seen or task is None or correction.acceptance_index >= len(task.acceptance)
                or task.acceptance[correction.acceptance_index] != correction.previous
                or correction.previous == correction.replacement):
            raise AA2Error('Timing correction has a duplicate, stale or invalid acceptance mapping')
        seen.add(key)
        expected.setdefault(task.id, list(task.acceptance))[correction.acceptance_index] = correction.replacement
    if patch.remove_tasks or set(updates) != set(expected) or len(updates) != len(patch.upsert_tasks):
        raise AA2Error('Timing correction must update exactly its existing tasks without additions or removals')
    for name in ('final_checks', 'runtime_prepare', 'runtime_probes', 'preview', 'environment_file', 'requirements_file'):
        value = getattr(patch, name)
        if value is not None and value != getattr(plan, name):
            raise AA2Error('Timing correction changes a delivery or environment contract')
    for task_id, acceptance in expected.items():
        before, after = old[task_id], updates[task_id]
        if (before.model_dump(exclude={'description', 'acceptance'}) != after.model_dump(exclude={'description', 'acceptance'})
                or after.acceptance != acceptance):
            raise AA2Error('Timing correction changes an unmapped obligation or a structured task contract')
    return expected


def _require_timing_references(references, context, *, review=False):
    provenance = {'patch': 'proposal', **({'plan': 'proposal'} if review else {})}
    catalog = controller_context_catalog(context, provenance=provenance)
    resolved = [catalog.resolve(ref) for ref in references]
    if None in resolved or not catalog.factual.intersection(resolved):
        raise AA2Error('Timing correction requires resolvable controller evidence, not only its own proposal')


def validate_timing_references(patch, context):
    for correction in patch.timing_corrections:
        _require_timing_references(correction.evidence_refs, context)


def validate_timing_review(patch, review, context=None):
    """Require explicit independent semantic judgments; these are not machine proofs."""
    if not patch.timing_corrections:
        return
    data = review.model_dump() if hasattr(review, 'model_dump') else review
    if "evidence_assessment" in data:
        verdict = EvidencePatchReview.model_validate(data)
    else:
        verdict = TimingPlanReview.model_validate(data)
    expected = {(c.task_id, c.acceptance_index) for c in patch.timing_corrections}
    actual = [(r.task_id, r.acceptance_index) for r in verdict.timing_reviews]
    if not verdict.approved or len(actual) != len(expected) or set(actual) != expected:
        raise AA2Error('Timing correction lacks a complete independent review')
    for row in verdict.timing_reviews:
        if not (row.timing_only and row.obligations_preserved and row.user_order_preserved):
            raise AA2Error('Timing review did not preserve substance or legitimate prerequisites')
        if context is not None:
            _require_timing_references(row.evidence_refs, context, review=True)


def timing_review_binding(previous, patch, candidate, review, controller_stage_contract=None):
    if controller_stage_contract is None:
        return digest(['timing-review-v1', previous, patch, candidate, review])
    return digest(['timing-review-v2', previous, patch, candidate, review, controller_stage_contract])


def valid_coverage_receipt(journal, get, receipt):
    """A verification-only patch must keep fresh passing coverage on this source."""
    from .models import CheckResult
    from .verification import passed
    rows = get("coverage_checks") or []
    return (receipt.get("verified") is True and receipt.get("checks_hash") == digest(rows)
            and passed([CheckResult.model_validate(row) for row in rows])
            and all(row.get("source_hash") == journal["source_hash"] for row in rows))


def validate_timing_journal(journal):
    """Recheck a prepared journal on activation, including after interruption."""
    patch_data = journal.get('patch', {})
    if not patch_data.get('timing_corrections'):
        return
    patch = PlanPatch.model_validate(patch_data)
    previous = PlanResult.model_validate(journal['previous_plan'])
    candidate, affected, reopen = apply_patch(previous, patch, journal['base_revision'],
        set(patch.failure_ids), {t.id: t.origin_id for t in previous.tasks})
    validate_timing_review(patch, journal['review'])
    protocol = journal.get('controller_stage_contract')
    if protocol is not None and not current_stage_contract(protocol):
        raise AA2Error('Timing correction journal cites a stale or invalid controller stage contract')
    if protocol is None and any(ref == 'controller_stage_contract' or ref.startswith('controller_stage_contract.')
            for row in [*patch_data['timing_corrections'], *journal['review'].get('timing_reviews', [])]
            for ref in row['evidence_refs']):
        raise AA2Error('Timing correction journal has no bound controller stage contract')
    expected = timing_review_binding(journal['previous_plan'], patch_data, journal['plan'], journal['review'], protocol)
    if (journal.get('timing_review_binding') != expected or candidate.model_dump() != journal['plan']
            or set(journal['affected']) != affected or set(journal['reopen']) != reopen):
        raise AA2Error('Timing correction journal no longer matches its reviewed candidate')


class PlanPatch(Record):
    base_revision: int = Field(ge=1)
    failure_ids: list[str] = Field(min_length=1)
    reason: str
    document: str
    upsert_tasks: list[TaskSpec] = []
    timing_corrections: list[TimingCorrection] = []
    contract_corrections: list[ContractCorrection] = []
    remove_tasks: list[str] = []
    final_checks: list[Command] | None = None
    runtime_prepare: list[Command] | None = None
    runtime_probes: list[Command] | None = None
    preview: PreviewSpec | None = None
    environment_file: str | None = None
    requirements_file: str | None = None
    evidence_contract: EvidenceContract | None = Field(default=None, description=(
        "Complete evidence contract for the candidate after this patch; required on new proposals."))

    @model_serializer(mode="wrap")
    def preserve_legacy_shape(self, handler):
        data = handler(self)
        if self.evidence_contract is None:
            data.pop("evidence_contract", None)
        return data


def descendants(plan, seeds):
    affected = set(seeds)
    while True:
        more = {t.id for t in plan.tasks if set(t.depends_on) & affected}
        if more <= affected:
            return affected
        affected |= more


def implementation_changed(old, new):
    return any(getattr(old, key) != getattr(new, key) for key in (
        "description", "requirement_ids", "allowed_paths", "acceptance"))


def _retain_checks(old, new):
    # Retain runnable test targets and minimum counts. Collector-only flags may
    # change; the independent review checks semantic assertions and command changes.
    def obligation(argv):
        kept, skip = [], False
        for index, arg in enumerate(argv):
            if skip:
                skip = False
            elif arg == "-p" and index + 1 < len(argv) and argv[index + 1] in {"no:junitxml", "junitxml"}:
                skip = True
            elif arg in {"--reporter", "--reporters", "--junitxml", "--junit-xml", "--outputFile"}:
                skip = True
            elif arg == "--run" or arg.startswith(("--reporter=", "--reporters=", "--junitxml=", "--junit-xml=", "--outputFile=")):
                continue
            else:
                kept.append(arg)
        return kept

    for command in old:
        generated_inputs = {p for p in command.inputs if {"dist", ".next"} & set(PurePosixPath(p).parts)}
        matches = [c for c in new if c.cwd == command.cwd and obligation(c.argv) == obligation(command.argv)
                   and set(command.outputs) <= set(c.outputs) and generated_inputs <= set(c.inputs)]
        if not matches or max(c.expected_tests for c in matches) < command.expected_tests:
            raise AA2Error("Plan patch removes a check obligation or reduces expected_tests")


def apply_patch(plan: PlanResult, patch: PlanPatch, revision: int, failures: set[str], origins: dict, *, coverage_verified=False,
                source_correction=None):
    if patch.base_revision != revision:
        raise AA2Error("Stale plan patch base_revision")
    if not set(patch.failure_ids) <= failures:
        raise AA2Error("Plan patch references unknown failures")
    contract = validate_contract_structure(plan, patch)
    timing = _timing_acceptance(plan, patch)
    old = {t.id: t for t in plan.tasks}
    updates = {t.id: t for t in patch.upsert_tasks}
    if len(updates) != len(patch.upsert_tasks) or set(patch.remove_tasks) & updates.keys():
        raise AA2Error("Conflicting task operations in plan patch")
    if not set(patch.remove_tasks) <= old.keys():
        raise AA2Error("Plan patch removes an unknown task")
    for task in updates.values():
        origin = origins.get(task.id)
        if origin and task.origin_id != origin or task.origin_id not in set(origins.values()):
            raise AA2Error("Plan patch changes or invents a budget origin")
    tasks = {k: v for k, v in old.items() if k not in patch.remove_tasks}
    tasks.update(updates)
    for task in plan.tasks:
        replacements = [t for t in tasks.values() if t.origin_id == task.origin_id]
        if not replacements or not set(task.requirement_ids) <= {r for t in replacements for r in t.requirement_ids}:
            raise AA2Error("Removed task lacks replacement coverage in its original budget lineage")
        if not contract and task.id not in timing and not set(task.acceptance) <= {a for t in replacements for a in t.acceptance}:
            raise AA2Error("Plan patch removes accepted obligations")
        if not contract:
            _retain_checks(task.checks, [c for t in replacements for c in t.checks])
    changes = {k: getattr(patch, k) for k in ("final_checks", "runtime_prepare", "runtime_probes", "preview", "environment_file", "requirements_file")
               if getattr(patch, k) is not None}
    candidate = plan.model_copy(update={"tasks": list(tasks.values()), "document": patch.document, **changes}).model_copy(deep=True)
    if patch.evidence_contract is not None:
        candidate.evidence_contract = patch.evidence_contract.model_copy(deep=True)
    from .evidence_admission import preserve_obligations
    preserve_obligations(plan, candidate, patch, source_correction=source_correction)
    if not contract:
        _retain_checks(plan.final_checks, candidate.final_checks)
        _retain_checks(plan.runtime_prepare, candidate.runtime_prepare)
        _retain_checks(plan.runtime_probes, candidate.runtime_probes)
    validate_plan(candidate, {r for t in plan.tasks for r in t.requirement_ids}, coverage_verified=coverage_verified)
    changed = {k for k, v in updates.items() if k not in old or v != old[k]} | set(patch.remove_tasks)
    affected = descendants(candidate, changed) | descendants(plan, changed)
    old_obligations = plan.evidence_contract.obligations if plan.evidence_contract else []
    new_obligations = candidate.evidence_contract.obligations if candidate.evidence_contract else []
    if changes or old_obligations != new_obligations:
        affected |= tasks.keys()  # preparation/verification contract: conservative recheck
    reopen = set()
    for key, task in updates.items():
        predecessors = ([old[key]] if key in old else
                        [old[k] for k in patch.remove_tasks if old[k].origin_id == task.origin_id])
        if len(predecessors) != 1 or implementation_changed(predecessors[0], task):
            reopen.add(key)
    reopen.update(row.task_id for row in patch.contract_corrections if row.task_id)
    return candidate, affected & tasks.keys(), reopen
