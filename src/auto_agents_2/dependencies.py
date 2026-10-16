"""Select pip inputs once and resolve all roots in a single transaction."""
from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from packaging.requirements import InvalidRequirement, Requirement

from .errors import AA2Error
from .util import contained, digest


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
    local_paths: dict[str, str] = field(default_factory=dict)
    protected: tuple[str, ...] = ()

    def include_local(self, workspace, path):
        from .readview import sensitive
        from .workspace import EXCLUDED, input_entry
        seen = set()
        def collect(item):
            name = item.relative_to(workspace).as_posix()
            if sensitive(Path(name), self.protected) or set(Path(name).parts) & {".git", ".aa2", ".venv", ".conda-env"}:
                raise AA2Error("Local dependency cannot include protected inputs")
            if item in seen:
                return
            seen.add(item)
            if item.is_symlink():
                target = item.resolve()
                if not target.is_relative_to(workspace) or not target.exists():
                    raise AA2Error("Local dependency links must remain within the execution workspace")
                collect(target)
            if item.is_dir() and not item.is_symlink():
                # Directory roots may be explicit generated inputs (e.g. dist).
                self.local_paths[name] = digest(["directory"])
                for child in sorted(item.iterdir()):
                    if child.name not in EXCLUDED and not sensitive(child.relative_to(workspace), self.protected):
                        collect(child)
            else:
                self.local_paths[name] = digest(input_entry(item, workspace))
        collect(path)

    def rendered(self, source: Path) -> tuple[str, str]:
        # Placeholder paths bind local dependencies to the owned immutable source copy.
        def render(line):
            line = line.replace("{AA2_SOURCE_URI}", source.as_uri())
            def local(match):
                operand = match[1]
                if line.startswith("-e "):
                    return shlex.quote(str(source / operand))
                extras = re.search(r"\[[A-Za-z0-9_,. -]+\]$", operand)
                suffix = extras[0] if extras else ""
                path = operand[:extras.start()] if extras else operand
                # Non-editable requirement operands are not shell-tokenized by pip.
                return (source / path).as_uri() + suffix
            return re.sub(r"\{AA2_SOURCE\}/(.*?)(?=\s+(?:;|--hash(?:=|\s)|--config-settings(?:=|\s))|$)",
                          local, line)
        return tuple("\n".join(render(line) for line in lines) + "\n"
                     for lines in (self.requirements, self.constraints))


def pip_inputs(workspace: Path, explicit: str = "", additional=(), *, protected=()) -> PipInputs:
    result = PipInputs(protected=tuple(protected))

    def options(line):
        quoted, escaped = "", False
        for index, char in enumerate(line):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char in {"'", '"'}:
                quoted = "" if char == quoted else char if not quoted else quoted
            elif not quoted and char.isspace() and re.match(r"\s+--(?:hash|config-settings)(?:=|\s)", line[index:]):
                suffix = line[index:].strip()
                try:
                    tokens = shlex.split(suffix)
                except ValueError as exc:
                    raise AA2Error("Invalid pip requirement options") from exc
                offset = 0
                while offset < len(tokens):
                    key, equals, value = tokens[offset].partition("=")
                    if key not in {"--hash", "--config-settings"}:
                        raise AA2Error("Unsupported per-requirement option")
                    if not equals:
                        offset += 1
                        value = tokens[offset] if offset < len(tokens) else ""
                    if not value or value.startswith("--"):
                        raise AA2Error("Missing pip requirement option value")
                    offset += 1
                return line[:index].rstrip(), " " + suffix
        return line, ""

    def include_name(name):
        try:
            parts = shlex.split(name)
        except ValueError as exc:
            raise AA2Error("Invalid quoting in pip manifest include") from exc
        if len(parts) != 1 or not parts[0].strip():
            raise AA2Error("Pip manifest include requires one nonempty filename")
        return parts[0]

    def normalize(line: str, base: Path) -> str:
        find_links = re.match(r"^(?:--find-links(?:=|\s+)|-f\s*)(.+)$", line)
        if find_links:
            operand = include_name(find_links[1])
            if urlsplit(operand).scheme in {"http", "https"}:
                return "--find-links " + operand
            if operand.startswith("file:"):
                url = urlsplit(operand)
                if url.netloc not in {"", "localhost"} or url.query or url.fragment:
                    raise AA2Error("Invalid local find-links URL")
                operand = unquote(url.path)
            path = Path(os.path.abspath(base / operand))
            if not path.resolve().is_relative_to(workspace.resolve()) or not path.exists():
                raise AA2Error("Local find-links must exist inside the execution workspace")
            result.include_local(workspace.resolve(), path)
            result.local = True
            return "--find-links {AA2_SOURCE_URI}/" + quote(path.relative_to(workspace.resolve()).as_posix(), safe="/")
        if line.startswith("-") and not re.match(r"^(?:-e\s+|--editable[=\s]+)", line):
            # Explicitly support pip's non-path requirement-file options only.
            if not re.match(r"^--(?:no-index|pre|prefer-binary|require-hashes|only-binary|no-binary|index-url|extra-index-url|trusted-host|use-feature)(?:\s|=|$)", line):
                raise AA2Error("Unsupported pip requirement-file option")
            if "file:" in line:
                raise AA2Error("Unsupported local path in pip option")
            if re.match(r"^--(?:index-url|extra-index-url)(?:=|\s)", line):
                value = re.split(r"[=\s]", line, maxsplit=1)[1].strip()
                if urlsplit(value).scheme not in {"http", "https"}:
                    raise AA2Error("Pip index options must name HTTP(S) URLs")
            return line
        line, requirement_options = options(line)
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
            path = (base / operand).absolute()
            if not path.resolve().is_relative_to(workspace.resolve()) or not path.exists():
                raise AA2Error("Local dependency must exist inside the execution workspace")
            path = Path(os.path.normpath(path))
            result.include_local(workspace.resolve(), path)
            result.local = True
            relative = path.relative_to(workspace.resolve()).as_posix()
            value = ((direct[1] + "{AA2_SOURCE_URI}/" + quote(relative, safe="/")) if direct else
                     "{AA2_SOURCE}/" + relative) + suffix + marker
        elif editable:
            result.local = True  # VCS editable installs also cannot share mutable bindings.
        return ("-e " if editable else "") + value + requirement_options

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
                read(path.parent / include_name(name), base, constraint or flag in {"-c", "--constraint"}, stack + (path,))
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
        if not line.strip():
            continue
        match = re.match(r"^(--requirement|--constraint)(?:=|\s+)(.+)$|^(-r|-c)\s*(.+)$", line.strip())
        if match:
            flag, name = (match[1], match[2]) if match[1] else (match[3], match[4])
            read(workspace / include_name(name), workspace, flag in {"-c", "--constraint"})
        else:
            result.requirements.append(normalize(line.strip(), workspace))
    return result


def copy_execution_source(workspace: Path, destination: Path, inputs: PipInputs | None = None):
    """Copy declared project inputs with internal links bound to the private copy."""
    import shutil

    from .readview import sensitive
    from .workspace import EXCLUDED, files, git, repository_workspace
    paths = set(files(workspace))
    if repository_workspace(workspace):
        paths.update(workspace / name for name in git(workspace, "ls-files", "-z").split("\0") if name)
    paths = {path for path in paths if not sensitive(path.relative_to(workspace), inputs.protected if inputs else ())
             and not set(path.relative_to(workspace).parts) & {".aa2", ".git", ".venv", ".conda-env"}}
    if inputs:
        paths.update(workspace / name for name in inputs.local_paths)
    directories = {parent for path in paths for parent in path.parents if parent.is_relative_to(workspace)}
    for directory, folders, names in os.walk(workspace, followlinks=False):
        directories.add(Path(directory))
        paths.update(Path(directory, name) for name in folders + names
                     if name not in EXCLUDED and name != ".env" and not name.startswith(".env.")
                     and Path(directory, name).is_symlink())
        folders[:] = [name for name in folders if name not in EXCLUDED and not Path(directory, name).is_symlink()]
    protected = inputs.protected if inputs else ()
    paths = {path for path in paths if not sensitive(path.relative_to(workspace), protected)}
    directories = {path for path in directories if not sensitive(path.relative_to(workspace), protected)}
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
    if inputs:
        from .workspace import input_entry
        for name, identity in inputs.local_paths.items():
            copied_path = destination / name
            actual = digest(["directory"]) if copied_path.is_dir() and not copied_path.is_symlink() else digest(input_entry(copied_path, destination))
            if actual != identity:
                raise AA2Error("Local dependency changed during source preparation")
