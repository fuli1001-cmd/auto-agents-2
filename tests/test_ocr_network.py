import asyncio
import http.server
import socket
import threading
from contextlib import contextmanager

import httpx
import pytest
from test_browser import browser_cache as browser_cache

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.design import preview_context
from auto_agents_2.errors import AA2Error
from auto_agents_2.models import SourceEvidence
from auto_agents_2.network import PublicNetworkBackend
from auto_agents_2.research import ResearchService
from auto_agents_2.store import Store


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::ffff:127.0.0.1"]])
async def test_dns_policy_rejects_private_results_before_connect(monkeypatch, addresses):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in addresses]
    async def dial(*args, **kwargs):
        pytest.fail("private destination must never be dialled")
    backend = PublicNetworkBackend()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(backend.backend, "connect_tcp", dial)
    with pytest.raises(AA2Error):
        await backend.connect_tcp("docs.example", 80, timeout=1)


async def test_connection_pins_numeric_address_without_second_dns_lookup(monkeypatch):
    lookups, connections = [], []
    async def resolve(host, port, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    async def dial(host, *args):
        connections.append(host)
        return "stream"
    backend = PublicNetworkBackend()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(backend.backend, "connect_tcp", dial)
    assert await backend.connect_tcp("docs.example", 443, timeout=1) == "stream"
    assert lookups == ["docs.example"] and connections == ["93.184.216.34"]


async def test_research_redirect_cannot_fetch_internal_response(tmp_path, monkeypatch):
    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    service = ResearchService(ArtifactStore(Store(tmp_path).root))
    with pytest.raises(AA2Error):
        await service.capture("https://docs.example/start")
    assert requests == ["https://docs.example/start"]


async def test_invalid_later_source_starts_no_fetches(tmp_path, monkeypatch):
    service = ResearchService(ArtifactStore(Store(tmp_path).root))
    started = []
    async def fetch(url):
        started.append(url)
    monkeypatch.setattr(service, "capture", fetch)
    with pytest.raises(AA2Error):
        await service.verify([SourceEvidence(url="https://docs.example", claim="one"),
                              SourceEvidence(url="file:///private", claim="two")])
    await asyncio.sleep(0)
    assert started == []


@contextmanager
def server(response):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = response(self.path)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["fetch", "redirect", "websocket", "popup"])
async def test_browser_origin_boundary_blocks_actual_external_requests(browser_cache, mode):
    from playwright.async_api import Error, async_playwright
    requests = []
    def external(path):
        requests.append(path)
        return 200, {}, b"private endpoint"
    with server(external) as target:
        def application(path):
            if mode == "redirect":
                return 302, {"Location": target + "/sensitive"}, b""
            script = {
                "fetch": f"fetch('{target}/sensitive').catch(()=>{{}})",
                "websocket": f"new WebSocket('{target.replace('http:', 'ws:')}/sensitive')",
                "popup": f"window.open('{target}/sensitive')",
            }[mode]
            return 200, {"Content-Type": "text/html"}, f"<h1>fixture</h1><script>{script}</script>".encode()
        with server(application) as origin:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                errors = []
                try:
                    context = await preview_context(browser, origin, errors, 800, 600)
                    page = await context.new_page()
                    try:
                        await page.goto(origin, wait_until="networkidle", timeout=4000)
                    except Error:
                        pass
                    await page.wait_for_timeout(300)
                    assert errors
                    assert requests == []
                finally:
                    await context.close()
                    assert not context.proxy.server.is_serving()
                    await browser.close()
