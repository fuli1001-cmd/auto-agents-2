"""Bounded capture of one controller-owned npm command's diagnostic files."""
from __future__ import annotations

import os
import re

from .safe_files import directory_fd, regular_file
from .util import scrub_text

MAX_LOG_FILES = 16
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024


def npm_debug_logs(directory, artifacts, secrets=()):
    """Never follow a path from npm output or enumerate a shared/host log directory."""
    result = {"status": "not_recorded", "logs": [], "unavailable": [],
              "scope": "This command's npm debug output; not a completed dependency graph or acceptance."}
    total = 0
    try:
        with directory_fd(directory) as parent:
            names = sorted(n for n in os.listdir(parent) if re.fullmatch(r".+-debug-\d+\.log", n))
            if len(names) > MAX_LOG_FILES:
                result.update(status="unavailable", unavailable=["debug_file_count_exceeded"])
                return result
            for name in names:
                try:
                    with regular_file(parent, name) as stream:
                        info = os.fstat(stream.fileno())
                        if info.st_nlink != 1:
                            raise ValueError("Linked diagnostic is not command-owned")
                        limit = min(MAX_LOG_BYTES, MAX_TOTAL_BYTES - total)
                        if info.st_size > limit:
                            raise ValueError("Diagnostic capture limit exceeded")
                        raw = stream.read(limit + 1)
                        if len(raw) > limit:
                            raise ValueError("Diagnostic capture limit exceeded")
                    total += len(raw)
                    text = scrub_text(raw.decode('utf-8'), list(secrets))
                    # npm's redaction is best-effort; keep credential assignments
                    # and standard npm token literals out of the archived original.
                    text = re.sub(r"(?i)((?:_authToken|_auth|password|access[_-]token)\s*[:=]\s*)\S+",
                                  r"\1<redacted>", text)
                    text = re.sub(r"\bnpm_[A-Za-z0-9]{20,}\b", "<redacted>", text)
                    result['logs'].append(artifacts.put(text, 'dependency_log'))
                except (OSError, ValueError, UnicodeError):
                    # Never hide the original installation failure or imply a
                    # truncated/unsafe file is complete. No host path is admitted.
                    result['unavailable'].append('unsafe_or_oversized_debug_file')
    except (OSError, ValueError):
        result['unavailable'].append('debug_directory_unavailable')
    result['status'] = ('incomplete' if result['unavailable'] else
                        'captured' if result['logs'] else 'not_recorded')
    return result
