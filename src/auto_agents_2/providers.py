from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Protocol

from jsonschema import ValidationError, validate
from pydantic import BaseModel

from .artifacts import ArtifactStore
from .capabilities import ReadChallengeResult, challenge_files, confirm_read
from .config import ConfigStore
from .context_plan import CONTEXT_VERSION, ContextPlanner, PreparedContext, remap_references
from .errors import ConfigurationError, InvalidStructuredResponse, PostCommitError, ProviderError
from .evidence import invocation_identity
from .materials import bundle, prepare_prompt
from .models import Usage, strict_schema
from .progress import capture_output
from .prompts import PROMPT_VERSION, PromptCompiler
from .readview import VIEW_VERSION, create_view
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .transports import BUILTIN_TRANSPORTS, CLITransport
from .util import atomic_write, digest, identifier, scrub_text, write_json
from .workspace import file_digest

INPUT_LIMIT_DIAGNOSTIC = "provider 输入超过 CLI 长度上限"


def diagnostic_summary(text: str) -> str:
    """Classify diagnostics without logging native configuration or credentials."""
    lower = text.lower()
    for needles, label in (
        (("input_too_large", "input exceeds the maximum length"), INPUT_LIMIT_DIAGNOSTIC),
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
                    self.messages[:] = [message]
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
                self.messages[:] = [json.dumps(response) if isinstance(response, dict) else str(response)]
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

    def effective(self, stage, candidate):
        effective = self.config.resolve(stage, candidate)
        try:
            transport = self.transports[effective["kind"]]
            native = transport.native_home(effective["environment"])
            sources = {file.relative_to(native).as_posix(): file_digest(file)
                       for pattern in transport.auth_patterns for file in native.glob(pattern) if file.is_file()}
        except (OSError, KeyError) as exc:
            raise ConfigurationError("Cannot inspect provider native inputs") from exc
        return {**effective, "fingerprint": digest([effective["fingerprint"], "native-inputs-v1", sources])}

    def execution_identity(self, stage):
        identities = []
        for candidate in self.config.load().providers:
            try:
                identities.append((candidate, self.effective(stage, candidate)["fingerprint"]))
            except ConfigurationError:
                # Resolution errors remain the router's responsibility so fallback
                # providers can still run. No configuration contents are recorded.
                identities.append((candidate, "unavailable"))
        return digest(identities)

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        previous = self.store.get(run_id, f"provider-result/{operation}")
        try:
            return await self._invoke_candidates(stage, context, schema, workspace, run_id, operation, fresh)
        except Exception as exc:
            completed = self.store.get(run_id, f"provider-result/{operation}")
            if completed and completed != previous and not isinstance(exc, PostCommitError):
                raise PostCommitError("Provider result saved; resume reconciles completion without redispatch") from exc
            raise

    async def _invoke_candidates(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        cfg = self.config.load()
        candidates = [cfg.active_provider] + [n for n in cfg.providers if n != cfg.active_provider]
        retries = cfg.execution.infra_retries
        if stage == "decision":
            # One request is one provider call. Format correction is owned by the
            # decision service, not multiplied by transport retry/failover loops.
            candidates, retries = [cfg.active_provider], 0
        errors = []
        for candidate in candidates:
            for retry in range(retries + 1):
                key = f"infra/{operation}/{candidate}"
                state = self.store.get(run_id, key, {"failures": 0, "grant": 0, "pending": False})
                if state["pending"]:
                    state["failures"] += 1  # An interrupted request consumed its attempt.
                    state["pending"] = False
                    self.store.checkpoint(run_id, key, state)
                if state["failures"] >= retries + 1 + state["grant"]:
                    errors.append(f"{candidate}: persisted retry budget exhausted")
                    break
                previous = self.store.get(run_id, f"provider-result/{operation}")
                try:
                    effective = self.effective(stage, candidate)
                    state["pending"] = True
                    self.store.checkpoint(run_id, key, state)
                    result = await self._call(effective, stage, context, schema, workspace, run_id, operation, fresh)
                    state["pending"] = False
                    self.store.checkpoint(run_id, key, state)
                    if candidate != cfg.active_provider:
                        try:
                            old, new = self.config.activate(candidate, expected_revision=cfg.config_revision)
                            if old != new:
                                self.store.event(run_id, "provider.changed", {"from": old, "to": new, "reason": "failover"})
                        except (ConfigurationError, OSError):
                            from .progress import safe_event
                            safe_event(self.store, run_id, "provider.preference_pending", {"provider": candidate})
                    return result
                except ConfigurationError as exc:
                    completed = self.store.get(run_id, f"provider-result/{operation}")
                    if completed and completed != previous:
                        raise PostCommitError("Provider result saved; reconcile completion without failover") from exc
                    state["pending"] = False
                    self.store.checkpoint(run_id, key, state)
                    errors.append(f"{candidate}: {exc}")
                    break
                except ProviderError as exc:
                    completed = self.store.get(run_id, f"provider-result/{operation}")
                    if completed and completed != previous:
                        raise PostCommitError("Provider result saved; reconcile completion without failover") from exc
                    state.update(pending=False, failures=state["failures"] + 1)
                    self.store.checkpoint(run_id, key, state)
                    errors.append(f"{candidate}: {exc}")
                    if stage == "decision" or stage == "recovery_diagnosis" and isinstance(exc, InvalidStructuredResponse):
                        raise
                    if not exc.retryable or retry == retries:
                        break
                    self.store.event(run_id, "agent.retrying", {"operation": operation, "provider": candidate,
                                     "retry": retry + 1, "delay": min(2 ** retry, 15)})
                    await asyncio.sleep(min(2 ** retry, 15))
        raise ProviderError("; ".join(errors)[-3000:])

    async def _call(self, eff, stage, context, schema, workspace, run_id, operation, fresh):
        previous = self.store.get(run_id, f"provider-result/{operation}")
        try:
            with ArtifactStore(self.store.root).reading(context, run_id):
                return await self._call_with_materials(eff, stage, context, schema, workspace, run_id, operation, fresh)
        except BaseException as exc:
            # Re-read the transaction receipt: a commit may succeed before its caller
            # observes an I/O error. Such a result must never enter provider failover.
            completed = self.store.get(run_id, f"provider-result/{operation}")
            if completed and completed != previous:
                if isinstance(exc, asyncio.CancelledError):
                    exc.add_note("Provider result committed; resume reconciles cleanup without redispatch")
                    raise
                raise PostCommitError("Provider result saved; resume must reconcile cleanup without redispatch") from exc
            if isinstance(exc, (OSError, ValueError)):
                raise ProviderError("Required evidence material is unavailable or invalid") from exc
            raise

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
            prepared = self._prepare_context(stage, context, workspace, run_id, operation, input_identity)
            context = prepared.payload
            workspace = create_view(workspace, root / "workspace", self.config.load().sensitive_paths)
            read_status = "uncertified"
            # Self-contained input always names the actually mounted public snapshot.
            context = json.loads(json.dumps(context))
            referenced = set(context.get("context_files", []))
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
                            artifact_store.bytes(value)
                        except (OSError, ValueError) as exc:
                            raise ProviderError("Referenced evidence artifact is unavailable") from exc
                        value["location"] = str(path)
                        referenced.add(str(path))
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
            prompt_file = root / "prompt.txt"
            atomic_write(prompt_file, prompt)
            delivery = prepare_prompt(prompt, prompt_file, getattr(transport, "max_prompt_characters", None))
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
            assets = list(dict.fromkeys(assets))
            context_files = [Path(p) for p in context.get("context_files", [])]
            if any(not p.is_file() or not p.resolve().is_relative_to(self.store.root / "artifacts") for p in context_files):
                raise ProviderError("Required context attachment cannot be delivered")
            image_read = eff["kind"] == "antigravity" and bool(assets)
            source_read = isinstance(context.get("project"), dict) and bool(context["project"].get("read_on_demand"))
            if stage == "material_probe" and delivery.file:
                raise ProviderError("Material read probe exceeds the native input limit")
            if stage != "material_probe" and (context_files or image_read or source_read or delivery.file):
                await self._ensure_material_read(eff, workspace, run_id, image_read)
                read_status = "observed_read"
            material = bundle(prompt, strict_schema(schema), context_files, assets,
                              {"provider": eff["kind"], "profile": eff["fingerprint"], "view": VIEW_VERSION,
                               "binary": file_digest(Path(eff["binary"])), "native_read_test": read_status},
                              delivery=delivery)
            _, _, after_context = prompt.partition("<context>\n")
            encoded_context, _, _ = after_context.rpartition("\n</context>")
            material.update(context_version=prepared.version, purpose=prepared.purpose,
                selections=prepared.selections,
                context_characters=len(encoded_context),
                instructions_characters=len(prompt) - len(encoded_context),
                on_demand_bytes=sum(p.stat().st_size for p in context_files),
                over_target_reason="Mandatory instructions, constraints or unclassified extension" if material["over_target"] else "",
                usage_scope="Initial prompt/schema characters and available attachment bytes; actual native tokens are recorded separately")
            self.store.checkpoint(run_id, "materials/" + operation, material)
            argv = transport.argv(eff, workspace, schema_file, out / "result.json", delivery.text, previous,
                                  assets, context_files)
            env = dict(eff["environment"])
            overlays = self._session_overlay(eff, run_id, operation + VIEW_VERSION + PROMPT_VERSION + eff["fingerprint"] + digest(self.config.load().sensitive_paths))
            secret_values = [v for k, v in env.items() if any(s in k.upper() for s in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))]
            self.store.event(run_id, "agent.started", {"attempt": aid, "operation": operation, "stage": stage,
                             "provider": eff["provider"], "level": eff["level"], "model": eff["model"],
                             "effort": eff["effort"],
                             "repair_kind": ("dependencies" if context.get("feedback", {}).get("environment_error") else
                                             "checks" if any(c.get("exit_code") for c in context.get("feedback", {}).get("checks", [])) else ""),
                             "task_title": context["task"].get("title", "") if isinstance(context.get("task"), dict) else "",
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
                        process.stdin.write(transport.encode_prompt(delivery.text))
                        await process.stdin.drain()
                    process.stdin.close()
                    await asyncio.gather(*readers, process.wait())
                finally:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                if process.returncode != 0 or decoder.error or not decoder.finished:
                    message = decoder.error or "".join(errors)
                    diagnostic = diagnostic_summary(message)
                    raise ProviderError(diagnostic + f"；未取得完整结果（退出码 {process.returncode}）",
                        retryable=diagnostic != INPUT_LIMIT_DIAGNOSTIC and any(t in message.lower() for t in
                            ("timeout", "timed out", "429", "503", "connection", "rate limit", "capacity", "overloaded")))
                from .safe_files import read_regular
                try:
                    response = read_regular(out / "result.json", 32 * 1024 * 1024).decode("utf-8")
                except FileNotFoundError:
                    response = decoder.messages[-1] if decoder.messages else ""
                except (OSError, ValueError) as exc:
                    raise ProviderError("Unsafe or invalid structured response file") from exc
                try:
                    validate(json.loads(response), strict_schema(schema))
                    result = schema.model_validate(remap_references(json.loads(response), prepared.reference_map))
                except (ValueError, ValidationError) as exc:
                    raise InvalidStructuredResponse("Invalid structured response: schema validation failed") from exc
                artifact = ArtifactStore(self.store.root).put(result.model_dump(mode="json"), stage)
                self.store.checkpoints(run_id, {
                    f"provider-result/{operation}": {"attempt": aid, "artifact": artifact, "usage": decoder.usage.model_dump()},
                    f"invocation/{operation}": {"attempt": aid, "identity": input_identity, "result": result.model_dump(mode="json"), "artifact": artifact,
                                                "cleanup_record": str(process.record)},
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
                from .cleanup import finalize_async
                await finalize_async(
                    *([lambda: self.sandbox.stop(process)] if process else []),
                    lambda: self.store.add_usage(run_id, eff["provider"], aid, "invocation", decoder.usage.model_dump()),
                    lambda: self.store.event(run_id, "agent.finished", {"attempt": aid, "complete": decoder.finished,
                        "outcome": outcome, "last_output": last_output,
                        "diagnostic": diagnostic_summary(decoder.error or "".join(errors)) if outcome == "error" else ""}))
                # Candidate responses have been consumed; persistent sessions and usage survive.
        except OSError as exc:
            raise ProviderError("Provider material preparation failed") from exc
        finally:
            if resource_id is not None:
                from .cleanup import finalize_async
                await finalize_async(lambda: resource_manager.release(resource_id), resource_manager.collect)

    def _prepare_context(self, stage, context, workspace, run_id, operation, identity):
        """Freeze presentation independently of semantic invocation/authorization identity."""
        artifacts = ArtifactStore(self.store.root)
        key = "presentation/" + operation + "/" + identity
        saved = self.store.get(run_id, key)
        if saved:
            record = artifacts.json(saved["artifact"])
            if record.get("version") != CONTEXT_VERSION:
                raise ProviderError("Unsupported frozen input presentation; preserve the pending request")
            return PreparedContext(**record)
        prepared = ContextPlanner(artifacts, self.config.load().sensitive_paths).prepare(stage, context, workspace, operation, run_id=run_id)
        ref = artifacts.put(prepared.record(), "prepared_context")
        self.store.checkpoint(run_id, key, {"identity": identity, "artifact": ref})
        # First dispatch and resumed dispatch use the same serialized key order.
        return PreparedContext(**artifacts.json(ref))

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
        from .safe_files import directory_fd, regular_exists, write_regular
        with directory_fd(mirror, create=True):
            pass
        ResourceManager(self.store, self.config.load().cleanup).register(mirror, "session", run_id)
        # Remember the native source digest independently of provider-written
        # mirror refreshes. A changed native source replaces an old credential.
        source_key = "native-auth/" + digest(str(mirror))
        sources = self.store.get(run_id, source_key, {})
        updated = {}
        # Copy only configuration/auth inputs, never all historical sessions.
        for pattern in transport.auth_patterns:
            for file in native.glob(pattern):
                if file.is_file():
                    dest = mirror / file.relative_to(native)
                    relative = file.relative_to(native).as_posix()
                    raw = file.read_bytes()
                    source_hash = digest(raw)
                    with directory_fd(dest.parent, create=True) as parent:
                        exists = regular_exists(parent, dest.name)
                        if (not exists or relative in sources and sources[relative] != source_hash
                                or relative not in sources and file.name != "auth.json"):
                            write_regular(parent, dest.name, raw)
                    updated[relative] = source_hash
        for relative in sources.keys() - updated.keys():
            dest = mirror / relative
            try:
                with directory_fd(dest.parent) as parent:
                    if regular_exists(parent, dest.name):
                        os.unlink(dest.name, dir_fd=parent)
                        os.fsync(parent)
            except FileNotFoundError:
                pass
        self.store.checkpoint(run_id, source_key, updated)
        return [(mirror, native)] if native.exists() else []
