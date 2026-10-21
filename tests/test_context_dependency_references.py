"""Admitted attachments retain citation provenance through their controller roots."""
import copy

import pytest
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import CONTEXT_VERSION, ContextPlanner, remap_references
from auto_agents_2.decisions import DecisionRequest, DecisionResult, DecisionService
from auto_agents_2.errors import AA2Error, InvalidStructuredResponse
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import Diagnosis, FailureRecord, adjudicate
from auto_agents_2.sandbox import SandboxRunner


def fixture(tmp_path, kind="source_proposal"):
    config, store = configured(tmp_path)
    rid = store.admit("dependency citation", {})["id"]
    artifacts = ArtifactStore(store.root)
    child = artifacts.put({"changes": [{"path": "test_old.py", "content": "assert original()"}]}, kind)
    root = artifacts.put({"proposals": [{"status": "proposal_only", "content": child}]}, "source_history")
    failure = FailureRecord.create(kind="proposal", component="implementation", origin_id="T1", step="task",
                                   obligations=["R1"], evidence={"checks": []})
    context = {"failure": failure.model_dump(), "source_history": {"artifact": root, "status": "available"}}
    return config, store, rid, artifacts, child, root, failure, context


@pytest.mark.parametrize("kind", ["source_proposal", "source_snapshot", "source_diff"])
def test_direct_history_children_can_be_cited_without_becoming_verified(tmp_path, kind):
    _, store, rid, artifacts, child, root, failure, context = fixture(tmp_path, kind)
    original = copy.deepcopy(context)
    prepared = ContextPlanner(artifacts).prepare("recovery_diagnosis", context, tmp_path)
    assert child in prepared.payload["evidence_materials"]
    result = remap_references({"action": "diagnose", "reason": "Inspect retained material", "evidence_refs": [child["hash"]]},
                              prepared.reference_map)
    assert adjudicate(failure, diagnosis=Diagnosis.model_validate(result), evidence_context=context).action == "diagnose"
    assert context == original
    assert artifacts.json(root)["proposals"][0]["status"] == "proposal_only"
    assert not store.get(rid, "develop/done") and not (tmp_path / "test_old.py").exists()
    with pytest.raises(AA2Error, match="not supplied"):
        adjudicate(failure, diagnosis=Diagnosis(action="diagnose", reason="Unprovided", evidence_refs=["f" * 64]),
                   evidence_context=context)


@pytest.mark.parametrize("compact", [False, True])
def test_resolution_and_log_citations_work_for_direct_and_compacted_records(tmp_path, compact):
    _, _, _, artifacts, _, _, failure, _ = fixture(tmp_path)
    resolution = artifacts.put({"packages": [{"name": "fixture"}]}, "environment_resolution")
    log = artifacts.put("original diagnostic", "check_log")
    record = {"checks": [{"environment_resolution": resolution, "logs": [log]}]}
    context = {"failure": failure.model_dump()}
    if compact:
        context["observations"] = record
    else:
        context["failure"]["evidence"]["archive"] = artifacts.put(record, "observation")
    prepared = ContextPlanner(artifacts).prepare("recovery_diagnosis", context, tmp_path)
    assert {resolution["hash"], log["hash"]} <= {ref["hash"] for ref in prepared.payload["evidence_materials"]}
    result = remap_references({"action": "diagnose", "reason": "Inspect observed dependencies",
                              "evidence_refs": [resolution["hash"], log["hash"]]}, prepared.reference_map)
    assert adjudicate(failure, diagnosis=Diagnosis.model_validate(result), evidence_context=context).action == "diagnose"


def test_attachment_from_model_owned_root_cannot_establish_controller_facts(tmp_path):
    _, _, _, artifacts, child, root, failure, _ = fixture(tmp_path)
    failure.proposal = {"attachment": root}
    context = {"failure": failure.model_dump()}
    prepared = ContextPlanner(artifacts).prepare("recovery_diagnosis", context, tmp_path)
    assert child in prepared.payload["evidence_materials"]
    result = remap_references({"action": "diagnose", "reason": "Unverified suggestion", "evidence_refs": [child["hash"]]},
                              prepared.reference_map)
    with pytest.raises(AA2Error, match="requires controller evidence"):
        adjudicate(failure, diagnosis=Diagnosis.model_validate(result), evidence_context=context)
    result["evidence_refs"].append("failure.evidence")
    assert adjudicate(failure, diagnosis=Diagnosis.model_validate(result), evidence_context=context).action == "diagnose"


@pytest.mark.parametrize("direct_child", [False, True])
def test_shared_attachment_requires_unambiguous_provenance_unless_directly_supplied(tmp_path, direct_child):
    _, _, _, artifacts, child, root, failure, context = fixture(tmp_path)
    failure.proposal = {"attachment": artifacts.put({"content": child, "status": "unverified"}, "source_history")}
    context["failure"] = failure.model_dump()
    if direct_child:
        context["known_attachment"] = child
    prepared = ContextPlanner(artifacts).prepare("recovery_diagnosis", context, tmp_path)
    response = {"action": "diagnose", "reason": "Inspect attachment", "evidence_refs": [child["hash"]]}
    if direct_child:
        result = remap_references(response, prepared.reference_map)
        assert result == response
        assert adjudicate(failure, diagnosis=Diagnosis.model_validate(result), evidence_context=context).action == "diagnose"
    else:
        with pytest.raises(InvalidStructuredResponse, match="Ambiguous"):
            remap_references(response, prepared.reference_map)


def test_decision_attachment_citations_use_the_same_original_evidence_paths(tmp_path):
    _, _, _, artifacts, child, _, _, context = fixture(tmp_path)
    request = DecisionRequest(kind="implementation_grant", owner="T1", allowed_choices=["approve", "ask_user"],
                              scope={}, evidence=context, state_identity="unchanged")
    prepared = ContextPlanner(artifacts).prepare("decision", {"decision_request": request.model_dump()}, tmp_path)
    result = remap_references({"action": "approve", "reason": "Inspect retained material", "evidence_refs": [child["hash"]]},
                              prepared.reference_map)
    DecisionService.validate(request, DecisionResult.model_validate(result))


@pytest.mark.parametrize("old_version", ["context-v1", "context-v2", "context-v3", "context-v4"])
def test_pending_legacy_view_upgrades_citations_without_rewriting_receipts(tmp_path, old_version):
    config, store, rid, artifacts, child, _, failure, context = fixture(tmp_path)
    old = ContextPlanner(artifacts).prepare("recovery_diagnosis", context, tmp_path).record()
    old["version"] = old_version
    old["reference_map"].pop(child["hash"], None)
    key = "presentation/recovery/diagnose/example/1/frozen"
    receipt = {"identity": "frozen", "artifact": artifacts.put(old, "prepared_context")}
    store.checkpoints(rid, {key: receipt, "recovery/budgets/diagnosis/T1": {"round": 8}})
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    current = router._prepare_context("recovery_diagnosis", context, tmp_path, rid, "recovery/diagnose/example/1", "frozen")
    result = remap_references({"action": "diagnose", "reason": "Inspect", "evidence_refs": [child["hash"]]}, current.reference_map)
    assert adjudicate(failure, diagnosis=Diagnosis.model_validate(result), evidence_context=context).action == "diagnose"
    assert current.version == CONTEXT_VERSION and CONTEXT_VERSION != old_version
    assert store.get(rid, key) == receipt and artifacts.json(receipt["artifact"]) == old
    assert store.get(rid, "recovery/budgets/diagnosis/T1") == {"round": 8}
    assert router._prepare_context("recovery_diagnosis", {}, tmp_path, rid, "recovery/diagnose/example/1", "frozen").record() == current.record()
