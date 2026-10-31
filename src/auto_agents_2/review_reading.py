"""Disclosed reading contract for large reviews; never changes authority or acceptance."""
from __future__ import annotations

import json

REVIEW_READING_VERSION = 1
REVIEW_INLINE = 64000


def reading_contract(basis):
    if len(json.dumps(basis, ensure_ascii=False)) <= REVIEW_INLINE:
        return None
    return {
        "version": REVIEW_READING_VERSION,
        "basis_hash": basis["hash"],
        "candidate_hash": basis["candidate_hash"],
        "instruction": (
            "The controller supplies a lossless reading view of the complete immutable review basis. "
            "Its hash and candidate_hash identify the ORIGINAL record, not this presentation; copy those "
            "exact identities into authority and never recompute them from the view. Read every criterion "
            "and all required_read materials before deciding. Large top-level inputs and criteria are "
            "complete mounted materials, not summaries. The sources material is a lookup catalogue: "
            "inspect the exact source keys and full clauses relevant to your assessment or quotations; "
            "it repeats input/candidate/accepted contracts and is not a second requirement to read every "
            "duplicate body. Keep all criterion IDs and assess each once. Unchanged or unrelated clauses "
            "are not waived; consult full old/new contracts when needed to establish preservation. "
            "Material paths/hashes locate content, never replace authority source/criterion IDs. "
            "Missing or unread necessary evidence must block; no identity or pointer proves acceptance."
        ),
    }
