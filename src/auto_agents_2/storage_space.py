"""Observe real storage boundaries without treating WSL guest capacity as host space."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import re
import shutil
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


def observe(path):
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
            report["volumes"].append(windows_backing_volume(os.environ.get("WSL_DISTRO_NAME")))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
            # Native stderr can contain host details; expose only an error category.
            report["errors"].append({"scope": "wsl_backing", "error_type": type(exc).__name__})
    return report


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

    def __init__(self, path):
        self.path = path
        self._lock = asyncio.Lock()
        self._report = None
        self._at = float("-inf")

    async def sample(self, *, force=False):
        async with self._lock:
            if not force and self._report is not None and time.monotonic() - self._at <= 5:
                return self._report
            # subprocess.run owns and waits for its bounded PowerShell process.
            # joined also waits through cancellation; no orphaned probe thread.
            report = await joined(asyncio.create_task(asyncio.to_thread(observe, self.path)))
            self._report, self._at = report, time.monotonic()
            return report
