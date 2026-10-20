"""Recovery status must survive event redaction and identify cached decisions."""
import asyncio
import sqlite3
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_decisions import ApprovingAgent, configure
from test_recovery_policy import authorize, ready

from auto_agents_2.decisions import DecisionRequest, DecisionResult, DecisionService
from auto_agents_2.display import ProgressDisplay
from auto_agents_2.store import Store
from auto_agents_2.util import digest


def render(events):
    output = StringIO()
    display = ProgressDisplay(Console(file=output, width=200), False)
    for event in events:
        display.event(event)
    display.close()
    return output.getvalue()


@pytest.mark.parametrize("route,reason,flag,expected", [
    ("decision", "recovery_assessment", False, "交由决策代理"),
    ("rule", "established_recovery", False, "控制器规则"),
    ("human", "automation_disabled", False, "需要人工授权"),
    ("human", "recorded_authorization", True, "复用人工授权"),
    ("human", "recorded_answer", True, "核对已保存的人工回答"),
    ("decision", "recovery_assessment", "<redacted>", "交由决策代理"),
    ("human", "automation_disabled", "false", "需要人工授权"),
])
def test_routing_uses_controller_route_after_event_redaction(tmp_path, route, reason, flag, expected):
    store = Store(tmp_path)
    store.event(None, "recovery.routing", {"phase": "task", "action": "diagnose", "route": route,
        "reason_code": reason, "recorded_authorization": flag,
        "authorization": "EXAMPLE_AUTHORIZATION_PLACEHOLDER"})
    events = store.events(None)
    assert events[0]["data"]["authorization"] == "<redacted>"
    output = render(events)
    assert expected in output and "EXAMPLE_AUTHORIZATION_PLACEHOLDER" not in output
    if expected != "复用人工授权":
        assert "复用人工授权" not in output


@pytest.mark.parametrize("value", ["<redacted>", "false", "true", 1, None, False, True])
def test_only_explicit_boolean_can_report_an_applied_grant(value):
    output = render([{"kind": "recovery.classified", "time": 1, "data": {
        "phase": "task", "reason_code": "review", "route": "human", "authorized": value}}])
    assert ("用户已授权" in output) is (value is True)


async def test_pause_resume_reports_cached_decision_then_consumes_only_explicit_approval(tmp_path):
    class DeferringAgent(ApprovingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "decision":
                self.calls.append((stage, True))
                return DecisionResult(action="ask_user", reason="EXAMPLE_CONTEXT_CAPACITY_FAILURE_PLACEHOLDER")
            return await super().invoke(stage, *args, **kwargs)

    workflow, rid, work, plan, failure = ready(tmp_path, DeferringAgent())
    counters = {"rounds/T3": {"round": 9}, "recovery/budgets/diagnosis/T3": {"round": 5}}
    workflow.store.checkpoints(rid, counters)

    async def pause(question):
        return "n"

    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, plan, failure, force_decision=True)
    key, saved = workflow.store.decision_requests(rid)[0]
    assert saved["result"]["action"] == "ask_user" and saved["attempts"] == 1
    assert not workflow.store.recovery_history(rid, "T3")["recent"]
    before = workflow.store.events(rid)[-1]["seq"]

    async def remain_pending(question):
        raise asyncio.CancelledError

    workflow.question_handler = remain_pending
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, plan, failure, force_decision=True)
    events = [row for row in workflow.store.events(rid) if row["seq"] > before]
    reused = [row for row in events if row["kind"] == "decision.reused"]
    assert len(reused) == 1 and reused[0]["data"]["action"] == "ask_user"
    assert reused[0]["data"]["request_key"] == key
    output = render(events)
    assert "复用已保存的决策" in output and "等待用户确认" in output
    assert "复用人工授权" not in output
    assert workflow.store.get(rid, key) == saved
    assert workflow.agent.calls == [("decision", True)]
    assert len(workflow.store.pending(rid)) == 1
    assert not workflow.store.recovery_history(rid, "T3")["recent"]

    async def approve(question):
        return "y"

    workflow.question_handler = approve
    _, permit_key = await authorize(workflow, rid, work, plan, failure, force_decision=True)
    assert workflow.store.get(rid, permit_key)["actor"] == "human"
    assert not workflow.store.pending(rid)
    assert len(workflow.store.recovery_history(rid, "T3")["recent"]) == 1
    assert (await authorize(workflow, rid, work, plan, failure, force_decision=True))[1] == permit_key
    assert workflow.agent.calls == [("decision", True)]
    assert all(workflow.store.get(rid, key) == value for key, value in counters.items())


@pytest.mark.parametrize("answer", ["y", "n"])
async def test_recorded_answer_is_reconciled_before_claiming_a_grant(tmp_path, answer):
    workflow, rid, work, plan, failure = ready(tmp_path)
    configure(workflow, automation={"enabled": False})

    async def leave_pending(question):
        raise asyncio.CancelledError

    workflow.question_handler = leave_pending
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, work, plan, failure, force_decision=True)
    question = workflow.store.pending(rid)[0]
    workflow.store.answer(question["id"], answer)
    before = workflow.store.events(rid)[-1]["seq"]
    if answer == "n":
        with pytest.raises(asyncio.CancelledError):
            await authorize(workflow, rid, work, plan, failure, force_decision=True)
        assert not workflow.store.recovery_history(rid, "T3")["recent"]
    else:
        _, key = await authorize(workflow, rid, work, plan, failure, force_decision=True)
        assert workflow.store.get(rid, key)["actor"] == "human"
    events = [row for row in workflow.store.events(rid) if row["seq"] > before]
    output = render(events)
    assert "核对已保存的人工回答" in output and "复用人工授权" not in output
    assert not workflow.agent.calls


async def test_cached_decision_survives_progress_event_failure(tmp_path, monkeypatch):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    request = DecisionRequest(kind="recovery_action", owner="T1", allowed_choices=["ask_user"],
        scope={}, evidence={}, state_identity="same")
    result = DecisionResult(action="ask_user", reason="Retained response")
    key = "decisions/requests/" + digest(request.model_dump())
    saved = {"request": request.model_dump(), "attempts": 1, "validation_version": 4, "result": result.model_dump()}
    store.checkpoint(rid, key, saved)

    async def unexpected(*args, **kwargs):
        pytest.fail("A display failure must not trigger a provider call")

    def fail_event(*args, **kwargs):
        raise sqlite3.OperationalError("EXAMPLE_EVENT_WRITE_FAILURE_PLACEHOLDER")

    monkeypatch.setattr(store, "event", fail_event)
    service = DecisionService(SimpleNamespace(store=store, _invoke=unexpected))
    assert await service.propose(rid, request, tmp_path) == (key, result)
    assert store.get(rid, key) == saved
