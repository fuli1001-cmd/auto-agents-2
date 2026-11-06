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


class EnvironmentCommandError(AA2Error):
    """An observed external environment command failure, never a repair permit."""
    code = "ENVIRONMENT_COMMAND_ERROR"

    def __init__(self, message, *, evidence=None):
        self.evidence = evidence or {}
        super().__init__(message)


class DependencyResolutionError(EnvironmentCommandError):
    """Resolution of declared workspace dependencies failed; diagnosis owns the cause."""
    code = "DEPENDENCY_RESOLUTION_ERROR"


class DependencySolverError(DependencyResolutionError):
    """A recognized external resolver crash, not proof of invalid dependencies."""
    code = "DEPENDENCY_SOLVER_ERROR"


class DocumentAcquisitionTimeout(AA2Error, TimeoutError):
    """An expired controller-owned HTTP document deadline, not an arbitrary timeout."""

    code = "DOCUMENT_ACQUISITION_TIMEOUT"

    def __init__(self, label, timeout, *, source_url):
        from .util import digest
        self.evidence = {"operation": label, "timeout_seconds": timeout,
                         "source_url_hash": digest(source_url)}
        super().__init__(f"{label} timed out after {timeout:g}s; check network connectivity and "
                         "HTTP_PROXY/HTTPS_PROXY in the aa2 process environment")


def http_failure_details(error):
    """Finite HTTPX types and numeric causes only; never retain arbitrary error text."""
    import ssl

    import httpx

    category = next(label for kind, label in (
        (httpx.HTTPStatusError, "http_status"), (httpx.ProxyError, "proxy"),
        (httpx.TimeoutException, "timeout"), (httpx.NetworkError, "network"),
        (httpx.ProtocolError, "protocol"), (httpx.HTTPError, "http")) if isinstance(error, kind))
    names = ("HTTPStatusError", "ProxyError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
             "PoolTimeout", "ConnectError", "ReadError", "WriteError", "CloseError",
             "LocalProtocolError", "RemoteProtocolError", "UnsupportedProtocol", "DecodingError",
             "TooManyRedirects", "TimeoutException", "NetworkError", "ProtocolError", "TransportError",
             "RequestError", "HTTPError")
    details = {"category": category, "error_type": next(name for name in names if isinstance(error, getattr(httpx, name)))}
    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    if type(status) is int and 100 <= status <= 599:
        details["status_code"] = status
    cause, seen = error, set()
    while isinstance(cause, BaseException) and id(cause) not in seen and len(seen) < 8:
        seen.add(id(cause))
        if isinstance(cause, OSError) and type(cause.errno) is int and -(2**31) <= cause.errno < 2**31:
            details.setdefault("os_errno", cause.errno)
        if isinstance(cause, ssl.SSLCertVerificationError):
            code = getattr(cause, "verify_code", None)
            if type(code) is int and 0 <= code < 2**31:
                details.setdefault("ssl_verify_code", code)
        cause = cause.__cause__  # Explicit causes only, not unrelated implicit context.
    return details


class DocumentAcquisitionUnavailable(AA2Error):
    """An observed document HTTP failure, distinct from resolver/policy/collection errors."""
    code = "DOCUMENT_ACQUISITION_UNAVAILABLE"

    def __init__(self, source_url, error):
        import httpx

        from .util import digest
        details = http_failure_details(error)
        status = details.pop("status_code", None)
        self.evidence = {"operation": "research_document", "source_origin": str(httpx.URL(source_url).copy_with(
            username="", password="", path="/", query=None, fragment=None)), "source_url_hash": digest(source_url), **details}
        if status is not None:
            self.evidence["http_status"] = status
        suffix = f", HTTP {status}" if status is not None else ""
        super().__init__(f"Research document acquisition failed ({details['category']}, {details['error_type']}{suffix}); "
                         "check the source service and aa2 process network/proxy route")


class ResearchResolverUnavailable(AA2Error):
    """An observed resolver HTTP failure, not a retry or network-policy permit."""
    code = "RESEARCH_RESOLVER_UNAVAILABLE"

    def __init__(self, *, category, error_type, endpoint_origin, endpoint_url_hash, status_code=None, os_errno=None, ssl_verify_code=None):
        self.evidence = {"operation": "research_resolver", "category": category,
                         "error_type": error_type, "endpoint_origin": endpoint_origin,
                         "endpoint_url_hash": endpoint_url_hash}
        if status_code is not None:
            self.evidence["http_status"] = status_code
        for key, value in (("os_errno", os_errno), ("ssl_verify_code", ssl_verify_code)):
            if value is not None:
                self.evidence[key] = value
        status = f", HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"Research HTTPS DNS JSON request failed ({category}, {error_type}{status}); "
                         "check the configured resolver and aa2 process proxy route")


class ResearchDestinationRejected(AA2Error):
    """The public research TCP boundary observed empty or non-public DNS results."""

    code = "RESEARCH_DESTINATION_REJECTED"

    def __init__(self, hostname, *, empty_resolution=False):
        self.evidence = {"operation": "research_connect", "hostname": hostname,
                         "resolution": "empty" if empty_resolution else "non_public"}
        observation = "DNS returned no addresses" if empty_resolution else "DNS included a non-public address"
        super().__init__(f"Research destination {hostname} must resolve exclusively to public addresses; "
                         f"{observation}. Check DNS, possible Fake-IP mapping, or whether the destination "
                         "itself is non-public.")


class RetryableInfrastructureError(AA2Error):
    """An explicitly recognized transient transport/timeout failure, not a manifest defect."""

    code = "RETRYABLE_INFRASTRUCTURE_ERROR"

    def __init__(self, message, *, evidence=None):
        self.evidence = evidence or {}
        super().__init__(message)


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


class InvalidEvidenceReference(InvalidStructuredResponse):
    """Safe, controller-generated reference diagnostics, never arbitrary model text."""

    def __init__(self, issues, *, omitted=0):
        import json
        self.issues = issues
        super().__init__("Ambiguous material hash; cite an explicit supplied source_ref or material_id "
                         "after checking its source; candidates do not establish factual authority. "
                         + json.dumps({"issues": issues, "omitted": omitted}, ensure_ascii=False))


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
