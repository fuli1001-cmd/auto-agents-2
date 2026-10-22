"""Public source comparisons from recorded Git snapshots and controller observations."""
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

    def _records(self, rid):
        with self.store.connect() as db:
            rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND "
                "(key LIKE 'recovery/gate-history/%' OR key LIKE 'recovery/task-baselines/%' "
                "OR key LIKE 'plan_revision/journal/%' OR key LIKE 'plan_revision/history/%' "
                "OR key='replan_feedback') ORDER BY rowid", (rid,)).fetchall()
        return [(key, json.loads(raw)) for key, raw in rows]

    def _anchors(self, rid, records):
        anchors = {}
        baseline = self.store.get(rid, "baseline")
        if isinstance(baseline, str) and re.fullmatch(r"[0-9a-f]{40,64}", baseline):
            anchors["baseline"] = baseline
        for key, record in records:
            refs = list(record.get("recovery_refs", []))
            if record.get("recovery_ref"):
                refs.append(record["recovery_ref"])
            refs.extend(s["ref"] for s in record.get("snapshots", {}).values() if s.get("commit") and s.get("ref"))
            for ref in refs:
                if isinstance(ref, str) and ref.startswith(f"refs/aa2/{rid}/"):
                    anchors[ref] = ref
        return anchors

    def _proposals(self, records, admitted):
        """Read explicit controller archive slots, never recurse into model-owned records."""
        proposals = []
        for origin, record in records:
            if origin.startswith("recovery/gate-history/"):
                tasks = record.get("checkpoints", {})
                plan = tasks.get("plan", {})
            elif origin.startswith("plan_revision/history/"):
                tasks, plan = record.get("tasks", {}), record.get("plan", {})
            elif origin.startswith("recovery/task-baselines/"):
                tasks = {"develop/task/" + origin.split("/")[2]: record.get("previous_task", {})}
                plan = {}
            else:
                continue
            for key, saved in tasks.items():
                if not re.fullmatch(r"develop/task/[^/]+", key) or not isinstance(saved, dict):
                    continue
                proposal = saved.get("implementation") or {}
                changes = proposal.get("changes", [])
                if not changes:
                    continue
                selected = [{k: change[k] for k in ("path", "operation", "content", "old_text") if k in change}
                            for change in changes if admitted(change["path"])]
                if not selected:
                    continue
                task_id = key.removeprefix("develop/task/")
                commands = next((t.get("checks", []) for t in plan.get("tasks", []) if t["id"] == task_id), [])
                proposals.append({"origin": origin + "/" + key, "task_id": task_id,
                    "status": "proposal_only", "recorded_phase": saved.get("phase"),
                    "recorded_source_hash": saved.get("source_hash"), "proposal_hash": digest(proposal),
                    "content": self.engine.artifacts.put({"changes": selected}, "source_proposal"),
                    "omitted_changes": len(changes) - len(selected), "commands": commands,
                    "checks": saved.get("checks") or saved.get("feedback", {}).get("checks", [])})
        return proposals

    def _response_proposals(self, rid, admitted, archived):
        """Ordinary retries retain returned proposals even when task slots advance.

        Read only this run's explicit controller task-response events. A response
        proves neither application nor acceptance; never follow model references
        or mount the raw, unfiltered response as a material dependency.
        """
        from .models import ImplementationResult

        with self.store.connect() as db:
            rows = db.execute("SELECT seq,data FROM events WHERE run_id=? AND kind='artifact.created' "
                              "ORDER BY seq", (rid,)).fetchall()
        seen = {(p["task_id"], p["proposal_hash"]) for p in archived}
        proposals = []
        for seq, raw in rows:
            record = json.loads(raw)
            operation = record.get("operation", "")
            match = re.fullmatch(r"develop/task/([^/]+)", operation)
            if record.get("stage") != "implement" or not match:
                continue
            try:
                ref = record["artifact"]
                if ref.get("kind") != "implement":
                    raise ValueError("Invalid task response kind")
                body = self.engine.artifacts.bytes(ref)
                if len(body) != ref["bytes"]:
                    raise ValueError("Invalid task response length")
                proposal = json.loads(body)
                ImplementationResult.model_validate(proposal)
                identity = (match[1], digest(proposal))
                if identity in seen:
                    continue
                seen.add(identity)
                changes = proposal.get("changes", [])
                selected = [{k: c[k] for k in ("path", "operation", "content", "old_text") if k in c}
                            for c in changes if admitted(c["path"])]
                if selected:
                    proposals.append({"origin": f"events/{seq}/" + operation, "task_id": match[1],
                        "status": "proposal_only", "recorded_phase": "returned", "recorded_source_hash": None,
                        "proposal_hash": identity[1], "response_hash": ref["hash"],
                        "content": self.engine.artifacts.put({"changes": selected}, "source_proposal"),
                        "omitted_changes": len(changes) - len(selected), "commands": [], "checks": []})
            except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
                raise StateConflict("Recorded task response is unavailable or inconsistent; preserve the candidate") from exc
        return proposals

    def _observations(self, rid, source, admitted):
        """Recover selected bytes from prior collections, not model citations or proposals.

        Only the original `current` slot is read. Following earlier observations
        would recursively expand history and make repeated collection unstable.
        Keep the first origin supplying each file so new manifests of the same
        observation cannot invalidate the cache indefinitely.
        """
        with self.store.connect() as db:
            rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND "
                "key LIKE 'review-evidence/source-history/%' ORDER BY rowid", (rid,)).fetchall()
        observed = {}
        for origin, raw in rows:
            try:
                record = json.loads(raw)
                previous = record.get("source_hash")
                if previous == source:
                    continue
                ref = record["artifact"]
                if (not re.fullmatch(r"[0-9a-f]{64}", previous or "") or ref.get("kind") != "source_history"):
                    raise ValueError("Invalid observation identity")
                manifest = self.engine.artifacts.json(ref)
                if (manifest.get("version") not in {1, 2, 3, 4} or manifest.get("source_hash") != previous
                        or not isinstance(manifest.get("current"), dict)):
                    raise ValueError("Unbound source observation")
                selected = {}
                for name, entry in manifest["current"].items():
                    # Apply today's public path/scope policy before reading any body.
                    if not admitted(name):
                        continue
                    content = entry["content"]
                    mode = entry["mode"]
                    if (content.get("kind") != "source_snapshot" or type(mode) is not int
                            or not 0 <= mode <= 0o777):
                        raise ValueError("Invalid observed file")
                    body = self.engine.artifacts.bytes(content)
                    body.decode("utf-8")
                    if len(body) != content["bytes"]:
                        raise ValueError("Invalid observed file length")
                    selected[name] = {"content": {k: content[k] for k in ("hash", "kind", "bytes")}, "mode": mode}
                if not selected:
                    continue
                item = observed.setdefault(previous, {"source_hash": previous, "status": "observed_only",
                    "origins": [], "files": {}})
                added = False
                for name, entry in selected.items():
                    existing = item["files"].get(name)
                    # Execution source identity binds contents and executable bits,
                    # not other permission bits. Preserve the first observed mode.
                    if existing is not None and (existing["content"] != entry["content"]
                            or bool(existing["mode"] & 0o111) != bool(entry["mode"] & 0o111)):
                        raise ValueError("Conflicting bytes for one observed source identity")
                    if existing is None:
                        item["files"][name] = entry
                        added = True
                if added:
                    # Provenance metadata, not permission to mount the unfiltered old manifest.
                    item["origins"].append({"checkpoint": origin, "manifest_hash": ref["hash"]})
            except (AttributeError, KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
                raise StateConflict("Recorded source observation is unavailable or inconsistent; preserve the candidate") from exc
        return [observed[key] for key in sorted(observed)]

    def _comparisons(self, label, files, current):
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
        return comparisons

    def collect_plan(self, rid, work, plan):
        """Use the reviewed plan's task scopes for integrated read-only review.

        Callers supply their active, inherited or journal-bound plan. This does
        not infer scope from model citations or expand any task's write policy.
        """
        scopes = sorted({path for task in plan.tasks for path in task.allowed_paths}) if plan else []
        return self.collect(rid, work, scopes)

    def collect(self, rid, work, scopes=()):
        source = tree_hash(work)
        extra = self.engine.config.load().sensitive_paths

        def public_path(name):
            path = Path(name)
            return (not path.is_absolute() and ".." not in path.parts and not set(path.parts) & EXCLUDED
                    and not sensitive(path, extra))

        def admitted(name):
            path = Path(name)
            protected = (path.name in PROTECTED_NAMES or bool(set(path.parts) & {
                "tests", "test", "__tests__", "__snapshots__", "fixtures", "scripts", ".github"}) or
                ".test." in path.name or ".spec." in path.name or path.name.startswith("test_")
                or path.name.endswith(("_test.py", ".snap")))
            return public_path(name) and (protected or scopes and path_allowed(name, scopes))

        records = self._records(rid)
        proposals = self._proposals(records, admitted)
        proposals.extend(self._response_proposals(rid, admitted, proposals))
        anchors, missing = {}, []
        for label, ref in self._anchors(rid, records).items():
            try:
                anchors[label] = git(work, "rev-parse", "--verify", ref + "^{commit}")
            except AA2Error:
                missing.append({"anchor": label, "status": "missing"})
        observations = self._observations(rid, source, admitted)
        identity = digest([4, source, anchors, missing, list(scopes), extra, proposals, observations])
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
            files, public_tree = {}, {}
            private_paths_omitted = False
            root_tree = git(work, "rev-parse", "--verify", commit + "^{tree}")
            for row in git(work, "ls-tree", "--full-tree", "-r", "-z", commit).split("\0"):
                if not row:
                    continue
                info, name = row.split("\t", 1)
                mode, kind, oid = info.split()
                if not public_path(name):
                    private_paths_omitted = True
                    continue
                # Enumerate public paths independently of body selection. Do not
                # follow links/submodules or read out-of-scope or private blobs.
                entry = {"mode": mode, "type": kind, "object": oid, "body_status": "unsupported_type"}
                public_tree[name] = entry
                if kind != "blob" or mode not in {"100644", "100755"}:
                    continue
                entry["body_status"] = "outside_scope"
                if not admitted(name):
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
                entry["body_status"] = "included" if blobs[oid] is not None else "non_utf8"
                if blobs[oid] is not None:
                    files[name] = {"blob": oid, "content": blobs[oid], "mode": int(mode, 8) & 0o777}
            comparisons = self._comparisons(label, files, current)
            snapshots.append({"anchor": label, "commit": commit, "root_tree": root_tree,
                "public_tree": public_tree, "private_paths_omitted": private_paths_omitted,
                "files": files, "changes_to_current": comparisons})
        for observation in observations:
            observation["changes_to_current"] = self._comparisons(
                "observed:" + observation["source_hash"], observation["files"], current)
        if tree_hash(work) != source:
            raise StateConflict("Source changed while collecting review history; preserve the candidate")
        ref = self.engine.artifacts.put({"version": 4, "source_hash": source, "scope": list(scopes),
            "includes_public_protected_files": True, "current": current, "snapshots": snapshots, "missing": missing,
            "proposals": proposals, "observations": observations,
            "coverage": {
                "inventory": "complete_public_git_tree", "body_selection": "protected_files_plus_scope",
                "missing": "unresolved_anchors_only",
                "proposals": "Filtered changes from explicit controller archives and returned task-response events "
                             "in this run. Returned responses do not establish application, acceptance, source "
                             "identity or historical checks. Their assertions are suggestions to reconcile "
                             "against accepted requirements, not new acceptance obligations by themselves.",
                "observations": "Selected UTF-8 regular file bytes from earlier controller collections in this run, "
                                "bound to their source hashes and original manifests. They may be uncommitted, "
                                "rejected or from another task workspace; they are not accepted baselines or approvals. "
                                "Only paths admitted by the current public/scope policy are exposed. "
                                "No complete inventory was captured for these observations: a missing file or null "
                                "comparison side means not captured, not proof of absence. No implied chronology "
                                "or exhaustive capture of every candidate; reconcile identities with task evidence.",
                "absence": "Each public_tree enumerates all public tracked entries in its bound commit/root_tree, "
                           "independently of scope. If private_paths_omitted is false it is the entire tracked tree; "
                           "otherwise absence from this inventory cannot prove absence from private paths. "
                           "Entries without included bodies are present, not absent.",
                "limits": "Tracked Git entries only: no untracked files, external configuration, installed plugins, "
                          "symlink targets or submodule contents. current and changes_to_current compare selected "
                          "UTF-8 regular file bodies only, not complete current path existence. Unavailable bodies "
                          "remain evidence gaps when needed. Inventory visibility never grants write permission."},
            "meaning": "Exact public file bytes and comparisons, not proof that test changes are acceptable. "
                       "Proposals are retained suggestions, not applied source or approvals. Their checks are prior "
                       "observations, not verification of the proposal. Reconcile against the recorded snapshots; "
                       "submit changes for the normal protected-change review and fresh verification. "
                       "Read the before/after files and diffs; missing history remains an evidence gap."}, "source_history")
        record = {"status": "incomplete" if missing else "available" if anchors or proposals or observations else "unavailable",
                  "source_hash": source, "artifact": ref, "anchors": anchors, "missing": missing}
        self.store.checkpoint(rid, key, record)
        return record
