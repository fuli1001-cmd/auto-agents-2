"""Source-governed full rebuilds, independent of the latest author feedback."""
from copy import deepcopy

import jsonschema
import pytest
from authority_review_fixtures import authority_review
from evidence_test_fixtures import evidence_plan, evidence_review, seed_check_production
from test_decisions import setup
from test_recovery_contracts import plan

from auto_agents_2.contract_reentry import (
    ACTIVE,
    ROOT,
    author_context,
    make_revision,
    state,
)
from auto_agents_2.evidence_admission import admission_context, admission_record
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import EvidencePlanReview, ReviewResult, strict_schema
from auto_agents_2.review_authority import InvalidAuthorityReview, review_schema, validate_review
from auto_agents_2.util import digest


def fixture(tmp_path, stage="clarify", *, command_backed=False):
    workflow, rid = setup(tmp_path)
    old = evidence_plan(plan(), admission_context(lambda k: workflow.store.get(rid, k))).model_dump()
    workflow.store.checkpoints(rid, {"plan": old, "plan_revision/current": {"version": 1, "revision": 5},
        "rounds/T1": {"round": 9}, "prototype_approval": {"artifact": "accepted-ui"}})
    seed_check_production(workflow, rid, type(plan()).model_validate(old))
    if command_backed:
        from auto_agents_2.check_production import activation_updates
        from auto_agents_2.evidence_admission import validate_admission_review
        from auto_agents_2.models import CheckProducer, Command
        context = {**admission_context(lambda k: workflow.store.get(rid, k)), "plan": old}
        reviewed = evidence_review(context)
        binding = reviewed.evidence_assessment.check_production.bindings[0]
        binding.kind = "tests"
        binding.producers = [CheckProducer(index=0, command=Command.model_validate(old["tasks"][0]["checks"][0]))]
        validate_admission_review(type(plan()).model_validate(old), reviewed, context)
        workflow.store.checkpoints(rid, activation_updates(admission_record(type(plan()).model_validate(old), reviewed, context)))
    workflow.store.update(rid, replans=26)
    rollback(workflow, rid, stage)
    return workflow, rid, old


def rollback(workflow, rid, stage):
    from auto_agents_2.stage_responsibilities import GATES, correction_request
    pending = workflow.store.prepare_replan(rid, stage, "Authorized owner correction fixture")
    request = correction_request(workflow.store, rid, action="plan_patch" if stage == "plan" else stage,
        reason=pending["reason"], evidence_refs=["replanning/pending"])
    workflow.store.checkpoint(rid, "replanning/pending", {**pending, "granted": True, "correction_request": request})
    workflow.store.complete_replan(rid, GATES[GATES.index(stage):], [])


def review_for(workflow, rid, owner, document):
    context = {"assessment" if owner == "clarify" else "plan": document}
    if owner == "plan":
        context.update(admission_context(lambda key: workflow.store.get(rid, key)))
        context.update(review_kind="plan", evidence_candidate_hash=digest(document))
    context, schema = workflow.authority.prepare(rid, "coverage_review" if owner == "clarify" else "review",
        context, ReviewResult if owner == "clarify" else EvidencePlanReview, workflow.project, "rebuild-" + owner)
    result = authority_review(context)
    result.authority["revision_dispositions"] = []
    return context, evidence_review(context, result) if owner == "plan" else result


def replacement(context, *, field=None, operation="replace", source="owner_reentry"):
    revision = context["controller_review_basis"]["revision"]
    old = [key for key in revision["changed"] if field is None or revision["old"][key]["field"] == field]
    new = [key for key in revision["added"] if field is None or revision["new"][key]["field"] == field]
    return {"operation": operation, "old_ids": old, "new_ids": new, "authority": [
        {"source": source, "quote": revision["sources"][source]["text"]}], "reason": "Explicit equivalent fixture mapping"}


def accept(workflow, rid, owner, document, context, result):
    workflow.authority.record(rid, result, context, "fixture-joint-" + owner)
    updates = {owner: document, **workflow.authority.acceptance(rid, owner, document, result)}
    if owner == "plan":
        from auto_agents_2.check_production import activation_updates
        record = admission_record(type(plan()).model_validate(document), result, context)
        workflow.store.checkpoint(rid, "evidence_admission/initial", record)
        updates.update(activation_updates(record))
    workflow.authority.commit(rid, owner, document, result, updates, workflow.project)


def test_continuous_rejections_keep_the_original_plan_and_counters(tmp_path):
    workflow, rid, old = fixture(tmp_path)
    def get(key):
        return workflow.store.get(rid, key)
    first = deepcopy(state(get))
    for _ in range(3):
        workflow.store.checkpoint(rid, "candidate/plan", {"document": {"tasks": []}})
        rollback(workflow, rid, "plan")
        assert state(get) == first
        assert author_context(get)["controller_contract_reentry"]["plan"] == old
    assert get("replan_feedback").get("preserved_plan") is None
    assert get("rounds/T1") == {"round": 9}
    assert get("prototype_approval") == {"artifact": "accepted-ui"}
    assert workflow.store.run(rid)["data"]["replans"] == 30


def test_repeated_reset_commit_is_idempotent(tmp_path):
    workflow, rid, _ = fixture(tmp_path)
    binding = workflow.store.get(rid, ACTIVE)
    workflow.store.complete_replan(rid, ["plan"], [])
    assert workflow.store.get(rid, ACTIVE) == binding
    assert workflow.store.run(rid)["data"]["replans"] == 27


def test_newly_accepted_spec_survives_another_clarify_reset(tmp_path):
    workflow, rid, _ = fixture(tmp_path)
    def get(key):
        return workflow.store.get(rid, key)
    old = state(get)["specification"]
    new = deepcopy(old)
    new["requirements"].append({"id": "NEW", "description": "User's newly requested export", "acceptance": ["Export safely"]})
    context, review = review_for(workflow, rid, "clarify", new)
    accept(workflow, rid, "clarify", new, context, review)
    head = get(ACTIVE)["head"]
    rollback(workflow, rid, "clarify")
    assert state(get)["specification"] == new
    assert get(ACTIVE)["head"] == head
    assert state(get)["base"]["specification"] == old
    candidate = deepcopy(new)
    candidate["requirements"].pop()
    context, review = review_for(workflow, rid, "clarify", candidate)
    with pytest.raises(InvalidAuthorityReview, match="complete_dispositions"):
        validate_review(review, context["controller_review_basis"])


@pytest.mark.parametrize("owner,field", [("clarify", "acceptance"), ("plan", "acceptance"),
    ("plan", "checks"), ("plan", "final_checks"), ("plan", "runtime_probes"), ("plan", "task_links")])
def test_omission_or_wrong_change_requires_exact_disposition(tmp_path, owner, field):
    workflow, rid, old = fixture(tmp_path, "clarify" if owner == "clarify" else "design")
    if owner == "clarify":
        new = deepcopy(state(lambda k: workflow.store.get(rid, k))["specification"])
        new["requirements"][0]["acceptance"][0] = "Dropped actual assertion"
    else:
        new = deepcopy(old)
        if field == "acceptance":
            new["tasks"][0][field] = ["An unrelated assertion"]
        elif field == "checks":
            new["tasks"][0][field][0]["argv"] = ["python", "-c", "pass"]
        elif field == "task_links":
            new["tasks"][0]["requirement_ids"] = ["R2"]
        elif field == "final_checks":
            new[field][0]["argv"] = ["python", "-c", "pass"]
        else:
            new[field] = []
    context, review = review_for(workflow, rid, owner, new)
    with pytest.raises(InvalidAuthorityReview, match="complete_dispositions"):
        validate_review(review, context["controller_review_basis"])


@pytest.mark.parametrize("kind", ["same_quote_wrong_slot", "stale", "duplicate", "wrong_kind",
                                   "null", "unknown", "no_source", "permit_retire"])
def test_closed_mapping_rejects_forged_or_ambiguous_input(tmp_path, kind):
    workflow, rid, _ = fixture(tmp_path)
    new = deepcopy(state(lambda k: workflow.store.get(rid, k))["specification"])
    new["requirements"][0]["acceptance"][0] += " with refresh"
    context, review = review_for(workflow, rid, "clarify", new)
    row = replacement(context, field="acceptance")
    if kind == "same_quote_wrong_slot":
        row["old_ids"] = [next(iter(context["controller_review_basis"]["revision"]["kept"]))]
    elif kind == "stale":
        row["old_ids"] = ["old"]
    elif kind == "duplicate":
        review.authority["revision_dispositions"] = [row, row]
    elif kind == "wrong_kind":
        row["operation"] = "merge"
    elif kind == "null":
        row["new_ids"] = None
    elif kind == "unknown":
        row["made_up"] = 1
    elif kind == "no_source":
        row["authority"][0]["source"] = "imaginary-answer"
    elif kind == "permit_retire":
        row.update(operation="retire", new_ids=[])
    if kind != "duplicate":
        review.authority["revision_dispositions"] = [row]
    with pytest.raises((InvalidAuthorityReview, ValueError)):
        validate_review(review, context["controller_review_basis"])


@pytest.mark.parametrize("mode", ["replace", "split", "merge"])
def test_legitimate_equivalent_clause_mapping_is_possible(tmp_path, tmp_path_factory, mode):
    workflow, rid, _ = fixture(tmp_path)
    new = deepcopy(state(lambda k: workflow.store.get(rid, k))["specification"])
    if mode == "merge":
        old = state(lambda k: workflow.store.get(rid, k))["specification"]
        old["requirements"][0]["acceptance"].append("Also refresh")
        # Prepare the fixture's distinct accepted baseline, not a production rewrite.
        workflow2, rid2 = setup(tmp_path_factory.mktemp("merge-contract"))
        workflow2.store.checkpoint(rid2, "clarify", old)
        rollback(workflow2, rid2, "clarify")
        workflow, rid = workflow2, rid2
        new = deepcopy(old)
        new["requirements"][0]["acceptance"] = ["Both original assertions and refresh"]
    else:
        new["requirements"][0]["acceptance"] = ["Original assertion and refreshed state"] if mode == "replace" else [
            "Original assertion", "Refreshed state"]
    context, review = review_for(workflow, rid, "clarify", new)
    review.authority["revision_dispositions"] = [replacement(context, field="acceptance", operation=mode)]
    validate_review(review, context["controller_review_basis"])
    accept(workflow, rid, "clarify", new, context, review)
    assert state(lambda k: workflow.store.get(rid, k))["specification"] == new


@pytest.mark.parametrize("field", ["expected_tests", "inputs", "outputs"])
def test_mapped_check_cannot_reduce_structural_minima(tmp_path, field):
    workflow, rid = setup(tmp_path)
    old = plan().model_dump()
    old["tasks"][0]["checks"][0].update(expected_tests=4, inputs=["schema.json"], outputs=["dist"])
    workflow.store.checkpoint(rid, "plan", old)
    rollback(workflow, rid, "design")
    new = deepcopy(old)
    new["tasks"][0]["checks"][0][field] = 0 if field == "expected_tests" else []
    context, review = review_for(workflow, rid, "plan", new)
    review.authority["revision_dispositions"] = [replacement(context, field="checks")]
    with pytest.raises(InvalidAuthorityReview, match="check_minima"):
        validate_review(review, context["controller_review_basis"])


def test_wrong_requirement_association_cannot_pass_a_mapped_rename(tmp_path):
    workflow, rid, old = fixture(tmp_path, "design")
    new = deepcopy(old)
    new["tasks"][0]["requirement_ids"] = ["R2"]
    context, review = review_for(workflow, rid, "plan", new)
    review.authority["revision_dispositions"] = [replacement(context, field="task_links")]
    with pytest.raises(InvalidAuthorityReview, match="requirement_association"):
        validate_review(review, context["controller_review_basis"])


def test_raw_answer_only_retires_exact_clarify_items_after_independent_mapping(tmp_path):
    workflow, rid, old = fixture(tmp_path)
    original = state(lambda k: workflow.store.get(rid, k))["specification"]
    q = workflow.store.request(rid, "clarify/0/0", "Change requested?", "bound-question")
    workflow.store.answer(q["id"], "Remove requirement R1 and its acceptance.")
    new = deepcopy(original)
    new["requirements"] = [r for r in new["requirements"] if r["id"] != "R1"]
    context, review = review_for(workflow, rid, "clarify", new)
    key = "answer:" + q["id"]
    review.authority["revision_dispositions"] = [replacement(context, operation="retire", source=key)]
    # A retirement has no successor; unrelated new rows cannot be smuggled into it.
    review.authority["revision_dispositions"][0]["new_ids"] = []
    validate_review(review, context["controller_review_basis"])
    revision = make_revision(lambda k: workflow.store.get(rid, k), "plan", old,
        workflow.store.submitted_answers(rid), None)
    assert key not in revision["sources"]
    accept(workflow, rid, "clarify", new, context, review)
    plan_context, _ = review_for(workflow, rid, "plan", old)
    assert any(k.startswith("accepted_spec:") for k in plan_context["controller_review_basis"]["revision"]["sources"])
    rollback(workflow, rid, "clarify")
    assert "R1" not in {r["id"] for r in state(lambda k: workflow.store.get(rid, k))["specification"]["requirements"]}


def test_stale_review_cannot_move_head_or_overwrite_accepted_norms(tmp_path):
    workflow, rid, _ = fixture(tmp_path)
    new = deepcopy(state(lambda k: workflow.store.get(rid, k))["specification"])
    context, review = review_for(workflow, rid, "clarify", new)
    workflow.authority.record(rid, review, context, "fixture-joint-clarify")
    updates = {"clarify": new, **workflow.authority.acceptance(rid, "clarify", new, review)}
    before = workflow.store.get(rid, ACTIVE)
    q = workflow.store.request(rid, "clarify/0/0", "Extra scope?", "new-question")
    workflow.store.answer(q["id"], "Preserve original scope")
    with pytest.raises(StateConflict, match="sources changed"):
        workflow.store.accept_contract(rid, "clarify", new, review.model_dump(), updates)
    assert workflow.store.get(rid, ACTIVE) == before
    assert workflow.store.get(rid, "clarify") is None


@pytest.mark.parametrize("damage", ["base", "history", "head"])
def test_corrupt_or_missing_sources_fail_closed(tmp_path, damage):
    workflow, rid, _ = fixture(tmp_path)
    binding = workflow.store.get(rid, ACTIVE)
    if damage == "base":
        with pytest.raises(StateConflict, match="immutable"):
            workflow.store.checkpoint(rid, binding["base"], {"wrong": True})
        return
    if damage == "history":
        baseline = workflow.store.get(rid, binding["base"])
        workflow.store.checkpoint(rid, baseline["history_key"], {"wrong": True})
    else:
        workflow.store.checkpoint(rid, ACTIVE, {**binding, "head": ROOT + "missing/accepted"})
    with pytest.raises(StateConflict):
        state(lambda k: workflow.store.get(rid, k))


def test_published_native_schema_and_legacy_schema_are_distinct_and_closed(tmp_path):
    workflow, rid, _ = fixture(tmp_path)
    new = state(lambda k: workflow.store.get(rid, k))["specification"]
    context, review = review_for(workflow, rid, "clarify", new)
    strict = strict_schema(review_schema(ReviewResult, True))
    jsonschema.validate(review.model_dump(), strict)
    assert "revision_dispositions" not in str(strict_schema(review_schema(ReviewResult)))
    for changed in ("missing", "null", "unknown"):
        bad = deepcopy(review.model_dump())
        if changed == "missing":
            bad["authority"].pop("revision_dispositions")
        elif changed == "null":
            bad["authority"]["revision_dispositions"] = None
        else:
            bad["authority"]["unexpected"] = "no"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, strict)


async def test_no_changes_cannot_bypass_open_rebuild(tmp_path):
    workflow, rid, _ = fixture(tmp_path)
    with pytest.raises(StateConflict, match="shortcut"):
        await workflow._finish_no_changes(rid, workflow.project, "current-baseline")

def test_rebuilt_plan_acceptance_closes_chain_atomically_and_replays(tmp_path):
    workflow, rid, old = fixture(tmp_path, "design")
    new = deepcopy(old)
    new["tasks"][0]["allowed_paths"].append("extra.py")
    new["tasks"][1]["depends_on"] = []
    context, review = review_for(workflow, rid, "plan", new)
    assert not context["controller_review_basis"]["revision"]["changed"]
    accept(workflow, rid, "plan", new, context, review)
    assert workflow.store.get(rid, ACTIVE) is None
    assert workflow.store.get(rid, "plan") == new
    # Production invocation will reuse the stage checkpoint; the transaction also
    # recognizes an exact already-committed record without appending receipts.
    updates = {"plan": new, "authority/accepted/plan": workflow.store.get(rid, "authority/accepted/plan")}
    workflow.store.accept_contract(rid, "plan", new, review.model_dump(), updates)
    assert workflow.store.run(rid)["data"]["replans"] == 27
    assert workflow.store.get(rid, "rounds/T1") == {"round": 9}


def test_two_spec_candidates_cannot_both_consume_the_same_head(tmp_path):
    workflow, rid, _ = fixture(tmp_path)
    first = deepcopy(state(lambda k: workflow.store.get(rid, k))["specification"])
    second = deepcopy(first)
    second["requirements"].append({"id": "X", "description": "extra", "acceptance": ["extra"]})
    context1, review1 = review_for(workflow, rid, "clarify", first)
    context2, review2 = review_for(workflow, rid, "clarify", second)
    workflow.authority.record(rid, review2, context2, "second")
    updates2 = {"clarify": second, **workflow.authority.acceptance(rid, "clarify", second, review2)}
    accept(workflow, rid, "clarify", first, context1, review1)
    with pytest.raises(StateConflict):
        workflow.store.accept_contract(rid, "clarify", second, review2.model_dump(), updates2)
    assert workflow.store.get(rid, "clarify") == first


@pytest.mark.parametrize("second_owner", ["plan", "clarify"])
def test_closed_chain_rejects_a_second_different_acceptance(tmp_path, second_owner):
    workflow, rid, old = fixture(tmp_path, "design")
    first = deepcopy(old)
    first["document"] = "first"
    context1, review1 = review_for(workflow, rid, "plan", first)
    second = deepcopy(old) if second_owner == "plan" else deepcopy(workflow.store.get(rid, "clarify"))
    second["document"] = "other candidate"
    context2, review2 = review_for(workflow, rid, second_owner, second)
    workflow.authority.record(rid, review2, context2, "second-independent")
    updates = {second_owner: second, **workflow.authority.acceptance(rid, second_owner, second, review2)}
    accept(workflow, rid, "plan", first, context1, review1)
    with pytest.raises(StateConflict, match="closed"):
        workflow.store.accept_contract(rid, second_owner, second, review2.model_dump(), updates)
    assert workflow.store.get(rid, "plan") == first


@pytest.mark.parametrize("mixed", ["record", "activation"])
def test_rebuild_cannot_commit_another_candidates_admission(tmp_path, mixed):
    from auto_agents_2.check_production import activation_updates
    workflow, rid, old = fixture(tmp_path, "design")
    first, second = deepcopy(old), deepcopy(old)
    second["document"] = "other"
    context, review = review_for(workflow, rid, "plan", first)
    context2, review2 = review_for(workflow, rid, "plan", second)
    workflow.authority.record(rid, review, context, "joint")
    record = admission_record(type(plan()).model_validate(first), review, context)
    other = admission_record(type(plan()).model_validate(second), review2, context2)
    workflow.store.checkpoint(rid, "evidence_admission/initial", other if mixed == "record" else record)
    updates = {"plan": first, **workflow.authority.acceptance(rid, "plan", first, review),
               **activation_updates(other if mixed == "activation" else record)}
    before = workflow.store.get(rid, ACTIVE)
    with pytest.raises(StateConflict, match="admission"):
        workflow.store.accept_contract(rid, "plan", first, review.model_dump(), updates)
    assert workflow.store.get(rid, ACTIVE) == before and workflow.store.get(rid, "plan") is None


def test_rebuild_preserves_real_prior_check_production_without_execution_approval(tmp_path):
    from auto_agents_2.check_production import active_receipt
    from auto_agents_2.evidence_admission import validate_admission_review
    workflow, rid, old = fixture(tmp_path, "design", command_backed=True)
    def get(k):
        return workflow.store.get(rid, k)
    # Historical mapping is a planning input; it cannot run checks without live plan.
    with pytest.raises(StateConflict):
        active_receipt(get)
    context, review = review_for(workflow, rid, "plan", old)
    assert context["accepted_check_production"]["candidate_hash"] == digest(old)
    target = review.evidence_assessment.check_production.bindings[0]
    target.kind = "review"
    target.producers = []
    target.attachment_formats = []
    with pytest.raises(Exception, match="downgrade"):
        validate_admission_review(type(plan()).model_validate(old), review, context)


def test_same_requirement_can_legitimately_withdraw_one_acceptance_leaf(tmp_path):
    workflow, rid = setup(tmp_path)
    spec = workflow.store.get(rid, "clarify")
    spec["requirements"][0]["acceptance"] = ["Optional obsolete output", "Required retained output"]
    workflow.store.checkpoint(rid, "clarify", spec)
    old = plan()
    old.tasks[0].acceptance = list(spec["requirements"][0]["acceptance"])
    old = evidence_plan(old, admission_context(lambda k: workflow.store.get(rid, k)))
    workflow.store.checkpoint(rid, "plan", old.model_dump())
    seed_check_production(workflow, rid, old)
    rollback(workflow, rid, "clarify")
    q = workflow.store.request(rid, "clarify/0/0", "Which output changes?", "scope-version")
    workflow.store.answer(q["id"], "Withdraw only Optional obsolete output; keep Required retained output.")
    new_spec = deepcopy(spec)
    new_spec["requirements"][0]["acceptance"] = ["Required retained output"]
    context, review = review_for(workflow, rid, "clarify", new_spec)
    rev = context["controller_review_basis"]["revision"]
    retired = next(k for k in rev["changed"] if rev["old"][k]["value"] == "Optional obsolete output")
    moved = next(k for k in rev["changed"] if rev["old"][k]["value"] == "Required retained output")
    new_id = next(k for k in rev["added"] if rev["new"][k]["value"] == "Required retained output")
    answer = "answer:" + q["id"]
    cite = {"source": answer, "quote": rev["sources"][answer]["text"]}
    review.authority["revision_dispositions"] = [
        {"operation": "retire", "old_ids": [retired], "new_ids": [], "authority": [cite],
         "reason": "Only the optional output was explicitly withdrawn."},
        {"operation": "replace", "old_ids": [moved], "new_ids": [new_id],
         "authority": [{"source": "owner_reentry", "quote": rev["sources"]["owner_reentry"]["text"]}],
         "reason": "Unchanged retained assertion moves index."}]
    accept(workflow, rid, "clarify", new_spec, context, review)
    new_plan = old.model_copy(deep=True)
    new_plan.tasks[0].acceptance = ["Required retained output"]
    new_plan = evidence_plan(new_plan, admission_context(lambda k: workflow.store.get(rid, k))).model_dump()
    context, review = review_for(workflow, rid, "plan", new_plan)
    rev = context["controller_review_basis"]["revision"]
    retired = next(k for k in rev["changed"] if rev["old"][k]["value"] == "Optional obsolete output")
    moved = next(k for k in rev["changed"] if rev["old"][k]["value"] == "Required retained output")
    new_id = next(k for k in rev["added"] if rev["new"][k]["value"] == "Required retained output")
    source = next(k for k, v in rev["sources"].items()
                  if v["kind"] == "accepted_spec_revision" and v["disposition"]["operation"] == "retire")
    review.authority["revision_dispositions"] = [
        {"operation": "retire", "old_ids": [retired], "new_ids": [],
         "authority": [{"source": source, "quote": rev["sources"][source]["text"]}],
         "reason": "This exact plan assertion only serves the explicitly withdrawn optional output."},
        {"operation": "replace", "old_ids": [moved], "new_ids": [new_id],
         "authority": [{"source": "owner_reentry", "quote": rev["sources"]["owner_reentry"]["text"]}],
         "reason": "The retained assertion remains exact after index movement."}]
    accept(workflow, rid, "plan", new_plan, context, review)
    assert workflow.store.get(rid, "plan")["tasks"][0]["acceptance"] == ["Required retained output"]


def legacy_records(workflow, rid):
    import json
    with workflow.store.connect() as db:
        records = {k: json.loads(v) for k, v in db.execute(
            "SELECT key,data FROM checkpoints WHERE run_id=?", (rid,))}
    return {k: v for k, v in records.items() if not k.startswith(ROOT)}


@pytest.mark.parametrize("damage", ["none", "history_gap", "plan_hash", "revision", "spec_binding"])
def test_legacy_migration_uses_exact_chronological_history(tmp_path, damage):
    from auto_agents_2.contract_reentry import legacy_baseline
    workflow, rid, old = fixture(tmp_path, "design")
    rollback(workflow, rid, "plan")
    records = legacy_records(workflow, rid)
    if damage == "history_gap":
        records.pop("recovery/gate-history/27")
    elif damage == "plan_hash":
        records["plan_revision/history/5"]["plan"]["document"] = "wrong"
    elif damage == "revision":
        records["plan_revision/current"]["revision"] = 7
    elif damage == "spec_binding":
        records["recovery/gate-history/26"]["pending"]["correction_request"]["bindings"]["clarify"] = "wrong"
    if damage != "none":
        with pytest.raises(StateConflict):
            legacy_baseline(records, rid, 28)
    else:
        updates = legacy_baseline(records, rid, 28)
        migrated = state({**records, **updates}.get)
        assert migrated["plan"] == old
        assert migrated["base"]["generation"] == 26
        assert records["rounds/T1"] == {"round": 9}


def test_legacy_intermediate_added_acceptance_requires_clarify_owner_review(tmp_path):
    from auto_agents_2.contract_reentry import legacy_baseline, needs_clarification
    workflow, rid, _ = fixture(tmp_path, "clarify")
    spec = state(lambda k: workflow.store.get(rid, k))["specification"]
    middle = deepcopy(spec)
    middle["requirements"][0]["acceptance"].append("Intermediate accepted obligation")
    workflow.store.checkpoint(rid, "clarify", middle)
    rollback(workflow, rid, "clarify")
    workflow.store.checkpoint(rid, "clarify", spec)
    records = legacy_records(workflow, rid)
    updates = legacy_baseline(records, rid, 28)
    get = {**records, **updates}.get
    assert needs_clarification(get)
    with pytest.raises(StateConflict, match="owner"):
        make_revision(get, "plan", records["plan_revision/history/5"]["plan"], [], None)
    revision = make_revision(get, "clarify", spec, [], None)
    assert any(row["value"] == "Intermediate accepted obligation" for row in revision["old"].values())
    assert revision["changed"]


@pytest.mark.parametrize("origin", ["user", "unknown"])
@pytest.mark.parametrize("formal", [False, True])
@pytest.mark.parametrize("explicit_input", [False, True])
def test_protected_evidence_origin_needs_exact_source_dispute(tmp_path, origin, formal, explicit_input):
    from auto_agents_2.engine import Replan
    from auto_agents_2.models import EvidenceObligation
    workflow, rid = setup(tmp_path)
    candidate = plan()
    candidate.tasks[0].acceptance = ["Derived extra image proof"]
    if explicit_input:
        captured = workflow.artifacts.put("I explicitly require Derived extra image proof.", "original-input")
        workflow.store.checkpoint(rid, "capture", {"artifact": captured})
    old = evidence_plan(candidate, admission_context(lambda k: workflow.store.get(rid, k)))
    old.evidence_contract.obligations.append(EvidenceObligation(id="EV1", target="acceptance", task_id="T1",
        index=0, value=old.tasks[0].acceptance[0], origin=origin, requirement_ids=["R1"],
        capability="test_file_attachments", capability_version=1, producer="VerificationService",
        production_stage="verification", required_stage="verification"))
    workflow.store.checkpoint(rid, "plan", old.model_dump())
    seed_check_production(workflow, rid, old)
    rollback(workflow, rid, "design")
    new = old.model_dump()
    new["evidence_contract"]["obligations"][0]["origin"] = "model_derived"
    context, review = review_for(workflow, rid, "plan", new)
    if formal:
        old_context, _ = review_for(workflow, rid, "plan", old.model_dump())
        basis = old_context["controller_review_basis"]
        ident = next(k for k, row in basis["revision"]["old"].items() if row["field"] == "evidence_obligation")
        source = "revision:old:" + ident
        refused = authority_review(old_context, approved=False, summary="Correct the false derived source label")
        refused.authority["revision_dispositions"] = []
        finding = refused.authority["findings"][0]
        finding.update(kind="source_dispute", owner="plan", method_source=source,
                       method_quote=basis["sources"][source]["text"])
        for row in refused.authority["coverage"]:
            if row["criterion_id"] == finding["criterion_id"]:
                row["status"] = "unverified"
        assert basis["sources"][source]["contract_target"] == {
            "target": "acceptance", "task_id": "T1", "index": 0, "value_hash": digest("Derived extra image proof")}
        metadata_quote = deepcopy(refused)
        from auto_agents_2.review_authority import source_text
        metadata_quote.authority["findings"][0]["method_quote"] = source_text(old.evidence_contract.obligations[0].model_dump())
        with pytest.raises(InvalidAuthorityReview, match="invalid_source_dispute"):
            validate_review(metadata_quote, basis)
        if explicit_input:
            with pytest.raises(InvalidAuthorityReview, match="explicit_input_clause"):
                workflow.authority.record(rid, refused, old_context, "explicit-user-source-cannot-be-waived")
            return
        workflow.authority.record(rid, refused, old_context, "exact-historical-source-dispute")
        with pytest.raises(Replan):
            workflow.authority.route_dispute(rid, refused)
        context, review = review_for(workflow, rid, "plan", new)
        row = replacement(context, field="evidence_obligation", source="dispute:0")
        request = context["controller_review_basis"]["correction"]
        from auto_agents_2.review_authority import source_text
        review.authority["amendments"] = [{"source": "previous_contract", "old_quote": text,
            "new_quote": source_text(new["evidence_contract"]["obligations"][0]),
            "preserve_ids": request["preserve_ids"], "reason": "Correct exact derived origin, preserve all goals."}
            for text in request["clauses"]]
    else:
        row = replacement(context, field="evidence_obligation")
    review.authority["revision_dispositions"] = [row]
    if formal:
        validate_review(review, context["controller_review_basis"])
    else:
        with pytest.raises(InvalidAuthorityReview, match="protected_evidence_source"):
            validate_review(review, context["controller_review_basis"])


@pytest.mark.parametrize("after_commit", [False, True])
def test_rebuild_acceptance_crash_keeps_one_exact_head(tmp_path, monkeypatch, after_commit):
    workflow, rid, _ = fixture(tmp_path)
    document = state(lambda k: workflow.store.get(rid, k))["specification"]
    context, review = review_for(workflow, rid, "clarify", document)
    workflow.authority.record(rid, review, context, "crash-review")
    updates = {"clarify": document, **workflow.authority.acceptance(rid, "clarify", document, review)}
    before = workflow.store.get(rid, ACTIVE)
    if not after_commit:
        put = workflow.store._put
        def interrupted(db, run, key, value):
            put(db, run, key, value)
            if key.endswith("/accepted"):
                raise RuntimeError("simulated transaction interruption")
        monkeypatch.setattr(workflow.store, "_put", interrupted)
        with pytest.raises(RuntimeError):
            workflow.store.accept_contract(rid, "clarify", document, review.model_dump(), updates)
        assert workflow.store.get(rid, ACTIVE) == before
        assert workflow.store.get(rid, "clarify") is None
        monkeypatch.setattr(workflow.store, "_put", put)
    workflow.store.accept_contract(rid, "clarify", document, review.model_dump(), updates)
    head = workflow.store.get(rid, ACTIVE)
    # An interrupted caller must resume via accepted clarify checkpoint, not re-accept.
    assert workflow.store.get(rid, "clarify") == document
    assert len(state(lambda k: workflow.store.get(rid, k))["receipts"]) == 1
    workflow.store.accept_contract(rid, "clarify", document, review.model_dump(), updates)
    assert workflow.store.get(rid, ACTIVE) == head
    assert head["base"] == before["base"]
    assert workflow.store.run(rid)["data"]["replans"] == 27


def test_large_joint_review_keeps_exact_revision_and_history_materials(tmp_path):
    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.review_authority import valid_basis
    workflow, rid = setup(tmp_path)
    spec = workflow.store.get(rid, "clarify")
    spec["requirements"] = [{"id": f"L{i}", "description": "Precise original goal " * 30,
        "acceptance": [f"Condition {i}: " + "Preserve every accepted assertion. " * 25]} for i in range(90)]
    workflow.store.checkpoint(rid, "clarify", spec)
    rollback(workflow, rid, "clarify")
    context, review = review_for(workflow, rid, "clarify", spec)
    context.update(author_context(lambda k: workflow.store.get(rid, k)))
    prepared = ContextPlanner(workflow.artifacts).prepare("coverage_review", context, workflow.project, run_id=rid)
    view = prepared.payload["controller_review_basis"]
    rebuilt = deepcopy(view)
    for key in ("sources", "criteria", "revision"):
        pointer = view[key]
        assert "artifact" in pointer
        rebuilt[key] = workflow.artifacts.json(pointer["artifact"])
    assert valid_basis(rebuilt) and rebuilt == context["controller_review_basis"]
    assert view["revision"]["required_read"]
    assert validate_review(review, rebuilt)
    historical = prepared.payload["controller_contract_reentry"]
    assert historical["required_read"]
    assert workflow.artifacts.json(historical["artifact"]) == context["controller_contract_reentry"]


@pytest.mark.parametrize("reject_first_rebuild", [False, True])
async def test_complete_accepted_plan_rebuild_uses_normal_gates(tmp_path, monkeypatch, reject_first_rebuild):
    import asyncio

    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.engine import Replan

    class RebuildingAgent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.rebuilds = []
        async def invoke(self, stage, context, schema, *args, **kwargs):
            result = await super().invoke(stage, context, schema, *args, **kwargs)
            if stage == "plan" and context.get("controller_contract_reentry"):
                self.rebuilds.append(deepcopy(context["controller_contract_reentry"]))
                if reject_first_rebuild and len(self.rebuilds) == 1:
                    result.tasks[0].requirement_ids = ["WRONG"]
            return result

    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers")
    agent = RebuildingAgent()
    workflow = engine(project, agent)
    async def stop_after_accepted_plan(*args, **kwargs):
        raise asyncio.CancelledError()
    with monkeypatch.context() as stopped:
        stopped.setattr(workflow, "_develop", stop_after_accepted_plan)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    accepted = workflow.store.get(rid, "plan")
    assert accepted and workflow.store.get(rid, "evidence_admission/active")
    workflow._controller_replan(rid, Replan("design", "Bound architecture reconsideration fixture"))
    result = await workflow.start()
    binding = agent.rebuilds[0]["binding"]
    assert result["status"] == "SUCCEEDED"
    assert len(agent.rebuilds) == (2 if reject_first_rebuild else 1)
    assert all(view["binding"] == binding and view["plan"] == accepted for view in agent.rebuilds)
    assert workflow.store.get(rid, ACTIVE) is None
    assert workflow.store.get(rid, "authority/accepted/plan")
    assert workflow.store.run(rid)["data"]["replans"] == (2 if reject_first_rebuild else 1)

def test_protocol_instruction_is_not_execution_evidence(tmp_path):
    from auto_agents_2.evidence_refs import controller_context_catalog
    workflow, rid, _ = fixture(tmp_path)
    context = author_context(lambda k: workflow.store.get(rid, k))
    catalog = controller_context_catalog(context)
    assert "controller_contract_reentry.instruction" in catalog.protocol
    assert "controller_contract_reentry.instruction" not in catalog.factual
    assert "controller_contract_reentry.plan" in catalog.factual


def test_changed_origin_registry_rejects_old_plan_review(tmp_path):
    workflow, rid, old = fixture(tmp_path, "design")
    context, review = review_for(workflow, rid, "plan", old)
    workflow.store.checkpoint(rid, "task_origins", {"T1": "different"})
    with pytest.raises(StateConflict, match="baseline"):
        workflow.authority.validate(rid, review, context)


def accept_original_specification(workflow, rid):
    spec = workflow.store.get(rid, "clarify")
    context, _ = workflow.authority.prepare(rid, "coverage_review", {"assessment": spec},
        ReviewResult, workflow.project, "initial-clarify-review")
    reviewed = authority_review(context)
    workflow.authority.record(rid, reviewed, context, "initial-clarify-review")
    workflow.store.checkpoints(rid, workflow.authority.acceptance(rid, "clarify", spec, reviewed))
    return spec


@pytest.mark.parametrize("timing", ["after_accept_before_rollback", "before_accept", "legacy_no_receipt"])
def test_answer_consumption_uses_accepted_review_not_rollback_time(tmp_path, timing):
    workflow, rid = setup(tmp_path)
    if timing == "after_accept_before_rollback":
        accept_original_specification(workflow, rid)
    q = workflow.store.request(rid, "prototype-clarification/0", "Which previous goal changes?", "bound-prototype")
    workflow.store.answer(q["id"], "Withdraw requirement R1 and its own acceptance; retain all R2 obligations.")
    if timing == "before_accept":
        accept_original_specification(workflow, rid)
    old = workflow.store.get(rid, "clarify")
    rollback(workflow, rid, "clarify")
    new = deepcopy(old)
    new["requirements"] = [r for r in old["requirements"] if r["id"] != "R1"]
    context, reviewed = review_for(workflow, rid, "clarify", new)
    revision = context["controller_review_basis"]["revision"]
    source = "answer:" + q["id"]
    if timing != "after_accept_before_rollback":
        assert source not in revision["sources"]
        return
    assert revision["sources"][source]["record"] == workflow.store.submitted_answers(rid)[0]
    reviewed.authority["revision_dispositions"] = [replacement(context, operation="retire", source=source)]
    accept(workflow, rid, "clarify", new, context, reviewed)
    rollback(workflow, rid, "clarify")
    context, _ = review_for(workflow, rid, "clarify", new)
    assert source not in context["controller_review_basis"]["revision"]["sources"]


@pytest.mark.parametrize("change", ["legitimate", "lose_retained_requirement", "retire_mixed_row"])
def test_mixed_requirement_link_removal_is_bounded_to_accepted_withdrawal(tmp_path, change):
    from auto_agents_2.models import EvidenceObligation
    workflow, rid = setup(tmp_path)
    original_spec = accept_original_specification(workflow, rid)
    candidate = plan()
    candidate.tasks[0].requirement_ids = ["R1", "R2"]
    old = evidence_plan(candidate, admission_context(lambda k: workflow.store.get(rid, k)))
    old.evidence_contract.obligations.append(EvidenceObligation(id="EV-MIXED", target="acceptance",
        task_id="T1", index=0, value=old.tasks[0].acceptance[0], origin="model_derived",
        requirement_ids=["R1", "R2"], capability="test_file_attachments", capability_version=1,
        producer="VerificationService", production_stage="verification", required_stage="verification"))
    workflow.store.checkpoint(rid, "plan", old.model_dump())
    seed_check_production(workflow, rid, old)
    q = workflow.store.request(rid, "design/0", "Withdraw which goal?", "design-withdrawal")
    workflow.store.answer(q["id"], "Withdraw R1; keep R2 and every check still serving R2.")
    rollback(workflow, rid, "clarify")
    new_spec = deepcopy(original_spec)
    new_spec["requirements"] = [r for r in original_spec["requirements"] if r["id"] != "R1"]
    context, reviewed = review_for(workflow, rid, "clarify", new_spec)
    reviewed.authority["revision_dispositions"] = [replacement(context, operation="retire", source="answer:" + q["id"])]
    accept(workflow, rid, "clarify", new_spec, context, reviewed)
    new = old.model_dump()
    remaining = ["R2"] if change != "lose_retained_requirement" else ["R3"]
    new["tasks"][0]["requirement_ids"] = remaining
    new["evidence_contract"]["obligations"][0]["requirement_ids"] = remaining
    # New source binding comes from the accepted new clarification, not the author.
    new["evidence_contract"]["source_binding"] = admission_context(
        lambda k: workflow.store.get(rid, k))["evidence_requirement_source"]["hash"]
    context, reviewed = review_for(workflow, rid, "plan", new)
    revision = context["controller_review_basis"]["revision"]
    source = next(k for k, v in revision["sources"].items() if v["kind"] == "accepted_spec_revision")
    rows = [replacement(context, field=field, source=source)
            for field in ("task_links", "evidence_obligation")]
    if change == "retire_mixed_row":
        rows[0].update(operation="retire", new_ids=[])
    reviewed.authority["revision_dispositions"] = rows
    if change != "legitimate":
        with pytest.raises(InvalidAuthorityReview, match="requirement_association|wrong_target"):
            validate_review(reviewed, context["controller_review_basis"])
        return
    accept(workflow, rid, "plan", new, context, reviewed)
    assert workflow.store.get(rid, "plan")["tasks"][0]["requirement_ids"] == ["R2"]
    assert workflow.store.get(rid, ACTIVE) is None


def test_evidence_metadata_disposition_cannot_downgrade_unchanged_check_producer(tmp_path):
    from auto_agents_2.contract_reentry import preserve_production
    workflow, rid, old = fixture(tmp_path, "design", command_backed=True)
    context, reviewed = review_for(workflow, rid, "plan", old)
    revision = context["controller_review_basis"]["revision"]
    ident = "metadata-only-fixture"
    revision["old"][ident] = {"field": "evidence_obligation", "contract_target": {
        "target": "acceptance", "task_id": "T1", "index": 0, "value_hash": digest(old["tasks"][0]["acceptance"][0])}}
    # Exercise the production-mapping layer with a previously validated mapping:
    # metadata change never means the executable assertion itself changed.
    from types import SimpleNamespace
    assessment = SimpleNamespace(revision_dispositions=[SimpleNamespace(old_ids=[ident])])
    target = reviewed.evidence_assessment.check_production.bindings[0]
    target.kind, target.producers, target.attachment_formats = "review", [], []
    with pytest.raises(Exception, match="downgrade"):
        preserve_production(type(plan()).model_validate(old), reviewed, context, assessment)


@pytest.mark.parametrize("drift", ["legacy_frontend_only", "different_requirement"])
def test_legacy_frontend_normalization_is_conservative_not_arbitrary_drift(tmp_path, drift):
    workflow, rid = setup(tmp_path)
    spec = workflow.store.get(rid, "clarify")
    spec["frontend"] = False
    workflow.store.checkpoint(rid, "clarify", spec)
    accept_original_specification(workflow, rid)
    q = workflow.store.request(rid, "design/0", "Change prior goal?", "bound")
    workflow.store.answer(q["id"], "Withdraw R1.")
    normalized = {**spec, "frontend": True}
    if drift == "different_requirement":
        normalized = deepcopy(normalized)
        normalized["requirements"][0]["description"] = "Unreviewed product drift"
    workflow.store.checkpoint(rid, "clarify", normalized)
    if drift == "different_requirement":
        with pytest.raises(StateConflict, match="beyond legacy frontend"):
            rollback(workflow, rid, "clarify")
        assert workflow.store.get(rid, ACTIVE) is None
        return
    rollback(workflow, rid, "clarify")
    current = state(lambda k: workflow.store.get(rid, k))
    assert current["specification"] == normalized
    assert current["base"]["clarify_answer_basis"] is None
    context, _ = review_for(workflow, rid, "clarify", normalized)
    assert "answer:" + q["id"] not in context["controller_review_basis"]["revision"]["sources"]


async def test_first_entry_frontend_fact_is_reviewed_before_acceptance(tmp_path):
    import asyncio

    from test_decisions import ApprovingAgent
    from test_workflow import engine
    class DetectedFrontendAgent(ApprovingAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "provider_research":
                raise asyncio.CancelledError()
            result = await super().invoke(stage, context, schema, *args, **kwargs)
            if stage == "clarify":
                result.frontend, result.ui_changes = False, False
            if stage == "coverage_review":
                assert context["assessment"]["frontend"] is True
            return result
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<!doctype html><title>Existing frontend</title>")
    (project / "package.json").write_text('{"dependencies":{"react":"*"}}')
    request = tmp_path / "request.md"
    request.write_text("Add two numbers; keep the existing UI.")
    workflow = engine(project, DetectedFrontendAgent())
    from auto_agents_2.workspace import git
    git(project, "add", "index.html", "package.json")
    git(project, "-c", "user.name=aa2 fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "Fixture accepted existing frontend")
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    spec = workflow.store.get(rid, "clarify")
    assert spec["frontend"] is True
    receipt = workflow.store.get(rid, "authority/accepted/clarify")
    assert receipt["artifact_hash"] == digest(spec)
    assert workflow.store.get(rid, receipt["review_key"])["owned_candidate"] == spec
    rollback(workflow, rid, "clarify")
    assert state(lambda k: workflow.store.get(rid, k))["base"]["clarify_answer_basis"] is not None


@pytest.mark.parametrize("cite_both", [False, True])
def test_sequential_accepted_withdrawals_jointly_retire_mixed_rows(tmp_path, cite_both):
    from auto_agents_2.models import EvidenceObligation
    workflow, rid = setup(tmp_path)
    spec = workflow.store.get(rid, "clarify")
    spec["requirements"].append({"id": "R5", "description": "Retained goal", "acceptance": ["Keep R5"]})
    workflow.store.checkpoint(rid, "clarify", spec)
    accept_original_specification(workflow, rid)
    candidate = plan()
    candidate.tasks[0].requirement_ids = ["R1", "R2"]
    candidate.tasks[1].requirement_ids = ["R5"]
    old = evidence_plan(candidate, admission_context(lambda k: workflow.store.get(rid, k)))
    old.evidence_contract.obligations.append(EvidenceObligation(id="EV-MIXED", target="acceptance",
        task_id="T1", index=0, value=old.tasks[0].acceptance[0], origin="model_derived",
        requirement_ids=["R1", "R2"], capability="test_file_attachments", capability_version=1,
        producer="VerificationService", production_stage="verification", required_stage="verification"))
    workflow.store.checkpoint(rid, "plan", old.model_dump())
    seed_check_production(workflow, rid, old)
    for index, removed in enumerate(["R1", "R2"]):
        q = workflow.store.request(rid, f"design/{index}", "Which goal is withdrawn?", f"binding-{index}")
        workflow.store.answer(q["id"], f"Withdraw only {removed}; preserve the other accepted goals.")
        rollback(workflow, rid, "clarify")
        spec = deepcopy(spec)
        spec["requirements"] = [r for r in spec["requirements"] if r["id"] != removed]
        context, reviewed = review_for(workflow, rid, "clarify", spec)
        reviewed.authority["revision_dispositions"] = [replacement(context,
            operation="retire", source="answer:" + q["id"])]
        accept(workflow, rid, "clarify", spec, context, reviewed)
    new = old.model_copy(deep=True)
    new.tasks = new.tasks[1:]
    new.tasks[0].depends_on = []
    new = evidence_plan(new, admission_context(lambda k: workflow.store.get(rid, k))).model_dump()
    context, reviewed = review_for(workflow, rid, "plan", new)
    revision = context["controller_review_basis"]["revision"]
    sources = [(k, v) for k, v in revision["sources"].items() if v["kind"] == "accepted_spec_revision"]
    assert len(sources) == 2
    citations = [{"source": k, "quote": v["text"]} for k, v in sources[:2 if cite_both else 1]]
    reviewed.authority["revision_dispositions"] = [{"operation": "retire",
        "old_ids": revision["changed"], "new_ids": [], "authority": citations,
        "reason": "Both requirements served by the removed task were independently withdrawn."}]
    if not cite_both:
        with pytest.raises(InvalidAuthorityReview, match="wrong_target"):
            validate_review(reviewed, context["controller_review_basis"])
        return
    accept(workflow, rid, "plan", new, context, reviewed)
    assert [t["id"] for t in workflow.store.get(rid, "plan")["tasks"]] == ["T2", "T3", "T4"]
