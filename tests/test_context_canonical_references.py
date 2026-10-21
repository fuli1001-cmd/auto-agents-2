"""Rendering must not shadow hashes already supplied by the controller."""
import copy
import sys

import pytest
from test_provider_material_index import inputs
from test_provider_prompt_delivery import fixture_router
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner, remap_references
from auto_agents_2.decisions import DecisionRequest, DecisionResult, DecisionService
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.models import ImplementationResult
from auto_agents_2.recovery import Diagnosis, FailureRecord, adjudicate


def stage_context(stage, checks, review_kind=""):
    if stage == "recovery_diagnosis":
        failure = FailureRecord.create(kind="checks", component="verification", origin_id="T1", step="task",
                                       obligations=["R1"], task_id="T1", evidence={"checks": checks})
        return {"failure": failure.model_dump()}
    if stage == "decision":
        request = DecisionRequest(kind="implementation_grant", owner="T1", allowed_choices=["approve", "ask_user"],
                                  scope={}, evidence={"checks": checks}, state_identity="unchanged")
        return {"decision_request": request.model_dump()}
    if stage == "implement":
        return {"feedback": {"checks": checks}}
    if stage == "review":
        if review_kind == "test_changes":
            return {"review_kind": review_kind, "feedback": {"checks": checks}}
        return {"review_kind": review_kind, "checks": checks}
    return {"evidence": checks}


@pytest.mark.parametrize("stage,review_kind", [
    ("implement", ""), ("recovery_diagnosis", ""), ("decision", ""),
    ("review", "implementation"), ("review", "test_changes"), ("verify", ""),
])
def test_compacted_original_attachment_hash_keeps_its_canonical_identity(tmp_path, stage, review_kind):
    _, store = configured(tmp_path)
    context, attachments = inputs(store)
    context = stage_context(stage, context["evidence"], review_kind)
    original = copy.deepcopy(context)
    artifacts = ArtifactStore(store.root)
    citation = attachments[0]["artifact"]["hash"]
    assert citation in controller_context_catalog(context).controller
    prepared = ContextPlanner(artifacts).prepare(stage, context, tmp_path)
    response = {"evidence_refs": [citation]}
    assert remap_references(response, prepared.reference_map) == response
    assert context == original
    assert {row["artifact"]["hash"] for row in attachments} <= {
        row["hash"] for row in prepared.payload["evidence_materials"]}
    if stage == "recovery_diagnosis":
        diagnosis = Diagnosis(action="diagnose", reason="Inspect actual failed checks", evidence_refs=[citation])
        failure = FailureRecord.model_validate(context["failure"])
        assert adjudicate(failure, diagnosis=diagnosis, evidence_context=context).action == "diagnose"
    elif stage == "decision":
        request = DecisionRequest.model_validate(context["decision_request"])
        DecisionService.validate(request, DecisionResult(action="approve", reason="Scoped repair", evidence_refs=[citation]))


def test_canonical_artifact_hash_wins_over_a_generated_material_alias(tmp_path):
    _, store = configured(tmp_path)
    artifacts = ArtifactStore(store.root)
    # Identical JSON bytes can occur as both an original artifact and a rendered observation.
    check = {"argv": ["pytest"], "exit_code": 1, "source_hash": "source", "environment_hash": "environment"}
    original = artifacts.put(check, "machine_observation")
    context = {"controller_observation": original,
               "unresolved_issues": {"first": {"evidence": {"check": check}},
                                     "second": {"evidence": {"check": copy.deepcopy(check)}}}}
    prepared = ContextPlanner(artifacts).prepare("verify", context, tmp_path)
    response = {"evidence_refs": [original["hash"]]}
    assert remap_references(response, prepared.reference_map) == response
    assert original["hash"] in controller_context_catalog(context).controller


@pytest.mark.parametrize("slot", ["failure.proposal", "diagnosis_feedback.diagnosis", "raw_response"])
def test_model_owned_original_hash_is_not_promoted_to_controller_evidence(tmp_path, slot):
    _, store = configured(tmp_path)
    original, attachments = inputs(store)
    failure = FailureRecord.create(kind="proposal", component="implementation", origin_id="T1", step="task",
                                   obligations=["R1"], task_id="T1")
    context = {"failure": failure.model_dump()}
    value = {"checks": original["evidence"]}
    if slot == "failure.proposal":
        context["failure"]["proposal"] = value
    elif slot == "diagnosis_feedback.diagnosis":
        context["diagnosis_feedback"] = {"diagnosis": value}
    else:
        context[slot] = value
    citation = attachments[0]["artifact"]["hash"]
    assert citation not in controller_context_catalog(context).controller
    prepared = ContextPlanner(ArtifactStore(store.root)).prepare("recovery_diagnosis", context, tmp_path)
    with pytest.raises(AA2Error):
        response = remap_references({"action": "diagnose", "reason": "Only model-owned material",
                                     "evidence_refs": [citation]}, prepared.reference_map)
        adjudicate(failure, diagnosis=Diagnosis.model_validate(response), evidence_context=context)


@pytest.mark.parametrize("stage,model", [("recovery_diagnosis", Diagnosis), ("implement", ImplementationResult)])
async def test_native_response_can_cite_an_original_hash_read_from_the_attachment_index(tmp_path, stage, model):
    router, project, store, rid = fixture_router(tmp_path)
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + '''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli canonical-citations")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split("<context>\\n", 1)[1].split("\\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    descriptor = context["evidence_materials"]
    assert isinstance(descriptor, dict)
    index = pathlib.Path(descriptor["artifact"]["location"])
    assert hashlib.sha256(index.read_bytes()).hexdigest() == descriptor["artifact"]["hash"]
    candidates = [ref for ref in json.loads(index.read_text()) if ref["kind"] == "test_diagnostic"]
    observed = next(ref for ref in candidates if json.loads(pathlib.Path(ref["location"]).read_text())["case"] == 0)
    assert hashlib.sha256(pathlib.Path(observed["location"]).read_bytes()).hexdigest() == observed["hash"]
    if "action" in schema["properties"]:
        result = {"action": "diagnose", "task_ids": ["T1"], "evidence_refs": [observed["hash"]],
                  "reason": "Inspect actual failed check; all gates remain", "repair_targets": []}
    else:
        result = {"summary": "Request scoped diagnosis", "changes": [], "replan_stage": "", "replan_reason": "",
                  "recovery_request": {"reason": "Inspect actual failed check", "suggested_action": "diagnose",
                                       "evidence_refs": [observed["hash"]]}}
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
''')
    original, attachments = inputs(store)
    context = stage_context(stage, original["evidence"])
    before = copy.deepcopy(context)
    result = await router.invoke(stage, context, model, project, rid, "canonical-response", fresh=True)
    references = result.evidence_refs if stage == "recovery_diagnosis" else result.recovery_request.evidence_refs
    assert references == [attachments[0]["artifact"]["hash"]]
    assert context == before and store.get(rid, "develop/done") is None
    assert store.get(rid, "infra/canonical-response/codex")["failures"] == 0
    assert store.get(rid, "provider-result/canonical-response")
    assert not list((store.root / "processes").glob("*.json"))
