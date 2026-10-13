import asyncio
import os
import sys

import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.config import ConfigStore
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import ProviderError
from auto_agents_2.models import Command, PlanResult, ReviewResult, TaskSpec
from auto_agents_2.planning import ready_batch, validate_plan
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService
from auto_agents_2.workspace import WorkspaceManager


def configured(project):
    config = ConfigStore(project)
    config.initialize()
    body = config.load().model_dump(mode="json")
    body["execution"]["infra_retries"] = 0
    body["cleanup"]["min_free_bytes"] = 0
    for name in body["providers"]:
        body["providers"][name]["binary"] = sys.executable
        body["providers"][name]["profile_map"] = {key: {"model": "test"} for key in ("balanced", "deep", "max")}
    write_json(config.path, body)
    return config, Store(project)


@pytest.mark.asyncio
async def test_failover_keeps_new_provider_when_old_parallel_call_finishes(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("idea", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    started, finish = asyncio.Event(), asyncio.Event()

    async def call(effective, stage, context, *args):
        if context.get("slow"):
            started.set()
            await finish.wait()
        elif effective["provider"] == "codex":
            raise ProviderError("at capacity")
        return ReviewResult(approved=True, summary=effective["provider"])

    router._call = call
    first = asyncio.create_task(router.invoke("review", {"slow": True}, ReviewResult, tmp_path, rid, "a"))
    await started.wait()
    second = await router.invoke("review", {}, ReviewResult, tmp_path, rid, "b")
    assert second.summary == "antigravity"
    assert config.load().active_provider == "antigravity"
    finish.set()
    assert (await first).summary == "codex"
    assert config.load().active_provider == "antigravity"


def test_stale_failover_cannot_overwrite_explicit_selection(tmp_path):
    config, _ = configured(tmp_path)
    revision = config.load().config_revision
    config.activate("antigravity")
    config.activate("codex")
    assert config.activate("antigravity", expected_revision=revision) == ("codex", "codex")


@pytest.mark.asyncio
async def test_infrastructure_budget_survives_router_restart_until_user_grant(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("idea", {})["id"]
    calls = []

    async def fail(effective, *args):
        calls.append(effective["provider"])
        raise ProviderError("capacity", retryable=True)

    for _ in range(2):
        router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
        router._call = fail
        with pytest.raises(ProviderError):
            await router.invoke("review", {}, ReviewResult, tmp_path, rid, "same-operation")
    assert calls == ["codex", "antigravity"]
    router.grant_retry(rid, "same-operation")
    with pytest.raises(ProviderError):
        await router.invoke("review", {}, ReviewResult, tmp_path, rid, "same-operation")
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["codex", "antigravity"])
async def test_real_cli_subprocess_contract_and_fresh_review(tmp_path, kind):
    config, store = configured(tmp_path)
    binary = tmp_path / "provider-fixture"
    binary.write_text(f"#!{sys.executable}\n" + '''import json, sys
args=sys.argv[1:]
result={"approved": True, "summary": "fixture verified", "findings": []}
if "exec" in args:
    assert "read-only" in args
    assert args[args.index("-m")+1] == "test"
    assert args[args.index("-c")+1] == "model_reasoning_effort=low"
    assert sys.stdin.read()
    if "resume" in args: assert args.index("-s") < args.index("resume")
    if "-o" in args:
        open(args[args.index("-o")+1], "w").write(json.dumps(result))
    print(json.dumps({"type":"thread.started","thread_id":"fixture-session"}))
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":3}}))
else:
    assert "--json-schema" in args and "--input-format" in args
    assert args[args.index("--model")+1] == "test"
    assert args[args.index("--effort")+1] == "low"
    assert json.loads(sys.stdin.readline())["event"] == "user"
    print(json.dumps({"event":"result","result":{"status":"SUCCESS","conversation_id":"fixture-session","structured_output":result,"usage":{"input_tokens":11,"output_tokens":3,"total_tokens":14}}}))
''')
    binary.chmod(0o755)
    native = tmp_path / "native"
    native.mkdir()
    body = config.load().model_dump(mode="json")
    body["active_provider"] = kind
    body["providers"] = {kind: body["providers"][kind]}
    body["providers"][kind].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    body["providers"][kind]["profile_map"]["balanced"]["effort"] = "high"
    body["providers"][kind]["extra_args"] = (
        ["-c", "model_reasoning_effort=low"] if kind == "codex" else ["--effort", "low"]
    )
    write_json(config.path, body)
    rid = store.admit("idea", {})["id"]
    from auto_agents_2.transports import AntigravityTransport, CodexTransport
    adapter = CodexTransport() if kind == "codex" else AntigravityTransport()
    adapter.native_home = lambda _: native
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True), transports={kind: adapter})
    for fresh in (False, False, True):
        result = await router.invoke("review", {}, ReviewResult, tmp_path, rid, "operation", fresh)
        assert result.approved
    assert store.usage_summary(rid)["total_tokens"] == 42
    starts = [event["data"] for event in store.events(rid) if event["kind"] == "agent.started"]
    assert len(starts) == 3
    assert all(event["model"] == "test" and event["effort"] == "low" for event in starts)
    assert not list((store.root / "processes").glob("*.json"))
    assert not list((store.root / "attempts" / rid).iterdir())


@pytest.mark.asyncio
async def test_junit_evidence_rejects_skip_only_and_cleans_temporary_data(tmp_path):
    config, store = configured(tmp_path)
    store.admit("idea", {})
    test = tmp_path / "test_sample.py"
    test.write_text("import pytest\n@pytest.mark.skip(reason='not implemented')\ndef test_missing():\n    assert False\n")
    verifier = VerificationService(store.root, SandboxRunner(store.root, _test_unconfined=True))
    command = Command(argv=[sys.executable, "-m", "pytest", "-q", str(test)])
    skipped = (await verifier.run([command], tmp_path))[0]
    assert skipped.exit_code != 0 and skipped.skipped == 1 and skipped.tests == 0
    test.write_text("def test_working():\n    assert 2 + 3 == 5\n")
    valid = (await verifier.run([command], tmp_path))[0]
    assert valid.exit_code == 0 and valid.tests == 1
    assert valid.test_ids == ["test_sample::test_working"]
    assert "testcase" in ArtifactStore(store.root).read(valid.report)
    assert not list((store.root / "tmp").glob("check_*"))


@pytest.mark.asyncio
async def test_timeout_kills_process_and_reclaims_scratch(tmp_path):
    _, store = configured(tmp_path)
    verifier = VerificationService(store.root, SandboxRunner(store.root, _test_unconfined=True))
    result = (await verifier.run([Command(argv=[sys.executable, "-c", "import time; time.sleep(60)"], timeout=1)], tmp_path))[0]
    assert result.timed_out and result.exit_code != 0
    assert not list((store.root / "processes").glob("*.json"))
    assert not list((store.root / "tmp").glob("check_*"))


def test_gc_reclaims_disposable_but_preserves_running_and_recovery(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("idea", {})["id"]
    manager = ResourceManager(store, config.load().cleanup)
    recovery = store.root / "tmp/recovery"
    recovery.mkdir(parents=True)
    resource = manager.register(recovery, "recovery", rid)
    manager.release(resource)
    with manager.temporary("test") as temporary:
        (temporary / "data").write_text("scratch")
        assert manager.collect()["removed"] == 0
    assert not temporary.exists() and recovery.exists()


def test_gc_recovers_crash_between_success_and_resource_release(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("idea", {})["id"]
    path = store.root / "sessions" / "orphaned-session"
    path.mkdir(parents=True)
    manager = ResourceManager(store, config.load().cleanup)
    manager.register(path, "session", rid)
    store.update(rid, status="SUCCEEDED")
    assert manager.collect()["removed"] == 1
    assert not path.exists()


def test_hard_crash_reports_incomplete_usage(tmp_path):
    _, store = configured(tmp_path)
    rid = store.admit("idea", {})["id"]
    store.event(rid, "agent.started", {"attempt": "interrupted-before-usage"})
    assert store.usage_summary(rid)["complete"] is False


@pytest.mark.asyncio
async def test_environment_shares_equal_dependencies_and_separates_private_installs(tmp_path):
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    prepared = []

    async def prepare(prefix, ready, fingerprint, *args):
        prepared.append(fingerprint)
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/python").touch()
        write_json(ready, {"fingerprint": fingerprint})

    manager._prepare = prepare
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        (root / "requirements.txt").write_text("pytest==9.0.3\n")
    envs = await asyncio.gather(manager.ensure(a, "python"), manager.ensure(b, "python"))
    assert envs[0] == envs[1] and len(prepared) == 1
    (b / "docs/aa2").mkdir(parents=True)
    (b / "docs/aa2/requirements.lock").write_text("irrelevant delivery evidence")
    assert await manager.ensure(b, "python") == envs[0]
    (b / "requirements.txt").write_text("pytest==8.4.2\n")
    assert await manager.ensure(b, "python") != envs[0]
    for root in (a, b):
        (root / "requirements.txt").write_text("-e .\n")
    assert await manager.ensure(a, "python") != await manager.ensure(b, "python")


def test_planning_orders_conflicting_writes_and_checks_coverage():
    def task(name, path, deps=()):
        return TaskSpec(id=name, title=name, description=name, requirement_ids=["R1"],
                        allowed_paths=[path], depends_on=list(deps), acceptance=["passes"], checks=[Command(argv=["pytest"])])
    tasks = [task("A", "src/a"), task("B", "src/a/shared.py"), task("C", "src/c"), task("D", "src/d", ["A"])]
    assert [t.id for t in ready_batch(tasks, set(), 2)] == ["A", "C"]
    assert [t.id for t in ready_batch(tasks, {"A", "C"}, 2)] == ["B", "D"]
    plan = PlanResult(summary="plan", document="plan", tasks=tasks, final_checks=[Command(argv=["pytest"])])
    validate_plan(plan, {"R1"})
    plan.tasks[0].depends_on = ["D"]
    with pytest.raises(Exception, match="cycle"):
        validate_plan(plan, {"R1"})


def test_plan_review_does_not_require_implementation_evidence():
    prompt, _ = PromptCompiler().compile("review", {"review_kind": "plan"})
    assert "PRE-IMPLEMENTATION" in prompt and "not expected to exist yet" in prompt


def test_generated_output_symlink_cannot_grant_write_access_elsewhere(tmp_path):
    from auto_agents_2.errors import PolicyViolation
    _, store = configured(tmp_path)
    runner = SandboxRunner(store.root, binary=sys.executable)
    modules = tmp_path / "node_modules"
    modules.mkdir()
    unrelated = tmp_path / "unrelated-source"
    unrelated.mkdir()
    (modules / ".vite").symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="symlink"):
        runner.command([sys.executable, "-c", "pass"], tmp_path, [modules / ".vite"])


@pytest.mark.asyncio
async def test_real_sandbox_protects_source_and_state(tmp_path):
    _, store = configured(tmp_path)
    WorkspaceManager(tmp_path).initialize()
    runner = SandboxRunner(store.root)
    ok, reason = runner.probe()
    if not ok:
        pytest.skip("OS isolation unavailable: " + reason)
    source = tmp_path / "source.txt"
    source.write_text("unchanged")
    scratch = store.root / "tmp/allowed"
    script = """from pathlib import Path
import sys
p=Path(sys.argv[1]); state=Path(sys.argv[2]); out=Path(sys.argv[3])
assert not (state/'state.sqlite').exists()
try: p.write_text('bad')
except PermissionError: pass
except OSError as e: assert e.errno == 30
else: raise AssertionError('source was writable')
(out/'ok').write_text('ok')
"""
    process = await runner.spawn([sys.executable, "-c", script, str(source), str(store.root), str(scratch)],
                                 tmp_path, dict(os.environ), [scratch])
    try:
        _, error = await process.communicate()
        assert process.returncode == 0, error.decode()
        assert source.read_text() == "unchanged" and (scratch / "ok").exists()
    finally:
        await runner.stop(process)
