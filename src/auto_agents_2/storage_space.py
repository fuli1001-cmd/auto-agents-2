"""Observe real storage boundaries without treating WSL guest capacity as host space."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .faults import ResourceBoundary
from .managed_process import joined

# Only local registry/volume queries; no user profile scripts or persistent changes.
WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$expectedHome = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0'
if ([IO.Path]::GetFullPath($PSHOME) -ine [IO.Path]::GetFullPath($expectedHome)) {
  throw 'Expected the local Windows PowerShell system executable.'
}
$env:PSModulePath = Join-Path $PSHOME 'Modules'
Import-Module (Join-Path $PSHOME 'Modules\Storage\Storage.psd1') -ErrorAction Stop
$request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')) | ConvertFrom-Json
function Assert-OrdinaryPath([string]$path) {
  $item = Get-Item -LiteralPath $path -Force
  while ($null -ne $item) {
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'Storage backing path contains a reparse point.'
    }
    $item = if ($item -is [IO.DirectoryInfo]) { $item.Parent } else { $item.Directory }
  }
}
$entries = @(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss\*' |
  Where-Object { $_.DistributionName -ceq $request.distribution })
if ($entries.Count -ne 1 -or $entries[0].Version -ne 2) {
  throw 'The current WSL2 distribution has no unique verified registration.'
}
$base = $entries[0].BasePath -replace '^\\\\\?\\', ''
if ($base -notmatch '^[A-Za-z]:\\') { throw 'Unsupported WSL backing path.' }
$leaf = 'ext4.vhdx'
if ($entries[0].PSObject.Properties.Name -contains 'VhdFileName') {
  $leaf = [string]$entries[0].VhdFileName
}
if ([IO.Path]::GetFileName($leaf) -cne $leaf -or $leaf -notmatch '\.vhdx$') {
  throw 'Unsupported WSL VHD filename.'
}
$vhd = Join-Path $base $leaf
Assert-OrdinaryPath $vhd
$item = Get-Item -LiteralPath $vhd -Force
if ($item.PSIsContainer) { throw 'WSL backing path is not a VHD file.' }
$volumes = @(Get-Volume -FilePath $item.FullName)
if ($volumes.Count -ne 1 -or $null -eq $volumes[0].SizeRemaining -or
    [string]::IsNullOrWhiteSpace([string]$volumes[0].UniqueId)) {
  throw 'The actual WSL VHD volume cannot be verified.'
}
$volume = $volumes[0]
@{
  version = 1
  distribution = [string]$entries[0].DistributionName
  backing_path = [string]$item.FullName
  volume_id = [string]$volume.UniqueId
  name = ('host:' + [string]$volume.DriveLetter)
  free = [long]$volume.SizeRemaining
  total = [long]$volume.Size
} | ConvertTo-Json -Compress
"""


def _windows_mount(path):
    """Require the kernel's Windows drive mount, not a look-alike PATH directory."""
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
    matches = []
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source, mount, filesystem, options = fields[:4]
        mount = Path(unescape(mount))
        if path.is_relative_to(mount):
            matches.append((len(mount.parts), mount, unescape(source), filesystem, options))
    if not matches:
        raise ValueError("Windows executable mount unavailable")
    _, mount, source, filesystem, options = max(matches, key=lambda row: row[0])
    if (not re.fullmatch(r"[A-Za-z]:\\", source)
            or not (filesystem == "drvfs" or filesystem == "9p" and "aname=drvfs" in options)
            or tuple(part.lower() for part in path.relative_to(mount).parts) !=
               ("windows", "system32", "windowspowershell", "v1.0", "powershell.exe")):
        raise ValueError("Executable is not on a verified Windows system drive mount")


def _powershell():
    executable = shutil.which("powershell.exe")
    if not executable:
        raise OSError("Windows PowerShell system executable unavailable")
    path = Path(executable)
    expected = ("windows", "system32", "windowspowershell", "v1.0", "powershell.exe")
    if tuple(part.lower() for part in path.parts[-5:]) != expected or path.absolute() != path.resolve():
        raise ValueError("Unverified Windows PowerShell executable path")
    _windows_mount(path)
    return str(path)


def windows_backing_volume(distribution):
    if not isinstance(distribution, str) or not distribution or len(distribution) > 256:
        raise ValueError("Current WSL distribution identity unavailable")
    request = base64.b64encode(json.dumps({"distribution": distribution}).encode()).decode("ascii")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command",
         WINDOWS_SCRIPT.replace("__REQUEST__", request)],
        capture_output=True, text=True, encoding="utf-8-sig", timeout=20, check=True,
    )
    if len(result.stdout) > 32768:
        raise ValueError("Oversized backing volume observation")
    row = json.loads(result.stdout)
    if (not isinstance(row, dict) or row.get("version") != 1
            or row.get("distribution") != distribution
            or not re.fullmatch(r"host:[A-Z]", row.get("name", ""))
            or not re.fullmatch(r"[A-Za-z]:\\.+\.vhdx", row.get("backing_path", ""), re.I)
            or not re.fullmatch(r"\\\\\?\\Volume\{[0-9a-f-]{36}\}\\", row.get("volume_id", ""), re.I)
            or type(row.get("free")) is not int or type(row.get("total")) is not int
            or not 0 <= row["free"] <= row["total"]):
        raise ValueError("Invalid backing volume observation")
    return {"scope": "wsl_backing", **row}


def _query_diagnostic(path, error):
    """Keep fixed-query stderr private; this is never an ArtifactStore object."""
    stderr = error.stderr
    representation = "bytes" if isinstance(stderr, bytes) else "exception_text_utf8_backslashreplace"
    raw = stderr if isinstance(stderr, bytes) else (stderr or "").encode("utf-8", "backslashreplace")
    retained = raw[:8192]
    result = {"scope": "wsl_backing", "returncode": error.returncode,
              "stderr_representation": representation, "stderr_bytes": len(raw),
              "stderr_sha256": hashlib.sha256(raw).hexdigest(), "retained": False}
    root = Path(path)
    if root.name != ".aa2":
        result["retention_error_type"] = "PrivateStateRootUnavailable"
        return result
    directory = root_fd = None
    try:
        if root.absolute() != root.resolve():
            raise ValueError("Storage diagnostics require a local state root")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(root_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Storage diagnostic root is not owned")
        try:
            os.mkdir("storage-diagnostics", 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        directory = os.open("storage-diagnostics", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Storage diagnostic directory is not private and owned")
        name = "query-" + os.urandom(16).hex() + ".stderr"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Storage diagnostic leaf is not private and owned")
            stream.write(retained)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        result.update(retained=True, diagnostic_id=name, retained_bytes=len(retained),
                      retained_sha256=hashlib.sha256(retained).hexdigest(), truncated=len(raw) > len(retained))
    except (OSError, ValueError) as exc:
        result["retention_error_type"] = type(exc).__name__
    finally:
        for fd in (directory, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as exc:
                    result.update(retained=False, retention_error_type=type(exc).__name__)
    return result


def observe(path, *, backing_volume_reader=None):
    """No cache/native payload reads. Probe errors are facts, never a healthy result."""
    report = {"version": 1, "observed_at": time.time(), "platform": None,
              "volumes": [], "errors": []}
    try:
        system, release = platform.system(), platform.release()
        is_wsl = system == "Linux" and ("microsoft" in release.lower()
            or bool(os.environ.get("WSL_DISTRO_NAME")) or bool(os.environ.get("WSL_INTEROP")))
        report["platform"] = "wsl" if is_wsl else system
        usage = shutil.disk_usage(path)
        report["volumes"].append({"scope": "filesystem", "name": "filesystem",
                                  "path": str(Path(path).absolute()), "free": usage.free, "total": usage.total})
    except OSError as exc:
        report["errors"].append({"scope": "filesystem", "error_type": type(exc).__name__})
        return report
    if is_wsl:
        try:
            reader = windows_backing_volume if backing_volume_reader is None else backing_volume_reader
            report["volumes"].append(reader(os.environ.get("WSL_DISTRO_NAME")))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
            # Preserve the public safe category. Native error text stays outside
            # artifacts/model material, in the already masked state directory.
            failure = {"scope": "wsl_backing", "error_type": type(exc).__name__}
            if interop_timeout(exc):
                failure["reason_code"] = "wsl_interop_timeout"
            report["errors"].append(failure)
            if isinstance(exc, subprocess.CalledProcessError):
                report["diagnostics"] = [_query_diagnostic(path, exc)]
    return report


def interop_timeout(error):
    """The fixed system query observed one explicit WSL interop timeout line."""
    if (not isinstance(error, subprocess.CalledProcessError) or type(error.returncode) is not int
            or error.returncode != 1 or error.stdout not in ("", b"")):
        return False
    raw = error.stderr
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "backslashreplace")
    return (isinstance(raw, bytes) and len(raw) <= 8192 and re.fullmatch(
        rb"<3>WSL \([0-9]+ - [^()\r\n]*\) ERROR: UtilAcceptVsock:[0-9]+: accept4 failed 110\r?\n?", raw) is not None)


def recheck_interop(report, minimum):
    """Unknown capacity remains blocked; only a positively identified transient can recheck."""
    return (report["platform"] == "wsl" and report["errors"] == [{"scope": "wsl_backing",
        "error_type": "CalledProcessError", "reason_code": "wsl_interop_timeout"}]
        and any(row["scope"] == "filesystem" for row in report["volumes"])
        and all(row["free"] >= minimum for row in report["volumes"]))


def boundary(report, minimum, phase):
    low = [row["name"] for row in report["volumes"] if row["free"] < minimum]
    if not report["errors"] and not low:
        return None
    reason = ("无法核验实际存储卷的可用空间" if report["errors"] else
              "磁盘空间仍不足：" + "、".join(low))
    error = ResourceBoundary(
        reason,
        action="释放所列实际存储卷的空间，或修复宿主卷定位/只读探测；保留当前迭代、候选和恢复记录。",
        condition=f"重新核验项目文件系统及适用的 WSL 宿主卷均可用且至少剩余 {minimum} 字节；回答不替代核验。",
        phase=phase, evidence_refs=["resources/storage"],
    )
    error.requirement.evidence = {"authority": "controller_storage_observation",
                                  "minimum_free_bytes": minimum, **report}
    return error


class StorageMonitor:
    """One bounded local probe at a time; cancellation joins its worker."""

    def __init__(self, path, *, backing_volume_reader=None):
        self.path = path
        self._backing_volume_reader = backing_volume_reader
        self._lock = asyncio.Lock()
        self._report = None
        self._at = float("-inf")

    async def sample(self, *, force=False, previous=None):
        async with self._lock:
            if (previous is not None and self._report is not None and self._report is not previous
                    and time.monotonic() - self._at <= 5):
                # A concurrent boundary or periodic observer already produced a
                # newer observation. Reuse that result, including any failure.
                return self._report
            if not force and self._report is not None and time.monotonic() - self._at <= 5:
                return self._report
            # subprocess.run owns and waits for its bounded PowerShell process.
            # joined also waits through cancellation; no orphaned probe thread.
            worker = (asyncio.to_thread(observe, self.path) if self._backing_volume_reader is None else
                      asyncio.to_thread(observe, self.path, backing_volume_reader=self._backing_volume_reader))
            report = await joined(asyncio.create_task(worker))
            self._report, self._at = report, time.monotonic()
            return report
