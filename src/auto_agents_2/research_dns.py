"""Opt-in Google-compatible HTTPS DNS JSON, not RFC 8484 DNS wire format."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from .errors import AA2Error, ResearchResolverUnavailable, http_failure_details
from .http_body import read_body
from .research_batch import ResearchRequestNotExecuted, current_batch, current_capture, current_http_failure
from .util import digest

DNS_BODY_LIMIT = 64 * 1024
DNS_ANSWER_LIMIT = 256
DNS_CNAME_LIMIT = 8


def dns_name(value):
    if not isinstance(value, str):
        raise AA2Error("Invalid research resolver DNS name")
    value = value.lower().removesuffix(".")
    if (not 1 <= len(value) <= 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                         for label in value.split("."))):
        raise AA2Error("Invalid research resolver DNS name")
    return value


def parse_dns_json(body, hostname, record_type):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        response = json.loads(body, object_pairs_hook=unique)
        question = response["Question"]
        if (type(response["Status"]) is not int or response["Status"] != 0 or response["TC"] is not False
                or not isinstance(question, list) or len(question) != 1
                or dns_name(question[0]["name"]) != hostname
                or type(question[0]["type"]) is not int or question[0]["type"] != record_type):
            raise ValueError("unmatched or incomplete DNS answer")
        answers = response.get("Answer", [])
        if not isinstance(answers, list) or len(answers) > DNS_ANSWER_LIMIT:
            raise ValueError("invalid DNS answers")
        aliases, addresses = {}, []
        for answer in answers:
            kind = answer["type"]
            if type(kind) is not int:
                raise ValueError("invalid record type")
            if kind == 5:
                owner, target = dns_name(answer["name"]), dns_name(answer["data"])
                if owner in aliases and aliases[owner] != target:
                    raise ValueError("ambiguous alias")
                aliases[owner] = target
            elif kind in (1, 28):
                data = answer["data"]
                if not isinstance(data, str) or "%" in data:
                    raise ValueError("invalid address string")
                address = ipaddress.ip_address(data)
                if kind != record_type or address.version != (4 if kind == 1 else 6):
                    raise ValueError("unexpected address family")
                addresses.append((dns_name(answer["name"]), str(address)))
            elif kind not in (46, 47, 48, 50):
                raise ValueError("unsupported answer record")
        chain, current = set(), hostname
        for _ in range(DNS_CNAME_LIMIT + 1):
            if current in chain:
                raise ValueError("CNAME loop")
            chain.add(current)
            if current not in aliases:
                break
            current = aliases[current]
        else:
            raise ValueError("CNAME chain too long")
        if set(aliases) - chain or any(owner != current for owner, _ in addresses):
            raise ValueError("unrelated answer owner")
        return tuple(dict.fromkeys(address for _, address in addresses))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        raise AA2Error("Research resolver returned an invalid or incomplete DNS JSON answer") from None


@dataclass(frozen=True)
class ResolvedAddresses:
    hostname: str
    addresses: tuple[str, ...]
    source: str


class BootstrapResolver:
    def __init__(self, hostname, addresses):
        self.hostname, self.addresses = hostname, tuple(addresses)

    async def resolve(self, host, port, timeout):
        if host != self.hostname:
            raise AA2Error("Research resolver bootstrap hostname changed")
        return ResolvedAddresses(host, self.addresses, "approved_bootstrap")

    def connected(self, resolution, ip, port):
        pass


class NoResolverCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class HTTPSJSONResolver:
    def __init__(self, config, proxy_settings):
        self.endpoint = config.endpoint
        self.bootstrap_ips = tuple(config.bootstrap_ips)
        self.proxy_settings = proxy_settings
        self.connections = []
        self.observed_failures = []

    async def resolve(self, host, port, timeout):
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return ResolvedAddresses(host, (str(literal),), "literal")
        hostname = dns_name(host)
        batch = current_batch.get()
        if batch is not None:
            batch.before_resolver_request(self.endpoint)
        from .network import PublicHTTPTransport
        endpoint = httpx.URL(self.endpoint)
        transport = PublicHTTPTransport(resolver=BootstrapResolver(endpoint.raw_host.decode("ascii"), self.bootstrap_ips),
                                        proxy_settings=self.proxy_settings)
        observed = []

        def unavailable(error):
            failure = ResearchResolverUnavailable(endpoint_origin=str(endpoint.copy_with(path="/", query=None)),
                endpoint_url_hash=digest(self.endpoint), **http_failure_details(error))
            history = current_capture.get()
            if history is not None:
                failure = history.record(failure)
            if all(row is not failure for row in observed):
                observed.append(failure)
                self.observed_failures.append(failure)
            if batch is not None:
                batch.observe_resolver(failure, self.endpoint)
            return failure

        deadline = asyncio.timeout(timeout)
        try:
            async with deadline, httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=timeout,
                    cookies=CookieJar(policy=NoResolverCookies()),
                    headers={"Accept": "application/dns-json", "Accept-Encoding": "identity"}) as client:
                async def query(kind):
                    token = current_http_failure.set(unavailable)
                    try:
                        if batch is not None:
                            batch.before_resolver_request(self.endpoint)
                        try:
                            async with client.stream("GET", self.endpoint, params={"name": hostname, "type": str(kind), "cd": "false"}) as response:
                                try:
                                    if response.is_redirect:
                                        raise AA2Error("Research resolver redirects are not permitted")
                                    response.raise_for_status()
                                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                                    if content_type not in {"application/json", "application/dns-json"}:
                                        raise AA2Error("Research resolver did not return supported DNS JSON")
                                    return parse_dns_json(await read_body(response, DNS_BODY_LIMIT), hostname, kind)
                                except httpx.HTTPError as error:
                                    # Freeze a refusal before stream/client cleanup
                                    # can hide it or another request can be dispatched.
                                    raise unavailable(error) from None
                        except httpx.HTTPError as error:
                            raise unavailable(error) from None

                    finally:
                        current_http_failure.reset(token)
                queries = [asyncio.create_task(query(kind)) for kind in (1, 28)]
                try:
                    ipv4, ipv6 = await asyncio.gather(*queries)
                except BaseException:
                    for task in queries:
                        task.cancel()
                    await asyncio.gather(*queries, return_exceptions=True)
                    raise
        except Exception as error:
            from .research_acquisition import failure_observation
            failures = [(failure, failure_observation(failure)) for failure in observed]
            selected = next((failure for failure, facts in failures
                             if facts["classification"] in {"security", "permission"}),
                            next((failure for failure, facts in failures if not facts["automatic"]), None))
            replaceable = isinstance(error, (ResearchResolverUnavailable, ResearchRequestNotExecuted, httpx.HTTPError))
            owned_timeout = isinstance(error, TimeoutError) and deadline.expired()
            if selected is not None and (replaceable or owned_timeout):
                if error is not selected and isinstance(error, (TimeoutError, httpx.HTTPError)):
                    selected.evidence["cleanup_failure"] = (
                        http_failure_details(error) if isinstance(error, httpx.HTTPError) else {"category": "unknown"})
                raise selected from None
            if isinstance(error, httpx.HTTPError):
                raise unavailable(error) from None
            raise
        return ResolvedAddresses(host, tuple(dict.fromkeys((*ipv4, *ipv6))), "https_json")

    def connected(self, resolution, ip, port):
        self.connections.append({"hostname": resolution.hostname, "addresses": list(resolution.addresses),
                                 "selected_ip": ip, "port": port, "resolution_source": resolution.source})

    def evidence(self):
        endpoint = httpx.URL(self.endpoint)
        policy = {"mode": "https_json", "protocol": "google-compatible-dns-json",
                  "endpoint_origin": str(endpoint.copy_with(path="/", query=None)),
                  "endpoint_url_hash": digest(self.endpoint), "bootstrap_ips": list(self.bootstrap_ips)}
        return {"policy": policy, "policy_hash": digest(policy), "tcp_bindings": self.connections,
                "scope": "new acquisition only; not DNSSEC proof or content/source approval"}
