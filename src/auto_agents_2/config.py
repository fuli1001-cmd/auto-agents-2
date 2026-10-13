from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .errors import ConfigurationError
from .models import Command, Record
from .util import digest, file_lock, redact, write_json

DEFAULT_EFFORTS = {
    "clarify": "deep", "coverage_review": "balanced", "provider_research": "deep",
    "prototype": "max", "design": "deep", "plan": "max", "implement": "deep",
    "review": "balanced", "verify": "balanced", "readme": "balanced",
}


class Profile(Record):
    file: str = ""
    model: str = ""
    effort: str = ""
    environment: dict[str, str | None] = {}
    extra_args: list[str] = []
    prompt_profile: str = "auto"


class ProviderConfig(Record):
    kind: str = Field(min_length=1)
    binary: str
    environment: dict[str, str | None] = {}
    profile_map: dict[str, str | Profile]
    extra_args: list[str] = []


class Execution(Record):
    max_parallel_tasks: int = Field(default=2, ge=1, le=32)
    max_rounds: int = Field(default=4, ge=1)
    max_stalled_rounds: int = Field(default=2, ge=1)
    max_replans: int = Field(default=2, ge=0)
    infra_retries: int = Field(default=3, ge=0, le=10)
    # Accepted only for compatibility with existing project configuration. Model
    # calls have no wall-clock deadline; command/build timeouts remain separate.
    agent_timeout: int | None = Field(default=None, exclude=True)
    max_tokens: int | None = Field(default=None, ge=1)
    wait_poll_seconds: float = Field(default=1, gt=0)


class Cleanup(Record):
    interval_seconds: int = Field(default=60, ge=1)
    cache_max_bytes: int = Field(default=10 * 1024**3, ge=0)
    cache_ttl_seconds: int = Field(default=7 * 86400, ge=0)
    min_free_bytes: int = Field(default=5 * 1024**3, ge=0)


class EnvironmentConfig(Record):
    conda: str = "conda"
    python_version: str = "3.11"
    node_version: str = "22"
    environment_file: str = ""
    requirements_file: str = ""


class DeliveryConfig(Record):
    target_branch: str = ""
    max_reintegrations: int = Field(default=2, ge=0)


class Config(Record):
    schema_version: Literal[1] = 1
    config_revision: int = 0
    active_provider: str = "codex"
    providers: dict[str, ProviderConfig]
    efforts: dict[str, str] = DEFAULT_EFFORTS
    execution: Execution = Execution()
    cleanup: Cleanup = Cleanup()
    environment: EnvironmentConfig = EnvironmentConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    project_profile: Literal["auto", "python", "react", "next", "python-react", "python-next"] = "auto"
    verification: list[Command] = []
    sandbox_binary: str = "bwrap"
    prompting: dict[str, str] = {"model_adaptation": "auto"}

    @model_validator(mode="after")
    def validate_providers(self):
        if self.active_provider not in self.providers:
            raise ValueError("active_provider must be one of providers")
        return self


def default_config() -> Config:
    native = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    mapping = {}
    for level, effort in (("balanced", "medium"), ("deep", "high"), ("max", "max")):
        mapping[level] = level if (native / f"{level}.config.toml").is_file() else Profile(effort=effort)
    return Config(providers={
        "codex": ProviderConfig(kind="codex", binary="codex", environment={
            "CODEX_HOME": str(native), "CODEX_API_KEY": None, "OPENAI_API_KEY": None,
        }, profile_map=mapping),
        "antigravity": ProviderConfig(kind="antigravity", binary="agy", profile_map={
            "balanced": Profile(effort="medium"), "deep": Profile(effort="high"),
            "max": Profile(effort="high"),
        }),
    })


class ConfigStore:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.path = self.project / ".aa2/config.json"
        if self.path.parent.is_symlink():
            raise ConfigurationError("Project configuration must use a local .aa2 directory")

    def initialize(self) -> Config:
        with file_lock(self.path.with_suffix(".lock")):
            if not self.path.exists():
                write_json(self.path, default_config().model_dump(mode="json"))
        return self.load()

    def load(self) -> Config:
        try:
            return Config.model_validate_json(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"Cannot read {self.path}: {exc}") from exc

    def select_target(self, branch: str):
        with file_lock(self.path.with_suffix(".lock")):
            body = json.loads(self.path.read_text())
            body.setdefault("delivery", {})["target_branch"] = branch
            body["config_revision"] = body.get("config_revision", 0) + 1
            Config.model_validate(body)
            write_json(self.path, body)

    def activate(self, provider: str, expected_revision: int | None = None) -> tuple[str, str]:
        with file_lock(self.path.with_suffix(".lock")):
            cfg = self.load()
            if provider not in cfg.providers:
                raise ConfigurationError(f"Provider is not configured: {provider}")
            old = cfg.active_provider
            if expected_revision is not None and cfg.config_revision != expected_revision:
                return old, old
            # Keep user ordering and fields, replacing only the active selection.
            body = json.loads(self.path.read_text())
            body["active_provider"] = provider
            body["config_revision"] = cfg.config_revision + 1
            write_json(self.path, body)
            return old, provider

    def resolve(self, stage: str, provider: str | None = None) -> dict:
        cfg = self.load()
        name = provider or cfg.active_provider
        if name not in cfg.providers:
            raise ConfigurationError(f"Unknown provider: {name}")
        spec = cfg.providers[name]
        level = cfg.efforts.get(stage, cfg.efforts.get("review", "balanced"))
        if level not in spec.profile_map:
            raise ConfigurationError(f"{name}: no profile for level {level}")
        environment = dict(os.environ)
        for key, value in spec.environment.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = os.path.expanduser(value)
        profile = spec.profile_map[level]
        native_profile = ""
        model = ""
        fingerprints = []
        if isinstance(profile, str):
            if spec.kind != "codex":
                profile = Profile(file=profile)
            else:
                native_profile = profile
                home = Path(environment.get("CODEX_HOME", "~/.codex")).expanduser()
                file = home / f"{profile}.config.toml"
                if not file.is_file():
                    raise ConfigurationError(f"Missing native profile: {file}")
                settings = {}
                if (home / "config.toml").exists():
                    settings.update(tomllib.loads((home / "config.toml").read_text()))
                settings.update(tomllib.loads(file.read_text()))
                fingerprints.append(digest(file.read_bytes()))
                profile = Profile(model=str(settings.get("model", "")),
                                  effort=str(settings.get("model_reasoning_effort", "")))
        if profile.file:
            file = Path(profile.file).expanduser()
            if not file.is_absolute():
                file = self.path.parent / file
            try:
                profile = Profile.model_validate_json(file.read_text())
                fingerprints.append(digest(file.read_bytes()))
            except (OSError, ValueError) as exc:
                raise ConfigurationError(f"Invalid profile file: {file}") from exc
        for key, value in profile.environment.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = os.path.expanduser(value)
        extra = profile.extra_args + spec.extra_args
        validate_extra_args(extra)
        model = profile.model
        effort = profile.effort
        i = 0
        while i < len(extra):
            key, separator, value = extra[i].partition("=")
            if not separator:
                i += 1
                value = extra[i]
            if key in {"-m", "--model"}:
                model = value
            elif key == "--effort":
                effort = value
            elif key in {"-c", "--config"}:
                setting, _, value = value.partition("=")
                if setting == "model":
                    model = value.strip().strip("\"'")
                elif setting == "model_reasoning_effort":
                    effort = value.strip().strip("\"'")
            i += 1
        binary = shutil.which(os.path.expanduser(spec.binary), path=environment.get("PATH"))
        if not binary:
            raise ConfigurationError(f"Provider executable not found: {spec.binary}")
        public = {"provider": name, "kind": spec.kind, "level": level, "model": model,
                  "effort": effort, "native_profile": native_profile,
                  "extra_args": redact(extra), "profile_hashes": fingerprints,
                  "prompt_profile": profile.prompt_profile}
        public["fingerprint"] = digest(public)
        return {**public, "binary": binary, "environment": environment, "args": extra}


def validate_extra_args(args: list[str]) -> None:
    permitted_values = {"--model", "-m", "--effort", "--print-timeout"}
    i = 0
    while i < len(args):
        arg = args[i]
        key = arg.split("=", 1)[0]
        if key in permitted_values:
            i += 1 if "=" in arg else 2
            if i > len(args):
                raise ConfigurationError(f"Missing value for {arg}")
            continue
        if arg in {"-c", "--config"} and i + 1 < len(args):
            setting = args[i + 1].split("=", 1)[0]
            if setting not in {"model", "model_provider", "model_reasoning_effort", "service_tier"}:
                raise ConfigurationError(f"Protected runtime setting: {setting}")
            i += 2
            continue
        raise ConfigurationError(f"Unsupported/protected extra argument: {arg}")
