"""Bounded decisions over existing provider transport; never a verification gate."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, ValidationError

from .errors import AA2Error, InvalidStructuredResponse, PostCommitError
from .interaction import choice_spec, is_prototype_approval
from .models import Record
from .util import digest
from .workspace import tree_hash

VALIDATION_VERSION = 2
LEGACY_FALLBACK_REASON = "自动决策未返回有效结论，保留现场并交由用户决定。"


class DecisionValidationError(ValueError):
    """Controller-authored feedback without echoing arbitrary provider output."""

    def __init__(self, code, field, message, **details):
        super().__init__(message)
        self.feedback = {"code": code, "field": field, "message": message, **details}


def decision_evidence_references(evidence):
    """Resolve paths and artifact IDs from supplied data, never from proposal text."""
    refs = set()

    def visit(value, path):
        if path:
            refs.add(path)
        if isinstance(value, dict):
            if isinstance(value.get("hash"), str) and re.fullmatch(r"[a-f0-9]{64}", value["hash"]):
                refs.add(value["hash"])
            for key, item in value.items():
                if key not in {"proposal", "recovery_request", "original_proposal"}:
                    visit(item, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(evidence, "")
    # RecoveryController already indexes its failure's public files and records.
    # Do not promote citations from diagnoses, history or other model proposals.
    failure = evidence.get("failure")
    if isinstance(failure, dict):
        refs.update(ref for ref in failure.get("evidence_refs", []) if isinstance(ref, str))
    return refs


def decision_request_identity(request):
    data = request.model_dump()
    if request.kind == "recovery_action":
        # The decision's own token charges must not create another decision on resume.
        data["evidence"].get("consumption", {}).pop("tokens", None)
    return digest(data)


class FactAnswer(Record):
    index: int = Field(ge=0)
    fact_ref: str


class DecisionResult(Record):
    action: Literal["approve", "answer", "ask_user"]
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = []
    answers: list[FactAnswer] = []
    selected_action: str | None = None
    expected_result: str = ""


class DecisionRequest(Record):
    version: Literal[1] = 1
    kind: Literal["implementation_grant", "diagnosis_grant", "technical_clarification", "recovery_action"]
    owner: str
    allowed_choices: list[str]
    scope: dict
    evidence: dict
    budget: dict = {}
    state_identity: str


class DecisionService:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def guards(self, rid, owner, extra=()):
        keys = ["plan", "clarify", "prototype_approval", "recovery/active", "replanning/pending",
                "automation/progress/" + owner, "rounds/" + owner,
                "budget_grants/" + owner, "recovery/budgets/diagnosis/" + owner,
                "recovery/diagnosis_grants/" + owner, *extra]
        return {key: self.store.get(rid, key) for key in keys}

    async def propose(self, rid, request, work):
        """At most one response and one correction, including across restarts."""
        key = "decisions/requests/" + digest(request.model_dump())
        saved = self.store.get(rid, key)
        if saved is None and request.kind == "recovery_action":
            identity = decision_request_identity(request)
            for previous_key, previous in self.store.decision_requests(rid, request.state_identity):
                original = DecisionRequest.model_validate(previous["request"])
                if decision_request_identity(original) == identity:
                    key, saved, request = previous_key, previous, original
                    break
        if saved is None:
            saved = {"request": request.model_dump(), "attempts": 0}
        if saved.get("result"):
            self._revalidate_legacy(rid, key, saved, request)
            return key, DecisionResult.model_validate(saved["result"])
        while (saved.get("pending") or saved["attempts"] < 2) and not saved.get("failed"):
            # Reserve before calling. An interrupted request never gets an unbounded retry.
            if not saved.get("pending"):
                saved["attempts"] += 1
                saved["pending"] = True
                saved["attempt_context"] = {
                    "validation_feedback": saved.get("error"),
                    "evidence_reference_contract": {
                        "roots": sorted(request.evidence),
                        "syntax": "Cite an existing path in decision_request.evidence using dot fields and [index], "
                                  "for example failure.evidence.checks[0] or history.recent[0].outcome.diagnosis. "
                                  "A supplied artifact hash or controller-indexed failure.evidence_refs entry also works. "
                                  "A root name is valid. Proposal prose cannot create evidence references.",
                    },
                }
            # Preserve the exact context of an interrupted older invocation.
            context = {"decision_request": request.model_dump(), **saved.get("attempt_context", {
                "validation_feedback": saved.get("error")})}
            self.store.checkpoint(rid, key, saved)
            try:
                result = await self.engine._invoke("decision", context,
                    DecisionResult, work, rid, key + f"/{saved['attempts']}", fresh=True, interactive_retry=False)
                if not isinstance(result, DecisionResult):
                    raise ValueError("Expected DecisionResult")
                self.validate(request, result)
            except (ValueError, ValidationError, InvalidStructuredResponse) as exc:
                saved["pending"] = False
                saved["error"] = self.validation_feedback(exc)
                saved.setdefault("rejections", []).append({"attempt": saved["attempts"], "feedback": saved["error"]})
                self.store.checkpoint(rid, key, saved)
                continue
            except PostCommitError:
                raise  # Resume the same durable result; never spend a new decision attempt.
            except AA2Error as exc:
                # No decision-agent recursion for authentication, provider or drift failures.
                saved.update(failed=True, error=type(exc).__name__)
                self.store.checkpoint(rid, key, saved)
                break
            saved["result"] = result.model_dump()
            saved["validation_version"] = VALIDATION_VERSION
            self.store.checkpoint(rid, key, saved)
            return key, result
        error = saved.get("error")
        detail = (f"字段 {error['field']}，错误 {error['code']}" if isinstance(error, dict) else str(error))
        result = DecisionResult(action="ask_user", reason=f"自动决策未返回有效结论（{detail}），保留现场并交由用户决定。")
        self.store.checkpoint(rid, key, {**saved, "result": result.model_dump(),
            "validation_version": VALIDATION_VERSION, "fallback": True})
        return key, result

    def _revalidate_legacy(self, rid, key, saved, request):
        """Reassess only the last committed response rejected by the old validator."""
        if (saved.get("validation_version", 1) >= VALIDATION_VERSION or saved.get("failed")
                or saved.get("pending") or saved.get("attempts") != 2
                or saved.get("error") != "ValueError: invalid action, answer or evidence reference"
                or saved["result"] != DecisionResult(action="ask_user", reason=LEGACY_FALLBACK_REASON).model_dump()):
            return
        invocation_key = f"invocation/{key}/{saved['attempts']}"
        invocation = self.store.get(rid, invocation_key, {})
        review = {"invocation": invocation_key, "previous_result": saved["result"], "previous_error": saved["error"]}
        try:
            result = DecisionResult.model_validate(invocation.get("result"))
            self.validate(request, result)
        except (ValueError, ValidationError) as exc:
            review["feedback"] = self.validation_feedback(exc)
        else:
            if invocation.get("cleanup_record"):
                self.engine.sandbox.confirm_record(invocation["cleanup_record"])
            saved["result"] = result.model_dump()
            review["valid"] = True
        saved.update(validation_version=VALIDATION_VERSION, revalidation=review)
        self.store.checkpoint(rid, key, saved)

    @staticmethod
    def validation_feedback(exc):
        if isinstance(exc, DecisionValidationError):
            return exc.feedback
        if isinstance(exc, ValidationError):
            fields = DecisionResult.model_fields.keys() | FactAnswer.model_fields.keys()
            return {"code": "invalid_schema", "field": "response", "message": "Return a complete DecisionResult.",
                    "errors": [{"field": [part if isinstance(part, int) or part in fields else "<unknown>"
                                          for part in error["loc"]], "type": error["type"]}
                               for error in exc.errors(include_input=False, include_context=False, include_url=False)]}
        return {"code": "invalid_structured_response", "field": "response", "message": "Return a complete DecisionResult."}

    @staticmethod
    def validate(request, result):
        if result.action not in request.allowed_choices:
            raise DecisionValidationError("action_not_allowed", "action", "Choose an allowed action.",
                                          allowed_choices=request.allowed_choices)
        refs = decision_evidence_references(request.evidence)
        unresolved = [i for i, ref in enumerate(result.evidence_refs) if ref not in refs]
        if unresolved:
            raise DecisionValidationError("unknown_evidence", "evidence_refs", "Cite only supplied evidence paths, "
                "artifact hashes or controller-indexed references; a root name is also valid.",
                unresolved_indexes=unresolved, allowed_roots=sorted(request.evidence))
        if result.action != "ask_user" and not result.evidence_refs:
            raise DecisionValidationError("evidence_required", "evidence_refs", "Cite at least one supplied evidence reference.")
        if request.kind == "recovery_action" and result.action == "approve":
            if result.selected_action not in request.scope["candidates"]:
                raise DecisionValidationError("unknown_candidate", "selected_action", "Select exactly one supplied candidate.",
                                              allowed_candidates=sorted(request.scope["candidates"]))
            if not result.expected_result.strip():
                raise DecisionValidationError("expected_result_required", "expected_result", "Describe an observable result.")
        if request.kind != "technical_clarification":
            if result.answers:
                raise DecisionValidationError("answers_not_allowed", "answers", "Grant cannot answer questions; return [].")
            return
        questions = request.scope["questions"]
        facts = request.evidence.get("facts", {})
        seen = set()
        for answer in result.answers:
            if answer.index in seen or str(answer.index) not in questions or answer.fact_ref not in facts:
                raise DecisionValidationError("invalid_fact", "answers", "Use distinct supplied question indexes and fact refs.")
            seen.add(answer.index)
            options = questions[str(answer.index)]["options"]
            if options and facts[answer.fact_ref] not in options:
                raise DecisionValidationError("fact_not_allowed", "answers", "Fact does not match a permitted answer.")
        if (result.action == "answer") != bool(result.answers):
            raise DecisionValidationError("answers_required", "answers", "Answer action requires factual answers.")

    async def human_action(self, rid, owner, action, reason):
        """One persistent human permit per concrete action; never reset progress."""
        progress = self.engine.convergence.state(rid, owner)
        key = "automation/human/" + digest([owner, action, progress])
        if self.store.get(rid, key):
            return
        interaction = choice_spec(["授权这一次操作", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
        interaction["pause_on"] = "n"
        await self.engine._ask(rid, key, reason + "\n仅授权这一次操作；历史计数保持不变。", digest([action, progress]), interaction)
        self.store.checkpoint(rid, key, {"actor": "human", "owner": owner, "action": action, "progress": progress})

    async def ensure_progress(self, rid, owner, action):
        """Compatibility entrypoint: progress is evidence, never an authorization gate."""
        return self.engine.convergence.state(rid, owner)

    async def grant(self, rid, *, kind, owner, count, limit_key, question_key, reason, work, evidence, extra_guards=(), force_human=False):
        if self.store.get(rid, limit_key, 0) >= count + 1:
            return
        guards = self.guards(rid, owner, extra_guards)
        policy = self.engine.config.load().automation
        ceiling = policy.max_implementation_rounds if kind == "implementation_grant" else policy.max_diagnosis_rounds
        source = tree_hash(work)
        tasks = (self.store.get(rid, "plan", {}) or {}).get("tasks", [])
        request = DecisionRequest(kind=kind, owner=owner, allowed_choices=["approve", "ask_user"],
            scope={"one_attempt": True, "same_requirements": True,
                   "tasks": [t for t in tasks if owner == "final" or (t.get("origin_id") or t["id"]) == owner]}, evidence=evidence,
            budget={"consumed": count, "grant": count + 1, "legacy_automatic_ceiling": ceiling,
                    "count_limits_enforced": False},
            state_identity=digest([guards, source, policy.model_dump()]))
        receipt = "decisions/applied/" + digest(request.model_dump())
        if self.store.get(rid, receipt):
            return
        # A human response already present takes precedence. Unanswered legacy questions
        # may be reassessed; applying an agent result supersedes them without answering.
        questions = self.store.decision_questions(rid, [question_key])
        manual = (force_human or not policy.enabled
                  or any(q["answer"] is not None for q in questions))
        if not manual:
            result_key, result = await self.propose(rid, request, work)
            if (tree_hash(work) == source and self.engine.config.load().automation == policy
                    and result.action == "approve"
                    and self.store.apply_decision(rid, receipt, request.model_dump(), result.model_dump(), "agent",
                        guards, {limit_key: count + 1}, questions, [question_key], result_key=result_key)):
                return
            self.store.event(rid, "decision.deferred", {"owner": owner, "reason": result.reason if result.action == "ask_user"
                else "授权依据或人工回答已变化，重新核对后由用户决定。"})
        elif policy.enabled:
            self.store.event(rid, "decision.deferred", {"owner": owner, "reason":
                "当前授权需要人工核对。累计次数和停滞记录不构成停止条件。"})
        interaction = choice_spec(["授权这一次操作", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
        interaction["pause_on"] = "n"
        answered = [q for q in self.store.decision_questions(rid, [question_key]) if q["answer"] is not None]
        if answered:
            # The key binds this specific action and consumed count. A wording-only
            # upgrade must not discard a human response committed before its grant.
            previous = self.store.question(answered[-1]["id"])
            await self.engine._ask(rid, question_key, previous["prompt"], previous["artifact_hash"], previous["interaction"])
        else:
            await self.engine._ask(rid, question_key, reason, digest([question_key, reason]), interaction)
        if tree_hash(work) != source:
            raise AA2Error("Decision evidence source changed while waiting; preserve the candidate")
        questions = self.store.decision_questions(rid, [question_key])
        action = ("implementation" if kind == "implementation_grant" else "diagnosis") + f"/{count + 1}"
        progress = self.engine.convergence.state(rid, owner)
        permit = "automation/human/" + digest([owner, action, progress])
        if not self.store.apply_decision(rid, receipt, request.model_dump(),
                {"action": "approve", "reason": "用户授权一次操作", "evidence_refs": list(evidence)},
                "human", guards, {limit_key: count + 1, permit: {"actor": "human", "owner": owner, "action": action}}, questions, [question_key]):
            raise AA2Error("Decision state changed; preserved authorization requires reassessment")

    def technical_facts(self, rid):
        """Only public, settled technical facts; never native provider configuration."""
        cfg = self.engine.config.load()
        facts = {"environment.python_version": cfg.environment.python_version,
                 "environment.node_version": cfg.environment.node_version}
        plan = self.store.get(rid, "plan", {}) or {}
        for kind in ("final_checks", "runtime_probes", "runtime_prepare"):
            for i, command in enumerate(plan.get(kind, [])):
                facts[f"plan.{kind}.{i}"] = f"{command.get('cwd', '.')}: " + " ".join(command["argv"])
        for task in plan.get("tasks", []):
            facts[f"plan.tasks.{task['id']}.allowed_paths"] = ", ".join(task["allowed_paths"])
        for i, command in enumerate((self.store.get(rid, "clarify", {}) or {}).get("verification", [])):
            facts[f"specification.verification.{i}"] = f"{command.get('cwd', '.')}: " + " ".join(command["argv"])
        return facts

    async def clarify(self, rid, key, questions, fingerprint, work):
        eligible = {str(i): {"prompt": q if isinstance(q, str) else q.prompt,
                             "options": [] if isinstance(q, str) else q.options}
                    for i, q in enumerate(questions) if not is_prototype_approval(q)}
        if not eligible or not self.engine.config.load().automation.enabled:
            return {}
        keys = [f"{key}/{i}" for i in eligible]
        questions_state = self.store.decision_questions(rid, keys)
        answered = {q["key"] for q in questions_state if q["answer"] is not None}
        if all(f"{key}/{i}" in answered for i in eligible):
            return {}
        guards = self.guards(rid, "clarification")
        facts, source = self.technical_facts(rid), tree_hash(work)
        request = DecisionRequest(kind="technical_clarification", owner=key, allowed_choices=["answer", "ask_user"],
            scope={"questions": eligible, "fingerprint": fingerprint}, evidence={"facts": facts},
            state_identity=digest([guards, facts, source]))
        receipt = "decisions/applied/" + digest(request.model_dump())
        saved = self.store.get(rid, receipt)
        if saved:
            return {int(i): answer for i, answer in saved["answers"].items() if f"{key}/{i}" not in answered}
        result_key, result = await self.propose(rid, request, work)
        answers = {a.index: facts[a.fact_ref] for a in result.answers
                   if f"{key}/{a.index}" not in answered} if result.action == "answer" else {}
        if not answers:
            return {}
        if (tree_hash(work) != source or self.technical_facts(rid) != facts
                or not self.engine.config.load().automation.enabled):
            return {}
        applicable_keys = [f"{key}/{i}" for i in answers]
        applicable_questions = [q for q in questions_state if q["key"] in applicable_keys]
        if not self.store.apply_decision(rid, receipt, request.model_dump(), result.model_dump(), "agent", guards,
                {}, applicable_questions, applicable_keys, result_key=result_key, answers=answers,
                supersede_keys=[f"{key}/{i}" for i in answers]):
            return {}
        return answers
