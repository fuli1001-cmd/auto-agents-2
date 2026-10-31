import gc
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import pytest_support as support


def readonly_tree(root):
    root.mkdir()
    (root / "empty").mkdir()  # Empty directory created before any regular file.
    (root / "payload").write_text("tiny fixture")
    (root / "payload").chmod(0o444)
    (root / "empty").chmod(0o555)
    root.chmod(0o555)


def test_readonly_empty_first_and_external_links(tmp_path):
    outside = tmp_path / "outside"
    readonly_tree(outside)
    tree = tmp_path / "private"
    readonly_tree(tree)
    tree.chmod(0o755)
    os.link(outside / "payload", tree / "hardlink")
    (tree / "link").symlink_to(outside, target_is_directory=True)
    tree.chmod(0o555)
    before = {path.name: (path.stat().st_mode, path.stat().st_ino) for path in outside.iterdir()}
    parent = support.directory(tmp_path)
    try:
        support.remove_directory(parent, tree.name, tmp_path.stat().st_dev)
    finally:
        os.close(parent)
    assert not tree.exists()
    assert outside.stat().st_mode & 0o777 == 0o555
    assert before == {path.name: (path.stat().st_mode, path.stat().st_ino) for path in outside.iterdir()}
    assert (outside / "payload").read_text() == "tiny fixture"


@pytest.mark.parametrize("kind", ["nonempty", "symlink_parent", "marker_symlink", "lock_hardlink"])
def test_unregistered_or_linked_metadata_is_not_adopted(tmp_path, kind):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    asset = outside / "asset"
    asset.write_text("do not modify")
    if kind == "symlink_parent":
        link = tmp_path / "alias"
        link.symlink_to(outside, target_is_directory=True)
        root = link / "store"
    else:
        root.mkdir(mode=0o700)
        if kind == "nonempty":
            (root / "user-file").write_text("user asset")
        elif kind == "marker_symlink":
            (root / "store.json").symlink_to(asset)
        else:
            os.link(asset, root / ".lock")
    with pytest.raises((support.CleanupError, OSError)):
        support.Store(root, tmp_path)
    assert asset.read_text() == "do not modify"
    assert stat.S_IMODE(asset.stat().st_mode) == 0o644
    if kind == "symlink_parent":
        assert not (outside / "store").exists()


def test_parent_replacement_never_chmods_external_target(tmp_path):
    store = support.Store(tmp_path / "store", tmp_path)
    work = store.start()
    readonly_tree(work / "payload")
    outside = tmp_path / "outside"
    readonly_tree(outside)
    saved = tmp_path / "saved"
    store.path.rename(saved)
    store.path.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(support.CleanupError, match="symlink"):
            store.remove(store.name)
        assert stat.S_IMODE(outside.stat().st_mode) == 0o555
        assert (saved / store.name / "work/payload/payload").exists()
    finally:
        store.path.unlink()
        saved.rename(store.path)
        store.close()


def test_active_roots_and_other_repository_are_protected(tmp_path):
    store = support.Store(tmp_path / "store", tmp_path, keep=1, days=1)
    work = store.start()
    store.record["created"] = 1
    store.checkpoint()
    observer = support.Store(store.path, tmp_path, keep=1, days=1)
    try:
        observer.prune()
        assert work.exists()
        assert any("active pytest root" in line for line in observer.messages)
        with pytest.raises(support.CleanupError, match="different repository"):
            support.Store(store.path, tmp_path / "other")
    finally:
        observer.close()
        store.close()


def test_bounded_failed_copies_keep_independent_summaries(tmp_path):
    names = []
    for _ in range(4):
        store = support.Store(tmp_path / "store", tmp_path, keep=2)
        try:
            work = store.start()
            readonly_tree(work / "evidence")
            names.append(store.name)
            store.record["failures"] = [{"nodeid": "test_failure", "phase": "call"}]
            store.finish(1)
        finally:
            store.close()
    assert sorted(p.name for p in store.path.glob("run-*")) == sorted(names[-2:])
    assert len(list((store.path / "reports").glob("run-*.json"))) == 4


@pytest.mark.parametrize("reference", ["cwd", "fd", "deleted_fd"])
def test_orphan_reference_is_kept_after_owner_lock_is_gone(tmp_path, reference):
    store = support.Store(tmp_path / "store", tmp_path, keep=1, days=1)
    work = store.start()
    payload = work / "payload"
    payload.write_text("evidence")
    store.record["created"] = 1
    store.checkpoint()
    code = "import sys,time; "
    if reference != "cwd":
        code += "f=open(sys.argv[1]); "
    code += "print('ready',flush=True); time.sleep(30)"
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(payload)], cwd=work if reference == "cwd" else tmp_path,
        stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "ready"
    if reference == "deleted_fd":
        payload.unlink()
    store.close()  # Same kernel lock release as a hard-killed pytest owner.
    observer = support.Store(store.path, tmp_path, keep=1, days=1)
    try:
        observer.prune()
        assert work.exists()
        assert any(f"process {child.pid}" in line for line in observer.messages)
        child.terminate()
        child.wait(timeout=5)
        observer.prune()
        assert not work.exists()
        report = json.loads((observer.path / "reports" / (store.name + ".json")).read_text())
        assert report["state"] == "interrupted"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        observer.close()


def test_mountinfo_includes_same_device_bind_mounts(tmp_path, monkeypatch):
    store = support.Store(tmp_path / "store", tmp_path)
    work = store.start()
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            return f"1 0 8:1 / {work} rw - ext4 /dev/test rw\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    try:
        with pytest.raises(support.Referenced, match="mounted directory"):
            store.remove(store.name)
        assert work.exists()
    finally:
        store.close()


def test_unreadable_proc_is_uncertain_not_idle(tmp_path, monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            raise PermissionError("injected read denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert "cannot verify" in support.references(tmp_path)


@pytest.mark.parametrize("guest,host,match", [(4, 9, "guest"), (9, 4, "host:D")])
def test_guest_and_host_preflight_are_separate(tmp_path, monkeypatch, guest, host, match):
    monkeypatch.setattr(support.platform, "release", lambda: "test-microsoft-WSL2")
    monkeypatch.setattr(support.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(100, 1, guest * support.GIB))
    monkeypatch.setattr(support, "host_space", lambda drives: [{"name": "host:D", "free": host * support.GIB}])
    with pytest.raises(support.CleanupError, match=match):
        support.check_space(tmp_path, 5, 5, "")


@pytest.mark.parametrize("failure", ["missing_distro", "permission", "invalid_json"])
def test_host_probe_failures_are_visible(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "invalid_json":
            return subprocess.CompletedProcess(args[0], 0, "not-json", "")
        error = "Cannot identify the WSL VHD volume" if failure == "missing_distro" else "Access is denied"
        raise subprocess.CalledProcessError(9, args[0], stderr=error)

    monkeypatch.setattr(support.subprocess, "run", run)
    with pytest.raises(support.CleanupError, match="Host checking was not skipped") as error:
        support.host_space("")
    assert ("exited 0" if failure == "invalid_json" else "exit 9") in str(error.value)


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="WSL host-probe integration needs PowerShell")
@pytest.mark.parametrize("blocked,match", [("temp", "Windows TEMP"), ("vhd", "WSL VHD file")])
def test_windows_temp_junction_requires_explicit_verified_volumes(monkeypatch, blocked, match):
    actual_run = subprocess.run
    # Execute the actual probe in PowerShell with filesystem/registry query
    # responses injected. No junction, disk pressure or host configuration is created.
    prefix = r'''
function Get-ItemProperty {
  [pscustomobject]@{ DistributionName='fixture-distro'; BasePath='D:\fixture-vhd' }
}
function Get-Item {
  param($LiteralPath, [switch]$Force)
  $attributes = [IO.FileAttributes]::Directory
  if (('__BLOCKED__' -eq 'temp' -and $LiteralPath -notin @('D:\fixture-vhd', 'D:\fixture-vhd\ext4.vhdx')) -or
      ('__BLOCKED__' -eq 'vhd' -and $LiteralPath -eq 'D:\fixture-vhd\ext4.vhdx')) {
    $attributes = $attributes -bor [IO.FileAttributes]::ReparsePoint
  }
  [pscustomobject]@{ Attributes=$attributes; Parent=$null }
}
function Get-PSDrive {
  param($Name, $PSProvider)
  [pscustomobject]@{ Free=[long]10737418240 }
}
'''.replace("__BLOCKED__", blocked).replace("fixture-distro", os.environ["WSL_DISTRO_NAME"].replace("'", "''"))

    def injected(command, **kwargs):
        return actual_run([*command[:-1], prefix + command[-1]], **kwargs)

    monkeypatch.setattr(support.subprocess, "run", injected)
    with pytest.raises(support.CleanupError, match=match + ".*reparse"):
        support.host_space("")
    assert all(row["free"] == 10 * support.GIB for row in support.host_space("C,D"))


@pytest.fixture
def run_pytest(tmp_path, request):
    root = tmp_path / "harness"
    root.mkdir()
    source = Path(support.__file__)
    shutil.copyfile(source, root / "pytest_support.py")
    shutil.copyfile(source.parent / "conftest.py", root / "conftest.py")
    store = tmp_path / "copies"

    def run(body, *extra, checkout=None):
        invocation = Path(checkout) if checkout is not None else root
        (invocation / "test_sample.py").write_text(textwrap.dedent(body))
        command = [sys.executable, "-m", "pytest", "-q", "test_sample.py", "--basetemp", str(store),
                   "--aa2-min-guest-gib", str(request.config.option.aa2_min_guest_gib),
                   "--aa2-min-host-gib", str(request.config.option.aa2_min_host_gib)]
        if request.config.option.aa2_host_drives:
            command += ["--aa2-host-drives", request.config.option.aa2_host_drives]
        env = {key: value for key, value in os.environ.items() if key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        result = subprocess.run([*command, *extra], cwd=invocation, env=env, capture_output=True, text=True, timeout=40)
        reports = sorted((store / "reports").glob("run-*.json"), key=lambda path: path.stat().st_mtime_ns)
        record = json.loads(reports[-1].read_text()) if reports else None
        return result, store, record

    return run


def test_direct_pytest_reclaims_success_and_session_factories(run_pytest):
    result, root, record = run_pytest('''
        def test_first(tmp_path, tmp_path_factory, tmpdir_factory):
            global factory_copy
            d = tmp_path / "readonly"
            d.mkdir()
            (d / "empty").mkdir()
            (d / "file").write_text("payload")
            d.chmod(0o555)
            factory_copy = tmp_path_factory.mktemp("trusted-provider")
            (factory_copy / "shared").write_text("retain through session")
            tmpdir_factory.mktemp("legacy").join("file").write("legacy")

        def test_second():
            assert (factory_copy / "shared").read_text() == "retain through session"
    ''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["state"] == "passed"
    assert not list(root.glob("run-*"))
    assert record["counts"]["call:passed"] == 2


def test_rootdir_override_cannot_change_checkout_ownership(run_pytest):
    result, root, record = run_pytest("def test_pass(): pass")
    assert result.returncode == 0, result.stdout + result.stderr
    checkout = Path(record["repo"])
    common = checkout.parent
    result, _, record = run_pytest("def test_pass(): pass", "--rootdir", str(common))
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["repo"] == str(checkout)
    assert record["invocation_root"] == str(common)
    other = common / "other-checkout"
    shutil.copytree(checkout, other)
    result, _, _ = run_pytest("def test_pass(): pass", "--rootdir", str(common), checkout=other)
    assert result.returncode == 4
    assert "different repository" in result.stderr
    assert json.loads((root / "store.json").read_text())["repo"] == str(checkout)


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_true_phase_failure_retains_only_required_copy(run_pytest, phase):
    result, root, record = run_pytest(f'''
        import pytest

        @pytest.fixture
        def fixture(tmp_path):
            (tmp_path / "evidence").write_text("{phase} evidence")
            if "{phase}" == "setup":
                raise RuntimeError("real setup error")
            yield
            if "{phase}" == "teardown":
                raise RuntimeError("real teardown error")

        def test_pass(tmp_path):
            (tmp_path / "passed").write_text("discard after all finalizers")

        def test_failure(tmp_path, fixture):
            if "{phase}" == "call":
                assert False, "real call failure"
    ''', "-o", "tmp_path_retention_policy=failed")
    assert result.returncode == 1, result.stdout + result.stderr
    work = root / record["name"] / "work"
    assert not list(work.glob("test_pass[0-9]*"))
    assert list(work.glob("test_failure*/evidence"))
    assert record["failures"] == [{"nodeid": "test_sample.py::test_failure", "phase": phase}]


@pytest.mark.parametrize("body,phase", [
    ("raise RuntimeError('collection failed')", "collection"),
    ("def test_stop(tmp_path):\n    (tmp_path / 'evidence').touch()\n    raise KeyboardInterrupt", "interrupt"),
])
def test_collection_and_keyboard_interrupt_remain_visible(run_pytest, body, phase):
    result, root, record = run_pytest(body)
    assert result.returncode == 2, result.stdout + result.stderr
    assert record["state"] == "interrupted"
    assert (root / record["name"]).is_dir()
    if phase == "collection":
        assert record["failures"][0]["phase"] == "collection"
    else:
        assert list((root / record["name"] / "work").glob("*/evidence"))


def test_hard_crash_is_recovered_without_discarding_small_result(run_pytest):
    result, root, crashed = run_pytest('''
        import os
        def test_crash(tmp_path):
            (tmp_path / "empty").mkdir()
            tmp_path.chmod(0o555)
            os._exit(73)
    ''')
    assert result.returncode == 73
    assert crashed["state"] == "running"
    result, _, record = run_pytest("def test_failure():\n    assert False", "--aa2-temp-keep=1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (root / crashed["name"]).exists()
    assert (root / record["name"]).is_dir()
    old_report = json.loads((root / "reports" / (crashed["name"] + ".json")).read_text())
    assert old_report["state"] == "interrupted"
    assert old_report["current"] == "test_sample.py::test_crash"


def test_killed_during_session_copy_deletion_is_recoverable(run_pytest):
    result, root, crashed = run_pytest('''
        import os
        import pytest_support
        def test_pass(tmp_path_factory):
            copy = tmp_path_factory.mktemp("shared-session")
            (copy / "stop-here").write_text("private payload")
            original = os.unlink
            def interrupted(name, *args, **kwargs):
                if name == "stop-here":
                    os._exit(75)
                return original(name, *args, **kwargs)
            pytest_support.os.unlink = interrupted
    ''')
    assert result.returncode == 75
    assert crashed["state"] == "cleaning"
    assert crashed["exitstatus"] is None
    abandoned = root / crashed["name"]
    assert (abandoned / ".lock").is_file() and (abandoned / "run.json").is_file()
    result, _, _ = run_pytest("def test_pass(): pass")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not abandoned.exists()
    summary = json.loads((root / "reports" / (crashed["name"] + ".json")).read_text())
    assert summary["state"] == "cleanup_interrupted"


def test_concurrent_observer_waits_until_deletion_metadata_is_finished(tmp_path, monkeypatch):
    store = support.Store(tmp_path / "store", tmp_path)
    work = store.start()
    readonly_tree(work / "payload")
    ready = tmp_path / "observer-started"
    original = support.remove_directory
    children = []

    def observe(parent, name, *args, **kwargs):
        if name == "work":
            assert (work.parent / ".lock").is_file() and (work.parent / "run.json").is_file()
            code = """import sys
from pathlib import Path
from pytest_support import Store
Path(sys.argv[3]).touch()
store = Store(sys.argv[1], sys.argv[2])
store.prune()
store.close()
"""
            children.append(subprocess.Popen([sys.executable, "-c", code, str(store.path), str(tmp_path), str(ready)],
                                             cwd=Path(support.__file__).parent,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            time.sleep(0.1)
            assert children[0].poll() is None  # Blocked on store lock, not missing metadata.
        return original(parent, name, *args, **kwargs)

    monkeypatch.setattr(support, "remove_directory", observe)
    try:
        store.remove(store.name)
        stdout, stderr = children[0].communicate(timeout=5)
        assert children[0].returncode == 0, stdout + stderr
        assert not work.exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        store.close()


def test_small_metadata_tail_is_visible_but_not_adopted(tmp_path):
    store = support.Store(tmp_path / "store", tmp_path)
    work = store.start()
    name = store.name
    store.close()
    # Model a kill in the tiny final metadata window, after payload removal.
    work.rmdir()
    (work.parent / "run.json").unlink()
    observer = support.Store(store.path, tmp_path)
    try:
        observer.prune()
        assert (store.path / name / ".lock").exists()
        assert any("small incomplete metadata residue" in message for message in observer.messages)
    finally:
        observer.close()


def test_metadata_tail_replacement_keeps_unregistered_empty_directory(tmp_path, monkeypatch):
    store = support.Store(tmp_path / "store", tmp_path)
    work = store.start()
    run = work.parent
    original = os.unlink

    def replace_after_unlink(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        if name == ".lock":
            run.rename(tmp_path / "registered-empty-tail")
            run.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(support.os, "unlink", replace_after_unlink)
    try:
        with pytest.raises(support.CleanupError, match="during metadata cleanup"):
            store.remove(store.name)
        assert run.is_dir()
        assert not list(run.iterdir())
    finally:
        store.close()


def test_cleanup_failure_cannot_report_a_successful_session(run_pytest):
    result, root, record = run_pytest('''
        import pytest_support
        def test_pass(tmp_path):
            (tmp_path / "evidence").write_text("kept on cleanup failure")
            def fail(*args):
                raise PermissionError("injected cleanup failure")
            pytest_support.remove_directory = fail
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert "injected cleanup failure" in result.stdout
    assert record["cleanup_errors"]
    assert (root / record["name"]).is_dir()


def test_strict_xpass_uses_final_report_and_preserves_evidence(run_pytest):
    result, root, record = run_pytest('''
        import pytest
        @pytest.mark.xfail(strict=True)
        def test_unexpected_pass(tmp_path):
            (tmp_path / "evidence").write_text("strict XPASS is a failure")
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert record["counts"]["call:failed"] == 1
    assert list((root / record["name"] / "work").glob("*/evidence"))


def test_late_prune_failure_updates_result_after_own_copy_is_gone(run_pytest):
    result, root, record = run_pytest('''
        import pytest_support
        def test_pass(tmp_path):
            (tmp_path / "payload").touch()
            def fail(*args):
                raise PermissionError("injected end-of-session prune failure")
            pytest_support.Store.prune = fail
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert record["state"] == "cleanup_failed"
    assert record["exitstatus"] == 1
    assert "end-of-session prune failure" in record["cleanup_errors"][0]
    assert not (root / record["name"]).exists()


@pytest.mark.parametrize("reference", ["cwd", "file"])
def test_self_references_are_protected_but_internal_descriptors_are_not(tmp_path, reference):
    store = support.Store(tmp_path / "store", tmp_path)
    work = store.start()
    original = Path.cwd()
    opened = None
    try:
        if reference == "cwd":
            os.chdir(work)
        else:
            opened = (work / "payload").open("w")
        with pytest.raises(support.Referenced, match=f"process {os.getpid()}"):
            store.remove(store.name)
        os.chdir(original)
        if opened:
            opened.close()
        store.remove(store.name)
        assert not work.exists()  # Only the plugin's own directory/lock FDs remain.
    finally:
        os.chdir(original)
        if opened:
            opened.close()
        store.close()


def test_unreachable_sqlite_cycle_is_collected_but_live_connection_is_preserved(tmp_path):
    enabled = gc.isenabled()
    gc.disable()
    live = None

    def abandoned_connection():
        with sqlite3.connect(tmp_path / "unreachable.sqlite") as connection:
            connection.execute("CREATE TABLE evidence (id INTEGER)")

    try:
        abandoned_connection()
        assert f"process {os.getpid()}" in support.references(tmp_path, collect_unreachable=False)
        assert support.references(tmp_path) is None
        live = sqlite3.connect(tmp_path / "reachable.sqlite")
        live.execute("CREATE TABLE evidence (id INTEGER)")
        assert f"process {os.getpid()}" in support.references(tmp_path)
        assert live.execute("SELECT 1").fetchone() == (1,)
    finally:
        if live is not None:
            live.close()
        if enabled:
            gc.enable()


def test_prune_rejects_replacement_after_marker_validation(tmp_path, monkeypatch):
    store = support.Store(tmp_path / "store", tmp_path, days=1)
    store.start()
    store.record["created"] = 1
    store.checkpoint()
    name = store.name
    store.close()
    observer = support.Store(store.path, tmp_path, days=1)
    original = observer.remove
    saved = tmp_path / "registered-original"

    def replace(name, *args):
        (observer.path / name).rename(saved)
        replacement = observer.path / name
        readonly_tree(replacement)
        return original(name, *args)

    monkeypatch.setattr(observer, "remove", replace)
    try:
        with pytest.raises(support.CleanupError, match="after authorization"):
            observer.prune()
        assert stat.S_IMODE((observer.path / name).stat().st_mode) == 0o555
        assert (observer.path / name / "payload").read_text() == "tiny fixture"
        assert (saved / "work").exists()
    finally:
        observer.close()


def test_replaced_work_does_not_trigger_builtin_symlink_cleanup(run_pytest):
    result, root, record = run_pytest('''
        import pytest
        @pytest.fixture(scope="session", autouse=True)
        def replace_at_session_end(tmp_path_factory):
            work = tmp_path_factory.getbasetemp()
            yield
            outside = work.parent / "outside"
            outside.mkdir()
            (outside / "dead-link").symlink_to("missing")
            work.rename(work.parent / "saved")
            work.symlink_to(outside, target_is_directory=True)

        def test_use(tmp_path):
            (tmp_path / "payload").touch()
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    outside = root / record["name"] / "outside"
    assert (outside / "dead-link").is_symlink()


def test_explicit_basetemp_does_not_delete_user_assets(run_pytest):
    _, root, _ = run_pytest("def test_pass(): pass")
    # An existing *different* nonempty container is never pytest's disposable root.
    user = root.parent / "user-data"
    user.mkdir(mode=0o700)
    (user / "asset").write_text("keep")
    result, _, _ = run_pytest("def test_never(): assert False", "--basetemp", str(user))
    assert result.returncode == 4
    assert "unregistered" in result.stderr
    assert (user / "asset").read_text() == "keep"
