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
    source_paths: dict[str, str] = field(default_factory=dict)
    local_roots: list[str] = field(default_factory=list)

    def include_local(self, workspace, path):
        if path.relative_to(workspace).as_posix() not in self.local_roots:
            self.local_roots.append(path.relative_to(workspace).as_posix())
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
                children = [child for child in sorted(item.iterdir()) if child.name not in EXCLUDED
                            and not sensitive(child.relative_to(workspace), self.protected)]
                self.local_paths[name] = digest(["directory", [child.name for child in children]])
                for child in children:
                    collect(child)
            else:
                self.local_paths[name] = digest(input_entry(item, workspace))
        collect(path)

    def verify_local(self, workspace):
        current = PipInputs(protected=self.protected)
        for name in self.local_roots:
            current.include_local(workspace, workspace / name)
        if current.local_paths != self.local_paths:
            raise AA2Error("Local dependency membership or contents changed during source preparation")

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

    def normalize(line: str, base: Path, constraint=False, stack=(), include_base=None) -> str:
        if line.startswith("-") and not re.match(r"^(?:-e\s+|--editable[=\s]+)", line):
            try:
                tokens = shlex.split(line)
            except ValueError as exc:
                raise AA2Error("Invalid pip option quoting") from exc
            flags = {"--no-index", "--pre", "--prefer-binary", "--require-hashes"}
            values = {"--only-binary", "--no-binary", "--index-url", "--extra-index-url", "--trusted-host", "--use-feature",
                      "--find-links", "--requirement", "--constraint"}
            aliases = {"-f": "--find-links", "-r": "--requirement", "-c": "--constraint"}
            rendered = []
            offset = 0
            while offset < len(tokens):
                key, equals, value = tokens[offset].partition("=")
                if key[:2] in aliases:
                    value = value if equals else key[2:]
                    equals = bool(value)
                    key = aliases[key[:2]]
                if key in flags and not equals:
                    rendered.append(key)
                elif key in values:
                    if not equals:
                        offset += 1
                        value = tokens[offset] if offset < len(tokens) else ""
                    if not value or value.startswith("-"):
                        raise AA2Error("Missing pip option operand")
                    if key in {"--requirement", "--constraint"}:
                        if "${" in value:
                            raise AA2Error("Dynamic local pip paths cannot be snapshotted")
                        read((include_base or base) / value, base, constraint or key == "--constraint", stack)
                    elif key == "--find-links":
                        if urlsplit(value).scheme not in {"http", "https"}:
                            if "${" in value:
                                raise AA2Error("Dynamic local pip paths cannot be snapshotted")
                            if value.startswith("file:"):
                                url = urlsplit(value)
                                if url.netloc not in {"", "localhost"} or url.query or url.fragment:
                                    raise AA2Error("Invalid local find-links URL")
                                value = unquote(url.path)
                            path = (base / value).resolve()
                            if not path.is_relative_to(workspace.resolve()) or not path.exists():
                                raise AA2Error("Local find-links must exist inside the execution workspace")
                            result.include_local(workspace.resolve(), path)
                            result.local = True
                            value = "{AA2_SOURCE_URI}/" + quote(path.relative_to(workspace.resolve()).as_posix(), safe="/")
                        rendered.append(key + " " + value)
                    else:
                        if "file:" in value or key in {"--index-url", "--extra-index-url"} and urlsplit(value).scheme not in {"http", "https"}:
                            raise AA2Error("Pip index options must name HTTP(S) URLs")
                        rendered.append(key + " " + shlex.quote(value))
                else:
                    raise AA2Error("Unsupported pip requirement-file option")
                offset += 1
            return "\n".join(rendered)
        line, requirement_options = options(line)
        editable = re.match(r"^(?:-e\s+|--editable[=\s]+)(.+)$", line)
        value = editable[1] if editable else line
        # Keep URLs/options/markers verbatim; only local path operands are rebound.
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            requirement = None
        direct = requirement if requirement and requirement.url else None
        operand = direct.url if direct else value
        marker = ""
        if direct:
            marker = " ; " + str(direct.marker) if direct.marker else ""
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
        if ("${" in operand and urlsplit(operand).scheme not in {"http", "https"}) or operand.startswith("~"):
            raise AA2Error("Dynamic local pip paths cannot be snapshotted")
        local = urlsplit(operand).scheme == "file" or operand.startswith(("./", "../", "/")) or operand == "." or (base / operand).exists()
        if local:
            if "${" in operand:
                raise AA2Error("Dynamic local pip paths cannot be snapshotted")
            if urlsplit(operand).scheme == "file":
                url = urlsplit(operand)
                if url.netloc not in {"", "localhost"} or url.query or url.fragment:
                    raise AA2Error("Local dependency must use a local file URL")
                operand = unquote(url.path)
            path = (base / operand).resolve()
            if not path.resolve().is_relative_to(workspace.resolve()) or not path.exists():
                raise AA2Error("Local dependency must exist inside the execution workspace")
            path = Path(os.path.normpath(path))
            result.include_local(workspace.resolve(), path)
            result.local = True
            relative = path.relative_to(workspace.resolve()).as_posix()
            value = ((direct.name + ("[" + ",".join(sorted(direct.extras)) + "]" if direct.extras else "") + " @ {AA2_SOURCE_URI}/" + quote(relative, safe="/")) if direct else
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
        raw = path.read_text(encoding="utf-8")
        result.manifests[path.relative_to(workspace.resolve()).as_posix()] = raw
        for line in raw.replace("\\\n", "").splitlines():
            line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue
            normalized = normalize(line, base, constraint, stack + (path,), include_base=path.parent)
            if normalized:
                (result.constraints if constraint else result.requirements).append(normalized)

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
                raw = path.read_text(encoding="utf-8")
                result.manifests[path.relative_to(workspace).as_posix()] = raw
                project = tomllib.loads(raw).get("project", {})
                extras = project.get("optional-dependencies", {})
                for dependency in project.get("dependencies", []) + extras.get("test", []) + extras.get("dev", []):
                    result.requirements.append(normalize(dependency, base))
    for line in additional:
        if not line.strip():
            continue
        normalized = normalize(line.strip(), workspace)
        if normalized:
            result.requirements.append(normalized)
    if result.local:
        result.source_paths = source_snapshot(workspace, result)
    return result


def copy_execution_source(workspace: Path, destination: Path, inputs: PipInputs | None = None):
    """Copy only the captured input membership; reject drift before installation."""
    import shutil

    if inputs:
        inputs.verify_local(workspace)
    snapshot = inputs.source_paths if inputs and inputs.source_paths else source_snapshot(workspace, inputs)
    if source_snapshot(workspace, inputs) != snapshot:
        raise AA2Error("Local dependency changed before source preparation")
    paths = {workspace / name for name in snapshot}
    directories = {p for p in paths if p.is_dir() and not p.is_symlink()}
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
    if source_snapshot(workspace, inputs) != snapshot:
        raise AA2Error("Local dependency changed during source preparation")
    if inputs:
        inputs.verify_local(workspace)
    copied_paths = {destination / name for name in snapshot}
    actual = {name: source_identity(destination / name, destination, copied_paths) for name in snapshot}
    if actual != snapshot:
        raise AA2Error("Local dependency copy differs from selected inputs")


def source_paths(workspace, inputs):
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
    return paths | directories


def source_identity(path, workspace, paths):
    from .workspace import input_entry
    if path.is_symlink():
        target = path.resolve()
        if not target.is_relative_to(workspace.resolve()):
            raise AA2Error("Editable source links must remain within the execution workspace")
        return digest(["link", target.relative_to(workspace).as_posix()])
    if path.is_dir():
        return digest(["directory", sorted(child.name for child in path.iterdir() if child in paths)])
    return digest(input_entry(path, workspace))


def source_snapshot(workspace, inputs=None):
    paths = source_paths(workspace, inputs)
    return {path.relative_to(workspace).as_posix(): source_identity(path, workspace, paths) for path in sorted(paths)}
