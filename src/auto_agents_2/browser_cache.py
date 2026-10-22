"""Exact-driver browser preparation and immutable cache publication."""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
from pathlib import Path

from .errors import PolicyViolation, ToolchainUnavailable
from .shared_cache import SharedCache, manifest_tree, read_json
from .util import async_file_lock, digest, write_json
from .workspace import input_entry


def controller_driver():
    spec = importlib.util.find_spec("playwright")
    if not spec or not spec.origin:
        raise ToolchainUnavailable("controller Playwright", "missing", "在已选 aa2 环境安装其 browser 依赖，保留当前运行")
    driver = Path(spec.origin).parent / "driver"
    node, package = driver / "node", driver / "package"
    if not node.is_file() or not (package / "browsers.json").is_file():
        raise ToolchainUnavailable("controller Playwright", "incomplete", "恢复该环境中完整的 Playwright 驱动")
    return node, package, package / "cli.js"


def driver_identity(package, families, environment):
    try:
        metadata = read_json(package / "package.json")
        browsers = read_json(package / "browsers.json")
        if not isinstance(metadata["version"], str) or not isinstance(browsers["browsers"], list):
            raise ValueError("Invalid driver metadata")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolchainUnavailable("Playwright driver", "metadata_unavailable", "恢复锁定驱动的完整 package.json/browsers.json") from exc
    return {"version": 1, "driver_version": metadata["version"], "browsers": browsers,
            "driver_hash": digest(input_entry(package, package, excluded={"node_modules", ".local-browsers", "__pycache__"})),
            "families": sorted(set(families)), "platform": platform.system(), "architecture": platform.machine(),
            "libc": list(platform.libc_ver()),
            "os_release": digest(Path("/etc/os-release").read_bytes()) if Path("/etc/os-release").is_file() else "",
            "download_configuration_hash": digest({key: value for key, value in environment.items()
                if key.startswith("PLAYWRIGHT_") and key.endswith("DOWNLOAD_HOST")})}


def expected_directories(output, root):
    """Read one stable CLI label, not localized titles or guessed revision suffixes."""
    paths = re.findall(r"(?m)^\s*Install location:\s*(.+?)\s*$", output)
    if not paths:
        raise ToolchainUnavailable("Playwright driver", "unsupported_metadata", "该驱动未提供可核对的安装位置；保留现场")
    names = set()
    for value in paths:
        path = Path(value)
        if path.parent != root or not re.fullmatch(r"[a-z][a-z0-9_-]*-\d+", path.name):
            raise PolicyViolation("Browser installation metadata escaped the selected cache directory")
        names.add(path.name)
    return sorted(names)


def validate_installation(root, expected):
    actual = {path.name for path in root.iterdir() if path.name != ".links"}
    if actual != set(expected):
        raise PolicyViolation("Browser install produced missing or unexpected components")
    for name in expected:
        directory = root / name
        if directory.is_symlink() or not (directory / "INSTALLATION_COMPLETE").is_file():
            raise PolicyViolation("Browser install did not complete every expected component")
        entries = manifest_tree(directory)
        if not any(row[1] == "file" and row[2] and row[3] for row in entries):
            raise PolicyViolation("Browser component has no executable content")
    # .links is package-client GC state, not browser content. The shared object is
    # owned by our explicit references; the installer never runs against it again.
    links = root / ".links"
    if links.is_symlink():
        raise PolicyViolation("Unexpected link in browser installer client metadata")
    if links.exists():
        shutil.rmtree(links)


class BrowserCache:
    def __init__(self, manager):
        self.manager, self.cache = manager, SharedCache(manager.config)

    async def acquire(self, workspace, node, package, cli, families, environment, consumer, *, runtime):
        manager, cache = self.manager, self.cache
        spec = driver_identity(package, families, environment)
        cache.initialize()
        run = manager.store.current(unfinished=True)
        rid = run["id"] if run else None
        async with async_file_lock(cache.root / "locks" / ("browser-" + digest(spec) + ".lock")):
            record = cache.lookup(spec, consumer, rid)
            if record:
                self.event(rid, record)
                return record
            with manager.resources.temporary("shared-browser-install") as scratch:
                install = scratch / "browser"
                install.mkdir()
                variables = {**environment, "PLAYWRIGHT_BROWSERS_PATH": str(install), "TMPDIR": str(scratch)}
                reads = [runtime, package]
                try:
                    raw = await manager._command([str(node), str(cli), "install", "--dry-run", *families],
                        workspace, variables, [scratch], reads, timeout=60, machine=True)
                    expected = expected_directories(raw, install)
                    await manager._command([str(node), str(cli), "install", *families],
                        workspace, variables, [scratch], reads, timeout=900)
                    validate_installation(install, expected)
                    # The public API resolves the exact configured family's executable.
                    probe = ("const p=require(process.argv[1]); console.log(JSON.stringify("
                             "process.argv.slice(2).map(n=>p[n].executablePath())))")
                    raw = await manager._command([str(node), "-e", probe, str(package), *families],
                        workspace, variables, [scratch], reads, timeout=60, machine=True)
                    executables = json.loads(raw)
                    if len(executables) != len(families):
                        raise PolicyViolation("Browser driver executable list is incomplete")
                    for executable in executables:
                        file = Path(executable)
                        if (not file.is_file() or not file.resolve().is_relative_to(install.resolve())
                                or file.relative_to(install).parts[0] not in expected):
                            raise PolicyViolation("The installed browser does not match this driver")
                    if driver_identity(package, families, environment) != spec:
                        raise PolicyViolation("Browser driver changed during installation")
                    record = cache.publish(spec, install, consumer, rid)
                except BaseException:
                    hold = manager.resources.hold(scratch, "unpublished browser installation", rid)
                    if rid:
                        manager.store.checkpoint(rid, "environment/browser-failures/" + scratch.name,
                                                 {"scratch": str(scratch), "hold": hold, "spec_key": digest(spec)})
                    raise
            self.event(rid, record)
            return record

    def event(self, rid, record):
        self.manager.store.event(rid, "cache.browser", {"hit": record["hit"], "path": record["path"],
            "object_id": record["object_id"], "content_hash": record["content_hash"],
            "spec_key": record["spec_key"], "driver_version": record["spec"]["driver_version"],
            "logical_bytes": record["logical_bytes"], "families": record["spec"]["families"]})

    async def node(self, workspace, environment):
        cli = workspace / "node_modules/playwright/cli.js"
        package = workspace / "node_modules/playwright-core"
        if not cli.is_file():
            return
        alias = package / ".local-browsers"
        # npm ci creates a private fresh tree. Never delete or import an older
        # hermetic browser directory just because sharing was enabled later.
        if alias.exists() and not alias.is_symlink():
            raise ToolchainUnavailable("project browser cache", "existing_local_install",
                "保留原浏览器目录；在新隔离安装树准备共享依赖，不自动迁移既有项目")
        with self.manager.resources.temporary("browser-variables") as scratch:
            record = await self.acquire(workspace, environment.prefix / "bin/node", package, cli,
                self.manager.config.load().environment.playwright_browsers, environment.variables(scratch), alias,
                runtime=environment.prefix)
        if alias.is_symlink():
            previous = self.manager.config.project / ".aa2/browser-bindings" / (digest(str(workspace)) + ".json")
            binding = read_json(previous)
            if binding.get("workspace") != str(workspace) or binding.get("path") != str(alias.resolve()):
                raise PolicyViolation("Refusing to replace an unregistered browser link")
            alias.unlink()
        temporary = alias.with_name(".aa2-browser-link")
        if temporary.exists() or temporary.is_symlink():
            raise PolicyViolation("An unfinished browser link publication must be reconciled")
        temporary.symlink_to(record["path"], target_is_directory=True)
        os.replace(temporary, alias)
        self.cache.save_binding(workspace, record)
        run = self.manager.store.current(unfinished=True)
        self.cache.activate(record, alias, run["id"] if run else None)

    async def prototype(self, workspace):
        node, package, cli = controller_driver()
        consumer = self.manager.root / "browser-bindings/controller.json"
        record = await self.acquire(workspace, node, package, cli, ["chromium"], dict(os.environ), consumer, runtime=node.parent)
        write_json(consumer, {key: record[key] for key in ("object_id", "path", "content_hash", "spec_key", "spec")})
        run = self.manager.store.current(unfinished=True)
        self.cache.activate(record, consumer, run["id"] if run else None)
        return record

    def prototype_ready(self):
        _, package, _ = controller_driver()
        spec = driver_identity(package, ["chromium"], dict(os.environ))
        consumer = self.manager.root / "browser-bindings/controller.json"
        run = self.manager.store.current(unfinished=True)
        rid = run["id"] if run else None
        record = self.cache.lookup(spec, consumer, rid)
        if record:
            write_json(consumer, {key: record[key] for key in ("object_id", "path", "content_hash", "spec_key", "spec")})
            self.cache.activate(record, consumer, rid)
            self.event(rid, record)
        return record

    def current_identity(self):
        """Read-only comparison; acquiring a live reference is a separate operation."""
        _, package, _ = controller_driver()
        spec = driver_identity(package, ["chromium"], dict(os.environ))
        path = self.manager.root / "browser-bindings/controller.json"
        if not path.is_file():
            return {"spec_key": digest(spec), "available": False}
        binding = read_json(path)
        record = self.cache.object(binding["object_id"])
        if any(binding[key] != record[key] for key in ("path", "content_hash", "spec_key")):
            raise PolicyViolation("Controller browser binding changed")
        return {"spec_key": digest(spec), "available": record["spec_key"] == digest(spec),
                "content_hash": record["content_hash"], "driver_version": spec["driver_version"]}
