"""Isolated real-Engine counterexamples; no historical project is accessed.

Model and external-research responses use existing scenario fixtures. Git,
verification, checkpoints, authority validation and recovery are production code.
"""
import asyncio

import pytest
from authority_review_fixtures import authority_review
from test_authority_workflow import BAD_METHOD, GOOD_METHOD
from test_decisions import ApprovingAgent
from test_workflow import engine

from auto_agents_2.models import DocumentResult, Finding, SourceEvidence
from auto_agents_2.research import ResearchService


class ResearchCorrectionAgent(ApprovingAgent):
    def __init__(self, drop_sources=False):
        super().__init__()
        self.authors = 0
        self.reviews = 0
        self.drop_sources = drop_sources

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if stage == "provider_research":
            self.authors += 1
            return DocumentResult(summary="research", document=(
                BAD_METHOD if self.authors == 1 or self.drop_sources else GOOD_METHOD),
                sources=[] if self.drop_sources and self.authors > 1 else [
                    SourceEvidence(url="https://fixture.invalid/reference", claim="fixture")])
        if context.get("review_kind") == "research":
            self.reviews += 1
            if self.authors == 1:
                result = authority_review(context, approved=False, summary="Research invented a mandatory method",
                    findings=[Finding(severity="major", location="research", description="Misattributed method",
                        evidence="candidate and original input", resolution="Correct the responsible research")])
                finding = result.authority["findings"][0]
                finding.update(kind="source_dispute", owner="provider_research",
                    method_source="candidate", method_quote=BAD_METHOD)
                for row in result.authority["coverage"]:
                    if row["criterion_id"] == finding["criterion_id"]:
                        row["status"] = "unverified"
                return result
            if self.drop_sources:
                assert context["controller_review_basis"]["correction"]
                raise asyncio.CancelledError()
            result = authority_review(context)
            correction = context["controller_review_basis"]["correction"]
            result.authority["amendments"] = [{"source": "previous_contract", "old_quote": clause,
                "new_quote": GOOD_METHOD, "preserve_ids": correction["preserve_ids"],
                "reason": "Preserve the business goal, remove the invented method"}
                for clause in correction["clauses"]]
            return result
        if stage == "design":
            raise asyncio.CancelledError()
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_sources", [False, True])
async def test_research_correction_must_be_reviewed_and_closed(tmp_path, monkeypatch, drop_sources):
    async def fixture_sources(*args):
        return []
    monkeypatch.setattr(ResearchService, "verify", fixture_sources)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve all assertions.")
    agent = ResearchCorrectionAgent(drop_sources)
    workflow = engine(project, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.reviews == 2, "A pending correction cannot skip independent review when sources are removed"
    correction = workflow.store.get(rid, "authority/correction")
    if drop_sources:
        assert workflow.store.get(rid, "provider_research") is None
    else:
        assert correction.get("completed"), "Accepted research must close its original correction"
        assert workflow.authority.author_scope(rid, "provider_research")["correction"] is None


class CorrectedEmptyAgent(ResearchCorrectionAgent):
    async def invoke(self, stage, context, schema, *args, **kwargs):
        result = await super().invoke(stage, context, schema, *args, **kwargs)
        if stage == "provider_research" and self.authors > 1:
            result.sources = []
        return result


@pytest.fixture
def local_research(monkeypatch):
    async def retrieve(*args):
        return []
    monkeypatch.setattr(ResearchService, "verify", retrieve)


def scenario(tmp_path, agent):
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve all assertions.")
    return engine(project, agent), request


async def test_corrected_empty_research_closes_same_dispute_without_changing_budget(tmp_path, local_research):
    from auto_agents_2.util import digest
    agent = CorrectedEmptyAgent()
    workflow, request = scenario(tmp_path, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    document = workflow.store.get(rid, "provider_research")
    correction = workflow.store.get(rid, "authority/correction")
    assert document["sources"] == [] and document["document"] == GOOD_METHOD
    assert correction["previous"]["document"] == BAD_METHOD
    assert correction["completed"]["artifact_hash"] == digest(document)
    assert agent.reviews == 2 and workflow.store.run(rid)["data"]["replans"] == 1


@pytest.mark.parametrize("when", ["before", "after"])
async def test_research_acceptance_and_correction_close_atomically_on_resume(tmp_path, monkeypatch, local_research, when):
    agent = CorrectedEmptyAgent()
    workflow, request = scenario(tmp_path, agent)
    checkpoints = workflow.store.checkpoints

    def interrupt(rid, updates):
        if "provider_research" in updates and updates.get("authority/correction", {}).get("completed"):
            if when == "after":
                checkpoints(rid, updates)
            raise asyncio.CancelledError()
        return checkpoints(rid, updates)

    monkeypatch.setattr(workflow.store, "checkpoints", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert bool(workflow.store.get(rid, "provider_research")) == (when == "after")
    assert bool(workflow.store.get(rid, "authority/correction").get("completed")) == (when == "after")
    previous_reviews = agent.reviews
    resumed = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert agent.authors == 2
    assert resumed.store.get(rid, "authority/correction")["completed"]
    assert resumed.store.run(rid)["data"]["replans"] == 1
    if when == "after":
        assert agent.reviews == previous_reviews
    assert resumed.authority.author_scope(rid, "provider_research")["correction"] is None


async def test_cached_legacy_research_reaudits_active_dispute_and_retains_controller_metadata(tmp_path, monkeypatch, local_research):
    from auto_agents_2.util import digest
    agent = CorrectedEmptyAgent()
    workflow, request = scenario(tmp_path, agent)
    checkpoints = workflow.store.checkpoints

    def old_incomplete_acceptance(rid, updates):
        if "provider_research" in updates and updates.get("authority/correction", {}).get("completed"):
            # Reproduce the old durable boundary with a legitimate older accepted
            # marker: neither that marker nor a cached document closes a dispute.
            doc = {**updates["provider_research"], "verified_sources": [{"source": "retained-controller-metadata"}]}
            accepted = {**updates["authority/accepted/provider_research"], "artifact_hash": digest(doc)}
            checkpoints(rid, {"provider_research": doc, "authority/accepted/provider_research": accepted})
            raise asyncio.CancelledError()
        return checkpoints(rid, updates)

    monkeypatch.setattr(workflow.store, "checkpoints", old_incomplete_acceptance)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    saved = workflow.store.get(rid, "provider_research")
    assert workflow.authority.accepted(rid, "provider_research", saved)
    assert not workflow.store.get(rid, "authority/correction").get("completed")
    before = agent.reviews
    resumed = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert agent.reviews == before + 1 and agent.authors == 2
    assert resumed.store.get(rid, "provider_research") == saved
    assert resumed.store.get(rid, "authority/correction")["completed"]["artifact_hash"] == digest(saved)
    assert resumed.authority.accepted(rid, "provider_research", saved)


async def test_source_free_research_without_correction_retains_shortcut(tmp_path, monkeypatch):
    class OrdinaryAgent(ApprovingAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "provider_research":
                return DocumentResult(summary="no external source", document="No external provider needed.")
            if context.get("review_kind") == "research":
                pytest.fail("Ordinary source-free research does not need correction review")
            if stage == "design":
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, *args, **kwargs)
    workflow, request = scenario(tmp_path, OrdinaryAgent())
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert workflow.store.get(rid, "provider_research")["sources"] == []
    assert workflow.store.get(rid, "authority/accepted/provider_research") is None
