from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import AA2Error


def package_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(not isinstance(value.get(key, {}), dict)
                for key in ("dependencies", "devDependencies")):
            raise ValueError("Expected dependency mappings")
        return value
    except (OSError, ValueError) as exc:
        raise AA2Error("Cannot read or validate package.json") from exc

@dataclass(frozen=True)
class ProjectProfile:
    name: str
    python_roots: tuple[Path, ...]
    node_roots: tuple[Path, ...]
    has_frontend: bool = False

    @classmethod
    def detect(cls, workspace: Path, selected: str = "auto"):
        roots = [workspace, *(workspace / p for p in ("backend", "frontend", "api", "web"))]
        python = tuple(p for p in roots if (p / "pyproject.toml").exists() or (p / "requirements.txt").exists())
        node = tuple(p for p in roots if (p / "package.json").is_file())
        nextjs, react = False, False
        for root in node:
            data = package_manifest(root / "package.json")
            nextjs |= "next" in {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            react |= "react" in {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        name = selected
        if name == "auto":
            frontend = "next" if nextjs else "react" if react else "node"
            name = f"python-{frontend}" if node and python else frontend if node else "python"
        return cls(name, python or ((workspace,) if not node else ()), node, nextjs or react or selected in {"react", "next", "python-react", "python-next"})
