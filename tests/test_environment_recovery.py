import asyncio
import sys

import pytest
from test_manifest_references import plan
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import AA2Error, DependencyResolutionError, Waiting
from auto_agents_2.models import Change, ImplementationResult
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import atomic_write, write_json


@pytest.mark.asyncio
@pytest.mark.parametrize("message,stream,mode,repairable", [
    ("WARNING conda.models.version: Using .* is deprecated\nResolvePackageNotFound:\n  - nodejs=22.20", "stderr", "manifest", True),
    ("PackagesNotFoundError: nodejs=22.20", "stdout", "manifest", True),
    ("UnsatisfiableError: incompatible dependencies", "stderr", "manifest", True),
    ("LibMambaUnsatisfiableError: incompatible dependencies", "stderr", "manifest", True),
    ("CondaHTTPError: unavailable channel", "stderr", "manifest", False),
    ("PermissionError: cache is read-only", "stderr", "manifest", False),
    ("WARNING: PackagesNotFoundError: example only", "stderr", "manifest", False),
    ("PackagesNotFoundError: nodejs=22.20", "stderr", "default", False),
    ("PackagesNotFoundError: missing locked package", "stderr", "lock", False),
])
async def test_conda_failure_classification_uses_manifest_and_actual_error(tmp_path, message, stream, mode, repairable):
    config, store = configured(tmp_path)
    rid = store.admit("dependency repair", {})["id"]
    executable = tmp_path / "fixture-conda"
    executable.write_text(f"#!{sys.executable}\nimport sys\n"
                          f"print({message!r}, file=sys.{stream})\nsys.exit(1)\n")
    executable.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["environment"]["conda"] = str(executable)
    write_json(config.path, body)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if mode != "default":
        (workspace / "environment.yml").write_text("dependencies:\n  - python=3.12\n  - nodejs=22.20\n")
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    if mode == "lock":
        prepare = manager._prepare

        async def locked(prefix, ready, fingerprint, *args):
            atomic_write(store.root / "environment-records" / fingerprint / "conda.lock", "@EXPLICIT\n")
            atomic_write(store.root / "environment-records" / fingerprint / "pip.lock", "")
            return await prepare(prefix, ready, fingerprint, *args)

        manager._prepare = locked
    with pytest.raises(AA2Error) as caught:
        await manager.ensure(workspace, "python")
    assert isinstance(caught.value, DependencyResolutionError) == repairable
    assert message in str(caught.value)
    if repairable:
        assert "Conda manifest environment.yml" in str(caught.value)
    assert not list((store.root / "environments").glob("*/aa2-ready.json"))
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id=? AND key LIKE 'environment/logs/%'",
                          (rid,)).fetchone()[0]


@pytest.mark.asyncio
async def test_dependency_failure_scrubs_credentials_and_keeps_stdout(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    executable = tmp_path / "fixture"
    executable.write_text(f"#!{sys.executable}\nimport sys\n"
                          "print('PackagesNotFoundError: nodejs=22.20')\n"
                          "print('https://user:private-pass@example.test/channel?token=private-token', file=sys.stderr)\n"
                          "print('private-value', file=sys.stderr)\nsys.exit(1)\n")
    executable.chmod(0o755)
    with pytest.raises(DependencyResolutionError) as caught:
        await manager._command([str(executable)], tmp_path, {"API_KEY": "private-value"}, [tmp_path],
                               conda_manifest=True)
    assert "nodejs=22.20" in str(caught.value)
    assert all(secret not in str(caught.value) for secret in ("private-pass", "private-token", "private-value"))


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_resolution_failure_is_repaired_and_verified_after_resume(tmp_path, interrupt):
    class RepairAgent(CodingAgent):
        attempts = 0
        interrupted = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "implement":
                if self.attempts:
                    assert "nodejs=22.20" in context["feedback"]["environment_error"]
                    assert (workspace / "calc.py").is_file()
                    if interrupt and not self.interrupted:
                        self.interrupted = True
                        raise asyncio.CancelledError()
                    self.attempts += 1
                    return ImplementationResult(summary="repair manifest", changes=[
                        Change(path="environment.yml", content="dependencies:\n  - python=3.12\n  - nodejs=22\n")])
                self.attempts += 1
                result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
                result.changes.append(Change(path="environment.yml", content="dependencies:\n  - nodejs=22.20\n"))
                return result
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = RepairAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("addition", {})["id"]
    proposal = plan(environment_file="environment.yml")
    proposal.tasks[0].allowed_paths.append("environment.yml")
    base = workflow.workspaces.committed_baseline(rid, "main")
    context = {"specification": {"description": "addition"}}
    attempts = []
    original_environment = workflow._environment

    async def environment(work, *args):
        text = (work / "environment.yml").read_text()
        attempts.append(text)
        if "22.20" in text:
            raise DependencyResolutionError("Conda manifest environment.yml: ResolvePackageNotFound: nodejs=22.20")
        return await original_environment(work, *args)

    workflow._environment = environment
    if interrupt:
        with pytest.raises(asyncio.CancelledError):
            await workflow._task(rid, proposal.tasks[0], base, proposal, context)
        assert workflow.store.get(rid, "rounds/T1")["round"] == 2
        workflow = engine(tmp_path, agent)
        workflow._environment = environment
    await workflow._task(rid, proposal.tasks[0], base, proposal, context)
    assert len(attempts) == agent.attempts == 2
    assert workflow.store.get(rid, "rounds/T1")["round"] == 2
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["commit"] and saved["checks"][0]["exit_code"] == 0 and saved["checks"][0]["tests"] > 0
    failure_key = "candidate/environment-failures/T1/p0/r1"
    failure = workflow.store.get(rid, failure_key)
    assert failure["phase"] == "rejected" and failure["implementation"] and failure["patch"]
    assert not failure.get("checks") and not failure.get("commit")
    assert failure["source_hash"] != saved["source_hash"]
    workflow.store.prepare_replan(rid, "plan", "exercise recovery retention")
    workflow.store.complete_replan(rid, ["plan", "develop"], [])
    assert workflow.store.get(rid, failure_key) == failure


@pytest.mark.asyncio
async def test_infrastructure_failure_keeps_applied_candidate_for_retry(tmp_path):
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("addition", {})["id"]
    proposal = plan()
    base = workflow.workspaces.committed_baseline(rid, "main")
    environment = workflow._environment

    async def unavailable(*args):
        raise AA2Error("CondaHTTPError: channel unavailable")

    workflow._environment = unavailable
    with pytest.raises(AA2Error, match="CondaHTTPError"):
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["phase"] == "applied" and "environment_error" not in saved
    workflow._environment = environment
    await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert sum(stage == "implement" for stage, _ in agent.calls) == 1
    assert workflow.store.get(rid, "rounds/T1")["round"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_rounds,expected,rounds", [(4, RecoveryNeeded, 2), (2, RecoveryNeeded, 2)])
async def test_dependency_repair_preserves_round_and_stall_budgets(tmp_path, max_rounds, expected, rounds):
    agent = CodingAgent()
    workflow = engine(tmp_path, agent)
    body = workflow.config.load().model_dump(mode="json")
    body["execution"]["max_rounds"] = max_rounds
    write_json(workflow.config.path, body)
    rid = workflow.store.admit("addition", {})["id"]
    proposal = plan()
    base = workflow.workspaces.committed_baseline(rid, "main")

    async def unresolved(*args):
        raise DependencyResolutionError("ResolvePackageNotFound: nodejs=22.20")

    async def wait(*args):
        raise Waiting("round budget")

    workflow._environment = unresolved
    workflow._ask = wait
    with pytest.raises(expected) as caught:
        await workflow._task(rid, proposal.tasks[0], base, proposal, {"specification": {}})
    assert workflow.store.get(rid, "rounds/T1")["round"] == rounds
    saved = workflow.store.get(rid, "develop/task/T1")
    assert not saved.get("commit") and not saved.get("checks")
    assert workflow.convergence.state(rid, "T1")["missing"] == 2
    before = len(agent.calls)
    workflow.recovery.submit(rid, caught.value.failure)
    with pytest.raises(Waiting):
        await workflow.recovery.resume(rid)
    assert len(agent.calls) == before  # No diagnostic/decision call after the stop.
