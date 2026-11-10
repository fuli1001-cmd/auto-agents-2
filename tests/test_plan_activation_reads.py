"""Plan activation reads only its dependencies, preserving old snapshot semantics."""
from copy import deepcopy

import pytest
from plan_proposal_fixtures import freeze_legacy_author
from test_contract_activation_contexts import archive_legacy_inputs, cite_roles, prepare
from test_recovery_contracts import prepare_activation
from test_stage_contract_corrections import setup_correction

import auto_agents_2.store as store_module
from auto_agents_2.errors import AA2Error
from auto_agents_2.state_codec import checkpoint_rows_db, delete_query_db


def bounded_reads(monkeypatch, store, rid, *, excluded=()):
    unrelated = {"unrelated/frozen-request", "recovery/patch-context/other/17",
                 "invocation/recovery/patch/other/17", "plan_revision/history/old"}
    for key in unrelated:
        store.checkpoint(rid, key, {"retained": ["unrelated frozen input"] * 128})
    excluded = unrelated | set(excluded)
    original_get, original_query, original_put = (store_module.checkpoint_get_db,
                                                  store_module.checkpoint_query_db, store._put)
    reads, writes = [], []

    def get(db, run_id, key, *args, **kwargs):
        assert key not in excluded, "Activation decoded unrelated history"
        assert not writes, "Activation fetched a live value after its first write"
        reads.append(key)
        return original_get(db, run_id, key, *args, **kwargs)

    def query(db, sql, parameters=(), **kwargs):
        assert "AND key=?" in sql, "Activation expanded the complete run mapping"
        return original_query(db, sql, parameters, **kwargs)

    def put(db, run_id, key, value):
        writes.append(key)
        return original_put(db, run_id, key, value)

    monkeypatch.setattr(store_module, "checkpoint_get_db", get)
    monkeypatch.setattr(store_module, "checkpoint_query_db", query)
    monkeypatch.setattr(store, "_put", put)
    return reads, writes


def test_activation_freezes_complete_old_task_history_without_full_run_decode(tmp_path, monkeypatch):
    store, rid = prepare_activation(tmp_path)
    extra = {"develop/retained/old-task": {"candidate": "old evidence outside affected tasks"},
             "task_state/retired": {"revision": 7, "evidence": "retained"}}
    store.checkpoints(rid, extra)
    with store.connect() as db:
        expected = dict(checkpoint_rows_db(db, rid, prefix="develop/")
                        + checkpoint_rows_db(db, rid, prefix="task_state/"))
    with monkeypatch.context() as guarded:
        reads, writes = bounded_reads(guarded, store, rid)
        store.activate_plan_patch(rid, "patch")
    assert len(reads) == len(set(reads))
    assert {"plan", "plan_revision/current", "task_origins", "develop/done", *extra} <= set(reads)
    assert writes
    assert store.get(rid, "plan_revision/history/1")["tasks"] == expected
    assert store.get(rid, "develop/done") == ["T1", "T2"]
    # T4 was accepted in the OLD done list, even though the new list omits it.
    assert store.get(rid, "task_state/T4")["implementation"] == "accepted"
    assert store.get(rid, "develop/task/T4")["phase"] == "reverify"
    assert store.get(rid, "develop/task/T4")["previous_evidence"] == expected["develop/task/T4"]
    assert store.get(rid, "rounds/T3") == {"round": 3}


def test_activated_replay_does_not_read_task_history_or_charge_again(tmp_path, monkeypatch):
    store, rid = prepare_activation(tmp_path)
    store.activate_plan_patch(rid, "patch")
    before = store.get(rid, "plan_revision/history/1")
    with monkeypatch.context() as guarded:
        reads, writes = bounded_reads(guarded, store, rid, excluded={"plan", "develop/done", "task_origins"})
        store.activate_plan_patch(rid, "patch")
    assert not writes and not any(key.startswith(("develop/", "task_state/")) for key in reads)
    assert store.get(rid, "plan_revision/current")["revision"] == 2
    assert store.get(rid, "task_state/T4")["revision"] == 2
    assert store.get(rid, "plan_revision/history/1") == before


@pytest.mark.parametrize("change", ["plan", "revision"])
def test_bounded_activation_keeps_exact_compare_and_swap(tmp_path, monkeypatch, change):
    store, rid = prepare_activation(tmp_path)
    if change == "plan":
        store.checkpoint(rid, "plan", {**store.get(rid, "plan"), "document": "concurrent plan"})
    else:
        store.checkpoint(rid, "plan_revision/current", {"version": 1, "revision": 9})
    before = deepcopy(store.get(rid, "plan"))
    with monkeypatch.context() as guarded:
        reads, writes = bounded_reads(guarded, store, rid)
        with pytest.raises(AA2Error, match="CAS failed"):
            store.activate_plan_patch(rid, "patch")
    assert not writes
    assert store.get(rid, "plan") == before
    assert store.get(rid, "plan_revision/journal/patch")["phase"] == "prepared"
    assert store.get(rid, "plan_revision/history/1") is None


def test_failure_after_partial_activation_writes_rolls_back_old_snapshot(tmp_path, monkeypatch):
    store, rid = prepare_activation(tmp_path)
    old_plan, old_done = store.get(rid, "plan"), store.get(rid, "develop/done")
    original = store._put

    def fail(db, run_id, key, value):
        if key == "task_state/T4":
            raise RuntimeError("synthetic activation failure")
        return original(db, run_id, key, value)

    monkeypatch.setattr(store, "_put", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        store.activate_plan_patch(rid, "patch")
    assert store.get(rid, "plan") == old_plan
    assert store.get(rid, "develop/done") == old_done
    assert store.get(rid, "plan_revision/history/1") is None
    assert store.get(rid, "plan_revision/journal/patch")["phase"] == "prepared"
    assert store.get(rid, "rounds/T3") == {"round": 3}


async def test_legacy_v2_decodes_only_its_exact_presentation_prefixes(tmp_path, monkeypatch):
    workflow, rid, work, _, record, agent = setup_correction(tmp_path)
    await freeze_legacy_author(workflow, rid, record)
    cite_roles(agent, author=["plan.tasks[2].checks[0]", "recovery_decision"],
               reviewer=["patch.contract_corrections[0]", "recovery_decision", "controller_stage_contract"])
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    archive_legacy_inputs(workflow, rid, work, identity, journal)
    unrelated = {"presentation/recovery/patch/other/1/body", "presentation/recovery/patch-review/other/1/body",
                 "presentation/recovery/patch/" + identity + "0/body",
                 "presentation/recovery/patch-review/" + identity + "0/body"}
    for name in unrelated:
        workflow.store.checkpoint(rid, name, {"artifact": "not a valid presentation for this journal"})
    with monkeypatch.context() as guarded:
        reads, _ = bounded_reads(guarded, workflow.store, rid, excluded=unrelated)
        workflow.store.activate_plan_patch(rid, identity)
    presentations = [name for name in reads if name.startswith("presentation/")]
    assert presentations
    assert all(name.startswith(("presentation/recovery/patch/" + identity + "/",
                                "presentation/recovery/patch-review/" + identity + "/")) for name in presentations)
    assert workflow.store.get(rid, key)["phase"] == "activated"
    assert workflow.store.get(rid, "rounds/T3") == {"round": 17}
    assert [stage for stage, _ in agent.views] == ["plan", "review"]


async def test_activated_replay_still_rechecks_materialization_receipt(tmp_path, monkeypatch):
    workflow, rid, _, _, record, _ = setup_correction(tmp_path)
    identity, key, _ = await prepare(workflow, rid, record, monkeypatch)
    workflow.store.activate_plan_patch(rid, identity)
    required = "recovery/patch-materialization/" + identity
    workflow.store.checkpoint(rid, required, None)
    with monkeypatch.context() as guarded:
        reads, writes = bounded_reads(guarded, workflow.store, rid)
        with pytest.raises(AA2Error, match="Materialization journal differs"):
            workflow.store.activate_plan_patch(rid, identity)
    assert required in reads and not writes
    assert workflow.store.get(rid, key)["phase"] == "activated"
    assert workflow.store.get(rid, "plan_revision/current")["revision"] == 2
    assert workflow.store.get(rid, "rounds/T3") == {"round": 17}


@pytest.mark.parametrize("role", ["patch", "patch-review"])
async def test_legacy_missing_invocation_remains_expected_rejection(tmp_path, monkeypatch, role):
    workflow, rid, work, old, record, agent = setup_correction(tmp_path)
    await freeze_legacy_author(workflow, rid, record)
    cite_roles(agent, author=["plan.tasks[2].checks[0]"],
               reviewer=["patch.contract_corrections[0]", "controller_stage_contract"])
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    archive_legacy_inputs(workflow, rid, work, identity, journal)
    missing_key = "invocation/recovery/" + role + "/" + identity
    with workflow.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        delete_query_db(db, "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key=?",
                        (rid, missing_key))
    with monkeypatch.context() as guarded:
        reads, writes = bounded_reads(guarded, workflow.store, rid)
        with pytest.raises(AA2Error, match="completed invocation receipt"):
            workflow.store.activate_plan_patch(rid, identity)
    assert missing_key in reads and not writes
    assert workflow.store.get(rid, "plan") == old.model_dump()
    assert workflow.store.get(rid, key)["phase"] == "prepared"
