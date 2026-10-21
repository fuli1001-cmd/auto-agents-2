"""Real managed-provider cancellation, persistent accounting and decision re-entry."""
import asyncio
import sys

import pytest
from test_runtime import configured

from auto_agents_2.decisions import DecisionRequest, DecisionResult
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.errors import PolicyViolation, ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, write_json


def fixture(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config, store = configured(project)
    native = tmp_path / "native"
    native.mkdir()
    mode = tmp_path / "mode"
    mode.write_text("hang")
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\nMODE = {str(mode)!r}\n" + '''
import json, pathlib, sys, time
args = sys.argv[1:]
sys.stdin.read()
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
decision = "action" in schema["properties"]
result = ({"action": "approve", "reason": "fixture scoped continuation", "evidence_refs": ["checks"],
           "answers": [], "selected_action": "diagnose", "expected_result": "Observe diagnosis"} if decision else
          {"approved": True, "summary": "fixture response", "findings": []})
mode = pathlib.Path(MODE).read_text()
if mode == "invalid":
    result["evidence_refs"] = ["not_supplied"]
if mode in {"success", "invalid", "response_hang", "unconfirmed_response"}:
    pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
if mode in {"error_hang", "failure"}:
    print(json.dumps({"type": "turn.failed", "error": "fixture provider failure"}), flush=True)
if mode in {"success", "invalid", "response_hang"}:
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}}), flush=True)
print(json.dumps({"type": "thread.started", "thread_id": "fixture-session"}), flush=True)
if mode not in {"success", "invalid", "failure"}:
    time.sleep(120)
''')
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    write_json(config.path, body)
    rid = store.admit("pause fixture", {})["id"]
    ready = asyncio.Event()
    event = store.event

    def observe(run_id, kind, data):
        result = event(run_id, kind, data)
        if kind == "agent.output" and data.get("detail") == "会话已建立":
            ready.set()
        return result

    monkeypatch.setattr(store, "event", observe)
    runner = SandboxRunner(store.root, _test_unconfined=True)
    router = ProviderRouter(config, store, runner)
    return router, project, store, rid, mode, ready


async def cancel_when_ready(coro, ready):
    ready.clear()
    task = asyncio.create_task(coro)
    try:
        await asyncio.wait_for(ready.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def restarted(router, project):
    store = Store(project)
    return ProviderRouter(router.config, store, SandboxRunner(store.root, _test_unconfined=True))


def starts(store, rid):
    return [e for e in store.events(rid) if e["kind"] == "agent.started"]


@pytest.mark.parametrize("stage", ["decision", "recovery_diagnosis", "review", "implement"])
async def test_confirmed_pause_resumes_same_operation_without_a_provider_failure(tmp_path, monkeypatch, stage):
    router, project, store, rid, mode, ready = fixture(tmp_path, monkeypatch)
    schema = DecisionResult if stage == "decision" else ReviewResult
    await cancel_when_ready(router.invoke(stage, {}, schema, project, rid, "op"), ready)
    assert not list((store.root / "processes").glob("*.json"))
    assert store.get(rid, "provider-result/op") is None
    mode.write_text("success")
    result = await restarted(router, project).invoke(stage, {}, schema, project, rid, "op")
    assert result.action == "approve" if stage == "decision" else result.approved
    state = store.get(rid, "infra/op/codex")
    assert state["failures"] == state["grant"] == 0
    assert state["cancellations"] == state["cancellation_resumes"] == 1
    assert len(starts(store, rid)) == 2 and not store.pending(rid)
    assert store.usage_summary(rid)["total_tokens"] == 14


async def test_second_pause_cannot_create_unbounded_redispatch(tmp_path, monkeypatch):
    router, project, store, rid, _, ready = fixture(tmp_path, monkeypatch)
    for _ in range(2):
        await cancel_when_ready(router.invoke("decision", {}, DecisionResult, project, rid, "op"), ready)
    for _ in range(2):
        with pytest.raises(ProviderError, match="budget exhausted"):
            await restarted(router, project).invoke("decision", {}, DecisionResult, project, rid, "op")
    state = store.get(rid, "infra/op/codex")
    assert state["failures"] == 1 and state["cancellations"] == 2
    assert state["cancellation_resumes"] == 1 and state["error_kind"] == "cancelled"
    assert len(starts(store, rid)) == 2


@pytest.mark.parametrize("mode_name", ["error_hang", "response_hang", "unconfirmed_response"])
async def test_known_failure_or_response_is_not_a_clean_pause(tmp_path, monkeypatch, mode_name):
    router, project, store, rid, mode, ready = fixture(tmp_path, monkeypatch)
    mode.write_text(mode_name)
    await cancel_when_ready(router.invoke("decision", {}, DecisionResult, project, rid, "op"), ready)
    mode.write_text("success")
    with pytest.raises(ProviderError, match="budget exhausted"):
        await restarted(router, project).invoke("decision", {}, DecisionResult, project, rid, "op")
    assert len(starts(store, rid)) == 1
    assert not store.get(rid, "infra/op/codex").get("cancellation_resumes")


async def test_cleanup_failure_cannot_authorize_a_pause_resume(tmp_path, monkeypatch):
    router, project, store, rid, _, ready = fixture(tmp_path, monkeypatch)
    processes = []
    spawn, stop = router.sandbox.spawn, router.sandbox.stop

    async def track(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    async def fail_stop(process):
        raise PolicyViolation("fixture cleanup not confirmed")

    monkeypatch.setattr(router.sandbox, "spawn", track)
    monkeypatch.setattr(router.sandbox, "stop", fail_stop)
    try:
        await cancel_when_ready(router.invoke("decision", {}, DecisionResult, project, rid, "op"), ready)
        assert list((store.root / "processes").glob("*.json"))
        with pytest.raises(ProviderError, match="budget exhausted"):
            await restarted(router, project).invoke("decision", {}, DecisionResult, project, rid, "op")
        assert len(starts(store, rid)) == 1
    finally:
        for process in processes:
            await stop(process)


@pytest.mark.parametrize("pending", [True, False])
async def test_crash_or_real_failure_keeps_the_original_budget(tmp_path, monkeypatch, pending):
    router, project, store, rid, _, _ = fixture(tmp_path, monkeypatch)
    state = {"pending": pending, "failures": 0 if pending else 1, "grant": 0}
    store.checkpoint(rid, "infra/op/codex", state)
    for _ in range(2):
        with pytest.raises(ProviderError, match="budget exhausted"):
            await router.invoke("decision", {}, DecisionResult, project, rid, "op")
    assert not starts(store, rid)
    assert store.get(rid, "infra/op/codex")["failures"] == 1


async def test_changed_input_cannot_spend_a_clean_cancellation_receipt(tmp_path, monkeypatch):
    router, project, store, rid, mode, ready = fixture(tmp_path, monkeypatch)
    await cancel_when_ready(router.invoke("decision", {"scope": "original"}, DecisionResult, project, rid, "op"), ready)
    mode.write_text("success")
    with pytest.raises(ProviderError, match="budget exhausted"):
        await restarted(router, project).invoke("decision", {"scope": "changed"}, DecisionResult, project, rid, "op")
    assert len(starts(store, rid)) == 1


async def test_prior_failures_and_grants_survive_clean_pause(tmp_path, monkeypatch):
    router, project, store, rid, mode, ready = fixture(tmp_path, monkeypatch)
    store.checkpoint(rid, "infra/op/codex", {"failures": 2, "grant": 2, "pending": False})
    await cancel_when_ready(router.invoke("decision", {}, DecisionResult, project, rid, "op"), ready)
    mode.write_text("success")
    await restarted(router, project).invoke("decision", {}, DecisionResult, project, rid, "op")
    state = store.get(rid, "infra/op/codex")
    assert state["failures"] == state["grant"] == 2
    assert state["cancellations"] == state["cancellation_resumes"] == 1


@pytest.mark.parametrize("request_kind", ["implementation_grant", "recovery_action"])
@pytest.mark.parametrize("mode_name, action, attempts, call_count", [
    ("success", "approve", 1, 2), ("invalid", "ask_user", 2, 3),
])
async def test_decision_resumes_reserved_attempt_then_reuses_committed_result(
        tmp_path, monkeypatch, request_kind, mode_name, action, attempts, call_count):
    router, project, store, rid, mode, ready = fixture(tmp_path, monkeypatch)
    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox)
    workflow.store = store
    workflow.decisions.store = store
    request = DecisionRequest(kind=request_kind, owner="T01", allowed_choices=["approve", "ask_user"],
                              scope={"candidates": {"diagnose": {"action": "diagnose", "task_ids": ["T01"]}}},
                              evidence={"checks": {"valid": True}}, state_identity="fixture")
    key = "decisions/requests/" + digest(request.model_dump())
    await cancel_when_ready(workflow.decisions.propose(rid, request, project), ready)
    assert store.get(rid, key)["attempts"] == 1
    mode.write_text(mode_name)
    workflow = WorkflowEngine(project, sandbox=SandboxRunner(store.root, _test_unconfined=True))
    resumed_key, result = await workflow.decisions.propose(rid, request, project)
    assert resumed_key == key and result.action == action
    assert store.get(rid, key)["attempts"] == attempts
    assert bool(store.get(rid, key).get("fallback")) == (action == "ask_user")
    assert not store.pending(rid)
    assert (await workflow.decisions.propose(rid, request, project))[1] == result
    assert len(starts(store, rid)) == call_count


async def test_saved_fallback_and_unanswered_question_are_not_migrated(tmp_path, monkeypatch):
    router, project, store, rid, _, _ = fixture(tmp_path, monkeypatch)
    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox)
    request = DecisionRequest(kind="implementation_grant", owner="T01", allowed_choices=["approve", "ask_user"],
                              scope={}, evidence={"checks": {"valid": True}}, state_identity="fixture")
    key = "decisions/requests/" + digest(request.model_dump())
    state = {"request": request.model_dump(), "attempts": 1, "pending": True, "failed": True,
             "error": "ProviderError", "fallback": True, "validation_version": 4,
             "result": DecisionResult(action="ask_user", reason="Existing fallback").model_dump()}
    store.checkpoint(rid, key, state)
    question = store.request(rid, "recovery-cycle/existing", "Existing recovery gate", "fixture-binding")
    assert (await workflow.decisions.propose(rid, request, project))[1].action == "ask_user"
    assert store.get(rid, key) == state
    assert store.question(question["id"]) == question
    assert not starts(store, rid)


@pytest.mark.parametrize("failure_point", ["usage", "receipt"])
async def test_missing_durable_cancellation_evidence_stays_conservative(tmp_path, monkeypatch, failure_point):
    router, project, store, rid, _, ready = fixture(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        if failure_point == "usage":
            def fail_usage(*args, **kwargs):
                raise OSError("fixture usage write failed")
            patch.setattr(store, "add_usage", fail_usage)
        else:
            checkpoint = store.checkpoint
            def fail_receipt(run_id, key, data):
                if key == "infra/op/codex" and data.get("cancellation"):
                    raise OSError("fixture receipt write failed")
                return checkpoint(run_id, key, data)
            patch.setattr(store, "checkpoint", fail_receipt)
        await cancel_when_ready(router.invoke("decision", {}, DecisionResult, project, rid, "op"), ready)
    with pytest.raises(ProviderError, match="budget exhausted"):
        await restarted(router, project).invoke("decision", {}, DecisionResult, project, rid, "op")
    assert len(starts(store, rid)) == 1
    assert not store.get(rid, "infra/op/codex").get("cancellation_resumes")


async def test_cancellation_after_result_commit_reuses_response_without_redispatch(tmp_path, monkeypatch):
    router, project, store, rid, mode, _ = fixture(tmp_path, monkeypatch)
    mode.write_text("success")
    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox)
    checkpoint = store.checkpoints

    def interrupt_after_commit(run_id, values, **kwargs):
        checkpoint(run_id, values, **kwargs)
        if "provider-result/op" in values:
            raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(store, "checkpoints", interrupt_after_commit)
        with pytest.raises(asyncio.CancelledError):
            await workflow._invoke("decision", {}, DecisionResult, project, rid, "op", interactive_retry=False)
    workflow = WorkflowEngine(project, sandbox=SandboxRunner(store.root, _test_unconfined=True))
    result = await workflow._invoke("decision", {}, DecisionResult, project, rid, "op", interactive_retry=False)
    assert result.action == "approve"
    assert len(starts(store, rid)) == 1 and store.usage_summary(rid)["total_tokens"] == 14
    assert not store.get(rid, "infra/op/codex").get("cancellation_resumes")


async def test_real_failure_after_clean_resume_stays_exhausted(tmp_path, monkeypatch):
    router, project, store, rid, mode, ready = fixture(tmp_path, monkeypatch)
    await cancel_when_ready(router.invoke("decision", {}, DecisionResult, project, rid, "op"), ready)
    mode.write_text("failure")
    for _ in range(2):
        with pytest.raises(ProviderError):
            await restarted(router, project).invoke("decision", {}, DecisionResult, project, rid, "op")
    state = store.get(rid, "infra/op/codex")
    assert state["failures"] == 1 and state["cancellation_resumes"] == 1
    assert state["error_kind"] == "unavailable" and len(starts(store, rid)) == 2
