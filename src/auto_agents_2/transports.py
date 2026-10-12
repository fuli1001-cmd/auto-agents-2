"""Provider-specific CLI syntax. Routing and workflow code depend on CLITransport."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class CLITransport(Protocol):
    stdin_prompt: bool
    auth_patterns: tuple[str, ...]

    def native_home(self, environment: dict) -> Path: ...
    def encode_prompt(self, prompt: str) -> bytes: ...
    def argv(self, effective: dict, workspace: Path, schema: Path, output: Path,
             prompt: str, previous: str | None, images: list[Path], context_files=()) -> list[str]: ...


class CodexTransport:
    stdin_prompt = True
    auth_patterns = ("*.toml", "auth.json", "*.config.toml")

    def native_home(self, environment):
        return Path(environment.get("CODEX_HOME", "~/.codex")).expanduser()

    def encode_prompt(self, prompt):
        return prompt.encode()

    def argv(self, effective, workspace, schema, output, prompt, previous, images, context_files=()):
        args = [effective["binary"], "exec"]
        if effective["native_profile"]:
            args += ["-p", effective["native_profile"]]
        else:
            if effective["model"]:
                args += ["-m", effective["model"]]
            if effective["effort"]:
                args += ["-c", f"model_reasoning_effort={effective['effort']}"]
        args += ["-s", "read-only", "-C", str(workspace)]
        for asset in images[:8]:
            args += ["-i", str(asset)]
        args += effective["args"] + ["--json", "--output-schema", str(schema), "-o", str(output)]
        if previous:
            args += ["resume", previous]
        return args + ["-"]


class AntigravityTransport:
    stdin_prompt = True
    auth_patterns = ("*.json", "antigravity-cli/settings.json", "antigravity-cli/antigravity-oauth-token", "config/*.json")

    def native_home(self, environment):
        return Path("~/.gemini").expanduser()

    def encode_prompt(self, prompt):
        return (json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n").encode()

    def argv(self, effective, workspace, schema, output, prompt, previous, images, context_files=()):
        args = [effective["binary"], "--input-format", "stream-json", "--output-format", "stream-json", "--json-schema", str(schema), "--print-timeout", "0"]
        for directory in sorted({str(p.parent) for p in [*images, *context_files]}):
            args += ["--add-dir", directory]
        if effective["model"]:
            args += ["--model", effective["model"]]
        if effective["effort"]:
            args += ["--effort", effective["effort"]]
        if previous:
            args += ["--conversation", previous]
        return args + effective["args"]


BUILTIN_TRANSPORTS: dict[str, CLITransport] = {"codex": CodexTransport(), "antigravity": AntigravityTransport()}
