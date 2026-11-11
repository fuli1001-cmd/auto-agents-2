"""Content-free native provider observations, never recovery or retry authority."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

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
MAX_RETRY_AFTER_SECONDS = 86400
# Only these shallow native error fields are examined; no payload/header traversal.
HTTP_FIELDS = ("http_status", "http_status_code", "httpStatus", "status_code", "statusCode")
RETRY_FIELDS = ("Retry-After", "retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds")
PUBLIC_CODE = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(sorted(CODES - {"SUCCESS"})) + r")(?![A-Za-z0-9_])")
HTTP_MARKER = re.compile(
    r"(?<![\w./\\-])(?:HTTP(?:/[0-9](?:\.[0-9])?)?(?:[ \t]+status(?:[ \t]+code)?)?"
    r"(?:[ \t]*[:=][ \t]*|[ \t]+)|(?:\"(?:" + "|".join(HTTP_FIELDS) + r")\"|(?:"
    + "|".join(HTTP_FIELDS) + r"))[ \t]*[:=][ \t]*)(?:\"(?P<quoted>[1-5][0-9]{2})\"|"
    r"(?P<bare>[1-5][0-9]{2}))(?![\w./\\-])", re.I)
KEYWORD_SIGNALS = {
    "rate_limit": r"\b(?:rate[ _-]limit(?:ed|ing)?|too many requests)\b",
    "capacity": r"\bcapacity\b", "quota": r"\bquota\b",
    "authentication": r"\b(?:authentication|unauthenticated|unauthorized)\b",
    "permission": r"\b(?:permission|access denied)\b",
    "timeout": r"\b(?:timeout|timed out)\b", "connection": r"\bconnection\b",
}
RFC3339 = (r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
           r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")
HTTP_DATE = (r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
             r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT")
RETRY_VALUE = "(?:" + HTTP_DATE + "|" + RFC3339 + r"|[0-9]{1,6})"
RETRY_MARKER = re.compile(
    r"(?<![\w./\\-])(?:\"(?P<quoted_key>" + "|".join(RETRY_FIELDS) + r")\"|(?P<key>"
    + "|".join(RETRY_FIELDS) + r"))[ \t]*[:=][ \t]*(?:\"(?P<quoted>" + RETRY_VALUE
    + r")\"|(?P<bare>" + RETRY_VALUE + r"))(?=[ \t]*(?:$|[\r\n,;}\])]))", re.I)


def opaque(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {"type": type(value).__name__, "bytes": len(encoded), "sha256": digest(encoded)}


def code(value, allowed=CODES):
    return {"value": value if type(value) is str and value in allowed else "unknown",
            **opaque(value)}


def denied_action(value):
    # Headless JSONDeniedAction uses action/display_name; internal protobuf uses tool_name/args/reason.
    # Legacy string entries and unknown extensions remain explicitly opaque.
    result = {"fingerprint": opaque(value), "tool": code(None, TOOLS), "reason": code(None)}
    if type(value) is str:
        result["tool"] = code(value, TOOLS)
    elif type(value) is dict:
        result["fields_present"] = {field: field in value for field in
            ("action", "display_name", "tool_name", "toolName", "args", "reason_code", "reason")}
        if "action" in value or "display_name" in value:
            result["wire_shape"] = "headless_action_display"
            result["headless_action"] = code(value.get("action"), TOOLS)
            result["display_name"] = opaque(value.get("display_name"))
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


def retry_after_marker(value, *, seconds_only=False):
    """Normalize only bounded whole seconds or a strict timezone-bearing date."""
    if type(value) is int or (type(value) is str and re.fullmatch(r"[0-9]{1,6}", value)):
        seconds = int(value)
        return {"kind": "seconds", "value": seconds} if 0 <= seconds <= MAX_RETRY_AFTER_SECONDS else None
    if type(value) is not str or seconds_only:
        return None
    try:
        if re.fullmatch(HTTP_DATE, value):
            parsed = parsedate_to_datetime(value)
            if format_datetime(parsed, usegmt=True) != value:
                return None  # Includes a mismatched weekday.
        elif re.fullmatch(RFC3339, value) and not value.endswith("-00:00"):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        return {"kind": "utc_datetime", "value": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    except (ValueError, OverflowError):
        return None


def native_signals(value):
    """Text/field markers only, never an HTTP receipt, root cause or retry grant."""
    texts = [value] if type(value) is str else []
    statuses, public_codes, keywords, retries = set(), set(), set(), []

    def observe_retry(key, raw):
        marker = retry_after_marker(raw, seconds_only=key.lower() in {"retry_after_seconds", "retryafterseconds"})
        if marker is not None and marker not in retries and len(retries) <= LIMIT:
            retries.append(marker)

    if type(value) is dict:
        texts = [value[key] for key in ("code", "type", "message") if type(value.get(key)) is str]
        for key in HTTP_FIELDS:
            status = value.get(key)
            if type(status) is int and 100 <= status <= 599:
                statuses.add(status)
            elif type(status) is str and re.fullmatch(r"[1-5][0-9]{2}", status):
                statuses.add(int(status))
        for key in RETRY_FIELDS:
            observe_retry(key, value.get(key))
    for text in texts:
        public_codes.update(PUBLIC_CODE.findall(text))
        statuses.update(int(match["quoted"] or match["bare"]) for match in HTTP_MARKER.finditer(text))
        keywords.update(name for name, pattern in KEYWORD_SIGNALS.items() if re.search(pattern, text, re.I))
        for match in RETRY_MARKER.finditer(text):
            observe_retry(match["quoted_key"] or match["key"], match["quoted"] or match["bare"])
    return {"authority": "native_text_or_field_markers_only",
            "public_codes": sorted(public_codes)[:LIMIT], "http_status_markers": sorted(statuses)[:LIMIT],
            "keyword_signals": sorted(keywords), "retry_after_markers": retries[:LIMIT],
            "truncated": any(len(rows) > LIMIT for rows in (public_codes, statuses, retries))}


def native_error(value):
    result = {"fingerprint": opaque(value), "code": code(None), "observed_signals": native_signals(value)}
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
