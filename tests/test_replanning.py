import asyncio
import sqlite3
import sys

import pytest
from pydantic import ValidationError
from test_workflow import CodingAgent, engine

from auto_agents_2.config import Execution
from auto_agents_2.engine import Replan
from auto_agents_2.evidence import CHECKER_VERSION
from auto_agents_2.models import BrowserAction, CheckResult, Command
from auto_agents_2.replanning import assess, browser_criteria, check_criteria
from auto_agents_2.util import digest


def observation(**criteria):
    return {"checks": {"criteria": criteria}}


def test_progress_compares_fixed_obligations_and_never_rewards_cycles():
    state, decision = assess({}, observation(a=False, b=False, c=False))
    assert decision["baseline"] and state["stalled"] == 0
    state, decision = assess(state, observation(a=True, b=False, c=False))
    assert decision["improved"]
    # Fixing B while regressing A is not progress.
    state, decision = assess(state, observation(a=False, b=True, c=False))
    assert not decision["improved"] and decision["regressions"]
    state, decision = assess(state, observation(a=True, b=False, c=False))
    assert not decision["improved"] and state["stalled"] == 2
    state, decision = assess(state, observation(a=True, b=True, c=False))
    assert not decision["improved"] and state["stalled"] == 3
    # B was already observed passing during a regression. Only an unseen gain counts.
    state, decision = assess(state, observation(a=True, b=True, c=True))
    assert decision["improved"] and state["stalled"] == 0
    # Removing C, or adding a trivial pass, cannot manufacture improvement.
    state, decision = assess(state, observation(a=True, b=True, invented=True))
    assert not decision["improved"] and state["stalled"] == 1
    state, decision = assess(state, {"renamed": {"criteria": {"new": True}}})
    assert not decision["improved"] and state["stalled"] == 2


def test_browser_progress_requires_executed_matching_assertions():
    actions = [BrowserAction(route="/index.html", action="click", selector="button"),
               BrowserAction(route="/index.html", action="expect_text", selector="#count", value="1")]
    rows = [{"viewport": width, "page": "index.html", "status": 200, "errors": [], "actions": []}
            for width in (1440, 390)]
    baseline = {"browser": {"criteria": browser_criteria(actions, rows)}}
    state, _ = assess({}, baseline)
    assert not any(baseline["browser"]["criteria"].values())
    # A new screenshot, reported self-improvement, or only clicks prove nothing.
    for row in rows:
        row.update(screenshot="new.png", summary="all fixed", actions=[actions[0].model_dump()])
    state, decision = assess(state, {"browser": {"criteria": browser_criteria(actions, rows)}})
    assert not decision["improved"]
    for row in rows:
        row["actions"] = [a.model_dump() for a in actions]
    state, decision = assess(state, {"browser": {"criteria": browser_criteria(actions, rows)}})
    assert decision["improved"]
    weakened = [actions[0], actions[1].model_copy(update={"value": "anything"})]
    for row in rows:
        row["actions"] = [a.model_dump() for a in weakened]
    _, decision = assess(state, {"browser": {"criteria": browser_criteria(weakened, rows)}})
    assert not decision["improved"] and decision["regressions"]


def test_check_progress_requires_current_source_environment_and_actual_junit_cases():
    command = Command(argv=["pytest", "tests"], expected_tests=2)
    result = CheckResult(argv=command.argv, cwd=".", exit_code=1, duration=1, output="999 passed",
                         source_hash="source", environment_hash="env", tests=999,
                         checker_version=CHECKER_VERSION, report_status="valid",
                         command_hash=digest(command.model_dump()),
                         test_ids=["a", "b"], passed_test_ids=["a"], report={"hash": "report"})
    criteria = check_criteria([command], [result], "source", "env")
    assert sum(criteria.values()) == 1
    for update in ({"checker_version": "verification-v2"}, {"failure_reasons": ["execution_inputs_changed"]},
                   {"failure_reasons": ["incomplete_log_capture"]}, {"report_status": "invalid"}):
        assert not any(check_criteria([command], [result.model_copy(update=update)], "source", "env").values())
    assert not any(check_criteria([command], [result], "changed", "env").values())
    assert not any(check_criteria([command], [result], "source", "changed").values())
    stale = result.model_copy(update={"command_hash": "different check contract"})
    assert not any(check_criteria([command], [stale], "source", "env").values())
    result.timed_out = True
    assert not any(check_criteria([command], [result], "source", "env").values())
    result.timed_out = False
    state, _ = assess({}, {"tests": {"criteria": criteria}})
    # Changing the test command cannot reset its history or close its failures.
    replacement = Command(argv=["pytest", "tests", "-k", "a"], expected_tests=1)
    result = result.model_copy(update={"argv": replacement.argv, "exit_code": 0,
                                      "command_hash": digest(replacement.model_dump())})
    _, decision = assess(state, {"tests": {"criteria": check_criteria([replacement], [result], "source", "env")}})
    assert not decision["improved"] and decision["regressions"]


async def test_junit_records_only_passed_ids_even_in_a_failed_suite(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    (tmp_path / "test_cases.py").write_text("""import pytest
def test_pass(): assert 1 + 1 == 2
def test_fail(): assert False
@pytest.mark.skip(reason='not verified')
def test_skip(): pass
""")
    command = Command(argv=[sys.executable, "-m", "pytest", "-q", "test_cases.py"])
    result = (await workflow.verifier.run([command], tmp_path))[0]
    assert result.exit_code != 0 and result.tests == 1 and result.skipped == 1
    assert result.passed_test_ids == ["test_cases::test_pass"]
    assert len(result.test_ids) == 3 and result.report


def test_stall_limit_rejects_disabled_monitoring_and_removed_config():
    config = Execution()
    assert config.max_stalled_replans == 2
    with pytest.raises(ValidationError):
        Execution(max_stalled_replans=0)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Execution.model_validate({"max_replans": 2})


async def test_verified_progress_continues_beyond_stall_limit(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    calls = 0

    async def execute(rid):
        nonlocal calls
        calls += 1
        if calls <= 5:
            workflow._replan_evidence(rid, "checks", {str(i): i < calls - 1 for i in range(4)})
            raise Replan("plan", "still working")
        return workflow.store.run(rid)

    workflow._execute = execute
    result = await workflow.start("idea")
    assert calls == 6 and result["data"]["replans"] == 5
    progress = workflow.store.get(result["id"], "replanning/progress")
    assert progress["stalled"] == 0 and progress["evaluations"] == 5
    assert not workflow.store.pending(result["id"])


async def test_stall_survives_restart_provider_switch_and_single_manual_grant(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    calls = 0

    async def execute(rid):
        nonlocal calls
        calls += 1
        # Changing the reason or target stage is not verified progress.
        raise Replan("prototype" if calls % 2 else "plan", f"different prose {calls}")

    async def pause(question):
        assert question["key"].startswith("replan-stalled/")
        assert "连续 2 次" in question["prompt"]
        return "n"

    workflow._execute = execute
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start("idea")
    rid = workflow.store.current()["id"]
    assert calls == 3 and workflow.store.run(rid)["data"]["replans"] == 2
    assert workflow.store.run(rid)["status"] == "PAUSED"
    pending = workflow.store.get(rid, "replanning/pending")
    workflow.config.activate("antigravity")
    workflow = engine(tmp_path, CodingAgent())
    workflow._execute = execute
    asked = []

    async def grant_once(question):
        asked.append(question)
        if len(asked) == 1:
            assert calls == 3  # Resume asks before any repeated provider work.
            assert workflow.store.get(rid, "replanning/pending") == pending
            return "y"
        assert calls == 4 and "连续 3 次" in question["prompt"]
        return "n"

    workflow.question_handler = grant_once
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    assert calls == 4 and workflow.store.run(rid)["data"]["replans"] == 3
    assert workflow.store.get(rid, "replanning/progress")["evaluations"] == 4
    assert workflow.store.get(rid, "replanning/progress")["stalled"] == 3
    assert workflow.config.load().active_provider == "antigravity"


async def test_crash_before_invalidation_recovers_decision_without_recounting(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("idea", {})["id"]
    workflow.store.checkpoint(rid, "clarify", {"accepted": True})
    workflow.store.checkpoint(rid, "prototype/pending", {"candidate": "keep until rollback"})
    (tmp_path / "user.txt").write_text("keep my work")

    async def reject(rid):
        raise Replan("prototype", "missing evidence")

    def crash(*args):
        raise asyncio.CancelledError()

    workflow._execute = reject
    monkeypatch.setattr(workflow, "archive_workers", crash)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    assert workflow.store.get(rid, "prototype/pending")
    assert workflow.store.run(rid)["data"]["replans"] == 0
    workflow = engine(tmp_path, CodingAgent())

    async def continued(rid):
        assert workflow.store.get(rid, "prototype/pending") is None
        assert workflow.store.get(rid, "clarify") == {"accepted": True}
        return workflow.store.run(rid)

    workflow._execute = continued
    result = await workflow.start()
    assert result["data"]["replans"] == 1
    assert workflow.store.get(rid, "replanning/progress")["evaluations"] == 1
    assert (tmp_path / "user.txt").read_text() == "keep my work"


def test_rollback_transaction_failure_preserves_pending_candidate_and_counters(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    store = workflow.store
    rid = store.admit("idea", {})["id"]
    store.checkpoint(rid, "prototype/pending", {"candidate": "saved"})
    pending = store.prepare_replan(rid, "prototype", "review rejected")
    assert store.prepare_replan(rid, "plan", "replayed failure") == pending
    with store.connect() as db:
        db.execute("CREATE TRIGGER crash BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.complete_replan(rid, ["prototype"], [])
    assert store.get(rid, "prototype/pending") == {"candidate": "saved"}
    assert store.get(rid, "replanning/pending") == pending
    assert store.run(rid)["data"]["replans"] == 0
    with store.connect() as db:
        db.execute("DROP TRIGGER crash")
    store.complete_replan(rid, ["prototype"], [])
    store.complete_replan(rid, ["prototype"], [])
    assert store.run(rid)["data"]["replans"] == 1
    assert store.get(rid, "replanning/progress")["evaluations"] == 1
