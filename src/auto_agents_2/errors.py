class AA2Error(Exception):
    code = "AA2_ERROR"


class IncompleteIteration(AA2Error):
    code = "ITERATION_INCOMPLETE"


class ConfigurationError(AA2Error):
    code = "CONFIGURATION_ERROR"


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
