from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import httpx

from .artifacts import ArtifactStore
from .errors import AA2Error
from .models import IntakeResult
from .util import contained, digest, now

MAX_REQUIREMENTS_BYTES = 4 * 1024 * 1024


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


async def capture(source: str, artifacts: ArtifactStore) -> dict:
    if source.startswith(("https://", "http://")):
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", source) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_REQUIREMENTS_BYTES:
                        raise AA2Error("Requirements document exceeds 4 MiB")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                kind = response.headers.get("content-type", "")
        text = raw.decode("utf-8-sig")
        if "html" in kind:
            parser = TextExtractor()
            parser.feed(text)
            text = "".join(parser.parts)
    else:
        file = Path(source).expanduser().resolve()
        if file.suffix.lower() not in {".md", ".txt", ".markdown"}:
            raise AA2Error("首期需求文件支持 Markdown、TXT 和文本网页")
        if file.stat().st_size > MAX_REQUIREMENTS_BYTES:
            raise AA2Error("Requirements document exceeds 4 MiB")
        text = file.read_text(encoding="utf-8-sig")
        source = str(file)
    if not text.strip():
        raise AA2Error("需求文档为空")
    return {"source": source, "captured_at": now(), "hash": digest(text),
            "artifact": artifacts.put(text, "raw_requirements")}


def validate_coverage(result: IntakeResult, workspace: Path) -> tuple[bool, list[str]]:
    ids = [r.id for r in result.requirements]
    mapped = [c.requirement_id for c in result.coverage]
    problems = []
    if not ids or len(ids) != len(set(ids)) or len(mapped) != len(set(mapped)) or set(ids) != set(mapped):
        return False, ["Coverage must map every requirement exactly once"]
    for item in result.coverage:
        if item.status == "satisfied":
            if not item.rationale.strip() or not item.evidence_paths:
                problems.append(f"{item.requirement_id}: no current implementation evidence")
            for path in item.evidence_paths:
                if not contained(workspace, path).is_file():
                    problems.append(f"{item.requirement_id}: missing evidence {path}")
    return bool(result.coverage) and not problems and all(c.status == "satisfied" for c in result.coverage), problems
