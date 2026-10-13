from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigStore
from .errors import AA2Error
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import SECRET, async_file_lock, atomic_write, contained, digest, write_json
from .workspace import files, tree_hash


@dataclass(frozen=True)
class Environment:
    prefix: Path
    fingerprint: str

    def variables(self, scratch: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
               and not SECRET.search(k)}
        env.update(PATH=str(self.prefix / "bin") + os.pathsep + env.get("PATH", ""),
                   CONDA_PREFIX=str(self.prefix), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                   XDG_CACHE_HOME=str(scratch / "cache"), TMPDIR=str(scratch),
                   npm_config_cache=str(scratch / "npm-cache"), npm_config_update_notifier="false",
                   NEXT_TELEMETRY_DISABLED="1", PLAYWRIGHT_BROWSERS_PATH=str(scratch / "browsers"))
        return env


class EnvironmentManager:
    def __init__(self, config: ConfigStore, sandbox: SandboxRunner):
        self.config, self.sandbox = config, sandbox
        self.root = config.project / ".aa2"
        self.store = Store(config.project)
        self.resources = ResourceManager(self.store, config.load().cleanup)

    async def ensure(self, workspace: Path, profile: str, environment_file="", requirements_file="") -> Environment:
        cfg = self.config.load().environment
        conda_name, pip_name = environment_file or cfg.environment_file, requirements_file or cfg.requirements_file
        conda_file = workspace / conda_name if conda_name else None
        pip_file = workspace / pip_name if pip_name else None
        if conda_file is None and (workspace / "environment.yml").exists():
            conda_file = workspace / "environment.yml"
        if pip_file is None:
            for name in ("requirements.lock", "requirements.txt", "backend/requirements.lock", "backend/requirements.txt"):
                if (workspace / name).is_file():
                    pip_file = workspace / name
                    break
        dependencies = []
        if pip_file is None:
            for name in ("pyproject.toml", "backend/pyproject.toml"):
                file = workspace / name
                if file.is_file():
                    project = tomllib.loads(file.read_text()).get("project", {})
                    dependencies += project.get("dependencies", [])
                    extras = project.get("optional-dependencies", {})
                    dependencies += extras.get("test", []) + extras.get("dev", [])
        for file in (conda_file, pip_file):
            if file and (not file.resolve().is_relative_to(workspace.resolve()) or not file.is_file()):
                raise AA2Error(f"Invalid environment manifest: {file}")
        manifests = {str(p.relative_to(workspace)): p.read_text() for p in files(workspace)
                     if not str(p.relative_to(workspace)).startswith(("docs/", "design/", "tests/"))
                     and (p.name in {"pyproject.toml", "setup.cfg", "setup.py", "environment.yml", "environment.yaml"}
                          or (p.name.startswith(("requirements", "constraints")) and p.suffix in {".txt", ".in", ".lock"}))}
        manifest_text = "\n".join(manifests.values())
        local = bool(re.search(r"(?m)(?:^|\s)(?:-e |--editable |\./|\.\./)|file:| @ (?:\.|/)", manifest_text))
        if local and conda_file:
            raise AA2Error("Local/editable pip dependencies must be declared in requirements_file, separately from the Conda manifest")
        fingerprint = digest({"format": 2, "platform": platform.platform(), "python": cfg.python_version,
                              "node": cfg.node_version, "profile": profile, "manifests": manifests,
                              "conda": conda_file.read_text() if conda_file else "",
                              "pip": pip_file.read_text() if pip_file else "", "dependencies": dependencies,
                              "private_source": [str(workspace), tree_hash(workspace)] if local else None})
        prefix = self.root / "environments" / fingerprint
        ready = prefix / "aa2-ready.json"
        run = self.store.current(unfinished=True)
        self.resources.register(prefix, "environment", run["id"] if run else None)
        async with async_file_lock(self.root / "environment-locks" / f"{fingerprint}.lock"):
            if ready.is_file() and (prefix / "bin/python").is_file():
                return Environment(prefix, fingerprint)
            await self._prepare(prefix, ready, fingerprint, profile, conda_file, pip_file, workspace, dependencies, local)
        return Environment(prefix, fingerprint)

    async def _command(self, argv, workspace, env, writes, reads=(), timeout=1800):
        process = await self.sandbox.spawn(argv, workspace, env, writes, list(reads))
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
            if process.returncode:
                raise AA2Error("Dependency preparation failed: " + stderr.decode(errors="replace")[-2000:])
            return stdout.decode(errors="replace")
        except TimeoutError as exc:
            raise AA2Error("Dependency preparation timed out") from exc
        finally:
            await self.sandbox.stop(process)

    async def _prepare(self, prefix, ready, fingerprint, profile, conda_file, pip_file, workspace, dependencies, local):
        cfg = self.config.load().environment
        if prefix.exists():
            shutil.rmtree(prefix)
        prefix.mkdir(parents=True)
        cache = self.root / "cache/conda"
        cache.mkdir(parents=True, exist_ok=True)
        run = self.store.current(unfinished=True)
        self.resources.register(cache, "cache", run["id"] if run else None)
        conda = shutil.which(cfg.conda)
        if not conda:
            raise AA2Error("Conda not found")
        records = self.root / "environment-records" / fingerprint
        with self.resources.temporary("environment") as scratch:
            environment = Environment(prefix, fingerprint)
            env = environment.variables(scratch)
            env.pop("CONDA_PREFIX", None)
            env.update(CONDA_PKGS_DIRS=str(cache), PIP_CACHE_DIR=str(scratch / "pip"), CONDA_ALWAYS_COPY="true")
            if (records / "conda.lock").is_file() and not local:
                argv = [conda, "create", "-y", "--copy", "-p", str(prefix), "--file", str(records / "conda.lock")]
            elif conda_file:
                argv = [conda, "env", "create", "-p", str(prefix), "-f", str(conda_file)]
            else:
                argv = [conda, "create", "-y", "--copy", "-p", str(prefix), f"python={cfg.python_version}", "pip", "pytest"]
                if profile in {"react", "next", "python-react", "python-next"}:
                    argv += [f"nodejs={cfg.node_version}", "--channel", "conda-forge"]
            reads = [Path(conda).resolve().parent.parent]
            if records.exists():
                reads.append(records)
            await self._command(argv, workspace, env, [prefix, cache, scratch], reads)
            install_workspace = workspace
            if local:
                # Editable bindings live in this private environment and never point into another task.
                install_workspace = prefix / "aa2-source"
                for file in files(workspace):
                    target = install_workspace / file.relative_to(workspace)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
                if pip_file:
                    pip_file = install_workspace / pip_file.relative_to(workspace)
            if (records / "pip.lock").is_file() and not local:
                pip_file = records / "pip.lock"
            elif dependencies and pip_file is None:
                pip_file = scratch / "dependencies.txt"
                atomic_write(pip_file, "\n".join(dependencies) + "\n")
            if pip_file:
                await self._command([str(prefix / "bin/python"), "-m", "pip", "install", "-r", str(pip_file)],
                                    install_workspace, env, [prefix, scratch], [prefix, *reads])
            identity = await self._command([str(prefix / "bin/python"), "-c", "import sys; print(sys.prefix)"],
                                           workspace, env, [scratch], [prefix], timeout=30)
            if Path(identity.strip()).resolve() != prefix.resolve():
                raise AA2Error("Environment interpreter identity mismatch")
            frozen = await self._command([str(prefix / "bin/python"), "-m", "pip", "freeze"], workspace, env, [scratch], [prefix], timeout=30)
            explicit = await self._command([conda, "list", "--explicit", "-p", str(prefix)], workspace, env,
                                           [scratch], [prefix, *reads], timeout=30)
            atomic_write(prefix / "aa2-pip.lock", frozen)
            atomic_write(prefix / "aa2-conda.lock", explicit)
            if not local:
                atomic_write(records / "pip.lock", frozen)
                atomic_write(records / "conda.lock", explicit)
            write_json(ready, {"fingerprint": fingerprint, "private": local,
                               "pip_hash": digest(frozen), "conda_hash": digest(explicit)})

    async def install_node(self, workspace: Path, environment: Environment, *, frozen: bool = False):
        if not (workspace / "package.json").is_file():
            return
        if any((workspace / p).exists() for p in ("pnpm-lock.yaml", "yarn.lock")):
            raise AA2Error("首期 Node 安装器使用 npm；已有 pnpm/yarn 锁文件需要对应安装器，不能自动改写包管理器")
        package_cache = self.root / "cache/npm"
        package_cache.mkdir(parents=True, exist_ok=True)
        run = self.store.current(unfinished=True)
        self.resources.register(package_cache, "cache", run["id"] if run else None)
        npm = str(environment.prefix / "bin/npm")
        if not Path(npm).exists():
            raise AA2Error("Node runtime is missing from the selected project environment")
        lock = workspace / "package-lock.json"
        with self.resources.temporary("node") as scratch:
            variables = environment.variables(scratch)
            variables["npm_config_cache"] = str(package_cache)
            if not lock.exists():
                if frozen:
                    raise AA2Error("发布版本缺少 package-lock.json，必须在隔离工作区补齐并重新验收")
                staging = scratch / "resolve"
                staging.mkdir()
                for file in files(workspace):
                    if file.name in {"package.json", ".npmrc"}:
                        target = staging / file.relative_to(workspace)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(file, target)
                await self._command([npm, "install", "--package-lock-only", "--ignore-scripts"], staging,
                                    variables, [scratch, package_cache], [environment.prefix], timeout=900)
                atomic_write(lock, (staging / "package-lock.json").read_bytes(), 0o644)
            modules = workspace / "node_modules"
            marker = modules / ".aa2-install.json"
            identity = digest([environment.fingerprint, (workspace / "package.json").read_text(), lock.read_text()])
            if marker.is_file() and json.loads(marker.read_text()).get("fingerprint") == identity:
                if not frozen:
                    await self.prepare_generated_types(workspace, environment)
                return
            await self._command([npm, "ci"], workspace, variables, [modules, scratch, package_cache], [environment.prefix], timeout=900)
            write_json(marker, {"fingerprint": identity})
            if not frozen:
                await self.prepare_generated_types(workspace, environment)

    async def prepare_delivery(self, environment: Environment, command_directories=()):
        """Handoff the verified immutable environment, installing only frozen Node outputs."""
        from .profiles import ProjectProfile
        project = self.config.project
        if not (environment.prefix / "bin/python").is_file():
            raise AA2Error("已验证的运行环境缺失；保留发布记录，需恢复该环境后续跑")
        for root in ProjectProfile.detect(project, self.config.load().project_profile).node_roots:
            await self.install_node(root, environment, frozen=True)
        paths = [project / "src", project]
        for directory in command_directories:
            cwd = project if directory == "." else contained(project, directory)
            paths.extend([cwd / "src", cwd])
        python_path = os.pathsep.join(dict.fromkeys(str(p) for p in paths))
        # Editable source copies belong to the environment, never to disposable worktrees.
        atomic_write(self.root / "runtime.sh", "# Generated by aa2; source this file from the project root.\n"
                     + f"export CONDA_PREFIX={shlex.quote(str(environment.prefix))}\n"
                     + f'export PATH={shlex.quote(str(environment.prefix / "bin"))}:"$PATH"\n'
                     + f"export PYTHONPATH={shlex.quote(python_path)}\n"
                     + "export PYTHONNOUSERSITE=1\nunset PYTHONHOME\n", 0o600)

    async def prepare_generated_types(self, workspace: Path, environment: Environment):
        package = workspace / "node_modules/next/package.json"
        if not package.exists() or not (workspace / "tsconfig.json").exists():
            return
        version = json.loads(package.read_text())["version"]
        if tuple(int(n) for n in re.findall(r"\d+", version)[:2]) < (15, 5):
            return  # Older projects retain their existing framework declaration file.
        declaration = workspace / "next-env.d.ts"
        if declaration.is_symlink():
            raise AA2Error("Generated declarations must be local files")
        if declaration.exists() and declaration.read_text().strip() and "This file should not be edited" not in declaration.read_text():
            raise AA2Error("next-env.d.ts contains custom declarations; move these to an application declaration file before regenerating")
        if not declaration.exists():
            atomic_write(declaration, "", 0o644)
        with self.resources.temporary("next-types") as scratch:
            before = digest(declaration.read_bytes())
            await self._command(["node", str(workspace / "node_modules/next/dist/bin/next"), "typegen"], workspace,
                                environment.variables(scratch), [scratch, workspace / ".next", declaration],
                                [environment.prefix], timeout=120)
            after = digest(declaration.read_bytes())
            if before != after:
                run = self.store.current(unfinished=True)
                self.store.event(run["id"] if run else None, "framework.types_prepared",
                                 {"path": str(declaration), "before": before, "after": after, "version": version})
