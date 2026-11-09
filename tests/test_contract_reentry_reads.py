"""Routine reentry must not decode unrelated checkpoint histories."""
from copy import deepcopy

import pytest
from test_contract_reentry import accept, fixture, review_for, rollback
from test_decisions import setup

import auto_agents_2.store as store_module
from auto_agents_2.contract_reentry import ACTIVE, state
from auto_agents_2.faults import StateConflict


def bounded_reads(monkeypatch, store, rid, *, corrupt=None):
    unrelated = "unrelated/frozen-request"
    store.checkpoint(rid, unrelated, {"materials": ["synthetic retained evidence"] * 32})
    original = store_module.checkpoint_get_db
    original_query = store_module.checkpoint_query_db
    reads = []

    def get(db, run_id, key, *args, **kwargs):
        reads.append(key)
        assert key != unrelated, "Routine reentry decoded an unrelated frozen request"
        if corrupt and key == corrupt[0]:
            return deepcopy(corrupt[1])
        return original(db, run_id, key, *args, **kwargs)

    def no_scan(db, sql, parameters=(), **kwargs):
        if "AND key=?" not in sql:
            pytest.fail("Routine reentry materialized the full run checkpoint mapping")
        return original_query(db, sql, parameters, **kwargs)

    monkeypatch.setattr(store_module, "checkpoint_get_db", get)
    monkeypatch.setattr(store_module, "checkpoint_query_db", no_scan)
    return reads


@pytest.mark.parametrize("current_plan", [{"tasks": []}, {}, False, None])
def test_ordinary_reentry_uses_only_admission_slots(tmp_path, monkeypatch, current_plan):
    workflow, rid = setup(tmp_path)
    store = workflow.store
    store.checkpoints(rid, {"plan": current_plan, "rounds/T1": {"round": 41},
        "replan_feedback": None if current_plan is None else {"reason": "retained feedback"}})
    reads = bounded_reads(monkeypatch, store, rid)
    store.ensure_contract_reentry(rid)
    assert reads == [ACTIVE, "plan"] + (["replan_feedback"] if current_plan is None else [])
    assert store.get(rid, "rounds/T1") == {"round": 41}


def active_successor(tmp_path):
    workflow, rid, old = fixture(tmp_path)
    document = deepcopy(state(lambda key: workflow.store.get(rid, key))["specification"])
    context, review = review_for(workflow, rid, "clarify", document)
    accept(workflow, rid, "clarify", document, context, review)
    # ACTIVE must still be validated even if a plan slot is present.
    workflow.store.checkpoint(rid, "plan", old)
    binding = workflow.store.get(rid, ACTIVE)
    assert binding["head"]
    return workflow, rid, binding


def test_active_reentry_validates_exact_chain_without_full_run_scan(tmp_path, monkeypatch):
    workflow, rid, binding = active_successor(tmp_path)
    store = workflow.store
    baseline = store.get(rid, binding["base"])
    head = store.get(rid, binding["head"])
    reads = bounded_reads(monkeypatch, store, rid)
    store.ensure_contract_reentry(rid)
    assert reads[0] == ACTIVE
    assert {binding["base"], baseline["history_key"], binding["head"], head["review_key"]} <= set(reads)
    assert store.get(rid, ACTIVE) == binding
    assert store.get(rid, "rounds/T1") == {"round": 9}
    assert store.run(rid)["data"]["replans"] == 27


@pytest.mark.parametrize("damage", ["missing_base", "tampered_base", "missing_history",
    "tampered_history", "missing_head", "tampered_review"])
def test_active_reentry_cannot_skip_damaged_sources_when_plan_exists(tmp_path, monkeypatch, damage):
    workflow, rid, binding = active_successor(tmp_path)
    store = workflow.store
    baseline = store.get(rid, binding["base"])
    if damage in {"missing_base", "tampered_base"}:
        key = binding["base"]
        bad = None if damage == "missing_base" else {**baseline, "version": 999}
    elif damage in {"missing_history", "tampered_history"}:
        key = baseline["history_key"]
        bad = None if damage == "missing_history" else {"changed": True}
    elif damage == "missing_head":
        key, bad = binding["head"], None
    else:
        head = store.get(rid, binding["head"])
        key = head["review_key"]
        bad = {**store.get(rid, key), "review_hash": "0" * 64}
    reads = bounded_reads(monkeypatch, store, rid, corrupt=(key, bad))
    with pytest.raises(StateConflict):
        store.ensure_contract_reentry(rid)
    assert key in reads  # A plan shortcut without real chain validation must fail.
    assert store.get(rid, ACTIVE) == binding
    assert store.get(rid, "rounds/T1") == {"round": 9}


@pytest.mark.parametrize("history_gap", [False, True])
def test_true_legacy_reentry_keeps_strict_history_migration(tmp_path, monkeypatch, history_gap):
    workflow, rid, old = fixture(tmp_path, "design")
    rollback(workflow, rid, "plan")
    store = workflow.store
    store.checkpoint(rid, ACTIVE, None)
    original = store_module.checkpoint_query_db
    scans = []

    def history_rows(db, sql, parameters=(), **kwargs):
        result = original(db, sql, parameters, **kwargs)
        if "AND key=?" in sql:
            return result
        scans.append(sql)
        return (row for row in result
            if not (history_gap and row[0] == "recovery/gate-history/27"))

    monkeypatch.setattr(store_module, "checkpoint_query_db", history_rows)
    if history_gap:
        with pytest.raises(StateConflict, match="incomplete generation chain"):
            store.ensure_contract_reentry(rid)
        assert store.get(rid, ACTIVE) is None
    else:
        store.ensure_contract_reentry(rid)
        migrated = state(lambda key: store.get(rid, key))
        assert migrated["plan"] == old
        assert migrated["base"]["generation"] == 26
        assert set(migrated["base"]["legacy_history"]) == {
            "recovery/gate-history/26", "recovery/gate-history/27"}
        assert store.get(rid, "plan") is None  # Protection is not execution approval.
    assert len(scans) == 1
    assert store.get(rid, "rounds/T1") == {"round": 9}
    assert store.run(rid)["data"]["replans"] == 28
