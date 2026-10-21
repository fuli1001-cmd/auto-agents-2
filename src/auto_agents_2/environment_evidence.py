"""Archive installed dependency facts for every consumer of a check observation."""
from __future__ import annotations

import json
import os

from .artifacts import ArtifactStore
from .errors import AA2Error
from .readview import sensitive_values
from .resolution import validate
from .safe_files import read_regular
from .util import SECRET, digest, scrub_text

# Run only the selected interpreter's standard library, without importing project
# packages. importlib.metadata also understands legacy/editable distribution metadata.
PYTHON_METADATA = """
import importlib.metadata as metadata
import json
import platform
import sys
packages = []
for dist in metadata.distributions():
    item = dist.metadata
    packages.append({"name": item["Name"], "version": dist.version,
        "requires_python": item.get("Requires-Python", ""),
        "requires_dist": dist.requires or [], "provides_extra": item.get_all("Provides-Extra") or []})
print(json.dumps({"prefix": sys.prefix, "runtime": {"python": platform.python_version(),
    "implementation": platform.python_implementation(), "platform": sys.platform,
    "machine": platform.machine()},
    "packages": sorted(packages, key=lambda item: (str(item["name"]).casefold(), item["version"]))}))
"""
MAX_RECORD_BYTES = 32 * 1024 * 1024


def read(path):
    return read_regular(path, MAX_RECORD_BYTES).decode("utf-8")


async def capture_resolution(manager, workspace, environment):
    """Capture facts from an existing ready environment; never resolve or install."""
    prefix = environment.prefix
    try:
        ready = json.loads(read(prefix / "aa2-ready.json"))
        pip, conda = read(prefix / "aa2-pip.lock"), read(prefix / "aa2-conda.lock")
        validate(pip, conda)
        if (digest(pip) != ready["pip_hash"] or digest(conda) != ready["conda_hash"]
                or manager.installed_identity(prefix) != environment.fingerprint):
            raise ValueError("Resolution identity mismatch")
        conda_packages = []
        for path in sorted((prefix / "conda-meta").glob("*.json")):
            item = json.loads(read(path))
            package = {key: item[key] for key in ("name", "version", "build")}
            if not all(isinstance(v, str) and v for v in package.values()):
                raise ValueError("Invalid Conda package metadata")
            for key in ("depends", "constrains"):
                package[key] = item.get(key, [])
                if not isinstance(package[key], list) or not all(isinstance(v, str) for v in package[key]):
                    raise ValueError("Invalid Conda dependency metadata")
            conda_packages.append(package)
        if not conda_packages:
            raise ValueError("Conda package metadata unavailable")
        with manager.resources.temporary("environment-evidence") as scratch:
            env = environment.variables(scratch)
            raw = await manager._command([str(prefix / "bin/python"), "-I", "-c", PYTHON_METADATA],
                workspace, env, [scratch], [prefix], timeout=60, machine=True)
        python = json.loads(raw)
        if not isinstance(python, dict) or python.pop("prefix", None) != str(prefix):
            raise ValueError("Metadata interpreter identity mismatch")
        if not isinstance(python, dict) or not isinstance(python.get("packages"), list) or not python["packages"]:
            raise ValueError("Python package metadata unavailable")
        for package in python["packages"]:
            if (not all(isinstance(package.get(k), str) for k in ("name", "version", "requires_python"))
                    or not package["name"] or not package["version"]
                    or not all(isinstance(package.get(k), list) and all(isinstance(v, str) for v in package[k])
                               for k in ("requires_dist", "provides_extra"))):
                raise ValueError("Invalid Python dependency metadata")
        if not manager.valid(prefix, environment.fingerprint):
            raise ValueError("Installed environment changed during evidence capture")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AA2Error("Environment resolution evidence unavailable or inconsistent; preserve the installed environment") from exc
    secrets = [v for k, v in os.environ.items() if SECRET.search(k)]
    secrets += sensitive_values(workspace, manager.config.load().sensitive_paths)

    def public(value):
        if isinstance(value, str):
            return scrub_text(value, secrets)
        if isinstance(value, list):
            return [public(v) for v in value]
        if isinstance(value, dict):
            return {k: public(v) for k, v in value.items()}
        return value

    facts = {"pip_lock": pip, "conda_explicit_lock": conda,
             "python": python, "conda_packages": conda_packages}
    sanitized = public(facts)
    return ArtifactStore(manager.root).put({"version": 1,
        "environment_hash": environment.fingerprint, "input_fingerprint": ready["fingerprint"],
        "resolution_hash": digest([pip, conda]), "pip_hash": digest(pip), "conda_hash": digest(conda),
        "redacted": sanitized != facts,
        "provenance": "Installed metadata and complete locks from the checked environment. "
            "Requires-Dist and Conda depends/constrains preserve conditional dependency edges; "
            "root requirements and constraints are declared in the workspace manifests. "
            "Hashes identify the original private locks; exposed bodies are credential-scrubbed. "
            "These facts are not a compatibility approval.",
        **sanitized}, "environment_resolution")
