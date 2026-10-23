"""Windows 部署承载卷：API 采用宿主盘读数；命令、内存、虚拟磁盘均使用假件。"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qtrade_agent.monitor import FakeProcReader
from tests.test_integration_wiring_common import H, TOKEN_READ, close_rig, make_rig


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    run = Mock(side_effect=AssertionError("unexpected subprocess.run"))
    spawn = AsyncMock(side_effect=AssertionError("unexpected subprocess"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    yield
    run.assert_not_called()
    spawn.assert_not_awaited()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    instance = make_rig(tmp_path)
    instance.agent.sampler._reader = FakeProcReader()
    # 这份特意偏大的虚拟盘数值不能被当成 Windows 物理剩余空间。
    virtual_usage = SimpleNamespace(total=1024 ** 4, free=854 * 1024 ** 3, used=170 * 1024 ** 3)
    monkeypatch.setattr(shutil, "disk_usage", Mock(return_value=virtual_usage))
    yield instance
    close_rig(instance)


def host_disks():
    return [
        {"mount": "C:\\", "total_mb": 512000, "free_mb": 299008,
         "source": "windows_volume", "roles": ["app"], "backing_path": "C:\\Apps\\QTrade"},
        {"mount": "D:\\", "total_mb": 1024000, "free_mb": 291328,
         "source": "windows_volume", "roles": ["data"], "backing_path": "D:\\WSL\\ext4.vhdx"},
    ]


def inject_snapshot(rig, value=None, error=None):
    reader = SimpleNamespace(snapshot=AsyncMock(return_value=value, side_effect=error))
    rig.agent.deployment_disks = reader
    return reader


def get_metrics(rig):
    response = rig.client.get("/api/v1/system/metrics?snapshot=1", headers=H(TOKEN_READ))
    assert response.status_code == 200
    return response.json()


def assert_host_unknown(body):
    assert body["hardware"]["disks"] == []
    assert body["hardware"].get("disk_source_error")
    assert body["disk_watermark"]["level"] == "unknown"
    assert body["disk_watermark"]["free_mb"] is None


def test_api_uses_windows_deployment_volumes_instead_of_virtual_free(rig):
    disks = host_disks()
    reader = inject_snapshot(rig, {"disks": disks, "error": None})
    body = get_metrics(rig)
    got = body["hardware"]["disks"]
    assert [(d["mount"], d["total_mb"], d["free_mb"]) for d in got] == [
        ("C:\\", 512000, 299008), ("D:\\", 1024000, 291328),
    ]
    assert body["hardware"].get("disk_source_error") is None
    assert body["disk_watermark"]["free_mb"] == 291328
    reader.snapshot.assert_awaited_once_with()


def test_api_without_host_reader_keeps_disk_and_watermark_unknown(rig):
    rig.agent.deployment_disks = None
    assert_host_unknown(get_metrics(rig))


def test_api_mapping_failure_does_not_fallback_to_virtual_disk(rig):
    inject_snapshot(rig, {"disks": [], "error": "current_distro_mapping_missing"})
    assert_host_unknown(get_metrics(rig))


def test_api_partial_host_mapping_cannot_report_normal_watermark(rig):
    inject_snapshot(rig, {"disks": host_disks()[:1], "error": "data_backing_volume_missing"})
    body = get_metrics(rig)
    assert body["hardware"].get("disk_source_error")
    assert body["disk_watermark"]["level"] == "unknown"
    assert body["disk_watermark"]["free_mb"] is None


@pytest.mark.parametrize("error", [OSError("Windows command failed"), asyncio.TimeoutError()])
def test_api_host_read_failure_is_unknown_without_500(rig, error):
    reader = inject_snapshot(rig, error=error)
    assert_host_unknown(get_metrics(rig))
    reader.snapshot.assert_awaited_once_with()


def test_api_zero_host_free_is_real_zero_and_critical(rig):
    disks = host_disks()
    disks[1]["free_mb"] = 0
    inject_snapshot(rig, {"disks": disks, "error": None})
    body = get_metrics(rig)
    assert body["hardware"]["disks"][1]["free_mb"] == 0
    assert body["disk_watermark"]["free_mb"] == 0
    assert body["disk_watermark"]["level"] == "critical"


def test_metrics_read_does_not_run_cleanup_or_mutate_watermark_state(rig, monkeypatch):
    inject_snapshot(rig, {"disks": host_disks(), "error": None})
    cleanup = Mock(side_effect=AssertionError("GET metrics must not run cleanup"))
    monkeypatch.setattr(rig.agent.maintenance, "check_watermark", cleanup)
    old_level = rig.agent.maintenance.level
    get_metrics(rig)
    cleanup.assert_not_called()
    assert rig.agent.maintenance.level == old_level


CURRENT_DISTRO = "Ubuntu-24.04"
APP_DIR = "/mnt/c/Apps/QTrade"
DATA_DIR = "/home/test/qtrade/data"
WINDOWS_APP = "C:\\Apps\\QTrade"
WINDOWS_DATA = "\\\\wsl.localhost\\Ubuntu-24.04\\home\\test\\qtrade\\data"
VHD = "D:\\WSL\\ext4.vhdx"
MB = 1024 ** 2


def registrations():
    return [
        {"DistributionName": "Ubuntu-22.04", "BasePath": "C:\\Old WSL", "VhdFileName": "ext4.vhdx"},
        {"DistributionName": CURRENT_DISTRO, "BasePath": "D:\\WSL", "VhdFileName": "ext4.vhdx"},
    ]


def volume(path, mount, free_mb, *, volume_id=None):
    return {"path": path, "volume_id": volume_id or f"Volume-{mount[0]}", "mount": mount,
            "total_bytes": 1024000 * MB, "free_bytes": free_mb * MB}


class FakeWindowsRunner:
    """返回 Windows 原始记录，路径选择和同卷合并留给产品 Python 代码执行。"""

    def __init__(self):
        self.path_map = {APP_DIR: WINDOWS_APP, DATA_DIR: WINDOWS_DATA}
        self.registrations = registrations()
        self.volumes = [volume(WINDOWS_APP, "C:\\", 299008), volume(VHD, "D:\\", 291328)]
        self.calls = []
        self.requests = []
        self.scripts = []
        self.fail_at = None
        self.timeout_at = None

    async def __call__(self, argv, *, input=b"", timeout_s):
        assert isinstance(argv, list)
        assert 0 < timeout_s <= 15
        self.calls.append({"argv": list(argv), "input": input, "timeout_s": timeout_s})
        if "wslpath" in argv[0]:
            operation = "wslpath"
            assert "-w" in argv
            assert argv[-1] in self.path_map
            data = self.path_map[argv[-1]] + "\n"
        else:
            assert "-EncodedCommand" in argv
            encoded = argv[argv.index("-EncodedCommand") + 1]
            script = base64.b64decode(encoded).decode("utf-16-le")
            assert script.isascii()
            self.scripts.append(script)
            request = json.loads(input.decode("utf-8"))
            self.requests.append(request)
            operation = request["operation"]
            assert operation in ("registry", "volumes")
            data = json.dumps(self.registrations if operation == "registry" else self.volumes)
        if operation == self.timeout_at:
            raise asyncio.TimeoutError()
        if operation == self.fail_at:
            return 1, b"", b"fake Windows read failed"
        return 0, data.encode("utf-8"), b""


def disk_reader(runner, **kwargs):
    # 业务 API RED 不依赖此新模块导入；实现提供约定 seam 后才跑读取器用例。
    from qtrade_agent.deployment_disks import DeploymentDiskReader
    options = {"runner": runner, "platform": "wsl", "distro_name": CURRENT_DISTRO, "cache_s": 0}
    options.update(kwargs)
    return DeploymentDiskReader(options.pop("app_dir", APP_DIR), options.pop("data_dir", DATA_DIR), **options)


def test_resolve_paths_selects_current_distro_not_another_ubuntu():
    from qtrade_agent.deployment_disks import resolve_windows_paths
    result = resolve_windows_paths({"app": WINDOWS_APP, "data": WINDOWS_DATA}, CURRENT_DISTRO, registrations())
    assert result == {"app": WINDOWS_APP, "data": VHD}


@pytest.mark.asyncio
async def test_reader_maps_app_c_and_current_distro_data_d_only():
    runner = FakeWindowsRunner()
    result = await disk_reader(runner).snapshot()
    assert result["error"] is None
    disks = {row["mount"]: row for row in result["disks"]}
    assert set(disks) == {"C:\\", "D:\\"}
    assert disks["C:\\"]["free_mb"] == 299008
    assert disks["D:\\"]["free_mb"] == 291328
    assert set(disks["C:\\"]["roles"]) == {"app"}
    assert set(disks["D:\\"]["roles"]) == {"data"}
    assert VHD in disks["D:\\"]["backing_path"]
    request = next(r for r in runner.requests if r["operation"] == "volumes")
    assert set(request["paths"]) == {WINDOWS_APP, VHD}
    assert "C:\\Old WSL\\ext4.vhdx" not in request["paths"]


@pytest.mark.asyncio
async def test_reader_same_volume_merges_roles_and_reports_capacity_once():
    runner = FakeWindowsRunner()
    same_app = "D:\\Apps\\QTrade"
    runner.path_map[APP_DIR] = same_app
    runner.volumes = [volume(same_app, "D:\\", 291328), volume(VHD, "D:\\", 291328)]
    result = await disk_reader(runner).snapshot()
    assert result["error"] is None
    assert len(result["disks"]) == 1
    row = result["disks"][0]
    assert set(row["roles"]) == {"app", "data"}
    assert row["free_mb"] == 291328
    assert row["total_mb"] == 1024000


@pytest.mark.asyncio
async def test_reader_unrelated_fixed_volume_is_not_a_deployment_candidate():
    runner = FakeWindowsRunner()
    runner.volumes.append(volume("E:\\Unrelated", "E:\\", 1))
    result = await disk_reader(runner).snapshot()
    assert {row["mount"] for row in result["disks"]} == {"C:\\", "D:\\"}
    request = next(r for r in runner.requests if r["operation"] == "volumes")
    assert set(request["paths"]) == {WINDOWS_APP, VHD}


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [],
    [{"DistributionName": "Ubuntu-22.04", "BasePath": "C:\\Wrong"}],
    [{"DistributionName": CURRENT_DISTRO, "VhdFileName": "ext4.vhdx"}],
])
async def test_reader_missing_current_distro_mapping_is_unknown(rows):
    runner = FakeWindowsRunner()
    runner.registrations = rows
    result = await disk_reader(runner).snapshot()
    assert result["error"]
    assert not any(row.get("mount") == "D:\\" for row in result["disks"])
    assert all(not str(row.get("mount", "")).startswith("/") for row in result["disks"])


@pytest.mark.asyncio
async def test_reader_linux_without_windows_host_is_unknown_without_running_commands():
    runner = FakeWindowsRunner()
    result = await disk_reader(runner, platform="linux", distro_name=None).snapshot()
    assert result["disks"] == []
    assert result["error"]
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["wslpath", "registry", "volumes"])
async def test_reader_command_failure_is_unknown(operation):
    runner = FakeWindowsRunner()
    runner.fail_at = operation
    result = await disk_reader(runner).snapshot()
    assert result["error"]
    assert not any(row.get("free_mb") == 854 * 1024 for row in result["disks"])


@pytest.mark.asyncio
async def test_reader_command_timeout_is_unknown():
    runner = FakeWindowsRunner()
    runner.timeout_at = "volumes"
    result = await disk_reader(runner).snapshot()
    assert result["disks"] == []
    assert result["error"]


@pytest.mark.asyncio
async def test_reader_zero_free_bytes_remains_zero():
    runner = FakeWindowsRunner()
    runner.volumes[1]["free_bytes"] = 0
    result = await disk_reader(runner).snapshot()
    assert result["error"] is None
    assert next(row for row in result["disks"] if row["mount"] == "D:\\")["free_mb"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("free_bytes", None), ("total_bytes", None)])
async def test_reader_missing_volume_measurement_is_not_fabricated_as_zero(field, value):
    runner = FakeWindowsRunner()
    runner.volumes[1][field] = value
    result = await disk_reader(runner).snapshot()
    assert result["error"]
    for row in result["disks"]:
        if row["mount"] == "D:\\":
            assert row.get(field.replace("bytes", "mb")) is None


@pytest.mark.asyncio
async def test_reader_paths_with_spaces_quotes_and_dollar_are_data_not_powershell_code():
    runner = FakeWindowsRunner()
    app = '/mnt/c/Program Files/QTrade "disk-test-marker"/$(Write-Output injected)'
    win_app = 'C:\\Program Files\\QTrade "disk-test-marker"\\$(Write-Output injected)'
    runner.path_map[app] = win_app
    runner.volumes[0]["path"] = win_app
    result = await disk_reader(runner, app_dir=app).snapshot()
    assert result["error"] is None
    wsl_calls = [call for call in runner.calls if "wslpath" in call["argv"][0]]
    assert any(call["argv"][-1] == app for call in wsl_calls)
    request = next(r for r in runner.requests if r["operation"] == "volumes")
    assert win_app in request["paths"]
    assert runner.scripts
    assert all("disk-test-marker" not in script and "Write-Output injected" not in script for script in runner.scripts)


@pytest.mark.asyncio
async def test_reader_whole_probe_has_bounded_wait(monkeypatch):
    real_wait_for = asyncio.wait_for
    real_timeout = asyncio.timeout
    timeouts = []

    async def bounded_wait(awaitable, timeout):
        assert 0 < timeout <= 15
        timeouts.append(timeout)
        return await real_wait_for(awaitable, 0.005)

    def bounded_timeout(delay):
        assert 0 < delay <= 15
        timeouts.append(delay)
        return real_timeout(0.005)

    runner = FakeWindowsRunner()

    async def stalled_runner(argv, *, input, timeout_s):
        await asyncio.sleep(0.05)
        return await runner(argv, input=input, timeout_s=timeout_s)

    monkeypatch.setattr(asyncio, "wait_for", bounded_wait)
    monkeypatch.setattr(asyncio, "timeout", bounded_timeout)
    # 缩短计时尺度，而不缩窄实现可选的有界等待机制；外层看门狗防测试自身挂起。
    result = await real_wait_for(disk_reader(stalled_runner).snapshot(), 0.3)
    assert result["disks"] == []
    assert "timeout" in result["error"]
    assert timeouts


def get_health(rig):
    response = rig.client.get("/api/v1/system/health", headers=H(TOKEN_READ))
    assert response.status_code == 200
    return response.json()


def test_health_data_volume_is_not_all_deployment_volumes_minimum(rig):
    disks = host_disks()
    disks[0]["free_mb"], disks[1]["free_mb"] = 100, 200
    reader = inject_snapshot(rig, {"disks": disks, "error": None})
    metrics = get_metrics(rig)
    health = get_health(rig)
    assert metrics["disk_watermark"]["free_mb"] == 100
    assert health["disk_free_mb"] == 200
    assert reader.snapshot.await_count == 2


def test_health_same_volume_with_app_and_data_roles_is_used(rig):
    disk = host_disks()[1]
    disk.update(roles=["app", "data"], free_mb=200)
    inject_snapshot(rig, {"disks": [disk], "error": None})
    assert get_health(rig)["disk_free_mb"] == 200


def test_health_without_host_reader_is_unknown_not_linux_virtual_free(rig):
    rig.agent.deployment_disks = None
    assert get_health(rig)["disk_free_mb"] is None


@pytest.mark.parametrize("snapshot", [
    {"disks": host_disks(), "error": "partial_host_mapping"},
    {"disks": host_disks()[:1], "error": None},
])
def test_health_partial_error_or_missing_data_role_is_unknown(rig, snapshot):
    inject_snapshot(rig, snapshot)
    assert get_health(rig)["disk_free_mb"] is None


@pytest.mark.parametrize("error", [OSError("host volume failed"), asyncio.TimeoutError()])
def test_health_reader_failure_is_unknown_without_500(rig, error):
    reader = inject_snapshot(rig, error=error)
    assert get_health(rig)["disk_free_mb"] is None
    reader.snapshot.assert_awaited_once_with()


def test_health_zero_data_volume_free_is_real_zero(rig):
    disks = host_disks()
    disks[1]["free_mb"] = 0
    inject_snapshot(rig, {"disks": disks, "error": None})
    assert get_health(rig)["disk_free_mb"] == 0


@pytest.mark.asyncio
async def test_reader_successive_snapshots_reuse_cached_windows_probe():
    runner = FakeWindowsRunner()
    reader = disk_reader(runner, cache_s=30, clock=lambda: 100.0)
    first = await reader.snapshot()
    count = len(runner.calls)
    second = await reader.snapshot()
    assert first == second
    assert first["error"] is None
    assert len(runner.calls) == count
    assert [row["operation"] for row in runner.requests].count("volumes") == 1
