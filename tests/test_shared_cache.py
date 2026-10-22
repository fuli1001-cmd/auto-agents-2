import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from test_runtime import configured

from auto_agents_2.browser_cache import expected_directories, validate_installation
from auto_agents_2.config import ConfigStore
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import PolicyViolation, ToolchainUnavailable
from auto_agents_2.evidence import input_identity
from auto_agents_2.models import Command
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.shared_cache import SharedCache
from auto_agents_2.util import digest, write_json


def project(root, cache_root):
    root.mkdir(parents=True)
    config, store = configured(root)
    body = config.load().model_dump(mode="json")
    body["shared_cache"] = {"enabled": True, "root": str(cache_root)}
    write_json(config.path, body)
    rid = store.admit("fixture://shared-cache", {})["id"]
    return config, store, rid


def browser_source(root, value="original"):
    root.mkdir(parents=True)
    component = root / "chromium-123"
    component.mkdir()
    (component / "INSTALLATION_COMPLETE").touch()
    binary = component / "chrome"
    binary.write_text(value)
    binary.chmod(0o755)
    (component / "empty").mkdir()
    return root


def published(tmp_path):
    config, store, rid = project(tmp_path / "project", tmp_path / "shared")
    cache = SharedCache(config)
    consumer = config.project / "workspace/browser"
    consumer.parent.mkdir()
    source = browser_source(config.project / "staging")
    record = cache.publish({"driver": "fixture"}, source, consumer, rid)
    consumer.symlink_to(record["path"], target_is_directory=True)
    cache.activate(record, consumer, rid)
    return cache, store, rid, consumer, record


def fake_installer(root, shared, *, version="1.55.1"):
    config, store, rid = project(root, shared)
    work = root / "frontend"
    package = work / "node_modules/playwright-core"
    package.mkdir(parents=True)
    write_json(package / "package.json", {"name": "playwright-core", "version": version})
    write_json(package / "browsers.json", {"browsers": [{"name": "chromium", "revision": "123"}]})
    cli = work / "node_modules/playwright/cli.js"
    cli.parent.mkdir()
    cli.write_text("fixture locked driver")
    write_json(work / "package.json", {"devDependencies": {"@playwright/test": version}})
    write_json(work / "package-lock.json", {"lockfileVersion": 3})
    prefix = root / "runtime"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/npm").touch()
    (prefix / "bin/node").touch()
    environment = Environment(prefix, "fixture-runtime")
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    calls = []

    async def command(argv, workspace, env, writes, reads=(), **kwargs):
        calls.append((argv, env.copy(), kwargs))
        if argv[1] == "ci":
            return "installed private node_modules"
        install = Path(env["PLAYWRIGHT_BROWSERS_PATH"])
        if "--dry-run" in argv:
            return "browser: chromium\n  Install location: " + str(install / "chromium-123") + "\n"
        if "-e" in argv:
            return json.dumps([str(install / "chromium-123/chrome")])
        browser_source(install / "payload")
        (install / "payload/chromium-123").rename(install / "chromium-123")
        (install / "payload").rmdir()
        (install / ".links").mkdir()
        (install / ".links/client").write_text(str(package))
        await asyncio.sleep(0)
        return "download completed"

    manager._command = command
    return manager, environment, work, calls, command


def test_legacy_projects_do_not_enable_or_create_shared_cache(tmp_path):
    config = ConfigStore(tmp_path)
    config.initialize()
    cache = SharedCache(config)
    assert not cache.enabled
    assert config.load().shared_cache.root == ""


@pytest.mark.parametrize("unsafe", ["symlink", "writable_by_others"])
def test_unsafe_cache_root_is_rejected(tmp_path, unsafe):
    config, _, _ = project(tmp_path / "project", tmp_path / "shared")
    if unsafe == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / "shared").symlink_to(target, target_is_directory=True)
    else:
        (tmp_path / "shared").mkdir(mode=0o777)
        (tmp_path / "shared").chmod(0o777)
    with pytest.raises(PolicyViolation):
        SharedCache(config).initialize()


def test_full_manifest_detects_changed_non_executable_and_missing_empty_directory(tmp_path):
    cache, _, _, _, record = published(tmp_path)
    assert cache.object(record["object_id"])["content_hash"] == record["content_hash"]
    empty = Path(record["path"]) / "chromium-123/empty"
    empty.parent.chmod(0o755)
    empty.rmdir()
    empty.parent.chmod(0o555)
    with pytest.raises(PolicyViolation, match="content changed"):
        cache.object(record["object_id"])


def test_escape_links_are_not_published(tmp_path):
    config, _, rid = project(tmp_path / "project", tmp_path / "shared")
    source = browser_source(config.project / "staging")
    (source / "outside").symlink_to(tmp_path)
    with pytest.raises(PolicyViolation, match="escapes"):
        SharedCache(config).publish({}, source, config.project / "consumer", rid)
    assert not list((tmp_path / "shared/objects").iterdir())


async def test_two_projects_share_exact_browser_without_second_installer(tmp_path):
    first = fake_installer(tmp_path / "one", tmp_path / "shared")
    second = fake_installer(tmp_path / "two", tmp_path / "shared")
    for manager, environment, work, _, _ in (first, second):
        await manager.install_node(work, environment)
    one = first[2] / "node_modules/playwright-core/.local-browsers"
    two = second[2] / "node_modules/playwright-core/.local-browsers"
    assert one.is_symlink() and one.resolve() == two.resolve()
    assert sum("install" in call[0] and "--dry-run" not in call[0] for call in first[3]) == 1
    assert len(second[3]) == 1 and second[3][0][0][1] == "ci"
    assert first[3][0][1]["npm_config_cache"] == second[3][0][1]["npm_config_cache"]
    assert (first[2] / "node_modules").resolve() != (second[2] / "node_modules").resolve()
    before = input_identity(first[2], Command(argv=["npm", "test"]))
    await first[0].install_node(first[2], first[1])
    assert len(first[3]) == 4
    assert input_identity(first[2], Command(argv=["npm", "test"])) == before
    assert not (one.resolve() / ".links").exists()


async def test_concurrent_identical_install_publishes_once(tmp_path):
    one = fake_installer(tmp_path / "one", tmp_path / "shared")
    two = fake_installer(tmp_path / "two", tmp_path / "shared")
    await asyncio.gather(one[0].prepare_node_browsers(one[2], one[1]), two[0].prepare_node_browsers(two[2], two[1]))
    assert sum("install" in c[0] and "--dry-run" not in c[0] for c in one[3] + two[3]) == 1
    assert len(list((tmp_path / "shared/objects").iterdir())) == 1


async def test_different_drivers_do_not_share_even_with_same_revision_number(tmp_path):
    one = fake_installer(tmp_path / "one", tmp_path / "shared", version="1.55.1")
    two = fake_installer(tmp_path / "two", tmp_path / "shared", version="1.58.2")
    await one[0].prepare_node_browsers(one[2], one[1])
    await two[0].prepare_node_browsers(two[2], two[1])
    assert len(list((tmp_path / "shared/objects").iterdir())) == 2
    assert len(one[3]) == len(two[3]) == 3


async def test_corrupt_object_is_preserved_and_new_checks_get_new_identity(tmp_path):
    manager, environment, work, calls, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    await manager.prepare_node_browsers(work, environment)
    alias = work / "node_modules/playwright-core/.local-browsers"
    old = alias.resolve()
    before = input_identity(work, Command(argv=["npm", "test"]))
    binary = old / "chromium-123/chrome"
    binary.chmod(0o755)
    binary.write_text("corrupt")
    assert input_identity(work, Command(argv=["npm", "test"])) != before
    with pytest.raises(PolicyViolation):
        manager.shared.browser_reads(work)
    await manager.prepare_node_browsers(work, environment)
    assert alias.resolve() != old and binary.read_text() == "corrupt"
    assert len(calls) == 6


async def test_partial_install_is_not_published_or_marked_ready(tmp_path):
    manager, environment, work, calls, command = fake_installer(tmp_path / "one", tmp_path / "shared")
    async def incomplete(argv, *args, **kwargs):
        value = await command(argv, *args, **kwargs)
        if "install" in argv and "--dry-run" not in argv:
            install = Path(args[1]["PLAYWRIGHT_BROWSERS_PATH"])
            (install / "chromium-123/INSTALLATION_COMPLETE").unlink()
        return value
    manager._command = incomplete
    with pytest.raises(PolicyViolation, match="complete"):
        await manager.install_node(work, environment)
    assert not list((tmp_path / "shared/objects").iterdir())
    assert not (manager.root / "node-records" / (digest(str(work)) + ".json")).exists()
    with manager.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_holds").fetchone()[0] > 0


async def test_existing_local_browser_is_not_moved_or_deleted(tmp_path):
    manager, environment, work, calls, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    local = work / "node_modules/playwright-core/.local-browsers"
    local.mkdir()
    (local / "keep").write_text("user installation")
    with pytest.raises(ToolchainUnavailable, match="existing_local_install"):
        await manager.prepare_node_browsers(work, environment)
    assert (local / "keep").read_text() == "user installation" and not calls


def test_pending_publication_reference_cannot_expire(tmp_path):
    cache, store, rid, consumer, record = published(tmp_path)
    consumer.unlink()
    store.update(rid, status="SUCCEEDED")
    with __import__('auto_agents_2.util', fromlist=['file_lock']).file_lock(cache.root / "locks/control.lock"):
        cache.reference(record, consumer, rid)
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]
    cache.activate(record, consumer, rid)
    assert cache.collect(dry_run=False, ttl_seconds=-1)["removed"]


@pytest.mark.parametrize("status", ["RUNNING", "PAUSED", "WAITING_USER", "CANCELLED"])
def test_unfinished_or_preserved_run_protects_browser_without_live_alias(tmp_path, status):
    cache, store, rid, consumer, _ = published(tmp_path)
    consumer.unlink()
    store.update(rid, status=status)
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]


def test_completed_delivery_alias_and_other_project_reference_survive_gc(tmp_path):
    cache, store, rid, consumer, record = published(tmp_path)
    store.update(rid, status="SUCCEEDED")
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]
    other_config, other_store, other_rid = project(tmp_path / "other", cache.root)
    other = SharedCache(other_config)
    other_consumer = other_config.project / "browser"
    reused = other.lookup(record["spec"], other_consumer, other_rid)
    assert reused["object_id"] == record["object_id"]
    other.activate(reused, other_consumer, other_rid)
    consumer.unlink()
    other_store.update(other_rid, status="PAUSED")
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]
    other_store.update(other_rid, status="SUCCEEDED")
    assert cache.collect(dry_run=True, ttl_seconds=-1)["eligible"]
    assert Path(record["path"]).exists()
    assert cache.collect(dry_run=False, ttl_seconds=-1)["removed"]


def test_unknown_reference_or_process_cleanup_protects_objects(tmp_path):
    cache, store, rid, consumer, _ = published(tmp_path)
    consumer.unlink()
    store.update(rid, status="SUCCEEDED")
    process = store.root / "processes/uncertain.json"
    process.parent.mkdir(exist_ok=True)
    process.write_text("unconfirmed")
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]
    process.unlink()
    (cache.root / "references/unknown.json").write_text("damaged reference")
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]


async def test_download_gc_cannot_race_installer_and_cancel_releases_lock(tmp_path):
    config, _, _ = project(tmp_path / "project", tmp_path / "shared")
    cache = SharedCache(config)
    async with cache.downloads(["npm"]) as paths:
        (paths["npm"] / "package").write_text("download")
        assert cache.collect(dry_run=False, ttl_seconds=-1)["busy"] == ["npm"]
    assert str(paths["npm"]) in cache.collect(dry_run=False, ttl_seconds=-1)["removed"]


def test_sandbox_rejects_writable_browser_or_arbitrary_shared_path(tmp_path):
    cache, store, _, _, record = published(tmp_path)
    runner = SandboxRunner(store.root)
    with pytest.raises(PolicyViolation, match="Only registered"):
        runner.command([sys.executable, "--version"], cache.config.project, [], cache_writes=[Path(record["path"])])
    with pytest.raises(PolicyViolation):
        runner.command([sys.executable, "--version"], cache.config.project, [], cache_writes=[tmp_path])


async def test_actual_sandbox_reads_selected_browser_but_cannot_write_or_read_cache_metadata(tmp_path):
    manager, environment, work, _, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    await manager.prepare_node_browsers(work, environment)
    runner = SandboxRunner(manager.root)
    browser = work / "node_modules/playwright-core/.local-browsers/chromium-123/chrome"
    cache_index = next((manager.shared.root / "index").glob("browser-*.json"))
    script = """import pathlib,sys
p=pathlib.Path(sys.argv[1])
assert p.read_text()=='original'
try: p.write_text('poisoned')
except OSError: pass
else: raise RuntimeError('browser writable')
assert not pathlib.Path(sys.argv[2]).exists()
print('readonly isolation passed')
"""
    process = await runner.spawn([sys.executable, "-c", script, str(browser), str(cache_index)], work, dict(os.environ), [], runner.execution_reads(work))
    try:
        from auto_agents_2.progress import capture_output
        stdout, stderr = await capture_output(process)
        assert process.returncode == 0, stderr
        assert b"readonly isolation passed" in stdout
    finally:
        await runner.stop(process)


@pytest.mark.parametrize("output", ["no metadata", "Install location: /outside/chromium-123"])
def test_unknown_or_escaping_driver_metadata_cannot_authorize_install(tmp_path, output):
    with pytest.raises((ToolchainUnavailable, PolicyViolation)):
        expected_directories(output, tmp_path)


def test_extra_installation_component_is_rejected(tmp_path):
    source = browser_source(tmp_path / "browser")
    (source / "unexpected").mkdir()
    with pytest.raises(PolicyViolation, match="unexpected"):
        validate_installation(source, ["chromium-123"])


@pytest.mark.parametrize("location", ["inside", "ancestor", "unmanaged", "wrong_marker"])
def test_shared_cache_never_adopts_project_or_unmanaged_content(tmp_path, location):
    root = (tmp_path / "project/cache" if location == "inside" else tmp_path if location == "ancestor"
            else tmp_path / "shared")
    config, _, _ = project(tmp_path / "project", root)
    if location in {"unmanaged", "wrong_marker"}:
        root.mkdir(mode=0o700)
        (root / ("important.txt" if location == "unmanaged" else "cache.json")).write_text("{}")
    with pytest.raises(PolicyViolation):
        SharedCache(config).initialize()
    if location in {"unmanaged", "wrong_marker"}:
        assert not (root / "objects").exists()


def test_disabled_custom_cache_path_is_not_adopted_or_validated(tmp_path):
    config = ConfigStore(tmp_path)
    config.initialize()
    settings = config.load().model_dump(mode="json")
    settings["shared_cache"] = {"enabled": False, "root": "unused-relative-path"}
    write_json(config.path, settings)
    cache = SharedCache(config)
    assert not cache.enabled
    with pytest.raises(PolicyViolation):
        cache.initialize()
