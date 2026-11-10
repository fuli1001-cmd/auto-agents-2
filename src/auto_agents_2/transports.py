"""Provider-specific CLI syntax. Routing and workflow code depend on CLITransport."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol


def native_input_bytes(path, *, optional=False):
    """Do not turn a selected input into access to a linked, different target."""
    from .errors import ConfigurationError, PolicyViolation
    from .safe_files import directory_fd, regular_file
    try:
        with directory_fd(path.parent) as parent, regular_file(parent, path.name) as stream:
            info = os.fstat(stream.fileno())
            limit = 32 * 1024**2
            if info.st_nlink != 1 or info.st_size > limit:
                raise ValueError("Selected native input is linked or too large")
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("Selected native input is too large")
            return raw
    except FileNotFoundError as error:
        if optional:
            return b""
        raise ConfigurationError("Selected native input is missing") from error
    except (OSError, ValueError) as error:
        raise PolicyViolation("Selected native input must be an unlinked regular file") from error


class CLITransport(Protocol):
    stdin_prompt: bool
    auth_patterns: tuple[str, ...]
    max_prompt_characters: int | None

    def native_home(self, environment: dict) -> Path: ...
    def encode_prompt(self, prompt: str) -> bytes: ...
    def argv(self, effective: dict, workspace: Path, schema: Path, output: Path,
             prompt: str, previous: str | None, images: list[Path], context_files=()) -> list[str]: ...


class CodexTransport:
    stdin_prompt = True
    auth_patterns = ("*.toml", "auth.json", "*.config.toml")
    # Codex turn/start rejects longer inputs before the model is invoked.
    max_prompt_characters = 1048576

    def native_home(self, environment):
        return Path(environment.get("CODEX_HOME", "~/.codex")).expanduser()

    def input_files(self, effective):
        # The ordinary CLI needs its base settings, credentials and selected profile,
        # not every unrelated profile or historical native-home file.
        native = self.native_home(effective["environment"])
        names = ["config.toml", "auth.json"]
        profile = effective["native_profile"]
        if profile:
            from .errors import ConfigurationError
            if Path(profile).name != profile or profile in {".", ".."} or "\\" in profile:
                raise ConfigurationError("Native profile must be a single profile name")
            names.append(profile + ".config.toml")
        from .errors import PolicyViolation
        from .safe_files import directory_fd, regular_exists
        try:
            with directory_fd(native) as parent:
                selected = []
                for name in names:
                    if regular_exists(parent, name):
                        if os.stat(name, dir_fd=parent, follow_symlinks=False).st_nlink != 1:
                            raise ValueError("Selected native input has another hard link")
                        selected.append(native / name)
                return tuple(selected)
        except FileNotFoundError:
            return ()
        except (OSError, ValueError) as error:
            raise PolicyViolation("Selected native inputs must be unlinked regular files") from error

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
        for asset in images:
            args += ["-i", str(asset)]
        args += effective["args"] + ["--json", "--output-schema", str(schema), "-o", str(output)]
        if previous:
            args += ["resume", previous]
        return args + ["-"]


class AntigravityTransport:
    stdin_prompt = True
    auth_patterns = ("*.json", "antigravity-cli/settings.json", "antigravity-cli/antigravity-oauth-token", "config/*.json")
    max_prompt_characters = None

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
