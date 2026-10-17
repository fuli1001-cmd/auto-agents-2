from __future__ import annotations

import codecs
import re
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .artifacts import ArtifactStore
from .errors import AA2Error, PolicyViolation
from .http_body import ACQUISITION_TIMEOUT, acquisition_deadline, read_body
from .models import IntakeResult
from .util import contained

MAX_REQUIREMENTS_BYTES = 4 * 1024 * 1024
BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "tr", "table",
              "section", "article", "header", "footer", "main", "aside", "nav", "ul", "ol", "dl", "dt", "dd", "blockquote"}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")
        if tag == "a":
            self.links.append(dict(attrs).get("href") or "")
        anchor = dict(attrs).get("id")
        if anchor:
            self.parts.append(f" [#{anchor}] ")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in {"script", "style"}:
                self.hidden = max(0, self.hidden - 1)
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and self.links:
            self.parts.append(" (" + self.links.pop() + ") ")
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def decode_document(raw: bytes, content_type: str) -> tuple[str, str]:
    encoding = None
    for marks, codec in (((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE), "utf-32"),
                         ((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE), "utf-16"),
                         ((codecs.BOM_UTF8,), "utf-8-sig")):
        if raw.startswith(marks):
            encoding = codec
            break
    message = Message()
    message["content-type"] = content_type
    encoding = encoding or message.get_content_charset()
    if not encoding and "html" in content_type.lower():
        match = re.search(rb"<meta\b[^>]*charset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)", raw[:4096], re.I)
        encoding = match[1].decode("ascii") if match else None
    try:
        return raw.decode(encoding or "utf-8-sig"), encoding or "utf-8-sig"
    except (LookupError, UnicodeError) as exc:
        raise AA2Error("Document has unsupported or invalid text encoding") from exc


def decode_response(raw: bytes, content_type: str) -> str:
    return decode_document(raw, content_type)[0]


def extract_document(raw: bytes, content_type: str) -> tuple[str, str]:
    text, encoding = decode_document(raw, content_type)
    if content_type.partition(";")[0].strip().lower() in {"text/html", "application/xhtml+xml"}:
        parser = TextExtractor()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    return text, encoding


async def capture(source: str, artifacts: ArtifactStore) -> dict:
    if source.startswith(("https://", "http://")):
        label = f"Requirements document download for {urlsplit(source).hostname}"
        async with acquisition_deadline(ACQUISITION_TIMEOUT, label), httpx.AsyncClient(
                follow_redirects=True, timeout=30, headers={"Accept-Encoding": "gzip, deflate"}) as client:
            async with client.stream("GET", source) as response:
                response.raise_for_status()
                raw = await read_body(response)
                kind = response.headers.get("content-type", "")
        import base64
        request = {"action": "requirements", "source": source, "raw": base64.b64encode(raw).decode(), "mime": kind}
        paths = []
    else:
        file = Path(source).expanduser().absolute()
        if file.suffix.lower() not in {".md", ".txt", ".markdown"}:
            raise AA2Error("首期需求文件支持 Markdown、TXT 和文本网页")
        request = {"action": "local_requirements", "source": str(file)}
        paths = [file]
    from .collection import run_collection
    return await run_collection(artifacts.store.root, request, timeout=ACQUISITION_TIMEOUT, paths=paths)


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
                try:
                    exists = contained(workspace, path).is_file()
                except (PolicyViolation, OSError, ValueError):
                    exists = False
                if not exists:
                    problems.append(f"{item.requirement_id}: missing evidence {path}")
    return bool(result.coverage) and not problems and all(c.status == "satisfied" for c in result.coverage), problems
