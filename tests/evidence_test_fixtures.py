"""Explicit structured responses used by test agents, not a controller fallback."""
from auto_agents_2.evidence_admission import empty_contract
from auto_agents_2.models import EvidenceAssessment, EvidencePlanReview
from auto_agents_2.plan_revision import EvidencePatchReview
from auto_agents_2.util import digest


def evidence_review(context, review=None, **changes):
    base = review.model_dump() if review is not None else {"approved": True, "summary": "fixture semantic assessment", "findings": []}
    assessment = dict(candidate_hash=digest(context["plan"]),
        catalog_hash=context["controller_evidence_capabilities"]["hash"],
        source_binding=context["evidence_requirement_source"]["hash"],
        mapping_complete=True, origins_accurate=True, capabilities_sufficient=True,
        requirements_preserved=True, explanation="Fixture explicitly maps no controller evidence obligations.")
    assessment.update(changes)
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
