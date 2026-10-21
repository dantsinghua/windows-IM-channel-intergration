"""本地采集工作台设置合同；合成值、临时 SQLite、内存 Vault，无真实传输。"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import sqlite3
from functools import cached_property
from types import SimpleNamespace
from urllib.request import OpenerDirector

import pytest

from qtrade_agent.ossbox.settings import OssboxSettingsError
from qtrade_agent.store import Store
from qtrade_agent.vault_client import FakeVault, VaultUnavailable
from tests.test_api_ext import H, P, TOK_R, TOK_W, Rig


NAME = "上海采集工作台"
BASE = "https://collector.example.invalid/its-relay"
DRAFT = "https://draft.example.invalid/its-relay"
KEY = "synthetic-independent-reporting-key"
SM4 = "0123456789abcdef"
FIELDS = {"workbench_name": NAME, "base_url": BASE, "key": KEY, "sm4_key": SM4}
URL = f"{P}/settings/ossbox"


def reporting_class():
    module = "qtrade_agent.ossbox.collector_settings"
    assert importlib.util.find_spec(module) is not None, "缺少独立采集工作台配置能力"
    from qtrade_agent.ossbox.collector_settings import CollectorReportingSettings
    return CollectorReportingSettings


def forbidden(*args, **kwargs):
    pytest.fail("配置/目录验收禁止真实网络、远端消费和消息发布")


class LocalWorkbench(SimpleNamespace):
    @cached_property
    def settings(self):
        return reporting_class()(self.store, self.vault)


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    store = Store(str(tmp_path / "collector.sqlite3")).open()
    vault = FakeVault()
    yield LocalWorkbench(store=store, vault=vault)
    store.close()


@pytest.fixture
def collector_rig(tmp_path, monkeypatch, request):
    for key in tuple(os.environ):
        if key.startswith(("OSS_", "OSSBOX_", "XUNJIA_")):
            monkeypatch.delenv(key)
    local_auth = tmp_path / "existing-router-auth.yaml"
    local_auth.write_text("key: synthetic-existing-router-key\nsm4_key: 'fedcba9876543210'\n")
    config = tmp_path / "existing-collector.json"
    config.write_text(json.dumps({"file_gateway": {"auth_file": local_auth.name}}))
    fixture_mode = getattr(request, "param", None)
    if isinstance(fixture_mode, tuple) and fixture_mode[0] == "seed-root":
        config.write_text(json.dumps(fixture_mode[1]))
    network = tmp_path / "shared-network.json"
    network.write_text(json.dumps({"owner_user_id": "unrelated-owner", "workbench_id": "old-bound-workbench",
        "account_uid": "old-bound-account", "auth_file": "unchanged-auth.yaml"}))
    state = tmp_path / "xunjia-v22"
    state.mkdir()
    old_identity = state / "host.sqlite3"
    with sqlite3.connect(old_identity) as db:
        db.execute("CREATE TABLE host_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO host_meta VALUES('binding', ?)",
            (json.dumps(["unrelated-owner", "old-bound-account", "old-bound-workbench"]),))
    from relay.file_gateway_journal import Journal
    unknown = state / "publication.sqlite3"
    journal = Journal(unknown, None)
    with journal.db() as db:
        db.execute("INSERT INTO receive(state,h,name,body) VALUES(?,?,?,?)",
            ("UNKNOWN", "synthetic-unknown-hash", "synthetic-uncertain.bin", b"preserve-uncertain-payload"))
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", str(config))
    monkeypatch.setenv("XUNJIA_FILE_GATEWAY_URL", BASE)
    if getattr(request, "param", None) == "shared-network":
        monkeypatch.setenv("XUNJIA_MQ_CONNECTION_CONFIG", str(network))
    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    rig = Rig(tmp_path)
    rig.protected_paths = (local_auth, config, network, old_identity, unknown)
    yield rig
    rig.close()


def save(settings, body):
    return asyncio.run(settings.save(body, actor="independent-collector-acceptance"))


def public_state(local):
    return {"view": local.settings.view(), "settings": local.store.settings_list_prefix("")}


def test_unconfigured_workbench_has_stable_local_id_without_owner_or_host(local):
    first = local.settings.view()
    assert first["workbench_id"]
    assert first["workbench_name"] == ""
    assert first["configured"] is False
    assert reporting_class()(local.store, local.vault).view()["workbench_id"] == first["workbench_id"]


@pytest.mark.parametrize("name", [None, "", "   ", 17])
def test_first_activation_requires_nonempty_workbench_name(local, name):
    body = {**FIELDS, "workbench_name": name}
    before = public_state(local)
    with pytest.raises(OssboxSettingsError):
        save(local.settings, body)
    assert public_state(local) == before
    assert not local.vault.entries


def test_save_reads_back_editable_url_and_name_without_original_config_file(local):
    save(local.settings, FIELDS)
    save(local.settings, {"workbench_name": "深圳采集工作台", "base_url": DRAFT})
    reopened = reporting_class()(local.store, local.vault)
    view = reopened.view()
    assert view["base_url"] == DRAFT
    assert view["workbench_name"] == "深圳采集工作台"
    assert view["configured"] is True
    assert view["key_configured"] is True and view["sm4_key_configured"] is True


@pytest.mark.parametrize("url", ["", "ftp://example.invalid", "https://user:pass@example.invalid/its-relay",
    "https://example.invalid/?token=private", "https://example.invalid/#private", "https://exa mple.invalid"])
def test_invalid_url_is_rejected_without_changing_saved_profile(local, url):
    save(local.settings, FIELDS)
    before = public_state(local)
    with pytest.raises(OssboxSettingsError):
        save(local.settings, {"base_url": url})
    assert public_state(local) == before


def test_secrets_only_exist_in_vault_not_sqlite_audit_or_get(local, caplog):
    save(local.settings, FIELDS)
    persisted = "\n".join(local.store.con.iterdump())
    public = json.dumps([local.settings.view(), local.settings.saved(), local.store.list_audit()], ensure_ascii=False)
    assert local.vault.entries
    vault_values = "\n".join(entry.value for entry in local.vault.entries.values())
    for secret in (KEY, SM4):
        assert secret in vault_values
        assert secret not in persisted
        assert secret not in public
        assert secret not in caplog.text
    assert "vault://" in persisted


def test_omitted_secrets_preserve_auth_and_explicit_empty_secret_is_rejected(local, monkeypatch):
    from relay.file_gateway import FileGatewayClient
    save(local.settings, FIELDS)
    save(local.settings, {"workbench_name": "更名后工作台"})
    requests = []
    def request(client, operation, *args, **kwargs):
        requests.append((operation, client.auth.key, client.auth.sm4_key))
        return 200, {}, b'{"pending":0}'
    monkeypatch.setattr(FileGatewayClient, "_request", request)
    asyncio.run(local.settings.test({}))
    assert requests == [("pending", KEY, SM4.encode())]
    before = public_state(local)
    for field in ("key", "sm4_key"):
        with pytest.raises(OssboxSettingsError):
            save(local.settings, {field: ""})
        assert public_state(local) == before


def test_vault_failure_keeps_complete_previous_profile(local):
    save(local.settings, FIELDS)
    before = public_state(local)
    local.vault.offline = True
    with pytest.raises((OssboxSettingsError, VaultUnavailable)):
        save(local.settings, {"base_url": DRAFT, "key": "synthetic-rotated-key"})
    local.vault.offline = False
    assert public_state(local) == before


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_sqlite_failure_after_vault_write_keeps_previous_profile_and_credential(local, monkeypatch, caplog, cleanup_fails):
    save(local.settings, FIELDS)
    before = public_state(local)
    old_vault = {name: entry.value for name, entry in local.vault.entries.items()}
    if cleanup_fails:
        async def failed_cleanup(*args, **kwargs):
            raise RuntimeError(f"synthetic cleanup failure {KEY} {SM4}")
        monkeypatch.setattr(local.vault, "delete", failed_cleanup)
    for operation in ("INSERT", "UPDATE"):
        local.store.con.execute(f"CREATE TEMP TRIGGER fail_{operation.lower()} BEFORE {operation} ON settings "
            "BEGIN SELECT RAISE(ABORT, 'synthetic persistence failure'); END")
    with pytest.raises(OssboxSettingsError):
        save(local.settings, {"base_url": DRAFT, "key": "synthetic-rotated-key"})
    assert public_state(local) == before
    for name, value in old_vault.items():
        assert local.vault.entries[name].value == value
    if cleanup_fails:
        assert "cleanup deferred" in caplog.text.lower()
        for private in (KEY, SM4, "synthetic-rotated-key", "synthetic cleanup failure", "synthetic persistence failure"):
            assert private not in caplog.text


def test_draft_auth_tests_only_pending_at_draft_url_without_persistence(local, monkeypatch):
    from relay.file_gateway import FileGatewayClient
    save(local.settings, FIELDS)
    before = public_state(local)
    requests = []

    def request(client, operation, *args, **kwargs):
        requests.append((client.base_url, operation, client.auth.key, client.auth.sm4_key))
        assert operation == "pending", "草稿认证只能查询 pending"
        return 200, {}, b'{"pending":4}'

    monkeypatch.setattr(FileGatewayClient, "_request", request)
    monkeypatch.setattr(FileGatewayClient, "upload", forbidden)
    monkeypatch.setattr(FileGatewayClient, "download", forbidden)
    result = asyncio.run(local.settings.test({"base_url": DRAFT}))
    assert result["authenticated"] is True and result["pending"] == 4
    assert requests == [(DRAFT, "pending", KEY, SM4.encode())]
    assert public_state(local) == before


def test_saved_profile_does_not_fall_back_to_seed_when_vault_entry_is_missing(local):
    settings = reporting_class()(local.store, local.vault, seed={"base_url": BASE, "key": KEY, "sm4_key": SM4})
    save(settings, FIELDS)
    local.vault.entries.clear()
    with pytest.raises(OssboxSettingsError):
        asyncio.run(settings.test({}))
    assert settings.view()["configured"] is False


@pytest.mark.parametrize("damage", ["missing", "malformed_json"])
def test_complete_explicit_credentials_repair_damaged_vault_entry_with_same_public_fields(local, monkeypatch, damage):
    from relay.file_gateway import FileGatewayClient
    save(local.settings, FIELDS)
    previous_revision = local.settings.saved()["revision"]
    wb = local.settings.view()["workbench_id"]
    if damage == "missing":
        local.vault.entries.clear()
    else:
        for entry in local.vault.entries.values():
            entry.value = "{broken-json"
    settings = reporting_class()(local.store, local.vault, seed={
        "base_url": BASE, "key": "synthetic-seed-must-not-be-used", "sm4_key": "fedcba9876543210"})
    save(settings, FIELDS)
    view = settings.view()
    assert view["workbench_id"] == wb
    assert view["workbench_name"] == NAME and view["base_url"] == BASE
    assert view["configured"] is True
    assert settings.saved()["revision"] != previous_revision
    observed = []
    def pending(client, operation, *args, **kwargs):
        observed.append((operation, client.auth.key, client.auth.sm4_key))
        return 200, {}, b'{"pending":0}'
    monkeypatch.setattr(FileGatewayClient, "_request", pending)
    assert asyncio.run(settings.test({}))["authenticated"] is True
    assert observed == [("pending", KEY, SM4.encode())]


def test_vault_read_unavailable_cannot_be_treated_as_damaged_entry_and_overwritten(local, monkeypatch):
    save(local.settings, FIELDS)
    before = public_state(local)
    vault_before = {name: entry.value for name, entry in local.vault.entries.items()}
    async def unavailable(*args, **kwargs):
        raise VaultUnavailable("synthetic-read-unavailable")
    monkeypatch.setattr(local.vault, "read", unavailable)
    with pytest.raises((OssboxSettingsError, VaultUnavailable)):
        save(local.settings, FIELDS)
    assert public_state(local) == before
    assert {name: entry.value for name, entry in local.vault.entries.items()} == vault_before


def test_each_pending_check_generates_fresh_access_token_without_saving_or_exposing_it(local, monkeypatch):
    save(local.settings, FIELDS)
    before = public_state(local)
    tokens = []
    class Pending(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json"}
        def __init__(self):
            super().__init__(b'{"pending":0}')
    def open_pending(opener, request, timeout=None):
        assert request.get_method() == "GET"
        assert request.full_url == DRAFT + "/api/files/pending"
        assert request.data is None
        token = {name.lower(): value for name, value in request.header_items()}["access_token"]
        assert token and KEY not in token and SM4 not in token
        tokens.append(token)
        return Pending()
    monkeypatch.setattr(OpenerDirector, "open", open_pending)
    results = [asyncio.run(local.settings.test({"base_url": DRAFT})) for _ in range(2)]
    assert all(result["authenticated"] is True for result in results)
    assert len(tokens) == 2 and tokens[0] != tokens[1]
    assert public_state(local) == before
    public = json.dumps([results, public_state(local), local.store.list_audit()])
    assert all(token not in public for token in tokens)


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_failed_pending_neither_succeeds_nor_leaks_upstream_secret_text(local, monkeypatch, error):
    from relay.file_gateway import FileGatewayClient
    save(local.settings, FIELDS)
    before = public_state(local)
    def fail(client, operation, *args, **kwargs):
        raise error(f"synthetic upstream failure {KEY} {SM4}")
    monkeypatch.setattr(FileGatewayClient, "_request", fail)
    with pytest.raises(OssboxSettingsError) as caught:
        asyncio.run(local.settings.test({}))
    assert KEY not in str(caught.value) and SM4 not in str(caught.value)
    assert public_state(local) == before


@pytest.mark.parametrize("collector_rig", [None, "shared-network"], indirect=True)
def test_api_save_is_local_without_online_host_and_reports_pending_alignment(collector_rig):
    rig = collector_rig
    before = {path: path.read_bytes() for path in rig.protected_paths}
    response = rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    data = rig.client.get(URL, headers=H()).json()
    assert data["mode"] == "its_relay"
    assert data["state"] == "pending_alignment"
    assert data["applied"] is False
    assert data["relay"]["workbench_name"] == NAME
    assert data["relay"]["base_url"] == BASE
    assert data["catalog_configured"] is False and data["inquiry_configured"] is False
    assert {path: path.read_bytes() for path in rig.protected_paths} == before
    assert rig.agent.xunjia_v22_host is None
    assert not rig.agent.scheduler._started and not rig.agent._started
    for secret in (KEY, SM4):
        assert secret not in response.text and secret not in json.dumps(data)


def test_api_keeps_saved_status_while_vault_is_unavailable_and_recovers_without_resaving(collector_rig, caplog):
    rig = collector_rig
    response = rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    online = rig.client.get(URL, headers=H()).json()
    assert online["relay"]["saved"] is True
    assert online["relay"]["configured"] is True
    before = rig.store.settings_list_prefix("")
    rig.agent.vault.offline = True
    offline_response = rig.client.get(URL, headers=H())
    assert offline_response.status_code == 200, offline_response.text
    offline = offline_response.json()
    assert offline["relay"]["saved"] is True
    assert offline["relay"]["configured"] is False
    assert offline["relay"]["credential_error"] == "vault_unavailable"
    assert offline["state"] == "pending_alignment" and offline["applied"] is False
    assert offline["relay"]["workbench_name"] == NAME and offline["relay"]["base_url"] == BASE
    assert rig.store.settings_list_prefix("") == before
    rig.agent.vault.offline = False
    recovered = rig.client.get(URL, headers=H()).json()
    assert recovered["relay"]["saved"] is True
    assert recovered["relay"]["configured"] is True
    assert not recovered["relay"].get("credential_error")
    assert recovered["relay"]["workbench_id"] == online["relay"]["workbench_id"]
    assert rig.store.settings_list_prefix("") == before
    for secret in (KEY, SM4):
        assert secret not in json.dumps([online, offline, recovered])
        assert secret not in caplog.text


@pytest.mark.parametrize("collector_rig", [("seed-root", []), ("seed-root", None),
    ("seed-root", False), ("seed-root", 7)], indirect=True)
def test_app_open_with_nonobject_seed_still_allows_independent_local_configuration(collector_rig):
    rig = collector_rig
    response = rig.client.get(URL, headers=H())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "pending_alignment"
    response = rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    assert response.json()["relay"]["workbench_name"] == NAME
    assert response.json()["relay"]["base_url"] == BASE
    assert rig.agent.xunjia_v22_host is None
    assert not rig.agent._started and not rig.agent.scheduler._started


@pytest.mark.parametrize("token", [TOK_R, TOK_W])
def test_nonadmin_cannot_read_write_or_test_collector_config(collector_rig, token):
    for method, path, kwargs in (("get", URL, {}), ("put", URL, {"json": FIELDS}),
        ("post", URL + "/test", {"json": FIELDS})):
        response = getattr(collector_rig.client, method)(path, headers=H(token), **kwargs)
        assert response.status_code == 403, response.text
