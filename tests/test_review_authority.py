from copy import deepcopy

import pytest
from authority_review_fixtures import authority_review

from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import Finding, ReviewResult
from auto_agents_2.review_authority import (
    InvalidAuthorityReview,
    make_basis,
    review_schema,
    validate_review,
)
from auto_agents_2.util import digest


def context(kind="design", original="Cancel selection must leave all data unchanged.", **extra):
    ctx = {"design": {"document": "Use a human native dialog as the only accepted proof."}, **extra}
    ctx["controller_review_basis"] = make_basis(kind, ctx, original=original,
        specification={"requirements": [{"id": "R1", "description": original}]},
        source_hash="source", source_identity={"capture": "original"})
    return ctx


def rejected(ctx, kind="source_dispute"):
    review = authority_review(ctx, approved=False, findings=[Finding(severity="major", location="design",
        description="Method is an unsupported derived choice", evidence="Exact candidate", resolution="Correct at design")])
    proof = review.authority["findings"][0]
    proof.update(kind=kind, owner="design", method_source="candidate",
                 method_quote="Use a human native dialog as the only accepted proof.")
    for row in review.authority["coverage"]:
        if row["criterion_id"] == proof["criterion_id"]:
            row["status"] = "unverified"
    return review


@pytest.mark.parametrize("kind", ["coverage_review", "research", "design", "plan", "plan_patch", "implementation",
                                  "test_changes", "preview_contract", "verify", "doctor"])
def test_finite_consumers_require_exact_new_review(kind):
    ctx = context(kind)
    review = authority_review(ctx)
    assert validate_review(review, ctx["controller_review_basis"]).requirements_preserved
    schema = review_schema(ReviewResult)
    assert schema.model_validate(review.model_dump()).model_dump() == review.model_dump()
    with pytest.raises(ValueError):
        schema.model_validate({"approved": True, "summary": "old verdict"})


def test_legacy_serialization_stays_byte_compatible():
    assert ReviewResult(approved=True, summary="old").model_dump() == {
        "approved": True, "summary": "old", "findings": []}


def test_legacy_and_new_provider_schemas_remain_strict_without_arbitrary_maps():
    from auto_agents_2.models import strict_schema
    legacy = strict_schema(ReviewResult)
    current = strict_schema(review_schema(ReviewResult))
    assert "authority" not in legacy["properties"]
    assert "authority" in current["required"]


@pytest.mark.parametrize("mutation", ["basis", "candidate", "missing_coverage", "unknown_criterion", "unknown_observation",
                                     "wrong_owner", "missing_quote", "advice_blocks", "defer_gate"])
def test_bad_binding_or_authority_is_not_a_product_rejection(mutation):
    ctx = context()
    review = rejected(ctx)
    a = review.authority
    if mutation == "basis":
        a["basis_hash"] = "old"
    elif mutation == "candidate":
        a["candidate_hash"] = "old"
    elif mutation == "missing_coverage":
        a["coverage"].pop()
    elif mutation == "unknown_criterion":
        a["findings"][0]["criterion_id"] = "author:extra-rule"
    elif mutation == "unknown_observation":
        a["findings"][0]["observation_refs"] = ["imaginary"]
    elif mutation == "wrong_owner":
        a["findings"][0]["owner"] = "develop"
    elif mutation == "missing_quote":
        a["findings"][0]["method_quote"] = "not in candidate"
    elif mutation == "advice_blocks":
        a["findings"][0]["kind"] = "advisory"
    elif mutation == "defer_gate":
        a["coverage"][0]["status"] = "later_stage"
    with pytest.raises(InvalidAuthorityReview):
        validate_review(review, ctx["controller_review_basis"])


def test_exact_input_method_is_distinct_from_a_derived_method():
    ctx = context(original="Use a human native dialog as the only accepted proof.")
    review = authority_review(ctx)
    review.authority["technical_choices"] = [{"id": "native-proof", "clause": ctx["design"]["document"],
        "goal_ids": ["input:goals"], "method_origin": "explicit_input_method", "input_quote": ctx["design"]["document"],
        "feasibility": "not_yet_verified", "reason": "The submitted input explicitly chose this method"}]
    validate_review(review, ctx["controller_review_basis"])
    other = context()
    review.authority.update(basis_hash=other["controller_review_basis"]["hash"],
                            candidate_hash=other["controller_review_basis"]["candidate_hash"])
    with pytest.raises(InvalidAuthorityReview, match="method_does_not_quote"):
        validate_review(review, other["controller_review_basis"])


def test_source_dispute_preserves_original_goal_and_does_not_approve():
    ctx = context()
    assessment = validate_review(rejected(ctx), ctx["controller_review_basis"])
    assert assessment.findings[0].owner == "design"
    assert ctx["controller_review_basis"]["sources"]["input"]["kind"] == "submitted_input"
    assert "personally" not in ctx["controller_review_basis"]["sources"]["input"]["text"]


def test_self_declared_origin_is_not_an_input_quote():
    ctx = context("plan", unsupported_evidence_obligations=["native"])
    review = authority_review(ctx, approved=False, findings=[Finding(severity="blocking", location="plan",
        description="unsupported", evidence="candidate", resolution="retain goal")])
    review.authority["obligations"] = [{"obligation_id": "native", "goal_ids": ["input:goals"],
        "method_source": "candidate", "method_quote": "native", "classification": "explicit_input_method",
        "disposition": "genuine_gap", "owner": "plan", "input_quote": "user demanded native operator",
        "preserve_ids": ["input:goals"], "reason": "Origin flag alone is not evidence"}]
    with pytest.raises(InvalidAuthorityReview):
        validate_review(review, ctx["controller_review_basis"])


def test_invalid_findings_do_not_mutate_issue_ledger(tmp_path):
    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.store import Store
    store = Store(tmp_path)
    run = store.admit("fixture", {})
    ctx = context()
    review = rejected(ctx)
    review.authority["findings"][0]["criterion_id"] = "author:geometry-json"
    with pytest.raises(InvalidAuthorityReview):
        IssueLedger(store, ArtifactStore(store.root)).record(run["id"], "design", review, ctx)
    assert store.get(run["id"], "issues") is None


def test_old_new_mapping_is_required_and_preserves_goals():
    ctx = context()
    request = {"owner": "design", "previous": ctx["design"], "clauses": [ctx["design"]["document"]],
               "preserve_ids": ["input:goals", "requirement:R1"]}
    ctx["design"] = {"document": "Observe real cancellation with unchanged state and refresh."}
    ctx["controller_review_basis"] = make_basis("design", ctx,
        original="Cancel selection must leave all data unchanged.",
        specification={"requirements": [{"id": "R1", "description": "Cancel selection preserves state"}]},
        correction=request)
    review = authority_review(ctx)
    with pytest.raises(InvalidAuthorityReview, match="complete_old_new"):
        validate_review(review, ctx["controller_review_basis"])
    review.authority["amendments"] = [{"source": "previous_contract", "old_quote": request["clauses"][0],
        "new_quote": ctx["design"]["document"], "preserve_ids": request["preserve_ids"],
        "reason": "Remove the derived operator method while preserving the actual business assertion"}]
    validate_review(review, ctx["controller_review_basis"])
    weakened = deepcopy(review)
    weakened.authority["amendments"][0]["preserve_ids"] = ["input:goals"]
    with pytest.raises(InvalidAuthorityReview, match="invalid_amendment"):
        validate_review(weakened, ctx["controller_review_basis"])


def test_candidate_hash_is_content_bound():
    ctx = context()
    basis = ctx["controller_review_basis"]
    assert basis["candidate_hash"] == digest({"design": ctx["design"]})


@pytest.mark.parametrize("target,bad", [(t, None) for t in
    ("checks", "final_checks", "runtime_prepare", "runtime_probes")] + [
    ("checks", x) for x in ("short_quote", "wrong_owner", "stale", "unmapped", "goals")])
def test_command_source_correction_keeps_exact_mapping_and_business_obligations(target, bad):
    from test_recovery_contracts import failure, plan

    from auto_agents_2.contract_corrections import ContractCorrection
    from auto_agents_2.errors import AA2Error
    from auto_agents_2.models import EvidenceContract, EvidenceObligation
    from auto_agents_2.plan_revision import PlanPatch, apply_patch
    from auto_agents_2.review_authority import source_text
    old = plan()
    command = old.tasks[2].checks[0].model_copy(deep=True)
    task_id = 'T3' if target == 'checks' else ''
    if not task_id:
        setattr(old, target, [command])
    replacement = command.model_copy(update={'argv': [*command.argv, '--strict-markers']})
    obligation = EvidenceObligation(id='derived-command', target=target, task_id=task_id, index=0,
        value=command, origin='user', requirement_ids=['R3'], capability='test_file_attachments',
        capability_version=1, producer='VerificationService', production_stage='verification', required_stage='verification')
    old.evidence_contract = EvidenceContract(catalog_hash='catalog', source_binding='source', obligations=[obligation])
    mapped = obligation.model_copy(update={'value': replacement, 'origin': 'model_derived'})
    fields = {target: [replacement]} if not task_id else {
        'upsert_tasks': [old.tasks[2].model_copy(update={'checks': [replacement]})]}
    row = ContractCorrection(target=target, task_id=task_id, index=0, previous=command, replacement=replacement,
        reason='Revise the evidenced derived expression', derived_decision='Planner selected the invocation',
        preserved_obligations=['Keep all business assertions'], evidence_refs=['failure'])
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason='Correct command source', document='Updated plan',
        contract_corrections=[row], evidence_contract=old.evidence_contract.model_copy(update={'obligations': [mapped]}), **fields)
    correction = {'owner': 'plan', 'previous': old.model_dump(), 'clauses': [source_text(command.model_dump())],
        'mapped_clauses': [{'target': target, 'task_id': task_id, 'index': 0, 'value_hash': digest(command.model_dump())}]}
    if bad == 'short_quote':
        correction['clauses'] = [command.argv[0]]
    elif bad == 'wrong_owner':
        correction['owner'] = 'design'
    elif bad == 'stale':
        correction['previous']['document'] += ' changed'
    elif bad == 'unmapped':
        patch.contract_corrections = []
    elif bad == 'goals':
        mapped.requirement_ids = ['R1']
    def apply():
        return apply_patch(old, patch, 1, {failure().id}, {t.id: t.origin_id for t in old.tasks},
                           source_correction=correction)
    if bad:
        with pytest.raises(AA2Error):
            apply()
    else:
        candidate, _, _ = apply()
        assert candidate.evidence_contract.obligations[0].origin == 'model_derived'
        assert candidate.evidence_contract.obligations[0].requirement_ids == ['R3']
        assert [t.acceptance for t in candidate.tasks] == [t.acceptance for t in old.tasks]
        assert candidate.evidence_contract.obligations[0].value.expected_tests == command.expected_tests


@pytest.mark.parametrize('same_command', [False, True])
@pytest.mark.parametrize('binding', ['correct', 'different_target', 'different_value', 'missing'])
def test_source_correction_cannot_transfer_between_equal_text_targets(same_command, binding):
    from test_recovery_contracts import failure, plan

    from auto_agents_2.contract_corrections import ContractCorrection
    from auto_agents_2.errors import AA2Error
    from auto_agents_2.models import EvidenceContract, EvidenceObligation
    from auto_agents_2.plan_revision import PlanPatch, apply_patch
    from auto_agents_2.review_authority import source_text
    old = plan()
    a = old.tasks[2].checks[0].model_copy(update={'argv': ['python', '-c', 'print(1)\nprint(2)']})
    b = a.model_copy(deep=True) if same_command else a.model_copy(
        update={'argv': ['python', '-c', 'print(1)', 'print(2)']})
    assert source_text(a.model_dump()) == source_text(b.model_dump())
    assert same_command or digest(a.model_dump()) != digest(b.model_dump())
    old.tasks[2].checks = [a, b]
    replacement = b.model_copy(update={'argv': [*b.argv, '--strict-markers']})
    row = EvidenceObligation(id='B', target='checks', task_id='T3', index=1, value=b, origin='user',
        requirement_ids=['R3'], capability='test_file_attachments', capability_version=1,
        producer='VerificationService', production_stage='verification', required_stage='verification')
    old.evidence_contract = EvidenceContract(catalog_hash='catalog', source_binding='source', obligations=[row])
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason='Test exact target binding', document='Changed',
        upsert_tasks=[old.tasks[2].model_copy(update={'checks': [a, replacement]})],
        contract_corrections=[ContractCorrection(target='checks', task_id='T3', index=1, previous=b,
            replacement=replacement, reason='Change B', derived_decision='Derived expression',
            preserved_obligations=['Keep business assertions'], evidence_refs=['failure'])],
        evidence_contract=old.evidence_contract.model_copy(update={'obligations': [row.model_copy(
            update={'value': replacement, 'origin': 'model_derived'})]}))
    target = {'target': 'checks', 'task_id': 'T3', 'index': 1, 'value_hash': digest(b.model_dump())}
    if binding == 'different_target':
        target.update(index=0, value_hash=digest(a.model_dump()))
    elif binding == 'different_value':
        target['value_hash'] = digest(replacement.model_dump())
    request = {'owner': 'plan', 'previous': old.model_dump(), 'clauses': [source_text(a.model_dump())],
               'mapped_clauses': [] if binding == 'missing' else [target]}
    def apply():
        return apply_patch(old, patch, 1, {failure().id}, {t.id: t.origin_id for t in old.tasks},
                           source_correction=request)
    if binding == 'correct':
        result, _, _ = apply()
        assert result.evidence_contract.obligations[0].origin == 'model_derived'
    else:
        with pytest.raises(AA2Error, match='reattribute'):
            apply()
