from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .artifacts import ArtifactStore
from .config import ConfigStore
from .dependencies import copy_execution_source, manifest_reference, pip_inputs
from .diagnostics import DiagnosticOperation, current_diagnostics
from .errors import AA2Error
from .evidence import RUNTIME_CACHES
from .logs import capture_diagnostics
from .progress import capture_output
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import SECRET, async_file_lock, atomic_write, contained, digest, identifier, scrub_text, write_json
from .workspace import files, input_entry, tree_hash


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
        manifest_reference(conda_name, "environment_file")
        manifest_reference(pip_name, "requirements_file")
        conda_file = workspace / conda_name if conda_name else None
        if conda_file is None and (workspace / "environment.yml").exists():
            conda_file = workspace / "environment.yml"
        if conda_file and (not conda_file.resolve().is_relative_to(workspace.resolve()) or not conda_file.is_file()):
            raise AA2Error("environment_file must refer to an existing file inside the execution workspace")
        conda_document, extra_pip = None, []
        if conda_file:
            try:
                conda_document = yaml.safe_load(conda_file.read_text())
                dependencies = conda_document.get("dependencies", [])
                for dependency in dependencies:
                    if isinstance(dependency, dict):
                        if set(dependency) != {"pip"} or not isinstance(dependency["pip"], list):
                            raise ValueError("Unsupported Conda dependency section")
                        extra_pip.extend(dependency["pip"])
                if not all(isinstance(line, str) for line in extra_pip):
                    raise ValueError("Invalid pip entries")
                conda_document["dependencies"] = [d for d in dependencies if isinstance(d, str)]
            except (ValueError, AttributeError, yaml.YAMLError) as exc:
                raise AA2Error("Invalid Conda environment document") from exc
        inputs = pip_inputs(workspace, pip_name, extra_pip)
        local = inputs.local
        fingerprint = digest({"format": 3, "platform": platform.platform(), "python": cfg.python_version,
                              "node": cfg.node_version, "profile": profile, "manifests": inputs.manifests,
                              "resolver_configuration": digest({k: v for k, v in os.environ.items()
                                                                  if k.startswith(("PIP_", "CONDA_"))}),
                              "conda": conda_file.read_text() if conda_file else "",
                              "requirements": inputs.requirements, "constraints": inputs.constraints,
                              "private_source": [str(workspace), tree_hash(workspace)] if local else None})
        location = self.root / "environment-records" / fingerprint / "instance.json"
        run = self.store.current(unfinished=True)
        async with async_file_lock(self.root / "environment-locks" / f"{fingerprint}.lock"):
            prefix = self.root / "environments" / fingerprint
            if location.is_file():
                try:
                    selected = Path(json.loads(location.read_text())["prefix"])
                    if selected.parent == prefix.parent:
                        prefix = selected
                except (ValueError, KeyError):
                    pass
            if self.valid(prefix, fingerprint):
                self.resources.register(prefix, "environment", run["id"] if run else None)
                return Environment(prefix, self.installed_identity(prefix))
            # A corrupt or interrupted instance can still be referenced by recovery records.
            if prefix.exists():
                prefix = prefix.with_name(fingerprint + "-" + identifier("instance"))
            ready = prefix / "aa2-ready.json"
            self.resources.register(prefix, "environment", run["id"] if run else None)
            with self.resources.temporary("pip-inputs") as scratch:
                source = prefix / "aa2-source" if local else workspace
                requirements, constraints = inputs.rendered(source)
                constraint_file = scratch / "constraints.txt"
                atomic_write(constraint_file, constraints)
                pip_file = scratch / "requirements.txt" if inputs.requirements else None
                if pip_file:
                    atomic_write(pip_file, f"-c {constraint_file}\n" + requirements)
                selected_conda = conda_file
                if conda_document is not None:
                    selected_conda = scratch / "environment.yml"
                    atomic_write(selected_conda, yaml.safe_dump(conda_document))
                await self._prepare(prefix, ready, fingerprint, profile, selected_conda, pip_file, workspace, [], local)
            write_json(location, {"prefix": str(prefix)})
        return Environment(prefix, self.installed_identity(prefix))

    @staticmethod
    def integrity(prefix: Path) -> str:
        # Interpreter, packages, copied source and resolved locks; writable caches are excluded.
        names = ("bin", "lib", "lib64", "conda-meta", "aa2-source", "aa2-pip.lock", "aa2-conda.lock")
        return digest([(name, input_entry(prefix / name, prefix, excluded=RUNTIME_CACHES)) for name in names])

    @staticmethod
    def installed_identity(prefix: Path) -> str:
        ready = json.loads((prefix / "aa2-ready.json").read_text())
        return digest(["installed-v2", ready["fingerprint"], ready["integrity"]])

    @classmethod
    def valid(cls, prefix: Path, fingerprint: str) -> bool:
        try:
            ready = json.loads((prefix / "aa2-ready.json").read_text())
            return (fingerprint in {ready.get("fingerprint"), cls.installed_identity(prefix)} and (prefix / "bin/python").is_file()
                    and ready.get("integrity") == cls.integrity(prefix))
        except (OSError, ValueError, KeyError, RecursionError):
            return False

    async def _command(self, argv, workspace, env, writes, reads=(), timeout=1800):
        process = await self.sandbox.spawn(argv, workspace, env, writes, list(reads))
        try:
            async with asyncio.timeout(timeout):
                machine = "--explicit" in argv or "freeze" in argv or "-c" in argv
                if machine:
                    stdout, stderr = await capture_output(process)
                else:
                    stdout, stderr, logs = await capture_diagnostics(process, self.resources, ArtifactStore(self.root),
                                                                     [v for k, v in env.items() if SECRET.search(k)])
                    run = self.store.current(unfinished=True)
                    operation = current_diagnostics.get()
                    if operation:
                        operation.capture(logs)
                    elif run:
                        self.store.checkpoint(run["id"], "environment/logs/" + identifier("command"),
                                              {"logs": logs, "exit_code": process.returncode})
            if process.returncode:
                raise AA2Error("Dependency preparation failed: " + scrub_text(stderr.decode(errors="replace"), [v for k, v in env.items() if SECRET.search(k)])[-2000:])
            return stdout.decode(errors="replace")
        except TimeoutError as exc:
            raise AA2Error("Dependency preparation timed out") from exc
        finally:
            await self.sandbox.stop(process)

    async def _prepare(self, prefix, ready, fingerprint, profile, conda_file, pip_file, workspace, dependencies, local):
        with DiagnosticOperation(self.store, self.resources, "prepare/" + str(prefix)):
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
                if conda_file:
                    reads.append(conda_file.parent)
                if records.exists():
                    reads.append(records)
                await self._command(argv, workspace, env, [prefix, cache, scratch], reads)
                install_workspace = workspace
                if local:
                    # Editable bindings live in this private environment and never point into another task.
                    install_workspace = prefix / "aa2-source"
                    copy_execution_source(workspace, install_workspace)
                if dependencies and pip_file is None:
                    pip_file = scratch / "dependencies.txt"
                    atomic_write(pip_file, "\n".join(dependencies) + "\n")
                if pip_file:
                    await self._command([str(prefix / "bin/python"), "-m", "pip", "install", "-r", str(pip_file)],
                                        install_workspace, env, [prefix, scratch], [prefix, pip_file.parent, *reads])
                await self._command([str(prefix / "bin/python"), "-m", "pip", "check"],
                                    workspace, env, [scratch], [prefix], timeout=60)
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
                                   "pip_hash": digest(frozen), "conda_hash": digest(explicit),
                                   "integrity": self.integrity(prefix)})

    async def install_node(self, workspace: Path, environment: Environment, *, frozen: bool = False):
        with DiagnosticOperation(self.store, self.resources, "node/" + str(workspace)):
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
                marker = self.root / "node-records" / (digest(str(workspace)) + ".json")
                identity = digest([environment.fingerprint, (workspace / "package.json").read_text(), lock.read_text()])
                try:
                    installed = json.loads(marker.read_text())
                except (OSError, ValueError):
                    installed = {}
                actual = digest(input_entry(modules, modules, excluded=RUNTIME_CACHES)) if modules.exists() else ""
                if installed.get("fingerprint") == identity and installed.get("integrity") == actual:
                    if not frozen:
                        await self.prepare_generated_types(workspace, environment)
                    return
                await self._command([npm, "ci"], workspace, variables, [modules, scratch, package_cache], [environment.prefix], timeout=900)
                write_json(marker, {"fingerprint": identity, "integrity": digest(input_entry(modules, modules, excluded=RUNTIME_CACHES))})
                if not frozen:
                    await self.prepare_generated_types(workspace, environment)

    async def prepare_delivery(self, environment: Environment, command_directories=()):
        """Handoff the verified immutable environment, installing only frozen Node outputs."""
        from .profiles import ProjectProfile
        project = self.config.project
        if environment.prefix.is_relative_to(self.root / "environments") and not self.valid(environment.prefix, environment.fingerprint):
            raise AA2Error("Verified environment integrity changed; preserve it and reverify a new instance before handoff")
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
