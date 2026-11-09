"""Explicit replacement of an unaccepted research proposal, never clearance of a refusal."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import httpx

from .config import ConfigStore
from .errors import AA2Error, ResearchSourceExcluded, http_failure_classification
from .faults import StateConflict
from .research_acquisition import HARD, PREFIX, WAIT, ResearchRecoveryDocument, claim_rows
from .research_contract import validate_research_output
from .safe_files import read_regular
from .state_codec import checkpoint_query_db
from .store import Store
from .util import digest, file_lock

VERSION = "research-source-replacement-v1"
CURRENT = "recovery/research-source-replacement/current"
HISTORY = "recovery/research-source-replacement/history/"
LIMIT = 1024 * 1024


def host(url):
    """Use the HTTP client's canonical hostname, including IDNA and escaped labels."""
    try:
        value = httpx.URL(url)
        if value.scheme not in {"http", "https"} or not value.host or value.username or value.password:
            raise ValueError
        target = value.host.lower().encode("idna").decode().rstrip(".")
        if "%" in target:
            raise ValueError
        return target
    except (ValueError, httpx.InvalidURL, UnicodeError) as exc:
        raise AA2Error("Replacement source must have an unambiguous public HTTP(S) hostname") from exc


def admit_url(url, excluded):
    target = host(url)
    try:
        ipaddress.ip_address(target)
    except ValueError:
        pass
    else:
        raise AA2Error("Replacement sources and redirects require an independent named host, not a literal address")
    if any(target == name or target.endswith("." + name) for name in excluded):
        raise ResearchSourceExcluded(target)


def code_identity():
    root = Path(__file__).resolve().parent
    return digest({p.name: digest(p.read_bytes()) for p in sorted(root.glob("*.py"))})


def counter_floor(records):
    result = {}
    for key, value in records.items():
        if key.startswith("prerequisite/") and type(value) is int:
            result[key] = value
        elif type(value) is dict:
            fields = ("round",) if key.startswith("rounds/") else (
                "failures", "cancellations", "cancellation_resumes") if key.startswith("infra/") else ()
            counts = {name: value[name] for name in fields if type(value.get(name)) is int}
            if counts:
                result[key] = counts
    return result


def counters_preserved(floor, records):
    for key, value in floor.items():
        current = records.get(key)
        if type(value) is int:
            if type(current) is not int or current < value:
                return False
        elif (type(current) is not dict or any(type(current.get(k)) is not int or current[k] < count
                                              for k, count in value.items())):
            return False
    return True


def snapshot(db, rid):
    run = db.execute("SELECT status,stage,data FROM runs WHERE id=?", (rid,)).fetchone()
    if run is None:
        raise AA2Error("Research replacement run does not exist")
    records = {row[0]: json.loads(row[1]) for row in checkpoint_query_db(
        db, "SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=?", (rid,), wire_index=1)}
    questions = {row["id"]: digest(dict(row)) for row in db.execute(
        "SELECT * FROM questions WHERE run_id=? ORDER BY id", (rid,))}
    basis = {"run": digest(tuple(run)), "checkpoints": {k: digest(v) for k, v in records.items()},
             "questions": questions}
    return run, records, basis


def _proposal(value, records, episode):
    if (type(value) is not dict or set(value) != {"proposal", "requirement_assessment"}
            or type(value["requirement_assessment"]) is not list):
        raise AA2Error("Replacement input needs a proposal and a complete original requirement assessment")
    try:
        result = ResearchRecoveryDocument.model_validate(value["proposal"])
    except ValueError as exc:
        raise AA2Error("Replacement must be a complete research recovery proposal, without controller evidence") from exc
    raw = result.model_dump()
    validate_research_output(raw)
    if (not result.summary.strip() or not result.document.strip() or result.questions
            or result.browser_actions or result.design_selection or result.acquisition_resolution.status != "ready"
            or result.acquisition_resolution.missing_information):
        raise AA2Error("Replacement needs a complete ready proposal; unresolved questions or claims stay blocked")
    resolution = result.acquisition_resolution
    rows = {row.claim_id: row for row in resolution.claims}
    wanted = {row["id"] for row in episode["claims"]}
    requirements = {row["id"] for row in records.get("clarify", {}).get("requirements", [])}
    if (resolution.episode_id != episode["id"] or set(rows) != wanted
            or len(rows) != len(resolution.claims) or not requirements):
        raise AA2Error("Replacement must map every original claim exactly once against the original requirements")
    old = records["candidate/provider_research"]["document"].get("acquisition_resolution", {})
    previous = {row["claim_id"]: set(row["requirement_ids"]) for row in old.get("claims", [])}
    for row in rows.values():
        indexes = row.source_indexes
        if (not row.requirement_ids or len(set(row.requirement_ids)) != len(row.requirement_ids)
                or not set(row.requirement_ids) <= requirements
                or len(set(indexes)) != len(indexes)
                or any(index < 0 or index >= len(raw["sources"]) for index in indexes)
                or (row.disposition == "supported") != bool(indexes)
                or row.disposition == "blocked"
                or row.claim_id in previous and set(row.requirement_ids) != previous[row.claim_id]):
            raise AA2Error("Replacement cannot lose the original requirement mapping or invent source evidence")
    assessment = value["requirement_assessment"]
    seen = set()
    for row in assessment:
        if (type(row) is not dict or set(row) != {"requirement_id", "claim_ids", "reason"}
                or not isinstance(row["requirement_id"], str) or row["requirement_id"] not in requirements
                or row["requirement_id"] in seen or type(row["claim_ids"]) is not list
                or any(type(item) is not str for item in row["claim_ids"])
                or len(row["claim_ids"]) != len(set(row["claim_ids"]))
                or not set(row["claim_ids"]) <= wanted
                or not isinstance(row["reason"], str) or not row["reason"].strip()
                or set(row["claim_ids"]) != {claim.claim_id for claim in rows.values()
                                           if row["requirement_id"] in claim.requirement_ids}):
            raise AA2Error("Every original requirement needs an exact claim mapping or an explicit no-research reason")
        seen.add(row["requirement_id"])
    if seen != requirements:
        raise AA2Error("Source replacement cannot omit an original requirement")
    return raw, assessment


def inspect(project, run_id, proposal, authorization_reference):
    """Read current facts only. In particular, never call the production prepare() path."""
    if not isinstance(authorization_reference, str) or not authorization_reference.strip():
        raise AA2Error("Explicit source-replacement authorization reference is required")
    store = Store(project, readonly=True)
    with store.connect() as db:
        run, records, basis = snapshot(db, run_id)
        audits = db.execute("SELECT 1 FROM events WHERE run_id=? AND kind=? LIMIT 1",
                            (run_id, "research.acquisition_recording_failed")).fetchone()
    if run[0] not in {"WAITING_USER", "PAUSED"} or run[1] != "provider_research":
        raise AA2Error("Source replacement requires the stopped original research stage")
    if (audits or records.get(HARD) or records.get("provider_research")
            or any(records.get(k) for k in ("replanning/pending", "recovery/active", "control/cancel"))):
        raise AA2Error("A recording hold, accepted research or another control transition blocks replacement")
    pointer = records.get("recovery/research-acquisition/current", {})
    episode = records.get(pointer.get("key"), {})
    candidate = records.get("candidate/provider_research", {}).get("document")
    if (not candidate or episode.get("completed") or episode.get("pending")
            or episode.get("version") != 1 or pointer.get("key") != PREFIX + episode.get("id", "")
            or pointer.get("id") != episode.get("id") or not episode.get("attempts")
            or episode.get("binding") != {n: digest(records.get(n)) for n in ("capture", "clarify")}):
        raise AA2Error("Source replacement requires the exact intact active acquisition episode")
    ref = episode["original_candidate"]
    original_bytes = read_regular(store.root / "artifacts" / ref["hash"][:2] / ref["hash"], LIMIT)
    if digest(original_bytes) != ref["hash"] or len(original_bytes) != ref["bytes"]:
        raise AA2Error("Original research artifact integrity changed")
    original = json.loads(original_bytes)
    if episode.get("claims") != claim_rows(original):
        raise AA2Error("Original claim identities changed")
    raw, assessment = _proposal(proposal, records, episode)
    previous = records.get(CURRENT) or {}
    if previous:
        preservation(previous, records, run_id)
    excluded = set(previous.get("request", {}).get("excluded_hosts", []))
    for document in [original, candidate, *episode["attempts"]]:
        excluded.update(host(row["url"]) for row in document.get("sources", []))
    # Actual redirected refusal destinations may differ from the original source.
    # Resolver/shared-route failures remain separate prerequisites, never repaired by a new source.
    holds = []

    def preserve_failure(failure):
        if failure.get("automatic"):
            return
        diagnostic = failure.get("diagnostic", {})
        if diagnostic.get("hostname"):
            excluded.add(host("https://" + diagnostic["hostname"]))
        if diagnostic.get("source_origin"):
            excluded.add(host(diagnostic["source_origin"]))
        phase = diagnostic.get("connection_phase")
        shared = (diagnostic.get("operation") == "research_resolver" or phase == "public_connect"
                  or diagnostic.get("category") == "proxy"
                  or diagnostic.get("error_type") == "ConnectError" and phase != "origin_tls")
        if shared:
            holds.append({"failure_hash": digest(failure), "code": failure["code"]})

    for attempt in episode["attempts"]:
        for observation in attempt["observations"]:
            final_url = observation.get("retrieval", {}).get("final_url")
            if final_url:
                excluded.add(host(final_url))
            for failure in ([observation["failure"]] if observation.get("failure") else []) + observation.get("observed_failures", []):
                preserve_failure(failure)
    fault = records.get("recovery/current-fault") or {}
    diagnostic = fault.get("human_requirement", {}).get("evidence", {})
    if diagnostic:
        _, automatic = http_failure_classification(
            diagnostic, document=fault.get("reason_code") == "DOCUMENT_ACQUISITION_UNAVAILABLE")
        preserve_failure({"code": fault.get("reason_code", "UNCLASSIFIED_ACQUISITION_ERROR"),
                          "automatic": automatic, "diagnostic": diagnostic})
    for source in raw["sources"]:
        from .research import public_url
        public_url(source["url"])
        admit_url(source["url"], excluded)
    return {"version": VERSION, "run_id": run_id,
            "generation": json.loads(run[2]).get("replans", 0),
            "authorization_reference": authorization_reference, "proposal": raw,
            "requirement_assessment": assessment,
            "basis": basis, "episode_key": pointer["key"], "episode_hash": digest(episode),
            "binding": episode["binding"], "excluded_hosts": sorted(excluded),
            "network_holds": sorted({digest(v): v for v in holds}.values(), key=digest),
            "counter_floor": counter_floor(records), "code_identity": code_identity(),
            "config_hash": digest(ConfigStore(project).load().model_dump(mode="json"))}


def prepare(project, *, run_id, proposal, authorization_reference):
    try:
        value = json.loads(read_regular(Path(proposal).absolute(), LIMIT))
        return inspect(project, run_id, value, authorization_reference)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AA2Error("Invalid or incomplete research source replacement input") from exc


def submit(project, path, expected_sha256, *, confirm_new_sources=False):
    if not confirm_new_sources:
        raise AA2Error("Explicit confirmation of the exact new-source proposal is required")
    try:
        raw = read_regular(Path(path).absolute(), LIMIT)
        request = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise AA2Error("Replacement request must be a bounded regular JSON file") from exc
    fields = {"version", "run_id", "generation", "authorization_reference", "proposal", "requirement_assessment",
              "basis", "episode_key", "episode_hash", "binding", "excluded_hosts", "network_holds", "counter_floor", "code_identity", "config_hash"}
    if (type(request) is not dict or set(request) != fields or type(request.get("run_id")) is not str
            or digest(raw) != expected_sha256 or request.get("version") != VERSION):
        raise AA2Error("Replacement request bytes or version differ from the reviewed request")
    store = Store(project)
    with file_lock(store.root / "controller.lock", blocking=False):
        existing = store.get(request["run_id"], CURRENT)
        if existing and existing.get("request") == request:
            current(store, request["run_id"])
            return {"request_hash": digest(request), "replayed": True, "phase": existing["phase"]}
        checked = inspect(project, request["run_id"],
                          {"proposal": request["proposal"], "requirement_assessment": request["requirement_assessment"]},
                          request["authorization_reference"])
        if checked != request:
            raise StateConflict("Research replacement evidence, configuration or code changed after review")
        store.register_research_replacement(request)
    return {"request_hash": digest(request), "replayed": False, "phase": "pending_review"}


def preservation(record, records, rid):
    """A new authorization cannot absorb a lost archive or lower a previous consumption floor."""
    request = record.get("request", {})
    key = HISTORY + digest(request)
    archive = records.get(key, {})
    original = request.get("basis", {})
    old_checkpoints = original.get("checkpoints", {})
    old_questions = archive.get("original_questions", [])
    acceptance_fields = {"phase", "review_key", "review_receipt_hash", "verified_sources", "retrieved_sources_hash"}
    if (record.get("version") != VERSION or record.get("request_hash") != digest(request)
            or request.get("run_id") != rid or archive.get("request") != request
            or {k: v for k, v in record.items() if k not in acceptance_fields}
               != {k: v for k, v in archive.get("registration", {}).items() if k != "phase"}
            or digest(archive.get("original_candidate")) != old_checkpoints.get("candidate/provider_research")
            or digest(archive.get("original_episode")) != request.get("episode_hash")
            or digest(archive.get("original_fault")) != old_checkpoints.get("recovery/current-fault", digest(None))
            or digest(archive.get("original_wait")) != old_checkpoints.get(WAIT, digest(None))
            or len(old_questions) != len(original.get("questions", {}))
            or {row["id"]: digest(row) for row in old_questions} != original.get("questions")
            or digest(archive.get("previous_registration")) != old_checkpoints.get(CURRENT, digest(None))
            or any(digest(records.get(name)) != sha for name, sha in old_checkpoints.items() if name.startswith(HISTORY))
            or not counters_preserved(request.get("counter_floor", {}), records)):
        raise StateConflict("Research replacement lost its exact registration and original archive")
    return request


def current(store, rid, *, db=None):
    """Validate only the exact new operation; old failures are never retagged or cleared."""
    if db is None:
        with store.connect() as connection:
            return current(store, rid, db=connection)
    if not db.execute("SELECT 1 FROM checkpoint_records WHERE run_id=? AND key=?", (rid, CURRENT)).fetchone():
        return None
    _, records, basis = snapshot(db, rid)
    record = records.get(CURRENT)
    request = preservation(record, records, rid)
    if record.get("phase") == "accepted":
        proof = records.get(record.get("review_key"), {})
        accepted = records.get("provider_research", {})
        if (proof.get("owned_candidate") != request["proposal"]
                or not proof.get("review", {}).get("approved") or digest(proof) != record.get("review_receipt_hash")
                or proof.get("basis", {}).get("observations", {}).get("retrieved_sources") != record.get("retrieved_sources_hash")
                or accepted.get("verified_sources", []) != record.get("verified_sources")
                or {k: v for k, v in accepted.items() if k != "verified_sources"} != request["proposal"]):
            raise StateConflict("Accepted replacement lost its owned review and collected research")
        return record
    run = db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()
    if (record.get("phase") != "pending_review" or json.loads(run[0]).get("replans", 0) != request["generation"]
            or {n: digest(records.get(n)) for n in ("capture", "clarify")} != request["binding"]
            or digest(records.get(request["episode_key"])) != request["episode_hash"]
            or (records.get("candidate/provider_research") or {}).get("document") != request["proposal"]
            or records.get(HARD) or records.get("provider_research")
            or db.execute("SELECT 1 FROM events WHERE run_id=? AND kind=? LIMIT 1",
                          (rid, "research.acquisition_recording_failed")).fetchone()
            or digest(records.get(WAIT)) != request["basis"]["checkpoints"].get(WAIT, digest(None))
            or digest(records.get("recovery/current-fault", {})) != record["original_fault_hash"]
            or any(basis["questions"].get(q) != expected
                                      for q, expected in request["basis"]["questions"].items())
            or digest(ConfigStore(store.root.parent).load().model_dump(mode="json")) != request["config_hash"]
            or code_identity() != request["code_identity"]):
        raise StateConflict("Research replacement inputs changed; preserve the old boundary and prepare a new request")
    return record


def covers_attempt(record, attempt):
    request = record["request"]
    return digest(attempt) in record["original_attempt_hashes"] and all(
        host(row["url"]) in request["excluded_hosts"] for row in attempt["observations"])


def guard_network(store, rid):
    record = current(store, rid)
    if record and record["phase"] == "pending_review" and record["request"]["network_holds"]:
        raise StateConflict("Source replacement does not resolve the recorded shared resolver/proxy prerequisite; "
                            "verify that prerequisite through its original owner before new acquisition")
    return record["request"]["excluded_hosts"] if record else []


def guard_provider(store, rid):
    record = current(store, rid)
    if record:
        raise StateConflict("This native provider has no verified controller-evidence-only capability; "
                            "no provider call was made for the source replacement or its downstream stages")


def acceptance(store, rid, document, review, retrieved):
    record = current(store, rid)
    if not record or record["phase"] != "pending_review":
        return {}
    guard_network(store, rid)
    if (document != record["request"]["proposal"] or not review.approved or retrieved is None
            or len(retrieved) != len(document["sources"]) or any(
                any(row.get(k) != value for k, value in source.items())
                for source, row in zip(document["sources"], retrieved))):
        raise StateConflict("Replacement acceptance must bind the exact independently reviewed new proposal")
    key = "authority/reviews/" + digest(review.model_dump())
    proof = store.get(rid, key)
    if (not proof or proof.get("owned_candidate") != document or proof.get("review") != review.model_dump()
            or proof.get("basis", {}).get("observations", {}).get("retrieved_sources") != digest(retrieved)):
        raise StateConflict("Replacement acceptance needs its exact owned independent review receipt")
    return {CURRENT: {**record, "phase": "accepted", "review_key": key, "review_receipt_hash": digest(proof),
                      "retrieved_sources_hash": digest(retrieved),
                      "verified_sources": [{k: v for k, v in row.items() if k != "excerpt"} for row in retrieved]}}
