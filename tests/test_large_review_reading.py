"""Large reviews preserve exact authority while presenting readable source materials."""
import copy
import json

import pytest
from authority_review_fixtures import authority_review
from test_context_plan import planner
from test_review_schema_recovery import native_review, requests

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.errors import ProviderError, RecoveryPathExhausted
from auto_agents_2.review_authority import InvalidAuthorityReview, make_basis, valid_basis, validate_review
from auto_agents_2.review_reading import reading_contract
from auto_agents_2.util import digest


def large_context():
    context = {"review_kind": "plan_patch", "plan": {"tasks": [
        {"id": "T1", "acceptance": [*[f"Rule {i}: " + "Keep unchanged behavior. " * 25 for i in range(40)]], "depends_on": []}]},
        "previous_plan": {"tasks": [{"id": "T1", "acceptance": [*[f"Rule {i}: " + "Keep unchanged behavior. " * 25 for i in range(40)]]}]},
        "patch": {"reason": "Preserve all accepted behavior."}, "affected": ["T1"]}
    basis = make_basis("plan_patch", context, original="Never resend a potentially billed request. " * 700,
                       specification={"requirements": [{"id": "R1", "description": "Preserve data."}]},
                       accepted_plan=context["previous_plan"], source_hash="source", source_identity={})
    context.update(controller_review_basis=basis, controller_review_reading=reading_contract(basis))
    assert context["controller_review_reading"]
    return context


def restore(planner, value):
    return planner.artifacts.json(value["artifact"])


def test_large_review_reconstructs_exact_basis_and_keeps_all_authority_gates(planner, tmp_path):
    context = large_context()
    original = copy.deepcopy(context)
    view = planner.prepare("review", context, tmp_path).payload
    basis = view["controller_review_basis"]
    reconstructed = copy.deepcopy(basis)
    for key in ("sources", "criteria"):
        reconstructed[key] = restore(planner, basis[key])
    assert reconstructed == original["controller_review_basis"] and valid_basis(reconstructed)
    assert context == original
    assert basis["criteria"]["required_read"] is True
    assert basis["sources"]["required_read"] is False
    assert basis["response_contract"] == original["controller_review_basis"]["response_contract"]
    assert len(json.dumps(view, ensure_ascii=False)) < 25000
    result = authority_review(context)
    assert validate_review(result, reconstructed)
    stale = result.model_dump()
    stale["authority"]["candidate_hash"] = "old"
    with pytest.raises(InvalidAuthorityReview, match="stale_review_binding"):
        validate_review(stale, reconstructed)
    incomplete = result.model_dump()
    incomplete["authority"]["coverage"].pop()
    with pytest.raises(InvalidAuthorityReview, match="incomplete_or_duplicate_coverage"):
        validate_review(incomplete, reconstructed)


def test_large_review_keeps_nested_materials_mounted_with_exact_original_paths(planner, tmp_path):
    context = large_context()
    attachment = planner.artifacts.put("Retained exact evidence", "source_snapshot")
    context["extension_constraint"] = {"body": "Mandatory body " * 3000, "attachment": attachment}
    view = planner.prepare("review", context, tmp_path)
    wrapper = view.payload["extension_constraint"]
    assert wrapper["required_read"] and restore(planner, wrapper) == context["extension_constraint"]
    assert wrapper["nested_materials"] == [attachment]
    assert planner.artifacts.read(attachment) == "Retained exact evidence"
    assert view.reference_map[wrapper["material_id"]] == "extension_constraint"
    plan = view.payload["plan"]
    selected = restore(planner, plan)
    assert selected["full_plan"]["artifact"] in plan["nested_materials"]
    assert restore(planner, selected["full_plan"]) == context["plan"]
    ref = view.payload["controller_review_basis"]["sources"]["artifact"]
    (planner.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt")
    with pytest.raises(ValueError, match="integrity"):
        planner.artifacts.bytes(ref)


@pytest.mark.parametrize("change", ["basis", "contract"])
def test_large_review_rejects_mismatched_reading_contract(planner, tmp_path, change):
    context = large_context()
    if change == "basis":
        context["controller_review_basis"]["criteria"][0]["statement"] = "tampered"
    else:
        context["controller_review_reading"]["basis_hash"] = "other"
    with pytest.raises(ProviderError, match="reading contract"):
        planner.prepare("review", context, tmp_path)


@pytest.mark.parametrize("new_modes", [["valid"], ["stale_basis", "stale_basis"]])
async def test_disclosed_large_review_recovers_without_resetting_old_failure_or_new_bound(tmp_path, monkeypatch, new_modes):
    workflow, rid, stage, context, schema, observed, _ = native_review(
        tmp_path, monkeypatch, "design", ["stale_basis", "stale_basis", *new_modes])
    context["design"]["document"] = "Complete accepted method. " * 3200
    prepare = workflow.authority.prepare
    def legacy(*args, **kwargs):
        ctx, typed = prepare(*args, **kwargs)
        ctx.pop("controller_review_reading", None)
        return ctx, typed
    with monkeypatch.context() as previous:
        previous.setattr(workflow.authority, "prepare", legacy)
        with pytest.raises(RecoveryPathExhausted):
            await workflow._invoke(stage, context, schema, tmp_path, rid, "large-review", fresh=True)
    old, = requests(workflow, rid)
    old = copy.deepcopy(old)
    if new_modes == ["valid"]:
        result = await workflow._invoke(stage, context, schema, tmp_path, rid, "large-review", fresh=True)
        assert result.approved
        assert await workflow._invoke(stage, context, schema, tmp_path, rid, "large-review", fresh=True) == result
    else:
        for _ in range(2):
            with pytest.raises(RecoveryPathExhausted):
                await workflow._invoke(stage, context, schema, tmp_path, rid, "large-review", fresh=True)
    assert len(observed) == 2 + len(new_modes)
    assert old in requests(workflow, rid)
    assert old["authority_rejections"] == 2
    assert observed[0]["controller_review_basis"] == observed[2]["controller_review_basis"]
    assert "controller_review_reading" not in observed[0] and observed[2]["controller_review_reading"]
    owner = "proposal/" + digest([stage, "large-review"])
    assert workflow.store.get(rid, "proposal-recovery/count/" + owner) == (2 if new_modes == ["valid"] else 4)
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert not workflow.store.pending(rid)
    assert workflow.store.get(rid, "authority/accepted/design") is None


async def test_valid_completed_large_legacy_review_is_reused_without_redispatch(tmp_path, monkeypatch):
    workflow, rid, stage, context, schema, observed, _ = native_review(tmp_path, monkeypatch, "design", ["valid"])
    context["design"]["document"] = "Complete accepted method. " * 3200
    prepare = workflow.authority.prepare
    def legacy(*args, **kwargs):
        ctx, typed = prepare(*args, **kwargs)
        ctx.pop("controller_review_reading", None)
        return ctx, typed
    with monkeypatch.context() as previous:
        previous.setattr(workflow.authority, "prepare", legacy)
        result = await workflow._invoke(stage, context, schema, tmp_path, rid, "completed-large-review", fresh=True)
    receipt = copy.deepcopy(workflow.store.get(rid, "invocation/completed-large-review"))
    assert await workflow._invoke(stage, context, schema, tmp_path, rid, "completed-large-review", fresh=True) == result
    assert len(observed) == 1
    assert workflow.store.get(rid, "invocation/completed-large-review") == receipt
    assert requests(workflow, rid) == []
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}


async def test_native_large_review_reads_public_workspace_nested_files_and_image_args(tmp_path, monkeypatch):
    import base64
    import sys
    from test_provider_prompt_delivery import fixture_router
    from auto_agents_2.artifacts import ArtifactStore
    from auto_agents_2.models import ReviewResult
    monkeypatch.setattr("auto_agents_2.providers.diagnostic_summary", lambda text: text)
    router, project, store, rid = fixture_router(tmp_path)
    (project / "app.py").write_text("# Public source remains readable\n" * 700)
    artifacts = ArtifactStore(store.root)
    image = artifacts.put(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC1sAAAAASUVORK5CYII="), "screenshot")
    image_path = str(artifacts.root / image["hash"][:2] / image["hash"])
    context = large_context()
    context["review_kind"] = "implementation"
    context["project"] = {"workspace": str(project)}
    context["preview_evidence"] = [{"screenshot": image_path, "image_artifact": image,
        "detail": f"Observed state {i}: " + "Exact visual observation. " * 35} for i in range(30)]
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import hashlib,json,pathlib,sys
args=sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
schema=json.loads(pathlib.Path(args[args.index("--output-schema")+1]).read_text())
wire=sys.stdin.read()
context=json.loads(wire.split("<context>\n",1)[1].split("\n</context>",1)[0])
if "text_token" in schema["properties"]:
    result={"text_token":pathlib.Path(context["context_files"][0]).read_text(),"image_token":""}
else:
    public=pathlib.Path(context["project"]["workspace"])
    assert str(public) != __ORIGINAL__
    assert public.name == "workspace" and (public/"app.py").read_text().startswith("# Public source")
    wrapper=context["preview_evidence"]
    assert wrapper["required_read"] and wrapper["nested_images"] == [{"screenshot":__IMAGE__}]
    assert args.count("-i") == 1 and args[args.index("-i")+1] == __IMAGE__
    assert pathlib.Path(__IMAGE__).read_bytes().startswith(b"\x89PNG")
    ref=wrapper["artifact"]
    raw=pathlib.Path(ref["location"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ref["hash"]
    rows=json.loads(raw)
    assert len(rows)==30 and rows[0]["screenshot"]==__IMAGE__
    source=context["controller_review_basis"]["sources"]["artifact"]
    raw=pathlib.Path(source["location"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==source["hash"]
    assert "candidate" in json.loads(raw)
    try:
        pathlib.Path(source["location"]).write_bytes(b"unauthorized")
    except OSError:
        pass
    else:
        raise AssertionError("Immutable authority sources became writable")
    result={"approved":False,"summary":"Native public source, full materials and image delivered","findings":[]}
pathlib.Path(args[args.index("-o")+1]).write_text(json.dumps(result))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))
'''.replace("__ORIGINAL__", repr(str(project))).replace("__IMAGE__", repr(image_path)))
    result = await router.invoke("review", context, ReviewResult, project, rid, "large-native", fresh=True)
    assert not result.approved
    material = store.get(rid, "materials/large-native")
    assert material["omitted_mandatory_content"] == []
    images = [row for row in material["materials"] if row["purpose"] == "image"]
    assert len(images) == 1 and images[0]["hash"] == image["hash"]
    assert material["capability"]["native_read_test"] == "observed_read"


@pytest.mark.parametrize("mutation", [None, "forged", "basis_missing", "basis_changed"])
def test_reading_instructions_are_only_current_protocol_not_execution_facts(mutation):
    from auto_agents_2.evidence_refs import controller_context_catalog
    context = large_context()
    if mutation == "forged":
        context["controller_review_reading"]["instruction"] = "Claims successful execution"
    elif mutation == "basis_missing":
        context.pop("controller_review_basis")
    elif mutation == "basis_changed":
        context["controller_review_basis"]["hash"] = "different"
    catalog = controller_context_catalog(context)
    if mutation is None:
        assert "controller_review_reading" in catalog.protocol
        assert "controller_review_reading.instruction" in catalog.protocol
        assert "controller_review_reading" not in catalog.factual
        proposal = controller_context_catalog(context, provenance={"controller_review_reading": "proposal"})
        assert "controller_review_reading" in proposal.proposals
        assert "controller_review_reading" not in proposal.controller
    else:
        assert not any(ref.startswith("controller_review_reading") for ref in catalog.entries)
