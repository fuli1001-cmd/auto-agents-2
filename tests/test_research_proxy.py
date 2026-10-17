import asyncio
import base64
import socket
import ssl

import httpcore
import httpx
import pytest

from auto_agents_2.errors import AA2Error
from auto_agents_2.network import PublicHTTPTransport, PublicNetworkBackend
from auto_agents_2.network_proxy import ProxySettings


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, data):
        super().__init__(data)
        self.writes, self.tls = [], []
        self.closed = False

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((server_hostname, ssl_context.verify_mode, ssl_context.check_hostname))
        return self

    async def aclose(self):
        self.closed = True


def install_network(monkeypatch, stream, addresses=None):
    lookups, connections = [], []

    async def resolve(host, port, **kwargs):
        lookups.append(host)
        ips = (addresses or {}).get(host, ["93.184.216.34"])
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]

    class Backend:
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            connections.append((host, port))
            return stream

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    return lookups, connections


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("proxy_scheme", ["http", "https"])
async def test_proxy_tunnels_pinned_ip_preserving_origin_and_tls(monkeypatch, scheme, proxy_scheme):
    # Credentials are deliberately unmistakable test placeholders.
    monkeypatch.setenv(scheme.upper() + "_PROXY",
                       f"{proxy_scheme}://EXAMPLE_USER:EXAMPLE_PASSWORD@127.0.0.1:7890")
    stream = RecordingStream([b"HTTP/1.1 200 Connection established\r\n\r\n",
                              b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
    lookups, connections = install_network(monkeypatch, stream)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False) as client:
        response = await client.get(f"{scheme}://docs.example/resource")
        assert response.text == "ok"
    assert lookups == ["docs.example"]
    assert connections == [("127.0.0.1", 7890)]
    request = b"".join(stream.writes)
    port = 443 if scheme == "https" else 80
    assert request.startswith(f"CONNECT 93.184.216.34:{port} HTTP/1.1\r\n".encode())
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"EXAMPLE_USER:EXAMPLE_PASSWORD") in request
    origin_request = request[request.index(b"GET /resource"):]
    assert b"Host: docs.example\r\n" in origin_request
    assert b"EXAMPLE_PASSWORD" not in origin_request and b"Proxy-Authorization" not in origin_request
    expected_names = (["127.0.0.1"] if proxy_scheme == "https" else []) + (["docs.example"] if scheme == "https" else [])
    assert stream.tls == [(name, ssl.CERT_REQUIRED, True) for name in expected_names]
    assert stream.closed


@pytest.mark.parametrize("ips", [["198.18.0.22"], ["127.0.0.1"], ["93.184.216.34", "10.0.0.1"],
                                ["::ffff:127.0.0.1"]])
async def test_proxy_does_not_bypass_public_destination_policy(monkeypatch, ips):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    stream = RecordingStream([])
    _, connections = install_network(monkeypatch, stream, {"docs.example": ips})
    async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False) as client:
        with pytest.raises(AA2Error, match="DNS/Fake-IP.*docs.example"):
            await client.get("https://docs.example/")
    assert connections == []


async def test_proxy_redirect_revalidates_destination(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    stream = RecordingStream([b"HTTP/1.1 200 Connection established\r\n\r\n",
                              b"HTTP/1.1 302 Found\r\nLocation: https://internal.example/\r\nContent-Length: 0\r\n\r\n"])
    lookups, connections = install_network(monkeypatch, stream, {"internal.example": ["10.0.0.1"]})
    async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False, follow_redirects=True) as client:
        with pytest.raises(AA2Error, match="exclusively"):
            await client.get("https://docs.example/")
    assert lookups == ["docs.example", "internal.example"]
    assert connections == [("127.0.0.1", 7890)]


@pytest.mark.parametrize("bypass", ["no_proxy", "NO_PROXY", "trust_env"])
async def test_explicit_proxy_bypass_retains_direct_pinning(monkeypatch, bypass):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    if bypass != "trust_env":
        monkeypatch.setenv(bypass, ".example")
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
    _, connections = install_network(monkeypatch, stream)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(trust_env=bypass != "trust_env"), trust_env=False) as client:
        assert (await client.get("https://docs.example/")).text == "ok"
    assert connections == [("93.184.216.34", 443)]
    assert not b"".join(stream.writes).startswith(b"CONNECT")


@pytest.mark.parametrize("response", [b"HTTP/1.1 407 EXAMPLE_PASSWORD\r\nContent-Length: 0\r\n\r\n",
                                    b"HTTP/1.1 502 Failed\r\nContent-Length: 0\r\n\r\n"])
async def test_proxy_failure_closes_stream_and_sanitizes_error(monkeypatch, response):
    monkeypatch.setenv("HTTPS_PROXY", "http://EXAMPLE_USER:EXAMPLE_PASSWORD@127.0.0.1:7890")
    stream = RecordingStream([response])
    _, connections = install_network(monkeypatch, stream)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False) as client:
        with pytest.raises(httpx.ProxyError) as error:
            await client.get("https://docs.example/")
    assert "EXAMPLE_PASSWORD" not in str(error.value)
    assert connections == [("127.0.0.1", 7890)]  # No silent direct fallback.
    assert stream.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_proxy_interrupted_handshake_closes_socket(monkeypatch, cancel):
    class WaitingStream(RecordingStream):
        async def read(self, max_bytes, timeout=None):
            if cancel:
                raise asyncio.CancelledError
            await asyncio.Future()

    stream = WaitingStream([])
    install_network(monkeypatch, stream)
    backend = PublicNetworkBackend(httpx.Proxy("http://127.0.0.1:7890"))
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await backend.connect_tcp("docs.example", 443, timeout=0.02)
    assert stream.closed


async def test_ipv6_connect_target_has_brackets(monkeypatch):
    stream = RecordingStream([b"HTTP/1.1 200 Connection established\r\n\r\n"])
    install_network(monkeypatch, stream, {"docs.example": ["2606:4700::1111"]})
    backend = PublicNetworkBackend(httpx.Proxy("http://127.0.0.1:7890"))
    result = await backend.connect_tcp("docs.example", 443, timeout=1)
    assert b"".join(stream.writes).startswith(b"CONNECT [2606:4700::1111]:443 HTTP/1.1\r\n")
    await result.aclose()


def test_proxy_environment_precedence_and_all_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7892")
    settings = ProxySettings()
    assert settings.for_origin(httpcore.URL("https://docs.example").origin).url.port == 7892
    assert settings.for_origin(httpcore.URL("http://docs.example").origin).url.port == 7890


@pytest.mark.parametrize("value", ["socks5://127.0.0.1:7890", "http://EXAMPLE_PASSWORD@[invalid",
                                 "http://127.0.0.1:7890/private?token=EXAMPLE_PASSWORD"])
def test_invalid_proxy_configuration_is_sanitized(monkeypatch, value):
    monkeypatch.setenv("HTTPS_PROXY", value)
    with pytest.raises(AA2Error) as error:
        ProxySettings().for_origin(httpcore.URL("https://docs.example").origin)
    assert "EXAMPLE_PASSWORD" not in str(error.value)
