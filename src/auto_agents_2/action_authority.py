"""Controller facts determine authority; stage transitions do not approve their outputs."""
from __future__ import annotations

from .faults import HumanRequirement, RecoveryRoute, route_failure
from .stage_responsibilities import GATES, STAGE_REENTRY_ACTIONS


def transition_effects(action, plan):
    if action not in STAGE_REENTRY_ACTIONS:
        return {}
    return {
        "restart_stage": action,
        "invalidate_stages": list(GATES[GATES.index(action):]),
        "affected_tasks": [task.id for task in plan.tasks] if plan and action != "readme" else [],
        "preserve": ["accepted_requirements", "acceptance_obligations", "task_origins", "consumption",
                     "user_changes", "candidates", "approval_history"],
        "authority": "Re-enter the owning stage under its existing permissions and gates; no output is approved.",
        "human_gates": "Product choices and prototype approval still require the original user interaction.",
    }


def route_action(failure, action, *, enabled, rule, granted=False, answered=False,
                 controller_owned=False, force_decision=False, published=False, prerequisite_resolved=False):
    facts = route_failure(failure)
    if facts.human_requirement and not prerequisite_resolved:
        return facts
    if published and action in STAGE_REENTRY_ACTIONS | {"plan_patch"}:
        return RecoveryRoute(route="human", reason_code="published_gate_change", phase=failure.phase,
            human_requirement=HumanRequirement(kind="state_conflict",
                reason="已开始发布，不能通过阶段回退使发布记录失效。",
                action="核对已保留的发布记录和目标状态；在交付恢复允许的范围内处理故障。",
                resume_condition="发布状态与恢复日志一致；确认本身不授权回退已发布门禁。",
                evidence_refs=failure.evidence_refs))
    # A recorded answer may be a pause. Consume it before describing it as a
    # grant; an already issued grant has a separately verified scope.
    if answered:
        route, reason = "human", "recorded_answer"
    elif granted:
        route, reason = "human", "recorded_authorization"
    elif rule and not force_decision and (controller_owned or action not in STAGE_REENTRY_ACTIONS):
        route, reason = "rule", "established_recovery"
    elif enabled:
        route, reason = "decision", "stage_transition" if action in STAGE_REENTRY_ACTIONS else "recovery_assessment"
    else:
        route, reason = "human", "automation_disabled"
    return RecoveryRoute(route=route, reason_code=reason, phase=failure.phase)
