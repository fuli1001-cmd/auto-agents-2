"""Source recovery counterexamples: fake documents/providers, real gates and receipts."""
import asyncio
import copy
import errno
import ssl

import httpx
import pytest
from authority_review_fixtures import authority_review
from test_decisions import ApprovingAgent
from test_workflow import engine

from auto_agents_2.errors import (
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    InvalidStructuredResponse,
    ResearchDestinationRejected,
)
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import DocumentResult, Finding, SourceEvidence
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import (
    CONTEXT,
    CURRENT,
    PREFIX,
    ResearchAcquisitionRecovery,
    ResearchRecoveryDocument,
    claim_rows,
    failure_observation,
)
from auto_agents_2.util import digest

URL = "https://docs.example.test/spec"
ALTERNATIVE = "https://standard.example.test/reference"


def http_error(status):
    request = httpx.Request("GET", URL)
    response = httpx.Response(status, request=request)
    return DocumentAcquisitionUnavailable(URL, httpx.HTTPStatusError("EXAMPLE_PRIVATE_ERROR", request=request, response=response))


@pytest.mark.parametrize("error,category,automatic", [
    (http_error(404), "unavailable", True), (http_error(410), "unavailable", True),
    (http_error(503), "network", True), (http_error(429), "network", True),
    (http_error(403), "permission", False), (http_error(401), "permission", False),
    (http_error(451), "permission", False),
    (DocumentAcquisitionTimeout("Research", 30, source_url=URL), "network", True),
    (DocumentAcquisitionUnavailable(URL, httpx.ConnectError("EXAMPLE_SECRET")), "unknown", False),
    (DocumentAcquisitionUnavailable(URL, httpx.ReadTimeout("EXAMPLE_SECRET")), "network", True),
    (ResearchDestinationRejected("docs.example.test"), "security", False),
    (RuntimeError("EXAMPLE_SECRET"), "unknown", False),
])
def test_classification_requires_positive_evidence(error, category, automatic):
    observed = failure_observation(error)
    assert (observed["classification"], observed["automatic"]) == (category, automatic)
    assert "EXAMPLE_" not in str(observed)


def test_explicit_tls_and_permission_causes_cannot_be_network_retries():
    for cause in (ssl.SSLCertVerificationError(1, "EXAMPLE_SECRET"), OSError(errno.EACCES, "EXAMPLE_SECRET")):
        if isinstance(cause, ssl.SSLCertVerificationError):
            cause.verify_code = 20
        error = httpx.ConnectError("EXAMPLE_SECRET")
        error.__cause__ = cause
        observed = failure_observation(DocumentAcquisitionUnavailable(URL, error))
        assert not observed["automatic"] and observed["classification"] in {"security", "permission"}


def publication():
    return DocumentResult(summary="Original research", document="Two separate proposed claims.", sources=[
        SourceEvidence(url=URL + "#first", claim="First fact", version="1"),
        SourceEvidence(url=URL + "#second", claim="Second fact", version="2")])


def captured(url, artifacts=None):
    if artifacts is not None:
        import base64

        from auto_agents_2.collection_worker import execute
        return execute(artifacts.store.root, {"action": "research", "source": url, "final_url": url,
            "mime": "text/plain", "raw": base64.b64encode(url.encode()).decode()})
    return {"artifact": {"hash": digest(url), "kind": "fixture", "bytes": 12},
            "excerpt": "Fixture source content", "url": url, "final_url": url}


async def test_same_document_multiple_claims_stay_distinct_in_batch(tmp_path, monkeypatch):
    seen = []
    async def capture(self, url):
        seen.append(url)
        return captured(url)
    monkeypatch.setattr(ResearchService, "capture", capture)
    result = await ResearchService(None).verify(publication().sources)
    assert seen == [URL] and [row["claim"] for row in result] == ["First fact", "Second fact"]
    assert [row["claimed_version"] for row in result] == ["1", "2"]


@pytest.mark.parametrize("unsafe", ["403", "tls", "unknown", "destination"])
async def test_concurrent_404_never_hides_stronger_boundary(monkeypatch, unsafe):
    errors = {"403": http_error(403), "tls": DocumentAcquisitionUnavailable(URL, httpx.ConnectError("unknown TLS cause")),
              "unknown": RuntimeError("EXAMPLE_BUG"), "destination": ResearchDestinationRejected("blocked.example.test")}
    async def capture(self, url):
        raise http_error(404) if url == URL else errors[unsafe]
    monkeypatch.setattr(ResearchService, "capture", capture)
    with pytest.raises(type(errors[unsafe])) as observed:
        await ResearchService(None).verify([SourceEvidence(url=URL, claim="a"),
            SourceEvidence(url=ALTERNATIVE, claim="b")])
    rows = observed.value.acquisition_observations
    assert len(rows) == 2 and all(row["status"] == "failed" for row in rows)
    assert any(not row["failure"]["automatic"] for row in rows)


async def test_transient_batch_keeps_partial_success_and_awaits_all_outcomes(monkeypatch):
    async def capture(self, url):
        if url == URL:
            raise http_error(404)
        await asyncio.sleep(0)
        return captured(url)
    monkeypatch.setattr(ResearchService, "capture", capture)
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(None).verify([SourceEvidence(url=URL, claim="a"), SourceEvidence(url=ALTERNATIVE, claim="b")])
    rows = observed.value.acquisition_observations
    assert [row["status"] for row in rows] == ["failed", "retrieved"]
    assert rows[1]["retrieval"]["artifact"]["hash"] == digest(ALTERNATIVE)


async def test_cancellation_still_cancels_all_siblings(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def capture(self, url):
        if url == URL:
            await started.wait()
            raise asyncio.CancelledError()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(ResearchService, "capture", capture)
    with pytest.raises(asyncio.CancelledError):
        await ResearchService(None).verify([SourceEvidence(url=URL, claim="a"), SourceEvidence(url=ALTERNATIVE, claim="b")])
    assert cancelled.is_set()


class RecoveryAgent(ApprovingAgent):
    def __init__(self, mode="alternative"):
        super().__init__()
        self.authors, self.reviews = 0, 0
        self.mode = mode
        self.contexts = []

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if stage == "provider_research":
            self.authors += 1
            if self.authors == 1:
                return publication()
            self.contexts.append(copy.deepcopy(context[CONTEXT]))
            recovery = context[CONTEXT]
            disposition = "not_required" if self.mode == "empty" else "blocked" if self.mode == "blocked" else "supported"
            sources = [] if self.mode in {"empty", "blocked"} else [
                SourceEvidence(url=(URL + "#reworded" if self.mode == "repeat" else ALTERNATIVE), claim="Both facts")]
            return ResearchRecoveryDocument(summary="Replacement", document="Preserve original business goals.", sources=sources,
                acquisition_resolution={"episode_id": recovery["episode_id"],
                    "status": "blocked" if self.mode == "blocked" else "ready",
                    "missing_information": ["Authoritative statement for the first fact"] if self.mode == "blocked" else [],
                    "claims": [{"claim_id": row["id"], "disposition": disposition,
                        "source_indexes": [0] if sources else [], "requirement_ids": ["R1"],
                        "reason": "Equivalent primary reference" if sources else "This proposed research detail is not required for adding numbers"}
                        for row in recovery["claims"]]})
        if context.get("review_kind") == "research":
            self.reviews += 1
            assert CONTEXT in context and len(context[CONTEXT]["claims"]) == 2
            criteria = [row for row in context["controller_review_basis"]["criteria"] if row["id"].startswith("stage:research-acquisition:")]
            assert len(criteria) == 2
            assert context["controller_review_basis"]["observations"][CONTEXT] == digest(context[CONTEXT])
            if self.mode == "empty":
                assert context["research"]["sources"] == [] and context["retrieved_sources"] == []
            else:
                assert context["retrieved_sources"][0]["retrieval"]["artifact"]["hash"] == digest(ALTERNATIVE)
            if self.mode == "review_repeat":
                result = authority_review(context, approved=False, summary="Equivalent coverage is still missing",
                    findings=[Finding(severity="major", location="research", description="Claim unsupported",
                        evidence="Current primary document does not state the claimed guarantee", resolution="Preserve the fact and correct its evidence")])
                target = criteria[0]
                for row in result.authority["coverage"]:
                    row["status"] = "unverified" if row["criterion_id"] == target["id"] else "satisfied"
                result.authority["findings"][0].update(criterion_id=target["id"], kind="evidence_gap",
                    source_quote=target["statement"])
                return result
            return authority_review(context)
        if stage == "design":
            raise asyncio.CancelledError()
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


def scenario(tmp_path, monkeypatch, mode="alternative"):
    async def capture(self, url):
        if url == URL:
            raise http_error(404)
        return captured(url, self.artifacts)
    monkeypatch.setattr(ResearchService, "capture", capture)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve all assertions.")
    agent = RecoveryAgent(mode)
    workflow = engine(project, agent)
    return workflow, agent, request


@pytest.mark.parametrize("mode", ["alternative", "empty"])
async def test_source_failure_returns_to_research_and_requires_independent_acceptance(tmp_path, monkeypatch, mode):
    workflow, agent, request = scenario(tmp_path, monkeypatch, mode)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.authors == 2 and agent.reviews == 1
    assert workflow.store.run(rid)["data"]["replans"] == 1
    assert workflow.store.get(rid, "plan") is None
    pointer = workflow.store.get(rid, CURRENT)
    record = workflow.store.get(rid, pointer["key"])
    assert len(record["claims"]) == 2 and len({row["id"] for row in record["claims"]}) == 2
    assert record["claims"] == claim_rows(publication().model_dump())
    assert len(record["attempts"]) == 1 and record["completed"]["review_key"]
    assert workflow.store.get(rid, "provider_research")["acquisition_resolution"]["status"] == "ready"
    assert workflow.authority.accepted(rid, "provider_research", workflow.store.get(rid, "provider_research"))
    history = workflow.store.get(rid, "recovery/gate-history/0")
    assert history["checkpoints"]["candidate/provider_research"]["document"] == publication().model_dump()
    permit = workflow.store.get(rid, history["pending"]["permit"])
    assert permit["phase"] == "completed" and permit["outcome"]["status"] == "gate_reset"


@pytest.mark.parametrize("mode", ["repeat", "blocked"])
async def test_no_progress_or_missing_information_stops_with_evidence(tmp_path, monkeypatch, mode):
    workflow, agent, request = scenario(tmp_path, monkeypatch, mode)
    questions = []
    async def stop(question):
        questions.append(question)
        raise asyncio.CancelledError()
    workflow.question_handler = stop
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.authors == 2 and agent.reviews == 0
    assert workflow.store.get(rid, "provider_research") is None
    record = ResearchAcquisitionRecovery(workflow).active(rid)
    assert len(record["attempts"]) == (2 if mode == "repeat" else 1)
    assert questions and "Claims requiring resolution" in questions[-1]["prompt"]
    assert URL in questions[-1]["prompt"] and "Observed attempts" in questions[-1]["prompt"]
    if mode == "blocked":
        assert "Authoritative statement" in questions[-1]["prompt"]


@pytest.mark.parametrize("window", ["observation", "gate_reset"])
async def test_interruption_preserves_observation_and_finishes_exact_reentry(tmp_path, monkeypatch, window):
    workflow, agent, request = scenario(tmp_path, monkeypatch)
    if window == "observation":
        def interrupt(*args):
            raise asyncio.CancelledError()
        monkeypatch.setattr(workflow, "_controller_replan", interrupt)
    else:
        complete = workflow.recovery_policy.complete
        def interrupt(rid, key, outcome):
            if outcome == {"status": "gate_reset", "stage": "provider_research"}:
                raise asyncio.CancelledError()
            return complete(rid, key, outcome)
        monkeypatch.setattr(workflow.recovery_policy, "complete", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.authors == 1
    resumed = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert agent.authors == 2 and agent.reviews == 1
    assert resumed.store.run(rid)["data"]["replans"] == 1
    assert not ResearchAcquisitionRecovery(resumed).active(rid)


async def test_stale_claims_and_missing_dispositions_cannot_advance(tmp_path, monkeypatch):
    workflow, agent, request = scenario(tmp_path, monkeypatch)
    def interrupt(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow, "_controller_replan", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    recovery = ResearchAcquisitionRecovery(workflow)
    with pytest.raises(InvalidStructuredResponse, match="disposition"):
        recovery.validate(rid, DocumentResult(summary="drop", document="Empty", sources=[]))
    specification = workflow.store.get(rid, "clarify")
    workflow.store.checkpoint(rid, "clarify", {**specification, "summary": "different requirements"})
    with pytest.raises(StateConflict, match="requirements changed"):
        recovery.context(rid)


async def test_ordinary_review_rejections_cannot_loop_on_identical_evidence(tmp_path, monkeypatch):
    workflow, agent, request = scenario(tmp_path, monkeypatch, "review_repeat")
    questions = []
    async def pause(question):
        questions.append(question)
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.authors == 3 and agent.reviews == 2
    record = ResearchAcquisitionRecovery(workflow).active(rid)
    assert len(record["rejected_reviews"]) == 2
    assert len({row["facts_hash"] for row in record["rejected_reviews"]}) == 1
    assert workflow.store.get(rid, "provider_research") is None
    assert "Independent research review rejected the same evidence" in questions[-1]["prompt"]
    assert workflow.store.run(rid)["data"]["replans"] == 2


def test_historical_claim_is_not_a_current_source_dispute_target():
    from auto_agents_2.review_authority import InvalidAuthorityReview, make_basis, validate_review
    document = publication().model_dump()
    context = {"research": document, CONTEXT: {"claims": claim_rows(document)}}
    basis = make_basis("research", context, original="Preserve the stated goals", specification={})
    context["controller_review_basis"] = basis
    review = authority_review(context, approved=False)
    target = next(row for row in basis["criteria"] if row["id"].startswith("stage:research-acquisition:"))
    for row in review.authority["coverage"]:
        row["status"] = "unverified" if row["criterion_id"] == target["id"] else "satisfied"
    review.authority["findings"][0].update(criterion_id=target["id"], kind="source_dispute",
        source_quote=target["statement"], method_source="failed_research", method_quote="First fact")
    assert basis["sources"]["failed_research"]["kind"] == "historical_proposal"
    with pytest.raises(InvalidAuthorityReview, match="invalid_source_dispute_owner_or_method"):
        validate_review(review, basis)


@pytest.mark.parametrize("window", ["before", "after"])
async def test_acceptance_and_episode_completion_are_atomic_on_resume(tmp_path, monkeypatch, window):
    workflow, agent, request = scenario(tmp_path, monkeypatch)
    checkpoints = workflow.store.checkpoints
    def interrupt(rid, updates, **kwargs):
        if "provider_research" in updates and any(value.get("completed") for key, value in updates.items()
                                                  if key.startswith(PREFIX)):
            if window == "after":
                checkpoints(rid, updates, **kwargs)
            raise asyncio.CancelledError()
        return checkpoints(rid, updates, **kwargs)
    monkeypatch.setattr(workflow.store, "checkpoints", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert bool(workflow.store.get(rid, "provider_research")) == (window == "after")
    pointer = workflow.store.get(rid, CURRENT)
    assert bool(workflow.store.get(rid, pointer["key"]).get("completed")) == (window == "after")
    before_reviews = agent.reviews
    resumed = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert agent.authors == 2 and not ResearchAcquisitionRecovery(resumed).active(rid)
    assert bool(resumed.store.get(rid, "provider_research"))
    if window == "after":
        assert agent.reviews == before_reviews


async def test_changed_acquisition_ledger_cannot_reuse_a_stale_approval(tmp_path, monkeypatch):
    workflow, agent, request = scenario(tmp_path, monkeypatch)
    invoke = agent.invoke
    async def changed(stage, context, schema, work, rid, operation, fresh=False):
        response = await invoke(stage, context, schema, work, rid, operation, fresh)
        if context.get("review_kind") == "research":
            record = ResearchAcquisitionRecovery(workflow).active(rid)
            workflow.store.checkpoint(rid, PREFIX + record["id"], {**record,
                "attempts": [*record["attempts"], {**record["attempts"][-1], "generation": 99}]})
        return response
    monkeypatch.setattr(agent, "invoke", changed)
    questions=[]
    async def pause(question):
        questions.append(question)
        raise asyncio.CancelledError()
    workflow.question_handler=pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid=workflow.store.current()["id"]
    assert workflow.store.get(rid, "provider_research") is None
    assert "exact independent review evidence" in questions[-1]["prompt"]
    assert ResearchAcquisitionRecovery(workflow).active(rid)


async def test_later_timeout_does_not_erase_a_refusal_or_its_attempt(tmp_path, monkeypatch):
    workflow, agent, request = scenario(tmp_path, monkeypatch)
    async def denied(self, url):
        raise http_error(403)
    monkeypatch.setattr(ResearchService, "capture", denied)
    def interrupt_reentry(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow, "_controller_replan", interrupt_reentry)
    async def pause(question):
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    recovery = ResearchAcquisitionRecovery(workflow)
    assert recovery.active(rid)  # A target refusal now reaches the agent, without retrying it.
    async def timed_out(self, url):
        raise DocumentAcquisitionTimeout("Fixture download", 30, source_url=url)
    monkeypatch.setattr(ResearchService, "capture", timed_out)
    with pytest.raises(DocumentAcquisitionTimeout) as observed:
        await ResearchService(None).verify(publication().sources)
    recovery.observe(rid, publication(), observed.value)
    assert recovery.active(rid) and agent.authors == 1
    identity = digest([rid, 0, recovery.binding(rid), publication().model_dump()])
    attempts = workflow.store.get(rid, PREFIX + identity)["attempts"]
    assert len(attempts) == 2 and len({row["id"] for row in attempts}) == 2
    assert [row["observations"][0]["failure"]["classification"] for row in attempts] == ["permission", "network"]


async def test_new_observed_final_authority_is_new_evidence_even_with_identical_text(tmp_path, monkeypatch):
    workflow, agent, request = scenario(tmp_path, monkeypatch, "review_repeat")
    captures = []
    invoke = agent.invoke
    async def independent_source(stage, context, schema, work, rid, operation, fresh=False):
        document = await invoke(stage, context, schema, work, rid, operation, fresh)
        if stage == "provider_research" and agent.authors >= 3:
            document.sources[0].url = "https://official.example.test/reference"
        return document
    monkeypatch.setattr(agent, "invoke", independent_source)
    async def capture(self, url):
        if url == URL:
            raise http_error(404)
        import base64

        from auto_agents_2.collection_worker import execute
        captures.append(url)
        final = ("https://third-party.example.test/reference" if url == ALTERNATIVE else
                 "https://official.example.test/reference")
        return execute(self.artifacts.store.root, {"action": "research", "source": url, "final_url": final,
            "mime": "text/plain", "raw": base64.b64encode(ALTERNATIVE.encode()).decode()})
    monkeypatch.setattr(ResearchService, "capture", capture)
    async def pause(question):
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    failures = ResearchAcquisitionRecovery(workflow).active(rid)["rejected_reviews"]
    assert len(captures) == 2 and agent.authors == 4 and agent.reviews == 3
    assert failures[0]["facts_hash"] != failures[1]["facts_hash"]
    assert failures[1]["facts_hash"] == failures[2]["facts_hash"]
    assert workflow.store.get(rid, "provider_research") is None


class CleanupStream(httpx.AsyncByteStream):
    def __init__(self, mode):
        self.mode = mode

    async def __aiter__(self):
        yield b"Fixture document"

    async def aclose(self):
        if self.mode == "read_timeout":
            raise httpx.ReadTimeout("EXAMPLE_CLEANUP_SECRET")
        if self.mode == "deadline":
            await asyncio.Event().wait()
        if self.mode == "cancel":
            raise asyncio.CancelledError()


def cleanup_transport(monkeypatch, status, mode):
    from auto_agents_2.http_body import acquisition_deadline
    monkeypatch.setattr("auto_agents_2.research.acquisition_deadline",
        lambda timeout, label, *, source_url: acquisition_deadline(.01, label, source_url=source_url))
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(status, stream=CleanupStream(mode))))


@pytest.mark.parametrize("status", [401, 403, 451])
@pytest.mark.parametrize("mode", ["read_timeout", "deadline"])
async def test_cleanup_timeout_cannot_downgrade_an_observed_refusal(monkeypatch, status, mode):
    cleanup_transport(monkeypatch, status, mode)
    with pytest.raises(DocumentAcquisitionUnavailable) as observed:
        await ResearchService(None).capture(URL)
    error = observed.value
    assert error.evidence["http_status"] == status
    assert error.evidence["cleanup_failure"]
    assert failure_observation(error)["classification"] == "permission"
    assert not failure_observation(error)["automatic"]
    assert "EXAMPLE_" not in str(error.evidence)


async def test_successful_body_with_slow_cleanup_remains_a_real_acquisition_timeout(monkeypatch):
    cleanup_transport(monkeypatch, 200, "deadline")
    with pytest.raises(DocumentAcquisitionTimeout) as observed:
        await ResearchService(None).capture(URL)
    assert failure_observation(observed.value)["automatic"]


@pytest.mark.parametrize("status", [200, 403])
async def test_explicit_cancellation_is_not_reinterpreted_as_a_refusal_or_timeout(monkeypatch, status):
    cleanup_transport(monkeypatch, status, "cancel")
    with pytest.raises(asyncio.CancelledError):
        await ResearchService(None).capture(URL)


async def test_unknown_processing_failure_cannot_become_a_cleanup_retry(monkeypatch):
    from auto_agents_2.errors import AA2Error
    cleanup_transport(monkeypatch, 200, "read_timeout")
    unknown = AA2Error("Fixture processing failure")
    async def fail(*args, **kwargs):
        raise unknown
    monkeypatch.setattr("auto_agents_2.research.read_body", fail)
    with pytest.raises(AA2Error) as observed:
        await ResearchService(None).capture(URL)
    assert observed.value is unknown and not failure_observation(observed.value)["automatic"]
