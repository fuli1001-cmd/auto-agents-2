"""Research-source recovery evidence; transport and stage authorization stay separate."""
from __future__ import annotations

from typing import Literal
from urllib.parse import urldefrag, urlsplit

from pydantic import Field

from .errors import (
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    InvalidStructuredResponse,
    ResearchDestinationRejected,
    ResearchResolverUnavailable,
    http_failure_classification,
)
from .faults import HumanIntervention, HumanRequirement, StateConflict
from .models import DocumentResult, Record
from .util import digest

CURRENT = "recovery/research-acquisition/current"
PREFIX = "recovery/research-acquisition/episodes/"
CONTEXT = "controller_research_acquisition"
WAIT = "recovery/research-acquisition/prerequisite-wait"
HARD = "recovery/research-acquisition/recording-error"
CONSUMED = "recovery/research-acquisition/prerequisite-consumed/"
SOURCE_CODES = {"DOCUMENT_ACQUISITION_UNAVAILABLE", "RESEARCH_RESOLVER_UNAVAILABLE", "RESEARCH_DESTINATION_REJECTED"}


class ClaimDisposition(Record):
    claim_id: str
    disposition: Literal["supported", "not_required", "blocked"]
    source_indexes: list[int] = Field(description="Exact zero-based indexes of this replacement's sources; [] for not_required/blocked.")
    requirement_ids: list[str] = Field(description="Relevant original requirement IDs; never invented requirements.")
    reason: str = Field(min_length=1, description="Explain necessity, equal claim/version coverage and first-party authority, or why removal preserves the original goals.")


class AcquisitionResolution(Record):
    episode_id: str
    status: Literal["ready", "blocked"]
    claims: list[ClaimDisposition]
    missing_information: list[str]


class ResearchRecoveryDocument(DocumentResult):
    acquisition_resolution: AcquisitionResolution


def failure_observation(error):
    """Positive, finite classifications only. A cause-less ConnectError may be TLS."""
    known = isinstance(error, (DocumentAcquisitionTimeout, DocumentAcquisitionUnavailable,
                               ResearchResolverUnavailable, ResearchDestinationRejected))
    evidence = dict(error.evidence) if known else {}
    category, automatic = "unknown", False
    if isinstance(error, ResearchDestinationRejected):
        category = "security"
    elif isinstance(error, DocumentAcquisitionTimeout):
        category, automatic = "network", True
    elif known:
        category, automatic = http_failure_classification(evidence, document=isinstance(error, DocumentAcquisitionUnavailable))
    if evidence.get("recording_failure"):
        automatic = False  # Missing durable observation is not a recovery permit.
    return {"code": error.code if known else "UNCLASSIFIED_ACQUISITION_ERROR",
            "classification": category, "automatic": automatic, "diagnostic": evidence}


def claim_rows(document):
    candidate = digest(document)
    return [{"id": digest([candidate, index, row]), "index": index, "source": row}
            for index, row in enumerate(document.get("sources", []))]


def origin(url):
    if not url:
        return "unknown"
    value = urlsplit(url)
    host = (value.hostname or "").rstrip(".").lower().encode("idna").decode()
    return f"{value.scheme.lower()}://{host}:{value.port or (443 if value.scheme.lower() == 'https' else 80)}"


def extend_basis(body, context):
    """Use the existing independent authority assessment, with finite claim criteria."""
    recovery = context.get(CONTEXT)
    if not recovery:
        return
    body["observations"][CONTEXT] = digest(recovery)
    text = "\n".join(row["source"]["claim"] for row in recovery["claims"])
    body["sources"]["failed_research"] = {"kind": "historical_proposal", "owner": "provider_research",
        "text": text, "hash": digest(text)}
    for row in recovery["claims"]:
        body["criteria"].append({"id": "stage:research-acquisition:" + row["id"],
            "kind": "research_evidence", "owner": "provider_research", "source": "failed_research",
            "statement": row["source"]["claim"],
            "instruction": "Assess the exact original source row and its replacement disposition. This is a proposal, "
                "not a new product requirement. Necessary facts require freshly acquired authoritative evidence covering "
                "the same claim and version. not_required needs a grounded necessity explanation preserving every original "
                "goal and explicit source constraint. Do not defer this assessment, infer success from acquisition, "
                "or accept blocked/missing facts. Use an evidence_gap/defect finding for an unresolved disposition, "
                "quoting the original claim. failed_research is historical evidence, not a source_dispute target."})


class ResearchAcquisitionRecovery:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def binding(self, rid):
        return {name: digest(self.store.get(rid, name)) for name in ("capture", "clarify")}

    def dispatch_binding(self, rid, document):
        raw = document.model_dump() if hasattr(document, "model_dump") else document
        candidate = self.store.get(rid, "candidate/provider_research", {}).get("document")
        if candidate != raw:
            raise StateConflict("Research dispatch lost its exact controller candidate", phase="provider_research")
        return {"version": 1, "run_id": rid, "generation": self.store.run(rid)["data"].get("replans", 0),
                "binding": self.binding(rid), "candidate_hash": digest(raw),
                "sources_hash": digest(raw.get("sources", [])),
                "source_urls": sorted({urldefrag(row["url"])[0] for row in raw.get("sources", [])})}

    @staticmethod
    def refusals(report):
        result = []
        for row in report:
            facts = ([row["failure"]] if row["status"] == "failed" else []) + row.get("observed_failures", [])
            selected = []
            for failure in facts:
                if (failure["code"] in SOURCE_CODES and not failure["automatic"]
                        and failure not in selected):
                    selected.append(failure)
            if selected:
                result.append({"url": row["url"], "failures": selected})
        return result

    def validate_dispatch(self, rid, stamp):
        candidate = self.store.get(rid, "candidate/provider_research", {}).get("document")
        if (not isinstance(stamp, dict) or stamp.get("version") != 1 or candidate is None
                or type(stamp.get("generation")) is not int or stamp["generation"] < 0):
            raise StateConflict("Research wait has no verifiable original dispatch binding", phase="provider_research")
        if stamp != self.dispatch_binding(rid, candidate):
            raise StateConflict("Research wait candidate, requirements or generation changed", phase="provider_research")

    @staticmethod
    def global_failures(report):
        # Only real failed captures establish this boundary. Cancellation and
        # not_executed/blocked_by rows cannot supply an internal failure.
        return [{"url": row["url"],
                 "error_type": row.get("global_error_type", "UNCLASSIFIED_ACQUISITION_ERROR")}
                for row in report if row["status"] == "failed"
                and row["failure"]["code"] == "UNCLASSIFIED_ACQUISITION_ERROR"]

    @staticmethod
    def hard_record(rid, original, error, operation):
        stamp = getattr(original, "acquisition_dispatch", None)
        global_failures = ResearchAcquisitionRecovery.global_failures(
            getattr(original, "acquisition_observations", []))
        error_type = (global_failures[0]["error_type"]
                      if operation == "research.capture.internal" and global_failures else type(error).__name__)
        return {"version": 1, "kind": "recording_error", "run_id": rid, "operation": operation,
                "binding_status": "original_frozen" if isinstance(stamp, dict) else "unverifiable",
                "dispatch": stamp,
                "acquisition_id": getattr(original, "acquisition_id", None),
                "error_type": error_type, "original_error_type": type(original).__name__,
                "global_failures": global_failures,
                "observations": getattr(original, "acquisition_observations", [])}

    @staticmethod
    def source_representative(own):
        failures = [failure for row in own for failure in row["failures"]]
        return next((failure for failure in failures if failure["classification"] in {"security", "permission"}),
                    failures[0])

    def source_wait(self, rid, stamp, acquisition_id, report):
        own = self.refusals(report)
        if not own:
            return None
        identity = digest([stamp, acquisition_id, own])
        representative = self.source_representative(own)
        requirement = HumanRequirement(kind="external_input",
            reason="Research acquisition observed an unresolved source boundary: " + "; ".join(
                row["url"] + " (" + ", ".join(failure["classification"] for failure in row["failures"]) + ")"
                for row in own),
            action="Provide new relevant authoritative material or verify the original source/network prerequisite is fixed; "
                   "do not waive permission, certificate, public-address or unknown failures.",
            resume_condition="A matching reply permits only the original bounded read-only reacquisition. All original "
                             "public-address, TLS, source review, counters and human prototype gates still apply.",
            evidence_refs=[WAIT],
            evidence={**representative["diagnostic"], "authority": "research_source_observation", "wait_id": identity,
                      "dispatch": stamp, "observations": own})
        reason_code, phase = representative["code"], "provider_research"
        body = requirement.model_dump()
        question_identity = digest([reason_code, phase, body])
        return {"version": 1, "kind": "source_wait", "id": identity, "dispatch": stamp,
                "acquisition_id": acquisition_id, "observations": own,
                "reason_code": reason_code, "phase": phase, "requirement": body,
                "identity": question_identity, "attempt": self.store.get(rid, "prerequisite/" + question_identity, 0)}

    def waiting(self, rid):
        wait = self.store.get(rid, WAIT)
        if wait is None:
            return None
        if (wait.get("kind") != "source_wait" or wait.get("version") != 1
                or type(wait.get("attempt")) is not int or wait["attempt"] < 0):
            raise StateConflict("Research wait kind/version is not a source confirmation", phase="provider_research")
        self.validate_dispatch(rid, wait.get("dispatch"))
        evidence = wait["requirement"].get("evidence", {})
        if (wait["reason_code"] not in SOURCE_CODES or wait["phase"] != "provider_research"
                or not wait["observations"] or wait["reason_code"] != self.source_representative(wait["observations"])["code"]
                or wait["requirement"].get("kind") != "external_input"
                or evidence.get("authority") != "research_source_observation"
                or evidence.get("wait_id") != wait["id"] or evidence.get("dispatch") != wait["dispatch"]
                or evidence.get("observations") != wait["observations"]
                or self.store.get(rid, "prerequisite/" + wait["identity"], 0) != wait["attempt"]
                or wait["id"] != digest([wait["dispatch"], wait["acquisition_id"], wait["observations"]])
                or wait["identity"] != digest([wait["reason_code"], wait["phase"], wait["requirement"]])
                or {row["url"] for row in wait["observations"]} - set(wait["dispatch"]["source_urls"])):
            raise StateConflict("Research wait lost its exact source/question identity", phase="provider_research")
        return wait

    @staticmethod
    def wait_error(wait):
        return HumanIntervention(HumanRequirement.model_validate(wait["requirement"]),
                                 reason_code=wait["reason_code"], phase=wait["phase"])

    def consume_wait(self, rid, wait, question_id):
        self.validate_dispatch(rid, wait["dispatch"])
        self.store.consume_research_prerequisite(rid, WAIT, HARD, CONSUMED + wait["id"], wait, question_id)

    def hard_error(self, rid):
        hard = self.store.get(rid, HARD)
        if hard is None:
            return None
        return StateConflict("Research recording/internal state remains unverified (" + hard.get("operation", "unknown") +
                             ", " + hard.get("error_type", "unknown") + "); a network confirmation cannot clear "
                             "this hard boundary. Restore and verify the original owner state first.",
                             phase="provider_research", evidence_refs=[HARD])

    def prepare_wait(self, rid):
        hard = self.hard_error(rid)
        if hard is not None:
            raise hard
        if self.store.has_event(rid, "research.acquisition_recording_failed"):
            raise StateConflict("Historical research recording audit has no verified hard-hold resolution; "
                                "preserve its original state and verify the owner repair before any new request.",
                                phase="provider_research")
        wait = self.waiting(rid)
        if wait is not None:
            raise self.wait_error(wait)
        # Do not invent a wait for historical refusals. Old records without the
        # new original stamp/consumption receipt must be checked by their owner.
        candidate = self.store.get(rid, "candidate/provider_research", {}).get("document")
        if candidate is None or self.store.get(rid, "provider_research"):
            return
        record = self.active(rid)
        if record is None:
            key = PREFIX + digest([rid, self.store.run(rid)["data"].get("replans", 0), self.binding(rid), candidate])
            record = self.store.get(rid, key)
        for attempt in (record or {}).get("attempts", []):
            own = self.refusals(attempt["observations"])
            if not own:
                continue
            stamp = attempt.get("dispatch")
            identity = digest([stamp, attempt["id"], own]) if stamp is not None else None
            receipt = self.store.get(rid, CONSUMED + identity) if identity else None
            if (not receipt or receipt.get("dispatch") != stamp
                    or receipt.get("acquisition_id") != attempt["id"]):
                raise StateConflict("Historical research refusal has no verified dispatch wait/consumption binding; "
                                    "preserve its original question and observations before any new request.",
                                    phase="provider_research", evidence_refs=[CURRENT])
        fault = self.store.get(rid, "recovery/current-fault", {})
        if fault.get("route") == "human" and fault.get("phase") == "provider_research" and fault.get("reason_code") in SOURCE_CODES:
            requirement = fault.get("human_requirement", {})
            category, automatic = http_failure_classification(
                requirement.get("evidence", {}), document=fault["reason_code"] == "DOCUMENT_ACQUISITION_UNAVAILABLE")
            identity = digest([fault["reason_code"], fault["phase"], requirement])
            key = "prerequisite/" + identity + "/" + str(self.store.get(rid, "prerequisite/" + identity, 0))
            if not automatic and self.store.decision_questions(rid, [key]):
                raise StateConflict("Legacy source prerequisite lacks a verified new dispatch binding; keep the "
                                    "original question and obtain an owner-verified bridge before reacquisition.",
                                    phase="provider_research")

    def active(self, rid):
        pointer = self.store.get(rid, CURRENT)
        if not pointer:
            return None
        record = self.store.get(rid, pointer["key"])
        if not record or record.get("id") != pointer.get("id") or pointer["key"] != PREFIX + record["id"]:
            raise StateConflict("Research acquisition lost its original episode evidence")
        if record.get("completed"):
            return None
        original = self.engine.artifacts.json(record["original_candidate"])
        if record.get("claims") != claim_rows(original):
            raise StateConflict("Research recovery original claim identities changed")
        if record["binding"] != self.binding(rid):
            raise StateConflict("Research recovery requirements changed; preserve the original claims and resolve the owning stage")
        return record

    def context(self, rid):
        record = self.active(rid)
        if not record:
            return None
        return {"episode_id": record["id"], "binding": record["binding"], "claims": record["claims"],
                "original_candidate": record["original_candidate"], "attempts": record["attempts"],
                **({"rejected_reviews": record["rejected_reviews"]} if record.get("rejected_reviews") else {}),
                "instruction": "The source failed, not necessarily the fact or the project. Assess necessity against "
                    "the original requirements. Return a full research replacement and exactly one acquisition_resolution "
                    "row per original claim. Choose first-party authoritative alternatives with equivalent coverage; "
                    "a new URL or download is not proof. Reacquisition and independent review are mandatory even for "
                    "empty sources. Preserve all safety/permission refusals and human prototype approval. Do not change "
                    "network settings or retry a denied operation. Repeating the same failed source set without new "
                    "evidence is not progress. If no useful permitted path remains, return blocked with the exact "
                    "missing information; do not silently remove necessary facts or change the requirements."}

    def prepare(self, rid):
        self.prepare_wait(rid)
        record = self.active(rid)
        if not record:
            return
        pending = record.get("pending", {})
        generation = self.store.run(rid)["data"].get("replans", 0)
        if pending and pending["generation"] == generation:
            # Durable observation precedes the normal Replan exception. Resume it,
            # rather than reacquiring the same failed candidate after a crash.
            from .engine import Replan
            raise Replan("provider_research", pending["reason"])
        if pending:
            history = self.store.get(rid, f"recovery/gate-history/{pending['generation']}", {})
            transition = history.get("pending", {})
            candidate = history.get("checkpoints", {}).get("candidate/provider_research", {}).get("document")
            permit_key = transition.get("permit")
            permit = self.store.get(rid, permit_key or "", {})
            if (transition.get("stage") != "provider_research" or transition.get("reason") != pending["reason"]
                    or not transition.get("granted") or digest(candidate) != pending["candidate_hash"]
                    or not permit_key or permit.get("action", {}).get("action") != "provider_research"):
                raise StateConflict("Research acquisition re-entry lost its exact authorized archived candidate")
            if (permit.get("phase") != "completed" or permit.get("outcome") != {
                    "status": "gate_reset", "stage": "provider_research"}
                    or permit.get("outcome_hash") != digest(permit["outcome"])):
                raise StateConflict("Research acquisition re-entry has no completed gate-reset receipt")

    def recording_failed(self, rid, document, original, error):
        raw = document.model_dump() if hasattr(document, "model_dump") else document
        stamp = getattr(original, "acquisition_dispatch", None)
        details = {"error_type": type(error).__name__, "original_error_type": type(original).__name__,
                   "candidate_hash": stamp.get("candidate_hash") if isinstance(stamp, dict) else None,
                   "reported_candidate_hash": digest(raw), "acquisition_id": getattr(original, "acquisition_id", None),
                   "dispatch": stamp}
        original.acquisition_recording_failure = {**details, "audit_recorded": False}
        original.add_note("Research observation recording failed: " + details["error_type"])
        if isinstance(getattr(original, "evidence", None), dict):
            original.evidence["recording_failure"] = original.acquisition_recording_failure
        try:
            hard = self.store.get(rid, HARD)
            updates = {} if hard is not None else {HARD: self.hard_record(
                rid, original, error, "research.acquisition.observe")}
            self.store.checkpoints(rid, updates, event=("research.acquisition_recording_failed",
                {**details, "observations": getattr(original, "acquisition_observations", [])}))
        except Exception as audit_error:
            original.acquisition_recording_failure["audit_error_type"] = type(audit_error).__name__
            original.add_note("Research observation audit also failed: " + type(audit_error).__name__)
        else:
            original.acquisition_recording_failure["audit_recorded"] = True

    def observe(self, rid, document, error, *, control=False):
        raw = document.model_dump() if hasattr(document, "model_dump") else document
        report = getattr(error, "acquisition_observations", None)
        if report is None or not getattr(error, "acquisition_id", None):
            # A custom acquisition adapter must bind its own observations before
            # automatic routing. An unbound first error cannot identify a batch.
            return
        if ({row["url"] for row in report} != {urldefrag(row["url"])[0] for row in raw.get("sources", [])}
                or len({row["url"] for row in report}) != len(report)):
            raise StateConflict("Research acquisition observations do not bind the exact candidate source set")
        stamp = getattr(error, "acquisition_dispatch", None)
        if stamp is not None:
            self.validate_dispatch(rid, stamp)
            if stamp["candidate_hash"] != digest(raw):
                raise StateConflict("Research observation no longer matches its original dispatch candidate")
        generation = self.store.run(rid)["data"].get("replans", 0)
        record = self.active(rid)
        had_active = record is not None
        if record is None:
            identity = digest([rid, generation, self.binding(rid), raw])
            record = self.store.get(rid, PREFIX + identity) or {"version": 1, "id": identity, "binding": self.binding(rid),
                "original_candidate": self.engine.artifacts.put(raw, "research-acquisition-candidate"),
                "claims": claim_rows(raw), "attempts": []}
        attempt = {"id": error.acquisition_id, "generation": generation, "candidate_hash": digest(raw), "sources": raw["sources"],
                   "observations": report, **({"dispatch": stamp} if stamp is not None else {})}
        # Exclude prose, fragment differences and attempt counters from this
        # no-progress comparison. New actual outcomes may reopen a useful path.
        def supplemental(row):
            # Extra presentation of the main failure is not new acquisition
            # evidence, including when resuming an older ledger format.
            return [failure for failure in row.get("observed_failures", [])
                    if failure != row.get("failure")]

        facts = [{"url": row["url"], "status": row["status"],
                  "failure": row.get("failure"),
                  "artifact_hash": row.get("retrieval", {}).get("artifact", {}).get("hash"),
                  **({"observed_failures": sorted(supplemental(row), key=digest)}
                     if supplemental(row) else {})} for row in report]
        attempt["facts_hash"] = digest(sorted(facts, key=lambda row: row["url"]))
        duplicate = any(row["facts_hash"] == attempt["facts_hash"] for row in record["attempts"])
        attempts = record["attempts"] if attempt in record["attempts"] else [*record["attempts"], attempt]
        failed = [row for row in report if row["status"] == "failed"]
        automatic = not control and bool(failed) and all(row["status"] in {"retrieved", "failed"} and
            (row["status"] == "retrieved" or row["failure"]["automatic"]) for row in report)
        # A later timeout/404 cannot erase an earlier unresolved refusal. Only
        # actual successful acquisition through the unchanged boundary resolves it.
        unresolved = set()
        for observed in attempts:
            for row in observed["observations"]:
                for failure in row.get("observed_failures", []):
                    if not failure["automatic"]:
                        unresolved.add(row["url"])
                if row["status"] == "retrieved":
                    unresolved.discard(row["url"])
                elif row["status"] == "failed" and not row["failure"]["automatic"]:
                    unresolved.add(row["url"])
        automatic = automatic and not unresolved
        reason = "Research acquisition " + record["id"] + ": " + "; ".join(
            f"{row['url']} ({row['failure']['classification']}, {row['failure']['code']})" for row in failed)
        updated = {**record, "attempts": attempts}
        if automatic and not duplicate:
            updated["pending"] = {"generation": generation, "candidate_hash": digest(raw), "reason": reason}
        else:
            updated.pop("pending", None)
        updates = {PREFIX + record["id"]: updated}
        own = self.refusals(report)
        from .research_batch import ResearchRequestNotExecuted
        source_error = isinstance(error, (DocumentAcquisitionTimeout, DocumentAcquisitionUnavailable,
                                         ResearchResolverUnavailable, ResearchDestinationRejected, ResearchRequestNotExecuted))
        if self.global_failures(report) or not control and not source_error:
            updates[HARD] = self.hard_record(rid, error, error, "research.capture.internal")
        elif own:
            if stamp is None:
                updates[HARD] = self.hard_record(rid, error, error, "research.dispatch.unverifiable")
            else:
                wait = self.source_wait(rid, stamp, error.acquisition_id, report)
                previous = self.store.get(rid, WAIT)
                if previous is not None and previous != wait:
                    raise StateConflict("Another exact research source wait must be resolved first")
                updates[WAIT] = wait
        if automatic or had_active:
            updates[CURRENT] = {"id": record["id"], "key": PREFIX + record["id"]}
        self.store.checkpoints(rid, updates, event=("research.acquisition_failed", {"episode_id": record["id"],
            "attempt": len(attempts), "automatic": automatic and not duplicate,
            "no_progress": duplicate, "observations": report}))
        if not automatic:
            return  # The original exception retains its original human/security boundary.
        if duplicate:
            self.block(rid, "The same research sources failed again with no new acquisition evidence.", raw)
        from .engine import Replan
        raise Replan("provider_research", reason)

    def validate(self, rid, document):
        record = self.active(rid)
        if not record:
            return
        raw = document.model_dump() if hasattr(document, "model_dump") else document
        try:
            resolution = ResearchRecoveryDocument.model_validate(raw).acquisition_resolution
        except ValueError as exc:
            raise InvalidStructuredResponse("Research recovery requires its original-claim disposition map") from exc
        rows = {row.claim_id: row for row in resolution.claims}
        wanted = {row["id"] for row in record["claims"]}
        requirements = {row["id"] for row in self.store.get(rid, "clarify", {}).get("requirements", [])}
        if (resolution.episode_id != record["id"] or set(rows) != wanted or len(rows) != len(resolution.claims)):
            raise InvalidStructuredResponse("Research recovery must map every original claim exactly once")
        for row in rows.values():
            if (not set(row.requirement_ids) <= requirements or len(set(row.source_indexes)) != len(row.source_indexes)
                    or any(index < 0 or index >= len(raw["sources"]) for index in row.source_indexes)
                    or (row.disposition == "supported") != bool(row.source_indexes)):
                raise InvalidStructuredResponse("Research claim disposition invents requirements or evidence indexes")
        blocked = any(row.disposition == "blocked" for row in rows.values())
        if blocked != (resolution.status == "blocked") or blocked and not resolution.missing_information:
            raise InvalidStructuredResponse("Unresolved research claims require a blocked result with missing information")

    def validate_retrieved(self, rid, document, retrieved):
        if not self.active(rid):
            return
        sources = document.get("sources", [])
        if (len(sources) != len(retrieved) or any(
                any(row.get(key) != value for key, value in source.items())
                or not row.get("retrieval", {}).get("artifact", {}).get("hash")
                for source, row in zip(sources, retrieved))):
            raise StateConflict("Research replacement has no exact controller-acquired source evidence")

    def reject_review(self, rid, document, retrieved, review):
        record = self.active(rid)
        if not record:
            return
        proof = self.engine.authority.receipt(rid, review.model_dump())
        if (review.approved or proof["owned_candidate"] != document
                or proof["basis"]["observations"].get(CONTEXT) != digest(self.context(rid))):
            raise StateConflict("Rejected research lost its exact acquisition review evidence")
        # Prose changes, fresh timestamps and URL fragments cannot manufacture
        # progress. A different observed source body/authority or independently
        # improved criterion is new evidence, rather than another model attempt.
        contents = [(origin(row["url"]), origin(row["retrieval"].get("final_url")), row["retrieval"]["artifact"]["hash"])
                    for row in retrieved]
        resolution = document["acquisition_resolution"]
        facts = {"binding": record["binding"], "contents": sorted(set(contents)),
            "coverage": sorted((row["criterion_id"], row["status"]) for row in review.authority["coverage"]),
            "findings": sorted((row["criterion_id"], row["kind"], row["owner"])
                               for row in review.authority["findings"]),
            "dispositions": sorted((row["claim_id"], row["disposition"],
                sorted(contents[index] for index in row["source_indexes"]), sorted(row["requirement_ids"]))
                for row in resolution["claims"])}
        failures = record.get("rejected_reviews", [])
        failed = {"candidate_hash": digest(document), "review_key": "authority/reviews/" + digest(review.model_dump()),
                  "facts_hash": digest(facts), "summary": review.summary}
        duplicate = any(row["facts_hash"] == failed["facts_hash"] for row in failures)
        reason = review.summary + "".join(f"\n[{row.severity}] {row.description}\nEvidence: {row.evidence}\nResolution: {row.resolution}"
                                         for row in review.findings)
        updated = {**record, "rejected_reviews": [*failures, failed]}
        if not duplicate:
            updated["pending"] = {"generation": self.store.run(rid)["data"].get("replans", 0),
                "candidate_hash": digest(document), "reason": reason}
        else:
            updated.pop("pending", None)
        self.store.checkpoint(rid, PREFIX + record["id"], updated)
        if duplicate:
            self.block(rid, "Independent research review rejected the same evidence again without progress.", document)
        from .engine import Replan
        raise Replan("provider_research", reason)

    def block(self, rid, reason, document):
        record = self.active(rid)
        resolution = document.get("acquisition_resolution", {})
        details = {"episode_id": record["id"], "claims": record["claims"], "attempts": record["attempts"],
                   "rejected_reviews": record.get("rejected_reviews", []),
                   "proposal": document, "missing_information": resolution.get("missing_information", [])}
        self.store.checkpoint(rid, PREFIX + record["id"] + "/blocked/" + digest(details), details)
        summary = "; ".join(row["source"]["claim"] for row in record["claims"])
        tried = "; ".join(f"{row['url']}: {row['status']}" +
            (f"/{row['failure']['classification']}/{row['failure']['code']}" if row.get("failure") else "")
            for attempt in record["attempts"] for row in attempt["observations"])
        missing = "; ".join(details["missing_information"])
        rejected = "; ".join(row["summary"] for row in details["rejected_reviews"])
        raise HumanIntervention(HumanRequirement(kind="external_input",
            reason=f"{reason}\nClaims requiring resolution: {summary}\nObserved attempts: {tried}\nRejected reviews: {rejected}\nMissing: {missing or 'new relevant source or connectivity evidence'}",
            action="Provide the missing authoritative material or new connectivity evidence. If the original requirements "
                   "must change, resolve that in clarification; do not waive a refusal or accept an unverified claim.",
            resume_condition="A useful permitted path has new evidence; reacquisition and independent research review "
                             "still pass before any gate advances. Human prototype approval remains required.",
            evidence_refs=[PREFIX + record["id"]], evidence=details),
            reason_code="research_acquisition_blocked", phase="provider_research")

    def acceptance(self, rid, document, review):
        record = self.active(rid)
        if not record:
            return {}
        self.validate(rid, document)
        if document["acquisition_resolution"]["status"] != "ready":
            self.block(rid, "Research has no verified permitted continuation.", document)
        proof = self.engine.authority.receipt(rid, review.model_dump())
        if (not review.approved or proof["owned_candidate"] != document
                or proof["basis"]["observations"].get(CONTEXT) != digest(self.context(rid))):
            raise StateConflict("Research acquisition disposition lost its exact independent review evidence")
        return {PREFIX + record["id"]: {**record, "completed": {
            "candidate_hash": digest(document), "review_key": "authority/reviews/" + digest(review.model_dump())}}}
