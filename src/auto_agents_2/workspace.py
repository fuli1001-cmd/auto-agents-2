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
    by_path = {}
    for change in changes:
        path = contained(root, change.path)
        previous = by_path.get(path)
        if previous is not None and (previous["operation"] != "replace" or change.operation != "replace"):
            raise PolicyViolation(f"Duplicate change: {change.path}; only sequential replacements may share a path")
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
        original = path.read_bytes() if previous is None and path.is_file() else None
        if change.operation == "replace":
            source = previous["content"] if previous is not None else (original.decode() if original is not None else None)
            if source is None or not change.old_text or source.count(change.old_text) != 1:
                raise PolicyViolation(f"Replacement must match exactly once: {change.path}")
            content = source.replace(change.old_text, change.content, 1)
        else:
            content = change.content
        if previous is not None:
            # One before/after pair per file makes partially applied batches replayable.
            previous.update(content=content, after=digest(content))
        else:
            item = {"path": change.path, "operation": change.operation, "content": content,
                    "before": digest(original) if original is not None else None,
                    "after": None if change.operation == "delete" else digest(content)}
            by_path[path] = item
            prepared.append(item)
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
        # Preserve already tracked files, including user .env files; never add new secrets.
        git(path, "add", "-u")
        new_files = [p for p in git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
                     if p and not any(part in EXCLUDED for part in Path(p).parts)
                     and Path(p).name != ".env" and (not Path(p).name.startswith(".env.") or Path(p).name == ".env.example")]
        for start in range(0, len(new_files), 100):
            git(path, "add", "--", *new_files[start:start + 100])
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

    def head(self, branch: str) -> str:
        try:
            return git(self.project, "rev-parse", "--verify", f"refs/heads/{branch}")
        except AA2Error:
            return ""

    def target_candidates(self) -> list[str]:
        branches = git(self.project, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        if not branches:
            return ["main"]
        try:
            default = git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD").removeprefix("refs/remotes/origin/")
            if default in branches:
                return [default]
        except AA2Error:
            pass
        conventional = [b for b in branches if b in {"main", "master"}]
        return conventional if len(conventional) == 1 else branches

    def clean_target(self, branch: str, *, switch: bool = False):
        git(self.project, "check-ref-format", "--branch", branch)
        if branch.startswith("-"):
            raise AA2Error("Invalid target branch")
        if git(self.project, "status", "--porcelain", "--untracked-files=all"):
            raise AA2Error("项目主目录有未提交或未跟踪的修改。请自行提交或保存到其它目录，再回复 retry；aa2 不会自动暂存或覆盖。")
        for record in git(self.project, "worktree", "list", "--porcelain").split("\n\n"):
            lines = record.splitlines()
            if f"branch refs/heads/{branch}" in lines and f"worktree {self.project}" not in lines:
                raise AA2Error(f"目标分支 {branch} 被另一个 worktree 占用，请先释放该分支")
        try:
            current = git(self.project, "symbolic-ref", "-q", "HEAD")
        except AA2Error:
            current = ""
        if current != f"refs/heads/{branch}":
            if not switch:
                raise AA2Error(f"主目录分支已变化，请切回 {branch} 后继续")
            if not self.head(branch):
                raise AA2Error(f"目标分支不存在：{branch}")
            git(self.project, "switch", "--no-overwrite-ignore", branch)

    def committed_baseline(self, rid: str, target: str) -> str:
        self.clean_target(target, switch=True)
        base = self.head(target)
        if not base:
            # Detached empty baseline; the unborn target is not advanced until delivery.
            tree = git(self.project, "hash-object", "-w", "-t", "tree", "--stdin", input="")
            base = git(self.project, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost",
                       "commit-tree", tree, input=f"aa2 empty baseline {rid}\n")
        git(self.project, "update-ref", f"refs/aa2/{rid}/baseline", base)
        return base

    def is_ancestor(self, older: str, newer: str) -> bool:
        if not older:
            return True
        try:
            git(self.project, "merge-base", "--is-ancestor", older, newer)
            return True
        except AA2Error:
            return False

    def entries(self, commit: str) -> dict:
        if not commit:
            return {}
        result = {}
        for entry in git(self.project, "ls-tree", "-rz", commit).split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                mode, kind, oid = meta.split()
                if kind != "blob":
                    raise AA2Error("Automatic delivery does not support submodule entries")
                result[path] = {"mode": mode, "oid": oid}
        return result

    def publication_manifest(self, old: str, new: str) -> dict:
        before, after = self.entries(old), self.entries(new)
        return {p: {"before": before.get(p), "after": after.get(p)}
                for p in before.keys() | after.keys() if before.get(p) != after.get(p)}

    def file_entry(self, name: str):
        path = self.project / name
        # Never inspect through a symlinked parent.
        for parent in path.parents:
            if parent == self.project:
                break
            if parent.is_symlink():
                raise AA2Error(f"Symlinked publication parent: {name}")
        if path.is_symlink():
            content = os.readlink(path).encode()
            mode = "120000"
        elif path.is_file():
            content = path.read_bytes()
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        elif path.exists():
            return {"directory": True}
        else:
            return None
        proc = subprocess.run(["git", "hash-object", "--stdin"], input=content, capture_output=True, check=True)
        return {"mode": mode, "oid": proc.stdout.decode().strip()}

    def check_publication_paths(self, manifest: dict):
        for name, images in manifest.items():
            if self.file_entry(name) != images["before"]:
                raise AA2Error(f"发布路径与基线不符（可能含被忽略的用户文件）：{name}")

    def publish(self, target: str, old: str, candidate: str):
        self.clean_target(target)
        if self.head(target) != old:
            raise AA2Error("Target advanced before publication; reintegration required")
        git(self.project, "merge", "--ff-only", "--no-overwrite-ignore", candidate)

    def recover_publication(self, journal: dict):
        """Repair only journal-attributable pre/post images; never reset a user's tree."""
        target, old, new = journal["target"], journal["old"], journal["candidate"]
        current = self.head(target)
        if current not in {old, new}:
            if self.is_ancestor(new, current):
                raise AA2Error("交付后目标分支又有新提交，请恢复交付版本或人工核对后继续；不会重置用户提交")
            raise AA2Error("发布中目标分支发生未知变化，需要人工核对")
        if git(self.project, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{target}":
            raise AA2Error("发布恢复需要主目录位于目标分支")
        lock = Path(git(self.project, "rev-parse", "--git-path", "index.lock"))
        if not lock.is_absolute():
            lock = self.project / lock
        if lock.exists():
            raise AA2Error("Git index.lock 存在，请确认没有其它 Git 进程后处理；aa2 不会删除未知锁")
        tree = git(self.project, "write-tree")
        empty = git(self.project, "hash-object", "-t", "tree", "--stdin", input="")
        old_tree = git(self.project, "rev-parse", f"{old}^{{tree}}") if old else empty
        new_tree = git(self.project, "rev-parse", f"{new}^{{tree}}")
        if tree not in {old_tree, new_tree}:
            raise AA2Error("发布恢复发现未知暂存区修改，保留现场等待处理")
        manifest = journal["manifest"]
        for name, images in manifest.items():
            if self.file_entry(name) not in (images["before"], images["after"]):
                raise AA2Error(f"发布恢复发现用户修改：{name}")
        # Unchanged tracked files must remain clean; nonignored untracked files also block.
        changed = set(git(self.project, "diff", "--name-only", "-z").split("\0")) - {""}
        untracked = set(git(self.project, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
        if (changed | untracked) - manifest.keys():
            raise AA2Error("发布恢复发现无关用户修改，保留现场等待处理")
        for name, images in manifest.items():
            actual = self.file_entry(name)
            if actual == images["after"]:
                continue
            if actual != images["before"]:
                raise AA2Error(f"发布恢复期间文件变化：{name}")
            path = self.project / name
            after = images["after"]
            if after is None:
                path.unlink(missing_ok=True)
            else:
                content = subprocess.run(["git", "-C", str(self.project), "cat-file", "blob", after["oid"]],
                                         capture_output=True, check=True).stdout
                if after["mode"] == "120000":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.unlink(missing_ok=True)
                    path.symlink_to(os.fsdecode(content))
                else:
                    if path.is_symlink():
                        path.unlink()
                    atomic_write(path, content, 0o755 if after["mode"] == "100755" else 0o644)
        git(self.project, "read-tree", new)
        git(self.project, "update-ref", f"refs/heads/{target}", new, current or "0" * len(new))
        self.clean_target(target)
