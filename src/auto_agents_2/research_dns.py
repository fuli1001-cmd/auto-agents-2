"""Opt-in Google-compatible HTTPS DNS JSON, not RFC 8484 DNS wire format."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from .errors import AA2Error, ResearchResolverUnavailable
from .http_body import read_body
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

    async def resolve(self, host, port, timeout):
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return ResolvedAddresses(host, (str(literal),), "literal")
        hostname = dns_name(host)
        from .network import PublicHTTPTransport
        endpoint = httpx.URL(self.endpoint)
        transport = PublicHTTPTransport(resolver=BootstrapResolver(endpoint.raw_host.decode("ascii"), self.bootstrap_ips),
                                        proxy_settings=self.proxy_settings)
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=timeout,
                    cookies=CookieJar(policy=NoResolverCookies()),
                    headers={"Accept": "application/dns-json, application/json", "Accept-Encoding": "identity"}) as client:
                async def query(kind):
                    async with client.stream("GET", self.endpoint, params={"name": hostname, "type": str(kind), "cd": "false"}) as response:
                        if response.is_redirect:
                            raise AA2Error("Research resolver redirects are not permitted")
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type not in {"application/json", "application/dns-json"}:
                            raise AA2Error("Research resolver did not return supported DNS JSON")
                        return parse_dns_json(await read_body(response, DNS_BODY_LIMIT), hostname, kind)

                queries = [asyncio.create_task(query(kind)) for kind in (1, 28)]
                try:
                    ipv4, ipv6 = await asyncio.gather(*queries)
                except BaseException:
                    for task in queries:
                        task.cancel()
                    await asyncio.gather(*queries, return_exceptions=True)
                    raise
        except httpx.HTTPError as error:
            category = next(label for kind, label in (
                (httpx.HTTPStatusError, "http_status"), (httpx.ProxyError, "proxy"),
                (httpx.TimeoutException, "timeout"), (httpx.NetworkError, "network"),
                (httpx.ProtocolError, "protocol"), (httpx.HTTPError, "http")) if isinstance(error, kind))
            # Fixed HTTPX names only: never persist the exception, request, text or response body.
            names = ("HTTPStatusError", "ProxyError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
                     "PoolTimeout", "ConnectError", "ReadError", "WriteError", "CloseError",
                     "LocalProtocolError", "RemoteProtocolError", "UnsupportedProtocol", "DecodingError",
                     "TooManyRedirects", "TimeoutException", "NetworkError", "ProtocolError", "HTTPError")
            error_type = next(name for name in names if isinstance(error, getattr(httpx, name)))
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if type(status) is not int or not 100 <= status <= 599:
                status = None
            raise ResearchResolverUnavailable(category=category, error_type=error_type,
                endpoint_origin=str(endpoint.copy_with(path="/", query=None)),
                endpoint_url_hash=digest(self.endpoint), status_code=status) from None
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
