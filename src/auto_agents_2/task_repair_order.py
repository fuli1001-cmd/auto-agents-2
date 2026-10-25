"""Execute an already-dispatched repair before stale evidence on its inputs.

Historical acceptance proves an implementation input, never current acceptance.
The ordinary task/verification/review/permit consumers remain authoritative.
"""
from __future__ import annotations

import json

from .models import PlanResult
from .planning import task_dependencies
from .util import digest


def _history(store, rid):
    with store.connect() as db:
        rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND "
            "(key LIKE 'plan_revision/history/%' OR key LIKE 'authority/task-history/%') ORDER BY rowid DESC",
            (rid,)).fetchall()
    return [(key, json.loads(raw)) for key, raw in rows]


def _accepted_input(engine, rid, task, plan, base, history):
    """Only controller-owned task slots and their exact review receipts qualify."""
    state = engine.store.get(rid, "task_state/" + task.id, {})
    if state.get("implementation") != "accepted":
        return None
    current = plan.model_dump()
    plans = {digest(current): current}
    records = [("develop/task/" + task.id, engine.store.get(rid, "develop/task/" + task.id, {}))]
    for key, row in history:
        if key.startswith("plan_revision/history/"):
            if not isinstance(row, dict) or not isinstance(row.get("plan"), dict):
                continue
            plans[digest(row["plan"])] = row["plan"]
            records.append((key + "/tasks/develop/task/" + task.id,
                            row.get("tasks", {}).get("develop/task/" + task.id, {})))
        elif key.startswith("authority/task-history/" + task.id + "/") and key.rsplit("/", 1)[-1] == digest(row):
            records.append((key, row))
    identity = engine.authority.identity(rid)
    visited = set()
    while records:
        key, saved = records.pop()
        if not isinstance(saved, dict) or digest(saved) in visited:
            continue
        visited.add(digest(saved))
        if isinstance(saved.get("previous_evidence"), dict):
            records.append((key + "/previous_evidence", saved["previous_evidence"]))
        review, commit = saved.get("review", {}), saved.get("commit")
        if (not commit or not review.get("approved") or not saved.get("source_hash")
                or not engine.authority.reusable(rid, review, current=False)):
            continue
        receipt = engine.authority.receipt(rid, review)
        basis = receipt["basis"]
        sources = basis["source_identity"]
        if (receipt["operation"] != "develop/task/" + task.id + "/review"
                or basis["scope"] != "implementation" or basis["source_hash"] != saved["source_hash"]
                or set(sources) != set(identity)
                or any(sources[k] != v for k, v in identity.items() if k not in {"plan", "plan_revision/current"})):
            continue
        previous = plans.get(sources["plan"])
        if not previous or task.model_dump() not in previous.get("tasks", []):
            continue
        # A task-local patch may preserve a dependency implementation. A changed
        # environment, global checks or other global contract cannot use this proof.
        if ({k: v for k, v in previous.items() if k not in {"tasks", "summary", "document"}}
                != {k: v for k, v in current.items() if k not in {"tasks", "summary", "document"}}):
            continue
        if not engine.workspaces.is_ancestor(commit, base):
            continue
        return {"task": task.id, "record": key, "commit": commit,
                "source_hash": saved["source_hash"], "review_hash": receipt["review_hash"],
                "plan_hash": sources["plan"]}
    return None


def _verified_result(engine, rid, task, saved, permit):
    """A consumed repair can only finish integrating its exact accepted commit."""
    result = permit.get("outcome", {}).get("tasks", {}).get(task.id, {})
    state = engine.store.get(rid, "task_state/" + task.id, {})
    review, commit = saved.get("review", {}), saved.get("commit")
    if (result.get("status") != "verified" or not commit or result.get("commit") != commit
            or result.get("evidence") != "develop/task/" + task.id
            or permit["phase"] == "completed" and permit.get("outcome_hash") != digest(permit["outcome"])
            or result.get("source_hash") != saved.get("source_hash")
            or state.get("implementation") != "accepted" or state.get("evidence") != "valid"
            or not review.get("approved") or not engine.authority.reusable(rid, review)
            or not engine.workspaces.is_ancestor(commit, commit)):
        return False
    receipt = engine.authority.receipt(rid, review)
    return (receipt["operation"] == "develop/task/" + task.id + "/review"
            and receipt["basis"]["scope"] == "implementation"
            and receipt["basis"]["source_hash"] == saved["source_hash"])


def dispatched_repair(engine, rid, plan: PlanResult, done, base):
    """Return at most one permitted repair whose only blockers are evidence.

No new permit, accepted flag, or implementation budget is created here. Missing
history or changed contracts simply leave the original dependency gate in place.
"""
    if (not engine.store.get(rid, "authority/protocol")
            or plan.model_dump() != engine.store.get(rid, "plan")):
        return None
    tasks = {task.id: task for task in plan.tasks}
    history = None
    for task in plan.tasks:
        missing = task_dependencies(task, tasks) - done
        if task.id in done or not missing:
            continue
        saved = engine.store.get(rid, "develop/task/" + task.id, {})
        permit = engine.store.get(rid, saved.get("recovery_permit", ""), {})
        if (permit.get("phase") not in {"started", "completed"}
                or permit.get("action", {}).get("action") != "repair"
                or task.id not in permit["action"].get("task_ids", [])
                or permit.get("plan_hash") != digest(plan.model_dump())
                or permit.get("task_scopes", {}).get(task.id) != task.allowed_paths
                or task.id not in permit.get("outcome", {}).get("expected_tasks", [])):
            continue
        if _verified_result(engine, rid, task, saved, permit):
            engine.recovery_policy.validate_consumption(rid, saved["recovery_permit"], expected={"repair"})
            mode = "integrate_verified_result"
        elif (not saved.get("commit") and permit["phase"] == "started"
              and engine.recovery_policy.task_authorized(rid, saved, task)):
            mode = "execute_dispatched_repair"
        else:
            continue
        if history is None:
            history = _history(engine.store, rid)
        proofs = [_accepted_input(engine, rid, tasks[dep], plan, base, history)
                  if dep in tasks else None for dep in sorted(missing)]
        if all(proofs):
            return task, {"task": task.id, "permit": saved["recovery_permit"],
                          "base": base, "dependencies": proofs, "mode": mode}
    return None
