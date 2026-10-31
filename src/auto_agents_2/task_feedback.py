"""Preserve the author's actual controller inputs for subsequent diagnosis."""
import copy
import re

from .faults import StateConflict
from .util import digest

FEEDBACK_FIELDS = ("phase", "checks", "review", "recording_review", "environment_error",
                   "environment_failure", "candidate_error", "controller_recheck",
                   "controller_approval_recheck", "controller_recovery", "upstream_recovery",
                   "failure", "baseline_refresh")


def selected_feedback(saved):
    previous = saved.get("feedback", saved)
    return copy.deepcopy({key: previous[key] for key in FEEDBACK_FIELDS if key in previous})


def author_input(engine, rid, task, saved, feedback, *, capture):
    """Snapshot input facts; recorded feedback claims never become verified product facts."""
    binding = {"run_id": rid, "operation": "develop/task/" + task.id,
               "task_id": task.id, "origin_id": task.origin_id or task.id,
               "task_hash": digest(task.model_dump()), "source_hash": saved.get("source_hash"),
               "observation_id": copy.deepcopy(saved.get("observation_id")),
               "feedback_hash": digest(feedback)}
    result = {"version": 1, "capture": capture, "binding": binding,
              "feedback": copy.deepcopy(feedback)}
    refresh = feedback.get("baseline_refresh")
    if not isinstance(refresh, dict):
        return result
    ref = refresh.get("recovery_ref", "")
    prefix = f"refs/aa2/{rid}/task-baselines/{task.id}/"
    if not isinstance(ref, str) or not ref.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{16}", ref[len(prefix):]):
        # Legacy inputs without a controller journal must not invent one.
        result["baseline_journal"] = {"status": "unavailable", "reason": "No matching controller journal reference"}
        return result
    key = f"recovery/task-baselines/{task.id}/{ref[len(prefix):]}"
    record = engine.store.get(rid, key)
    if record is None:
        result["baseline_journal"] = {"status": "unavailable", "checkpoint": key}
        return result
    if (record.get("recovery_ref") != ref or record.get("base") != refresh.get("incoming_base")
            or saved.get("workspace") not in {record.get("workspace"), record.get("repair_workspace")}):
        raise StateConflict("Author feedback baseline journal does not match its task workspace and dependency input")
    # No recursive previous_task/candidate prose or unselected private paths.
    journal = {name: copy.deepcopy(record[name]) for name in
               ("phase", "base", "snapshot", "source_hash", "recovery_ref", "merged_commit", "retained_candidate")
               if name in record}
    previous = record.get("previous_task", {})
    journal.update(status="recorded", checkpoint=key, record_hash=digest(record),
                   previous_phase=previous.get("phase"),
                   previous_implementation_proposal_present=bool(previous.get("implementation")),
                   interpretation="This journal records preservation and dependency refresh, not implementation completeness or acceptance. "
                   "Proposal presence describes checkpoint metadata only; inspect the bound source history and current files.")
    result["baseline_journal"] = journal
    return result


def retained_author_input(rid, task, saved):
    # Only the controller's explicit retained-candidate chain, never feedback or
    # model-provided paths/hashes. Reverification may wrap a previously accepted task.
    current, seen = saved, set()
    while isinstance(current, dict) and id(current) not in seen:
        seen.add(id(current))
        record = current.get("author_input")
        if record is not None:
            binding = record.get("binding", {})
            if (binding.get("run_id") != rid or binding.get("origin_id") != (task.origin_id or task.id)
                    or binding.get("feedback_hash") != digest(record.get("feedback"))):
                raise StateConflict("Retained author input binding does not match its task owner and feedback")
            return copy.deepcopy(record)
        current = current.get("previous_evidence")
    return None


def failure_input(engine, rid, task, saved):
    record = retained_author_input(rid, task, saved)
    if record is None and saved.get("feedback"):
        record = author_input(engine, rid, task, saved, selected_feedback(saved), capture="legacy_saved_task")
    return {"author_input": copy.deepcopy(record)} if record is not None else {}
