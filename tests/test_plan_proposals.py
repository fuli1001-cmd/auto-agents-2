"""Source-bound author deltas cannot silently retype accepted evidence."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import jsonschema
from pydantic import ValidationError

from auto_agents_2.errors import AA2Error, RecoveryPathExhausted
from auto_agents_2.evidence_admission import evidence_instructions, preserve_obligations, validate_obligations
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.models import EvidenceContract, PlanResult, strict_schema
from auto_agents_2.plan_proposals import (
    PROTOCOL_KEY, AmendEvidence, EvidencePlanPatch, PreserveEvidence, materialize, protocol,
)
from auto_agents_2.plan_revision import PlanPatch, apply_patch
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash

from plan_proposal_fixtures import freeze_legacy_author, proposal_response
from test_contract_activation_contexts import prepare
from test_evidence_admission import candidate, context
from test_recovery_contracts import failure
from test_recovery_policy import ready
from test_stage_contract_corrections import setup_correction


def sample():
    ctx = {**context(), PROTOCOL_KEY: protocol(), "base_revision": 1, "plan_patch": True}
    previous = candidate(ctx)
    patch = PlanPatch(base_revision=1, failure_ids=[failure().id], reason="Local scope repair",
                      document="Full revised plan", evidence_contract=previous.evidence_contract)
    ctx["plan"] = previous.model_dump()
    return previous, ctx, proposal_response(patch, ctx, EvidencePlanPatch)


def test_preserve_needs_no_old_rows_and_keeps_full_gate_contract():
    previous, ctx, proposal = sample()
    assert isinstance(proposal.evidence_update, PreserveEvidence)
    assert "obligations" not in proposal.model_dump()["evidence_update"]
    full = materialize(proposal, previous, ctx)
    updated, _, _ = apply_patch(previous, full, 1, {failure().id}, {t.id: t.origin_id for t in previous.tasks})
    preserve_obligations(previous, updated, full)
    assert full.evidence_contract == previous.evidence_contract
    assert full.evidence_contract is not previous.evidence_contract
    assert validate_obligations(updated, ctx) == []


@pytest.mark.parametrize("round_index", [0, 1])
def test_real_mu09_wrong_association_is_rejected_but_preserve_cannot_emit_it(round_index):
    frozen = json.loads((Path(__file__).parent / "fixtures/mu09_evidence_retyping.json").read_text())
    old = EvidenceContract.model_validate(frozen["accepted_evidence_contract"])
    failure_record = frozen["rounds"][round_index]
    broken = old.model_copy(deep=True)
    for delta in failure_record["diffs"]:
        row = next(r for r in broken.obligations if r.id == delta["obligation_id"])
        for key, values in delta["changed_fields"].items():
            assert getattr(row, key) == values["before"]
            setattr(row, key, values["after"])
    legacy_patch = PlanPatch(base_revision=5, failure_ids=[failure().id], reason="Frozen MU09 counterexample",
                            document="No change", evidence_contract=broken)
    with pytest.raises(AA2Error, match=failure_record["validation_error"]):
        preserve_obligations(SimpleNamespace(evidence_contract=old), SimpleNamespace(evidence_contract=broken), legacy_patch)

    previous, ctx, _ = sample()
    # Isolated materializer fixture: rebind headers to the synthetic current source.
    # Exact ten rows are the real incident; this does not assert the synthetic plan's semantic acceptance.
    previous.evidence_contract = old.model_copy(update={"catalog_hash": ctx["controller_evidence_capabilities"]["hash"],
                                                       "source_binding": ctx["evidence_requirement_source"]["hash"]})
    ctx["plan"] = previous.model_dump()
    proposal = EvidencePlanPatch(base_revision=1, failure_ids=[failure().id], reason="Preserve exact associations",
        document="No evidence changes", proposal_version=1, base_plan_hash=digest(ctx["plan"]),
        evidence_update=PreserveEvidence(mode="preserve", base_contract_hash=digest(previous.evidence_contract.model_dump())))
    rebuilt = materialize(proposal, previous, ctx)
    assert [r.model_dump() for r in rebuilt.evidence_contract.obligations] == [r.model_dump() for r in old.obligations]
    assert len(rebuilt.evidence_contract.obligations) == 10


@pytest.mark.parametrize("bad", ["missing_update", "null_update", "empty_update", "unknown_mode",
    "unknown_operation", "legacy_array", "extra_contract", "extra_old_rows", "missing_mode", "missing_version"])
def test_closed_wire_schema_does_not_guess_preserve(bad):
    _, _, proposal = sample()
    data = proposal.model_dump()
    if bad == "missing_update":
        data.pop("evidence_update")
    elif bad == "null_update":
        data["evidence_update"] = None
    elif bad == "empty_update":
        data["evidence_update"] = {}
    elif bad == "unknown_mode":
        data["evidence_update"]["mode"] = "copy"
    elif bad == "unknown_operation":
        data["evidence_update"] = dict(mode="amend", base_contract_hash="x", changes=[{"operation": "patch"}])
    elif bad == "legacy_array":
        data["evidence_update"] = []
    elif bad == "extra_contract":
        data["evidence_contract"] = {}
    elif bad == "extra_old_rows":
        data["evidence_update"]["obligations"] = []
    elif bad == "missing_mode":
        data["evidence_update"].pop("mode")
    else:
        data.pop("proposal_version")
    with pytest.raises(ValidationError):
        EvidencePlanPatch.model_validate(data)
    schema = strict_schema(EvidencePlanPatch)
    jsonschema.validate(proposal.model_dump(), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)
    assert set(schema["required"]) == set(schema["properties"])
    assert "evidence_contract" not in schema["properties"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("bad", ["plan", "revision", "contract", "protocol", "source", "catalog", "legacy"])
def test_preserve_exact_baselines_fail_closed(bad):
    previous, ctx, proposal = sample()
    if bad == "plan":
        previous.document += " changed"
    elif bad == "revision":
        ctx["base_revision"] += 1
    elif bad == "contract":
        proposal.evidence_update.base_contract_hash = "0" * 64
    elif bad == "protocol":
        ctx[PROTOCOL_KEY]["materializer"] = "unimplemented-v2"
    elif bad in {"source", "catalog"}:
        setattr(previous.evidence_contract, "source_binding" if bad == "source" else "catalog_hash", "0" * 64)
        proposal.base_plan_hash = digest(previous.model_dump())
        proposal.evidence_update.base_contract_hash = digest(previous.evidence_contract.model_dump())
    else:
        previous.evidence_contract = None
        proposal.base_plan_hash = digest(previous.model_dump())
        proposal.evidence_update.base_contract_hash = digest(None)
    with pytest.raises(AA2Error):
        materialize(proposal, previous, ctx)


@pytest.mark.parametrize("operation", ["replace", "remove"])
def test_explicit_changes_are_not_auto_overwritten_and_still_require_formal_correction(operation):
    previous, ctx, proposal = sample()
    row = previous.evidence_contract.obligations[0]
    change = dict(operation=operation, obligation_id=row.id, previous_hash=digest(row.model_dump()))
    if operation == "replace":
        change["replacement"] = row.model_copy(update={"requirement_ids": ["R2"]}).model_dump()
    proposal.evidence_update = AmendEvidence(mode="amend", base_contract_hash=digest(previous.evidence_contract.model_dump()),
                                             changes=[change])
    full = materialize(proposal, previous, ctx)
    after = previous.model_copy(update={"evidence_contract": full.evidence_contract})
    with pytest.raises(AA2Error, match="formal mapped"):
        preserve_obligations(previous, after, full)
    assert full.evidence_contract != previous.evidence_contract


@pytest.mark.parametrize("bad", ["stale", "missing", "rename", "duplicate", "add_existing"])
def test_amend_requires_exact_unique_preimages(bad):
    previous, ctx, proposal = sample()
    row = previous.evidence_contract.obligations[0]
    delta = dict(operation="replace", obligation_id=row.id, previous_hash=digest(row.model_dump()),
                 replacement=row.model_dump())
    if bad == "stale":
        delta["previous_hash"] = "0" * 64
    elif bad == "missing":
        delta["obligation_id"] = "missing"
    elif bad == "rename":
        delta["replacement"]["id"] = "renamed"
    elif bad == "add_existing":
        delta = dict(operation="add", obligation=row.model_dump())
    proposal.evidence_update = AmendEvidence(mode="amend", base_contract_hash=digest(previous.evidence_contract.model_dump()),
                                             changes=[delta, delta] if bad == "duplicate" else [delta])
    with pytest.raises(AA2Error):
        materialize(proposal, previous, ctx)


def test_author_instruction_and_evidence_roles_are_separate_from_review():
    _, ctx, _ = sample()
    author = evidence_instructions("plan_patch", ctx)
    reviewer = evidence_instructions("plan_patch", {**ctx, "plan_patch": False, "review_kind": "plan_patch"})
    assert "return EvidencePlanPatch" in author and "return EvidencePlanPatch" not in reviewer
    assert "complete evidence_contract" in reviewer
    catalog = controller_context_catalog(ctx)
    assert PROTOCOL_KEY in catalog.protocol and PROTOCOL_KEY not in catalog.factual
    assert protocol()["hash"] in catalog.protocol


@pytest.mark.parametrize("bad", [None, "record_missing", "malformed", "raw", "artifact", "patch", "context", "base", "concurrent", "receipt", "candidate"])
async def test_prepared_materialization_recomputed_before_activation(tmp_path, monkeypatch, bad):
    workflow, rid, _, old, record, agent = setup_correction(tmp_path)
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    receipt = journal["proposal_materialization"]
    raw = workflow.artifacts.json(receipt["proposal"])
    assert "evidence_update" in raw and "evidence_contract" not in raw
    assert "evidence_contract" in workflow.artifacts.json(receipt["patch"])
    assert workflow.artifacts.json(journal["materialized_candidate"]) == journal["plan"]
    assert agent.views[1][1]["plan_materialized_candidate"] == journal["materialized_candidate"]
    assert agent.views[1][1]["plan_patch_proposal"] == raw
    assert agent.views[1][1]["plan_patch_materialization"] == receipt
    review_context = agent.views[1][1]
    catalog = controller_context_catalog(review_context, provenance={"patch": "proposal", "plan": "proposal"})
    for field in ("plan_patch_proposal", "plan_patch_materialization", "plan_materialized_candidate"):
        assert field in catalog.proposals and field not in catalog.factual
    assert journal["materialized_candidate"]["hash"] not in catalog.factual
    from auto_agents_2.context_plan import ContextPlanner
    prepared = ContextPlanner(workflow.artifacts).prepare("review", review_context, workflow.project, run_id=rid)
    assert prepared.payload["plan_materialized_candidate"] == journal["materialized_candidate"]
    assert workflow.store.get(rid, "invocation/recovery/patch/" + identity)["result"] == raw
    if bad == "record_missing":
        journal.pop("proposal_materialization")
    elif bad == "malformed":
        receipt.pop("previous")
    elif bad == "raw":
        invocation = workflow.store.get(rid, "invocation/recovery/patch/" + identity)
        invocation["result"]["reason"] += " altered"
        workflow.store.checkpoint(rid, "invocation/recovery/patch/" + identity, invocation)
    elif bad == "artifact":
        ref = receipt["proposal"]
        (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("{}")
    elif bad == "patch":
        journal["patch"]["reason"] += " altered"
    elif bad == "receipt":
        workflow.store.checkpoint(rid, "recovery/patch-materialization/" + identity, None)
    elif bad == "candidate":
        ref = journal["materialized_candidate"]
        (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("{}")
    elif bad == "context":
        ctxkey = "recovery/patch-context/" + identity
        ctx = workflow.store.get(rid, ctxkey)
        ctx["evidence_requirement_source"]["hash"] = "0" * 64
        workflow.store.checkpoint(rid, ctxkey, ctx)
    elif bad == "base":
        journal["previous_plan"]["document"] += " altered"
    elif bad == "concurrent":
        changed = old.model_copy(update={"document": "Concurrent accepted source"})
        workflow.store.checkpoint(rid, "plan", changed.model_dump())
    workflow.store.checkpoint(rid, key, journal)
    if bad:
        with pytest.raises(AA2Error):
            workflow.store.activate_plan_patch(rid, identity)
        assert workflow.store.get(rid, key)["phase"] == "prepared"
    else:
        workflow.store.activate_plan_patch(rid, identity)
        workflow.store.activate_plan_patch(rid, identity)
        assert workflow.store.get(rid, key)["phase"] == "activated"
        assert workflow.store.get(rid, "plan_revision/current")["revision"] == 2
    assert workflow.store.get(rid, "rounds/T3") == {"round": 17}


async def test_frozen_old_author_stays_old_and_is_not_reinterpreted(tmp_path):
    workflow, rid, _, _, record, _ = setup_correction(tmp_path)
    await freeze_legacy_author(workflow, rid, record)
    await workflow.recovery.resume(rid)
    identity = f"{record.id}/1"
    journal = workflow.store.get(rid, "plan_revision/journal/" + identity)
    assert journal["phase"] == "activated" and "proposal_materialization" not in journal
    raw = workflow.store.get(rid, "invocation/recovery/patch/" + identity)["result"]
    assert "evidence_contract" in raw and "evidence_update" not in raw


def test_capability_changes_only_patch_basis_and_does_not_reset_old_failures(tmp_path):
    workflow, rid, work, plan, record = ready(tmp_path)
    guard = workflow.recovery_policy.progress
    ctx = guard.inputs(rid, record, work, plan)
    old_ctx = {k: v for k, v in ctx.items() if k != PROTOCOL_KEY}
    source = tree_hash(work)
    old = {action: guard.basis(old_ctx, source, action) for action in ("diagnose", "plan_patch")}
    new = {action: guard.basis(ctx, source, action) for action in old}
    assert new["diagnose"] == old["diagnose"]
    assert new["plan_patch"] != old["plan_patch"]
    cycle, operation = "same-cycle", "dispatch/3"
    stopped_key = guard._stop_key(cycle, operation)
    frozen_stop = dict(facts=old, question_key="original-unasked", excluded_actions=["diagnose", "plan_patch"], reason="exhausted")
    workflow.store.checkpoint(rid, stopped_key, frozen_stop)
    for action in old:
        for i in range(2):
            guard.record(rid, record.origin_id, action, old[action], f"old/{action}/{i}", {"status": "rejected"})
    assert set(guard.exclusions(rid, record, work, plan)) == {"diagnose"}
    selected = guard.resume_operation(rid, record, work, plan, cycle, operation)
    assert selected.startswith(operation + "/evidence/")
    assert workflow.store.get(rid, stopped_key) == frozen_stop
    for i in range(2):
        guard.record(rid, record.origin_id, "plan_patch", new["plan_patch"], f"new/{i}", {"status": "rejected"})
    assert set(guard.exclusions(rid, record, work, plan)) == {"diagnose", "plan_patch"}
    with pytest.raises(RecoveryPathExhausted):
        guard.stop(rid, record, work, plan, cycle, selected, ["diagnose", "plan_patch"], "still exhausted")
    with pytest.raises(RecoveryPathExhausted):
        guard.resume_operation(rid, record, work, plan, cycle, operation)
    assert len(workflow.store.get(rid, "recovery/no-progress/plan_patch/" + old["plan_patch"])["operations"]) == 2


async def test_proposal_and_full_candidate_dependency_closure_survives_collection(tmp_path, monkeypatch):
    from auto_agents_2.artifact_gc import collect_artifacts
    workflow, rid, _, _, record, _ = setup_correction(tmp_path)
    identity, _, journal = await prepare(workflow, rid, record, monkeypatch)
    workflow.store.activate_plan_patch(rid, identity)
    refs = [journal["materialized_candidate"], *[journal["proposal_materialization"][k]
             for k in ("previous", "proposal", "patch")]]
    garbage = workflow.artifacts.put("Unreferenced test object", "document")
    workflow.store.update(rid, status="SUCCEEDED")
    collect_artifacts(workflow.store)
    assert not (workflow.artifacts.root / garbage["hash"][:2] / garbage["hash"]).exists()
    for ref in refs:
        assert workflow.artifacts.json(ref)
    workflow.store.activate_plan_patch(rid, identity)


def test_explicit_empty_amend_migrates_legacy_contract_but_never_guesses_preserve():
    previous, ctx, proposal = sample()
    previous.evidence_contract = None
    proposal.base_plan_hash = digest(previous.model_dump())
    proposal.evidence_update = AmendEvidence(mode="amend", base_contract_hash=digest(None), changes=[])
    jsonschema.validate(proposal.model_dump(), strict_schema(EvidencePlanPatch))
    full = materialize(proposal, previous, ctx)
    assert full.evidence_contract.obligations == []
    assert full.evidence_contract.source_binding == ctx["evidence_requirement_source"]["hash"]
    assert not hasattr(full, "approved")


@pytest.mark.parametrize("state", ["pending", "answered", "issued"])
async def test_protocol_upgrade_preserves_old_human_question_and_permit(tmp_path, monkeypatch, state):
    import asyncio
    import auto_agents_2.plan_proposals as proposals
    from test_decisions import configure
    from test_recovery_policy import authorize
    workflow, rid, work, plan, record = ready(tmp_path)
    configure(workflow, automation={"enabled": False})
    async def interrupt(question):
        raise asyncio.CancelledError()
    workflow.question_handler = interrupt
    current = proposals.decision_protocol
    monkeypatch.setattr(proposals, "decision_protocol", lambda *a, **kw: {})
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, plan, record, force_decision=True)
    question = copy.deepcopy(workflow.store.pending(rid)[0])
    if state in {"answered", "issued"}:
        workflow.store.answer(question["id"], "y")
    permit = None
    if state == "issued":
        _, permit = await authorize(workflow, rid, work, plan, record, force_decision=True)
        prior = workflow.store.get(rid, permit)
    with workflow.store.connect() as db:
        snapshots = {r["key"]: json.loads(r["data"]) for r in db.execute(
            "SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/request-snapshots/%'", (rid,))}
    assert snapshots and all(PROTOCOL_KEY not in s["request"]["evidence"] for s in snapshots.values())
    monkeypatch.setattr(proposals, "decision_protocol", current)
    if state == "pending":
        with pytest.raises(asyncio.CancelledError):
            await authorize(workflow, rid, work, plan, record, force_decision=True)
        assert workflow.store.pending(rid)[0] == question
    else:
        _, actual = await authorize(workflow, rid, work, plan, record, force_decision=True)
        if permit:
            assert actual == permit and workflow.store.get(rid, permit) == prior
        assert workflow.store.question(question["id"])["answer"] == "y"
    for key, frozen in snapshots.items():
        assert workflow.store.get(rid, key) == frozen


@pytest.mark.parametrize("reject_new", [False, True])
async def test_completed_diagnosis_migrates_through_policy_and_real_author_only_with_new_capability(
        tmp_path, monkeypatch, reject_new):
    import auto_agents_2.plan_proposals as proposals
    from auto_agents_2.decisions import DecisionResult
    from auto_agents_2.recovery import Diagnosis
    from test_stage_contract_corrections import CorrectionAgent
    from test_workflow import engine
    workflow, rid, work, plan, record, agent = setup_correction(tmp_path)
    workflow.store.update(rid, replans=26)
    invoke = agent.invoke
    requests, diagnoses = [], []
    async def choose(stage, ctx, schema, *args, **kwargs):
        if schema is Diagnosis:
            diagnoses.append(ctx)
            return Diagnosis(action="diagnose", task_ids=["T3"], reason="Existing diagnosis has no executable correction",
                             evidence_refs=["failure"])
        if stage == "decision":
            request = ctx["decision_request"]
            requests.append(copy.deepcopy(request))
            if "plan_patch" in request["scope"]["candidates"]:
                return DecisionResult(action="approve", selected_action="plan_patch", reason="Use actual source-bound materialization",
                    evidence_refs=["failure"], expected_result="Independently reviewed complete plan")
            return DecisionResult(action="ask_user", reason="No applicable remaining correction")
        response = await invoke(stage, ctx, schema, *args, **kwargs)
        if schema is EvidencePlanPatch and reject_new:
            response.contract_corrections = []
        return response
    agent.invoke = choose
    guard = workflow.recovery_policy.progress
    current_inputs, current_protocol = guard.inputs, proposals.decision_protocol
    def legacy_inputs(*args):
        ctx = current_inputs(*args)
        ctx.pop(PROTOCOL_KEY)
        return ctx
    monkeypatch.setattr(guard, "inputs", legacy_inputs)
    monkeypatch.setattr(proposals, "decision_protocol", lambda *a, **kw: {})
    source = tree_hash(work)
    old_ctx = legacy_inputs(rid, record, work, plan)
    old_patch = guard.basis(old_ctx, source, "plan_patch")
    for op in ["legacy-mu09-wrong-association", "legacy-mu09-missing-association"]:
        guard.record(rid, record.origin_id, "plan_patch", old_patch, op, {"status": "rejected"})
    old_diagnosis = guard.basis(old_ctx, source, "diagnose")
    guard.record(rid, record.origin_id, "diagnose", old_diagnosis, "older-inconclusive", {"status": "inconclusive"})
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    completed_key = active["diagnosis_permit"]
    completed = workflow.store.get(rid, completed_key)
    budget = workflow.store.get(rid, "recovery/budgets/diagnosis/T3")
    assert completed["phase"] == "completed" and len(diagnoses) == 1
    assert not workflow.store.pending(rid)
    old_requests = copy.deepcopy(workflow.store.decision_requests(rid))
    # A restarted controller with the actual new capability, unchanged product state.
    workflow = engine(tmp_path, agent)
    guard = workflow.recovery_policy.progress
    monkeypatch.setattr(proposals, "decision_protocol", current_protocol)
    if reject_new:
        await workflow.recovery.resume(rid)
        await workflow.recovery.resume(rid)
        with pytest.raises(RecoveryPathExhausted):
            await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, "plan") == plan.model_dump()
        assert workflow.store.run(rid)["data"]["replans"] == 26
        assert len([view for stage, view in agent.views if stage == "plan"]) == 2
        assert workflow.store.get(rid, "recovery/active")["attempt"] == 3
        current_basis = guard.basis(guard.inputs(rid, record, work, plan), tree_hash(work), "plan_patch")
        assert len(workflow.store.get(rid, "recovery/no-progress/plan_patch/" + current_basis)["operations"]) == 2
    else:
        await workflow.recovery.resume(rid)
        journal = workflow.store.get(rid, f"plan_revision/journal/{record.id}/1")
        assert journal["phase"] == "activated"
        assert "proposal_materialization" in journal and journal["review"]["approved"]
        assert workflow.store.run(rid)["data"]["replans"] == 26
        assert len([view for stage, view in agent.views if stage == "plan"]) == 1
        assert any(PROTOCOL_KEY in request["evidence"] and "plan_patch" in request["scope"]["candidates"] for request in requests)
    assert workflow.store.get(rid, completed_key) == completed
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3") == budget
    assert len(diagnoses) == 1 and workflow.store.get(rid, "rounds/T3") == {"round": 17}
    for key, value in old_requests:
        assert workflow.store.get(rid, key) == value
    assert len(workflow.store.get(rid, "recovery/no-progress/plan_patch/" + old_patch)["operations"]) == 2


@pytest.mark.parametrize("operation", ["add", "replace", "remove"])
def test_nonempty_amend_operations_validate_against_actual_wire_schema(operation):
    previous, ctx, proposal = sample()
    row = previous.evidence_contract.obligations[0]
    change = dict(operation=operation, obligation_id=row.id, previous_hash=digest(row.model_dump()))
    if operation == "add":
        change = dict(operation="add", obligation=row.model_copy(update={"id": "new-obligation"}).model_dump())
    elif operation == "replace":
        change["replacement"] = row.model_dump()
    proposal.evidence_update = AmendEvidence(mode="amend", base_contract_hash=digest(previous.evidence_contract.model_dump()),
                                             changes=[change])
    schema = strict_schema(EvidencePlanPatch)
    jsonschema.validate(proposal.model_dump(), schema)
    malformed = proposal.model_dump()
    malformed["evidence_update"]["changes"][0]["unapproved_key"] = "ignored is forbidden"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(malformed, schema)
    materialize(proposal, previous, ctx)
