"""Content-free native provider observations, never recovery or retry authority."""
from __future__ import annotations

import hashlib
import json
import re

from .util import digest

# Exact identifiers only. Unknown strings may contain prompts, arguments or secrets.
TOOLS = frozenset({
    "read_file", "view_file", "list_dir", "grep_search", "find_by_name", "code_search",
    "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content",
    "run_command", "send_command_input", "command_status", "read_url_content",
    "search_web", "browser_subagent",
})
CODES = frozenset({
    "SUCCESS", "ERROR", "FAILED", "FAILURE", "CANCELLED", "INTERRUPTED", "TIMEOUT",
    "DENIED", "PERMISSION_DENIED", "ACCESS_DENIED", "UNAUTHORIZED", "UNAUTHENTICATED",
    "POLICY_DENIED", "POLICY_VIOLATION", "SAFETY", "SAFETY_POLICY", "SANDBOX_DENIED",
    "USER_DENIED", "NOT_AUTHORIZED", "RESOURCE_EXHAUSTED", "RATE_LIMITED",
    "INTERNAL", "UNAVAILABLE", "INVALID_ARGUMENT", "NOT_FOUND", "INPUT_TOO_LARGE",
    "CONTEXT_WINDOW_EXCEEDED", "MODEL_NOT_FOUND", "MAX_OUTPUT_TOKENS",
})
TARGET_FIELDS = (
    "TargetFile", "AbsolutePath", "DirectoryPath", "SearchDirectory", "Cwd", "Url",
    "path", "file_path", "directory", "cwd", "url",
)
LIMIT = 16


def opaque(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {"type": type(value).__name__, "bytes": len(encoded), "sha256": digest(encoded)}


def code(value, allowed=CODES):
    return {"value": value if type(value) is str and value in allowed else "unknown",
            **opaque(value)}


def denied_action(value):
    # AGY DeniedAction has tool_name, Struct args, and free-text reason.
    # Legacy string entries and unknown extensions remain explicitly opaque.
    result = {"fingerprint": opaque(value), "tool": code(None, TOOLS), "reason": code(None)}
    if type(value) is str:
        result["tool"] = code(value, TOOLS)
    elif type(value) is dict:
        result["tool"] = code(value.get("tool_name", value.get("toolName")), TOOLS)
        result["reason"] = code(value.get("reason_code", value.get("reason")))
        args = value.get("args")
        result["arguments"] = opaque(args)
        targets = []
        if type(args) is dict:
            for field in TARGET_FIELDS:
                target = args.get(field)
                if type(target) is not str:
                    continue
                # No hostname, URL userinfo/query, basename or path segment is emitted.
                kind = ("https_url" if target.startswith("https://") else
                        "http_url" if target.startswith("http://") else
                        "file_url" if target.startswith("file://") else
                        "absolute_path" if target.startswith("/") else
                        "windows_path" if re.match(r"^[A-Za-z]:[\\/]", target) else "opaque")
                targets.append({"field": field, "kind": kind, **opaque(target)})
        result["targets"] = targets
    return result


def native_error(value):
    result = {"fingerprint": opaque(value), "code": code(None)}
    if type(value) is dict:
        result["code"] = code(value.get("code", value.get("type")))
    elif type(value) is str:
        result["code"] = code(value)
    return result


class ProviderFailureFacts:
    """Observe protocol facts independently of the decoder's acceptance decisions."""

    def __init__(self, kind):
        self.kind = kind
        self.result_seen = False
        self.projection_complete = True
        self.terminal_count = 0
        self.last_terminal = None
        self.failures = []
        self.failure_count = 0
        self.errors = []
        self.error_count = 0
        self.sources = set()
        self.streams = {}

    def stream(self, source, chunk):
        state = self.streams.setdefault(source, {"bytes": 0, "chunks": 0, "digest": hashlib.sha256()})
        state["bytes"] += len(chunk)
        state["chunks"] += 1
        state["digest"].update(chunk)
        self.sources.add(source)

    def _error(self, source, value):
        self.sources.add(source)
        self.error_count += 1
        if len(self.errors) < LIMIT:
            self.errors.append({"source": source, **native_error(value)})

    def event(self, event):
        if self.kind == "antigravity" and event.get("event") == "result":
            self.result_seen = True
            result = event.get("result", event)
            if type(result) is not dict:
                self._error("result.structure", result)
                return
            denied = result.get("denied_actions")
            terminal = {"event": "result", "status": code(result.get("status")),
                        "denied_actions_present": bool(denied)}
            if result.get("error"):
                terminal["error"] = native_error(result["error"])
                self.sources.add("result.error")
            if denied:
                self.sources.add("result.denied_actions")
                rows = denied if type(denied) is list else [denied]
                terminal.update(denied_action_count=len(rows),
                                denied_actions=[denied_action(row) for row in rows[:LIMIT]],
                                denied_actions_truncated=len(rows) > LIMIT)
            failed = result.get("status") != "SUCCESS" or bool(denied)
            if result.get("status") != "SUCCESS":
                self.sources.add("result.status")
            self._terminal(terminal, failed)
        elif self.kind == "codex":
            typ = event.get("type")
            if type(typ) is not str:
                return
            if typ in {"turn.completed", "turn.failed"}:
                self.result_seen = True
                terminal = {"event": typ}
                if typ == "turn.failed":
                    self.sources.add("turn.failed")
                    terminal["error"] = native_error(event.get("error"))
                self._terminal(terminal, typ == "turn.failed")
            elif typ == "error":
                self._error("error", event.get("error", event.get("message")))

    def _terminal(self, terminal, failed):
        self.terminal_count += 1
        self.last_terminal = terminal
        if failed:
            self.failure_count += 1
            if len(self.failures) < LIMIT:
                self.failures.append(terminal)

    def record(self, *, run_id, attempt, stage, operation, provider, input_identity,
               process, decoder, phase, error):
        # These categories describe observed boundaries, not inferred root causes.
        category = ("start_failed" if process is None else
                    "terminal_failure" if self.failure_count else
                    "nonzero_exit" if process.returncode not in {None, 0} else
                    "incomplete_result" if not decoder.finished else "controller_rejected")
        return {
            "version": 1, "authority": "provider_failure_observation",
            "run_id": run_id, "attempt": attempt, "stage": stage, "operation": operation,
            "provider": provider, "kind": self.kind, "input_identity": input_identity,
            "phase": phase, "category": category,
            "returncode": process.returncode if process is not None else None,
            "result_seen": self.result_seen, "projection_complete": self.projection_complete,
            "decoder_finished": decoder.finished,
            "decoder_error_present": bool(decoder.error),
            "terminal_count": self.terminal_count, "last_terminal": self.last_terminal,
            "terminal_failure_count": self.failure_count, "terminal_failures": self.failures,
            "terminal_failures_truncated": self.failure_count > len(self.failures),
            "error_count": self.error_count, "errors": self.errors,
            "errors_truncated": self.error_count > len(self.errors),
            "error_sources": sorted(self.sources | {"controller." + phase}),
            "streams": {source: {"bytes": state["bytes"], "chunks": state["chunks"],
                                 "sha256": state["digest"].hexdigest()}
                        for source, state in self.streams.items()},
            "os_errno": error.errno if isinstance(error, OSError) else None,
        }


def failure_evidence(refs):
    unique = {ref["hash"]: ref for ref in refs}
    return {"provider_failures": list(unique.values())} if unique else {}
