"""Resume schema-invalid proposals through the shared action policy, without recursive decisions."""
from __future__ import annotations

from .errors import InvalidStructuredResponse
from .faults import StateConflict
from .recovery import FailureRecord, RecoveryDecision
from .util import digest
from .workspace import tree_hash


class ProposalRecovery:
    def __init__(self, engine):
        self.engine = engine

    async def invoke(self, stage, context, schema, work, rid, operation, fresh):
        engine, store = self.engine, self.engine.store
        source = tree_hash(work)
        owner = "proposal/" + digest([stage, operation])
        counter = "proposal-recovery/count/" + owner
        generation = store.run(rid)["data"].get("replans", 0)
        identity = digest([stage, operation, context, schema.model_json_schema(), str(work), source, generation])
        key = "proposal-recovery/requests/" + identity
        state = store.get(rid, key, {})
        while True:
            if tree_hash(work) != source:
                raise StateConflict("Proposal recovery inputs changed; preserve the original request and user edits")
            if state.get("phase") == "completed":
                return schema.model_validate(engine.artifacts.json(state["result"]))
            if state.get("phase") == "rejected":
                if state.get("permit"):
                    engine.recovery_policy.complete(rid, state["permit"], {"status": "invalid_structured_response"})
                    engine.recovery_policy.finish_cycle(rid, state["permit_active"], {"status": "invalid_structured_response"})
                failure = FailureRecord.model_validate(state["active"]["failure"])
                suggested = RecoveryDecision(failure_id=failure.id, action="recheck", budget_owner=owner,
                    evidence_refs=["failure.evidence.response_contract"],
                    reason="当前阶段响应未通过结构校验；评估是否按同一任务与 schema 重新生成提案，不执行源码修改或验收。")
                _, permit = await engine.recovery_policy.authorize(rid, failure, state["active"], work, None, suggested,
                    operation="proposal-response", candidates={"recheck": suggested}, force_decision=True,
                    supersede_keys=state.get("legacy_question_keys", []))
                state.update(phase="authorized", permit=permit, permit_active=state["active"],
                             attempt=state["active"]["attempt"])
                store.checkpoint(rid, key, state)
            attempt = state.get("attempt", 0)
            request = context if not attempt else {**context, "validation_feedback": {
                "code": "invalid_structured_response", "schema": schema.__name__, "attempt": attempt,
                "instruction": "Return one complete result satisfying the supplied schema. Preserve the original task, "
                               "constraints and evidence; this retry does not approve any gate.",
                "previous_feedback": context.get("validation_feedback"),
            }}
            call_key = operation if not attempt else f"{operation}/response-retry/{attempt}"
            engine.recovery_policy.start(rid, state.get("permit"))
            try:
                result = await engine._invoke_once(stage, request, schema, work, rid, call_key, fresh,
                                                  logical_operation=operation, logical_context=context)
            except InvalidStructuredResponse:
                if tree_hash(work) != source:
                    raise StateConflict("Proposal source changed after an invalid response; preserve user edits") from None
                count = store.get(rid, counter, 0) + 1
                failure = FailureRecord.create(kind="proposal", component="provider_response", origin_id=owner,
                    step="structured_response", phase=stage, obligations=[digest(schema.model_json_schema())],
                    evidence_refs=[key], evidence={"response_contract": {
                        "stage": stage, "operation": operation, "schema": schema.__name__,
                        "source_hash": source, "failed_responses": count,
                        "action_effect": "Repeat this read-only model invocation; no checks or file writes are authorized."}})
                pending = [q["key"] for q in store.decision_questions(rid, ["provider/" + call_key])
                           if q["answer"] is None]
                state.update(phase="rejected", active={"attempt": count, "failure": failure.model_dump()},
                             legacy_question_keys=pending)
                store.checkpoints(rid, {key: state, counter: count})
                continue
            if state:
                outcome = {"status": "response_validated", "response_hash": digest(result.model_dump())}
                engine.recovery_policy.complete(rid, state["permit"], outcome)
                engine.recovery_policy.finish_cycle(rid, state["permit_active"], outcome)
                state.update(phase="completed", result=engine.artifacts.put(result.model_dump(), "recovered_proposal"))
                store.checkpoint(rid, key, state)
            return result
