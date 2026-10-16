"""Shared validation of durable process identity and filesystem ownership."""
import json
from pathlib import Path

from .errors import PolicyViolation


def read_record(path):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError()
        if data.get("version") == 3:
            if data.get("status") not in {"starting", "running", "cleaned"}:
                raise ValueError()
            paths = data.get("paths")
            if not isinstance(paths, list) or not paths or not all(valid_path(value) for value in paths):
                raise ValueError()
            if not valid_path(data.get("cwd")):
                raise ValueError()
            if data["status"] != "starting":
                require_identity(data)
        elif data.get("version", 2) != 2:
            raise ValueError()
        else:
            require_identity(data)
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise PolicyViolation("Invalid process ownership record; preserve resources for reconciliation") from exc


def valid_path(value):
    return isinstance(value, str) and bool(value) and "\x00" not in value and Path(value).is_absolute() and ".." not in Path(value).parts


def require_identity(data):
    if (type(data.get("pid")) is not int or data["pid"] <= 0
            or not isinstance(data.get("start"), str) or not data["start"].isascii()
            or not data["start"].isdigit()):
        raise PolicyViolation("Recorded process identity is unknown; preserving recovery record")
