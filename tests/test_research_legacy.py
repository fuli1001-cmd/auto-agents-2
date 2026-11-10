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


def legacy(tmp_path, statuses=("retrieved",)):
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
            observation["failure"] = failure_observation(DocumentAcquisitionTimeout("fixture", 30, source_url=URL))
        attempt = {"id": "attempt-" + str(ordinal), "generation": 0,
                   "candidate_hash": digest(doc.model_dump()), "sources": doc.model_dump()["sources"],
                   "observations": [observation]}
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
