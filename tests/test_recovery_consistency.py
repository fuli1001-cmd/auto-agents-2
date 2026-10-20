"""Cross-stage recovery contracts exercised through native checks and run/resume."""
import json

import pytest
from test_recovery_policy import authorize, ready
from test_verification_input_identity import task_fixture
from test_workflow import CodingAgent, engine

from auto_agents_2.decisions import DecisionResult, DecisionService, DecisionValidationError
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import InternalWorkflowError, StateConflict
from auto_agents_2.recovery import Diagnosis, adjudicate
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


async def test_unknown_internal_error_exits_without_question_and_resumes_same_run(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    async def crash(rid):
        raise RuntimeError("<UNSAFE_EXCEPTION_DETAILS>")
    monkeypatch.setattr(workflow, "_execute", crash)
    with pytest.raises(InternalWorkflowError) as caught:
        await workflow.start(str(tmp_path / "requirements.md"))
    current = workflow.store.current()
    assert current["status"] == "PAUSED" and current["data"]["pause_reason"] == "internal_error"
    assert current["data"]["incident_id"] == caught.value.incident
    assert not workflow.store.pending(current["id"])
    fault = workflow.store.get(current["id"], "recovery/current-fault")
    assert fault["exception_type"] == "RuntimeError" and fault["frames"]
    assert "<UNSAFE_EXCEPTION_DETAILS>" not in json.dumps(workflow.store.events())
    async def finish(rid):
        assert rid == current["id"]
        workflow.store.update(rid, status="SUCCEEDED")
        return workflow.store.run(rid)
    monkeypatch.setattr(workflow, "_execute", finish)
    assert (await workflow.start())["status"] == "SUCCEEDED"


def test_internal_error_cli_has_nonzero_exit_and_no_yes_no(monkeypatch):
    from typer.testing import CliRunner

    from auto_agents_2 import cli
    async def fail(*args, **kwargs):
        raise InternalWorkflowError("incident-fixture", "RuntimeError")
    monkeypatch.setattr(cli, "execute", fail)
    result = CliRunner().invoke(cli.app, ["run", "--project", "/tmp"])
    assert result.exit_code == 2 and "INTERNAL_ERROR" in result.output
    assert "incident-fixture" in result.output and "[y/n]" not in result.output


async def test_internal_fault_write_failure_does_not_claim_checkpoint_saved(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    def unavailable(*args, **kwargs):
        raise OSError("<UNSAFE_STORAGE_ERROR>")
    monkeypatch.setattr(workflow.store, "record_fault", unavailable)
    error = workflow._internal_error(rid, RuntimeError("<UNSAFE_EXCEPTION_DETAILS>"))
    assert "写入失败" in str(error) and "UNSAFE" not in str(error)


@pytest.mark.parametrize("phase", ["final", "reuse", "reintegration", "runtime", "completion"])
@pytest.mark.parametrize("change", ["cache", "dependency", "source"])
async def test_native_evidence_classification_is_consistent_across_gates(tmp_path, phase, change):
    workflow, rid, work, _, task, contract = await task_fixture(tmp_path)
    environment = await workflow._environment(work, rid, contract)
    commands = workflow._task_commands(rid, task, work, contract)
    results = await workflow._checks(rid, commands, work, environment, task)
    original = tree_hash(work)
    target = work / {"cache": "frontend/node_modules/.cache/generated.txt",
                     "dependency": "frontend/node_modules/example/index.js",
                     "source": "frontend/source.txt"}[change]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed fixture input")
    if change == "source":
        with pytest.raises(StateConflict):
            workflow._verification_problem(rid, commands, results, work, environment,
                                           phase=phase, expected_source=original)
    else:
        failure = workflow._verification_problem(rid, commands, results, work, environment,
                                                 phase=phase, expected_source=original)
        if change == "cache":
            assert failure is None
        else:
            assert failure.kind == "evidence" and failure.reason_code == "inputs_changed"
            assert failure.phase == phase and adjudicate(failure).action == "recheck"
            assert all(row["category"] == "evidence" for row in failure.evidence["verification_assessment"]["issues"])
    assert workflow.store.get(rid, "rounds/T1")["round"] == 6
    assert target.read_text() == "changed fixture input"


@pytest.mark.parametrize("operation", ["final-review", "no-change-review"])
async def test_real_run_automatically_rechecks_review_drift_without_reimplementation(tmp_path, operation):
    class DriftingReviewer(CodingAgent):
        drifted = False
        async def invoke(self, stage, context, schema, workspace, run_id, key, fresh=False):
            result = await super().invoke(stage, context, schema, workspace, run_id, key, fresh)
            if key == operation and not self.drifted:
                dependency = workspace / "node_modules/fixture/index.js"
                dependency.parent.mkdir(parents=True, exist_ok=True)
                dependency.write_text("export const fixture = 1;")
                self.drifted = True
            return result
    project = tmp_path / "project"
    project.mkdir()
    requirement = tmp_path / "requirements.md"
    requirement.write_text("Add two numbers")
    agent = DriftingReviewer()
    first = await engine(project, agent).start(str(requirement))
    assert first["status"] == "SUCCEEDED"
    workflow = engine(project, agent)
    current = await workflow.start(str(requirement)) if operation == "no-change-review" else first
    assert current["status"] == "SUCCEEDED" and agent.drifted
    assert agent.calls.count(("implement", False)) == 1
    events = workflow.store.events(current["id"])
    assert any(event["kind"] == "evidence.invalidated" for event in events)
    assert not any(event["kind"] == "question.waiting" for event in events)


async def test_completed_verification_survives_failure_before_consumer_checkpoint(tmp_path, monkeypatch):
    workflow, rid, work, _, task, contract = await task_fixture(tmp_path)
    environment = await workflow._environment(work, rid, contract)
    commands = workflow._task_commands(rid, task, work, contract)
    checkpoints = workflow.store.checkpoints
    native_check = workflow.verifier._check
    images = []
    async def with_attachment(*args, **kwargs):
        result = await native_check(*args, **kwargs)
        image = workflow.artifacts.put("synthetic durable handoff screenshot", "test_diagnostic")
        images.append(image)
        return result.model_copy(update={"attachments": [{"path": "view.png", "artifact": image}]})
    monkeypatch.setattr(workflow.verifier, "_check", with_attachment)
    def after_result(run_id, values, **kwargs):
        checkpoints(run_id, values, **kwargs)
        if any(key.startswith("verification/batches/") and value.get("phase") == "completed"
               for key, value in values.items()):
            raise RuntimeError("Injected after durable verification")
    monkeypatch.setattr(workflow.store, "checkpoints", after_result)
    with pytest.raises(RuntimeError, match="Injected after durable verification"):
        await workflow._checks(rid, commands, work, environment, task)
    monkeypatch.setattr(workflow.store, "checkpoints", checkpoints)
    workflow.resources.collect_artifacts()
    assert images and all(workflow.artifacts.bytes(image) for image in images)
    assert workflow.store.get(rid, "develop/review-evidence/" + task.id)["phase"] == "pending"
    async def forbidden(*args, **kwargs):
        pytest.fail("A completed native check cannot execute again")
    monkeypatch.setattr(workflow.verifier, "_check", forbidden)
    rows = await workflow._checks(rid, commands, work, environment, task)
    assert rows and all(row.exit_code == 0 for row in rows)
    assert [attachment["artifact"] for row in rows for attachment in row.attachments] == images


async def test_started_verification_without_result_preserves_occupied_reservation(tmp_path, monkeypatch):
    workflow, rid, work, _, task, contract = await task_fixture(tmp_path)
    environment = await workflow._environment(work, rid, contract)
    commands = workflow._task_commands(rid, task, work, contract)
    calls = []
    async def crash(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("unknown completion")
    monkeypatch.setattr(workflow.verifier, "_check", crash)
    with pytest.raises(RuntimeError):
        await workflow._checks(rid, commands, work, environment, task)
    with pytest.raises(StateConflict, match="occupied"):
        await workflow._checks(rid, commands, work, environment, task)
    assert calls == [1]


async def test_run_resumes_completed_runtime_probe_without_spending_another_attempt(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    requirement = tmp_path / "requirements.md"
    requirement.write_text("Add two numbers")
    agent = CodingAgent()
    workflow = engine(project, agent)
    checkpoint = workflow.store.checkpoint
    injected = False
    def interrupt(run_id, key, value):
        nonlocal injected
        checkpoint(run_id, key, value)
        if (key.startswith("verification/batches/") and value.get("phase") == "completed" and not injected
                and (workflow.store.get(run_id, "delivery/journal") or {}).get("phase") == "runtime"
                and any("from calc import add" in " ".join(row["argv"]) for row in value.get("commands", []))):
            injected = True
            raise RuntimeError("Injected after native runtime result, before completion journal")
    monkeypatch.setattr(workflow.store, "checkpoint", interrupt)
    with pytest.raises(InternalWorkflowError):
        await workflow.start(str(requirement))
    rid = workflow.store.current()["id"]
    journal = workflow.store.get(rid, "delivery/journal")
    assert journal["phase"] == "runtime" and journal["runtime_budget_used"] == 1
    restarted = engine(project, agent)
    async def forbidden(*args, **kwargs):
        pytest.fail("The completed runtime probe must be replayed from its durable observation")
    monkeypatch.setattr(restarted.verifier, "_check", forbidden)
    result = await restarted.start()
    assert result["status"] == "SUCCEEDED"
    assert restarted.store.get(rid, "delivery/receipt")["runtime_budget_used"] == 1


async def test_completed_response_survives_transport_identity_change(tmp_path, monkeypatch):
    workflow, rid, work, _, record = ready(tmp_path)
    context = {"review_kind": "recovery_diagnosis", "failure": record.model_dump()}
    result = await workflow._invoke("recovery_diagnosis", context, Diagnosis, work, rid, "logical-operation", fresh=True)
    calls = len(workflow.agent.calls)
    monkeypatch.setattr("auto_agents_2.engine.invocation_identity", lambda *args, **kwargs: "new-transport-version")
    again = await workflow._invoke("recovery_diagnosis", context, Diagnosis, work, rid, "logical-operation", fresh=True)
    assert again == result and len(workflow.agent.calls) == calls
    changed = {**context, "failure": {**record.model_dump(), "evidence": {"new_observation": True}}}
    with pytest.raises(StateConflict):
        await workflow._invoke("recovery_diagnosis", changed, Diagnosis, work, rid, "logical-operation", fresh=True)


async def test_completed_action_receipt_cannot_be_overwritten(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    _, key = await authorize(workflow, rid, work, contract, record)
    outcome = {"status": "validated", "diagnosis": {"action": "recheck"}}
    workflow.recovery_policy.complete(rid, key, outcome)
    before = workflow.store.get(rid, key)
    with pytest.raises(StateConflict):
        workflow.store.checkpoints(rid, {"unrelated": "must roll back", key: {**before, "outcome": {"status": "other"}}})
    assert workflow.store.get(rid, key) == before and workflow.store.get(rid, "unrelated") is None
    workflow.recovery_policy.complete(rid, key, outcome)
    assert workflow.store.get(rid, key)["outcome_hash"] == digest(outcome)


async def test_presentation_changes_do_not_invalidate_an_issued_action(tmp_path):
    workflow, rid, work, contract, record = ready(tmp_path)
    original, key = await authorize(workflow, rid, work, contract, record)
    active = workflow.store.get(rid, "recovery/active")
    rewritten = original.model_copy(update={"reason": "Updated display wording", "evidence_refs": ["failure"]})
    action, same = await workflow.recovery_policy.authorize(rid, record, active, work, contract, rewritten, operation="fixture")
    assert same == key and action == original


def test_producer_provenance_and_historical_model_output_cannot_mint_facts():
    context = {"custom": {"hash": "a" * 64}, "failure": {"observation_id": "obs", "evidence": {"checks": []}},
               "history": {"recent": [{"outcome": {"diagnosis": {"hash": "b" * 64, "action": "repair"}}}]}}
    catalog = controller_context_catalog(context, provenance={"custom": "proposal"})
    assert "a" * 64 not in catalog.controller and "b" * 64 not in catalog.controller
    assert catalog.entries["custom"].status == "unverified"
    assert catalog.resolve("decision_request.evidence.failure.evidence.checks") == "failure.evidence.checks"
    assert catalog.entries["failure"].observation_id == "obs"
    rejected = {"diagnosis_feedback": {"diagnosis": {"hash": "c" * 64}}, "history": {"recent": [
        {"outcome": {"status": "invalid", "feedback": {"diagnosis": {"hash": "d" * 64}}}}]}}
    rejected_refs = controller_context_catalog(rejected)
    assert {"c" * 64, "d" * 64}.isdisjoint(rejected_refs.controller)
    assert "history.recent[0].outcome.feedback.diagnosis" in rejected_refs.proposals
    from test_decision_evidence import request
    decision = DecisionResult(action="approve", selected_action="recheck", reason="Inspect current state",
                              expected_result="Collect checked results", evidence_refs=["history.recent[0].outcome.diagnosis"])
    with pytest.raises(DecisionValidationError, match="controller record"):
        DecisionService.validate(request(), decision)


@pytest.mark.parametrize("envelope", ["direct", "runtime_failure", "feedback"])
def test_current_checks_are_classified_and_described_identically_in_every_envelope(envelope):
    from test_recovery_contracts import failure

    from auto_agents_2.faults import route_failure
    from auto_agents_2.recovery import check_rows, failure_summary
    checks = [{"argv": ["pytest"], "exit_code": 1, "process_exit_code": 1,
               "failure_reasons": ["test_report_invalid"]}]
    evidence = {"checks": checks} if envelope == "direct" else {envelope: {"checks": checks}}
    record = failure(kind="review", evidence=evidence)
    assert adjudicate(record).action == "recheck"
    assert "证据无效" in failure_summary(check_rows(record.evidence))
    assert route_failure(record).route == "rule"
    checks[0]["failure_reasons"] = ["evidence_collection_failed"]
    assert route_failure(failure(kind="review", evidence=evidence)).human_requirement.kind == "resource_limit"


def test_legacy_runtime_error_is_not_promoted_to_a_passing_check():
    from auto_agents_2.recovery import check_kind, check_rows, failure_summary
    rows = check_rows({"runtime_failure": {"checks": [{"failure": "legacy runtime error"}]}})
    assert check_kind(rows, default="review") == "review"
    assert "检查通过" not in failure_summary(rows)
    assert "证据无效" in failure_summary(rows)


@pytest.mark.parametrize("published", [False, True])
async def test_browser_environment_failure_uses_decisions_or_published_prerequisite(tmp_path, monkeypatch, published):
    from auto_agents_2.errors import RetryableInfrastructureError
    from auto_agents_2.faults import HumanIntervention
    workflow, rid, work, _, _ = ready(tmp_path)
    workflow.store.checkpoint(rid, "recovery/active", None)
    if published:
        workflow.store.checkpoint(rid, "delivery/journal", {"publication_started": True})
    calls = []
    async def install(workspace, cache):
        calls.append(1)
        if len(calls) == 1:
            raise RetryableInfrastructureError("Browser installer timed out")
        binary = cache / "chromium-fixture/chrome-linux/chrome"
        binary.parent.mkdir(parents=True)
        binary.write_text("<BROWSER_FIXTURE>")
    monkeypatch.setattr(workflow, "_install_browser", install)
    if published:
        with pytest.raises(HumanIntervention):
            await workflow._ensure_browser(rid, work)
        with pytest.raises(HumanIntervention):
            await workflow._ensure_browser(rid, work)
        assert calls == [1] and not workflow.agent.calls
    else:
        await workflow._ensure_browser(rid, work)
        assert calls == [1, 1] and [stage for stage, _ in workflow.agent.calls] == ["decision"]
        assert workflow.store.recovery_history(rid, "browser_environment")["recent"][0]["outcome"]["status"] == "verified"
