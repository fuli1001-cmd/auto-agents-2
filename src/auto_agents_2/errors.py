class AA2Error(Exception):
    code = "AA2_ERROR"


class RecoveryPathExhausted(AA2Error):
    """No useful permitted action remains on these unchanged recovery facts."""
    code = "RECOVERY_PATH_EXHAUSTED"

    def __init__(self, evidence, reason):
        self.evidence = evidence
        super().__init__(reason)


class IncompleteIteration(AA2Error):
    code = "ITERATION_INCOMPLETE"


class ConfigurationError(AA2Error):
    code = "CONFIGURATION_ERROR"


class ToolchainUnavailable(AA2Error):
    """A concrete execution prerequisite, not a product defect or a retry permit."""
    code = "TOOLCHAIN_UNAVAILABLE"

    def __init__(self, tool, status, action, report=None):
        self.tool, self.status, self.action, self.report = tool, status, action, report
        super().__init__(f"工具链预检未通过：{tool} ({status})。{action}")

    def record(self):
        return {"tool": self.tool, "status": self.status, "action": self.action}


class DependencyResolutionError(AA2Error):
    """The resolver rejected a workspace dependency manifest."""

    code = "DEPENDENCY_RESOLUTION_ERROR"


class RetryableInfrastructureError(AA2Error):
    """An explicitly recognized transient transport/timeout failure, not a manifest defect."""

    code = "RETRYABLE_INFRASTRUCTURE_ERROR"


class IntegrationConflict(AA2Error):
    """Git reported unmerged paths, rather than an unrelated command failure."""

    code = "INTEGRATION_CONFLICT"


class VerificationPreparationError(AA2Error):
    """The declared command cannot safely collect controller-owned evidence."""

    code = "VERIFICATION_PREPARATION_ERROR"


class PolicyViolation(AA2Error):
    code = "POLICY_VIOLATION"


class ReplacementMismatch(PolicyViolation):
    """A replace anchor failed validation before any files were written."""

    def __init__(self, path, index, matches, reason):
        self.details = {"path": path, "change_index": index, "matches": matches, "reason": reason}
        super().__init__(f"Replacement must match exactly once: {path} "
                         f"(change {index + 1}, matches={matches}, reason={reason})")


class PatchRepairBlocked(AA2Error):
    code = "PATCH_REPAIR_BLOCKED"


class PatchRepairExhausted(PatchRepairBlocked):
    """This correction attempt ended; recovery may authorize a new candidate."""


class ProtectedChange(PolicyViolation):
    """A valid candidate requiring independent protected-file authorization."""


class ProviderError(AA2Error):
    code = "PROVIDER_ERROR"

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class InvalidStructuredResponse(ProviderError):
    code = "INVALID_STRUCTURED_RESPONSE"


class PrototypeBrowserError(AA2Error):
    """Controller browser failure with the actual completed steps attached."""

    def __init__(self, message: str, results: list[dict]):
        super().__init__(message)
        self.results = results


class BusyProject(AA2Error):
    code = "PROJECT_BUSY"


class Waiting(AA2Error):
    code = "WAITING_USER"


class PostCommitError(AA2Error):
    """A durable provider result exists; only reconciliation is permitted."""

    code = "PROVIDER_RESULT_COMMITTED"


class CollectionError(AA2Error):
    code = "EVIDENCE_COLLECTION_FAILED"

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}
