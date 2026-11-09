"""Reassess an unconsumed task dispatch after one reviewed local plan amendment.

This observation never migrates a permit. Unknown source changes and any retained
implementation still reach the original fail-closed consumption gate.
"""
from __future__ import annotations

import json

from .errors import AA2Error
from .faults import StateConflict
from .recovery import FailureRecord
from .recovery_identity import action_contract, binding_matches_failure
from .state_codec import checkpoint_query_db
from .util import digest
from .workspace import tree_hash


def is_reassessment(failure):
    return (failure.component == "task_authority" and failure.kind == "contract"
            and failure.step == "authority_reassessment")



def authorization_sources(policy, rid, failure, active, work, operation):
    """Freeze this observation's authority across decision/user waits."""
    if not is_reassessment(failure):
        return None
    observation = failure.evidence.get("authority_reassessment", {})
    expected = observation.get("current_sources")
    current = policy.engine.authority.identity(rid)
    if expected == current:
        if observation.get("workspace") != str(work) or observation.get("source_hash") != tree_hash(work):
            raise StateConflict("Task authority reassessment workspace changed; preserve user edits")
        return current
    if operation == "patch-followup":
        key = active.get("patch_permit", "")
        permit = policy.store.get(rid, key, {})
        outcome = policy.completed_outcome(rid, key, action="plan_patch") if key else None
        if (permit.get("cycle_id") == policy.cycle_id(failure, active)
                and permit.get("authority_sources") == expected
                and permit.get("action", {}).get("failure_id") == failure.id
                and outcome == {"status": "applied", "plan_revision":
                                policy.store.get(rid, "plan_revision/current", {}).get("revision")}):
            transition = policy.engine.authority.patch_transition(rid, active, failure)
            if transition["before"] == expected and transition["after"] == current:
                return current
    raise StateConflict("Task authority reassessment sources changed; preserve the original observation")


def _historical_consumption(engine, rid, key, permit, previous, revision):
    """Validate the ORIGINAL grant in a separate read-only historical view."""
    if permit.get("resolution_diagnosis") or permit.get("recording") or permit.get("authority_patch"):
        return False  # Specialized recovery keeps its own reconciliation path.
    from types import SimpleNamespace

    from .recovery_policy import RecoveryPolicy
    from .workflow_authority import WorkflowAuthority

    class HistoricalStore:
        def get(self, run_id, name, default=None):
            if run_id == rid and name in {"plan", "plan_revision/current"}:
                return previous if name == "plan" else revision
            return engine.store.get(run_id, name, default)

        def authority_get(self, run_id, name):
            if name in {"plan", "plan_revision/current"}:
                return self.get(run_id, name)
            return engine.store.authority_get(run_id, name)

    historical = SimpleNamespace(store=HistoricalStore())
    historical.authority = WorkflowAuthority(historical)
    policy = SimpleNamespace(store=historical.store, engine=historical)
    try:
        RecoveryPolicy.validate_consumption(policy, rid, key, expected={"repair"})
    except (AA2Error, KeyError, TypeError, ValueError):
        return False
    return True


def _unimplemented(value):
    if isinstance(value, dict):
        if value.get("phase") in {"applied", "tested", "reverify"}:
            return False
        if any(value.get(k) for k in ("implementation", "patch", "checks", "review", "commit",
                                     "protected_review", "recording_review", "retained_candidate")):
            return False
        return all(_unimplemented(v) for v in value.values())
    if isinstance(value, list):
        return all(_unimplemented(v) for v in value)
    return True


def _baseline_chain(store, rid, task, saved, historical):
    """Account for fields stripped by dependency refresh, not just today's slot."""
    chain, seen, current = [], set(), saved
    if not _unimplemented(historical):
        return None
    while current != historical:
        if not _unimplemented(current):
            return None
        refresh = current.get("feedback", {}).get("baseline_refresh", {})
        prefix = f"refs/aa2/{rid}/task-baselines/{task.id}/"
        ref = refresh.get("recovery_ref", "")
        if not ref.startswith(prefix):
            return None
        suffix = ref.removeprefix(prefix)
        key = f"recovery/task-baselines/{task.id}/{suffix}"
        record = store.get(rid, key, {})
        if (key in seen or record.get("phase") != "merged" or record.get("recovery_ref") != ref
                or record.get("workspace") != current.get("workspace")
                or record.get("base") != refresh.get("incoming_base")
                or suffix != digest([record.get("workspace"), record.get("base")])[:16]
                or not record.get("snapshot") or not record.get("merged_commit")
                or not isinstance(record.get("previous_task"), dict)):
            return None
        seen.add(key)
        chain.append({"journal": key, "journal_hash": digest(record), "record": record})
        current = record["previous_task"]
    return chain or None


def _frozen_dispatch(store, rid, key, permit, previous):
    prefix = "recovery/permits/"
    if not key.startswith(prefix):
        return None
    suffix = key.removeprefix(prefix)
    binding = store.get(rid, "recovery/authorization-inputs/" + suffix, {})
    snapshot = store.get(rid, "recovery/request-snapshots/" + suffix, {})
    cycle = store.get(rid, "recovery/cycles/" + permit.get("cycle_id", ""), {})
    failure = permit.get("authority_failure", {})
    frozen = snapshot.get("request", {}).get("scope", {})
    tasks = [t for t in previous["tasks"] if t["id"] in permit.get("task_scopes", {})]
    choices = frozen.get("candidates", {})
    if (not failure or snapshot.get("binding") != binding
            or not binding_matches_failure(binding, failure)
            or binding.get("source_hash") != permit.get("source_hash")
            or binding.get("plan_hash") != permit.get("plan_hash")
            or binding.get("scope_hash") != digest([tasks, failure.get("evidence", {}).get("allowed_paths", [])])
            or frozen.get("tasks") != tasks or frozen.get("cycle_id") != permit.get("cycle_id")
            or permit.get("task_scopes") != {t["id"]: t["allowed_paths"] for t in tasks}
            or choices.get("repair") != permit.get("action")
            or key not in cycle.get("permits", []) or cycle.get("id") != permit.get("cycle_id")
            or cycle.get("id") != digest([cycle.get("owner"), cycle.get("failure_id"), cycle.get("observation")])
            or cycle.get("failure_id") != failure.get("id") or permit["action"].get("failure_id") != failure.get("id")
            or cycle.get("owner") != failure.get("origin_id") or permit.get("owner") != failure.get("origin_id")):
        return None
    choice_hash = digest({k: action_contract(v) for k, v in choices.items()}) if binding.get("version") == 2 else digest(choices)
    if binding.get("candidates") != choice_hash:
        return None
    return {"permit": permit, "binding": binding, "request": snapshot, "cycle": cycle}


def plan_reassessment(engine, rid, task, saved):
    """Return a diagnosis observation only; do not consume/complete the old grant."""
    store, authority = engine.store, engine.authority
    if (not store.get(rid, "authority/protocol") or saved.get("phase") != "rejected"
            or not saved.get("source_hash") or not _unimplemented(saved)):
        return None
    key = saved.get("recovery_permit", "")
    permit = store.get(rid, key, {})
    before, after = permit.get("authority_sources", {}), authority.identity(rid)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    if (changed != {"plan", "plan_revision/current"} or before.keys() != after.keys()
            or permit.get("phase") != "started" or permit.get("action", {}).get("action") != "repair"
            or task.id not in permit["action"].get("task_ids", [])
            or task.id not in permit.get("outcome", {}).get("expected_tasks", [])
            or task.id in permit.get("outcome", {}).get("tasks", {})
            or permit.get("task_scopes", {}).get(task.id) != task.allowed_paths
            or permit.get("plan_hash") != before.get("plan")):
        return None
    plan, revision = store.get(rid, "plan", {}), store.get(rid, "plan_revision/current", {})
    if digest(plan) != after["plan"] or task.model_dump() not in plan.get("tasks", []):
        return None
    with store.connect() as db:
        rows = checkpoint_query_db(db, "SELECT key,slot_order AS data FROM checkpoint_records WHERE run_id=? AND key LIKE 'plan_revision/journal/%'", (rid,), wire_index=1).fetchall()
    journals = [(name, json.loads(raw)) for name, raw in rows]
    matches = [(name, value) for name, value in journals if value.get("phase") == "activated"
               and value.get("plan") == plan and digest(value.get("previous_plan")) == before["plan"]]
    if len(matches) != 1:
        return None
    journal_key, journal = matches[0]
    previous = journal["previous_plan"]
    base = journal.get("base_revision")
    history_key = f"plan_revision/history/{base}"
    history = store.get(rid, history_key, {})
    prior_revision = {"version": 1, "revision": base}
    if (not isinstance(base, int) or revision != {"version": 1, "revision": base + 1}
            or digest(prior_revision) != before["plan_revision/current"]
            or history.get("plan") != previous or task.id in journal.get("affected", [])
            or task.id in journal.get("reopen", []) or task.model_dump() not in previous.get("tasks", [])
            or {k: v for k, v in previous.items() if k not in {"tasks", "document", "summary"}}
               != {k: v for k, v in plan.items() if k not in {"tasks", "document", "summary"}}):
        return None
    historical = history.get("tasks", {}).get("develop/task/" + task.id, {})
    if historical.get("recovery_permit") != key:
        return None
    chain = _baseline_chain(store, rid, task, saved, historical)
    frozen = _frozen_dispatch(store, rid, key, permit, previous)
    if (chain is None or frozen is None
            or not _historical_consumption(engine, rid, key, permit, previous, prior_revision)):
        return None
    raw = (journal.get("authority_reassessment") or journal).get("review", {})
    try:
        receipt = authority.receipt(rid, raw)
    except (AA2Error, KeyError, TypeError, ValueError):
        return None
    basis = receipt["basis"]
    accepted = store.get(rid, "authority/accepted/plan", {})
    if (not raw.get("approved") or basis.get("scope") != "plan_patch"
            or basis.get("source_identity") != before or basis.get("source_hash") != journal.get("source_hash")
            or basis.get("candidate_hash") != digest({k: journal[k] for k in ("plan", "patch", "previous_plan")})
            or accepted.get("artifact_hash") != digest(plan) or accepted.get("review_hash") != digest(raw)
            or accepted.get("review_key") != "authority/reviews/" + digest(raw)):
        return None
    archive = engine.artifacts.put({"historical_dispatch": frozen,
        "transition": {"journal": journal, "receipt": receipt, "task_history": historical},
        "retained_baselines": chain}, "task_authority_history")
    proof = {"version": 1, "previous_permit": key, "previous_permit_hash": digest(permit),
        "previous_sources": before, "current_sources": after, "history_artifact": archive,
        "workspace": saved["workspace"], "source_hash": saved["source_hash"],
        "transition": {"journal": journal_key, "journal_hash": digest(journal), "history": history_key,
                       "base_revision": base, "revision": base + 1, "review_hash": digest(raw)},
        "retained_baselines": [{"journal": row["journal"], "journal_hash": row["journal_hash"]} for row in chain],
        "retained_task": historical, "task": task.model_dump(),
        "meaning": "An unconsumed dispatch predates a formally reviewed plan amendment. Its task and global "
            "contracts are unchanged, and the retained checkpoint chain has no implementation candidate. "
            "The old permit remains historical and cannot authorize mutation. The complete original dispatch, "
            "review and baseline records are in history_artifact. Assess the remaining current task through "
            "normal recovery and obtain a fresh scoped permit. This is not "
            "a failed product test, permission expansion, acceptance, or a request to change the controller."}
    return FailureRecord.create(kind="contract", component="task_authority", origin_id=task.origin_id or task.id,
        task_id=task.id, step="authority_reassessment", obligations=task.requirement_ids,
        evidence_refs=["develop/task/" + task.id, key, journal_key, history_key],
        evidence={"task": task.model_dump(), "checks": [], "authority_reassessment": proof})
