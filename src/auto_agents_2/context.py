from __future__ import annotations

from pathlib import Path

from .readview import public_files
from .workspace import path_allowed


class ContextBuilder:
    def __init__(self, sensitive_paths=()):
        self.sensitive_paths = sensitive_paths

    def build(self, workspace: Path, *, scopes: list[str] | None = None, limit: int = 24000) -> dict:
        selected, total = [], 0
        candidates = sorted(public_files(workspace, self.sensitive_paths), key=lambda p: (p.name != "AGENTS.md", str(p)))
        inventory = [str(p.relative_to(workspace)) for p in candidates]
        for path in candidates:
            if total >= limit and path.name != "AGENTS.md":
                break
            relative = str(path.relative_to(workspace))
            if relative.startswith(("docs/aa2/", "docs/requirements/")) and not scopes:
                continue  # Already supplied as stage artifacts; avoid duplicating them in every prompt.
            if scopes and path.name != "AGENTS.md" and not path_allowed(relative, scopes):
                continue
            try:
                if path.stat().st_size > 100000 and path.name != "AGENTS.md":
                    continue
                content = path.read_bytes().decode("utf-8")
            except (UnicodeError, OSError):
                continue
            if total + len(content) > limit and path.name != "AGENTS.md":
                continue
            selected.append({"path": relative, "content": content})
            total += len(content)
        return {"workspace": str(workspace), "inventory": inventory, "files": selected,
                "omitted_files_available_via_tools": len(inventory) - len(selected)}
