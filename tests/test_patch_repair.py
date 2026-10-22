import asyncio

import pytest
from test_workflow import CodingAgent, engine

from auto_agents_2.errors import (
    InvalidStructuredResponse,
    PatchRepairBlocked,
    PolicyViolation,
    ReplacementMismatch,
)
from auto_agents_2.models import Change
from auto_agents_2.patch_repair import ReplacementCorrection
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.workspace import apply_prepared, prepare_changes


def replacement(path, old, new):
    return Change(path=path, operation="replace", old_text=old, content=new)


class Corrector(CodingAgent):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.repairs = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if not context.get("patch_repair"):
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
        assert schema is ReplacementCorrection and fresh
        self.repairs.append((stage, context, operation))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(workspace)
        return ReplacementCorrection(**result)


def setup(tmp_path, responses):
    agent = Corrector(responses)
    workflow = engine(tmp_path, agent)
    run = workflow.store.admit("fixture", workflow.config.load().model_dump(mode="json"))
    return workflow, agent, run["id"]


async def prepare(workflow, rid, work, changes, *, stage="implement", scopes=None, contracts=False, operation="fixture"):
    return await workflow.patch_repair.prepare(rid, work, changes, scopes or ["*.txt"], stage=stage,
        context={"specification": "Preserve every requirement", "acceptance": ["unchanged"]},
        operation=operation, contracts=contracts)


@pytest.mark.parametrize("stage", ["prototype", "readme", "implement"])
@pytest.mark.parametrize("mode", ["not_found", "empty"])
async def test_correction_preserves_batch_and_requires_exact_revalidation(tmp_path, stage, mode):
    prefix = {"prototype": "design/prototypes/", "readme": "docs/", "implement": ""}[stage]
    app, other = prefix + "app.txt", prefix + "other.txt"
    (tmp_path / app).parent.mkdir(parents=True, exist_ok=True)
    original = "one=old\ntwo=old\n"
    (tmp_path / app).write_text(original)
    changes = [replacement(app, "one=old", "one=middle"),
               replacement(app, {"not_found": "one = middle", "empty": ""}[mode], "done"),
               replacement(other, "before", "after")]
    (tmp_path / other).write_text("before")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "one=middle", "content": "fixed=done"}])
    repaired, patch = await prepare(workflow, rid, tmp_path, changes, stage=stage, scopes=[app, other], contracts=stage == "prototype")
    assert len(agent.repairs) == 1
    assert agent.repairs[0][0] == stage
    assert agent.repairs[0][1]["patch_repair"]["failure"]["change_index"] == 1
    assert changes[0] == repaired[0] and changes[2] == repaired[2]
    assert repaired[1].path == changes[1].path and repaired[1].operation == "replace"
    assert (tmp_path / app).read_text() == original
    assert (tmp_path / other).read_text() == "before"
    apply_prepared(tmp_path, patch[:1])
    apply_prepared(tmp_path, patch)
    assert (tmp_path / app).read_text() == "fixed=done\ntwo=old\n"
    apply_prepared(tmp_path, patch)
    assert (tmp_path / other).read_text() == "after"


async def test_ambiguous_anchor_can_expand_context_without_dropping_surroundings(tmp_path):
    (tmp_path / "app.txt").write_text("first=old\nsecond=old\n")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "second=old", "content": "second=new"}])
    repaired, patch = await prepare(workflow, rid, tmp_path, [replacement("app.txt", "old", "new")])
    assert agent.repairs[0][1]["patch_repair"]["failure"]["matches"] == 2
    apply_prepared(tmp_path, patch)
    assert (tmp_path / "app.txt").read_text() == "first=old\nsecond=new\n"


async def test_budget_survives_restart_and_repeated_retry(tmp_path):
    (tmp_path / "app.txt").write_text("actual")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "wrong again", "content": "new"}] * 2)
    changes = [replacement("app.txt", "wrong", "new")]
    with pytest.raises(PatchRepairBlocked, match="2 次上限"):
        await prepare(workflow, rid, tmp_path, changes)
    assert len(agent.repairs) == 2 and (tmp_path / "app.txt").read_text() == "actual"
    restarted = engine(tmp_path, agent)
    with pytest.raises(PatchRepairBlocked, match="2 次上限"):
        await prepare(restarted, rid, tmp_path, changes)
    assert len(agent.repairs) == 2
    with workflow.store.connect() as db:
        key = db.execute("SELECT key FROM checkpoints WHERE key LIKE 'patch-repair/%'").fetchone()[0]
    state = workflow.store.get(rid, key)
    assert state["phase"] == "blocked" and len(state["attempts"]) == 2
    assert workflow.artifacts.json(state["original"]) == [c.model_dump() for c in changes]
    assert all(workflow.artifacts.json(a["response"]) for a in state["attempts"])


@pytest.mark.parametrize("interruption", ["cancel", "transaction"])
async def test_resume_after_response_saved_does_not_call_model_twice(tmp_path, monkeypatch, interruption):
    (tmp_path / "app.txt").write_text("actual")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "actual", "content": "new"}])
    changes = [replacement("app.txt", "wrong", "new")]
    save = workflow.store.checkpoint
    interrupted = False

    def interrupt(rid, key, value):
        nonlocal interrupted
        if key.startswith("patch-repair/") and value.get("active", "absent") is None and not interrupted:
            interrupted = True
            if interruption == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("fixture checkpoint transaction failure")
        return save(rid, key, value)

    monkeypatch.setattr(workflow.store, "checkpoint", interrupt)
    with pytest.raises(asyncio.CancelledError if interruption == "cancel" else RuntimeError):
        await prepare(workflow, rid, tmp_path, changes)
    assert (tmp_path / "app.txt").read_text() == "actual"
    restarted = engine(tmp_path, agent)
    repaired, patch = await prepare(restarted, rid, tmp_path, changes)
    assert len(agent.repairs) == 1
    apply_prepared(tmp_path, patch)
    assert (tmp_path / "app.txt").read_text() == "new"


async def test_two_sequential_failures_repair_without_applying_a_prefix(tmp_path):
    (tmp_path / "app.txt").write_text("first=old\nsecond=old\n")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "first=old", "content": "first=middle"},
                                           {"old_text": "first=middle", "content": "first=new"}])
    changes = [replacement("app.txt", "first = old", "first=middle"),
               replacement("app.txt", "first = middle", "first=new")]
    repaired, patch = await prepare(workflow, rid, tmp_path, changes)
    assert [row[1]["patch_repair"]["failure"]["change_index"] for row in agent.repairs] == [0, 1]
    assert (tmp_path / "app.txt").read_text() == "first=old\nsecond=old\n"
    apply_prepared(tmp_path, patch)
    assert (tmp_path / "app.txt").read_text() == "first=new\nsecond=old\n"


async def test_new_authorized_candidate_round_does_not_reuse_old_source_identity(tmp_path):
    (tmp_path / "app.txt").write_text("first")
    workflow, agent, rid = setup(tmp_path, [{"old_text": "first", "content": "new"},
                                           {"old_text": "second", "content": "new"}])
    changes = [replacement("app.txt", "wrong", "new")]
    await prepare(workflow, rid, tmp_path, changes, operation="task/r1")
    (tmp_path / "app.txt").write_text("second")
    _, patch = await prepare(workflow, rid, tmp_path, changes, operation="task/r2")
    apply_prepared(tmp_path, patch)
    assert len(agent.repairs) == 2
    assert (tmp_path / "app.txt").read_text() == "new"


async def test_repair_never_overwrites_concurrent_user_changes(tmp_path):
    (tmp_path / "app.txt").write_text("actual")

    def drift(work):
        (work / "app.txt").write_text("user edit")
        return ReplacementCorrection(old_text="actual", content="new")

    workflow, agent, rid = setup(tmp_path, [drift])
    changes = [replacement("app.txt", "wrong", "new")]
    with pytest.raises(PatchRepairBlocked, match="Workspace changed"):
        await prepare(workflow, rid, tmp_path, changes)
    with pytest.raises(PatchRepairBlocked, match="工作区已改变"):
        await prepare(engine(tmp_path, agent), rid, tmp_path, changes)
    assert len(agent.repairs) == 1 and (tmp_path / "app.txt").read_text() == "user edit"


@pytest.mark.parametrize("kind", ["out_of_scope", "secret", "missing", "duplicate", "contract"])
async def test_nonrepairable_policy_errors_do_not_dispatch_model(tmp_path, kind):
    (tmp_path / "app.txt").write_text("actual")
    workflow, agent, rid = setup(tmp_path, [])
    changes = [replacement("app.txt", "wrong", "new")]
    scopes = ["*.txt"]
    if kind == "out_of_scope":
        scopes = ["other.txt"]
    elif kind == "secret":
        workflow.contexts.sensitive_paths = ["app.txt"]
    elif kind == "missing":
        (tmp_path / "app.txt").unlink()
    elif kind == "duplicate":
        changes = [Change(path="app.txt", content="actual"), replacement("app.txt", "actual", "new")]
    else:
        path = tmp_path / "design/prototypes/app.txt"
        path.parent.mkdir(parents=True)
        path.write_text("actual")
        changes = [replacement("design/prototypes/app.txt", "wrong", "new")]
        scopes = ["design/prototypes"]
    with pytest.raises((PolicyViolation, PatchRepairBlocked)):
        await prepare(workflow, rid, tmp_path, changes, scopes=scopes)
    assert not agent.repairs


async def test_failed_provider_responses_consume_durable_correction_budget(tmp_path):
    (tmp_path / "app.txt").write_text("actual")
    workflow, agent, rid = setup(tmp_path, [InvalidStructuredResponse("fixture")] * 2)
    changes = [replacement("app.txt", "wrong", "new")]
    for _ in range(2):
        with pytest.raises(PatchRepairBlocked, match="调用未完成"):
            await prepare(workflow, rid, tmp_path, changes)
    with pytest.raises(PatchRepairBlocked, match="2 次上限"):
        await prepare(engine(tmp_path, agent), rid, tmp_path, changes)
    assert len(agent.repairs) == 2


def test_mismatch_carries_sequential_index_and_count(tmp_path):
    (tmp_path / "app.txt").write_text("old")
    with pytest.raises(ReplacementMismatch) as error:
        prepare_changes(tmp_path, [replacement("app.txt", "old", "twice twice"),
                                  replacement("app.txt", "twice", "new")], ["app.txt"])
    assert error.value.details == {"path": "app.txt", "change_index": 1, "matches": 2, "reason": "ambiguous"}
    assert (tmp_path / "app.txt").read_text() == "old"


def test_correction_prompt_uses_stage_profile_but_narrow_output_contract():
    prompt, family = PromptCompiler().compile("prototype", {"patch_repair": {"failure": {"path": "app.txt"}}})
    assert 'stage="prototype"' in prompt and "ReplacementCorrection schema" in prompt
    assert "copy it exactly, including whitespace" in prompt
