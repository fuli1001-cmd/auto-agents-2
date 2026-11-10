"""Exact controller-owned inputs for one read-only review invocation.

Only already admitted public materials are read here. Controller state, unrelated
artifacts, native profiles and writable provider outputs are not input trees.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .faults import StateConflict
from .safe_files import directory_fd, regular_file, regular_files
from .util import digest


class ReviewMaterialChanged(StateConflict):
    code = "REVIEW_MATERIAL_CHANGED"

    def __init__(self, reason):
        super().__init__(reason, phase="review")
        self.reason_code = self.code

    def bind_failure(self, key, state):
        self.requirement.reason = state.get("material_failure_reason", self.requirement.reason)
        self.requirement.evidence = {"review_material_failure": {"key": key, "state": dict(state)}}
        self.args = (self.requirement.prompt(),)
        return self


def _entry(stream):
    info = os.fstat(stream.fileno())
    return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            "mode": stat.S_IMODE(info.st_mode), "bytes": info.st_size}


def _file(path):
    with directory_fd(path.parent) as parent, regular_file(parent, path.name) as stream:
        return _entry(stream)


def _tree(root):
    # Empty directories and directory timestamps/modes carry no admitted content.
    # Non-regular entries (including directory links) are rejected by regular_files.
    return {name: _entry(stream) for name, stream in regular_files(root)}


class ReviewMaterials:
    """Freeze each input at preparation; recheck before dispatch and acceptance."""

    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.files = {}
        self.projections = {}
        try:
            self.snapshot = _tree(self.workspace)
        except (OSError, ValueError) as exc:
            raise ReviewMaterialChanged("Review snapshot is unavailable or unsafe") from exc

    def admit_files(self, paths):
        try:
            self.files = {str(path): _file(path) for path in sorted(set(paths))}
        except (OSError, ValueError) as exc:
            raise ReviewMaterialChanged("Admitted review material is unavailable or unsafe") from exc

    def admit_projection(self, projected, destination):
        expected = {Path(path).relative_to(destination).as_posix(): entry
                    for path, entry in self.files.items() if Path(path).is_relative_to(destination)}
        self.projections[str(projected)] = expected
        self.verify()

    def record(self):
        value = {"version": 1, "workspace": str(self.workspace), "snapshot": self.snapshot,
                 "files": self.files, "projections": self.projections}
        return {**value, "hash": digest(value)}

    def verify(self):
        try:
            if (_tree(self.workspace) != self.snapshot
                    or any(_file(Path(path)) != entry for path, entry in self.files.items())
                    or any(_tree(Path(path)) != entries for path, entries in self.projections.items())):
                raise ReviewMaterialChanged("Review delivery materials changed; result cannot be accepted")
        except (OSError, ValueError) as exc:
            raise ReviewMaterialChanged("Review delivery materials are missing or unsafe; result cannot be accepted") from exc
