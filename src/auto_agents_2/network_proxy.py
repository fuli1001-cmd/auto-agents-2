"""User-configured proxy routing with numeric, already validated destinations."""
from __future__ import annotations

import base64
import ssl
from urllib.parse import urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

import httpcore
import httpx

from .errors import AA2Error


class ProxySettings:
    def __init__(self, trust_env=True):
        self.proxies = getproxies_environment() if trust_env else {}

    def for_origin(self, origin):
        host = origin.host.decode("ascii")
        authority = f"[{host}]:{origin.port}" if ":" in host else f"{host}:{origin.port}"
        if proxy_bypass_environment(authority, self.proxies):
            return None
        value = self.proxies.get(origin.scheme.decode("ascii")) or self.proxies.get("all")
        if not value:
            return None
        try:
            value = value if "://" in value else "http://" + value
            parsed = urlsplit(value)
            if not parsed.hostname or parsed.port == 0:
                raise ValueError
            proxy = httpx.Proxy(value)
            if (proxy.url.scheme not in {"http", "https"} or not proxy.url.host
                    or proxy.url.path not in {"", "/"} or proxy.url.query or proxy.url.fragment):
                raise ValueError
            return proxy
        except (ValueError, httpx.InvalidURL):
            raise AA2Error("Research proxy must be a valid HTTP(S) proxy; check proxy environment variables") from None


async def open_proxy_tunnel(backend, proxy, ip, port, timeout, local_address, socket_options):
    """The proxy receives a numeric CONNECT target, never a hostname to resolve.

    The returned stream belongs to the origin connection, which performs its own
    TLS handshake with the original hostname and normal certificate validation.
    """
    url = httpcore.URL(scheme=proxy.url.raw_scheme, host=proxy.url.raw_host,
                       port=proxy.url.port, target=b"/")
    stream = await backend.connect_tcp(url.host.decode("ascii"), url.origin.port,
                                      timeout, local_address, socket_options)
    try:
        if url.scheme == b"https":
            stream = await stream.start_tls(ssl_context=ssl.create_default_context(),
                                            server_hostname=url.host.decode("ascii"), timeout=timeout)
        target = (f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}").encode("ascii")
        headers = [(b"Host", target)]
        if proxy.raw_auth is not None:
            headers.append((b"Proxy-Authorization", b"Basic " + base64.b64encode(b":".join(proxy.raw_auth))))
        connection = httpcore.AsyncHTTP11Connection(origin=url.origin, stream=stream)
        response = await connection.handle_async_request(httpcore.Request(
            method=b"CONNECT",
            url=httpcore.URL(scheme=url.scheme, host=url.host, port=url.port, target=target),
            headers=headers, extensions={"timeout": {"read": timeout, "write": timeout}},
        ))
        if not 200 <= response.status < 300:
            raise httpcore.ProxyError("Research proxy refused the public destination tunnel")
        return response.extensions["network_stream"]
    except BaseException:
        await stream.aclose()
        raise
