"""Publish a complete dependency resolution with one atomic authority record."""
import json

from .errors import AA2Error
from .util import digest, write_json


def load_resolution(records):
    manifest = records / "resolution.json"
    try:
        if manifest.exists():
            data = json.loads(manifest.read_text())
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not all(isinstance(data.get(k), str) for k in ("pip", "conda", "hash"))
                    or data["hash"] != digest([data["pip"], data["conda"]])):
                raise ValueError("Invalid resolution generation")
            validate(data["pip"], data["conda"])
            return data
        pip, conda = records / "pip.lock", records / "conda.lock"
        if pip.is_file() and conda.is_file():
            # A complete legacy pair is migrated without modifying either source.
            return save_resolution(records, pip.read_text(), conda.read_text())
        if (records / "instance.json").exists():
            raise ValueError("Completed legacy environment lost its resolution pair")
        # An incomplete pre-publication legacy fragment never constrains a new solve.
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise AA2Error("Saved environment resolution is damaged; preserve records and reconcile") from exc


def validate(pip, conda):
    if (not isinstance(pip, str) or not isinstance(conda, str)
            or "@EXPLICIT" not in conda.splitlines() or "\x00" in pip + conda):
        raise ValueError("Incomplete environment resolution")


def save_resolution(records, pip, conda):
    try:
        validate(pip, conda)
    except ValueError as exc:
        raise AA2Error("Incomplete environment resolution") from exc
    data = {"version": 1, "pip": pip, "conda": conda, "hash": digest([pip, conda])}
    write_json(records / "resolution.json", data)
    return data
