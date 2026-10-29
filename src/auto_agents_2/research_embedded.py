"""Read the current public document's Markdown export without running JavaScript.

Only the known doc-center JSON assignment and URL-bound document are admitted.
The result remains untrusted source evidence for independent research review.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

ASSIGNMENT = "window._ROUTER_DATA"
PAGE = "docs/(libid)/(docid$)/page"
SOURCE = ASSIGNMENT + ".loaderData." + PAGE + ".curDoc.MDContent"
HOSTS = {"docs.volcengine.com", "www.volcengine.com"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous document JSON")
        result[key] = value
    return result


def _non_json_constant(value):
    raise ValueError("Non-JSON constant in document")


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.candidates = []
        self.valid_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = []
            attributes = dict(attrs)
            self.valid_script = ("src" not in attributes and len(attrs) == len(attributes)
                                 and (attributes.get("type") or "").lower() in {"", "text/javascript", "application/javascript"})

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            body = "".join(self.current).strip()
            if body.startswith(ASSIGNMENT):
                self.candidates.append(body if self.valid_script else "")
            self.current = None


def embedded_document(html: str, source_url: str):
    """Return one exact, page-bound export or None; never execute or guess fields."""
    url = urlsplit(source_url)
    if (url.scheme != "https" or url.hostname not in HOSTS or url.username or url.password
            or url.port not in {None, 443}):
        return None
    parser = _Scripts()
    parser.feed(html)
    parser.close()
    if len(parser.candidates) != 1 or parser.current is not None:
        return None
    assignment = parser.candidates[0][len(ASSIGNMENT):].lstrip()
    if not assignment.startswith("="):
        return None
    try:
        state, end = json.JSONDecoder(object_pairs_hook=_unique_object,
                                      parse_constant=_non_json_constant).raw_decode(assignment[1:].lstrip())
        if assignment[1:].lstrip()[end:].strip() not in {"", ";"} or state.get("errors"):
            return None
        page = state["loaderData"][PAGE]
        doc = page["curDoc"]
        for field, current in (("LibraryID", "curLibId"), ("DocumentID", "curDocId")):
            if (type(doc[field]) is not int or type(page[current]) is not int
                    or doc[field] <= 0 or doc[field] != page[current]):
                return None
        for field, current in (("LibraryCode", "curLibCode"), ("DocumentCode", "curDocCode")):
            if not isinstance(doc[field], str) or not doc[field] or doc[field] != page[current]:
                return None
        library, document = page["curLibRouteKey"], page["curDocRouteKey"]
        if (library not in {doc["LibraryCode"], str(doc["LibraryID"])}
                or document not in {doc["DocumentCode"], str(doc["DocumentID"])}):
            return None
        path = unquote(url.path).rstrip("/")
        if path not in {f"/docs/{library}/{document}", f"/docs/{library}/doc/{document}"}:
            return None
        if doc.get("RedirectInfo") or not isinstance(doc["MDContent"], str) or not doc["MDContent"].strip():
            return None
        if not isinstance(doc["Title"], str) or not isinstance(doc["UpdatedTime"], str):
            return None
        return {"text": doc["MDContent"], "source": SOURCE, "title": doc["Title"],
                "document_id": doc["DocumentID"], "library_id": doc["LibraryID"],
                "updated_time": doc["UpdatedTime"]}
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None
