"""Move retained, unfinished candidates onto the current dependency baseline."""
from __future__ import annotations

from .errors import AA2Error, IntegrationConflict
from .readview import public_files
from .util import digest
from .workspace import git, path_allowed, tree_hash


def refresh_task_baseline(engine, rid, task, saved, work, base, revision):
    store, manager = engine.store, engine.workspaces
    task_key = "develop/task/" + task.id
    identity = digest([str(work), base])[:16]
    key = f"recovery/task-baselines/{task.id}/{identity}"
    record = store.get(rid, key)
    prior_feedback = {**saved.get("feedback", {}), **{k: saved[k] for k in (
        "checks", "review", "environment_error", "environment_failure", "candidate_error") if k in saved}}
    at_base = manager.is_ancestor(base, git(work, "rev-parse", "HEAD"))
    if record and record["phase"] in {"merged", "repair"} and not at_base:
        raise AA2Error("Completed dependency refresh no longer matches the task workspace; preserve its history")
    if (not record and at_base) or record and record["phase"] in {"merged", "repair"}:
        return saved, work
    if not record:
        expected = saved.get("source_hash") or saved.get("previous_evidence", {}).get("source_hash")
        source = tree_hash(work)
        if expected and source != expected or not expected and git(work, "status", "--porcelain"):
            raise AA2Error("Retained task changed outside its recorded candidate; preserve user edits before updating dependencies")
        ref = f"refs/aa2/{rid}/task-baselines/{task.id}/{identity}"
        snapshot = manager.checkpoint(work, ref, "aa2: preserve unfinished task before dependency refresh")
        if git(work, "status", "--porcelain"):
            raise AA2Error("Unarchived task files remain; preserve them before updating dependencies")
        record = {"version": 1, "phase": "prepared", "base": base, "workspace": str(work),
                  "snapshot": snapshot, "recovery_ref": ref, "source_hash": source, "previous_task": saved}
        store.checkpoint(rid, key, record)
    if record["workspace"] != str(work) or record["base"] != base:
        raise AA2Error("Task baseline journal identity changed")
    if not at_base:
        if tree_hash(work) != record["source_hash"] or git(work, "rev-parse", "HEAD") != record["snapshot"]:
            raise AA2Error("Retained candidate changed during dependency refresh; preserve both snapshots")
        try:
            manager.integrate(work, base)
        except IntegrationConflict as exc:
            # integrate aborts a failed merge. Keep the original worktree and ref;
            # ask the existing scoped implementation task to reconcile its candidate
            # against the accepted dependency code in a new, isolated worktree.
            if (tree_hash(work) != record["source_hash"]
                    or git(work, "rev-parse", "HEAD") != record["snapshot"]):
                raise AA2Error("Failed dependency merge did not restore its snapshot; preserve the workspace") from exc
            sources = []
            for path in public_files(work, engine.config.load().sensitive_paths):
                relative = path.relative_to(work).as_posix()
                if path_allowed(relative, task.allowed_paths):
                    try:
                        sources.append({"path": relative, "content": path.read_bytes().decode("utf-8")})
                    except UnicodeError:
                        continue
            artifact = engine.artifacts.put({"snapshot": record["snapshot"], "files": sources,
                "previous_proposal": record["previous_task"].get("implementation")}, "retained-task-candidate")
            fresh = manager.allocate(rid, f"{task.id}-r{revision}-{base[:8]}-repair", base)
            engine.resources.register(fresh, "worktree", rid)
            if git(fresh, "rev-parse", "HEAD") != base or git(fresh, "status", "--porcelain"):
                raise AA2Error("Dependency repair workspace has unknown changes; preserve it before retrying")
            feedback = {**prior_feedback, "baseline_refresh": {
                "reason": "Reconcile the retained candidate with current dependency code within the existing task scope. Preserve accepted dependency fixes.",
                "incoming_base": base, "retained_candidate": artifact, "recovery_ref": record["recovery_ref"]}}
            updated = {"phase": "rejected", "workspace": str(fresh), "source_hash": tree_hash(fresh),
                       "revision": revision, "feedback": feedback, "recovery_permit": saved.get("recovery_permit")}
            store.checkpoints(rid, {key: {**record, "phase": "repair", "retained_candidate": artifact,
                                          "repair_workspace": str(fresh)}, task_key: updated})
            store.event(rid, "task.baseline_refreshed", {"task": task.id, "base": base, "action": "repair", "journal": key})
            return updated, fresh
    # If the process stopped after git merge but before SQLite publication, the
    # exact parents identify our completed merge. A later user commit is unknown.
    parents = git(work, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if set(parents) != {record["snapshot"], base} or git(work, "status", "--porcelain"):
        raise AA2Error("Dependency merge has unknown changes; preserve the journal and workspace")
    phase = "reverify" if saved.get("phase") in {"applied", "tested", "reverify"} else "rejected"
    updated = {"phase": phase, "workspace": str(work), "revision": revision, "source_hash": tree_hash(work),
               "recovery_permit": saved.get("recovery_permit"),
               "feedback": {**prior_feedback, "baseline_refresh": {
                   "incoming_base": base, "reason": "Dependencies updated; retained implementation requires fresh verification.",
                   "recovery_ref": record["recovery_ref"]}}}
    store.checkpoints(rid, {key: {**record, "phase": "merged", "merged_commit": git(work, "rev-parse", "HEAD")}, task_key: updated})
    store.event(rid, "task.baseline_refreshed", {"task": task.id, "base": base, "action": phase, "journal": key})
    return updated, work
