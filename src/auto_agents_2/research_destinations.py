"""Document-only destination admission, never a DNS/proxy or host-wide grant."""
from __future__ import annotations

import ipaddress
import re
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .errors import (
    ResearchDestinationPolicyRejected,
    ResearchDocumentAuthorizationRequired,
    ResearchSourceExcluded,
    ResearchURLRejected,
)
from .network import public_address
from .util import SECRET, digest

POLICY_VERSION = "research-document-destinations-v1"
METADATA_HOSTS = {"metadata.google.internal", "metadata.google", "instance-data.ec2.internal",
                  "metadata.azure.internal", "metadata.aliyun.com"}
METADATA_IPS = {"100.100.100.200", "168.63.129.16", "fd00:ec2::254"}
SENSITIVE_SERVICES = {22: "ssh", 23: "telnet", 389: "ldap", 445: "smb", 636: "ldaps",
    1433: "mssql", 1521: "oracle", 2375: "docker_http", 2376: "docker_tls", 3306: "mysql",
    3389: "rdp", 5432: "postgresql", 5900: "vnc", 6379: "redis", 9200: "elasticsearch_http",
    9300: "elasticsearch_transport", 11211: "memcached", 27017: "mongodb"}
SENSITIVE_PORTS = frozenset(SENSITIVE_SERVICES)
MANAGEMENT_ROOTS = {"admin", "manage", "management", "console"}
ACTION_TERMINALS = {"login", "logout", "signin", "signout", "authorize", "token",
                    "shutdown", "reboot", "restart", "reset"}
ACTION_VALUES = {"delete", "remove", "restart", "shutdown", "reboot", "reset", "exec", "execute",
                 "write", "update", "create", "set", "change", "logout"}
ACTION_KEYS = {"action", "command", "cmd", "operation", "op", "do"}


def hostname(value):
    return value.rstrip(".").lower().encode("idna").decode()


def special_host(host):
    return (host == "localhost" or host.endswith(".localhost")
            or any(host == name or host.endswith("." + name) for name in METADATA_HOSTS))


def special_address(value):
    address = ipaddress.ip_address(value)
    # Embedded forms must be inspected before IPv6's broad reserved category.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return special_address(str(address.ipv4_mapped))
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return special_address(str(ipaddress.IPv4Address(int(address) & 0xffffffff)))
        if address in ipaddress.ip_network("64:ff9b:1::/48"):
            return True
        if address.sixtofour and special_address(str(address.sixtofour)):
            return True
        if address.teredo and any(special_address(str(ip)) for ip in address.teredo):
            return True
    elif address in ipaddress.ip_network("0.0.0.0/8"):
        return True  # this-network targets are not ordinary private documents.
    return (str(address) in METADATA_IPS or address.is_loopback or address.is_link_local
            or address.is_unspecified or address.is_multicast or address.is_reserved
            or str(address) == "255.255.255.255")


def address_identities(value):
    address = ipaddress.ip_address(value)
    result = {str(address)}
    if isinstance(address, ipaddress.IPv6Address):
        embedded = (address.ipv4_mapped or address.sixtofour or
                    (ipaddress.IPv4Address(int(address) & 0xffffffff)
                     if address in ipaddress.ip_network("64:ff9b::/96") else None))
        if embedded is not None:
            result.update(address_identities(str(embedded)))
    return result


def document_path(value):
    """No ambiguous path can enlarge an explicitly authorized document subtree."""
    path = urlsplit(value).path if "://" in value else value
    if (not path.startswith("/") or chr(92) in path
            or re.search(r"%(?:2f|5c|25)", path, re.I)):
        raise ValueError("Document scope has an ambiguous path or encoded separator")
    decoded = unquote(path, errors="strict")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise ValueError("Document scope cannot contain dot traversal")
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise ValueError("Document scope cannot contain control characters")
    return decoded


def parse_document_url(value):
    try:
        original = urlsplit(value)
        url = httpx.URL(value)
        if (url.scheme not in {"http", "https"} or not url.host or url.username or url.password
                or "%" in url.host or url.port == 0 or original.username is not None or original.password is not None):
            raise ValueError
        host = hostname(url.host)
        if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:[.](?:0x[0-9a-f]+|[0-9]+)){0,3}", host, re.I):
            ipaddress.ip_address(host)  # Reject ambiguous numeric host spellings, not DNS names.
        path = document_path(original.path or "/")
        if any(SECRET.search(key) for key in parse_qs(original.query)):
            raise ValueError
        return url, host, path
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise ResearchURLRejected("", "invalid_document_url",
            "Research evidence must use unambiguous HTTP(S) document URLs without credentials or path traversal") from None


def origin(url):
    host = hostname(url.host)
    authority = "[" + host + "]" if ":" in host else host
    return f"{url.scheme}://{authority}:{url.port or (443 if url.scheme == 'https' else 80)}"


def management_path(path):
    segments = [segment.lower() for segment in path.split("/") if segment]
    return bool(segments and (segments[0] in MANAGEMENT_ROOTS or segments[-1] in ACTION_TERMINALS))


def public_document_action(path):
    # A narrow documentation spelling exception, never a private-target grant.
    segments = [part.lower() for part in path.split("/") if part]
    return (bool(segments) and segments[-1] in {"reset", "restart"}
            and any(part in {"docs", "reference"} for part in segments[:-1])
            and not any(part in MANAGEMENT_ROOTS | (ACTION_TERMINALS - {"reset", "restart"})
                        for part in segments[:-1]))


def normalize_document_authorization(value):
    try:
        url, host, _ = parse_document_url(value["origin"])
    except ResearchURLRejected as error:
        raise ValueError("Document authorization needs an unambiguous origin") from error
    if (url.path not in {"", "/"} or url.query or url.fragment or special_host(host)
            or (url.port or (443 if url.scheme == "https" else 80)) in SENSITIVE_PORTS):
        raise ValueError("Document authorization needs an ordinary exact HTTP(S) origin")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and special_address(str(address)):
        raise ValueError("Document authorization cannot override a special destination")
    prefix = document_path(value["path_prefix"])
    if prefix == "/" or management_path(prefix):
        raise ValueError("Document authorization needs a bounded document path, not a management target")
    return {"origin": origin(url), "path_prefix": prefix.rstrip("/")}


def matching_document_grant(failure, authorizations):
    diagnostic = failure.get("diagnostic", {})
    if (failure.get("code") != ResearchDocumentAuthorizationRequired.code
            or failure.get("classification") != "authorization_required"
            or diagnostic.get("operation") != "research_document_authorization"
            or diagnostic.get("resolution") != "authorization_required"
            or diagnostic.get("local_admission_only") is not True
            or diagnostic.get("target_request_executed") is not False
            or diagnostic.get("target_connection_dispatched") is not False
            or diagnostic.get("policy_version") != 1):
        return None
    scope = diagnostic.get("scope", {})
    if set(scope) != {"origin", "path"}:
        return None
    for value in authorizations or ():
        row = normalize_document_authorization(value.model_dump() if hasattr(value, "model_dump") else dict(value))
        if row["origin"] == scope["origin"] and (
                scope["path"] == row["path_prefix"] or scope["path"].startswith(row["path_prefix"] + "/")):
            return row
    return None


class NoSourceCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def source_cookie_jar():
    return CookieJar(policy=NoSourceCookies())


class ResearchDestinationPolicy:
    """One capture's frozen document grants and exact request/connection evidence."""
    def __init__(self, source, authorizations=()):
        self.source = source
        self.authorizations = tuple(normalize_document_authorization(
            row.model_dump() if hasattr(row, "model_dump") else dict(row)) for row in authorizations)
        self.current = None
        self.crossed_origin = False
        self.addresses = {}
        self.connections = []
        self.before_request(source)

    def permitted_document(self, url, path):
        target = origin(url)
        return any(row["origin"] == target and (
            path == row["path_prefix"] or path.startswith(row["path_prefix"] + "/"))
            for row in self.authorizations)

    def admit_recorded_denials(self, denied_hosts, denied_addresses):
        url, host, path = parse_document_url(self.current)
        if any(host == name or host.endswith("." + name) for name in denied_hosts):
            raise ResearchSourceExcluded(host)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return  # Independent named sources keep their exact host/SNI identity.
        blocked = set()
        for value in denied_addresses:
            blocked.update(address_identities(value))
        if not self.permitted_document(url, path) or address_identities(str(address)) & blocked:
            raise ResearchSourceExcluded(host)

    @staticmethod
    def reject(host, reason):
        raise ResearchDestinationPolicyRejected(host, reason)

    def before_request(self, value, *, headers=()):
        url, host, path = parse_document_url(value)
        if {"authorization", "proxy-authorization", "cookie"} & {str(key).lower() for key in headers}:
            self.reject(host, "origin_credentials_not_allowed")
        port = url.port or (443 if url.scheme == "https" else 80)
        if special_host(host):
            self.reject(host, "special_host")
        if port in SENSITIVE_PORTS:
            self.reject(host, "sensitive_service")
        if (management_path(path) and not public_document_action(path)
                or any(key.lower() in ACTION_KEYS and any(item.lower() in ACTION_VALUES for item in values)
                       for key, values in parse_qs(url.query.decode()).items())):
            self.reject(host, "authentication_or_management_action")
        if self.current is not None:
            old = httpx.URL(self.current)
            if old.scheme == "https" and url.scheme != "https":
                self.reject(host, "https_downgrade")
            self.crossed_origin = self.crossed_origin or origin(old) != origin(url)
        if public_document_action(path) and any(
                binding.get("selected_ip") and not public_address(binding["selected_ip"])
                for binding in self.connections
                if binding["hostname"] == host and binding["port"] == port):
            self.reject(host, "document_action_requires_public_connection")
        explicit = self.permitted_document(url, path)
        if (host.endswith((".local", ".internal", ".lan", ".home", ".localdomain")) or "." not in host) and not explicit:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise ResearchDocumentAuthorizationRequired(host, origin(url), path, "internal_document")
        self.current = str(url)
        try:
            literal = str(ipaddress.ip_address(host))
        except ValueError:
            literal = None
        if literal is not None:
            self.admit_addresses(host, port, (literal,), "literal", record=False)
        elif (host, port) in self.addresses:
            # A reused socket does not bypass document path/redirect authority.
            self.admit_addresses(host, port, self.addresses[(host, port)], "existing_connection", record=False)

    def before_connect(self, host, port):
        url = httpx.URL(self.current)
        if hostname(host) != hostname(url.host) or port != (url.port or (443 if url.scheme == "https" else 80)):
            self.reject(hostname(host), "unexpected_connection_origin")

    def admit_addresses(self, host, port, targets, source, *, record=True):
        self.before_connect(host, port)
        url, current_host, path = parse_document_url(self.current)
        try:
            values = tuple(str(ipaddress.ip_address(value)) for value in targets)
        except ValueError:
            self.reject(current_host, "invalid_address")
        if not values or any(special_address(value) for value in values):
            self.reject(current_host, "special_address")
        try:
            literal = str(ipaddress.ip_address(current_host))
        except ValueError:
            literal = None
        if literal is not None and any(value != literal for value in values):
            self.reject(current_host, "literal_address_mismatch")
        public = all(public_address(value) for value in values)
        if public_document_action(path) and not public:
            self.reject(current_host, "document_action_requires_public_connection")
        explicit = self.permitted_document(url, path)
        if not public:
            try:
                ipaddress.ip_address(current_host)
                literal = True
            except ValueError:
                literal = False
            if (not explicit and (url.scheme != "https" or port != 443 or self.crossed_origin or literal)):
                raise ResearchDocumentAuthorizationRequired(current_host, origin(url), path, "private_document")
        if record:
            self.addresses[(current_host, port)] = values
            self.connections.append({"hostname": current_host, "port": port, "addresses": list(values),
                "resolution_source": source, "classification": "public" if public else "unknown",
                "mapping_status": "not established", "explicit_document_authorization": explicit,
                "crossed_origin": self.crossed_origin, "document_url_hash": digest(self.current)})

    def connected(self, host, port, ip):
        if self.connections and self.connections[-1]["hostname"] == hostname(host) and self.connections[-1]["port"] == port:
            self.connections[-1]["selected_ip"] = ip

    def evidence(self):
        policy = {"version": POLICY_VERSION, "authorizations": list(self.authorizations),
                  "unknown_default": "Named HTTPS documents on port 443 with strict original-host TLS",
                  "cookies": "neither stored nor sent", "origin_authentication": "none",
                  "mapping": "Never inferred from an IP range or successful TLS"}
        return {"policy": policy, "policy_hash": digest(policy), "tcp_bindings": self.connections,
                "scope": "Document acquisition only; no source approval or permission to retry a denied operation"}
