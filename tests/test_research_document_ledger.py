"""Offline document lifecycle tests: real artifacts, fake HTTP/providers only."""
import asyncio
import base64
import ssl
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest
from authority_review_fixtures import authority_review
from test_decisions import ApprovingAgent
from test_workflow import engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.collection_worker import execute
from auto_agents_2.engine import Replan, choice_spec
from auto_agents_2.errors import (
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    ResearchDestinationRejected,
    ResearchResolverUnavailable,
    preserve_http_connection_failure,
)
from auto_agents_2.faults import HumanIntervention, StateConflict
from auto_agents_2.models import DocumentResult, SourceEvidence
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import (
    CONSUMED,
    CONTEXT,
    HARD,
    WAIT,
    ResearchAcquisitionRecovery,
    ResearchRecoveryDocument,
    failure_observation,
)
from auto_agents_2.research_batch import ResearchBatch, ResearchRequestNotExecuted
from auto_agents_2.research_documents import MAX_CAUSE_ATTEMPTS, RetainedDocumentFailure, stable_cause
from auto_agents_2.store import Store

URL = "https://docs.example.test/spec"
GOOD = "https://independent.example.test/reference"


@pytest.fixture(autouse=True)
def no_real_sockets(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Offline fixture must not open real sockets")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def publication(urls=(URL, GOOD)):
    return DocumentResult(summary="Fixture research", document="Original complete claims.", sources=[
        SourceEvidence(url=url, claim="Fact " + str(index), version="declared-v1")
        for index, url in enumerate(urls)])


def capture(artifacts, url, body=None):
    return execute(artifacts.store.root, {"action": "research", "source": url, "final_url": url,
        "mime": "text/plain", "raw": base64.b64encode((body or url).encode()).decode()})


def status_error(url, status, text="EXAMPLE_PRIVATE_MESSAGE"):
    response = httpx.Response(status, request=httpx.Request("GET", url))
    return DocumentAcquisitionUnavailable(url, httpx.HTTPStatusError(
        text, request=response.request, response=response))


def setup(tmp_path, document=None):
    store = Store(tmp_path)
    rid = store.admit("Fixture", {})["id"]
    artifacts = ArtifactStore(store.root)
    recovery = ResearchAcquisitionRecovery(SimpleNamespace(store=store, artifacts=artifacts))
    doc = document or publication()
    store.checkpoint(rid, "candidate/provider_research", {"document": doc.model_dump()})
    stamp = recovery.dispatch_binding(rid, doc)
    ledger = recovery.begin(rid, doc, stamp)
    service = ResearchService(artifacts)
    service.ledger = ledger
    return store, rid, recovery, service, doc


def reopen(store, rid, recovery, document):
    store.checkpoint(rid, "candidate/provider_research", {"document": document.model_dump()})
    service = ResearchService(recovery.engine.artifacts)
    service.ledger = recovery.begin(rid, document, recovery.dispatch_binding(rid, document))
    return service


async def observed_failure(recovery, rid, service, doc):
    with pytest.raises((DocumentAcquisitionUnavailable, DocumentAcquisitionTimeout,
                        ResearchDestinationRejected, ResearchResolverUnavailable, RetainedDocumentFailure)) as failed:
        await service.verify(doc.sources)
    failed.value.acquisition_dispatch = service.ledger.dispatch
    return failed.value


async def test_success_cache_survives_restart_generation_claim_and_declared_version_changes(tmp_path, monkeypatch):
    doc = publication((URL + "#one", URL + "#two"))
    store, rid, recovery, service, doc = setup(tmp_path, doc)
    calls = []
    async def fetch(self, url):
        calls.append(url)
        return capture(self.artifacts, url, "Current document version is 6 November.")
    monkeypatch.setattr(ResearchService, "capture", fetch)
    first = await service.verify(doc.sources)
    assert calls == [URL] and not recovery.active(rid)
    assert first[0]["supported_version"] is None
    store.update(rid, replans=4)
    revised = publication((URL + "#three", URL + "#four"))
    revised.sources[0].version = "6 November, corrected from 5 November"
    revised.sources[0].claim = "Corrected source claim, still only a proposal"
    resumed = reopen(store, rid, ResearchAcquisitionRecovery(recovery.engine), revised)
    second = await resumed.verify(revised.sources)
    assert calls == [URL]
    assert second[0]["claimed_version"] == revised.sources[0].version and second[0]["supported_version"] is None
    assert second[0]["retrieval"]["captured_at"] == first[0]["retrieval"]["captured_at"]
    receipt = second[0]["retrieval"]["acquisition_receipt"]
    assert receipt["reused"] and receipt["original_dispatch"]["generation"] == 0
    assert receipt["current_dispatch"]["generation"] == 4
    # A genuinely different version URL is a different file, with its own provenance.
    changed = publication((URL + "?version=2",))
    await reopen(store, rid, recovery, changed).verify(changed.sources)
    assert calls == [URL, URL + "?version=2"]


async def test_failed_file_does_not_discard_or_redownload_successful_sibling(tmp_path, monkeypatch):
    store, rid, recovery, service, doc = setup(tmp_path)
    calls = Counter()
    async def fetch(self, url):
        calls[url] += 1
        if url == URL:
            raise status_error(url, 404)
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    failed = await observed_failure(recovery, rid, service, doc)
    assert [row["status"] for row in failed.acquisition_observations] == ["failed", "retrieved"]
    with pytest.raises(Replan):
        recovery.observe(rid, doc, failed)
    store.update(rid, replans=1)
    resumed = reopen(store, rid, recovery, doc)
    repeated = await observed_failure(recovery, rid, resumed, doc)
    assert calls == {URL: 1, GOOD: 1}
    assert repeated.acquisition_observations[0]["request_executed"] is False
    assert repeated.acquisition_observations[1]["retrieval"]["acquisition_receipt"]["reused"]
    with pytest.raises(HumanIntervention):
        recovery.observe(rid, doc, repeated)
    assert calls == {URL: 1, GOOD: 1}  # Agent had a chance; identical terminal sources cannot spin.


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_file_is_not_retried_and_reaches_same_agent_context(tmp_path, monkeypatch, status):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        raise status_error(url, status)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    error = await observed_failure(recovery, rid, service, doc)
    with pytest.raises(Replan):
        recovery.observe(rid, doc, error)
    assert calls == [URL] and store.get(rid, WAIT) is None
    context = recovery.context(rid)
    assert context["claims"] and context["documents"][URL]["outcomes"]
    assert context["retry_policy"]["max_attempts_per_file_cause"] == MAX_CAUSE_ATTEMPTS


@pytest.mark.parametrize("kind", ["timeout", "429", "503"])
async def test_retry_budget_is_per_stable_cause_and_survives_restart(tmp_path, monkeypatch, kind):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    def failure(text):
        return (DocumentAcquisitionTimeout(text, 30, source_url=URL) if kind == "timeout"
                else status_error(URL, int(kind), text))
    first = failure("first finite label")
    pending = service.ledger.start(URL)
    service.ledger.finish(URL, pending, error=first)
    assert stable_cause(failure_observation(first)) == stable_cause(failure_observation(failure("new prose")))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        raise failure("changed text is not a new cause")
    monkeypatch.setattr(ResearchService, "capture", fetch)
    store.update(rid, replans=2)
    resumed = reopen(store, rid, ResearchAcquisitionRecovery(recovery.engine), doc)
    await observed_failure(recovery, rid, resumed, doc)
    assert len(calls) == MAX_CAUSE_ATTEMPTS - 1
    await observed_failure(recovery, rid, reopen(store, rid, recovery, doc), doc)
    assert len(calls) == MAX_CAUSE_ATTEMPTS - 1
    outcomes = resumed.ledger.record()["documents"][URL]["outcomes"]
    assert len(outcomes) == MAX_CAUSE_ATTEMPTS


async def test_transient_failure_can_succeed_within_its_bound(tmp_path, monkeypatch):
    _, _, _, service, doc = setup(tmp_path, publication((URL,)))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        if len(calls) < MAX_CAUSE_ATTEMPTS:
            raise status_error(url, 503)
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    retrieved = await service.verify(doc.sources)
    assert len(calls) == MAX_CAUSE_ATTEMPTS and retrieved[0]["retrieval"]["artifact"]


@pytest.mark.parametrize("corruption", ["body", "owner", "manifest", "cause", "pending", "event"])
async def test_record_or_artifact_corruption_stops_before_a_new_request(tmp_path, monkeypatch, corruption):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    async def fetch(self, url):
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    if corruption == "cause":
        pending = service.ledger.start(URL)
        service.ledger.finish(URL, pending, error=status_error(URL, 404))
    else:
        await service.verify(doc.sources)
    record = service.ledger.record()
    outcome = record["documents"][URL]["outcomes"][0]
    if corruption == "body":
        ref = outcome["retrieval"]["raw"]
        (store.root / "artifacts" / ref["hash"][:2] / ref["hash"]).write_text("Damaged fixture")
    elif corruption == "owner":
        with store.connect() as db:
            db.execute("DELETE FROM artifact_owners WHERE run_id=? AND hash=?",
                       (rid, outcome["retrieval"]["artifact"]["hash"]))
    elif corruption == "event":
        with store.connect() as db:
            db.execute("DELETE FROM events WHERE run_id=? AND kind=?", (rid, "research.document_finished"))
    else:
        if corruption == "manifest":
            outcome["retrieval"]["hash"] = "0" * 64
        elif corruption == "cause":
            outcome["cause"]["facts"]["http_status"] = 503
        else:
            record["documents"][URL]["pending"] = {"id": "incomplete"}
        store.checkpoint(rid, service.ledger.key, record)
    async def forbidden(self, url):
        pytest.fail("Corrupt evidence must not fall back to redownloading")
    monkeypatch.setattr(ResearchService, "capture", forbidden)
    with pytest.raises(StateConflict):
        await service.verify(doc.sources)
    assert store.get(rid, "provider_research") is None


async def test_target_refusal_preserves_other_files_then_allows_only_independent_alternative(tmp_path, monkeypatch):
    store, rid, recovery, service, doc = setup(tmp_path)
    calls = []
    async def fetch(self, url):
        calls.append(url)
        if url == URL:
            raise ResearchDestinationRejected("docs.example.test")
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    error = await observed_failure(recovery, rid, service, doc)
    assert set(calls) == {URL, GOOD}
    with pytest.raises(Replan):
        recovery.observe(rid, doc, error)
    assert store.get(rid, WAIT) is None and store.get(rid, HARD) is None
    # A new path/subdomain is not independent evidence and must not dispatch.
    alias = publication(("https://sub.docs.example.test/renamed",))
    store.update(rid, replans=1)
    alias_error = await observed_failure(recovery, rid, reopen(store, rid, recovery, alias), alias)
    assert alias_error.code == "RESEARCH_DESTINATION_REJECTED" and len(calls) == 2
    alternative = publication(("https://another-authority.example.test/spec", GOOD))
    result = await reopen(store, rid, recovery, alternative).verify(alternative.sources)
    assert len(calls) == 3 and result[1]["retrieval"]["acquisition_receipt"]["reused"]


def shared_error():
    error = httpx.ConnectError("EXAMPLE_PROXY_FAILURE")
    preserve_http_connection_failure(error, "public_connect", scope="shared_proxy")
    return DocumentAcquisitionUnavailable(URL, error)


async def test_real_shared_boundary_still_requires_exact_normal_prerequisite(tmp_path, monkeypatch):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    async def fetch(self, url):
        raise shared_error()
    monkeypatch.setattr(ResearchService, "capture", fetch)
    error = await observed_failure(recovery, rid, service, doc)
    recovery.observe(rid, doc, error)
    wait = store.get(rid, WAIT)
    assert wait and store.get(rid, HARD) is None
    assert not recovery.active(rid)  # A prerequisite does not alter the author schema.
    with pytest.raises(HumanIntervention):
        recovery.prepare(rid)
    with pytest.raises(RetainedDocumentFailure):
        service.ledger.previous(URL)


async def test_not_executed_is_not_a_failed_capture_or_a_retry_charge(tmp_path, monkeypatch):
    _, _, _, service, doc = setup(tmp_path, publication((URL,)))
    batch = ResearchBatch(None)
    error = shared_error()
    blocked = ResearchRequestNotExecuted(batch, "proxy_request", error)
    blocked.evidence["prior_response_headers"] = [{"url_hash": "fixture", "http_status": 302}]
    pending = service.ledger.start(URL)
    service.ledger.finish(URL, pending, error=blocked)
    assert service.ledger.previous(URL) is None
    saved = service.ledger.record()["documents"][URL]["outcomes"][0]
    assert saved["status"] == "not_executed" and saved["boundary"] == blocked.evidence
    async def fetch(self, url):
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    assert await service.verify(doc.sources)


async def test_cancelled_capture_keeps_observed_refusal_and_cannot_redownload(tmp_path):
    _, _, _, service, _ = setup(tmp_path, publication((URL,)))
    error = asyncio.CancelledError()
    error.observed_failures = [failure_observation(status_error(URL, 403))]
    pending = service.ledger.start(URL)
    service.ledger.finish(URL, pending, error=error)
    with pytest.raises(RetainedDocumentFailure) as retained:
        service.ledger.previous(URL)
    assert retained.value.observation["diagnostic"]["http_status"] == 403
    assert service.ledger.excluded_hosts() == ("docs.example.test",)


class FirstSuccessAgent(ApprovingAgent):
    def __init__(self):
        super().__init__()
        self.authors = self.reviews = 0

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if stage == "provider_research":
            self.authors += 1
            assert schema is DocumentResult and CONTEXT not in context
            return publication()
        if context.get("review_kind") == "research":
            self.reviews += 1
            assert CONTEXT not in context and "acquisition_resolution" not in context["research"]
            return authority_review(context)
        if stage == "design":
            raise asyncio.CancelledError()
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


async def test_first_all_success_keeps_original_schema_author_count_and_review(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers.")
    agent = FirstSuccessAgent()
    workflow = engine(project, agent)
    calls = []
    async def fetch(self, url):
        calls.append(url)
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.authors == agent.reviews == 1 and calls == [URL, GOOD]
    assert workflow.store.run(rid)["data"]["replans"] == 0
    record = ResearchAcquisitionRecovery(workflow).episode(rid)
    assert record["completed"]["mode"] == "initial_collection"
    assert "acquisition_resolution" not in workflow.store.get(rid, "provider_research")
    assert workflow.authority.accepted(rid, "provider_research", workflow.store.get(rid, "provider_research"))


async def test_first_capture_cancel_resumes_normal_candidate_and_keeps_success_cache(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers.")
    agent = FirstSuccessAgent()
    workflow = engine(project, agent)
    calls = Counter()
    completed = asyncio.Event()
    async def fetch(self, url):
        calls[url] += 1
        if url == GOOD and calls[url] == 1:
            await completed.wait()
            # Let the other result persist its immutable successful receipt.
            await asyncio.sleep(0)
            raise asyncio.CancelledError()
        result = capture(self.artifacts, url)
        completed.set()
        return result
    monkeypatch.setattr(ResearchService, "capture", fetch)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert not ResearchAcquisitionRecovery(workflow).active(rid)
    resumed = engine(project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    assert agent.authors == agent.reviews == 1 and calls == {URL: 1, GOOD: 2}
    assert resumed.store.get(rid, "provider_research")


async def test_completed_collection_does_not_lock_new_authorized_requirement_binding(tmp_path, monkeypatch):
    store, rid, recovery, service, doc = setup(tmp_path)
    calls = []
    async def fetch(self, url):
        calls.append(url)
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    await service.verify(doc.sources)
    old = service.ledger.record()
    # Simulate the already independently accepted episode; no acceptance is fabricated for a run.
    store.checkpoint(rid, service.ledger.key, {**old, "completed": {"fixture": "accepted historical episode"}})
    store.checkpoint(rid, "clarify", {"requirements": [{"id": "R1", "statement": "New authorized requirement"}]})
    store.update(rid, replans=1)
    recovery.prepare(rid)
    fresh = reopen(store, rid, recovery, doc)
    result = await fresh.verify(doc.sources)
    assert fresh.ledger.key != service.ledger.key and calls == [URL, GOOD]
    assert result[0]["retrieval"]["acquisition_receipt"]["reused"]
    assert store.get(rid, service.ledger.key)["completed"] == {"fixture": "accepted historical episode"}


async def test_origin_freeze_does_not_become_a_shared_direct_route():
    batch = ResearchBatch(None)
    failure = DocumentAcquisitionUnavailable(URL, httpx.ConnectError("EXAMPLE_UNKNOWN"))
    batch.observe_connection(failure, URL)
    assert not batch.route_failures and batch.origin_failures
    with pytest.raises(ResearchRequestNotExecuted):
        batch.before_capture(URL)
    batch.before_capture(GOOD)


def consume_current_wait(store, rid, recovery):
    wait = store.get(rid, WAIT)
    assert wait
    key = "prerequisite/" + wait["identity"] + "/" + str(wait["attempt"])
    interaction = choice_spec(["Resolved, recheck", "Pause"], ["retry", "pause"], yes_no=True)
    interaction["pause_on"] = "n"
    question = store.request(rid, key, "Exact synthetic shared prerequisite", wait["identity"], interaction)
    store.answer(question["id"], "y", wait["identity"])
    recovery.consume_wait(rid, wait, question["id"])
    assert store.get(rid, CONSUMED + wait["id"])["question_id"] == question["id"]
    return wait


@pytest.mark.parametrize("kind", ["proxy_407", "resolver_tls"])
async def test_normal_shared_prerequisite_reply_rechecks_actual_original_boundary(tmp_path, monkeypatch, kind):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    calls = []
    def refusal():
        if kind == "resolver_tls":
            return ResearchResolverUnavailable(category="network", error_type="ConnectError",
                ssl_verify_code=20, connection_phase="public_connect",
                endpoint_origin="https://resolver.example.test/", endpoint_url_hash="fixture")
        error = httpx.ProxyError("EXAMPLE_AUTH")
        preserve_http_connection_failure(error, "public_connect", scope="shared_proxy", proxy_status=407)
        return DocumentAcquisitionUnavailable(URL, error)
    async def fetch(self, url):
        calls.append(url)
        if len(calls) == 1:
            raise refusal()
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    error = await observed_failure(recovery, rid, service, doc)
    recovery.observe(rid, doc, error)
    assert service.ledger.excluded_hosts() == ()
    wait = consume_current_wait(store, rid, recovery)
    recovery.prepare(rid)
    result = await reopen(store, rid, recovery, doc).verify(doc.sources)
    assert calls == [URL, URL] and result[0]["retrieval"]["artifact"]
    assert store.get(rid, "prerequisite/" + wait["identity"]) == 1
    outcomes = service.ledger.record()["documents"][URL]["outcomes"]
    assert [row["status"] for row in outcomes] == ["failed", "retrieved"]


async def test_old_consumed_shared_refusal_does_not_bypass_later_timeout_limit(tmp_path, monkeypatch):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        if len(calls) == 1:
            raise shared_error()
        raise DocumentAcquisitionTimeout("Fixture current deadline", 30, source_url=url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    error = await observed_failure(recovery, rid, service, doc)
    recovery.observe(rid, doc, error)
    consume_current_wait(store, rid, recovery)
    await observed_failure(recovery, rid, reopen(store, rid, recovery, doc), doc)
    assert len(calls) == 1 + MAX_CAUSE_ATTEMPTS
    await observed_failure(recovery, rid, reopen(store, rid, recovery, doc), doc)
    assert len(calls) == 1 + MAX_CAUSE_ATTEMPTS


async def test_alternating_actual_causes_are_counted_once_per_capture(tmp_path, monkeypatch):
    _, _, _, service, doc = setup(tmp_path, publication((URL,)))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        if len(calls) in {1, 3, 4}:
            raise DocumentAcquisitionTimeout("Fixture", 30, source_url=url)
        raise status_error(url, 503)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    with pytest.raises(DocumentAcquisitionTimeout):
        await service.verify(doc.sources)
    assert len(calls) == 4  # timeout A, 503 B, timeout A, timeout A reaches its three actual attempts
    outcomes = service.ledger.record()["documents"][URL]["outcomes"]
    assert [row["failure"]["code"] for row in outcomes] == [
        "DOCUMENT_ACQUISITION_TIMEOUT", "DOCUMENT_ACQUISITION_UNAVAILABLE",
        "DOCUMENT_ACQUISITION_TIMEOUT", "DOCUMENT_ACQUISITION_TIMEOUT"]


class OriginBoundaryAgent(ApprovingAgent):
    def __init__(self, document):
        super().__init__()
        self.document = document
        self.authors = self.reviews = 0

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if stage == "provider_research":
            self.authors += 1
            if self.authors == 1:
                return self.document
            recovery = context[CONTEXT]
            return ResearchRecoveryDocument(summary="Fixture necessity assessment",
                document="These references are not needed for the original arithmetic requirement.",
                sources=[], acquisition_resolution={"episode_id": recovery["episode_id"], "status": "ready",
                    "missing_information": [], "claims": [{"claim_id": row["id"], "disposition": "not_required",
                        "source_indexes": [], "requirement_ids": ["R1"],
                        "reason": "The original arithmetic requirement needs no external service or these references."}
                        for row in recovery["claims"]]})
        if context.get("review_kind") == "research":
            self.reviews += 1
            assert len(context[CONTEXT]["claims"]) == 6 and context["retrieved_sources"] == []
            return authority_review(context)
        if stage == "design":
            raise asyncio.CancelledError()
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


async def test_same_origin_blocked_sibling_reaches_agent_while_independent_files_finish(tmp_path, monkeypatch):
    urls = [URL, GOOD + "/1", GOOD + "/2", GOOD + "/3", URL + "/second", GOOD + "/4"]
    doc = publication(urls)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers.")
    agent = OriginBoundaryAgent(doc)
    workflow = engine(project, agent)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def fetch(self, url):
        calls.append(url)
        if len(calls) == 4:
            started.set()
        if url == URL:
            await started.wait()
            release.set()
            error = httpx.ConnectError("EXAMPLE_ORIGIN_TLS")
            cause = ssl.SSLCertVerificationError(1, "EXAMPLE_CERTIFICATE")
            cause.verify_code = 20
            error.__cause__ = cause
            preserve_http_connection_failure(error, "origin_tls", scope="origin")
            raise DocumentAcquisitionUnavailable(url, error)
        await release.wait()
        return capture(self.artifacts, url)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    record = ResearchAcquisitionRecovery(workflow).episode(rid)
    assert [row["status"] for row in record["attempts"][0]["observations"]] == [
        "failed", "retrieved", "retrieved", "retrieved", "not_executed", "retrieved"]
    assert agent.authors == 2 and agent.reviews == 1 and URL + "/second" not in calls
    assert workflow.store.get(rid, WAIT) is None and workflow.store.get(rid, HARD) is None
    assert record["completed"] and workflow.store.get(rid, "provider_research")["sources"] == []


async def test_document_finish_event_failure_preserves_original_error_and_hard_hold(tmp_path, monkeypatch):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    original_event = store._event
    def event(db, run_id, kind, data):
        if kind == "research.document_finished":
            raise OSError("EXAMPLE_RECORDING_FAILURE")
        return original_event(db, run_id, kind, data)
    monkeypatch.setattr(store, "_event", event)
    async def fetch(self, url):
        raise status_error(url, 503)
    monkeypatch.setattr(ResearchService, "capture", fetch)
    error = await observed_failure(recovery, rid, service, doc)
    assert error.evidence["http_status"] == 503 and error.evidence["recording_failure"]
    recording = error.acquisition_document_recording_error
    recovery.recording_failed(rid, doc, error, recording)
    recovery.observe(rid, doc, error)
    assert store.get(rid, HARD)
    assert service.ledger.record()["documents"][URL]["pending"]
    with pytest.raises(StateConflict):
        recovery.prepare(rid)
    assert len([row for row in store.events(rid) if row["kind"] == "research.document_started"]) == 1


@pytest.mark.parametrize("drift", ["candidate", "clarify", "generation"])
def test_inflight_finish_cas_keeps_pending_and_rolls_back_outcome_event_on_owner_drift(tmp_path, drift):
    store, rid, _, service, doc = setup(tmp_path, publication((URL,)))
    pending = service.ledger.start(URL)
    evidence = capture(service.artifacts, URL)
    if drift == "candidate":
        store.checkpoint(rid, "candidate/provider_research",
                         {"document": {**doc.model_dump(), "summary": "Different inflight candidate"}})
    elif drift == "clarify":
        store.checkpoint(rid, "clarify", {"requirements": [{"id": "R1", "statement": "Different binding"}]})
    else:
        store.update(rid, replans=1)
    before = service.ledger.record()
    with pytest.raises(StateConflict, match="exact dispatch"):
        service.ledger.finish(URL, pending, evidence=evidence)
    after = service.ledger.record()
    assert after == before and after["documents"][URL]["pending"] == pending
    assert after["documents"][URL]["outcomes"] == []
    assert not any(row["kind"] == "research.document_finished" for row in store.events(rid))
