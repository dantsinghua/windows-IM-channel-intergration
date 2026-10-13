"""部署卷只读桥接权限与输入边界；没有真实 PowerShell 或 Windows 调用。"""
from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import Mock
from types import SimpleNamespace
import base64
import json
import subprocess

import pytest

from tests.conftest import AGENT_TOKEN, CONSOLE_TOKEN, INSTALLER_TOKEN, client


PATH = "/wa/v1/metrics/deployment-volumes"


@pytest.mark.parametrize("token", [CONSOLE_TOKEN, INSTALLER_TOKEN, None])
async def test_deployment_volume_probe_rejects_nonagent_before_ipc(rig_with_user, monkeypatch, token):
    call = AsyncMock(return_value={"rows": []})
    monkeypatch.setattr(rig_with_user.deps.hub, "call", call)
    async with client(rig_with_user, token=token) as http:
        response = await http.post(PATH, json={"operation": "registry"})
    assert response.status_code == (401 if token is None else 403)
    call.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {"operation": "execute", "command": "Get-Process"},
    {"operation": "volumes", "paths": []},
    {"operation": "volumes", "paths": ["C:\\a", "D:\\b", "E:\\c"]},
    {"operation": "volumes", "paths": ["relative"]},
    {"operation": "volumes", "paths": ["\\\\server\\share\\data"]},
    {"operation": "volumes", "paths": ["\\\\?\\UNC\\server\\share"]},
    {"operation": "volumes", "paths": ["\\\\.\\PhysicalDrive0"]},
    {"operation": "volumes", "paths": ["\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1"]},
    {"operation": "volumes", "paths": ["\\\\?\\Volume{--}\\data"]},
    {"operation": "volumes", "paths": ["C:relative"]},
    {"operation": "volumes", "paths": ["C:\\data\u0000hidden"]},
])
async def test_invalid_probe_request_never_reaches_windows(rig_with_user, monkeypatch, body):
    call = AsyncMock(return_value={"rows": []})
    monkeypatch.setattr(rig_with_user.deps.hub, "call", call)
    async with client(rig_with_user, token=AGENT_TOKEN) as http:
        response = await http.post(PATH, json=body)
    assert response.status_code in (400, 422)
    call.assert_not_awaited()


@pytest.mark.parametrize("body", [{"operation": "registry"},
    {"operation": "volumes", "paths": ["D:\\用户 空间\\ext4.vhdx"]},
    {"operation": "volumes", "paths": ["\\\\?\\C:\\ProgramData\\QTrade-E2E\\wsl\\distro"]},
    {"operation": "volumes", "paths": ["\\\\?\\Volume{12345678-1234-1234-1234-1234567890ab}\\data"]}])
async def test_valid_probe_keeps_exact_path_and_bounded_service_deadline(rig_with_user, monkeypatch, body):
    call = AsyncMock(return_value={"rows": []})
    monkeypatch.setattr(rig_with_user.deps.hub, "call", call)
    async with client(rig_with_user, token=AGENT_TOKEN) as http:
        response = await http.post(PATH, json=body)
    assert response.status_code == 200
    assert call.await_count == 1
    assert call.call_args.args[:2] == ("metrics.deployment-volumes", body)
    assert call.call_args.kwargs["timeout_s"] == 11


@pytest.mark.parametrize("operation", ["registry", "volumes"])
def test_native_probe_uses_hidden_fixed_script_and_json_stdin(monkeypatch, operation):
    from qtrade_winagent import win
    from qtrade_winagent.win import storage
    monkeypatch.setattr(win, "is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", "Z:\\Windows")
    query = {"operation": operation}
    exact_path = "D:\\用户 $literal (not-code)\\data"
    if operation == "volumes":
        query["paths"] = [exact_path]
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"[]"))
    monkeypatch.setattr(storage.subprocess, "run", run)
    assert storage.probe_deployment_volumes(query, distro="qtrade custom") == []
    argv = run.call_args.args[0]
    assert argv[0] == "Z:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert script.isascii()
    assert exact_path not in script and "qtrade custom" not in script
    assert not run.call_args.kwargs.get("shell")
    assert run.call_args.kwargs["creationflags"] & 0x08000000
    assert run.call_args.kwargs["timeout"] == 9
    payload = json.loads(run.call_args.kwargs["input"])
    assert payload == (query | {"distro": "qtrade custom"} if operation == "registry" else query)


@pytest.mark.parametrize("kind", ["timeout", "exit", "json"])
def test_native_probe_errors_are_bounded_and_do_not_expose_raw_windows_output(monkeypatch, kind):
    from qtrade_winagent import win
    from qtrade_winagent.win import storage
    monkeypatch.setattr(win, "is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    private = "sensitive synthetic details"
    run = Mock(return_value=SimpleNamespace(returncode=1 if kind == "exit" else 0, stdout=private.encode()))
    if kind == "timeout":
        run.side_effect = subprocess.TimeoutExpired(private, 9)
    monkeypatch.setattr(storage.subprocess, "run", run)
    with pytest.raises(ValueError) as failure:
        storage.probe_deployment_volumes({"operation": "registry"}, distro="qtrade")
    assert private not in str(failure.value)
