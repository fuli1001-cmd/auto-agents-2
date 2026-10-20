"""Large recovery inputs must preserve evidence without multiplying citation indexes."""
import copy
import json
from types import SimpleNamespace

import pytest
from test_context_plan import text_of
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import CONTEXT_VERSION, ContextPlanner, remap_references
from auto_agents_2.decisions import DecisionRequest, DecisionResult, DecisionService
from auto_agents_2.errors import InvalidStructuredResponse, ProviderError
from auto_agents_2.models import strict_schema
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import Diagnosis, FailureRecord, adjudicate, evidence_aliases, evidence_references
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest


@pytest.fixture
def planner(tmp_path):
    store = Store(tmp_path)
    store.admit("fixture", {})
    return ContextPlanner(ArtifactStore(store.root))


def legacy_request():
    cases = [f"case-{i}" for i in range(427)]
    check = {"argv": ["pytest"], "exit_code": 0, "process_exit_code": 0,
             "source_hash": "source", "environment_hash": "environment", "report_status": "valid",
             "test_ids": cases, "passed_test_ids": cases, "failure_reasons": [], "tests": 427}
    refs = [f"failure.evidence.checks[0].test_ids[{i}]" for i in range(2974)]
    task = {"id": "T010", "allowed_paths": ["frontend/e2e/pagination.spec.ts", "README.md"],
            "acceptance": ["Production preview must have independent evidence", "README must reflect current evidence"]}
    failure = {"evidence_refs": refs, "task_id": "T010", "kind": "review",
               "evidence": {"checks": [check], "task": task,
                            "review": {"approved": False, "findings": [{"description": "Preview and README incomplete"}]}}}
    candidates = {action: {"action": action, "task_ids": ["T010"], "evidence_refs": refs,
                          "reason": "Bounded action"}
                  for action in ("diagnose", "repair", "recheck", "plan_patch", "prototype_recheck")}
    recent = [{"action": candidates["diagnose"], "phase": "completed", "reason": "Prior authorization",
               "outcome": {"status": "validated", "diagnosis": {"reason": f"Prior finding {i}",
                           "evidence_refs": ["failure.evidence.review"]}}} for i in range(15)]
    return DecisionRequest(kind="recovery_action", owner="T010", allowed_choices=["approve", "ask_user"],
        scope={"tasks": [task], "candidates": candidates, "one_action": True}, state_identity="unchanged",
        evidence={"failure": failure, "history": {"recent": recent, "total_actions": 15},
                  "consumption": {"implementation": {"round": 7}, "diagnosis": {"round": 15}}})


def test_million_character_legacy_request_keeps_current_facts_and_scope_inline(planner, tmp_path):
    request = legacy_request()
    context = {"decision_request": request.model_dump()}
    original = copy.deepcopy(context)
    assert len(PromptCompiler().compile("decision", context)[0]) > 1_000_000
    prepared = planner.prepare("decision", context, tmp_path)
    prompt = PromptCompiler().compile("decision", prepared.payload)[0]
    assert len(prompt) + len(json.dumps(strict_schema(DecisionResult))) < 64000
    assert context == original
    view = prepared.payload["decision_request"]
    assert view["scope"]["tasks"] == request.scope["tasks"]
    assert set(view["scope"]["candidates"]) == set(request.scope["candidates"])
    assert view["evidence"]["consumption"] == request.evidence["consumption"]
    assert view["evidence"]["failure"]["evidence"]["review"] == request.evidence["failure"]["evidence"]["review"]
    check = view["evidence"]["failure"]["evidence"]["checks"][0]
    assert check["tests"] == check["total_tests"] == 427 and check["unpassed_test_ids"] == []
    assert json.loads(text_of(planner, check["observation"])) == request.evidence["failure"]["evidence"]["checks"][0]
    index = view["evidence"]["failure"]["evidence_refs"]
    assert not index["required_read"]
    assert json.loads(text_of(planner, index)) == request.evidence["failure"]["evidence_refs"]
    for action in view["scope"]["candidates"].values():
        assert action["evidence_refs"]["artifact"] == index["artifact"]
    history = view["evidence"]["history"]
    assert len(history["recent"]) == 15 and history["total_actions"] == 15
    assert all(row["status"] == "completed" for row in history["recent"])
    assert json.loads(text_of(planner, history["details"])) == request.evidence["history"]["recent"]
    assert not [row for row in prepared.selections if row["presentation"] == "required_read"]
    result = remap_references({"action": "approve", "reason": "Read-only diagnosis is useful",
        "evidence_refs": [check["observation"]["material_id"]], "selected_action": "diagnose",
        "expected_result": "Identify the missing preview evidence"}, prepared.reference_map)
    DecisionService.validate(request, DecisionResult.model_validate(result))
    with pytest.raises(ValueError, match="Unverified proposals"):
        DecisionService.validate(request, DecisionResult.model_validate({**result,
            "evidence_refs": ["history.recent[0].outcome.diagnosis"]}))
    with pytest.raises(InvalidStructuredResponse, match="Ambiguous"):
        remap_references({"evidence_refs": [index["artifact"]["hash"]]}, prepared.reference_map)


@pytest.mark.parametrize("stage,envelope", [("decision", "decision_request"),
    ("recovery_diagnosis", "failure"), ("implement", "feedback"), ("review", "feedback"), ("verify", "feedback")])
def test_large_citation_lists_are_optional_across_stage_envelopes(planner, tmp_path, stage, envelope):
    refs = [f"controller-observation/{i}" for i in range(3000)]
    original = {envelope: {"evidence_refs": refs, "observations": 37,
        "constraints": ["Preserve all assertions", "Do not expand allowed paths"]}}
    result = planner.prepare(stage, original, tmp_path)
    view = result.payload[envelope]
    assert view["observations"] == 37
    assert view["constraints"] == original[envelope]["constraints"]
    assert json.loads(text_of(planner, view["evidence_refs"])) == refs
    assert not view["evidence_refs"]["required_read"]
    assert len(PromptCompiler().compile(stage, result.payload)[0]) < 64000


def test_progress_ids_and_diagnostic_history_do_not_become_mandatory_reading(planner, tmp_path):
    obligations = {f"test/command/{i}": i % 2 == 0 for i in range(1000)}
    progress = {"tracked": {"obligations": obligations}, "observations": 100, "stalled": 4,
                "last": {"improved": False, "regressions": list(obligations)}}
    prepared = planner.prepare("decision", {"decision_request": {"evidence": {"progress": progress}}}, tmp_path)
    view = prepared.payload["decision_request"]["evidence"]["progress"]
    assert view["observations"] == 100 and view["stalled"] == 4
    assert view["tracked"]["obligations"]["satisfied"] == 500
    assert view["tracked"]["obligations"]["unsatisfied"] == 500
    assert view["last"]["regressions"]["count"] == 1000 and not view["last"]["improved"]
    assert json.loads(text_of(planner, view["tracked"]["obligations"]["details"])) == obligations
    observations = [{"checks": ["Prior check record"]}] * 1000
    view = planner.prepare("recovery_diagnosis", {"observations": observations,
        "failure": {"kind": "review", "evidence": {"review": {"approved": False}}}}, tmp_path).payload
    assert not view["observations"]["required_read"]
    assert json.loads(text_of(planner, view["observations"])) == observations
    assert view["failure"]["evidence"]["review"]["approved"] is False


def test_alias_registration_does_not_persist_the_derived_catalog():
    failure = FailureRecord.create(kind="review", component="review", origin_id="T010", step="review",
        obligations=["R1"], evidence_refs=["develop/task/T010"],
        evidence={"checks": [{"test_ids": [f"case-{i}" for i in range(3000)]}]})
    registered = evidence_aliases(failure, {"inventory": ["app.py", "test_app.py"]})
    assert registered == ["app.py", "develop/task/T010", "test_app.py"]
    failure = failure.model_copy(update={"evidence_refs": registered})
    assert "failure.evidence.checks[0].test_ids[2999]" in evidence_references(failure)
    diagnosis = Diagnosis(action="diagnose", reason="Locate missing evidence",
        evidence_refs=["failure.evidence.checks[0].test_ids[2999]"])
    assert adjudicate(failure, diagnosis=diagnosis).action == "diagnose"
    # An already recorded expanded index remains valid; upgrades do not rewrite history.
    legacy = failure.model_copy(update={"evidence_refs": evidence_references(failure)})
    assert set(evidence_aliases(legacy)) == set(legacy.evidence_refs)


def test_selected_report_dependencies_are_exposed_once_and_remain_readable(planner, tmp_path):
    report = planner.artifacts.put("<testsuite/>", "junit")
    log = planner.artifacts.put("Historical diagnostic", "check_log")
    observations = [{"report": report, "logs": [log]}]
    view = planner.prepare("recovery_diagnosis", {"observations": observations, "failure": {"report": report}}, tmp_path).payload
    assert view["evidence_materials"] == [log]
    assert planner.artifacts.read(view["failure"]["report"]) == "<testsuite/>"
    assert planner.artifacts.read(view["evidence_materials"][0]) == "Historical diagnostic"


def test_pending_legacy_presentation_upgrades_once_without_replacing_receipts(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    artifacts = ArtifactStore(store.root)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    context = {"decision_request": legacy_request().model_dump()}
    old = {"version": "context-v1", "purpose": "decision", "payload": context, "selections": [], "reference_map": {}}
    key = "presentation/decision/identity"
    receipt = {"identity": "identity", "artifact": artifacts.put(old, "prepared_context")}
    store.checkpoints(rid, {key: receipt, "recovery/budgets/diagnosis/T010": {"round": 15},
        "decisions/requests/saved": {"attempts": 1, "result": {"action": "ask_user"}}})
    new = router._prepare_context("decision", context, tmp_path, rid, "decision", "identity")
    assert new.version == CONTEXT_VERSION and len(json.dumps(new.payload)) < 64000
    assert store.get(rid, key) == receipt and artifacts.json(receipt["artifact"]) == old
    assert store.get(rid, "recovery/budgets/diagnosis/T010") == {"round": 15}
    assert store.get(rid, "decisions/requests/saved")["attempts"] == 1
    frozen = router._prepare_context("decision", {}, tmp_path, rid, "decision", "identity")
    assert frozen.record() == new.record()
    upgraded = store.get(rid, key + "/" + CONTEXT_VERSION)["artifact"]
    (artifacts.root / upgraded["hash"][:2] / upgraded["hash"]).write_text("corrupt")
    with pytest.raises(ValueError, match="integrity"):
        router._prepare_context("decision", context, tmp_path, rid, "decision", "identity")


@pytest.mark.parametrize("version", ["context-v1", "unsupported"])
def test_corrupt_or_unknown_old_presentations_are_not_silently_replaced(tmp_path, version):
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    artifacts = ArtifactStore(store.root)
    ref = artifacts.put({"version": version}, "prepared_context")
    store.checkpoint(rid, "presentation/decision/identity", {"artifact": ref})
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    if version == "context-v1":
        (artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt")
    with pytest.raises((ValueError, ProviderError)):
        router._prepare_context("decision", {}, tmp_path, rid, "decision", "identity")
    assert store.get(rid, "presentation/decision/identity/" + CONTEXT_VERSION) is None


@pytest.mark.parametrize("action", ["approve", "ask_user"])
async def test_rendering_upgrade_never_repeats_a_completed_decision(tmp_path, action):
    _, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    request = legacy_request()
    result = DecisionResult(action=action, reason="Saved conclusion", evidence_refs=["failure.evidence.review"],
        selected_action="diagnose" if action == "approve" else None, expected_result="A diagnosis")
    key = "decisions/requests/" + digest(request.model_dump())
    saved = {"request": request.model_dump(), "attempts": 1, "result": result.model_dump(), "validation_version": 4}
    store.checkpoint(rid, key, saved)

    async def unexpected_invocation(*args, **kwargs):
        pytest.fail("Rendering changes cannot authorize a new decision")

    service = DecisionService(SimpleNamespace(store=store, _invoke=unexpected_invocation))
    assert await service.propose(rid, request, tmp_path) == (key, result)
    assert store.get(rid, key) == saved
