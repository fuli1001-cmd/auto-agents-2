import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from test_recovery_contracts import failure, plan
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.build_publication import BuildPublication
from auto_agents_2.config import Cleanup, ConfigStore
from auto_agents_2.design import inspect_application, inspect_prototype
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error, ConfigurationError, PolicyViolation, PrototypeBrowserError
from auto_agents_2.models import Command, PreviewSpec, ReviewResult
from auto_agents_2.process_ownership import PreviewOwnershipError
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import RecoveryDecision
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner, process_start
from auto_agents_2.store import Store
from auto_agents_2.task_batch import collect_batch
from auto_agents_2.util import write_json
from auto_agents_2.workspace import WorkspaceManager, git, tree_hash


async def test_inherited_environment_invalidates_pending_provider_response(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    workflow = WorkflowEngine(tmp_path)
    calls = []
    async def call(effective, *args):
        calls.append(effective["fingerprint"])
        return ReviewResult(approved=True, summary="fixture")
    monkeypatch.setattr(workflow.agent, "_call", call)
    await workflow._invoke("review", {}, ReviewResult, tmp_path, rid, "cache")
    await workflow._invoke("review", {}, ReviewResult, tmp_path, rid, "cache")
    assert len(calls) == 1
    monkeypatch.setenv("OCR_FIXTURE_ENDPOINT", "changed inherited input")
    await workflow._invoke("review", {}, ReviewResult, tmp_path, rid, "cache")
    assert len(calls) == 2 and calls[0] != calls[1]
    assert "changed inherited input" not in json.dumps(store.get(rid, "invocation/cache"))


async def test_execution_identity_does_not_prevent_provider_failover(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    resolve = config.resolve
    def broken(stage, provider=None):
        if provider == "codex":
            raise ConfigurationError("fixture invalid provider")
        return resolve(stage, provider)
    monkeypatch.setattr(config, "resolve", broken)
    async def call(effective, *args):
        return ReviewResult(approved=True, summary=effective["provider"])
    monkeypatch.setattr(router, "_call", call)
    assert router.execution_identity("review")
    result = await router.invoke("review", {}, ReviewResult, tmp_path, rid, "fallback")
    assert result.summary == "antigravity"


@pytest.mark.parametrize("shape", [None, [], 1, {"prefix": None}, {"prefix": []}])
async def test_corrupt_instance_locator_rebuilds_without_destroying_previous_instance(tmp_path, shape):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root))
    async def prepare(prefix, ready, fingerprint, *args):
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/python").write_text("fixture")
        write_json(ready, {"fingerprint": fingerprint, "integrity": manager.integrity(prefix)})
    manager._prepare = prepare
    original = await manager.ensure(tmp_path, "python")
    locator = next((store.root / "environment-records").glob("*/instance.json"))
    write_json(locator, shape)
    (original.prefix / "bin/python").write_text("corrupt retained fixture")
    rebuilt = await manager.ensure(tmp_path, "python")
    assert rebuilt.prefix != original.prefix
    assert (original.prefix / "bin/python").read_text() == "corrupt retained fixture"


@pytest.mark.parametrize("shape", [None, [], 1])
async def test_invalid_node_marker_reinstalls_instead_of_crashing(tmp_path, shape):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root))
    work = tmp_path / "work"
    work.mkdir()
    write_json(work / "package.json", {"name": "fixture"})
    write_json(work / "package-lock.json", {"name": "fixture", "lockfileVersion": 3})
    prefix = store.root / "environments/fixture"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/npm").write_text("fixture")
    from auto_agents_2.util import digest
    write_json(store.root / "node-records" / (digest(str(work)) + ".json"), shape)
    commands = []
    async def command(argv, *args, **kwargs):
        commands.append(argv)
        (work / "node_modules").mkdir(exist_ok=True)
        return ""
    async def no_op(*args, **kwargs):
        pass
    manager._command = command
    manager.prepare_node_browsers = no_op
    manager.prepare_generated_types = no_op
    await manager.install_node(work, Environment(prefix, "fixture"))
    assert any("ci" in argv for argv in commands)


async def test_question_answered_while_waiting_for_lock_never_calls_handler(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    async with workflow.question_lock:
        pending = asyncio.create_task(workflow._ask(rid, "queued", "Question"))
        await asyncio.sleep(0)
        question = workflow.store.pending(rid)[0]
        workflow.store.answer(question["id"], "external answer")
    assert await asyncio.wait_for(pending, 2) == "external answer"


async def test_batch_pause_cancels_and_joins_siblings_immediately():
    started, stopped = asyncio.Event(), asyncio.Event()
    async def paused():
        await started.wait()
        raise asyncio.CancelledError()
    async def waiting():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(collect_batch([paused(), waiting()]), 1)
    assert stopped.is_set()


async def test_batch_recoverable_failure_keeps_successful_sibling():
    async def failure():
        raise AA2Error("fixture recoverable failure")
    async def success():
        await asyncio.sleep(.01)
        return "retained commit"
    results = await collect_batch([failure(), success()])
    assert isinstance(results[0], AA2Error) and results[1] == "retained commit"


async def test_cli_attachment_stays_with_original_run_after_new_admission(tmp_path, monkeypatch):
    import auto_agents_2.cli as cli
    from auto_agents_2.errors import BusyProject
    _, store = configured(tmp_path)
    original = store.admit("old", {})["id"]
    calls, new_runs = [], []
    class Engine:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self, requirements=None, provider=None, run_id=None):
            calls.append(run_id)
            store.update(original, status="SUCCEEDED")
            new_runs.append(store.admit("new", {})["id"])
            raise BusyProject("fixture lock race")
    async def render(store, stop, *args):
        await stop.wait()
    monkeypatch.setattr(cli, "WorkflowEngine", Engine)
    monkeypatch.setattr(cli, "render", render)
    await cli.execute(tmp_path)
    assert calls == [original]
    assert store.current()["id"] == new_runs[0] and store.current()["status"] != "SUCCEEDED"


def committed_workspace(tmp_path):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / "tracked.py").write_text("original")
    manager.checkpoint(tmp_path, "refs/aa2/base", "baseline")
    (tmp_path / "tracked.py").write_text("updated")
    (tmp_path / "new.py").write_text("eligible new input")
    return manager


@pytest.mark.parametrize("legacy", [False, True])
def test_checkpoint_rejects_partial_commit_omitting_new_file(tmp_path, legacy):
    manager = committed_workspace(tmp_path)
    intent = manager.checkpoint_intent(tmp_path, "refs/aa2/reviewed")
    if legacy:
        intent.pop("tree")
        intent.pop("version")
    git(tmp_path, "add", "tracked.py")
    git(tmp_path, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "partial")
    partial = git(tmp_path, "rev-parse", "HEAD")
    assert tree_hash(tmp_path) == intent["source"]
    with pytest.raises(AA2Error, match="HEAD changed"):
        manager.finish_checkpoint(tmp_path, intent)
    assert git(tmp_path, "rev-parse", "HEAD") == partial
    assert "new.py" not in git(tmp_path, "ls-tree", "--name-only", "HEAD").splitlines()
    assert not git(tmp_path, "for-each-ref", intent["ref"])


@pytest.mark.parametrize("already_committed", [False, True])
def test_checkpoint_reconciles_the_complete_expected_tree(tmp_path, already_committed):
    manager = committed_workspace(tmp_path)
    intent = manager.checkpoint_intent(tmp_path, "refs/aa2/reviewed")
    if already_committed:
        manager.checkpoint(tmp_path, "refs/aa2/interrupted", "complete")
    result = manager.finish_checkpoint(tmp_path, intent)
    assert git(tmp_path, "rev-parse", result + "^{tree}") == intent["tree"]
    assert git(tmp_path, "show", result + ":new.py") == "eligible new input"


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_checkpoint_tree_uses_original_index_flags(tmp_path, flag):
    manager = committed_workspace(tmp_path)
    git(tmp_path, "update-index", flag, "tracked.py")
    intent = manager.checkpoint_intent(tmp_path, "refs/aa2/reviewed")
    commit = manager.finish_checkpoint(tmp_path, intent)
    assert git(tmp_path, "show", commit + ":tracked.py") == "original"
    assert git(tmp_path, "show", commit + ":new.py") == "eligible new input"


async def test_committed_unintegrated_task_keeps_its_candidate_workspace(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    (tmp_path / "base.py").write_text("base")
    base = workflow.workspaces.checkpoint(tmp_path, "refs/aa2/base", "baseline")
    rid = workflow.store.admit("fixture", {})["id"]
    workflow.workspaces.allocate(rid, "integration", base)
    work = workflow.workspaces.allocate(rid, "worker", base)
    (work / "part3.py").write_text("retained implementation")
    ref = f"refs/aa2/{rid}/worker"
    commit = workflow.workspaces.checkpoint(work, ref, "candidate")
    saved = {"workspace": str(work), "commit": commit, "source_hash": tree_hash(work), "recovery_ref": ref}
    workflow.store.checkpoints(rid, {"plan": plan().model_dump(), "develop/task/T3": saved})
    record = failure(kind="integration")
    decision = RecoveryDecision(failure_id=record.id, action="recheck", task_ids=["T3"], reason="retry",
                                evidence_refs=record.evidence_refs, budget_owner="T3")
    workflow.store.checkpoint(rid, "recovery/active", {"failure": record.model_dump(), "attempt": 1,
                                                       "decision": decision.model_dump()})
    await workflow.recovery.resume(rid)
    resumed = workflow.store.get(rid, "develop/task/T3")
    assert resumed["workspace"] == str(work) and resumed["recovery_ref"] == ref
    assert resumed["source_hash"] == saved["source_hash"] and "commit" not in resumed
    assert (Path(resumed["workspace"]) / "part3.py").read_text() == "retained implementation"


async def test_diagnosis_grant_source_drift_preserves_active_recovery(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    (tmp_path / "part3.py").write_text("original")
    base = workflow.workspaces.checkpoint(tmp_path, "refs/aa2/base", "baseline")
    rid = workflow.store.admit("fixture", {})["id"]
    work = workflow.workspaces.allocate(rid, "integration", base)
    record = failure()
    workflow.store.checkpoints(rid, {"plan": plan().model_dump(),
        "recovery/active": {"failure": record.model_dump(), "attempt": 1},
        "recovery/budgets/diagnosis/T3": {"round": workflow.config.load().execution.max_diagnosis_rounds}})
    async def grant(*args):
        (work / "part3.py").write_text("user edit during grant")
    workflow.recovery._grant = grant
    with pytest.raises(AA2Error, match="changed during diagnosis approval"):
        await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active")
    assert workflow.store.get(rid, "recovery/active")["evidence_source_hash"] != tree_hash(work)
    assert not workflow.agent.calls
    assert (work / "part3.py").read_text() == "user edit during grant"
    with pytest.raises(AA2Error, match="evidence source changed"):
        await workflow.recovery.resume(rid)


async def test_delivery_diagnosis_approval_also_rejects_source_drift(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    (tmp_path / "source.py").write_text("original")
    rid = workflow.store.admit("fixture", {})["id"]
    workflow.store.checkpoint(rid, "recovery/budgets/diagnosis/delivery",
        {"round": workflow.config.load().execution.max_diagnosis_rounds})
    async def grant(*args):
        (tmp_path / "source.py").write_text("user edit during approval")
    workflow.recovery._grant = grant
    with pytest.raises(AA2Error, match="changed during diagnosis approval"):
        await workflow.recovery.delivery_decision(rid, tmp_path, {"runtime_failure": "fixture"}, ["source.py"])
    assert not workflow.agent.calls


@pytest.mark.parametrize("status", ["RUNNING", "CANCELLED", "SUCCEEDED"])
def test_failed_output_rollback_survives_gc_and_can_be_recovered(tmp_path, monkeypatch, status):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    work = store.root / "worktrees" / rid / "integration"
    resources.register(work, "worktree", rid)
    for output in ("dist", ".next"):
        (work / output).mkdir(parents=True)
        (work / output / "old").write_text("old " + output)
    original = Path.rename
    def failed(source, target):
        if source.name == ".next" and source.parent.name == "generated":
            raise OSError("primary publish failure")
        if source.name == "1" and source.parent.name.startswith("build_"):
            raise OSError("secondary restore failure")
        return original(source, target)
    with resources.temporary("build") as scratch:
        generated = scratch / "generated"
        for output in ("dist", ".next"):
            (generated / output).mkdir(parents=True)
            (generated / output / "new").write_text("new " + output)
        monkeypatch.setattr(Path, "rename", failed)
        with pytest.raises(OSError, match="primary publish failure") as caught:
            BuildPublication(resources).publish(Command(argv=["build"], outputs=["dist", ".next"]), work, generated)
        assert caught.value.__notes__
        store.update(rid, status=status)
    journal = next((store.root / "recovery").glob("build_*/journal.json"))
    resources.recover_disposable()
    assert journal.exists() and scratch.exists() and work.exists()
    assert (work / "dist/old").read_text() == "old dist"
    assert (journal.parent / "1/old").read_text() == "old .next"
    monkeypatch.setattr(Path, "rename", original)
    BuildPublication(resources).recover(work)
    assert (work / ".next/old").read_text() == "old .next"
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM resource_holds").fetchone()


async def test_supervisor_retains_ownership_after_workload_exit_and_stop_is_idempotent(tmp_path, monkeypatch):
    runner = SandboxRunner(tmp_path / "state", _test_unconfined=True)
    process = await runner.spawn([sys.executable, "-c",
        "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True); print(p.pid,flush=True)"],
        tmp_path, dict(os.environ), [])
    child = int(await process.stdout.readline())
    assert await process.wait() == 0 and process.supervisor.returncode is None
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("controller must never signal a numeric process group"))
    await asyncio.gather(runner.stop(process), runner.stop(process))
    await runner.stop(process)
    assert process.returncode == 0 and process.supervisor.returncode is not None
    assert not process_start(child) and not list((runner.root / "processes").glob("*.json"))


def test_legacy_live_process_records_are_preserved_without_signalling(tmp_path, monkeypatch):
    runner = SandboxRunner(tmp_path)
    record = tmp_path / "processes/fixture.json"
    write_json(record, {"pid": os.getpid(), "start": process_start(os.getpid())})
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("Unsafe adoption"))
    with pytest.raises(PolicyViolation, match="still live"):
        runner.reap_orphans()
    assert record.is_file()


def test_missing_sandbox_is_a_failed_doctor_probe(tmp_path):
    ok, explanation = SandboxRunner(tmp_path, binary="fixture-missing-bwrap").probe()
    assert not ok and "bubblewrap" in explanation


async def test_foreign_preview_listener_receives_no_http_bytes_and_retries_are_bounded(tmp_path):
    ConfigStore(tmp_path).initialize()
    runner = SandboxRunner(tmp_path / ".aa2", _test_unconfined=True)
    original = runner.spawn
    servers, received = [], []
    handlers = set()
    async def accept(reader, writer):
        try:
            received.append(await reader.read(4096))
        finally:
            writer.close()
    def connected(reader, writer):
        task = asyncio.create_task(accept(reader, writer))
        handlers.add(task)
        task.add_done_callback(handlers.discard)
    async def stolen(argv, *args, **kwargs):
        servers.append(await asyncio.start_server(connected, "127.0.0.1", int(argv[-1])))
        return await original(argv, *args, **kwargs)
    runner.spawn = stolen
    spec = PreviewSpec(command=Command(argv=[sys.executable, "-c", "import time; time.sleep(60)", "{port}"]))
    try:
        with pytest.raises(PreviewOwnershipError):
            await inspect_application(spec, tmp_path, tmp_path / "images", Environment(Path(sys.prefix), "fixture"), runner, runner.root)
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        await asyncio.gather(*handlers)
    assert len(servers) == 3 and received and all(data == b"" for data in received)
    assert not list((runner.root / "processes").glob("*.json"))


async def test_prototype_context_failure_retains_completed_viewport(tmp_path, monkeypatch):
    import auto_agents_2.design as design
    cache = Path(__file__).resolve().parents[1] / ".cache/browsers"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    (tmp_path / "index.html").write_text("<p>fixture</p>")
    original = design.preview_context
    calls = 0
    async def context(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second context creation failed")
        return await original(*args)
    monkeypatch.setattr(design, "preview_context", context)
    with pytest.raises(PrototypeBrowserError) as caught:
        await inspect_prototype(tmp_path, tmp_path / "screens")
    rows = caught.value.results
    assert len(rows) == 2 and Path(rows[0]["screenshot"]).is_file()
    assert "second context creation failed" in str(caught.value)
