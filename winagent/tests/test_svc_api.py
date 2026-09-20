"""``/wa/v1`` 端点 × 令牌 × 执行体矩阵(02 §3.6 #1~#47 + #33b/#33c)、错误信封(00 §10)、版本头(§3.8)。"""
from __future__ import annotations

import pytest

from qtrade_winagent import API_VERSION, __version__
from qtrade_winagent.errors import USER_AGENT_OFFLINE_MESSAGE

from tests.conftest import AGENT_TOKEN, CONSOLE_TOKEN, INSTALLER_TOKEN, attach_user_agent, build_rig, client


# ---------------------------------------------------------------- 横切
async def test_every_response_carries_x_wa_version(rig):
    async with client(rig) as c:
        for path in ("/wa/v1/ping", "/wa/v1/health", "/wa/v1/time"):
            r = await c.get(path)
            assert r.headers["X-WA-Version"] == __version__


async def test_ping_is_unauthenticated_and_returns_only_那四个字段(rig):
    async with client(rig, token=None) as c:
        r = await c.get("/wa/v1/ping")
    assert r.status_code == 200 and set(r.json()) == {"agent_id", "version", "time", "listen"}


async def test_bad_token_is_401_with_envelope(rig):
    async with client(rig, token="wrong") as c:
        r = await c.get("/wa/v1/health")
    b = r.json()
    assert r.status_code == 401 and b["ok"] is False and b["code"] == "UNAUTHORIZED"
    assert set(b["error"]) >= {"message", "reason", "retryable", "needs_human"} and b["trace_id"]


@pytest.mark.parametrize("method,path,allowed,denied", [
    ("GET", "/wa/v1/health", (AGENT_TOKEN, CONSOLE_TOKEN), (INSTALLER_TOKEN,)),
    ("GET", "/wa/v1/time", (AGENT_TOKEN,), (CONSOLE_TOKEN, INSTALLER_TOKEN)),
    ("GET", "/wa/v1/metrics", (AGENT_TOKEN,), (CONSOLE_TOKEN,)),
    ("GET", "/wa/v1/alerts", (AGENT_TOKEN,), (CONSOLE_TOKEN,)),
    ("GET", "/wa/v1/vault", (AGENT_TOKEN, CONSOLE_TOKEN), (INSTALLER_TOKEN,)),
    ("GET", "/wa/v1/net", (AGENT_TOKEN, CONSOLE_TOKEN, INSTALLER_TOKEN), ()),
    ("GET", "/wa/v1/audit", (CONSOLE_TOKEN,), (AGENT_TOKEN, INSTALLER_TOKEN)),
    ("GET", "/wa/v1/wechat/profiles", (AGENT_TOKEN,), (CONSOLE_TOKEN,)),
    ("GET", "/wa/v1/settings/wechat", (CONSOLE_TOKEN,), (AGENT_TOKEN,)),
])
async def test_token_matrix_matches_02_3_6(rig, method, path, allowed, denied):
    for tok in allowed:
        async with client(rig, token=tok) as c:
            assert (await c.request(method, path)).status_code != 403
    for tok in denied:
        async with client(rig, token=tok) as c:
            r = await c.request(method, path)
            assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


async def test_every_call_is_audited(rig):
    async with client(rig) as c:
        await c.get("/wa/v1/health")
    rows = rig.db.query("SELECT * FROM wa_audit_log WHERE action='health'")
    assert rows and rows[0]["actor"] == "agent" and rows[0]["ip"] == "127.0.0.1"


# ---------------------------------------------------------------- #2 health / #3 version / #4 time
async def test_health_shape_and_user_agent_flag(rig):
    async with client(rig) as c:
        b = (await c.get("/wa/v1/health")).json()
    assert b["api_version"] == API_VERSION and b["user_agent"] is False
    assert set(b["host"]) == {"total_mb", "available_mb", "wsl_vm_mb", "wechat_mb", "chatlog_mb"}
    assert b["modules"]["wslctl"] == "offline" and b["modules"]["wechat"] == "offline"
    assert set(b["modules"]) == {"vault", "monitor", "netprobe", "power", "wslctl", "wechat"}


async def test_health_user_agent_true_after_handshake(rig_with_user):
    async with client(rig_with_user) as c:
        b = (await c.get("/wa/v1/health")).json()
    assert b["user_agent"] is True and b["user_session"]["user"] == "Console"
    assert b["modules"]["wechat"] == "enabled"


async def test_wechat_module_disabled_reports_disabled(tmp_path):
    r = build_rig(tmp_path, wechat_enabled=False)
    async with client(r) as c:
        assert (await c.get("/wa/v1/health")).json()["modules"]["wechat"] == "disabled"
    r.db.close()


async def test_time_has_last_resume_ms_for_agent_polling(rig):
    rig.sys.resume_ms = 1_700_000_000_000
    async with client(rig) as c:
        b = (await c.get("/wa/v1/time")).json()
    assert set(b) == {"now_ms", "tz_offset_min", "last_resume_ms", "w32time"}   # R3-5:没它主机唤醒事件就丢了
    assert b["last_resume_ms"] == 1_700_000_000_000 and b["tz_offset_min"] == 480


async def test_version_reports_null_wsl_when_user_agent_offline(rig):
    async with client(rig) as c:
        b = (await c.get("/wa/v1/version")).json()
    assert b["wsl_version"] is None and b["user_agent_version"] is None
    assert b["svc_version"] == __version__


# ---------------------------------------------------------------- #7~#12 vault
async def test_vault_crud_over_http(rig):
    async with client(rig) as c:
        assert (await c.put("/wa/v1/vault/account/qd01", json={"value": "s3cret", "scope": "account"})).status_code == 204
        assert (await c.request("HEAD", "/wa/v1/vault/account/qd01")).status_code == 200
        r = await c.post("/wa/v1/vault/account/qd01/read", headers={"X-Trace-Id": "T1"})
        assert r.status_code == 200 and r.json() == {"value": "s3cret"}
        lst = (await c.get("/wa/v1/vault?scope=account")).json()["items"]
        assert lst[0]["name"] == "account/qd01" and "value" not in lst[0]
        assert (await c.post("/wa/v1/vault/account/qd01/flag", json={"suspect": True})).status_code == 204
        assert (await c.delete("/wa/v1/vault/account/qd01")).status_code == 204
        assert (await c.request("HEAD", "/wa/v1/vault/account/qd01")).status_code == 404


async def test_vault_read_requires_trace_id(rig):
    async with client(rig) as c:
        await c.put("/wa/v1/vault/api/x", json={"value": "v", "scope": "api"})
        r = await c.post("/wa/v1/vault/api/x/read")
    assert r.status_code == 400 and r.json()["error"]["reason"] == "missing_trace_id"


async def test_vault_read_is_agent_only_console_gets_403(rig):
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/vault/api/x/read", headers={"X-Trace-Id": "T"})
    assert r.status_code == 403


async def test_vault_read_rejects_non_local_source(rig):
    """#11:只接受 loopback / WSL 子网来源。"""
    async with client(rig, host="8.8.8.8") as c:
        await c.put("/wa/v1/vault/api/x", json={"value": "v", "scope": "api"})
        r = await c.post("/wa/v1/vault/api/x/read", headers={"X-Trace-Id": "T"})
    assert r.status_code == 403 and r.json()["error"]["reason"] == "source_not_allowed"


async def test_vault_read_allows_wsl_subnet_source(rig):
    await rig.deps.netprobe.refresh()                    # wsl_subnet = 172.23.16.0/20
    async with client(rig) as c:
        await c.put("/wa/v1/vault/api/x", json={"value": "v", "scope": "api"})
    async with client(rig, host="172.23.16.7") as c:
        r = await c.post("/wa/v1/vault/api/x/read", headers={"X-Trace-Id": "T"})
    assert r.status_code == 200


async def test_vault_put_installer_token_allowed_delete_not(rig):
    async with client(rig, token=INSTALLER_TOKEN) as c:
        assert (await c.put("/wa/v1/vault/winagent/agent_token", json={"value": "t", "scope": "winagent"})).status_code == 204
        assert (await c.delete("/wa/v1/vault/winagent/agent_token")).status_code == 403


async def test_vault_nested_name_with_slashes(rig):
    """``mail/hmac/cmd/<短名>``(R6-10:``cmd/`` 这一段不得省)—— 路径参数必须吃得下多级名字。"""
    async with client(rig) as c:
        assert (await c.put("/wa/v1/vault/mail/hmac/cmd/ops", json={"value": "k", "scope": "mail"})).status_code == 204
        r = await c.post("/wa/v1/vault/mail/hmac/cmd/ops/read", headers={"X-Trace-Id": "T"})
    assert r.json()["value"] == "k"
    assert rig.db.one("SELECT name FROM vault_index")["name"] == "mail/hmac/cmd/ops"


async def test_vault_read_missing_is_404(rig):
    async with client(rig) as c:
        r = await c.post("/wa/v1/vault/account/none/read", headers={"X-Trace-Id": "T"})
    assert r.status_code == 404


# ---------------------------------------------------------------- #13~#17 / #45 网络与防火墙
async def test_net_snapshot_shape(rig):
    await rig.deps.netprobe.refresh()
    async with client(rig) as c:
        b = (await c.get("/wa/v1/net")).json()
    assert set(b) == {"seq", "net_state", "proxy", "vpn_adapters", "wsl_subnet", "host_ip", "agent_base_url", "mtu"}
    assert b["wsl_subnet"] == "172.23.16.0/20" and b["agent_base_url"] == "http://172.23.16.1:17600"


async def test_probe_returns_202_and_probes_readback(rig):
    async with client(rig, token=INSTALLER_TOKEN) as c:
        r = await c.post("/wa/v1/probe", json={"trigger": "install", "targets": [
            {"target": "wechat_servers", "host": "long.weixin.qq.com", "port": 443, "side": "windows", "tls": True}]})
        assert r.status_code == 202
        rid = r.json()["run_id"]
        got = (await c.get(f"/wa/v1/probes?run_id={rid}")).json()["results"]
    assert got[0]["target"] == "wechat_servers" and got[0]["result"] == "OK"


async def test_put_probes_is_agent_only(rig):
    body = {"run_id": "r1", "trigger": "boot", "results": [
        {"target": "apk_url", "side": "wsl", "host": "apk.corp", "port": 443, "level_reached": "http", "result": "OK"}]}
    async with client(rig, token=CONSOLE_TOKEN) as c:
        assert (await c.put("/wa/v1/probes", json=body)).status_code == 403
    async with client(rig) as c:
        assert (await c.put("/wa/v1/probes", json=body)).json() == {"written": 1}


async def test_probe_sample_mode(rig):
    rig.probe.conns = [{"ip": "203.205.1.1", "port": 443, "hostname": "long.weixin.qq.com", "channel": "wechat"}]
    async with client(rig, token=CONSOLE_TOKEN) as c:
        b = (await c.post("/wa/v1/probe", json={"mode": "sample", "pid_names": ["Weixin.exe"], "duration_s": 3})).json()
    assert b["rows"][0]["hostname"] == "long.weixin.qq.com" and b["duration_s"] == 3   # 04 §3.4 的行形状
    assert rig.db.query("SELECT * FROM probe_targets_observed") == []                  # 不落库,由 Agent 回写


async def test_firewall_ensure_and_delete_pair(rig):
    await rig.deps.netprobe.refresh()
    async with client(rig, token=CONSOLE_TOKEN) as c:
        a = (await c.post("/wa/v1/firewall/ensure", json={})).json()
        assert a["result"] == "created" and a["rule_name"] == "QTrade-WinAgent-17610-from-WSL"
        assert (await c.post("/wa/v1/firewall/ensure", json={})).json()["result"] == "unchanged"
        assert (await c.request("DELETE", "/wa/v1/firewall")).json()["result"] == "removed"
    async with client(rig) as c:                                    # Agent 不管防火墙
        assert (await c.post("/wa/v1/firewall/ensure", json={})).status_code == 403


# ---------------------------------------------------------------- user 执行体离线 ⇒ 503
@pytest.mark.parametrize("method,path,token,body", [
    ("GET", "/wa/v1/wsl/status", AGENT_TOKEN, None),
    ("POST", "/wa/v1/wsl/start", CONSOLE_TOKEN, {}),
    ("POST", "/wa/v1/wsl/stop", CONSOLE_TOKEN, {}),
    ("GET", "/wa/v1/wsl/config", CONSOLE_TOKEN, None),
    ("GET", "/wa/v1/wechat/status", AGENT_TOKEN, None),
    ("POST", "/wa/v1/wechat/login/start", AGENT_TOKEN, {}),
    ("GET", "/wa/v1/wechat/read", AGENT_TOKEN, None),
    ("POST", "/wa/v1/wechat/send", AGENT_TOKEN, {"session_name": "x", "text": "y"}),
])
async def test_user_endpoints_are_503_when_user_agent_offline(rig, method, path, token, body):
    async with client(rig, token=token) as c:
        r = await c.request(method, path, json=body)
    assert r.status_code == 503 and r.json()["code"] == "NOT_READY"
    assert r.json()["error"]["message"] == USER_AGENT_OFFLINE_MESSAGE


# ---------------------------------------------------------------- user 执行体在线
async def test_wsl_status_via_pipe(rig_with_user):
    async with client(rig_with_user) as c:
        b = (await c.get("/wa/v1/wsl/status")).json()
    assert b["distro"]["name"] == "qtrade" and "wslconfig" in b


async def test_wsl_stop_only_terminates(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        assert (await c.post("/wa/v1/wsl/stop", json={})).json() == {"terminated": "qtrade"}
    assert rig_with_user.wsl.shutdown_calls == 0


async def test_wsl_restart_shutdown_requires_confirm(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wsl/restart", json={"mode": "shutdown"})
        assert r.status_code == 400 and r.json()["error"]["reason"] == "shutdown_not_confirmed"
        assert rig_with_user.wsl.shutdown_calls == 0
        r2 = await c.post("/wa/v1/wsl/restart", json={"mode": "shutdown", "confirm": True})
        assert r2.status_code == 202
    assert rig_with_user.wsl.shutdown_calls == 1


async def test_wsl_config_put_whitelist_and_pending_restart(rig_with_user):
    from qtrade_winagent import alerts as A
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        b = (await c.put("/wa/v1/wsl/config", json={"memory": "11GB", "kernel": "C:\\x"})).json()
    assert b["pending_restart"] is True and "memory" in b["changed"] and "kernel" not in b["changed"]
    assert rig_with_user.deps.alerts.is_firing(A.WSLCONFIG_PENDING_RESTART, "wsl")
    assert rig_with_user.wsl.shutdown_calls == 0                       # 🔴 不自动 shutdown


async def test_kernel_apply_requires_confirm_shutdown_and_reports_stage(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wsl/kernel/apply", json={})
    b = r.json()
    assert r.status_code == 400 and b["error"]["reason"] == "shutdown_not_confirmed"
    assert b["error"]["stage"] == "svc" and b["error"]["partial"] == []      # 02 §2.4.1 R3-15 的失败合并形状
    assert rig_with_user.wsl.shutdown_calls == 0


async def test_kernel_apply_success_path(rig_with_user, tmp_path):
    src = tmp_path / "bzImage"
    src.write_bytes(b"KERNEL")
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wsl/kernel/apply", json={"confirm_shutdown": True, "kernel_src": str(src)})
    b = r.json()
    assert r.status_code == 202 and b["verify"]["ok"] is True
    assert b["partial"][:2] == ["svc:stage_kernel", "svc:history_begin"] and b["partial"][-1] == "user:apply"
    hist = rig_with_user.db.query("SELECT to_state FROM install_history ORDER BY id")
    assert [h["to_state"] for h in hist] == ["KERNEL_APPLYING", "KERNEL_APPLIED"]


async def test_kernel_apply_verify_failure_auto_rolls_back(rig_with_user, tmp_path):
    src = tmp_path / "bzImage"
    src.write_bytes(b"K")
    rig_with_user.wsl.uname = "6.6.87.2-generic"                        # 三判据过不了
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wsl/kernel/apply", json={"confirm_shutdown": True, "kernel_src": str(src)})
    b = r.json()
    assert r.status_code == 500 and b["error"]["reason"] == "kernel_verify_failed"
    assert "svc:auto_rollback" in b["error"]["partial"]
    states = [h["to_state"] for h in rig_with_user.db.query("SELECT to_state FROM install_history ORDER BY id")]
    assert "KERNEL_ROLLED_BACK" in states


async def test_kernel_verify_records_history(rig_with_user):
    async with client(rig_with_user, token=INSTALLER_TOKEN) as c:
        b = (await c.post("/wa/v1/wsl/kernel/verify", json={})).json()
    assert b == {"ok": True, "uname": "6.6.87.2-binder+", "binder_fs": True, "binderfs_mount": True}
    assert rig_with_user.db.one("SELECT to_state FROM install_history")["to_state"] == "KERNEL_VERIFIED"


async def test_distro_repair_requires_confirm(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        assert (await c.post("/wa/v1/wsl/distro/repair", json={})).status_code == 400
        assert (await c.post("/wa/v1/wsl/distro/repair", json={"confirm": True})).status_code == 202
    assert rig_with_user.wsl.shutdown_calls == 0


# ---------------------------------------------------------------- power 混合端点
async def test_keepawake_svc_half_runs_even_without_user_agent(rig):
    async with client(rig, token=CONSOLE_TOKEN) as c:
        b = (await c.post("/wa/v1/power/keepawake", json={"mode": "powercfg"})).json()
    assert b["mode"] == "powercfg" and len(b["changed"]) == 6
    assert b["display"]["reason"] == "user_agent_offline"           # display 半降级,不整单失败
    async with client(rig) as c:
        st = (await c.get("/wa/v1/power")).json()
    assert st["system_required"] is True


async def test_keepawake_display_half_via_user_agent(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        b = (await c.post("/wa/v1/power/keepawake", json={"mode": "powercfg"})).json()
    assert b["display"]["applied"] is True
    assert rig_with_user.power_backend.display_required is True


# ---------------------------------------------------------------- wechat
async def test_wechat_update_block_is_svc_side(rig):
    """#33b 执行体 = svc:**会话代理不在线也照做**(整机 hosts 归 LocalSystem)。"""
    async with client(rig, token=CONSOLE_TOKEN) as c:
        b = (await c.post("/wa/v1/wechat/update-block", json={"enable": True})).json()
    assert b["result"] == "applied" and b["domains"] == ["dldir1.qq.com", "dldir1v6.qq.com"]
    assert "# QTrade-wechat-update-block" in rig.hosts.text


async def test_wechat_update_block_policy_failure_raises_h21(rig):
    from qtrade_winagent import alerts as A
    rig.hosts.can_write = False
    async with client(rig, token=CONSOLE_TOKEN) as c:
        b = (await c.post("/wa/v1/wechat/update-block", json={"enable": True})).json()
    assert b["result"] == "blocked_by_policy"
    assert rig.deps.alerts.is_firing(A.H21_WECHAT_HOSTS_BLOCK_FAILED, "host")


async def test_wechat_status_merges_hosts_block(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        await c.post("/wa/v1/wechat/update-block", json={"enable": True})
    async with client(rig_with_user) as c:
        b = (await c.get("/wa/v1/wechat/status")).json()
    assert b["enabled"] is True and b["hosts_block"]["enabled"] is True


async def test_wechat_bind_then_profiles(rig_with_user):
    async with client(rig_with_user) as c:
        assert (await c.post("/wa/v1/wechat/bind", json={"wxid": "wxid_a", "account_id": "wx01"})).json()["ok"] is True
        assert (await c.post("/wa/v1/wechat/bind", json={"wxid": "wxid_a", "account_id": "wx01"})).status_code == 200
        bad = await c.post("/wa/v1/wechat/bind", json={"wxid": "wxid_b", "account_id": "wx1"})
        assert bad.status_code == 400
        profiles = (await c.get("/wa/v1/wechat/profiles")).json()["profiles"]
    assert [p["account_id"] for p in profiles] == ["wx01"]


async def test_wechat_send_rejected_without_key_and_ok_with(rig_with_user):
    async with client(rig_with_user) as c:
        r = await c.post("/wa/v1/wechat/send", json={"session_name": "群A", "text": "hi"})
        assert r.status_code == 503 and r.json()["error"]["reason"] == "key_fail"
        rig_with_user.wechat.data_key = rig_with_user.wechat.img_key = True
        ok = await c.post("/wa/v1/wechat/send", json={"session_name": "群A", "text": "hi"})
    assert ok.json()["code"] == "DELIVERED" and ok.json()["ext_msg_id"] == "群A:1"


async def test_wechat_read_and_media_and_screenshot(rig_with_user):
    rig_with_user.wechat.data_key = rig_with_user.wechat.img_key = True
    rig_with_user.wechat.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    rig_with_user.wechat.media_blobs["abc"] = b"\xff\xd8JPEG"
    async with client(rig_with_user) as c:
        b = (await c.get("/wa/v1/wechat/read?talker=群A")).json()
        assert b["messages"][0]["ext_msg_id"] == "群A:1"
        m = await c.get("/wa/v1/wechat/media/abc")
        assert m.content == b"\xff\xd8JPEG"
        s = await c.get("/wa/v1/wechat/screenshot")
        assert s.content.startswith(b"\x89PNG") and s.headers["content-type"] == "image/png"


async def test_wechat_version_match_endpoint(rig):
    rig.deps.wechat_store.put_install(version="4.1.13.12")
    async with client(rig, token=INSTALLER_TOKEN) as c:
        b = (await c.get("/wa/v1/wechat/version-match")).json()
    assert b["match"] == "UNSUPPORTED_NEWER" and b["bundled_version"] == "4.1.12.26"


async def test_settings_wechat_subset_only_and_disable_restores(rig_with_user):
    async with client(rig_with_user, token=CONSOLE_TOKEN) as c:
        await c.post("/wa/v1/power/keepawake", json={"mode": "powercfg"})
        await c.post("/wa/v1/wechat/update-block", json={"enable": True})
        bad = await c.put("/wa/v1/settings/wechat", json={"chatlog_port": 1234})
        assert bad.status_code == 400 and bad.json()["error"]["reason"] == "key_not_allowed"
        b = (await c.put("/wa/v1/settings/wechat", json={"enabled": False})).json()
    assert b["enabled"] is False
    assert rig_with_user.power_backend.timeouts["monitor-timeout-ac"] == 10        # 关模块即还原电源计划
    assert "# QTrade-wechat-update-block" not in rig_with_user.hosts.text          # 并成对删 hosts 行


# ---------------------------------------------------------------- #5 / #6 / #44
async def test_metrics_and_alerts_and_audit_pagination(rig):
    rig.deps.monitor.sample_fast()
    rig.deps.alerts.firing("H14_REBOOT_PENDING", subject="host")
    async with client(rig) as c:
        m = (await c.get("/wa/v1/metrics?scope=host&subject=host&resolution=raw")).json()
        assert len(m["samples"]) == 1
        a = (await c.get("/wa/v1/alerts?since=0")).json()
        assert a["alerts"][0]["payload"]["code"] == "H14_REBOOT_PENDING" and a["next_since"] == 1
        assert (await c.get(f"/wa/v1/alerts?since={a['next_since']}")).json()["alerts"] == []
    async with client(rig, token=CONSOLE_TOKEN) as c:
        page = (await c.get("/wa/v1/audit?limit=2")).json()
    assert len(page["items"]) <= 2 and "next_cursor" in page
