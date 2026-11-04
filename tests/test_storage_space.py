"""Real-volume observation and existing prerequisite routing, without real disk pressure."""
import asyncio
import base64
import json
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime import configured

from auto_agents_2 import storage_space
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.faults import ResourceBoundary
from auto_agents_2.util import write_json

GIB = 1024 ** 3
VOLUME = r"\\?\Volume{12345678-abcd-1234-abcd-123456789abc}" + "\\"


def observation(*, host=20 * GIB, error=None):
    return {
        "version": 1, "observed_at": 123.0, "platform": "wsl",
        "volumes": [
            {"scope": "filesystem", "name": "filesystem", "free": 50 * GIB, "total": 100 * GIB},
            {"scope": "wsl_backing", "name": "host:E", "free": host, "total": 100 * GIB},
        ],
        "errors": [] if error is None else [{"scope": "wsl_backing", "error_type": error}],
    }


def fake_platform(monkeypatch, *, system="Linux", release="6.6.0-generic", distribution=None, interop=None):
    monkeypatch.setattr(storage_space.platform, "system", lambda: system)
    monkeypatch.setattr(storage_space.platform, "release", lambda: release)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    if distribution is not None:
        monkeypatch.setenv("WSL_DISTRO_NAME", distribution)
    if interop is not None:
        monkeypatch.setenv("WSL_INTEROP", interop)
    monkeypatch.setattr(storage_space.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=50 * GIB, total=100 * GIB))


@pytest.mark.parametrize("system", ["Linux", "Windows", "Darwin"])
def test_native_host_uses_actual_filesystem_without_inventing_wsl_volume(tmp_path, monkeypatch, system):
    fake_platform(monkeypatch, system=system)
    monkeypatch.setattr(storage_space, "windows_backing_volume",
                        lambda _: pytest.fail("Non-WSL platform must not inspect a WSL registry"))
    report = storage_space.observe(tmp_path)
    assert report["platform"] == system and not report["errors"]
    assert [row["scope"] for row in report["volumes"]] == ["filesystem"]
    assert storage_space.boundary(report, 5 * GIB, "startup") is None


@pytest.mark.parametrize("signal", ["kernel", "distribution", "interop"])
def test_every_wsl_signal_requires_verified_host_volume(tmp_path, monkeypatch, signal):
    fake_platform(monkeypatch, release="6.6-microsoft-standard" if signal == "kernel" else "custom-linux",
                  distribution="Fixture-Distro" if signal != "interop" else None,
                  interop="/run/fixture-interop" if signal == "interop" else None)
    called = []

    def host(distribution):
        called.append(distribution)
        raise ValueError("EXAMPLE_PLACEHOLDER host failure must not leak native stderr")

    monkeypatch.setattr(storage_space, "windows_backing_volume", host)
    report = storage_space.observe(tmp_path)
    assert called and report["platform"] == "wsl"
    assert report["errors"] == [{"scope": "wsl_backing", "error_type": "ValueError"}]
    error = storage_space.boundary(report, 5 * GIB, "provider")
    assert isinstance(error, ResourceBoundary)
    assert error.requirement.kind == "resource_limit"
    assert "EXAMPLE_PLACEHOLDER" not in json.dumps(error.requirement.model_dump())


def test_guest_free_space_does_not_override_actual_host_floor():
    report = observation(host=4 * GIB)
    error = storage_space.boundary(report, 5 * GIB, "verification")
    assert isinstance(error, ResourceBoundary) and error.phase == "verification"
    assert "host:E" in str(error) and "host:D" not in str(error)
    assert error.requirement.evidence["minimum_free_bytes"] == 5 * GIB
    assert error.requirement.evidence["volumes"][0]["free"] == 50 * GIB
    assert storage_space.boundary(observation(host=5 * GIB), 5 * GIB, "verification") is None


def test_unavailable_filesystem_is_a_normal_resource_boundary(tmp_path, monkeypatch):
    fake_platform(monkeypatch)
    def missing(_):
        raise OSError("EXAMPLE_PLACEHOLDER unavailable mount")
    monkeypatch.setattr(storage_space.shutil, "disk_usage", missing)
    report = storage_space.observe(tmp_path)
    assert report["volumes"] == [] and report["errors"][0]["scope"] == "filesystem"
    assert isinstance(storage_space.boundary(report, 5 * GIB, "startup"), ResourceBoundary)


def volume_response(distribution="Fixture-Distro"):
    return {"version": 1, "distribution": distribution,
            "backing_path": r"E:\isolated\Fixture-Distro\ext4.vhdx",
            "volume_id": VOLUME, "name": "host:E", "free": 20 * GIB, "total": 100 * GIB}


def test_windows_probe_is_fixed_local_bounded_and_never_interpolates_distribution(monkeypatch):
    distribution = "Fixture-Distro'; throw 'EXAMPLE_PLACEHOLDER"
    calls = []
    executable = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    monkeypatch.setattr(storage_space, "_powershell", lambda: executable)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=json.dumps(volume_response(distribution)))

    monkeypatch.setattr(storage_space.subprocess, "run", run)
    result = storage_space.windows_backing_volume(distribution)
    argv, kwargs = calls[0]
    assert argv[:4] == [executable, "-NoProfile", "-NonInteractive", "-Command"]
    assert kwargs["timeout"] == 20 and kwargs["check"] and kwargs["capture_output"]
    assert distribution not in argv[4]
    encoded = re.search(r"FromBase64String\('([^']+)'\)", argv[4]).group(1)
    assert json.loads(base64.b64decode(encoded)) == {"distribution": distribution}
    assert "Get-Volume -FilePath $item.FullName" in argv[4]
    assert "$env:PSModulePath = Join-Path $PSHOME 'Modules'" in argv[4]
    assert "ReparsePoint" in argv[4] and "-ceq $request.distribution" in argv[4]
    assert result["scope"] == "wsl_backing" and result["volume_id"] == VOLUME


@pytest.mark.parametrize("mutation", [
    {"distribution": "Other-Distro"}, {"volume_id": "unverified"},
    {"backing_path": r"\\remote\share\ext4.vhdx"}, {"name": "host:guessed"},
    {"free": True}, {"free": -1}, {"free": 101 * GIB}, {"version": 2},
])
def test_invalid_host_identity_or_measurement_cannot_authorize_dispatch(monkeypatch, mutation):
    monkeypatch.setattr(storage_space, "_powershell", lambda: "/fixed/system/powershell.exe")
    monkeypatch.setattr(storage_space.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(stdout=json.dumps({**volume_response(), **mutation})))
    with pytest.raises(ValueError):
        storage_space.windows_backing_volume("Fixture-Distro")


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("fixed-local-query", 20),
    subprocess.CalledProcessError(1, "fixed-local-query", stderr="EXAMPLE_PLACEHOLDER private host output"),
    OSError("fixed-local-query unavailable"),
])
def test_probe_failure_keeps_safe_error_category_and_blocks(tmp_path, monkeypatch, error):
    fake_platform(monkeypatch, distribution="Fixture-Distro")
    def fail(_):
        raise error
    monkeypatch.setattr(storage_space, "windows_backing_volume", fail)
    report = storage_space.observe(tmp_path)
    assert report["errors"] == [{"scope": "wsl_backing", "error_type": type(error).__name__}]
    assert "EXAMPLE_PLACEHOLDER" not in json.dumps(report)
    assert isinstance(storage_space.boundary(report, 5 * GIB, "startup"), ResourceBoundary)


@pytest.mark.parametrize("candidate", [None, "/tmp/powershell.exe", "/tmp/Windows/System32/WindowsPowerShell/v1.0/linked.exe"])
def test_probe_refuses_unverified_executable_location(monkeypatch, candidate):
    monkeypatch.setattr(storage_space.shutil, "which", lambda _: candidate)
    with pytest.raises((ValueError, OSError)):
        storage_space._powershell()


async def test_probe_does_not_block_loop_and_cancellation_joins_worker(tmp_path, monkeypatch):
    entered, finish, left = threading.Event(), threading.Event(), threading.Event()

    def probe(_):
        entered.set()
        try:
            assert finish.wait(5)
            return observation()
        finally:
            left.set()

    monkeypatch.setattr(storage_space, "observe", probe)
    monitor = storage_space.StorageMonitor(tmp_path)
    task = asyncio.create_task(monitor.sample())
    try:
        async def await_entered():
            while not entered.is_set():
                await asyncio.sleep(0)
        await asyncio.wait_for(await_entered(), 2)
        # The event loop remains usable while the real worker waits.
        await asyncio.wait_for(asyncio.sleep(0), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not left.is_set()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert left.is_set() and monitor._report is None


async def test_probe_singleflight_and_short_cache_do_not_start_parallel_children(tmp_path, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def probe(_):
        calls.append(1)
        entered.set()
        assert finish.wait(5)
        return observation()

    monkeypatch.setattr(storage_space, "observe", probe)
    monitor = storage_space.StorageMonitor(tmp_path)
    first = asyncio.create_task(monitor.sample())
    second = asyncio.create_task(monitor.sample())
    try:
        async def await_entered():
            while not entered.is_set():
                await asyncio.sleep(0)
        await asyncio.wait_for(await_entered(), 2)
        assert len(calls) == 1
    finally:
        finish.set()
    assert await first == await second
    assert await monitor.sample() == observation()
    assert len(calls) == 1
    await monitor.sample(force=True)
    assert len(calls) == 2


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("gateway", ["checks", "batch"])
async def test_existing_check_gate_reobserves_space_and_preserves_authority(tmp_path, monkeypatch, unavailable, gateway):
    config, store = configured(tmp_path)
    settings = config.load().model_dump(mode="json")
    settings["cleanup"]["min_free_bytes"] = 5 * GIB
    write_json(config.path, settings)
    workflow = WorkflowEngine(tmp_path, agent=SimpleNamespace())
    rid = store.admit("storage prerequisite fixture", {})["id"]
    store.checkpoints(rid, {
        "budget/original": {"calls": 7, "replans": 27},
        "prototype_approval": {"approved": True, "hash": "original"},
        "verification/original": {"passed": True, "source": "original"},
    })
    keys = ["budget/original", "prototype_approval", "verification/original"]
    before = {key: store.get(rid, key) for key in keys}
    report = observation(host=4 * GIB, error="ValueError" if unavailable else None)
    probes, dispatches, gc = [], [], []

    async def sample(**kwargs):
        probes.append(kwargs)
        return report

    async def verify(*args):
        dispatches.append(args)
        return [{"passed": True}]

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.resources, "collect", lambda: gc.append("collected"))
    monkeypatch.setattr(workflow.verifier, "run", verify)
    monkeypatch.setattr(workflow, "_verification_operation", lambda *a, **kw: "fixture-operation")
    run = workflow._checks if gateway == "checks" else workflow._run_checks
    for _ in range(2):
        with pytest.raises(ResourceBoundary):
            await run(rid, [], tmp_path, None)
    assert not dispatches
    assert len(gc) == (0 if unavailable else 2)
    assert {key: store.get(rid, key) for key in keys} == before
    report = observation()
    assert await run(rid, [], tmp_path, None) == [{"passed": True}]
    assert len(dispatches) == 1 and len(probes) == (3 if unavailable else 5)
    assert store.get(rid, "resources/storage")["minimum_free_bytes"] == 5 * GIB
    assert {key: store.get(rid, key) for key in keys} == before


async def test_active_observation_warns_without_cancelling_or_collecting(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    settings = config.load().model_dump(mode="json")
    settings["cleanup"]["min_free_bytes"] = 5 * GIB
    write_json(config.path, settings)
    workflow = WorkflowEngine(tmp_path, agent=SimpleNamespace())
    rid = store.admit("active storage warning fixture", {})["id"]

    async def sample(**kwargs):
        return observation(host=4 * GIB)

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow.resources, "collect", lambda: pytest.fail("Observation cannot run collection"))
    assert (await workflow._storage_observation(rid))["volumes"][1]["free"] == 4 * GIB
    with store.connect() as db:
        rows = list(db.execute("SELECT kind FROM events WHERE run_id=? AND kind LIKE 'resources.space_%'", (rid,)))
    assert [row[0] for row in rows] == ["resources.space_low"]
    assert not store.pending(rid)


@pytest.mark.parametrize("mounts,allowed", [
    ("C:\\134 /mnt/c 9p rw,aname=drvfs;path=C:\\134 0 0\n", True),
    ("E:\\134 /mnt/c drvfs rw 0 0\n", True),
    ("none /mnt/c tmpfs rw 0 0\n", False),
    ("C:\\134 /mnt/c 9p rw,aname=drvfs;path=C:\\134 0 0\nnone /mnt/c/Windows tmpfs rw 0 0\n", False),
    ("unverified /mnt/c 9p rw,aname=drvfs 0 0\n", False),
])
def test_system_executable_requires_actual_windows_mount(monkeypatch, mounts, allowed):
    original = Path.read_text
    def read(path, *args, **kwargs):
        return mounts if path == Path("/proc/mounts") else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    executable = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if allowed:
        storage_space._windows_mount(executable)
    else:
        with pytest.raises(ValueError):
            storage_space._windows_mount(executable)
    with pytest.raises(ValueError):
        storage_space._windows_mount(Path("/tmp/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))


@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("kind", ["resources.space_low", "resources.space_unknown"])
def test_storage_notice_is_visible_and_omits_native_details(tty, kind):
    from io import StringIO
    from rich.console import Console
    from auto_agents_2.display import ProgressDisplay

    output = StringIO()
    display = ProgressDisplay(Console(file=output, width=300), tty)
    display.event({"kind": "activity.started", "time": 100,
                   "data": {"activity": "active", "label": "正在执行", "total": 5}})
    before = dict(display.steps)
    payload = observation(host=4 * GIB)
    payload["minimum_free_bytes"] = 5 * GIB
    payload["volumes"][1].update(backing_path=r"EXAMPLE_PRIVATE_REGISTRY_PATH",
                                volume_id="EXAMPLE_PRIVATE_VOLUME_GUID")
    payload["errors"] = [{"scope": "wsl_backing", "detail": "EXAMPLE_PRIVATE_NATIVE_STDERR"}]
    payload["volumes"].append({"name": "EXAMPLE_PRIVATE_UNVERIFIED_LABEL", "free": 0})
    display.event({"kind": kind, "time": 101, "data": payload})
    result = output.getvalue()
    assert "存储空间告警" in result and "host:E 剩余 4.00 GiB" in result
    assert "最低 5.00 GiB" in result and "新工作派发前重新核验" in result
    assert "EXAMPLE_PRIVATE" not in result
    assert display.steps == before
    display.close()


async def test_space_lost_while_awaiting_retry_answer_prevents_second_provider_call(tmp_path, monkeypatch):
    from auto_agents_2.errors import ProviderError
    from auto_agents_2.models import DocumentResult

    config, store = configured(tmp_path)
    settings = config.load().model_dump(mode="json")
    settings["cleanup"]["min_free_bytes"] = 5 * GIB
    write_json(config.path, settings)
    calls, samples = [], []
    report = observation()

    class Agent:
        async def invoke(self, *args):
            calls.append(args)
            raise ProviderError("synthetic transport unavailable")

    workflow = WorkflowEngine(tmp_path, agent=Agent())
    rid = store.admit("retry space fixture", {})["id"]
    store.checkpoint(rid, "budget/original", {"calls": 7, "replans": 27})

    async def sample(**kwargs):
        samples.append(kwargs)
        return report

    async def answered(*args, **kwargs):
        nonlocal report
        report = observation(host=4 * GIB)
        return "retry"

    monkeypatch.setattr(workflow.storage, "sample", sample)
    monkeypatch.setattr(workflow, "_ask", answered)
    monkeypatch.setattr(workflow.resources, "collect", lambda: {"removed": 0})
    with pytest.raises(ResourceBoundary):
        await workflow._invoke_once("readme", {}, DocumentResult, tmp_path, rid, "retry-space")
    assert len(calls) == 1
    assert len(samples) >= 2 and all(row["force"] for row in samples)
    assert store.get(rid, "budget/original") == {"calls": 7, "replans": 27}
    assert store.get(rid, "invocation/retry-space") is None
