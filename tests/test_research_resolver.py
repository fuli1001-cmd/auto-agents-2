import asyncio
import copy
import json
import ssl
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_research_proxy import RecordingStream, install_network

from auto_agents_2.config import Config, ResearchResolverConfig
from auto_agents_2.errors import AA2Error, ResearchDestinationRejected
from auto_agents_2.evidence import invocation_identity
from auto_agents_2.models import DocumentResult
from auto_agents_2.network import PublicHTTPTransport, PublicNetworkBackend
from auto_agents_2.network_proxy import ProxySettings
from auto_agents_2.research_dns import (
    DNS_BODY_LIMIT,
    BootstrapResolver,
    HTTPSJSONResolver,
    ResolvedAddresses,
    parse_dns_json,
)


@pytest.fixture(autouse=True)
def no_proxy_environment(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def policy(**changes):
    return ResearchResolverConfig(mode="https_json", endpoint="https://resolver.example/resolve",
                                  bootstrap_ips=["8.8.8.8"], **changes)


def config(resolver=None):
    return Config(active_provider="fixture", providers={"fixture": {
        "kind": "codex", "binary": "EXAMPLE_NOT_EXECUTED", "profile_map": {"deep": {"model": "EXAMPLE_MODEL"}}}},
        research_resolver=resolver)


def answer(kind=1, addresses=None, *, hostname="docs.example", aliases=()):
    addresses = (["93.184.216.34"] if kind == 1 else ["2606:4700:4700::1111"]) if addresses is None else addresses
    owner = aliases[-1][1] if aliases else hostname
    rows = [{"name": name + ".", "type": 5, "TTL": 60, "data": target + "."} for name, target in aliases]
    rows.extend({"name": owner + ".", "type": kind, "TTL": 60, "data": value} for value in addresses)
    return {"Status": 0, "TC": False, "Question": [{"name": hostname + ".", "type": kind}], "Answer": rows}


def install_dns(monkeypatch, handler):
    factories = []

    def transport(**kwargs):
        factories.append(kwargs)
        return httpx.MockTransport(handler)

    monkeypatch.setattr("auto_agents_2.network.PublicHTTPTransport", transport)
    return factories


async def no_system_dns(*args, **kwargs):
    pytest.fail("Opt-in research DNS must not use system hostname resolution")


def test_default_config_dump_and_native_identity_are_compatible(tmp_path):
    default = config()
    body = default.model_dump(mode="json")
    assert "research_resolver" not in body
    assert json.loads(default.model_dump_json()) == body
    assert Config.model_validate(body).model_dump(mode="json") == body
    selected = default.model_copy(update={"research_resolver": policy()})
    arguments = ("clarify", {"fixture": True}, DocumentResult, tmp_path)
    assert invocation_identity(*arguments, default, source_hash="EXAMPLE_SOURCE") == invocation_identity(
        *arguments, selected, source_hash="EXAMPLE_SOURCE")
    assert selected.model_dump(mode="json")["research_resolver"]["mode"] == "https_json"


@pytest.mark.parametrize("changes", [
    {"mode": "system"}, {"endpoint": "http://resolver.example/resolve"},
    {"endpoint": "https://EXAMPLE_USER:EXAMPLE_PASSWORD@resolver.example/resolve"},
    {"endpoint": "https://resolver.example/resolve?token=EXAMPLE_VALUE"},
    {"endpoint": "https://resolver.example/resolve#fragment"}, {"endpoint": "https://resolver.example:0/"},
    {"bootstrap_ips": []}, {"bootstrap_ips": ["198.18.0.1"]}, {"bootstrap_ips": ["2001:2::1"]},
    {"bootstrap_ips": ["8.8.8.8", "10.0.0.1"]}, {"bootstrap_ips": ["resolver.example"]},
    {"bootstrap_ips": ["::ffff:127.0.0.1"]}, {"bootstrap_ips": ["2606:4700:4700::1111%eth0"]},
])
def test_resolver_configuration_requires_explicit_safe_bootstrap(changes):
    body = policy().model_dump()
    body.update(changes)
    with pytest.raises(ValidationError):
        ResearchResolverConfig.model_validate(body)


@pytest.mark.parametrize("missing", ["mode", "endpoint", "bootstrap_ips"])
def test_resolver_configuration_has_no_implicit_endpoint(missing):
    body = policy().model_dump()
    body.pop(missing)
    with pytest.raises(ValidationError):
        ResearchResolverConfig.model_validate(body)


@pytest.mark.parametrize("kind", [1, 28])
def test_dns_json_accepts_complete_alias_chain_and_nodata(kind):
    response = answer(kind, aliases=[("docs.example", "edge.example")])
    expected = tuple(row["data"] for row in response["Answer"] if row["type"] == kind)
    assert parse_dns_json(json.dumps(response), "docs.example", kind) == expected
    response = answer(kind, [])
    response.pop("Answer")
    assert parse_dns_json(json.dumps(response), "docs.example", kind) == ()


@pytest.mark.parametrize("case", ["status", "tc", "question_name", "question_type", "duplicate",
    "unrelated", "family", "integer", "zone", "loop", "ambiguous", "too_many", "unsupported"])
def test_dns_json_rejects_malformed_or_unmatched_answers(case):
    response = answer()
    if case == "status":
        response["Status"] = 2
    elif case == "tc":
        response["TC"] = True
    elif case == "question_name":
        response["Question"][0]["name"] = "other.example."
    elif case == "question_type":
        response["Question"][0]["type"] = 28
    elif case == "unrelated":
        response["Answer"][0]["name"] = "other.example."
    elif case == "family":
        response["Answer"][0]["data"] = "2606:4700:4700::1111"
    elif case == "integer":
        response["Answer"][0]["data"] = 1572395042
    elif case == "zone":
        response = answer(28, ["2606:4700:4700::1111%eth0"])
    elif case == "loop":
        response = answer(1, [], aliases=[("docs.example", "edge.example"), ("edge.example", "docs.example")])
    elif case == "ambiguous":
        response = answer(1, [], aliases=[("docs.example", "one.example"), ("docs.example", "two.example")])
    elif case == "too_many":
        response["Answer"] *= 257
    elif case == "unsupported":
        response["Answer"][0]["type"] = 15
    raw = json.dumps(response)
    if case == "duplicate":
        raw = raw.replace('"Status": 0', '"Status": 0, "Status": 0')
    with pytest.raises(AA2Error):
        parse_dns_json(raw, "docs.example", 28 if case == "zone" else 1)


async def test_both_families_finish_before_any_target_dial(monkeypatch):
    ipv6_started, release_ipv6 = asyncio.Event(), asyncio.Event()
    async def respond(request):
        kind = int(request.url.params["type"])
        assert request.url.params["cd"] == "false"
        if kind == 28:
            ipv6_started.set()
            await release_ipv6.wait()
        return httpx.Response(200, json=answer(kind))
    factories = install_dns(monkeypatch, respond)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", no_system_dns)
    resolver = HTTPSJSONResolver(policy(), ProxySettings(False))
    backend = PublicNetworkBackend(resolver=resolver)
    calls = []
    async def dial(ip, port, *args):
        calls.append((ip, port))
        return "EXAMPLE_STREAM"
    monkeypatch.setattr(backend.backend, "connect_tcp", dial)
    task = asyncio.create_task(backend.connect_tcp("docs.example", 443, 1))
    await asyncio.wait_for(ipv6_started.wait(), 1)
    assert calls == []
    release_ipv6.set()
    assert await task == "EXAMPLE_STREAM"
    assert calls == [("93.184.216.34", 443)]
    assert resolver.connections[0]["addresses"] == ["93.184.216.34", "2606:4700:4700::1111"]
    assert all(isinstance(row["resolver"], BootstrapResolver) for row in factories)
    assert all(row["proxy_settings"] is resolver.proxy_settings for row in factories)


@pytest.mark.parametrize("bad", ["private_v6", "empty", "failed_v6"])
async def test_incomplete_or_nonpublic_family_never_opens_target_or_falls_back(monkeypatch, bad):
    def respond(request):
        kind = int(request.url.params["type"])
        values = [] if bad == "empty" else ["93.184.216.34"] if kind == 1 else ["fd00::1"]
        response = answer(kind, values)
        if bad == "failed_v6" and kind == 28:
            response["Status"] = 2
        return httpx.Response(200, json=response)
    install_dns(monkeypatch, respond)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", no_system_dns)
    backend = PublicNetworkBackend(resolver=HTTPSJSONResolver(policy(), ProxySettings(False)))
    async def forbidden(*args, **kwargs):
        pytest.fail("A rejected address set must never reach a target socket")
    monkeypatch.setattr(backend.backend, "connect_tcp", forbidden)
    with pytest.raises((ResearchDestinationRejected, AA2Error)):
        await backend.connect_tcp("docs.example", 443, 1)


@pytest.mark.parametrize("value", ["93.184.216.34", "2606:4700:4700::1111", "127.0.0.1"])
async def test_numeric_target_avoids_resolver_requests_but_keeps_public_gate(monkeypatch, value):
    def forbidden(**kwargs):
        pytest.fail("IP literals must not generate DNS JSON queries")
    monkeypatch.setattr("auto_agents_2.network.PublicHTTPTransport", forbidden)
    resolver = HTTPSJSONResolver(policy(), ProxySettings(False))
    assert (await resolver.resolve(value, 443, 1)).addresses == (value,)
    backend = PublicNetworkBackend(resolver=resolver)
    calls = []
    async def dial(ip, *args):
        calls.append(ip)
        return "EXAMPLE_STREAM"
    monkeypatch.setattr(backend.backend, "connect_tcp", dial)
    if value == "127.0.0.1":
        with pytest.raises(ResearchDestinationRejected):
            await backend.connect_tcp(value, 443, 1)
        assert calls == []
    else:
        assert await backend.connect_tcp(value, 443, 1) == "EXAMPLE_STREAM"
        assert calls == [value]


@pytest.mark.parametrize("case", ["redirect", "content_type", "wire_limit", "decoded_limit", "http_error"])
async def test_resolver_response_is_bounded_and_redirects_never_followed(monkeypatch, case):
    import gzip

    requests = []
    def respond(request):
        requests.append(str(request.url))
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example/"})
        if case == "http_error":
            return httpx.Response(503, text="EXAMPLE_PRIVATE_ENDPOINT_PATH")
        if case == "wire_limit":
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b" " * (DNS_BODY_LIMIT + 1))
        if case == "decoded_limit":
            body = gzip.compress(b" " * (DNS_BODY_LIMIT + 1))
            return httpx.Response(200, headers={"content-type": "application/json", "content-encoding": "gzip"},
                                  stream=httpx.ByteStream(body))
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"not DNS")
    install_dns(monkeypatch, respond)
    with pytest.raises(AA2Error) as failure:
        await HTTPSJSONResolver(policy(), ProxySettings(False)).resolve("docs.example", 443, 1)
    assert all(value.startswith("https://resolver.example/resolve?") for value in requests)
    assert "EXAMPLE_PRIVATE_ENDPOINT_PATH" not in str(failure.value)


async def test_resolver_timeout_cancels_and_drains_both_families(monkeypatch):
    started, ended = [], []
    async def respond(request):
        kind = request.url.params["type"]
        started.append(kind)
        try:
            await asyncio.Future()
        finally:
            ended.append(kind)
    install_dns(monkeypatch, respond)
    with pytest.raises(TimeoutError):
        await HTTPSJSONResolver(policy(), ProxySettings(False)).resolve("docs.example", 443, 0.02)
    assert set(started) == {"1", "28"} == set(ended)


async def test_resolver_never_carries_cookies_between_queries(monkeypatch):
    cookies = []
    def respond(request):
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, json=answer(int(request.url.params["type"])),
                              headers={"set-cookie": "EXAMPLE_COOKIE=EXAMPLE_VALUE; Path=/"})
    install_dns(monkeypatch, respond)
    resolver = HTTPSJSONResolver(policy(), ProxySettings(False))
    await resolver.resolve("docs.example", 443, 1)
    await resolver.resolve("docs.example", 443, 1)
    assert cookies == [None] * 4


@pytest.mark.parametrize("proxied", [False, True])
async def test_bootstrap_pins_approved_ip_with_original_tls_and_host(monkeypatch, proxied):
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    data = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]
    if proxied:
        data.insert(0, b"HTTP/1.1 200 Connection established\r\n\r\n")
    stream = RecordingStream(data)
    lookups, connections = install_network(monkeypatch, stream)
    resolver = BootstrapResolver("resolver.example", ["8.8.8.8"])
    async with httpx.AsyncClient(transport=PublicHTTPTransport(resolver=resolver), trust_env=False) as client:
        assert (await client.get("https://resolver.example/resolve")).text == "ok"
    assert lookups == []
    expected = [("127.0.0.1", 7890)] if proxied else [("8.8.8.8", 443)]
    assert connections == expected
    written = b"".join(stream.writes)
    assert b"Host: resolver.example\r\n" in written
    assert stream.tls == [("resolver.example", ssl.CERT_REQUIRED, True)]
    if proxied:
        assert written.startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n")


async def test_bootstrap_refuses_changed_origin_or_mixed_private_set(monkeypatch):
    resolver = BootstrapResolver("resolver.example", ["8.8.8.8", "10.0.0.1"])
    with pytest.raises(AA2Error, match="hostname changed"):
        await resolver.resolve("other.example", 443, 1)
    backend = PublicNetworkBackend(resolver=resolver)
    async def forbidden(*args, **kwargs):
        pytest.fail("All bootstrap addresses must pass before connecting")
    monkeypatch.setattr(backend.backend, "connect_tcp", forbidden)
    with pytest.raises(ResearchDestinationRejected):
        await backend.connect_tcp("resolver.example", 443, 1)


async def test_opt_in_target_preserves_tls_host_and_revalidates_redirect_origin(monkeypatch):
    resolutions = []
    class Resolver:
        async def resolve(self, host, port, timeout):
            resolutions.append(host)
            return ResolvedAddresses(host, ("93.184.216.34",) if host == "docs.example" else ("10.0.0.1",), "fixture")
        def connected(self, *args):
            pass
    stream = RecordingStream([b"HTTP/1.1 302 Found\r\nLocation: https://other.example/\r\nContent-Length: 0\r\n\r\n"])
    lookups, connections = install_network(monkeypatch, stream)
    async with httpx.AsyncClient(transport=PublicHTTPTransport(resolver=Resolver()), trust_env=False,
                                 follow_redirects=True) as client:
        with pytest.raises(ResearchDestinationRejected):
            await client.get("https://docs.example/reference")
    assert resolutions == ["docs.example", "other.example"] and lookups == []
    assert connections == [("93.184.216.34", 443)]
    assert stream.tls == [("docs.example", ssl.CERT_REQUIRED, True)]
    assert b"Host: docs.example\r\n" in b"".join(stream.writes)


async def test_shared_acquisition_deadline_includes_resolution_and_http_body(tmp_path, monkeypatch):
    from auto_agents_2 import research
    from auto_agents_2.errors import DocumentAcquisitionTimeout

    started = asyncio.Event()
    class StalledResolver:
        def __init__(self, *args):
            pass
        async def resolve(self, *args):
            started.set()
            await asyncio.Future()
    monkeypatch.setattr("auto_agents_2.research_dns.HTTPSJSONResolver", StalledResolver)
    monkeypatch.setattr(research, "ACQUISITION_TIMEOUT", 0.02)
    artifacts = SimpleNamespace(store=SimpleNamespace(root=tmp_path))
    with pytest.raises(DocumentAcquisitionTimeout):
        await research.ResearchService(artifacts, resolver_config=policy()).capture("https://docs.example/reference")
    assert started.is_set()


async def test_new_capture_records_only_its_policy_and_does_not_relabel_old_result(tmp_path, monkeypatch):
    from auto_agents_2 import research

    observed, policies = [], []
    old = {"artifact": {"hash": "EXAMPLE_HASH"}, "excerpt": "fixture body"}
    saved = copy.deepcopy(old)
    class Resolver:
        def __init__(self, config, proxy):
            self.proxy = proxy
            policies.append(self)
        def evidence(self):
            return {"policy_hash": "EXAMPLE_POLICY", "scope": "new acquisition only"}
    def transport(**kwargs):
        observed.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, text="fixture body"))
    async def collect(*args, **kwargs):
        return old
    monkeypatch.setattr(research, "PublicHTTPTransport", transport)
    monkeypatch.setattr("auto_agents_2.research_dns.HTTPSJSONResolver", Resolver)
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    artifacts = SimpleNamespace(store=SimpleNamespace(root=tmp_path))
    disabled = await research.ResearchService(artifacts).capture("https://docs.example/reference")
    assert disabled is old and observed == [{}]
    enabled = await research.ResearchService(artifacts, resolver_config=policy()).capture("https://docs.example/reference")
    assert old == saved and "network_policy" not in old
    assert enabled["network_policy"]["policy_hash"] == "EXAMPLE_POLICY"
    assert observed[1]["resolver"] is policies[0]
    assert observed[1]["proxy_settings"] is policies[0].proxy


@pytest.mark.parametrize("enabled", [False, True])
def test_workflow_selects_resolver_for_research_only(monkeypatch, enabled):
    from auto_agents_2 import engine

    workflow = object.__new__(engine.WorkflowEngine)
    selected = config(policy() if enabled else None)
    workflow.config = SimpleNamespace(load=lambda: selected)
    workflow.artifacts = object()
    calls = []
    def service(*args, **kwargs):
        calls.append((args, kwargs))
        return "EXAMPLE_SERVICE"
    monkeypatch.setattr(engine, "ResearchService", service)
    assert workflow._research_service() == "EXAMPLE_SERVICE"
    assert calls == [((workflow.artifacts,), {"resolver_config": selected.research_resolver} if enabled else {})]
