"""Checkpoint history can be large without needing every body resident at once."""

import json
import resource
import sqlite3
import tracemalloc
from contextlib import closing

import pytest

from auto_agents_2 import artifact_gc
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.store import Store


def setup(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("synthetic checkpoint collection", {})["id"]
    return store, rid, ArtifactStore(store.root)


def peak_collection(store):
    # Measure allocations made by collection, excluding fixture construction.
    # The native SQLite cache and the live artifact graph have separate costs.
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    try:
        artifact_gc.collect_artifacts(store)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_checkpoint_body_peak_memory_stays_bounded_as_history_grows(tmp_path, record_property):
    store, rid, artifacts = setup(tmp_path)
    log = artifacts.put("synthetic current diagnostic", "check_log")
    report = artifacts.put("<testsuite tests='1'/>", "junit")
    obsolete = artifacts.put("synthetic obsolete diagnostic", "check_log")
    body_bytes = 256 * 1024
    body = json.dumps({"padding": "x" * body_bytes, "logs": [log], "report": report})

    def append_history(start, stop):
        with store.connect() as db:
            db.executemany("INSERT INTO checkpoints VALUES(?,?,?)",
                           ((rid, f"history/{index:04d}", body) for index in range(start, stop)))
            # Model already published legacy checkpoints, without producer holds
            # that could mask a missing checkpoint root.
            db.execute("DELETE FROM diagnostic_holds")

    append_history(0, 8)
    small_peak = peak_collection(store)
    small_rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    append_history(8, 96)
    large_peak = peak_collection(store)
    large_rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    record_property("checkpoint_body_bytes", body_bytes)
    record_property("small_history_rows", 8)
    record_property("large_history_rows", 96)
    record_property("small_gc_peak_bytes", small_peak)
    record_property("large_gc_peak_bytes", large_peak)
    record_property("small_process_rss_peak_bytes", small_rss_peak)
    record_property("large_process_rss_peak_bytes", large_rss_peak)

    # A further 22 MiB of historical JSON must not stay resident during marking.
    # Leave ample room for decoder/current-row overlap and allocator variation.
    assert large_peak < small_peak + 4 * body_bytes, (small_peak, large_peak)
    assert large_peak < 8 * 1024 * 1024, large_peak
    assert artifacts.read(log) == "synthetic current diagnostic"
    assert artifacts.read(report) == "<testsuite tests='1'/>"
    assert artifacts.released(obsolete)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM checkpoints WHERE key LIKE 'history/%'").fetchone()[0] == 96
        assert {row[0] for row in db.execute(
            "SELECT data FROM checkpoints WHERE key LIKE 'history/%'")} == {body}


def test_metadata_writes_during_scan_keep_every_root_and_the_writer_lock(tmp_path, monkeypatch):
    store, rid, artifacts = setup(tmp_path)
    logs = [artifacts.put(f"synthetic retained root {index}", "check_log") for index in range(20)]
    values = {f"gc-root/{index:02d}": {"gc-root": index, "logs": [ref]} for index, ref in enumerate(logs)}
    store.checkpoints(rid, values)
    with store.connect() as db:
        # Legacy roots supply kind metadata during marking, exercising writes on
        # the same connection while its checkpoint cursor is still being read.
        db.execute("DELETE FROM artifact_kinds")
        assert not db.execute("SELECT 1 FROM diagnostic_holds").fetchone()
    original = json.loads
    observed = []

    def inspect_root(raw, *args, **kwargs):
        value = original(raw, *args, **kwargs)
        if isinstance(value, dict) and "gc-root" in value:
            observed.append(value["gc-root"])
            with closing(sqlite3.connect(store.path, timeout=0)) as writer:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute("BEGIN IMMEDIATE")
        return value

    with monkeypatch.context() as patch:
        patch.setattr(artifact_gc.json, "loads", inspect_root)
        assert artifact_gc.collect_artifacts(store) == {"removed": 0, "bytes": 0}
    assert sorted(observed) == list(range(20))
    assert all(artifacts.read(ref) == f"synthetic retained root {index}" for index, ref in enumerate(logs))
    assert all(store.get(rid, key) == value for key, value in values.items())
    with store.connect() as db:
        assert {tuple(row) for row in db.execute("SELECT hash,kind FROM artifact_kinds")} == {
            (ref["hash"], "check_log") for ref in logs}
        assert not db.execute("SELECT 1 FROM retired_diagnostics").fetchone()
    # The read cursor and transaction must both be released after collection.
    store.checkpoint(rid, "after-collection", {"writer": "available"})


def test_mid_scan_failure_rolls_back_metadata_and_keeps_artifacts(tmp_path, monkeypatch):
    store, rid, artifacts = setup(tmp_path)
    logs = [artifacts.put(f"synthetic terminal root {index}", "check_log") for index in range(2)]
    with store.connect() as db:
        db.executemany("INSERT INTO checkpoints VALUES(?,?,?)", (
            (rid, f"gc-root/{index}", json.dumps({"gc-root": index, "logs": [ref]}))
            for index, ref in enumerate(logs)))
        db.execute("DELETE FROM artifact_kinds")
        before_holds = [tuple(row) for row in db.execute("SELECT * FROM diagnostic_holds ORDER BY id")]
    store.update(rid, status="SUCCEEDED")
    original, observed = json.loads, []

    def fail_second_root(raw, *args, **kwargs):
        value = original(raw, *args, **kwargs)
        if isinstance(value, dict) and "gc-root" in value:
            observed.append(value["gc-root"])
            if len(observed) == 2:
                raise ValueError("injected checkpoint decode failure")
        return value

    with monkeypatch.context() as patch:
        patch.setattr(artifact_gc.json, "loads", fail_second_root)
        with pytest.raises(ValueError, match="injected checkpoint decode failure"):
            artifact_gc.collect_artifacts(store)
    assert len(observed) == 2
    assert all(artifacts.read(ref) == f"synthetic terminal root {index}" for index, ref in enumerate(logs))
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM diagnostic_holds ORDER BY id")] == before_holds
        assert not db.execute("SELECT 1 FROM artifact_kinds").fetchone()
        assert not db.execute("SELECT 1 FROM retired_diagnostics").fetchone()
    # A failed scan cannot leave a cursor/write lock behind or turn an unknown
    # body into success. A subsequent ordinary collection can finish retirement.
    store.checkpoint(rid, "after-failure", {"writer": "available"})
    result = artifact_gc.collect_artifacts(store)
    assert result["removed"] == 2
    assert all(artifacts.released(ref) for ref in logs)
