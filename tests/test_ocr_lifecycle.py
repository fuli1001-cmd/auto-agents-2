import asyncio
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine
from typer.testing import CliRunner

from auto_agents_2.cli import app, execute
from auto_agents_2.config import Cleanup, ConfigStore
from auto_agents_2.delivery import DeliveryCoordinator, RuntimeVerificationError
from auto_agents_2.environment import Environment
from auto_agents_2.errors import AA2Error, BusyProject, ProviderError
from auto_agents_2.models import (
    Change,
    CheckResult,
    Command,
    ImplementationResult,
    PlanResult,
    ReviewResult,
    TaskSpec,
)
from auto_agents_2.progress import observe_activity
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import Diagnosis, FailureRecord, RecoveryDecision, RepairTarget, adjudicate
from auto_agents_2.recovery_controller import RecoveryController
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, file_lock, write_json


@pytest.mark.parametrize("successor", [False, True])
def test_recovery_handoff_is_all_or_nothing_and_preserves_history(tmp_path, monkeypatch, successor):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    failure = FailureRecord.create(kind="contract", component="controller", origin_id="final", step="verify", obligations=["R"])
    active = {"failure": failure.model_dump(), "attempt": 1,
              "decision": RecoveryDecision(failure_id=failure.id, action="design", reason="change", budget_owner="final").model_dump()}
    store.checkpoints(rid, {"recovery/active": active, "recovery/count/" + failure.id: 1})
    recovery = RecoveryController(SimpleNamespace(store=store))
    original = store._put
    def fail(db, run, key, value):
        original(db, run, key, value)
        if (key == "recovery/active" and value) if successor else key == "replanning/pending" and value:
            raise sqlite3.OperationalError("injected handoff failure")
    monkeypatch.setattr(store, "_put", fail)
    kwargs = {"successor": failure.model_dump()} if successor else {"replan": ("design", "change")}
    with pytest.raises(sqlite3.OperationalError):
        recovery._finish(rid, active, {}, **kwargs)
    assert store.get(rid, "recovery/active") == active
    assert store.get(rid, "replanning/pending") is None
    assert store.get(rid, "recovery/count/" + failure.id) == 1
    monkeypatch.setattr(store, "_put", original)
    recovery._finish(rid, active, {}, **kwargs)
    assert store.get(rid, f"recovery/completed/{failure.id}/1")["phase"] == "dispatched"
    if successor:
        assert store.get(rid, "recovery/active")["attempt"] == 2
    else:
        assert store.get(rid, "recovery/active") is None
        assert store.get(rid, "replanning/pending")["controller_owned"]
        assert store.get(rid, "replanning/progress")["evaluations"] == 1


def test_duplicate_repair_targets_are_rejected_before_information_loss():
    task = TaskSpec(id="B", title="B", description="B", requirement_ids=["R"], allowed_paths=["b"],
                    acceptance=["passes"], checks=[Command(argv=["check"])])
    plan = PlanResult(summary="", document="", tasks=[task], final_checks=task.checks)
    failure = FailureRecord.create(kind="checks", component="verification", origin_id="A", task_id="A", step="check",
                                   obligations=["R"], evidence_refs=["proof", "b/file"])
    diagnosis = Diagnosis(action="repair", task_ids=["B"], evidence_refs=["proof"], reason="repair", repair_targets=[
        RepairTarget(task_id="B", paths=["unowned/file"], evidence_refs=["invented"], reason="bad"),
        RepairTarget(task_id="B", paths=["b/file"], evidence_refs=["proof"], reason="valid")])
    with pytest.raises(AA2Error, match="Duplicate"):
        adjudicate(failure, diagnosis=diagnosis, plan=plan)


async def test_completed_repair_replays_only_for_same_observation(tmp_path, monkeypatch):
    class Agent(CodingAgent):
        proposals = 0
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "implement":
                self.proposals += 1
                return ImplementationResult(summary="repair", changes=[Change(path="app.py", content=str(self.proposals))])
            return ReviewResult(approved=True, summary="reviewed")
    agent = Agent()
    workflow = engine(tmp_path, agent)
    (tmp_path / "app.py").write_text("initial")
    workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/base", "baseline")
    rid = workflow.store.admit("fixture", {})["id"]
    async def decision(*args, **kwargs):
        return "repair"
    monkeypatch.setattr(workflow.recovery, "delivery_decision", decision)
    for observation, count in (("first", 1), ("first", 1), ("second", 2)):
        await workflow._delivery_repair(rid, tmp_path, {"review": "same failure"}, ["app.py"], observation=observation)
        assert agent.proposals == count
        assert workflow.store.get(rid, "rounds/delivery")["round"] == count


async def test_delivery_recheck_budget_survives_new_observation_ids(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    row = CheckResult(argv=["check"], cwd=".", exit_code=1, process_exit_code=0, output="missing report",
        duration=1, source_hash="fixture", environment_hash="fixture", failure_reasons=["test_report_missing"])
    diagnoses = []
    async def diagnose(stage, context, *args, **kwargs):
        diagnoses.append(context)
        return Diagnosis(action="recheck", evidence_refs=context["failure"]["evidence_refs"], reason="repeat")
    monkeypatch.setattr(workflow, "_invoke", diagnose)
    assert await workflow.recovery.delivery_decision(rid, tmp_path, {"checks": [row.model_dump()]},
                                                      ["app.py"], observation="first") == "recheck"
    row.duration = 2  # New collection metadata must not create another automatic budget.
    with pytest.raises(AA2Error, match="已自动复验一次"):
        await workflow.recovery.delivery_decision(rid, tmp_path, {"checks": [row.model_dump()]},
                                                  ["app.py"], observation="second")
    assert len(diagnoses) == 1


async def test_delivery_recompiles_inferred_outputs_after_repair(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    write_json(tmp_path / "package.json", {"devDependencies": {"vite": "1"}})
    commit = workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/base", "baseline")
    rid = workflow.store.admit("fixture", {})["id"]
    command = Command(argv=["npm", "run", "build"])
    task = TaskSpec(id="T", title="T", description="T", requirement_ids=["R"], allowed_paths=["package.json"],
                    acceptance=["build"], checks=[command])
    plan = PlanResult(summary="", document="", tasks=[task], final_checks=[command])
    seen = []
    class Observed(Exception):
        pass
    async def verify(commands, work, env):
        seen.append(commands[0].outputs)
        if len(seen) == 2:
            raise Observed()
        return [CheckResult(argv=command.argv, cwd=".", exit_code=1, process_exit_code=1, output="fail",
                            duration=0, source_hash="fixture", environment_hash="fixture")]
    async def environment(*args):
        return Environment(Path(sys.prefix), "fixture")
    async def repair(*args, **kwargs):
        write_json(tmp_path / "package.json", {"dependencies": {"next": "1"}})
    monkeypatch.setattr(workflow, "_environment", environment)
    monkeypatch.setattr(workflow, "_export_environment", lambda *a: None)
    monkeypatch.setattr(workflow.verifier, "run", verify)
    monkeypatch.setattr(workflow, "_delivery_repair", repair)
    with pytest.raises(Observed):
        await workflow._reintegrate_delivery(rid, {"work": str(tmp_path), "incoming": commit, "candidate": commit,
            "plan": plan.model_dump(), "reintegrations": 0})
    assert seen == [["dist"], [".next"]]


async def test_runtime_failures_are_bounded_across_restarts(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    journal = {"identity_version": 2, "phase": "runtime", "target": "main", "old": "c", "candidate": "c",
        "incoming": "c", "candidate_tree": "tree", "work": str(tmp_path), "source_hash": "source",
        "environment": {"prefix": "/no-installation", "fingerprint": "fixture"}, "plan": {},
        "reintegrations": 0, "runtime_budget_used": 0, "outcome": "DELIVERED", "validation": "proof"}
    store.checkpoint(rid, "delivery/journal", journal)
    monkeypatch.setattr("auto_agents_2.delivery.tree_hash", lambda *a: "source")
    monkeypatch.setattr("auto_agents_2.delivery.git", lambda root, *a, **kw: "c" if a[0] == "rev-parse" else "")
    workspace = SimpleNamespace(project=tmp_path, clean_target=lambda *a: None, head=lambda *a: "c", entries=lambda *a: [],
        checkout_policy=lambda *a: {}, publication_manifest=lambda *a: {}, check_publication_paths=lambda *a: None,
        publish=lambda *a: None, recover_publication=lambda *a: None)
    calls = []
    async def runtime(*args):
        calls.append(1)
        raise RuntimeVerificationError({"same": "failure"})
    async def verify(*args):
        return {}
    async def prepare(*args):
        pass
    for _ in range(6):
        delivery = DeliveryCoordinator(store, workspace, SimpleNamespace(), SimpleNamespace(prepare_delivery=prepare), verify, runtime)
        with pytest.raises(AA2Error):
            await delivery.finish(rid)
    assert len(calls) == 4
    assert store.get(rid, "delivery/journal")["runtime_budget_used"] == 4
    assert len(store.get(rid, "delivery/journal")["runtime_failures"]) == 4
    body = config.load().model_dump(mode="json")
    body["delivery"]["max_runtime_retries"] = 4
    write_json(config.path, body)
    with pytest.raises(AA2Error):
        await delivery.finish(rid)
    assert len(calls) == 5


async def test_renderer_failure_unwinds_workflow(tmp_path, monkeypatch):
    ConfigStore(tmp_path).initialize()
    cancelled = asyncio.Event()
    async def render(*args):
        raise OSError("display failed")
    class Engine:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    monkeypatch.setattr("auto_agents_2.cli.render", render)
    monkeypatch.setattr("auto_agents_2.cli.WorkflowEngine", Engine)
    with pytest.raises(AA2Error, match="display failed"):
        await asyncio.wait_for(execute(tmp_path), 2)
    assert cancelled.is_set()


def test_cancel_revalidates_after_acquiring_controller_lock(tmp_path, monkeypatch):
    _, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    @contextmanager
    def won_race(*args, **kwargs):
        store.update(rid, status="SUCCEEDED")
        yield
    monkeypatch.setattr("auto_agents_2.cli.file_lock", won_race)
    monkeypatch.setattr(SandboxRunner, "reap_orphans", lambda self: pytest.fail("terminal run must not reap children"))
    result = CliRunner().invoke(app, ["cancel", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert store.run(rid)["status"] == "SUCCEEDED"


def test_live_doctor_owns_controller_lock(tmp_path, monkeypatch):
    _, store = configured(tmp_path)
    monkeypatch.setattr(SandboxRunner, "probe", lambda self: (True, "fixture"))
    async def invoke(*args, **kwargs):
        with pytest.raises(BusyProject), file_lock(store.root / "controller.lock", blocking=False):
            pytest.fail("live diagnostic must hold the controller lock")
        return ReviewResult(approved=True, summary="fixture")
    monkeypatch.setattr(ProviderRouter, "invoke", invoke)
    result = CliRunner().invoke(app, ["doctor", "--live", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output


async def test_maintenance_error_does_not_disable_cancellation(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    polls, cancelled = [], []
    async def tick(*args):
        polls.append(1)
        if len(polls) == 2:
            workflow.store.checkpoint(rid, "control/cancel", True)
    def broken_config():
        raise AA2Error("bad configuration")
    monkeypatch.setattr(asyncio, "sleep", tick)
    monkeypatch.setattr(workflow.config, "load", broken_config)
    await workflow._collect_periodically(rid, SimpleNamespace(cancel=lambda: cancelled.append(1)))
    assert len(polls) == 2 and cancelled == [1]


def test_display_failures_do_not_replace_cancellation():
    def event(run, kind, data):
        if kind != "activity.started":
            raise sqlite3.OperationalError("injected")
    with pytest.raises(asyncio.CancelledError), observe_activity(SimpleNamespace(event=event), "run", "fixture"):
        raise asyncio.CancelledError()


def test_temporary_registration_failure_leaves_no_orphan(tmp_path, monkeypatch):
    resources = ResourceManager(Store(tmp_path), Cleanup(min_free_bytes=0))
    def fail(*args):
        raise sqlite3.OperationalError("injected")
    monkeypatch.setattr(resources, "register", fail)
    with pytest.raises(sqlite3.OperationalError), resources.temporary("fixture"):
        pytest.fail("setup must fail")
    assert not list((resources.store.root / "tmp").iterdir())


def test_delivery_cleanup_failure_retains_success_and_pending_cleanup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    receipt = {"outcome": "DELIVERED", "target": "main", "candidate": "commit", "source_hash": "source",
               "runtime": {"resources": []}, "evidence": {}, "owned_refs": {}}
    receipt["digest"] = digest(receipt)
    store.complete_delivery(rid, receipt)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    def fail():
        raise OSError("disk usage unavailable")
    monkeypatch.setattr(resources, "collect", fail)
    resources.finish_delivery(rid)
    assert store.run(rid)["status"] == "SUCCEEDED"
    assert store.run(rid)["data"]["cleanup_pending"]


async def test_probe_timeout_is_retryable_and_stops_child(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    stopped = []
    async def spawn(*args, **kwargs):
        return SimpleNamespace(returncode=0)
    async def stop(process):
        stopped.append(process)
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()
    timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: timeout(.01))
    monkeypatch.setattr("auto_agents_2.providers.capture_output", hang)
    router = ProviderRouter(config, store, SimpleNamespace(spawn=spawn, stop=stop))
    with pytest.raises(ProviderError) as caught:
        await router._ensure_material_read(config.resolve("review"), tmp_path, "fixture", False)
    assert caught.value.retryable and len(stopped) == 1


def test_store_closes_connection_when_setup_fails(tmp_path, monkeypatch):
    store = Store(tmp_path)
    closed = []
    class Connection:
        def execute(self, *args):
            raise sqlite3.OperationalError("pragma failed")
        def rollback(self):
            pass
        def close(self):
            closed.append(True)
    monkeypatch.setattr("auto_agents_2.store.sqlite3.connect", lambda *args, **kwargs: Connection())
    with pytest.raises(sqlite3.OperationalError), store.connect():
        pytest.fail("setup must fail")
    assert closed == [True]


@pytest.mark.parametrize("defect", ["oversized", "nested"])
async def test_provider_stream_rejects_bad_records_before_accepting_later_result(tmp_path, defect):
    config, store = configured(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    binary = tmp_path / "provider-fixture"
    emission = ("sys.stdout.write(' ' * (8 * 1024 * 1024 + 1) + '\\n')" if defect == "oversized" else
                "print(json.dumps({'type':'item.started','item':[]}))")
    binary.write_text(f"#!{sys.executable}\nimport sys,json\nfrom pathlib import Path\n"
        "sys.stdin.read()\nresult={'approved':True,'summary':'fixture','findings':[]}\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))\n" + emission + "\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n")
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    write_json(config.path, body)
    rid = store.admit("fixture", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    with pytest.raises(ProviderError, match="8 MiB" if defect == "oversized" else "Malformed"):
        await router.invoke("review", {}, ReviewResult, tmp_path, rid, "fixture")
    assert not list((store.root / "attempts" / rid).iterdir())
    assert not list((store.root / "processes").glob("*.json"))
    assert not store.get(rid, "infra/fixture/codex")["pending"]


async def test_provider_preparation_failure_releases_attempt_directory(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    def failed(*args):
        raise ProviderError("injected preparation failure")
    monkeypatch.setattr("auto_agents_2.providers.create_view", failed)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    with pytest.raises(ProviderError):
        await router._call(config.resolve("review"), "review", {}, ReviewResult, tmp_path, rid, "fixture", True)
    assert not list((store.root / "attempts" / rid).iterdir())
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM resources WHERE kind='temporary' AND refs>0").fetchone()
