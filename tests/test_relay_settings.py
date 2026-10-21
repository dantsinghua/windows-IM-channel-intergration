"""V2.2 公网认证设置：临时部署文件、假 HTTP，绝不操作真实队列。"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from urllib.request import OpenerDirector

import pytest

from tests.test_api_ext import H, P, TOK_R, TOK_W, Rig


URL = f"{P}/settings/ossbox"
BASE = "https://relay.example.invalid/its-relay"
KEY = "synthetic-relay-business-secret"
SM4 = "0123456789abcdef"
FLAT = f"key: {KEY}\nsm4_key: '{SM4}'\n"
NESTED = f"auth:\n  key: {KEY}\n  sm4_key: '{SM4}'\n"
FIELDS = {"key": KEY, "sm4_key": SM4}


@pytest.fixture
def relay_rig(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("OSS_", "OSSBOX_", "XUNJIA_")):
            monkeypatch.delenv(name)
    private = tmp_path / "deployment"
    private.mkdir()
    auth_file = private / "deployed-auth.yaml"
    config = private / "collector.json"
    config.write_text(json.dumps({"file_gateway": {"auth_file": auth_file.name,
        "uplink_url": "https://uplink.example.invalid", "downlink_url": BASE}}))
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", str(config))
    monkeypatch.setenv("XUNJIA_FILE_GATEWAY_URL", BASE)
    monkeypatch.setattr(OpenerDirector, "open", lambda *a, **k: pytest.fail("禁止真实公网认证 HTTP"))
    rig = Rig(tmp_path)
    rig.auth_file = auth_file
    rig.collector_file = config
    yield rig
    rig.close()


def assert_private(rig, *responses, extra=()):
    public = "".join(r.text for r in responses)
    public += json.dumps(rig.store.settings_list_prefix(""), ensure_ascii=False)
    public += json.dumps(rig.store.list_audit(), ensure_ascii=False)
    for value in (KEY, SM4, str(rig.auth_file), *extra):
        assert value not in public


class PendingReply(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self):
        super().__init__(b'{"pending":3}')


def pending_http(monkeypatch, *, error=None):
    calls = []

    def fake_open(opener, request, timeout=None):
        calls.append(request)
        assert request.get_method() == "GET"
        assert request.full_url == BASE + "/api/files/pending"
        assert request.data is None
        if error:
            raise error
        return PendingReply()

    monkeypatch.setattr(OpenerDirector, "open", fake_open)
    return calls


def test_v22_get_exposes_only_fixed_relay_metadata_without_auth_content(relay_rig):
    response = relay_rig.client.get(URL, headers=H())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "its_relay"
    assert body["relay"] == {"base_url": BASE, "config_file": "config.yaml", "configured": False,
        "key_configured": False, "sm4_key_configured": False}
    assert body["restart_required"] is False
    for field in ("sts_url", "bucket", "endpoint", "workbench_id", "auth_key", "sm4_key", "config_yaml"):
        assert field not in body.get("data", {})
    assert_private(relay_rig, response)


def test_save_creates_deployment_auth_file_without_vault_or_business_start(relay_rig, caplog):
    response = relay_rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    from relay.file_gateway import GatewayAuth
    auth = GatewayAuth.from_file(relay_rig.auth_file)
    assert auth.key == KEY and auth.sm4_key == SM4.encode()
    assert response.json()["relay"]["configured"] is True
    assert response.json()["restart_required"] is True
    assert relay_rig.store.settings_get("ossbox.config") is None
    assert not relay_rig.agent.vault.entries
    assert not relay_rig.agent.scheduler._started and not relay_rig.agent._started
    assert relay_rig.agent.xunjia_v22_host is None
    assert_private(relay_rig, response)
    assert KEY not in caplog.text and SM4 not in caplog.text


@pytest.mark.parametrize("invalid", [
    {"key": ""}, {"key": None}, {"key": 12}, {"key": "x" * 4097},
    {"sm4_key": ""}, {"sm4_key": None}, {"sm4_key": 123}, {"sm4_key": "short"},
    {"sm4_key": "0123456789abcdef0"}, {"sm4_key": "中" * 16},
])
def test_invalid_fields_are_rejected_without_destroying_existing_auth(relay_rig, invalid, caplog):
    relay_rig.auth_file.write_text(FLAT)
    before = relay_rig.auth_file.read_bytes()
    response = relay_rig.client.put(URL, headers=H(), json={**FIELDS, **invalid})
    assert response.status_code in (400, 422), response.text
    assert response.json()["code"] == "INVALID_ARGS"
    assert relay_rig.auth_file.read_bytes() == before
    assert_private(relay_rig, response)
    assert KEY not in caplog.text and SM4 not in caplog.text


def test_sm4_validation_counts_utf8_bytes(relay_rig):
    sm4 = "中" * 5 + "a"
    assert len(sm4.encode()) == 16
    response = relay_rig.client.put(URL, headers=H(), json={"key": KEY, "sm4_key": sm4})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("yaml_text", [FLAT, NESTED, "\ufeff" + NESTED])
def test_same_value_save_preserves_original_bytes_and_unrelated_settings(relay_rig, yaml_text):
    original = "# deployment note\n" + yaml_text.lstrip("\ufeff") + "logging:\n  level: warning\n"
    if yaml_text.startswith("\ufeff"):
        original = "\ufeff" + original
    relay_rig.auth_file.write_text(original, encoding="utf-8")
    before = relay_rig.auth_file.read_bytes()
    response = relay_rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    assert relay_rig.auth_file.read_bytes() == before


@pytest.mark.parametrize("yaml_text", [FLAT, NESTED])
@pytest.mark.parametrize("rotation", [{"key": "synthetic-rotated-key"}, {"sm4_key": "fedcba9876543210"}])
def test_partial_rotation_retains_other_key_and_unrelated_yaml_values(relay_rig, yaml_text, rotation):
    import yaml
    relay_rig.auth_file.write_text(yaml_text + "logging:\n  level: warning\nother:\n  retries: 7\n")
    response = relay_rig.client.put(URL, headers=H(), json=rotation)
    assert response.status_code == 200, response.text
    data = yaml.safe_load(relay_rig.auth_file.read_text())
    auth = data.get("auth", data)
    assert auth["key"] == rotation.get("key", KEY)
    assert auth["sm4_key"] == rotation.get("sm4_key", SM4)
    assert data["logging"] == {"level": "warning"} and data["other"] == {"retries": 7}
    assert_private(relay_rig, response, extra=tuple(rotation.values()))


@pytest.mark.parametrize("existing", ["key: one\nkey: two\nsm4_key: '0123456789abcdef'",
    "auth:\n  key: one\n  key: two\n  sm4_key: '0123456789abcdef'", "key: [broken", "[]"])
def test_ambiguous_or_malformed_existing_yaml_cannot_be_silently_overwritten(relay_rig, existing):
    relay_rig.auth_file.write_text(existing)
    response = relay_rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code in (400, 503), response.text
    assert relay_rig.auth_file.read_text() == existing


def test_successful_probe_does_not_publish_configuration_or_restart_requirement(relay_rig, monkeypatch):
    pending_http(monkeypatch)
    before = relay_rig.client.get(URL, headers=H())
    response = relay_rig.client.post(URL + "/test", headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    after = relay_rig.client.get(URL, headers=H())
    assert before.json()["relay"] == after.json()["relay"]
    assert after.json()["restart_required"] is False


@pytest.mark.parametrize("extra", [{"config_file": "/tmp/forbidden.yaml"},
    {"auth_file": "../../forbidden.yaml"}, {"base_url": "https://other.invalid"},
    {"sts_url": "https://sts.invalid"}, {"bucket": "unused-bucket"}, {"config_yaml": FLAT}])
def test_save_rejects_path_or_legacy_field_injection(relay_rig, extra):
    response = relay_rig.client.put(URL, headers=H(), json={**FIELDS, **extra})
    assert response.status_code in (400, 422), response.text
    assert not relay_rig.auth_file.exists()


@pytest.mark.parametrize("body", [{}, {"key": KEY}, {"sm4_key": SM4}])
def test_first_save_requires_both_keys(relay_rig, body):
    response = relay_rig.client.put(URL, headers=H(), json=body)
    assert response.status_code in (400, 422), response.text
    assert not relay_rig.auth_file.exists()


def test_atomic_replace_failure_preserves_prior_file_and_returns_safe_error(relay_rig, monkeypatch):
    relay_rig.auth_file.write_text(FLAT)
    before = relay_rig.auth_file.read_bytes()
    original = os.replace
    replacements = []

    def fail_auth_replace(src, dst, *args, **kwargs):
        if Path(dst).resolve() == relay_rig.auth_file.resolve():
            replacements.append(Path(src))
            assert relay_rig.auth_file.read_bytes() == before
            raise OSError(f"synthetic failure {KEY} {SM4}")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_auth_replace)
    response = relay_rig.client.put(URL, headers=H(), json={"key": "rotated-key", "sm4_key": SM4})
    assert response.status_code == 503, response.text
    assert replacements, "必须经原子替换发布认证文件"
    assert relay_rig.auth_file.read_bytes() == before
    assert_private(relay_rig, response)


@pytest.mark.parametrize("from_saved", [False, True])
def test_connection_probe_is_pending_only_and_does_not_save(relay_rig, monkeypatch, from_saved, caplog):
    if from_saved:
        relay_rig.auth_file.write_text(FLAT)
    before = relay_rig.auth_file.read_bytes() if from_saved else None
    calls = pending_http(monkeypatch)
    response = relay_rig.client.post(URL + "/test", headers=H(), json={} if from_saved else FIELDS)
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True
    assert len(calls) == 1
    token = calls[0].get_header("Access_token")
    assert isinstance(token, str) and token
    assert (relay_rig.auth_file.read_bytes() if relay_rig.auth_file.exists() else None) == before
    assert not relay_rig.agent.vault.entries
    assert relay_rig.store.settings_get("ossbox.config") is None
    assert not relay_rig.agent.scheduler._started
    assert_private(relay_rig, response, extra=(token,))
    assert KEY not in caplog.text and token not in caplog.text


def test_every_probe_generates_a_new_sm4_access_token_for_same_relay_url(relay_rig, monkeypatch):
    calls = pending_http(monkeypatch)
    responses = [relay_rig.client.post(URL + "/test", headers=H(), json=FIELDS) for _ in range(2)]
    assert all(response.status_code == 200 for response in responses)
    assert len(calls) == 2
    tokens = [request.get_header("Access_token") for request in calls]
    assert len(set(tokens)) == 2
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    for token in tokens:
        raw = base64.b64decode(token, validate=True)
        decryptor = Cipher(algorithms.SM4(SM4.encode()), modes.CBC(raw[:16])).decryptor()
        padded = decryptor.update(raw[16:]) + decryptor.finalize()
        unpad = padding.PKCS7(128).unpadder()
        plain = json.loads(unpad.update(padded) + unpad.finalize())
        assert plain["key"] == KEY and str(plain["ts"]).isdigit()
    assert_private(relay_rig, *responses, extra=tokens)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_probe_failure_never_reports_success_or_echoes_upstream_secrets(relay_rig, monkeypatch, caplog, error_type):
    calls = pending_http(monkeypatch, error=error_type(f"upstream {KEY} {SM4} access_token=do-not-echo"))
    response = relay_rig.client.post(URL + "/test", headers=H(), json=FIELDS)
    assert response.status_code == 503, response.text
    assert response.json()["ok"] is False and response.json().get("authenticated") is not True
    assert len(calls) == 1
    assert_private(relay_rig, response, extra=("do-not-echo",))
    assert KEY not in caplog.text and SM4 not in caplog.text
    assert not relay_rig.auth_file.exists()


def test_empty_probe_without_saved_auth_fails_locally_without_network(relay_rig):
    response = relay_rig.client.post(URL + "/test", headers=H(), json={})
    assert response.status_code in (400, 503), response.text
    assert response.json()["ok"] is False
    assert not relay_rig.auth_file.exists()


@pytest.mark.parametrize("method", ["get", "put", "post"])
@pytest.mark.parametrize("token,expected", [(None, 401), (TOK_R, 403), (TOK_W, 403)])
def test_relay_settings_and_probe_remain_admin_only(relay_rig, method, token, expected):
    path = URL + ("/test" if method == "post" else "")
    kwargs = {"headers": H(token)} if token else {}
    if method != "get":
        kwargs["json"] = FIELDS
    response = getattr(relay_rig.client, method)(path, **kwargs)
    assert response.status_code == expected, response.text
    assert not relay_rig.auth_file.exists()
