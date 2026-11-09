"""Per-verify network admission; never a retry, pool or response cache."""
import ipaddress
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpcore

from .errors import AA2Error, DocumentAcquisitionUnavailable, http_failure_classification
from .network_proxy import ProxySettings
from .util import digest, identifier

current_batch = ContextVar("research_batch", default=None)
current_capture = ContextVar("research_capture", default=None)
current_http_failure = ContextVar("research_http_failure", default=None)


class CaptureHistory:
    """Original failures owned by one capture, including while it is cleaning up."""
    def __init__(self, url):
        self.source_url = self.request_url = url
        self.failures = []
        self.destination_policy = None

    def record(self, failure):
        previous = next((row for row in self.failures
                         if type(row) is type(failure) and row.evidence == failure.evidence), None)
        if previous is not None:
            return previous
        self.failures.append(failure)
        return failure

    def document_failure(self, error):
        return self.record(DocumentAcquisitionUnavailable(self.request_url, error))

    def strongest(self):
        from .research_acquisition import failure_observation
        rows = [(failure, failure_observation(failure)) for failure in self.failures]
        return next((failure for failure, facts in rows if facts["classification"] in {"security", "permission"}),
                    next((failure for failure, facts in rows if not facts["automatic"]), None))

    def observations(self):
        from .research_acquisition import failure_observation
        return [failure_observation(failure) for failure in self.failures]


class ConnectionBoundary:
    """Finite pre-cleanup facts when no document/resolver mapper owns the call."""
    code = "RESEARCH_CONNECTION_BOUNDARY"

    def __init__(self, host, port, details):
        self.evidence = {"operation": "research_connect", "hostname": host, "port": port,
                         **details, "connection_phase": "public_connect"}


class ResearchRequestNotExecuted(AA2Error):
    """No request was dispatched across an observed shared boundary."""
    code = "RESEARCH_REQUEST_NOT_EXECUTED"

    def __init__(self, context, operation, failure):
        self.evidence = {"context_id": context.id, "resolver_policy_hash": context.resolver_policy_hash,
                         "operation": operation,
                         "blocked_by": {"code": failure.code, "diagnostic": dict(failure.evidence)}}
        super().__init__("Research request not executed across an unresolved shared network boundary")


class BatchProxySettings(ProxySettings):
    def __init__(self, context):
        super().__init__()
        self.context = context

    def route(self, origin):
        return super().for_origin(origin)

    @staticmethod
    def key(proxy):
        # Credentials participate only in this in-memory identity, never evidence.
        return (proxy.url, proxy.raw_auth) if proxy is not None else ("direct",)

    def for_origin(self, origin):
        proxy = self.route(origin)
        failure = (self.context.route_failures.get(self.key(proxy))
                   or self.context.origin_failures.get(self.context.origin_key(proxy, origin.host.decode(), origin.port)))
        if failure is not None:
            operation = "proxy_request" if proxy is not None else "public_connect"
            raise ResearchRequestNotExecuted(self.context, operation, failure)
        return proxy


class ResearchBatch:
    def __init__(self, resolver_config):
        self.id = identifier("research-network-context")
        self.resolver_policy_hash = digest(resolver_config.model_dump()) if resolver_config is not None else None
        self.route_failures = {}
        self.origin_failures = {}
        self.resolver_failure = None
        self.proxy_settings = BatchProxySettings(self)

    def before_capture(self, url):
        # A new capture has no reusable socket. Literal targets need no resolver.
        if self.resolver_failure is not None:
            try:
                ipaddress.ip_address(urlsplit(url).hostname)
            except ValueError:
                raise ResearchRequestNotExecuted(self, "capture_dispatch", self.resolver_failure) from None
        self.proxy_settings.for_origin(httpcore.URL(url).origin)

    def before_resolver_request(self, endpoint):
        if self.resolver_failure is not None:
            raise ResearchRequestNotExecuted(self, "resolver_request", self.resolver_failure)
        self.proxy_settings.for_origin(httpcore.URL(endpoint).origin)

    def observe_resolver(self, error, endpoint=None):
        from .research_acquisition import failure_observation
        if not failure_observation(error)["automatic"] and self.resolver_failure is None:
            self.resolver_failure = error
        if endpoint is not None:
            self.observe_connection(error, endpoint)

    def origin_key(self, proxy, host, port):
        return self.proxy_settings.key(proxy), (host or "").rstrip(".").lower(), port

    def before_connect(self, proxy, host=None, port=None):
        failure = (self.route_failures.get(self.proxy_settings.key(proxy))
                   or self.origin_failures.get(self.origin_key(proxy, host, port)))
        if failure is not None:
            operation = "proxy_request" if proxy is not None else "public_connect"
            raise ResearchRequestNotExecuted(self, operation, failure)

    def freeze_connect(self, proxy, host, port, details, failure=None):
        if not http_failure_classification(details)[1]:
            boundary = failure or ConnectionBoundary(host, port, details)
            if details.get("connection_scope") == "shared_proxy":
                self.route_failures.setdefault(self.proxy_settings.key(proxy), boundary)
            else:
                self.origin_failures.setdefault(self.origin_key(proxy, host, port), boundary)

    def observe_connection(self, error, url):
        proxy = self.proxy_settings.route(httpcore.URL(url).origin)
        # A phase is not proof of shared infrastructure. Freeze only this
        # origin unless the actual proxy TCP/TLS/407 observation proves sharing.
        _, automatic = http_failure_classification(error.evidence)
        phase = error.evidence.get("connection_phase")
        if not automatic and (phase in {"public_connect", "origin_tls"}
                or error.evidence.get("error_type") == "ConnectError"
                or error.evidence.get("category") == "proxy"):
            target = httpcore.URL(url).origin
            self.freeze_connect(proxy, target.host.decode(), target.port, error.evidence, error)
