from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

import httpx

from .artifacts import ArtifactStore
from .errors import AA2Error
from .models import SourceEvidence
from .network import PublicHTTPTransport, public_address
from .requirements import MAX_REQUIREMENTS_BYTES, extract_document
from .util import SECRET, now


def public_url(value: str):
    url = urlsplit(value)
    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
        raise AA2Error("Research evidence must use public HTTP(S) documentation URLs")
    if any(SECRET.search(k) for k in parse_qs(url.query)):
        raise AA2Error("Research URLs must not contain credentials")
    if url.hostname.rstrip(".").lower() == "localhost":
        raise AA2Error("Research destination must be public")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        return  # DNS addresses are checked again at connection time.
    if not public_address(str(address)):
        raise AA2Error("Research destination must be public")


class ResearchService:
    """Preserve the response and provenance; acquisition is not proof of a claim."""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    async def capture(self, url: str) -> dict:
        public_url(url)
        async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False, follow_redirects=False, timeout=30) as client:
            final = url
            for _ in range(6):
                public_url(final)
                async with client.stream("GET", final) as response:
                    if response.is_redirect and "location" in response.headers:
                        final = urljoin(final, response.headers["location"])
                        continue
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_REQUIREMENTS_BYTES:
                            raise AA2Error("Research response exceeds the supported acquisition size; no claim verified")
                    mime = response.headers.get("content-type", "")
                    break
            else:
                raise AA2Error("Research redirect limit exceeded")
        raw = self.artifacts.put(bytes(data), "research_response")
        text, encoding = extract_document(bytes(data), mime)
        extracted = self.artifacts.put(text, "research_extracted")
        metadata = {"source": url, "final_url": final, "mime_type": mime, "encoding": encoding,
                    "captured_at": now(), "extractor_version": "structured-text-v3",
                    "raw": raw, "artifact": extracted, "hash": extracted["hash"]}
        return {**metadata, "manifest": self.artifacts.put(metadata, "research_manifest")}

    async def verify(self, sources: list[SourceEvidence]) -> list[dict]:
        semaphore = asyncio.Semaphore(4)
        documents = {}

        async def fetch(url):
            async with semaphore:
                return await self.capture(url)

        for source in sources:
            public_url(source.url)
        for source in sources:
            url = urldefrag(source.url)[0]
            if url not in documents:
                documents[url] = asyncio.create_task(fetch(url))
        try:
            await asyncio.gather(*documents.values())
        except BaseException:
            for task in documents.values():
                task.cancel()
            await asyncio.gather(*documents.values(), return_exceptions=True)
            raise
        results = []
        for source in sources:
            evidence = documents[urldefrag(source.url)[0]].result()
            results.append({**source.model_dump(), "claimed_version": source.version,
                            "supported_version": None, "retrieval": evidence,
                            "excerpt": self.artifacts.read(evidence["artifact"]),
                            "verification_level": "document_retrieved; claim/version/first-party provenance require review; live behaviour requires tests"})
        return results


def request_documents(context: dict) -> dict:
    """One complete inline body per identical retrieval snapshot in this request.

    Claims, fragments, provenance and conflicting version claims stay on their own
    rows. No semantic merging, whitespace normalization or cross-request history.
    """
    import copy

    from .util import digest
    result = copy.deepcopy(context)
    bodies = {}
    visited = set()

    def visit(value):
        if isinstance(value, (dict, list)):
            if id(value) in visited:
                return
            visited.add(id(value))
        if isinstance(value, dict):
            retrieval, body = value.get("retrieval"), value.get("excerpt")
            if isinstance(retrieval, dict) and isinstance(body, str) and isinstance(retrieval.get("artifact"), dict):
                # The entire retrieval descriptor distinguishes versions and provenance.
                key = "research-" + digest([retrieval, body])
                rows = bodies.setdefault(key, [])
                rows.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    documents = []
    for key, rows in bodies.items():
        if len(rows) < 2:
            continue
        documents.append({"id": key, "body": rows[0]["excerpt"], "retrieval": rows[0]["retrieval"]})
        for row in rows:
            row.pop("excerpt")
            row["document_id"] = key
    if documents:
        if "research_document_bodies" in result:
            raise AA2Error("Research document table already exists in the input context")
        result["research_document_bodies"] = documents
    return result
