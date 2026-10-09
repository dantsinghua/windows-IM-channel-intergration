"""#73 系统版本只读来源：真实 API 映射，系统读取与命令全部使用假件。"""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qtrade_agent import sysenv
from qtrade_agent.runtime import backends
from qtrade_agent.sysenv import LinuxSysReader, WslEnvReader
from tests.test_integration_wiring_common import H, TOKEN_READ, close_rig, make_rig


@pytest.fixture(autouse=True)
def reject_unmocked_system_commands(monkeypatch):
    run = Mock(side_effect=AssertionError("unexpected real subprocess.run"))
    spawn = AsyncMock(side_effect=AssertionError("unexpected real subprocess"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    yield
    # 命令层用例另外注入自己的假 run；其他用例不得偷偷走真实命令兜底。
    run.assert_not_called()
    spawn.assert_not_awaited()


@pytest.fixture
def rig(tmp_path):
    instance = make_rig(tmp_path)
    assert instance.agent.wsl_env_reader is None
    yield instance
    close_rig(instance)


def test_api_maps_injected_readonly_versions(rig):
    reader = SimpleNamespace(versions=AsyncMock(return_value={
        "kernel": "6.6.87.2-test-binder", "docker": "27.5.1", "distro": "Ubuntu 22.04.5 LTS",
    }))
    rig.agent.wsl_env_reader = reader

    response = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ))

    assert response.status_code == 200
    body = response.json()
    assert body["kernel"] == "6.6.87.2-test-binder"
    assert body["docker"] == "27.5.1"
    assert body["distro"] == "Ubuntu 22.04.5 LTS"
    assert body.get("kernel_state") is None
    assert body.get("wsl_state") is None
    reader.versions.assert_awaited_once_with()


def test_api_no_reader_preserves_unknown_without_creating_real_reader(rig):
    response = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ))
    assert response.status_code == 200
    body = response.json()
    for key in ("kernel", "docker", "distro", "wsl", "kernel_state", "wsl_state"):
        assert body.get(key) is None
    assert rig.agent.wsl_env_reader is None


def test_api_reader_failure_keeps_metadata_available_without_500(rig):
    reader = SimpleNamespace(versions=AsyncMock(side_effect=OSError("test source unavailable")))
    rig.agent.wsl_env_reader = reader
    response = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["agent"]["version"]
    assert body["schema_version"] == 1
    for key in ("kernel", "docker", "distro", "kernel_state", "wsl_state"):
        assert body.get(key) is None
    reader.versions.assert_awaited_once_with()


def test_api_reader_cannot_overwrite_trusted_metadata(rig):
    before = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ)).json()
    rig.agent.wsl_env_reader = SimpleNamespace(versions=AsyncMock(return_value={
        "kernel": "6.6.87.2-test", "docker": "27.5.1", "distro": "Test Linux",
        "agent": {"version": "untrusted"}, "api_version": "999.0", "schema_version": 999,
        "capabilities_version": "untrusted", "ok": False, "trace_id": "untrusted",
        "images": {"untrusted": "value"}, "unexpected_field": "must not leak",
    }))
    response = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ))
    assert response.status_code == 200
    body = response.json()
    assert body["docker"] == "27.5.1"
    for key in ("agent", "api_version", "schema_version", "capabilities_version", "ok", "images"):
        assert body[key] == before[key]
    assert body["trace_id"] != "untrusted"
    assert "unexpected_field" not in body


def test_api_authenticates_before_reading_versions(rig):
    reader = SimpleNamespace(versions=AsyncMock(return_value={"docker": "27.5.1"}))
    rig.agent.wsl_env_reader = reader
    assert rig.client.get("/api/v1/system/version").status_code == 401
    reader.versions.assert_not_awaited()


@pytest.mark.parametrize("health", [None, False, True])
def test_reading_docker_version_does_not_set_health(rig, health):
    rig.agent.health.dockerd_ok = health
    rig.agent.wsl_env_reader = SimpleNamespace(versions=AsyncMock(return_value={"docker": "27.5.1"}))
    response = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ))
    assert response.status_code == 200
    assert response.json()["docker"] == "27.5.1"
    assert rig.agent.health.dockerd_ok is health


class FakeBase:
    def snapshot(self):
        return {}


def fake_sys(*, missing=False):
    def uname():
        if missing:
            raise OSError("test uname unavailable")
        return SimpleNamespace(sysname="Linux", release="6.6.87.2-test-binder", version="#1 TEST", machine="x86_64")

    return LinuxSysReader(
        read_text=lambda _path: None if missing else 'ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04.5 LTS"\n',
        uname=uname,
        disk_usage=Mock(side_effect=AssertionError("version read must not inspect disks")),
        df_paths=(),
    )


@pytest.mark.asyncio
async def test_reader_uses_injected_sources_without_inferring_kernel_ownership():
    docker_version = AsyncMock(return_value="27.5.1")
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys(), docker_version=docker_version)
    result = await reader.versions()
    assert result["kernel"] == "6.6.87.2-test-binder"
    assert result["docker"] == "27.5.1"
    assert result["distro"] == "Ubuntu 22.04.5 LTS"
    assert result.get("kernel_state") is None
    assert result.get("wsl_state") is None
    docker_version.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reader_missing_sources_are_unknown():
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys(missing=True), docker_version=AsyncMock(return_value=None))
    result = await reader.versions()
    for key in ("kernel", "docker", "distro", "kernel_state", "wsl_state"):
        assert result.get(key) is None


@pytest.mark.asyncio
async def test_docker_getter_failure_does_not_discard_other_readonly_versions():
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys(), docker_version=AsyncMock(side_effect=OSError("unavailable")))
    result = await reader.versions()
    assert result["docker"] is None
    assert result["kernel"] == "6.6.87.2-test-binder"
    assert result["distro"] == "Ubuntu 22.04.5 LTS"


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,stdout,expected", [
    (0, "27.5.1\n", "27.5.1"),
    (0, " \n\t", None),
    (1, "Cannot connect to the Docker daemon", None),
])
async def test_default_docker_version_command_is_readonly_bounded_and_checks_exit(monkeypatch, returncode, stdout, expected):
    async def run(argv, *, timeout_s):
        assert argv == ["docker", "version", "--format", "{{.Server.Version}}"]
        assert 0 < timeout_s <= 5
        return returncode, stdout

    fake_run = AsyncMock(side_effect=run)
    monkeypatch.setattr(backends, "_run_cancellable", fake_run)
    monkeypatch.setattr(sysenv, "_run_cancellable", fake_run, raising=False)
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys())
    result = await reader.versions()
    assert result["docker"] == expected
    fake_run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [FileNotFoundError("docker missing"), asyncio.TimeoutError()])
async def test_default_docker_version_errors_remain_unknown(monkeypatch, error):
    fake_run = AsyncMock(side_effect=error)
    monkeypatch.setattr(backends, "_run_cancellable", fake_run)
    monkeypatch.setattr(sysenv, "_run_cancellable", fake_run, raising=False)
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys())
    result = await reader.versions()
    assert result["docker"] is None
    assert result["kernel"] == "6.6.87.2-test-binder"
    assert result.get("kernel_state") is None
    fake_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_injected_getter_has_bounded_outer_wait(monkeypatch):
    real_wait_for = asyncio.wait_for
    timeouts = []

    async def bounded_wait(awaitable, timeout):
        assert 0 < timeout <= 10
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", bounded_wait)
    getter = AsyncMock(side_effect=asyncio.TimeoutError())
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys(), docker_version=getter)
    result = await reader.versions()
    assert timeouts
    assert result["docker"] is None
    assert result["kernel"] == "6.6.87.2-test-binder"
    getter.assert_awaited_once()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_disguised_as_unknown(monkeypatch):
    fake_run = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(backends, "_run_cancellable", fake_run)
    monkeypatch.setattr(sysenv, "_run_cancellable", fake_run, raising=False)
    reader = WslEnvReader(base=FakeBase(), sys_reader=fake_sys())
    with pytest.raises(asyncio.CancelledError):
        await reader.versions()
    fake_run.assert_awaited_once()


def test_api_rejects_nonstring_versions_and_unverified_states(rig):
    rig.agent.wsl_env_reader = SimpleNamespace(versions=AsyncMock(return_value={
        "kernel": ["6.6.87.2-binder"], "docker": True, "distro": " \n\t",
        "kernel_state": "OURS", "wsl_state": "WSL2_STORE", "wsl": "2.5.9.0",
    }))
    response = rig.client.get("/api/v1/system/version", headers=H(TOKEN_READ))
    assert response.status_code == 200
    body = response.json()
    for key in ("kernel", "docker", "distro", "kernel_state", "wsl_state", "wsl"):
        assert body.get(key) is None
