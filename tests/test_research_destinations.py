"""Research-only policy tests. Every DNS, TCP, TLS and HTTP response is a fixture."""
import asyncio
import base64
import json
import socket
import ssl

import httpcore
import httpx
import pytest
from pydantic import ValidationError
from test_research_proxy import RecordingStream, install_network
from test_research_resolver import answer, config, policy

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.collection_worker import execute
from auto_agents_2.config import Config, ResearchDocumentAuthorization, ResearchResolverConfig
from auto_agents_2.errors import (
    ResearchDestinationPolicyRejected,
    ResearchDocumentAuthorizationRequired,
    ResearchURLRejected,
)
from auto_agents_2.evidence import invocation_identity
from auto_agents_2.models import DocumentResult
from auto_agents_2.network import PublicHTTPTransport, PublicNetworkBackend, public_address
from auto_agents_2.research import ResearchService
from auto_agents_2.research_destinations import ResearchDestinationPolicy
from auto_agents_2.store import Store

SOURCE = "https://docs.example.test/reference"
TARGET = "https://independent.example.test/docs/page"


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A policy regression must never use real DNS or sockets")
    async def forbidden_async(*args, **kwargs):
        forbidden()
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden_async)
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def test_default_empty_grants_preserve_serialization_and_native_identity(tmp_path):
    original = config()
    body = original.model_dump(mode="json")
    assert "research_document_authorizations" not in body
    assert json.loads(original.model_dump_json()) == body
    assert Config.model_validate(body).model_dump(mode="json") == body
    selected = Config.model_validate({**body, "research_document_authorizations": [
        {"origin": "https://DOCS.INTRA.EXAMPLE.:8443/", "path_prefix": "/reference/"}]})
    assert selected.model_dump(mode="json")["research_document_authorizations"] == [
        {"origin": "https://docs.intra.example:8443", "path_prefix": "/reference"}]
    args = ("clarify", {"fixture": True}, DocumentResult, tmp_path)
    assert invocation_identity(*args, original, source_hash="EXAMPLE_SOURCE") == invocation_identity(
        *args, selected, source_hash="EXAMPLE_SOURCE")


@pytest.mark.parametrize("origin,prefix", [
    ("https://EXAMPLE_USER:EXAMPLE_PASSWORD@docs.example:8443", "/reference"),
    ("https://docs.example:8443/wider", "/reference"), ("https://docs.example:8443?x=1", "/reference"),
    ("https://docs.example:8443", "/"), ("https://docs.example:8443", "/reference/../other"),
    ("https://docs.example:8443", "/reference/%2e%2e/other"),
    ("https://docs.example:8443", "/reference%2fother"),
    ("https://docs.example:8443", "/reference/%252e%252e/other"),
    ("https://localhost:8443", "/reference"), ("https://127.0.0.1:8443", "/reference"),
    ("https://metadata.google.internal", "/reference"), ("https://docs.example:6379", "/reference"),
])
def test_explicit_document_grant_cannot_be_ambiguous_or_cover_special_targets(origin, prefix):
    with pytest.raises((ValidationError, ValueError)):
        ResearchDocumentAuthorization(origin=origin, path_prefix=prefix)


@pytest.mark.parametrize("target", [
    "https://docs.intra.example:8443/reference2",
    "https://docs.intra.example:8443/reference/../outside",
    "https://docs.intra.example:8443/reference/%2e%2e/outside",
    "https://docs.intra.example:8443/reference%2foutside",
    "https://docs.intra.example:8443/reference/%252foutside",
    "https://docs.intra.example.evil:8443/reference/chapter",
    "https://docs.intra.example:8444/reference/chapter",
    "http://docs.intra.example:8443/reference/chapter",
])
def test_document_grant_does_not_expand_path_host_port_or_scheme(target):
    grants = [{"origin": "https://docs.intra.example:8443", "path_prefix": "/reference/"}]
    with pytest.raises((ResearchDestinationPolicyRejected, ResearchDocumentAuthorizationRequired, ResearchURLRejected)):
        selected = ResearchDestinationPolicy(target, grants)
        url = httpx.URL(target)
        selected.admit_addresses(url.host, url.port, ("10.0.0.8",), "fixture")


@pytest.mark.parametrize("source,grant", [
    ("https://docs.intra.example:8443/reference", {"origin": "https://docs.intra.example:8443", "path_prefix": "/reference"}),
    ("http://docs.intra.example:8080/reference/chapter", {"origin": "http://docs.intra.example:8080", "path_prefix": "/reference"}),
])
def test_exact_document_authorization_allows_only_its_private_document(source, grant):
    selected = ResearchDestinationPolicy(source, [grant])
    url = httpx.URL(source)
    selected.admit_addresses(url.host, url.port, ("10.0.0.8",), "fixture")
    facts = selected.evidence()["tcp_bindings"][0]
    assert facts["explicit_document_authorization"] and facts["classification"] == "unknown"


@pytest.mark.parametrize("port", [8080, 8443])
def test_ordinary_public_document_custom_ports_remain_compatible(port):
    selected = ResearchDestinationPolicy(f"https://docs.example.test:{port}/reference")
    selected.admit_addresses("docs.example.test", port, ("93.184.216.34",), "fixture")
    assert selected.evidence()["tcp_bindings"][0]["classification"] == "public"


@pytest.mark.parametrize("port", [22, 2375, 2376, 3389, 6379])
@pytest.mark.parametrize("address", ["93.184.216.34", "10.0.0.8"])
def test_sensitive_service_ports_are_not_document_fallbacks(port, address):
    with pytest.raises(ResearchDestinationPolicyRejected, match="sensitive_service"):
        selected = ResearchDestinationPolicy(f"https://docs.example.test:{port}/reference")
        selected.admit_addresses("docs.example.test", port, (address,), "fixture")


@pytest.mark.parametrize("ip", ["10.0.0.8", "172.16.1.8", "192.168.1.8", "fd00::8",
                                "100.64.0.8", "198.18.0.8", "198.51.100.8"])
async def test_unknown_named_https_pins_numbers_and_preserves_original_strict_tls(monkeypatch, ip):
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
    lookups, connections = install_network(monkeypatch, stream, {"docs.example.test": [ip]})
    selected = ResearchDestinationPolicy(SOURCE)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(destination_policy=selected), trust_env=False) as client:
        assert (await client.get(SOURCE)).text == "ok"
    assert not public_address(ip) and connections == [(ip, 443)] and lookups == ["docs.example.test"]
    assert stream.tls == [("docs.example.test", ssl.CERT_REQUIRED, True)]
    assert b"Host: docs.example.test" in b"".join(stream.writes)
    facts = selected.evidence()["tcp_bindings"][0]
    assert facts["classification"] == "unknown" and facts["mapping_status"] == "not established"


@pytest.mark.parametrize("proxy_scheme", ["http", "https"])
async def test_unknown_proxy_route_keeps_numeric_connect_and_authentication_only_at_proxy(monkeypatch, proxy_scheme):
    monkeypatch.setenv("HTTPS_PROXY", f"{proxy_scheme}://EXAMPLE_USER:EXAMPLE_PASSWORD@127.0.0.1:7890")
    stream = RecordingStream([b"HTTP/1.1 200 Connection established\r\n\r\n",
                              b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
    _, connections = install_network(monkeypatch, stream, {"docs.example.test": ["198.18.0.8"]})
    selected = ResearchDestinationPolicy(SOURCE)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(destination_policy=selected), trust_env=False) as client:
        assert (await client.get(SOURCE)).text == "ok"
    assert connections == [("127.0.0.1", 7890)]
    request = b"".join(stream.writes)
    assert request.startswith(b"CONNECT 198.18.0.8:443 HTTP/1.1")
    assert base64.b64encode(b"EXAMPLE_USER:EXAMPLE_PASSWORD") in request
    origin_request = request[request.index(b"GET /reference"):]
    assert b"Proxy-Authorization" not in origin_request and b"EXAMPLE_PASSWORD" not in origin_request
    assert stream.tls[-1] == ("docs.example.test", ssl.CERT_REQUIRED, True)


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.169.254",
    "fe80::1", "0.0.0.0", "::", "224.0.0.1", "ff02::1", "255.255.255.255",
    "100.100.100.200", "168.63.129.16", "fd00:ec2::254", "64:ff9b::7f00:1", "2002:7f00:1::"])
async def test_special_or_mixed_addresses_stop_before_any_numeric_dial(monkeypatch, ip):
    stream = RecordingStream([])
    lookups, connections = install_network(monkeypatch, stream, {"docs.example.test": ["93.184.216.34", ip]})
    backend = PublicNetworkBackend(destination_policy=ResearchDestinationPolicy(SOURCE))
    with pytest.raises(ResearchDestinationPolicyRejected, match="special_address"):
        await backend.connect_tcp("docs.example.test", 443, 1)
    assert lookups == ["docs.example.test"] and connections == []


@pytest.mark.parametrize("target", ["https://localhost/reference", "https://sub.localhost/reference",
    "https://metadata.google.internal/reference", "https://metadata.aliyun.com/reference",
    "https://docs.example.test/admin/reboot", "https://docs.example.test/logout",
    "https://docs.example.test/reference?action=delete",
    "https://EXAMPLE_USER:EXAMPLE_PASSWORD@docs.example.test/reference"])
def test_special_hosts_authentication_and_management_actions_never_use_unknown_fallback(target):
    with pytest.raises((ResearchDestinationPolicyRejected, ResearchDocumentAuthorizationRequired, ResearchURLRejected)):
        ResearchDestinationPolicy(target)


async def test_new_connection_revalidates_rebinding_to_special_address(monkeypatch):
    stream = RecordingStream([])
    calls = []
    async def resolve(host, port, **kwargs):
        ip = "93.184.216.34" if not calls else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            calls.append((host, port))
            return stream
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    backend = PublicNetworkBackend(destination_policy=ResearchDestinationPolicy(SOURCE))
    await backend.connect_tcp("docs.example.test", 443, 1)
    with pytest.raises(ResearchDestinationPolicyRejected):
        await backend.connect_tcp("docs.example.test", 443, 1)
    assert calls == [("93.184.216.34", 443)]


def test_cross_origin_and_port_change_do_not_inherit_private_document_authorization():
    grants = [{"origin": "https://docs.example.test:8443", "path_prefix": "/reference"}]
    selected = ResearchDestinationPolicy("https://docs.example.test:8443/reference", grants)
    selected.admit_addresses("docs.example.test", 8443, ("10.0.0.8",), "fixture")
    selected.before_request(TARGET)
    with pytest.raises(ResearchDocumentAuthorizationRequired):
        selected.admit_addresses("independent.example.test", 443, ("10.0.0.9",), "fixture")
    selected = ResearchDestinationPolicy("https://docs.example.test:8443/reference", grants)
    selected.before_request("https://docs.example.test/reference")
    with pytest.raises(ResearchDocumentAuthorizationRequired):
        selected.admit_addresses("docs.example.test", 443, ("10.0.0.8",), "fixture")


def test_https_downgrade_is_forbidden_even_with_an_explicit_destination_grant():
    selected = ResearchDestinationPolicy(SOURCE, [{"origin": "http://independent.example.test", "path_prefix": "/docs"}])
    with pytest.raises(ResearchDestinationPolicyRejected, match="https_downgrade"):
        selected.before_request(TARGET.replace("https:", "http:"))


@pytest.mark.parametrize("authorized", [False, True])
async def test_cross_origin_private_redirect_requires_its_own_document_grant(tmp_path, monkeypatch, authorized):
    streams = [
        RecordingStream([("HTTP/1.1 302 Found\r\nLocation: " + TARGET + "\r\nContent-Length: 0\r\n"
                          "Set-Cookie: session=EXAMPLE_NOT_SENT\r\n\r\n").encode()]),
        RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])]
    calls = []
    async def resolve(host, port, **kwargs):
        ip = "93.184.216.34" if host == "docs.example.test" else "10.0.0.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            calls.append((host, port))
            return streams[len(calls) - 1]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    store = Store(tmp_path)
    store.admit("Fixture", {})
    grants = [{"origin": "https://independent.example.test", "path_prefix": "/docs"}] if authorized else []
    service = ResearchService(ArtifactStore(store.root), document_authorizations=grants)
    if authorized:
        result = await service.capture(SOURCE)
        assert result["excerpt"] == "ok" and calls == [("93.184.216.34", 443), ("10.0.0.8", 443)]
        second = b"".join(streams[1].writes)
        assert b"Cookie:" not in second and b"Authorization:" not in second
        assert streams[1].tls == [("independent.example.test", ssl.CERT_REQUIRED, True)]
    else:
        with pytest.raises(ResearchDocumentAuthorizationRequired):
            await service.capture(SOURCE)
        assert calls == [("93.184.216.34", 443)]


async def test_same_origin_redirects_do_not_store_or_send_source_cookies(tmp_path, monkeypatch):
    from auto_agents_2 import research, research_destinations
    jar = research_destinations.source_cookie_jar()
    monkeypatch.setattr(research_destinations, "source_cookie_jar", lambda: jar)
    requests = []
    def respond(request):
        requests.append(request)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/reference/final", "Set-Cookie": "session=EXAMPLE_NOT_SENT"})
        return httpx.Response(200, text="Complete fixture document")
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    store = Store(tmp_path)
    store.admit("Fixture", {})
    result = await ResearchService(ArtifactStore(store.root), document_authorizations=[]).capture(SOURCE)
    assert len(requests) == 2 and result["final_url"].endswith("/reference/final")
    assert list(jar) == []


@pytest.mark.parametrize("ip", ["93.184.216.34", "198.18.0.8"])
async def test_configured_doh_answers_use_policy_but_resolver_bootstrap_remains_strict(tmp_path, monkeypatch, ip):
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])
    _, connections = install_network(monkeypatch, stream)
    factories = []
    def dns_transport(**kwargs):
        factories.append(kwargs)
        def respond(request):
            kind = int(request.url.params["type"])
            return httpx.Response(200, json=answer(kind, [ip] if kind == 1 else [],
                                                  hostname="docs.example.test"))
        return httpx.MockTransport(respond)
    monkeypatch.setattr("auto_agents_2.network.PublicHTTPTransport", dns_transport)
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    store = Store(tmp_path)
    store.admit("Fixture", {})
    service = ResearchService(ArtifactStore(store.root), resolver_config=policy(), document_authorizations=[])
    result = await service.capture(SOURCE)
    assert connections == [(ip, 443)]
    assert result["network_policy"]["destination_policy"]["tcp_bindings"][0]["classification"] == (
        "public" if public_address(ip) else "unknown")
    assert factories and all("destination_policy" not in options for options in factories)
    with pytest.raises(ValidationError):
        ResearchResolverConfig(mode="https_json", endpoint="https://resolver.example/resolve", bootstrap_ips=["10.0.0.8"])


@pytest.mark.parametrize("grant", [False, True])
async def test_retained_target_denial_is_not_retried_by_new_policy_or_document_grant(tmp_path, monkeypatch, grant):
    from test_research_document_ledger import publication, setup

    from auto_agents_2.errors import ResearchDestinationRejected
    from auto_agents_2.research_documents import RetainedDocumentFailure
    _, _, _, service, doc = setup(tmp_path, publication((SOURCE,)))
    pending = service.ledger.start(SOURCE)
    saved = service.ledger.finish(SOURCE, pending, error=ResearchDestinationRejected("docs.example.test"))
    service.document_authorizations = ([{"origin": "https://docs.example.test", "path_prefix": "/reference"}]
                                       if grant else [])
    async def forbidden(self, url):
        pytest.fail("A new policy or document grant cannot retry a retained refusal")
    monkeypatch.setattr(ResearchService, "capture", forbidden)
    with pytest.raises(RetainedDocumentFailure):
        await service.verify(doc.sources)
    assert service.ledger.record()["documents"][SOURCE]["outcomes"] == [saved]


def test_documentation_about_oauth_is_not_an_automatic_login_action():
    selected = ResearchDestinationPolicy("https://docs.example.test/docs/oauth")
    selected.admit_addresses("docs.example.test", 443, ("198.18.0.8",), "fixture")
    with pytest.raises(ResearchDestinationPolicyRejected):
        ResearchDestinationPolicy("https://accounts.example.test/oauth/token")


@pytest.mark.parametrize("ip", ["10.0.0.8", "fd00::8"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_literal_private_https_documents_need_exact_scope_and_keep_strict_ip_identity(tmp_path, monkeypatch, ip, authorized):
    authority = "[" + ip + "]" if ":" in ip else ip
    source = "https://" + authority + "/reference/page"
    grants = [{"origin": "https://" + authority, "path_prefix": "/reference"}] if authorized else []
    if not authorized:
        with pytest.raises(ResearchDocumentAuthorizationRequired):
            ResearchDestinationPolicy(source)
        return
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])
    _, connections = install_network(monkeypatch, stream, {ip: [ip]})
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    store = Store(tmp_path)
    store.admit("Fixture", {})
    result = await ResearchService(ArtifactStore(store.root), document_authorizations=grants).capture(source)
    assert result["excerpt"] == "ok" and connections == [(ip, 443)]
    # Real TLS receives the IP identity with strict verification, requiring an IP SAN.
    assert stream.tls == [(ip, ssl.CERT_REQUIRED, True)]


@pytest.mark.parametrize("target", ["https://@docs.example.test/reference", "https://:@docs.example.test/reference"])
def test_empty_userinfo_is_still_rejected(target):
    with pytest.raises(ResearchURLRejected):
        ResearchDestinationPolicy(target)


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization", "Cookie"])
async def test_policy_transport_refuses_origin_credential_headers_before_dns(header):
    selected = ResearchDestinationPolicy(SOURCE)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(destination_policy=selected), trust_env=False) as client:
        with pytest.raises(ResearchDestinationPolicyRejected, match="origin_credentials"):
            await client.get(SOURCE, headers={header: "EXAMPLE_NEVER_TRANSMITTED"})


@pytest.mark.parametrize("ip", ["64:ff9b::5db8:d822", "2002:5db8:d822::1"])
def test_normal_public_translation_paths_keep_the_shared_public_address_semantics(ip):
    selected = ResearchDestinationPolicy(SOURCE)
    selected.admit_addresses("docs.example.test", 443, (ip,), "fixture")
    assert selected.evidence()["tcp_bindings"][0]["classification"] == (
        "public" if public_address(ip) else "unknown")


@pytest.mark.parametrize("ip", ["0.0.0.1", "0.1.2.3", "::ffff:0.1.2.3", "64:ff9b::1:203",
                                "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_embedded_special_and_this_network_targets_cannot_be_granted_or_dialed(monkeypatch, ip, authorized):
    stream = RecordingStream([])
    _, connections = install_network(monkeypatch, stream, {"docs.example.test": [ip]})
    grants = [{"origin": "https://docs.example.test", "path_prefix": "/reference"}] if authorized else []
    backend = PublicNetworkBackend(destination_policy=ResearchDestinationPolicy(SOURCE, grants))
    with pytest.raises(ResearchDestinationPolicyRejected, match="special_address"):
        await backend.connect_tcp("docs.example.test", 443, 1)
    assert connections == []


@pytest.mark.parametrize("target", ["https://10.000.0.8/reference", "https://167772168/reference",
                                    "https://0x0a000008/reference"])
def test_ambiguous_numeric_hosts_cannot_masquerade_as_independent_dns_sources(target):
    with pytest.raises(ResearchURLRejected):
        ResearchDestinationPolicy(target)


async def test_unrelated_denial_does_not_block_a_scoped_literal_but_known_numeric_alias_stays_denied(tmp_path, monkeypatch):
    from test_research_document_ledger import publication, setup

    from auto_agents_2.errors import ResearchDestinationRejected
    from auto_agents_2.models import SourceEvidence
    _, _, _, service, _ = setup(tmp_path, publication((SOURCE,)))
    refusal = ResearchDestinationRejected("docs.example.test")
    refusal.document_destination_bindings = [{"hostname": "docs.example.test", "port": 443,
        "addresses": ["198.18.0.9"], "classification": "unknown"}]
    pending = service.ledger.start(SOURCE)
    saved = service.ledger.finish(SOURCE, pending, error=refusal)
    grants = [{"origin": "https://10.0.0.8", "path_prefix": "/reference"},
              {"origin": "https://198.18.0.9", "path_prefix": "/reference"}]
    service.document_authorizations = grants
    # Exact new candidate registration is required for this different scoped document.
    store, rid = service.ledger.store, service.ledger.rid
    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    recovery = ResearchAcquisitionRecovery(service.ledger.recovery.engine)
    document = publication(("https://10.0.0.8/reference/page",))
    store.checkpoint(rid, "candidate/provider_research", {"document": document.model_dump()})
    service.ledger = recovery.begin(rid, document, recovery.dispatch_binding(rid, document))
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])
    _, connections = install_network(monkeypatch, stream, {"10.0.0.8": ["10.0.0.8"]})
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    result = await service.verify(document.sources)
    assert connections == [("10.0.0.8", 443)] and result[0]["excerpt"] == "ok"
    assert stream.tls == [("10.0.0.8", ssl.CERT_REQUIRED, True)]
    assert service.ledger.record()["documents"][SOURCE]["outcomes"] == [saved]
    assert service.ledger.excluded_addresses() == ("198.18.0.9",)
    # The explicit grant cannot turn a recorded numeric alias into a fresh target.
    alias = publication(("https://198.18.0.9/reference/page",))
    store.checkpoint(rid, "candidate/provider_research", {"document": alias.model_dump()})
    service.ledger = recovery.begin(rid, alias, recovery.dispatch_binding(rid, alias))
    with pytest.raises(ResearchDestinationRejected):
        await service.verify([SourceEvidence(url=alias.sources[0].url, claim=alias.sources[0].claim,
                                            version=alias.sources[0].version)])
    assert connections == [("10.0.0.8", 443)]


async def test_policy_metadata_event_hash_survives_real_secret_redaction_and_is_reused(tmp_path, monkeypatch):
    from test_research_document_ledger import publication, reopen, setup

    from auto_agents_2.util import digest
    store, rid, recovery, service, doc = setup(tmp_path, publication((SOURCE + "#one", SOURCE + "#two")))
    service.document_authorizations = [{"origin": "https://docs.example.test", "path_prefix": "/reference"}]
    stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])
    _, connections = install_network(monkeypatch, stream, {"docs.example.test": ["198.18.0.8"]})
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    await service.verify(doc.sources)
    outcome = service.ledger.record()["documents"][SOURCE]["outcomes"][0]
    event = next(row for row in store.events(rid) if row["kind"] == "research.document_finished")["data"]
    assert event["outcome"]["retrieval"]["network_policy"]["destination_policy"]["policy"]["authorizations"] == "<redacted>"
    assert event["run_id"] == rid and event["episode_id"] == outcome["episode_id"]
    assert event["operation_id"] == outcome["id"] and event["outcome_hash"] == digest(outcome)
    resumed = reopen(store, rid, recovery, doc)
    resumed.document_authorizations = service.document_authorizations
    assert (await resumed.verify(doc.sources))[0]["retrieval"]["acquisition_receipt"]["reused"]
    assert connections == [("198.18.0.8", 443)]


async def test_local_missing_grant_keeps_same_origin_sibling_and_only_exact_new_scope_can_recheck(tmp_path, monkeypatch):
    from test_research_document_ledger import publication, setup

    from auto_agents_2.research_documents import RetainedDocumentFailure
    bad = "https://docs.example.test:8443/missing/page"
    good = "https://docs.example.test:8443/reference/page"
    _, _, _, service, doc = setup(tmp_path, publication((bad, good)))
    service.document_authorizations = [{"origin": "https://docs.example.test:8443", "path_prefix": "/reference"}]
    connections = []
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))]
    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])
            connections.append((host, port, stream))
            return stream
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    with pytest.raises(ResearchDocumentAuthorizationRequired) as first:
        await service.verify(doc.sources)
    assert [row["status"] for row in first.value.acquisition_observations] == ["failed", "retrieved"]
    assert len(connections) == 1 and service.ledger.excluded_hosts() == ()
    original = service.ledger.record()["documents"][bad]["outcomes"][0]
    service.document_authorizations.append({"origin": "https://unrelated.example.test:8443", "path_prefix": "/missing"})
    with pytest.raises(RetainedDocumentFailure):
        await service.verify(doc.sources)
    assert len(connections) == 1
    service.document_authorizations.append({"origin": "https://docs.example.test:8443", "path_prefix": "/missing"})
    result = await service.verify(doc.sources)
    assert len(connections) == 2 and result[1]["retrieval"]["acquisition_receipt"]["reused"]
    assert service.ledger.record()["documents"][bad]["outcomes"][0] == original
    await service.verify(doc.sources)
    assert len(connections) == 2
    assert all(stream.tls == [("docs.example.test", ssl.CERT_REQUIRED, True)] for _, _, stream in connections)


@pytest.mark.parametrize("missing", ["target_connection_dispatched", "target_request_executed", "local_admission_only"])
def test_local_scope_without_original_undispatched_proof_is_not_released(missing):
    from auto_agents_2.research_acquisition import failure_observation
    from auto_agents_2.research_destinations import matching_document_grant
    error = ResearchDocumentAuthorizationRequired("docs.example.test", "https://docs.example.test:8443",
                                                "/reference/page", "private_document")
    error.evidence.pop(missing)
    assert matching_document_grant(failure_observation(error), [
        {"origin": "https://docs.example.test:8443", "path_prefix": "/reference"}]) is None


def test_old_remote_refusal_is_not_reinterpreted_as_a_new_local_scope_gap():
    from auto_agents_2.errors import ResearchDestinationRejected
    from auto_agents_2.research_acquisition import failure_observation
    from auto_agents_2.research_destinations import matching_document_grant
    old = ResearchDestinationRejected("docs.example.test")
    old.evidence.update(scope={"origin": "https://docs.example.test:8443", "path": "/reference/page"},
                        local_admission_only=True, target_request_executed=False,
                        target_connection_dispatched=False, policy_version=1)
    assert matching_document_grant(failure_observation(old), [
        {"origin": "https://docs.example.test:8443", "path_prefix": "/reference"}]) is None


async def blocked_grant_workflow(tmp_path, monkeypatch, *, still_blocked=False):
    from authority_review_fixtures import authority_review
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    from auto_agents_2.models import SourceEvidence
    from auto_agents_2.research_acquisition import CONTEXT, ResearchRecoveryDocument
    target = "https://docs.example.test:8443/private/page"
    class Agent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.authors = self.reviews = 0
        async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
            if stage == "provider_research":
                self.authors += 1
                source = SourceEvidence(url=target, claim="Original required fixture fact")
                if self.authors == 1:
                    return DocumentResult(summary="Original", document="Preserve the original fact.", sources=[source])
                recovery = context[CONTEXT]
                ready = bool(recovery["document_grant_updates"]) and not still_blocked
                return ResearchRecoveryDocument(summary="Ready" if ready else "A required fact still lacks evidence",
                    document="Preserve the complete original requirements.", sources=[source] if ready else [],
                    acquisition_resolution={"episode_id": recovery["episode_id"], "status": "ready" if ready else "blocked",
                        "claims": [{"claim_id": row["id"], "disposition": "supported" if ready else "blocked",
                            "source_indexes": [0] if ready else [], "requirement_ids": ["R1"],
                            "reason": "Same original fact, subject to complete controller collection and independent review"}
                            for row in recovery["claims"]],
                        "missing_information": [] if ready else ["Owner-scoped authoritative evidence for the required fact"]})
            if context.get("review_kind") == "research":
                self.reviews += 1
                return authority_review(context)
            if stage == "design":
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, work, rid, operation, fresh)
    connections = []
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))]
    class Backend:
        async def connect_tcp(self, host, port, *args, **kwargs):
            stream = RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"])
            connections.append((host, port, stream))
            return stream
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    async def collect(root, request, **kwargs):
        return execute(root, request)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers and preserve every original requirement.")
    agent = Agent()
    workflow = engine(project, agent)
    questions = []
    async def pause(question):
        questions.append(question)
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    assert agent.authors == 2 and agent.reviews == 0 and connections == [] and questions
    assert workflow.store.run(rid)["data"]["replans"] == 1
    return workflow, agent, rid, connections, questions


def owner_grants(workflow, values):
    body = json.loads(workflow.config.path.read_text())
    body["research_document_authorizations"] = values
    workflow.config.path.write_text(json.dumps(body))


async def test_exact_owner_grant_reenters_blocked_candidate_through_normal_archive_and_independent_review(tmp_path, monkeypatch):
    from test_workflow import engine

    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    workflow, agent, rid, connections, questions = await blocked_grant_workflow(tmp_path, monkeypatch)
    original = workflow.store.get(rid, "candidate/provider_research")["document"]
    original_question = workflow.store.question(questions[0]["id"])
    workflow.store.checkpoint(rid, "rounds/T01", {"round": 7})
    workflow.store.checkpoint(rid, "recovery/count/fixture", 4)
    owner_grants(workflow, [{"origin": "https://docs.example.test:8443", "path_prefix": "/unrelated"}])
    assert ResearchAcquisitionRecovery(workflow).prepare(rid) is None
    assert not ResearchAcquisitionRecovery(workflow).active(rid).get("document_grant_rechecks")
    assert workflow.store.run(rid)["data"]["replans"] == 1
    owner_grants(workflow, [{"origin": "https://docs.example.test:8443", "path_prefix": "/private"}])
    resumed = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await resumed.start()
    record = ResearchAcquisitionRecovery(resumed).episode(rid)
    assert agent.authors == 3 and agent.reviews == 1 and len(connections) == 1
    assert record["completed"] and len(record["document_grant_rechecks"]) == 1
    assert workflow.store.run(rid)["data"]["replans"] == 2
    history = workflow.store.get(rid, "recovery/gate-history/1")
    assert history["checkpoints"]["candidate/provider_research"]["document"] == original
    assert workflow.store.question(original_question["id"])["prompt"] == original_question["prompt"]
    assert workflow.store.get(rid, "rounds/T01") == {"round": 7}
    assert workflow.store.get(rid, "recovery/count/fixture") == 4
    assert record["attempts"][0]["observations"][0]["failure"]["code"] == "RESEARCH_DOCUMENT_AUTHORIZATION_REQUIRED"
    assert connections[0][2].tls == [("docs.example.test", ssl.CERT_REQUIRED, True)]
    again = engine(workflow.project, agent)
    with pytest.raises(asyncio.CancelledError):
        await again.start()
    assert agent.authors == 3 and len(connections) == 1 and workflow.store.run(rid)["data"]["replans"] == 2


async def test_local_grant_reentry_receipt_is_once_only_when_author_still_blocks(tmp_path, monkeypatch):
    from test_workflow import engine

    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    workflow, agent, rid, connections, _ = await blocked_grant_workflow(tmp_path, monkeypatch, still_blocked=True)
    owner_grants(workflow, [{"origin": "https://docs.example.test:8443", "path_prefix": "/private"}])
    async def pause(question):
        raise asyncio.CancelledError()
    for expected in (3, 3):
        resumed = engine(workflow.project, agent)
        resumed.question_handler = pause
        with pytest.raises(asyncio.CancelledError):
            await resumed.start()
        assert agent.authors == expected and workflow.store.run(rid)["data"]["replans"] == 2
    record = ResearchAcquisitionRecovery(workflow).active(rid)
    assert len(record["document_grant_rechecks"]) == 1 and connections == []
    assert sum(row["kind"] == "research.document_grant_reentry" for row in workflow.store.events(rid)) == 1


async def test_grant_reentry_event_failure_rolls_back_pending_and_receipt(tmp_path, monkeypatch):
    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    workflow, _, rid, connections, _ = await blocked_grant_workflow(tmp_path, monkeypatch)
    owner_grants(workflow, [{"origin": "https://docs.example.test:8443", "path_prefix": "/private"}])
    recovery = ResearchAcquisitionRecovery(workflow)
    before = recovery.active(rid)
    original_event = workflow.store._event
    def event(db, run_id, kind, data):
        if kind == "research.document_grant_reentry":
            raise OSError("EXAMPLE_REENTRY_EVENT_FAILURE")
        return original_event(db, run_id, kind, data)
    monkeypatch.setattr(workflow.store, "_event", event)
    with pytest.raises(OSError):
        recovery.prepare(rid)
    assert recovery.active(rid) == before and connections == []
    assert not workflow.store.has_event(rid, "research.document_grant_reentry")


async def test_matching_grant_does_not_reset_later_timeout_budget(tmp_path, monkeypatch):
    from test_research_document_ledger import publication, setup

    from auto_agents_2.errors import DocumentAcquisitionTimeout
    from auto_agents_2.research_documents import MAX_CAUSE_ATTEMPTS, RetainedDocumentFailure
    target = "https://docs.example.test:8443/private/page"
    _, _, _, service, doc = setup(tmp_path, publication((target,)))
    local = ResearchDocumentAuthorizationRequired("docs.example.test", "https://docs.example.test:8443",
                                                 "/private/page", "private_document")
    pending = service.ledger.start(target)
    service.ledger.finish(target, pending, error=local)
    service.document_authorizations = [{"origin": "https://docs.example.test:8443", "path_prefix": "/private"}]
    calls = []
    async def timeout(self, url):
        calls.append(url)
        raise DocumentAcquisitionTimeout("Fixture", 30, source_url=url)
    monkeypatch.setattr(ResearchService, "capture", timeout)
    with pytest.raises(DocumentAcquisitionTimeout):
        await service.verify(doc.sources)
    assert len(calls) == MAX_CAUSE_ATTEMPTS
    service.document_authorizations.append({"origin": "https://unrelated.example.test", "path_prefix": "/docs"})
    with pytest.raises(RetainedDocumentFailure):
        await service.verify(doc.sources)
    assert len(calls) == MAX_CAUSE_ATTEMPTS


@pytest.mark.parametrize("defect", ["missing_proof", "old_remote"])
async def test_exact_owner_grant_does_not_reenter_unproven_or_old_remote_observation(tmp_path, monkeypatch, defect):
    from auto_agents_2.errors import ResearchDestinationRejected
    from auto_agents_2.research_acquisition import ResearchAcquisitionRecovery
    if defect == "missing_proof":
        initialize = ResearchDocumentAuthorizationRequired.__init__
        def missing(self, *args, **kwargs):
            initialize(self, *args, **kwargs)
            self.evidence.pop("target_connection_dispatched")
        monkeypatch.setattr(ResearchDocumentAuthorizationRequired, "__init__", missing)
    else:
        def old(self, host, port, targets, source, *, record=True):
            error = ResearchDestinationRejected(host)
            error.evidence.update(scope={"origin": "https://docs.example.test:8443", "path": "/private/page"},
                                  local_admission_only=True, target_request_executed=False,
                                  target_connection_dispatched=False, policy_version=1)
            raise error
        monkeypatch.setattr(ResearchDestinationPolicy, "admit_addresses", old)
    workflow, agent, rid, connections, _ = await blocked_grant_workflow(tmp_path, monkeypatch)
    owner_grants(workflow, [{"origin": "https://docs.example.test:8443", "path_prefix": "/private"}])
    recovery = ResearchAcquisitionRecovery(workflow)
    assert recovery.prepare(rid) is None
    assert not recovery.active(rid).get("document_grant_rechecks")
    assert agent.authors == 2 and workflow.store.run(rid)["data"]["replans"] == 1 and connections == []


@pytest.mark.parametrize("damage", ["receipt", "event"])
async def test_grant_reentry_receipt_damage_stops_instead_of_reissuing_permission(tmp_path, monkeypatch, damage):
    from auto_agents_2.engine import Replan
    from auto_agents_2.faults import StateConflict
    from auto_agents_2.research_acquisition import PREFIX, ResearchAcquisitionRecovery
    workflow, _, rid, connections, _ = await blocked_grant_workflow(tmp_path, monkeypatch)
    owner_grants(workflow, [{"origin": "https://docs.example.test:8443", "path_prefix": "/private"}])
    recovery = ResearchAcquisitionRecovery(workflow)
    with pytest.raises(Replan):
        recovery.prepare(rid)
    record = recovery.active(rid)
    assert len(record["document_grant_rechecks"]) == 1
    if damage == "receipt":
        workflow.store.checkpoint(rid, PREFIX + record["id"], {**record, "document_grant_rechecks": []})
    else:
        with workflow.store.connect() as db:
            db.execute("DELETE FROM events WHERE run_id=? AND kind=?", (rid, "research.document_grant_reentry"))
    with pytest.raises(StateConflict, match="atomic receipt"):
        recovery.prepare(rid)
    assert connections == []
