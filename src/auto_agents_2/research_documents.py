"""Run-owned document receipts inside the existing acquisition episode.

The ledger records collection, not acceptance. It never imports historical batches
or clears a refusal. A pending dispatch with no durable outcome is a hard boundary.
"""
from __future__ import annotations

import copy
import json
import math
import re
from urllib.parse import urldefrag, urlsplit

from .errors import AA2Error
from .faults import StateConflict
from .safe_files import read_regular
from .util import digest, identifier

MAX_CAUSE_ATTEMPTS = 3  # Initial capture plus at most two retries of one stable cause.
REF_KINDS = {"raw": "research_response", "artifact": "research_extracted",
             "manifest": "research_manifest", "section_index": "research_section_index"}
CAUSE_FIELDS = ("category", "error_type", "connection_phase", "connection_scope",
                "http_status", "proxy_status", "os_errno", "ssl_verify_code",
                "connection_context_uncertain", "resolution")


def stable_cause(failure):
    diagnostic = failure["diagnostic"]
    facts = {key: diagnostic[key] for key in CAUSE_FIELDS if key in diagnostic}
    rows = [{key: row[key] for key in CAUSE_FIELDS if key in row}
            for row in diagnostic.get("connection_failures", [])]
    if rows:
        facts["connection_failures"] = sorted({digest(row): row for row in rows}.values(), key=digest)
    return {"code": failure["code"], "classification": failure["classification"], "facts": facts}


def retryable(failure):
    # Recovery classification is not permission to redownload 404/410 or refusals.
    return (failure["automatic"] and failure["classification"] == "network"
            and failure["diagnostic"].get("http_status") not in {404, 410}
            and not failure["diagnostic"].get("recording_failure"))


def shared_boundary(failure):
    diagnostic = failure["diagnostic"]
    return (not failure["automatic"] and (
        failure["code"] == "RESEARCH_RESOLVER_UNAVAILABLE"
        or diagnostic.get("connection_scope") == "shared_proxy"))


def denied_hosts(url, failure):
    diagnostic = failure["diagnostic"]
    local_dns = (failure["code"] == "RESEARCH_DESTINATION_REJECTED"
                 and diagnostic.get("operation") == "research_connect"
                 and diagnostic.get("resolution") in {"non_public", "empty"})
    if local_dns or shared_boundary(failure) or failure["classification"] not in {"security", "permission"}:
        return []
    diagnostic = failure["diagnostic"]
    values = [url, diagnostic.get("source_origin", "")]
    hosts = {(urlsplit(value).hostname or "").rstrip(".").lower() for value in values}
    if diagnostic.get("hostname"):
        hosts.add(diagnostic["hostname"].rstrip(".").lower())
    return sorted(hosts - {""})


class RetainedDocumentFailure(AA2Error):
    """A previous real outcome, explicitly not a new HTTP request or response."""
    def __init__(self, outcome, failure=None):
        self.outcome = copy.deepcopy(outcome)
        self.observation = copy.deepcopy(failure if failure is not None else outcome["failure"])
        self.code = self.observation["code"]
        self.evidence = self.observation["diagnostic"]
        self.observed_failures = copy.deepcopy(outcome.get("observed_failures", []))
        super().__init__("Research document was not requested again: its retained cause is terminal or exhausted")


def _get(db, rid, key):
    from .state_codec import checkpoint_query_db
    row = checkpoint_query_db(db,
        "SELECT slot_order AS data FROM checkpoint_records WHERE run_id=? AND key=?",
        (rid, key), wire_index=0).fetchone()
    return json.loads(row[0]) if row else None


def commit(store, rid, dispatch, expected, updates, event):
    """Exact episode/candidate/requirement CAS, including the mandatory event."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        candidate = (_get(db, rid, "candidate/provider_research") or {}).get("document")
        run = db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()
        if (not run or dispatch["run_id"] != rid
                or json.loads(run[0]).get("replans", 0) != dispatch["generation"]
                or {name: digest(_get(db, rid, name)) for name in ("capture", "clarify")} != dispatch["binding"]
                or digest(candidate) != dispatch["candidate_hash"]
                or any(_get(db, rid, key) != value for key, value in expected.items())):
            raise StateConflict("Research document ledger lost its exact dispatch or episode")
        for key, value in updates.items():
            store._put(db, rid, key, value)
            store._event(db, rid, "checkpoint.saved", {"key": key})
        store._event(db, rid, *event)


def _artifact(store, db, rid, ref, kind):
    limit = 4 * 1024**2 if kind == "research_response" else 32 * 1024**2
    if (type(ref) is not dict or set(ref) != {"hash", "kind", "bytes"}
            or ref.get("kind") != kind or type(ref.get("bytes")) is not int
            or not 0 <= ref["bytes"] <= limit
            or not isinstance(ref.get("hash"), str) or not re.fullmatch("[0-9a-f]{64}", ref["hash"])):
        raise StateConflict("Research document has an invalid artifact reference")
    sha = ref["hash"]
    if (not db.execute("SELECT 1 FROM artifact_owners WHERE hash=? AND run_id=?", (sha, rid)).fetchone()
            or not db.execute("SELECT 1 FROM artifact_kinds WHERE hash=? AND kind=?", (sha, kind)).fetchone()):
        raise StateConflict("Research document lacks its run owner or registered kind")
    try:
        raw = read_regular(store.root / "artifacts" / sha[:2] / sha, limit)
    except (OSError, ValueError) as exc:
        raise StateConflict("Research document artifact is missing or unsafe") from exc
    if len(raw) != ref["bytes"] or digest(raw) != sha:
        raise StateConflict("Research document artifact integrity changed")
    return raw


def validate_capture(store, rid, url, retrieval):
    from .research_sections import extract_sections
    required = {"source", "final_url", "mime_type", "encoding", "captured_at", "extractor_version",
                "raw", "artifact", "hash", "section_index", "manifest"}
    if (not isinstance(retrieval, dict)
            or set(retrieval) not in (required, required | {"network_policy"})
            or retrieval["source"] != url or retrieval["extractor_version"] != "structured-text-v4"
            or type(retrieval["captured_at"]) not in (int, float)
            or not math.isfinite(retrieval["captured_at"]) or retrieval["captured_at"] <= 0):
        raise StateConflict("Research document source, manifest or extractor is invalid")
    with store.connect() as db:
        bodies = {key: _artifact(store, db, rid, retrieval[key], kind) for key, kind in REF_KINDS.items()}
        try:
            manifest = json.loads(bodies["manifest"])
            index = json.loads(bodies["section_index"])
        except (ValueError, UnicodeError) as exc:
            raise StateConflict("Research document manifest or index is not valid JSON") from exc
        metadata = {key: value for key, value in retrieval.items() if key not in {"manifest", "network_policy"}}
        if manifest != metadata or retrieval["hash"] != retrieval["artifact"]["hash"]:
            raise StateConflict("Research document manifest and receipt disagree")
        for key in ("raw", "artifact", "section_index"):
            if not db.execute("SELECT 1 FROM artifact_edges WHERE parent=? AND child=?",
                              (retrieval["manifest"]["hash"], retrieval[key]["hash"])).fetchone():
                raise StateConflict("Research document manifest dependency edge is missing")
    try:
        text, encoding, sections = extract_sections(bodies["raw"], retrieval["mime_type"],
                                                   source_url=retrieval["final_url"])
    except Exception as exc:
        raise StateConflict("Research document extraction cannot be reproduced") from exc
    if text.encode() != bodies["artifact"] or encoding != retrieval["encoding"] or sections != index:
        raise StateConflict("Research document full text or section index changed")
    return text


class ResearchDocumentLedger:
    def __init__(self, recovery, rid, key, dispatch):
        self.recovery, self.store = recovery, recovery.store
        self.rid, self.key, self.dispatch = rid, key, dispatch

    def record(self):
        from .research_acquisition import CURRENT
        pointer = self.store.get(self.rid, CURRENT)
        record = self.store.get(self.rid, self.key)
        if (not record or pointer != {"id": record["id"], "key": self.key}
                or record.get("run_id") != self.rid or record.get("document_ledger_version") != 1
                or not isinstance(record.get("documents"), dict)):
            raise StateConflict("Research document ledger owner or episode changed")
        return record

    def save(self, record, updated, kind, outcome):
        from .research_acquisition import CURRENT
        commit(self.store, self.rid, self.dispatch,
               {CURRENT: {"id": record["id"], "key": self.key}, self.key: record},
               {self.key: updated}, (kind, {**outcome, "run_id": self.rid, "episode_id": record["id"]}))

    def entry(self, record, url, *, allow_pending=False):
        entry = record["documents"].get(url, {"url": url, "outcomes": []})
        if (not isinstance(entry, dict) or entry.get("url") != url
                or not isinstance(entry.get("outcomes"), list)):
            raise StateConflict("Research document outcome history is invalid")
        for outcome in entry["outcomes"]:
            if "legacy" in outcome:
                if outcome.get("url") != url or any(urldefrag(row.get("url", ""))[0] != url
                                                     for row in outcome.get("sources", [])):
                    raise StateConflict("Legacy research outcome belongs to a different document")
                from .research_legacy import validate
                validate(self.store, self.rid, outcome)
                continue
            stamp = outcome.get("dispatch", {})
            if (stamp.get("run_id") != self.rid or url not in stamp.get("source_urls", [])
                    or not outcome.get("id") or outcome.get("status") not in {"retrieved", "failed", "cancelled", "not_executed"}
                    or not outcome.get("sources")
                    or any(urldefrag(row["url"])[0] != url for row in outcome["sources"])):
                raise StateConflict("Research document outcome lost its source/dispatch provenance")
            with self.store.connect() as db:
                try:
                    candidate = json.loads(_artifact(self.store, db, self.rid, outcome.get("candidate"),
                                                     "research-acquisition-dispatch"))
                except (ValueError, UnicodeError) as exc:
                    raise StateConflict("Research document original dispatch candidate is invalid") from exc
                if (digest(candidate) != stamp.get("candidate_hash")
                        or digest(candidate.get("sources", [])) != stamp.get("sources_hash")
                        or outcome["sources"] != [row for row in candidate["sources"]
                                                  if urldefrag(row["url"])[0] == url]):
                    raise StateConflict("Research document original claim/version provenance changed")
                events = db.execute("SELECT data FROM events WHERE run_id=? AND kind=?",
                                    (self.rid, "research.document_finished"))
                def matches(row):
                    event = json.loads(row[0])
                    if "outcome_hash" in event:
                        return (event.get("run_id") == self.rid and outcome.get("episode_id") is not None
                                and event.get("episode_id") == outcome["episode_id"]
                                and event.get("operation_id") == outcome["id"]
                                and event["outcome_hash"] == digest(outcome))
                    return event.get("outcome") == outcome  # Existing events remain strictly bound, never rewritten.
                if not any(matches(row) for row in events):
                    raise StateConflict("Research document outcome lost its original mandatory event")
            if outcome["status"] == "failed" and outcome.get("cause") != stable_cause(outcome["failure"]):
                raise StateConflict("Research document stable-cause evidence changed")
        if entry.get("pending") and not allow_pending:
            raise StateConflict("Research document has a dispatched operation without a durable outcome")
        return entry

    def excluded_hosts(self):
        record = self.record()
        result = set()
        for url in record["documents"]:
            for row in self.entry(record, url, allow_pending=True)["outcomes"]:
                for failure in self.failures(row):
                    result.update(denied_hosts(url, failure))
        return tuple(sorted(result))

    def excluded_addresses(self):
        """Observed numeric routes of target refusals, never inferred old DNS."""
        import ipaddress

        record = self.record()
        result = set()
        for url in record["documents"]:
            for outcome in self.entry(record, url, allow_pending=True)["outcomes"]:
                failures = [row for row in self.failures(outcome)
                            if row["classification"] in {"security", "permission"} and not shared_boundary(row)]
                if not failures:
                    continue
                hosts = {urlsplit(url).hostname}
                hosts.update(row["diagnostic"].get("hostname") for row in failures)
                for host in hosts - {None}:
                    try:
                        result.add(str(ipaddress.ip_address(host)))
                    except ValueError:
                        pass
                for binding in outcome.get("destination_bindings", []):
                    for value in binding["addresses"]:
                        try:
                            result.add(str(ipaddress.ip_address(value)))
                        except ValueError as error:
                            raise StateConflict("Research document has invalid recorded numeric destination evidence") from error
        return tuple(sorted(result))

    def previous(self, url, *, document_authorizations=None):
        from .research_destinations import matching_document_grant
        record = self.record()
        outcomes = self.entry(record, url)["outcomes"]
        successful = next((row for row in reversed(outcomes) if row["status"] == "retrieved"), None)
        if successful is not None:
            text = validate_capture(self.store, self.rid, url, successful["retrieval"])
            return self.evidence(successful, text, reused=True)
        observed = [(row, failure) for row in outcomes for failure in self.failures(row)]
        if observed:
            last, failure = observed[-1]
            # A consumed prerequisite covers only its exact old refusal.
            # It never skips later failures or resets any cause's attempt count.
            for row, fact in reversed(observed):
                if fact["automatic"]:
                    continue
                if shared_boundary(fact) and self.consumed_shared_wait(record, url, row, fact):
                    continue
                if matching_document_grant(fact, document_authorizations) is not None:
                    continue
                raise RetainedDocumentFailure(row, fact)
            if not failure["automatic"]:
                return None  # Only an exact consumed shared or owner-granted local gap remains.
            count = self.cause_count(outcomes, failure)
            if not retryable(failure) or count >= MAX_CAUSE_ATTEMPTS:
                raise RetainedDocumentFailure(last, failure)
        return None

    @staticmethod
    def failures(outcome):
        rows = ([outcome["failure"]] if outcome["status"] == "failed"
                and outcome.get("request_executed") is not False else []) + outcome.get("observed_failures", [])
        return list({digest(stable_cause(row)): row for row in rows}.values())

    @classmethod
    def cause_count(cls, outcomes, failure):
        cause = stable_cause(failure)
        return sum(any(stable_cause(row) == cause for row in cls.failures(outcome)) for outcome in outcomes)

    def consumed_shared_wait(self, record, url, outcome, failure):
        from .research_acquisition import CONSUMED, PREFIX
        if "legacy" in outcome:
            record = self.store.get(self.rid, PREFIX + outcome["legacy"]["episode_id"])
            if not record:
                raise StateConflict("Legacy prerequisite lost its original episode")
        for attempt in record["attempts"]:
            own = self.recovery.refusals(attempt["observations"], shared_only=attempt.get("document_protocol") == 1)
            legacy = outcome.get("legacy", {})
            matching = any(row["url"] == url and (row.get("document_operation_id") == outcome["id"]
                           or legacy.get("attempt_id") == attempt["id"]
                           and index == legacy.get("observation_index"))
                           and failure in ([row["failure"]] if row["status"] == "failed" else [])
                               + row.get("observed_failures", []) for index, row in enumerate(attempt["observations"]))
            if not matching or not own or not attempt.get("dispatch"):
                continue
            identity = digest([attempt["dispatch"], attempt["id"], own])
            receipt = self.store.get(self.rid, CONSUMED + identity)
            if (receipt and receipt.get("dispatch") == attempt["dispatch"]
                    and receipt.get("acquisition_id") == attempt["id"] and receipt.get("question_id")):
                return True
        return False

    def start(self, url):
        record = self.record()
        entry = self.entry(record, url)
        candidate = self.store.get(self.rid, "candidate/provider_research")["document"]
        dispatches = [row for row in record["dispatches"] if row["stamp"] == self.dispatch]
        if not dispatches:
            raise StateConflict("Research document dispatch registration is absent")
        pending = {"id": identifier("research-document"), "episode_id": record["id"], "dispatch": self.dispatch,
                   "candidate": dispatches[-1]["candidate"],
                   "sources": [row for row in candidate["sources"] if urldefrag(row["url"])[0] == url]}
        updated = copy.deepcopy(record)
        updated["documents"][url] = {**entry, "pending": pending}
        self.save(record, updated, "research.document_started", {"url": url, "operation_id": pending["id"]})
        return pending

    def finish(self, url, pending, *, evidence=None, error=None):
        from .research_acquisition import failure_observation
        record = self.record()
        entry = record["documents"].get(url, {})
        if entry.get("pending") != pending:
            raise StateConflict("Research document completion lost its original dispatch")
        outcome = {**pending, "observed_failures": getattr(error, "observed_failures", []) if error else []}
        if getattr(error, "document_destination_bindings", None) is not None:
            outcome["destination_bindings"] = copy.deepcopy(error.document_destination_bindings)
        if evidence is not None:
            retrieval = {key: value for key, value in evidence.items() if key != "excerpt"}
            text = validate_capture(self.store, self.rid, url, retrieval)
            if evidence.get("excerpt") != text:
                raise StateConflict("Research document inline body is incomplete")
            outcome.update(status="retrieved", retrieval=retrieval)
        elif getattr(error, "code", None) == "RESEARCH_REQUEST_NOT_EXECUTED":
            outcome.update(status="not_executed", boundary=error.evidence)
        elif isinstance(error, Exception):
            failure = failure_observation(error)
            outcome.update(status="failed", failure=failure, cause=stable_cause(failure),
                           observed_failures=getattr(error, "observed_failures", []))
        else:
            outcome.update(status="cancelled", error_type=type(error).__name__)
        updated = copy.deepcopy(record)
        updated["documents"][url] = {"url": url, "outcomes": [*entry["outcomes"], outcome]}
        self.save(record, updated, "research.document_finished", {
            "url": url, "operation_id": outcome["id"], "outcome_hash": digest(outcome), "outcome": outcome})
        if evidence is not None:
            return self.evidence(outcome, text, reused=False)
        return outcome

    def evidence(self, outcome, text, *, reused):
        return {**outcome["retrieval"], "excerpt": text,
                "acquisition_receipt": {"version": 1, "run_id": self.rid, "operation_id": outcome["id"],
                    "outcome_hash": digest(outcome), "original_dispatch": outcome.get("dispatch", outcome.get("legacy", {}).get("original_dispatch")),
                    **({"legacy_provenance": outcome["legacy"]} if "legacy" in outcome else {}),
                    "current_dispatch": self.dispatch, "reused": reused,
                    "version_status": "Original requested versions are proposals, never verified support"}}

    def retry(self, url, error):
        from .research_acquisition import failure_observation
        failure = failure_observation(error)
        if (not retryable(failure)
                or any(not row["automatic"] for row in getattr(error, "observed_failures", []))):
            return False
        rows = self.entry(self.record(), url)["outcomes"]
        count = self.cause_count(rows, failure)
        return count < MAX_CAUSE_ATTEMPTS
