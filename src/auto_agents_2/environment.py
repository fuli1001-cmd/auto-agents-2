from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .artifacts import ArtifactStore
from .config import ConfigStore
from .dependencies import copy_execution_source, manifest_reference, pip_inputs
from .diagnostics import DiagnosticOperation, current_diagnostics
from .errors import AA2Error, DependencyResolutionError, RetryableInfrastructureError
from .evidence import RUNTIME_CACHES
from .logs import capture_diagnostics
from .progress import capture_output
from .readview import sensitive_values
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import SECRET, async_file_lock, atomic_write, contained, digest, identifier, scrub_text, write_json
from .workspace import files, input_entry, tree_hash


@dataclass(frozen=True)
class Environment:
    prefix: Path
    fingerprint: str
    resolution: dict = field(default_factory=dict)

    def variables(self, scratch: Path) -> dict[str, str]:
        # Target checks are headless. Inheriting WSLg/desktop endpoints can freeze
        # Chromium's frame clock even though DOM evaluation continues to work.
        env = {k: v for k, v in os.environ.items() if k not in {
            "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"}
               and not SECRET.search(k)}
        env.update(PATH=str(self.prefix / "bin") + os.pathsep + env.get("PATH", ""),
                   CONDA_PREFIX=str(self.prefix), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                   XDG_CACHE_HOME=str(scratch / "cache"), TMPDIR=str(scratch),
                   npm_config_cache=str(scratch / "npm-cache"), npm_config_update_notifier="false",
                   NEXT_TELEMETRY_DISABLED="1", PLAYWRIGHT_BROWSERS_PATH="0")
        # Proxy clients disagree on CIDR/wildcard syntax. Validation servers always
        # bind loopback; explicit entries keep their probes off external proxies.
        bypass = list(dict.fromkeys(part.strip() for name in ("no_proxy", "NO_PROXY", "npm_config_no_proxy", "npm_config_noproxy")
                                    for part in env.get(name, "").split(",") if part.strip()))
        bypass = list(dict.fromkeys([*bypass, "127.0.0.1", "localhost", "[::1]", "::1"]))
        for name in ("no_proxy", "NO_PROXY", "npm_config_no_proxy", "npm_config_noproxy"):
            if name != "npm_config_no_proxy" or name in env:
                env[name] = ",".join(bypass)
        return env


def node_dependency_inputs(workspace):
    manifests = sorted(p for p in files(workspace) if p.name in {"package.json", ".npmrc"})
    dependency_inputs = [(p.relative_to(workspace).as_posix(), digest(p.read_bytes())) for p in manifests]
    for manifest in manifests:
        if manifest.name != "package.json":
            continue
        data = json.loads(manifest.read_text())
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for value in data.get(section, {}).values():
                if isinstance(value, str) and value.startswith("file:"):
                    path = (manifest.parent / value.removeprefix("file:")).resolve()
                    if not path.is_relative_to(workspace.resolve()) or not path.exists():
                        raise AA2Error("Local npm dependency must exist inside the execution workspace")
                    dependency_inputs.append((path.relative_to(workspace).as_posix(), input_entry(path, workspace)))
    return dependency_inputs


class EnvironmentManager:
    async def install_browser(self, workspace, cache, scratch):
        """One project-local install. The recovery controller decides subsequent attempts."""
        env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(cache), "TMPDIR": str(scratch)}
        try:
            process = await self.sandbox.spawn([sys.executable, "-m", "playwright", "install", "chromium"],
                                               workspace, env, [cache, scratch])
        except OSError as exc:
            raise RetryableInfrastructureError("Browser installer could not start") from exc
        try:
            async with asyncio.timeout(900):
                await capture_output(process)
            if process.returncode:
                raise RetryableInfrastructureError(f"Browser installer exited with code {process.returncode}")
            if not any(cache.glob("chromium-*/chrome-linux*/chrome")):
                raise RetryableInfrastructureError("Browser installation produced no usable executable")
        except TimeoutError as exc:
            raise RetryableInfrastructureError("Browser installation timed out") from exc
        finally:
            await self.sandbox.stop(process)

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
                if not isinstance(conda_document, dict):
                    raise ValueError("Expected a mapping")
                dependencies = conda_document.get("dependencies", [])
                if not isinstance(dependencies, list):
                    raise ValueError("Expected a dependency list")
                for dependency in dependencies:
                    if isinstance(dependency, dict):
                        if set(dependency) != {"pip"} or not isinstance(dependency["pip"], list):
                            raise ValueError("Unsupported Conda dependency section")
                        extra_pip.extend(dependency["pip"])
                    elif not isinstance(dependency, str):
                        raise ValueError("Invalid Conda dependency")
                if not all(isinstance(line, str) for line in extra_pip):
                    raise ValueError("Invalid pip entries")
                conda_document["dependencies"] = [d for d in dependencies if isinstance(d, str)]
            except (ValueError, AttributeError, yaml.YAMLError) as exc:
                raise AA2Error("Invalid Conda environment document") from exc
        inputs = pip_inputs(workspace, pip_name, extra_pip, protected=self.config.load().sensitive_paths)
        local = inputs.local
        fingerprint = digest({"format": 4, "platform": platform.platform(), "python": cfg.python_version,
                              "node": cfg.node_version, "profile": profile, "manifests": inputs.manifests,
                              "resolver_configuration": digest({k: v for k, v in os.environ.items()
                                                                  if k.startswith(("PIP_", "CONDA_"))}),
                              "conda": conda_file.read_text() if conda_file else "",
                              "requirements": inputs.requirements, "constraints": inputs.constraints,
                              "local_paths": inputs.local_paths, "source_paths": inputs.source_paths,
                              "private_source": [str(workspace), tree_hash(workspace)] if local else None,
                              **({"source_copy_version": 2} if local else {})})
        location = self.root / "environment-records" / fingerprint / "instance.json"
        run = self.store.current(unfinished=True)
        async with async_file_lock(self.root / "environment-locks" / f"{fingerprint}.lock"):
            prefix = self.root / "environments" / fingerprint
            if location.is_file():
                try:
                    selected = Path(json.loads(location.read_text())["prefix"])
                    if selected.parent == prefix.parent:
                        prefix = selected
                except (OSError, ValueError, KeyError, TypeError):
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
                try:
                    await self._prepare(prefix, ready, fingerprint, profile, selected_conda, pip_file, workspace, [], local, inputs)
                except DependencyResolutionError as exc:
                    raise DependencyResolutionError(f"Conda manifest {conda_file.relative_to(workspace)}: {exc}") from exc
            write_json(location, {"prefix": str(prefix)})
        return Environment(prefix, self.installed_identity(prefix))

    async def with_resolution(self, workspace: Path, environment: Environment) -> Environment:
        from .environment_evidence import capture_resolution
        evidence = await capture_resolution(self, workspace, environment)
        return Environment(environment.prefix, environment.fingerprint, evidence)

    @staticmethod
    def integrity(prefix: Path) -> str:
        # Interpreter, packages, copied source and resolved locks; writable caches are excluded.
        return digest(["prefix-v3", input_entry(prefix, prefix, excluded=RUNTIME_CACHES,
                                              omitted={prefix / "aa2-ready.json"})])

    @staticmethod
    def installed_identity(prefix: Path) -> str:
        ready = json.loads((prefix / "aa2-ready.json").read_text())
        return digest(["installed-v3", ready["fingerprint"], ready["integrity"]])

    @classmethod
    def valid(cls, prefix: Path, fingerprint: str) -> bool:
        try:
            ready = json.loads((prefix / "aa2-ready.json").read_text())
            if not isinstance(ready, dict) or any(not isinstance(ready.get(key), str)
                    for key in ("fingerprint", "integrity")):
                return False
            return (fingerprint in {ready.get("fingerprint"), cls.installed_identity(prefix)} and (prefix / "bin/python").is_file()
                    and ready.get("integrity") == cls.integrity(prefix))
        except (OSError, ValueError, KeyError, RecursionError):
            return False

    async def _command(self, argv, workspace, env, writes, reads=(), timeout=1800, *, conda_manifest=False, machine=False):
        process = await self.sandbox.spawn(argv, workspace, env, writes, list(reads))
        try:
            async with asyncio.timeout(timeout):
                if machine:
                    stdout, stderr = await capture_output(process)
                else:
                    stdout, stderr, logs = await capture_diagnostics(process, self.resources, ArtifactStore(self.root),
                        [v for k, v in env.items() if SECRET.search(k)] + sensitive_values(workspace, self.config.load().sensitive_paths))
                    run = self.store.current(unfinished=True)
                    operation = current_diagnostics.get()
                    if operation:
                        operation.capture(logs)
                    elif run:
                        self.store.checkpoint(run["id"], "environment/logs/" + identifier("command"),
                                              {"logs": logs, "exit_code": process.returncode})
            if process.returncode:
                diagnostic = scrub_text((stdout + b"\n" + stderr).decode(errors="replace"),
                                        [v for k, v in env.items() if SECRET.search(k)] + sensitive_values(workspace, self.config.load().sensitive_paths))
                # Only explicit solver failures from a workspace manifest are repairable
                # proposals. Network, permissions, locks and configured defaults stay blocked.
                resolution = re.search(r"(?m)^(?:ResolvePackageNotFound|PackagesNotFoundError|"
                                       r"UnsatisfiableError|LibMambaUnsatisfiableError):[^\n]*", diagnostic)
                failure = DependencyResolutionError if conda_manifest and resolution else AA2Error
                if not resolution and re.search(r"CondaHTTPError|ConnectionError|ConnectionResetError|ReadTimeout|ETIMEDOUT|ECONNRESET|EAI_AGAIN", diagnostic):
                    failure = RetryableInfrastructureError
                detail = diagnostic[-2000:]
                if failure is DependencyResolutionError and resolution.group() not in detail:
                    detail = resolution.group() + "\n" + detail
                raise failure("Dependency preparation failed: " + detail)
            return stdout.decode(errors="replace")
        except TimeoutError as exc:
            raise RetryableInfrastructureError("Dependency preparation timed out") from exc
        finally:
            await self.sandbox.stop(process)

    async def _prepare(self, prefix, ready, fingerprint, profile, conda_file, pip_file, workspace, dependencies, local, inputs=None):
        with DiagnosticOperation(self.store, self.resources, "prepare/" + str(prefix)):
            cfg = self.config.load().environment
            from .resolution import load_resolution, save_resolution
            records = self.root / "environment-records" / fingerprint
            resolution_record = load_resolution(records) if not local else None
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
                replay_lock = resolution_record is not None
                saved_pip, saved_conda = scratch / "saved-pip.lock", scratch / "saved-conda.lock"
                if replay_lock:
                    atomic_write(saved_pip, resolution_record["pip"])
                    atomic_write(saved_conda, resolution_record["conda"])
                if replay_lock:
                    argv = [conda, "create", "-y", "--copy", "-p", str(prefix), "--file", str(saved_conda)]
                elif conda_file:
                    argv = [conda, "env", "create", "-p", str(prefix), "-f", str(conda_file)]
                else:
                    argv = [conda, "create", "-y", "--copy", "-p", str(prefix), f"python={cfg.python_version}", "pip", "pytest"]
                    if profile in {"node", "python-node", "react", "next", "python-react", "python-next"}:
                        argv += [f"nodejs={cfg.node_version}", "--channel", "conda-forge"]
                reads = [Path(conda).resolve().parent.parent]
                if conda_file:
                    reads.append(conda_file.parent)
                if records.exists():
                    reads.append(records)
                await self._command(argv, workspace, env, [prefix, cache, scratch], reads,
                                    conda_manifest=bool(conda_file) and not replay_lock)
                install_workspace = workspace
                if local:
                    # Editable bindings live in this private environment and never point into another task.
                    install_workspace = prefix / "aa2-source"
                    copy_execution_source(workspace, install_workspace, inputs)
                if dependencies and pip_file is None:
                    pip_file = scratch / "dependencies.txt"
                    atomic_write(pip_file, "\n".join(dependencies) + "\n")
                if pip_file:
                    if replay_lock and not saved_pip.is_file():
                        raise AA2Error("Recorded pip resolution is missing; preserving the previous environment identity")
                    constraints = ["-c", str(saved_pip)] if replay_lock else []
                    await self._command([str(prefix / "bin/python"), "-m", "pip", "install", "-r", str(pip_file), *constraints],
                                        install_workspace, env, [prefix, scratch], [prefix, pip_file.parent, *reads])
                await self._command([str(prefix / "bin/python"), "-m", "pip", "check"],
                                    workspace, env, [scratch], [prefix], timeout=60)
                identity = await self._command([str(prefix / "bin/python"), "-c", "import sys; print(sys.prefix)"],
                                               workspace, env, [scratch], [prefix], timeout=30, machine=True)
                if Path(identity.strip()).resolve() != prefix.resolve():
                    raise AA2Error("Environment interpreter identity mismatch")
                frozen = await self._command([str(prefix / "bin/python"), "-m", "pip", "freeze"], workspace, env, [scratch], [prefix], timeout=30, machine=True)
                explicit = await self._command([conda, "list", "--explicit", "-p", str(prefix)], workspace, env,
                                               [scratch], [prefix, *reads], timeout=30, machine=True)
                if replay_lock:
                    def resolution(text):
                        return sorted(line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#"))
                    if resolution(frozen) != resolution(resolution_record["pip"]):
                        raise AA2Error("Reconstructed pip resolution differs from the saved lock; previous records retained")
                if replay_lock and resolution(explicit) != resolution(resolution_record["conda"]):
                    raise AA2Error("Reconstructed Conda resolution differs from the saved generation")
                atomic_write(prefix / "aa2-pip.lock", frozen)
                atomic_write(prefix / "aa2-conda.lock", explicit)
                if not local and not replay_lock:
                    save_resolution(records, frozen, explicit)
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
            dependency_inputs = node_dependency_inputs(workspace)
            from .node_inputs import lock_inputs
            lock = workspace / "package-lock.json"
            with self.resources.temporary("node") as scratch:
                variables = environment.variables(scratch)
                variables["npm_config_cache"] = str(package_cache)
                if not lock.exists():
                    if frozen:
                        raise AA2Error("发布版本缺少 package-lock.json，必须在隔离工作区补齐并重新验收")
                    staging = scratch / "resolve"
                    staging.mkdir()
                    copy_execution_source(workspace, staging)
                    await self._command([npm, "install", "--package-lock-only", "--ignore-scripts"], staging,
                                        variables, [scratch, package_cache], [environment.prefix], timeout=900)
                    lock_inputs(workspace, staging / "package-lock.json")
                    atomic_write(lock, (staging / "package-lock.json").read_bytes(), 0o644)
                dependency_inputs += lock_inputs(workspace, lock)
                locked_inputs = digest([dependency_inputs, lock.read_bytes().hex()])
                modules = workspace / "node_modules"
                marker = self.root / "node-records" / (digest(str(workspace)) + ".json")
                identity = digest(["node-install-v3", environment.fingerprint, dependency_inputs,
                                   self.config.load().environment.playwright_browsers,
                                   (workspace / "package.json").read_text(), lock.read_text()])
                try:
                    installed = json.loads(marker.read_text())
                except (OSError, ValueError):
                    installed = {}
                if not isinstance(installed, dict):
                    installed = {}
                actual = digest(input_entry(modules, modules, excluded=RUNTIME_CACHES)) if modules.exists() else ""
                if installed.get("fingerprint") == identity and installed.get("integrity") == actual:
                    if not frozen:
                        await self.prepare_generated_types(workspace, environment)
                    return
                await self._command([npm, "ci"], workspace, variables, [modules, scratch, package_cache], [environment.prefix], timeout=900)
                if digest([node_dependency_inputs(workspace) + lock_inputs(workspace, lock), lock.read_bytes().hex()]) != locked_inputs:
                    raise AA2Error("Npm installation inputs changed; installed files retained for reconciliation")
                await self.prepare_node_browsers(workspace, environment)
                write_json(marker, {"fingerprint": identity, "integrity": digest(input_entry(modules, modules, excluded=RUNTIME_CACHES))})
                if not frozen:
                    await self.prepare_generated_types(workspace, environment)

    async def prepare_node_browsers(self, workspace: Path, environment: Environment):
        """Install browsers with the locked project's CLI, inside its node_modules.

        Installation precedes the Node readiness marker. Browser binaries are part
        of the same immutable execution-input identity as the installed packages.
        """
        cli = workspace / "node_modules/playwright/cli.js"
        if not cli.is_file():
            return
        with self.resources.temporary("node-browsers") as scratch:
            await self._command([str(environment.prefix / "bin/node"), str(cli), "install",
                                 *self.config.load().environment.playwright_browsers], workspace,
                                environment.variables(scratch), [workspace / "node_modules", scratch],
                                [environment.prefix], timeout=900)

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
