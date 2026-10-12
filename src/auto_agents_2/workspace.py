from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

from .errors import AA2Error, PolicyViolation
from .models import Change
from .util import atomic_write, contained, digest

EXCLUDED = {".git", ".aa2", ".conda-env", ".venv", "node_modules", ".next", "__pycache__",
            ".pytest_cache", ".ruff_cache", ".cache", "test-results", "dist", "build", ".aws", ".ssh", ".codex", ".gemini"}
PROTECTED_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "conftest.py", "pytest.ini",
                   "playwright.config.ts", "vitest.config.ts", "vitest.config.js", "jest.config.js", "jest.config.ts",
                   "pyproject.toml", "package.json", "package-lock.json", "tox.ini", "setup.cfg", "tsconfig.json",
                   "Makefile", "justfile"}


def git(root: Path, *args: str, input: str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], input=input, text=True,
                            capture_output=True, env=env, timeout=120)
    if result.returncode:
        raise AA2Error(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


def files(root: Path):
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [f for f in folders if f not in EXCLUDED and not Path(directory, f).is_symlink()]
        for name in sorted(names):
            path = Path(directory, name)
            if name not in EXCLUDED and not path.is_symlink() and name != ".env" and (not name.startswith(".env.") or name == ".env.example"):
                yield path


def tree_hash(root: Path) -> str:
    return digest([(str(p.relative_to(root)), digest(p.read_bytes())) for p in sorted(files(root))])


def path_allowed(path: str, scopes: list[str]) -> bool:
    return any(path == scope.rstrip("/") or path.startswith(scope.rstrip("/") + "/")
               or fnmatch.fnmatchcase(path, scope) for scope in scopes)


def prepare_changes(root: Path, changes: list[Change], scopes: list[str], *, protected: bool = True, contracts: bool = False):
    prepared = []
    paths = set()
    for change in changes:
        path = contained(root, change.path)
        if change.path in paths:
            raise PolicyViolation(f"Duplicate change: {change.path}")
        paths.add(change.path)
        parts = Path(change.path).parts
        if any(p in EXCLUDED for p in parts) or path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            raise PolicyViolation(f"Protected path: {change.path}")
        if not path_allowed(change.path, scopes):
            raise PolicyViolation(f"Out-of-scope change: {change.path}")
        if not contracts and (change.path == "DESIGN.md" or change.path.startswith(("docs/aa2/", "docs/requirements/", "design/prototypes/"))):
            raise PolicyViolation(f"Accepted contracts require a stage replan: {change.path}")
        if protected and (path.name in PROTECTED_NAMES or any(p in {"tests", "test", "__tests__", "__snapshots__", "fixtures", "scripts", ".github"} for p in parts)
                          or path.name.startswith("test_") or path.name.endswith(("_test.py", ".snap"))
                          or ".test." in path.name or ".spec." in path.name):
            raise PolicyViolation(f"Test/rule change requires independent authorization: {change.path}")
        # An existing package manifest's scripts are part of the verification boundary.
        if protected and path.name == "package.json" and path.exists():
            import json
            if change.operation != "write" or json.loads(path.read_text()).get("scripts") != json.loads(change.content).get("scripts"):
                raise PolicyViolation("package.json script changes require independent authorization")
        if change.operation == "replace":
            if not path.is_file() or not change.old_text or path.read_text().count(change.old_text) != 1:
                raise PolicyViolation(f"Replacement must match exactly once: {change.path}")
            content = path.read_text().replace(change.old_text, change.content, 1)
        else:
            content = change.content
        prepared.append({"path": change.path, "operation": change.operation, "content": content,
                         "before": digest(path.read_bytes()) if path.is_file() else None,
                         "after": None if change.operation == "delete" else digest(content)})
    return prepared


def apply_prepared(root: Path, prepared: list[dict]):
    for item in prepared:
        path = contained(root, item["path"])
        current = digest(path.read_bytes()) if path.is_file() else None
        if current not in {item["before"], item["after"]}:
            raise PolicyViolation(f"File changed after candidate preparation: {item['path']}")
    for item in prepared:
        path, op, content = contained(root, item["path"]), item["operation"], item["content"]
        current = digest(path.read_bytes()) if path.is_file() else None
        if current == item["after"]:
            continue
        if op == "delete":
            if path.is_file():
                path.unlink()
        else:
            atomic_write(path, content, 0o644)


def apply_changes(root: Path, changes: list[Change], scopes: list[str], *, protected: bool = True, contracts: bool = False):
    apply_prepared(root, prepare_changes(root, changes, scopes, protected=protected, contracts=contracts))


class WorkspaceManager:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.root = self.project / ".aa2"

    def initialize(self):
        try:
            top = Path(git(self.project, "rev-parse", "--show-toplevel")).resolve()
            if top != self.project:
                raise AA2Error("Project must be a Git repository root")
        except AA2Error:
            if (self.project / ".git").exists():
                raise
            git(self.project, "init", "-b", "main")
        exclude = Path(git(self.project, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = self.project / exclude
        old = exclude.read_text() if exclude.exists() else ""
        additions = [f"/{name}/" for name in EXCLUDED - {".git"} if f"/{name}/" not in old]
        if additions:
            atomic_write(exclude, old + "\n" + "\n".join(additions) + "\n")

    def baseline(self, run_id: str) -> str:
        """Snapshot user changes using a private index; leave their index and checkout alone."""
        index = self.root / "tmp" / run_id / "baseline-index"
        index.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        try:
            head = git(self.project, "rev-parse", "--verify", "HEAD")
            git(self.project, "read-tree", head, env=env)
        except AA2Error:
            head = ""
            git(self.project, "read-tree", "--empty", env=env)
        git(self.project, "add", "-u", env=env)
        excluded = [p for p in git(self.project, "ls-files", "-z", env=env).split("\0")
                    if p and (any(part in EXCLUDED for part in Path(p).parts) or Path(p).name == ".env"
                              or (Path(p).name.startswith(".env.") and Path(p).name != ".env.example"))]
        for start in range(0, len(excluded), 100):
            git(self.project, "rm", "--cached", "--ignore-unmatch", "--", *excluded[start:start + 100], env=env)
        candidates = [p for p in git(self.project, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
                      if p and not any(part in EXCLUDED for part in Path(p).parts)
                      and Path(p).name != ".env" and (not Path(p).name.startswith(".env.") or Path(p).name == ".env.example")
                      and (self.project / p).is_file() and not (self.project / p).is_symlink()]
        if candidates:
            for start in range(0, len(candidates), 100):
                git(self.project, "add", "--", *candidates[start:start + 100], env=env)
        tree = git(self.project, "write-tree", env=env)
        if head and tree == git(self.project, "rev-parse", f"{head}^{{tree}}"):
            commit = head
        else:
            args = ["-c", "user.name=aa2", "-c", "user.email=aa2@localhost", "commit-tree", tree]
            if head:
                args += ["-p", head]
            commit = git(self.project, *args, input=f"aa2 baseline {run_id}\n", env=env)
        git(self.project, "update-ref", f"refs/aa2/{run_id}/baseline", commit)
        index.unlink(missing_ok=True)
        return commit

    def allocate(self, run_id: str, name: str, base: str) -> Path:
        if not name.replace("-", "").replace("_", "").isalnum():
            raise PolicyViolation("Unsafe workspace name")
        path = self.root / "worktrees" / run_id / name
        if path.exists():
            git(path, "rev-parse", "--show-toplevel")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.project, "worktree", "add", "--detach", str(path), base)
        return path

    def checkpoint(self, path: Path, ref: str, message: str) -> str:
        git(path, "add", "-A")
        hidden = [p for p in git(path, "ls-files", "-z").split("\0")
                  if p and (any(part in EXCLUDED for part in Path(p).parts) or Path(p).name == ".env"
                            or (Path(p).name.startswith(".env.") and Path(p).name != ".env.example"))]
        for start in range(0, len(hidden), 100):
            git(path, "rm", "--cached", "--ignore-unmatch", "--", *hidden[start:start + 100])
        if git(path, "diff", "--cached", "--name-only"):
            git(path, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost", "commit", "-m", message)
        commit = git(path, "rev-parse", "HEAD")
        git(self.project, "update-ref", ref, commit)
        return commit

    def integrate(self, target: Path, commit: str):
        try:
            git(target, "merge-base", "--is-ancestor", commit, "HEAD")
            return
        except AA2Error:
            pass
        try:
            git(target, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost",
                "merge", "--no-edit", "--no-ff", commit)
        except AA2Error:
            git(target, "merge", "--abort")
            raise AA2Error("Integration conflict: replan affected tasks against the current integration baseline")

    def release(self, path: Path):
        if path.exists():
            git(self.project, "worktree", "remove", "--force", str(path))

    def deliver(self, run_id: str, commit: str) -> str:
        branch = f"aa2/{run_id}"
        git(self.project, "update-ref", f"refs/heads/{branch}", commit)
        return branch
