"""Prototype-review-only reading disclosure; original evidence and gates stay unchanged."""
from __future__ import annotations

import json

from .prototype_review import valid_basis

KEY = "controller_prototype_review_reading"
VERSION = 1
INLINE = 64000


def reading_contract(context):
    basis = context.get("prototype_review_basis")
    original = {key: value for key, value in context.items() if key != KEY}
    if (context.get("review_kind") != "prototype" or not valid_basis(basis)
            or len(json.dumps(original, ensure_ascii=False)) <= INLINE):
        return None
    return {
        "version": VERSION, "basis_hash": basis["hash"], "candidate_hash": basis["candidate_hash"],
        "instruction": (
            "This prototype-review-only view preserves the complete frozen controller inputs. "
            "The visible basis hash and candidate_hash identify the ORIGINAL basis, not this reading view; "
            "copy these exact identities into the response, never recompute them from the presentation. "
            "Read every criterion and every required_read material, including their nested required sources "
            "and historical proposals/reviews, before deciding. Equal browser report results are supplied "
            "once with an exact source pointer; inspect the complete executed actions and observations. "
            "The visual_evidence material is a complete image-ID lookup catalogue, not another obligation "
            "to reread duplicate observation bodies; locate the IDs for images actually inspected. "
            "Mounted source, original requirements, submitted answers, accepted specification and visual "
            "design retain their original roles. Missing or unread necessary evidence cannot establish "
            "success. Counts, hashes, pointers and historical consent never approve the current review "
            "or replace applicable human prototype approval."
        ),
    }
