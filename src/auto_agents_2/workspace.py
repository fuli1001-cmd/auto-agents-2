from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import subprocess
from pathlib import Path

from .errors import AA2Error, IntegrationConflict, PolicyViolation, ProtectedChange
from .models import Change
from .util import atomic_write, contained, digest

EXCLUDED = {".git", ".aa2", ".conda-env", ".venv", "node_modules", ".next", "__pycache__",
            ".pytest_cache", ".ruff_cache", ".cache", "test-results", "dist", "build", ".aws", ".ssh", ".codex", ".gemini"}
GENERATED_DIRECTORIES = {"node_modules", ".next", "__pycache__", ".pytest_cache", ".ruff_cache",
                         ".cache", "test-results", "dist", "build"}
PROTECTED_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "conftest.py", "pytest.ini",
                   "playwright.config.ts", "vitest.config.ts", "vitest.config.js", "jest.config.js", "jest.config.ts",
                   "pyproject.toml", "package.json", "package-lock.json", "tox.ini", "setup.cfg", "tsconfig.json",
                   "Makefile", "justfile"}


def git(root: Path, *args: str, input: str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], input=input, text=True,
                            capture_output=True, env=env, timeout=120)
    if result.returncode:
        raise AA2Error(result.stderr.strip() or "Git operation failed")
    # NUL-delimited names and leading/trailing pathname whitespace are data.
    return result.stdout if "-z" in args else result.stdout.removesuffix("\n")


def repository_workspace(root: Path) -> bool:
    """Distinguish explicit filesystem snapshots from a broken Git checkout."""
    if not root.is_dir():
        return False
    for parent in (root, *root.parents):
        marker = parent / ".git"
        if marker.is_file() or marker.is_symlink():
            return True
        if marker.is_dir() and (parent == root or (marker / "HEAD").exists() or (marker / "objects").exists()):
            return True
    return False


def files(root: Path):
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [f for f in folders if f not in EXCLUDED and not Path(directory, f).is_symlink()]
        for name in sorted(names):
            path = Path(directory, name)
            if name not in EXCLUDED and not path.is_symlink() and name != ".env" and (not name.startswith(".env.") or name == ".env.example"):
                yield path


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def input_entry(path: Path, root: Path, seen=frozenset(), excluded=None, *, omitted=frozenset(), overlay=None):
    """Execution identity, deliberately independent of the model's read policy."""
    excluded = EXCLUDED if excluded is None else excluded
    overlay = overlay or {}
    if path in overlay:
        return overlay[path] if overlay[path] is not None else ["missing"]
    if path in seen:
        return ["cycle", str(path)]
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ["missing"]
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        resolved = path.parent / target
        resolved = Path(os.path.abspath(resolved))
        return ["link", target, input_entry(resolved, root, seen | {path}, excluded, omitted=omitted, overlay=overlay)]
    if stat.S_ISREG(info.st_mode):
        return ["file", bool(info.st_mode & 0o111), file_digest(path)]
    if stat.S_ISDIR(info.st_mode):
        children = set(path.iterdir()) | {p for p, value in overlay.items() if p.parent == path and value is not None}
        return ["directory", [(p.name, input_entry(p, root, seen | {path}, excluded, omitted=omitted, overlay=overlay))
                              for p in sorted(children) if p.name not in excluded and p not in omitted
                              and not (p in overlay and overlay[p] is None)]]
    raise PolicyViolation(f"Unsupported execution input type: {path.name}")


def legacy_tree_hash(root: Path) -> str:
    """Read-only v1 compatibility proof; never used for new acceptance evidence."""
    return digest([(str(p.relative_to(root)), file_digest(p)) for p in sorted(files(root))])


def _tree_paths(root: Path):
    paths = set()
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in folders + names:
            path = Path(directory, name)
            if name not in EXCLUDED and (path.is_symlink() or path.is_file()):
                paths.add(path.relative_to(root).as_posix())
        folders[:] = [f for f in folders if f not in EXCLUDED and not Path(directory, f).is_symlink()]
    # Tracked inputs remain relevant even when hidden from model material or normally generated.
    tracked = set()
    if repository_workspace(root):
        tracked.update(p for p in git(root, "ls-files", "-z").split("\0") if p)
        paths.update(tracked)
    return paths, tracked


def tree_hash(root: Path) -> str:
    paths, _ = _tree_paths(root)
    return digest(["execution-v2", [(p, input_entry(root / p, root)) for p in sorted(paths)]])


def prepared_before_hash(root: Path, prepared: list[dict]) -> str:
    """Reconstruct the recorded preimage identity without reverting any actual file."""
    paths, tracked = _tree_paths(root)
    overrides = {}
    for item in prepared:
        _, path = write_path(root, item["path"])
        if item["before"] is None:
            overrides[root / path] = None
            if not item.get("was_tracked", path in tracked):
                paths.discard(path)
            for directory in item.get("created_parents", []):
                overrides[root / directory] = None
        else:
            paths.add(path)
            overrides[root / path] = ["file", bool(item.get("mode", 0o644) & 0o111), item["before"]]
    return digest(["execution-v2", [(p, input_entry(root / p, root, overlay=overrides)) for p in sorted(paths)]])


def write_path(root: Path, relative: str) -> tuple[Path, str]:
    path = contained(root, relative)
    canonical = path.relative_to(root).as_posix()
    current = root
    for part in Path(canonical).parts:
        current /= part
        if current.is_symlink():
            raise PolicyViolation(f"Symlink write path: {canonical}")
    return path, canonical


def path_allowed(path: str, scopes: list[str]) -> bool:
    return any(path == scope.rstrip("/") or path.startswith(scope.rstrip("/") + "/")
               or fnmatch.fnmatchcase(path, scope) for scope in scopes)


def prepare_changes(root: Path, changes: list[Change], scopes: list[str], *, protected: bool = True, contracts: bool = False):
    # Validate all non-reviewable errors before requesting test/rule authorization.
    if protected:
        prepare_changes(root, changes, scopes, protected=False, contracts=contracts)
    prepared = []
    by_path = {}
    _, tracked = _tree_paths(root)
    for change in changes:
        path, canonical = write_path(root, change.path)
        previous = by_path.get(path)
        if previous is not None and (previous["operation"] != "replace" or change.operation != "replace"):
            raise PolicyViolation(f"Duplicate change: {change.path}; only sequential replacements may share a path")
        parts = Path(canonical).parts
        if any(p in EXCLUDED for p in parts) or path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            raise PolicyViolation(f"Protected path: {change.path}")
        if not path_allowed(canonical, scopes):
            raise PolicyViolation(f"Out-of-scope change: {change.path}")
        if not contracts and path_allowed(canonical, ["DESIGN.md", "docs/aa2", "docs/requirements", "design/prototypes"]):
            raise PolicyViolation(f"Accepted contracts require a stage replan: {change.path}")
        if protected and (path.name in PROTECTED_NAMES or any(p in {"tests", "test", "__tests__", "__snapshots__", "fixtures", "scripts", ".github"} for p in parts)
                          or path.name.startswith("test_") or path.name.endswith(("_test.py", ".snap"))
                          or ".test." in path.name or ".spec." in path.name):
            raise ProtectedChange(f"Test/rule change requires independent authorization: {change.path}")
        if path.exists() and not path.is_file():
            raise PolicyViolation(f"Expected regular file: {canonical}")
        original = path.read_bytes() if previous is None and path.is_file() else None
        if change.operation == "replace":
            try:
                source = previous["content"] if previous is not None else (original.decode("utf-8") if original is not None else None)
            except UnicodeError as exc:
                raise PolicyViolation(f"Replacement requires supported UTF-8 text: {canonical}") from exc
            if source is None or not change.old_text or source.count(change.old_text) != 1:
                raise PolicyViolation(f"Replacement must match exactly once: {change.path}")
            content = source.replace(change.old_text, change.content, 1)
        else:
            content = change.content
        if previous is not None:
            # One before/after pair per file makes partially applied batches replayable.
            previous.update(content=content, after=digest(content))
        else:
            item = {"path": canonical, "mode": stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644, "operation": change.operation, "content": content,
                    "before": digest(original) if original is not None else None,
                    "after": None if change.operation == "delete" else digest(content)}
            item["created_parents"] = [p.relative_to(root).as_posix() for p in path.parents
                                       if p != root and p.is_relative_to(root) and not p.exists()]
            item["was_tracked"] = canonical in tracked
            by_path[path] = item
            prepared.append(item)
    validate_patch_structure(prepared)
    return prepared


def validate_patch_structure(prepared):
    live = [Path(item["path"]) for item in prepared if item["after"] is not None]
    for index, path in enumerate(live):
        if any(path.is_relative_to(other) or other.is_relative_to(path) for other in live[index + 1:]):
            raise PolicyViolation("Conflicting ancestor/descendant file changes")


def apply_prepared(root: Path, prepared: list[dict]):
    validate_patch_structure(prepared)
    ordered = sorted(prepared, key=lambda row: (row["after"] is not None,
                     -len(Path(row["path"]).parts) if row["after"] is None else len(Path(row["path"]).parts)))
    for item in ordered:
        path, _ = write_path(root, item["path"])
        if path.is_dir() and item["after"] is None:
            children = {str(child.relative_to(root)) for child in path.rglob("*") if child.is_symlink() or not child.is_dir()}
            owned = {row["path"] for row in prepared if row["after"] is not None}
            if children <= owned and any(Path(name).is_relative_to(item["path"]) for name in owned):
                continue  # A partially applied, owned file-to-directory conversion.
        if path.exists() and (not path.is_file() or stat.S_IMODE(path.stat().st_mode) != item.get("mode", 0o644)):
            raise PolicyViolation(f"File type or mode changed after candidate preparation: {item['path']}")
        current = digest(path.read_bytes()) if path.is_file() else None
        if current not in {item["before"], item["after"]}:
            raise PolicyViolation(f"File changed after candidate preparation: {item['path']}")
    for item in ordered:
        path, _ = write_path(root, item["path"])
        op, content = item["operation"], item["content"]
        if path.is_dir() and item["after"] is None:
            continue
        current = digest(path.read_bytes()) if path.is_file() else None
        if current == item["after"]:
            continue
        if op == "delete":
            if path.is_file():
                path.unlink()
        else:
            atomic_write(path, content, item.get("mode", 0o644))


def apply_changes(root: Path, changes: list[Change], scopes: list[str], *, protected: bool = True, contracts: bool = False):
    apply_prepared(root, prepare_changes(root, changes, scopes, protected=protected, contracts=contracts))


class WorkspaceManager:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.root = self.project / ".aa2"

    def initialize(self):
        try:
            top = Path(git(self.project, "rev-parse", "--show-toplevel")).resolve()
        except AA2Error:
            if repository_workspace(self.project):
                raise
            git(self.project, "init", "-b", "main")
        else:
            if top != self.project:
                raise AA2Error("Project must be a Git repository root")
        self._ensure_exclusions()

    def _ensure_exclusions(self):
        exclude = Path(git(self.project, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = self.project / exclude
        old = exclude.read_text() if exclude.exists() else ""
        patterns = {f"/{name}/" for name in EXCLUDED - {".git"}} | {f"{name}/" for name in GENERATED_DIRECTORIES}
        additions = sorted(patterns - set(old.splitlines()))
        if additions:
            atomic_write(exclude, old + "\n" + "\n".join(additions) + "\n")

    def allocate(self, run_id: str, name: str, base: str) -> Path:
        if not name.replace("-", "").replace("_", "").isalnum():
            raise PolicyViolation("Unsafe workspace name")
        # Also upgrade repositories initialized before nested generated directories
        # were excluded. The shared private exclude file covers existing worktrees.
        self._ensure_exclusions()
        path = self.root / "worktrees" / run_id / name
        if path.exists():
            git(path, "rev-parse", "--show-toplevel")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.project, "worktree", "add", "--detach", str(path), base)
        return path

    def checkpoint(self, path: Path, ref: str, message: str, *, expected_ref=None) -> str:
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
        git(self.project, "update-ref", ref, commit, *([] if expected_ref is None else [expected_ref or "0" * len(commit)]))
        return commit

    def checkpoint_intent(self, path, ref):
        head = git(path, "rev-parse", "HEAD")
        merge_file = Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))
        return {"head": head, "parents": [head, *(merge_file.read_text().splitlines() if merge_file.exists() else [])],
                "ref": ref, "ref_before": git(self.project, "for-each-ref", "--format=%(objectname)", ref),
                "source": tree_hash(path), "index": git(path, "ls-files", "--stage", "-z")}

    def finish_checkpoint(self, path, intent):
        """Reconcile an interrupted, reviewed commit without accepting unrelated index/HEAD edits."""
        if tree_hash(path) != intent["source"]:
            raise AA2Error("Prepared checkpoint source changed; preserving user changes")
        head = git(path, "rev-parse", "HEAD")
        current_ref = git(self.project, "for-each-ref", "--format=%(objectname)", intent["ref"])
        if current_ref not in {intent["ref_before"], head}:
            raise AA2Error("Prepared checkpoint reference changed")
        if head == intent["head"]:
            def entries(raw):
                result = {}
                for row in raw.split("\0"):
                    if row:
                        metadata, name = row.split("\t", 1)
                        result.setdefault(name, []).append(metadata)
                return result
            before = entries(intent["index"])
            current = entries(git(path, "ls-files", "--stage", "-z"))
            view = WorkspaceManager(path)
            for name in before.keys() | current.keys():
                if current.get(name) == before.get(name):
                    continue
                actual = view.file_entry(name)
                expected = [f"{actual['mode']} {actual['oid']} 0"] if actual and "oid" in actual else None
                if current.get(name) != expected:
                    raise AA2Error("Prepared checkpoint index changed; preserving staged user changes")
            return self.checkpoint(path, intent["ref"], "aa2: reviewed integration repair", expected_ref=current_ref)
        parents = git(path, "rev-list", "--parents", "-n", "1", head).split()[1:]
        if parents != intent["parents"] or git(path, "diff", "--name-only", "HEAD"):
            raise AA2Error("Prepared checkpoint HEAD changed; preserving user commits")
        git(self.project, "update-ref", intent["ref"], head, current_ref or "0" * len(head))
        return head

    def integrate(self, target: Path, commit: str):
        try:
            git(target, "merge-base", "--is-ancestor", commit, "HEAD")
            return
        except AA2Error:
            pass
        try:
            git(target, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost",
                "merge", "--no-edit", "--no-ff", commit)
        except AA2Error as exc:
            conflicts = git(target, "diff", "--name-only", "--diff-filter=U", "-z").strip("\0")
            try:
                git(target, "rev-parse", "--verify", "MERGE_HEAD")
            except AA2Error:
                pass
            else:
                git(target, "merge", "--abort")
            if conflicts:
                raise IntegrationConflict("Integration conflict: " + conflicts.replace("\0", ", ")) from exc
            raise

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

    @staticmethod
    def raw_file_hash(path):
        if path.is_symlink():
            return digest(os.readlink(path).encode())
        return file_digest(path) if path.is_file() else None

    def publication_manifest(self, old: str, new: str, work: Path | None = None) -> dict:
        before, after = self.entries(old), self.entries(new)
        view = work or self.project
        self.checkout_policy(view, sorted(after))
        manifest = {}
        structure = {name: {"before": before.get(name), "after": after.get(name)} for name in before.keys() | after.keys()}
        for name in before.keys() | after.keys():
            entry = after.get(name)
            content = None
            if entry:
                conversion = ["blob", entry["oid"]] if entry["mode"] == "120000" else ["--filters", f"{new}:{name}"]
                content = subprocess.run(["git", "-C", str(view), "cat-file", *conversion],
                                         capture_output=True, check=True).stdout
            actual = self.publication_entry(name, structure)
            before_bytes = self.raw_file_hash(self.project / name) if actual and actual != {"directory": True} else None
            after_bytes = digest(content) if content is not None else None
            if before.get(name) != entry or before_bytes != after_bytes:
                manifest[name] = {"before": before.get(name), "after": entry,
                                  "before_bytes": before_bytes, "after_bytes": after_bytes}
        return manifest

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
        proc = subprocess.run(["git", "-C", str(self.project), "hash-object", "--stdin",
                               *([] if mode == "120000" else ["--path", name])], input=content, capture_output=True, check=True)
        return {"mode": mode, "oid": proc.stdout.decode().strip()}

    def check_publication_paths(self, manifest: dict):
        for name, images in manifest.items():
            actual = self.publication_entry(name, manifest)
            if actual == {"directory": True} and images["before"] is None:
                self._attributable_directory(name, manifest)
                continue
            if actual != images["before"]:
                raise AA2Error(f"发布路径与基线不符（可能含被忽略的用户文件）：{name}")

    def publication_entry(self, name, manifest):
        """A verified file/link ancestor can hide an absent journal descendant."""
        relative = Path(name)
        for parent in reversed(relative.parents):
            if parent == Path("."):
                continue
            path = self.project / parent
            if path.is_symlink() or path.is_file():
                images = manifest.get(parent.as_posix(), {})
                actual = self.file_entry(parent.as_posix())
                if any(actual == images.get(phase) and images.get(phase) is not None
                       and manifest[name].get(phase) is None for phase in ("before", "after")):
                    return None
                raise AA2Error(f"Unattributable publication parent: {name}")
        return self.file_entry(name)

    def _attributable_directory(self, name, manifest, recovery=False):
        path = self.project / name
        for child in path.rglob("*"):
            relative = child.relative_to(self.project).as_posix()
            if child.is_dir() and not child.is_symlink():
                continue
            if relative not in manifest or not recovery and manifest[relative]["after"] is not None:
                raise AA2Error(f"Publication directory contains unrelated data: {relative}")
            if self.file_entry(relative) not in (manifest[relative]["before"], manifest[relative]["after"] if recovery else None):
                raise AA2Error(f"Publication directory contains modified data: {relative}")

    @staticmethod
    def checkout_policy(work, paths):
        records = []
        for start in range(0, len(paths), 100):
            raw = git(work, "check-attr", "-z", "filter", "ident", "working-tree-encoding", "text", "eol", "--", *paths[start:start + 100])
            fields = raw.split("\0")
            for index in range(0, len(fields) - 2, 3):
                path, attribute, value = fields[index:index + 3]
                if attribute in {"filter", "ident", "working-tree-encoding"} and value not in {"unspecified", "unset"}:
                    raise PolicyViolation(f"Unsupported publication conversion: {path} ({attribute})")
                records.append([path, attribute, value])
        for setting in ("core.autocrlf", "core.eol"):
            try:
                value = git(work, "config", "--get", setting)
            except AA2Error:
                value = ""
            records.append([setting, value])
        return digest(records)

    def normalize_checkout(self, work: Path, commit: str) -> str:
        entries = self.entries(commit)
        policy = self.checkout_policy(work, sorted(entries))
        for name, entry in entries.items():
            if entry["mode"] == "120000":
                continue
            path, _ = write_path(work, name)
            content = subprocess.run(["git", "-C", str(work), "cat-file", "--filters", f"{commit}:{name}"],
                                     capture_output=True, check=True).stdout
            if path.read_bytes() != content:
                atomic_write(path, content, stat.S_IMODE(path.stat().st_mode))
        return policy

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
        if journal.get("checkout_policy"):
            policy = self.checkout_policy(Path(journal["work"]), sorted(self.entries(new)))
            if policy != journal["checkout_policy"]:
                raise AA2Error("Publication conversion policy changed during recovery")
        for name, images in manifest.items():
            actual = self.publication_entry(name, manifest)
            if actual == {"directory": True} and (images["before"] is None or images["after"] is None):
                self._attributable_directory(name, manifest, recovery=True)
                continue
            if actual not in (images["before"], images["after"]):
                raise AA2Error(f"发布恢复发现用户修改：{name}")
            raw = self.raw_file_hash(self.project / name) if actual and actual != {"directory": True} else None
            if "before_bytes" in images and raw not in (images["before_bytes"], images["after_bytes"]):
                raise AA2Error(f"Publication recovery found changed file bytes: {name}")
        # Unchanged tracked files must remain clean; nonignored untracked files also block.
        changed = set(git(self.project, "diff", "--name-only", "-z").split("\0")) - {""}
        untracked = set(git(self.project, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
        if (changed | untracked) - manifest.keys():
            raise AA2Error("发布恢复发现无关用户修改，保留现场等待处理")
        # Remove old leaves before creating parents or replacing directories.
        ordered = sorted(manifest, key=lambda n: (manifest[n]["after"] is not None,
                                                  -len(Path(n).parts) if manifest[n]["after"] is None else len(Path(n).parts), n))
        for name in ordered:
            images = manifest[name]
            actual = self.publication_entry(name, manifest)
            raw = self.raw_file_hash(self.project / name) if actual and actual != {"directory": True} else None
            if actual == images["after"] and ("after_bytes" not in images or raw == images["after_bytes"]):
                continue
            if actual == {"directory": True} and images["after"] is None:
                continue
            structural = actual == {"directory": True} and images["before"] is None
            if actual != images["before"] and not structural:
                raise AA2Error(f"发布恢复期间文件变化：{name}")
            path = self.project / name
            after = images["after"]
            if after is None:
                if path.is_dir() and not path.is_symlink():
                    continue  # A completed file-to-directory transition.
                path.unlink(missing_ok=True)
            else:
                if path.is_dir() and not path.is_symlink():
                    for directory in sorted(path.rglob("*"), key=lambda p: -len(p.parts)):
                        directory.rmdir()
                    path.rmdir()
                conversion = ["blob", after["oid"]] if after["mode"] == "120000" else ["--filters", f"{new}:{name}"]
                content = subprocess.run(["git", "-C", str(self.project), "cat-file", *conversion],
                                         capture_output=True, check=True).stdout
                if "after_bytes" in images and digest(content) != images["after_bytes"]:
                    raise AA2Error("Checkout bytes changed during recovery")
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
