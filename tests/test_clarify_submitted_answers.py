"""Submitted requirement answers remain authoritative input after upstream replans."""
import copy

import pytest
from test_plan_lineage import pending_run
from test_workflow import CodingAgent

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import IntakeResult


class ReachedCoverage(Exception):
    pass


@pytest.mark.parametrize("answer_key", ["prototype-feedback", "design/0", "clarify/9/0"])
async def test_replanned_clarify_and_coverage_receive_complete_bound_answers(tmp_path, answer_key):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage in {"clarify", "coverage_review"}:
                assert context["submitted_answers"] == expected
                assert context["submitted_answers"][0]["prompt"] == "Original question"
                assert context["submitted_answers"][0]["artifact_hash"] == "original-binding"
                seen.append(stage)
            if stage == "coverage_review":
                source = context["controller_review_basis"]["sources"]["answer:" + original["id"]]
                assert source["text"] == answer
                raise ReachedCoverage()
            return await super().invoke(stage, context, *args, **kwargs)

    workflow, rid = pending_run(tmp_path, Agent())
    answer = "Use the new creation flow.\nKeep the existing verification gates.\n" + "Full detail. " * 1200
    original = workflow.store.request(rid, answer_key, "Original question", "original-binding")
    workflow.store.answer(original["id"], answer)
    for key in ("prototype-approval", "provider/retry", "recovery-cycle/1"):
        row = workflow.store.request(rid, key, "Operational permission", "other-binding")
        workflow.store.answer(row["id"], "yes", "other-binding")
    obsolete = workflow.store.request(rid, "clarify/8/0", "Obsolete", "old-binding")
    workflow.store.answer(obsolete["id"], "OBSOLETE")
    workflow.store.supersede_questions(rid, "clarify/8/0", include_answered=True)
    expected = workflow.store.submitted_answers(rid)
    assert len(expected) == 1
    workflow.store.checkpoint(rid, "clarify_context", {
        "submitted_answers": [{"answer": "STALE SNAPSHOT"}],
        "decisions": [[{"question": "Technical choice", "answer": "automatic inference"}]],
    })
    workflow.store.checkpoint(rid, "replan_feedback", {"stage": "clarify", "reason": "Summary only"})
    before = copy.deepcopy(workflow.store.question(original["id"]))
    seen = []
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    assert seen == ["clarify", "coverage_review"]
    assert workflow.store.question(original["id"]) == before


async def test_each_clarification_turn_reloads_newly_submitted_answer(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "clarify":
                seen.append(copy.deepcopy(context["submitted_answers"]))
                if len(seen) == 1:
                    return IntakeResult(summary="Need a product choice", document="", questions=["Choose flow"])
                assert seen[-1] == workflow.store.submitted_answers(rid)
                assert seen[-1][0]["answer"] == "Selected flow"
            if stage == "coverage_review":
                assert context["submitted_answers"] == seen[-1]
                raise ReachedCoverage()
            return await super().invoke(stage, context, *args, **kwargs)

    workflow, rid = pending_run(tmp_path, Agent())
    async def submitted_response(run_id, key, questions, fingerprint):
        row = workflow.store.request(run_id, key + "/0", "Choose flow", fingerprint)
        workflow.store.answer(row["id"], "Selected flow")
        return [{"question": "Choose flow", "answer": "Selected flow"}]
    workflow._questions = submitted_response
    seen = []
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    assert len(seen) == 2 and seen[0] == []


@pytest.mark.parametrize("stage", ["clarify", "coverage_review"])
def test_presentation_preserves_full_answer_and_binding_over_inline_limit(tmp_path, stage):
    workflow, rid = pending_run(tmp_path, CodingAgent())
    raw = "Complete external requirement\n" * 1500
    row = workflow.store.request(rid, "prototype-feedback", "Question is context only", "bound-version")
    workflow.store.answer(row["id"], raw)
    expected = workflow.store.submitted_answers(rid)
    context = {"submitted_answers": expected, "project": {}}
    view = ContextPlanner(workflow.artifacts).prepare(stage, context, workflow.project, run_id=rid)
    assert view.payload["submitted_answers"] == expected
    assert view.payload["submitted_answers"][0]["raw_answer"] == raw
    assert context == {"submitted_answers": expected, "project": {}}

async def test_new_answer_invalidates_cached_clarify_without_resetting_usage(tmp_path):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "clarify":
                seen.append(copy.deepcopy(context["submitted_answers"]))
            if stage == "coverage_review":
                raise ReachedCoverage()
            return await super().invoke(stage, context, *args, **kwargs)

    workflow, rid = pending_run(tmp_path, Agent())
    workflow.store.checkpoint(rid, "proposal-recovery/count/proposal/retained", 3)
    seen = []
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    previous = workflow.store.get(rid, "invocation/clarify")
    row = workflow.store.request(rid, "prototype-feedback", "Original feedback question", "prototype-binding")
    workflow.store.answer(row["id"], "Apply the revised creation flow")
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    current = workflow.store.get(rid, "invocation/clarify")
    assert len(seen) == 2 and seen[0] == []
    assert seen[1][0]["id"] == row["id"]
    assert current["identity"] != previous["identity"]
    assert workflow.artifacts.read(previous["artifact"])
    assert workflow.store.get(rid, "proposal-recovery/count/proposal/retained") == 3
