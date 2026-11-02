"""Accepted contract continuity across complete upstream rebuilds.

This is a finite catalogue and an acceptance journal, not a patch language. The
controller preserves source identities; independent reviewers decide meaning.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import Field

from .faults import StateConflict
from .models import Record
from .util import digest

ROOT = "recovery/contract-reentry/"
ACTIVE = ROOT + "active"
CONTEXT = "controller_contract_reentry"


class RevisionCitation(Record):
    source: str
    quote: str = Field(min_length=1)


class RevisionDisposition(Record):
    operation: Literal["replace", "split", "merge", "retire"]
    old_ids: list[str] = Field(min_length=1)
    new_ids: list[str]
    authority: list[RevisionCitation] = Field(min_length=1)
    reason: str = Field(min_length=1)


def catalogue(owner, document):
    """Identity includes the exact slot and value; equal text is not equal ownership."""
    result = {}
    if not document:
        return result

    def add(field, entity, index, value, *, origin="", requirements=(), target=None):
        locator = {"owner": owner, "field": field, "entity": entity, "index": index}
        ident = digest([locator, value])
        result[ident] = {"id": ident, **locator, "value": value, "value_hash": digest(value),
                         "origin": origin, "requirements": list(requirements)}
        if target:
            result[ident]["contract_target"] = target

    if owner == "clarify":
        for req in document.get("requirements", []):
            rid = req["id"]
            add("requirement", rid, 0, {"id": rid, "description": req["description"]}, requirements=[rid])
            for index, text in enumerate(req.get("acceptance", [])):
                add("acceptance", rid, index, text, requirements=[rid])
        for index, command in enumerate(document.get("verification", [])):
            add("verification", "", index, command)
    else:
        for task in document.get("tasks", []):
            tid, origin, reqs = task["id"], task.get("origin_id") or task["id"], task["requirement_ids"]
            add("task_links", tid, 0, {"origin_id": origin, "requirement_ids": reqs},
                origin=origin, requirements=reqs)
            for field in ("acceptance", "checks"):
                for index, value in enumerate(task.get(field, [])):
                    add(field, tid, index, value, origin=origin, requirements=reqs,
                        target={"target": field, "task_id": tid, "index": index, "value_hash": digest(value)})
        for field in ("final_checks", "runtime_prepare", "runtime_probes"):
            for index, value in enumerate(document.get(field, [])):
                add(field, "", index, value, target={"target": field, "task_id": "",
                    "index": index, "value_hash": digest(value)})
        origins = {t["id"]: t.get("origin_id") or t["id"] for t in document.get("tasks", [])}
        for row in (document.get("evidence_contract") or {}).get("obligations", []):
            add("evidence_obligation", row["id"], 0, row,
                origin=origins.get(row.get("task_id"), ""), requirements=row.get("requirement_ids", []),
                target={"target": row["target"], "task_id": row.get("task_id", ""),
                        "index": row["index"], "value_hash": digest(row["value"])})
        if document.get("preview") is not None:
            add("preview", "", 0, document["preview"])
    return result


def _answer_basis(get, document, receipt=None):
    """Use the exact accepted owner's consumed answers, not the rollback clock."""
    receipt = receipt if receipt is not None else get("authority/accepted/clarify")
    if not receipt:
        return None  # Legacy absence cannot grant historical answers new authority.
    proof = get(receipt["review_key"])
    if (not proof or proof.get("review_hash") != receipt.get("review_hash")
            or digest(proof.get("review")) != receipt.get("review_hash")
            or not proof["review"].get("approved")
            or proof["basis"].get("scope") != "coverage_review"
            or receipt.get("artifact_hash") != digest(proof.get("owned_candidate"))):
        raise StateConflict("Accepted clarification lost its consumed-answer receipt")
    from .review_authority import validate_review
    validate_review(proof["review"], proof["basis"])
    owned = proof["owned_candidate"]
    if owned != document:
        # Historical first-entry code could set this controller-detected fact
        # after review. Protect the live specification but infer no new waiver
        # from this nonidentical receipt. No other drift is accepted.
        if (isinstance(owned, dict) and owned.get("frontend") is False
                and document == {**owned, "frontend": True}):
            return None
        raise StateConflict("Accepted clarification changed beyond legacy frontend normalization")
    sources = [source for source in proof["basis"]["sources"].values() if source["kind"] == "submitted_answer"]
    if any("answer_record" not in source for source in sources):
        return None  # Older unbound answer text cannot establish what this owner consumed.
    return {**receipt, "answers": [source["answer_record"] for source in sources]}


def _consumed_answers(current, get):
    accepted = [item for item in current["receipts"] if item["owner"] == "clarify"]
    if accepted:
        item = accepted[-1]
        receipt = {"version": 1, "artifact_hash": digest(item["document"]),
                   "review_key": item["review_key"], "review_hash": item["review_hash"]}
        consumed = _answer_basis(get, item["document"], receipt)
        if consumed is None:
            raise StateConflict("Accepted rebuild successor lost its consumed-answer identity")
        return consumed["answers"]
    baseline = current["base"].get("clarify_answer_basis")
    return baseline["answers"] if baseline is not None else current["base"].get("submitted_answers", [])


def begin(records, rid, pending, history_key, history):
    """Called only inside the existing authorized reset transaction."""
    if records.get(ACTIVE):
        state(records.get)  # A repeat rollback cannot silently repair a damaged chain.
        return {}
    removed = history["checkpoints"]
    if not any(removed.get(name) for name in ("clarify", "plan")):
        return {}
    body = {"version": 1, "run": rid, "generation": pending["generation"],
            "history_key": history_key, "history_hash": digest(history),
            "specification": records.get("clarify"), "plan": records.get("plan"),
            "plan_revision": records.get("plan_revision/current"),
            "check_production": records.get("evidence_admission/active"),
            "check_production_required": records.get("evidence_admission/check_production_required"),
            "submitted_answers": records.get("authority/submitted-answers", []),
            "clarify_answer_basis": _answer_basis(records.get, records.get("clarify")),
            "correction_request": pending.get("correction_request")}
    base_key = ROOT + digest(body) + "/base"
    return {base_key: body, ACTIVE: {"version": 1, "base": base_key, "head": None}}


def state(get):
    active = get(ACTIVE)
    if not active:
        return None
    base = get(active.get("base", ""))
    if (active.get("version") != 1 or not base or base.get("version") != 1
            or active["base"] != ROOT + digest(base) + "/base"
            or digest(get(base["history_key"])) != base["history_hash"]):
        raise StateConflict("Contract rebuild lost its exact accepted baseline")
    if any(digest(get(key)) != value for key, value in base.get("legacy_history", {}).items()):
        raise StateConflict("Migrated contract history changed")
    consumed = base.get("clarify_answer_basis")
    if consumed is not None and _answer_basis(get, base["specification"], {
            k: v for k, v in consumed.items() if k != "answers"}) != consumed:
        raise StateConflict("Accepted clarification answer basis changed")
    seen, receipts = set(), []
    key = active.get("head")
    while key:
        item = get(key)
        if (key in seen or not item or item.get("base") != active["base"]
                or key != ROOT + digest(item) + "/accepted"):
            raise StateConflict("Contract rebuild acceptance chain is damaged")
        review = get(item["review_key"])
        if (not review or digest(review["review"]) != item["review_hash"]
                or review.get("review_hash") != item["review_hash"]
                or review.get("owned_candidate") != item["document"]
                or not review["review"].get("approved")):
            raise StateConflict("Contract rebuild lost an accepted successor review")
        from .review_authority import validate_review
        validate_review(review["review"], review["basis"])
        expected = {"version": 1, "base": active["base"], "head": item["previous_head"]}
        if ((review["basis"].get("revision") or {}).get("binding") != expected
                or review["basis"]["revision"]["owner"] != item["owner"]
                or item["dispositions"] != review["review"]["authority"]["revision_dispositions"]):
            raise StateConflict("Accepted rebuild successor belongs to another baseline or head")
        receipts.append({"key": key, **item})
        seen.add(key)
        key = item["previous_head"]
    receipts.reverse()
    specification = base.get("legacy_current_specification", base["specification"])
    for item in receipts:
        if item["owner"] == "clarify":
            specification = item["document"]
    return {"binding": deepcopy(active), "base": base, "receipts": receipts,
            "specification": specification, "plan": base["plan"]}


def author_context(get):
    current = state(get)
    if not current:
        return {}
    return {CONTEXT: {**current,
        "instruction": "This controller-held accepted baseline survives repeated re-entry and rejected candidates. "
            "Propose the complete new document against the effective specification and original plan. "
            "Preserve unaffected obligations. A changed/deleted requirement, acceptance, check, evidence association "
            "or preview needs an explicit independently reviewed successor or lawful retirement. "
            "Historical approval is not approval of this candidate; a stage permit alone never waives an obligation."}}


def make_revision(get, owner, candidate, answers, correction):
    current = state(get)
    if not current or owner not in {"clarify", "plan"}:
        return None
    if owner == "plan" and needs_clarification(get):
        raise StateConflict("Legacy accepted specification changes require their owner's joint preservation review")
    previous = current["specification"] if owner == "clarify" else current["plan"]
    versions = ([item["document"] for item in current["receipts"] if item["owner"] == "clarify"][-1:]
        or current["base"].get("legacy_specifications") or [previous]) if owner == "clarify" else [previous]
    old = {}
    for version in versions:
        old.update({key: {**row, "source_document_hash": digest(version)} for key, row in catalogue(owner, version).items()})
    new = catalogue(owner, candidate)
    kept = sorted(set(old) & set(new))
    changed = sorted(set(old) - set(kept))
    added = sorted(set(new) - set(kept))
    sources = {"owner_reentry": {"kind": "owner_reentry", "owner": owner,
        "text": "Preserve substantive goals through the responsible owner's independently reviewed rebuild.",
        "allowed_old_ids": changed, "retire": False, "binding": current["binding"]}}
    # Answers authorize possible goal changes only through clarify's exact joint
    # review. A plan cannot turn the mere existence of an answer into a waiver.
    if owner == "clarify":
        prior = {digest(row) for row in _consumed_answers(current, get)}
        for answer in answers:
            if digest(answer) not in prior:
                sources["answer:" + answer["id"]] = {"kind": "submitted_answer", "owner": owner,
                    "text": answer["answer"], "record": answer, "allowed_old_ids": changed, "retire": True}
    if correction and correction.get("owner") == owner and not correction.get("completed"):
        review = get(correction.get("review_key", ""))
        archived = get("authority/corrections/" + correction.get("id", ""))
        if (review and not review["review"].get("approved")
                and review.get("review_hash") == digest(review.get("review"))
                and archived == correction and correction.get("previous_kind") in {
                    "accepted_technical_contract", "accepted_interpretation"}
                and correction.get("source_identity") == review["basis"].get("source_identity")):
            from .review_authority import validate_review
            assessment = validate_review(review["review"], review["basis"])
            target_ids = set(correction.get("revision_targets", []))
            for index, finding in enumerate(assessment.findings):
                if finding.kind != "source_dispute" or finding.owner != owner:
                    continue
                source = review["basis"]["sources"][finding.method_source]
                target = source.get("contract_target")
                permitted = [ident for ident, row in old.items()
                    if (correction.get("previous") == next((v for v in versions if digest(v) == row.get("source_document_hash")), previous)
                        and source.get("revision_item_id") == ident and ident in target_ids)
                    or target and row.get("contract_target") == target
                    and correction.get("previous") == previous]
                if permitted:
                    sources["dispute:" + str(index)] = {"kind": "source_dispute", "owner": owner,
                        "text": finding.method_quote, "review_key": correction["review_key"],
                        "allowed_old_ids": permitted, "retire": True}
    if owner == "plan":
        # Only accepted clarification successors may withdraw old requirement
        # coverage downstream. A raw answer or an unaccepted candidate cannot.
        existing = {r["id"] for r in (current["specification"] or {}).get("requirements", [])}
        for receipt in current["receipts"]:
            if receipt["owner"] != "clarify":
                continue
            proof = get(receipt["review_key"])
            revision = proof["basis"].get("revision", {})
            for index, disposition in enumerate(receipt["dispositions"]):
                leaves = [revision["old"][ident] for ident in disposition["old_ids"]]
                successor_leaves = [revision["new"][ident] for ident in disposition["new_ids"]]
                ids = {row["entity"] for row in leaves if row["field"] in {"requirement", "acceptance"}}
                removed = ids - existing
                retire_ids = [ident for ident, row in old.items()
                    if row["requirements"] and set(row["requirements"]) <= (
                        removed if row["field"] == "task_links" else ids)]
                link_ids = [ident for ident, row in old.items()
                    if set(row["requirements"]) & removed]
                permitted = sorted(set(retire_ids) | set(link_ids))
                if permitted:
                    sources["accepted_spec:" + receipt["key"] + ":" + str(index)] = {
                        "kind": "accepted_spec_revision", "owner": "clarify",
                        "text": disposition["reason"], "receipt": receipt["key"],
                        "review_key": receipt["review_key"], "disposition": disposition,
                        "old_leaves": leaves, "successor_leaves": successor_leaves,
                        "allowed_old_ids": permitted, "retire_old_ids": retire_ids,
                        "withdrawn_requirement_ids": sorted(removed), "retire": True,
                        "scope": "Requirement IDs only delimit candidates for joint semantic review. "
                            "A plan retirement must serve only the specifically withdrawn/replaced leaf; "
                            "all other acceptance under the same ID remains binding. This is not automatic authorization."}
    result = {"version": 1, "owner": owner, "binding": current["binding"], "previous": previous,
              "origin_registry": get("task_origins") or {}, "plan_revision": get("plan_revision/current"),
              "old": old, "new": new, "previous_versions": {digest(v): v for v in versions},
              "kept": kept, "changed": changed, "added": added,
              "sources": sources, "effective_requirement_ids": sorted({
                  r["id"] for spec in ([current["specification"]] if current["receipts"] else
                    current["base"].get("legacy_specifications") or [current["specification"]])
                  for r in (spec or {}).get("requirements", [])})}
    return result


def extend_basis(body, revision):
    if not revision:
        return
    from .review_authority import source_text
    owner = revision["owner"]
    kind = "accepted_interpretation" if owner == "clarify" else "accepted_technical_contract"
    root = "reentry_previous"
    body["sources"][root] = {"kind": kind, "owner": owner, "text": source_text(revision["previous"]),
        "revision_binding": revision["binding"], "value": revision["previous"]}
    for side, row_kind in (("old", kind), ("new", "proposal")):
        for ident, row in revision[side].items():
            key = "revision:" + side + ":" + ident
            clause = row["value"]["value"] if row["field"] == "evidence_obligation" else row["value"]
            source = {"kind": row_kind, "owner": owner, "text": source_text(clause),
                      "revision_item_id": ident, "value": row["value"]}
            if side == "old":
                previous = revision["previous_versions"][row["source_document_hash"]]
                historical_root = root + ":" + digest(previous)
                body["sources"][historical_root] = {"kind": kind, "owner": owner,
                    "text": source_text(previous), "value": previous, "revision_binding": revision["binding"]}
                source.update(root=historical_root, revision_binding=revision["binding"])
                if row.get("contract_target"):
                    source["contract_target"] = row["contract_target"]
            body["sources"][key] = source
            body["criteria"].append({"id": key, "kind": row_kind, "owner": owner,
                                     "source": key, "statement": source_text(row["value"])})
    body["revision"] = revision
    for source in body["sources"].values():
        source["hash"] = digest(source["text"])


def validate_revision(assessment, basis, approved, fail):
    revision = basis.get("revision")
    if not revision:
        return
    rows = assessment.revision_dispositions
    old, new, wanted, seen, used_new = revision["old"], revision["new"], set(revision["changed"]), set(), set()
    for row in rows:
        before, after = set(row.old_ids), set(row.new_ids)
        if (len(before) != len(row.old_ids) or len(after) != len(row.new_ids)
                or not before <= wanted or before & seen or not after <= set(revision["added"])
                or after & used_new):
            fail("invalid_revision_identity_or_duplicate")
        sizes = (len(before), len(after))
        if (row.operation == "replace" and sizes != (1, 1)
                or row.operation == "split" and not (sizes[0] == 1 and sizes[1] > 1)
                or row.operation == "merge" and not (sizes[0] > 1 and sizes[1] == 1)
                or row.operation == "retire" and sizes[1] != 0):
            fail("invalid_revision_cardinality")
        authorized, withdrawal, corrections = set(), {}, set()
        for citation in row.authority:
            source = revision["sources"].get(citation.source)
            if (not source or not citation.quote.strip() or citation.quote not in source["text"]
                    or row.operation == "retire" and not source["retire"]):
                fail("revision_has_no_exact_authority")
            authorized |= set(source.get("retire_old_ids", source["allowed_old_ids"])
                              if row.operation == "retire" else source["allowed_old_ids"])
            for ident in source["allowed_old_ids"]:
                withdrawal.setdefault(ident, set()).update(source.get("withdrawn_requirement_ids", []))
            if source["kind"] == "source_dispute":
                corrections |= set(source["allowed_old_ids"])
        if row.operation == "retire":
            # Several independently accepted withdrawals can jointly retire a
            # mixed row, but only when every old requirement is covered by the
            # exact sources cited in this disposition.
            authorized |= {ident for ident in before if old[ident]["requirements"]
                and set(old[ident]["requirements"]) <= withdrawal.get(ident, set())}
        if not before <= authorized:
            fail("revision_authority_has_wrong_target")
        if row.operation != "retire":
            previous, successors = [old[key] for key in row.old_ids], [new[key] for key in row.new_ids]
            _validate_successors(previous, successors, revision, fail, withdrawal, corrections)
        seen |= before
        used_new |= after
    if approved and seen != wanted:
        fail("revision_requires_complete_dispositions")


def _validate_successors(previous, successors, revision, fail, withdrawal, corrections):
    commands = {"verification", "checks", "final_checks", "runtime_prepare", "runtime_probes"}
    for old in previous:
        family = "command" if old["field"] in commands else old["field"]
        matches = [new for new in successors if ("command" if new["field"] in commands else new["field"]) == family]
        if not matches:
            fail("revision_successor_has_wrong_kind")
        if family == "command":
            value = old["value"]
            same_cwd = [n["value"] for n in matches if n["value"]["cwd"] == value["cwd"]]
            if (not same_cwd or sum(n["expected_tests"] for n in same_cwd) < value["expected_tests"]
                    or not set(value["inputs"]) <= {p for n in same_cwd for p in n["inputs"]}
                    or not set(value["outputs"]) <= {p for n in same_cwd for p in n["outputs"]}):
                fail("revision_reduces_check_minima")
        if old["origin"] and not any(new["origin"] == old["origin"] for new in matches):
            fail("revision_changes_budget_origin")
        if family in {"task_links", "evidence_obligation"}:
            required = set(old["requirements"]) & set(revision["effective_requirement_ids"])
            after_ids = {rid for n in matches for rid in n["requirements"]}
            if old["id"] not in corrections and (
                    not required <= after_ids or not set(old["requirements"]) - after_ids <= withdrawal.get(old["id"], set())):
                fail("revision_loses_requirement_association")
            if (family == "evidence_obligation" and old["value"].get("origin") in {"user", "unknown"}
                    and any(n["value"].get("origin") != old["value"]["origin"] for n in matches)
                    and old["id"] not in corrections):
                fail("revision_changes_protected_evidence_source")


def current_revision(get, basis, candidate, answers):
    previous = basis.get("revision")
    if previous is None:
        if get(ACTIVE) and basis["scope"] in {"coverage_review", "plan"}:
            raise StateConflict("Legacy review cannot accept a contract rebuild without preservation review")
        return
    owner = previous["owner"]
    actual = make_revision(get, owner, candidate, answers, get("authority/correction"))
    if actual != previous:
        raise StateConflict("Contract rebuild baseline or accepted successors changed during review")


def acceptance_updates(get, owner, document, review_key, answers):
    """Validate again inside the transaction that accepts the replacement."""
    from .review_authority import source_identity, validate_review
    proof = get(review_key)
    if not proof or not proof["review"].get("approved") or proof.get("owned_candidate") != document:
        raise StateConflict("Contract rebuild acceptance has no exact reviewed document")
    basis = proof["basis"]
    if basis["source_identity"] != source_identity(get):
        raise StateConflict("Contract rebuild sources changed before acceptance")
    assessment = validate_review(proof["review"], basis)
    current_revision(get, basis, document, answers)
    revision = basis.get("revision")
    if not revision:
        return {}, None
    active = get(ACTIVE)
    receipt = {"version": 1, "base": active["base"], "previous_head": active["head"],
               "owner": owner, "document": document, "review_key": review_key,
               "review_hash": proof["review_hash"],
               "dispositions": [row.model_dump() for row in assessment.revision_dispositions]}
    key = ROOT + digest(receipt) + "/accepted"
    updates = {key: receipt}
    if owner == "plan":
        updates[ROOT + digest([active, key]) + "/closed"] = {"binding": active, "accepted": key}
        return updates, "close"
    updates[ACTIVE] = {**active, "head": key}
    return updates, None


def legacy_baseline(records, rid, replans):
    """Migrate only a uniquely evidenced open rebuild, never select a latest guess.

    Old accepted intermediate specifications remain protection sources until a
    new joint clarification review explicitly accounts for their differences.
    No historical answer or unfinished permit is promoted to retirement authority.
    """
    if records.get(ACTIVE) or records.get("plan") is not None:
        return {}
    histories = {int(key.rsplit("/", 1)[-1]): value for key, value in records.items()
                 if key.startswith("recovery/gate-history/") and key.rsplit("/", 1)[-1].isdigit()}
    revision = (records.get("plan_revision/current") or {}).get("revision", 1)
    archived_plan = records.get(f"plan_revision/history/{revision - 1}", {}).get("plan")
    if revision > 1:
        anchors = [g for g, h in histories.items() if h.get("checkpoints", {}).get("plan") == archived_plan
                   and archived_plan is not None and g < replans]
        if len(anchors) != 1:
            raise StateConflict("Legacy contract rebuild has no unique plan/history/revision baseline")
        generation = anchors[0]
    else:
        if any(h.get("checkpoints", {}).get("plan") for h in histories.values()):
            raise StateConflict("Legacy accepted plan history contradicts its current revision")
        candidates = [g for g, h in histories.items() if h.get("checkpoints", {}).get("clarify") and g < replans]
        if not candidates:
            return {}
        generation = min(candidates)
    chain = []
    for index in range(generation, replans):
        history = histories.get(index)
        if not history or history.get("version") != 1 or history.get("pending", {}).get("generation") != index:
            raise StateConflict("Legacy contract rebuild has an incomplete generation chain")
        if index != generation and history.get("checkpoints", {}).get("plan") is not None:
            raise StateConflict("Legacy contract rebuild crosses another accepted plan")
        request = history["pending"].get("correction_request")
        if request:
            expected_revision = revision - 1 if index == generation and archived_plan else revision
            expected_plan = archived_plan if index == generation else None
            if (request.get("plan_revision") != expected_revision
                    or request.get("bindings", {}).get("plan") != digest(expected_plan)):
                raise StateConflict("Legacy contract rebuild source bindings do not match its history")
        chain.append(history)
    first = chain[0]
    specs = [h["checkpoints"]["clarify"] for h in chain if h.get("checkpoints", {}).get("clarify")]
    if records.get("clarify"):
        specs.append(records["clarify"])
    if not specs:
        raise StateConflict("Legacy contract rebuild has no accepted specification source")
    request = first["pending"].get("correction_request")
    if (request and request.get("bindings", {}).get("clarify") != digest(specs[0])
            or not request and not first["checkpoints"].get("clarify")):
        raise StateConflict("Legacy specification cannot be located in its chronological accepted history")
    unique = list({digest(value): value for value in specs}.values())
    body = {"version": 1, "run": rid, "generation": generation,
        "history_key": f"recovery/gate-history/{generation}", "history_hash": digest(first),
        "specification": specs[0], "plan": archived_plan,
        "plan_revision": {"revision": revision - 1 if archived_plan else revision},
        "submitted_answers": records.get("authority/submitted-answers", []),
        "check_production": records.get("evidence_admission/active"),
        "check_production_required": records.get("evidence_admission/check_production_required"),
        "correction_request": request, "legacy_specifications": unique,
        "legacy_current_specification": specs[-1],
        "legacy_history": {f"recovery/gate-history/{g}": digest(histories[g])
                           for g in range(generation, replans)},
        "migration_scope": "Protection sources only; no inferred authorization, acceptance or counter reset."}
    key = ROOT + digest(body) + "/base"
    return {key: body, ACTIVE: {"version": 1, "base": key, "head": None}}


def admission_previous(get):
    """Historical mapping input only, never a live execution plan or new approval."""
    if get("plan") is not None or not get(ACTIVE):
        return None
    current = state(get)
    from .check_production import ACTIVE_KEY, REQUIRED_KEY
    if (get(ACTIVE_KEY) != current["base"].get("check_production")
            or get(REQUIRED_KEY) != current["base"].get("check_production_required")):
        raise StateConflict("Rebuild prior check-production receipt changed from its frozen accepted source")
    from .models import PlanResult
    return PlanResult.model_validate(current["plan"]) if current["plan"] else None


def preserve_production(plan, verdict, context, assessment):
    """Mapped source changes do not silently downgrade unchanged executable proof."""
    revision = context.get("controller_review_basis", {}).get("revision")
    if not revision or revision["owner"] != "plan" or not revision["previous"]:
        return
    from .check_production import preserve_mapping
    from .models import PlanResult
    targets = set()
    for disposition in assessment.revision_dispositions:
        for ident in disposition.old_ids:
            row = revision["old"][ident]
            if row.get("contract_target") and row["field"] != "evidence_obligation":
                target = row["contract_target"]
                targets.add((target["target"], target["task_id"], target["index"]))
    preserve_mapping(PlanResult.model_validate(revision["previous"]), plan,
        context.get("accepted_check_production"), verdict.evidence_assessment.check_production,
        {}, mapped_targets=targets)


def needs_clarification(get):
    current = state(get)
    return bool(current and len(current["base"].get("legacy_specifications", [])) > 1
        and not any(row["owner"] == "clarify" for row in current["receipts"]))
