from pathlib import Path

import pytest
from test_decisions import ApprovingAgent
from test_runtime import configured
from test_workflow import CodingAgent, engine
from typer.testing import CliRunner

from auto_agents_2 import cli
from auto_agents_2.errors import AA2Error, BusyProject


async def test_invalid_stage_stop_changes_no_task_state(tmp_path):
    workflow = engine(tmp_path, ApprovingAgent())
    before = workflow.store.events()
    with pytest.raises(AA2Error, match="only after provider_research"):
        await workflow.start(requirements_text="Fixture", stop_after="prototype")
    assert workflow.store.current() is None and workflow.store.events() == before


def test_resume_cli_forwards_runtime_only_stage_stop(tmp_path, monkeypatch):
    calls = []
    async def execute(project, **kwargs):
        calls.append((project, kwargs))
    monkeypatch.setattr(cli, "execute", execute)
    result = CliRunner().invoke(cli.app, ["resume", "--project", str(tmp_path), "--run", "fixture",
                                        "--stop-after", "provider_research"])
    assert result.exit_code == 0, result.output
    assert calls == [(Path(tmp_path).resolve(), {"provider": None, "run_id": "fixture", "stop_after": "provider_research"})]


async def test_stage_stop_never_attaches_to_unbounded_active_controller(tmp_path, monkeypatch):
    _, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    class BusyEngine:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self, *args, **kwargs):
            raise BusyProject("fixture controller already running")
    monkeypatch.setattr(cli, "WorkflowEngine", BusyEngine)
    with pytest.raises(BusyProject, match="cannot be attached"):
        await cli.execute(tmp_path, run_id=rid, stop_after="provider_research")


@pytest.mark.parametrize("key", ["delivery/journal", "recovery/active"])
async def test_stage_stop_does_not_resume_downstream_work(tmp_path, monkeypatch, key):
    workflow = engine(tmp_path, ApprovingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    record = {"fixture": "pending downstream operation"}
    workflow.store.checkpoint(rid, key, record)
    async def forbidden(*args, **kwargs):
        pytest.fail("Bounded research must not enter downstream recovery, approval or delivery")
    monkeypatch.setattr(workflow.recovery, "resume", forbidden)
    monkeypatch.setattr(workflow, "_ensure_prototype_approval", forbidden)
    monkeypatch.setattr(workflow.delivery, "finish", forbidden)
    result = await workflow.start(run_id=rid, stop_after="provider_research")
    assert result["status"] == "PAUSED"
    assert workflow.store.get(rid, key) == record
    if key == "delivery/journal":
        assert (await workflow._execute(rid, stop_after="provider_research"))["status"] == "PAUSED"


async def test_stage_stop_cannot_take_no_change_delivery_shortcuts(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    agent = CodingAgent()
    assert (await engine(project, agent).start(str(requirements)))["status"] == "SUCCEEDED"
    for text in ("Add two numbers", "Compute the sum of two inputs"):
        requirements.write_text(text)
        bounded = engine(project, agent)
        async def forbidden(*args, **kwargs):
            pytest.fail("Bounded research must not finalize a no-change delivery")
        monkeypatch.setattr(bounded, "_finish_no_changes", forbidden)
        paused = await bounded.start(str(requirements), stop_after="provider_research")
        assert paused["status"] == "PAUSED"
        assert paused["data"]["pause_reason"] == "requested_stage_boundary"
        assert not paused["data"].get("outcome")
        resumed = await engine(project, agent).start()
        assert resumed["status"] == "SUCCEEDED"
        assert resumed["data"]["outcome"] == "NO_CHANGES"


@pytest.mark.parametrize("stage", ["prototype", "design", "plan"])
async def test_stage_stop_preserves_downstream_pending_replan(tmp_path, monkeypatch, stage):
    workflow = engine(tmp_path, ApprovingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    pending = {"stage": stage, "reason": "fixture downstream repair"}
    workflow.store.checkpoint(rid, "replanning/pending", pending)
    async def forbidden(*args, **kwargs):
        pytest.fail("Bounded research must not process a downstream replan")
    monkeypatch.setattr(workflow, "_resume_replan", forbidden)
    monkeypatch.setattr(workflow, "_refresh_legacy_approval_context", forbidden)
    result = await workflow.start(run_id=rid, stop_after="provider_research")
    assert result["status"] == "PAUSED"
    assert workflow.store.get(rid, "replanning/pending") == pending


async def test_stage_stop_preserves_new_recovery_without_resuming_it(tmp_path, monkeypatch):
    from auto_agents_2.recovery import FailureRecord, RecoveryNeeded
    workflow = engine(tmp_path, ApprovingAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    failure = FailureRecord.create(kind="checks", component="fixture", origin_id="fixture",
                                   step="fixture", obligations=["keep original check"])
    async def failed(*args, **kwargs):
        raise RecoveryNeeded(failure)
    async def forbidden(*args, **kwargs):
        pytest.fail("Bounded research must not invoke downstream recovery diagnosis")
    monkeypatch.setattr(workflow, "_execute", failed)
    monkeypatch.setattr(workflow.recovery, "resume", forbidden)
    result = await workflow.start(run_id=rid, stop_after="provider_research")
    assert result["status"] == "PAUSED"
    assert workflow.store.get(rid, "recovery/active")["failure"]["id"] == failure.id
    assert workflow.store.get(rid, "recovery/count/" + failure.id) == 1
