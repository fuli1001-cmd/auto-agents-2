"""Unsafe reboot sidecars must be rejected before the first SQLite open."""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from auto_agents_2 import process_reboot as recovery
from auto_agents_2.errors import PolicyViolation
from auto_agents_2.store import Store
from auto_agents_2.util import digest


@pytest.fixture
def state_scene(tmp_path):
    store = Store(tmp_path / "project")
    processes = store.root / "processes"
    processes.mkdir()
    record = processes / "process_3333333333333333.json"
    original = b'{"synthetic":"retain exact process bytes"}\n'
    record.write_bytes(original)
    request_path = tmp_path / "request.json"
    request_path.write_bytes(b'{"synthetic":"must not read before preflight"}\n')
    return SimpleNamespace(store=store, record=record, original=original, request_path=request_path)


def assert_retained(scene, database_bytes):
    assert scene.record.read_bytes() == scene.original
    assert scene.store.path.read_bytes() == database_bytes
    assert not (scene.store.root / "controller.lock").exists()
    assert not (scene.store.root / "process-reboot-recovery").exists()


class ForeignSidecarMetadata:
    """Change only this module's selected sidecar metadata, never global os."""

    def __init__(self, sidecar):
        self.sidecar = sidecar
        self.observations = []

    def __getattr__(self, name):
        return getattr(os, name)

    def stat(self, name, *, dir_fd=None, follow_symlinks=True):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        parent = self.sidecar.parent.stat()
        selected = (
            name == self.sidecar.name and dir_fd is not None
            and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino) == (parent.st_dev, parent.st_ino)
        )
        if selected:
            assert follow_symlinks is False
            self.observations.append(name)
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info


@pytest.mark.parametrize("entry", ["reconcile", "events"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("damage", ["symlink", "dangling_symlink", "directory", "fifo", "hardlink", "owner"])
def test_unsafe_sidecar_rejected_before_any_sqlite_open(state_scene, monkeypatch, entry, suffix, damage):
    scene = state_scene
    sidecar = scene.store.root / ("state.sqlite" + suffix)
    target = scene.store.root.parent / "sidecar-target"
    target_bytes = b"synthetic target must remain untouched\n"
    owner_view = None
    if damage in {"symlink", "hardlink"}:
        target.write_bytes(target_bytes)
    if damage in {"symlink", "dangling_symlink"}:
        sidecar.symlink_to(target)
    elif damage == "directory":
        sidecar.mkdir()
    elif damage == "fifo":
        os.mkfifo(sidecar)
    elif damage == "hardlink":
        os.link(target, sidecar)
    else:
        sidecar.write_bytes(target_bytes)
        owner_view = ForeignSidecarMetadata(sidecar)
        monkeypatch.setattr(recovery, "os", owner_view)

    before = sidecar.lstat()
    database_bytes = scene.store.path.read_bytes()
    sqlite_opens = []

    def must_not_open(*args, **kwargs):
        sqlite_opens.append((args, kwargs))
        raise AssertionError("SQLite opened before unsafe sidecar preflight")

    def must_not_read(*args, **kwargs):
        raise AssertionError("Request/evidence read before unsafe sidecar preflight")

    monkeypatch.setattr(
        recovery, "sqlite3", SimpleNamespace(connect=must_not_open, Error=sqlite3.Error, Row=sqlite3.Row)
    )
    monkeypatch.setattr(recovery, "owned_file", must_not_read)
    with pytest.raises(PolicyViolation, match="unsafe state database or sidecar"):
        if entry == "reconcile":
            recovery.reconcile(
                scene.store.root.parent, scene.request_path, "synthetic", confirm_local_host_reboot=True
            )
        else:
            with recovery.directory(scene.store.root) as state_fd:
                recovery.events(state_fd, {})

    assert sqlite_opens == []
    assert_retained(scene, database_bytes)
    after = sidecar.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_nlink
    )
    if damage in {"symlink", "dangling_symlink"}:
        assert sidecar.is_symlink() and sidecar.readlink() == target
    if damage == "dangling_symlink":
        assert not target.exists()
    elif damage in {"symlink", "hardlink"}:
        assert target.read_bytes() == target_bytes
    elif damage == "owner":
        assert owner_view.observations
        assert sidecar.read_bytes() == target_bytes


class RequestReached(AssertionError):
    pass


def stop_at_request(monkeypatch, scene):
    def reached(folder, name, **kwargs):
        assert name == scene.request_path.name
        raise RequestReached("First schema9 format read completed before request access")

    monkeypatch.setattr(recovery, "owned_file", reached)


def test_absent_sidecars_allow_format_preflight_without_lock_or_archive(state_scene, monkeypatch):
    scene = state_scene
    assert not any((scene.store.root / ("state.sqlite" + suffix)).exists() for suffix in ("-wal", "-shm", "-journal"))
    before = scene.store.path.read_bytes()
    stop_at_request(monkeypatch, scene)
    with pytest.raises(RequestReached):
        recovery.reconcile(
            scene.store.root.parent, scene.request_path, "synthetic", confirm_local_host_reboot=True
        )
    assert_retained(scene, before)


@pytest.mark.parametrize("entry", ["reconcile", "events"])
def test_owned_regular_wal_and_shm_remain_readable_without_business_writes(state_scene, monkeypatch, entry):
    scene = state_scene
    rid, aid = "run_" + "1" * 16, "attempt_" + "2" * 16
    rows = [
        {"seq": 10, "run_id": rid, "time": 10.0, "kind": "agent.started", "data": {"attempt": aid}},
        {"seq": 11, "run_id": rid, "time": 11.0, "kind": "agent.spawned", "data": {"attempt": aid, "pid": 12345}},
    ]
    request = {
        "started_event_seq": 10, "spawned_event_seq": 11, "run_id": rid, "attempt_id": aid,
        "events_sha256": digest(rows),
    }
    with scene.store.connect() as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO runs VALUES(?, 'PAUSED', 'review', 1, 2, '{}')", (rid,))
        for row in rows:
            writer.execute(
                "INSERT INTO events(seq,run_id,time,kind,data) VALUES(?,?,?,?,?)",
                (row["seq"], row["run_id"], row["time"], row["kind"], json.dumps(row["data"])),
            )
        writer.commit()
        paths = [scene.store.path, scene.store.root / "state.sqlite-wal", scene.store.root / "state.sqlite-shm"]
        assert all(path.is_file() and not path.is_symlink() for path in paths)
        identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in paths}
        before = {path: path.read_bytes() for path in paths[:2]}
        if entry == "reconcile":
            stop_at_request(monkeypatch, scene)
            with pytest.raises(RequestReached):
                recovery.reconcile(
                    scene.store.root.parent, scene.request_path, "synthetic", confirm_local_host_reboot=True
                )
        else:
            with recovery.directory(scene.store.root) as state_fd:
                original, parsed = recovery.events(state_fd, request)
            assert parsed == rows
            assert [row["seq"] for row in original] == [10, 11]
        assert {path: path.read_bytes() for path in before} == before
        assert {path: (path.stat().st_dev, path.stat().st_ino) for path in paths} == identities
        assert writer.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert_retained(scene, before[scene.store.path])
