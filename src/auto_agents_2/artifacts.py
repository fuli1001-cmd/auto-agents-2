from __future__ import annotations

import json
from pathlib import Path

from .util import atomic_write, digest


class ArtifactStore:
    """Immutable content-addressed objects; the controller alone publishes references."""

    def __init__(self, root: Path):
        self.root = root / "artifacts"

    def put(self, value: str | dict | list, kind: str = "document") -> dict:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        sha = digest(text)
        path = self.root / sha[:2] / sha
        if not path.exists():
            atomic_write(path, text)
        return {"hash": sha, "kind": kind, "bytes": len(text.encode())}

    def read(self, ref: dict) -> str:
        sha = ref["hash"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid artifact address")
        data = (self.root / sha[:2] / sha).read_bytes()
        if digest(data) != sha:
            raise ValueError("Artifact integrity check failed")
        return data.decode()

    def json(self, ref: dict):
        return json.loads(self.read(ref))
