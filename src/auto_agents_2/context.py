from __future__ import annotations

import ast
import re
from pathlib import Path

from .readview import public_files, view_contract
from .util import digest
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

    def select(self, workspace: Path, *, scopes=(), paths=(), purpose="clarify", limit=24000) -> dict:
        """Model presentation; canonical build() remains stable for recovery identities."""
        files = {p.relative_to(workspace).as_posix(): p for p in public_files(workspace, self.sensitive_paths)}
        explicit = {p for p in files if p in paths or scopes and path_allowed(p, scopes)}
        manifests = {p for p in files if Path(p).name in {
            "package.json", "pyproject.toml", "requirements.txt", "environment.yml", "tsconfig.json",
            "vite.config.ts", "next.config.js", "next.config.ts", "pytest.ini", "playwright.config.ts"}}
        seeds = explicit or manifests
        rules = {p for p in files if Path(p).name == "AGENTS.md" and
                 (p == "AGENTS.md" or any(Path(p).parent in Path(s).parents for s in seeds))}
        dependencies = set()
        for name in explicit:
            if files[name].stat().st_size > 100000:
                continue
            try:
                source = files[name].read_text(encoding="utf-8")
                if name.endswith(".py"):
                    imports = []
                    roots = {Path(), Path("src"), *Path(name).parents}
                    for node in ast.walk(ast.parse(source)):
                        if isinstance(node, ast.Import):
                            imports.extend(str(root / a.name.replace(".", "/")) for a in node.names for root in roots)
                        elif isinstance(node, ast.ImportFrom):
                            parent = Path(name).parent
                            for _ in range(max(0, node.level - 1)):
                                parent = parent.parent
                            base = (parent if node.level else Path()) / (node.module or "").replace(".", "/")
                            for root in ([Path()] if node.level else roots):
                                imports.extend([str(root / base), *(str(root / base / a.name) for a in node.names)])
                else:
                    imports = [str(Path(name).parent / m) for m in re.findall(
                        r'''(?:from\s*|import\s*(?:\(\s*)?|require\s*\(\s*)['"](\.[^'"]+)''', source)]
                for base in imports:
                    for suffix in ("", ".py", "/__init__.py", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx"):
                        candidate = (workspace / (base + suffix)).resolve()
                        if candidate.is_relative_to(workspace.resolve()):
                            relative = candidate.relative_to(workspace.resolve()).as_posix()
                            if relative in files:
                                dependencies.add(relative)
            except (UnicodeError, OSError, SyntaxError):
                continue  # The complete public file remains available for native inspection.
        rules |= {p for p in files if Path(p).name == "AGENTS.md" and
                  any(Path(p).parent in Path(s).parents for s in dependencies)}
        if scopes or paths:
            manifests = {p for p in manifests if Path(p).parent == Path(".")
                         or any(Path(p).parent in Path(s).parents for s in explicit | dependencies)}

        def priority(name):
            if name in rules:
                return 0
            if Path(name).name == "AGENTS.md":
                return 9
            if name in explicit:
                return 1
            if purpose == "readme" and Path(name).name.lower().startswith("readme"):
                return 1
            if name in dependencies:
                return 2
            if name in manifests:
                return 3
            if name.startswith(("docs/aa2/", "docs/requirements/")):
                return 9
            if name.endswith((".py", ".ts", ".tsx", ".js", ".jsx")) and Path(name).name not in {"__init__.py", "vite-env.d.ts"}:
                return 4 if Path(name).stem in {"main", "app", "App", "routes", "index"} else 5
            return 8

        candidates = [p for p in files if p in rules or p in explicit or p in dependencies or p in manifests
                      or not scopes and purpose not in {"provider_research", "research"} and priority(p) < 9]
        selected, available, total = [], [], 0
        for name in sorted(candidates, key=lambda p: (priority(p), p)):
            path = files[name]
            reason = ("applicable project instructions" if name in rules else "task or evidence path" if name in explicit
                      else "direct project dependency" if name in dependencies else "project entry or configuration")
            size = path.stat().st_size
            if name not in rules and (size > 100000 or total >= limit):
                if priority(name) <= 3:
                    available.append({"path": name, "bytes": size, "reason": reason})
                continue
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (UnicodeError, OSError):
                if priority(name) <= 3:
                    available.append({"path": name, "reason": reason, "availability": "requires native inspection"})
                continue
            if name not in rules and total + len(text) > limit:
                if priority(name) <= 3:
                    available.append({"path": name, "bytes": len(raw), "reason": reason})
                continue
            selected.append({"path": name, "content": text, "source_hash": digest(raw), "reason": reason})
            total += len(text)
        selected_names = {r["path"] for r in selected}
        # General discovery can select code outside the initial manifest seeds.
        # Its applicable instructions remain mandatory even beyond the soft target.
        for name in sorted(files):
            if (Path(name).name == "AGENTS.md" and name not in selected_names
                    and any(Path(name).parent in Path(s).parents for s in selected_names)):
                raw = files[name].read_bytes()
                selected.insert(0, {"path": name, "content": raw.decode("utf-8"), "source_hash": digest(raw),
                                    "reason": "applicable project instructions"})
        return {"workspace": str(workspace), "view_contract": view_contract(),
                "files": selected, "read_on_demand": available,
                "roots": sorted({Path(p).parts[0] for p in files}), "public_file_count": len(files),
                "discovery": "Search the mounted public workspace for additional relevant files; read scopes do not grant writes."}
