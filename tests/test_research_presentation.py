import copy
import json

from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.research import request_documents
from auto_agents_2.util import digest


def evidence(body, *, snapshot="v1", **claim):
    return {"url": "https://docs.example/api#" + claim.get("claim", ""), "version": "claimed", **claim,
            "retrieval": {"artifact": {"hash": digest(body), "kind": "research_extracted", "bytes": len(body.encode())},
                          "raw": {"hash": digest(snapshot), "kind": "research_response", "bytes": 1},
                          "extractor_version": "v2", "final_url": "https://docs.example/api"},
            "excerpt": body}


def test_identical_snapshot_inline_once_preserves_every_claim_and_constraint():
    body = "API requires permission X.\r\nStreaming is unsupported.\nVersion 2 only."
    rows = [evidence(body, claim="permission", version="1", validation="must reject without X"),
            evidence(body, claim="streaming", version="2", validation="must not invent streaming")]
    context = {"retrieved_sources": rows, "specification": {"mandatory": "preserve all acceptance conditions"}}
    original = copy.deepcopy(context)
    presented = request_documents(context)
    assert context == original
    assert len(presented["research_document_bodies"]) == 1
    doc = presented["research_document_bodies"][0]
    assert doc["body"] == body
    assert presented["specification"] == context["specification"]
    for old, new in zip(rows, presented["retrieved_sources"]):
        reconstructed = {k: v for k, v in new.items() if k != "document_id"}
        reconstructed["excerpt"] = doc["body"]
        assert new["document_id"] == doc["id"] and reconstructed == old
    assert request_documents(presented) == presented


def test_different_versions_or_text_are_not_merged():
    first = evidence("text", snapshot="v1", claim="one")
    second = evidence("text", snapshot="v2", claim="two")
    third = evidence("text\n", snapshot="v1", claim="three")
    context = {"retrieved_sources": [first, second, third]}
    assert request_documents(context) == context


def test_no_semantic_dedup_or_cross_role_constraint_removal():
    context = {"task": {"acceptance": ["Do not weaken tests"]}, "policy": "Do not weaken tests",
               "notes": [{"excerpt": "same"}, {"excerpt": "same"}]}
    assert request_documents(context) == context


def test_actual_compiled_prompt_has_single_body_and_no_extra_read_step():
    body = "Mandatory version rule. " * 1000
    rows = [evidence(body, claim=str(i), validation=f"condition {i}") for i in range(5)]
    prompt, _ = PromptCompiler().compile("review", {"review_kind": "research", "retrieved_sources": rows})
    payload = json.loads(prompt.split("<context>\n", 1)[1].split("\n</context>", 1)[0])
    assert prompt.count(body) == 1
    assert len(payload["retrieved_sources"]) == 5
    assert payload["research_document_bodies"][0]["body"] == body
    assert "context_files" not in payload
    assert len(json.dumps(payload)) < len(json.dumps({"retrieved_sources": rows})) / 2
