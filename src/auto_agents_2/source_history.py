"""Public source comparisons from controller-recorded Git snapshots, never native Git history."""
from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path

from .errors import AA2Error
from .faults import StateConflict
from .readview import public_files, sensitive
from .util import digest
from .workspace import EXCLUDED, PROTECTED_NAMES, git, git_environment, path_allowed, tree_hash


class SourceHistory:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _anchors(self, rid):
        anchors = {}
        baseline = self.store.get(rid, "baseline")
        if isinstance(baseline, str) and re.fullmatch(r"[0-9a-f]{40,64}", baseline):
            anchors["baseline"] = baseline
        with self.store.connect() as db:
            rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND "
                "(key LIKE 'recovery/gate-history/%' OR key LIKE 'recovery/task-baselines/%' "
                "OR key LIKE 'plan_revision/journal/%' OR key='replan_feedback') ORDER BY rowid", (rid,)).fetchall()
        for key, raw in rows:
            record = json.loads(raw)
            refs = list(record.get("recovery_refs", []))
            if record.get("recovery_ref"):
                refs.append(record["recovery_ref"])
            refs.extend(s["ref"] for s in record.get("snapshots", {}).values() if s.get("commit") and s.get("ref"))
            for ref in refs:
                if isinstance(ref, str) and ref.startswith(f"refs/aa2/{rid}/"):
                    anchors[ref] = ref
        return anchors

    def collect(self, rid, work, scopes=()):
        source = tree_hash(work)
        extra = self.engine.config.load().sensitive_paths

        def admitted(name):
            path = Path(name)
            protected = (path.name in PROTECTED_NAMES or bool(set(path.parts) & {
                "tests", "test", "__tests__", "__snapshots__", "fixtures", "scripts", ".github"}) or
                ".test." in path.name or ".spec." in path.name or path.name.startswith("test_")
                or path.name.endswith(("_test.py", ".snap")))
            return (not path.is_absolute() and ".." not in path.parts and not set(path.parts) & EXCLUDED
                    and not sensitive(path, extra) and (protected or scopes and path_allowed(name, scopes)))

        anchors, missing = {}, []
        for label, ref in self._anchors(rid).items():
            try:
                anchors[label] = git(work, "rev-parse", "--verify", ref + "^{commit}")
            except AA2Error:
                missing.append({"anchor": label, "status": "missing"})
        identity = digest([source, anchors, missing, list(scopes), extra])
        key = "review-evidence/source-history/" + identity
        cached = self.store.get(rid, key)
        if cached:
            self.engine.artifacts.json(cached["artifact"])
            return cached
        current = {}
        for path in public_files(work, extra):
            name = path.relative_to(work).as_posix()
            if admitted(name):
                raw = path.read_bytes()
                try:
                    raw.decode("utf-8")
                except UnicodeError:
                    continue
                current[name] = {"content": self.engine.artifacts.put(raw, "source_snapshot"),
                                 "mode": path.stat().st_mode & 0o777}
        snapshots, blobs = [], {}
        for label, commit in anchors.items():
            files = {}
            for row in git(work, "ls-tree", "-r", "-z", commit).split("\0"):
                if not row:
                    continue
                info, name = row.split("\t", 1)
                mode, kind, oid = info.split()
                if kind != "blob" or mode not in {"100644", "100755"} or not admitted(name):
                    continue
                if oid not in blobs:
                    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(work), "cat-file", "blob", oid],
                        capture_output=True, env=git_environment(), timeout=120)
                    if result.returncode:
                        raise StateConflict("Recorded source history object is unavailable; preserve the candidate")
                    try:
                        result.stdout.decode("utf-8")
                    except UnicodeError:
                        blobs[oid] = None
                    else:
                        blobs[oid] = self.engine.artifacts.put(result.stdout, "source_snapshot")
                if blobs[oid] is not None:
                    files[name] = {"blob": oid, "content": blobs[oid], "mode": int(mode, 8) & 0o777}
            comparisons = []
            for name in sorted(files.keys() | current.keys()):
                before, after = files.get(name), current.get(name)
                if before and after and before["content"] == after["content"] and before["mode"] == after["mode"]:
                    continue
                old = self.engine.artifacts.bytes(before["content"]).decode("utf-8") if before else ""
                new = self.engine.artifacts.bytes(after["content"]).decode("utf-8") if after else ""
                patch = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                    fromfile=label + ":" + name, tofile="current:" + name))
                comparisons.append({"path": name, "before": before, "after": after,
                                    "diff": self.engine.artifacts.put(patch, "source_diff")})
            snapshots.append({"anchor": label, "commit": commit, "files": files, "changes_to_current": comparisons})
        if tree_hash(work) != source:
            raise StateConflict("Source changed while collecting review history; preserve the candidate")
        ref = self.engine.artifacts.put({"version": 1, "source_hash": source, "scope": list(scopes),
            "includes_public_protected_files": True, "current": current, "snapshots": snapshots, "missing": missing,
            "meaning": "Exact public file bytes and comparisons, not proof that test changes are acceptable. "
                       "Read the before/after files and diffs; missing history remains an evidence gap."}, "source_history")
        record = {"status": "incomplete" if missing else "available" if anchors else "unavailable",
                  "source_hash": source, "artifact": ref, "anchors": anchors, "missing": missing}
        self.store.checkpoint(rid, key, record)
        return record
