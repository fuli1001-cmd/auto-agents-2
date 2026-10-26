"""Explicit structured responses used by test agents, not a controller fallback."""
from authority_review_fixtures import authority_review

from auto_agents_2.evidence_admission import empty_contract
from auto_agents_2.models import EvidenceAssessment, EvidencePlanReview
from auto_agents_2.plan_revision import EvidencePatchReview
from auto_agents_2.util import digest


def evidence_review(context, review=None, **changes):
    base = review.model_dump() if review is not None else {"approved": True, "summary": "fixture semantic assessment", "findings": []}
    if context.get("controller_review_basis") and not base.get("authority"):
        base.update(authority_review(context, **{k: v for k, v in base.items()
                    if k in {"approved", "summary", "findings"}}).model_dump())
    assessment = dict(candidate_hash=digest(context["plan"]),
        catalog_hash=context["controller_evidence_capabilities"]["hash"],
        source_binding=context["evidence_requirement_source"]["hash"],
        mapping_complete=True, origins_accurate=True, capabilities_sufficient=True,
        requirements_preserved=True, explanation="Fixture explicitly maps no controller evidence obligations.")
    if context.get("controller_check_production"):
        from auto_agents_2.models import (
            CheckProducer,
            CheckProductionAssessment,
            CheckProductionBinding,
            PlanResult,
        )
        plan = PlanResult.model_validate(context["plan"])
        # Scenario fixture declaration, not a controller fallback. General legacy
        # scenarios use semantic acceptance; dedicated closure tests supply the
        # command-backed classifications and invalid cases explicitly.
        files = {(r.task_id, r.index) for r in plan.evidence_contract.obligations
                 if r.target == "acceptance" and r.capability == "test_file_attachments"} if plan.evidence_contract else set()
        rows = [CheckProductionBinding(target="acceptance", task_id=t.id, index=i, value=v,
            kind="attachments" if (t.id, i) in files else "review",
            producers=[CheckProducer(index=0, command=t.checks[0])] if (t.id, i) in files else [],
            reason="Fixture semantic/source assertion; this scenario does not claim command closure")
            for t in plan.tasks for i, v in enumerate(t.acceptance)]
        rows.extend(CheckProductionBinding(target="final_checks", index=i, value=c, kind="command",
            producers=[CheckProducer(index=i, command=c)], reason="Explicit final execution")
            for i, c in enumerate(plan.final_checks))
        assessment["check_production"] = CheckProductionAssessment(bindings=rows)
    assessment.update(changes)
    basis = context.get("controller_review_basis")
    if basis and basis["unsupported"]:
        # The scenario agent must report the real declared gap under the new
        # protocol. Do not create a user-source claim unless this fixture's
        # explicit unsupported_user_obligations says to test that claim.
        base.update(authority_review(context, approved=False, summary=base["summary"]).model_dump())
        for item in base["authority"]["coverage"]:
            if item["criterion_id"] == base["authority"]["findings"][0]["criterion_id"]:
                item["status"] = "unverified"
        base["authority"]["findings"][0]["kind"] = "evidence_gap"
        confirmed = set(assessment.get("unsupported_user_obligations", []))
        for row in context["plan"]["evidence_contract"]["obligations"]:
            if row["id"] not in basis["unsupported"]:
                continue
            from auto_agents_2.review_authority import source_text
            base["authority"]["obligations"].append({"obligation_id": row["id"],
                "goal_ids": ["input:goals"], "method_source": "input" if row["id"] in confirmed else "candidate",
                "method_quote": basis["sources"]["input"]["text"] if row["id"] in confirmed else source_text(row["value"]),
                "classification": "explicit_input_method" if row["id"] in confirmed else "unresolved",
                "disposition": "genuine_gap" if row["id"] in confirmed else "unresolved", "owner": "plan",
                "input_quote": basis["sources"]["input"]["text"] if row["id"] in confirmed else "",
                "preserve_ids": ["input:goals"], "reason": "Explicit scenario source assessment"})
    schema = EvidencePatchReview if context.get('review_kind') == 'plan_patch' else EvidencePlanReview
    return schema(**base, evidence_assessment=EvidenceAssessment(**assessment))


def evidence_plan(plan, context):
    return plan.model_copy(update={"evidence_contract": empty_contract(context)}, deep=True)


def seed_requirements(workflow, rid, plan):
    """Retain an explicit input for tests starting after the normal intake phase."""
    artifact = workflow.artifacts.put('Fixture: retain all accepted business assertions.', 'original-input')
    workflow.store.checkpoints(rid, {
        'capture': {'artifact': artifact},
        'clarify': {'document': 'Fixture original goals', 'requirements': [
            {'id': key, 'description': key, 'acceptance': ['Retain original assertions']}
            for key in sorted({r for task in plan.tasks for r in task.requirement_ids})]}})


def seed_check_production(workflow, rid, plan):
    """Explicit current-protocol admission for tests starting after plan acceptance.

    Historical migration tests intentionally omit this receipt. This does not
    bypass production validators or create runtime observations.
    """
    from auto_agents_2.check_production import activation_updates
    from auto_agents_2.evidence_admission import (
        admission_context,
        admission_record,
        validate_admission_review,
    )
    context = admission_context(lambda key: workflow.store.get(rid, key))
    if plan.evidence_contract is None:
        plan.evidence_contract = empty_contract(context)
        workflow.store.checkpoint(rid, "plan", plan.model_dump())
    context.update(review_kind="plan", plan=plan.model_dump())
    review = evidence_review(context)
    validate_admission_review(plan, review, context)
    workflow.store.checkpoints(rid, activation_updates(admission_record(plan, review, context)))
