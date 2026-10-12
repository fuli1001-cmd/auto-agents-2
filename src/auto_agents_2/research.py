from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from .artifacts import ArtifactStore
from .errors import AA2Error
from .models import SourceEvidence
from .requirements import capture
from .util import SECRET


class ResearchService:
    """Archive documents actually retrieved by the controller, separately from agent claims."""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    async def verify(self, sources: list[SourceEvidence]) -> list[dict]:
        semaphore = asyncio.Semaphore(4)

        async def fetch(source):
            url = urlsplit(source.url)
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
                raise AA2Error("Research evidence must use public HTTP(S) documentation URLs")
            if any(SECRET.search(k) for k in parse_qs(url.query)):
                raise AA2Error("Research URLs must not contain credentials")
            async with semaphore:
                evidence = await capture(source.url, self.artifacts)
            text = self.artifacts.read(evidence["artifact"])
            return {**source.model_dump(), "retrieval": evidence, "excerpt": text[:12000],
                    "verification_level": "document_retrieved; live API behaviour requires separate tests"}

        return await asyncio.gather(*(fetch(source) for source in sources))
