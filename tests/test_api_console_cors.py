"""R6-85:桌面壳渲染进程跨源直连 Agent 的 CORS 口径(2026-10-10 真实链路实测补)。

- 预检 `OPTIONS` 由最外层中间件直接应答(此前回 405,桌面壳一条 API 都发不出去);
- 放行表 `[api] console_origins` 只收 `null`(file://)与回环 http(s) 源,其余静默丢弃;
- 放行不是鉴权:带放行源但无令牌仍 401;预检不进 `audit_log`。
"""
from __future__ import annotations

from qtrade_agent.api.app import console_cors_origins
from qtrade_agent.config import AgentConfig, ApiConfig, BackupConfig, RuntimeConfig
from tests.test_integration_wiring_common import H, TOKEN_READ, close_rig, make_rig

PREFLIGHT = {"Origin": "null", "Access-Control-Request-Method": "GET",
             "Access-Control-Request-Headers": "x-trace-id,x-qt-api-min"}


def _cfg(tmp_path, origins):
    return AgentConfig(backup=BackupConfig(backup_dir=str(tmp_path / "backup")),
                       runtime=RuntimeConfig(accounts_dir=str(tmp_path / "accounts")),
                       api=ApiConfig(console_origins=tuple(origins)))


def test_allowlist_only_keeps_null_and_loopback_origins():
    got = console_cors_origins(("null", "http://127.0.0.1:5273/", "https://localhost:17601", "http://[::1]:5273",
                                "http://192.168.3.10:5273", "https://evil.example", "file://", "null"))
    assert got == ["null", "http://127.0.0.1:5273", "https://localhost:17601", "http://[::1]:5273"]


def test_default_config_allows_only_null_origin():
    assert ApiConfig().console_origins == ("null",)
    assert AgentConfig.from_toml_dict({"api": {"console_origins": ["null", "http://127.0.0.1:5313"]}}).api.console_origins \
        == ("null", "http://127.0.0.1:5313")


def test_preflight_from_desktop_shell_is_answered_without_auth_or_audit(tmp_path):
    rig = make_rig(tmp_path, cfg=_cfg(tmp_path, ["null", "http://127.0.0.1:5313"]))
    try:
        before = len(rig.store.list_audit())
        r = rig.client.options("/api/v1/accounts", headers=PREFLIGHT)
        assert r.status_code == 200, r.text
        assert r.headers.get("access-control-allow-origin") == "null"
        allowed = {h.strip().lower() for h in r.headers.get("access-control-allow-headers", "").split(",")}
        assert {"x-trace-id", "x-qt-api-min"} <= allowed
        assert "GET" in r.headers.get("access-control-allow-methods", "")
        assert len(rig.store.list_audit()) == before, "预检不得进 audit_log"

        # 开发期 Vite 回环源同样放行;真实请求能把版本头暴露给页面
        r2 = rig.client.get("/api/v1/system/version", headers={**H(TOKEN_READ), "Origin": "http://127.0.0.1:5313"})
        assert r2.status_code == 200
        assert r2.headers.get("access-control-allow-origin") == "http://127.0.0.1:5313"
        assert "X-QT-Api-Version" in r2.headers.get("access-control-expose-headers", "")
    finally:
        close_rig(rig)


def test_cors_allow_is_not_authentication(tmp_path):
    rig = make_rig(tmp_path, cfg=_cfg(tmp_path, ["null"]))
    try:
        r = rig.client.get("/api/v1/accounts", headers={"Origin": "null"})
        assert r.status_code == 401
        assert r.headers.get("access-control-allow-origin") == "null"      # 浏览器能把 401 信封交给页面去处理
    finally:
        close_rig(rig)


def test_non_loopback_origin_gets_no_cors_headers(tmp_path):
    rig = make_rig(tmp_path, cfg=_cfg(tmp_path, ["null", "http://192.168.3.10:5273"]))
    try:
        r = rig.client.options("/api/v1/accounts", headers={**PREFLIGHT, "Origin": "http://192.168.3.10:5273"})
        assert r.status_code == 400                                          # Starlette 对不在表内的源:预检拒绝
        assert "access-control-allow-origin" not in r.headers
    finally:
        close_rig(rig)
