from types import SimpleNamespace

import pytest
from test_runtime import configured

from auto_agents_2.config import Cleanup, validate_extra_args
from auto_agents_2.errors import AA2Error, ConfigurationError, VerificationPreparationError
from auto_agents_2.models import Command, PlanResult, TaskSpec
from auto_agents_2.planning import validate_plan
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.readview import public_files, sensitive_values
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import scrub_text, write_json
from auto_agents_2.workspace import WorkspaceManager, git, isolated_git


@pytest.mark.parametrize("args", [["--model", "--effort"], ["--effort"], ["--model", "--"], ["--model", "-m"]])
def test_provider_option_values_cannot_consume_another_option(args):
    with pytest.raises(ConfigurationError):
        validate_extra_args(args)
    validate_extra_args(["--model=-literal-model"])


def test_protected_glob_ancestors_and_numeric_values_only_affect_logs(tmp_path):
    private = tmp_path / "private-a"
    private.mkdir()
    (private / "config.json").write_text('{"pin":739182,"token":"fixture-private"}')
    (tmp_path / "ordinary.py").write_text("PIN=739182")
    assert public_files(tmp_path, ["private-?"]) == [tmp_path / "ordinary.py"]
    values = sensitive_values(tmp_path, ["private-?"])
    assert scrub_text("739182 fixture-private", values) == "<redacted> <redacted>"


@pytest.mark.parametrize("action", ["refresh", "prune"])
def test_native_session_parent_links_never_write_or_delete_outside(tmp_path, action):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    native = tmp_path / "native"
    write_json(native / "config/settings.json", {"fixture": "old"})
    transport = SimpleNamespace(native_home=lambda env: native, auth_patterns=("config/*.json",))
    router = ProviderRouter(config, store, SandboxRunner(store.root), transports={"fixture": transport})
    effective = {"environment": {}, "kind": "fixture", "provider": "fixture"}
    mirror = router._session_overlay(effective, rid, "op")[0][0]
    (mirror / "config/settings.json").unlink()
    (mirror / "config").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "settings.json"
    victim.write_text("preserve")
    (mirror / "config").symlink_to(outside, target_is_directory=True)
    if action == "refresh":
        write_json(native / "config/settings.json", {"fixture": "new"})
    else:
        (native / "config/settings.json").unlink()
    with pytest.raises((OSError, ValueError)):
        router._session_overlay(effective, rid, "op")
    assert victim.read_text() == "preserve"


@pytest.mark.parametrize("shape", [[], None, {"paths": []}, {"paths": "abc"}, {"paths": ["relative"]}])
def test_malformed_process_ownership_protects_state_root(tmp_path, shape):
    store = Store(tmp_path)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    path = store.root / "tmp/live"
    path.mkdir(parents=True)
    resources.register(path, "temporary", refs=0)
    if isinstance(shape, dict):
        shape = {**shape, "version": 3, "status": "running", "cwd": str(path)}
    write_json(store.root / "processes/fixture.json", shape)
    assert store.root in resources.process_paths()
    resources.collect()
    assert path.exists()


def test_git_operations_and_temporary_index_use_one_isolated_environment(tmp_path, monkeypatch):
    current, other = tmp_path / "current", tmp_path / "other"
    for path, value in ((current, "ours"), (other, "other")):
        path.mkdir()
        manager = WorkspaceManager(path)
        manager.initialize()
        (path / "file").write_text(value)
        manager.checkpoint(path, "refs/aa2/base", "base")
    foreign_index = (other / ".git/index").read_bytes()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git/index"))
    with isolated_git():
        manager = WorkspaceManager(current)
        intent = manager.checkpoint_intent(current, "refs/aa2/test")
        assert intent["tree"] == git(current, "rev-parse", "HEAD^{tree}")
        assert manager.file_entry("file")["oid"] == git(current, "rev-parse", "HEAD:file")
    assert (other / ".git/index").read_bytes() == foreign_index


def test_git_literal_filename_cannot_stage_excluded_secret(tmp_path):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / "base").write_text("base")
    manager.checkpoint(tmp_path, "refs/aa2/base", "base")
    (tmp_path / ".env").write_text("FIXTURE=placeholder")
    (tmp_path / ":(glob)*").write_text("literal")
    manager.checkpoint(tmp_path, "refs/aa2/next", "next")
    tracked = git(tmp_path, "ls-files", "-z").split("\0")
    assert ":(glob)*" in tracked and ".env" not in tracked


def test_checkpoint_subtree_deletion_is_literal_and_matches_archived_history(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    store.checkpoints(rid, {"a_b": 1, "a_b/child": 2, "axb/child": 3, "a%b/child": 4})
    store.invalidate(rid, ["a_b"])
    assert store.get(rid, "a_b/child") is None
    assert store.get(rid, "axb/child") == 3 and store.get(rid, "a%b/child") == 4
    store.checkpoints(rid, {"provider_research/own": 1, "providerXresearch/keep": 2})
    store.prepare_replan(rid, "provider_research", "fixture")
    store.complete_replan(rid, ["provider_research"], [])
    assert store.get(rid, "providerXresearch/keep") == 2
    history = store.get(rid, "recovery/gate-history/0")["checkpoints"]
    assert "provider_research/own" in history and "providerXresearch/keep" not in history


def test_long_plan_order_is_irrelevant_and_cycles_are_rejected():
    tasks = [TaskSpec(id=f"T{i}", title="x", description="x", requirement_ids=["R"], allowed_paths=["a"],
        acceptance=["a"], checks=[Command(argv=["true"])], depends_on=[f"T{i+1}"] if i < 1499 else []) for i in range(1500)]
    plan = PlanResult(summary="chain", document="chain", tasks=tasks, final_checks=[Command(argv=["true"])])
    validate_plan(plan, {"R"})
    validate_plan(plan.model_copy(update={"tasks": list(reversed(tasks))}), {"R"})
    tasks[-1].depends_on = ["T0"]
    with pytest.raises(AA2Error, match="cycle"):
        validate_plan(plan, {"R"})


def test_build_output_filesystem_errors_are_preparation_failures(tmp_path, monkeypatch):
    import auto_agents_2.builds as builds
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/file").write_text("fixture")
    def fail(*args, **kwargs):
        raise PermissionError("fixture")
    monkeypatch.setattr(builds, "generated_entry", fail)
    with pytest.raises(VerificationPreparationError) as caught:
        builds.output_hashes(Command(argv=["build"], outputs=["dist"]), tmp_path)
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_npm_lock_validates_transitive_local_paths_and_links(tmp_path, version):
    from auto_agents_2.node_inputs import lock_inputs
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside.tgz"
    outside.write_text("fixture")
    lock = work / "package-lock.json"
    entry = {"version": "1.0.0", "resolved": outside.as_uri()}
    write_json(lock, {"lockfileVersion": version, "dependencies": {"dep": entry}})
    with pytest.raises(AA2Error, match="inside"):
        lock_inputs(work, lock)
    (work / "linked").symlink_to(tmp_path)
    write_json(lock, {"lockfileVersion": version, "packages": {"node_modules/dep": {"link": True, "resolved": "linked"}}})
    with pytest.raises(AA2Error, match="inside"):
        lock_inputs(work, lock)
    (work / "local").mkdir()
    (work / "local/package.json").write_text('{"name":"local"}')
    write_json(lock, {"lockfileVersion": version, "packages": {"node_modules/dep": {"link": True, "resolved": "local"}}})
    assert lock_inputs(work, lock)[0][0] == "local"


async def test_npm_ci_is_never_dispatched_for_external_lock(tmp_path, monkeypatch):
    from auto_agents_2.environment import Environment, EnvironmentManager
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root))
    work = tmp_path / "work"
    work.mkdir()
    prefix = store.root / "environments/fixture"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/npm").write_text("fixture")
    write_json(work / "package.json", {"name": "fixture", "dependencies": {"dep": "1.0.0"}})
    write_json(work / "package-lock.json", {"lockfileVersion": 3, "packages": {
        "node_modules/dep": {"version": "1.0.0", "resolved": "file:/outside/fixture.tgz"}}})
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid lock reached npm execution")
    monkeypatch.setattr(manager, "_command", forbidden)
    with pytest.raises(AA2Error, match="inside"):
        await manager.install_node(work, Environment(prefix, "fixture"))


@pytest.mark.parametrize("changed", ["server/route-cache/APP_PAGE/fixture/index.html", "server/app/page.js"])
def test_next_runtime_cache_changes_do_not_hide_modified_server_code(tmp_path, changed):
    from auto_agents_2.builds import output_hashes
    from auto_agents_2.models import Command
    output = tmp_path / ".next"
    (output / "server/app").mkdir(parents=True)
    (output / "server/app/page.js").write_text("compiled code")
    command = Command(argv=["npm", "run", "build"], outputs=[".next"])
    before = output_hashes(command, tmp_path)
    path = output / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed")
    assert (output_hashes(command, tmp_path) == before) == ("route-cache" in changed)


@pytest.mark.parametrize("record", [{"version": 2, "cwd": "/tmp/elsewhere"},
                                   {"cwd": "/tmp/elsewhere", "pid": 123, "start": "unknown"}])
def test_invalid_legacy_identity_cannot_redirect_resource_protection(tmp_path, record):
    store = Store(tmp_path)
    write_json(store.root / "processes/legacy.json", record)
    assert ResourceManager(store, Cleanup()).process_paths() == [store.root]


@pytest.mark.parametrize("target", ["FILE:///outside.tgz", "git+file:///outside/repository#main"])
def test_local_npm_url_schemes_cannot_bypass_lock_boundary(tmp_path, target):
    from auto_agents_2.node_inputs import lock_inputs
    write_json(tmp_path / "package-lock.json", {"lockfileVersion": 3,
        "packages": {"node_modules/dep": {"resolved": target}}})
    with pytest.raises(AA2Error):
        lock_inputs(tmp_path, tmp_path / "package-lock.json")
