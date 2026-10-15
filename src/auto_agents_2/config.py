from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

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
    max_stalled_replans: int = Field(default=2, ge=1)
    max_diagnosis_rounds: int = Field(default=2, ge=1)
    infra_retries: int = Field(default=3, ge=0, le=10)
    @model_validator(mode="before")
    @classmethod
    def legacy_token_budget(cls, value):
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "max_tokens"}
        return value

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
    playwright_browsers: list[Literal["chromium", "firefox", "webkit"]] = Field(default=["chromium"], min_length=1)


class DeliveryConfig(Record):
    target_branch: str = ""
    max_reintegrations: int = Field(default=2, ge=0)
    max_runtime_retries: int = Field(default=3, ge=0)


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
    project_profile: Literal["auto", "python", "node", "python-node", "react", "next", "python-react", "python-next"] = "auto"
    verification: list[Command] = []
    sandbox_binary: str = "bwrap"
    sensitive_paths: list[str] = []
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
        except ValidationError as exc:
            schema = Config.model_json_schema()
            known = {"agent_timeout", *schema.get("properties", {})}
            for definition in schema.get("$defs", {}).values():
                known.update(definition.get("properties", {}))
            locations = [".".join(str(part) if part in known else "<key>" for part in e["loc"])
                         for e in exc.errors(include_input=False, include_context=False)]
            raise ConfigurationError("Invalid configuration fields: " + ", ".join(locations)) from None
        except (OSError, ValueError):
            raise ConfigurationError("Cannot read or validate configuration") from None

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
                try:
                    settings = {}
                    if (home / "config.toml").exists():
                        raw_base = (home / "config.toml").read_bytes()
                        settings.update(tomllib.loads(raw_base.decode()))
                        fingerprints.append(digest(raw_base))
                    settings.update(tomllib.loads(file.read_text()))
                    fingerprints.append(digest(file.read_bytes()))
                except (OSError, ValueError) as exc:
                    raise ConfigurationError("Cannot read or parse native provider profile") from exc
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
        binary = shutil.which(os.path.expanduser(spec.binary), path=environment.get("PATH", os.defpath))
        if not binary:
            raise ConfigurationError(f"Provider executable not found: {spec.binary}")
        public = {"provider": name, "kind": spec.kind, "level": level, "model": model,
                  "effort": effort, "native_profile": native_profile,
                  "extra_args": redact(extra), "profile_hashes": fingerprints,
                  "prompt_profile": profile.prompt_profile}
        from .workspace import file_digest
        try:
            native = Path(environment.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
            native_hash = digest(native.read_bytes()) if spec.kind == "codex" and native.is_file() else ""
            public["fingerprint"] = digest(["provider-execution-v2", public, str(Path(binary).resolve()),
                file_digest(Path(binary)), spec.environment, profile.environment, native_hash])
        except OSError as exc:
            raise ConfigurationError("Cannot establish provider execution identity") from exc
        return {**public, "binary": binary, "environment": environment, "args": extra}


def validate_extra_args(args: list[str]) -> None:
    permitted_values = {"--model", "-m", "--effort", "--print-timeout"}
    i = 0
    while i < len(args):
        arg = args[i]
        key = arg.split("=", 1)[0]
        if key in permitted_values:
            value = arg.partition("=")[2] if "=" in arg else args[i + 1] if i + 1 < len(args) else ""
            if not value.strip():
                raise ConfigurationError("Unsupported provider argument or setting (value omitted)")
            i += 1 if "=" in arg else 2
            if i > len(args):
                raise ConfigurationError("Unsupported provider argument or setting (value omitted)")
            continue
        if arg in {"-c", "--config"} and i + 1 < len(args):
            setting, separator, value = args[i + 1].partition("=")
            if not separator or not value.strip().strip("\"'"):
                raise ConfigurationError("Unsupported provider argument or setting (value omitted)")
            if setting not in {"model", "model_provider", "model_reasoning_effort", "service_tier"}:
                raise ConfigurationError("Unsupported provider argument or setting (value omitted)")
            i += 2
            continue
        raise ConfigurationError("Unsupported provider argument or setting (value omitted)")
