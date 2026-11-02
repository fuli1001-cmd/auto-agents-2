"""Citations of displayed check wrappers must resolve to their original record."""
import copy

import pytest

from test_context_check_observations import observations
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import CONTEXT_VERSION, ContextPlanner, remap_references
from auto_agents_2.contract_corrections import _references
from auto_agents_2.errors import AA2Error, ProviderError
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import Diagnosis, FailureRecord, adjudicate
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize("stage,slot,extra", [
    ("plan", "failure.evidence.checks", {"plan_patch": True}),
    ("recovery_diagnosis", "failure.evidence.checks", {}),
    ("implement", "feedback.checks", {}),
    ("review", "checks", {"review_kind": "implementation"}),
    ("review", "feedback.checks", {"review_kind": "test_changes"}),
    ("verify", "evidence", {}),
    ("decision", "decision_request.evidence.checks", {}),
])
def test_displayed_observation_path_keeps_original_source(observations, tmp_path, stage, slot, extra):
    planner, check = observations
    context = copy.deepcopy(extra)
    target = context
    parts = slot.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = [check, copy.deepcopy(check)]
    original = copy.deepcopy(context)
    prepared = planner.prepare(stage, context, tmp_path)
    shown = prepared.payload
    for part in parts:
        shown = shown[part]
    catalog = controller_context_catalog(context)
    for index, row in enumerate(shown):
        source = f"{slot}[{index}]"
        assert row["observation"]["source_ref"] == source
        assert planner.artifacts.json(row["observation"]["artifact"]) == check
        response = {"evidence_refs": [source + ".observation"],
                    "nested": {"fact_ref": source + ".observation"}}
        result = remap_references(response, prepared.reference_map)
        assert result == {"evidence_refs": [source], "nested": {"fact_ref": source}}
        assert catalog.resolve(result["evidence_refs"][0]) in catalog.factual
        if stage == "plan":
            _references(result["evidence_refs"], context)
    assert shown[1]["same_observation_as"] == slot + "[0]"
    assert context == original


def test_wrapper_cannot_promote_model_owned_observation(observations, tmp_path):
    planner, check = observations
    failure = FailureRecord.create(kind="proposal", component="implementation",
        origin_id="T1", step="task", obligations=["R1"], evidence={"checks": [check]})
    failure.proposal = {"checks": [copy.deepcopy(check)]}
    context = {"failure": failure.model_dump()}
    prepared = planner.prepare("recovery_diagnosis", context, tmp_path)
    response = remap_references({"action": "diagnose", "reason": "Inspect supplied observation",
        "evidence_refs": ["failure.proposal.checks[0].observation"]}, prepared.reference_map)
    assert response["evidence_refs"] == ["failure.proposal.checks[0]"]
    with pytest.raises(AA2Error, match="requires controller evidence"):
        adjudicate(failure, diagnosis=Diagnosis.model_validate(response), evidence_context=context)


@pytest.mark.parametrize("reference", [
    "failure.evidence.checks[99].observation",
    "failure.evidence.checks[0].observation.fabricated",
    "failure.evidence.checks[0].not_observation",
])
def test_unknown_wrapper_paths_still_fail_validation(observations, tmp_path, reference):
    planner, check = observations
    context = {"plan_patch": True, "failure": {"evidence": {"checks": [check]}}}
    prepared = planner.prepare("plan", context, tmp_path)
    response = remap_references({"evidence_refs": ["failure", reference]}, prepared.reference_map)
    assert response["evidence_refs"] == ["failure", reference]
    with pytest.raises(AA2Error, match="real supplied evidence"):
        _references(response["evidence_refs"], context)


def test_existing_observation_field_is_not_aliased_to_parent(observations, tmp_path):
    planner, check = observations
    check["observation"] = {"source": "existing original field"}
    context = {"evidence": [check]}
    original = copy.deepcopy(context)
    prepared = planner.prepare("verify", context, tmp_path)
    result = remap_references({"evidence_refs": ["evidence[0].observation"]}, prepared.reference_map)
    assert result["evidence_refs"] == ["evidence[0].observation"]
    assert controller_context_catalog(context).entries[result["evidence_refs"][0]].path == "evidence[0].observation"
    assert context == original


def test_pending_v7_context_regenerates_view_without_changing_receipts(tmp_path):
    config, store = configured(tmp_path)
    store.admit("check observation citation recovery", {})
    rid = store.current()["id"]
    artifacts = ArtifactStore(store.root)
    check = {"argv": ["pytest"], "exit_code": 1, "source_hash": "source",
             "environment_hash": "environment"}
    context = {"failure": {"evidence": {"checks": [check]}}}
    previous = ContextPlanner(artifacts).prepare("recovery_diagnosis", context, tmp_path).record()
    previous["version"] = "context-v7"
    previous["reference_map"].pop("failure.evidence.checks[0].observation", None)
    key = "presentation/recovery/diagnose/retained/1/frozen"
    receipt = {"identity": "frozen", "artifact": artifacts.put(previous, "prepared_context")}
    completed = {"artifact": {"hash": "a" * 64}, "result": {"action": "diagnose"}}
    retained = {key: receipt, "recovery/budgets/diagnosis/T1": {"round": 8},
                "invocation/already-completed": completed, "develop/done": ["T0"]}
    store.checkpoints(rid, retained)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    current = router._prepare_context("recovery_diagnosis", context, tmp_path, rid,
                                      "recovery/diagnose/retained/1", "frozen")
    assert current.version == CONTEXT_VERSION and CONTEXT_VERSION != "context-v7"
    assert remap_references({"evidence_refs": ["failure.evidence.checks[0].observation"]},
                            current.reference_map)["evidence_refs"] == ["failure.evidence.checks[0]"]
    assert all(store.get(rid, k) == v for k, v in retained.items())
    assert artifacts.json(receipt["artifact"]) == previous
    assert router._prepare_context("recovery_diagnosis", {}, tmp_path, rid,
        "recovery/diagnose/retained/1", "frozen").record() == current.record()


def test_corrupt_v7_view_is_preserved_and_rejected(tmp_path):
    config, store = configured(tmp_path)
    store.admit("check observation citation recovery", {})
    rid = store.current()["id"]
    artifacts = ArtifactStore(store.root)
    ref = artifacts.put({"version": "context-v7", "purpose": "recovery_diagnosis",
        "payload": {}, "selections": [], "reference_map": {}}, "prepared_context")
    receipt = {"identity": "frozen", "artifact": ref}
    key = "presentation/recovery/diagnose/retained/1/frozen"
    store.checkpoint(rid, key, receipt)
    (artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt")
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    with pytest.raises((ValueError, ProviderError), match="integrity"):
        router._prepare_context("recovery_diagnosis", {}, tmp_path, rid,
                                "recovery/diagnose/retained/1", "frozen")
    assert store.get(rid, key) == receipt
    assert store.get(rid, key + "/" + CONTEXT_VERSION) is None
