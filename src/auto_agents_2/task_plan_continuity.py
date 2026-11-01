"""Preserve scoped task acceptance across ordinary reviewed local plan patches.

This is not a verification-cache hit or an execution permit. Original reviews,
check source/environment identities and cumulative budgets remain immutable.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import PurePosixPath

from .errors import AA2Error
from .faults import StateConflict
from .models import PlanResult
from .plan_revision import PlanPatch, apply_patch
from .planning import task_dependencies
from .review_authority import source_identity, validate_review
from .util import digest
from .workspace import git, git_environment, path_allowed, source_manifest, source_manifest_hash

REASSESSMENT = {"authority_reassessment": "Retain accepted implementation; obtain a current grounded verdict."}


def retained_record(saved):
    """Recognize only the old controller's untouched invalidation envelope."""
    if not isinstance(saved, dict):
        return None
    original = saved.get("previous_evidence", {})
    if not isinstance(original, dict):
        return None
    if (saved == {"phase": "reverify", "resume_base": original.get("commit"),
                  "previous_evidence": original, "feedback": REASSESSMENT}
            and original.get("commit")):
        return original
    return None


def _receipt(rows, raw, scope):
    record = rows.get("authority/reviews/" + digest(raw), {})
    if (not raw.get("approved") or record.get("review") != raw
            or record.get("review_hash") != digest(raw) or record.get("basis", {}).get("scope") != scope):
        raise ValueError("Missing exact independent review")
    validate_review(raw, record["basis"])
    return record


def _chain(rows, identity, task, saved, basis):
    current = rows["plan"]
    tasks = {t.id: t for t in PlanResult.model_validate(current).tasks}
    if task.model_dump() != tasks[task.id].model_dump():
        return None
    closure = task_dependencies(task, tasks) | {task.id}
    before = basis["source_identity"]
    if (set(before) != set(identity) or any(before[k] != identity[k]
            for k in identity if k not in {"plan", "plan_revision/current"})):
        return None
    revision = rows["plan_revision/current"]["revision"]
    links = []
    while identity != before and revision > 1:
        matches = [(key, j) for key, j in rows.items() if key.startswith("plan_revision/journal/")
                   and j.get("phase") == "activated" and j.get("base_revision") == revision - 1
                   and j.get("plan") == current]
        if len(matches) != 1:
            return None
        key, journal = matches[0]
        # Specialized amendments retain their existing reassessment paths.
        if (journal.get("recording") or journal.get("authority_reassessment")
                or journal["patch"].get("timing_corrections") or journal["patch"].get("contract_corrections")):
            return None
        history = rows.get(f"plan_revision/history/{revision - 1}", {})
        previous = journal["previous_plan"]
        historical = history.get("tasks", {})
        if (history.get("plan") != previous or task.id not in historical.get("develop/done", [])
                or historical.get("develop/task/" + task.id) != saved
                or historical.get("task_state/" + task.id, {}).get("implementation") != "accepted"
                or historical.get("task_state/" + task.id, {}).get("evidence") != "valid"):
            return None
        if ({k: v for k, v in previous.items() if k not in {"document", "tasks"}}
                != {k: v for k, v in current.items() if k not in {"document", "tasks"}}):
            return None
        old = PlanResult.model_validate(previous)
        patch = PlanPatch.model_validate(journal["patch"])
        candidate, affected, reopen = apply_patch(old, patch, revision - 1, set(patch.failure_ids),
                                                  {t.id: t.origin_id for t in old.tasks})
        from .check_production import journal_affected
        affected |= journal_affected(old, candidate, journal)
        if (candidate.model_dump() != current or affected != set(journal["affected"])
                or reopen != set(journal["reopen"]) or closure & affected
                or any(tasks[tid].model_dump() not in previous["tasks"] for tid in closure)):
            return None
        prior = {**identity, "plan": digest(previous),
                 "plan_revision/current": digest({"version": 1, "revision": revision - 1})}
        review = _receipt(rows, journal["review"], "plan_patch")
        proof = review["basis"]
        if (proof["source_identity"] != prior
                or proof != journal.get("evidence_admission", {}).get("authority")
                or proof["source_hash"] != journal.get("source_hash")
                or proof["candidate_hash"] != digest({k: journal[k] for k in ("plan", "patch", "previous_plan")})):
            return None
        links.append({"journal": key, "journal_hash": digest(journal), "review_hash": review["review_hash"]})
        current, identity, revision = previous, prior, revision - 1
    if identity != before or not links:
        return None
    return links, closure, tasks


def _committed_manifest(workspaces, commit):
    entries = workspaces.entries(commit)
    if any(v["mode"] not in {"100644", "100755"} for v in entries.values()):
        return None  # Symlink/submodule observations need the existing full reassessment.
    oids = sorted({v["oid"] for v in entries.values()})
    result = subprocess.run(["git", "-C", str(workspaces.project), "cat-file", "--batch"],
        input=("\n".join(oids) + "\n").encode(), capture_output=True, env=git_environment(), timeout=120)
    if result.returncode:
        return None
    hashes, offset = {}, 0
    for oid in oids:
        end = result.stdout.index(b"\n", offset)
        actual, kind, size = result.stdout[offset:end].decode().split()
        size = int(size)
        body = result.stdout[end + 1:end + 1 + size]
        if actual != oid or kind != "blob" or len(body) != size or result.stdout[end + 1 + size:end + 2 + size] != b"\n":
            return None
        hashes[oid] = hashlib.sha256(body).hexdigest()
        offset = end + 2 + size
    return {p: ["file", v["mode"] == "100755", hashes[v["oid"]]] for p, v in entries.items()}


def _source(engine, work, task, saved, closure, tasks):
    commit = git(work, "rev-parse", "HEAD")
    if not engine.workspaces.is_ancestor(saved["commit"], commit):
        return None
    original = _committed_manifest(engine.workspaces, saved["commit"])
    actual = source_manifest(work)
    if (original is None or source_manifest_hash(original) != saved["source_hash"]
            or actual != _committed_manifest(engine.workspaces, commit)):
        return None
    scopes = []
    for tid in closure:
        current = tasks[tid]
        scopes += current.allowed_paths
        for command in current.checks:
            if not command.inputs or any(p == "." for p in command.inputs):
                return None  # Unknown input boundaries cannot justify selective reuse.
            scopes += command.inputs
    # Execution resolves ./ and repeated separators; raw scope matching does not.
    # Unknown aliases must reverify, never silently omit a declared input.
    if any(not p.rstrip("/") or "\\" in p or PurePosixPath(p).is_absolute()
           or PurePosixPath(p).as_posix() in {".", ""}
           or PurePosixPath(p).as_posix() != p.rstrip("/") or ".." in PurePosixPath(p).parts
           for p in scopes):
        return None
    paths = sorted(p for p in original.keys() | actual.keys() if path_allowed(p, scopes))
    if any(original.get(p) != actual.get(p) for p in paths):
        return None
    return {"commit": commit, "source_hash": source_manifest_hash(actual), "scopes": sorted(set(scopes)),
            "scoped_source_hash": digest({p: actual[p] for p in paths})}


def retain(engine, rid, task, saved, work):
    """Prove and publish acceptance continuity under one state transaction."""
    if work is None:
        return None
    original = retained_record(saved) or saved
    if not original.get("commit") or not original.get("source_hash"):
        return None
    store, key = engine.store, "develop/task/" + task.id
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = {row["key"]: json.loads(row["data"]) for row in db.execute(
            "SELECT key,data FROM checkpoints WHERE run_id=?", (rid,))}
        if rows.get(key) != saved:
            raise StateConflict("Task acceptance changed during continuity assessment")
        state = rows.get("task_state/" + task.id, {})
        restoring = original != saved
        if (state.get("implementation") != "accepted" or state.get("evidence") != ("stale" if restoring else "valid")
                or not restoring and task.id not in rows.get("develop/done", [])):
            return None
        rows["authority/submitted-answers"] = store._submitted_answers(db, rid)
        identity = source_identity(rows.get)
        try:
            record = _receipt(rows, original.get("review", {}), "implementation")
            basis = record["basis"]
            if (record.get("operation") != "develop/task/" + task.id + "/review"
                    or basis["source_hash"] != original["source_hash"]
                    or basis["observations"].get("checks") != digest(original.get("checks"))
                    or basis["observations"].get("implementation") != digest(original.get("implementation"))):
                return None
            chain = _chain(rows, identity, task, original, basis)
            if chain is None:
                return None
            links, closure, tasks = chain
            source = _source(engine, work, task, original, closure, tasks)
            if source is None:
                return None
        except (AA2Error, KeyError, TypeError, ValueError, AttributeError, OSError, subprocess.SubprocessError):
            return None  # Missing/damaged proof retains the existing reverify gate.
        history = rows["plan_revision/history/" + str(rows["plan_revision/current"]["revision"] - 1)]["tasks"]
        old_state = history["task_state/" + task.id]
        if {**state, "evidence": "valid"} != old_state:
            return None
        if (git(work, "rev-parse", "HEAD") != source["commit"]
                or source_manifest_hash(source_manifest(work)) != source["source_hash"]):
            raise StateConflict("Source changed during task continuity assessment")
        proof = {"version": 1, "task": task.id, "from": basis["source_identity"], "to": identity,
            "review_hash": record["review_hash"], "accepted_record_hash": digest(original),
            "accepted_commit": original["commit"], "accepted_source_hash": original["source_hash"],
            "transitions": links, "current_source": source,
            "meaning": "Retained scoped acceptance, not new execution evidence or permission."}
        proof_key = "authority/task-continuity/" + task.id
        if rows.get(proof_key) != proof or restoring:
            store._put(db, rid, proof_key, proof)
            if restoring:
                store._put(db, rid, key, original)
                store._put(db, rid, "task_state/" + task.id, old_state)
                store._put(db, rid, "develop/done", sorted(set(rows.get("develop/done", [])) | {task.id}))
            store._event(db, rid, "task.authority_retained", {"task": task.id, "proof": proof_key,
                "review_hash": record["review_hash"], "restored": restoring})
        return original
