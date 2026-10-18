"""References to supplied controller data; reference existence is not acceptance."""
import re


def supplied_references(value, *, prefixes=("",)):
    refs = set()

    def visit(item, path):
        if path:
            refs.update(prefix + path for prefix in prefixes)
        if isinstance(item, dict):
            if isinstance(item.get("hash"), str) and re.fullmatch(r"[a-f0-9]{64}", item["hash"]):
                refs.add(item["hash"])
            for key, child in item.items():
                if key not in {"proposal", "recovery_request", "original_proposal"}:
                    visit(child, f"{path}.{key}" if path else key)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, "")
    return refs
