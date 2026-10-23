"""Controller-owned fault routing; no provider transport or recovery execution."""
from __future__ import annotations

from typing import Literal

from pydantic import model_serializer

from .errors import (
    AA2Error,
    ConfigurationError,
    DependencyResolutionError,
    EnvironmentCommandError,
    PolicyViolation,
    ProviderError,
    RetryableInfrastructureError,
    ToolchainUnavailable,
)
from .models import Record


class HumanRequirement(Record):
    kind: Literal["authorization", "external_input", "resource_limit", "state_conflict", "decision_unavailable"]
    reason: str
    action: str
    resume_condition: str
    evidence_refs: list[str] = []
    evidence: dict = {}

    @model_serializer(mode='wrap')
    def serialize_evidence(self, handler):
        # Preserve the exact identity of existing prerequisite questions. Only
        # new observations add this field; old empty records remain unchanged.
        value = handler(self)
        if not self.evidence:
            value.pop('evidence', None)
        return value

    def prompt(self):
        return f"{self.reason}\n需要处理：{self.action}\n继续条件：{self.resume_condition}"


class RecoveryRoute(Record):
    route: Literal["rule", "decision", "human", "internal_error"]
    reason_code: str
    phase: str
    human_requirement: HumanRequirement | None = None


class HumanIntervention(AA2Error):
    """A known boundary with a concrete prerequisite, never an execution permit."""
    code = "RECOVERY_PREREQUISITE"

    def __init__(self, requirement: HumanRequirement, *, reason_code="state_conflict", phase="recovery"):
        self.requirement, self.reason_code, self.phase = requirement, reason_code, phase
        super().__init__(requirement.prompt())


class StateConflict(HumanIntervention):
    def __init__(self, reason, *, phase="recovery", evidence_refs=()):
        super().__init__(HumanRequirement(kind="state_conflict", reason=reason,
            action="核对并保留用户修改、候选及恢复记录；恢复记录所绑定的输入，或修复相应恢复记录。",
            resume_condition="控制器重新核对源码、范围和操作回执一致；确认本身不会跳过核对。",
            evidence_refs=list(evidence_refs)), reason_code="state_conflict", phase=phase)


class ResourceBoundary(HumanIntervention):
    def __init__(self, reason, *, action, condition, phase="recovery", evidence_refs=()):
        super().__init__(HumanRequirement(kind="resource_limit", reason=reason, action=action,
            resume_condition=condition, evidence_refs=list(evidence_refs)), reason_code="resource_limit", phase=phase)


class ApprovalBoundary(HumanIntervention):
    def __init__(self, reason, *, phase="prototype"):
        super().__init__(HumanRequirement(kind="authorization", reason=reason,
            action="恢复或完成与当前内容及动作匹配的原始审批；查看 aa2 status 中的待处理审批记录。",
            resume_condition="控制器验证真实审批来源、内容和允许动作一致；重试不替代原始批准。"),
            reason_code="approval_required", phase=phase)


def known_exception(error, phase):
    """Typed external boundaries only. Unknown AA2Error is an internal defect."""
    if isinstance(error, HumanIntervention):
        return error
    if isinstance(error, ToolchainUnavailable):
        return HumanIntervention(HumanRequirement(kind="external_input", reason=str(error),
            action=error.action, resume_condition="在相同执行环境重新预检通过；回答本身不会跳过工具链核对。"),
            reason_code=error.code, phase=phase)
    if isinstance(error, DependencyResolutionError):
        # Tasks consume this as RecoveryNeeded. Other callers have no implicit
        # authority to edit manifests or retry; keep a concrete external boundary.
        from .installation_failure import failure_references
        return HumanIntervention(HumanRequirement(kind="external_input", reason=str(error),
            action="核对已声明依赖、锁文件和求解器；通过原职责阶段修订，保留失败材料。",
            resume_condition="原范围内修订或工具恢复完成后重新核验；确认不跳过安装和验收。",
            evidence_refs=failure_references(error), evidence=error.evidence),
            reason_code=error.code, phase=phase)
    if isinstance(error, EnvironmentCommandError) or isinstance(error, RetryableInfrastructureError) and error.evidence:
        from .installation_failure import failure_references
        return HumanIntervention(HumanRequirement(kind="external_input", reason=str(error),
            action="读取本次命令的原始证据，核对外部工具、访问或安装前提；原因未定不授权改依赖、权限或安全设置。",
            resume_condition="已确定并处理实际前提后，按原范围重新核对；不跳过安装、证据或验收。",
            evidence_refs=failure_references(error), evidence=error.evidence),
            reason_code=error.code, phase=phase)
    if isinstance(error, (ConfigurationError, ProviderError)):
        return HumanIntervention(HumanRequirement(kind="external_input",
            reason=f"外部配置或 provider 尚不可用（{error.code}）。",
            action="检查项目配置和原生 provider 的认证、连接状态。",
            resume_condition="配置验证和原生 provider 检查通过后重新执行当前已声明操作。"),
            reason_code=error.code, phase=phase)
    if isinstance(error, PolicyViolation):
        return StateConflict("当前操作未通过路径、权限或候选完整性核对；保留未应用提案及现场。", phase=phase)
    return None


class InternalWorkflowError(AA2Error):
    code = "INTERNAL_ERROR"

    def __init__(self, incident, exception_type, *, saved=True):
        self.incident = incident
        state = "已保留现场" if saved else "故障记录写入失败；已有现场保留"
        super().__init__(f"aa2 内部错误 {exception_type}（故障编号 {incident}）；{state}。"
                         "修复后使用相同项目的 aa2 run 续跑。")


def route_failure(failure, *, repeated=False, diagnosis_inconclusive=False, human_requirement=None):
    """The same classification is consumed by every stage; publication constrains actions separately."""
    if human_requirement:
        return RecoveryRoute(route="human", reason_code=human_requirement.kind, phase=failure.phase,
                             human_requirement=human_requirement)
    from .recovery import check_rows
    checks = check_rows(failure.evidence)
    if any("evidence_collection_failed" in row.get("failure_reasons", []) for row in checks):
        return RecoveryRoute(route="human", reason_code="resource_limit", phase=failure.phase,
            human_requirement=HumanRequirement(kind="resource_limit", reason="测试附件未能完整归档。",
                action="调整 attachment_capture 上限或测试录制配置；候选、JUnit 和原始附件保持保留。",
                resume_condition="采集配置或录制输入已变化，并重新通过附件采集核对。",
                evidence_refs=failure.evidence_refs))
    return RecoveryRoute(route="decision" if repeated or diagnosis_inconclusive else "rule",
                         reason_code=failure.reason_code, phase=failure.phase)
