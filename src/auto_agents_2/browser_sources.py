"""Conservative download-source proofs, separate from browser payload identity."""
from __future__ import annotations

import re
from pathlib import Path

# These exact bases are Playwright's official mirror routes, not a domain-wide
# allowlist. Equivalence additionally requires a common complete artifact URL
# for every component emitted by the same, content-identified driver.
OFFICIAL_BASES = (
    "https://cdn.playwright.dev/dbazure/download/playwright",
    "https://playwright.download.prss.microsoft.com/dbazure/download/playwright",
    "https://cdn.playwright.dev",
)
HOST_KEYS = ("PLAYWRIGHT_DOWNLOAD_HOST", *(f"PLAYWRIGHT_{name}_DOWNLOAD_HOST"
             for name in ("CHROMIUM", "FIREFOX", "WEBKIT")))


def download_configuration(environment):
    """Preserve v1 hashes and include the driver's previously missed npm fallbacks."""
    result = {key: value for key, value in environment.items()
              if key.startswith("PLAYWRIGHT_") and key.endswith("DOWNLOAD_HOST")}
    fallbacks = {}
    for key in HOST_KEYS:
        if key in environment:
            continue  # Even an empty primary value suppresses npm fallbacks.
        for prefix in ("npm_config_", "npm_package_config_"):
            alias = prefix + key.lower()
            if alias in environment:
                fallbacks[key] = environment[alias]
                break
    if fallbacks:
        result["environment_fallbacks"] = fallbacks
    return result


def official_artifact(url):
    for base in OFFICIAL_BASES:
        if url.startswith(base + "/"):
            relative = url[len(base) + 1:]
            if (relative.startswith("builds/") and not any(c in relative for c in "?#\\")
                    and all(part not in {"", ".", ".."} for part in relative.split("/"))):
                return relative
    return None


def official_plan(output, root):
    """Unknown CLI formats or custom routes grant no cross-source reuse."""
    sections = re.split(r"(?m)^\s*Install location:\s*", output)[1:]
    result = []
    for section in sections:
        lines = section.splitlines()
        if not lines:
            return None
        path = Path(lines[0].strip())
        if path.parent != root or not re.fullmatch(r"[a-z][a-z0-9_-]*-\d+", path.name):
            return None
        urls = re.findall(r"(?m)^\s*Download (?:url|fallback \d+):\s*(\S+)\s*$", "\n".join(lines[1:]))
        relatives = {official_artifact(url) for url in urls}
        if not urls or None in relatives or len(relatives) != 1:
            return None
        result.append([path.name, relatives.pop(), sorted(set(urls))])
    return sorted(result) if result and len({row[0] for row in result}) == len(result) else None


def valid_plan(plan):
    if not isinstance(plan, list) or not plan:
        return False
    names = set()
    for row in plan:
        if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]*-\d+", row[0]) or row[0] in names
                or not isinstance(row[1], str) or not isinstance(row[2], list) or not row[2]
                or any(not isinstance(url, str) or official_artifact(url) != row[1] for url in row[2])):
            return False
        names.add(row[0])
    return True


def equivalent_plans(left, right):
    if not valid_plan(left) or not valid_plan(right):
        return False
    one, two = {row[0]: row[1:] for row in left}, {row[0]: row[1:] for row in right}
    return (one.keys() == two.keys() and all(one[name][0] == two[name][0]
            and set(one[name][1]) & set(two[name][1]) for name in one))
