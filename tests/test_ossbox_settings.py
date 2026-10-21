"""页面 STS 配置的开发者回归；只用假 WinAgent/Vault/HTTP，不启动业务。"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock
from urllib.request import OpenerDirector

import pytest

from qtrade_agent.ossbox.sts import StsCredentials, StsCredentialProvider, StsError
from tests.test_api_ext import H, P, TOK_R, TOK_W, Rig


URL = f"{P}/settings/ossbox"
AUTH = "settings-test-business-secret"
SM4 = "fEdCbA9876543210"
PUBLIC = {
    "sts_url": "https://sts.example.invalid/v1/api/sts",
    "allow_http": False,
    "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
    "bucket": "settings-test-bucket",
    "workbench_id": "settings-test-workbench",
}


def payload(**overrides):
    return {**PUBLIC, "auth_key": AUTH, "sm4_key": SM4, **overrides}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("OSS_", "OSSBOX_")):
            monkeypatch.delenv(key)
    monkeypatch.setattr(OpenerDirector, "open", lambda *a, **k: pytest.fail("禁止真实 STS/OSS HTTP"))


@pytest.fixture
def rig(tmp_path):
    instance = Rig(tmp_path)
    instance.agent.config_path = str(tmp_path / "agent.toml")
    yield instance
    instance.close()


def saved(rig):
    response = rig.client.put(URL, headers=H(), json=payload())
    assert response.status_code == 200, response.text
    assert response.json()["data"]["saved"] is True
    return response


def state(rig):
    return rig.store.settings_get("ossbox.config")


def assert_no_secrets(rig, *responses):
    persisted = json.dumps(rig.store.settings_list_prefix(""), ensure_ascii=False)
    audit = json.dumps(rig.store.list_audit(), ensure_ascii=False)
    files = list(__import__("pathlib").Path(rig.agent.config_path).parent.glob("*.toml"))
    haystack = persisted + audit + "".join(r.text for r in responses) + "".join(p.read_text() for p in files)
    for secret in (AUTH, SM4, "returned-oss-secret", "returned-security-token"):
        assert secret not in haystack


def fake_credentials(monkeypatch, *, error=None):
    calls = []

    def get(provider):
        calls.append(provider)
        if error:
            raise error
        return StsCredentials("STS.settings-test-id", "returned-oss-secret", "returned-security-token",
                              1_900_000_000.0, PUBLIC["bucket"])

    monkeypatch.setattr(StsCredentialProvider, "get_credentials", get)
    return calls


def test_fresh_install_configuration_is_blank_and_has_no_assumed_business_readiness(rig):
    response = rig.client.get(URL, headers=H())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["group"] == "ossbox"
    assert body["data"] == {"sts_url": "", "allow_http": False, "endpoint": "", "bucket": "",
                            "workbench_id": "", "auth_key_configured": False,
                            "sm4_key_configured": False, "saved": False}
    assert body["restart_required"] is False
    assert body["inquiry_configured"] is False and body["catalog_configured"] is False


@pytest.mark.parametrize("method,path", [("get", URL), ("put", URL), ("post", URL + "/test")])
@pytest.mark.parametrize("token,expected", [(None, 401), (TOK_R, 403), (TOK_W, 403)])
def test_configuration_and_authentication_probe_are_admin_only(rig, method, path, token, expected):
    kwargs = {"headers": H(token)} if token else {}
    if method != "get":
        kwargs["json"] = payload()
    response = getattr(rig.client, method)(path, **kwargs)
    assert response.status_code == expected, response.text
    assert state(rig) is None and not rig.agent.vault.entries


def test_save_keeps_secrets_only_in_one_vault_bundle_and_requires_restart(rig, caplog):
    response = saved(rig)
    read = rig.client.get(URL, headers=H())
    assert read.json()["data"] == {**PUBLIC, "auth_key_configured": True,
                                   "sm4_key_configured": True, "saved": True}
    assert read.json()["restart_required"] is True
    assert len(rig.agent.vault.entries) == 1
    entry = next(iter(rig.agent.vault.entries.values()))
    assert json.loads(entry.value) == {"auth_key": AUTH, "sm4_key": SM4}
    assert "vault://" in state(rig)["credentials_ref"]
    assert_no_secrets(rig, response, read)
    assert AUTH not in caplog.text and SM4 not in caplog.text


def test_omitted_password_fields_reuse_saved_bundle_without_emptying_keys(rig):
    saved(rig)
    response = rig.client.put(URL, headers=H(), json={**PUBLIC, "workbench_id": "second-workbench"})
    assert response.status_code == 200, response.text
    bundle = rig.agent.vault.entries[state(rig)["credentials_ref"].removeprefix("vault://")]
    assert json.loads(bundle.value) == {"auth_key": AUTH, "sm4_key": SM4}
    assert response.json()["data"]["workbench_id"] == "second-workbench"


def test_rotation_publishes_a_new_vault_reference_only_after_both_keys_are_saved(rig):
    saved(rig)
    previous = dict(state(rig))
    response = rig.client.put(URL, headers=H(), json=payload(auth_key="rotated-business-secret"))
    assert response.status_code == 200, response.text
    assert state(rig)["credentials_ref"] != previous["credentials_ref"]
    bundle = rig.agent.vault.entries[state(rig)["credentials_ref"].removeprefix("vault://")]
    assert json.loads(bundle.value) == {"auth_key": "rotated-business-secret", "sm4_key": SM4}


@pytest.mark.parametrize("initial_saved", [False, True])
def test_vault_failure_does_not_publish_partial_settings(rig, initial_saved):
    if initial_saved:
        saved(rig)
    previous = state(rig)
    rig.agent.vault.offline = True
    response = rig.client.put(URL, headers=H(), json=payload(auth_key="new-secret", workbench_id="new-workbench"))
    assert response.status_code == 503, response.text
    assert response.json()["ok"] is False
    assert state(rig) == previous
    assert_no_secrets(rig, response)


@pytest.mark.parametrize("overrides", [
    {"unexpected": "value"}, {"credentials_ref": "vault://account/qd01"},
    {"auth_key_ref": "vault://api/console"}, {"auth_key": ""}, {"sm4_key": ""},
    {"sm4_key": "short"}, {"sm4_key": "0123456789abcdef0"}, {"sm4_key": "中" * 16},
    {"allow_http": "true"}, {"allow_http": 1}, {"sts_url": ""},
    {"sts_url": "file:///etc/passwd"}, {"sts_url": "https://user:pass@sts.example.invalid/sts"},
    {"sts_url": "http://mq.example.invalid/sts"}, {"sts_url": "https://sts.example.invalid/sts?token=abc"},
    {"sts_url": "https://sts.example.invalid/sts#frag"}, {"auth_key": None}, {"sm4_key": None},
])
def test_invalid_settings_are_rejected_without_vault_or_settings_mutation(rig, overrides):
    # 正向基线先锁定新端点，避免旧版“未知配置组”被误算作负向通过。
    assert rig.client.get(URL, headers=H()).status_code == 200
    response = rig.client.put(URL, headers=H(), json=payload(**overrides))
    assert response.status_code in (400, 422), response.text
    assert response.json()["code"] == "INVALID_ARGS"
    assert state(rig) is None and not rig.agent.vault.entries


@pytest.mark.parametrize("missing", ["auth_key", "sm4_key"])
def test_first_save_cannot_reuse_credentials_that_do_not_exist(rig, missing):
    assert rig.client.get(URL, headers=H()).status_code == 200
    body = payload()
    del body[missing]
    response = rig.client.put(URL, headers=H(), json=body)
    assert response.status_code in (400, 422), response.text
    assert state(rig) is None and not rig.agent.vault.entries


@pytest.mark.parametrize("missing", PUBLIC)
def test_put_requires_the_complete_public_configuration(rig, missing):
    saved(rig)
    before = dict(state(rig))
    body = payload()
    del body[missing]
    response = rig.client.put(URL, headers=H(), json=body)
    assert response.status_code in (400, 422), response.text
    assert state(rig) == before


def test_tampered_vault_reference_is_rejected_without_reading_another_secret(rig, monkeypatch):
    saved(rig)
    original = dict(state(rig))
    rig.store.settings_set("ossbox.config", {**original, "credentials_ref": "vault://account/qd01"})
    calls = fake_credentials(monkeypatch)
    rig.agent.vault.reads.clear()
    response = rig.client.post(URL + "/test", headers=H(), json=PUBLIC)
    assert response.status_code >= 400, response.text
    assert not calls
    assert not rig.agent.vault.reads


def test_settings_write_failure_preserves_previous_keys_and_reference(rig, monkeypatch):
    saved(rig)
    before = dict(state(rig))
    original_write = rig.store.settings_set

    def fail_only_new_profile(key, value, **kwargs):
        if key == "ossbox.config":
            raise OSError("simulated storage failure")
        return original_write(key, value, **kwargs)

    monkeypatch.setattr(rig.store, "settings_set", fail_only_new_profile)
    response = rig.client.put(URL, headers=H(), json=payload(auth_key="new-key-not-published"))
    assert response.status_code == 503, response.text
    assert state(rig) == before
    entry = rig.agent.vault.entries[before["credentials_ref"].removeprefix("vault://")]
    assert json.loads(entry.value) == {"auth_key": AUTH, "sm4_key": SM4}


def test_sm4_length_is_utf8_bytes_not_character_count(rig):
    value = "中" * 5 + "a"
    assert len(value.encode("utf-8")) == 16
    response = rig.client.put(URL, headers=H(), json=payload(sm4_key=value))
    assert response.status_code == 200, response.text


def test_explicit_http_opt_in_allows_mq_while_loopback_needs_no_opt_in(rig):
    for url, allow in [("http://mq.example.invalid/sts", True), ("http://127.0.0.1:9999/sts", False)]:
        response = rig.client.put(URL, headers=H(), json=payload(sts_url=url, allow_http=allow))
        assert response.status_code == 200, response.text


def test_probe_uses_entered_credentials_and_returns_only_authentication_summary(rig, monkeypatch):
    calls = fake_credentials(monkeypatch)
    response = rig.client.post(URL + "/test", headers=H(), json=payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["authenticated"] is True
    assert "bucket" not in body and body["expiration"]
    assert len(calls) == 1 and calls[0]._auth_key == AUTH and calls[0]._sm4_key == SM4.encode()
    assert state(rig) is None and not rig.agent.vault.entries
    assert not rig.agent.scheduler._started
    assert_no_secrets(rig, response)


def test_probe_omitted_passwords_must_not_use_saved_credentials(rig, monkeypatch):
    saved(rig)
    before = dict(state(rig))
    entries_before = {name: entry.value for name, entry in rig.agent.vault.entries.items()}
    calls = fake_credentials(monkeypatch)
    response = rig.client.post(URL + "/test", headers=H(), json={**PUBLIC, "workbench_id": "unsaved-change"})
    assert response.status_code == 400, response.text
    assert len(calls) == 0
    assert state(rig) == before
    assert {name: entry.value for name, entry in rig.agent.vault.entries.items()} == entries_before


@pytest.mark.parametrize("code", ["sts_http_401", "sts_unavailable", "sts_bucket_mismatch"])
def test_failed_probe_never_reports_authenticated_or_leaks_credentials(rig, monkeypatch, code, caplog):
    calls = fake_credentials(monkeypatch, error=StsError(code))
    response = rig.client.post(URL + "/test", headers=H(), json=payload())
    assert response.status_code >= 400, response.text
    assert len(calls) == 1
    assert response.json()["ok"] is False and response.json().get("authenticated") is not True
    assert state(rig) is None and not rig.agent.vault.entries
    assert_no_secrets(rig, response)
    assert AUTH not in caplog.text and SM4 not in caplog.text


def test_save_does_not_replace_live_services_or_clear_their_dedup_state(rig):
    inbox = rig.agent.xunjia_inbox
    publisher = rig.agent.catalog_publisher
    saved(rig)
    assert rig.agent.xunjia_inbox is inbox
    assert rig.agent.catalog_publisher is publisher
    assert not rig.agent.scheduler._started


def set_legacy_environment(monkeypatch):
    for key, value in {
        "OSS_ENDPOINT": "https://legacy-oss.example.invalid", "OSS_BUCKET": "legacy-bucket",
        "OSS_ACCESS_KEY_ID": "legacy-key-id", "OSS_ACCESS_KEY_SECRET": "legacy-secret",
        "OSS_WORKBENCH_ID": "legacy-workbench", "OSS_SEAL_KEY_XUNJIA": "11" * 32,
        "OSS_SEAL_KEY_DEPOSIT": "22" * 32,
    }.items():
        monkeypatch.setenv(key, value)


def start_without_scheduling(rig, monkeypatch):
    # 测启动装配，不运行任何周期任务或账号恢复；所有运行时仍为假件。
    scheduler_start = AsyncMock()
    monkeypatch.setattr(rig.agent.scheduler, "start", scheduler_start)
    monkeypatch.setattr(rig.agent.accounts, "recover", AsyncMock(side_effect=AssertionError("禁止恢复账号")))
    asyncio.run(rig.agent.start(recover=False))
    scheduler_start.assert_awaited_once()


def test_next_start_loads_page_vault_keys_and_destination_instead_of_legacy_environment(rig, monkeypatch):
    set_legacy_environment(monkeypatch)
    saved(rig)
    calls = fake_credentials(monkeypatch)
    start_without_scheduling(rig, monkeypatch)
    inbox = rig.agent.xunjia_inbox
    publisher = rig.agent.catalog_publisher
    assert inbox is not None and publisher is not None
    assert inbox.workbench_id == PUBLIC["workbench_id"]
    assert publisher.workbench_id == PUBLIC["workbench_id"]
    assert inbox.box is publisher.box
    provider = inbox.box._credentials_provider
    assert provider._auth_key == AUTH and provider._sm4_key == SM4.encode()
    assert inbox.box._host == "oss-cn-hangzhou.aliyuncs.com"
    assert calls == []  # 装配本身不认证，更不查询对象或发送消息。
    assert rig.client.get(URL, headers=H()).json()["restart_required"] is False


@pytest.mark.parametrize("broken", ["vault_offline", "bundle_missing", "invalid_ref"])
def test_saved_configuration_failure_disables_business_and_never_falls_back_to_environment(rig, monkeypatch, broken):
    set_legacy_environment(monkeypatch)
    saved(rig)
    if broken == "vault_offline":
        rig.agent.vault.offline = True
    elif broken == "bundle_missing":
        rig.agent.vault.entries.clear()
    else:
        rig.store.settings_set("ossbox.config", {**state(rig), "credentials_ref": "vault://account/qd01"})
    start_without_scheduling(rig, monkeypatch)
    assert rig.agent.xunjia_inbox is None and rig.agent.catalog_publisher is None
    assert rig.agent.oss_start_error
    assert rig.agent.oss_settings.applied_revision is None


@pytest.mark.parametrize("missing", ["workbench_id", "endpoint", "seal_keys"])
def test_authentication_settings_alone_do_not_enable_unconfigured_business(rig, monkeypatch, missing):
    set_legacy_environment(monkeypatch)
    fields = {}
    if missing == "seal_keys":
        monkeypatch.delenv("OSS_SEAL_KEY_XUNJIA")
        monkeypatch.delenv("OSS_SEAL_KEY_DEPOSIT")
    else:
        fields[missing] = ""
    response = rig.client.put(URL, headers=H(), json=payload(**fields))
    assert response.status_code == 200, response.text
    start_without_scheduling(rig, monkeypatch)
    assert rig.agent.xunjia_inbox is None and rig.agent.catalog_publisher is None
    view = rig.client.get(URL, headers=H()).json()
    assert view["inquiry_configured"] is False and view["catalog_configured"] is False


def test_save_during_running_process_keeps_existing_destination_and_requires_restart(rig, monkeypatch):
    set_legacy_environment(monkeypatch)
    saved(rig)
    start_without_scheduling(rig, monkeypatch)
    original = rig.agent.xunjia_inbox
    assert original._dedup.seen_command("previous-command") is True
    response = rig.client.put(URL, headers=H(), json=payload(workbench_id="next-workbench"))
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is True
    assert rig.agent.xunjia_inbox is original
    assert original.workbench_id == PUBLIC["workbench_id"]
    assert original._dedup.seen_command("previous-command") is False


def test_no_page_profile_preserves_existing_environment_configuration(tmp_path, monkeypatch):
    set_legacy_environment(monkeypatch)
    instance = Rig(tmp_path)
    try:
        inbox = instance.agent.xunjia_inbox
        assert inbox is not None and inbox.workbench_id == "legacy-workbench"
        start_without_scheduling(instance, monkeypatch)
        assert instance.agent.xunjia_inbox is inbox
        assert instance.agent.catalog_publisher is not None
        assert state(instance) is None
    finally:
        instance.close()
