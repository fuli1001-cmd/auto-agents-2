"""Installation-time facts, retained with the immutable environment prefix."""
from __future__ import annotations

import json
import os

from packaging.utils import canonicalize_name

from .artifacts import ArtifactStore
from .errors import AA2Error
from .readview import sensitive_values
from .safe_files import read_regular
from .util import SECRET, atomic_write, digest, scrub_text, write_json

RECORD = "aa2-installation.json"
REPORT = "aa2-pip-install-report.json"
MAX_BYTES = 32 * 1024 * 1024


def report_document(raw):
    report = json.loads(raw)
    if (not isinstance(report, dict) or report.get("version") != "1"
            or not isinstance(report.get("pip_version"), str) or not report["pip_version"]
            or not isinstance(report.get("install"), list)
            or not isinstance(report.get("environment"), dict)
            or not all(isinstance(v, str) for v in report["environment"].values())):
        raise ValueError("Unsupported or incomplete pip installation report")
    names = set()
    for item in report["install"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid installed distribution record")
        metadata, download = item.get("metadata"), item.get("download_info")
        if (not isinstance(metadata, dict)
                or not all(isinstance(metadata.get(k), str) and metadata[k] for k in ("name", "version"))
                or not isinstance(download, dict) or not isinstance(download.get("url"), str) or not download["url"]
                or not all(isinstance(item.get(k), bool) for k in ("requested", "is_direct", "is_yanked"))):
            raise ValueError("Incomplete installed distribution identity")
        name = canonicalize_name(metadata["name"])
        if name in names:
            raise ValueError("Duplicate installed distribution identity")
        names.add(name)
    return report


def save_installation(manager, prefix, report_path, logs, *, workspace=None):
    """Called only after a successful real install, before readiness is published."""
    record = {"version": 1, "pip_invoked": report_path is not None, "exit_code": 0}
    try:
        if report_path is not None:
            report = report_document(read_regular(report_path, MAX_BYTES))
            secrets = [v for k, v in os.environ.items() if SECRET.search(k)]
            secrets += sensitive_values(workspace or manager.config.project, manager.config.load().sensitive_paths)

            def public(value):
                if isinstance(value, str):
                    return scrub_text(value, secrets)
                if isinstance(value, list):
                    return [public(v) for v in value]
                if isinstance(value, dict):
                    return {k: public(v) for k, v in value.items()}
                return value

            sanitized = public(report)
            raw = json.dumps(sanitized, ensure_ascii=False).encode()
            record["redacted"] = sanitized != report
            if len(raw) > MAX_BYTES:
                raise ValueError("Oversized sanitized installation report")
            report_document(raw)
            if len(logs) != 2 or any(ref.get("kind") != "dependency_log" for ref in logs):
                raise ValueError("Complete installer stdout/stderr unavailable")
            atomic_write(prefix / REPORT, raw)
            record["report_hash"] = digest(raw)
            record["logs"] = {}
            for stream, ref in zip(("stdout", "stderr"), logs, strict=True):
                data = ArtifactStore(manager.root).bytes(ref)
                if len(data) > MAX_BYTES or not ref.get("capture_complete", True):
                    raise ValueError("Incomplete or oversized installer transcript")
                sanitized_log = scrub_text(data.decode("utf-8"), secrets).encode()
                record["redacted"] |= sanitized_log != data
                data = sanitized_log
                if len(data) > MAX_BYTES:
                    raise ValueError("Oversized sanitized installer transcript")
                atomic_write(prefix / f"aa2-pip-{stream}.log", data)
                record["logs"][stream] = {"sha256": digest(data), "bytes": len(data)}
        write_json(prefix / RECORD, record)
        return digest((prefix / RECORD).read_bytes())
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AA2Error("Installation provenance capture failed; preserve the environment and installer diagnostics") from exc


def installation_record(prefix, expected_hash, python=None):
    if expected_hash is None:
        return {"status": "not_recorded", "reason": "Legacy prefix has no installation-time report or transcript. "
                "Installed metadata cannot establish historical archive identities or source-build inputs."}
    raw = read_regular(prefix / RECORD, MAX_BYTES)
    record = json.loads(raw)
    if (not isinstance(expected_hash, str) or digest(raw) != expected_hash
            or not isinstance(record, dict) or record.get("version") != 1
            or type(record.get("pip_invoked")) is not bool or record.get("exit_code") != 0):
        raise ValueError("Installation capture identity mismatch")
    result = {"status": "recorded", "record_hash": expected_hash, **record}
    if not record["pip_invoked"]:
        if set(record) != {"version", "pip_invoked", "exit_code"}:
            raise ValueError("Unexpected evidence for an uninvoked installer")
        result["coverage"] = "No pip installation was invoked; Conda artifacts are recorded by the explicit lock."
        return result
    if type(record.get("redacted")) is not bool:
        raise ValueError("Installation redaction metadata unavailable")
    report_raw = read_regular(prefix / REPORT, MAX_BYTES)
    if digest(report_raw) != record.get("report_hash"):
        raise ValueError("Installation report changed")
    report = report_document(report_raw)
    result["report"] = report
    result["transcript"] = {}
    for stream in ("stdout", "stderr"):
        data = read_regular(prefix / f"aa2-pip-{stream}.log", MAX_BYTES)
        expected = record["logs"][stream]
        if digest(data) != expected["sha256"] or len(data) != expected["bytes"]:
            raise ValueError("Installation transcript changed")
        result["transcript"][stream] = data.decode("utf-8")
    if python is not None:
        packages = {canonicalize_name(p["name"]): p["version"] for p in python["packages"]}
        for item in report["install"]:
            metadata = item["metadata"]
            if packages.get(canonicalize_name(metadata["name"])) != metadata["version"]:
                raise ValueError("Installation report does not match installed packages")
        markers = report["environment"]
        for marker, field in (("python_full_version", "python"), ("sys_platform", "platform"),
                              ("platform_machine", "machine")):
            if markers.get(marker) != python["runtime"].get(field):
                raise ValueError("Installation report runtime mismatch")
    result["coverage"] = ("Actual successful pip install report plus complete sanitized stdout/stderr. "
        "The report identifies distributions selected by that transaction, not already satisfied Conda packages. "
        "It is not a lock or compatibility approval. It does not inventory isolated build environments; "
        "source/build dependencies and build results must be assessed from the actual transcript. "
        "Missing archive hashes or build facts are unavailable, never inferred from installed versions.")
    return result


def has_installation_record(prefix):
    """Do not silently reuse a legacy prefix when new checks need install facts."""
    try:
        ready = json.loads(read_regular(prefix / "aa2-ready.json", MAX_BYTES))
        expected = ready.get("installation_hash")
        return expected is not None and installation_record(prefix, expected)["status"] == "recorded"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
