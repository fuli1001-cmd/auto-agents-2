"""Pre-ledger compatibility uses authentic local evidence, never real networking."""
import copy

import pytest
from test_research_document_ledger import URL, capture, publication, setup

from auto_agents_2.errors import DocumentAcquisitionTimeout, ResearchDestinationRejected
from auto_agents_2.faults import StateConflict
from auto_agents_2.research_acquisition import CURRENT, HARD, PREFIX, WAIT, claim_rows, failure_observation
from auto_agents_2.research_documents import RetainedDocumentFailure, denied_hosts
from auto_agents_2.research_legacy import documents
from auto_agents_2.util import digest


def legacy(tmp_path, statuses=("retrieved",), *, failure=None):
    store, rid, recovery, service, doc = setup(tmp_path, publication((URL,)))
    record = {"version": 1, "id": "legacy", "binding": recovery.binding(rid),
              "original_candidate": recovery.engine.artifacts.put(doc.model_dump(), "research-acquisition-candidate"),
              "claims": claim_rows(doc.model_dump()), "attempts": []}
    for ordinal, status in enumerate(statuses, 1):
        observation = {"url": URL, "status": status}
        if status == "retrieved":
            evidence = capture(recovery.engine.artifacts, URL)
            observation["retrieval"] = {key: value for key, value in evidence.items() if key != "excerpt"}
        elif status == "failed":
            observation["failure"] = failure or failure_observation(DocumentAcquisitionTimeout("fixture", 30, source_url=URL))
        attempt = {"id": "attempt-" + str(ordinal), "generation": 0,
                   "candidate_hash": digest(doc.model_dump()), "sources": doc.model_dump()["sources"],
                   "observations": [observation]}
        attempt["facts_hash"] = digest(attempt["observations"])
        record["attempts"].append(attempt)
        store.event(rid, "research.acquisition_failed", {"episode_id": "legacy", "attempt": ordinal,
                    "observations": [observation]})
    store.checkpoints(rid, {CURRENT: {"id": "legacy", "key": PREFIX + "legacy"}, PREFIX + "legacy": record})
    return store, rid, recovery, doc, record


def reopen(store, rid, recovery, doc):
    return recovery.begin(rid, doc, recovery.dispatch_binding(rid, doc))


def test_legacy_success_keeps_unknown_transport_and_original_event(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path)
    original = copy.deepcopy(record)
    ledger = reopen(store, rid, recovery, doc)
    evidence = ledger.previous(URL)
    receipt = evidence["acquisition_receipt"]
    assert receipt["original_dispatch"] is None
    assert receipt["legacy_provenance"]["http_status"] is None
    assert receipt["legacy_provenance"]["attempt_id"] == "attempt-1"
    assert receipt["reused"] and evidence["captured_at"] == original["attempts"][0]["observations"][0]["retrieval"]["captured_at"]
    assert store.get(rid, PREFIX + "legacy")["attempts"] == original["attempts"]
    assert len(reopen(store, rid, recovery, doc).entry(ledger.record(), URL)["outcomes"]) == 1


def test_legacy_failures_keep_actual_budget_and_do_not_charge_not_executed(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed", "not_executed", "failed", "failed"))
    ledger = reopen(store, rid, recovery, doc)
    outcomes = ledger.entry(ledger.record(), URL)["outcomes"]
    assert ledger.cause_count(outcomes, outcomes[0]["failure"]) == 3
    with pytest.raises(RetainedDocumentFailure):
        ledger.previous(URL)


@pytest.mark.parametrize("damage", ["event", "artifact", "owner", "attempt"])
def test_legacy_corruption_is_rejected(tmp_path, damage):
    store, rid, recovery, doc, record = legacy(tmp_path)
    ledger = reopen(store, rid, recovery, doc)
    ref = record["attempts"][0]["observations"][0]["retrieval"]["raw"]
    if damage == "artifact":
        (store.root / "artifacts" / ref["hash"][:2] / ref["hash"]).write_bytes(b"changed")
    elif damage == "owner":
        with store.connect() as db:
            db.execute("DELETE FROM artifact_owners WHERE hash=? AND run_id=?", (ref["hash"], rid))
    elif damage == "event":
        with store.connect() as db:
            db.execute("UPDATE events SET data='{}' WHERE run_id=? AND kind='research.acquisition_failed'", (rid,))
    else:
        modified = store.get(rid, PREFIX + "legacy")
        modified["attempts"][0]["sources"][0]["claim"] = "changed"
        store.checkpoint(rid, PREFIX + "legacy", modified)
    with pytest.raises(StateConflict):
        ledger.previous(URL)


def test_legacy_projection_preserves_prerequisites_and_no_new_dispatch(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path)
    store.checkpoints(rid, {WAIT: {"original": "wait"}, HARD: {"original": "hard"}})
    before = store.events(rid)
    first = documents(store, rid, record)
    assert documents(store, rid, {**record, "documents": first}) == first
    assert store.events(rid) == before
    assert store.get(rid, WAIT) == {"original": "wait"} and store.get(rid, HARD) == {"original": "hard"}
    with pytest.raises(StateConflict):
        recovery.prepare_wait(rid)


def test_local_dns_refusal_keeps_exact_block_without_permanent_host_deny(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed",))
    failure = failure_observation(ResearchDestinationRejected("docs.example.test"))
    assert denied_hosts(URL, failure) == []
    assert failure["automatic"] is False


@pytest.mark.parametrize("status", ["cancelled", "not_executed"])
def test_actual_supplemental_failure_survives_incomplete_request(tmp_path, status):
    store, rid, recovery, doc, record = legacy(tmp_path, (status,))
    failure = failure_observation(DocumentAcquisitionTimeout("fixture", 30, source_url=URL))
    outcome = {"status": status, "observed_failures": [failure]}
    from auto_agents_2.research_documents import ResearchDocumentLedger
    assert ResearchDocumentLedger.cause_count([outcome], failure) == 1


def test_preprojection_source_tamper_is_rejected(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path)
    record["attempts"][0]["sources"][0]["claim"] = "changed before migration"
    with pytest.raises(StateConflict, match="archived candidate"):
        documents(store, rid, record)


def test_legacy_row_cannot_be_transplanted_to_another_url(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path)
    ledger = reopen(store, rid, recovery, doc)
    record = ledger.record()
    record["documents"]["https://other.example.test"] = {
        "url": "https://other.example.test", "outcomes": record["documents"][URL]["outcomes"]}
    with pytest.raises(StateConflict, match="different document"):
        ledger.entry(record, "https://other.example.test")


def test_consumed_legacy_shared_wait_stays_consumed_across_episode(tmp_path):
    from test_research_document_ledger import shared_error

    from auto_agents_2.research_acquisition import CONSUMED
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed",))
    attempt = record["attempts"][0]
    attempt["observations"][0]["failure"] = failure_observation(shared_error())
    attempt["dispatch"] = recovery.dispatch_binding(rid, doc)
    # Rebuild fixture's authentic original event before first compatibility use.
    import json
    with store.connect() as db:
        db.execute("UPDATE events SET data=? WHERE run_id=? AND kind='research.acquisition_failed'",
                   (json.dumps({"episode_id": "legacy", "attempt": 1, "observations": attempt["observations"]}), rid))
    store.checkpoint(rid, PREFIX + "legacy", record)
    own = recovery.refusals(attempt["observations"])
    key = CONSUMED + digest([attempt["dispatch"], attempt["id"], own])
    store.checkpoint(rid, key, {"dispatch": attempt["dispatch"], "acquisition_id": attempt["id"], "question_id": "fixture-original"})
    ledger = reopen(store, rid, recovery, doc)
    assert ledger.previous(URL) is None
    record = ledger.record()
    record["completed"] = {"fixture": True}
    store.checkpoint(rid, PREFIX + "legacy", record)
    store.update(rid, replans=1)
    next_ledger = reopen(store, rid, recovery, doc)
    assert next_ledger.record()["attempts"] == []
    assert next_ledger.previous(URL) is None


@pytest.mark.parametrize("damage", [None, "future-event", "sources", "candidate-hash", "artifact", "owner"])
def test_changed_legacy_sources_require_exact_prior_owned_candidate(tmp_path, damage):
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed",))
    newer = copy.deepcopy(doc.model_dump())
    newer["sources"][0]["claim"] = "A revised original claim"
    ref = recovery.engine.artifacts.put(newer, "provider_research")
    event = {"stage": "provider_research", "operation": "provider_research", "artifact": ref}
    if damage != "future-event":
        store.event(rid, "artifact.created", event)
    attempt = copy.deepcopy(record["attempts"][0])
    attempt.update(id="attempt-2", generation=1, candidate_hash=digest(newer), sources=newer["sources"])
    if damage == "sources":
        attempt["sources"][0]["claim"] = "tampered"
    if damage == "candidate-hash":
        attempt["candidate_hash"] = "0" * 64
    record["attempts"].append(attempt)
    store.event(rid, "research.acquisition_failed", {"episode_id": "legacy", "attempt": 2,
                "observations": attempt["observations"]})
    if damage == "future-event":
        store.event(rid, "artifact.created", event)
    if damage == "artifact":
        (store.root / "artifacts" / ref["hash"][:2] / ref["hash"]).write_text("changed")
    if damage == "owner":
        with store.connect() as db:
            db.execute("DELETE FROM artifact_owners WHERE hash=? AND run_id=?", (ref["hash"], rid))
    store.checkpoint(rid, PREFIX + "legacy", record)
    if damage is not None:
        with pytest.raises(StateConflict):
            documents(store, rid, record)
        return
    ledger = reopen(store, rid, recovery, doc)
    projected = ledger.entry(ledger.record(), URL)["outcomes"]
    assert len(projected) == 2 and projected[1]["sources"] == newer["sources"]
    assert projected[1]["legacy"]["original_source_basis"] == ref
    assert projected[1]["legacy"]["candidate_event"]["seq"] > 0
    assert ledger.cause_count(projected, projected[0]["failure"]) == 2


def test_changed_legacy_projection_is_not_appended_or_double_charged(tmp_path):
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed",))
    record["documents"] = documents(store, rid, record)
    old = record["documents"][URL]["outcomes"][0]
    old["legacy"]["transport_proof"] = "Older projection shape"
    old["id"] = digest(old["legacy"])
    before = copy.deepcopy(record)
    with pytest.raises(StateConflict, match="explicit provenance reconciliation"):
        documents(store, rid, record)
    assert record == before


async def test_complete_legacy_cache_allows_normal_verification_but_not_network(tmp_path, monkeypatch):
    from auto_agents_2.research import ResearchService
    failure = failure_observation(ResearchDestinationRejected("docs.example.test"))
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed", "retrieved"), failure=failure)
    before = copy.deepcopy(record)
    requirement = {"evidence": {"category": "unknown", "error_type": "ConnectError",
                                "connection_context_uncertain": True}}
    identity = digest(["RESEARCH_RESOLVER_UNAVAILABLE", "provider_research", requirement])
    question = store.request(rid, "prerequisite/" + identity + "/0", "Original unresolved prerequisite", "fixture-original")
    fault = {"route": "human", "phase": "provider_research", "reason_code": "RESEARCH_RESOLVER_UNAVAILABLE",
             "human_requirement": requirement}
    store.checkpoint(rid, "recovery/current-fault", fault)
    recovery.prepare_wait(rid)
    assert store.question(question["id"]) == question
    assert store.get(rid, "recovery/current-fault") == fault
    assert store.get(rid, PREFIX + "legacy") == before
    assert store.get(rid, WAIT) is None
    with pytest.raises(StateConflict, match="no verified dispatch"):
        recovery.prepare_wait(rid, network=True)
    ledger = reopen(store, rid, recovery, doc)
    async def forbidden(*args):
        pytest.fail("Cache-only continuation cannot capture any document")
    monkeypatch.setattr(ResearchService, "capture", forbidden)
    service = ResearchService(recovery.engine.artifacts)
    service.ledger = ledger
    result = await service.verify(doc.sources)
    assert result[0]["retrieval"]["acquisition_receipt"]["reused"]
    assert result[0]["retrieval"]["acquisition_receipt"]["original_dispatch"] is None
    assert ledger.cause_count(ledger.entry(ledger.record(), URL)["outcomes"], failure) == 1
    assert store.get(rid, PREFIX + "legacy")["attempts"] == before["attempts"]
    assert store.get(rid, WAIT) is None


@pytest.mark.parametrize("change", ["new-source", "fragment", "empty", "hard", "corrupt"])
def test_legacy_cache_exception_never_admits_missing_or_unsafe_material(tmp_path, change):
    failure = failure_observation(ResearchDestinationRejected("docs.example.test"))
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed", "retrieved"), failure=failure)
    if change == "new-source":
        doc.sources[0].url = "https://new.example.test/spec"
    elif change == "fragment":
        doc.sources[0].url += "#absent-section"
    elif change == "empty":
        doc.sources = []
    elif change == "hard":
        store.checkpoint(rid, HARD, {"operation": "fixture", "error_type": "unverified"})
    else:
        ref = record["attempts"][1]["observations"][0]["retrieval"]["raw"]
        (store.root / "artifacts" / ref["hash"][:2] / ref["hash"]).write_text("changed")
    store.checkpoint(rid, "candidate/provider_research", {"document": doc.model_dump()})
    with pytest.raises(StateConflict):
        recovery.prepare_wait(rid)


async def test_changed_cache_only_candidate_is_not_dispatched_or_charged(tmp_path, monkeypatch):
    from auto_agents_2.research import ResearchService
    failure = failure_observation(ResearchDestinationRejected("docs.example.test"))
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed", "retrieved"), failure=failure)
    recovery.prepare_wait(rid)
    doc.sources[0].url = "https://new.example.test/spec"
    store.checkpoint(rid, "candidate/provider_research", {"document": doc.model_dump()})
    ledger = reopen(store, rid, recovery, doc)
    before = copy.deepcopy(ledger.record())
    async def forbidden(*args):
        pytest.fail("Missing cache cannot open the network")
    monkeypatch.setattr(ResearchService, "capture", forbidden)
    service = ResearchService(recovery.engine.artifacts)
    service.ledger = ledger
    with pytest.raises(StateConflict) as blocked:
        await service.verify(doc.sources)
    assert blocked.value.acquisition_observations[0]["status"] == "not_executed"
    assert blocked.value.acquisition_observations[0]["capture_started"] is False
    assert ledger.record() == before
    blocked.value.acquisition_dispatch = ledger.dispatch
    recovery.observe(rid, doc, blocked.value)
    assert store.get(rid, HARD) is None and store.get(rid, WAIT) is None
    recorded = ledger.record()["attempts"][-1]["observations"][0]
    assert recorded["status"] == "not_executed"
    assert ledger.cause_count(ledger.entry(ledger.record(), URL)["outcomes"], failure) == 1


def test_completed_cache_review_does_not_release_old_network_prerequisite(tmp_path):
    failure = failure_observation(ResearchDestinationRejected("docs.example.test"))
    store, rid, recovery, doc, record = legacy(tmp_path, ("failed", "retrieved"), failure=failure)
    ledger = reopen(store, rid, recovery, doc)
    original = ledger.record()
    original["completed"] = {"fixture": "independent cache review"}
    store.checkpoint(rid, PREFIX + "legacy", original)
    store.update(rid, replans=1)
    doc.sources[0].url = "https://new.example.test/spec"
    store.checkpoint(rid, "candidate/provider_research", {"document": doc.model_dump()})
    following = reopen(store, rid, recovery, doc)
    assert following.record()["attempts"] == []
    with pytest.raises(StateConflict, match="still blocks network") as blocked:
        following.start(doc.sources[0].url)
    assert blocked.value.acquisition_request_not_executed is True
    assert not following.record()["documents"].get(doc.sources[0].url, {}).get("pending")


async def legacy_cache_engine(tmp_path, monkeypatch):
    from test_research_prerequisite_wait import paused_engine

    from auto_agents_2.research import ResearchService
    workflow, agent, rid, state, questions = await paused_engine(tmp_path, monkeypatch, normal=True)
    original_question = copy.deepcopy(questions[0])
    wait = workflow.store.get(rid, WAIT)
    pointer = workflow.store.get(rid, CURRENT)
    record = workflow.store.get(rid, pointer["key"])
    document = workflow.store.get(rid, "candidate/provider_research")["document"]
    # A private fixture with authentic v1-shaped events and a retained capture.
    record["version"] = 1
    record.pop("document_ledger_version", None)
    record.pop("documents", None)
    for attempt in record["attempts"]:
        attempt.pop("dispatch", None)
        attempt.pop("document_protocol", None)
    evidence = capture(workflow.artifacts, document["sources"][0]["url"], "Fixture fact")
    row = {"url": document["sources"][0]["url"], "status": "retrieved",
           "retrieval": {key: value for key, value in evidence.items() if key != "excerpt"}}
    record["attempts"].append({"id": "fixture-retained-capture", "generation": 0,
        "candidate_hash": digest(document), "sources": document["sources"], "observations": [row]})
    workflow.store.event(rid, "research.acquisition_failed", {"episode_id": record["id"],
        "attempt": len(record["attempts"]), "observations": [row]})
    workflow.store.checkpoints(rid, {pointer["key"]: record, WAIT: None})
    before_requests = list(state["requests"])
    async def forbidden(*args):
        pytest.fail("Validated v1 cache must not reacquire or answer a prerequisite")
    monkeypatch.setattr(ResearchService, "capture", forbidden)
    return workflow, agent, rid, state, original_question, wait, pointer, record, before_requests, forbidden


async def test_normal_engine_reviews_legacy_cache_without_answering_old_question(tmp_path, monkeypatch):
    import asyncio

    from test_workflow import engine

    from auto_agents_2.research_acquisition import CONSUMED
    workflow, agent, rid, state, original_question, wait, pointer, record, before_requests, forbidden = await legacy_cache_engine(tmp_path, monkeypatch)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = forbidden
    config_before = restarted.config.path.read_bytes()
    result = await asyncio.wait_for(restarted.start(stop_after="provider_research"), 12)
    assert result["status"] == "PAUSED" and result["stage"] == "provider_research"
    assert result["data"]["pause_reason"] == "requested_stage_boundary"
    assert restarted.config.path.read_bytes() == config_before
    assert agent.research_reviews == 1
    assert workflow.store.get(rid, "provider_research")["verified_sources"]
    assert workflow.store.question(original_question["id"]) == original_question
    assert workflow.store.get(rid, CONSUMED + wait["id"]) is None
    assert workflow.store.get(rid, pointer["key"])["attempts"] == record["attempts"]
    assert state["requests"] == before_requests
    # A second bounded resume reuses the accepted result, without another model.
    assert (await restarted.start(stop_after="provider_research"))["status"] == "PAUSED"
    assert agent.research_reviews == 1
    # The option is not persisted: an ordinary later resume reaches the next stage.
    with pytest.raises(asyncio.CancelledError):
        await restarted.start()
    assert agent.research_reviews == 1


async def test_typed_research_rejection_reuses_completed_review_after_recording_crash(tmp_path, monkeypatch):
    import asyncio

    from authority_review_fixtures import authority_review
    from test_workflow import engine

    from auto_agents_2.faults import InternalWorkflowError
    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    workflow, agent, rid, state, question, wait, pointer, record, requests, forbidden = await legacy_cache_engine(tmp_path, monkeypatch)
    # Unlike a first collection, this v1 failure episode owns recovery dispositions.
    record["collecting"] = False
    document = workflow.store.get(rid, "candidate/provider_research")["document"]
    document["acquisition_resolution"] = {"episode_id": record["id"], "status": "ready",
        "claims": [{"claim_id": row["id"], "disposition": "supported", "source_indexes": [row["index"]],
                    "requirement_ids": [], "reason": "Same retained first-party fixture fact"}
                   for row in record["claims"]], "missing_information": []}
    workflow.store.checkpoints(rid, {pointer["key"]: record, "candidate/provider_research": {"document": document}})
    original_invoke = agent.invoke
    reviews, authors = [], []
    async def typed_rejection(stage, context, schema, work, run_id, operation, fresh=False):
        if stage == "provider_research":
            authors.append(operation)
            return schema.model_validate({**document, "document": document["document"] + "\nCorrected fixture source version."})
        if context.get("review_kind") == "research":
            reviews.append(operation)
            return schema.model_validate(authority_review(context, approved=bool(authors),
                summary="Fixture source version must be corrected from retained evidence").model_dump())
        return await original_invoke(stage, context, schema, work, run_id, operation, fresh=fresh)
    monkeypatch.setattr(agent, "invoke", typed_rejection)
    restarted = engine(workflow.project, agent)
    restarted.question_handler = forbidden
    original_reject = ResearchAcquisitionRecovery.reject_review
    def recording_crash(*args):
        raise TypeError("Fixture crash after a valid typed review was saved")
    monkeypatch.setattr(ResearchAcquisitionRecovery, "reject_review", recording_crash)
    with pytest.raises(InternalWorkflowError):
        await restarted.start(stop_after="provider_research")
    saved = workflow.store.get(rid, "invocation/research-review")
    assert saved["result"]["approved"] is False
    assert len(reviews) == 1
    monkeypatch.setattr(ResearchAcquisitionRecovery, "reject_review", original_reject)
    resumed = engine(workflow.project, agent)
    replan_reasons = []
    def reached_normal_replan(run_id, error):
        replan_reasons.append(error.reason)
        assert error.stage == "provider_research"
        raise asyncio.CancelledError()
    monkeypatch.setattr(resumed, "_controller_replan", reached_normal_replan)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start(stop_after="provider_research")
    rejected = workflow.store.get(rid, pointer["key"])
    assert len(reviews) == 1, "A persisted valid review must not be sent to a model again"
    assert len(rejected.get("rejected_reviews", [])) == 1 and rejected.get("pending"), replan_reasons
    assert rejected["attempts"] == record["attempts"]
    assert workflow.store.question(question["id"]) == question
    assert state["requests"] == requests
    # Continue the full normal decision/permit/archive/author/review handoff.
    following = engine(workflow.project, agent)
    following.question_handler = forbidden
    accepted = await asyncio.wait_for(following.start(stop_after="provider_research"), 20)
    assert accepted["status"] == "PAUSED" and accepted["stage"] == "provider_research"
    assert len(authors) == 1 and len(reviews) == 2
    assert workflow.store.get(rid, "provider_research")["document"].endswith("Corrected fixture source version.")
    assert workflow.store.get(rid, pointer["key"])["attempts"] == record["attempts"]
    assert workflow.store.question(question["id"]) == question and state["requests"] == requests
