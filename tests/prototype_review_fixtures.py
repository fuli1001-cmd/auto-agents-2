"""Explicit model responses for the finite prototype reviewer schema in tests."""
from auto_agents_2.prototype_review import PrototypeCoverage, PrototypeFinding, PrototypeReview


def prototype_verdict(context, *, approved=True, summary="Prototype reviewed", findings=None):
    basis = context["prototype_review_basis"]
    if findings is None:
        findings = [] if approved else [PrototypeFinding(
            severity="major", location="design/prototypes/index.html", description=summary,
            evidence="Current prototype source", resolution="Repair the evidenced UI within prototype scope",
            criterion_id="stage:prototype", kind="defect", observation=summary,
            evidence_refs=["candidate"], owner="prototype", requested_capability="none")]
    unresolved = {f.criterion_id: "violated" if f.kind == "defect" else "unverified"
                  for f in findings if f.kind != "advisory"}
    coverage = [PrototypeCoverage(criterion_id=row["id"], status=unresolved.get(row["id"], "satisfied"),
        rationale="Fixture independently inspected the current source and browser observations",
        evidence_refs=["candidate"]) for row in basis["criteria"]]
    return PrototypeReview(approved=approved, summary=summary, basis_hash=basis["hash"],
        candidate_hash=basis["candidate_hash"], coverage=coverage, findings=findings)
