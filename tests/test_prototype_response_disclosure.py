"""Prototype review disclosures preserve strict gates and accumulated recovery history."""
import copy

import pytest
from prototype_review_fixtures import prototype_verdict
from test_prototype_review_authority import review_fixture
from test_review_schema_recovery import native_review, requests

from auto_agents_2.engine import Replan
from auto_agents_2.errors import RecoveryPathExhausted
from auto_agents_2.prototype_review import (
    InvalidPrototypeReview,
    PrototypeFinding,
    PrototypeReview,
    validate_review,
)
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash

OLD_RULE = "Custom duration always requires a second confirmation."
NEW_RULE = "Custom duration within the estimate starts after the first confirmation."


def amended_review(tmp_path, monkeypatch):
    workflow, rid, ctx, _, _, _ = review_fixture(tmp_path, monkeypatch)
    workflow.store.checkpoint(rid, "clarify", {"document": OLD_RULE, "requirements": []})
    question = workflow.store.request(rid, "prototype-feedback", "Prototype feedback", "rejected-version")
    workflow.store.answer(question["id"], NEW_RULE)
    ctx = workflow._prototype_review_context(rid, tmp_path, ctx)
    finding = PrototypeFinding(
        criterion_id="specification:document", kind="evidence_gap", severity="blocking",
        owner="clarify", requested_capability="none", source_quote=OLD_RULE,
        description="Accepted specification has not incorporated the submitted amendment",
        location="Accepted specification and current submitted feedback",
        evidence="The retained specification requires the step removed by the new explicit input",
        evidence_refs=["requirements", "candidate"],
        observation="The current specification and the explicit submitted answer conflict",
        resolution="Revise and independently review the specification, preserving other obligations",
    )
    return workflow, rid, ctx, prototype_verdict(ctx, approved=False, findings=[finding])


def test_explicit_amendment_can_return_to_specification_without_product_edit_or_approval(tmp_path, monkeypatch):
    workflow, rid, ctx, review = amended_review(tmp_path, monkeypatch)
    before = tree_hash(tmp_path)
    approval = copy.deepcopy(workflow.store.get(rid, "prototype_approval"))
    assert validate_review(review, ctx["prototype_review_basis"], ctx["prototype_requirement_source"]) == review
    with pytest.raises(Replan) as exc:
        workflow._prototype_rejection(review)
    assert exc.value.stage == "clarify"
    assert tree_hash(tmp_path) == before
    assert workflow.store.get(rid, "prototype_approval") == approval
    assert workflow.store.submitted_answers(rid)[-1]["answer"] == NEW_RULE
    assert not workflow.store.pending(rid)


@pytest.mark.parametrize("mutation,code", [
    ("upstream_defect", "defect_does_not_grant_new_capability"),
    ("orphaned_coverage", "verdict_requires_grounded_findings"),
    ("wrong_quote_origin", "source_gap_requires_original_clause"),
    ("unchecked_pass", "unresolved_criterion_cannot_pass"),
])
def test_disclosure_never_repairs_or_accepts_invalid_verdict(tmp_path, monkeypatch, mutation, code):
    _, _, ctx, review = amended_review(tmp_path, monkeypatch)
    value = review.model_dump()
    if mutation == "upstream_defect":
        value["findings"][0]["kind"] = "defect"
        next(row for row in value["coverage"] if row["criterion_id"] == "specification:document")["status"] = "violated"
    elif mutation == "orphaned_coverage":
        value["findings"] = []
    elif mutation == "wrong_quote_origin":
        value["findings"][0]["source_quote"] = NEW_RULE
    elif mutation == "unchecked_pass":
        value["approved"] = True
        value["findings"] = []
    before = copy.deepcopy(value)
    with pytest.raises(InvalidPrototypeReview, match=code):
        validate_review(value, ctx["prototype_review_basis"], ctx["prototype_requirement_source"])
    assert value == before


async def test_native_prototype_review_receives_full_coverage_and_source_ownership_rules(tmp_path, monkeypatch):
    workflow, rid, stage, ctx, schema, observed, _ = native_review(
        tmp_path, monkeypatch, "prototype", ["valid"])
    binary = workflow.store.root / "native-review-fixture"
    script = binary.read_text()
    marker = 'else:\n    raw='
    assert script.count(marker) == 1
    script = script.replace(marker, '''else:
    assert "Every violated or unverified criterion" in schema["properties"]["coverage"]["description"]
    finding = schema["$defs"]["PrototypeFinding"]["properties"]
    assert "explicit submitted-answer/accepted-specification conflict" in finding["owner"]["description"]
    assert "same criterion" in finding["source_quote"]["description"]
    assert "must not leave an orphaned coverage row" in wire
    assert "does not itself ask the user" in wire
    raw=''')
    binary.write_text(script)
    source = tree_hash(tmp_path)
    result = await workflow._invoke(stage, ctx, schema, tmp_path, rid, "disclosed-prototype", fresh=True)
    assert result.approved and len(observed) == 1
    assert tree_hash(tmp_path) == source
    assert workflow.store.get(rid, "prototype_approval") is None


async def test_updated_schema_disclosure_retains_exhausted_request_and_cumulative_count(tmp_path, monkeypatch):
    workflow, rid, stage, ctx, schema, observed, _ = native_review(
        tmp_path, monkeypatch, "prototype", ["missing", "missing", "valid"])
    current_schema = PrototypeReview.model_json_schema

    def without_descriptions(value):
        value = copy.deepcopy(value)
        for field in ("coverage", "findings"):
            value["properties"][field].pop("description", None)
        finding = value["$defs"]["PrototypeFinding"]["properties"]
        for field in ("criterion_id", "kind", "owner", "source_quote"):
            finding[field].pop("description", None)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(PrototypeReview, "model_json_schema", classmethod(
            lambda cls, *args, **kwargs: without_descriptions(current_schema(*args, **kwargs))))
        with pytest.raises(RecoveryPathExhausted):
            await workflow._invoke(stage, ctx, schema, tmp_path, rid, "bounded-prototype", fresh=True)
        old, = requests(workflow, rid)
        with pytest.raises(RecoveryPathExhausted):
            await workflow._invoke(stage, ctx, schema, tmp_path, rid, "bounded-prototype", fresh=True)
        assert len(observed) == 2
    result = await workflow._invoke(stage, ctx, schema, tmp_path, rid, "bounded-prototype", fresh=True)
    assert result.approved and len(observed) == 3
    assert old in requests(workflow, rid)
    assert old["authority_rejections"] == 2 and old["phase"] == "rejected"
    counter = "proposal-recovery/count/proposal/" + digest([stage, "bounded-prototype"])
    assert workflow.store.get(rid, counter) == 3
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert workflow.store.get(rid, "prototype_approval") is None
