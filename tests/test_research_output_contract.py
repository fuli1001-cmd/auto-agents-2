"""Research publishes one complete document and source set, never unapplied files."""
import copy

import pytest
from authority_review_fixtures import authority_review, seed_original
from test_decisions import ApprovingAgent, setup
from test_plan_lineage import pending_run

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.decisions import DecisionResult
from auto_agents_2.engine import Replan
from auto_agents_2.errors import InvalidStructuredResponse
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.interaction import choice_spec
from auto_agents_2.models import Change, DocumentResult, ReviewResult, SourceEvidence, UserQuestion
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.research import ResearchService
from auto_agents_2.research_contract import RESEARCH_PUBLICATION_RULE
from auto_agents_2.util import digest

URLS = [f"https://docs.example.test/provider/{index}/contract" for index in range(7)]


def complete():
    return DocumentResult(summary="Complete external service research",
        document="# Complete research\nService A and service B retain their documented interfaces. "
                 "The corrected multi-image limit is documented, not a live-account result.",
        sources=[SourceEvidence(url=url, claim="Documented interface; account behavior remains unverified")
                 for url in URLS])


def supplement():
    return DocumentResult(summary="Supplement only", document="Combine this with the previous research.",
        sources=[SourceEvidence(url="https://docs.example.test/retired-only", claim="Rejected historical claim")],
        files=[Change(path="docs/aa2/provider_research_correction.md", content="Required correction body")])


async def test_author_and_presentation_receive_latest_complete_answers_even_with_cached_input(tmp_path):
    observed = []

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
            if stage == "provider_research":
                expected = workflow.store.submitted_answers(rid)
                assert context["submitted_answers"] == expected
                assert context["provider_research_answer"] == [{"question": "Same-stage question", "answer": "Keep context"}]
                view = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=rid)
                assert view.payload["submitted_answers"] == expected
                assert all(url in view.payload["submitted_answers"][-1]["raw_answer"] for url in URLS)
                rendered, _ = PromptCompiler().compile(stage, view.payload)
                assert RESEARCH_PUBLICATION_RULE in rendered
                observed.append(copy.deepcopy(expected))
                return complete()
            return await super().invoke(stage, context, schema, work, rid, operation, fresh)

    workflow, rid = setup(tmp_path, Agent())
    first = workflow.store.request(rid, "provider_research/0", "Use supplied primary materials", "original-question")
    answer = "\n".join(URLS) + "\n" + "Complete bound supplier material. " * 1800
    workflow.store.answer(first["id"], answer)
    context = {"submitted_answers": [{"answer": "stale snapshot"}],
               "provider_research_answer": [{"question": "Same-stage question", "answer": "Keep context"}]}
    await workflow._invoke("provider_research", context, DocumentResult, tmp_path, rid, "provider_research")
    saved = workflow.store.get(rid, "invocation/provider_research")
    second = workflow.store.request(rid, "clarify/0/0", "Additional source qualification", "new-binding")
    workflow.store.answer(second["id"], answer + "\nPreserve documented-versus-live distinctions.")
    await workflow._invoke("provider_research", context, DocumentResult, tmp_path, rid, "provider_research")
    assert len(observed) == 2 and len(observed[0]) == 1 and len(observed[1]) == 2
    assert workflow.store.get(rid, "invocation/provider_research")["identity"] != saved["identity"]
    assert context["submitted_answers"] == [{"answer": "stale snapshot"}]


async def test_new_research_files_are_rejected_before_invocation_publication(tmp_path):
    class Agent(ApprovingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "provider_research":
                return supplement()
            return await super().invoke(stage, *args, **kwargs)

    workflow, rid = setup(tmp_path, Agent())
    with pytest.raises(InvalidStructuredResponse, match="files"):
        await workflow._invoke_once("provider_research", {}, DocumentResult, tmp_path, rid,
                                    "provider_research", interactive_retry=False)
    assert workflow.store.get(rid, "invocation/provider_research") is None
    assert not (tmp_path / supplement().files[0].path).exists()


class ReachedDesign(Exception):
    pass


@pytest.mark.parametrize("location", ["candidate", "accepted"])
async def test_old_files_replan_normally_before_capture_and_preserve_history_and_approval(tmp_path, monkeypatch, location):
    seen = []

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
            if stage == "provider_research":
                seen.append(("author", operation))
                return complete()
            if context.get("review_kind") == "research":
                assert context["research"] == complete().model_dump()
                assert context["controller_research_output"]["mode"] == "complete_replacement"
                basis = context["controller_review_basis"]
                assert RESEARCH_PUBLICATION_RULE in basis["criteria"][0]["statement"]
                view = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=rid)
                assert view.payload["research"] == complete().model_dump()
                assert [row["url"] for row in view.payload["retrieved_sources"]] == URLS
                seen.append(("review", operation))
                return authority_review(context)
            if stage == "design":
                research = context["provider_research"]
                assert research["document"] == complete().document
                assert [row["url"] for row in research["sources"]] == URLS
                assert len(research["verified_sources"]) == len(URLS)
                assert (work / "docs/aa2/provider_research.md").read_text() == complete().document
                for downstream in ("design", "plan"):
                    view = ContextPlanner(workflow.artifacts).prepare(downstream, context, work, run_id=rid)
                    assert [row["url"] for row in view.payload["provider_research"]["sources"]] == URLS
                    assert view.payload["provider_research"]["document"] == complete().document
                    rows = view.payload["provider_research"]["verified_sources"]
                    assert len(rows) == len(research["verified_sources"])
                    for index, (presented, original) in enumerate(zip(rows, research["verified_sources"])):
                        # The existing downstream view keeps claims and an exact original reference,
                        # while acquisition metadata remains in the controller's retained record.
                        assert presented == {k: v for k, v in original.items() if k != "retrieval"} | {
                            "source_ref": f"provider_research.verified_sources[{index}]"}
                        assert controller_context_catalog(context).resolve(presented["source_ref"]) == f"provider_research.verified_sources[{index}]"
                        assert workflow.artifacts.bytes(original["retrieval"]["artifact"]) == b"Original complete primary-source body"
                raise ReachedDesign()
            return await super().invoke(stage, context, schema, work, rid, operation, fresh)

    workflow, rid = pending_run(tmp_path, Agent())
    store = workflow.store
    seed_original(workflow, rid)
    raw = workflow.artifacts.put("Original complete primary-source body", "research_raw")
    bad = supplement().model_dump()
    historical = {**bad, "verified_sources": [{"url": bad["sources"][0]["url"], "retrieval": {"artifact": raw}}]}
    key = "candidate/provider_research" if location == "candidate" else "provider_research"
    value = {"document": bad} if location == "candidate" else historical
    store.checkpoint(rid, key, value)
    # Preserve a genuine controller-bound older review/acceptance, not a fake new approval.
    review_context, _ = workflow.authority.prepare(rid, "review",
        {"review_kind": "research", "research": bad, "retrieved_sources": []},
        ReviewResult, workflow.project, "old-research-review")
    old_review = authority_review(review_context)
    workflow.authority.record(rid, old_review, review_context, "old-research-review")
    if location == "accepted":
        store.checkpoints(rid, workflow.authority.acceptance(rid, "provider_research", historical, old_review))
    approval = store.request(rid, "prototype-approval", "Original displayed prototype", "approved-content",
                             choice_spec(["Approve", "Reject"], ["approve", "reject"]))
    store.answer(approval["id"], "1", "approved-content")
    store.checkpoints(rid, {"prototype_approval": {"identity": "approved-content", "human_question": approval["id"]},
                           "rounds/retained": {"round": 8}, "proposal-recovery/count/retained": 3})
    protected = {k: store.get(rid, k) for k in (
        "prototype_approval", "rounds/retained", "proposal-recovery/count/retained")}
    old_question = store.question(approval["id"])

    async def capture_sources(service, sources):
        seen.append(("capture", [s.url for s in sources]))
        assert [s.url for s in sources] == URLS, "A partial files proposal must not reach acquisition"
        return [{**source.model_dump(), "retrieval": {"artifact": raw}, "excerpt": "Fixture captured body"}
                for source in sources]
    monkeypatch.setattr(ResearchService, "verify", capture_sources)

    with pytest.raises(Replan) as rejected:
        await workflow._execute(rid)
    assert rejected.value.stage == "provider_research"
    assert "files" in rejected.value.reason
    assert not seen
    assert store.get(rid, key) == value
    assert store.question(approval["id"]) == old_question
    assert not (store.root / "worktrees" / rid / "integration" / bad["files"][0]["path"]).exists()

    workflow._controller_replan(rid, rejected.value)
    await workflow._resume_replan(rid)
    history = store.get(rid, "recovery/gate-history/0")
    assert history["checkpoints"][key] == value
    assert store.run(rid)["data"]["replans"] == 1
    assert workflow.artifacts.bytes(raw) == b"Original complete primary-source body"
    assert all(store.get(rid, k) == v for k, v in protected.items())
    assert store.question(approval["id"]) == old_question
    assert store.get(rid, "authority/reviews/" + digest(old_review.model_dump()))["review"] == old_review.model_dump()

    with pytest.raises(ReachedDesign):
        await workflow._execute(rid)
    accepted = store.get(rid, "provider_research")
    assert accepted["files"] == [] and [row["url"] for row in accepted["sources"]] == URLS
    assert workflow.authority.accepted(rid, "provider_research", accepted)
    assert all(store.get(rid, k) == v for k, v in protected.items())
    assert store.question(approval["id"]) == old_question


async def test_files_format_correction_uses_existing_budgeted_proposal_recovery(tmp_path):
    class Agent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.authors = 0

        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "provider_research":
                self.authors += 1
                if self.authors == 1:
                    return supplement()
                assert context["validation_feedback"]["code"] == "invalid_structured_response"
                return complete()
            return await super().invoke(stage, context, schema, *args, **kwargs)

    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    workflow.store.checkpoint(rid, "rounds/T1", {"round": 8})
    count = "proposal-recovery/count/proposal/" + digest(["provider_research", "provider_research"])
    workflow.store.checkpoint(rid, count, 3)
    result = await workflow._invoke("provider_research", {}, DocumentResult, tmp_path, rid, "provider_research")
    assert result == complete() and agent.authors == 2
    assert [stage for stage, _ in agent.calls].count("decision") == 1
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert not workflow.store.pending(rid)
    again = await workflow._invoke("provider_research", {}, DocumentResult, tmp_path, rid, "provider_research")
    assert again == result and agent.authors == 2
    assert workflow.store.get(rid, count) == 4


@pytest.mark.parametrize("files", [None, []])
async def test_legacy_source_free_research_without_files_stays_compatible(tmp_path, files):
    class Agent(ApprovingAgent):
        async def invoke(self, stage, *args, **kwargs):
            if stage == "provider_research" or stage == "review":
                pytest.fail("Valid source-free cache must retain its existing shortcut")
            if stage == "design":
                raise ReachedDesign()
            return await super().invoke(stage, *args, **kwargs)

    workflow, rid = pending_run(tmp_path, Agent())
    data = DocumentResult(summary="No services needed", document="No external providers required.").model_dump()
    if files is None:
        data.pop("files")
    workflow.store.checkpoint(rid, "provider_research", data)
    with pytest.raises(ReachedDesign):
        await workflow._execute(rid)
    assert workflow.store.get(rid, "provider_research") == data


@pytest.mark.parametrize("approved", [False, True])
async def test_same_stage_answer_and_final_publication_reach_independent_review(tmp_path, monkeypatch, approved):
    answer = "\n".join(URLS) + "\n" + "Complete supplemental source text; documented, not live tested. " * 250
    prompt = "Which supplied source set and account-test boundary should this report use?"
    authors = []
    reviews = []

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
            if stage == "decision" and context["decision_request"]["kind"] == "technical_clarification":
                return DecisionResult(action="ask_user", reason="The requested source choice is external input")
            if stage == "provider_research":
                authors.append(copy.deepcopy(context))
                if len(authors) == 1:
                    return DocumentResult(summary="Source choice required", document="Pending source qualification",
                                          questions=[UserQuestion(prompt=prompt)])
                expected = workflow.store.submitted_answers(rid)
                assert len(expected) == 1 and expected[0]["raw_answer"] == answer
                assert context["submitted_answers"] == expected
                assert context["provider_research_answer"] == [{"question": prompt, "answer": answer}]
                view = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=rid)
                assert view.payload["submitted_answers"] == expected
                return complete()
            if context.get("review_kind") == "research":
                assert context["research"] == complete().model_dump()
                basis = context["controller_review_basis"]
                bound = workflow.store.submitted_answers(rid)[0]
                source = basis["sources"]["answer:" + bound["id"]]
                assert source["answer_record"] == bound and source["binding"] == bound["artifact_hash"]
                assert source["text"] == answer and source["question_context"] == prompt
                view = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=rid)
                assert view.payload["controller_review_basis"]["sources"]["answer:" + bound["id"]] == source
                assert view.payload["research"] == complete().model_dump()
                rendered, _ = PromptCompiler().compile(stage, view.payload)
                assert RESEARCH_PUBLICATION_RULE in rendered
                reviews.append(copy.deepcopy(context))
                return authority_review(context, approved=approved, summary="Independent complete-report judgment")
            if stage == "design":
                assert approved
                assert context["provider_research"]["document"] == complete().document
                raise ReachedDesign()
            return await super().invoke(stage, context, schema, work, rid, operation, fresh)

    workflow, rid = pending_run(tmp_path, Agent())
    async def respond(question):
        assert question["key"] == "provider_research/0" and question["prompt"] == prompt
        return answer
    workflow.question_handler = respond
    async def acquire(service, sources):
        assert [source.url for source in sources] == URLS
        return [source.model_dump() for source in sources]
    monkeypatch.setattr(ResearchService, "verify", acquire)
    with pytest.raises(ReachedDesign if approved else Replan) as stopped:
        await workflow._execute(rid)
    assert len(authors) == 2 and len(reviews) == 1
    path = workflow.store.root / "worktrees" / rid / "integration/docs/aa2/provider_research.md"
    if approved:
        assert path.read_text() == reviews[0]["research"]["document"]
        assert workflow.store.get(rid, "provider_research")["sources"] == reviews[0]["research"]["sources"]
    else:
        assert stopped.value.stage == "provider_research"
        assert not path.exists() and workflow.store.get(rid, "provider_research") is None


async def test_format_retry_keeps_answers_frozen_until_next_logical_call(tmp_path):
    observed = []
    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "provider_research":
                observed.append(copy.deepcopy(context["submitted_answers"]))
                if len(observed) == 1:
                    question = workflow.store.request(rid, "clarify/1/0", "New external detail", "new-source")
                    workflow.store.answer(question["id"], "New detail arriving during the frozen format retry")
                    return supplement()
                return complete()
            return await super().invoke(stage, context, schema, *args, **kwargs)

    workflow, rid = setup(tmp_path, Agent())
    await workflow._invoke("provider_research", {}, DocumentResult, tmp_path, rid, "provider_research")
    assert observed == [[], []]
    expected = workflow.store.submitted_answers(rid)
    await workflow._invoke("provider_research", {}, DocumentResult, tmp_path, rid, "provider_research")
    assert observed == [[], [], expected]
    count = "proposal-recovery/count/proposal/" + digest(["provider_research", "provider_research"])
    assert workflow.store.get(rid, count) == 1


@pytest.mark.parametrize("stage", ["prototype", "design", "readme"])
async def test_file_proposals_of_other_document_stages_remain_valid(tmp_path, stage):
    class Agent(ApprovingAgent):
        async def invoke(self, *args, **kwargs):
            return supplement()
    workflow, rid = setup(tmp_path, Agent())
    result = await workflow._invoke_once(stage, {}, DocumentResult, tmp_path, rid, "other-stage")
    assert result.files == supplement().files
    assert workflow.store.get(rid, "invocation/other-stage")["result"] == result.model_dump()
