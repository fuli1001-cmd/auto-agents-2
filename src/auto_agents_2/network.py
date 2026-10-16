"""Public research transport: resolve and validate at the actual TCP boundary."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from .errors import AA2Error
from .process_ownership import PreviewOwnershipError, verify_connection

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
    def __init__(self):
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        async with asyncio.timeout(timeout):
            addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            targets = list(dict.fromkeys(row[4][0] for row in addresses))
            if not targets or not all(public_address(ip) for ip in targets):
                raise AA2Error("Research destination must resolve exclusively to public addresses")
            error = None
            for ip in targets:
                try:
                    # Only the numeric validated address is dialled. The pool retains
                    # the original hostname for Host, TLS SNI and certificate checks.
                    return await self.backend.connect_tcp(ip, port, timeout, local_address, socket_options)
                except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    error = exc
            if isinstance(error, (httpcore.ConnectTimeout, httpcore.ConnectError)):
                raise error
            raise httpcore.ConnectError("Public research connection failed") from error

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


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
    def __init__(self):
        self.pool = httpcore.AsyncConnectionPool(network_backend=PublicNetworkBackend())

    async def handle_async_request(self, request):
        incoming = httpcore.Request(method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        with http_errors(request):
            response = await self.pool.handle_async_request(incoming)
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
            await asyncio.gather(*pumps)
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
