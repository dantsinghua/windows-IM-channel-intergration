"""R6-104 独立验收：只依据 02 §3.4.12，不引用开发者 STS 页面测试。

真实 FastAPI HTTP 边界 + 隔离 SQLite + FakeVault + 独占回环 STS。
所有业务执行体使用公共 make_rig 假后端；测试之外的网络一律拒绝。
不把本套仿真结果称为 MQ 真实认证或安装包验收。
"""
from __future__ import annotations

import base64
import functools
import json
import logging
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from starlette.testclient import TestClient

from conftest import make_rig
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig
from qtrade_agent.vault_client import VaultUnavailable


PATH = "/api/v1/settings/ossbox"
TOKEN = "acceptance-only-admin-token"
AUTH = "acceptance-page-business-密钥"
SM4 = "fedcba9876543210"
AK = "STS.acceptance-page-only"
SK = "acceptance-page-only-access-secret"
SECURITY = "acceptance-page-only-session-secret"
PUBLIC = {"sts_url", "allow_http", "endpoint", "bucket", "workbench_id"}
VIEW = PUBLIC | {"saved", "auth_key_configured", "sm4_key_configured"}


@pytest.fixture(autouse=True)
def guard_network(monkeypatch):
    allowed, attempted = set(), []
    real_connect, real_resolve = socket.socket.connect, socket.getaddrinfo
    for name in list(os.environ):
        if name.startswith("OSS_") or name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name, raising=False)

    def check(address):
        if address not in allowed:
            attempted.append(address)
            raise AssertionError("independent acceptance attempted unowned network")

    def connect(sock, address):
        check(address)
        return real_connect(sock, address)

    def resolve(host, port, *args, **kwargs):
        check((host, int(port)))
        return real_resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    yield allowed
    assert attempted == [], "unowned network access was attempted"


@pytest.fixture
def sts(guard_network):
    state = SimpleNamespace(status=200, requests=[], body=None,
        expiration=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append((self.path, dict(self.headers.items())))
            body = state.body or {
                "access_key_id": AK, "access_key_secret": SK, "security_token": SECURITY,
                "bucket": "acceptance-page-bucket", "expiration": state.expiration,
            }
            data = json.dumps(body).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            if state.status == 302:
                self.send_header("Location", "https://unowned.invalid/secret")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    guard_network.add(server.server_address)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/api/sts"
    yield state
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    for level in ("admin", "write", "read"):
        r.store.upsert_api_client(app_id=f"acceptance_{level}", name=level, level=level,
                                  token=TOKEN if level == "admin" else f"acceptance-{level}")
    with TestClient(r.agent.create_api(), client=("127.0.0.1", 40810)) as client:
        r.http = client
        yield r
        client.portal.call(r.agent.bus.close)
    r.store.close()


def body(url="https://sts.acceptance.invalid/v1/api/sts", **extra):
    return {"sts_url": url, "allow_http": False, "endpoint": "", "bucket": "", "workbench_id": "",
            "auth_key": AUTH, "sm4_key": SM4, **extra}


def request(r, method="GET", payload=None, token=TOKEN):
    return r.http.request(method, PATH, json=payload,
        headers={"Authorization": f"Bearer {token}"} if token else {})


def public_response(response):
    result = response.json()
    result.pop("trace_id", None)
    return result


def profile(r):
    return r.store.settings_get("ossbox.config")


def snapshot(r):
    return (profile(r), {name: (entry.value, entry.scope) for name, entry in r.vault.entries.items()})


def assert_no_secrets(r, *texts):
    dump = "\n".join(r.store.con.iterdump())
    combined = dump + "\n" + "\n".join(str(t) for t in texts)
    for secret in (AUTH, SM4, AK, SK, SECURITY):
        assert secret not in combined


def test_fresh_install_has_blank_profile_and_no_secret_references(rig):
    response = request(rig)
    assert response.status_code == 200
    view = response.json()
    assert set(view["data"]) == VIEW
    assert view["data"] == {key: False if key in {"allow_http", "saved", "auth_key_configured", "sm4_key_configured"} else "" for key in VIEW}
    assert not view["restart_required"]
    assert not view["inquiry_configured"] and not view["catalog_configured"]
    assert profile(rig) is None and not rig.vault.entries
    assert_no_secrets(rig, response.text)


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("PUT", ""), ("POST", "/test")])
@pytest.mark.parametrize("level,expected", [(None, 401), ("read", 403), ("write", 403)])
def test_all_three_routes_require_admin_without_side_effects(rig, method, suffix, level, expected):
    response = rig.http.request(method, PATH + suffix, json=body(),
        headers={"Authorization": f"Bearer acceptance-{level}"} if level else {})
    assert response.status_code == expected
    assert profile(rig) is None and not rig.vault.entries


def test_save_bundle_only_and_public_readback_pending_restart(rig, sts, caplog):
    caplog.set_level(logging.DEBUG)
    response = request(rig, "PUT", body(sts.url, allow_http=True))
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert sts.requests == []
    saved = profile(rig)
    assert set(saved) == PUBLIC | {"revision", "credentials_ref"}
    ref = saved["credentials_ref"]
    assert ref.startswith("vault://settings/ossbox/") and ref.endswith("/credentials")
    assert len(rig.vault.entries) == 1
    entry = rig.vault.entries[ref.removeprefix("vault://")]
    assert entry.scope == "other"
    assert json.loads(entry.value) == {"auth_key": AUTH, "sm4_key": SM4}
    readback = request(rig)
    assert set(readback.json()["data"]) == VIEW
    assert readback.json()["data"]["auth_key_configured"] is True
    assert readback.json()["data"]["sm4_key_configured"] is True
    assert ref not in readback.text
    assert_no_secrets(rig, response.text, readback.text, caplog.text)


def test_omitted_secrets_preserve_values_in_single_replacement_bundle(rig):
    assert request(rig, "PUT", body()).status_code == 200
    old = profile(rig)
    draft = {key: value for key, value in body(workbench_id="new-terminal").items() if key in PUBLIC}
    response = request(rig, "PUT", draft)
    assert response.status_code == 200
    new = profile(rig)
    assert new["revision"] != old["revision"] and new["workbench_id"] == "new-terminal"
    assert len(rig.vault.entries) == 1
    assert json.loads(next(iter(rig.vault.entries.values())).value) == {"auth_key": AUTH, "sm4_key": SM4}
    assert old["credentials_ref"].removeprefix("vault://") in rig.vault.deleted


@pytest.mark.parametrize("missing", ["auth_key", "sm4_key"])
def test_first_save_requires_both_secrets(rig, missing):
    draft = body()
    del draft[missing]
    response = request(rig, "PUT", draft)
    assert response.status_code == 400
    assert profile(rig) is None and not rig.vault.entries


@pytest.mark.parametrize("field", ["auth_key", "sm4_key"])
@pytest.mark.parametrize("invalid", ["", None, False, 1, [], {}])
def test_explicit_empty_or_nonstring_secret_rejected_not_preserved(rig, field, invalid):
    assert request(rig, "PUT", body()).status_code == 200
    before = snapshot(rig)
    response = request(rig, "PUT", body(**{field: invalid}))
    assert response.status_code == 400 and response.json()["code"] == "INVALID_ARGS"
    assert snapshot(rig) == before


@pytest.mark.parametrize("missing", sorted(PUBLIC))
@pytest.mark.parametrize("method,suffix", [("PUT", ""), ("POST", "/test")])
def test_public_five_keys_are_one_complete_shape(rig, missing, method, suffix):
    draft = body()
    del draft[missing]
    response = rig.http.request(method, PATH + suffix, json=draft, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 400
    assert profile(rig) is None and not rig.vault.entries


@pytest.mark.parametrize("change", [
    {"credentials_ref": "vault://other"}, {"revision": "caller-selected"}, {"unknown": True},
    {"allow_http": "true"}, {"allow_http": 1}, {"sts_url": ""},
    {"sts_url": "http://sts.invalid/a"}, {"sts_url": "https://user:password@sts.invalid/a"},
    {"sts_url": "https://sts.invalid/a?q=secret"}, {"sts_url": "https://sts.invalid/a#secret"},
    {"endpoint": "http://oss.invalid"}, {"endpoint": "https://oss.invalid/path"},
    {"bucket": "BadBucket"}, {"workbench_id": "terminal/name"},
    {"sm4_key": "中" * 16}, {"sm4_key": "too-short"}, {"auth_key": "x" * 4097},
])
def test_malformed_profile_is_rejected_before_vault_or_db(rig, change):
    response = request(rig, "PUT", body(**change))
    assert response.status_code == 400 and response.json()["code"] == "INVALID_ARGS"
    assert profile(rig) is None and not rig.vault.entries


def test_utf8_byte_length_and_secret_whitespace_preserved(rig):
    draft = body(auth_key=" keep surrounding spaces ", sm4_key="中" * 5 + " ")
    response = request(rig, "PUT", draft)
    assert response.status_code == 200
    saved = json.loads(next(iter(rig.vault.entries.values())).value)
    assert saved == {"auth_key": draft["auth_key"], "sm4_key": draft["sm4_key"]}


def test_vault_offline_save_fails_closed_and_preserves_old_profile(rig):
    assert request(rig, "PUT", body()).status_code == 200
    before = snapshot(rig)
    rig.vault.offline = True
    response = request(rig, "PUT", body(workbench_id="changed"))
    assert response.status_code == 503 and response.json()["code"] == "NOT_READY"
    assert response.json()["error"]["reason"] == "vault_unavailable"
    assert snapshot(rig) == before


def test_database_failure_preserves_old_profile_and_removes_new_bundle(rig):
    assert request(rig, "PUT", body()).status_code == 200
    before = snapshot(rig)
    rig.store.con.execute("CREATE TRIGGER acceptance_reject_profile BEFORE UPDATE ON settings WHEN NEW.key='ossbox.config' BEGIN SELECT RAISE(ABORT, 'acceptance forced failure'); END")
    response = request(rig, "PUT", body(workbench_id="changed"))
    assert response.status_code == 503
    assert snapshot(rig) == before
    assert "acceptance forced failure" not in response.text


def test_failed_old_bundle_cleanup_does_not_undo_valid_new_profile(rig, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    assert request(rig, "PUT", body()).status_code == 200
    old = profile(rig)

    async def failed_delete(name):
        raise VaultUnavailable(AUTH)

    monkeypatch.setattr(rig.vault, "delete", failed_delete)
    response = request(rig, "PUT", body(workbench_id="new-terminal"))
    assert response.status_code == 200
    assert profile(rig)["revision"] != old["revision"]
    assert profile(rig)["workbench_id"] == "new-terminal"
    assert len(rig.vault.entries) == 2
    assert_no_secrets(rig, response.text, caplog.text)


def test_authentication_test_only_gets_sts_and_returns_public_triplet(rig, sts, caplog):
    caplog.set_level(logging.DEBUG)
    before = snapshot(rig)
    response = rig.http.post(PATH + "/test", json=body(sts.url, allow_http=True),
                             headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert set(public_response(response)) == {"ok", "authenticated", "bucket", "expiration"}
    assert response.json()["ok"] is True and response.json()["authenticated"] is True
    assert response.json()["bucket"] == "acceptance-page-bucket"
    assert datetime.fromisoformat(response.json()["expiration"]) == datetime.fromisoformat(sts.expiration)
    assert snapshot(rig) == before
    assert len(sts.requests) == 1
    path, headers = sts.requests[0]
    assert path == "/v1/api/sts"
    token = {key.lower(): value for key, value in headers.items()}["access_token"]
    packed = base64.b64decode(token)
    decryptor = Cipher(algorithms.SM4(SM4.encode()), modes.CBC(packed[:16])).decryptor()
    padded = decryptor.update(packed[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    decoded = unpadder.update(padded) + unpadder.finalize()
    assert json.loads(decoded)["key"] == AUTH
    assert_no_secrets(rig, response.text, caplog.text)


def test_authentication_test_can_read_saved_secrets_without_creating_revision(rig, sts):
    assert request(rig, "PUT", body()).status_code == 200
    before = snapshot(rig)
    draft = {key: value for key, value in body(sts.url, allow_http=True).items() if key in PUBLIC}
    response = rig.http.post(PATH + "/test", json=draft, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200 and response.json()["authenticated"] is True
    assert snapshot(rig) == before


@pytest.mark.parametrize("status", [401, 403, 500, 302])
def test_sts_failure_or_redirect_is_fixed_failure_without_leak_or_save(rig, sts, status, caplog):
    caplog.set_level(logging.DEBUG)
    sts.status = status
    sts.body = {"error": AUTH, "access_key_secret": SK, "security_token": SECURITY}
    response = rig.http.post(PATH + "/test", json=body(sts.url, allow_http=True),
                             headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503 and response.json()["code"] == "NOT_READY"
    assert response.json()["error"]["reason"] == f"sts_http_{status}"
    assert len(sts.requests) == 1 and profile(rig) is None and not rig.vault.entries
    assert_no_secrets(rig, response.text, caplog.text)


def test_missing_saved_secret_never_silently_uses_environment(rig, monkeypatch):
    assert request(rig, "PUT", body()).status_code == 200
    rig.vault.entries.clear()
    monkeypatch.setenv("OSS_STS_AUTH_KEY", AUTH)
    monkeypatch.setenv("OSS_STS_SM4_KEY", SM4)
    draft = {key: value for key, value in body().items() if key in PUBLIC}
    response = rig.http.post(PATH + "/test", json=draft, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503
    assert not rig.vault.entries


def test_save_does_not_replace_running_consumers_or_their_dedup(rig):
    inbox = SimpleNamespace(dedup={"old-message"})
    catalog = SimpleNamespace(dedup={"old-catalog"})
    rig.agent.xunjia_inbox, rig.agent.catalog_publisher = inbox, catalog
    response = request(rig, "PUT", body())
    assert response.status_code == 200 and response.json()["restart_required"] is True
    assert rig.agent.xunjia_inbox is inbox and inbox.dedup == {"old-message"}
    assert rig.agent.catalog_publisher is catalog and catalog.dedup == {"old-catalog"}
    readback = request(rig).json()
    assert readback["inquiry_configured"] is True and readback["catalog_configured"] is True


def start_without_scheduling(rig, monkeypatch):
    async def idle():
        pass
    monkeypatch.setattr(rig.agent.scheduler, "start", idle)
    rig.http.portal.call(functools.partial(rig.agent.start, recover=False))


def test_saved_auth_only_applies_at_start_but_does_not_claim_business_configured(rig, monkeypatch):
    assert request(rig, "PUT", body()).status_code == 200
    assert request(rig).json()["restart_required"] is True
    start_without_scheduling(rig, monkeypatch)
    view = request(rig).json()
    assert view["restart_required"] is False
    assert not view["inquiry_configured"] and not view["catalog_configured"]


@pytest.mark.parametrize("broken", ["offline", "missing", "corrupt", "malformed-profile"])
def test_unusable_page_profile_start_disables_consumers_despite_valid_env(rig, monkeypatch, broken):
    assert request(rig, "PUT", body()).status_code == 200
    for key, value in {
        "OSS_AUTH_MODE": "static", "OSS_ENDPOINT": "https://oss.env.invalid",
        "OSS_BUCKET": "environment-bucket", "OSS_ACCESS_KEY_ID": "env-ak",
        "OSS_ACCESS_KEY_SECRET": "env-sk", "OSS_WORKBENCH_ID": "env-terminal",
        "OSS_SEAL_KEY_XUNJIA": "ab" * 32, "OSS_SEAL_KEY_DEPOSIT": "cd" * 32,
    }.items():
        monkeypatch.setenv(key, value)
    if broken == "offline":
        rig.vault.offline = True
    elif broken == "missing":
        rig.vault.entries.clear()
    elif broken == "corrupt":
        next(iter(rig.vault.entries.values())).value = "{invalid-json"
    else:
        rig.store.settings_set("ossbox.config", {"invalid": True})
    rig.agent.xunjia_inbox, rig.agent.catalog_publisher = object(), object()
    start_without_scheduling(rig, monkeypatch)
    assert rig.agent.xunjia_inbox is None and rig.agent.catalog_publisher is None
    response = request(rig)
    if broken == "malformed-profile":
        assert response.status_code == 503
    else:
        assert response.json()["restart_required"] is True


@pytest.mark.parametrize("stored_json", ["null", "[]"])
def test_corrupt_persisted_profile_never_falls_back_or_crashes_agent(rig, monkeypatch, tmp_path, stored_json):
    """R6-104：profile 行存在即优先；损坏只能禁用 OSS，不能回退或使 Agent 崩溃。"""
    assert request(rig, "PUT", body()).status_code == 200
    rig.store.con.execute("UPDATE settings SET value_json=? WHERE key='ossbox.config'", (stored_json,))
    rig.store.con.commit()
    for key, value in {
        "OSS_AUTH_MODE": "static", "OSS_ENDPOINT": "https://oss.env.invalid",
        "OSS_BUCKET": "environment-bucket", "OSS_ACCESS_KEY_ID": "env-ak",
        "OSS_ACCESS_KEY_SECRET": "env-sk", "OSS_WORKBENCH_ID": "env-terminal",
        "OSS_SEAL_KEY_XUNJIA": "ab" * 32, "OSS_SEAL_KEY_DEPOSIT": "cd" * 32,
    }.items():
        monkeypatch.setenv(key, value)
    restarted = AgentApp(AgentConfig(), db_path=str(tmp_path / "agent.db"), clock=rig.clock,
        containers=rig.containers, adb=rig.adb, vault=rig.vault, fs=rig.fs,
        winagent_transport=rig.winagent, winagent_base_url="http://winagent.fake:17610",
        winagent_token=rig.winagent.token, wsl_total_mb=11264, boot_poll_s=0).open()
    async def idle():
        pass
    monkeypatch.setattr(restarted.scheduler, "start", idle)
    try:
        rig.http.portal.call(functools.partial(restarted.start, recover=False))
        assert restarted.xunjia_inbox is None and restarted.catalog_publisher is None
    finally:
        rig.http.portal.call(restarted.stop)


def test_new_agent_reads_saved_profile_and_applies_it_before_scheduler(rig, monkeypatch, tmp_path):
    assert request(rig, "PUT", body(endpoint="oss.page.invalid", workbench_id="page-terminal")).status_code == 200
    for key, value in {"OSS_WORKBENCH_ID": "wrong-environment-terminal",
                       "OSS_SEAL_KEY_XUNJIA": "ab" * 32, "OSS_SEAL_KEY_DEPOSIT": "cd" * 32}.items():
        monkeypatch.setenv(key, value)
    restarted = AgentApp(AgentConfig(), db_path=str(tmp_path / "agent.db"), clock=rig.clock,
        containers=rig.containers, adb=rig.adb, vault=rig.vault, fs=rig.fs,
        winagent_transport=rig.winagent, winagent_base_url="http://winagent.fake:17610",
        winagent_token=rig.winagent.token, wsl_total_mb=11264, boot_poll_s=0).open()
    observations = []
    async def observe_start():
        observations.append((restarted.xunjia_inbox, restarted.catalog_publisher))
    monkeypatch.setattr(restarted.scheduler, "start", observe_start)
    try:
        assert restarted.xunjia_inbox is None and restarted.catalog_publisher is None
        rig.http.portal.call(functools.partial(restarted.start, recover=False))
        assert len(observations) == 1
        inbox, publisher = observations[0]
        assert inbox is not None and publisher is not None
        assert inbox.workbench_id == "page-terminal"
        assert publisher.workbench_id == "page-terminal"
        with TestClient(restarted.create_api(), client=("127.0.0.1", 40811)) as client:
            view = client.get(PATH, headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert view["restart_required"] is False
        assert view["inquiry_configured"] is True and view["catalog_configured"] is True
    finally:
        rig.http.portal.call(restarted.stop)


def test_no_profile_environment_sts_requires_explicit_url(monkeypatch):
    from qtrade_agent.ossbox.oss_http import OssV1Box
    for key, value in {"OSS_AUTH_MODE": "sts", "OSS_ENDPOINT": "oss.env.invalid",
                       "OSS_STS_AUTH_KEY": AUTH, "OSS_STS_SM4_KEY": SM4}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        OssV1Box.from_env()


def test_shipping_sources_contain_no_scene_specific_auth_defaults():
    root = Path(__file__).resolve().parents[2]
    forbidden = ["synthetic-business-key", "0123456789abcdef", "nytoken.yingmi.cn",
                 "nginx.mq.dq.yingmi-inc.com"]
    paths = [root / "src/qtrade_agent/ossbox/settings.py", root / "src/qtrade_agent/ossbox/routes.py",
             root / "src/qtrade_agent/ossbox/oss_http.py", root / "console/src/pages/mail/OssboxConfig.vue"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path
