"""Archived proposal bytes remain inspectable without becoming accepted source."""
import copy
import json

import pytest
from test_diagnosis_decisions import proposal_failure
from test_recovery_policy import ready
from test_review_evidence import commit
from test_workflow import CodingAgent, engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import Change, ImplementationResult
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.source_history import SourceHistory
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash


def archive_fixture(tmp_path, owner="gate"):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("retained rejected candidate", {})["id"]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_original.py").write_bytes(b"assert original()\r\n")
    commit(tmp_path)
    base = git(tmp_path, "rev-parse", "HEAD")
    ref = f"refs/aa2/{rid}/replans/0/T1"
    git(tmp_path, "update-ref", ref, base)
    workflow.store.checkpoint(rid, "baseline", base)
    text = "assert original()\r\n" + "".join(f"assert regression_{i}()\r\n" for i in range(11))
    proposal = ImplementationResult(summary="Unapplied repair", changes=[
        Change(path="tests/test_original.py", operation="replace", old_text="assert original()\r\n", content=text),
        Change(path="app.py", content="proposed parser repair\n")]).model_dump()
    command = {"argv": ["python", "-m", "pytest", "tests"], "cwd": ".", "expected_tests": 233}
    report = workflow.artifacts.put("<testsuite tests='233'/>", "junit")
    checks = [{"argv": command["argv"], "report": report, "test_ids": ["tests/test_original.py::test_original"],
               "source_hash": tree_hash(tmp_path), "exit_code": 0}]
    saved = {"phase": "rejected", "workspace": str(tmp_path), "source_hash": tree_hash(tmp_path),
             "implementation": proposal, "candidate_error": "Protected candidate rejected",
             "feedback": {"checks": checks}, "review": {"approved": False, "summary": "Needs architecture review"}}
    plan = {"tasks": [{"id": "T1", "checks": [command]}]}
    if owner == "gate":
        workflow.store.checkpoints(rid, {"plan": plan, "develop/task/T1": saved,
            "invocation/develop/task/T1": {"result": proposal, "artifact": workflow.artifacts.put(proposal, "implement")}})
        workflow.store.prepare_replan(rid, "design", "Preserve proposed regressions across design repair")
        workflow.store.complete_replan(rid, ["design", "plan", "develop"], [ref])
        archive = "recovery/gate-history/0"
    elif owner == "baseline":
        archive = "recovery/task-baselines/T1/fixture"
        workflow.store.checkpoint(rid, archive, {"previous_task": saved, "recovery_ref": ref})
    else:
        archive = "plan_revision/history/1"
        workflow.store.checkpoint(rid, archive, {"plan": plan, "tasks": {"develop/task/T1": saved}})
        workflow.store.checkpoint(rid, "replan_feedback", {"recovery_refs": [ref]})
    return workflow, rid, proposal, saved, archive, command, report


@pytest.mark.parametrize("owner", ["gate", "baseline", "plan"])
def test_archived_unapplied_proposal_and_prior_checks_are_readable_without_applying(tmp_path, owner):
    workflow, rid, original, saved, archive, command, junit = archive_fixture(tmp_path, owner)
    (tmp_path / "tests/test_original.py").unlink()  # New integration baseline has no task source.
    before = tree_hash(tmp_path)
    history = SourceHistory(workflow).collect(rid, tmp_path, ["app.py", "tests"])
    manifest = workflow.artifacts.json(history["artifact"])
    proposals = manifest.get("proposals", [])
    assert proposals, "Git history alone omits the rejected pre-application proposal"
    candidate = next(c for c in proposals if c["origin"] == archive + "/develop/task/T1")
    assert candidate["status"] == "proposal_only"
    assert candidate["recorded_phase"] == "rejected"
    assert candidate["recorded_source_hash"] == saved["source_hash"]
    assert candidate["proposal_hash"] == digest(original)
    body = workflow.artifacts.json(candidate["content"])
    assert body["changes"] == original["changes"] and candidate["omitted_changes"] == 0
    assert candidate["checks"] == saved["feedback"]["checks"]
    if owner != "baseline":
        assert candidate["commands"] == [command]
    view = ContextPlanner(workflow.artifacts).prepare("implement", {"source_history": history}, tmp_path)
    mounted = {r["hash"] for r in view.payload["evidence_materials"]}
    assert {candidate["content"]["hash"], junit["hash"]} <= mounted
    assert tree_hash(tmp_path) == before and not (tmp_path / "app.py").exists()
    assert not workflow.store.get(rid, "develop/done")


def test_proposal_history_cache_binds_bytes_even_when_source_and_git_refs_do_not_change(tmp_path):
    workflow, rid, _, _, archive, _, _ = archive_fixture(tmp_path)
    original = SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])
    assert SourceHistory(workflow).collect(rid, tmp_path, ["app.py"]) == original
    record = workflow.store.get(rid, archive)
    record["checkpoints"]["develop/task/T1"]["implementation"]["changes"][0]["content"] += "assert extra_regression()\n"
    workflow.store.checkpoint(rid, archive, record)
    updated = SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])
    assert updated["source_hash"] == original["source_hash"] and updated["anchors"] == original["anchors"]
    assert updated["artifact"] != original["artifact"]


def test_proposals_filter_paths_and_metadata_without_following_proposal_references(tmp_path):
    workflow, rid, _, _, archive, _, _ = archive_fixture(tmp_path)
    config = workflow.config.load().model_dump(mode="json")
    config["sensitive_paths"] = ["private/**"]
    write_json(workflow.config.path, config)
    unrelated = workflow.artifacts.put("UNRELATED_PRIVATE_FIXTURE", "private_fixture")
    record = workflow.store.get(rid, archive)
    proposal = record["checkpoints"]["develop/task/T1"]["implementation"]
    proposal["summary"] = "SUMMARY_NOT_A_SOURCE_FILE"
    proposal["report"] = unrelated
    paths = [".env", "private/test_secret.py", ".codex/test_config.py", ".git/test_config.py", ".aa2/test_config.py",
             "../test_escape.py", "/tmp/test_escape.py", "other.py"]
    proposal["changes"] += [Change(path=p, content="EXCLUDED_FIXTURE").model_dump() for p in paths]
    proposal["changes"].append(Change(path="tests/reference.json", content=json.dumps({"report": unrelated})).model_dump())
    workflow.store.checkpoint(rid, archive, record)
    history = SourceHistory(workflow).collect(rid, tmp_path, ["app.py", "private"])
    manifest = workflow.artifacts.json(history["artifact"])
    assert manifest.get("proposals")
    candidate = next(c for c in manifest["proposals"] if c["origin"].startswith(archive))
    body = workflow.artifacts.json(candidate["content"])
    assert {c["path"] for c in body["changes"]} == {"app.py", "tests/test_original.py", "tests/reference.json"}
    assert candidate["omitted_changes"] == len(paths)
    assert "EXCLUDED_FIXTURE" not in json.dumps(body) and "SUMMARY_NOT_A_SOURCE_FILE" not in json.dumps(body)
    view = ContextPlanner(workflow.artifacts).prepare("recovery_diagnosis", {"source_history": history}, tmp_path)
    mounted = {r["hash"] for r in view.payload["evidence_materials"]}
    assert candidate["content"]["hash"] in mounted and unrelated["hash"] not in mounted


async def test_recovery_diagnosis_receives_archived_proposal_and_baseline(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path, record=proposal_failure())
    proposal = ImplementationResult(summary="Unapplied historical repair", changes=[
        Change(path="tests/test_regression.py", content="assert original_regression()\n")]).model_dump()
    workflow.store.checkpoint(rid, "recovery/gate-history/0", {"checkpoints": {
        "develop/task/" + failure.task_id: {"phase": "rejected", "implementation": proposal,
            "source_hash": tree_hash(work), "candidate_error": "Protected candidate rejected"}}})
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        assert stage == "recovery_diagnosis"
        history = workflow.artifacts.json(context["source_history"]["artifact"])
        assert history["proposals"]
        body = workflow.artifacts.json(history["proposals"][0]["content"])
        assert body["changes"] == proposal["changes"]
        calls.append(copy.deepcopy(context))
        return Diagnosis(action="repair", task_ids=[failure.task_id], evidence_refs=["source_history"],
                         reason="Retained proposal is available for scoped repair, not acceptance")

    workflow.agent.invoke = invoke
    before = tree_hash(work)
    await workflow.recovery.resume(rid)
    assert len(calls) == 1 and tree_hash(work) == before
    assert not workflow.store.get(rid, "develop/done")
    assert not workflow.store.pending(rid)


def test_available_proposal_does_not_hide_a_missing_git_baseline(tmp_path):
    workflow, rid, _, _, _, _, _ = archive_fixture(tmp_path)
    ref = f"refs/aa2/{rid}/replans/0/T1"
    git(tmp_path, "update-ref", "-d", ref)
    history = SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])
    assert history["status"] == "incomplete" and history["missing"] == [{"anchor": ref, "status": "missing"}]
    assert workflow.artifacts.json(history["artifact"])["proposals"]


@pytest.mark.parametrize('grounded', [True, False])
async def test_delivery_diagnosis_uses_the_same_filtered_history_without_acceptance(tmp_path, grounded):
    from authority_review_fixtures import recorded_review
    from test_decisions import setup

    from auto_agents_2.faults import StateConflict

    workflow, rid = setup(tmp_path)
    proposal = ImplementationResult(summary="Retained parser repair", changes=[
        Change(path="value.py", content="value = 2\n")]).model_dump()
    workflow.store.checkpoint(rid, "recovery/gate-history/0", {"checkpoints": {
        "develop/task/T1": {"phase": "rejected", "implementation": proposal, "source_hash": tree_hash(tmp_path)}}})
    original_invoke = workflow.agent.invoke
    calls = []

    async def invoke(stage, context, *args, **kwargs):
        if stage != "recovery_diagnosis":
            return await original_invoke(stage, context, *args, **kwargs)
        history = workflow.artifacts.json(context["source_history"]["artifact"])
        assert history["scope"] == ["value.py"]
        assert workflow.artifacts.json(history["proposals"][0]["content"])["changes"] == proposal["changes"]
        calls.append(stage)
        return Diagnosis(action="repair", evidence_refs=["source_history"], reason="Reconcile the retained proposal")

    workflow.agent.invoke = invoke
    before = tree_hash(tmp_path)
    raw = {"approved": False, "summary": "Needs repair", "findings": []}
    if grounded:
        raw = recorded_review(workflow, rid, raw, work=tmp_path)
        action = await workflow.recovery.delivery_decision(rid, tmp_path, {"review": raw}, ["value.py"])
        assert action == "repair"
    else:
        with pytest.raises(StateConflict, match='ungrounded review'):
            await workflow.recovery.delivery_decision(rid, tmp_path, {"review": raw}, ["value.py"])
    assert calls == ["recovery_diagnosis"]
    assert tree_hash(tmp_path) == before and not workflow.store.get(rid, "delivery/receipt")
