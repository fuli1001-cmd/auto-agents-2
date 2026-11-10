from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

import httpx

from .artifacts import ArtifactStore
from .errors import (
    AA2Error,
    DocumentAcquisitionTimeout,
    DocumentAcquisitionUnavailable,
    ResearchDestinationRejected,
    ResearchResolverUnavailable,
    ResearchURLRejected,
    http_failure_details,
)
from .http_body import ACQUISITION_TIMEOUT, acquisition_deadline, read_body
from .managed_process import joined
from .models import SourceEvidence
from .network import PublicHTTPTransport, public_address
from .research_batch import (
    CaptureHistory,
    ResearchBatch,
    ResearchRequestNotExecuted,
    current_batch,
    current_capture,
    current_http_failure,
)
from .util import SECRET, digest, identifier


def public_url(value: str):
    url = urlsplit(value)
    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
        raise ResearchURLRejected(url.hostname or "", "invalid_url",
                                  "Research evidence must use public HTTP(S) documentation URLs")
    if any(SECRET.search(k) for k in parse_qs(url.query)):
        raise ResearchURLRejected(url.hostname, "credentials",
                                  "Research URLs must not contain credentials")
    if url.hostname.rstrip(".").lower() == "localhost":
        raise ResearchURLRejected(url.hostname, "non_public", "Research destination must be public")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        return  # DNS addresses are checked again at connection time.
    if not public_address(str(address)):
        raise ResearchURLRejected(url.hostname, "non_public", "Research destination must be public")


class ResearchService:
    """Preserve the response and provenance; acquisition is not proof of a claim."""

    def __init__(self, artifacts: ArtifactStore, *, resolver_config=None, excluded_hosts=(),
                 document_authorizations=None):
        self.document_authorizations = document_authorizations
        self.artifacts = artifacts
        self.ledger = None
        self.excluded_hosts = tuple(excluded_hosts)
        self.resolver_config = resolver_config.model_copy(deep=True) if resolver_config is not None else None

    def admit(self, url, *, network=True):
        policy = None
        if self.document_authorizations is None:
            public_url(url)
        else:
            from .research_destinations import ResearchDestinationPolicy
            policy = ResearchDestinationPolicy(url, self.document_authorizations)
        from .research_replacement import admit_url
        if self.excluded_hosts:
            admit_url(url, self.excluded_hosts)  # Original explicit replacement contract remains named-only.
        if network and self.ledger is not None:
            denied = self.ledger.excluded_hosts()
            if denied:
                if policy is None:
                    admit_url(url, denied)
                else:
                    policy.admit_recorded_denials(denied, self.ledger.excluded_addresses())

    async def capture(self, url: str) -> dict:
        history = current_capture.get()
        if history is None or history.source_url != url:
            history = CaptureHistory(url)
        capture_token = current_capture.set(history)
        failure_token = current_http_failure.set(history.document_failure)
        try:
            return await self._capture(url, history)
        except BaseException as error:
            if history.destination_policy is not None:
                import copy
                error.document_destination_bindings = copy.deepcopy(history.destination_policy.connections)
            if history.failures:
                error.observed_failures = history.observations()
            raise
        finally:
            current_http_failure.reset(failure_token)
            current_capture.reset(capture_token)

    async def _capture(self, url, history):
        self.admit(url)
        label = f"Research document download for {urlsplit(url).hostname}"
        resolver = None
        from .research_destinations import ResearchDestinationPolicy, source_cookie_jar
        policy = (ResearchDestinationPolicy(url, self.document_authorizations)
                  if self.document_authorizations is not None else None)
        history.destination_policy = policy
        transport_options = {"destination_policy": policy} if policy is not None else {}
        client_options = {"cookies": source_cookie_jar()} if policy is not None else {}
        batch = current_batch.get()
        if batch is not None and batch.resolver_policy_hash != (
                digest(self.resolver_config.model_dump()) if self.resolver_config is not None else None):
            raise AA2Error("Research resolver policy changed within its frozen batch")
        if self.resolver_config is None:
            transport = PublicHTTPTransport(**transport_options)
        else:
            from .network_proxy import ProxySettings
            from .research_dns import HTTPSJSONResolver
            proxy_settings = batch.proxy_settings if batch is not None else ProxySettings()
            resolver = HTTPSJSONResolver(self.resolver_config, proxy_settings)
            transport = PublicHTTPTransport(resolver=resolver, proxy_settings=proxy_settings, **transport_options)
        final = attempted_url = url
        observed_error = None
        prior_requests = []
        try:
            async with acquisition_deadline(ACQUISITION_TIMEOUT, label, source_url=url), httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=30,
                    headers={"Accept-Encoding": "gzip, deflate"}, **client_options) as client:
                try:
                    for _ in range(6):
                        self.admit(final)
                        if policy is not None:
                            policy.before_request(final)
                        attempted_url = history.request_url = final
                        async with client.stream("GET", final) as response:
                            prior_requests.append({"url_hash": digest(attempted_url),
                                                   "http_status": response.status_code})
                            try:
                                if response.is_redirect and "location" in response.headers:
                                    final = urljoin(final, response.headers["location"])
                                    continue
                                response.raise_for_status()
                                data = await read_body(response)
                                mime = response.headers.get("content-type", "")
                                break
                            except Exception as error:
                                observed_error = error
                                if isinstance(error, httpx.HTTPError):
                                    failure = history.document_failure(error)
                                    if batch is not None:
                                        batch.observe_connection(failure, attempted_url)
                                raise
                    else:
                        raise AA2Error("Research redirect limit exceeded")
                except Exception as error:
                    observed_error = observed_error or error
                    if isinstance(error, httpx.HTTPError):
                        failure = history.document_failure(error)
                        if batch is not None:
                            batch.observe_connection(failure, attempted_url)
                    raise
        except Exception as error:
            if isinstance(error, ResearchRequestNotExecuted):
                error.evidence["blocked_request_url_hash"] = digest(attempted_url)
                error.evidence["prior_response_headers"] = prior_requests
            from .research_acquisition import failure_observation
            observed = (history.document_failure(observed_error)
                        if isinstance(observed_error, httpx.HTTPError) else observed_error)
            # Only this capture's controller-owned deadline may restore its
            # earlier HTTP refusal. External cancellation never enters here.
            if isinstance(error, DocumentAcquisitionTimeout) and (
                    observed is None or failure_observation(observed)["automatic"]):
                observed = history.strongest() or observed
            if observed is not None and not failure_observation(observed)["automatic"]:
                # HTTP status/security/unknown processing failures were observed
                # before __aexit__. A cleanup timeout must not turn them into a
                # transient retry. Cancellation remains a BaseException above.
                if isinstance(observed, (DocumentAcquisitionUnavailable, ResearchResolverUnavailable)) and error is not observed_error:
                    observed.evidence["cleanup_failure"] = (
                        {"code": error.code, "diagnostic": error.evidence}
                        if isinstance(error, DocumentAcquisitionTimeout) else
                        http_failure_details(error) if isinstance(error, httpx.HTTPError) else {"category": "unknown"})
                raise observed from None
            if isinstance(error, httpx.HTTPError):
                raise DocumentAcquisitionUnavailable(attempted_url, error) from None
            raise
        import base64

        from .collection import run_collection
        result = await run_collection(self.artifacts.store.root, {"action": "research", "source": url,
            "final_url": final, "mime": mime, "raw": base64.b64encode(data).decode()}, timeout=ACQUISITION_TIMEOUT)
        if resolver is not None or policy is not None:
            evidence = resolver.evidence() if resolver is not None else {}
            if policy is not None:
                evidence["destination_policy"] = policy.evidence()
            return {**result, "network_policy": evidence}
        return result

    async def verify(self, sources: list[SourceEvidence]) -> list[dict]:
        batch = ResearchBatch(self.resolver_config)
        token = current_batch.set(batch)
        try:
            return await self._verify(sources, batch)
        finally:
            current_batch.reset(token)

    async def _verify(self, sources, batch):
        from .research_acquisition import failure_observation
        from .research_documents import RetainedDocumentFailure

        semaphore = asyncio.Semaphore(4)
        documents = {}
        started = set()
        histories = {}
        completed_outcomes = {}
        global_failure = None

        def independent_failure(error):
            return isinstance(error, (DocumentAcquisitionTimeout, DocumentAcquisitionUnavailable,
                                      ResearchDestinationRejected, ResearchResolverUnavailable, ResearchRequestNotExecuted,
                                      RetainedDocumentFailure))

        async def fetch(url):
            nonlocal global_failure
            async with semaphore:
                if global_failure is not None:
                    raise asyncio.CancelledError()
                history = histories[url] = CaptureHistory(url)
                capture_token = current_capture.set(history)
                failure_token = current_http_failure.set(history.document_failure)
                try:
                    self.admit(url, network=False)
                    if self.ledger is not None:
                        previous = self.ledger.previous(url, document_authorizations=self.document_authorizations)
                        if previous is not None:
                            return previous
                    while True:
                        batch.before_capture(url)
                        pending = self.ledger.start(url) if self.ledger is not None else None
                        started.add(url)
                        attempt_history = CaptureHistory(url)
                        attempt_capture = current_capture.set(attempt_history)
                        attempt_failure = current_http_failure.set(attempt_history.document_failure)
                        try:
                            try:
                                self.admit(url)
                                evidence = await self.capture(url)
                            finally:
                                current_http_failure.reset(attempt_failure)
                                current_capture.reset(attempt_capture)
                                for failure in attempt_history.failures:
                                    history.record(failure)
                        except BaseException as error:
                            if pending is not None:
                                try:
                                    error.document_outcome = self.ledger.finish(url, pending, error=error)
                                    completed_outcomes[url] = error.document_outcome
                                except Exception as recording_error:
                                    error.acquisition_document_recording_error = recording_error
                                    if isinstance(getattr(error, "evidence", None), dict):
                                        error.evidence["recording_failure"] = {
                                            "error_type": type(recording_error).__name__, "operation": "research.document.finish"}
                                    global_failure = error
                                    raise error
                                if isinstance(error, Exception) and self.ledger.retry(url, error):
                                    await asyncio.sleep(.25)
                                    continue
                            raise
                        if pending is not None:
                            evidence = self.ledger.finish(url, pending, evidence=evidence)
                        return evidence
                except BaseException as error:
                    if isinstance(error, ResearchResolverUnavailable):
                        history.record(error)
                        batch.observe_resolver(error)
                    elif isinstance(error, DocumentAcquisitionUnavailable):
                        history.record(error)
                        batch.observe_connection(error, error.evidence["source_origin"])
                    if not independent_failure(error) and global_failure is None:
                        global_failure = error
                    raise
                finally:
                    current_http_failure.reset(failure_token)
                    current_capture.reset(capture_token)

        for source in sources:
            url = urldefrag(source.url)[0]
            if url not in documents:
                documents[url] = asyncio.create_task(fetch(url))
        try:
            pending = set(documents.values())
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                # A shared network boundary stops new dispatch, not requests
                # already in flight. Only true global/control failures cancel.
                if global_failure is not None:
                    raise global_failure
                for task in done:
                    if task.cancelled():
                        raise asyncio.CancelledError()
            failed = [(task.exception(), failure_observation(task.exception()))
                      for task in documents.values() if task.exception() is not None
                      and not isinstance(task.exception(), ResearchRequestNotExecuted)]
            if failed:
                selected = next((error for error, facts in failed
                                 if facts["classification"] in {"security", "permission"}),
                                next((error for error, facts in failed if not facts["automatic"]), failed[0][0]))
                raise selected
            blocked = [task.exception() for task in documents.values()
                       if isinstance(task.exception(), ResearchRequestNotExecuted)]
            if blocked:
                raise blocked[0]
        except BaseException as original:
            outcome = original
            async def cancel_documents():
                for task in documents.values():
                    task.cancel()
                await asyncio.gather(*documents.values(), return_exceptions=True)
            try:
                await joined(asyncio.create_task(cancel_documents()))
            except BaseException as delayed:
                # joined has drained every child but must still propagate a
                # later caller cancellation. Publish owned facts before doing so.
                outcome = delayed
            # Control errors retain their type. The controller records these
            # facts with automatic recovery explicitly disabled.
            rows = []
            for url, task in documents.items():
                if task.cancelled():
                    row = {"url": url, "status": "cancelled", "capture_started": url in started}
                elif getattr(task.exception(), "acquisition_request_not_executed", False):
                    row = {"url": url, "status": "not_executed", "capture_started": False,
                           "unit": "capture", "boundary": {"operation": "research_prerequisite_admission",
                                                             "error_type": type(task.exception()).__name__}}
                elif isinstance(task.exception(), ResearchRequestNotExecuted):
                    row = {"url": url, "status": "not_executed", "capture_started": url in started,
                           "unit": "pending_request" if url in started else "capture",
                           "boundary": task.exception().evidence}
                elif task.exception() is not None:
                    row = {"url": url, "status": "failed", "failure": failure_observation(task.exception())}
                    owned = getattr(task.exception(), "document_outcome", None)
                    if owned is not None:
                        row["document_operation_id"] = owned["id"]
                    if isinstance(task.exception(), RetainedDocumentFailure):
                        row.update(request_executed=False, retained_outcome_hash=digest(task.exception().outcome),
                                   retained_operation_id=task.exception().outcome["id"])
                    if not independent_failure(task.exception()):
                        # A later caller cancellation may become primary while
                        # this genuine global failure remains a hard boundary.
                        row["global_error_type"] = type(task.exception()).__name__
                else:
                    row = {"url": url, "status": "retrieved", "retrieval": {
                        key: value for key, value in task.result().items() if key != "excerpt"}}
                owned = completed_outcomes.get(url)
                if owned is not None:
                    row["document_operation_id"] = owned["id"]
                history = histories.get(url)
                observations = history.observations() if history is not None else []
                if not task.cancelled() and task.exception() is not None:
                    observations += getattr(task.exception(), "observed_failures", [])
                if observations:
                    row["observed_failures"] = list({digest(fact): fact for fact in observations}.values())
                rows.append(row)
            outcome.acquisition_observations = rows
            outcome.acquisition_id = identifier("research-acquisition")
            outcome.document_ledger = self.ledger is not None
            raise outcome
        results = []
        for source in sources:
            evidence = documents[urldefrag(source.url)[0]].result()
            results.append({**source.model_dump(), "claimed_version": source.version,
                            "supported_version": None, "retrieval": {k: v for k, v in evidence.items() if k != "excerpt"},
                            "excerpt": evidence["excerpt"],
                            "verification_level": "document_retrieved; claim/version/first-party provenance require review; live behaviour requires tests"})
        return results


def request_documents(context: dict) -> dict:
    """One complete inline body per identical retrieval snapshot in this request.

    Claims, fragments, provenance and conflicting version claims stay on their own
    rows. No semantic merging, whitespace normalization or cross-request history.
    """
    import copy

    from .util import digest
    result = copy.deepcopy(context)
    bodies = {}
    visited = set()

    def visit(value):
        if isinstance(value, (dict, list)):
            if id(value) in visited:
                return
            visited.add(id(value))
        if isinstance(value, dict):
            retrieval, body = value.get("retrieval"), value.get("excerpt")
            if isinstance(retrieval, dict) and isinstance(body, str) and isinstance(retrieval.get("artifact"), dict):
                # The entire retrieval descriptor distinguishes versions and provenance.
                key = "research-" + digest([retrieval, body])
                rows = bodies.setdefault(key, [])
                rows.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    documents = []
    for key, rows in bodies.items():
        if len(rows) < 2:
            continue
        documents.append({"id": key, "body": rows[0]["excerpt"], "retrieval": rows[0]["retrieval"]})
        for row in rows:
            row.pop("excerpt")
            row["document_id"] = key
    if documents:
        if "research_document_bodies" in result:
            raise AA2Error("Research document table already exists in the input context")
        result["research_document_bodies"] = documents
    return result
