"""Generated-output contracts and dependency order, independent of process execution."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import AA2Error, PolicyViolation, VerificationPreparationError
from .models import Command
from .profiles import package_manifest
from .util import contained, digest
from .workspace import git, input_entry, repository_workspace

BUILD_CONTRACT_VERSION = 1


def normalized(command: Command, workspace: Path) -> Command:
    outputs = list(command.outputs)
    cwd = contained(workspace, command.cwd) if command.cwd != "." else workspace
    # Compatibility for already accepted Vite/Next plans. A script is still run
    # exactly as declared; inferred outputs must actually be produced and verified.
    if not outputs and Path(command.argv[0]).name == "npm" and command.argv[1:] == ["run", "build"]:
        manifest = cwd / "package.json"
        try:
            if not manifest.exists():
                return command
            data = package_manifest(manifest)
            dependencies = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            directory = ".next" if "next" in dependencies else "dist" if "vite" in dependencies else None
            if directory:
                outputs = [(PurePosixPath(command.cwd) / directory).as_posix()]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AA2Error) as exc:
            raise VerificationPreparationError(f"Cannot infer build outputs from {command.cwd}/package.json") from exc
    allowed = {cwd / "dist", cwd / ".next"}
    for output in outputs:
        path = contained(workspace, output)
        if path not in allowed or path.absolute() != path.resolve():
            raise PolicyViolation("Build outputs must be the command cwd's dist or .next, without symlinks")
        for declared in command.inputs:
            source = contained(workspace, declared)
            if path.is_relative_to(source) or source.is_relative_to(path):
                raise VerificationPreparationError(f"Build input overlaps its output: {declared} / {output}")
        try:
            tracked = git(workspace, "ls-files", "-z", "--", output) if repository_workspace(workspace) else ""
        except AA2Error as exc:
            raise VerificationPreparationError("Cannot verify build output ownership") from exc
        if tracked:
            raise PolicyViolation(f"Build output contains tracked source: {output}")
    outputs = [contained(workspace, path).relative_to(workspace).as_posix() for path in outputs]
    if len(outputs) != len(set(outputs)):
        raise VerificationPreparationError("Duplicate build outputs")
    return command.model_copy(update={"outputs": outputs})


def generated_entry(path: Path, workspace: Path):
    # Next's runtime cache and tracing are mutable; server code and build manifests
    # remain part of the immutable output identity.
    omitted = {path / "cache", path / "trace", path / "server/route-cache"} if path.name == ".next" else set()
    return input_entry(path, workspace, excluded=set(), omitted=omitted)


def output_hashes(command: Command, workspace: Path) -> dict[str, str]:
    try:
        return _output_hashes(command, workspace)
    except OSError as exc:
        raise VerificationPreparationError("Build outputs cannot be inspected or hashed") from exc


def _output_hashes(command: Command, workspace: Path) -> dict[str, str]:
    result = {}
    for output in command.outputs:
        path = contained(workspace, output)
        if not path.is_dir() or path.is_symlink() or any(
                p.is_symlink() or not (p.is_dir() or p.is_file()) for p in path.rglob("*")):
            raise VerificationPreparationError(f"Build output missing or contains symlinks: {output}")
        entry = generated_entry(path, workspace)
        def has_file(value):
            return value[0] == "file" or value[0] == "directory" and any(has_file(child) for _, child in value[1])
        if not has_file(entry):
            raise VerificationPreparationError(f"Build produced no files: {output}")
        result[output] = digest(entry)
    return result


def command_hashes(command: Command) -> set[str]:
    """Recognize an old observation for recovery, never reuse it as new evidence."""
    body = command.model_dump()
    return {digest(body), digest({k: v for k, v in body.items() if k != "outputs"})} if not command.outputs else {digest(body)}


def overlaps(workspace, first, second):
    left, right = (contained(workspace, name).resolve() for name in (first, second))
    return left.is_relative_to(right) or right.is_relative_to(left)


def runtime_commands(plan, workspace, environment):
    commands = verification_commands(plan.runtime_probes, workspace, plan.runtime_prepare, prepare_all=True) if plan else []
    return commands or [Command(argv=[str(environment.prefix / "bin/python"), "-c",
                                      "import sys; sys.exit(0 if sys.prefix == " + repr(str(environment.prefix)) + " else 1)"])]


def verification_commands(commands: list[Command], workspace: Path, preparations=(), *, prepare_all=False) -> list[Command]:
    """Topologically place declared producers before consumers in this workspace.

    Task gates select only preparations their inputs require. Candidate acceptance
    also requires every runtime preparation, before any tests or runtime probes.
    """
    checks = [normalized(c, workspace) for c in commands]
    prepare = [normalized(c, workspace) for c in preparations]
    builds = {}
    def execution(command):
        return digest([command.argv, command.cwd, command.outputs])
    for command in [*prepare, *checks]:
        if command.outputs:
            key = execution(command)
            previous = builds.get(key, command)
            builds[key] = command.model_copy(update={
                "timeout": max(previous.timeout, command.timeout),
                "expected_tests": max(previous.expected_tests, command.expected_tests),
                "inputs": sorted(set(previous.inputs) | set(command.inputs)),
            })
    checks = [builds[execution(c)] if c.outputs else c for c in checks]
    prepare = [builds[execution(c)] if c.outputs else c for c in prepare]
    producers = {}
    for command in [*prepare, *checks]:
        for output in command.outputs:
            for owned, producer in producers.items():
                if (producer.model_dump() != command.model_dump()
                        and (PurePosixPath(output).is_relative_to(owned) or PurePosixPath(owned).is_relative_to(output))):
                    raise VerificationPreparationError(f"Conflicting producers for {output} and {owned}")
            previous = producers.get(output)
            if previous and previous.model_dump() != command.model_dump():
                raise VerificationPreparationError(f"Conflicting producers for {output}")
            producers[output] = command
    ordered, visited, active = [], set(), set()

    def visit(command):
        key = digest(command.model_dump())
        if key in active:
            raise VerificationPreparationError("Build preparation dependency cycle")
        if key in visited:
            return
        active.add(key)
        for declared in command.inputs:
            for output, producer in producers.items():
                if overlaps(workspace, declared, output):
                    visit(producer)
        active.remove(key)
        visited.add(key)
        ordered.append(command)

    for command in [*(prepare if prepare_all else []), *checks]:
        visit(command)
    return ordered
