from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Protocol

from jsonschema import ValidationError, validate
from pydantic import BaseModel

from .artifacts import ArtifactStore
from .capabilities import ReadChallengeResult, challenge_files, confirm_read
from .config import ConfigStore
from .diagnostics import references
from .errors import ConfigurationError, ProviderError
from .evidence import invocation_identity
from .materials import bundle
from .models import Usage, strict_schema
from .progress import capture_output
from .prompts import PROMPT_VERSION, PromptCompiler
from .readview import VIEW_VERSION, create_view, sensitive_values
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .transports import BUILTIN_TRANSPORTS, CLITransport
from .util import atomic_write, digest, identifier, scrub_text, write_json
from .workspace import file_digest


def diagnostic_summary(text: str) -> str:
    """Classify diagnostics without logging native configuration or credentials."""
    lower = text.lower()
    for needles, label in (
        (("429", "rate limit", "capacity", "overloaded"), "provider 限流或容量不足"),
        (("retry", "reconnect"), "provider 正在重试或重连"),
        (("connection", "disconnect", "503"), "provider 连接异常"),
        (("401", "403", "unauthorized", "authentication"), "provider 认证或权限异常"),
        (("config", "argument", "profile"), "provider 配置或参数异常"),
    ):
        if any(word in lower for word in needles):
            return label
    return "provider 诊断输出"


class Agent(Protocol):
    async def invoke(self, stage: str, context: dict, schema: type[BaseModel],
                     workspace: Path, run_id: str, operation: str, fresh: bool = False) -> BaseModel: ...


class EventDecoder:
    def __init__(self, kind: str):
        self.kind = kind
        self.session = ""
        self.messages: list[str] = []
        self.usage = Usage(complete=False)
        self.finished = False
        self.error = ""
        self.last_counter = 0

    def feed(self, event: dict):
        try:
            self._feed(event)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("Malformed provider event structure") from exc

    def _feed(self, event: dict):
        def mapping(value):
            if not isinstance(value, dict):
                raise ProviderError("Malformed provider event structure")
            return value
        def string(value):
            if not isinstance(value, str):
                raise ProviderError("Malformed provider event string")
            return value
        event = mapping(event)
        if self.kind == "codex":
            typ = string(event.get("type", ""))
            if typ == "thread.started":
                self.session = string(event.get("thread_id", ""))
            if typ in {"item.started", "item.completed"}:
                item = mapping(event.get("item", {}))
                item_type = string(item.get("type", ""))
                if typ == "item.completed" and item_type == "agent_message":
                    message = item.get("text", "")
                    if not isinstance(message, str):
                        raise ProviderError("Malformed provider message")
                    self.messages.append(message)
            if typ == "turn.completed":
                u = mapping(event.get("usage", {}))
                incoming, outgoing = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
                self.usage = Usage(input_tokens=self.usage.input_tokens + incoming,
                                   output_tokens=self.usage.output_tokens + outgoing,
                                   cached_tokens=self.usage.cached_tokens + int(u.get("cached_input_tokens", 0)),
                                   reasoning_tokens=self.usage.reasoning_tokens + int(u.get("reasoning_output_tokens", 0)),
                                   total_tokens=self.usage.total_tokens + incoming + outgoing, complete=bool(u))
                self.finished = True
            if typ in {"turn.failed", "error"}:
                self.error = str(event.get("error", event.get("message", "provider error")))
        else:
            typ = string(event.get("event", ""))
            self.session = string(event.get("conversation_id", self.session))
            if typ == "result":
                result = mapping(event.get("result", event))
                self.session = string(result.get("conversation_id", self.session))
                response = result.get("structured_output", result.get("response", ""))
                self.messages.append(json.dumps(response) if isinstance(response, dict) else str(response))
                u = mapping(result.get("usage", {}))
                # Result counters are cumulative within this invocation's stream.
                self.usage = Usage(input_tokens=int(u.get("input_tokens", 0)),
                                   output_tokens=int(u.get("output_tokens", 0)),
                                   cached_tokens=int(u.get("cache_read_tokens", 0)),
                                   reasoning_tokens=int(u.get("thinking_tokens", 0)),
                                   total_tokens=int(u.get("total_tokens", u.get("input_tokens", 0) + u.get("output_tokens", 0))),
                                   complete=bool(u))
                self.finished = result.get("status") == "SUCCESS"
                if not self.finished or result.get("denied_actions"):
                    self.error = str(result.get("error") or result.get("denied_actions") or result.get("status"))


class ProviderRouter:
    def __init__(self, config: ConfigStore, store: Store, sandbox: SandboxRunner,
                 transports: dict[str, CLITransport] | None = None, decoders: dict | None = None):
        self.config, self.store, self.sandbox = config, store, sandbox
        self.compiler = PromptCompiler()
        self.transports = {**BUILTIN_TRANSPORTS, **(transports or {})}
        self.decoders = decoders or {}

    def grant_retry(self, run_id: str, operation: str):
        cfg = self.config.load()
        for candidate in cfg.providers:
            key = f"infra/{operation}/{candidate}"
            state = self.store.get(run_id, key, {"failures": 0, "grant": 0, "pending": False})
            state["grant"] += cfg.execution.infra_retries + 1
            self.store.checkpoint(run_id, key, state)

    def execution_identity(self, stage):
        identities = []
        for candidate in self.config.load().providers:
            try:
                identities.append((candidate, self.config.resolve(stage, candidate)["fingerprint"]))
            except ConfigurationError:
                # Resolution errors remain the router's responsibility so fallback
                # providers can still run. No configuration contents are recorded.
                identities.append((candidate, "unavailable"))
        return digest(identities)

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        cfg = self.config.load()
        candidates = [cfg.active_provider] + [n for n in cfg.providers if n != cfg.active_provider]
        errors = []
        for candidate in candidates:
            for retry in range(cfg.execution.infra_retries + 1):
                key = f"infra/{operation}/{candidate}"
                state = self.store.get(run_id, key, {"failures": 0, "grant": 0, "pending": False})
                if state["pending"]:
                    state["failures"] += 1  # An interrupted request consumed its attempt.
                    state["pending"] = False
                    self.store.checkpoint(run_id, key, state)
                if state["failures"] >= cfg.execution.infra_retries + 1 + state["grant"]:
                    errors.append(f"{candidate}: persisted retry budget exhausted")
                    break
                try:
                    effective = self.config.resolve(stage, candidate)
                    state["pending"] = True
                    self.store.checkpoint(run_id, key, state)
                    result = await self._call(effective, stage, context, schema, workspace, run_id, operation, fresh)
                    state["pending"] = False
                    self.store.checkpoint(run_id, key, state)
                    if candidate != cfg.active_provider:
                        old, new = self.config.activate(candidate, expected_revision=cfg.config_revision)
                        if old != new:
                            self.store.event(run_id, "provider.changed", {"from": old, "to": new, "reason": "failover"})
                    return result
                except ConfigurationError as exc:
                    state["pending"] = False
                    self.store.checkpoint(run_id, key, state)
                    errors.append(f"{candidate}: {exc}")
                    break
                except ProviderError as exc:
                    state.update(pending=False, failures=state["failures"] + 1)
                    self.store.checkpoint(run_id, key, state)
                    errors.append(f"{candidate}: {exc}")
                    if not exc.retryable or retry == cfg.execution.infra_retries:
                        break
                    self.store.event(run_id, "agent.retrying", {"operation": operation, "provider": candidate,
                                     "retry": retry + 1, "delay": min(2 ** retry, 15)})
                    await asyncio.sleep(min(2 ** retry, 15))
        raise ProviderError("; ".join(errors)[-3000:])

    async def _call(self, eff, stage, context, schema, workspace, run_id, operation, fresh):
        try:
            with ArtifactStore(self.store.root).reading(context, run_id):
                return await self._call_with_materials(eff, stage, context, schema, workspace, run_id, operation, fresh)
        except (OSError, ValueError) as exc:
            raise ProviderError("Required evidence material is unavailable or invalid") from exc

    async def _call_with_materials(self, eff, stage, context, schema, workspace, run_id, operation, fresh):
        if eff["kind"] not in self.transports:
            raise ConfigurationError(f"No installed provider transport: {eff['kind']}")
        transport = self.transports[eff["kind"]]
        input_identity = invocation_identity(stage, context, schema, workspace, self.config.load(),
                                            execution=self.execution_identity(stage))
        aid = identifier("attempt")
        root = self.store.root / "attempts" / run_id / aid
        out = root / "out"
        resource_manager = ResourceManager(self.store, self.config.load().cleanup)
        resource_id = None
        try:
            out.mkdir(parents=True)
            resource_id = resource_manager.register(root, "temporary", run_id)
            schema_file = root / "schema.json"
            write_json(schema_file, strict_schema(schema))
            original_workspace = workspace
            workspace = create_view(workspace, root / "workspace", self.config.load().sensitive_paths)
            read_status = "uncertified"
            # Self-contained input always names the actually mounted public snapshot.
            context = json.loads(json.dumps(context))
            referenced = set(context.get("context_files", []))
            visited = set()
            released = []
            artifact_store = ArtifactStore(self.store.root)
            def attach(value):
                if isinstance(value, dict):
                    sha = value.get("hash")
                    if isinstance(sha, str) and len(sha) == 64 and "kind" in value and "bytes" in value:
                        path = artifact_store.root / sha[:2] / sha
                        value.pop("lease_id", None)
                        if artifact_store.released(value):
                            value.pop("location", None)
                            value["availability"] = "released"
                            value["reason"] = "Diagnostic body released after its consumers finished; rerun the check if needed"
                            released.append(dict(value))
                            return
                        try:
                            raw = artifact_store.bytes(value)
                        except (OSError, ValueError) as exc:
                            raise ProviderError("Referenced evidence artifact is unavailable") from exc
                        value["location"] = str(path)
                        referenced.add(str(path))
                        if sha not in visited:
                            visited.add(sha)
                            # Exposure follows controller-registered edges, never arbitrary JSON in raw documents.
                            with self.store.connect() as db:
                                present = db.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_edges'").fetchone()
                                children = [row[0] for row in db.execute("SELECT child FROM artifact_edges WHERE parent=?", (sha,))] if present else []
                            nested = list(references(json.loads(raw))) if children else []
                            for child in children:
                                entries = [ref for ref in nested if ref["hash"] == child]
                                if entries:
                                    for ref in entries:
                                        attach(ref)
                                else:
                                    child_path = artifact_store.root / child[:2] / child
                                    if not child_path.is_file():
                                        raise ProviderError("Referenced evidence dependency is unavailable")
                                    attach({"hash": child, "kind": "evidence_dependency", "bytes": child_path.stat().st_size})
                    for child in list(value.values()):
                        attach(child)
                elif isinstance(value, list):
                    for child in value:
                        attach(child)
            attach(context)
            context["context_files"] = sorted(referenced)
            if released:
                context["released_diagnostics"] = released
            def locate(value):
                if isinstance(value, dict):
                    if value.get("workspace") == str(original_workspace):
                        value["workspace"] = str(workspace)
                    for child in value.values():
                        locate(child)
                elif isinstance(value, list):
                    for child in value:
                        locate(child)
            locate(context)
            prompt, family = self.compiler.compile(stage, context, eff["model"], eff["prompt_profile"])
            confidential = sensitive_values(original_workspace, self.config.load().sensitive_paths)
            # Preserve exact editable text: never silently rewrite model-visible file content.
            if any(value in prompt for value in confidential):
                raise ProviderError("Required material includes a protected value; provide a safe execution contract before model delivery")
            atomic_write(root / "prompt.txt", prompt)
            session_key = f"session/{operation}/{eff['provider']}/{VIEW_VERSION}/{PROMPT_VERSION}/{eff['fingerprint']}/{digest(self.config.load().sensitive_paths)}"
            previous = self.store.get(run_id, session_key) if not fresh else None
            assets = []

            def images(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if k == "screenshot" and isinstance(v, str):
                            file = Path(v)
                            if not file.is_file() or not file.resolve().is_relative_to(self.store.root):
                                raise ProviderError("Required screenshot cannot be delivered")
                            assets.append(file)
                        else:
                            images(v)
                elif isinstance(value, list):
                    for v in value:
                        images(v)

            images(context)
            context_files = [Path(p) for p in context.get("context_files", [])]
            if any(not p.is_file() or not p.resolve().is_relative_to(self.store.root / "artifacts") for p in context_files):
                raise ProviderError("Required context attachment cannot be delivered")
            for attachment in context_files:
                body = attachment.read_bytes()
                if any(value.encode() in body for value in confidential):
                    raise ProviderError("Required attachment includes protected data and cannot be delivered")
            image_read = eff["kind"] == "antigravity" and bool(assets)
            if stage != "material_probe" and (context_files or image_read):
                await self._ensure_material_read(eff, workspace, run_id, image_read)
                read_status = "observed_read"
            material = bundle(prompt, strict_schema(schema), context_files, assets,
                              {"provider": eff["kind"], "profile": eff["fingerprint"], "view": VIEW_VERSION,
                               "binary": file_digest(Path(eff["binary"])), "native_read_test": read_status})
            self.store.checkpoint(run_id, "materials/" + operation, material)
            argv = transport.argv(eff, workspace, schema_file, out / "result.json", prompt, previous, assets, context_files)
            env = dict(eff["environment"])
            overlays = self._session_overlay(eff, run_id, operation + VIEW_VERSION + PROMPT_VERSION + eff["fingerprint"] + digest(self.config.load().sensitive_paths))
            secret_values = [v for k, v in env.items() if any(s in k.upper() for s in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))]
            self.store.event(run_id, "agent.started", {"attempt": aid, "operation": operation, "stage": stage,
                             "provider": eff["provider"], "level": eff["level"], "model": eff["model"],
                             "effort": eff["effort"],
                             "repair_kind": ("dependencies" if context.get("feedback", {}).get("environment_error") else
                                             "checks" if any(c.get("exit_code") for c in context.get("feedback", {}).get("checks", [])) else ""),
                             "task_title": context.get("task", {}).get("title", ""),
                             "config_hash": eff["fingerprint"], "prompt_version": PROMPT_VERSION, "prompt_family": family})
            decoder, errors = self.decoders.get(eff["kind"], EventDecoder)(eff["kind"]), []
            process = None
            outcome = "interrupted"
            last_output = None
            published = 0.0
            session = previous

            def observe(stream, detail="", force=False):
                nonlocal last_output, published
                last_output = time.time()
                if force or time.monotonic() - published >= 1:
                    self.store.event(run_id, "agent.output", {"attempt": aid, "last_output": last_output,
                                     "stream": stream, "detail": detail})
                    published = time.monotonic()

            def decode(line):
                nonlocal session
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Expected object")
                    decoder.feed(event)
                    if decoder.session and decoder.session != session:
                        session = decoder.session
                        self.store.checkpoint(run_id, session_key, session)
                    typ = event.get("type", event.get("event", ""))
                    item = event.get("item", {})
                    if (not isinstance(typ, str) or typ in {"item.started", "item.completed"}
                            and (not isinstance(item, dict) or not isinstance(item.get("type", ""), str))):
                        raise ProviderError("Malformed provider progress event structure")
                    detail = {"thread.started": "会话已建立", "turn.started": "等待模型响应",
                              "turn.completed": "响应已返回，校验结果", "turn.failed": "调用失败",
                              "error": "provider 报错"}.get(typ, "")
                    if typ in {"item.started", "item.completed"}:
                        what = {"command_execution": "工具", "mcp_tool_call": "工具", "web_search": "搜索",
                                "agent_message": "响应内容", "reasoning": "模型事件"}.get(item.get("type"), "执行事件")
                        detail = what + ("执行中" if typ == "item.started" else "已返回")
                    if detail:
                        observe("stdout", detail, force=True)
                except (ValueError, TypeError):
                    errors.append(scrub_text(line.decode(errors="replace")[:2000], secret_values))
                    del errors[:-100]

            try:
                process = await self.sandbox.spawn(argv, workspace, env, [out], [schema_file, root / "prompt.txt", *assets, *context_files], overlays,
                                                   hidden=[self.config.project, original_workspace])
                self.store.event(run_id, "agent.spawned", {"attempt": aid, "pid": process.pid})

                async def stdout():
                    pending = bytearray()
                    while chunk := await process.stdout.read(65536):
                        observe("stdout")
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if len(line) > 8 * 1024 * 1024:
                                raise ProviderError("Provider event exceeds 8 MiB")
                            decode(line)
                        if len(pending) > 8 * 1024 * 1024:
                            raise ProviderError("Provider event exceeds 8 MiB")
                    if pending:
                        decode(pending)

                async def stderr():
                    while chunk := await process.stderr.read(8192):
                        observe("stderr", diagnostic_summary(chunk.decode(errors="replace")))
                        errors.append(scrub_text(chunk.decode(errors="replace")[:2000], secret_values))
                        if len(errors) > 100:
                            del errors[:50]

                readers = [asyncio.create_task(stdout()), asyncio.create_task(stderr())]
                try:
                    if transport.stdin_prompt:
                        process.stdin.write(transport.encode_prompt(prompt))
                        await process.stdin.drain()
                    process.stdin.close()
                    await asyncio.gather(*readers, process.wait())
                finally:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                if process.returncode != 0 or decoder.error or not decoder.finished:
                    message = decoder.error or "".join(errors)[-2000:]
                    raise ProviderError(diagnostic_summary(message) + f"；未取得完整结果（退出码 {process.returncode}）",
                        retryable=any(t in message.lower() for t in ("timeout", "timed out", "429", "503", "connection", "rate limit", "capacity", "overloaded")))
                from .safe_files import read_regular
                try:
                    response = read_regular(out / "result.json", 32 * 1024 * 1024).decode("utf-8")
                except FileNotFoundError:
                    response = decoder.messages[-1] if decoder.messages else ""
                except (OSError, ValueError) as exc:
                    raise ProviderError("Unsafe or invalid structured response file") from exc
                try:
                    validate(json.loads(response), strict_schema(schema))
                    result = schema.model_validate_json(response)
                except (ValueError, ValidationError) as exc:
                    raise ProviderError("Invalid structured response: schema validation failed") from exc
                artifact = ArtifactStore(self.store.root).put(result.model_dump(mode="json"), stage)
                self.store.checkpoints(run_id, {
                    f"provider-result/{operation}": {"attempt": aid, "artifact": artifact, "usage": decoder.usage.model_dump()},
                    f"invocation/{operation}": {"identity": input_identity, "result": result.model_dump(mode="json"), "artifact": artifact},
                }, usage=(eff["provider"], aid, decoder.usage.model_dump()))
                outcome = "success"
                return result
            except OSError as exc:
                outcome = "error"
                raise ProviderError(str(exc)) from exc
            except Exception:
                outcome = "error"
                raise
            finally:
                if process:
                    await self.sandbox.stop(process)
                self.store.add_usage(run_id, eff["provider"], aid, "invocation", decoder.usage.model_dump())
                self.store.event(run_id, "agent.finished", {"attempt": aid, "complete": decoder.finished,
                                                           "outcome": outcome, "last_output": last_output,
                                                           "diagnostic": diagnostic_summary(decoder.error or "".join(errors))
                                                           if outcome == "error" else ""})
                # Candidate responses have been consumed; persistent sessions and usage survive.
        except OSError as exc:
            raise ProviderError("Provider material preparation failed") from exc
        finally:
            shutil.rmtree(root, ignore_errors=True)
            if resource_id is not None:
                resource_manager.release(resource_id)

    @staticmethod
    def _has_images(value):
        if isinstance(value, dict):
            return bool(value.get("screenshot")) or any(ProviderRouter._has_images(v) for v in value.values())
        return isinstance(value, list) and any(ProviderRouter._has_images(v) for v in value)

    async def _ensure_material_read(self, eff, workspace, run_id, image_read):
        process = await self.sandbox.spawn([eff["binary"], "--version"], workspace, eff["environment"], [],
                                           hidden=[self.config.project])
        try:
            async with asyncio.timeout(30):
                version, _ = await capture_output(process, machine_limit=65536)
            if process.returncode:
                raise ProviderError("Cannot establish native CLI material capability identity")
        except TimeoutError as exc:
            raise ProviderError("Provider capability probe timed out", retryable=True) from exc
        finally:
            await self.sandbox.stop(process)
        identity = digest([eff["fingerprint"], file_digest(Path(eff["binary"])), digest(version),
                           VIEW_VERSION, PROMPT_VERSION, self.config.load().sensitive_paths, image_read])
        key = "material-capability/" + identity
        if self.store.get(run_id, key, {}).get("status") == "observed_read":
            return
        resources = ResourceManager(self.store, self.config.load().cleanup)
        with resources.temporary("read-challenge") as directory:
            context, expected = await challenge_files(ArtifactStore(self.store.root), directory, image_read)
            response = await self._call(eff, "material_probe", context, ReadChallengeResult,
                                        workspace, run_id, "material-probe/" + identity, True)
            proof = confirm_read(response, expected)
            self.store.checkpoint(run_id, key, {**proof, "capability": identity,
                                              "scope": "text and PNG read; not task correctness" if image_read else "text read; not task correctness"})

    def _session_overlay(self, eff, run_id, operation):
        env = eff["environment"]
        transport = self.transports[eff["kind"]]
        native = transport.native_home(env)
        mirror = self.store.root / "sessions" / run_id / digest(eff["provider"])[:16] / digest(operation)[:20]
        mirror.mkdir(parents=True, exist_ok=True)
        ResourceManager(self.store, self.config.load().cleanup).register(mirror, "session", run_id)
        # Copy only configuration/auth inputs, never all historical sessions.
        for pattern in transport.auth_patterns:
            for file in native.glob(pattern):
                if file.is_file():
                    dest = mirror / file.relative_to(native)
                    if file.name != "auth.json" or not dest.exists():
                        atomic_write(dest, file.read_bytes())
        return [(mirror, native)] if native.exists() else []
