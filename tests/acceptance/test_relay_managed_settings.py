"""远端 MQ 凭据管理验收：真实安全管理器，临时 journal，仅假 pending。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from conftest import make_rig
from qtrade_agent.ossbox import relay_settings
from qtrade_agent.ossbox.settings import OssboxSettingsError
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host


OWNER = "synthetic-managed-owner"
ACCOUNT = "synthetic-managed-account"
WORKBENCH = "synthetic-managed-workbench"
KEY = "synthetic-remote-business-key"
SM4 = "0123456789abcdef"
ROTATED = "fedcba9876543210"
BASE = "https://remote-mq.example.invalid/its-relay"
UP = "https://local-router.example.invalid/up"
DOWN = "https://local-router.example.invalid/down"


def private_write(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    from relay.file_gateway import FileGatewayClient, GatewayAuth
    from relay.file_gateway_connection import connection_identity, load_connection
    from relay.file_gateway_journal import Journal
    from relay.protocol import Binding
    from server.file_gateway_transport import FileGatewayTransport
    from server.mq_settings import MQSettings

    for name in tuple(os.environ):
        if name.startswith(("OSS_", "OSSBOX_", "XUNJIA_")):
            monkeypatch.delenv(name)
    local = tmp_path / "collector-local-auth.yaml"
    private_write(local, "# Local router authentication must not change.\nauth:\n  key: synthetic-local-router-key\n  sm4_key: '0123456789abcdef'\n")
    secrets = tmp_path / ".mq-secrets"
    secrets.mkdir(mode=0o700)
    remote = secrets / "existing-remote.yaml"
    private_write(remote, f"key: {KEY}\nsm4_key: '{SM4}'\n")
    network = tmp_path / "network.json"
    config = {"owner_user_id": OWNER, "account_uid": ACCOUNT, "workbench_id": WORKBENCH,
        "base_url": BASE, "auth_file": ".mq-secrets/" + remote.name,
        "uplink_url": UP, "downlink_url": DOWN, "allow_http": False}
    private_write(network, json.dumps(config))
    collector = tmp_path / "collector.json"
    private_write(collector, json.dumps({"file_gateway": {"auth_file": local.name,
        "uplink_url": UP, "downlink_url": DOWN}}))
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", str(collector))
    monkeypatch.setenv("XUNJIA_FILE_GATEWAY_URL", BASE)
    monkeypatch.setenv("XUNJIA_MQ_CONNECTION_CONFIG", str(network))
    binding = Binding(OWNER, ACCOUNT, WORKBENCH, b"b" * 16, b"s" * 32)
    local_auth = GatewayAuth.from_file(local)
    adapter = FileGatewayTransport(binding, FileGatewayClient(DOWN, local_auth),
        FileGatewayClient(UP, local_auth), tmp_path / "collector-journal.sqlite3", role="collector")
    host = SimpleNamespace(binding=binding, bridge=SimpleNamespace(adapters={"oss": adapter}))
    journal = Journal(tmp_path / "coordinator-journal.sqlite3", None)
    with journal.db() as db:
        db.execute("INSERT INTO receive(state,h,name,body) VALUES(?,?,?,?)",
            ("UNKNOWN", "synthetic-hash", "synthetic-uncertain.bin", b"preserve-uncertain-payload"))
    http_calls, manager_calls = [], []

    def request(client, operation, *args, **kwargs):
        assert operation == "pending", "设置管理不得 upload/download"
        http_calls.append((client.base_url, client.auth.key, client.auth.sm4_key))
        if client.base_url in (UP, DOWN):
            assert client.auth.key == "synthetic-local-router-key"
            active = load_connection(network)
            payload = {"pending": 0, "connection_config": str(network.resolve()),
                "connection_revision": revision(network), "connection_identity": connection_identity(active),
                "switch_safe": not journal.stats()["receive"] and not journal.stats()["upload"]}
        else:
            payload = {"pending": 7}
        return 200, {}, json.dumps(payload).encode()

    def forbidden(*args, **kwargs):
        pytest.fail("凭据设置不得消费/发布队列或启动采集轮询")
    monkeypatch.setattr(FileGatewayClient, "_request", request)
    monkeypatch.setattr(FileGatewayClient, "upload", forbidden)
    monkeypatch.setattr(FileGatewayClient, "download", forbidden)
    monkeypatch.setattr(FileGatewayTransport, "poll", forbidden)
    for operation in ("status", "test", "save"):
        original = getattr(MQSettings, operation)
        def tracked(manager, owner, *args, _operation=operation, _original=original, **kwargs):
            manager_calls.append((_operation, owner, args))
            return _original(manager, owner, *args, **kwargs)
        monkeypatch.setattr(MQSettings, operation, tracked)
    return SimpleNamespace(root=tmp_path, network=network, collector=collector,
        local=local, remote=remote, adapter=adapter, host=host, journal=journal,
        http_calls=http_calls, manager_calls=manager_calls)


def managed(world):
    assert hasattr(relay_settings, "ManagedRelaySettings"), "缺少共用 MQSettings 的远端凭据管理适配器"
    settings = relay_settings.ManagedRelaySettings(world.network)
    settings.bind_host(world.host)
    return settings


def snapshots(world):
    return {str(path.relative_to(world.root)): path.read_bytes()
        for path in world.root.rglob("*") if path.is_file() and path.suffix in (".yaml", ".json")}


def journal_rows(world):
    with world.journal.db() as db:
        return {"receive": [tuple(row) for row in db.execute("SELECT * FROM receive ORDER BY id")],
            "upload": [tuple(row) for row in db.execute("SELECT * FROM upload ORDER BY h")]}


def test_managed_view_and_pending_use_remote_pointer_not_local_router_auth(world):
    settings = managed(world)
    before = snapshots(world)
    view = settings.view()
    assert view["base_url"] == BASE and view["configured"] is True
    assert view["key_configured"] is True and view["sm4_key_configured"] is True
    result = asyncio.run(settings.test({}))
    assert result["authenticated"] is True
    remote_calls = [call for call in world.http_calls if call[0] not in (UP, DOWN)]
    assert remote_calls == [(BASE, KEY, SM4.encode())]
    assert {entry[0] for entry in world.manager_calls} >= {"status", "test"}
    assert all(entry[1] == OWNER for entry in world.manager_calls)
    assert snapshots(world) == before
    for sensitive in (KEY, SM4, "synthetic-local-router-key", str(world.remote)):
        assert sensitive not in json.dumps([view, result])


def test_same_business_key_sm4_rotation_uses_shared_cas_pointer_preserves_unknown_and_local_auth(world):
    from relay.file_gateway_connection import load_connection
    settings = managed(world)
    local_before, collector_before = world.local.read_bytes(), world.collector.read_bytes()
    old_auth, old_revision = world.remote.read_bytes(), revision(world.network)
    journal_before = journal_rows(world)
    asyncio.run(settings.save({"sm4_key": ROTATED}, actor="independent-acceptance"))
    calls = [entry for entry in world.manager_calls if entry[0] == "save"]
    assert len(calls) == 1 and calls[0][1] == OWNER
    assert calls[0][2][0]["expected_revision"] == old_revision
    assert revision(world.network) != old_revision
    config = json.loads(world.network.read_text())
    assert config["auth_file"] != ".mq-secrets/" + world.remote.name
    current = load_connection(world.network)
    assert current.auth.key == KEY and current.auth.sm4_key == ROTATED.encode()
    assert world.local.read_bytes() == local_before and world.collector.read_bytes() == collector_before
    assert world.remote.read_bytes() == old_auth and journal_rows(world) == journal_before
    assert world.adapter.role == "collector" and world.adapter.inbound.base_url == DOWN
    assert world.adapter.outbound.base_url == UP
    assert stat.S_IMODE(world.network.stat().st_mode) == 0o600
    assert stat.S_IMODE((world.network.parent / config["auth_file"]).stat().st_mode) == 0o600


def test_changed_business_key_is_rejected_by_shared_manager_when_journal_has_history(world):
    settings = managed(world)
    before, journal_before = snapshots(world), journal_rows(world)
    with pytest.raises(OssboxSettingsError) as caught:
        asyncio.run(settings.save({"key": "synthetic-other-remote-key"}, actor="independent-acceptance"))
    assert any(entry[0] == "save" for entry in world.manager_calls)
    assert snapshots(world) == before and journal_rows(world) == journal_before
    assert KEY not in str(caught.value) and SM4 not in str(caught.value)


def test_another_editor_pointer_change_is_seen_on_next_view_and_pending_without_cache(world):
    settings = managed(world)
    local_before = world.local.read_bytes()
    assert settings.view()["base_url"] == BASE
    other_auth = world.root / ".mq-secrets" / "other-editor-revision.yaml"
    private_write(other_auth, f"key: {KEY}\nsm4_key: '{ROTATED}'\n")
    config = json.loads(world.network.read_text())
    changed_base = "https://changed-mq.example.invalid/its-relay"
    config.update(auth_file=".mq-secrets/" + other_auth.name, base_url=changed_base)
    private_write(world.network, json.dumps(config))
    assert settings.view()["base_url"] == changed_base
    result = asyncio.run(settings.test({}))
    assert result["authenticated"] is True
    remote_calls = [call for call in world.http_calls if call[0] not in (UP, DOWN)]
    assert remote_calls[-1] == (changed_base, KEY, ROTATED.encode())
    assert world.local.read_bytes() == local_before


def test_shared_manager_cas_rejects_a_concurrent_network_pointer_change(world, monkeypatch):
    from server.mq_settings import MQSettings
    settings = managed(world)
    original_save = MQSettings.save
    committed_elsewhere = []

    def concurrent_save(manager, owner, body):
        assert body["expected_revision"] == revision(world.network)
        config = json.loads(world.network.read_text())
        config["base_url"] = "https://concurrent-editor.example.invalid/its-relay"
        private_write(world.network, json.dumps(config))
        committed_elsewhere.append(snapshots(world))
        return original_save(manager, owner, body)

    monkeypatch.setattr(MQSettings, "save", concurrent_save)
    with pytest.raises(OssboxSettingsError):
        asyncio.run(settings.save({"sm4_key": ROTATED}, actor="independent-acceptance"))
    assert len(committed_elsewhere) == 1
    assert snapshots(world) == committed_elsewhere[0]


def test_from_env_selects_network_manager_when_connection_config_is_present(world):
    settings = relay_settings.RelaySettings.from_env()
    assert hasattr(relay_settings, "ManagedRelaySettings"), "缺少远端凭据管理模式"
    assert isinstance(settings, relay_settings.ManagedRelaySettings)
    settings.bind_host(world.host)
    assert settings.view()["base_url"] == BASE


def test_noop_direct_file_save_repairs_world_readable_mode_without_changing_bytes(tmp_path):
    path = tmp_path / "direct-config.yaml"
    original = f"# Keep comment and exact serialization.\nauth:\n  key: {KEY}\n  sm4_key: '{SM4}'\n"
    path.write_text(original)
    path.chmod(0o644)
    settings = relay_settings.RelaySettings(path, BASE)
    asyncio.run(settings.save({"key": KEY, "sm4_key": SM4}, actor="independent-acceptance"))
    assert path.read_bytes() == original.encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_managed_unapplied_status_clears_prior_application_marker(world, monkeypatch):
    settings = managed(world)
    current = revision(world.network)
    settings.applied_revision = current
    status = {"editable": True, "configured": True, "credentials_set": True,
        "base_url": BASE, "revision": current, "applied": False}
    manager = SimpleNamespace(path=world.network, status=lambda owner: dict(status))
    monkeypatch.setattr(settings, "_manager", lambda: (OWNER, manager))
    view = settings.view()
    assert view["configured"] is True
    assert settings.applied_revision is None, "远端明确尚未加载，不能沿用此前已应用标记"
    rig = make_rig(world.root)
    try:
        rig.agent.relay_settings = settings
        rig.store.upsert_api_client(app_id="managed-admin", name="independent-test", level="admin", token="managed-isolated-admin")
        with TestClient(rig.agent.create_api(), client=("127.0.0.1", 41000)) as client:
            response = client.get("/api/v1/settings/ossbox", headers={"Authorization": "Bearer managed-isolated-admin"})
            assert response.status_code == 200, response.text
            assert response.json()["applied"] is False
    finally:
        asyncio.run(rig.agent.stop())


def test_managed_same_value_save_repairs_remote_auth_mode_without_switching_pointer(world, monkeypatch):
    settings = managed(world)
    owner, manager = settings._manager()
    active = manager._active(manager._load(owner)[3])
    current = revision(world.network)
    status = {"editable": True, "configured": True, "credentials_set": True,
        "base_url": BASE, "revision": current, "applied": True}
    def forbidden(*args, **kwargs):
        pytest.fail("同值保存不应重新生成凭据或切换网络指针")
    monkeypatch.setattr(manager, "_active", lambda adapter: dict(active))
    monkeypatch.setattr(manager, "status", lambda owner: dict(status))
    monkeypatch.setattr(manager, "save", forbidden)
    monkeypatch.setattr(settings, "_manager", lambda: (OWNER, manager))
    before, journal_before = snapshots(world), journal_rows(world)
    world.remote.chmod(0o644)
    asyncio.run(settings.save({"key": KEY, "sm4_key": SM4}, actor="independent-acceptance"))
    assert stat.S_IMODE(world.remote.stat().st_mode) == 0o600
    assert snapshots(world) == before and journal_rows(world) == journal_before


@pytest.mark.parametrize("succeeds", [True, False])
def test_app_advances_applied_revision_only_after_successful_host_start(tmp_path, monkeypatch, succeeds):
    for name in tuple(os.environ):
        if name.startswith(("OSS_", "OSSBOX_", "XUNJIA_")):
            monkeypatch.delenv(name)
    auth = tmp_path / "auth.yaml"
    private_write(auth, f"key: {KEY}\nsm4_key: '{SM4}'\n")
    collector = tmp_path / "collector.json"
    private_write(collector, json.dumps({"file_gateway": {"auth_file": auth.name}}))
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", str(collector))
    monkeypatch.setenv("XUNJIA_FILE_GATEWAY_URL", BASE)
    rig = make_rig(tmp_path)

    class Host:
        binding = SimpleNamespace(workbench_id=WORKBENCH)
        async def start(self):
            if not succeeds:
                raise RuntimeError("synthetic host start failure")
        async def stop(self):
            pass
    host = Host()
    monkeypatch.setattr(XunjiaV22Host, "from_env", classmethod(lambda cls, **kwargs: host))

    async def run():
        try:
            settings = rig.agent.relay_settings
            prior = settings.applied_revision
            assert prior is None, "首次采集服务启动前不能假标应用"
            await settings.save({"sm4_key": ROTATED}, actor="independent-acceptance")
            current = settings.saved()["revision"]
            assert current != prior
            await rig.agent._configure_xunjia_v22()
            assert settings.applied_revision == (current if succeeds else prior)
            rig.store.upsert_api_client(app_id="revision-admin", name="independent-test", level="admin", token="revision-isolated-admin")
            with TestClient(rig.agent.create_api(), client=("127.0.0.1", 41000)) as client:
                response = client.get("/api/v1/settings/ossbox", headers={"Authorization": "Bearer revision-isolated-admin"})
                assert response.status_code == 200, response.text
                assert response.json()["applied"] is succeeds
            assert not rig.agent._started and not rig.agent.scheduler._started
            if not succeeds:
                assert rig.agent.oss_start_error == "xunjia_v22_configuration_unavailable"
        finally:
            await rig.agent.stop()
    asyncio.run(run())
