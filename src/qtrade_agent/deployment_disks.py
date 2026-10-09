"""只读部署承载卷：WSL 路径映射到 Windows 卷，不把 VHD 内部容量当宿主余量。"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import math
import ntpath
import os
import platform as platform_module
import re
import shutil
import sys
import time
from pathlib import Path


# All PowerShell source, comments and output strings must remain ASCII.
WINDOWS_PROBE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ($request.operation -eq 'registry') {
    $rows = @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' |
        ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } |
        Select-Object DistributionName, BasePath, VhdFileName)
} elseif ($request.operation -eq 'volumes') {
    $rows = @(foreach ($path in $request.paths) {
        $item = Get-Item -LiteralPath $path
        $volumes = @(Get-Volume -FilePath $item.FullName)
        if ($volumes.Count -ne 1) { throw 'volume_identity_unavailable' }
        $volume = $volumes[0]
        $mount = [string]$volume.Path
        if ($volume.DriveLetter) { $mount = [string]$volume.DriveLetter + ':\' }
        [PSCustomObject]@{
            path = [string]$path
            volume_id = [string]$volume.UniqueId
            mount = $mount
            total_bytes = $volume.Size
            free_bytes = $volume.SizeRemaining
        }
    })
} else { throw 'unknown_probe_operation' }
ConvertTo-Json -InputObject @($rows) -Depth 5 -Compress
"""


async def run_probe(argv: list[str], *, input: bytes, timeout_s: float) -> tuple[int, bytes, bytes]:
    """有界只读子进程；取消时只终止并回收本次句柄，不留后台 PowerShell。"""
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output = asyncio.create_task(proc.communicate(input))
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout_s)
        return proc.returncode, stdout, stderr
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(asyncio.shield(output), 2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            while not output.done():
                try:
                    await asyncio.shield(output)
                except asyncio.CancelledError:
                    continue
        raise


_WSL_UNC = re.compile(r"^\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)(?:\\|$)", re.IGNORECASE)


def resolve_windows_paths(paths: dict[str, str], distro_name: str,
                          registrations: list[dict]) -> dict[str, str]:
    """只将当前发行版的 UNC 解析成其 VHD；普通 Windows 路径保持实际路径。"""
    result = {}
    for role, path in paths.items():
        match = _WSL_UNC.match(path)
        if match:
            if not distro_name or match.group(1).casefold() != distro_name.casefold():
                raise ValueError("current_distro_path_mismatch")
            selected = [row for row in registrations if str(row.get("DistributionName", "")).casefold() == distro_name.casefold()]
            if len(selected) != 1 or not selected[0].get("BasePath"):
                raise ValueError("current_distro_mapping_missing")
            base = selected[0]["BasePath"]
            filename = selected[0].get("VhdFileName") or "ext4.vhdx"
            if not isinstance(base, str) or not isinstance(filename, str) or ntpath.basename(filename) != filename:
                raise ValueError("invalid_distro_backing_path")
            path = ntpath.join(base, filename)
        if not isinstance(path, str) or not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]:
            raise ValueError("windows_path_unavailable")
        result[role] = ntpath.normpath(path)
    return result


def _capacity(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("volume_capacity_unavailable")
    return int(value)


def _windows_disks(paths: dict[str, str], rows: list[dict]) -> list[dict]:
    disks: dict[str, dict] = {}
    for role, path in paths.items():
        selected = [row for row in rows if isinstance(row.get("path"), str)
                    and ntpath.normcase(ntpath.normpath(row["path"])) == ntpath.normcase(path)]
        if len(selected) != 1:
            raise ValueError("requested_volume_unavailable")
        row = selected[0]
        identity, mount = row.get("volume_id"), row.get("mount")
        if not isinstance(identity, str) or not identity or not isinstance(mount, str) or not mount:
            raise ValueError("volume_identity_unavailable")
        total, free = _capacity(row.get("total_bytes")), _capacity(row.get("free_bytes"))
        if free > total:
            raise ValueError("invalid_volume_capacity")
        key = identity.casefold()
        if key not in disks:
            disks[key] = {"mount": mount, "total_mb": total // 1048576, "free_mb": free // 1048576,
                          "source": "windows_volume", "roles": [], "backing_path": path}
        disk = disks[key]
        if disk["total_mb"] != total // 1048576:
            raise ValueError("inconsistent_volume_capacity")
        disk["free_mb"] = min(disk["free_mb"], free // 1048576)
        disk["roles"].append(role)
        if path not in disk["backing_path"].split(" ; "):
            disk["backing_path"] += " ; " + path
    return list(disks.values())


class DeploymentDiskReader:
    def __init__(self, app_dir: str, data_dir: str, *, runner=None, platform: str | None = None,
                 distro_name: str | None = None, clock=time.monotonic, cache_s: float = 30):
        self.paths = {"app": app_dir, "data": data_dir}
        self.platform = platform or ("win32" if sys.platform == "win32" else
                                     "wsl" if os.environ.get("WSL_DISTRO_NAME") or "microsoft" in platform_module.release().lower() else "linux")
        self.distro_name = os.environ.get("WSL_DISTRO_NAME", "") if distro_name is None else distro_name
        self._runner = runner or run_probe
        self._clock, self._cache_s = clock, cache_s
        self._cache = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def snapshot(self) -> dict:
        async with self._lock:
            if self._cache is not None and self._clock() < self._expires:
                return copy.deepcopy(self._cache)
            try:
                async with asyncio.timeout(15):
                    disks = await self._windows() if self.platform in ("wsl", "win32") else await asyncio.to_thread(self._linux)
                result = {"disks": disks, "error": None}
            except Exception as exc:
                # Do not expose command text, registry content or raw Windows errors to the UI.
                error = str(exc) if isinstance(exc, ValueError) else "deployment_disk_timeout" if isinstance(exc, TimeoutError) else "deployment_disk_probe_failed"
                result = {"disks": [], "error": error}
            self._cache, self._expires = result, self._clock() + self._cache_s
            return copy.deepcopy(result)

    async def _powershell(self, request: dict) -> list[dict]:
        encoded = base64.b64encode(WINDOWS_PROBE_SCRIPT.encode("utf-16-le")).decode("ascii")
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        rc, out, _ = await self._runner(argv, input=json.dumps(request, ensure_ascii=True).encode("ascii"), timeout_s=15)
        if rc:
            raise ValueError("windows_volume_command_failed")
        rows = json.loads(out.decode("utf-8-sig"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("windows_volume_response_invalid")
        return rows

    async def _windows(self) -> list[dict]:
        paths = dict(self.paths)
        if self.platform == "wsl":
            for role, path in paths.items():
                rc, out, _ = await self._runner(["wslpath", "-w", str(Path(path).resolve())], input=b"", timeout_s=15)
                if rc or not out.strip():
                    raise ValueError("windows_path_conversion_failed")
                paths[role] = out.decode("utf-8").strip()
        registrations = await self._powershell({"operation": "registry"}) if any(_WSL_UNC.match(p) for p in paths.values()) else []
        backing = resolve_windows_paths(paths, self.distro_name, registrations)
        rows = await self._powershell({"operation": "volumes", "paths": list(dict.fromkeys(backing.values()))})
        return _windows_disks(backing, rows)

    def _linux(self) -> list[dict]:
        disks = {}
        for role, raw_path in self.paths.items():
            path = Path(raw_path).resolve(strict=True)
            device = path.stat().st_dev
            usage = shutil.disk_usage(path)
            mount = path if path.is_dir() else path.parent
            while mount.parent != mount and mount.parent.stat().st_dev == device:
                mount = mount.parent
            if device not in disks:
                disks[device] = {"mount": str(mount), "total_mb": usage.total // 1048576, "free_mb": usage.free // 1048576,
                                 "source": "native_filesystem", "roles": [], "backing_path": str(path)}
            disk = disks[device]
            disk["roles"].append(role)
            disk["free_mb"] = min(disk["free_mb"], usage.free // 1048576)
            if str(path) not in disk["backing_path"].split(" ; "):
                disk["backing_path"] += " ; " + str(path)
        return list(disks.values())
