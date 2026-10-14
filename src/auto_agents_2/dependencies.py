"""Select pip inputs once and resolve all roots in a single transaction."""
from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AA2Error
from .util import contained


@dataclass
class PipInputs:
    manifests: dict[str, str] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    local: bool = False

    def rendered(self, source: Path) -> tuple[str, str]:
        # Placeholder paths bind local dependencies to the owned immutable source copy.
        return tuple("\n".join(lines).replace("{AA2_SOURCE}", str(source)) + "\n"
                     for lines in (self.requirements, self.constraints))


def pip_inputs(workspace: Path, explicit: str = "", additional=()) -> PipInputs:
    result = PipInputs()

    def normalize(line: str, base: Path) -> str:
        editable = re.match(r"^(?:-e\s+|--editable[=\s]+)(.+)$", line)
        value = editable[1] if editable else line
        # Keep URLs/options/markers verbatim; only local path operands are rebound.
        direct = re.match(r"^(.*?\s+@\s+)(.+)$", value)
        operand = direct[2] if direct else value
        operand = operand.strip('"\'')
        local = operand.startswith(("./", "../", "/", "file:")) or operand == "." or (base / operand).exists()
        if local:
            operand = operand.removeprefix("file://").removeprefix("file:")
            suffix = ""
            if "[" in operand:
                operand, suffix = operand.split("[", 1)
                suffix = "[" + suffix
            path = (base / operand).resolve()
            if not path.is_relative_to(workspace.resolve()) or not path.exists():
                raise AA2Error("Local dependency must exist inside the execution workspace")
            result.local = True
            value = (direct[1] if direct else "") + "{AA2_SOURCE}/" + path.relative_to(workspace.resolve()).as_posix() + suffix
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
                path = base / "pyproject.toml"
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

    from .workspace import EXCLUDED, files, git
    paths = set(files(workspace))
    try:
        paths.update(workspace / name for name in git(workspace, "ls-files", "-z").split("\0") if name)
    except AA2Error:
        pass
    for directory, folders, names in os.walk(workspace, followlinks=False):
        paths.update(Path(directory, name) for name in folders + names
                     if name not in EXCLUDED and Path(directory, name).is_symlink())
        folders[:] = [name for name in folders if name not in EXCLUDED and not Path(directory, name).is_symlink()]
    copied = set()
    def copy(path):
        if path in copied:
            return
        copied.add(path)
        target = destination / path.relative_to(workspace)
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(workspace.resolve()):
                raise AA2Error("Editable source links must remain within the execution workspace")
            copy(resolved)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.path.relpath(destination / resolved.relative_to(workspace), target.parent))
        elif path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in path.iterdir():
                if child.name not in {".git", ".aa2"}:
                    copy(child)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for path in sorted(paths):
        copy(path)
