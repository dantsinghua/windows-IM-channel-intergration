"""独立装配验收：文件网关与历史 STS 状态隔离，不启动真实账号或队列。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.request import OpenerDirector

import pytest
from starlette.testclient import TestClient

from conftest import make_rig
from qtrade_agent.ossbox.catalog_publish import CatalogPublisher
from qtrade_agent.ossbox.oss_http import OssV1Box
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("OSS_", "OSSBOX_", "XUNJIA_")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(OpenerDirector, "open", lambda *a, **k: pytest.fail("禁止真实网络"))
    private = tmp_path / "deploy"
    private.mkdir()
    auth_file = private / "auth.yaml"
    auth_file.write_text("auth:\n  key: synthetic-deployed-key\n  sm4_key: '0123456789abcdef'\n")
    (private / "seal.key").write_text((b"k" * 16).hex())
    (private / "sign.key").write_text((b"s" * 32).hex())
    config = {"owner_user_id": "synthetic-owner", "account_uid": "synthetic-account",
        "workbench_id": "synthetic-bound-workbench", "seal_key_file": "seal.key", "sign_key_file": "sign.key",
        "file_gateway": {"auth_file": auth_file.name, "uplink_url": "https://up.example.invalid",
            "downlink_url": "https://down.example.invalid"}}
    config_file = private / "collector.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("XUNJIA_FILE_GATEWAY_URL", "https://relay.example.invalid/its-relay")
    return SimpleNamespace(auth_file=auth_file, config_file=config_file, config=config)


def test_file_gateway_open_never_constructs_legacy_oss_or_catalog(tmp_path, monkeypatch, deployment):
    def forbidden(*args, **kwargs):
        pytest.fail("V2.2 file_gateway 不得装配旧 OSS/STS 或 CatalogPublisher")
    monkeypatch.setattr(OssV1Box, "from_env", forbidden)
    monkeypatch.setattr(CatalogPublisher, "from_env", forbidden)
    monkeypatch.setenv("OSS_STS_URL", "invalid-old-url")
    monkeypatch.setenv("OSS_BUCKET", "obsolete-bucket")
    rig = make_rig(tmp_path)
    try:
        assert rig.agent.xunjia_inbox is None and rig.agent.catalog_publisher is None
        assert rig.agent._xunjia_oss_box is None
        assert not rig.agent._started and not rig.agent.scheduler._started
        assert not rig.vault.reads
    finally:
        asyncio.run(rig.agent.stop())


@pytest.mark.parametrize("legacy_profile", [
    {"revision": "invalid", "credentials_ref": "vault://account/forbidden"},
    {"revision": "a" * 32, "credentials_ref": "vault://settings/ossbox/" + "a" * 32 + "/credentials",
        "sts_url": "https://old-sts.example.invalid", "allow_http": False, "endpoint": "https://old-oss.invalid",
        "bucket": "old-bucket", "workbench_id": "obsolete-other-workbench"},
])
def test_historical_sts_profile_and_offline_vault_do_not_veto_file_gateway_host(
        tmp_path, monkeypatch, deployment, legacy_profile):
    rig = make_rig(tmp_path)
    calls = []

    class Host:
        binding = SimpleNamespace(workbench_id="synthetic-bound-workbench")
        async def start(self):
            calls.append("host-start")
        async def stop(self):
            calls.append("host-stop")

    host = Host()
    def factory(cls, **kwargs):
        assert kwargs["box"] is None and kwargs["legacy_inbox_disabled"]() is True
        calls.append("host-configure")
        return host
    monkeypatch.setattr(XunjiaV22Host, "from_env", classmethod(factory))

    async def run():
        try:
            rig.store.settings_set("ossbox.config", legacy_profile)
            rig.vault.offline = True
            await rig.agent._configure_saved_ossbox()
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_v22_host is host
            assert calls == ["host-configure", "host-start"]
            assert not rig.vault.reads
            assert rig.agent.catalog_publisher is None and rig.agent.xunjia_inbox is None
            assert not rig.agent._started and not rig.agent.scheduler._started
        finally:
            await rig.agent.stop()
    asyncio.run(run())


def test_config_file_relative_auth_path_is_deployment_owned_and_not_cwd(
        tmp_path, monkeypatch, deployment):
    from qtrade_agent.ossbox.relay_settings import RelaySettings
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = RelaySettings.from_env()
    asyncio.run(settings.save({"key": "rotated-through-deployed-path"}, actor="independent-test"))
    from relay.file_gateway import GatewayAuth
    auth = GatewayAuth.from_file(deployment.auth_file)
    assert auth.key == "rotated-through-deployed-path"
    assert not (elsewhere / "auth.yaml").exists()
    assert not (elsewhere / "config.yaml").exists()


def test_existing_auth_is_applied_at_open_and_only_changed_save_requires_restart(tmp_path, deployment):
    rig = make_rig(tmp_path)
    rig.store.upsert_api_client(app_id="relay-test", name="independent-test", level="admin", token="isolated-admin")
    headers = {"Authorization": "Bearer isolated-admin"}
    try:
        with TestClient(rig.agent.create_api(), client=("127.0.0.1", 41000)) as client:
            before = client.get("/api/v1/settings/ossbox", headers=headers)
            assert before.status_code == 200, before.text
            assert before.json()["mode"] == "its_relay"
            assert before.json()["relay"]["configured"] is True
            assert before.json()["restart_required"] is False
            unchanged = client.put("/api/v1/settings/ossbox", headers=headers,
                json={"key": "synthetic-deployed-key", "sm4_key": "0123456789abcdef"})
            assert unchanged.status_code == 200, unchanged.text
            assert unchanged.json()["restart_required"] is False
            changed = client.put("/api/v1/settings/ossbox", headers=headers,
                json={"key": "synthetic-new-key", "sm4_key": "0123456789abcdef"})
            assert changed.status_code == 200, changed.text
            assert changed.json()["restart_required"] is True
            assert rig.agent.xunjia_v22_host is None and not rig.agent._started
    finally:
        asyncio.run(rig.agent.stop())


def test_binding_identity_still_refuses_another_collector_state(tmp_path, deployment):
    async def run():
        kwargs = dict(store=SimpleNamespace(outbox_seq_bounds=lambda: (0, 0)),
            bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=asyncio.get_running_loop(), box=None,
            data_dir=tmp_path / "bound-state", legacy_inbox_disabled=lambda: True)
        host = XunjiaV22Host.from_env(**kwargs)
        assert host.binding.workbench_id == "synthetic-bound-workbench"
        changed = {**deployment.config, "workbench_id": "different-workbench"}
        deployment.config_file.write_text(json.dumps(changed))
        with pytest.raises((ValueError, RuntimeError)):
            XunjiaV22Host.from_env(**kwargs)
    asyncio.run(run())
