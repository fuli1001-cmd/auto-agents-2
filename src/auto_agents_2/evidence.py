"""Central reuse policy: an input match permits reuse only of complete current evidence."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .util import contained, digest
from .workspace import input_entry, tree_hash

CHECKER_VERSION = "verification-v2"
RUNTIME_CACHES = {"__pycache__", ".cache", ".vite", ".tmp", ".pytest_cache"}


def input_identity(workspace, command):
    entries = [(p, input_entry(contained(workspace, p), workspace, excluded=set())) for p in command.inputs]
    for name in ("", "backend", "frontend", "api", "web"):
        modules = workspace / name / "node_modules"
        if modules.exists() and any(p.name not in RUNTIME_CACHES for p in modules.iterdir()):
            entries.append((modules.relative_to(workspace).as_posix(), input_entry(modules, workspace, excluded=RUNTIME_CACHES)))
    return digest(sorted(entries))



def check_environment(workspace, command, environment, scratch):
    cwd = workspace if command.cwd == "." else contained(workspace, command.cwd)
    variables = environment.variables(scratch) if environment else {
        **os.environ, "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}
    variables["PYTHONPATH"] = os.pathsep.join([str(cwd / "src"), str(cwd), str(workspace / "src"), str(workspace)])
    return variables


def runtime_configuration(workspace, command, environment):
    # Account for every inherited execution variable without storing its contents.
    # A stable placeholder represents controller-owned temporary/cache locations.
    variables = check_environment(workspace, command, environment, Path("/__aa2_temporary_input__"))
    return digest(variables)


@dataclass(frozen=True)
class ReuseDecision:
    allowed: bool
    reason: str


def reusable(commands, results, workspace: Path, environment, artifacts=None):
    if len(commands) != len(results) or not results:
        return ReuseDecision(False, "missing command evidence")
    source = tree_hash(workspace)
    for command, result in zip(commands, results):
        if (result.checker_version != CHECKER_VERSION or result.source_hash != source
                or result.environment_hash != environment.fingerprint
                or result.command_hash != digest(command.model_dump())
                or result.input_hash != input_identity(workspace, command)
                or result.runtime_config_hash != runtime_configuration(workspace, command, environment)):
            return ReuseDecision(False, "execution inputs, environment, command or checker changed")
        if result.timed_out or result.exit_code:
            return ReuseDecision(False, "unsuccessful observation")
        if artifacts:
            try:
                for ref in result.logs:
                    if not artifacts.released(ref):
                        artifacts.bytes(ref)
                if result.report:
                    artifacts.bytes(result.report)
            except (OSError, ValueError):
                return ReuseDecision(False, "evidence artifact unavailable or changed")
    return ReuseDecision(True, "complete matching controller evidence")


def invocation_identity(stage, context, schema, workspace, config, source_hash=None):
    from .prompts import PROMPT_VERSION
    from .readview import VIEW_VERSION
    return digest(["invocation-v3", PROMPT_VERSION, VIEW_VERSION, stage, context, schema.model_json_schema(),
                   source_hash or tree_hash(workspace), config.model_dump(mode="json")])
