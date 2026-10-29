"""Exact ranges in extracted research text; selection never rewrites the evidence."""
from __future__ import annotations

from urllib.parse import unquote

from .requirements import TextExtractor, decode_document
from .util import digest

SECTION_VERSION = 1
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
CONTAINERS = {"section", "article", "div", "p", "pre", "table", "dl", "li", "dd", "dt"}


class SectionExtractor(TextExtractor):
    def __init__(self):
        super().__init__()
        self.position = 0
        self.stack = []
        self.headings = []
        self.anchors = []

    def _advance(self, before):
        self.position += sum(map(len, self.parts[before:]))

    def handle_starttag(self, tag, attrs):
        start, before = self.position, len(self.parts)
        super().handle_starttag(tag, attrs)
        self._advance(before)
        if self.hidden or tag in {"script", "style"}:
            return
        attributes = dict(attrs)
        level = int(tag[1]) if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else 0
        heading = None
        if level:
            for old in self.headings:
                if old["end"] is None and old["level"] >= level:
                    old["end"] = start
            heading = {"start": start, "end": None, "title_end": None, "level": level,
                       "parents": [h["start"] for h in self.headings if h["end"] is None]}
            self.headings.append(heading)
        node = {"tag": tag, "start": start, "end": None, "heading": heading}
        for anchor in {attributes.get("id"), attributes.get("name") if tag == "a" else None} - {None, ""}:
            self.anchors.append({"anchor": anchor, "node": node,
                                 "heading": self.headings[-1] if self.headings else None})
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        hidden, before = self.hidden, len(self.parts)
        super().handle_endtag(tag)
        self._advance(before)
        if hidden or tag in {"script", "style"}:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                for node in self.stack[i:]:
                    node["end"] = self.position
                    if node["heading"]:
                        node["heading"]["title_end"] = self.position
                del self.stack[i:]
                break

    def handle_data(self, data):
        before = len(self.parts)
        super().handle_data(data)
        self._advance(before)

    def index(self):
        text = "".join(self.parts)
        headings = []
        for row in self.headings:
            headings.append({"start": row["start"], "end": row["end"] or len(text),
                             "level": row["level"], "parents": row["parents"],
                             "title": text[row["start"]:row["title_end"] or row["start"]].strip()})
        anchors = []
        for row in self.anchors:
            node, heading = row["node"], row["heading"]
            if node["heading"]:
                start, end = heading["start"], heading["end"] or len(text)
            elif node["tag"] in CONTAINERS and node["end"] is not None:
                start, end = node["start"], node["end"]
            elif heading:
                start, end = heading["start"], heading["end"] or len(text)
            else:
                continue
            anchors.append({"anchor": row["anchor"], "start": start, "end": end})
        return text, {"version": SECTION_VERSION, "characters": len(text), "text_hash": digest(text),
                      "headings": headings, "anchors": anchors}


def extract_sections(raw: bytes, mime: str, *, source_url: str = ""):
    text, encoding = decode_document(raw, mime)
    if mime.partition(";")[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
        return text, encoding, {"version": SECTION_VERSION, "characters": len(text), "text_hash": digest(text),
                                "headings": [], "anchors": []}
    parser = SectionExtractor()
    parser.feed(text)
    parser.close()
    visible, index = parser.index()
    if source_url:
        from .research_embedded import embedded_document
        embedded = embedded_document(text, source_url)
        if embedded:
            # Preserve visible caveats as well as the exact public Markdown export.
            # Existing HTML ranges stay bound to the visible portion; absent
            # Markdown anchors use the existing full-text/index fallback.
            start = len(visible) + len("\n\n[Public document Markdown export]\n")
            visible += "\n\n[Public document Markdown export]\n" + embedded["text"]
            index["embedded_documents"] = [{k: v for k, v in embedded.items() if k != "text"} |
                                           {"start": start, "end": len(visible)}]
            index.update(characters=len(visible), text_hash=digest(visible))
    return visible, encoding, index


def select_section(index, fragment):
    if not fragment:
        return {"status": "no_anchor"}
    matches = [r for r in index["anchors"] if r["anchor"] == unquote(fragment)]
    if len(matches) != 1:
        return {"status": "ambiguous_anchor" if matches else "missing_anchor", "anchor": fragment}
    selected = matches[0]
    return {"status": "selected", **selected,
            "headings": [h["title"] for h in index["headings"]
                         if h["start"] <= selected["start"] < h["end"]]}
