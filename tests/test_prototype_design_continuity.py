"""Required synchronization survives applied proposals, not arbitrary history."""
import copy

import pytest
from test_decisions import ApprovingAgent
from test_visual_design import PAGE, setup
from visual_design_fixtures import visual_brief

from auto_agents_2.engine import Replan
from auto_agents_2.models import Change, DocumentResult
from auto_agents_2.prototype_design import PrototypeDesign
from auto_agents_2.util import digest
from auto_agents_2.workspace import git


class BrowserReached(Exception):
    pass


async def applied_fixture(tmp_path, *, default_scope=False):
    workflow, rid, spec, service, source = setup(tmp_path)
    spec.visual_brief = visual_brief(update="required")
    spec.prototype_paths = [] if default_scope else [PAGE, "DESIGN.md"]
    workflow.store.checkpoint(rid, "clarify", spec.model_dump())
    service.bind_brief(rid, spec.model_dump(), source)
    (tmp_path / "DESIGN.md").write_text("Original design")
    git(tmp_path, "add", "DESIGN.md")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
    workflow.store.checkpoint(rid, "baseline", git(tmp_path, "rev-parse", "HEAD"))
    proposal = DocumentResult(summary="Applied UI", document="Complete UI obligations", files=[
        Change(path="DESIGN.md", operation="replace", old_text="Original design", content="Synchronized design"),
        Change(path=PAGE, content="<h1>Original UI</h1>")])

    class Agent(ApprovingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "prototype":
                return proposal
            return await super().invoke(stage, *args, **kwargs)
    workflow.agent = Agent()

    async def browser(*args):
        return {"error": "Download observation failed", "results": []}
    workflow._prototype_browser_report = browser
    with pytest.raises(Replan, match="Download observation failed") as failure:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    pending = workflow.store.get(rid, "prototype/pending")
    assert pending["design_application"] and "review" not in pending
    assert (tmp_path / "DESIGN.md").read_text() == "Synchronized design"
    workflow._controller_replan(rid, failure.value)
    await workflow._resume_replan(rid)
    proposal = proposal.model_copy(update={"files": [
        Change(path=PAGE, operation="replace", old_text="Original UI", content="Repaired UI")]})
    async def reached(*args):
        raise BrowserReached()
    workflow._prototype_browser_report = reached
    return workflow, rid, spec


@pytest.mark.parametrize("default_scope", [False, True])
async def test_applied_design_browser_failure_allows_current_html_only(tmp_path, default_scope):
    workflow, rid, spec = await applied_fixture(tmp_path, default_scope=default_scope)
    original = workflow.store.get(rid, "recovery/gate-history/0")
    assert PrototypeDesign(workflow).synchronized(rid, tmp_path, spec.model_dump())
    with pytest.raises(BrowserReached):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert (tmp_path / PAGE).read_text() == "<h1>Repaired UI</h1>"
    assert (tmp_path / "DESIGN.md").read_text() == "Synchronized design"
    assert workflow.store.get(rid, "recovery/gate-history/0") == original
    assert workflow.store.get(rid, "prototype/pending")["prepared"][0]["path"] == PAGE
    assert workflow.store.get(rid, "prototype_approval") is None
    assert not workflow.store.pending(rid)
    assert workflow.store.run(rid)["data"]["replans"] == 1


@pytest.mark.parametrize("damage", [
    "changed_design", "missing_design", "brief", "specification", "missing_application",
    "application_artifact", "prepared", "fingerprint", "unapplied", "permit", "author_artifact",
    "missing_history", "legacy", "scope",
])
async def test_omission_fails_closed_without_same_bound_application(tmp_path, monkeypatch, damage):
    workflow, rid, spec = await applied_fixture(tmp_path)
    key = "recovery/gate-history/0"
    history = workflow.store.get(rid, key)
    pending = history["checkpoints"]["prototype/pending"]
    if damage == "changed_design":
        (tmp_path / "DESIGN.md").write_text("Unreviewed change")
    elif damage == "missing_design":
        (tmp_path / "DESIGN.md").unlink()
    elif damage == "brief":
        brief = workflow.store.get(rid, "ui_design/brief")
        brief["source_binding"] = {"changed": True}
        workflow.store.checkpoint(rid, "ui_design/brief", brief)
    elif damage == "specification":
        spec.document = "Changed accepted requirements"
        workflow.store.checkpoint(rid, "clarify", spec.model_dump())
    elif damage in {"missing_application", "legacy"}:
        pending.pop("design_application")
    elif damage == "application_artifact":
        ref = pending["design_application"]
        (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt application")
    elif damage == "prepared":
        pending["prepared"][0]["content"] = "Tampered proposal"
        pending["prepared"][0]["after"] = digest("Tampered proposal")
    elif damage == "fingerprint":
        pending["fingerprint"] = "tampered"
    elif damage == "unapplied":
        pending.pop("fingerprint")
    elif damage == "permit":
        permit_key = history["pending"]["permit"]
        permit = workflow.store.get(rid, permit_key)
        permit["phase"] = "started"
        get = workflow.store.get
        monkeypatch.setattr(workflow.store, "get", lambda run, key, default=None:
                            permit if key == permit_key else get(run, key, default))
    elif damage == "author_artifact":
        ref = history["checkpoints"]["invocation/prototype"]["artifact"]
        (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("corrupt author")
    elif damage == "missing_history":
        history = {}
    else:
        spec.prototype_paths = [PAGE]
        workflow.store.checkpoint(rid, "clarify", spec.model_dump())
    workflow.store.checkpoint(rid, key, history)
    before = (tmp_path / PAGE).read_bytes()
    # Corrupt immutable artifacts are integrity failures, not repair requests.
    from auto_agents_2.faults import StateConflict
    with pytest.raises((Replan, ValueError, StateConflict)):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert (tmp_path / PAGE).read_bytes() == before
    assert workflow.store.get(rid, "prototype_approval") is None
    assert workflow.store.run(rid)["data"]["replans"] == 1


async def test_initial_omission_rejects_existing_design(tmp_path):
    workflow, rid, spec, service, source = setup(tmp_path)
    spec.visual_brief = visual_brief(update="required")
    spec.prototype_paths = [PAGE, "DESIGN.md"]
    (tmp_path / "DESIGN.md").write_text("Existing file alone is not synchronization")
    git(tmp_path, "add", "DESIGN.md")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
    workflow.store.checkpoints(rid, {"clarify": spec.model_dump(), "baseline": git(tmp_path, "rev-parse", "HEAD")})
    service.bind_brief(rid, spec.model_dump(), source)
    class Agent(ApprovingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "prototype":
                return DocumentResult(summary="UI", document="Claims design is synchronized")
            return await super().invoke(stage, *args, **kwargs)
    workflow.agent = Agent()
    with pytest.raises(Replan, match="Synchronize DESIGN.md"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")


async def test_later_html_only_reentry_keeps_original_application(tmp_path):
    workflow, rid, spec = await applied_fixture(tmp_path)
    for _ in range(2):
        # Re-entry after a no-write proposal/browser failure keeps application
        # provenance in its original generation, not a copied approval.
        class Agent(ApprovingAgent):
            async def invoke(self, stage, *args, **kwargs):
                if stage == "prototype":
                    return DocumentResult(summary="UI", document="Complete obligations")
                return await super().invoke(stage, *args, **kwargs)
        workflow.agent = Agent()
        async def browser(*args):
            return {"error": "Still needs observation", "results": []}
        workflow._prototype_browser_report = browser
        with pytest.raises(Replan, match="Still needs observation") as failure:
            await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
        workflow._controller_replan(rid, failure.value)
        await workflow._resume_replan(rid)
    assert PrototypeDesign(workflow).synchronized(rid, tmp_path, spec.model_dump())
    history = copy.deepcopy(workflow.store.get(rid, "recovery/gate-history/1"))
    history["pending"]["granted"] = False
    workflow.store.checkpoint(rid, "recovery/gate-history/1", history)
    assert not PrototypeDesign(workflow).synchronized(rid, tmp_path, spec.model_dump())


@pytest.mark.parametrize("damage", ["changed", "missing", "brief"])
async def test_saved_html_preparation_revalidates_design_before_application(tmp_path, monkeypatch, damage):
    workflow, rid, spec = await applied_fixture(tmp_path)
    checkpoint = workflow.store.checkpoint
    class PreparedPause(Exception):
        pass
    def pause(run, key, value):
        checkpoint(run, key, value)
        if key == "prototype/pending" and "prepared" in value and "fingerprint" not in value:
            raise PreparedPause()
    monkeypatch.setattr(workflow.store, "checkpoint", pause)
    with pytest.raises(PreparedPause):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    monkeypatch.setattr(workflow.store, "checkpoint", checkpoint)
    if damage == "changed":
        (tmp_path / "DESIGN.md").write_text("Concurrent changed design")
    elif damage == "missing":
        (tmp_path / "DESIGN.md").unlink()
    else:
        brief = workflow.store.get(rid, "ui_design/brief")
        brief["source_binding"] = {"changed": True}
        checkpoint(rid, "ui_design/brief", brief)
    before = (tmp_path / PAGE).read_bytes()
    with pytest.raises(Replan, match="Synchronize DESIGN.md"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert (tmp_path / PAGE).read_bytes() == before
    assert "fingerprint" not in workflow.store.get(rid, "prototype/pending")
