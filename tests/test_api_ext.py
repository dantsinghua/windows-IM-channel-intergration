"""02 §3.4 补齐的那批端点(#24/#36/#51/#74/#75/#78/#80/#84/#85/#86/#87/#90~#93)+ 三处横切修复:
未知路由/方法/422 走 00 §10 信封、WS 握手被拒能看到 4401、成功响应带 trace_id、#107 时间键 ISO、#77 两组并排。

全部后端是假件(``FakeWeChatWinAgent`` 派生的 ``ExtWinAgent`` 另补 ``/wa/v1/net``、``/wa/v1/wsl/*``、
非 sample 的 ``POST /wa/v1/probe``);**不碰真 docker / adb / WinAgent / 出网**。
"""
from __future__ import annotations

import json
import zipfile
from typing import Any, Optional

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qtrade_agent.adapters.wechat import FakeWeChatWinAgent
from qtrade_agent.api.routes_ext import DEFAULT_NOTICE_VERSION
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, RuntimeConfig
from qtrade_agent.maintenance import BackupConfig, FakeDisk
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs
from qtrade_agent.vault_client import FakeVault
from qtrade_agent.webhook import FakeHttp
from tests.conftest import Clock

TOK_A = "ext-admin-token"
TOK_W = "ext-write-token"
TOK_R = "ext-read-token"
P = "/api/v1"


def H(token: str = TOK_A, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


class ExtWinAgent(FakeWeChatWinAgent):
    """在假 WinAgent 上补本批端点要转发的四条路由;``net_offline`` 可编程成「对面没这个端点」。"""

    def __init__(self) -> None:
        super().__init__()
        self.net = {"proxy": {"winhttp": {"http": "http://127.0.0.1:7890"}}, "mtu": 1280, "reboot_required": False}
        self.wsl_config = {"path": "C:/Users/x/.wslconfig", "effective": {"memory": "11GB"}, "backups": []}
        self.net_supported = True
        self.wsl_restarts: list[dict[str, Any]] = []
        self.probe_rows: list[dict[str, Any]] = [{"side": "windows", "target": "msfxg.3g.qq.com:8080",
                                                  "status": "OK", "level_reached": "http"}]

    @property
    def sample_rows(self) -> list[dict[str, Any]]:
        """`POST /probe {mode:'sample'}` 的假行落在被委托的 ``FakeWinAgent`` 上,这里只做转发。"""
        return self._base.sample_rows

    @sample_rows.setter
    def sample_rows(self, rows: list[dict[str, Any]]) -> None:
        self._base.sample_rows = rows

    async def __call__(self, method: str, url: str, headers: dict[str, str], body: Optional[bytes], timeout_s: float):
        path = url.split("://", 1)[-1].split("/", 1)[1] if "://" in url else url
        path = "/" + path
        bare = path.split("?", 1)[0]
        rh = {"Content-Type": "application/json"}
        data = json.loads(body) if body else {}
        if self.offline:
            raise OSError("connection refused")
        if bare == "/wa/v1/net" and method == "GET":
            if not self.net_supported:
                return 404, rh, b'{"ok":false,"code":"TARGET_NOT_FOUND"}'
            return 200, rh, json.dumps(self.net).encode()
        if bare == "/wa/v1/wsl/config" and method == "GET":
            return 200, rh, json.dumps(self.wsl_config).encode()
        if bare == "/wa/v1/wsl/restart" and method == "POST":
            self.wsl_restarts.append(data)
            return 200, rh, b'{"ok":true}'
        if bare == "/wa/v1/probe" and method == "POST" and data.get("mode") != "sample":
            return 200, rh, json.dumps({"run_id": data.get("run_id"), "results": self.probe_rows}).encode()
        return await super().__call__(method, url, headers, body, timeout_s)


class Rig:
    def __init__(self, tmp_path):
        self.clock = Clock(auto_step_ms=50)
        self.wa = ExtWinAgent()
        cfg = AgentConfig(backup=BackupConfig(backup_dir=str(tmp_path / "backup")),
                          runtime=RuntimeConfig(accounts_dir=str(tmp_path / "accounts")))
        self.agent = AgentApp(cfg, db_path=str(tmp_path / "agent.db"), clock=self.clock,
                              containers=FakeContainers(), adb=FakeAdb(), vault=FakeVault(),
                              winagent_transport=self.wa, winagent_base_url="http://winagent.fake:17610",
                              winagent_token=self.wa.token, fs=FakeFs(), wsl_total_mb=11264, boot_poll_s=0,
                              http=FakeHttp(), disk=FakeDisk(free=100_000), data_dir=str(tmp_path)).open()
        self.agent.pool.set_windows(total_mb=16384, wechat_enabled=True, known=True)
        self.agent.health.set_winagent(True, version=self.wa.version, user_agent=True)
        self.store = self.agent.store
        self.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_A)
        self.store.upsert_api_client(app_id="writer", name="可写", level="write", token=TOK_W)
        self.store.upsert_api_client(app_id="reader", name="只读", level="read", token=TOK_R)
        self.client = TestClient(self.agent.create_api(), client=("127.0.0.1", 40000))
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        self.store.close()

    def job(self, job_id: str) -> dict[str, Any]:
        r = self.client.get(f"{P}/jobs/{job_id}", headers=H())
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def wait_job(self, job_id: str, *, tries: int = 50) -> dict[str, Any]:
        for _ in range(tries):
            row = self.job(job_id)
            if row["state"] in ("succeeded", "failed", "cancelled"):
                return row
            self.client.get(f"{P}/system/version", headers=H())        # 让事件循环转一圈
        raise AssertionError(f"作业 {job_id} 没有进终态")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


# ══════════════════════════════════════════════════ 横切:信封 / trace_id / WS 关闭码
def test_unknown_route_and_method_use_error_envelope(rig):
    """00 §10:未知路由 404 `NOT_FOUND`、方法不对 405 `METHOD_NOT_ALLOWED`,**不得漏出 FastAPI 的 `{detail}`**。"""
    r = rig.client.get(f"{P}/no-such-endpoint", headers=H())
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False and body["code"] == "NOT_FOUND" and body["error"]["reason"] == "unknown_route"
    assert "detail" not in body and isinstance(body["trace_id"], str) and body["trace_id"]
    r = rig.client.delete(f"{P}/system/version", headers=H())
    assert r.status_code == 405 and r.json()["code"] == "METHOD_NOT_ALLOWED"
    assert r.json()["error"]["reason"] == "method_not_allowed"


def test_validation_error_is_envelope_with_pointer(rig):
    """422 校验错误同样包信封:``INVALID_ARGS`` + ``details[].pointer``。"""
    r = rig.client.get(f"{P}/messages", headers=H(), params={"limit": 99999})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "INVALID_ARGS" and body["error"]["reason"] == "validation_error"
    assert any(d["pointer"] == "/limit" for d in body["error"]["details"])


def test_success_response_carries_trace_id(rig):
    """成功响应带 ``trace_id``(总控裁决);给了 ``X-Trace-Id`` 就采纳它,并与审计行同值。"""
    r = rig.client.get(f"{P}/capabilities", headers=H(**{"X-Trace-Id": "tr-console-1"}))
    assert r.status_code == 200 and r.json()["trace_id"] == "tr-console-1"
    rows = [a for a in rig.store.list_audit() if a["action"].endswith("/capabilities")]
    assert rows and rows[-1]["trace_id"] == "tr-console-1"
    # #72 的键集被 02 逐字定死 ⇒ 不塞 trace_id
    assert "trace_id" not in rig.client.get(f"{P}/system/health", headers=H()).json()


def test_ws_bad_token_closes_with_4401_after_accept(rig):
    """E-05:令牌无效时先 ``accept()`` 再 ``close(4401)``,客户端必须**看得到 4401**(此前只见 1006)。"""
    with rig.client.websocket_connect(f"{P}/events?token=nope") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4401
    with rig.client.websocket_connect(f"{P}/events") as ws:            # 完全不带令牌同理
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4401


def test_ws_first_frame_illegal_closes_4400(rig):
    """4400 那条路径原本就在 accept 之后,修 4401 不能把它带坏。"""
    with rig.client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"nope": 1})
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4400


# ══════════════════════════════════════════════════ #24 机型档案库
def test_device_profile_templates(rig):
    r = rig.client.get(f"{P}/device-profiles/templates", headers=H(TOK_R))
    assert r.status_code == 200
    rows = r.json()["data"]
    assert rows and all(set(x) == {"profile_key", "brand", "model", "release", "weight"} for x in rows)
    assert len({x["profile_key"] for x in rows}) == len(rows)
    assert rig.client.get(f"{P}/device-profiles/templates").status_code == 401


# ══════════════════════════════════════════════════ #74 环境快照
def test_system_env_merges_windows_and_wsl(rig):
    r = rig.client.get(f"{P}/system/env", headers=H(TOK_R))
    assert r.status_code == 200
    body = r.json()
    assert body["windows"]["mtu"] == 1280 and body["windows_error"] is None
    assert body["wslconfig"]["effective"]["memory"] == "11GB"
    assert set(body["wsl"]) >= {"mtu", "resolv_conf", "docker", "ksm", "zram", "clock", "adb_server"}
    assert body["winagent"]["online"] is True
    assert rig.client.get(f"{P}/system/env").status_code == 401


def test_system_env_reports_winagent_gap_instead_of_faking(rig):
    """WinAgent 不可达 ⇒ ``windows`` 为 null 且 ``windows_error`` 写明原因,**不假装拿到机器级信息**。"""
    rig.wa.offline = True
    body = rig.client.get(f"{P}/system/env", headers=H(TOK_R)).json()
    assert body["windows"] is None and body["windows_error"] == "winagent_offline"


# ══════════════════════════════════════════════════ #75 探测
def test_probe_sample_returns_rows_and_skipped(rig):
    rig.wa.sample_rows = [{"remote_host": "msfxg.3g.qq.com", "remote_port": 8080, "proto": "tcp", "hits": 3}]
    r = rig.client.post(f"{P}/system/probe", headers=H(TOK_W), json={"mode": "sample", "duration_s": 5})
    assert r.status_code == 200
    body = r.json()
    assert "run_id" not in body and body["rows"] and body["rows"][0]["side"] == "windows"
    assert {s["channel"] for s in body["skipped"]} == {"qidian", "qq", "wechat"}      # 没有 running 账号


def test_probe_full_marks_wsl_side_skipped_without_probe(rig):
    """``mode:'full'`` 的 WSL 侧缺省**不出网**:该侧记 SKIPPED(agent_probe_disabled),Windows 侧照常转发。"""
    r = rig.client.post(f"{P}/system/probe", headers=H(TOK_W),
                        json={"mode": "full", "targets": ["msfxg.3g.qq.com:8080"], "trigger": "manual"})
    assert r.status_code == 200
    rows = r.json()["results"]
    assert any(x["side"] == "windows" and x["status"] == "OK" for x in rows)
    wsl = [x for x in rows if x["side"] == "wsl"]
    assert wsl and wsl[0]["status"] == "SKIPPED" and wsl[0]["detail"] == "agent_probe_disabled"


def test_probe_full_uses_injected_level_probe(rig):
    """注入四级探测器后 WSL 侧真的跑,并把结论经 ``PUT /wa/v1/probes`` 回写。"""
    class FakeLevelProbe:
        def __init__(self):
            self.seen: list[str] = []

        async def probe(self, target: str, *, timeout_s: float = 5.0) -> dict[str, Any]:
            self.seen.append(target)
            return {"status": "OK", "level_reached": "tls", "rtt_ms": 12}

    rig.agent.net_probe = FakeLevelProbe()
    r = rig.client.post(f"{P}/system/probe", headers=H(TOK_W), json={"targets": ["a.example:443"]})
    rows = [x for x in r.json()["results"] if x["side"] == "wsl"]
    assert rows[0]["level_reached"] == "tls" and rig.agent.net_probe.seen == ["a.example:443"]


def test_probe_rejects_bad_args_and_read_token(rig):
    assert rig.client.post(f"{P}/system/probe", headers=H(TOK_R), json={}).status_code == 403
    r = rig.client.post(f"{P}/system/probe", headers=H(TOK_W), json={"mode": "nope"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_mode"
    r = rig.client.post(f"{P}/system/probe", headers=H(TOK_W), json={"mode": "sample", "duration_s": 999})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_duration"
    r = rig.client.post(f"{P}/system/probe", headers=H(TOK_W), json={"targets": [1]})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_targets"


# ══════════════════════════════════════════════════ #78 自检
def test_selftest_run_writes_result_and_last(rig):
    r = rig.client.post(f"{P}/system/selftest", headers=H())
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    for _ in range(50):
        if rig.store.settings_get("system.selftest.last") == run_id:
            break
        rig.client.get(f"{P}/system/version", headers=H())
    got = rig.client.get(f"{P}/system/selftest", headers=H(TOK_R)).json()
    assert got["run_id"] == run_id and got["data"]["winagent_ok"] is True
    # 本期没有临时 redroid 执行体 ⇒ 如实 null + skipped,不假装启过容器
    assert got["data"]["redroid_boot_ms"] is None
    assert {s["step"] for s in got["data"]["skipped"]} == {"redroid_boot", "napcat"}
    assert rig.client.post(f"{P}/system/selftest", headers=H(TOK_W)).status_code == 403


# ══════════════════════════════════════════════════ #80 诊断包
def test_diagnostics_job_produces_zip_without_excluded_items(rig):
    r = rig.client.post(f"{P}/system/diagnostics", headers=H(), json={})
    assert r.status_code == 202
    row = rig.wait_job(r.json()["job_id"])
    assert row["state"] == "succeeded", row
    res = row["result"]
    assert set(res["excluded"]) >= {"vault", "message_text", "screenshots"}
    with zipfile.ZipFile(res["file_path"]) as z:
        names = z.namelist()
        assert {"env.json", "health.json", "alerts.json", "schema_version.json"} <= set(names)
        assert not any("vault" in n.lower() or "wechat" in n.lower() for n in names)
    assert row["created_at"].endswith("+08:00") and "created_ms" not in row


def test_diagnostics_rejects_screenshots_and_non_admin(rig):
    r = rig.client.post(f"{P}/system/diagnostics", headers=H(), json={"with_screenshots": True})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "screenshots_not_implemented"
    assert rig.client.post(f"{P}/system/diagnostics", headers=H(TOK_W), json={}).status_code == 403


# ══════════════════════════════════════════════════ #84 WSL 重启(危险操作)
def test_wsl_restart_requires_confirm_and_goes_through_winagent(rig):
    r = rig.client.post(f"{P}/system/wsl-restart", headers=H(), json={"mode": "shutdown"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "confirm_required"
    assert rig.wa.wsl_restarts == []                                    # 没确认就一条请求都不许发出去
    audit = [a for a in rig.store.list_audit() if a["action"] == "system.wsl_restart"]
    assert audit and audit[-1]["result_code"] == "INVALID_ARGS"         # 拒绝也留痕
    r = rig.client.post(f"{P}/system/wsl-restart", headers=H(), json={"mode": "shutdown", "confirm": True})
    assert r.status_code == 202 and r.json()["mode"] == "shutdown"
    run_id = r.json()["run_id"]
    for _ in range(50):
        if rig.wa.wsl_restarts:
            break
        rig.client.get(f"{P}/system/version", headers=H())
    assert rig.wa.wsl_restarts and rig.wa.wsl_restarts[-1] == {
        "mode": "shutdown", "run_id": run_id, "confirm": True,
    }


@pytest.mark.parametrize("confirm", [False, None, 1, "true"])
def test_wsl_restart_shutdown_rejects_non_true_before_drain(rig, monkeypatch, confirm):
    """仅 JSON 布尔 true 有效；拒绝时既不排空账号，也不转发假 WinAgent。"""
    from unittest.mock import AsyncMock

    rig.store.ensure_account("qd01", "qidian", state="running")
    stop = AsyncMock()
    monkeypatch.setattr(rig.agent.accounts, "stop", stop)
    response = rig.client.post(f"{P}/system/wsl-restart", headers=H(),
                               json={"mode": "shutdown", "confirm": confirm})
    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "confirm_required"
    rig.client.get(f"{P}/system/version", headers=H())
    stop.assert_not_awaited()
    assert rig.wa.wsl_restarts == []


def test_wsl_restart_terminate_forwards_false_confirmation(rig):
    """terminate 保持原契约可无确认，但不能凭空升级成已确认 shutdown。"""
    response = rig.client.post(f"{P}/system/wsl-restart", headers=H(), json={"mode": "terminate"})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    for _ in range(50):
        if rig.wa.wsl_restarts:
            break
        rig.client.get(f"{P}/system/version", headers=H())
    assert rig.wa.wsl_restarts == [{"mode": "terminate", "run_id": run_id, "confirm": False}]


def test_wsl_restart_rejects_bad_mode_and_non_admin(rig):
    r = rig.client.post(f"{P}/system/wsl-restart", headers=H(), json={"mode": "reboot", "confirm": True})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_mode"
    assert rig.client.post(f"{P}/system/wsl-restart", headers=H(TOK_W),
                           json={"mode": "shutdown", "confirm": True}).status_code == 403
    assert rig.wa.wsl_restarts == []


# ══════════════════════════════════════════════════ #85 docker 代理
def test_docker_proxy_reports_missing_applier(rig):
    """没有执行体时**诚实回 503**,不假装写过 proxy.conf。"""
    r = rig.client.post(f"{P}/system/docker-proxy", headers=H(), json={"enable": True})
    assert r.status_code == 503 and r.json()["error"]["reason"] == "docker_proxy_applier_missing"


def test_docker_proxy_applies_and_reads_back(rig):
    class FakeApplier:
        def __init__(self):
            self.applied: list[Any] = []

        async def apply(self, proxy):
            self.applied.append(proxy)
            return True

        async def disable(self):
            self.applied.append(None)
            return True

    rig.agent.docker_proxy = FakeApplier()
    r = rig.client.post(f"{P}/system/docker-proxy", headers=H(), json={"enable": True})
    assert r.status_code == 200 and r.json()["applied"] is True
    assert rig.agent.docker_proxy.applied == [{"http": "http://127.0.0.1:7890"}]
    got = rig.client.get(f"{P}/system/docker-proxy", headers=H(TOK_R)).json()
    assert got["enabled"] is True and got["proxy"] == {"http": "http://127.0.0.1:7890"}


def test_docker_proxy_pends_when_accounts_running(rig):
    """重启 docker 会连带重启容器 ⇒ 有 running 账号且没 confirm 时回 202 pending。"""
    rig.store.ensure_account("qd01", "qidian", state="running")
    r = rig.client.post(f"{P}/system/docker-proxy", headers=H(), json={"enable": True})
    assert r.status_code == 202 and r.json() == {"ok": True, "pending": True, "reason": "accounts_running",
                                                 "running": ["qd01"], "trace_id": r.json()["trace_id"]}


def test_docker_proxy_rejects_bad_args_and_non_admin(rig):
    r = rig.client.post(f"{P}/system/docker-proxy", headers=H(), json={"enable": "yes"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_enable"
    assert rig.client.post(f"{P}/system/docker-proxy", headers=H(TOK_W), json={"enable": True}).status_code == 403


# ══════════════════════════════════════════════════ #86 / #87 合规告知
def test_notice_read_write_and_ack(rig):
    got = rig.client.get(f"{P}/system/notice", headers=H(TOK_R)).json()
    assert got["notice_version"] == DEFAULT_NOTICE_VERSION and got["text"] and got["ack_ms"] is None
    r = rig.client.post(f"{P}/system/notice/ack", headers=H(), json={"notice_version": DEFAULT_NOTICE_VERSION})
    assert r.status_code == 200 and isinstance(r.json()["ack_ms"], int)
    assert rig.client.get(f"{P}/system/notice", headers=H(TOK_R)).json()["acked_version"] == DEFAULT_NOTICE_VERSION
    # 文案升版后旧版本号的勾选一律拒:必须重新阅读
    assert rig.client.put(f"{P}/system/notice", headers=H(), json={"notice_version": "2", "text": "新文案"}).status_code == 200
    r = rig.client.post(f"{P}/system/notice/ack", headers=H(), json={"notice_version": DEFAULT_NOTICE_VERSION})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "notice_version_stale"
    assert rig.client.get(f"{P}/system/notice", headers=H(TOK_R)).json()["text"] == "新文案"


def test_notice_loopback_is_unauthenticated_but_ack_needs_admin(rig):
    """#86 对 loopback 免鉴权(首启还没令牌时也要能显示);#87 仍是 A 级。"""
    assert rig.client.get(f"{P}/system/notice").status_code == 200
    assert rig.client.post(f"{P}/system/notice/ack", json={"notice_version": "1"}).status_code == 401
    assert rig.client.post(f"{P}/system/notice/ack", headers=H(TOK_W), json={"notice_version": "1"}).status_code == 403
    r = rig.client.put(f"{P}/system/notice", headers=H(), json={"notice_version": ""})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_notice_version"


# ══════════════════════════════════════════════════ #90~#93 调用方令牌
def test_api_clients_crud_and_one_shot_token(rig):
    r = rig.client.get(f"{P}/settings/api-clients", headers=H())
    assert r.status_code == 200 and {x["app_id"] for x in r.json()["data"]} == {"console", "writer", "reader"}
    assert all("secret_hash" not in x and "token" not in x for x in r.json()["data"])
    r = rig.client.post(f"{P}/settings/api-clients", headers=H(),
                        json={"name": "报价机器人", "level": "write", "allow_accounts": ["qd01"]})
    assert r.status_code == 201
    created = r.json()
    app_id, token = created["app_id"], created["token"]
    # R6-55 单一形状:行字段与一次性明文令牌**同在顶层**(此前行包 `data`、令牌在顶层,客户端取 data 会丢令牌)
    assert "data" not in created and created["level"] == "write" and created["allow_accounts"] == ["qd01"]
    # 新令牌立刻可用;再列一次**读不回明文**
    assert rig.client.get(f"{P}/accounts", headers=H(token)).status_code == 200
    row = next(x for x in rig.client.get(f"{P}/settings/api-clients", headers=H()).json()["data"] if x["app_id"] == app_id)
    assert "token" not in row and row["created_at"].endswith("+08:00")
    # 吊销后立刻失效,且默认不再出现在列表里
    assert rig.client.delete(f"{P}/settings/api-clients/{app_id}", headers=H()).status_code == 200
    assert rig.client.get(f"{P}/accounts", headers=H(token)).status_code == 401
    ids = {x["app_id"] for x in rig.client.get(f"{P}/settings/api-clients", headers=H()).json()["data"]}
    assert app_id not in ids
    assert app_id in {x["app_id"] for x in rig.client.get(f"{P}/settings/api-clients", headers=H(),
                                                          params={"include_revoked": True}).json()["data"]}


def test_api_client_rotate_keeps_old_token_within_grace(rig):
    """#92:轮换后旧令牌在 ``grace_minutes`` 内仍可用,过期即 401。"""
    created = rig.client.post(f"{P}/settings/api-clients", headers=H(),
                              json={"name": "轮换测试", "level": "read"}).json()
    app_id, old = created["app_id"], created["token"]
    r = rig.client.post(f"{P}/settings/api-clients/{app_id}/rotate", headers=H(), json={"grace_minutes": 10})
    assert r.status_code == 200
    new = r.json()["token"]
    assert new != old and r.json()["grace_until"].endswith("+08:00")
    assert rig.client.get(f"{P}/accounts", headers=H(new)).status_code == 200
    assert rig.client.get(f"{P}/accounts", headers=H(old)).status_code == 200      # 宽限期内
    rig.clock.advance(11 * 60_000)
    assert rig.client.get(f"{P}/accounts", headers=H(old)).status_code == 401      # 过期
    assert rig.client.get(f"{P}/accounts", headers=H(new)).status_code == 200


def test_api_clients_reject_bad_args_and_builtin_and_non_admin(rig):
    assert rig.client.get(f"{P}/settings/api-clients", headers=H(TOK_W)).status_code == 403
    r = rig.client.post(f"{P}/settings/api-clients", headers=H(), json={"name": "x", "level": "root"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_level"
    r = rig.client.post(f"{P}/settings/api-clients", headers=H(), json={"level": "read"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_name"
    r = rig.client.delete(f"{P}/settings/api-clients/console", headers=H())
    assert r.status_code == 409 and r.json()["error"]["reason"] == "builtin_readonly"
    assert rig.client.delete(f"{P}/settings/api-clients/zzz", headers=H()).status_code == 404
    r = rig.client.post(f"{P}/settings/api-clients/reader/rotate", headers=H(), json={"grace_minutes": -1})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_grace_minutes"


def test_api_clients_route_not_eaten_by_settings_group(rig):
    """`/settings/api-clients` 必须排在 `{group}` 兜底之前,否则会被当成「未知配置组」。"""
    assert rig.client.get(f"{P}/settings/api-clients", headers=H()).json()["data"]
    r = rig.client.get(f"{P}/settings/nosuchgroup", headers=H())
    assert r.status_code == 404 and r.json()["code"] == "TARGET_NOT_FOUND"


# ══════════════════════════════════════════════════ #36 群发
def test_broadcast_runs_per_account_and_rejects_wildcard(rig):
    rig.store.ensure_account("qd01", "qidian", state="running")
    rig.store.ensure_account("qd02", "qidian", state="running")
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W),
                        json={"account_ids": ["qd01", "qd02"], "op": "get_state", "args": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["broadcast_id"] and set(body["results"]) == {"qd01", "qd02"}
    assert all("code" in v and "cost_ms" in v for v in body["results"].values())
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W), json={"account_ids": ["*"], "op": "get_state"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "wildcard_not_allowed"


def test_broadcast_rejects_bad_args_and_levels(rig):
    rig.store.ensure_account("qd01", "qidian", state="running")
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W), json={"account_ids": [], "op": "get_state"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_account_ids"
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W),
                        json={"account_ids": ["qd01", "qd01"], "op": "get_state"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "duplicate_account_ids"
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W), json={"account_ids": ["qd01"], "op": "nope"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "unknown_op"
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W), json={"account_ids": ["qd01"], "op": "send_text",
                                                                           "args": {"session": "s", "text": "t"}})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "idempotency_key_required"
    # 写类 op 用只读令牌 ⇒ 403;账号不存在 ⇒ 404
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_R),
                        json={"account_ids": ["qd01"], "op": "send_text", "idempotency_key": "k",
                              "args": {"session": "s", "text": "t"}})
    assert r.status_code == 403
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W), json={"account_ids": ["zz99"], "op": "get_state"})
    assert r.status_code == 404


# ══════════════════════════════════════════════════ #51 消息导出
def test_messages_export_job(rig):
    r = rig.client.post(f"{P}/messages/export", headers=H(TOK_R), json={"fmt": "jsonl", "filter": {}})
    assert r.status_code == 202
    row = rig.wait_job(r.json()["job_id"])
    assert row["state"] == "succeeded" and row["result"]["file_path"].endswith(".jsonl")
    assert row["kind"] == "messages_export" and row["updated_at"].endswith("+08:00")
    r = rig.client.post(f"{P}/messages/export", headers=H(TOK_R), json={"fmt": "csv"})
    assert rig.wait_job(r.json()["job_id"])["result"]["file_path"].endswith(".csv")


def test_messages_export_rejects_unimplemented_and_bad_args(rig):
    """eml / with_media=zip 本期没有执行体 ⇒ 明着 400,不静默降级成别的格式。"""
    r = rig.client.post(f"{P}/messages/export", headers=H(TOK_R), json={"fmt": "eml"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "fmt_not_implemented"
    r = rig.client.post(f"{P}/messages/export", headers=H(TOK_R), json={"fmt": "jsonl", "with_media": "zip"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "with_media_not_implemented"
    r = rig.client.post(f"{P}/messages/export", headers=H(TOK_R), json={"fmt": "pdf"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_fmt"
    assert rig.client.post(f"{P}/messages/export", json={}).status_code == 401


# ══════════════════════════════════════════════════ #107 / #77 形状
def test_job_view_uses_iso_at_keys(rig):
    """S-05:#107 出参的时间键是 ``*_at``(ISO 8601),库列仍 ``*_ms``。"""
    job_id = rig.store.job_create(kind="diagnostics", actor="token:console", params={})
    row = rig.job(job_id)
    assert set(row) >= {"job_id", "kind", "state", "progress", "created_at", "updated_at", "expires_at"}
    assert not any(k.endswith("_ms") for k in row)
    assert row["created_at"].endswith("+08:00")
    assert rig.store.job_get(job_id)["created_ms"] > 0          # 库列没被动


def test_metrics_has_hardware_and_ours_groups(rig):
    """S-07:#77 快照分 ``hardware`` / ``ours`` 两组并排;没有采集方的项一律 null,不编造。"""
    assert rig.agent.deployment_disks is None
    m = rig.client.get(f"{P}/system/metrics", headers=H(TOK_R)).json()
    assert set(m["hardware"]) == {"mem", "cpu", "disks", "disk_source_error"}
    assert m["hardware"]["disks"] == []
    assert m["hardware"]["disk_source_error"]
    assert m["disk_watermark"]["level"] == "unknown"
    assert m["disk_watermark"]["free_mb"] is None
    assert set(m["hardware"]["mem"]) == {"total_mb", "used_mb", "avail_mb", "vmmem_mb"}
    assert m["hardware"]["mem"]["vmmem_mb"] is None and m["hardware"]["cpu"]["logical_cores"]
    assert set(m["ours"]) == {"procs", "procs_detail", "accounts", "wechat", "storage"}
    assert m["ours"]["wechat"] == {"chatlog_mb": None, "wechat_pc_mb": None}
    assert set(m["ours"]["storage"]) == {"db_mb", "media_mb", "mail_mb", "accounts_mb", "backup_mb", "vhdx_mb"}
    assert "vhdx_grown_mb" in m["disk_watermark"]


def test_session_view_and_precheck_shapes(rig):
    """S-10/S-11:Session 出参的最后消息时间是 ``last_msg_at``(ISO);``precheck.can_add`` 是 bool。"""
    rig.store.ensure_account("qd01", "qidian", state="running")
    rows = rig.client.get(f"{P}/sessions", headers=H(TOK_R)).json()["data"]
    assert all("last_ts" not in s and "last_msg_at" in s for s in rows)
    r = rig.client.post(f"{P}/resources/precheck", headers=H(TOK_R), json={"channel": "qidian"})
    assert r.status_code == 200 and isinstance(r.json()["can_add"], bool)


def test_account_view_has_no_settings_subobject(rig):
    """S-12(总控裁决④):Account 出参**不带** ``settings`` 子对象,账号级设置走 #22。"""
    rig.store.ensure_account("qd01", "qidian", state="running")
    a = rig.client.get(f"{P}/accounts/qd01", headers=H()).json()["data"]
    assert "settings" not in a
