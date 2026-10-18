"""Semantic recovery identities, independent of transport caches and presentation."""
from __future__ import annotations

from copy import deepcopy

from .util import digest


def action_contract(action):
    value = deepcopy(action)
    value.pop("reason", None)
    value.pop("evidence_refs", None)
    for name in ("task_ids", "budget_owners"):
        if name in value:
            value[name] = sorted(value[name])
    if "repair_targets" in value:
        value["repair_targets"] = sorted(({
            "task_id": row["task_id"], "paths": sorted(row["paths"])
        } for row in value["repair_targets"]), key=lambda row: row["task_id"])
    return value


def failure_contract(failure):
    value = deepcopy(failure)
    # These describe the view/version of an observation, not a new observation.
    for name in ("version", "phase", "observation_id", "reason_code", "evidence_refs"):
        value.pop(name, None)
    return value


def failure_identity(failure):
    return digest(failure_contract(failure))


def legacy_failure_records(failure):
    """Exact v1 encodings, used only to verify existing receipts before upgrading."""
    value = deepcopy(failure)
    value["version"] = 1
    value.pop("phase", None)
    value.pop("observation_id", None)
    variants = [value]
    if not value.get("reason_code"):
        variants.append({k: v for k, v in value.items() if k != "reason_code"})
    return variants


def authorization_binding(failure, choices, source, plan_hash, scope_hash):
    return {"version": 2, "source_hash": source, "plan_hash": plan_hash, "scope_hash": scope_hash,
            "candidates": digest({key: action_contract(value.model_dump()) for key, value in choices.items()}),
            "failure": failure_identity(failure.model_dump())}


def binding_matches_failure(binding, failure):
    if binding.get("version") == 2:
        return binding.get("failure") == failure_identity(failure)
    return binding.get("failure") in {digest(failure), *[digest(row) for row in legacy_failure_records(failure)]}


def invocation_contract(context):
    value = deepcopy(context)
    value.pop("evidence_reference_contract", None)
    if isinstance(value.get("failure"), dict):
        value["failure"] = failure_contract(value["failure"])
    return value
