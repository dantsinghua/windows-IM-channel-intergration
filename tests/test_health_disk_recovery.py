"""systemd 环境部署承载卷恢复；WSL 与 Windows 读数均为假件。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_agent.deployment_disks import DeploymentDiskReader
from qtrade_agent.config import WinAgentConfig
from qtrade_agent.winagent_client import WinAgentClient
from tests.test_windows_deployment_disks import APP_DIR, DATA_DIR, VHD, WINDOWS_APP, WINDOWS_DATA, volume


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    paths = {APP_DIR: WINDOWS_APP, DATA_DIR: WINDOWS_DATA.replace("Ubuntu-24.04", "qtrade")}
    requests = []

    async def runner(argv, *, input, timeout_s):
        assert argv[0] == "wslpath", "systemd must not depend on Windows executable discovery"
        return 0, paths[argv[-1]].encode(), b""

    async def windows_probe(request):
        requests.append(request)
        if request["operation"] == "registry":
            return [{"DistributionName": "qtrade", "BasePath": "D:\\WSL", "VhdFileName": "ext4.vhdx"}]
        return [volume(WINDOWS_APP, "C:\\", 10000), volume(VHD, "D:\\", 20000)]

    return SimpleNamespace(runner=runner, windows_probe=windows_probe, paths=paths, requests=requests)


async def test_systemd_without_windows_path_or_distro_env_reads_actual_host_volumes(bridge):
    reader = DeploymentDiskReader(APP_DIR, DATA_DIR, platform="wsl", runner=bridge.runner,
                                  windows_probe=bridge.windows_probe, cache_s=0)
    result = await reader.snapshot()
    assert result["error"] is None
    assert {(row["mount"], row["free_mb"]) for row in result["disks"]} == {("C:\\", 10000), ("D:\\", 20000)}
    assert bridge.requests == [{"operation": "registry"}, {"operation": "volumes", "paths": [WINDOWS_APP, VHD]}]


async def test_distro_env_cannot_redirect_current_unc_to_another_installation(bridge):
    reader = DeploymentDiskReader(APP_DIR, DATA_DIR, platform="wsl", runner=bridge.runner,
                                  windows_probe=bridge.windows_probe, distro_name="another-distro", cache_s=0)
    result = await reader.snapshot()
    assert result["disks"] == []
    assert result["error"]
    assert not any(request["operation"] == "volumes" for request in bridge.requests)


async def test_missing_current_registration_does_not_use_first_other_distribution(bridge):
    async def wrong_registration(request):
        assert request["operation"] == "registry"
        return [{"DistributionName": "unrelated", "BasePath": "E:\\Other", "VhdFileName": "ext4.vhdx"}]
    reader = DeploymentDiskReader(APP_DIR, DATA_DIR, platform="wsl", runner=bridge.runner,
                                  windows_probe=wrong_registration, cache_s=0)
    result = await reader.snapshot()
    assert result["disks"] == []
    assert result["error"] == "current_distro_mapping_missing"


async def test_bridge_failure_remains_unknown_without_virtual_disk_fallback(bridge):
    reader = DeploymentDiskReader(APP_DIR, DATA_DIR, platform="wsl", runner=bridge.runner,
                                  windows_probe=AsyncMock(side_effect=TimeoutError()), cache_s=0)
    result = await reader.snapshot()
    assert result["disks"] == []
    assert result["error"] == "deployment_disk_timeout"


async def test_bridge_http_has_bounded_budget_and_no_transport_replay(monkeypatch):
    wa = WinAgentClient(WinAgentConfig(), base_url="http://fake.invalid", token="synthetic", transport=AsyncMock())
    request = AsyncMock(return_value=(200, {"rows": []}))
    monkeypatch.setattr(wa, "request", request)
    await wa.deployment_volume_probe({"operation": "registry"})
    assert request.call_args.args == ("POST", "/wa/v1/metrics/deployment-volumes")
    assert request.call_args.kwargs["timeout_s"] == 12
    assert request.call_args.kwargs["retry"] is False
