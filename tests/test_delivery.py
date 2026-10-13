import asyncio
import json

import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.config import Cleanup, ConfigStore
from auto_agents_2.errors import AA2Error
from auto_agents_2.resources import ResourceManager
from auto_agents_2.store import Store
from auto_agents_2.util import digest
from auto_agents_2.workspace import WorkspaceManager, git


def commit(project, message="user commit"):
    git(project, "add", "-A")
    git(project, "-c", "user.name=User", "-c", "user.email=user@example.test", "commit", "-m", message)
    return git(project, "rev-parse", "HEAD")


def fixture(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Addition")
    return project, requirements


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["main", "master", "release"])
async def test_delivers_root_and_reuses_target_without_run_branches(tmp_path, branch):
    project, requirements = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    (project / "existing.txt").write_text("keep")
    commit(project)
    if branch != "main":
        git(project, "branch", "-m", branch)
    run = await workflow.start(str(requirements))
    assert run["data"]["branch"] == branch
    assert git(project, "rev-parse", "HEAD") == run["data"]["delivery_commit"]
    assert (project / "calc.py").exists()
    assert (project / "existing.txt").read_text() == "keep"
    assert not git(project, "status", "--porcelain")
    assert not git(project, "for-each-ref", "refs/aa2", "refs/heads/aa2")
    assert not run["data"]["cleanup_pending"]
    head = git(project, "rev-parse", "HEAD")
    calls = len(workflow.agent.calls)
    second = await workflow.start(str(requirements))
    assert second["data"]["outcome"] == "NO_CHANGES"
    assert git(project, "rev-parse", "HEAD") == head
    assert len(workflow.agent.calls) == calls
    assert (project / ".aa2/runtime.sh").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unstaged", "staged", "untracked"])
async def test_dirty_user_work_is_not_captured(tmp_path, kind):
    project, requirements = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    (project / "user.txt").write_text("original")
    original = commit(project)
    path = project / ("other.txt" if kind == "untracked" else "user.txt")
    path.write_text("private edits")
    if kind == "staged":
        git(project, "add", "user.txt")
    status = git(project, "status", "--porcelain")
    with pytest.raises(AssertionError, match="未提交"):
        await workflow.start(str(requirements))
    assert git(project, "rev-parse", "HEAD") == original
    assert git(project, "status", "--porcelain") == status
    assert path.read_text() == "private edits"
    assert workflow.agent.calls == []


@pytest.mark.asyncio
async def test_ignored_collision_never_overwritten(tmp_path):
    project, requirements = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    (project / ".gitignore").write_text("calc.py\n")
    commit(project)
    (project / "calc.py").write_text("ignored user file")
    with pytest.raises(AssertionError):
        await workflow.start(str(requirements))
    assert (project / "calc.py").read_text() == "ignored user file"
    assert Store(project).current()["status"] == "PAUSED"


def test_tracked_excluded_files_survive_checkpoint(tmp_path):
    project, _ = fixture(tmp_path)
    ConfigStore(project).initialize()
    manager = WorkspaceManager(project)
    manager.initialize()
    (project / ".env").write_text("USER_SECRET=preserved")
    (project / "app.py").write_text("initial")
    original = commit(project)
    work = manager.allocate("run_test", "integration", original)
    (work / "app.py").write_text("changed")
    candidate = manager.checkpoint(work, "refs/aa2/run_test/candidate", "update")
    assert git(project, "show", candidate + ":.env") == "USER_SECRET=preserved"


def test_branch_selection_and_occupied_worktree(tmp_path):
    project, _ = fixture(tmp_path)
    ConfigStore(project).initialize()
    manager = WorkspaceManager(project)
    manager.initialize()
    (project / "user.txt").write_text("keep")
    commit(project)
    git(project, "branch", "master")
    assert set(manager.target_candidates()) == {"main", "master"}
    git(project, "update-ref", "refs/remotes/origin/master", "HEAD")
    git(project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    assert manager.target_candidates() == ["master"]
    git(project, "worktree", "add", str(tmp_path / "occupied"), "master")
    with pytest.raises(AA2Error, match="占用"):
        manager.clean_target("master", switch=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["ready", "publishing", "published", "runtime"])
async def test_resume_at_journal_boundaries_does_not_repeat_implementation(tmp_path, boundary):
    project, requirements = fixture(tmp_path)
    agent = CodingAgent()
    workflow = engine(project, agent)
    save = workflow.delivery.save
    interrupted = False

    def crash(rid, journal, phase=None):
        nonlocal interrupted
        save(rid, journal, phase)
        if journal["phase"] == boundary and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()

    workflow.delivery.save = crash
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    calls = list(agent.calls)
    result = await engine(project, agent).start()
    assert result["status"] == "SUCCEEDED"
    assert agent.calls == calls
    assert (project / "calc.py").exists()
    assert not git(project, "status", "--porcelain")


@pytest.mark.asyncio
async def test_crash_after_git_update_before_journal_write(tmp_path):
    project, requirements = fixture(tmp_path)
    agent = CodingAgent()
    workflow = engine(project, agent)
    publish = workflow.workspaces.publish

    def interrupted(*args):
        publish(*args)
        raise asyncio.CancelledError()

    workflow.workspaces.publish = interrupted
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    head = git(project, "rev-parse", "HEAD")
    count = len(agent.calls)
    result = await engine(project, agent).start()
    assert result["status"] == "SUCCEEDED"
    assert git(project, "rev-parse", "HEAD") == head
    assert len(agent.calls) == count


@pytest.mark.asyncio
async def test_runtime_failure_and_cancel_preserve_published_source(tmp_path):
    project, requirements = fixture(tmp_path)
    agent = CodingAgent()
    workflow = engine(project, agent)

    async def cancelled(*args):
        rid = workflow.store.current()["id"]
        workflow.store.checkpoint(rid, "control/cancel", True)
        raise asyncio.CancelledError()

    workflow.delivery.runtime = cancelled
    result = await workflow.start(str(requirements))
    assert result["status"] == "PAUSED"
    assert (project / "calc.py").exists()
    count = len(agent.calls)
    result = await engine(project, agent).start()
    assert result["status"] == "SUCCEEDED" and len(agent.calls) == count


@pytest.mark.asyncio
async def test_target_advance_is_reintegrated_and_reverified(tmp_path):
    project, requirements = fixture(tmp_path)

    class AdvancingAgent(CodingAgent):
        advanced = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if operation == "final-review" and not self.advanced:
                self.advanced = True
                (project / "user.txt").write_text("concurrent work")
                commit(project)
            return result

    workflow = engine(project, AdvancingAgent())
    (project / "initial.txt").write_text("initial")
    baseline = commit(project)
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED"
    assert (project / "user.txt").read_text() == "concurrent work"
    assert workflow.workspaces.is_ancestor(baseline, git(project, "rev-parse", "HEAD"))
    assert [s for s, _ in workflow.agent.calls].count("verify") == 2
    assert workflow.store.get(result["id"], "delivery/receipt")["reintegrations"] == 1


def test_partial_git_recovery_only_repairs_journal_images(tmp_path):
    project, _ = fixture(tmp_path)
    ConfigStore(project).initialize()
    manager = WorkspaceManager(project)
    manager.initialize()
    (project / "a.txt").write_text("old")
    old = commit(project)
    work = manager.allocate("run_test", "integration", old)
    (work / "a.txt").write_text("new")
    (work / "b.txt").write_text("new file")
    new = manager.checkpoint(work, "refs/aa2/run_test/candidate", "candidate")
    journal = {"target": "main", "old": old, "candidate": new, "manifest": manager.publication_manifest(old, new)}
    (project / "a.txt").write_text("new")
    manager.recover_publication(journal)
    assert git(project, "rev-parse", "HEAD") == new
    assert (project / "b.txt").read_text() == "new file"
    assert not git(project, "status", "--porcelain")
    (project / "a.txt").write_text("unrelated user edit")
    with pytest.raises(AA2Error, match="用户修改"):
        manager.recover_publication(journal)
    assert (project / "a.txt").read_text() == "unrelated user edit"


def test_gc_pins_current_runtime_and_preserves_shared_artifacts(tmp_path):
    project, _ = fixture(tmp_path)
    cfg = ConfigStore(project).initialize()
    store = Store(project)
    resources = ResourceManager(store, Cleanup(cache_ttl_seconds=0, min_free_bytes=0))
    run = store.admit("fixture", cfg.model_dump())
    env = store.root / "environments/current"
    env.mkdir(parents=True)
    (env / "runtime").write_text("needed")
    resources.register(env, "environment", run["id"])
    artifacts = ArtifactStore(store.root)
    kept = artifacts.put("final evidence")
    trash = artifacts.put("intermediate")
    store.checkpoint(run["id"], "verify/checks", {"report": kept})
    receipt = {"target": "main", "candidate": "fixture", "source_hash": "fixture", "outcome": "DELIVERED",
               "runtime": {"resources": [str(env)]}, "evidence": store.delivery_evidence(run["id"])}
    receipt["digest"] = digest(receipt)
    store.complete_delivery(run["id"], receipt)
    resources.release_run(run["id"])
    resources.collect()
    resources.collect_artifacts()
    assert env.exists()
    assert artifacts.read(kept) == "final evidence"
    assert not (artifacts.root / trash["hash"][:2] / trash["hash"]).exists()
    second = store.admit("second", cfg.model_dump())
    shared = artifacts.put("final evidence")
    assert shared == kept
    resources.collect_artifacts()
    assert artifacts.read(shared)
    assert store.current()["id"] == second["id"]
    orphan = artifacts.put("do not delete through replaced ancestor")
    store.update(second["id"], status="SUCCEEDED")
    external = tmp_path / "external-artifacts"
    artifacts.root.rename(external)
    artifacts.root.symlink_to(external, target_is_directory=True)
    resources.collect_artifacts()
    assert (external / orphan["hash"][:2] / orphan["hash"]).read_text() == "do not delete through replaced ancestor"


@pytest.mark.asyncio
async def test_cleanup_failure_is_success_and_plain_run_retries_without_models(tmp_path, monkeypatch):
    project, requirements = fixture(tmp_path)
    agent = CodingAgent()
    workflow = engine(project, agent)
    original = workflow.resources.collect_artifacts

    def fail():
        raise OSError("simulated disk error")

    monkeypatch.setattr(workflow.resources, "collect_artifacts", fail)
    first = await workflow.start(str(requirements))
    assert first["status"] == "SUCCEEDED" and first["data"]["cleanup_pending"]
    count = len(agent.calls)
    monkeypatch.setattr(workflow.resources, "collect_artifacts", original)
    second = await workflow.start()
    assert second["status"] == "SUCCEEDED" and not second["data"]["cleanup_pending"]
    assert len(agent.calls) == count
    receipt = workflow.store.get(first["id"], "delivery/receipt")
    assert receipt["tokens"] == workflow.store.usage_summary(first["id"])
    assert json.loads((project / ".aa2/runtime.json").read_text())["fingerprint"] == "test-environment"


@pytest.mark.asyncio
async def test_conflict_fix_is_reviewed_and_never_applied_in_primary_workdir(tmp_path):
    from auto_agents_2.models import Change, ImplementationResult
    project, requirements = fixture(tmp_path)

    class ConflictAgent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if operation == "final-review":
                (project / "calc.py").write_text("# user comment\ndef add(a, b):\n    return 0\n")
                commit(project)
            if operation.startswith("delivery-repair/"):
                assert workspace != project
                assert (project / "calc.py").read_text().endswith("return 0\n")
                return ImplementationResult(summary="retain user comment and accepted addition", changes=[
                    Change(path="calc.py", content="# user comment\ndef add(a, b):\n    return a + b\n")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    workflow = engine(project, ConflictAgent())
    (project / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    commit(project)
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED"
    assert (project / "calc.py").read_text() == "# user comment\ndef add(a, b):\n    return a + b\n"
    assert workflow.store.get(result["id"], "rounds/delivery")["round"] == 1


@pytest.mark.asyncio
async def test_reintegration_budget_survives_restarts(tmp_path):
    project, requirements = fixture(tmp_path)

    class AlwaysAdvancing(CodingAgent):
        advances = 0

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "verify":
                self.advances += 1
                (project / f"external-{self.advances}.txt").write_text("concurrent")
                commit(project)
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = AlwaysAdvancing()
    workflow = engine(project, agent)
    (project / "initial.txt").write_text("initial")
    commit(project)
    with pytest.raises(AssertionError, match="预算"):
        await workflow.start(str(requirements))
    run = workflow.store.current()
    assert workflow.store.get(run["id"], "delivery/journal")["reintegrations"] == 2
    count = len(agent.calls)
    with pytest.raises(AssertionError, match="预算"):
        await engine(project, agent).start()
    assert len(agent.calls) == count


@pytest.mark.asyncio
async def test_later_user_commit_after_publication_is_never_reset(tmp_path):
    project, requirements = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    save = workflow.delivery.save

    def stop(rid, journal, phase=None):
        save(rid, journal, phase)
        if phase == "published":
            raise asyncio.CancelledError()

    workflow.delivery.save = stop
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    (project / "user.txt").write_text("after publication")
    user_commit = commit(project)
    with pytest.raises(AssertionError, match="提交发生变化"):
        await engine(project, workflow.agent).start()
    assert git(project, "rev-parse", "HEAD") == user_commit
    assert (project / "user.txt").read_text() == "after publication"


@pytest.mark.asyncio
async def test_runtime_source_fix_returns_to_isolated_verification(tmp_path):
    from auto_agents_2.delivery import RuntimeVerificationError
    from auto_agents_2.models import Change, ImplementationResult
    project, requirements = fixture(tmp_path)

    class RuntimeRepairAgent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if operation.startswith("delivery-repair/"):
                assert workspace != project
                assert "# runtime ready" not in (project / "calc.py").read_text()
                return ImplementationResult(summary="runtime correction", changes=[
                    Change(path="calc.py", content="# runtime ready\ndef add(a,b):\n    return a + b\n")])
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    workflow = engine(project, RuntimeRepairAgent())
    checks = workflow.delivery.runtime
    failed = False

    async def runtime(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeVerificationError([{"failure": "injected main-workdir contract failure"}])
        return await checks(*args)

    workflow.delivery.runtime = runtime
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED"
    assert (project / "calc.py").read_text().startswith("# runtime ready")
    assert workflow.store.get(result["id"], "rounds/delivery")["round"] == 1
    assert [s for s, _ in workflow.agent.calls].count("verify") == 2


def test_partial_recovery_keeps_unknown_index_lock_and_staged_edits(tmp_path):
    project, _ = fixture(tmp_path)
    ConfigStore(project).initialize()
    manager = WorkspaceManager(project)
    manager.initialize()
    (project / "a.txt").write_text("old")
    old = commit(project)
    work = manager.allocate("run_test", "integration", old)
    (work / "a.txt").write_text("new")
    new = manager.checkpoint(work, "refs/aa2/run_test/candidate", "candidate")
    journal = {"target": "main", "old": old, "candidate": new, "manifest": manager.publication_manifest(old, new)}
    lock = project / ".git/index.lock"
    lock.write_text("another process")
    with pytest.raises(AA2Error, match="index.lock"):
        manager.recover_publication(journal)
    assert lock.read_text() == "another process"
    lock.unlink()
    (project / "a.txt").write_text("user staged change")
    git(project, "add", "a.txt")
    with pytest.raises(AA2Error, match="暂存区"):
        manager.recover_publication(journal)
    assert git(project, "show", ":a.txt") == "user staged change"
    assert git(project, "rev-parse", "HEAD") == old


@pytest.mark.asyncio
async def test_busy_cli_with_new_requirements_does_not_attach_or_change_provider(tmp_path, monkeypatch):
    from auto_agents_2.cli import execute
    from auto_agents_2.engine import WorkflowEngine
    from auto_agents_2.errors import BusyProject, IncompleteIteration
    project, requirements = fixture(tmp_path)
    config = ConfigStore(project)
    config.initialize()
    before = config.path.read_bytes()

    async def busy(*args, **kwargs):
        raise BusyProject("controller is busy")

    monkeypatch.setattr(WorkflowEngine, "start", busy)
    with pytest.raises(IncompleteIteration):
        await execute(project, str(requirements), "antigravity")
    assert config.path.read_bytes() == before
    assert Store(project).current() is None


@pytest.mark.asyncio
async def test_ambiguous_target_is_selected_once_and_frozen(tmp_path):
    project, requirements = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    (project / "initial.txt").write_text("initial")
    commit(project)
    git(project, "branch", "master")
    questions = []

    async def select(question):
        questions.append(question["key"])
        assert question["key"] == "delivery-target"
        return next(o["key"] for o in question["interaction"]["options"] if o["value"] == "master")

    workflow.question_handler = select
    first = await workflow.start(str(requirements))
    assert first["data"]["branch"] == "master"
    assert workflow.config.load().delivery.target_branch == "master"
    await workflow.start(str(requirements))
    assert questions == ["delivery-target"]


@pytest.mark.asyncio
async def test_usage_ledger_and_receipt_survive_artifact_compaction(tmp_path):
    project, requirements = fixture(tmp_path)

    class MeteredAgent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            attempt = str(len(self.calls))
            store = Store(project)
            store.event(run_id, "agent.started", {"attempt": attempt})
            store.add_usage(run_id, "fixture", attempt, "usage", {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "complete": True})
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = MeteredAgent()
    workflow = engine(project, agent)
    run = await workflow.start(str(requirements))
    usage = workflow.store.usage_summary(run["id"])
    assert usage["total_tokens"] == len(agent.calls) * 5 and usage["complete"]
    assert workflow.store.get(run["id"], "delivery/receipt")["tokens"] == usage
    assert workflow.store.get(run["id"], "develop/task/T1").get("implementation") is None
    workflow.resources.finish_delivery(run["id"])
    assert workflow.store.usage_summary(run["id"]) == usage


@pytest.mark.asyncio
async def test_activation_matches_src_layout_and_declared_runtime_cwd(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from auto_agents_2.environment import Environment

    project, _ = fixture(tmp_path)
    workflow = engine(project, CodingAgent())
    (project / "src").mkdir()
    (project / "src/top_module.py").write_text("answer = 42\n")
    (project / "backend/src").mkdir(parents=True)
    (project / "backend/src/backend_module.py").write_text("answer = 7\n")
    environment = Environment(Path(sys.prefix), "fixture")
    await workflow.environments.prepare_delivery(environment, ["backend"])
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c",
        "source .aa2/runtime.sh && python -c 'import top_module, backend_module; assert top_module.answer + backend_module.answer == 49'"],
        cwd=project, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
