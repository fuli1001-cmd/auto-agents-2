"""Retain-all lifecycle tests; never retry the host's denied /proc scan."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import pytest_support as support


def forbidden(*args, **kwargs):
    raise AssertionError("cleanup/reference/permission operation must not run")


@pytest.mark.parametrize("status", range(6))
def test_retained_store_never_enters_delete_prune_or_reference_paths(tmp_path, monkeypatch, status):
    store = support.Store(tmp_path / "retained", tmp_path, retain_all=True)
    try:
        work = store.start()
        factory = support.RetainedPathFactory(store)
        (factory.mktemp("case") / "evidence").write_text("retain")
        (work / "casecurrent").symlink_to("missing")
        assert factory.mktemp("case").name == "case1"
        for name in ("references", "remove_directory"):
            monkeypatch.setattr(support, name, forbidden)
        for name in ("unlink", "rmdir", "fchmod", "chmod"):
            monkeypatch.setattr(support.os, name, forbidden)
        with monkeypatch.context() as patch:
            patch.setattr(store, "exclusive", forbidden)
            store.prune()
        with pytest.raises(support.CleanupError, match="forbids removal"):
            store.remove(store.name)
        store.finish(status)
        report = json.loads((store.path / "reports" / f"{store.name}.json").read_text())
        assert report["state"] == "retained"
        assert report["tests_exitstatus"] == status
        assert report["cleanup"] == {
            "status": "not_attempted", "process_mount_references": "not_checked", "safe_to_delete": None,
        }
        assert report["inventory"]["size_bytes"] is None
        assert report["inventory"]["allocated_directories"] == ["case0", "case1"]
        assert report["retention"]["count"] is None and report["retention"]["days"] is None
        assert (work / "case0" / "evidence").read_text() == "retain"
        assert (work / "casecurrent").is_symlink()
    finally:
        store.close()
        store.close()


def test_default_guard_failure_still_prevents_every_delete(tmp_path, monkeypatch):
    # Synthetic failed guard only: no real /proc access, chmod or successful delete.
    store = support.Store(tmp_path / "default", tmp_path)
    try:
        work = store.start()
        (work / "evidence").write_text("keep")
        monkeypatch.setattr(support, "references", lambda *args, **kwargs: "cannot verify (injected PermissionError)")
        monkeypatch.setattr(support, "remove_directory", forbidden)
        monkeypatch.setattr(support.os, "fchmod", forbidden)
        with pytest.raises(support.Referenced, match="cannot verify"):
            store.finish(0)
        assert (work / "evidence").read_text() == "keep"
        assert store.record["state"] == "cleaning"
        assert store.record["exitstatus"] is None
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["empty", "foreign", "symlink", "symlink_parent"])
def test_retain_requires_a_fresh_non_symlink_root(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    asset = outside / "asset"
    asset.write_text("unchanged")
    root = tmp_path / "store"
    if kind == "symlink_parent":
        alias = tmp_path / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        root = alias / "store"
    elif kind == "symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if kind == "foreign":
            (root / "store.json").write_text('{"kind":"foreign"}')
    before = asset.stat()
    with pytest.raises((support.CleanupError, OSError)):
        support.Store(root, tmp_path, retain_all=True)
    assert asset.read_text() == "unchanged"
    after = asset.stat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns,
    )
    assert not (outside / "store").exists()
    if kind == "foreign":
        assert (root / "store.json").read_text() == '{"kind":"foreign"}'



@pytest.mark.parametrize("kind", ["default", "other_repo", "malformed", "symlink"])
def test_disposable_or_unsafe_ancestor_marker_is_rejected_before_writing(tmp_path, kind):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o700)
    marker = ancestor / "store.json"
    asset = tmp_path / "asset"
    asset.write_text("do not change")
    if kind == "symlink":
        marker.symlink_to(asset)
    else:
        marker.write_text('{"kind":' if kind == "malformed" else json.dumps(
            {"kind": "aa2-pytest", "version": 1, "repo": "other" if kind == "other_repo" else str(tmp_path)}
        ))
    before = marker.read_bytes()
    root = ancestor / "new-retained"
    with pytest.raises((support.CleanupError, OSError, ValueError)):
        support.Store(root, tmp_path, retain_all=True)
    assert not root.exists()
    assert marker.read_bytes() == before
    assert asset.read_text() == "do not change"


def test_non_aa2_parent_marker_is_not_adopted_or_modified(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    marker = parent / "store.json"
    marker.write_text('{"kind":"unrelated-user-file"}')
    before = marker.read_bytes()
    store = support.Store(parent / "retained", tmp_path, retain_all=True)
    store.close()
    assert marker.read_bytes() == before


def test_retention_marker_protects_against_later_default_adoption(tmp_path):
    store = support.Store(tmp_path / "retained", tmp_path, retain_all=True)
    work = store.start()
    (work / "evidence").write_text("keep until separately authorized")
    store.finish(0)
    store.close()
    before = {p: p.read_bytes() for p in [store.path / "store.json", work.parent / "run.json",
                                         store.path / "reports" / f"{store.name}.json"]}
    with pytest.raises(support.CleanupError, match="different repository or format"):
        support.Store(store.path, tmp_path)
    with pytest.raises(FileExistsError):
        support.Store(store.path, tmp_path, retain_all=True)
    assert before == {p: p.read_bytes() for p in before}
    assert (work / "evidence").exists()


def test_close_is_idempotent_and_does_not_invent_a_test_result(tmp_path):
    store = support.Store(tmp_path / "retained", tmp_path, retain_all=True)
    work = store.start()
    (work / "evidence").touch()
    store.close()
    path = work.parent / "run.json"
    before = path.read_bytes()
    report = json.loads(before)
    assert report["state"] == "retained_unfinished"
    assert report["tests_exitstatus"] is None and report["exitstatus"] is None
    store.close()
    assert path.read_bytes() == before


def test_replaced_root_is_not_followed_or_repaired(tmp_path, capsys):
    store = support.Store(tmp_path / "retained", tmp_path, retain_all=True)
    store.start()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "asset").write_text("unchanged")
    saved = tmp_path / "saved"
    store.path.rename(saved)
    store.path.symlink_to(outside, target_is_directory=True)
    store.close()
    assert "metadata incomplete" in capsys.readouterr().err
    assert sorted(p.name for p in outside.iterdir()) == ["asset"]
    assert (outside / "asset").read_text() == "unchanged"
    assert (saved / store.name / "work").is_dir()
    assert json.loads((saved / store.name / "run.json").read_text())["state"] == "running"


def test_factory_rejects_escape_and_bounds_its_nonrecursive_inventory(tmp_path):
    store = support.Store(tmp_path / "retained", tmp_path, retain_all=True)
    try:
        store.start()
        factory = support.RetainedPathFactory(store)
        for basename in ("..", ".", "../escape", "nested/escape"):
            with pytest.raises(ValueError):
                factory.mktemp(basename)
        for _ in range(130):
            factory.mktemp("case")
        assert len(store.record["inventory"]["allocated_directories"]) == 128
        assert store.record["inventory"]["unlisted_allocations"] == 2
        assert factory.mktemp("exact", numbered=False).name == "exact"
        with pytest.raises(FileExistsError):
            factory.mktemp("exact", numbered=False)
    finally:
        store.close()


@pytest.fixture
def retained_pytest(tmp_path, request):
    harness = tmp_path / "harness"
    harness.mkdir()
    source = Path(support.__file__)
    shutil.copyfile(source, harness / "pytest_support.py")
    base_conftest = (source.parent / "conftest.py").read_text()
    count = 0

    def run(body, *, extra=(), conftest="", explicit=True):
        nonlocal count
        count += 1
        root = tmp_path / f"copies-{count}"
        (harness / "test_sample.py").write_text(textwrap.dedent(body))
        # Assertions, not bypasses: record calls even if atexit swallows errors.
        sentinels = f'''
import _pytest.pathlib
import _pytest.tmpdir
import pytest_support as _support

def _forbidden(*args, **kwargs):
    from pathlib import Path
    Path({str(harness / "forbidden-call")!r}).write_text("unexpected cleanup/reference call")
    raise AssertionError("unexpected cleanup/reference call")

_support.references = _forbidden
_support.remove_directory = _forbidden
_support.Store.remove = _forbidden
_pytest.tmpdir.rm_rf = _forbidden
_pytest.tmpdir.rmtree = _forbidden
_pytest.tmpdir.cleanup_dead_symlinks = _forbidden
_pytest.pathlib.make_numbered_dir_with_cleanup = _forbidden
_pytest.pathlib.cleanup_numbered_dir = _forbidden
_pytest.pathlib._force_symlink = _forbidden

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    assert session.config._tmp_path_factory._basetemp is None
'''
        (harness / "conftest.py").write_text(
            base_conftest + "\nimport pytest\n" + sentinels + textwrap.dedent(conftest)
        )
        command = [sys.executable, "-B", "-m", "pytest", "-q", "test_sample.py", "--aa2-temp-retain-all",
                   "--aa2-min-guest-gib", str(request.config.option.aa2_min_guest_gib),
                   "--aa2-min-host-gib", str(request.config.option.aa2_min_host_gib)]
        if explicit:
            command += ["--basetemp", str(root)]
        if request.config.option.aa2_host_drives:
            command += ["--aa2-host-drives", request.config.option.aa2_host_drives]
        env = {key: value for key, value in os.environ.items()
               if key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTEST_DEBUG_TEMPROOT"] = str(tmp_path)
        result = subprocess.run([*command, *extra], cwd=harness, env=env,
                                capture_output=True, text=True, timeout=45)
        assert not (harness / "forbidden-call").exists(), result.stdout + result.stderr
        if not explicit:
            roots = sorted(tmp_path.glob("aa2-pytest-retained-*"))
            root = roots[-1] if roots else root
        reports = list((root / "reports").glob("run-*.json"))
        record = json.loads(reports[-1].read_text()) if reports else None
        return result, root, record

    return run


def test_public_fixtures_and_builtin_session_atexit_keep_everything(retained_pytest):
    result, root, record = retained_pytest('''
        import pytest
        @pytest.fixture(scope="session", autouse=True)
        def shared(tmp_path_factory, tmpdir_factory):
            global work, first
            work = tmp_path_factory.getbasetemp()
            (work / "dead-link").symlink_to("missing")
            (work / "samecurrent").symlink_to("missing-too")
            first = tmp_path_factory.mktemp("same")
            assert first.name == "same0"
            assert tmp_path_factory.mktemp("same").name == "same1"
            tmpdir_factory.mktemp("legacy-session").join("evidence").write("keep")
        def test_first(tmp_path, tmpdir):
            global previous
            previous = tmp_path
            (tmp_path / "evidence").write_text("keep successful tmp_path")
            tmpdir.join("legacy").write("keep successful tmpdir")
        def test_second():
            assert (previous / "evidence").read_text() == "keep successful tmp_path"
            assert (work / "samecurrent").is_symlink()
    ''', extra=("-o", "tmp_path_retention_policy=failed"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["state"] == "retained" and record["test_outcome"] == "passed"
    assert record["counts"]["call:passed"] == 2
    work = root / record["name"] / "work"
    assert (work / "dead-link").is_symlink() and (work / "samecurrent").is_symlink()
    assert list(work.glob("test_first*/evidence"))
    assert list(work.glob("legacy-session*/evidence"))
    assert "cleanup NOT ATTEMPTED" in result.stdout and "NOT verified safe to delete" in result.stdout


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_phase_failures_preserve_successful_and_failed_copies(retained_pytest, phase):
    result, root, record = retained_pytest(f'''
        import pytest
        @pytest.fixture
        def fail(tmp_path):
            (tmp_path / "evidence").write_text("{phase}")
            if "{phase}" == "setup":
                raise RuntimeError("setup failure")
            yield
            if "{phase}" == "teardown":
                raise RuntimeError("teardown failure")
        def test_success(tmp_path):
            (tmp_path / "success").touch()
        def test_failed(tmp_path, fail):
            if "{phase}" == "call":
                assert False
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert record["state"] == "retained" and record["tests_exitstatus"] == 1
    work = root / record["name"] / "work"
    assert list(work.glob("test_success*/success"))
    assert list(work.glob("test_failed*/evidence"))
    assert record["failures"] == [{"nodeid": "test_sample.py::test_failed", "phase": phase}]


@pytest.mark.parametrize("body,status", [
    ("raise RuntimeError('collection failed')", 2),
    ("def test_stop(tmp_path):\n    (tmp_path / 'evidence').touch()\n    raise KeyboardInterrupt", 2),
    ("import pytest\ndef test_cancel(tmp_path):\n    (tmp_path / 'evidence').touch()\n"
     "    pytest.exit('explicit cancellation', returncode=2)", 2),
])
def test_collection_interrupt_and_cancellation_remain_nonpassing(retained_pytest, body, status):
    result, root, record = retained_pytest(body)
    assert result.returncode == status, result.stdout + result.stderr
    assert record["state"] == "retained" and record["tests_exitstatus"] == status
    assert record["test_outcome"] == "interrupted"
    assert (root / record["name"] / "work").is_dir()


def test_internal_exception_and_configuration_exception_retain_evidence(retained_pytest):
    result, root, record = retained_pytest("def test_never(): pass", conftest='''
        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_protocol(item, nextitem):
            raise RuntimeError("injected protocol exception")
    ''')
    assert result.returncode == 3, result.stdout + result.stderr
    assert record["test_outcome"] == "internal_error" and record["state"] == "retained"
    assert (root / record["name"] / "work").is_dir()
    result, root, record = retained_pytest("def test_never(): pass", conftest='''
        @pytest.hookimpl(trylast=True)
        def pytest_configure(config):
            raise pytest.UsageError("injected late configure failure")
    ''')
    assert result.returncode == 4, result.stdout + result.stderr
    assert record["state"] == "retained_unfinished" and record["tests_exitstatus"] is None
    assert (root / record["name"] / "work").is_dir()


@pytest.mark.parametrize("window", ["preflight", "start"])
def test_preflight_and_partial_start_errors_never_cleanup(retained_pytest, window):
    injection = '''
        def failed_space(*args, **kwargs):
            raise _support.CleanupError("injected space failure")
        _support.check_space = failed_space
    ''' if window == "preflight" else '''
        _original_mkdir = _support.os.mkdir
        def failed_work(name, *args, **kwargs):
            if name == "work":
                raise PermissionError("injected work creation failure")
            return _original_mkdir(name, *args, **kwargs)
        _support.os.mkdir = failed_work
    '''
    result, root, record = retained_pytest("def test_never(): pass", conftest=injection)
    assert result.returncode == 4, result.stdout + result.stderr
    assert str(root) in result.stderr
    assert "preflight_failed" in result.stderr and "cleanup NOT ATTEMPTED" in result.stderr
    marker = json.loads((root / "store.json").read_text())
    assert marker["retention_mode"] == "all"
    assert marker["retention_notice"]["cleanup"] == "not_attempted"
    if window == "preflight":
        assert record is None and not list(root.glob("run-*"))
    else:
        assert record["state"] == "retained_unfinished" and record["tests_exitstatus"] is None
        assert (root / record["name"]).is_dir()


def test_hard_exit_stays_unfinished_and_next_run_cannot_rewrite_it(retained_pytest):
    result, root, record = retained_pytest('''
        import os
        def test_crash(tmp_path):
            (tmp_path / "evidence").touch()
            os._exit(73)
    ''')
    assert result.returncode == 73
    assert record["state"] == "running" and record["tests_exitstatus"] is None
    old = {p: p.read_bytes() for p in [root / "store.json", root / record["name"] / "run.json",
                                      root / "reports" / f"{record['name']}.json"]}
    result, new_root, new_record = retained_pytest("def test_pass(tmp_path): pass")
    assert result.returncode == 0, result.stdout + result.stderr
    assert new_root != root and new_record["state"] == "retained"
    assert old == {p: p.read_bytes() for p in old}


def test_default_retention_roots_are_unique_and_never_adopted(retained_pytest):
    result, first, record = retained_pytest("def test_pass(tmp_path): pass", explicit=False)
    assert result.returncode == 0, result.stdout + result.stderr
    before = (first / "reports" / f"{record['name']}.json").read_bytes()
    result, _, _ = retained_pytest("def test_pass(tmp_path): pass", explicit=False)
    assert result.returncode == 0, result.stdout + result.stderr
    roots = list(first.parent.glob("aa2-pytest-retained-*"))
    assert len(roots) == 2
    assert (first / "reports" / f"{record['name']}.json").read_bytes() == before


def test_retention_metadata_failure_keeps_incomplete_evidence_and_nonzero_exit(retained_pytest):
    result, root, record = retained_pytest("""
        import pytest_support
        def test_pass(tmp_path):
            (tmp_path / "evidence").touch()
            original = pytest_support.write_json
            def fail(folder, name, value):
                if value.get("state") in {"retained", "retention_failed"}:
                    raise OSError("injected retention metadata failure")
                return original(folder, name, value)
            pytest_support.write_json = fail
    """)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "injected retention metadata failure" in result.stdout
    assert record["state"] == "running" and record["tests_exitstatus"] is None
    assert record["cleanup"]["status"] == "not_attempted"
    assert list((root / record["name"] / "work").glob("test_pass*/evidence"))
