"""Public research transport: resolve and validate at the actual TCP boundary."""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from .errors import (
    ResearchDestinationRejected,
    http_failure_classification,
    http_failure_details,
    preserve_http_connection_failure,
)
from .network_proxy import ProxySettings, open_proxy_tunnel
from .process_ownership import PreviewOwnershipError, verify_connection
from .research_batch import ResearchRequestNotExecuted, current_batch, current_http_failure
from .util import digest

preview_owner = ContextVar("preview_owner", default=None)
OWNERSHIP_FAILURE = "Preview connection ownership could not be verified"


@contextmanager
def http_errors(request=None):
    """Keep HTTPX's public exception hierarchy at both request and stream boundaries."""
    try:
        yield
    except (httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError,
            httpcore.ProxyError, httpcore.UnsupportedProtocol) as exc:
        kinds = ("ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
                 "ConnectError", "ReadError", "WriteError", "NetworkError",
                 "LocalProtocolError", "RemoteProtocolError", "ProtocolError", "ProxyError", "UnsupportedProtocol")
        kind = next((name for name in kinds if isinstance(exc, getattr(httpcore, name))), "TransportError")
        raise getattr(httpx, kind)("Research transport failed", request=request) from exc
    except TimeoutError as exc:
        raise httpx.ConnectTimeout("Research connection timed out", request=request) from exc
    except OSError as exc:
        raise httpx.ConnectError("Research connection failed", request=request) from exc


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv6Address):
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return public_address(str(ipaddress.IPv4Address(int(address) & 0xffffffff)))
        if address in ipaddress.ip_network("64:ff9b:1::/48"):
            return False  # Local-use translation prefixes are not public destinations.
        if address.sixtofour and not public_address(str(address.sixtofour)):
            return False
        if address.teredo and not all(public_address(str(ip)) for ip in address.teredo):
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, proxy=None, *, resolver=None):
        self.backend = httpcore.AnyIOBackend()
        self.proxy, self.resolver = proxy, resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        async with asyncio.timeout(timeout):
            batch = current_batch.get()
            if batch is not None:
                batch.before_connect(self.proxy)
            resolution = None
            if self.resolver is None:
                addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
                targets = list(dict.fromkeys(row[4][0] for row in addresses))
            else:
                resolution = await self.resolver.resolve(host, port, timeout)
                targets = list(dict.fromkeys(resolution.addresses))
            if not targets or not all(public_address(ip) for ip in targets):
                raise ResearchDestinationRejected(host, empty_resolution=not targets)
            observed = []

            def observe(failure):
                previous = next((row for row in observed if row[0] is failure), None)
                if previous is not None:
                    return not previous[2][1]
                try:
                    with http_errors():
                        raise failure
                except httpx.HTTPError as mapped:
                    details = http_failure_details(mapped)
                classification = http_failure_classification(details)
                observed.append((failure, details, classification))
                if batch is not None and not classification[1]:
                    # Freeze in the failing attempt, before proxy cleanup or
                    # semaphore release can dispatch another connection.
                    indexes = sorted({*range(min(14, len(observed))), len(observed) - 1})
                    summary = [{**observed[i][1], "attempt": i + 1} for i in indexes]
                    preserve_http_connection_failure(failure, "public_connect",
                                                     attempts=summary, attempt_count=len(observed))
                    try:
                        with http_errors():
                            raise failure
                    except httpx.HTTPError as mapped:
                        handler = current_http_failure.get()
                        owned = handler(mapped) if handler is not None else None
                    batch.freeze_connect(self.proxy, host, port, details, owned)
                return not classification[1]

            for ip in targets:
                if batch is not None:
                    try:
                        batch.before_connect(self.proxy)
                    except ResearchRequestNotExecuted:
                        if not observed:
                            raise
                        break  # Keep this call's real failure, not a blocked substitute.
                try:
                    if self.proxy is not None:
                        kwargs = {"failure_observer": observe} if batch is not None else {}
                        stream = await open_proxy_tunnel(self.backend, self.proxy, ip, port,
                                                         timeout, local_address, socket_options, **kwargs)
                    else:
                        # Preserve origin Host/TLS while dialling only the validated number.
                        stream = await self.backend.connect_tcp(ip, port, timeout, local_address, socket_options)
                    if resolution is not None:
                        self.resolver.connected(resolution, ip, port)
                    return stream
                except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout, httpcore.ProxyError) as exc:
                    if batch is None and isinstance(exc, httpcore.ProxyError):
                        raise  # Preserve the original non-batch proxy behavior.
                    stopped = observe(exc)
                    if batch is not None and stopped:
                        break
            # Non-batch consumers keep their accepted address fallback behavior.
            # Batch attempts stop at a newly observed non-automatic boundary.
            selected = next((i for i, row in enumerate(observed) if row[2][0] in {"security", "permission"}),
                            next((i for i, row in enumerate(observed) if not row[2][1]), len(observed) - 1))
            error = observed[selected][0]
            indexes = sorted({*range(min(14, len(observed))), selected, len(observed) - 1})
            summary = [{**observed[i][1], "attempt": i + 1} for i in indexes]
            preserve_http_connection_failure(error, "public_connect", attempts=summary, attempt_count=len(observed))
            if isinstance(error, (httpcore.ConnectTimeout, httpcore.ConnectError, httpcore.ProxyError)):
                raise error
            raise httpcore.ConnectError("Public research connection failed") from error

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class PublicConnectionPool(httpcore.AsyncConnectionPool):
    def __init__(self, trust_env=True, *, resolver=None, proxy_settings=None):
        super().__init__(network_backend=PublicNetworkBackend(resolver=resolver))
        self.resolver = resolver
        batch = current_batch.get()
        self.proxy_settings = (proxy_settings if proxy_settings is not None else
                               batch.proxy_settings if batch is not None and trust_env else ProxySettings(trust_env))

    def create_connection(self, origin):
        proxy = self.proxy_settings.for_origin(origin)
        if proxy is None:
            return super().create_connection(origin)
        return httpcore.AsyncHTTPConnection(origin=origin, network_backend=PublicNetworkBackend(proxy, resolver=self.resolver))


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream, request=None):
        self.stream, self.request = stream, request

    async def __aiter__(self):
        with http_errors(self.request):
            async for chunk in self.stream:
                yield chunk

    async def aclose(self):
        with http_errors(self.request):
            await self.stream.aclose()


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, trust_env=True, resolver=None, proxy_settings=None):
        self.pool = PublicConnectionPool(trust_env, resolver=resolver, proxy_settings=proxy_settings)

    async def handle_async_request(self, request):
        previous_trace = request.extensions.get("trace")

        async def trace(name, info):
            phase = {"connection.connect_tcp.failed": "public_connect",
                     "connection.start_tls.failed": "origin_tls"}.get(name)
            if phase is not None:
                preserve_http_connection_failure(info.get("exception"), phase)
            if previous_trace is not None:
                coroutine = previous_trace(name, info)
                if not inspect.iscoroutine(coroutine):
                    raise TypeError("The asynchronous trace callback must return a coroutine")
                await coroutine

        incoming = httpcore.Request(method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions={**request.extensions, "trace": trace})
        try:
            with http_errors(request):
                response = await self.pool.handle_async_request(incoming)
        except ResearchDestinationRejected as exc:
            # Each redirect is a separate request. Bind the rejected hop without
            # retaining its path, query, credentials or request object in errors.
            exc.evidence["source_url_hash"] = digest(str(request.url))
            raise
        return httpx.Response(response.status, headers=response.headers,
                              stream=ResponseStream(response.stream, request), extensions=response.extensions)

    async def aclose(self):
        with http_errors():
            await self.pool.aclose()


class PreviewProxy:
    """Browser proxy that can dial only the controller-selected preview endpoint.

    Request interception alone does not cover Chromium's automatic redirects.
    This boundary applies to the actual connection, including redirect hops.
    """

    def __init__(self, origin, errors):
        self.origin, self.errors = urlsplit(origin), errors
        self.owner = preview_owner.get()
        self.ownership_failed = False
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0, limit=65536)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    def accept(self, reader, writer):
        task = asyncio.create_task(self.forward(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def stop(self, *args):
        self.server.close()
        for task in self.tasks:
            if not task.cancelling():
                task.cancel()

    async def close(self):
        pending = list(self.tasks)
        self.stop()
        await self.server.wait_closed()
        await asyncio.gather(*pending, return_exceptions=True)

    async def forward(self, reader, writer):
        upstream = None
        pumps = []
        try:
            async with asyncio.timeout(10):
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("iso-8859-1").split("\r\n")
                method, target, version = lines[0].split(" ", 2)
                address = urlsplit("http://" + target if method == "CONNECT" else target)
                if (address.hostname != self.origin.hostname or address.port != self.origin.port
                        or address.username or address.password
                        or method != "CONNECT" and address.scheme not in {"http", "ws"}):
                    self.errors.append("Preview attempted external request through redirect or proxy")
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    return
                incoming, upstream = await asyncio.open_connection(self.origin.hostname, self.origin.port)
                if self.owner is not None:
                    await verify_connection(self.owner, upstream)
                upgrade = method == "CONNECT"
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    path = urlunsplit(("", "", address.path or "/", address.query, ""))
                    headers = [line for line in lines[1:] if line]
                    upgrade = any(line.lower().startswith("upgrade:") for line in headers)
                    if not upgrade:
                        headers = [line for line in headers if not line.lower().startswith(("connection:", "proxy-connection:"))]
                        headers.append("Connection: close")
                    upstream.write((f"{method} {path} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
                    await upstream.drain()

            async def relay(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()
                    await destination.drain()
            pumps = [asyncio.create_task(relay(reader, upstream)), asyncio.create_task(relay(incoming, writer))]
            async def finish_relays():
                completed, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                for task in completed:
                    task.result()  # Observe either direction's error immediately.
                if upgrade:
                    await asyncio.gather(*pumps)
                elif pumps[1] not in completed:
                    # A successful request half-close still permits the full reply.
                    await pumps[1]
            if upgrade:
                async with asyncio.timeout(10):
                    await finish_relays()
            else:
                await finish_relays()
        except PreviewOwnershipError:
            self.ownership_failed = True
            self.errors.append(OWNERSHIP_FAILURE)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # Navigation/connection failures remain visible to the browser inspector.
        finally:
            for task in pumps:
                task.cancel()
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
            await asyncio.gather(*pumps, return_exceptions=True)
