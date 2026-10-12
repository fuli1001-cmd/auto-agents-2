from __future__ import annotations

from pathlib import Path

from .workspace import files


class ContextBuilder:
    def build(self, workspace: Path, *, scopes: list[str] | None = None, limit: int = 24000) -> dict:
        selected, total = [], 0
        candidates = sorted(files(workspace), key=lambda p: (p.name != "AGENTS.md", str(p)))
        inventory = [str(p.relative_to(workspace)) for p in candidates]
        for path in candidates:
            relative = str(path.relative_to(workspace))
            if relative.startswith(("docs/aa2/", "docs/requirements/")) and not scopes:
                continue  # Already supplied as stage artifacts; avoid duplicating them in every prompt.
            if scopes and path.name != "AGENTS.md" and not any(relative.startswith(s.rstrip("/*")) for s in scopes):
                continue
            if path.stat().st_size > 100000:
                continue
            try:
                content = path.read_text()
            except (UnicodeError, OSError):
                continue
            if total + len(content) > limit:
                continue
            selected.append({"path": relative, "content": content})
            total += len(content)
        return {"workspace": str(workspace), "inventory": inventory[:2000], "files": selected,
                "omitted_files_available_via_tools": len(inventory) - len(selected)}
