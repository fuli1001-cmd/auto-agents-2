"""Explicit, independently reviewed correction of model-derived plan obligations."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field

from .errors import AA2Error
from .evidence_refs import controller_context_catalog
from .execution_stages import current_stage_contract
from .models import Command, Record, ReviewResult
from .stage_responsibilities import current_correction_contract
from .util import digest

Target = Literal['acceptance', 'checks', 'final_checks', 'runtime_prepare', 'runtime_probes']


class ContractCorrection(Record):
    owner: Literal['plan'] = 'plan'
    target: Target
    task_id: str = ''
    index: int = Field(ge=0)
    previous: str | Command
    replacement: str | Command
    reason: str = Field(min_length=1)
    derived_decision: str = Field(min_length=1)
    preserved_obligations: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class ContractReview(Record):
    target: Target
    task_id: str = ''
    index: int = Field(ge=0)
    model_derived_only: bool
    user_constraints_preserved: bool
    assertions_preserved: bool
    collection_preserved: bool
    dependency_scope_preserved: bool
    scope_minimal: bool
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class ContractPlanReview(ReviewResult):
    contract_reviews: list[ContractReview] = []


def key(row):
    return row.target, row.task_id, row.index


def _value(value):
    return value.model_dump() if isinstance(value, Command) else value


def validate_contract_structure(plan, patch):
    """Prove exact mappings and structural minima only; never claim semantic equivalence."""
    if not patch.contract_corrections:
        return False
    if patch.timing_corrections or patch.remove_tasks:
        raise AA2Error('Contract correction cannot combine timing corrections or task removals')
    before = {t.id: t for t in plan.tasks}
    updates = {t.id: t for t in patch.upsert_tasks}
    if len(updates) != len(patch.upsert_tasks) or not set(updates) <= before.keys():
        raise AA2Error('Contract correction cannot add, rename or duplicate tasks')
    for field in ('preview', 'environment_file', 'requirements_file'):
        if getattr(patch, field) is not None and getattr(patch, field) != getattr(plan, field):
            raise AA2Error('Use a separate ordinary amendment for unrelated environment/preview contracts')
    expected = plan.model_copy(deep=True)
    targets = {t.id: t for t in expected.tasks}
    seen, changed_tasks = set(), set()
    for row in patch.contract_corrections:
        local = row.target in {'acceptance', 'checks'}
        if key(row) in seen or local != bool(row.task_id) or local and row.task_id not in targets:
            raise AA2Error('Duplicate or invalid contract correction target')
        seen.add(key(row))
        values = getattr(targets[row.task_id] if local else expected, row.target)
        if row.index >= len(values) or _value(values[row.index]) != _value(row.previous):
            raise AA2Error('Contract correction has a stale or missing previous obligation')
        if row.previous == row.replacement:
            raise AA2Error('Contract correction does not change its mapped expression')
        if row.target == 'acceptance':
            if not isinstance(row.replacement, str) or not row.replacement.strip():
                raise AA2Error('Acceptance correction requires a nonempty replacement clause')
        else:
            old, new = row.previous, row.replacement
            if not isinstance(old, Command) or not isinstance(new, Command):
                raise AA2Error('Check correction requires the complete old and new Command')
            if (new.cwd != old.cwd or new.expected_tests < old.expected_tests
                    or not set(old.inputs) <= set(new.inputs) or not set(old.outputs) <= set(new.outputs)):
                raise AA2Error('Check correction reduces structural test/input/output obligations')
        values[row.index] = row.replacement
        if local:
            changed_tasks.add(row.task_id)
    if set(updates) != changed_tasks:
        raise AA2Error('Contract correction must update exactly the mapped tasks')
    for tid, after in updates.items():
        allowed = before[tid].allowed_paths
        added = set(after.allowed_paths) - set(allowed)
        declared_inputs = {path for command in before[tid].checks for path in command.inputs}
        if (not set(allowed) <= set(after.allowed_paths) or not added <= declared_inputs
                or any(path in {'', '.'} or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                       or str(PurePosixPath(path)) != path or '\\' in path
                       or any(c in path for c in '*?[]') for path in added)):
            raise AA2Error('Contract correction cannot remove task paths or add wildcard scope')
        if after.model_dump(exclude={'title', 'description', 'allowed_paths'}) != targets[tid].model_dump(
                exclude={'title', 'description', 'allowed_paths'}):
            raise AA2Error('Contract correction changes an unmapped obligation, origin or dependency')
    for field in ('final_checks', 'runtime_prepare', 'runtime_probes'):
        actual = getattr(patch, field)
        if (getattr(plan, field) if actual is None else actual) != getattr(expected, field):
            raise AA2Error('Contract correction changes an unmapped final/prepare/probe command')
    return True


def _references(refs, context, *, review=False):
    catalog = controller_context_catalog(context, provenance={'patch': 'proposal', **({'plan': 'proposal'} if review else {})})
    resolved = [catalog.resolve(ref) for ref in refs]
    if None in resolved or not catalog.factual.intersection(resolved):
        raise AA2Error('Contract correction requires real supplied evidence, not protocol or proposal alone')
    paths = [catalog.entries[ref].path for ref in resolved if ref in catalog.factual]
    required = ('previous_plan', 'original_requirements', 'failure') if review else ('failure',)
    if any(not any(path == root or path.startswith(root + '.') or path.startswith(root + '[')
                   for path in paths) for root in required):
        raise AA2Error('Contract correction lacks references to the required independent sources')


def validate_contract_references(patch, context):
    if not patch.contract_corrections:
        return
    if (not context.get('original_requirements') or not context.get('specification')
            or not context.get('correction_request') or
            not current_correction_contract(context.get('controller_correction_contract'))):
        raise AA2Error('Contract correction requires original user input and a bound owner request')
    capture = context.get("original_capture") or {}
    if digest(context["original_requirements"]) != (capture.get("artifact") or {}).get("hash"):
        raise AA2Error("Contract correction original user text does not match its captured artifact")
    for row in patch.contract_corrections:
        _references(row.evidence_refs, context)


def validate_contract_review(patch, review, context=None):
    if not patch.contract_corrections:
        return
    data = review.model_dump() if hasattr(review, 'model_dump') else review
    if "evidence_assessment" in data:
        from .plan_revision import EvidencePatchReview
        verdict = EvidencePatchReview.model_validate(data)
    else:
        verdict = ContractPlanReview.model_validate(data)
    expected = {key(row) for row in patch.contract_corrections}
    if not verdict.approved or len(verdict.contract_reviews) != len(expected) or {key(r) for r in verdict.contract_reviews} != expected:
        raise AA2Error('Contract correction lacks complete independent semantic review')
    for row in verdict.contract_reviews:
        if not all(getattr(row, field) for field in ('model_derived_only', 'user_constraints_preserved',
                'assertions_preserved', 'collection_preserved', 'dependency_scope_preserved', 'scope_minimal')):
            raise AA2Error('Independent review rejected contract correction substance or scope')
        if context is not None:
            _references(row.evidence_refs, context, review=True)


def contract_review_binding(journal):
    fields = ('base_revision', 'previous_plan', 'patch', 'plan', 'review', 'affected', 'reopen',
              'correction_request', 'contract_review_context', 'contract_coverage')
    if journal.get('version') == 3:
        return digest(['contract-correction-review-v2', *[journal.get(k) for k in fields],
                       journal.get('contract_author_context')])
    return digest(['contract-correction-review-v1', *[journal.get(k) for k in fields]])


def legacy_contract_contexts(journal, journal_id, records, read_artifact):
    """Recover v2's omitted roots only from its frozen inputs, never today's workspace.

    The old journal kept a review subset. Its separate recovery context and
    content-addressed presentation receipts can prove the missing supplied roots.
    Unsupported/unretained roots stay unavailable; this is not a new review.
    """
    if journal.get('version') != 2 or journal.get('contract_review_binding') != contract_review_binding(journal):
        raise AA2Error('Legacy contract correction review binding changed')
    saved = journal.get('contract_review_context', {})
    base = records.get('recovery/patch-context/' + journal_id)
    if (not isinstance(base, dict) or not base
            or any(base[name] != saved[name] for name in base.keys() & saved.keys())):
        raise AA2Error('Legacy contract correction lost its frozen recovery inputs')
    author = {**saved, **base, 'plan': journal['previous_plan']}
    author.pop('previous_plan', None)
    reviewer = {**saved, **base, 'plan': journal['plan'], 'patch': journal['patch'],
                'affected': journal['affected'], 'reopen': journal['reopen'], 'review_kind': 'plan_patch'}
    for operation, context, result in (
            ('recovery/patch/' + journal_id, author, journal['patch']),
            ('recovery/patch-review/' + journal_id, reviewer, journal['review'])):
        invocation = records.get('invocation/' + operation, {})
        if invocation.get('result') != result:
            raise AA2Error('Legacy contract correction lacks its completed invocation receipt')
        proven = False
        for name, receipt in records.items():
            if not name.startswith('presentation/' + operation + '/'):
                continue
            try:
                presentation = read_artifact(receipt['artifact'])
            except (OSError, ValueError, KeyError):
                continue
            if not isinstance(presentation, dict) or presentation.get('purpose') != 'plan_patch':
                continue
            identities = {(row.get('source_ref'), row.get('identity'))
                          for row in presentation.get('selections', [])
                          if isinstance(row, dict) and row.get('presentation') != 'not_selected'}
            payload = presentation.get('payload', {})
            # These hashes describe original root values, before model projection.
            # The projector copies review_kind directly without a selection row.
            if all((root, digest(value)) in identities or root in payload and payload[root] == value
                   for root, value in context.items()):
                proven = True
                break
        if not proven:
            raise AA2Error('Legacy contract correction frozen input evidence is missing or changed')
    return author, reviewer


def validate_contract_journal(journal, get, *, legacy_contexts=None):
    """Re-evaluate the exact reviewed candidate before transactional activation."""
    if not journal.get('patch', {}).get('contract_corrections'):
        if journal.get('version') in {2, 3} or 'contract_review_binding' in journal or 'contract_review_context' in journal:
            raise AA2Error('Contract correction journal lost its explicit mapping')
        return
    from .models import PlanResult
    from .plan_revision import PlanPatch, apply_patch
    from .stage_responsibilities import validate_correction_request
    patch = PlanPatch.model_validate(journal['patch'])
    previous = PlanResult.model_validate(journal['previous_plan'])
    request = journal.get('correction_request', {})
    validate_correction_request(request, get, action='plan_patch')
    if journal.get('contract_review_binding') != contract_review_binding(journal):
        raise AA2Error('Contract correction no longer matches its independently reviewed candidate')
    if journal.get('version') == 3:
        author = journal.get('contract_author_context', {})
        context = journal.get('contract_review_context', {})
    elif journal.get('version') == 2 and legacy_contexts is not None:
        author, context = legacy_contexts
    else:
        raise AA2Error('Contract correction lacks separate frozen author and review inputs')
    if (author.get('plan') != journal['previous_plan']
            or context.get('previous_plan') != journal['previous_plan']
            or context.get('plan') != journal['plan'] or context.get('patch') != journal['patch']
            or any(view.get('correction_request') != request
                   or view.get('original_capture') != get('capture')
                   or view.get('specification') != get('clarify') for view in (author, context))
            or any(author[name] != context[name] for name in author.keys() & context.keys()
                   if name not in {'plan', 'evidence_reference_contract'})):
        raise AA2Error('Contract correction review lost its frozen original source bindings')
    if any('controller_stage_contract' in view and not current_stage_contract(view['controller_stage_contract'])
           for view in (author, context)):
        raise AA2Error('Contract correction stage protocol changed before activation')
    validate_contract_references(patch, author)
    validate_contract_review(patch, journal['review'], context)
    coverage_verified = False
    if previous.mode == 'verification_only':
        from .plan_revision import valid_coverage_receipt
        coverage_verified = valid_coverage_receipt(journal, get, journal.get('contract_coverage', {}))
        if not coverage_verified:
            raise AA2Error('Contract correction coverage proof changed before activation')
    from .review_authority import plan_source_correction
    candidate, affected, reopen = apply_patch(previous, patch, journal['base_revision'],
        {context.get('failure', {}).get('id')}, {t.id: t.origin_id for t in previous.tasks},
        coverage_verified=coverage_verified, source_correction=plan_source_correction(get, previous))
    from .check_production import journal_affected
    affected |= journal_affected(previous, candidate, journal)
    if (journal.get('contract_review_binding') != contract_review_binding(journal)
            or candidate.model_dump() != journal['plan'] or set(journal['affected']) != affected
            or set(journal['reopen']) != reopen):
        raise AA2Error('Contract correction no longer matches its independently reviewed candidate')


CONTRACT_CORRECTION_INSTRUCTIONS = '''
For an evidenced error in a MODEL-DERIVED technical obligation, use PlanPatch.contract_corrections.
This is not a timing amendment. Do not combine it with timing_corrections. Each mapping names
owner=plan, target (acceptance/checks/final_checks/runtime_prepare/runtime_probes), task_id for
task fields (otherwise empty), zero-based index, complete previous and replacement values,
derived_decision, reason, preserved_obligations and factual evidence_refs including failure.
Command values are full Command objects; inline scripts also require a full old/new mapping.
Use upsert_tasks for exactly mapped tasks. Keep task IDs/origins/requirements/dependencies,
all unmapped acceptance and commands, minimum tests and existing inputs/outputs. Map conflicting
old technical clauses explicitly; never append an override leaving the old obligation contradictory.
Necessary added implementation paths must be explicit inputs of the OLD accepted task checks and justified; do not remove old paths or
add wildcard scope. Do not mix environment-file/preview changes into this mode. Align document
and descriptions with the mapped correction. This proposal does not grant permission or pass checks.
Original user requirements, explicit versions, security/consent and historical substantive
assertions cannot be weakened. If those require change, request human clarification instead.
For independent review return ContractPlanReview.contract_reviews, one per mapping with matching
target/task_id/index. Independently assess every boolean: model_derived_only,
user_constraints_preserved, assertions_preserved, collection_preserved,
dependency_scope_preserved, scope_minimal. Give specific reasoning and citations to
previous_plan, original_requirements AND failure. Counts and the author's assurances are not
equivalence evidence. Reject hidden assertion/test-selection changes, contradictions, invented
availability, unrelated edits and missing evidence. Approval only permits activation and real
installation/checks afterward; it cannot certify installation, passing tests or final acceptance.
'''
