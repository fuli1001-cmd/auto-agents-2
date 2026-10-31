"""Bounded private pytest copies, independent of the production shared cache.

Linux/WSL only, matching the repository's sandbox tests. No file chmod, symlink
traversal, global /tmp scavenging, or production cache access.
"""

from __future__ import annotations

import contextlib
import fcntl
import gc
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

GIB = 1024**3
STATE = pytest.StashKey()
CASE = pytest.StashKey()
RUN = re.compile(r"run-[0-9a-f]{32}\Z")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class CleanupError(Exception):
    pass


class Referenced(CleanupError):
    pass


def directory(name, *, parent=None):
    fd = os.open(name, DIRECTORY, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        os.close(fd)
        raise CleanupError(f"Directory is not owned by this user: {name}")
    return fd


def regular(name, flags, *, parent):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise CleanupError(f"Unsafe metadata/lock file: {name}")
    return fd


def read_json(folder, name):
    with os.fdopen(regular(name, os.O_RDONLY, parent=folder)) as stream:
        value = stream.read(256 * 1024 + 1)
    if len(value) > 256 * 1024:
        raise CleanupError(f"Oversized metadata: {name}")
    return json.loads(value)


def write_json(folder, name, value):
    temporary = f".write-{uuid.uuid4().hex}"
    with os.fdopen(regular(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, parent=folder), "w") as stream:
        json.dump(value, stream, ensure_ascii=True)
        stream.write("\n")
    os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)


def remove_directory(parent, name, device, expected=None, preserve=()):
    """Restore *directory* permissions top down, with all operations fd-relative.

    Unlinking read-only files needs writable parents on Linux. Never chmod files:
    their inodes may also belong to an external cache through a hard link.
    """
    fd = directory(name, parent=parent)
    try:
        info = os.fstat(fd)
        if expected is not None and (info.st_dev, info.st_ino) != expected:
            raise CleanupError(f"Directory replaced after authorization: {name}")
        if info.st_dev != device:
            raise CleanupError(f"Refusing a different filesystem: {name}")
        os.fchmod(fd, stat.S_IMODE(info.st_mode) | 0o700)
        for child in os.listdir(fd):
            if child in preserve:
                continue
            item = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(item.st_mode):
                remove_directory(fd, child, device, (item.st_dev, item.st_ino))
            else:
                os.unlink(child, dir_fd=fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise CleanupError(f"Directory changed during cleanup: {name}")
        if not preserve:
            os.rmdir(name, dir_fd=parent)
    finally:
        os.close(fd)


def references(path, ignored_fds=(), *, collect_unreachable=True):
    """Return why deletion is unsafe; uncertainty is a reason to retain.

    Other unprivileged users cannot enter the mode-0700 store. Inspect all of our
    processes, including orphan children; the pytest PID/lock alone is inadequate.
    """
    prefix = str(path)

    def within(value):
        value = value.removesuffix(" (deleted)")
        return value == prefix or value.startswith(prefix + "/")

    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
            if within(mount):
                return f"mounted directory under {path}"
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                for link in [process / "cwd", *(process / "fd").iterdir()]:
                    if (int(process.name) == os.getpid() and link.parent.name == "fd"
                            and int(link.name) in ignored_fds):
                        continue
                    try:
                        if within(os.readlink(link)):
                            if int(process.name) == os.getpid() and collect_unreachable:
                                # sqlite3 context managers can leave unreachable
                                # cycles holding FDs. Release only unreachable
                                # Python objects, then run the same guard again.
                                gc.collect()
                                return references(path, ignored_fds, collect_unreachable=False)
                            return f"process {process.name} still references {path}"
                    except FileNotFoundError:
                        continue  # Process/descriptor closed while inspected.
            except FileNotFoundError:
                continue
    except (OSError, ValueError, IndexError) as exc:
        return f"cannot verify process/mount references ({type(exc).__name__})"
    return None


def host_space(drives):
    """Probe actual Windows volumes; registry/junction ambiguity fails closed."""
    distro = os.environ.get("WSL_DISTRO_NAME", "").replace("'", "''")
    if drives and not re.fullmatch(r"[A-Za-z](?:,[A-Za-z])*", drives):
        raise CleanupError("--aa2-host-drives must be comma-separated drive letters, e.g. C,D")
    explicit = ",".join(f"'{drive.upper()}'" for drive in drives.split(",")) if drives else ""
    # Resolve ordinary BasePath directories only. PowerShell 5 cannot reliably
    # resolve arbitrary reparse chains without native interop: require verified
    # explicit volumes instead of misreporting the lexical drive as the VHD drive.
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$drives = @(__DRIVES__)
function Assert-NoReparse([string]$path, [string]$label) {
  $item = Get-Item -LiteralPath $path -Force
  while ($null -ne $item) {
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw ($label + ' traverses a reparse point; specify its actual --aa2-host-drives.')
    }
    $item = $item.Parent
  }
}
if ($drives.Count -eq 0) {
  $entries = @(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss\*' |
    Where-Object { $_.DistributionName -eq '__DISTRO__' })
  if ($entries.Count -ne 1) { throw 'Cannot identify the WSL VHD volume; specify --aa2-host-drives.' }
  $base = $entries[0].BasePath -replace '^\\\\\?\\', ''
  if ($base -notmatch '^[A-Za-z]:\\') { throw 'Unsupported VHD BasePath; specify --aa2-host-drives.' }
  Assert-NoReparse $base 'VHD BasePath'
  Assert-NoReparse (Join-Path $base 'ext4.vhdx') 'WSL VHD file'
  Assert-NoReparse ([IO.Path]::GetTempPath()) 'Windows TEMP'
  $drives += $base.Substring(0, 1)
}
$drives += [IO.Path]::GetPathRoot([IO.Path]::GetTempPath()).Substring(0, 1)
$drives += $env:SystemDrive.Substring(0, 1)
$result = @($drives | Sort-Object -Unique | ForEach-Object {
  $drive = Get-PSDrive -Name $_ -PSProvider FileSystem
  if ($null -eq $drive.Free) { throw 'Host free space unavailable.' }
  @{name = ('host:' + $_); free = [long]$drive.Free}
})
ConvertTo-Json -InputObject $result -Compress
""".replace("__DRIVES__", explicit).replace("__DISTRO__", distro)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8-sig", timeout=20, check=True,
        )
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or not rows:
            raise ValueError("empty host volume result")
        if any(not isinstance(row["free"], int) or row["free"] < 0 for row in rows):
            raise ValueError("invalid host free space")
        return rows
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        detail = type(exc).__name__
        if isinstance(exc, subprocess.CalledProcessError):
            detail += f" (exit {exc.returncode}): " + next(
                (line.strip()[:300] for line in (exc.stderr or "").splitlines() if line.strip()), "no stderr"
            )
        elif isinstance(exc, (ValueError, KeyError, TypeError)):
            detail += " (PowerShell exited 0; invalid JSON/volume data)"
        raise CleanupError(
            "WSL host free-space probe failed; verify the VHD's actual volume and pass "
            "--aa2-host-drives=C,D (use your actual letters). Host checking was not skipped. "
            f"Cause: {detail}"
        ) from exc


def check_space(path, guest_gib, host_gib, drives):
    rows = [{"name": "guest", "free": shutil.disk_usage(path).free, "minimum_gib": guest_gib}]
    if "microsoft" in platform.release().lower():
        rows += [{**row, "minimum_gib": host_gib} for row in host_space(drives)]
    for row in rows:
        if row["free"] < row["minimum_gib"] * GIB:
            raise CleanupError(
                f"{row['name']} has {row['free'] / GIB:.3f} GiB free; "
                f"requires {row['minimum_gib']:g} GiB. Pause or explicitly choose "
                "a justified smaller test budget; no tests started."
            )
    return rows


def small_metadata_residue(fd):
    """Never adopt a markerless payload; tolerate only bounded metadata remnants."""
    size = 0
    for name in os.listdir(fd):
        if name not in {".lock", "run.json"} and not re.fullmatch(r"\.write-[0-9a-f]{32}", name):
            return False
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return False
        size += info.st_size
    return size <= 256 * 1024


class Store:
    def __init__(self, path, repo, keep=3, days=7):
        self.path, self.repo = Path(path).absolute(), str(Path(repo).resolve())
        self.keep, self.days = keep, days
        self.fd = self.lock = self.run_fd = self.run_lock = None
        self.lock_depth = 0
        self.messages = []
        self.identity = {"kind": "aa2-pytest", "version": 1, "repo": self.repo}
        if self.path.parent != self.path.parent.resolve(strict=True):
            raise CleanupError("Managed pytest store parent must not traverse symlinks")
        self.path.mkdir(mode=0o700, exist_ok=True)
        if self.path != self.path.resolve():
            raise CleanupError("Managed pytest store must not traverse symlinks")
        info = self.path.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CleanupError("Managed pytest store must be owned by this user with mode 0700")
        self.fd = directory(self.path)
        if (info.st_dev, info.st_ino) != (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino):
            self.close()
            raise CleanupError("Managed pytest store changed while opening")
        self.device = info.st_dev
        try:
            entries = os.listdir(self.fd)
            if "store.json" not in entries and any(name != ".lock" for name in entries):
                raise CleanupError("Refusing nonempty unregistered --basetemp/store directory")
            self.lock = regular(".lock", os.O_RDWR | os.O_CREAT, parent=self.fd)
            with self.exclusive():
                try:
                    marker = read_json(self.fd, "store.json")
                except FileNotFoundError:
                    if set(os.listdir(self.fd)) != {".lock"}:
                        raise CleanupError("Refusing unregistered store contents") from None
                    write_json(self.fd, "store.json", self.identity)
                    marker = self.identity
                if marker != self.identity:
                    raise CleanupError("Pytest store belongs to a different repository or format")
                with contextlib.suppress(FileExistsError):
                    os.mkdir("reports", 0o700, dir_fd=self.fd)
        except BaseException:
            self.close()
            raise

    @contextlib.contextmanager
    def exclusive(self):
        if self.lock_depth == 0:
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.lock_depth += 1
        try:
            self.check_identity()
            yield
        finally:
            self.lock_depth -= 1
            if self.lock_depth == 0:
                fcntl.flock(self.lock, fcntl.LOCK_UN)

    def check_identity(self):
        if self.path != self.path.resolve():
            raise CleanupError("Managed store path was replaced by a symlink")
        current, opened = self.path.lstat(), os.fstat(self.fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise CleanupError("Managed store path was replaced")

    def save(self, body):
        fd = directory("reports", parent=self.fd)
        try:
            write_json(fd, body["name"] + ".json", body)
        finally:
            os.close(fd)

    def start(self):
        with self.exclusive():
            self.name = "run-" + uuid.uuid4().hex
            os.mkdir(self.name, 0o700, dir_fd=self.fd)
            self.run_fd = directory(self.name, parent=self.fd)
            self.run_lock = regular(".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, parent=self.run_fd)
            fcntl.flock(self.run_lock, fcntl.LOCK_EX)
            self.record = {
                **self.identity, "name": self.name, "state": "running", "created": time.time(),
                "python": sys.version.split()[0], "pytest": pytest.__version__,
                "plugin_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "counts": {}, "failures": [], "current": None,
            }
            write_json(self.run_fd, "run.json", self.record)
            self.save(self.record)
            os.mkdir("work", 0o700, dir_fd=self.run_fd)
        return self.path / self.name / "work"

    def checkpoint(self):
        self.check_identity()
        write_json(self.run_fd, "run.json", self.record)
        self.save(self.record)

    def remove(self, name, expected=None, ignored_fds=()):
        with self.exclusive():
            if expected is None:
                if name != self.name:
                    raise CleanupError("Removal requires the authorized run identity")
                info = os.fstat(self.run_fd)
                expected = (info.st_dev, info.st_ino)
            internal = {fd for fd in (self.fd, self.lock, self.run_fd, self.run_lock, *ignored_fds) if fd is not None}
            blocked = references(self.path / name, internal)
            if blocked:
                raise Referenced(blocked)
            # Preserve authorization/lock through every potentially large delete.
            # Cooperating observers cannot enter during final metadata removal.
            remove_directory(self.fd, name, self.device, expected, preserve={".lock", "run.json"})
            fd = directory(name, parent=self.fd)
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != expected:
                    raise CleanupError("Run replaced before metadata cleanup")
                os.unlink("run.json", dir_fd=fd)
                os.unlink(".lock", dir_fd=fd)
                current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != expected:
                    raise CleanupError("Run replaced during metadata cleanup")
                os.rmdir(name, dir_fd=self.fd)
            finally:
                os.close(fd)

    def prune(self):
        """Only idle, owned run roots, with bounded separate result summaries."""
        with self.exclusive():
            idle = []
            try:
                for name in os.listdir(self.fd):
                    if not RUN.fullmatch(name):
                        continue
                    run_fd = directory(name, parent=self.fd)
                    lock = None
                    try:
                        try:
                            lock = regular(".lock", os.O_RDWR, parent=run_fd)
                        except FileNotFoundError:
                            if small_metadata_residue(run_fd):
                                self.messages.append(f"small incomplete metadata residue retained: {name}")
                                continue
                            raise CleanupError(f"Missing run lock with payload; preserved: {name}") from None
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            self.messages.append(f"active pytest root retained: {name}")
                            continue
                        try:
                            body = read_json(run_fd, "run.json")
                        except FileNotFoundError:
                            if small_metadata_residue(run_fd):
                                self.messages.append(f"small incomplete metadata residue retained: {name}")
                                continue
                            raise CleanupError(f"Missing run marker with payload; preserved: {name}") from None
                        if any(body.get(key) != value for key, value in self.identity.items()) or body["name"] != name:
                            raise CleanupError(f"Unregistered run retained: {name}")
                        if body["state"] in {"running", "cleaning"}:
                            body["state"] = "interrupted" if body["state"] == "running" else "cleanup_interrupted"
                            write_json(run_fd, "run.json", body)
                            self.save(body)
                        idle.append((body, lock, run_fd))
                        lock = run_fd = None  # Keep the validated directory AND its lock through pruning.
                    finally:
                        if run_fd is not None:
                            os.close(run_fd)
                        if lock is not None:
                            os.close(lock)
                idle.sort(key=lambda entry: entry[0]["created"], reverse=True)
                retained = 0
                for body, lock, run_fd in idle:
                    age = time.time() - body["created"]
                    disposable = body["state"] in {"passed", "cleanup_interrupted"} or (
                        body["state"] == "cleanup_failed" and body.get("tests_exitstatus") == 0
                    )
                    if not disposable and age < self.days * 86400:
                        retained += 1
                        if retained <= self.keep:
                            continue
                    try:
                        info = os.fstat(run_fd)
                        self.remove(body["name"], (info.st_dev, info.st_ino), (lock, run_fd))
                    except Referenced as exc:
                        self.messages.append(f"retained {body['name']}: {exc}")
                reports = directory("reports", parent=self.fd)
                try:
                    names = []
                    for name in os.listdir(reports):
                        if RUN.fullmatch(name.removesuffix(".json")) and name.endswith(".json"):
                            info = os.stat(name, dir_fd=reports, follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise CleanupError(f"Unsafe report retained: {name}")
                            names.append((info.st_mtime, name))
                    for index, (mtime, name) in enumerate(sorted(names, reverse=True)):
                        if index >= 200 or time.time() - mtime > 90 * 86400:
                            os.unlink(name, dir_fd=reports)
                finally:
                    os.close(reports)
            finally:
                for _, lock, run_fd in idle:
                    os.close(lock)
                    os.close(run_fd)

    def finish(self, status):
        state = "cleaning" if status == 0 else "interrupted" if status == 2 else "failed"
        self.record.update(state=state, exitstatus=None if status == 0 else int(status),
                           tests_exitstatus=int(status), ended=time.time())
        self.checkpoint()
        if status == 0:
            self.remove(self.name)
        fcntl.flock(self.run_lock, fcntl.LOCK_UN)
        self.prune()
        if status == 0:
            self.record.update(state="passed", exitstatus=0, completed=time.time())
            self.save(self.record)

    def close(self):
        for attr in ("run_lock", "run_fd", "lock", "fd"):
            fd = getattr(self, attr, None)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)


def pytest_addoption(parser):
    group = parser.getgroup("aa2 pytest copies")
    group.addoption("--aa2-temp-keep", type=int, default=3, help="Retain this many failed/interrupted run copies")
    group.addoption("--aa2-temp-days", type=float, default=7, help="Maximum idle failed-copy age in days")
    group.addoption("--aa2-min-guest-gib", type=float, default=5, help="Required guest free space (GiB)")
    group.addoption("--aa2-min-host-gib", type=float, default=5, help="Required WSL host free space (GiB)")
    group.addoption("--aa2-host-drives", default="", help="Verified actual VHD host volumes, e.g. C,D")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if config.getoption("numprocesses", default=0):
        raise pytest.UsageError("aa2 private-copy cleanup supports concurrent pytest processes, not xdist workers")
    for name in ("aa2_temp_keep", "aa2_temp_days", "aa2_min_guest_gib", "aa2_min_host_gib"):
        value = config.getoption(name)
        if not math.isfinite(value) or value <= 0:
            raise pytest.UsageError(f"{name} must be positive and finite")
    # --rootdir is a collection setting, not authority to adopt another checkout.
    repo = Path(__file__).resolve().parent
    tag = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
    root = config.option.basetemp or (
        Path(os.environ.get("PYTEST_DEBUG_TEMPROOT", tempfile.gettempdir())) / f"aa2-pytest-{os.getuid()}-{tag}"
    )
    store = None
    try:
        store = Store(root, repo, config.option.aa2_temp_keep, config.option.aa2_temp_days)
        config.add_cleanup(store.close)
        store.prune()
        space = check_space(root, config.option.aa2_min_guest_gib, config.option.aa2_min_host_gib,
                            config.option.aa2_host_drives)
        config.option.basetemp = str(store.start())
        store.record["space"] = space
        store.record["invocation_root"] = str(config.rootpath)
        store.record["host_drives"] = config.option.aa2_host_drives or "auto"
        store.record["retention"] = {"count": store.keep, "days": store.days}
        revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=10)
        store.record["git_head"] = revision.stdout.strip() if revision.returncode == 0 else None
        store.record["source_sha256"] = {
            name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
            for name in ("conftest.py", "pytest_support.py", "pyproject.toml", "tests/test_pytest_cleanup.py")
            if (repo / name).is_file()
        }
        store.checkpoint()
        config.stash[STATE] = store
        store.items = {}
        config.pluginmanager.register(FinalReports(store), "aa2-copy-final-reports")
    except (CleanupError, OSError, ValueError, KeyError) as exc:
        if store:
            store.close()
        raise pytest.UsageError(f"aa2 pytest preflight: {exc}") from exc


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    # Delay cleanup until the teardown report exists. A fixture finalizer cannot
    # know about failures in later finalizers, including setup/teardown errors.
    name = re.sub(r"[\W]", "_", request.node.name)[:30]
    path = tmp_path_factory.mktemp(name, numbered=True)
    info = path.lstat()
    request.node.stash[CASE] = {"path": path, "identity": (info.st_dev, info.st_ino), "failed": False}
    return path


@pytest.fixture(scope="session")
def tmp_path_factory(request):
    # Own a separate public factory. The builtin tmpdir plugin's unused factory
    # has no basetemp, so its sessionfinish hook cannot follow a replaced work
    # path or silently attempt its own permission recovery.
    work = request.config.stash[STATE].path / request.config.stash[STATE].name / "work"
    return pytest.TempPathFactory(
        given_basetemp=work, basetemp=work, retention_count=0, retention_policy="all",
        trace=lambda *args, **kwargs: None, _ispytest=True,
    )


@pytest.fixture(scope="session")
def tmpdir_factory(tmp_path_factory):
    return pytest.TempdirFactory(tmp_path_factory, _ispytest=True)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    store = item.config.stash[STATE]
    store.items[item.nodeid] = item
    store.record["current"] = item.nodeid[:512]
    store.checkpoint()
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    report = yield
    store = collector.config.stash.get(STATE, None)
    if store is not None and report.failed and len(store.record["failures"]) < 256:
        store.record["failures"].append({"nodeid": report.nodeid[:512], "phase": "collection"})
        store.checkpoint()
    return report


class FinalReports:
    def __init__(self, store):
        self.store = store

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        # logreport receives the final report, including strict XPASS changes by
        # outer makereport wrappers. A wrapper's local return value is too early.
        store = self.store
        item = store.items[report.nodeid]
        counts = store.record["counts"]
        key = f"{report.when}:{report.outcome}"
        counts[key] = counts.get(key, 0) + 1
        if report.failed and len(store.record["failures"]) < 256:
            store.record["failures"].append({"nodeid": item.nodeid[:512], "phase": report.when})
        case = item.stash.get(CASE, None)
        if case is not None:
            case["failed"] |= report.failed
            if report.when == "teardown" and not case["failed"]:
                try:
                    store.check_identity()
                    blocked = references(case["path"])
                    if blocked:
                        raise CleanupError(blocked)
                    work = directory("work", parent=store.run_fd)
                    try:
                        remove_directory(work, case["path"].name, store.device, case["identity"])
                    finally:
                        os.close(work)
                except (OSError, CleanupError) as exc:
                    errors = store.record.setdefault("cleanup_errors", [])
                    if len(errors) < 32:
                        errors.append(str(exc)[:1024])
                    item.session.shouldfail = "aa2 pytest cleanup failed; retained evidence, stopping further writes"
        if report.when == "teardown":
            store.record["current"] = None
            del store.items[report.nodeid]
        store.checkpoint()


def pytest_report_header(config):
    store = config.stash[STATE]
    return [f"aa2 private copies: {store.path}", *store.messages,
            "aa2 space: " + ", ".join(f"{r['name']}={r['free'] / GIB:.3f} GiB" for r in store.record["space"])]


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    store = session.config.stash[STATE]
    errors = store.record.get("cleanup_errors", [])
    if errors and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    try:
        store.finish(session.exitstatus)
    except (OSError, CleanupError) as exc:
        errors.append(str(exc))
        if session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        store.record.update(state="cleanup_failed", cleanup_errors=errors, exitstatus=int(session.exitstatus))
        with contextlib.suppress(OSError, CleanupError):
            write_json(store.run_fd, "run.json", store.record)
        with contextlib.suppress(OSError, CleanupError):
            # Successful own-copy deletion may already have unlinked run_fd.
            # The independent result must still record any later pruning error.
            store.save(store.record)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        for message in [*store.messages, *errors]:
            reporter.write_line(f"aa2 pytest cleanup: {message}", red=bool(errors))
        reporter.write_line(f"aa2 pytest result: {store.path / 'reports' / (store.name + '.json')}")
