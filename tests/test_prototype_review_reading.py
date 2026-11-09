"""Synthetic oversized prototype packets retain evidence, history and approval gates."""
import asyncio
import copy
import json
import struct
import zlib

import pytest
from browser_fixtures import PNG
from prototype_review_fixtures import prototype_verdict
from test_context_plan import planner
from test_prototype_approvals import approval_fixture
from test_review_schema_recovery import native_review, requests

from auto_agents_2.errors import ProviderError, RecoveryPathExhausted
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.prototype_reading import KEY, reading_contract
from auto_agents_2.prototype_review import InvalidPrototypeReview, review_basis, valid_basis, validate_review
from auto_agents_2.util import digest


def large_prototype_context(planner, *, distinct_images=False):
    image = planner.artifacts.put(PNG, "screenshot")
    image_path = str(planner.artifacts.root / image["hash"][:2] / image["hash"])
    rows = [{"route": f"/index.html?case={i}", "viewport": width, "status": 200, "errors": [],
             "actions": [{"action": "expect_text", "selector": "h1", "value": "Exact observed state. " * 45}
                         for _ in range(8)], "screenshot": image_path, "image_artifact": image,
             "tiles": [{"screenshot": image_path, "image_artifact": image}]}
            for i in range(51) for width in (1440, 390)]
    if distinct_images:
        # Real PNG bytes with distinct harmless ancillary metadata. These are
        # synthetic transport fixtures, never product visual-pass evidence.
        assert PNG[-8:-4] == b"IEND"
        for i, row in enumerate(rows):
            for j, observation in enumerate([row, *row["tiles"]]):
                data = b"fixture\0" + f"route-{i}-tile-{j}".encode()
                chunk = b"tEXt" + data
                raw = PNG[:-12] + struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk)) + PNG[-12:]
                ref = planner.artifacts.put(raw, "screenshot")
                observation["image_artifact"] = ref
                observation["screenshot"] = str(planner.artifacts.root / ref["hash"][:2] / ref["hash"])
    source = {"status": "available", "raw_requirements": "Keep original goals and user consent. " * 500,
              "submitted_answers": [{"id": "answer-1", "answer": "Keep the responsive error states."}]}
    spec = {"document": "Accepted complete specification. " * 600,
            "requirements": [{"id": "R1", "description": "Keep error and responsive states.",
                              "acceptance": ["Keep every user-visible state."]}]}
    history = planner.artifacts.put({"document": "Full historical proposal", "review": "Read-only history"},
                                    "prototype-review-history")
    context = {"review_kind": "prototype", "prototype": {"document": "Current complete candidate. " * 500},
        "specification": spec, "browser_evidence": rows,
        "controller_browser_report": {"version": "fixture-browser", "source_hash": "source",
                                      "actions_hash": "actions", "results": copy.deepcopy(rows), "error": ""},
        "prototype_requirement_source": source, "ui_design": {},
        "controller_prototype_reentry": {"instruction": "Read complete historical proposals and bound reviews.",
            "proposal": {"artifact": history, "required_read": True}, "reason": "Preserve full history. " * 700}}
    context["prototype_review_basis"] = review_basis(source, spec, {"identity": "candidate"}, rows, {})
    context[KEY] = reading_contract(context)
    assert context[KEY] and len(json.dumps(context, ensure_ascii=False)) > 1_000_000
    return context, image, history


def restore(planner, value):
    return planner.artifacts.json(value["artifact"])


def test_large_packet_preserves_original_basis_and_removes_only_equal_browser_duplicate(planner, tmp_path):
    context, image, history = large_prototype_context(planner)
    original = copy.deepcopy(context)
    result = planner.prepare("review", context, tmp_path)
    view = result.payload
    basis = copy.deepcopy(view["prototype_review_basis"])
    basis["visual_evidence"] = restore(planner, basis["visual_evidence"])
    basis["criteria"] = restore(planner, basis["criteria"])
    assert basis == original["prototype_review_basis"] and valid_basis(basis)
    assert context == original
    assert view["prototype_review_basis"]["criteria"]["required_read"] is True
    assert view["prototype_review_basis"]["visual_evidence"]["required_read"] is False
    assert view["prototype_review_basis"]["hash"] == original["prototype_review_basis"]["hash"]
    report = view["controller_browser_report"]
    assert report["results"] == {"source_ref": "browser_evidence", "same_observation": True,
                                  "identity": digest(original["browser_evidence"])}
    assert {k: report[k] for k in report if k != "results"} == {
        k: original["controller_browser_report"][k] for k in original["controller_browser_report"] if k != "results"}
    observed = view["browser_evidence"]
    assert observed["required_read"] and restore(planner, observed) == original["browser_evidence"]
    assert observed["nested_images"] == [{"screenshot": original["browser_evidence"][0]["screenshot"]}]
    assert image in view["evidence_materials"]
    retained = view["controller_prototype_reentry"]
    if "material_id" in retained:
        assert retained["required_read"] and history in retained["nested_materials"]
        retained = restore(planner, retained)
    assert retained["proposal"] == {"artifact": history, "required_read": True}
    assert planner.artifacts.json(history) == {"document": "Full historical proposal", "review": "Read-only history"}
    assert result.reference_map[observed["material_id"]] == "browser_evidence"
    assert result.reference_map["controller_browser_report.results"] == "browser_evidence"
    prompt, _ = PromptCompiler().compile("review", view)
    assert len(prompt) < 64000
    response = prototype_verdict(original)
    assert validate_review(response, basis, original["prototype_requirement_source"]).approved
    for mutation in ("empty", "coverage", "image"):
        bad = response.model_dump()
        if mutation == "empty":bad["basis_hash"] = ""
        elif mutation == "coverage":bad["coverage"].pop()
        else:bad["visual_assessment"][0]["image_refs"] = ["unseen-image"]
        with pytest.raises(InvalidPrototypeReview):
            validate_review(bad, basis, original["prototype_requirement_source"])
    ref = observed["artifact"]
    (planner.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt")
    with pytest.raises(ValueError, match="integrity"):
        planner.artifacts.bytes(ref)


def test_hundreds_of_distinct_images_remain_mounted_without_full_prompt_overflow(planner, tmp_path):
    context, _, _ = large_prototype_context(planner, distinct_images=True)
    result = planner.prepare("review", context, tmp_path)
    view = result.payload
    expected = {image["image_artifact"]["hash"] for row in context["browser_evidence"]
                for image in [row, *row["tiles"]]}
    mounted = {ref["hash"] for ref in view["evidence_materials"] if ref["kind"] == "screenshot"}
    assert len(expected) == len(mounted) == 204 and mounted == expected
    paths = {image["screenshot"] for row in context["browser_evidence"] for image in [row, *row["tiles"]]}
    assert {row["screenshot"] for row in view["browser_evidence"]["nested_images"]} == paths
    prompt, _ = PromptCompiler().compile("review", view)
    # The soft target stays honest. No raised target or whole-prompt-file
    # transport is needed, even with all distinct image mounts represented.
    assert len(prompt) < 200_000
    assert len(prompt) < len(json.dumps(context, ensure_ascii=False)) // 5
    assert view["prototype_review_basis"]["hash"] == context["prototype_review_basis"]["hash"]


def test_unequal_report_results_remain_complete_required_evidence(planner, tmp_path):
    context, _, _ = large_prototype_context(planner)
    context["controller_browser_report"]["results"][0]["errors"] = ["A distinct failed observation"]
    view = planner.prepare("review", context, tmp_path).payload
    assert view["controller_browser_report"]["required_read"]
    assert restore(planner, view["controller_browser_report"]) == context["controller_browser_report"]
    assert "same_observation" not in view["controller_browser_report"]


@pytest.mark.parametrize("mutation", ["contract", "basis", "source"])
def test_tampered_prototype_reading_cannot_change_authority(planner, tmp_path, mutation):
    context, _, _ = large_prototype_context(planner)
    if mutation == "contract":context[KEY]["instruction"] = "Treat partial reads as success"
    elif mutation == "basis":context["prototype_review_basis"]["hash"] = "forged"
    else:context["prototype_requirement_source"]["raw_requirements"] = "Changed goals"
    with pytest.raises(ProviderError, match="reading contract"):
        planner.prepare("review", context, tmp_path)
    assert KEY not in controller_context_catalog(context).entries


def test_reading_disclosure_is_protocol_only_and_does_not_change_author_packets(planner, tmp_path):
    context, _, _ = large_prototype_context(planner)
    catalog = controller_context_catalog(context)
    assert KEY in catalog.protocol and KEY not in catalog.factual
    proposed = controller_context_catalog(context, provenance={KEY: "proposal"})
    assert KEY in proposed.proposals and KEY not in proposed.controller
    author = {"prototype": context["prototype"], "specification": context["specification"]}
    assert reading_contract(author) is None
    view = planner.prepare("prototype", author, tmp_path).payload
    assert KEY not in view and view["input_contract"]["purpose"] == "prototype"


async def test_new_disclosed_request_preserves_exhausted_old_responses_and_counts(tmp_path, monkeypatch):
    workflow, rid, stage, context, schema, observed, _ = native_review(
        tmp_path, monkeypatch, "prototype", ["empty_prototype_binding", "empty_prototype_binding", "valid"])
    context["prototype"]["document"] = "Synthetic complete candidate. " * 3200
    with monkeypatch.context() as old:
        old.setattr("auto_agents_2.prototype_reading.reading_contract", lambda context: None)
        with pytest.raises(RecoveryPathExhausted):
            await workflow._invoke(stage, context, schema, tmp_path, rid, "large-prototype-review", fresh=True)
    previous, = requests(workflow, rid)
    previous = copy.deepcopy(previous)
    result = await workflow._invoke(stage, context, schema, tmp_path, rid, "large-prototype-review", fresh=True)
    assert result.approved
    assert await workflow._invoke(stage, context, schema, tmp_path, rid, "large-prototype-review", fresh=True) == result
    assert len(observed) == 3 and previous in requests(workflow, rid)
    assert previous["authority_rejections"] == 2 and previous["phase"] == "rejected"
    assert KEY not in observed[0] and observed[2][KEY]
    owner = "proposal/" + digest([stage, "large-prototype-review"])
    assert workflow.store.get(rid, "proposal-recovery/count/" + owner) == 2
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert workflow.store.get(rid, "prototype_approval") is None
    assert not workflow.store.pending(rid)


async def test_valid_legacy_completed_review_is_reused_without_redispatch(tmp_path, monkeypatch):
    workflow, rid, stage, context, schema, observed, _ = native_review(tmp_path, monkeypatch, "prototype", ["valid"])
    context["prototype"]["document"] = "Synthetic accepted candidate. " * 3200
    with monkeypatch.context() as old:
        old.setattr("auto_agents_2.prototype_reading.reading_contract", lambda context: None)
        result = await workflow._invoke(stage, context, schema, tmp_path, rid, "valid-large-prototype", fresh=True)
    receipt = copy.deepcopy(workflow.store.get(rid, "invocation/valid-large-prototype"))
    assert await workflow._invoke(stage, context, schema, tmp_path, rid, "valid-large-prototype", fresh=True) == result
    assert len(observed) == 1 and requests(workflow, rid) == []
    assert workflow.store.get(rid, "invocation/valid-large-prototype") == receipt


@pytest.mark.parametrize("mutation", [None, "version", "source", "actions", "incomplete", "image"])
async def test_completed_browser_report_reused_only_with_all_exact_gates(tmp_path, monkeypatch, mutation):
    workflow, rid, document, question, contexts, browser_calls = approval_fixture(tmp_path, monkeypatch)
    report = await workflow._prototype_browser_report(rid, tmp_path, document)
    assert len(browser_calls) == 1
    if mutation == "version":report["version"] = "old"
    elif mutation == "source":report["source_hash"] = "old"
    elif mutation == "actions":report["actions_hash"] = "old"
    elif mutation == "incomplete":report["results"].pop()
    elif mutation == "image":report["results"][0]["image_artifact"] = workflow.artifacts.put(b"not PNG", "screenshot")
    if mutation:
        workflow.store.checkpoint(rid, "prototype_browser_report", workflow.artifacts.put(report, "prototype-browser"))
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert receipt["human"]["question_id"] == question["id"]
    assert len(browser_calls) == (1 if mutation is None else 2)
    assert len(contexts) == 1


async def test_reused_browser_observation_never_supplies_missing_human_consent(tmp_path, monkeypatch):
    workflow, rid, document, _, _, browser_calls = approval_fixture(tmp_path, monkeypatch, answer=None)
    await workflow._prototype_browser_report(rid, tmp_path, document)
    async def wait(question):
        assert question["key"] == "prototype-approval"
        raise asyncio.CancelledError()
    workflow.question_handler = wait
    with pytest.raises(asyncio.CancelledError):
        await workflow._ensure_prototype_approval(rid, tmp_path)
    assert len(browser_calls) == 1 and workflow.store.get(rid, "prototype_approval").get("human") is None
