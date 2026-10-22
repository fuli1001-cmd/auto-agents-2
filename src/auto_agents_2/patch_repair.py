"""Bounded, durable proposal correction; validation and application stay separate."""
from __future__ import annotations

from pydantic import Field

from .errors import (
    PatchRepairBlocked,
    PatchRepairExhausted,
    PolicyViolation,
    ProviderError,
    ReplacementMismatch,
)
from .models import Change, Record
from .stage_responsibilities import validate_stage_writes
from .util import digest
from .workspace import prepare_changes, tree_hash

MAX_PATCH_CORRECTIONS = 2


class ReplacementCorrection(Record):
    old_text: str = Field(min_length=1, description="Exact unique anchor after preceding replacements")
    content: str = Field(description="Replacement preserving the intended edit and any added anchor context")


class PatchRepairService:
    def __init__(self, engine):
        self.engine = engine

    async def prepare(self, rid, work, changes, scopes, *, stage, context, operation, contracts=False, artifact_owner=None):
        """Return corrected proposals and a validated patch, without writing source.

        Only the failed replace's two text fields may change. All other operations,
        paths, document metadata, obligations and subsequent gates remain owned by
        the caller. Prepared/applied patches must bypass this service on recovery.
        """
        engine = self.engine
        validate_stage_writes(artifact_owner or stage, changes, scopes)
        original = [c.model_dump() for c in changes]
        key = "patch-repair/" + digest([operation, original, scopes, contracts,
                                        engine.store.get(rid, "replan_feedback")])
        state = engine.store.get(rid, key, {})
        source_hash = tree_hash(work)
        if state and state["source_hash"] != source_hash:
            raise PatchRepairBlocked("补丁纠错期间工作区已改变，已保留候选和用户修改；请核对后重新生成候选")
        current = [Change.model_validate(c) for c in state.get("candidate", original)]
        while True:
            try:
                validate_stage_writes(artifact_owner or stage, current, scopes)
                prepared = prepare_changes(work, current, scopes, protected=False, contracts=contracts)
            except ReplacementMismatch as exc:
                failure = exc.details
            else:
                if state:
                    state.update(phase="validated", candidate=[c.model_dump() for c in current])
                    engine.store.checkpoint(rid, key, state)
                return current, prepared

            project = engine.contexts.build(work, scopes=[failure["path"]])
            if failure["path"] not in project["inventory"]:
                raise PatchRepairBlocked(f"无法自动纠正替换：{failure['path']} 不存在或不在允许读取的公开文件中")
            if not state:
                state = {"source_hash": source_hash, "candidate": original, "attempts": [],
                         "original": engine.artifacts.put(original, "patch-repair-original"), "phase": "repairing"}
                engine.store.checkpoint(rid, key, state)
            if state.get("active") is None:
                if len(state["attempts"]) >= MAX_PATCH_CORRECTIONS:
                    state.update(phase="blocked", failure=failure)
                    engine.store.checkpoint(rid, key, state)
                    raise PatchRepairExhausted(
                        f"补丁自动纠错已达 {MAX_PATCH_CORRECTIONS} 次上限：{failure['path']} "
                        f"第 {failure['change_index'] + 1} 项替换仍匹配 {failure['matches']} 次。"
                        "原候选、纠错响应和当前文件均已保留；重复重试不会重置额度。")
                request = {**context, "project": project, "patch_repair": {
                    "failure": failure, "candidate_changes": [c.model_dump() for c in current],
                    "allowed_paths": scopes,
                    "instruction": "Read the current public file and simulate preceding replacements in list order. "
                    "Correct ONLY the failed replacement, preserving its intended edit and all surrounding behaviour. "
                    "Return old_text and content, not a new document or implementation. If the anchor needs more "
                    "context to become unique, preserve that context in content. Do not change any other operation, "
                    "path, acceptance condition or test obligation. No source has been written by this batch.",
                }}
                attempt = {"failure": failure, "candidate": engine.artifacts.put(
                    [c.model_dump() for c in current], "patch-repair-candidate"),
                    "request": engine.artifacts.put(request, "patch-repair-request")}
                state["active"] = len(state["attempts"])
                state["attempts"].append(attempt)
                engine.store.checkpoint(rid, key, state)
            number = state["active"]
            attempt = state["attempts"][number]
            # The exact request survives a crash after invocation result persistence.
            request = engine.artifacts.json(attempt["request"])
            if tree_hash(work) != source_hash:
                raise PatchRepairBlocked("补丁纠错期间工作区已改变，已保留用户修改")
            try:
                correction = await engine._invoke(stage, request, ReplacementCorrection, work, rid,
                                                 f"{key}/{number + 1}", fresh=True, interactive_retry=False)
            except ProviderError as exc:
                attempt["error_type"] = type(exc).__name__
                state.update(active=None)
                engine.store.checkpoint(rid, key, state)
                raise PatchRepairBlocked("补丁纠错调用未完成，已保留候选并计入纠错额度；请检查 provider 状态") from exc
            except PolicyViolation as exc:
                raise PatchRepairBlocked(str(exc)) from exc
            if tree_hash(work) != source_hash:
                raise PatchRepairBlocked("补丁纠错期间工作区已改变，纠错响应未应用")
            index = attempt["failure"]["change_index"]
            current[index] = current[index].model_copy(update=correction.model_dump())
            attempt["response"] = engine.artifacts.put(correction.model_dump(), "patch-repair-response")
            state.update(candidate=[c.model_dump() for c in current], active=None)
            engine.store.checkpoint(rid, key, state)
