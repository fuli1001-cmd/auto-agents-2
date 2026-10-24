"""Real Engine entrypoints keep prototype proposals distinct from controller capabilities."""
import copy

from test_prototype_approvals import approval_fixture
from test_prototype_recovery import prototype_fixture

from auto_agents_2.models import IntakeResult
from auto_agents_2.util import digest

ORIGINAL = "Final acceptance requires actual state JSON and real file round trips; prototype approval checks the UI."


def capture_source(workflow, rid, spec=None):
    raw = workflow.artifacts.put(ORIGINAL, "requirements")
    workflow.store.checkpoint(rid, "capture", {"artifact": raw})
    if spec is not None:
        workflow.store.checkpoint(rid, "clarify", spec)
    return raw


def assert_context(context, raw, spec):
    contract = context["controller_prototype_contract"]
    assert contract["version"] == 1
    assert "dom_json" in contract["not_collected"]
    assert "candidate_collector_execution" in contract["not_collected"]
    source = context["prototype_requirement_source"]
    assert source["status"] == "available"
    assert source["capture"] == raw
    assert source["raw_requirements"] == ORIGINAL
    assert source["specification_hash"] == digest(spec)


async def test_engine_author_and_initial_review_receive_same_actual_contract(tmp_path, monkeypatch):
    workflow, rid, _, _, _ = prototype_fixture(tmp_path, monkeypatch, empty=True)
    spec = IntakeResult(summary="UI", document="UI and future real file acceptance", frontend=True)
    raw = capture_source(workflow, rid, spec.model_dump())
    received = []
    invoke = workflow.agent.invoke

    async def record(stage, context, *args, **kwargs):
        received.append((stage, copy.deepcopy(context)))
        return await invoke(stage, context, *args, **kwargs)

    async def approve(question):
        assert question["key"] == "prototype-approval"
        return "1"

    workflow.agent.invoke = record
    workflow.question_handler = approve
    await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, ORIGINAL)
    assert [stage for stage, _ in received] == ["prototype", "review"]
    for _, context in received:
        assert_context(context, raw, spec.model_dump())


async def test_engine_cached_approval_recheck_receives_original_source(tmp_path, monkeypatch):
    workflow, rid, _, question, contexts, browsers = approval_fixture(tmp_path, monkeypatch, legacy=True)
    spec = workflow.store.get(rid, "clarify")
    raw = capture_source(workflow, rid)
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert len(browsers) == 1 and len(contexts) == 1
    assert_context(contexts[0][1], raw, spec)
    assert receipt["human"]["question_id"] == question["id"]
