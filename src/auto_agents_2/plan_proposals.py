"""Source-bound evidence deltas for new plan amendments, never gate approval."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from .errors import AA2Error
from .models import EvidenceContract, EvidenceObligation, PlanResult, Record
from .plan_revision import PlanAmendment, PlanPatch
from .util import digest

PROTOCOL_KEY = "controller_plan_proposal"


def protocol():
    body = {"version": 1, "author_schema": "EvidencePlanPatch",
        "materializer": "evidence-delta-v1",
        "base": "Exact accepted plan and evidence contract hashes, revision and current source/catalog",
        "preserve": "Copy the frozen nonlegacy contract; no obligation rows in the author response",
        "amend": "Explicit add/replace/remove by stable ID; replace/remove require exact previous row hash",
        "gates": "Full PlanPatch validation, source/obligation preservation, independent review and CAS remain required"}
    return {**body, "hash": digest(body)}


def current_protocol(value):
    return value == protocol()


def decision_protocol(snapshot, *, already_bound):
    """Do not upgrade a frozen question/decision while publishing new capability."""
    if snapshot:
        evidence = snapshot["request"]["evidence"]
        return {PROTOCOL_KEY: evidence[PROTOCOL_KEY]} if PROTOCOL_KEY in evidence else {}
    # Older pending decisions may predate request snapshots. Keep their absence.
    return {} if already_bound else {PROTOCOL_KEY: protocol()}


class PreserveEvidence(Record):
    mode: Literal["preserve"]
    base_contract_hash: str = Field(min_length=1)


class AddObligation(Record):
    operation: Literal["add"]
    obligation: EvidenceObligation


class ReplaceObligation(Record):
    operation: Literal["replace"]
    obligation_id: str = Field(min_length=1)
    previous_hash: str = Field(min_length=1)
    replacement: EvidenceObligation


class RemoveObligation(Record):
    operation: Literal["remove"]
    obligation_id: str = Field(min_length=1)
    previous_hash: str = Field(min_length=1)


class AmendEvidence(Record):
    mode: Literal["amend"]
    base_contract_hash: str = Field(min_length=1)
    changes: list[AddObligation | ReplaceObligation | RemoveObligation]


class EvidencePlanPatch(PlanAmendment):
    proposal_version: Literal[1]
    base_plan_hash: str = Field(min_length=1)
    evidence_update: PreserveEvidence | AmendEvidence


def materialize(proposal, previous, context):
    """Build a complete UNAPPROVED patch. Never repair an explicit wrong value."""
    from .evidence_admission import current_catalog, current_requirement_source
    if not current_protocol(context.get(PROTOCOL_KEY)):
        raise AA2Error("Missing or unsupported frozen plan proposal protocol")
    catalog, source = (context.get(k) for k in
                      ("controller_evidence_capabilities", "evidence_requirement_source"))
    if not current_catalog(catalog) or not current_requirement_source(source):
        raise AA2Error("Plan evidence materialization needs current source/capability bindings")
    before = previous.evidence_contract
    update = proposal.evidence_update
    if (proposal.base_plan_hash != digest(previous.model_dump())
            or proposal.base_revision != context["base_revision"]
            or update.base_contract_hash != digest(before.model_dump() if before else None)):
        raise AA2Error("Stale plan/evidence materialization base")
    if isinstance(update, PreserveEvidence):
        if (before is None or before.catalog_hash != catalog["hash"]
                or before.source_binding != source["hash"]):
            raise AA2Error("Cannot preserve a legacy or stale evidence contract")
        contract = before.model_copy(deep=True)
    else:
        rows = {row.id: row.model_copy(deep=True) for row in before.obligations} if before else {}
        if before and len(rows) != len(before.obligations):
            raise AA2Error("Duplicate evidence identity in accepted base")
        seen = set()
        for change in update.changes:
            identity = change.obligation.id if isinstance(change, AddObligation) else change.obligation_id
            if identity in seen:
                raise AA2Error("Conflicting evidence delta operations")
            seen.add(identity)
            if isinstance(change, AddObligation):
                if identity in rows:
                    raise AA2Error("Evidence add names an existing obligation")
                rows[identity] = change.obligation.model_copy(deep=True)
            else:
                if identity not in rows or digest(rows[identity].model_dump()) != change.previous_hash:
                    raise AA2Error("Stale or missing evidence delta preimage")
                if isinstance(change, ReplaceObligation):
                    if change.replacement.id != identity:
                        raise AA2Error("Evidence replacement cannot rename its identity")
                    rows[identity] = change.replacement.model_copy(deep=True)
                else:
                    del rows[identity]
        contract = EvidenceContract(catalog_hash=catalog["hash"], source_binding=source["hash"],
                                    obligations=list(rows.values()))
    values = proposal.model_dump(exclude={"proposal_version", "base_plan_hash", "evidence_update"})
    return PlanPatch(**values, evidence_contract=contract)


def record_materialization(artifacts, operation, source_hash, previous, proposal, patch, context):
    body = {"version": 1, "operation": operation, "source_hash": source_hash,
        "protocol": context[PROTOCOL_KEY], "base_revision": proposal.base_revision,
        "context": {k: context[k] for k in ("controller_evidence_capabilities", "evidence_requirement_source")},
        "previous": artifacts.put(previous.model_dump(), "plan_proposal_base"),
        "proposal": artifacts.put(proposal.model_dump(), "plan_proposal"),
        "patch": artifacts.put(patch.model_dump(), "materialized_plan_patch")}
    return {**body, "binding": digest(body)}


def validate_materialization(journal, get, root, identity):
    """Recompute on prepared/activated replay; legacy journals keep their encoding."""
    operation = "recovery/patch/" + identity
    frozen = get("recovery/patch-context/" + identity) or {}
    invocation = get("invocation/" + operation) or {}
    record = journal.get("proposal_materialization")
    retained = get("recovery/patch-materialization/" + identity)
    required = (PROTOCOL_KEY in frozen or "evidence_update" in (invocation.get("result") or {})
                or retained is not None or "materialized_candidate" in journal)
    if record != retained:
        raise AA2Error("Materialization journal differs from its frozen proposal receipt")
    if record is None:
        if required:
            raise AA2Error("New plan proposal lost its materialization record")
        return
    from .safe_files import read_regular
    def read(ref):
        sha = ref.get("hash", "")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise AA2Error("Invalid materialization artifact address")
        try:
            data = read_regular(root / "artifacts" / sha[:2] / sha, 32 * 1024 * 1024)
            if digest(data) != sha:
                raise ValueError("hash")
            return json.loads(data)
        except (OSError, ValueError) as exc:
            raise AA2Error("Missing or corrupt materialization artifact") from exc
    required_keys = {"version", "operation", "source_hash", "protocol", "base_revision",
                     "context", "previous", "proposal", "patch", "binding"}
    if (not isinstance(record, dict) or set(record) != required_keys
            or not isinstance(record["context"], dict)
            or set(record["context"]) != {"controller_evidence_capabilities", "evidence_requirement_source"}
            or any(not isinstance(record[k], dict) for k in ("previous", "proposal", "patch"))):
        raise AA2Error("Malformed plan materialization record")
    body = {k: v for k, v in record.items() if k != "binding"}
    if (record.get("version") != 1 or record.get("binding") != digest(body)
            or not current_protocol(record.get("protocol")) or frozen.get(PROTOCOL_KEY) != record["protocol"]
            or record.get("operation") != operation or record.get("source_hash") != journal.get("source_hash")
            or record.get("base_revision") != journal.get("base_revision")):
        raise AA2Error("Plan proposal materialization binding changed")
    previous_data, raw, patch_data = (read(record[k]) for k in ("previous", "proposal", "patch"))
    if previous_data != journal.get("previous_plan") or raw != invocation.get("result"):
        raise AA2Error("Materialization does not match its original invocation/base")
    if invocation.get("artifact", {}).get("hash") != record["proposal"]["hash"]:
        raise AA2Error("Materialization lost its original proposal artifact")
    if record["context"] != {k: frozen.get(k) for k in record["context"]}:
        raise AA2Error("Materialization source context changed")
    context = {**record["context"], PROTOCOL_KEY: record["protocol"], "base_revision": record["base_revision"]}
    rebuilt = materialize(EvidencePlanPatch.model_validate(raw), PlanResult.model_validate(previous_data), context)
    if rebuilt.model_dump() != patch_data or patch_data != journal.get("patch"):
        raise AA2Error("Materialization differs from the reviewed complete patch")
    candidate_ref = journal.get("materialized_candidate")
    if not isinstance(candidate_ref, dict) or read(candidate_ref) != journal.get("plan"):
        raise AA2Error("Materialized candidate differs from the reviewed complete plan")


def instructions(context):
    if not current_protocol(context.get(PROTOCOL_KEY)) or context.get("plan_patch") is not True:
        return ""
    return ("\nSource-bound plan evidence proposal: return EvidencePlanPatch with proposal_version=1, "
        "base_plan_hash=plan_proposal_base.plan_hash and evidence_update.base_contract_hash="
        "plan_proposal_base.evidence_hash. Choose mode=preserve when no evidence obligation changes: "
        "do not repeat any old obligations and do not emit evidence_contract. Choose mode=amend "
        "only for explicit add/replace/remove operations; use the supplied row hashes for exact preimages. "
        "New/changed rows still require complete declarations and existing formal correction rules. "
        "The controller materializes the complete patch for independent review; preserve is not approval. "
        "Keep all other normal PlanPatch fields, including complete changed TaskSpecs and the full resulting document.\n")
