import json
import os
import sys
from pathlib import Path

import manual_frontend as frontend
import manual_node_browser as node
import pytest
from manual_recovery_snapshot import snapshot, validate_copy
from test_ocr_manual_tools import paused_project

from auto_agents_2.environment import Environment
from auto_agents_2.errors import PrototypeBrowserError
from auto_agents_2.models import CheckResult
from auto_agents_2.store import Store
from auto_agents_2.workspace import git


def test_snapshot_sidecars_are_private_even_with_permissive_umask(tmp_path):
    source, store, _ = paused_project(tmp_path)
    original = store.path.read_bytes()
    previous = os.umask(0)
    try:
        result = snapshot(source, tmp_path / "copy")
    finally:
        os.umask(previous)
    for path in (Path(result["original_database"]), tmp_path / "copy-snapshot.json"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.path.read_bytes() == original


def test_snapshot_normalizes_parent_segments_before_rebinding(tmp_path):
    source, store, rid = paused_project(tmp_path)
    store.checkpoint(rid, "develop/task/T", {"workspace": str(source / ".." / "source")})
    result = snapshot(source, tmp_path / "copy")
    assert Store(Path(result["project"])).get(rid, "develop/task/T")["workspace"] == str(tmp_path / "copy")


@pytest.mark.parametrize("path", ["/outside/project", "relative/work"])
def test_snapshot_rejects_external_or_relative_mutable_locators(tmp_path, path):
    source, store, rid = paused_project(tmp_path)
    store.checkpoint(rid, "develop/task/T", {"workspace": path})
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="snapshot"):
        snapshot(source, tmp_path / "copy")
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("setting", ["[core]\nworktree=/outside/fixture\n", "[include]\npath=/outside/config\n"])
def test_main_worktree_configuration_indirection_rejected(tmp_path, setting):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "extensions.worktreeConfig", "true")
    (tmp_path / ".git/config.worktree").write_text(setting)
    with pytest.raises(ValueError, match="indirection"):
        validate_copy(tmp_path)


async def test_node_manual_copy_rejects_external_package_link(tmp_path, monkeypatch):
    modules = tmp_path / "installation/node_modules"
    modules.mkdir(parents=True)
    package = modules.parent / "packages/playwright"
    package.mkdir(parents=True)
    (package / "cli.js").write_text("fixture")
    (modules / "playwright").symlink_to("../packages/playwright")
    root = tmp_path / "smoke"
    root.mkdir()
    monkeypatch.setattr(node.tempfile, "mkdtemp", lambda **kw: str(root))
    with pytest.raises(ValueError, match="internal relative"):
        await node.main(Path(sys.prefix), modules)
    assert (package / "cli.js").read_text() == "fixture"
    assert Store(root).current()["status"] == "PAUSED"


@pytest.mark.parametrize("status_fails", [False, True])
async def test_manual_browser_failure_preserves_evidence_and_primary_error(tmp_path, monkeypatch, status_fails):
    root = tmp_path / "smoke"
    root.mkdir()
    monkeypatch.setattr(frontend.tempfile, "mkdtemp", lambda **kw: str(root))
    async def environment(*a, **kw):
        return Environment(Path(sys.prefix), "fixture")
    async def no_install(*a, **kw):
        pass
    async def checks(*a, **kw):
        return [CheckResult(argv=["build"], cwd=".", exit_code=0, process_exit_code=0, duration=0,
                            source_hash="fixture", environment_hash="fixture", output="")]
    rows = [{"errors": ["specific action failed"]}]
    async def browser(*a, **kw):
        raise PrototypeBrowserError("primary browser failure", rows)
    monkeypatch.setattr(frontend.EnvironmentManager, "ensure", environment)
    monkeypatch.setattr(frontend.EnvironmentManager, "install_node", no_install)
    monkeypatch.setattr(frontend.VerificationService, "run", checks)
    monkeypatch.setattr(frontend, "inspect_application", browser)
    if status_fails:
        original = frontend.Store.update
        def update(self, *a, **kw):
            if kw.get("outcome") == "DIAGNOSTIC_FAILED":
                raise OSError("secondary status failure")
            return original(self, *a, **kw)
        monkeypatch.setattr(frontend.Store, "update", update)
    with pytest.raises(PrototypeBrowserError, match="primary browser failure") as caught:
        await frontend.main()
    assert json.loads((root / "results.json").read_text())["react"]["browser"] == rows
    if status_fails:
        assert caught.value.__notes__
