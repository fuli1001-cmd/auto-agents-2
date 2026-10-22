"""Read-only toolchain facts; prerequisites never authorize or satisfy a gate."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import ConfigurationError, ToolchainUnavailable


def require_executable(tool, command, *, environment=None):
    environment = os.environ if environment is None else environment
    path = shutil.which(str(command), path=environment.get("PATH", os.defpath))
    if not path:
        raise ToolchainUnavailable(tool, "missing", "在启动 aa2 的同一终端激活已有工具链，或配置该命令的明确路径")
    return str(Path(path).absolute())


def interpreter_requirement(binary, environment):
    """Check only declared shebang interpreters; never guess wrapper dependencies."""
    try:
        with Path(binary).open("rb") as stream:
            line = stream.readline(4096)
        if not line.startswith(b"#!"):
            return None
        words = shlex.split(line[2:].decode("utf-8").strip())
        if not words:
            raise ValueError("Empty shebang")
        interpreter = words[0]
        if Path(interpreter).name == "env":
            words = words[1:]
            if words[:1] == ["-S"]:
                words = words[1:]
            if not words or words[0].startswith("-") or "=" in words[0]:
                raise ValueError("Unsupported env shebang")
            interpreter = words[0]
        return require_executable("provider interpreter", interpreter, environment=environment)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolchainUnavailable("provider interpreter", "unverified", "检查 provider 启动脚本的解释器声明") from exc


def version_probe(tool, binary, *, environment=None, timeout=15):
    path = require_executable(tool, binary, environment=environment)
    try:
        completed = subprocess.run([path, "--version"], env=environment,
                                   capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolchainUnavailable(tool, "probe_failed", "修复该工具的启动或版本查询；预检不改变系统配置") from exc
    # Raw diagnostics may contain configuration or credentials; retain only a version.
    match = re.search(rb"(?<![a-zA-Z0-9])v?(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9.+-]*))\b", completed.stdout)
    if completed.returncode or not match:
        raise ToolchainUnavailable(tool, "probe_failed", "确认该工具可在当前终端成功执行 --version")
    return {"path": path, "version": match.group(1).decode("ascii")}


class ToolchainPreflight:
    def __init__(self, config, sandbox):
        self.config, self.sandbox = config, sandbox

    def inspect(self):
        cfg = self.config.load()
        checks = {"controller_python": {"ok": sys.version_info >= (3, 11),
            "path": sys.executable, "prefix": sys.prefix, "version": sys.version.split()[0]}}
        for tool, binary in (("git", "git"), ("conda", cfg.environment.conda)):
            try:
                checks[tool] = {"ok": True, **version_probe(tool, binary)}
            except ToolchainUnavailable as exc:
                checks[tool] = {"ok": False, **exc.record()}
        try:
            effective = self.config.resolve("review")
            interpreter = interpreter_requirement(effective["binary"], effective["environment"])
            interpreter_version = None
            if interpreter and Path(interpreter).name in {"node", "nodejs"}:
                interpreter_version = version_probe("provider Node", interpreter, environment=effective["environment"])["version"]
            checks["provider"] = {"ok": True, "path": effective["binary"], "interpreter": interpreter,
                                  "interpreter_version": interpreter_version}
        except ToolchainUnavailable as exc:
            checks["provider"] = {"ok": False, **exc.record()}
        except ConfigurationError:
            checks["provider"] = {"ok": False, "tool": "provider", "status": "configuration_unavailable",
                "action": "检查当前 provider 的路径、profile 和原生配置；本预检不会调用模型或修改配置"}
        ok, _ = self.sandbox.probe()
        checks["sandbox"] = {"ok": ok, "tool": "sandbox", "status": "ready" if ok else "probe_failed",
            "action": "确认指定的 bubblewrap 能通过实际只读权限探针"}
        optional = {"prototype_playwright": {"available": importlib.util.find_spec("playwright") is not None,
            "required_when": "prototype or application browser inspection"}}
        return {"version": 1, "ok": all(item["ok"] for item in checks.values()),
                "checks": checks, "optional": optional}

    async def check(self):
        report = await asyncio.to_thread(self.inspect)
        if not report["ok"]:
            failed = next((item for item in report["checks"].values() if not item["ok"]))
            raise ToolchainUnavailable(failed.get("tool", "controller Python"),
                failed.get("status", "unsupported_version"), failed.get("action", "使用 Python 3.11+ 的 aa2 专用环境"), report)
        return report
