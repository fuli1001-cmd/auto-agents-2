import asyncio
import json
import sys

import pytest
from test_recovery_contracts import failure, plan
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.decisions import DecisionRequest, DecisionResult, FactAnswer
from auto_agents_2.errors import (
    AA2Error,
    ConfigurationError,
    InvalidStructuredResponse,
    PostCommitError,
    ProviderError,
)
from auto_agents_2.evidence import invocation_identity
from auto_agents_2.interaction import choice_spec
from auto_agents_2.models import (
    Change,
    Command,
    Finding,
    ImplementationResult,
    PlanResult,
    ReviewResult,
    TaskSpec,
    UserQuestion,
)
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import Diagnosis, RecoveryNeeded, failure_summary
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git


class ApprovingAgent(CodingAgent):
    async def invoke(self, stage, context, schema, *args, **kwargs):
        if stage == "decision":
            self.calls.append((stage, kwargs.get("fresh", False)))
            return DecisionResult(action="approve", reason="One scoped attempt within the supplied budget",
                evidence_refs=list(context["decision_request"]["evidence"]),
                selected_action=next(iter(context["decision_request"]["scope"].get("candidates", {})), None),
                expected_result="Observe the selected action's actual result")
        return await super().invoke(stage, context, schema, *args, **kwargs)


def setup(tmp_path, agent=None):
    workflow = engine(tmp_path, agent or ApprovingAgent())
    rid = workflow.store.admit("decision tests", {})["id"]
    from evidence_test_fixtures import seed_requirements
    seed_requirements(workflow, rid, plan())
    return workflow, rid


def configure(workflow, **settings):
    data = workflow.config.load().model_dump(mode="json")
    for key, value in settings.items():
        data[key].update(value)
    write_json(workflow.config.path, data)


async def grant(workflow, rid, count=4, kind="implementation_grant", **kwargs):
    return await workflow.decisions.grant(rid, kind=kind, owner="T1", count=count,
        limit_key="budget_grants/T1" if kind == "implementation_grant" else "recovery/diagnosis_grants/T1",
        question_key=f"grant/{kind}/{count}", reason="One scoped retry", work=workflow.project,
        evidence={"checks": {"valid": True}, "diagnosis": {"action": "repair"}}, **kwargs)


def test_old_config_inherits_roles_and_automation_without_overwriting_profiles(tmp_path):
    config, _ = configured(tmp_path)
    data = config.load().model_dump(mode="json")
    data.pop("automation")
    data["efforts"] = {"review": "balanced", "implement": "custom"}
    data["providers"]["codex"]["profile_map"].update({
        "balanced": {"model": "chosen", "effort": "medium"},
        "deep": {"model": "chosen", "effort": "high"}, "max": {"model": "chosen", "effort": "max"}})
    write_json(config.path, data)
    cfg = config.load()
    assert cfg.efforts["implement"] == "custom"
    assert cfg.automation.enabled and cfg.automation.max_implementation_rounds == 12
    assert cfg.automation.max_diagnosis_rounds == 4
    assert [(config.resolve(stage)["model"], config.resolve(stage)["effort"]) for stage in
            ("review", "decision", "recovery_diagnosis")] == [("chosen", "medium"), ("chosen", "high"), ("chosen", "max")]
    data["providers"]["codex"]["profile_map"].pop("max")
    write_json(config.path, data)
    with pytest.raises(ConfigurationError, match="no profile"):
        config.resolve("recovery_diagnosis")
    assert json.loads(config.path.read_text()) == data  # Loading does not rewrite native/user config.


def test_new_defaults_preserve_existing_worker_invocation_receipts(tmp_path):
    from auto_agents_2.readview import VIEW_VERSION
    config, _ = configured(tmp_path)
    cfg = config.load()
    original = cfg.model_dump(mode="json")
    original.pop("automation")
    original.pop("shared_cache")  # This field did not exist in the historical configuration.
    original["efforts"].pop("decision")
    original["efforts"].pop("recovery_diagnosis")
    # Freeze the pre-presentation-split value: changing rendering must not
    # invalidate an already committed worker response.
    old = digest(["invocation-v4", "2026-10-16.2", VIEW_VERSION, "implement", {"input": 1},
                  ImplementationResult.model_json_schema(), "source", original, None])
    assert invocation_identity("implement", {"input": 1}, ImplementationResult, tmp_path, cfg, "source") == old


@pytest.mark.asyncio
async def test_grant_is_one_increment_audited_and_idempotent_across_restart(tmp_path):
    workflow, rid = setup(tmp_path)
    workflow.store.checkpoint(rid, "rounds/T1", {"round": 4, "stalled": 0})
    question = workflow.store.request(rid, "grant/implementation_grant/4", "old prompt")
    await grant(workflow, rid)
    assert workflow.store.get(rid, "budget_grants/T1") == 5
    assert workflow.store.get(rid, "rounds/T1")["round"] == 4
    assert workflow.store.question(question["id"])["answer"] is None
    assert workflow.store.question(question["id"])["superseded"]
    assert not workflow.store.pending(rid)
    audit = [v for k, v in workflow.store.delivery_evidence(rid).items() if k.startswith("decisions/applied/")]
    assert len(audit) == 1 and audit[0]["actor"] == "agent"
    workflow.store = Store(tmp_path)
    await grant(workflow, rid)
    assert len(workflow.agent.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,ceiling", [("implementation_grant", 12), ("diagnosis_grant", 4)])
async def test_legacy_ceiling_does_not_prevent_a_new_decision(tmp_path, kind, ceiling):
    workflow, rid = setup(tmp_path)
    await grant(workflow, rid, ceiling - 1, kind)
    await grant(workflow, rid, ceiling, kind)
    assert len(workflow.agent.calls) == 2
    assert not workflow.store.pending(rid)
    key = "budget_grants/T1" if kind == "implementation_grant" else "recovery/diagnosis_grants/T1"
    assert workflow.store.get(rid, key) == ceiling + 1


@pytest.mark.asyncio
async def test_disabled_automation_uses_existing_human_path(tmp_path):
    workflow, rid = setup(tmp_path)
    configure(workflow, automation={"enabled": False})
    async def approve(question):
        return "y"
    workflow.question_handler = approve
    await grant(workflow, rid)
    assert not workflow.agent.calls
    records = [v for k, v in workflow.store.delivery_evidence(rid).items() if k.startswith("decisions/applied/")]
    assert records[0]["actor"] == "human"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["y", "n"])
async def test_old_human_response_survives_prompt_wording_upgrade(tmp_path, answer):
    workflow, rid = setup(tmp_path)
    interaction = choice_spec(["yes", "pause"], ["continue", "pause"], yes_no=True)
    interaction["pause_on"] = "n"
    q = workflow.store.request(rid, "grant/implementation_grant/4", "legacy wording", "legacy-hash", interaction)
    workflow.store.answer(q["id"], answer)
    if answer == "y":
        await grant(workflow, rid)
        assert workflow.store.get(rid, "budget_grants/T1") == 5
    else:
        with pytest.raises(asyncio.CancelledError):
            await grant(workflow, rid)
        assert workflow.store.get(rid, "budget_grants/T1") is None
    assert not workflow.agent.calls


@pytest.mark.asyncio
async def test_concurrent_human_pause_wins_over_automatic_grant(tmp_path):
    workflow, rid = setup(tmp_path)
    interaction = choice_spec(["yes", "pause"], ["continue", "pause"], yes_no=True)
    interaction["pause_on"] = "n"
    key = "grant/implementation_grant/4"
    q = workflow.store.request(rid, key, "One scoped retry", digest([key, "One scoped retry"]), interaction)
    original = workflow.agent.invoke
    async def competing(*args, **kwargs):
        workflow.store.answer(q["id"], "n")
        return await original(*args, **kwargs)
    workflow.agent.invoke = competing
    with pytest.raises(asyncio.CancelledError):
        await grant(workflow, rid)
    assert workflow.store.get(rid, "budget_grants/T1") is None


@pytest.mark.asyncio
async def test_stale_evidence_cannot_apply_decision(tmp_path):
    workflow, rid = setup(tmp_path)
    original = workflow.agent.invoke
    async def changed(*args, **kwargs):
        workflow.store.checkpoint(rid, "plan", {"changed": True})
        return await original(*args, **kwargs)
    workflow.agent.invoke = changed
    async def approve(question):
        return "y"
    workflow.question_handler = approve
    with pytest.raises(AA2Error, match="state changed"):
        await grant(workflow, rid)
    assert workflow.store.get(rid, "budget_grants/T1") is None


def test_grant_receipt_and_budget_roll_back_together_on_sql_failure(tmp_path, monkeypatch):
    workflow, rid = setup(tmp_path)
    original = workflow.store._put
    def broken(db, run, key, value):
        if key == "receipt":
            raise OSError("injected before commit")
        original(db, run, key, value)
    monkeypatch.setattr(workflow.store, "_put", broken)
    with pytest.raises(OSError):
        workflow.store.apply_decision(rid, "receipt", {"kind": "implementation_grant", "owner": "T1"},
            {"action": "approve", "reason": "bounded"}, "agent", {}, {"budget_grants/T1": 5}, [], [])
    assert workflow.store.get(rid, "budget_grants/T1") is None
    assert workflow.store.get(rid, "receipt") is None


def test_competing_grants_cas_and_paused_run_rejection(tmp_path):
    workflow, rid = setup(tmp_path)
    request = {"kind": "implementation_grant", "owner": "T1"}
    result = {"action": "approve", "reason": "bounded"}
    for name, expected in (("first", True), ("second", False)):
        assert workflow.store.apply_decision(rid, name, request, result, "agent",
            {"budget_grants/T1": None}, {"budget_grants/T1": 5}, [], []) is expected
    workflow.store.update(rid, status="PAUSED")
    assert not workflow.store.apply_decision(rid, "third", request, result, "agent", {}, {}, [], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("error,calls", [(InvalidStructuredResponse("bad"), 2), (ProviderError("auth"), 1)])
async def test_decision_failure_is_bounded_and_never_approves_its_retry(tmp_path, error, calls):
    workflow, rid = setup(tmp_path)
    count = 0
    async def invalid(*args, **kwargs):
        nonlocal count
        count += 1
        raise error
    workflow.agent.invoke = invalid
    async def pause(question):
        assert question["key"].startswith("grant/")
        return "n"
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await grant(workflow, rid)
    assert count == calls
    with pytest.raises(asyncio.CancelledError):
        await grant(workflow, rid)
    assert count == calls


@pytest.mark.asyncio
async def test_decision_router_does_not_multiply_calls_by_retries_and_providers(tmp_path):
    config, store = configured(tmp_path)
    data = config.load().model_dump(mode="json")
    data["execution"]["infra_retries"] = 3
    write_json(config.path, data)
    rid = store.admit("bounded", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    calls = []
    async def fail(effective, *args):
        calls.append(effective["provider"])
        raise ProviderError("connection", retryable=True)
    router._call = fail
    with pytest.raises(ProviderError):
        await router.invoke("decision", {}, DecisionResult, tmp_path, rid, "decision/one")
    assert calls == ["codex"]


@pytest.mark.asyncio
async def test_committed_decision_result_is_reconciled_after_interrupt(tmp_path):
    workflow, rid = setup(tmp_path)
    original = workflow._invoke
    called = False
    async def interrupted(*args, **kwargs):
        nonlocal called
        result = await original(*args, **kwargs)
        if not called:
            called = True
            raise PostCommitError("after response checkpoint")
        return result
    workflow._invoke = interrupted
    with pytest.raises(PostCommitError):
        await grant(workflow, rid)
    await grant(workflow, rid)
    assert len(workflow.agent.calls) == 1
    assert workflow.store.get(rid, "budget_grants/T1") == 5


@pytest.mark.asyncio
async def test_old_completed_diagnosis_is_reused_without_dispatch_after_upgrade(tmp_path):
    workflow, rid = setup(tmp_path)
    context = {"review_kind": "recovery_diagnosis", "failure": failure().model_dump()}
    original = await workflow._invoke("review", context, Diagnosis, tmp_path, rid, "recovery/diagnose/old/1", fresh=True)
    resumed = await workflow._invoke("recovery_diagnosis", context, Diagnosis, tmp_path, rid, "recovery/diagnose/old/1", fresh=True)
    assert resumed == original and len(workflow.agent.calls) == 1


@pytest.mark.asyncio
async def test_explicit_pause_does_not_auto_resume(tmp_path):
    workflow, rid = setup(tmp_path)
    workflow.store.update(rid, status="PAUSED")
    assert (await workflow.start(run_id=rid, allow_resume=False))["status"] == "PAUSED"
    assert not workflow.agent.calls


def test_progress_requires_historical_gain_and_deduplicates_completed_observation(tmp_path):
    workflow, rid = setup(tmp_path)
    progress = workflow.convergence
    def observe(token, **criteria):
        return progress.observe(rid, "T1", token, {"criteria": criteria, "comparable": True})
    assert observe(1, a=False, b=False, c=False)["stalled"] == 0
    assert observe(2, a=True, b=False, c=False)["last"]["improved"]
    assert observe(3, a=False, b=True, c=False)["stalled"] == 1
    assert observe(4, a=True, b=True, c=False, invented=True)["stalled"] == 2
    assert progress.blocked(rid, "T1")
    assert observe(4, a=True, b=True, c=True)["stalled"] == 2  # Same receipt, no recount.
    assert observe(5, a=True, b=True, c=True)["stalled"] == 0


@pytest.mark.asyncio
async def test_no_progress_is_decision_evidence_without_reset(tmp_path):
    workflow, rid = setup(tmp_path)
    for token in (1, 2, 3):
        workflow.convergence.observe(rid, "T1", token, {"criteria": {"a": False}})
    before = workflow.convergence.state(rid, "T1")
    for action in ("implementation/4", "diagnosis/1", "implementation/5"):
        await workflow.decisions.ensure_progress(rid, "T1", action)
    await grant(workflow, rid)
    assert workflow.convergence.state(rid, "T1") == before
    assert len(workflow.agent.calls) == 1
    assert not workflow.store.pending(rid)


def test_missing_evidence_is_allowed_once_and_old_stall_is_retained(tmp_path):
    workflow, rid = setup(tmp_path)
    workflow.convergence.observe(rid, "T1", 1)
    assert not workflow.convergence.blocked(rid, "T1")
    workflow.convergence.observe(rid, "T1", 2)
    assert workflow.convergence.blocked(rid, "T1")
    workflow.store.checkpoint(rid, "rounds/old-origin", {"round": 7, "stalled": 2})
    assert workflow.convergence.blocked(rid, "old-origin")
    workflow.convergence.observe(rid, "old-origin", 8, {"criteria": {"a": False}})
    assert workflow.convergence.blocked(rid, "old-origin")
    assert workflow.store.get(rid, "rounds/old-origin")["round"] == 7


def test_adding_trivial_tests_to_reach_minimum_count_is_not_progress(tmp_path):
    from auto_agents_2.evidence import CHECKER_VERSION
    from auto_agents_2.models import CheckResult
    from auto_agents_2.replanning import check_criteria
    workflow, rid = setup(tmp_path)
    command = Command(argv=["pytest", "tests"], expected_tests=2)
    result = CheckResult(argv=command.argv, cwd=".", duration=0, output="", exit_code=1, process_exit_code=0,
        source_hash="source", environment_hash="env", command_hash=digest(command.model_dump()),
        checker_version=CHECKER_VERSION, tests=1, test_ids=["original"], passed_test_ids=["original"],
        report_status="valid", report={"hash": "report"}, failure_reasons=["insufficient_test_cases"])
    for token, row in enumerate([result, result.model_copy(update={"exit_code": 0, "tests": 2, "failure_reasons": [],
            "test_ids": ["original", "trivial"], "passed_test_ids": ["original", "trivial"]})]):
        state = workflow.convergence.observe(rid, "T1", token, {
            "criteria": check_criteria([command], [row], "source", "env"),
            "progress_criteria": check_criteria([command], [row], "source", "env", process_exit=True)})
    assert not state["last"]["improved"] and state["stalled"] == 1


@pytest.mark.asyncio
async def test_technical_clarification_is_batched_extracts_facts_and_keeps_business_human(tmp_path):
    workflow, rid = setup(tmp_path)
    questions = [UserQuestion(prompt="配置中的 Python 版本是什么？"), UserQuestion(prompt="收费标准定多少？"),
                 UserQuestion(prompt="原型是否通过？", purpose="prototype_approval")]
    async def answer(stage, context, *args, **kwargs):
        assert stage == "decision"
        assert set(context["decision_request"]["scope"]["questions"]) == {"0", "1"}
        return DecisionResult(action="answer", reason="Existing environment setting", evidence_refs=["facts"],
            answers=[FactAnswer(index=0, fact_ref="environment.python_version")])
    workflow.agent.invoke = answer
    automatic = await workflow.decisions.clarify(rid, "clarify/1", questions, "fingerprint", tmp_path)
    assert automatic == {0: workflow.config.load().environment.python_version}
    assert not workflow.store.pending(rid)
    assert not workflow.store.delivery_evidence(rid)["approvals"]
    q = workflow.store.request(rid, "clarify/1/1", "收费标准定多少？", "fingerprint")
    workflow.store.answer(q["id"], "暂不收费")
    async def no_replay(*args, **kwargs):
        pytest.fail("A resumed batch must reuse its factual answers")
    workflow.agent.invoke = no_replay
    assert await workflow.decisions.clarify(rid, "clarify/1", questions, "fingerprint", tmp_path) == automatic


@pytest.mark.parametrize("result", [
    DecisionResult(action="approve", reason="not a gate", evidence_refs=["facts"]),
    DecisionResult(action="answer", reason="invented", evidence_refs=["facts"], answers=[FactAnswer(index=0, fact_ref="invented")]),
    DecisionResult(action="answer", reason="invented", evidence_refs=["invented"], answers=[FactAnswer(index=0, fact_ref="python")]),
])
def test_invalid_decision_actions_and_citations_are_rejected(result):
    from auto_agents_2.decisions import DecisionService
    request = DecisionRequest(kind="technical_clarification", owner="clarify", allowed_choices=["answer", "ask_user"],
        scope={"questions": {"0": {"options": []}}}, evidence={"facts": {"python": "3.11"}}, state_identity="state")
    with pytest.raises(ValueError):
        DecisionService.validate(request, result)


def test_blocker_summary_separates_green_tests_review_absent_invalid_and_diagnosis():
    rows = [{"argv": ["build"], "exit_code": 0, "process_exit_code": 0},
            {"argv": ["pytest"], "exit_code": 0, "test_ids": list(map(str, range(18))), "passed_test_ids": list(map(str, range(18)))}]
    review = {"approved": False, "summary": "Evidence incomplete", "findings": [
        {"location": "screenshots", "description": "Required 375×812 and 1280×900 screenshots missing"},
        {"location": "README.md", "description": "Verification record is stale"}]}
    message = failure_summary(rows, review=review)
    assert "检查通过" in message and "18/18" in message and "README.md" in message and "375×812" in message
    assert "尚无有效失败报告" not in message
    assert "没有已执行" in failure_summary([])
    assert "尚未执行" in failure_summary([{"argv": ["browser"], "process_exit_code": None,
                                         "failure_reasons": ["prerequisite_failed", "not_executed"]}])
    assert "证据无效" in failure_summary([{"argv": ["pytest"], "failure_reasons": ["test_report_invalid"]}])
    assert "Duplicate repair-target" in failure_summary(rows, diagnosis_feedback={"validation_error": "Duplicate repair-target task IDs"})
    assert "环境准备失败" in failure_summary([], environment_error="solver rejected")


@pytest.mark.asyncio
async def test_diagnosis_routing_correction_and_budget_use_existing_provider(tmp_path):
    class Agent(ApprovingAgent):
        calls_to_diagnose = 0
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if schema is Diagnosis:
                self.calls_to_diagnose += 1
                assert stage == "recovery_diagnosis"
                if self.calls_to_diagnose == 1:
                    return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=["invented"], reason="bad")
                assert "validation_error" in context["diagnosis_feedback"]
            return await super().invoke(stage, context, schema, *args, **kwargs)
    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoints(rid, {"plan": plan().model_dump(), "recovery/budgets/diagnosis/T3": {"round": 2}})
    record = failure()
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 4
    assert workflow.store.get(rid, "recovery/active") is None
    completed = workflow.store.get(rid, f"recovery/completed/{record.id}/1")
    assert "diagnosis_feedback" not in completed
    assert len([s for s, _ in agent.calls if s == "decision"]) == 0


@pytest.mark.asyncio
async def test_invalid_diagnosis_schema_gets_one_correction_then_a_continuation_decision(tmp_path):
    workflow, rid = setup(tmp_path)
    configure(workflow, execution={"max_diagnosis_rounds": 9})
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.workspaces.allocate(rid, "integration", base)
    workflow.store.checkpoint(rid, "plan", plan().model_dump())
    calls = []
    async def invalid(stage, context, *args, **kwargs):
        calls.append(stage)
        if stage == "decision":
            return DecisionResult(action="ask_user", reason="Repeated invalid diagnosis needs user input")
        assert stage == "recovery_diagnosis"
        if len(calls) == 2:
            assert "Diagnosis schema" in context["diagnosis_feedback"]["validation_error"]
        raise InvalidStructuredResponse("fixture")
    workflow.agent.invoke = invalid
    workflow.recovery.submit(rid, failure())
    await workflow.recovery.resume(rid)
    await workflow.recovery.resume(rid)
    async def pause(question):
        assert "Repeated invalid diagnosis" in question["prompt"]
        return "n"
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    assert calls == ["recovery_diagnosis", "recovery_diagnosis", "decision"]


@pytest.mark.asyncio
async def test_real_task_keeps_progressing_past_four_rounds_and_stops_when_verified(tmp_path):
    class ImprovingAgent(ApprovingAgent):
        implementations = 0
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "implement":
                self.implementations += 1
                return ImplementationResult(summary="Next fixed obligation", changes=[Change(path="value.py",
                    content=f"value = {self.implementations}\n")])
            return await super().invoke(stage, context, schema, *args, **kwargs)
    (tmp_path / "test_value.py").write_text("import pytest\nfrom value import value\n@pytest.mark.parametrize('n', range(1, 7))\ndef test_value(n): assert value >= n\n")
    agent = ImprovingAgent()
    workflow, rid = setup(tmp_path, agent)
    git(tmp_path, "add", "test_value.py")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture obligations")
    base = workflow.workspaces.committed_baseline(rid, "main")
    command = Command(argv=[sys.executable, "-m", "pytest", "test_value.py"], expected_tests=6)
    task = TaskSpec(id="T1", origin_id="T1", title="value", description="value reaches six", requirement_ids=["R1"],
        allowed_paths=["value.py"], acceptance=["all six assertions"], checks=[command])
    contract = PlanResult(summary="value", document="value", tasks=[task], final_checks=[command])
    workflow.store.checkpoint(rid, "plan", contract.model_dump())
    workflow.workspaces.allocate(rid, "integration", base)
    for _ in range(8):
        try:
            commit = await workflow._task(rid, task, base, contract, {"specification": {}})
            break
        except RecoveryNeeded as exc:
            workflow.recovery.submit(rid, exc.failure)
            await workflow.recovery.resume(rid)
    else:
        pytest.fail("task never completed")
    assert commit and agent.implementations == 6
    assert workflow.store.get(rid, "rounds/T1")["round"] == 6
    assert workflow.store.recovery_history(rid, "T1")["total_actions"] >= 5
    assert not workflow.store.pending(rid)


@pytest.mark.asyncio
async def test_review_wording_changes_are_not_progress_and_decision_can_request_pause(tmp_path):
    class RejectedAgent(ApprovingAgent):
        iterations = 0
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "implement":
                self.iterations += 1
                return ImplementationResult(summary="candidate", changes=[Change(path="value.py", content=f"value = {self.iterations}\n")])
            if stage == "decision" and self.iterations == 3:
                return DecisionResult(action="ask_user", reason="Repeated review rejection without progress")
            if issubclass(schema, ReviewResult):
                from authority_review_fixtures import authority_review
                return authority_review(context, approved=False, summary=f"still missing {self.iterations}", findings=[Finding(
                    severity="major", location=f"line {self.iterations}", description="same unmet requirement", evidence="source", resolution="fix")])
            return await super().invoke(stage, context, schema, *args, **kwargs)
    agent = RejectedAgent()
    workflow, rid = setup(tmp_path, agent)
    base = workflow.workspaces.committed_baseline(rid, "main")
    command = Command(argv=[sys.executable, "-c", "assert True"])
    task = TaskSpec(id="T1", title="value", description="value", requirement_ids=["R1"], allowed_paths=["value.py"],
        acceptance=["correct behavior"], checks=[command])
    contract = PlanResult(summary="value", document="value", tasks=[task], final_checks=[command])
    async def pause(question):
        assert "Repeated review rejection" in question["prompt"]
        return "n"
    workflow.question_handler = pause
    workflow.store.checkpoint(rid, "plan", contract.model_dump())
    workflow.workspaces.allocate(rid, "integration", base)
    with pytest.raises(asyncio.CancelledError):
        for _ in range(3):
            with pytest.raises(RecoveryNeeded) as caught:
                await workflow._task(rid, task, base, contract, {"specification": {}})
            workflow.recovery.submit(rid, caught.value.failure)
            await workflow.recovery.resume(rid)
    assert agent.iterations == 3 and agent.calls
    assert workflow.store.get(rid, "rounds/T1")["stalled"] == 2
