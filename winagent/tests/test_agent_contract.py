"""🔴 **契约测试**:用 Agent 侧的**真实 HTTP 客户端**(``qtrade_agent.winagent_client`` / ``vault_client``)
打本仓库实现的 WinAgent 服务,证明两侧对 02 §2.5 / §3.6 的理解**逐字吻合**。

这里不 mock 任何一侧:
- Agent 侧用它自己的 ``WinAgentClient``(地址解析、令牌读取、超时/重试、``X-WA-Version`` 处理都走它的真代码);
- WinAgent 侧用 ``svc.build_app`` 起的真 FastAPI(只有**平台后端**是 ``Fake*``);
- 中间只垫一层 ``httpx.ASGITransport``,签名与 Agent 的 ``Transport`` 协议逐字一致。

⚠️ 本文件**只读** Agent 侧代码,不改它一个字符;发现不一致写进 ``.omc/handoffs/winagent.md``。
"""
from __future__ import annotations

import pytest

# Agent 侧(``../src`` 经 pyproject 的 pythonpath 引入;只读引用)
from qtrade_agent.config import WinAgentConfig as AgentWinAgentConfig
from qtrade_agent.vault_client import VaultUnavailable, WinAgentVault, credential_ref, vault_name
from qtrade_agent.winagent_client import WA_PORT, WinAgentClient, WinAgentUnavailable

from qtrade_winagent import __version__

from tests.conftest import AGENT_TOKEN, CONSOLE_TOKEN, agent_transport, attach_user_agent, build_rig


def agent_client(rig, *, token: str = AGENT_TOKEN, host: str = "127.0.0.1") -> WinAgentClient:
    """Agent 侧真实客户端,base_url 指到本服务;token 直接给(现实里从 ``/etc/qtrade/winagent.token`` 读)。"""
    return WinAgentClient(AgentWinAgentConfig(url=f"http://winagent.test:{WA_PORT}"),
                          transport=agent_transport(rig, client_host=host),
                          base_url="http://winagent.test", token=token)


@pytest.fixture
async def pair(tmp_path):
    rig = build_rig(tmp_path)
    yield rig, agent_client(rig)
    await rig.deps.hub.stop()
    rig.db.close()


# ---------------------------------------------------------------- #1 ping / #2 health / #4 time
async def test_agent_ping_matches_endpoint_1(pair):
    rig, cl = pair
    body = await cl.ping()
    assert body is not None and body["agent_id"] == "winagent" and body["version"] == __version__
    assert isinstance(body["listen"], list)


async def test_agent_reads_x_wa_version_header(pair):
    rig, cl = pair
    await cl.ping()
    assert cl.last_version == __version__          # §3.8:Agent 靠这个头做主版本协商


async def test_agent_health_matches_endpoint_2(pair):
    rig, cl = pair
    h = await cl.health()
    assert h["ok"] is True and h["user_agent"] is False
    assert set(h["host"]) == {"total_mb", "available_mb", "wsl_vm_mb", "wechat_mb", "chatlog_mb"}   # pool 的输入
    assert h["modules"]["vault"] == "ok"


async def test_agent_health_sees_user_agent_true(tmp_path):
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    cl = agent_client(rig)
    assert (await cl.health())["user_agent"] is True
    await rig.deps.hub.stop()
    rig.db.close()


async def test_agent_time_parses_into_WaTime(pair):
    rig, cl = pair
    rig.sys.resume_ms = 1_700_000_000_000
    t = await cl.time()
    assert t.tz_offset_min == 480 and t.last_resume_ms == 1_700_000_000_000
    assert t.w32time["source"] == "time.windows.com" and t.rtt_ms >= 0


async def test_agent_unauthorized_when_token_wrong(pair):
    rig, _cl = pair
    bad = WinAgentClient(AgentWinAgentConfig(), transport=agent_transport(rig),
                         base_url="http://winagent.test", token="nope")
    assert await bad.health() is None                # 401 ⇒ 客户端按「不可用」处理
    assert (await bad.ping()) is not None            # ping 无鉴权,仍然通


async def test_agent_time_without_token_raises(pair):
    rig, _cl = pair
    cl = WinAgentClient(AgentWinAgentConfig(token_file="/不存在/winagent.token"),
                        transport=agent_transport(rig), base_url="http://winagent.test")
    with pytest.raises(WinAgentUnavailable) as e:
        await cl.time()
    assert e.value.reason == "no_token"


# ---------------------------------------------------------------- #7~#12 Vault(经 Agent 侧 WinAgentVault)
async def test_agent_vault_put_read_exists_delete_roundtrip(pair):
    rig, cl = pair
    v = WinAgentVault(cl)
    ref = credential_ref("qd01")                      # vault://account/qd01
    assert await v.exists(ref) is False
    await v.put(ref, '{"account":"a","secret":"s"}', scope="account")
    assert await v.exists(ref) is True
    assert await v.read(ref, trace_id="TRACE-X") == '{"account":"a","secret":"s"}'
    row = rig.db.one("SELECT * FROM vault_index WHERE name=?", (vault_name(ref),))
    assert row["read_count"] == 1 and row["scope"] == "account"
    await v.delete(ref)
    assert await v.exists(ref) is False
    assert await v.read(ref, trace_id="T") is None


async def test_agent_vault_flag_marks_suspect(pair):
    rig, cl = pair
    v = WinAgentVault(cl)
    await v.put("account/qd01", "pw", scope="account")
    await v.flag("vault://account/qd01", suspect=True)
    assert rig.db.one("SELECT suspect FROM vault_index WHERE name='account/qd01'")["suspect"] == 1


async def test_agent_vault_read_sends_trace_id_and_is_audited(pair):
    rig, cl = pair
    v = WinAgentVault(cl)
    await v.put("mail/smtp", "pw", scope="mail")
    await v.read("mail/smtp", trace_id="TR-CONTRACT")
    row = rig.db.one("SELECT * FROM wa_audit_log WHERE action='vault.read' ORDER BY id DESC")
    assert row["trace_id"] == "TR-CONTRACT" and row["target"] == "mail/smtp"
    assert "pw" not in row["detail_json"]


async def test_agent_vault_nested_hmac_name_survives_the_wire(pair):
    """R6-10:``mail/hmac/cmd/<短名>`` 的 ``cmd/`` 段不得省 —— 多级名字必须原样到达服务端。"""
    rig, cl = pair
    v = WinAgentVault(cl)
    await v.put("vault://mail/hmac/cmd/ops", "key", scope="mail")
    assert await v.read("mail/hmac/cmd/ops", trace_id="T") == "key"
    assert rig.db.one("SELECT name FROM vault_index")["name"] == "mail/hmac/cmd/ops"


async def test_agent_vault_console_token_cannot_read(pair):
    """05 §2.2.4:控制台只能写与删、**不能读回明文**;Agent 侧客户端会把 403 转成 VaultUnavailable。"""
    rig, _cl = pair
    console = WinAgentClient(AgentWinAgentConfig(), transport=agent_transport(rig),
                             base_url="http://winagent.test", token=CONSOLE_TOKEN)
    v_agent = WinAgentVault(agent_client(rig))
    await v_agent.put("account/qd01", "pw", scope="account")
    with pytest.raises(VaultUnavailable) as e:
        await WinAgentVault(console).read("account/qd01", trace_id="T")
    assert e.value.reason == "http_403"


async def test_agent_vault_read_from_non_local_source_is_refused(pair):
    """#11:只接受 loopback / WSL 子网来源。"""
    rig, _cl = pair
    v = WinAgentVault(agent_client(rig))
    await v.put("account/qd01", "pw", scope="account")
    far = WinAgentVault(WinAgentClient(AgentWinAgentConfig(), transport=agent_transport(rig, client_host="8.8.8.8"),
                                       base_url="http://winagent.test", token=AGENT_TOKEN))
    with pytest.raises(VaultUnavailable) as e:
        await far.read("account/qd01", trace_id="T")
    assert e.value.reason == "http_403"


# ---------------------------------------------------------------- 契约面:方法/状态码/重试口径
async def test_write_class_is_not_retried_read_class_is(pair):
    """02 §2.5「重试」:只读类 1 次重试;写类(vault put/delete)**不重试**。

    这里直接数服务端收到的请求条数 —— 契约是双方共同遵守的,不能只在客户端断言。
    """
    rig, cl = pair
    calls: list[str] = []
    inner = agent_transport(rig)

    async def counting(method, url, headers, body, timeout_s):
        calls.append(f"{method} {url}")
        if method in ("PUT", "DELETE"):
            raise OSError("transient")             # 写类失败一次:客户端不得再试
        return await inner(method, url, headers, body, timeout_s)
    cl2 = WinAgentClient(AgentWinAgentConfig(), transport=counting, base_url="http://winagent.test", token=AGENT_TOKEN)
    with pytest.raises(VaultUnavailable):
        await WinAgentVault(cl2).put("account/qd01", "x")
    assert len([c for c in calls if c.startswith("PUT")]) == 1
    calls.clear()
    await cl2.ping()                               # 只读类:失败会重试一次(这里成功,只发一次)
    assert len(calls) == 1


async def test_put_and_delete_return_204_as_client_expects(pair):
    """Agent 侧 ``WinAgentVault`` 把 200/201/204 都当成功、404 当已删;这里锁住我们实际回的码。"""
    rig, cl = pair
    status, _ = await cl.request("PUT", "/wa/v1/vault/api/x", json={"value": "v", "scope": "api"}, retry=False)
    assert status == 204
    status2, _ = await cl.request("DELETE", "/wa/v1/vault/api/x", retry=False)
    assert status2 == 204
    status3, _ = await cl.request("DELETE", "/wa/v1/vault/api/none", retry=False)
    assert status3 == 204                          # 幂等删:不存在也回 204(客户端也接受 404)


async def test_head_exists_returns_200_or_404(pair):
    rig, cl = pair
    s1, _ = await cl.request("HEAD", "/wa/v1/vault/api/none", retry=True)
    assert s1 == 404
    await WinAgentVault(cl).put("api/x", "v", scope="api")
    s2, _ = await cl.request("HEAD", "/wa/v1/vault/api/x", retry=True)
    assert s2 == 200


async def test_user_endpoint_offline_is_503_not_ready_for_agent(pair):
    """Agent 侧据 ``503 NOT_READY`` 把微信账号标 ``degraded(WINAGENT_USER_OFFLINE)``(02 §2.5 降级表)。"""
    rig, cl = pair
    status, body = await cl.request("GET", "/wa/v1/wechat/status", retry=False)
    assert status == 503 and body["code"] == "NOT_READY"
    assert body["error"]["message"] == "用户会话代理未运行(用户未登录或代理被结束)"


async def test_agent_polls_net_seq_to_detect_flips(pair):
    """C-03 单向化:Agent 只能靠轮询 ``GET /wa/v1/net`` 的 ``seq`` 感知网络翻转。"""
    rig, cl = pair
    await rig.deps.netprobe.refresh()
    _s, b1 = await cl.request("GET", "/wa/v1/net", retry=True)
    rig.net.add_vpn()
    await rig.deps.netprobe.refresh()
    _s2, b2 = await cl.request("GET", "/wa/v1/net", retry=True)
    assert b2["seq"] > b1["seq"] and b2["net_state"] == "VPN_ACTIVE"


async def test_agent_pulls_buffered_alerts(pair):
    """#6:Agent 不在时 WinAgent 本地缓冲告警,恢复后 ``GET /wa/v1/alerts?since=`` 拉走。"""
    rig, cl = pair
    rig.deps.alerts.firing("H14_REBOOT_PENDING", subject="host")
    rig.deps.alerts.firing("H16_WINAGENT_BIND_MISMATCH", subject="host")
    _s, b = await cl.request("GET", "/wa/v1/alerts?since=0", retry=True)
    assert [a["payload"]["code"] for a in b["alerts"]] == ["H14_REBOOT_PENDING", "H16_WINAGENT_BIND_MISMATCH"]
    _s2, b2 = await cl.request("GET", f"/wa/v1/alerts?since={b['next_since']}", retry=True)
    assert b2["alerts"] == []


async def test_agent_puts_wsl_side_probe_results_back(pair):
    """#16:``probe_results`` 只在 winagent.db 一份,WSL/容器侧由 Agent 回写。"""
    rig, cl = pair
    status, body = await cl.request("PUT", "/wa/v1/probes", retry=False, json={
        "run_id": "run-contract", "trigger": "boot",
        "results": [{"target": "qidian_msf", "side": "container", "host": "msfxg.3g.qq.com", "port": 8080,
                     "level_reached": "tcp", "result": "OK", "latency_ms": 9}]})
    assert status == 200 and body == {"written": 1}
    rows = rig.deps.netprobe.read_results(run_id="run-contract")
    assert rows[0]["side"] == "container" and rows[0]["trigger"] == "boot"


async def test_agent_unreachable_raises_unavailable(pair):
    rig, _cl = pair

    async def dead(method, url, headers, body, timeout_s):
        raise OSError("connection refused")
    cl = WinAgentClient(AgentWinAgentConfig(), transport=dead, base_url="http://winagent.test", token=AGENT_TOKEN)
    assert await cl.ping() is None and await cl.health() is None
    with pytest.raises(WinAgentUnavailable) as e:
        await cl.time()
    assert e.value.reason == "unreachable"


# ---------------------------------------------------------------- 二进制端点(#40 / #42)
async def test_binary_endpoints_survive_the_agent_client_raw_mode(tmp_path):
    """#40 ``wechat/media`` 与 #42 ``wechat/screenshot`` 回的是**字节不是 JSON**。

    Agent 侧客户端为此提供了 ``request(..., raw=True)``(回 ``(status, headers, body_bytes)``);
    这里验证我们回的 ``Content-Type`` 与字节能原样穿过去 —— 用默认的 JSON 模式会把二进制解析成 ``None``、丢内容。
    """
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    rig.wechat.data_key = rig.wechat.img_key = True
    rig.wechat.media_blobs["k1"] = b"\xff\xd8\xff\xe0JPEGBYTES\x00\x01"
    cl = agent_client(rig)
    status, headers, raw = await cl.request("GET", "/wa/v1/wechat/media/k1", retry=False, raw=True)
    assert status == 200 and raw == b"\xff\xd8\xff\xe0JPEGBYTES\x00\x01"
    assert headers.get("content-type", "").startswith("image/")
    s2, h2, png = await cl.request("GET", "/wa/v1/wechat/screenshot", retry=False, raw=True)
    assert s2 == 200 and png.startswith(b"\x89PNG") and h2.get("content-type") == "image/png"
    await rig.deps.hub.stop()
    rig.db.close()
