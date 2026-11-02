"""Bounded retained evidence for clarification after an authorized gate reset."""
from __future__ import annotations

import copy
import json
import re

from .approvals import prototype_identity
from .faults import StateConflict
from .models import IntakeResult
from .stage_responsibilities import validate_correction_request
from .util import digest
from .workspace import tree_hash


class ClarificationReentry:
    def __init__(self, engine):
        self.engine = engine
        self.store, self.artifacts = engine.store, engine.artifacts
        self.guards = {}
        self.consent = {}
        self.original = None
        self.receipt = None
        self.key = ""
        self.source = ""
        self.question_prefix = None
        self.replans = None

    def _get(self, rid, key):
        value = self.store.get(rid, key)
        self.guards[key] = value
        return value

    def _bound_questions(self, rid, keys, fingerprint):
        return [q for q in self.store.decision_questions(rid, keys) if q["artifact_hash"] == fingerprint]

    def _previous(self, rid, receipt):
        if receipt:
            return receipt["original"]
        previous = self.store.get(rid, "invocation/clarify", {})
        result = previous.get("result", {})
        if not previous.get("identity") or not result.get("questions"):
            return None
        fingerprint = digest(result)
        with self.store.connect() as db:
            rows = list(db.execute("SELECT key,answer FROM questions WHERE run_id=? AND superseded=0 "
                                   "AND artifact_hash=?", (rid, fingerprint)))
        rows = [row for row in rows if re.fullmatch(r"clarify/\d+/\d+", row["key"])]
        pending = {row["key"].rsplit("/", 1)[0] for row in rows if row["answer"] is None}
        if len(pending) > 1:
            raise StateConflict("Retained clarification has multiple waiting batches; preserve its questions")
        prefixes = pending or {row["key"].rsplit("/", 1)[0] for row in rows}
        if len(prefixes) != 1:
            return None  # Multiple fully answered batches remain in submitted_answers.
        prefix = prefixes.pop()
        keys = [f"{prefix}/{index}" for index in range(len(result["questions"]))]
        questions = self._bound_questions(rid, keys, fingerprint)
        if not questions:
            raise StateConflict("Retained clarification questions changed during selection")
        presentation_key = "presentation/clarify/" + previous["identity"]
        presentation = self.store.get(rid, presentation_key)
        if not presentation:
            raise StateConflict("Retained clarification has no presentation receipt; preserve its question")
        try:
            prepared = self.artifacts.json(presentation["artifact"])
            matches = (presentation.get("identity") == previous["identity"]
                       and self.artifacts.json(previous["artifact"]) == result)
        except (KeyError, OSError, ValueError):
            raise StateConflict("Retained clarification materials are incomplete; preserve its question") from None
        if not matches:
            raise StateConflict("Retained clarification receipt does not match its question")
        if prepared.get("purpose") != "clarify" or "controller_reentry" in prepared.get("payload", {}):
            return None
        return {"invocation": previous, "presentation_key": presentation_key,
                "presentation": presentation, "questions": questions, "keys": keys, "question_prefix": prefix}

    def context(self, rid, work):
        from .contract_reentry import ACTIVE
        self._get(rid, ACTIVE)
        feedback = self._get(rid, "replan_feedback") or {}
        request = feedback.get("correction_request", {})
        if feedback.get("stage") != "clarify" or request.get("action") != "clarify":
            return {}
        self.replans = self.store.run(rid)["data"].get("replans", 0)
        generation = self.replans - 1
        history_key = f"recovery/gate-history/{generation}"
        history = self._get(rid, history_key) or {}
        pending = history.get("pending", {})
        self.source = tree_hash(work)
        unavailable = {"version": 1, "status": "unverified_reentry", "history_key": history_key,
                       "scope": "Historical evidence only; no current approval or permission."}
        if (history.get("version") != 1 or pending.get("generation") != generation
                or pending.get("stage") != "clarify" or not pending.get("granted")
                or pending.get("correction_request") != request
                or pending.get("reason") != feedback.get("reason")):
            return {**unavailable, "reason": "No matching retained clarification transition"}
        archived = history.get("checkpoints", {})

        def before(key):
            current = self._get(rid, key)
            if key == "plan_revision/current" and "plan" in archived:
                # complete_replan advances this retained counter when it archives a plan.
                revision = request.get("plan_revision")
                if not isinstance(revision, int) or (current or {}).get("revision") != revision + 1:
                    raise StateConflict("Retained plan revision does not match the recorded reset")
                return {"revision": revision}
            return archived[key] if key in archived else current

        permit_key = request.get("permit")
        permit = self._get(rid, permit_key) if permit_key else None
        try:
            validate_correction_request(request, before, action="clarify")
        except (StateConflict, KeyError, TypeError, ValueError):
            return {**unavailable, "reason": "Retained transition inputs or authorization do not match"}
        if (not permit or (pending.get("correction_permit") or pending.get("permit")) != permit_key
                or permit.get("phase") != "completed"
                or permit.get("outcome") != {"status": "gate_reset", "stage": "clarify"}
                or permit.get("outcome_hash") != digest(permit.get("outcome"))
                or permit.get("source_hash") != self.source
                or pending.get("correction_source") != {"workspace": str(work), "hash": self.source}):
            return {**unavailable, "reason": "Completed transition receipt or current source does not match"}
        self.key = "recovery/clarify-reentry/" + digest([history_key, history])
        self.receipt = self.store.get(rid, self.key)
        self.original = self._previous(rid, self.receipt)
        result = {
            "version": 1, "status": "verified_reentry", "source_hash": self.source,
            "scope": "The controller verified this recorded transition and its input bindings. "
                     "Historical candidates and reviews are not current acceptance or new user requirements.",
            "transition": {"checkpoint": history_key, "hash": digest(history),
                           "report": self.artifacts.put(history, "clarify-reentry-history")},
            "operation": {"checkpoint": permit_key, "hash": digest(permit),
                          "report": self.artifacts.put(permit, "clarify-reentry-permit")},
            "instruction": "Use these retained records to resolve controller-material questions. "
                           "Interpret the original request and submitted answers; preserve genuine product choices "
                           "and missing user information as questions. Do not ask the user to retrieve private "
                           "receipts or reports. Submit the clarification for ordinary coverage review; the "
                           "controller separately revalidates prototype consent, browser evidence and review. "
                           "No historical review is certified for the current requirements by this material.",
        }
        approval = self._get(rid, "prototype_approval")
        if approval:
            result["retained_prototype"] = self._prototype(rid, work, approval, generation)
        if self.original:
            old = self.original["invocation"]
            result["previous_clarification"] = {
                "identity": old["identity"], "proposal": old["artifact"],
                "scope": "Unaccepted model proposal. Reassess its questions with the supplied records; "
                         "keep unresolved product questions and all substantive obligations.",
            }
        return result

    def _prototype(self, rid, work, approval, generation):
        evidence = {
            "status": "historical", "current_automatic_gates": "not_evaluated",
            "record": self.artifacts.put(approval, "retained-prototype-approval"),
            "human_binding_matches": False,
        }
        with self.store.connect() as db:
            histories = [(row["key"], json.loads(row["data"])) for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/gate-history/%'",
                (rid,))]
        candidates = [("prototype", self._get(rid, "prototype"))]
        histories = [(key, history) for key, history in histories if re.fullmatch(r"recovery/gate-history/\d+", key)]
        for key, history in sorted(histories, key=lambda row: int(row[0].rsplit("/", 1)[1]), reverse=True):
            if int(key.rsplit("/", 1)[1]) <= generation:
                candidates.append((key, history.get("checkpoints", {}).get("prototype")))
        for origin, prototype in candidates:
            if not isinstance(prototype, dict):
                continue
            actions = prototype.get("browser_actions", [])
            binding = prototype_identity(work, actions)
            if any(approval.get(key) != binding[key] for key in ("hash", "identity", "actions_hash")):
                continue
            question_id = approval.get("human", {}).get("question_id")
            question = self.store.question(question_id) if question_id else None
            if (not question or question["run_id"] != rid or question["key"] != "prototype-approval"
                    or approval.get("human", {}).get("run_id") != rid
                    or question["artifact_hash"] != binding["identity"]):
                continue
            proof = self.engine.approvals.human(rid, work, actions, approval, search_history=False)
            if not proof or proof != approval["human"]:
                continue
            self.consent[question_id] = question
            self._get(rid, origin)
            evidence.update(human_binding_matches=True, binding=binding,
                consent={"question": question, "proof": proof},
                candidate={"checkpoint": origin, "proposal_digest": digest(prototype),
                           "proposal": self.artifacts.put(prototype, "retained-prototype-proposal")})
            report = self._get(rid, "prototype_browser_report")
            if report:
                evidence["report"] = report
            break
        return evidence

    async def refresh(self, rid, work, context):
        """Replace only the legacy proposal's unanswered wait, after a guarded new call."""
        if not self.original or self.receipt:
            return None
        original = self.original
        previous = original["invocation"]
        current = self._bound_questions(rid, original["keys"], digest(previous["result"]))
        if any(q["answer"] is not None and q["artifact_hash"] == digest(previous["result"]) for q in current):
            self.question_prefix = original["question_prefix"]
            return IntakeResult.model_validate(previous["result"])
        if current != original["questions"]:
            raise StateConflict("Clarification questions changed before retained-evidence refresh")
        operation = "clarify/reentry-refresh/" + digest([previous["identity"], context, self.source])
        result = await self.engine._invoke("clarify", context, IntakeResult, work, rid, operation, fresh=True)
        if tree_hash(work) != self.source:
            raise StateConflict("Clarification source changed during retained-evidence refresh")
        refreshed = self.store.get(rid, "invocation/" + operation)
        record = {"version": 1, "source_hash": self.source, "replans": self.replans,
                  "original": copy.deepcopy(original),
                  "operation": operation, "refreshed": refreshed,
                  "materials": context["controller_reentry"]}
        accepted = self.store.refresh_clarify_questions(rid, original, refreshed, self.guards, self.consent,
            context["submitted_answers"], operation, self.key, record)
        # A real answer received during the call wins; consume it in the original question loop.
        if not accepted:
            self.question_prefix = original["question_prefix"]
            return IntakeResult.model_validate(previous["result"])
        return result
