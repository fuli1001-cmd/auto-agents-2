from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import PolicyViolation
from .util import atomic_write, identifier


class SandboxRunner:
    """A process-wide read-only host view with explicit, controller-owned write mounts."""

    def __init__(self, state_root: Path, binary: str = "bwrap", *, _test_unconfined: bool = False):
        self.root = state_root.resolve()
        local = Path(sys.executable).parent / binary
        self.binary = shutil.which(binary) or str(local if local.is_file() else Path(binary).expanduser())
        self.test_unconfined = _test_unconfined

    def command(self, argv: list[str], cwd: Path, writable: list[Path],
                readable: list[Path] | None = None, overlays: list[tuple[Path, Path]] | None = None, hidden: list[Path] | None = None) -> list[str]:
        if self.test_unconfined:
            return argv
        if not Path(self.binary).is_file():
            raise PolicyViolation("bubblewrap 不可用；请在 aa2 专用环境安装后运行 aa2 doctor")
        cmd = [self.binary, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
               "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        user_home = Path.home()
        cmd += ["--tmpfs", str(user_home)]
        for path in hidden or []:
            if path.exists():
                cmd += ["--tmpfs", str(path.resolve())]
        mounts = [Path(sys.prefix).resolve(), cwd.resolve(), *(p.resolve() for p in readable or [])]
        implicit = [Path(sys.prefix).resolve()]
        direct = []
        executable = Path(argv[0])
        if executable.is_absolute() and executable.is_file():
            real = executable.resolve()
            if real.is_relative_to(user_home) or real.is_relative_to(Path("/tmp")):
                prefix = next((p for p in (*executable.parents, *real.parents)
                               if p.is_relative_to(user_home) and
                               ((p / "bin/python").exists() or (p / "bin/node").exists())), None)
                mounts.insert(0, prefix or real)
                implicit.append((prefix or real).resolve())
                if executable != real and not (prefix and executable.is_relative_to(prefix)):
                    direct.append((real, executable))
        for path in [*implicit, *(p for pair in direct for p in pair)]:
            if any(path == h.resolve() or path.is_relative_to(h.resolve()) or h.resolve().is_relative_to(path)
                   for h in hidden or []):
                raise PolicyViolation("Provider runtime mount would expose a protected project root")
        for source, destination in direct:
            cmd += ["--ro-bind", str(source), str(destination)]
        metadata = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                  capture_output=True, text=True, timeout=10)
        if metadata.returncode == 0:
            mounts.append(Path(metadata.stdout.strip()))
        for path in mounts:
            if hidden and any(h.resolve() == path or h.resolve().is_relative_to(path) for h in hidden):
                raise PolicyViolation("Provider runtime mount would expose a protected project root")
            if path.is_relative_to(self.root):
                continue
            if path.exists():
                cmd += ["--ro-bind", str(path), str(path)]
        # Mask state AFTER a parent project mount; restore only explicitly selected children.
        cmd += ["--tmpfs", str(self.root)]
        for path in mounts:
            if path.is_relative_to(self.root) and path.exists():
                cmd += ["--ro-bind", str(path), str(path)]
        for path in writable:
            if path.absolute() != path.resolve() or not any(path.resolve().is_relative_to(root) for root in (self.root, cwd.resolve())):
                raise PolicyViolation(f"Writable mount must be an owned path without symlinks: {path}")
            if not path.is_file():
                path.mkdir(parents=True, exist_ok=True)
            cmd += ["--bind", str(path.resolve()), str(path.resolve())]
        for source, destination in overlays or []:
            if source.absolute() != source.resolve() or not source.resolve().is_relative_to(self.root):
                raise PolicyViolation("Provider session overlays must be owned by the controller")
            cmd += ["--bind", str(source.resolve()), str(destination)]
        cmd += ["--chdir", str(cwd.resolve()), "--", *argv]
        return cmd

    def probe(self) -> tuple[bool, str]:
        if self.test_unconfined:
            return True, "test-only runner"
        scratch = self.root / "tmp" / identifier("sandbox_probe")
        scratch.mkdir(parents=True)
        protected = scratch / "protected.txt"
        atomic_write(protected, "original")
        out = scratch / "out"
        out.mkdir()
        script = 'test "$(cat protected.txt)" = original && ! (echo bad > protected.txt) && echo ok > out/ok'
        try:
            result = subprocess.run(self.command(["/bin/sh", "-c", script], scratch, [out]),
                                    capture_output=True, text=True, timeout=10)
            ok = result.returncode == 0 and protected.read_text() == "original" and (out / "ok").exists()
            return ok, "read-only enforcement passed" if ok else result.stderr[-2000:]
        except (OSError, subprocess.TimeoutExpired, PolicyViolation) as exc:
            return False, str(exc)
        finally:
            shutil.rmtree(scratch)

    async def spawn(self, argv, cwd, env, writable, readable=None, overlays=None, hidden=None):
        executable = shutil.which(argv[0], path=env.get("PATH", os.defpath))
        if not executable:
            raise PolicyViolation(f"Executable unavailable in selected environment: {argv[0]}")
        argv = [executable, *argv[1:]]
        env = {**env, "GIT_OPTIONAL_LOCKS": "0"}
        command = await asyncio.to_thread(self.command, argv, cwd, writable, readable, overlays, hidden)
        from .managed_process import spawn_managed
        paths = [cwd, *writable, *(readable or []), *(p for pair in overlays or [] for p in pair)]
        return await spawn_managed(command, cwd, env, self.root, paths)

    async def stop(self, process):
        if process.stop_task is None:
            process.stop_task = asyncio.create_task(self._stop(process))
        task = process.stop_task
        cancelled = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
                if task.cancelled():
                    raise
            except BaseException as exc:
                if cancelled is None:
                    raise
                cancelled.add_note("Process termination failed: " + type(exc).__name__)
                break
        if cancelled is not None:
            raise cancelled

    async def _stop(self, process):
        try:
            process.control.write_eof()
        except (OSError, RuntimeError):
            process.control.close()
        from .managed_process import finish_supervisor
        await finish_supervisor(process.supervisor, process.record)
        await process.monitor
        process.cleanup_confirmed = True
        process.control.close()
        process.record.unlink(missing_ok=True)

    def confirm_record(self, value):
        record = Path(value)
        if record.parent != self.root / "processes":
            raise PolicyViolation("Invalid process cleanup record")
        if not record.exists():
            return
        data = json.loads(record.read_text())
        if data.get("status") != "cleaned":
            raise PolicyViolation("Validated provider result awaits process cleanup; preserve it without redispatch")
        record.unlink(missing_ok=True)

    def reap_orphans(self):
        directory = self.root / "processes"
        if not directory.exists():
            return
        for file in directory.glob("*.json"):
            data = json.loads(file.read_text())
            if data.get("version") == 3 and data.get("status") == "cleaned":
                file.unlink(missing_ok=True)
                continue
            if not data.get("start") or not data.get("pid"):
                raise PolicyViolation("Recorded process identity is unknown; preserving recovery record")
            current = process_start(data["pid"])
            if current == data["start"]:
                raise PolicyViolation("A recorded process is still live; preserve its recovery record until owned cleanup completes")
            if data.get("version") == 3:
                raise PolicyViolation("Process cleanup lacks confirmation; preserving recovery record and mounted resources")
            file.unlink(missing_ok=True)


def process_start(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return "" if fields[0] == "Z" else fields[19]
    except FileNotFoundError:
        return ""
    except (OSError, IndexError, ValueError) as exc:
        raise PolicyViolation("Process identity cannot be inspected; preserving recovery state") from exc
