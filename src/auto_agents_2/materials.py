"""Material delivery accounting; size targets never alter mandatory facts or gates."""
from __future__ import annotations

import json
from pathlib import Path

from .errors import ProviderError
from .util import digest

DEFAULT_PROMPT_TARGET = 64000
DEFAULT_FEEDBACK_TARGET = 8000


def bundle(prompt: str, schema: dict, files: list[Path], images: list[Path], capability: dict) -> dict:
    rows = []
    for purpose, paths in (("context", files), ("image", images)):
        for path in paths:
            if not path.is_file() or path.is_symlink():
                raise ProviderError("Required material is missing or unsafe; acceptance cannot proceed")
            rows.append({"purpose": purpose, "required": True, "presentation": "attachment",
                         "location": str(path), "hash": digest(path.read_bytes()),
                         "delivery_state": "controller_readable", "omissions": []})
    characters = len(prompt) + len(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    return {"version": 1, "capability": capability, "materials": rows,
            "prompt_hash": digest(prompt), "schema_hash": digest(schema), "characters": characters,
            "soft_target": DEFAULT_PROMPT_TARGET, "over_target": characters > DEFAULT_PROMPT_TARGET,
            "presentation": "conservative_self_contained", "omitted_mandatory_content": [],
            "usage_scope": "prompt_and_schema_characters_only; excludes_native_history_tools_images"}
