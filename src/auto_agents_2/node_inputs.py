"""Local npm resolutions are execution inputs, including transitive lock entries."""
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import AA2Error
from .util import digest
from .workspace import input_entry


def lock_inputs(workspace, lock):
    root = workspace.resolve()
    try:
        document = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AA2Error("Cannot read npm lock inputs") from exc
    if not isinstance(document, dict) or document.get("lockfileVersion", 1) not in {1, 2, 3}:
        raise AA2Error("Unsupported npm lock structure")
    selected = {}

    def local(value):
        url = urlsplit(value)
        if url.scheme == "file":
            if url.netloc not in {"", "localhost"} or url.query or url.fragment:
                raise AA2Error("Unsupported local npm lock URL")
            value = unquote(url.path)
        elif url.scheme:
            raise AA2Error("Unsupported npm link target")
        path = (root / value).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise AA2Error("Local npm lock resolution must exist inside the execution workspace")
        selected[path.relative_to(root).as_posix()] = digest(input_entry(path, root))

    def visit(node):
        if not isinstance(node, dict):
            raise AA2Error("Invalid npm lock package entry")
        if node.get("link") is True and not isinstance(node.get("resolved"), str):
            raise AA2Error("Npm link has no resolved target")
        for key in ("resolved", "version"):
            value = node.get(key)
            if isinstance(value, str) and urlsplit(value).scheme == "git+file":
                raise AA2Error("Local Git npm resolutions require an explicitly supported installer")
            if isinstance(value, str) and (urlsplit(value).scheme == "file" or value.startswith(("./", "../", "/"))
                    or key == "resolved" and (node.get("link") is True or not urlsplit(value).scheme)):
                local(value)
        dependencies = node.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise AA2Error("Invalid npm lock dependencies")
        # v1/v2 dependency records recurse; package declarations use strings.
        for child in dependencies.values():
            if isinstance(child, dict):
                visit(child)

    visit(document)
    packages = document.get("packages", {})
    if not isinstance(packages, dict):
        raise AA2Error("Invalid npm lock packages")
    for name, node in packages.items():
        if Path(name).is_absolute() or not (root / name).resolve().is_relative_to(root):
            raise AA2Error("Npm lock package path escapes the execution workspace")
        visit(node)
    return sorted(selected.items())
