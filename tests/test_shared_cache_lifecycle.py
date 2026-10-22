import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_prototype_approvals import approval_fixture
from test_shared_cache import browser_source, fake_installer, project, published

from auto_agents_2.browser_cache import BrowserCache
from auto_agents_2.errors import PolicyViolation
from auto_agents_2.shared_cache import SharedCache
from auto_agents_2.util import write_json


async def test_cancelled_download_owner_releases_its_lock(tmp_path):
    config, _, _ = project(tmp_path / "project", tmp_path / "shared")
    cache = SharedCache(config)
    started = asyncio.Event()
    async def owner():
        async with cache.downloads(["npm"]):
            started.set()
            await asyncio.Future()
    task = asyncio.create_task(owner())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(2):
        async with cache.downloads(["npm"]) as paths:
            assert paths["npm"].is_dir()


def test_real_processes_serialize_same_browser_publication(tmp_path):
    tests = Path(__file__).parent
    script = """import asyncio,json,sys
from pathlib import Path
from test_shared_cache import fake_installer
m,e,w,c,_=fake_installer(Path(sys.argv[1]),Path(sys.argv[2]))
asyncio.run(m.prepare_node_browsers(w,e))
print(json.dumps({'calls':len(c),'object':str((w/'node_modules/playwright-core/.local-browsers').resolve())}))
"""
    env = {**os.environ, "PYTHONPATH": str(tests), "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path / name), str(tmp_path / "shared")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("one", "two")]
    outputs = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            outputs.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert sorted(row["calls"] for row in outputs) == [0, 3]
    assert outputs[0]["object"] == outputs[1]["object"]


def test_interrupted_copy_preserves_staging_without_publishing_index(tmp_path, monkeypatch):
    config, _, rid = project(tmp_path / "project", tmp_path / "shared")
    cache = SharedCache(config)
    source = browser_source(config.project / "source")
    from auto_agents_2 import shared_cache
    copy = shared_cache.shutil.copytree
    def interrupted(*args, **kwargs):
        copy(*args, **kwargs)
        raise OSError("fixture interrupted before publication")
    monkeypatch.setattr(shared_cache.shutil, "copytree", interrupted)
    with pytest.raises(OSError):
        cache.publish({}, source, config.project / "consumer", rid)
    assert list((cache.root / "staging").iterdir())
    assert not list((cache.root / "objects").iterdir())
    assert not list((cache.root / "index").iterdir())


def test_permission_change_invalidates_shared_object_even_with_same_bytes(tmp_path):
    cache, _, _, _, record = published(tmp_path)
    (Path(record["path"]) / "chromium-123/chrome").chmod(0o755)
    with pytest.raises(PolicyViolation, match="immutable"):
        cache.object(record["object_id"])


async def test_cache_metadata_and_other_objects_are_not_granted_to_installers(tmp_path):
    manager, _, work, _, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    from auto_agents_2.progress import capture_output
    from auto_agents_2.sandbox import SandboxRunner
    runner = SandboxRunner(manager.root)
    async with manager.shared.downloads(["npm"]) as paths:
        script = """import pathlib,sys
p=pathlib.Path(sys.argv[1]); (p/'test-package').write_text('fixture download')
assert not pathlib.Path(sys.argv[2]).exists()
print('cache write confined')
"""
        process = await runner.spawn([sys.executable, "-c", script, str(paths["npm"]), str(manager.shared.root / "objects")],
            work, dict(os.environ), [], cache_writes=list(paths.values()))
        try:
            stdout, stderr = await capture_output(process)
            assert process.returncode == 0, stderr
            assert b"cache write confined" in stdout
        finally:
            await runner.stop(process)
        assert (paths["npm"] / "test-package").read_text() == "fixture download"


async def test_browser_environment_change_rechecks_automation_and_preserves_human_consent(tmp_path, monkeypatch):
    workflow, rid, document, question, contexts, browser_calls = approval_fixture(tmp_path, monkeypatch, legacy=False)
    settings = workflow.config.load().model_dump(mode="json")
    settings["shared_cache"] = {"enabled": True, "root": str(tmp_path.parent / (tmp_path.name + "-shared-cache"))}
    write_json(workflow.config.path, settings)
    workflow.environments.shared = SharedCache(workflow.config)
    identity = {"spec_key": "a" * 64, "content_hash": "b" * 64, "available": True, "driver_version": "fixture"}
    monkeypatch.setattr(BrowserCache, "current_identity", lambda self: dict(identity))
    first = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert len(browser_calls) == 1
    assert first["human"]["question_id"] == question["id"]
    again = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert len(browser_calls) == 1 and again == first
    identity["content_hash"] = "c" * 64
    renewed = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert len(browser_calls) == 2
    assert renewed["browser_version"] != first["browser_version"]
    assert renewed["human"] == first["human"]
    assert not workflow.store.pending(rid)
    assert sum(stage == "review" for stage, _ in contexts) == 2


async def test_registered_browser_facts_include_actual_driver_and_content(tmp_path):
    manager, environment, work, _, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    await manager.prepare_node_browsers(work, environment)
    facts = manager.shared.facts(work)
    assert len(facts) == 1 and facts[0]["driver_version"] == "1.55.1"
    assert facts[0]["components"] == ["chromium-123"]
    assert len(facts[0]["content_hash"]) == 64
    assert facts[0]["browser_manifest"]["browsers"][0]["revision"] == "123"


def test_controller_binding_survives_completed_run_until_replaced(tmp_path):
    cache, store, rid, consumer, record = published(tmp_path)
    consumer.unlink()
    controller = store.root / "browser-bindings/controller.json"
    cache.reference(record, controller, rid)
    write_json(controller, {"object_id": record["object_id"], "path": record["path"]})
    cache.activate(record, controller, rid)
    store.update(rid, status="SUCCEEDED")
    assert not cache.collect(dry_run=False, ttl_seconds=-1)["removed"]
    write_json(controller, {"object_id": "f" * 64})
    assert cache.collect(dry_run=False, ttl_seconds=-1)["removed"]


async def test_gc_before_download_lock_does_not_yield_a_removed_directory(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from auto_agents_2 import shared_cache
    config, _, _ = project(tmp_path / "project", tmp_path / "shared")
    cache = SharedCache(config)
    async with cache.downloads(["npm"]):
        pass
    original = shared_cache.async_file_lock
    @asynccontextmanager
    async def collect_before_acquiring(path):
        cache.collect(dry_run=False, ttl_seconds=-1)
        async with original(path):
            yield
    monkeypatch.setattr(shared_cache, "async_file_lock", collect_before_acquiring)
    async with cache.downloads(["npm"]) as paths:
        (paths["npm"] / "new-download").write_text("still writable after competing GC")


async def test_removed_browser_entry_grants_no_mount_and_requires_normal_preparation(tmp_path):
    from auto_agents_2.workspace import input_entry
    manager, environment, work, calls, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    await manager.install_node(work, environment)
    modules = work / "node_modules"
    before = input_entry(modules, modules)
    alias = modules / "playwright-core/.local-browsers"
    target = alias.resolve()
    alias.unlink()
    assert manager.shared.browser_reads(work) == []
    assert manager.shared.facts(work) == []
    assert input_entry(modules, modules) != before
    count = len(calls)
    await manager.install_node(work, environment)
    assert alias.resolve() == target and len(calls) == count + 1  # npm ci, browser object reused.
    assert manager.shared.browser_reads(work) == [target]


def test_only_enabled_shared_cache_changes_worker_invocation_identity(tmp_path):
    from auto_agents_2.evidence import invocation_identity
    from auto_agents_2.models import ImplementationResult
    config, _, _ = project(tmp_path / "project", tmp_path / "shared")
    cfg = config.load()
    cfg.shared_cache.enabled = False
    first = invocation_identity("implement", {"input": 1}, ImplementationResult, config.project, cfg, "source")
    cfg.shared_cache.root = "/unused/disabled-cache"
    assert invocation_identity("implement", {"input": 1}, ImplementationResult, config.project, cfg, "source") == first
    cfg.shared_cache.enabled = True
    assert invocation_identity("implement", {"input": 1}, ImplementationResult, config.project, cfg, "source") != first


async def test_browser_installer_can_read_selected_runtime_libraries_in_real_sandbox(tmp_path):
    from auto_agents_2.environment import EnvironmentManager
    from auto_agents_2.sandbox import SandboxRunner
    manager, environment, work, _, _ = fake_installer(tmp_path / "one", tmp_path / "shared")
    (environment.prefix / "lib").mkdir()
    (environment.prefix / "lib/runtime-dependency").write_text("selected runtime")
    node = environment.prefix / "bin/node"
    node.write_text("""#!/usr/bin/python3
import json,os,sys
from pathlib import Path
assert (Path(__file__).resolve().parents[1]/'lib/runtime-dependency').read_text()=='selected runtime'
root=Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])
component=root/'chromium-123'
if '--dry-run' in sys.argv:
    print('Install location: '+str(component))
elif '-e' in sys.argv:
    print(json.dumps([str(component/'chrome')]))
else:
    component.mkdir()
    (component/'INSTALLATION_COMPLETE').touch()
    (component/'chrome').write_text('fixture executable')
    (component/'chrome').chmod(0o755)
""")
    node.chmod(0o755)
    manager = EnvironmentManager(manager.config, SandboxRunner(manager.root))
    await manager.prepare_node_browsers(work, environment)
    assert len(manager.shared.browser_reads(work)) == 1
