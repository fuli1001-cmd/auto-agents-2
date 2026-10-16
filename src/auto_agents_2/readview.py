"""Provider-only public snapshot; execution and verification keep the real input view."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
from pathlib import Path

from .errors import PolicyViolation
from .util import SECRET
from .workspace import EXCLUDED, git

VIEW_VERSION = "public-v5-paths"
SENSITIVE_DIRS = {".aws", ".ssh", ".codex", ".gemini", ".credentials"}
SENSITIVE_FILES = {".netrc", ".npmrc", ".pypirc", ".git-credentials"}


def sensitive(path: Path, extra=()) -> bool:
    return (any(fnmatch.fnmatchcase(path.as_posix(), p) or path.as_posix().startswith(p.rstrip("/") + "/") for p in extra)
            or bool(set(path.parts) & SENSITIVE_DIRS) or path.name in SENSITIVE_FILES or path.name == ".env"
            or path.name.startswith(".env.") and path.name != ".env.example")


def public_files(root: Path, extra=()):
    """Admission depends only on protected paths, never on file contents or identity."""
    candidates = []
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name not in EXCLUDED
                      and not Path(directory, name).is_symlink()
                      and not sensitive(Path(directory, name).relative_to(root), extra)]
        for name in names:
            path = Path(directory, name)
            relative = path.relative_to(root)
            if name in EXCLUDED or sensitive(relative, extra) or path.is_symlink() or not path.is_file():
                continue
            try:
                relative.as_posix().encode("utf-8")
            except UnicodeError:
                raise PolicyViolation("Non-UTF-8 filenames cannot be delivered to the model; rename them explicitly") from None
            candidates.append(path)
    return candidates


def create_view(workspace: Path, destination: Path, extra=()) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for path in public_files(workspace, extra):
        target = destination / path.relative_to(workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Independent object database: no remotes, alternates, hooks or original history.
    variables = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    variables.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    git(destination, "-c", "init.templateDir=", "init", "-q", env=variables)
    git(destination, "add", "--all", "--force", env=variables)
    git(destination, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost",
        "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "Filtered current execution snapshot", env=variables)
    return destination


def sensitive_values(workspace: Path, extra=()) -> list[str]:
    values = set()
    def strings(value, protected=False):
        if isinstance(value, str) and protected and value:
            values.add(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                strings(child, protected or bool(SECRET.search(key)))
        elif isinstance(value, list):
            for child in value:
                strings(child, protected)
    for path in sensitive_files(workspace, extra):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeError:
            continue
        strings(text.strip(), True)
        try:
            document = json.loads(text)
            strings(document, True)
        except ValueError:
            # Every assignment in an explicitly protected file is confidential,
            # irrespective of its key. Quoted values can span physical lines.
            assignments = r'''(?m)^\s*(?:export\s+)?(?:[^\n#=]+=[ \t]*|[^\n#=:]+:[ \t]*)("(?:\\.|[^"\\])*"|'[^']*'|[^\n]*)'''
            for match in re.finditer(assignments, text):
                value = match[1].strip()
                if value.startswith(("'", '"')):
                    double_quoted = value.startswith('"')
                    value = value[1:-1]
                    if double_quoted:
                        escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
                        strings(re.sub(r'\\([nrt"\\])', lambda match: escapes[match[1]], value), True)
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                strings(value, True)
            if "PRIVATE KEY" in text:
                for line in text.splitlines():
                    if line.strip() and not line.startswith("-----"):
                        strings(line.strip(), True)
            for match in re.finditer(r"(?i)\bpassword\s+(\S+)", text):
                strings(match[1], True)
    return sorted(values, key=len, reverse=True)


def sensitive_files(root: Path, extra=()):
    """Protected values are read only for diagnostic redaction, never admission."""
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in folders + names:
            path = Path(directory, name)
            if path.is_symlink() and sensitive(path.relative_to(root), extra):
                raise PolicyViolation("Protected paths must not be symlinks; preserve and resolve them before model delivery")
        folders[:] = [name for name in folders if not Path(directory, name).is_symlink()]
        for name in names:
            path = Path(directory, name)
            if sensitive(path.relative_to(root), extra) and not path.is_symlink() and path.is_file():
                yield path
