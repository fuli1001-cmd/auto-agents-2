"""Final evidence is required across real Engine delivery journal recovery."""
import asyncio
import copy
import sys

import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Command
from auto_agents_2.workspace import git


class FinalGateAgent(CodingAgent):
    async def invoke(self, stage, context, schema, *args, **kwargs):
        result = await super().invoke(stage, context, schema, *args, **kwargs)
        if stage == "plan":
            result.final_checks.append(Command(argv=[sys.executable, "-c",
                "import os; assert os.environ.get('AA2_AUDIT_FINAL_GATE') == 'pass'"]))
        return result


async def paused_delivery(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv("AA2_AUDIT_FINAL_GATE", "pass")
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers")
    agent = FinalGateAgent()
    workflow = engine(project, agent)
    save = workflow.delivery.save

    def stop(rid, journal, phase=None):
        save(rid, journal, phase)
        if journal["phase"] == boundary:
            raise asyncio.CancelledError()

    monkeypatch.setattr(workflow.delivery, "save", stop)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    return workflow, rid, agent


@pytest.mark.parametrize("drift", ["corrupt_report", "missing_report", "environment", "command"])
async def test_ready_requires_complete_current_final_observation(tmp_path, monkeypatch, drift):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, "ready")
    journal = workflow.store.get(rid, "delivery/journal")
    target = workflow.workspaces.head(journal["target"])
    original_checks = workflow.store.get(rid, "verify/checks")
    ref = next(row["report"] for row in original_checks if row.get("report"))
    path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]
    raw = path.read_bytes()
    if drift == "corrupt_report":
        path.write_bytes(b"corrupt isolated report")
    elif drift == "missing_report":
        path.unlink()
    elif drift == "environment":
        monkeypatch.setenv("AA2_AUDIT_FINAL_GATE", "fail")
    else:
        rows = copy.deepcopy(original_checks)
        rows[0]["command_hash"] = "another-command-with-the-same-display-name"
        workflow.store.checkpoint(rid, "verify/checks", rows)
    calls = list(agent.calls)
    resumed = engine(workflow.project, agent)
    with pytest.raises(StateConflict):
        await resumed._execute(rid)
    assert resumed.store.run(rid)["status"] != "SUCCEEDED"
    assert resumed.workspaces.head(journal["target"]) == target
    assert resumed.store.get(rid, "delivery/journal") == journal
    assert resumed.store.get(rid, "verify/review")["approved"]
    assert agent.calls == calls
    # Restoring the exact original bytes/inputs is not a new approval or run.
    path.write_bytes(raw)
    monkeypatch.setenv("AA2_AUDIT_FINAL_GATE", "pass")
    workflow.store.checkpoint(rid, "verify/checks", original_checks)
    result = await resumed._execute(rid)
    assert result["status"] == "SUCCEEDED"
    assert agent.calls == calls


@pytest.mark.parametrize("boundary", ["publishing", "published", "runtime", "completion_pending"])
async def test_postpublication_required_report_failure_never_creates_success(tmp_path, monkeypatch, boundary):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, boundary)
    original = workflow.store.get(rid, "delivery/journal")
    ref = next(row["report"] for row in workflow.store.get(rid, "verify/checks") if row.get("report"))
    path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]
    raw = path.read_bytes()
    path.unlink()
    resumed = engine(workflow.project, agent)
    with pytest.raises(StateConflict):
        await resumed._execute(rid)
    saved = resumed.store.get(rid, "delivery/journal")
    assert saved["candidate"] == original["candidate"]
    assert resumed.workspaces.head(saved["target"]) == saved["candidate"]
    assert saved["runtime_budget_used"] == original["runtime_budget_used"]
    assert resumed.store.run(rid)["status"] != "SUCCEEDED"
    path.write_bytes(raw)
    assert (await resumed._execute(rid))["status"] == "SUCCEEDED"
    assert resumed.store.get(rid, "delivery/receipt")["candidate"] == saved["candidate"]


async def test_pending_receipt_cannot_outlive_its_required_final_report(tmp_path, monkeypatch):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, "completion_pending")
    resumed = engine(workflow.project, agent)
    def interrupt(*args):
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(resumed.store, "complete_delivery", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await resumed._execute(rid)
    pending = resumed.store.get(rid, "delivery/journal")
    assert pending.get("completion_receipt")
    ref = next(row["report"] for row in pending["completion_receipt"]["evidence"]["verify/checks"] if row.get("report"))
    path = resumed.artifacts.root / ref["hash"][:2] / ref["hash"]
    raw = path.read_bytes()
    path.write_bytes(b"bad report after receipt save")
    with pytest.raises(StateConflict):
        await resumed._execute(rid)
    assert resumed.store.get(rid, "delivery/journal") == pending
    assert git(workflow.project, "rev-parse", "HEAD") == pending["candidate"]
    path.write_bytes(raw)
    assert (await resumed._execute(rid))["status"] == "SUCCEEDED"
    completed = resumed.store.get(rid, "delivery/receipt")
    assert completed["runtime_budget_used"] == pending["runtime_budget_used"]
    # A completed run is historical; a later protocol update does not reopen it.
    monkeypatch.setenv("AA2_AUDIT_FINAL_GATE", "changed-after-completion")
    assert (await resumed._execute(rid))["status"] == "SUCCEEDED"
    assert resumed.store.get(rid, "delivery/receipt") == completed


@pytest.mark.parametrize("defect", ["missing_report", "bad_hash", "no_report_status", "attachment_missing_ref"])
async def test_completion_rejects_malformed_required_references(tmp_path, monkeypatch, defect):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, "completion_pending")
    rows = workflow.store.get(rid, "verify/checks")
    row = next(row for row in rows if row.get("test_runner"))
    if defect == "missing_report":
        row["report"] = {}
    elif defect == "bad_hash":
        row["report"]["hash"] = "not-an-artifact"
    elif defect == "no_report_status":
        row["report_status"] = ""
    else:
        row["attachments"].append({"name": "required-state.json"})
    workflow.store.checkpoint(rid, "verify/checks", rows)
    resumed = engine(workflow.project, agent)
    with pytest.raises(StateConflict):
        await resumed._execute(rid)
    assert resumed.store.run(rid)["status"] != "SUCCEEDED"


@pytest.mark.parametrize("defect", ["missing", "bad_hash", "bad_content", "tile"])
async def test_completion_reads_current_final_screenshot_references(tmp_path, monkeypatch, defect):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, "completion_pending")
    # This is an archive-integrity injection, not a claim that these fixture
    # bytes prove rendered product behaviour. Rendering has its own real tests.
    ref = workflow.artifacts.put(b"isolated screenshot bytes", "screenshot")
    visual = {"screenshot": "archived.png", "image_artifact": ref}
    original = copy.deepcopy(visual)
    if defect == "missing":
        visual.pop("image_artifact")
    elif defect == "bad_hash":
        visual["image_artifact"] = {**ref, "hash": "invalid"}
    else:
        (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).write_bytes(b"corrupt")
    rows = [{"tiles": [visual]}] if defect == "tile" else [visual]
    workflow.store.checkpoint(rid, "verify/visuals", rows)
    resumed = engine(workflow.project, agent)
    with pytest.raises(StateConflict):
        await resumed._execute(rid)
    assert resumed.store.run(rid)["status"] != "SUCCEEDED"
    # Retain the original archived reference for diagnosing this fixture failure.
    assert original["image_artifact"]["hash"] == ref["hash"]


async def test_consumed_diagnostics_and_old_invalid_history_do_not_become_new_gates(tmp_path, monkeypatch):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, "ready")
    from auto_agents_2.review_evidence import ReviewEvidence, consumer_key
    rows = workflow.store.get(rid, "verify/checks")
    # An explicitly synthetic optional attachment exercises real consumer/GC
    # transitions. Ordinary command logs remain protected while the run is active.
    diagnostic = workflow.artifacts.put("optional isolated diagnostic", "test_diagnostic")
    rows[0]["attachments"].append({"path": "diagnostic.txt", "artifact": diagnostic})
    owners = ReviewEvidence(workflow.store)
    assert owners.retain(rid, consumer_key(), rows, updates={"verify/checks": rows})
    workflow.resources.collect_artifacts()
    assert not workflow.artifacts.released(diagnostic)
    owners.complete(rid, consumer_key(), rows)
    workflow.resources.collect_artifacts()
    assert workflow.artifacts.released(diagnostic)
    stale = workflow.artifacts.put(b"an earlier invalid machine report", "junit")
    workflow.store.checkpoint(rid, "recovery/verification-history/old/fixture", {"report": stale})
    (workflow.artifacts.root / stale["hash"][:2] / stale["hash"]).write_bytes(b"old bad evidence")
    resumed = engine(workflow.project, agent)
    assert (await resumed._execute(rid))["status"] == "SUCCEEDED"


async def test_new_candidate_ready_gate_is_not_disabled_by_earlier_publication(tmp_path, monkeypatch):
    workflow, rid, agent = await paused_delivery(tmp_path, monkeypatch, "ready")
    journal = workflow.store.get(rid, "delivery/journal")
    journal["publication_started"] = True  # A retained prior publication/race marker is not an exemption.
    workflow.store.checkpoint(rid, "delivery/journal", journal)
    monkeypatch.setenv("AA2_AUDIT_FINAL_GATE", "fail")
    resumed = engine(workflow.project, agent)
    with pytest.raises(StateConflict):
        await resumed._execute(rid)
    assert resumed.workspaces.head(journal["target"]) == journal["old"]
