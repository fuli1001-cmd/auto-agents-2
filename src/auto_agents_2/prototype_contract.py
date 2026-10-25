"""The finite static-prototype protocol, separate from product test attachments.

This is controller capability/ownership metadata, not a semantic waiver or a
plugin API. It never executes candidate collectors or changes a gate's verdict.
"""
from __future__ import annotations

from typing import get_args

from .models import BrowserAction
from .util import digest

PROTOTYPE_VIEWPORTS = ((1440, 900), (390, 844))


def prototype_contract():
    body = {
        "version": 1,
        "producer": "inspect_prototype",
        "stage": "prototype",
        "actions": list(get_args(BrowserAction.model_fields["action"].annotation)),
        "viewports": [{"width": width, "height": height} for width, height in PROTOTYPE_VIEWPORTS],
        "records": ["route/status/errors", "completed_actions/failed_action", "viewport_png/scroll_tiles",
                    "capture_origin/page_dimensions"],
        "not_collected": ["dom_json", "local_storage_snapshot", "download_bytes", "file_chooser_upload",
                          "candidate_collector_execution"],
        "execution": "Declared actions run sequentially per route in a fresh context for each route and viewport. "
                     "Only the declared action schema is executable; candidate helper modules do not add capabilities.",
        "authority": "Controller protocol only, not evidence of collection, product acceptance or permission to run code.",
        "ownership": {
            "prototype": "Author static UI and supported interaction checks; submit corrections for independent review.",
            "review": "Read-only semantic review of requirements, proposal, actual images and executed actions; cannot add collectors.",
            "verification": "Later planned product checks create and attach required PNG/JSON/real-file evidence "
                            "through VerificationService's existing test_file_attachments protocol.",
        },
        "correction": "Distinguish original user requirements and explicit timing, approved contracts, and unapproved "
                      "model-derived technical proposals. Correct an unsupported derived mechanism or misplaced phase "
                      "through its owning stage with rationale and preserved substantive acceptance; do not silently drop "
                      "a user requirement or treat a prior review suggestion as new authority. An explicit user requirement "
                      "for unsupported evidence at this stage remains unresolved; it needs an authorized solution, not a waiver.",
    }
    return {**body, "hash": digest(body)}


def current_prototype_contract(value):
    return value == prototype_contract()


def source_context(store, artifacts, rid, specification=None):
    """Read only the retained capture; never follow a candidate-supplied path."""
    captured = store.get(rid, "capture")
    spec = store.get(rid, "clarify", specification or {})
    source = {"status": "unavailable", "specification_hash": digest(spec),
              "limit": "Missing legacy original capture does not prove a clause is model-derived or removable."}
    if captured and captured.get("artifact"):
        ref = captured["artifact"]
        # ArtifactStore validates bytes/hash; corrupt or missing retained material
        # is an error, unlike a legacy record that never contained a capture.
        source = {"status": "available", "capture": ref,
                  "raw_requirements": artifacts.read(ref), "specification_hash": digest(spec)}
    answers = store.submitted_answers(rid)
    if answers:
        source["submitted_answers"] = answers
    return {"controller_prototype_contract": prototype_contract(), "prototype_requirement_source": source}


def prototype_instructions(purpose, context):
    evidence = context.get("decision_request", {}).get("evidence", {}) if purpose == "decision" else context
    if "controller_prototype_contract" not in evidence:
        return ""
    return ("\nStatic prototype evidence contract: read controller_prototype_contract and prototype_requirement_source. "
            "The original user input determines substantive obligations and any explicit stage timing; candidate documents "
            "and earlier review suggestions are not new user authorization. Use only the supplied prototype action/collector "
            "capabilities. Writing a helper script, DOM JSON element or adapter instructions does not execute a collector "
            "or create evidence; do not ask the controller to run candidate modules. Screenshots and action passes do not "
            "prove uncollected file/state content. For an unsupported model-derived precondition in an UNAPPROVED prototype, "
            "the prototype author must explain and correct its mechanism/timing through normal re-entry and independent "
            "review while explicitly retaining the user's later product checks. Review must assess that correction against "
            "the original input, not require every unapproved technical promise to be implemented. Do not defer an explicit "
            "user requirement for this stage, weaken approved obligations, or excuse real UI defects/missing supported "
            "evidence. If original source or a legitimate capability is unavailable, report the gap and use only existing "
            "authorized recovery choices; this protocol grants no action, approval or acceptance. Product state JSON, "
            "real file round trips and independent final verification remain required when specified.\n")


def prototype_context(engine, rid, specification=None):
    return source_context(engine.store, engine.artifacts, rid, specification)
