"""Bounded historical proposal inputs for a normal prototype re-entry."""
from __future__ import annotations

import json
import re

from .approvals import prototype_identity
from .faults import StateConflict
from .models import DocumentResult
from .stage_responsibilities import validate_correction_request
from .state_codec import checkpoint_query_db
from .util import digest
from .workspace import tree_hash


class PrototypeReentry:
    def __init__(self, engine):
        self.engine, self.store, self.artifacts = engine, engine.store, engine.artifacts

    @staticmethod
    def _document(history):
        records = history.get("checkpoints", {})
        return (records.get("prototype/pending", {}).get("document")
                or records.get("prototype") or records.get("invocation/prototype", {}).get("result"))

    def _transition_approval(self, rid, history):
        records = history.get("checkpoints", {})
        if "prototype_approval" in records:
            return records["prototype_approval"]
        expected = history.get("pending", {}).get("correction_request", {}).get(
            "bindings", {}).get("prototype_approval")
        current = self.store.get(rid, "prototype_approval")
        if digest(current) == expected or not isinstance(expected, str):
            return current
        # Normal publication archives the exact previous receipt. A later
        # approval must not replace the preimage bound by this completed reset.
        retained = self.store.get(rid, "candidate/prototype-approval-history/" + expected)
        return retained if retained is not None and digest(retained) == expected else current

    def _transition(self, rid, work, history, generation, feedback):
        pending = history.get("pending", {})
        request = feedback.get("correction_request")
        stage = feedback.get("stage")
        if (history.get("version") != 1 or pending.get("generation") != generation
                or stage not in {"clarify", "provider_research", "prototype"}
                or pending.get("stage") != stage or not pending.get("granted")
                or pending.get("reason") != feedback.get("reason")
                or not request or pending.get("correction_request") != request):
            return False
        archived = history.get("checkpoints", {})
        def before(key):
            if key == "plan_revision/current":
                return {"revision": request.get("plan_revision")}
            if key in archived:
                return archived[key]
            if key in request.get("invalidate_stages", []):
                return None
            if key == "prototype_approval":
                return self._transition_approval(rid, history)
            return self.store.get(rid, key)
        try:
            validate_correction_request(request, before, action=stage)
        except (StateConflict, KeyError, TypeError, ValueError):
            return False
        permit_key = pending.get("correction_permit") or pending.get("permit")
        permit = self.store.get(rid, permit_key) if permit_key else None
        source = pending.get("correction_source", {})
        return bool(permit and request.get("permit") == permit_key
                    and permit.get("phase") == "completed"
                    and permit.get("outcome") == {"status": "gate_reset", "stage": stage}
                    and permit.get("outcome_hash") == digest(permit.get("outcome"))
                    and source.get("workspace") == str(work)
                    and source.get("hash") == permit.get("source_hash"))

    def _review(self, history, reviews):
        pending = history.get("checkpoints", {}).get("prototype/pending", {})
        if not pending.get("fingerprint") or "visuals" not in pending:
            return {"status": "not_available"}
        document = self._document(history)
        candidate = digest([pending["fingerprint"], digest(document.get("browser_actions", []))])
        bindings = history.get("pending", {}).get("correction_request", {}).get("bindings", {})
        matches = []
        for key, record in reviews:
            basis, review, source = (record.get(name, {}) for name in ("basis", "review", "source"))
            identity = record.get("source_identity", {})
            if (basis.get("candidate_hash") != candidate
                    or basis.get("observations_hash") != digest(pending["visuals"])
                    or not bindings.get("clarify") or basis.get("specification_hash") != bindings["clarify"]
                    or any(identity.get(name) != bindings.get(name) for name in ("capture", "clarify"))
                    or source.get("specification_hash") != basis.get("specification_hash")
                    or basis.get("source_hash") != digest(source)
                    or identity.get("authority/submitted-answers") != digest(source.get("submitted_answers", []))):
                continue
            if (key != "authority/prototype-reviews/" + digest(review)
                    or basis.get("hash") != digest({k: v for k, v in basis.items() if k != "hash"})
                    or review.get("basis_hash") != basis["hash"] or review.get("candidate_hash") != candidate):
                raise StateConflict("Retained prototype review identity is inconsistent; preserve the archive")
            matches.append((key, record))
        if len(matches) != 1:
            return {"status": "not_available" if not matches else "ambiguous", "matching_records": len(matches)}
        key, record = matches[0]
        # This proves which historical record was supplied, not that an old
        # protocol/source review is valid for this new candidate or requirement.
        return {"status": "historical", "checkpoint": key, "hash": digest(record),
                "report": self.artifacts.put(record, "prototype-reentry-review"), "required_read": True}

    def _candidate(self, key, history, reviews):
        records = history.get("checkpoints", {})
        invocation = records.get("invocation/prototype", {})
        raw = invocation.get("result")
        document = self._document(history)
        if not isinstance(document, dict) or not raw or not invocation.get("identity") or not invocation.get("artifact"):
            raise StateConflict("Retained prototype candidate lacks its original invocation receipt")
        if self.artifacts.json(invocation["artifact"]) != raw:
            raise StateConflict("Retained prototype artifact differs from its invocation")
        provider = records.get("provider-result/prototype")
        if provider and provider.get("artifact") != invocation["artifact"]:
            raise StateConflict("Retained prototype provider and invocation receipts disagree")
        fields = DocumentResult.model_fields
        normalized = DocumentResult.model_validate({k: v for k, v in document.items() if k in fields}).model_dump(mode="json")
        original = DocumentResult.model_validate({k: v for k, v in raw.items() if k in fields}).model_dump(mode="json")
        # PatchRepair may normalize file operations and resolved questions may
        # disappear. Full non-file proposal fields must retain their author.
        if any(normalized.get(k) != original.get(k) for k in normalized.keys() | original.keys() if k not in {"files", "questions"}):
            raise StateConflict("Retained prototype fields disagree with their author receipt")
        return {"checkpoint": key, "history_hash": digest(history),
                "invocation": {"identity": invocation["identity"], "proposal": invocation["artifact"]},
                "source": history.get("pending", {}).get("correction_source", {}),
                "reason": history.get("pending", {}).get("reason", ""),
                "proposal_hash": digest(document),
                "proposal": {"artifact": self.artifacts.put(document, "prototype-reentry-proposal"),
                             "required_read": True,
                             "scope": "Historical model proposal only. Read the full fields; never replay its files."},
                "review": self._review(history, reviews), "human_binding_matches": False}

    def _consent(self, rid, work, approval, document):
        actions = document.get("browser_actions", [])
        human = approval.get("human", {})
        question = self.store.question(human.get("question_id")) if human.get("question_id") else None
        if (not question or question["run_id"] != rid or human.get("run_id") != rid
                or question["key"] != "prototype-approval"
                or not self.engine.approvals._approved({**question, "interaction": json.dumps(question["interaction"])})
                or approval.get("actions_hash") != digest(actions)
                or approval.get("identity") != digest([approval.get("hash"), digest(actions)])
                or question["artifact_hash"] != approval["identity"]
                or human.get("artifact_hash") != question["artifact_hash"]
                or human.get("actions_hash") != approval["actions_hash"]
                or human.get("content_hash") != approval["hash"]):
            return None
        binding = prototype_identity(work, actions)
        matches = all(approval.get(k) == binding[k] for k in ("hash", "actions_hash", "identity"))
        if matches:
            matches = self.engine.approvals.human(rid, work, actions, approval, search_history=False) == human
        return {"human_binding_matches": matches, "consent": {"question": question, "proof": human,
                "approval_hash": digest(approval)}, "current_binding": binding}

    def context(self, rid, work):
        generation = self.store.run(rid)["data"].get("replans", 0) - 1
        feedback = self.store.get(rid, "replan_feedback", {})
        key = f"recovery/gate-history/{generation}"
        latest = self.store.get(rid, key)
        if not latest:
            return {}
        base = {"version": 1, "current_source_hash": tree_hash(work),
                "current_automatic_gates": "not_evaluated", "candidates": []}
        if not self._transition(rid, work, latest, generation, feedback):
            return {**base, "status": "unverified_transition"}
        with self.store.connect() as db:
            histories = [(r["key"], json.loads(r["data"])) for r in checkpoint_query_db(db, "SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=? AND key LIKE 'recovery/gate-history/%'", (rid,), wire_index=1)]
            reviews = [(r["key"], json.loads(r["data"])) for r in checkpoint_query_db(db, "SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=? AND key LIKE 'authority/prototype-reviews/%'", (rid,), wire_index=1)]
        histories = sorted([(k, h) for k, h in histories if re.fullmatch(r"recovery/gate-history/\d+", k)
                            and int(k.rsplit("/", 1)[1]) <= generation and self._document(h)],
                           key=lambda row: int(row[0].rsplit("/", 1)[1]), reverse=True)
        chosen = []
        if histories:
            chosen.append(("previous", *histories[0], None))
        actions = next(((k, h) for k, h in histories if self._document(h).get("browser_actions")), None)
        if actions:
            chosen.append(("recent_actions", *actions, None))
        approval = self._transition_approval(rid, latest) or {}
        for k, h in histories:
            document = self._document(h)
            records = h.get("checkpoints", {})
            pending = records.get("prototype/pending", {})
            if not records.get("prototype") and not (
                    pending.get("fingerprint") == approval.get("hash") and pending.get("review", {}).get("approved")):
                continue
            consent = self._consent(rid, work, approval, document)
            if consent:
                chosen.append(("human_baseline", k, h, consent))
                break
        selected = {}
        for role, k, h, consent in chosen:
            candidate = self._candidate(k, h, reviews)
            origin = {name: candidate[name] for name in ("checkpoint", "history_hash", "source", "reason", "review")}
            existing = selected.setdefault(candidate["proposal_hash"], {**candidate, "roles": [], "origins": []})
            existing["roles"].append(role)
            existing["origins"].append({"role": role, **origin})
            if consent:
                existing.update(consent)
        return {**base, "status": "historical_reentry", "candidates": list(selected.values()),
                "transition": {"checkpoint": key, "hash": digest(latest),
                               "source_matches_current": latest["pending"]["correction_source"]["hash"] == base["current_source_hash"],
                               "permit": latest["pending"].get("correction_permit") or latest["pending"].get("permit")},
                "instruction": "Read each complete required proposal and its bound historical review/reason. "
                               "These are correction references, not current acceptance or new user authority. "
                               "Preserve valid obligations in a complete new proposal; do not union rejected fields, "
                               "infer coverage from action counts, or replay historical file patches. "
                               "Current source, browser checks, independent review and human consent gates still apply."}
