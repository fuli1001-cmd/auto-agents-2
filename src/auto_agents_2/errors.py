class AA2Error(Exception):
    code = "AA2_ERROR"


class IncompleteIteration(AA2Error):
    code = "ITERATION_INCOMPLETE"


class ConfigurationError(AA2Error):
    code = "CONFIGURATION_ERROR"


class PolicyViolation(AA2Error):
    code = "POLICY_VIOLATION"


class ProviderError(AA2Error):
    code = "PROVIDER_ERROR"

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class PrototypeBrowserError(AA2Error):
    """Controller browser failure with the actual completed steps attached."""

    def __init__(self, message: str, results: list[dict]):
        super().__init__(message)
        self.results = results


class BusyProject(AA2Error):
    code = "PROJECT_BUSY"


class Waiting(AA2Error):
    code = "WAITING_USER"
