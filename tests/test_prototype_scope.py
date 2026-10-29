import pytest
from test_decisions import ApprovingAgent
from test_workflow import engine

from auto_agents_2.engine import PROTOTYPE_BROWSER_VERSION, Replan
from auto_agents_2.errors import PolicyViolation
from auto_agents_2.models import Change, DocumentResult, IntakeResult
from auto_agents_2.util import digest
from auto_agents_2.workspace import apply_prepared, git, prepare_changes


PAGE = "design/prototypes/task/index.html"
DEFAULT_SCOPE = ["design/prototypes", "DESIGN.md"]


def candidate(tmp_path, *, paths, changes, prepared=False):
    workflow = engine(tmp_path, ApprovingAgent())
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-m", "baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    rid = workflow.store.admit("prototype scope", {})["id"]
    spec = IntakeResult(summary="UI", document="# UI", frontend=True, prototype_paths=paths)
    proposal = DocumentResult(summary="update prototype", document="# Prototype", files=changes)
    # An initial iteration may already contain an earlier, uncommitted prototype.
    for change in changes:
        path = tmp_path / change.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("prior " + change.path)
    pending = {"identity": digest([spec.model_dump(), baseline, None]),
               "document": proposal.model_dump(), "design_candidates": [],
               "question_refresh_version": PROTOTYPE_BROWSER_VERSION}
    if prepared:
        pending.update(retrieved=[], prepared=prepare_changes(
            tmp_path, changes, DEFAULT_SCOPE, protected=False, contracts=True))
    workflow.store.checkpoints(rid, {"baseline": baseline, "clarify": spec.model_dump(),
                                     "prototype/pending": pending})
    return workflow, rid, spec, pending


@pytest.mark.parametrize("outside", ["DESIGN.md", "design/prototypes/other/index.html"])
async def test_initial_explicit_scope_rejects_whole_batch_before_preparation(tmp_path, outside):
    changes = [Change(path=PAGE, content="new page"), Change(path=outside, content="out of scope")]
    workflow, rid, spec, pending = candidate(tmp_path, paths=[PAGE], changes=changes)

    with pytest.raises(Replan, match="Out-of-scope") as caught:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")

    assert caught.value.stage == "prototype"
    assert workflow.store.get(rid, "prototype/pending") == pending
    assert not workflow.store.get(rid, "replanning/evidence/contracts")["criteria"]["prototype_scope"]
    assert all((tmp_path / c.path).read_text() == "prior " + c.path for c in changes)
    assert not workflow.store.pending(rid) and not workflow.agent.calls


async def test_saved_out_of_scope_candidate_returns_through_normal_recovery_and_retains_history(tmp_path):
    changes = [Change(path=PAGE, content="new page"), Change(path="DESIGN.md", content="out of scope")]
    workflow, rid, spec, pending = candidate(tmp_path, paths=[PAGE], changes=changes, prepared=True)
    counts = {"round": 3, "stalled": 1, "failure": "retained"}
    workflow.store.checkpoint(rid, "rounds/T1", counts)
    feedback = workflow.store.request(rid, "prototype-feedback", "Requested revision", digest(pending))
    workflow.store.answer(feedback["id"], "Keep title details")

    with pytest.raises(Replan, match="Out-of-scope") as caught:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")

    assert caught.value.stage == "prototype"
    assert workflow.store.get(rid, "prototype/pending") == pending
    assert not workflow.store.get(rid, "replanning/evidence/contracts")["criteria"]["prototype_scope"]
    assert not workflow.store.pending(rid)
    workflow._controller_replan(rid, caught.value)
    await workflow._resume_replan(rid)

    archive = workflow.store.get(rid, "recovery/gate-history/0")
    assert archive["checkpoints"]["prototype/pending"] == pending
    assert archive["pending"]["granted"] and archive["pending"]["permit"]
    assert workflow.store.get(rid, "prototype/pending") is None
    assert workflow.store.get(rid, "clarify") == spec.model_dump()
    assert workflow.store.get(rid, "rounds/T1") == counts
    assert workflow.store.run(rid)["data"]["replans"] == 1
    assert workflow.store.question(feedback["id"])["answer"] == "Keep title details"
    assert workflow.store.get(rid, "prototype_approval") is None
    assert all((tmp_path / c.path).read_text() == "prior " + c.path for c in changes)


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("prepared", [False, True])
async def test_allowed_initial_candidate_reaches_browser_without_approval(tmp_path, explicit, prepared):
    changes = [Change(path=PAGE, content="new page"), Change(path="DESIGN.md", content="new design")]
    paths = [PAGE, "DESIGN.md"] if explicit else []
    workflow, rid, spec, _ = candidate(tmp_path, paths=paths, changes=changes, prepared=prepared)

    class BrowserReached(Exception):
        pass

    async def browser(*args):
        assert all((tmp_path / c.path).read_text() == c.content for c in changes)
        raise BrowserReached()

    workflow._prototype_browser_report = browser
    with pytest.raises(BrowserReached):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")

    assert "fingerprint" in workflow.store.get(rid, "prototype/pending")
    assert workflow.store.get(rid, "replanning/evidence/contracts")["criteria"]["prototype_scope"]
    assert workflow.store.get(rid, "prototype_approval") is None
    assert not workflow.store.pending(rid) and not workflow.agent.calls


async def test_existing_baseline_still_requires_explicit_scope(tmp_path):
    workflow, rid, spec, pending = candidate(
        tmp_path, paths=[], changes=[Change(path=PAGE, content="new page")])
    git(tmp_path, "add", "design/prototypes")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "existing prototype")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    pending["identity"] = digest([spec.model_dump(), baseline, None])
    workflow.store.checkpoints(rid, {"baseline": baseline, "prototype/pending": pending})

    with pytest.raises(Replan, match="explicit prototype_paths") as caught:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")

    assert caught.value.stage == "clarify"
    assert workflow.store.get(rid, "prototype/pending") == pending
    assert (tmp_path / PAGE).read_text() == "prior " + PAGE
    assert not workflow.store.pending(rid) and not workflow.agent.calls


@pytest.mark.parametrize("drift", ["content", "mode"])
@pytest.mark.parametrize("outside", [False, True])
async def test_saved_candidate_keeps_real_file_drift_blocked(tmp_path, drift, outside):
    changes = [Change(path=PAGE, content="new page"), Change(path="DESIGN.md", content="new design")]
    workflow, rid, spec, pending = candidate(
        tmp_path, paths=[PAGE] if outside else [PAGE, "DESIGN.md"], changes=changes, prepared=True)
    # An interrupted application must not allow the remaining file to be overwritten.
    apply_prepared(tmp_path, pending["prepared"][:1])
    design = tmp_path / "DESIGN.md"
    if drift == "content":
        design.write_text("user changed design")
    else:
        design.chmod(0o600)

    with pytest.raises(PolicyViolation, match="changed after candidate preparation"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")

    assert (tmp_path / PAGE).read_text() == "new page"
    assert design.read_text() == ("user changed design" if drift == "content" else "prior DESIGN.md")
    assert design.stat().st_mode & 0o777 == (0o644 if drift == "content" else 0o600)
    assert workflow.store.get(rid, "prototype/pending") == pending
    assert workflow.store.get(rid, "prototype_approval") is None
    assert not workflow.store.pending(rid) and not workflow.agent.calls
