"""Interrupted handoff, recovery and ownership regressions from OCR round two."""

import asyncio
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_recovery_contracts as contracts
from authority_review_fixtures import authority_review, seed_original
from evidence_test_fixtures import empty_contract, seed_requirements
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2 import design, logs
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.delivery import DeliveryCoordinator, RuntimeVerificationError
from auto_agents_2.environment import Environment
from auto_agents_2.errors import AA2Error, PolicyViolation, PrototypeBrowserError, ProviderError
from auto_agents_2.models import (
    Change,
    Command,
    ImplementationResult,
    PlanResult,
    PreviewSpec,
    ReviewResult,
    TaskSpec,
)
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import FailureRecord, RecoveryNeeded
from auto_agents_2.research import request_documents
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import SCHEMA, Store
from auto_agents_2.util import digest, write_json
from auto_agents_2.verification import VerificationService
from auto_agents_2.workspace import (
    WorkspaceManager,
    apply_prepared,
    git,
    prepare_changes,
    prepared_before_hash,
    tree_hash,
)


@pytest.mark.parametrize("method", ["put", "put_file"])
def test_corrupt_existing_artifact_cannot_be_republished(tmp_path, method):
    art = ArtifactStore(Store(tmp_path).root)
    source = tmp_path / "source"
    source.write_bytes(b"good")
    def publish():
        return art.put(b"good") if method == "put" else art.put_file(source)
    ref = publish()
    path = art.root / ref["hash"][:2] / ref["hash"]
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="integrity"):
        publish()
    assert path.read_bytes() == b"bad"


def test_retired_diagnostic_type_is_inferred(tmp_path):
    store = Store(tmp_path)
    art = ArtifactStore(store.root)
    rid = store.admit("fixture", {})["id"]
    ref = art.put("log", "check_log")
    (art.root / ref["hash"][:2] / ref["hash"]).unlink()
    with store.connect() as db:
        db.execute("DELETE FROM diagnostic_holds")
        db.execute("INSERT INTO retired_diagnostics VALUES(?,?,?)", (ref["hash"], 1, "collected"))
    with art.reading({"hash": ref["hash"]}, rid):
        pass
    with store.connect() as db:
        db.execute("DELETE FROM retired_diagnostics")
    with pytest.raises(ValueError, match="unavailable"):
        with art.reading({"hash": ref["hash"]}, rid):
            pass


async def test_partial_log_initialization_closes_acquired_handles(tmp_path, monkeypatch):
    created = []
    original = logs.SanitizedLog
    def make(path, *a, **kw):
        if path.name == "stderr":
            raise OSError("second log failed")
        log = original(path, *a, **kw)
        created.append(log)
        return log
    @contextmanager
    def temporary(*a):
        yield tmp_path
    monkeypatch.setattr(logs, "SanitizedLog", make)
    with pytest.raises(OSError):
        await logs.capture_diagnostics(None, SimpleNamespace(temporary=temporary), None)
    assert created[0].stream.closed


def test_log_constructor_closes_on_chmod_failure(tmp_path, monkeypatch):
    streams = []
    original = Path.open
    def opened(path, *a, **kw):
        stream = original(path, *a, **kw)
        streams.append(stream)
        return stream
    def fail(*a):
        raise OSError("chmod")
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(logs.os, "chmod", fail)
    with pytest.raises(OSError):
        logs.SanitizedLog(tmp_path / "log")
    assert streams[0].closed


async def test_proxy_cleanup_retains_setup_error(monkeypatch):
    class Proxy:
        url = "http://127.0.0.1:1"
        closed = False
        async def start(self):
            return self
        def stop(self, *a):
            pass
        async def close(self):
            self.closed = True
    proxy = Proxy()
    class Context:
        def on(self, *a):
            pass
        async def route(self, *a):
            raise ValueError("setup failure")
        async def close(self):
            raise OSError("cleanup failure")
    async def context(**kw):
        return Context()
    monkeypatch.setattr(design, "PreviewProxy", lambda *a: proxy)
    with pytest.raises(ValueError, match="setup failure") as caught:
        await design.preview_context(SimpleNamespace(new_context=context), "http://127.0.0.1:1", [], 800, 600)
    assert proxy.closed and caught.value.__notes__


async def test_verifier_closes_logs_when_stopping_fails(tmp_path, monkeypatch):
    store = Store(tmp_path)
    work, scratch, logroot = (tmp_path / "work", store.root / "scratch", store.root / "logs")
    for path in (work, scratch, logroot):
        path.mkdir()
    class Process:
        returncode = None
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        async def wait(self):
            await asyncio.Event().wait()
    async def spawn(*a, **kw):
        return Process()
    async def stop(*a):
        raise OSError("stop failure")
    created = []
    import auto_agents_2.logs as logging
    original = logging.SanitizedLog
    def make(*a, **kw):
        log = original(*a, **kw)
        created.append(log)
        return log
    monkeypatch.setattr(logging, "SanitizedLog", make)
    service = VerificationService(store.root, SimpleNamespace(spawn=spawn, stop=stop))
    with pytest.raises(OSError, match="stop failure"):
        await service._check(Command(argv=["true"], timeout=1), work, None, "baseline", scratch, log_root=logroot)
    assert all(log.stream.closed for log in created)


async def test_non_codex_bad_progress_is_provider_failure(tmp_path):
    config, store = configured(tmp_path)
    binary = tmp_path / "provider-fixture"
    binary.write_text(f"#!{sys.executable}\nimport sys,json\nsys.stdin.read()\n"
                      "print(json.dumps({'event':'item.started','item':[1]}))\n")
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    spec = body["providers"]["antigravity"]
    spec.update(binary=str(binary), environment={})
    body.update(active_provider="antigravity", providers={"antigravity": spec})
    write_json(config.path, body)
    rid = store.admit("fixture", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    with pytest.raises(ProviderError, match="progress event"):
        await router.invoke("review", {}, ReviewResult, tmp_path, rid, "fixture")
    assert not store.get(rid, "infra/fixture/antigravity")["pending"]


def test_plan_and_research_mutation_preserve_input_objects():
    original = contracts.plan()
    patch = contracts.patch_for(original)
    candidate, _, _ = contracts.apply(original, patch)
    candidate.tasks[0].acceptance.append("new")
    candidate.tasks[2].checks[0].argv.append("--new")
    assert "new" not in original.tasks[0].acceptance
    assert "--new" not in patch.upsert_tasks[0].checks[0].argv
    row = {"excerpt": "body", "retrieval": {"artifact": {"hash": "a" * 64}}}
    result = request_documents({"a": row, "b": row})
    assert result["a"] is result["b"] and row["excerpt"] == "body"


@pytest.mark.parametrize("stage", ["plan", "review"])
@pytest.mark.parametrize("change", ["source", "head"])
async def test_plan_patch_rejects_whole_operation_baseline_drift(tmp_path, monkeypatch, stage, change):
    class Agent(CodingAgent):
        async def invoke(self, actual, context, schema, workspace, *args, **kw):
            if actual == stage:
                if change == "source":
                    (workspace / "concurrent.txt").write_text("user change")
                else:
                    git(workspace, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                        "commit", "--allow-empty", "-qm", "concurrent commit")
            if schema is PlanPatch:
                return contracts.patch_for(PlanResult.model_validate(context["plan"]), evidence_contract=empty_contract(context))
            return await super().invoke(actual, context, schema, workspace, *args, **kw)
    workflow = engine(tmp_path, Agent())
    rid = workflow.store.admit("fixture", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "integration", base)
    plan = contracts.plan()
    seed_requirements(workflow, rid, plan)
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "task_origins": {t.id: t.origin_id for t in plan.tasks}})
    async def grant(*a):
        pass
    monkeypatch.setattr(workflow, "_grant_replan", grant)
    with pytest.raises(AA2Error):
        await workflow.recovery._patch(rid, {"attempt": 1, "decision": {"reason": "repair"}}, contracts.failure(), plan, work)
    assert workflow.store.get(rid, "plan_revision/current") is None
    if change == "source":
        assert (work / "concurrent.txt").read_text() == "user change"
    else:
        assert git(work, "rev-parse", "HEAD") != base


def simple_plan():
    check = Command(argv=[sys.executable, "-c", "pass"])
    task = TaskSpec(id="T", origin_id="T", title="T", description="T", requirement_ids=["R"],
                    allowed_paths=["app.py"], acceptance=["passes"], checks=[check])
    return PlanResult(summary="", document="", tasks=[task], final_checks=[check], runtime_probes=[check],
                      preview=PreviewSpec(command=Command(argv=["server", "{port}"])))


async def test_retained_evidence_must_cover_all_commands(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "integration", base)
    plan = simple_plan()
    plan.preview = None
    commands = [Command(argv=[sys.executable, "-c", "pass"]), Command(argv=[sys.executable, "-c", "print(2)"])]
    plan.tasks[0].checks = commands
    environment = await workflow._environment(work, rid, plan)
    rows = await workflow.verifier.run(commands[:1], work, environment)
    failure = FailureRecord.create(kind="checks", component="legacy_adapter", origin_id="T", task_id="T",
        step="recovery", obligations=["R"], evidence={"checks": [r.model_dump() for r in rows]})
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "develop/task/T": {"workspace": str(work)}})
    workflow.recovery.submit(rid, failure)
    verified = []
    original = workflow.verifier.run
    async def run(checks, *a):
        verified.extend(checks)
        return await original(checks, *a)
    class ReachedDiagnosis(Exception):
        pass
    async def diagnose(*a, **kw):
        raise ReachedDiagnosis()
    monkeypatch.setattr(workflow.verifier, "run", run)
    monkeypatch.setattr(workflow, "_invoke", diagnose)
    with pytest.raises(ReachedDiagnosis):
        await workflow.recovery.resume(rid)
    active = workflow.store.get(rid, "recovery/active")
    assert active["evidence_reconciled"] and len(active["failure"]["evidence"]["checks"]) == 2
    assert verified == commands


@pytest.mark.parametrize("mode", ["runtime", "reuse", "reintegration"])
async def test_browser_failures_keep_partial_evidence_and_enter_recovery(tmp_path, monkeypatch, mode):
    workflow = engine(tmp_path, CodingAgent())
    plan, store = simple_plan(), workflow.store
    if mode == "reuse":
        previous = store.admit("previous", {})["id"]
        store.checkpoints(previous, {"plan": plan.model_dump(), "prototype": {"summary": "fixture"}})
        store.update(previous, status="SUCCEEDED")
    rid = store.admit("fixture", {})["id"]
    store.checkpoint(rid, "clarify", {"frontend": True})
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "integration", base)
    rows = [{"route": "/", "viewport": 1440, "errors": ["assertion failed"], "actions": []}]
    async def browser(*a, **kw):
        raise PrototypeBrowserError("fixture failure", rows)
    async def ready(*a, **kw):
        pass
    async def approved(*a, **kw):
        return {"approved": True}
    class RepairReached(Exception):
        pass
    async def repair(run, checkout, feedback, *a, **kw):
        assert checkout == work and feedback["visuals"] == rows
        raise RepairReached()
    monkeypatch.setattr("auto_agents_2.engine.inspect_application", browser)
    monkeypatch.setattr(workflow, "_ensure_browser", ready)
    monkeypatch.setattr(workflow, "_ensure_prototype_approval", approved)
    monkeypatch.setattr(workflow, "_export_environment", lambda *a: None)
    monkeypatch.setattr(workflow, "_delivery_repair", repair)
    if mode == "runtime":
        with pytest.raises(RuntimeVerificationError) as caught:
            await workflow._runtime_checks(rid, {"candidate": base, "plan": plan.model_dump()}, Environment(Path(sys.prefix), "fixture"))
        assert caught.value.evidence["visuals"] == rows and caught.value.evidence["checks"]
    elif mode == "reuse":
        with pytest.raises(RecoveryNeeded):
            await workflow._finish_no_changes(rid, work, previous)
        assert store.get(rid, "verify/visuals") == rows
    else:
        with pytest.raises(RepairReached):
            await workflow._reintegrate_delivery(rid, {"work": str(work), "incoming": base, "candidate": base,
                "plan": plan.model_dump(), "reintegrations": 0})


def delivery_fixture(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    journal = {"identity_version": 2, "phase": "runtime", "target": "main", "old": "c", "candidate": "c",
        "incoming": "c", "candidate_tree": "tree", "work": str(tmp_path), "source_hash": "source",
        "environment": {"prefix": sys.prefix, "fingerprint": "fixture"}, "plan": {}, "reintegrations": 0,
        "runtime_budget_used": 0, "outcome": "DELIVERED", "validation": "proof"}
    store.checkpoint(rid, "delivery/journal", journal)
    monkeypatch.setattr("auto_agents_2.delivery.tree_hash", lambda *a: "source")
    monkeypatch.setattr("auto_agents_2.delivery.git", lambda root, *a, **kw: "c" if a[0] == "rev-parse" else "")
    workspace = SimpleNamespace(project=tmp_path, clean_target=lambda *a: None, head=lambda *a: "c", entries=lambda *a: [],
        checkout_policy=lambda *a: {}, publication_manifest=lambda *a: {}, check_publication_paths=lambda *a: None,
        publish=lambda *a: None, recover_publication=lambda *a: None)
    async def verify(*a):
        return {}
    async def prepare(*a):
        pass
    def make(runtime):
        async def checked_runtime(*args):
            evidence = await runtime(*args)
            if not evidence.get("checks"):
                from auto_agents_2.builds import runtime_commands
                environment = Environment(Path(sys.prefix), "fixture")
                verifier = VerificationService(store.root, SandboxRunner(store.root, _test_unconfined=True))
                rows = await verifier.run(runtime_commands(None, tmp_path, environment), tmp_path, environment)
                evidence = {**evidence, "checks": [row.model_dump() for row in rows]}
            return evidence
        return DeliveryCoordinator(store, workspace, SimpleNamespace(finish_delivery=lambda *a: None),
                                   SimpleNamespace(prepare_delivery=prepare), verify, checked_runtime)
    return config, store, rid, make


@pytest.mark.parametrize("boundary", ["runtime_file", "receipt_reads", "complete_transaction"])
async def test_successful_runtime_survives_last_attempt_handoff_failure(tmp_path, monkeypatch, boundary):
    config, store, rid, make = delivery_fixture(tmp_path, monkeypatch)
    body = config.load().model_dump(mode="json")
    body["delivery"]["max_runtime_retries"] = 0
    write_json(config.path, body)
    calls = []
    async def runtime(*a):
        calls.append(1)
        return {"checks": [], "verified": True}
    def fail(*a):
        raise OSError("injected handoff failure")
    with monkeypatch.context() as patch:
        if boundary == "runtime_file":
            patch.setattr("auto_agents_2.delivery.write_json", fail)
        elif boundary == "receipt_reads":
            patch.setattr(store, "delivery_evidence", fail)
        else:
            patch.setattr(store, "complete_delivery", fail)
        with pytest.raises(OSError):
            await make(runtime).finish(rid)
    assert store.get(rid, "delivery/journal")["phase"] == "completion_pending"
    assert (await make(runtime).finish(rid))["status"] == "SUCCEEDED"
    assert calls == [1] and store.get(rid, "delivery/receipt")["runtime_budget_used"] == 1


async def test_repeated_runtime_failure_stays_blocked_across_restarts(tmp_path, monkeypatch):
    config, store, rid, make = delivery_fixture(tmp_path, monkeypatch)
    calls = []
    async def runtime(*a):
        calls.append(1)
        raise RuntimeVerificationError({"checks": [], "error": "failure"})
    for _ in range(3):
        with pytest.raises(AA2Error):
            await make(runtime).finish(rid)
    assert len(calls) == 2 and store.get(rid, "delivery/journal")["phase"] == "runtime_blocked"
    body = config.load().model_dump(mode="json")
    body["delivery"]["max_runtime_retries"] += 1
    write_json(config.path, body)
    with pytest.raises(AA2Error):
        await make(runtime).finish(rid)
    assert len(calls) == 3


@pytest.mark.parametrize("boundary", ["staging", "commit", "reference"])
async def test_added_delivery_repair_reconciles_interrupted_git_checkpoint(tmp_path, monkeypatch, boundary):
    class Agent(CodingAgent):
        proposals = 0
        async def invoke(self, stage, *a, **kw):
            if stage == "implement":
                self.proposals += 1
                return ImplementationResult(summary="repair", changes=[Change(path="new.py", content="new")])
            return authority_review(a[0], approved=True, summary="reviewed")
    agent = Agent()
    workflow = engine(tmp_path, agent)
    (tmp_path / "old.py").write_text("old")
    workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/base", "baseline")
    rid = workflow.store.admit("fixture", {})["id"]
    seed_original(workflow, rid)
    async def decision(*a, **kw):
        return "repair"
    monkeypatch.setattr(workflow.recovery, "delivery_decision", decision)
    import auto_agents_2.workspace as workspace
    original = workspace.git
    interrupted = False
    def command(path, *a, **kw):
        nonlocal interrupted
        result = original(path, *a, **kw)
        hit = ((boundary == "staging" and a[:2] == ("add", "--")) or
               (boundary == "commit" and "commit" in a) or (boundary == "reference" and a[0] == "update-ref"))
        if hit and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(workspace, "git", command)
        with pytest.raises(asyncio.CancelledError):
            await workflow._delivery_repair(rid, tmp_path, {"review": "repair"}, ["new.py"], observation="first")
    await workflow._delivery_repair(rid, tmp_path, {"review": "repair"}, ["new.py"], observation="first")
    pending = workflow.store.get(rid, "candidate/delivery-repair")
    assert pending["commit"] == git(tmp_path, "rev-parse", "HEAD")
    assert agent.proposals == 1 and workflow.store.get(rid, "rounds/delivery")["round"] == 1
    assert (tmp_path / "new.py").read_text() == "new"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_publication_recovers_link_directory_transitions(tmp_path, reverse, published):
    project = tmp_path / "project"
    project.mkdir()
    manager = WorkspaceManager(project)
    manager.initialize()
    (project / "target").mkdir()
    (project / "target/item").write_text("protected target")
    if reverse:
        (project / "a").mkdir()
        (project / "a/b").write_text("before")
    else:
        (project / "a").symlink_to("target")
    old = manager.checkpoint(project, "refs/aa2/fixture/old", "old")
    work = manager.allocate("fixture", "candidate", old)
    if reverse:
        (work / "a/b").unlink()
        (work / "a").rmdir()
        (work / "a").symlink_to("target")
    else:
        (work / "a").unlink()
        (work / "a").mkdir()
        (work / "a/b").write_text("after")
    new = manager.checkpoint(work, "refs/aa2/fixture/new", "new")
    manifest = manager.publication_manifest(old, new, work)
    manager.check_publication_paths(manifest)
    if published:
        manager.publish("main", old, new)
    journal = {"target": "main", "old": old, "candidate": new, "work": str(work), "manifest": manifest}
    manager.recover_publication(journal)
    manager.recover_publication(journal)
    assert git(project, "rev-parse", "HEAD") == new and tree_hash(project) == tree_hash(work)
    assert (project / "target/item").read_text() == "protected target"


def test_initialization_rejects_subdirectory_and_patch_rejects_conflicting_files(tmp_path):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(AA2Error, match="repository root"):
        WorkspaceManager(nested).initialize()
    assert not (nested / ".git").exists()
    with pytest.raises(PolicyViolation, match="ancestor"):
        prepare_changes(tmp_path, [Change(path="a", content="a"), Change(path="a/b", content="b")], ["a"])
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize("version", ["1", "2", "3", "4"])
@pytest.mark.parametrize("fail", [False, True])
def test_actual_missing_table_schema_migration_is_atomic(tmp_path, version, fail):
    state = tmp_path / ".aa2"
    state.mkdir()
    sha = digest("legacy document")
    document = state / "artifacts" / sha[:2] / sha
    document.parent.mkdir(parents=True)
    document.write_text("legacy document")
    # Historical schemas predate these tables; do not initialize a modern Store first.
    statements = [s for s in SCHEMA.split(";") if not any(name in s for name in (
        "artifact_edges", "artifact_kinds", "diagnostic_holds", "retired_diagnostics", "resource_holds"))]
    with sqlite3.connect(state / "state.sqlite") as db:
        db.executescript(";".join(statements))
        db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (version,))
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", ("old", "PAUSED", "verify", 0, 0, "{}"))
        db.execute("INSERT INTO checkpoints VALUES(?,?,?)", ("old", "rounds/T", '{"round":4}'))
        db.execute("INSERT INTO checkpoints VALUES(?,?,?)", ("old", "proof", json.dumps({
            "hash": sha, "kind": "document", "bytes": len("legacy document")})))
        if fail:
            db.execute("CREATE TRIGGER fault BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT,'fault'); END")
    store = Store(tmp_path)
    if fail:
        with pytest.raises(sqlite3.IntegrityError):
            store.migrate()
        with store.connect() as db:
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_holds'").fetchone()
            db.execute("DROP TRIGGER fault")
    store.migrate()
    store.migrate()
    with store.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "8"
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_holds'").fetchone()
    assert store.get("old", "rounds/T") == {"round": 4}
    assert (state / f"state-v{version}.backup.sqlite").is_file()


def test_mixed_role_parent_keeps_nested_diagnostics_during_read(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    artifacts = ArtifactStore(store.root)
    resources = ResourceManager(store, config.load().cleanup)
    child = artifacts.put("nested log", "check_log")
    document = artifacts.put({"child": child}, "document")
    same_log = artifacts.put({"child": child}, "check_log")
    store.checkpoint(rid, "receipt", [document, same_log])
    store.update(rid, status="SUCCEEDED")
    with artifacts.reading(document, rid):
        resources.collect_artifacts()
        assert artifacts.read(child) == "nested log"


async def test_cancel_during_stop_still_reaps_child(tmp_path):
    runner = SandboxRunner(tmp_path / "state", _test_unconfined=True)
    process = await runner.spawn([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"],
                                 tmp_path, {}, [tmp_path / "state"])
    assert await process.stdout.readline() == b"ready\n"
    stopping = asyncio.create_task(runner.stop(process))
    await asyncio.sleep(0.05)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, 6)
    assert process.returncode is not None
    assert not list((runner.root / "processes").glob("*.json"))


async def test_completion_pending_does_not_accept_changed_inputs(tmp_path, monkeypatch):
    _, store, rid, make = delivery_fixture(tmp_path, monkeypatch)
    calls = []
    async def runtime(*a):
        calls.append(1)
        return {"checks": []}
    def fail(*a):
        raise OSError("interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(store, "complete_delivery", fail)
        with pytest.raises(OSError):
            await make(runtime).finish(rid)
    monkeypatch.setattr("auto_agents_2.delivery.tree_hash", lambda *a: "changed")
    with pytest.raises(AA2Error, match="source changed"):
        await make(runtime).finish(rid)
    assert calls == [1] and store.run(rid)["status"] != "SUCCEEDED"


@pytest.mark.parametrize("after_commit", [False, True])
def test_checkpoint_intent_does_not_overwrite_user_edits(tmp_path, after_commit):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / "app.py").write_text("before")
    manager.checkpoint(tmp_path, "refs/aa2/fixture/base", "base")
    (tmp_path / "app.py").write_text("approved")
    intent = manager.checkpoint_intent(tmp_path, "refs/aa2/fixture/candidate")
    if after_commit:
        manager.finish_checkpoint(tmp_path, intent)
    (tmp_path / "app.py").write_text("user edit")
    with pytest.raises(AA2Error, match="source changed"):
        manager.finish_checkpoint(tmp_path, intent)
    assert (tmp_path / "app.py").read_text() == "user edit"


@pytest.mark.parametrize("reverse", [False, True])
def test_file_to_directory_patch_is_order_independent_and_replayable(tmp_path, reverse):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / "a").write_text("old")
    manager.checkpoint(tmp_path, "refs/aa2/fixture/base", "base")
    before = tree_hash(tmp_path)
    changes = [Change(path="a", operation="delete"), Change(path="a/b", content="new")]
    patch = prepare_changes(tmp_path, list(reversed(changes)) if reverse else changes, ["a"])
    apply_prepared(tmp_path, patch)
    apply_prepared(tmp_path, patch)
    manager.checkpoint(tmp_path, "refs/aa2/fixture/new", "new")
    assert prepared_before_hash(tmp_path, patch) == before
    assert (tmp_path / "a/b").read_text() == "new"
