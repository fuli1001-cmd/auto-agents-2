"""Explicit UI intent and acquisition evidence for workflow tests."""
from auto_agents_2.models import VisualBrief
from auto_agents_2.util import digest
from auto_agents_2.visual_design import BRIEF_FIELDS

URL = "https://raw.githubusercontent.com/example/design/" + "a" * 40 + "/DESIGN.md"


def visual_brief(*, update="unchanged", refs=None):
    return VisualBrief(**{field: {"text": "Simple task-focused interface; preserve accepted behavior.",
                                 "source_refs": refs or ["user:original"]} for field in BRIEF_FIELDS},
                       design_update=update, design_update_reason="Preserve current style unless requirements change.")


def acquired_source(artifacts, *, url=URL, body="# Design\nRestrained controls for a task workspace."):
    raw = artifacts.put(body.encode(), "research_response")
    extracted = artifacts.put(body, "research_extracted")
    metadata = {"source": url, "final_url": url, "raw": raw, "artifact": extracted, "hash": digest(body),
                "mime_type": "text/plain", "encoding": "utf-8", "extractor_version": "structured-text-v4"}
    manifest = artifacts.put(metadata, "research_manifest")
    return {"url": url, "claim": "Task-first hierarchy fits the accepted workflow.",
            "version": "a" * 40, "validation": "Provider claim is not acquisition proof.",
            "retrieval": {**metadata, "manifest": manifest}, "excerpt": body}
