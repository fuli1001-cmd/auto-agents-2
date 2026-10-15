"""Provider-only public snapshot; execution and verification keep the real input view."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
from pathlib import Path

from .util import SECRET
from .workspace import EXCLUDED, file_digest, git

VIEW_VERSION = "public-v3"
SENSITIVE_DIRS = {".aws", ".ssh", ".codex", ".gemini", ".credentials"}
SENSITIVE_FILES = {".netrc", ".npmrc", ".pypirc", ".git-credentials"}


def sensitive(path: Path, extra=()) -> bool:
    return (any(fnmatch.fnmatchcase(path.as_posix(), p) or path.as_posix().startswith(p.rstrip("/") + "/") for p in extra)
            or bool(set(path.parts) & SENSITIVE_DIRS) or path.name in SENSITIVE_FILES or path.name == ".env"
            or path.name.startswith(".env.") and path.name != ".env.example")


def public_files(root: Path, extra=()):
    candidates, denied_inodes, denied_hashes = [], set(), set()
    for path in sensitive_files(root, extra):
        info = path.stat()
        denied_inodes.add((info.st_dev, info.st_ino))
        denied_hashes.add(file_digest(path))
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [n for n in folders if n not in EXCLUDED - SENSITIVE_DIRS
                      and not Path(directory, n).is_symlink()]
        for name in names:
            path = Path(directory, name)
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            inode = (info.st_dev, info.st_ino)
            if sensitive(path.relative_to(root), extra):
                denied_inodes.add(inode)
                denied_hashes.add(file_digest(path))
            elif name not in EXCLUDED:
                candidates.append((path, inode))
    values = [v.encode() for v in sensitive_values(root, extra)]
    def contains_protected(path):
        retain = max((len(v) for v in values), default=1) - 1
        tail = b""
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                data = tail + chunk
                if any(v in data for v in values):
                    return True
                tail = data[-retain:] if retain else b""
        return False
    return [path for path, inode in candidates if inode not in denied_inodes
            and file_digest(path) not in denied_hashes and not contains_protected(path)]


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
            for line in text.splitlines():
                match = re.match(r"\s*([^=]+)\s*=\s*(.*)", line) if "=" in line else re.match(r"\s*([^:]+)\s*:\s*(.*)", line)
                if match and not line.lstrip().startswith("#"):
                    key, value = match.groups()
                    value = value.strip().strip("\"'")
                    if SECRET.search(key) or re.search(r"://[^/\s]+:[^/\s]+@", value):
                        strings(value, True)
            if "PRIVATE KEY" in text:
                for line in text.splitlines():
                    if line.strip() and not line.startswith("-----"):
                        strings(line.strip(), True)
            for match in re.finditer(r"(?i)\bpassword\s+(\S+)", text):
                strings(match[1], True)
    return sorted(values, key=len, reverse=True)


def sensitive_files(root: Path, extra=()):
    """Output exclusions do not exempt protected files from alias detection."""
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if not Path(directory, name).is_symlink()]
        for name in names:
            path = Path(directory, name)
            if sensitive(path.relative_to(root), extra) and not path.is_symlink() and path.is_file():
                yield path
