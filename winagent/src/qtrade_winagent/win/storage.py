"""Read-only deployment-volume queries in the installing user's Windows session."""
from __future__ import annotations

import base64
import json
import ntpath
import os
import re
import subprocess

from . import hidden_process_kwargs, require_windows


WINDOWS_VOLUME_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ($request.operation -eq 'registry') {
    $root = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    $rows = @()
    if (Test-Path -LiteralPath $root) {
        $rows = @(Get-ChildItem -LiteralPath $root |
            ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } |
            Where-Object { $_.DistributionName -eq $request.distro } |
            Select-Object DistributionName, BasePath, VhdFileName)
    }
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

_LOCAL_PATH = re.compile(
    r"^(?:[a-z]:\\|\\\\\?\\(?:[a-z]:\\|Volume\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}\\))",
    re.IGNORECASE,
)


def validate_volume_request(request: object) -> dict:
    """Only two read operations and at most two local absolute paths are accepted."""
    if not isinstance(request, dict):
        raise ValueError("invalid_volume_request")
    operation = request.get("operation")
    if operation == "registry" and set(request) == {"operation"}:
        return {"operation": operation}
    if operation != "volumes" or set(request) != {"operation", "paths"}:
        raise ValueError("invalid_volume_request")
    paths = request.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 2:
        raise ValueError("invalid_volume_paths")
    for path in paths:
        if (not isinstance(path, str) or not path or len(path) > 32767 or "\x00" in path
                or not _LOCAL_PATH.match(path) or not ntpath.isabs(path)):
            raise ValueError("invalid_volume_paths")
    return {"operation": operation, "paths": list(dict.fromkeys(paths))}


def probe_deployment_volumes(request: dict, *, distro: str) -> list[dict]:
    """Run a fixed script with JSON stdin, no shell interpolation or visible console."""
    payload = validate_volume_request(request)
    require_windows("deployment volumes")
    if payload["operation"] == "registry":
        if not isinstance(distro, str) or not distro:
            raise ValueError("current_distro_mapping_missing")
        payload["distro"] = distro
    system_root = os.environ.get("SystemRoot")
    if not system_root or not ntpath.isabs(system_root):
        raise ValueError("windows_system_root_unavailable")
    exe = ntpath.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    encoded = base64.b64encode(WINDOWS_VOLUME_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                input=json.dumps(payload, ensure_ascii=True).encode("ascii"),
                                capture_output=True, timeout=9, **hidden_process_kwargs())
    except subprocess.TimeoutExpired:
        raise ValueError("windows_volume_timeout") from None
    except OSError:
        raise ValueError("windows_volume_command_unavailable") from None
    if result.returncode:
        raise ValueError("windows_volume_command_failed")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ValueError("windows_volume_response_invalid") from None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("windows_volume_response_invalid")
    return rows
