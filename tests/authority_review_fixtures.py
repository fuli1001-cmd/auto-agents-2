"""Explicit responses for existing scenario agents; never a production fallback.

These fixtures model a reviewer accepting the simple test scenario or reporting
its deliberately injected stage defect. Source-dispute tests build their own
source-specific records instead of inferring semantic entailment here.
"""
from auto_agents_2.models import ReviewResult
from auto_agents_2.review_authority import KINDS, AuthorityAssessment


def authority_review(context, review=None, **changes):
    base = review.model_dump() if review is not None else {"approved": True, "summary": "fixture assessment", "findings": []}
    base.update(changes)
    basis = context.get("controller_review_basis")
    if basis is None:
        return ReviewResult(**base)  # Unit fixtures that deliberately exercise the legacy persisted format.
    if not base["approved"] and not base["findings"]:
        base["findings"] = [{"severity": "major", "location": basis["scope"],
            "description": base["summary"], "evidence": "Deliberate test scenario candidate",
            "resolution": "Correct the scenario's rejected stage contract"}]
    target = next((row for row in basis["criteria"] if row["kind"] != "controller_rule"), basis["criteria"][0])
    stage = target["id"]
    coverage = [{"criterion_id": row["id"], "status": "violated" if row["id"] == stage and not base["approved"]
                 else "satisfied", "reason": "Explicit scenario fixture assessment"} for row in basis["criteria"]]
    findings = [{"finding_index": index, "criterion_id": stage,
                 "kind": "advisory" if base["approved"] and row["severity"] == "minor" else "defect",
                 "owner": KINDS[basis["scope"]][0], "observation_refs": ["candidate"],
                 "source_quote": target["statement"],
                 "rationale": "The test scenario injects this defect at the reviewed stage"}
                for index, row in enumerate(base["findings"])]
    base["authority"] = {"version": 1, "basis_hash": basis["hash"], "candidate_hash": basis["candidate_hash"],
        "coverage": coverage, "findings": findings, "choices_complete": True,
        "requirements_preserved": True, "explanation": "Explicit fixture response, not an inferred production approval"}
    base["authority"] = AuthorityAssessment.model_validate(base["authority"]).model_dump()
    return ReviewResult(**base)


def seed_original(workflow, rid):
    """Explicit input for subsystem tests admitted after the normal intake gate."""
    if not workflow.store.get(rid, "capture"):
        artifact = workflow.artifacts.put(
            "Fixture input: preserve accepted business behavior, existing tests, and the stated task contracts.",
            "original-input")
        workflow.store.checkpoint(rid, "capture", {"artifact": artifact, "hash": artifact["hash"]})


def recorded_review(workflow, rid, raw, *, work=None, kind="verify"):
    """A scenario's fake independent verdict, with the real controller receipt."""
    from auto_agents_2.models import ReviewResult
    seed_original(workflow, rid)
    work = work or workflow.project
    context = {"review_kind": kind, "plan": workflow.store.get(rid, "plan"),
               "project": workflow.contexts.build(work)}
    context, _ = workflow.authority.prepare(rid, "verify" if kind == "verify" else "review",
                                          context, ReviewResult, work, "fixture/grounded-review")
    values = raw.model_dump() if hasattr(raw, "model_dump") else raw
    result = authority_review(context, **{k: v for k, v in values.items() if k in {"approved", "summary", "findings"}})
    workflow.authority.record(rid, result, context, "fixture/grounded-review")
    return result.model_dump()


