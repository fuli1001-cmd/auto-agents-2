from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from .config import ConfigStore
from .errors import ConfigurationError, ProviderError
from .models import Usage, strict_schema
from .prompts import PROMPT_VERSION, PromptCompiler
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .transports import BUILTIN_TRANSPORTS, CLITransport
from .util import atomic_write, digest, identifier, scrub_text, write_json


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
        if self.kind == "codex":
            typ = event.get("type", "")
            if typ == "thread.started":
                self.session = event.get("thread_id", "")
            if typ == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    self.messages.append(item.get("text", ""))
            if typ == "turn.completed":
                u = event.get("usage", {})
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
            typ = event.get("event", "")
            self.session = event.get("conversation_id", self.session)
            if typ == "result":
                result = event.get("result", event)
                self.session = result.get("conversation_id", self.session)
                response = result.get("structured_output", result.get("response", ""))
                self.messages.append(json.dumps(response) if isinstance(response, dict) else str(response))
                u = result.get("usage", {})
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
        if eff["kind"] not in self.transports:
            raise ConfigurationError(f"No installed provider transport: {eff['kind']}")
        transport = self.transports[eff["kind"]]
        aid = identifier("attempt")
        root = self.store.root / "attempts" / run_id / aid
        out = root / "out"
        out.mkdir(parents=True)
        schema_file = root / "schema.json"
        write_json(schema_file, strict_schema(schema))
        prompt, family = self.compiler.compile(stage, context, eff["model"], eff["prompt_profile"])
        atomic_write(root / "prompt.txt", prompt)
        previous = self.store.get(run_id, f"session/{operation}/{eff['provider']}") if not fresh else None
        assets = []

        def images(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == "screenshot" and isinstance(v, str):
                        file = Path(v)
                        if file.is_file() and file.resolve().is_relative_to(self.store.root):
                            assets.append(file)
                    else:
                        images(v)
            elif isinstance(value, list):
                for v in value:
                    images(v)

        images(context)
        context_files = [Path(p) for p in context.get("context_files", [])
                         if Path(p).is_file() and Path(p).resolve().is_relative_to(self.store.root / "artifacts")]
        argv = transport.argv(eff, workspace, schema_file, out / "result.json", prompt, previous, assets, context_files)
        env = dict(eff["environment"])
        overlays = self._session_overlay(eff, run_id, operation)
        secret_values = [v for k, v in env.items() if any(s in k.upper() for s in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))]
        self.store.event(run_id, "agent.started", {"attempt": aid, "operation": operation, "stage": stage,
                         "provider": eff["provider"], "level": eff["level"], "model": eff["model"],
                         "effort": eff["effort"],
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
                    self.store.checkpoint(run_id, f"session/{operation}/{eff['provider']}", session)
                typ = event.get("type", event.get("event", ""))
                item = event.get("item") or {}
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
            process = await self.sandbox.spawn(argv, workspace, env, [out], [schema_file, root / "prompt.txt", *assets, *context_files], overlays)
            self.store.event(run_id, "agent.spawned", {"attempt": aid, "pid": process.pid})

            async def stdout():
                pending = bytearray()
                while chunk := await process.stdout.read(65536):
                    observe("stdout")
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
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
            response = (out / "result.json").read_text() if (out / "result.json").exists() else (decoder.messages[-1] if decoder.messages else "")
            try:
                result = schema.model_validate_json(response)
            except ValueError as exc:
                raise ProviderError("Invalid structured response: schema validation failed") from exc
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
            shutil.rmtree(root, ignore_errors=True)

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
