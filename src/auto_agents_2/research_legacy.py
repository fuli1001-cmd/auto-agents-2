"""Validate pre-ledger observations without inventing a request or an approval."""
from __future__ import annotations

import copy
import json
from urllib.parse import urldefrag

from .faults import StateConflict
from .util import digest

TAG = "research-legacy-observation-v1"


def _rows(store, rid, record):
    """One authentic observation per attempt; repeated event presentation is not work."""
    from .research_acquisition import claim_rows
    from .research_documents import _artifact
    with store.connect() as db:
        ref = record.get("original_candidate", {})
        original = json.loads(_artifact(store, db, rid, ref, "research-acquisition-candidate"))
        if record.get("claims") != claim_rows(original):
            raise StateConflict("Legacy research original claim evidence changed")
        events = list(db.execute(
            "SELECT seq,time,kind,data FROM events WHERE run_id=? AND kind=? ORDER BY seq",
            (rid, "research.acquisition_failed")))
    seen = set()
    for ordinal, attempt in enumerate(record.get("attempts", []), 1):
        if attempt.get("document_protocol") == 1:
            continue
        if not attempt.get("id") or attempt["id"] in seen:
            raise StateConflict("Legacy research attempt identity is absent or duplicated")
        seen.add(attempt["id"])
        if attempt.get("sources") != original.get("sources"):
            raise StateConflict("Legacy research changed sources need their exact archived candidate evidence")
        if attempt.get("dispatch") is not None:
            stamp = attempt["dispatch"]
            if (stamp.get("run_id") != rid or stamp.get("binding") != record["binding"]
                    or stamp.get("generation") != attempt.get("generation")
                    or stamp.get("candidate_hash") != attempt.get("candidate_hash")
                    or stamp.get("sources_hash") != digest(attempt["sources"])):
                raise StateConflict("Legacy research dispatch and original attempt disagree")
        matches = []
        for event in events:
            data = json.loads(event["data"])
            if data.get("episode_id") == record["id"] and data.get("attempt") == ordinal:
                if data.get("observations") != attempt.get("observations"):
                    raise StateConflict("Legacy research event and observation disagree")
                matches.append(event)
        if not matches:
            raise StateConflict("Legacy research has no original observation event")
        event = matches[0]
        for index, observation in enumerate(attempt["observations"]):
            url = observation.get("url")
            sources = [row for row in attempt.get("sources", [])
                       if urldefrag(row.get("url", ""))[0] == url]
            if not url or not sources or observation.get("status") not in {
                    "retrieved", "failed", "cancelled", "not_executed"}:
                raise StateConflict("Legacy research source or outcome is invalid")
            proof = {"tag": TAG, "run_id": rid, "episode_id": record["id"],
                     "attempt_ordinal": ordinal, "attempt_id": attempt["id"],
                     "attempt_hash": digest(attempt), "observation_index": index,
                     "original_source_basis": record["original_candidate"],
                     "event_seq": event["seq"], "event_hash": digest(dict(event)),
                     "original_dispatch": attempt.get("dispatch"),
                     "http_status": None,
                     "transport_proof": "Historical observation; TLS, HTTP status and outbound route not established"}
            outcome = {"id": digest(proof), "episode_id": record["id"],
                       "legacy": proof, "sources": sources, **copy.deepcopy(observation)}
            if observation["status"] == "retrieved":
                if observation.get("failure") or observation.get("observed_failures"):
                    raise StateConflict("Legacy successful observation contains ambiguous failures")
                if observation.get("retrieval", {}).get("captured_at", float("inf")) > event["time"]:
                    raise StateConflict("Legacy research event predates its capture")
            if observation["status"] == "failed":
                from .research_documents import stable_cause
                outcome["cause"] = stable_cause(observation["failure"])
            yield url, outcome


def validate(store, rid, outcome):
    from .research_acquisition import PREFIX
    from .research_documents import validate_capture
    proof = outcome.get("legacy", {})
    if proof.get("tag") != TAG or proof.get("run_id") != rid:
        raise StateConflict("Legacy research provenance tag or owner changed")
    original = store.get(rid, PREFIX + proof.get("episode_id", ""))
    if not original or original.get("id") != proof.get("episode_id"):
        raise StateConflict("Legacy research episode is absent")
    matching = next((row for _, row in _rows(store, rid, original)
                     if row["id"] == outcome.get("id")), None)
    if matching != outcome:
        raise StateConflict("Legacy research provenance changed")
    if outcome["status"] == "retrieved":
        validate_capture(store, rid, outcome["url"], outcome["retrieval"])


def documents(store, rid, record):
    """Pure compatibility projection; original attempts, waits and events stay intact."""
    from .research_documents import validate_capture
    result = copy.deepcopy(record.get("documents", {}))
    for url, outcome in _rows(store, rid, record):
        entry = result.setdefault(url, {"url": url, "outcomes": []})
        previous = next((row for row in entry["outcomes"] if row["id"] == outcome["id"]), None)
        if previous is not None:
            if previous != outcome:
                raise StateConflict("Legacy research projection changed")
            continue
        if outcome["status"] == "retrieved":
            validate_capture(store, rid, url, outcome["retrieval"])
        entry["outcomes"].append(outcome)
    return result
