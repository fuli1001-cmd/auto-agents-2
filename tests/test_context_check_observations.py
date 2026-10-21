"""Large per-case issue ledgers must not replicate complete check evidence."""
import copy
import json

import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import CONTEXT_VERSION, ContextPlanner, remap_references
from auto_agents_2.errors import AA2Error, InvalidStructuredResponse
from auto_agents_2.recovery import Diagnosis, FailureRecord, adjudicate
from auto_agents_2.store import Store


@pytest.fixture
def observations(tmp_path):
    store = Store(tmp_path)
    store.admit("check presentation fixture", {})
    artifacts = ArtifactStore(store.root)
    attachments = [{"path": f"SENTINEL_ATTACHMENT_CASE_{i}/component-observation.json", "purpose": "diagnostic",
                    "artifact": artifacts.put({"actual": i, "expected": i + 1}, "test_diagnostic")}
                   for i in range(40)]
    check = {"argv": ["pytest"], "source_hash": "candidate-A", "environment_hash": "environment-A",
             "command_hash": "command-A", "exit_code": 1, "process_exit_code": 1, "tests": 38, "skipped": 0,
             "timed_out": False, "report_status": "valid", "failure_reasons": ["assertions_failed"],
             "test_ids": [f"case-{i}" for i in range(40)], "passed_test_ids": [f"case-{i}" for i in range(38)],
             "attachments": attachments, "attachment_capture": {"status": "complete"},
             "logs": [artifacts.put("complete failure log", "check_log")],
             "report": artifacts.put({"tests": 40, "failed": 2}, "machine_report"),
             "environment_resolution": artifacts.put({"version": "locked"}, "environment_resolution")}
    return ContextPlanner(artifacts), check


@pytest.mark.parametrize("stage,slot,review_kind", [
    ("implement", "unresolved_issues", None),
    ("recovery_diagnosis", "current_issues", None),
    ("review", "unresolved_issues", "implementation"),
    ("review", "unresolved_issues", "test_changes"),
    ("verify", "unresolved_issues", None),
])
def test_same_check_is_presented_once_with_each_issue_and_original_citation(observations, tmp_path, stage, slot, review_kind):
    planner, check = observations
    issues = {f"issue-{i}": {"scope": "checks/T1", "owner": "T1", "status": "stale" if i == 10 else "open",
                            "subject": f"case-{i}", "evidence": {"valid": i != 10, "reason": "original reason",
                            "check": copy.deepcopy(check)}, "observations": 7} for i in range(11)}
    context = {"task": {"id": "T1", "allowed_paths": ["tests"]}, slot: issues,
               "mandatory": ["Preserve every assertion", "No permission changes"],
               "verification_contract": {"commands": [{"argv": ["pytest"], "expected_tests": 40}]}}
    if review_kind:
        context["review_kind"] = review_kind
    original = copy.deepcopy(context)
    result = planner.prepare(stage, context, tmp_path)
    projected = result.payload[slot]
    first_path = f"{slot}.issue-0.evidence.check"
    first = projected["issue-0"]["evidence"]["check"]
    assert isinstance(first["attachments"], dict)
    assert planner.artifacts.json(first["attachments"]["artifact"]) == check["attachments"]
    assert first["unpassed_test_ids"] == ["case-38", "case-39"]
    assert first["failure_reasons"] == check["failure_reasons"]
    assert first["report_status"] == "valid" and first["exit_code"] == 1
    for i, issue in enumerate(projected.values()):
        shown = issue["evidence"]["check"]
        path = f"{slot}.issue-{i}.evidence.check"
        assert shown["source_ref"] == path
        if i:
            assert shown["same_observation_as"] == first_path
        assert planner.artifacts.json(shown["observation"]["artifact"]) == check
        assert remap_references({"evidence_refs": [shown["observation"]["material_id"]]}, result.reference_map) == {"evidence_refs": [path]}
        assert issue["subject"] == issues[f"issue-{i}"]["subject"]
        assert issue["observations"] == 7
        assert issue["status"] == issues[f"issue-{i}"]["status"]
        assert issue["evidence"]["valid"] == (i != 10)
    assert context == original
    assert result.payload["mandatory"] == context["mandatory"]
    if stage != "recovery_diagnosis":
        assert result.payload["verification_contract"] == context["verification_contract"]
    assert "SENTINEL_ATTACHMENT_CASE" not in json.dumps(result.payload)
    supplied = {row["hash"] for row in result.payload["evidence_materials"]}
    assert {row["artifact"]["hash"] for row in check["attachments"]} <= supplied
    with pytest.raises(InvalidStructuredResponse, match="Ambiguous"):
        remap_references({"evidence_refs": [first["observation"]["artifact"]["hash"]]}, result.reference_map)


@pytest.mark.parametrize("changed", ["source_hash", "environment_hash", "exit_code", "passed_test_ids", "attachments"])
def test_distinct_observations_are_not_merged(observations, tmp_path, changed):
    planner, check = observations
    other = copy.deepcopy(check)
    other[changed] = ([] if isinstance(other[changed], list) else 0 if isinstance(other[changed], int) else "different")
    result = planner.prepare("verify", {"evidence": [check, other]}, tmp_path)
    first, second = result.payload["evidence"]
    assert "same_observation_as" not in first and "same_observation_as" not in second
    assert planner.artifacts.json(second["observation"]["artifact"]) == other


def test_same_check_in_proposal_keeps_its_unverified_origin(observations, tmp_path):
    planner, check = observations
    failure = FailureRecord.create(kind="proposal", component="implementation", origin_id="T1", step="task",
                                   obligations=["R1"], evidence={"checks": [check]})
    failure.proposal = {"checks": [copy.deepcopy(check)]}
    context = {"failure": failure.model_dump()}
    prepared = planner.prepare("recovery_diagnosis", context, tmp_path)
    untrusted = prepared.payload["failure"]["proposal"]["checks"][0]
    response = remap_references({"action": "diagnose", "reason": "Inspect the record",
                                "evidence_refs": [untrusted["observation"]["material_id"]]}, prepared.reference_map)
    with pytest.raises(AA2Error, match="requires controller evidence"):
        adjudicate(failure, diagnosis=Diagnosis.model_validate(response), evidence_context=context)
    trusted = prepared.payload["failure"]["evidence"]["checks"][0]
    response["evidence_refs"] = [trusted["observation"]["material_id"]]
    response = remap_references(response, prepared.reference_map)
    assert adjudicate(failure, diagnosis=Diagnosis.model_validate(response), evidence_context=context).action == "diagnose"


def test_large_unknown_constraints_and_command_attachment_contracts_remain_exact(observations, tmp_path):
    planner, check = observations
    command = {"argv": ["pytest"], "attachments": [{"path": "required.json", "required": True}], "expected_tests": 40}
    context = {"extension_constraints": {"attachments": ["Do not remove this obligation" * 500]},
               "verification_contract": {"commands": [command]}, "checks": [check]}
    result = planner.prepare("review", context, tmp_path)
    assert result.payload["extension_constraints"] == context["extension_constraints"]
    assert result.payload["verification_contract"] == context["verification_contract"]


def test_small_attachment_index_stays_inline(observations, tmp_path):
    planner, check = observations
    check["attachments"] = check["attachments"][:1]
    result = planner.prepare("verify", {"evidence": [check]}, tmp_path)
    assert result.payload["evidence"][0]["attachments"] == check["attachments"]


@pytest.mark.parametrize("stage", ["decision", "plan"])
def test_recovery_decision_and_plan_patch_share_observation_projection(observations, tmp_path, stage):
    planner, check = observations
    evidence = {"checks": [check, copy.deepcopy(check)]}
    if stage == "decision":
        context = {"decision_request": {"evidence": evidence, "allowed_choices": ["ask_user"], "scope": {"tasks": ["T1"]}}}
        path = ["decision_request", "evidence", "checks"]
    else:
        context = {"plan_patch": True, "failure": {"task_id": "T1", "evidence": evidence}}
        path = ["failure", "evidence", "checks"]
    prepared = planner.prepare(stage, context, tmp_path)
    value = prepared.payload
    for key in path:
        value = value[key]
    assert value[1]["same_observation_as"] == ".".join(path) + "[0]"
    assert planner.artifacts.json(value[0]["observation"]["artifact"]) == check
    assert planner.artifacts.json(value[1]["observation"]["artifact"]) == check


def test_location_index_does_not_mint_controller_evidence(observations, tmp_path):
    from auto_agents_2.materials import present_materials
    planner, check = observations
    failure = FailureRecord.create(kind="proposal", component="implementation", origin_id="T1", step="task",
                                   obligations=["R1"], evidence={"checks": [check]})
    context = {"failure": failure.model_dump()}
    prepared = planner.prepare("recovery_diagnosis", context, tmp_path)
    for ref in prepared.payload["evidence_materials"]:
        ref["location"] = str(planner.artifacts.root / ref["hash"][:2] / ref["hash"])
    rendered = present_materials(prepared.payload, planner.artifacts, explicit_files=set())
    catalogue = rendered["evidence_materials"]["artifact"]
    with pytest.raises(AA2Error, match="not supplied"):
        adjudicate(failure, diagnosis=Diagnosis(action="diagnose", reason="Not a new fact", evidence_refs=[catalogue["hash"]]),
                   evidence_context=context)
    with pytest.raises(AA2Error, match="not supplied"):
        adjudicate(failure, diagnosis=Diagnosis(action="diagnose", reason="Not a supplied source", evidence_refs=["f" * 64]),
                   evidence_context=context)


@pytest.mark.parametrize("damage", ["unknown_version", "corrupt_legacy"])
def test_pending_view_does_not_replace_unknown_or_corrupt_record(observations, tmp_path, damage):
    from test_runtime import configured

    from auto_agents_2.errors import ProviderError
    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.sandbox import SandboxRunner
    config, store = configured(tmp_path)
    rid = store.current()["id"]
    artifacts = ArtifactStore(store.root)
    record = {"version": "context-v999" if damage == "unknown_version" else "context-v3",
              "purpose": "implementation", "payload": {"mandatory": ["immutable constraint"]},
              "selections": [], "reference_map": {}}
    ref = artifacts.put(record, "prepared_context")
    key = "presentation/review/frozen"
    store.checkpoint(rid, key, {"identity": "frozen", "artifact": ref})
    if damage == "corrupt_legacy":
        (artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt")
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    with pytest.raises((ValueError, ProviderError), match="integrity|Unsupported"):
        router._prepare_context("review", {}, tmp_path, rid, "review", "frozen")
    assert store.get(rid, key) == {"identity": "frozen", "artifact": ref}
    assert store.get(rid, key + "/" + CONTEXT_VERSION) is None
