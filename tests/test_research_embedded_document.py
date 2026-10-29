"""Public document hydration is evidence, never a fact or an approval."""
import base64
import copy
import json

import httpx
import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.collection_worker import execute
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import SourceEvidence
from auto_agents_2.requirements import extract_document
from auto_agents_2.research import ResearchService
from auto_agents_2.research_sections import extract_sections
from auto_agents_2.store import Store
from auto_agents_2.util import digest

URL = "https://docs.volcengine.com/docs/voice/example?lang=zh"
BODY = "# Contract\n\nPOST /api/example\n\n| Parameter | Rule |\n| --- | --- |\n| model | required |\n\nDo not retry unknown submissions.\n[Limits](/docs/voice/limits#quota)\n"


def state():
    return {"loaderData": {"docs/(libid)/(docid$)/page": {
        "curLibId": 7, "curDocId": 9, "curLibCode": "voice", "curDocCode": "example",
        "curLibRouteKey": "voice", "curDocRouteKey": "example",
        "curDoc": {"LibraryID": 7, "DocumentID": 9, "LibraryCode": "voice",
                   "DocumentCode": "example", "Title": "Example contract", "MDContent": BODY,
                   "UpdatedTime": "2026-10-29T00:00:00Z", "RedirectInfo": None}}},
        "errors": None}


def html(data=None, suffix="", repeat=False):
    script = "window._ROUTER_DATA = " + json.dumps(state() if data is None else data) + suffix
    block = "<script>" + script + "</script>"
    return ("<h1>Public docs</h1><p>Visible caveat.</p>" + block * (2 if repeat else 1)).encode()


def capture(tmp_path, raw, final=URL):
    artifacts = ArtifactStore(Store(tmp_path).root)
    return artifacts, execute(artifacts.store.root, {"action": "research", "source": URL,
        "final_url": final, "mime": "text/html; charset=utf-8", "raw": base64.b64encode(raw).decode()})


def test_collection_preserves_exact_public_markdown_and_provenance_without_approval(tmp_path):
    raw = html(suffix=";")
    artifacts, result = capture(tmp_path, raw)
    assert BODY in result["excerpt"]
    assert "Visible caveat." in result["excerpt"]
    assert artifacts.bytes(result["raw"]) == raw
    assert artifacts.read(result["artifact"]) == result["excerpt"]
    assert result["extractor_version"] == "structured-text-v4"
    index = artifacts.json(result["section_index"])
    embedded = index["embedded_documents"][0]
    assert result["excerpt"][embedded["start"]:embedded["end"]] == BODY
    assert index["text_hash"] == digest(result["excerpt"])
    assert embedded["document_id"] == 9
    assert embedded["updated_time"] == "2026-10-29T00:00:00Z"
    assert embedded["source"] == "window._ROUTER_DATA.loaderData.docs/(libid)/(docid$)/page.curDoc.MDContent"
    assert "approved" not in result and "supported_version" not in result


@pytest.mark.parametrize("bad", ["foreign_host", "login", "wrong_path", "wrong_document",
    "wrong_library", "wrong_code", "wrong_route", "redirected_document", "script_suffix",
    "duplicate_script", "duplicate_json_key", "missing_body", "body_not_string", "errors",
    "array", "unclosed_script", "external_script", "data_script", "non_json_constant"])
def test_ambiguous_unbound_or_executable_data_does_not_become_document(tmp_path, bad):
    data, final = state(), URL
    page = data["loaderData"]["docs/(libid)/(docid$)/page"]
    doc = page["curDoc"]
    suffix = ""
    if bad == "foreign_host":
        final = URL.replace("docs.volcengine.com", "docs.volcengine.com.example")
    elif bad == "login":
        final = "https://signin.volcengine.com/auth/login"
    elif bad == "wrong_path":
        final = URL.replace("/example?", "/other?")
    elif bad == "wrong_document":
        doc["DocumentID"] = 10
    elif bad == "wrong_library":
        doc["LibraryID"] = 8
    elif bad == "wrong_code":
        doc["DocumentCode"] = "different"
    elif bad == "wrong_route":
        page["curDocRouteKey"] = "other"
    elif bad == "redirected_document":
        doc["RedirectInfo"] = {"url": "/other"}
    elif bad == "script_suffix":
        suffix = "; window.injected = true"
    elif bad == "missing_body":
        del doc["MDContent"]
    elif bad == "body_not_string":
        doc["MDContent"] = {"text": BODY}
    elif bad == "errors":
        data["errors"] = {"page": "denied"}
    elif bad == "array":
        data = [data]
    raw = html(data, suffix=suffix, repeat=bad == "duplicate_script")
    if bad == "duplicate_json_key":
        raw = raw.replace(b'"errors": null', b'"errors": null, "errors": null')
    elif bad == "unclosed_script":
        raw = raw.replace(b"</script>", b"")
    if bad == "external_script":
        raw = raw.replace(b"<script>", b'<script src="https://docs.volcengine.com/app.js">')
    elif bad == "data_script":
        raw = raw.replace(b"<script>", b'<script type="application/ld+json">')
    elif bad == "non_json_constant":
        raw = raw.replace(b'"errors": null', b'"unused": NaN, "errors": null')
    _, result = capture(tmp_path, raw, final)
    assert BODY not in result["excerpt"]
    assert result["excerpt"] == extract_document(raw, "text/html")[0]


def test_legacy_sections_and_requirements_keep_their_original_identity():
    raw = html()
    text, _, index = extract_sections(raw, "text/html")
    assert text == extract_document(raw, "text/html")[0]
    assert BODY not in text
    assert "embedded_documents" not in index


@pytest.mark.parametrize("route", ["voice/example", "7/9", "voice/doc/example"])
def test_current_slug_numeric_and_canonical_routes_bind_to_same_document(tmp_path, route):
    data = state()
    if route == "7/9":
        page = data["loaderData"]["docs/(libid)/(docid$)/page"]
        page.update(curLibRouteKey="7", curDocRouteKey="9")
    _, result = capture(tmp_path, html(data), "https://www.volcengine.com/docs/" + route)
    assert BODY in result["excerpt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("repetitions", [1, 1000])
async def test_real_collection_and_review_context_receive_body_but_not_verified_claim(tmp_path, monkeypatch, repetitions):
    requests = []
    data = state()
    body = BODY * repetitions
    data["loaderData"]["docs/(libid)/(docid$)/page"]["curDoc"]["MDContent"] = body
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html(data))
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    artifacts = ArtifactStore(Store(tmp_path).root)
    rows = await ResearchService(artifacts).verify([SourceEvidence(url=URL, claim="Unknown submissions need reconciliation", version="candidate")])
    assert requests == [URL]
    assert BODY in rows[0]["excerpt"]
    assert rows[0]["supported_version"] is None
    planner = ContextPlanner(artifacts)
    prepared = planner.prepare("review", {"review_kind": "research", "retrieved_sources": rows}, tmp_path)
    row = prepared.payload["retrieved_sources"][0]
    assert body in (artifacts.read(row["full_text"]["artifact"]) if "full_text" in row else row["excerpt"])
    assert row["supported_version"] is None
    # Rebuilding a missing index for a new snapshot must use its extraction version.
    without_index = copy.deepcopy(rows)
    del without_index[0]["retrieval"]["section_index"]
    prepared = planner.prepare("review", {"review_kind": "research", "retrieved_sources": without_index}, tmp_path)
    row = prepared.payload["retrieved_sources"][0]
    assert body in (artifacts.read(row["full_text"]["artifact"]) if "full_text" in row else row["excerpt"])
