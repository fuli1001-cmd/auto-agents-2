"""Scoped acceptance across independently reviewed local contract corrections."""
import pytest
from evidence_test_fixtures import empty_contract, evidence_review
from plan_proposal_fixtures import EvidencePlanPatch, proposal_response
from test_stage_contract_corrections import review_for
from test_task_plan_continuity import LocalPatchAgent, amended, invalidate

from auto_agents_2.contract_corrections import ContractCorrection
from auto_agents_2.models import PlanResult
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.state_codec import update_wire_db
from auto_agents_2.util import digest


def local_timeout_patch(monkeypatch):
    invoke = LocalPatchAgent.invoke

    async def corrected(self, stage, context, schema, *args, **kwargs):
        if schema in (PlanPatch, EvidencePlanPatch):
            old = PlanResult.model_validate(context["plan"])
            task = old.tasks[2].model_copy(deep=True)
            before = task.checks[0].model_copy(deep=True)
            task.checks[0].timeout = before.timeout * 2
            correction = ContractCorrection(target="checks", task_id=task.id, index=0,
                previous=before, replacement=task.checks[0], reason="Observed bounded timeout",
                derived_decision="Only change the model-derived execution time budget",
                preserved_obligations=["All assertions, inputs and test minimums remain"],
                evidence_refs=["failure", "plan.tasks[2].checks[0]"])
            return proposal_response(PlanPatch(base_revision=context["base_revision"],
                failure_ids=[context["failure"]["id"]], reason="Local timeout correction",
                document="One reviewed local timeout correction", upsert_tasks=[task],
                contract_corrections=[correction], evidence_contract=empty_contract(context)), context, schema)
        if context.get("review_kind") == "plan_patch":
            return evidence_review(context, review_for(PlanPatch.model_validate(context["patch"])))
        return await invoke(self, stage, context, schema, *args, **kwargs)

    monkeypatch.setattr(LocalPatchAgent, "invoke", corrected)


@pytest.mark.parametrize("restoring", [False, True])
async def test_unrelated_timeout_correction_retains_exact_acceptance(tmp_path, monkeypatch, restoring):
    local_timeout_patch(monkeypatch)
    workflow, rid, work, plan, records = await amended(tmp_path)
    permits = {k: v for k, v in workflow.store.delivery_evidence(rid).items()
               if k.startswith("recovery/permits/")}
    journal = next(v for k, v in workflow.store.delivery_evidence(rid).items()
                   if k.startswith("plan_revision/journal/"))
    assert journal["version"] == 3 and journal["affected"] == ["T3", "T4"]
    assert journal["patch"]["contract_corrections"][0]["replacement"]["timeout"] == (
        journal["patch"]["contract_corrections"][0]["previous"]["timeout"] * 2)
    for task in plan.tasks[:2]:
        saved = invalidate(workflow, rid, task, records[task.id]) if restoring else records[task.id]
        assert workflow.authority.retain_task(rid, task, saved, work=work) == records[task.id]
        proof = workflow.store.get(rid, "authority/task-continuity/" + task.id)
        assert proof["accepted_record_hash"] == digest(records[task.id])
    assert workflow.store.get(rid, "develop/done") == ["T1", "T2"]
    assert workflow.store.get(rid, "rounds/T1") == {"round": 7}
    assert {k: v for k, v in workflow.store.delivery_evidence(rid).items()
            if k.startswith("recovery/permits/")} == permits
    assert not workflow.authority.reusable(rid, records["T1"]["review"])

@pytest.mark.parametrize("damage", [
    "binding", "author_context", "review_context", "review_mapping", "permit",
    "legacy_context", "upstream", "preimage",
])
async def test_local_contract_correction_requires_its_complete_historical_proof(tmp_path, monkeypatch, damage):
    from auto_agents_2.contract_corrections import contract_review_binding
    local_timeout_patch(monkeypatch)
    workflow, rid, work, plan, records = await amended(tmp_path)
    store = workflow.store
    key = next(k for k in store.delivery_evidence(rid) if k.startswith("plan_revision/journal/"))
    journal = store.get(rid, key)
    if damage == "binding":
        journal["contract_review_binding"] = "corrupt"
    elif damage == "author_context":
        journal["contract_author_context"]["original_capture"] = {"different": "source"}
    elif damage == "review_context":
        journal["contract_review_context"]["patch"] = {}
    elif damage == "review_mapping":
        journal["review"]["contract_reviews"] = []
    elif damage == "permit":
        permit_key = journal["correction_request"]["permit"]
        permit = store.get(rid, permit_key)
        permit["source_hash"] = "different original permission"
        # Deliberate persisted damage: never treat it as authority.
        with store.connect() as db:
            import json
            update_wire_db(db, rid, permit_key, json.dumps(permit))
    elif damage == "legacy_context":
        journal["version"] = 2
    elif damage == "upstream":
        store.checkpoint(rid, "provider_research", {"changed": "unbound research"})
    elif damage == "preimage":
        journal["patch"]["contract_corrections"][0]["previous"]["timeout"] += 1
    if damage not in {"binding", "permit", "upstream"}:
        journal["contract_review_binding"] = contract_review_binding(journal)
    store.checkpoint(rid, key, journal)
    result = workflow.authority.retain_task(rid, plan.tasks[0], records["T1"], work=work)
    assert result["phase"] == "reverify"
    assert result["previous_evidence"] == records["T1"]
    assert "T1" not in store.get(rid, "develop/done")
    assert store.get(rid, "authority/task-continuity/T1") is None


@pytest.mark.parametrize("change", ["own", "dependency", "test_input", "untracked", "mode", "symlink", "unrelated"])
async def test_contract_transition_keeps_actual_source_boundary(tmp_path, monkeypatch, change):
    from test_task_plan_continuity import test_actual_source_and_dependency_content_controls_reuse as exercise
    local_timeout_patch(monkeypatch)
    await exercise(tmp_path, change)


async def test_contract_continuity_rolls_back_and_retries_atomically(tmp_path, monkeypatch):
    from test_task_plan_continuity import test_failed_publication_rolls_back_then_normal_retry_recovers as exercise
    local_timeout_patch(monkeypatch)
    await exercise(tmp_path)


async def test_contract_continuity_cannot_overwrite_concurrent_work(tmp_path, monkeypatch):
    from test_task_plan_continuity import test_stale_caller_cannot_overwrite_concurrent_task_state as exercise
    local_timeout_patch(monkeypatch)
    await exercise(tmp_path)


@pytest.mark.parametrize("mutation", ["new_work", "feedback", "revision"])
async def test_contract_transition_restoration_respects_new_observations(tmp_path, monkeypatch, mutation):
    from test_task_plan_continuity import test_recovery_never_overwrites_new_work_or_other_invalidation as exercise
    local_timeout_patch(monkeypatch)
    await exercise(tmp_path, mutation)


async def test_multiple_contract_corrections_keep_the_original_review_chain(tmp_path, monkeypatch):
    from test_task_plan_continuity import test_multiple_reviewed_local_patches_preserve_original_review_chain as exercise
    local_timeout_patch(monkeypatch)
    await exercise(tmp_path)


@pytest.mark.parametrize("damage", ["capture", "answer", "approval", "receipt", "journal", "affected",
                                  "history", "contract", "global", "checks", "implementation", "source_hash", "commit", "unknown_inputs"])
async def test_contract_transition_preserves_all_existing_fail_closed_gates(tmp_path, monkeypatch, damage):
    from test_task_plan_continuity import test_damaged_or_changed_proof_keeps_original_reverify_gate as exercise
    local_timeout_patch(monkeypatch)
    await exercise(tmp_path, damage)
