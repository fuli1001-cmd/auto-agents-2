"""Select pip inputs once and resolve all roots in a single transaction."""
from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from packaging.requirements import InvalidRequirement, Requirement

from .errors import AA2Error
from .util import contained


def manifest_reference(value: str, field: str) -> str:
    """Selectors are literal workspace-relative paths, never inline dependency text."""
    if not value:
        return value
    path = PurePosixPath(value)
    if (value != value.strip() or not path.parts or path.is_absolute() or ".." in path.parts
            or value.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in "\\:<>|?*=[]{};")):
        example = "environment.yml" if field == "environment_file" else "requirements.txt"
        raise AA2Error(f"{field} must be a workspace-relative file path such as {example}, "
                       "or empty for discovery; file contents, dependency expressions and external paths are not accepted")
    return value


@dataclass
class PipInputs:
    manifests: dict[str, str] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    local: bool = False

    def rendered(self, source: Path) -> tuple[str, str]:
        # Placeholder paths bind local dependencies to the owned immutable source copy.
        def render(line):
            line = line.replace("{AA2_SOURCE_URI}", source.as_uri())
            return re.sub(r"\{AA2_SOURCE\}/(.*?)(?=\s+;|$)",
                          lambda match: shlex.quote(str(source / match[1])), line)
        return tuple("\n".join(render(line) for line in lines) + "\n"
                     for lines in (self.requirements, self.constraints))


def pip_inputs(workspace: Path, explicit: str = "", additional=()) -> PipInputs:
    result = PipInputs()

    def normalize(line: str, base: Path) -> str:
        editable = re.match(r"^(?:-e\s+|--editable[=\s]+)(.+)$", line)
        value = editable[1] if editable else line
        # Keep URLs/options/markers verbatim; only local path operands are rebound.
        direct = re.match(r"^(.*?\s+@\s+)(.+)$", value)
        operand = direct[2] if direct else value
        marker = ""
        if direct:
            try:
                requirement = Requirement(value)
            except InvalidRequirement as exc:
                raise AA2Error("Invalid direct pip requirement") from exc
            operand = requirement.url or ""
            marker = " ; " + str(requirement.marker) if requirement.marker else ""
        else:
            quote_char = ""
            for index, char in enumerate(operand):
                if char in {"'", '"'}:
                    quote_char = "" if quote_char == char else char if not quote_char else quote_char
                elif char == ";" and not quote_char:
                    operand, marker = operand[:index], " ; " + operand[index + 1:].strip()
                    break
        operand = operand.strip()
        if len(operand) > 1 and operand[0] == operand[-1] and operand[0] in {"'", '"'}:
            operand = operand[1:-1]
        extras = re.search(r"\[[A-Za-z0-9_,. -]+\]$", operand)
        suffix = extras[0] if extras else ""
        operand = operand[:extras.start()] if extras else operand
        local = operand.startswith(("./", "../", "/", "file:")) or operand == "." or (base / operand).exists()
        if local:
            if operand.startswith("file:"):
                url = urlsplit(operand)
                if url.netloc not in {"", "localhost"} or url.query or url.fragment:
                    raise AA2Error("Local dependency must use a local file URL")
                operand = unquote(url.path)
            path = (base / operand).resolve()
            if not path.is_relative_to(workspace.resolve()) or not path.exists():
                raise AA2Error("Local dependency must exist inside the execution workspace")
            result.local = True
            relative = path.relative_to(workspace.resolve()).as_posix()
            value = ((direct[1] + "{AA2_SOURCE_URI}/" + quote(relative, safe="/")) if direct else
                     "{AA2_SOURCE}/" + relative) + suffix + marker
        elif editable:
            result.local = True  # VCS editable installs also cannot share mutable bindings.
        return ("-e " if editable else "") + value

    def read(path: Path, base: Path, constraint=False, stack=()):
        path = path.resolve()
        if not path.is_relative_to(workspace.resolve()) or not path.is_file():
            raise AA2Error("Missing or external pip manifest")
        if path in stack:
            raise AA2Error("Cyclic pip manifest inclusion")
        raw = path.read_text()
        result.manifests[path.relative_to(workspace.resolve()).as_posix()] = raw
        for line in raw.replace("\\\n", "").splitlines():
            line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^(--requirement|--constraint)(?:=|\s+)(.+)$|^(-r|-c)\s*(.+)$", line)
            if match:
                flag, name = (match[1], match[2]) if match[1] else (match[3], match[4])
                read(path.parent / shlex.split(name)[0], base, constraint or flag in {"-c", "--constraint"}, stack + (path,))
            else:
                (result.constraints if constraint else result.requirements).append(normalize(line, base))

    if explicit:
        read(contained(workspace, explicit), workspace)
    else:
        for name in ("", "backend", "frontend", "api", "web"):
            base = workspace / name
            selected = next((base / f for f in ("requirements.lock", "requirements.txt") if (base / f).is_file()), None)
            if selected:
                read(selected, base)
            elif (base / "pyproject.toml").is_file():
                path = (base / "pyproject.toml").resolve()
                if not path.is_relative_to(workspace.resolve()) or not path.is_file():
                    raise AA2Error("Missing or external project metadata")
                raw = path.read_text()
                result.manifests[path.relative_to(workspace).as_posix()] = raw
                project = tomllib.loads(raw).get("project", {})
                extras = project.get("optional-dependencies", {})
                for dependency in project.get("dependencies", []) + extras.get("test", []) + extras.get("dev", []):
                    result.requirements.append(normalize(dependency, base))
    for line in additional:
        match = re.match(r"^(--requirement|--constraint)(?:=|\s+)(.+)$|^(-r|-c)\s*(.+)$", line.strip())
        if match:
            flag, name = (match[1], match[2]) if match[1] else (match[3], match[4])
            read(workspace / shlex.split(name)[0], workspace, flag in {"-c", "--constraint"})
        else:
            result.requirements.append(normalize(line.strip(), workspace))
    return result


def copy_execution_source(workspace: Path, destination: Path):
    """Copy declared project inputs with internal links bound to the private copy."""
    import os
    import shutil

    from .workspace import EXCLUDED, files, git, repository_workspace
    paths = set(files(workspace))
    if repository_workspace(workspace):
        paths.update(workspace / name for name in git(workspace, "ls-files", "-z").split("\0") if name)
    directories = {parent for path in paths for parent in path.parents if parent.is_relative_to(workspace)}
    for directory, folders, names in os.walk(workspace, followlinks=False):
        directories.add(Path(directory))
        paths.update(Path(directory, name) for name in folders + names
                     if name not in EXCLUDED and name != ".env" and not name.startswith(".env.")
                     and Path(directory, name).is_symlink())
        folders[:] = [name for name in folders if name not in EXCLUDED and not Path(directory, name).is_symlink()]
    copied = set()
    def copy(path):
        if path in copied:
            return
        copied.add(path)
        if path not in paths and path not in directories:
            raise AA2Error("Editable link targets must be selected execution inputs")
        target = destination / path.relative_to(workspace)
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(workspace.resolve()):
                raise AA2Error("Editable source links must remain within the execution workspace")
            if resolved.exists():
                copy(resolved)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.path.relpath(destination / resolved.relative_to(workspace), target.parent))
        elif path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in path.iterdir():
                if child in paths or child in directories:
                    copy(child)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for path in sorted(paths):
        copy(path)
