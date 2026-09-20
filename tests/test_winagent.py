"""WinAgent 客户端(02 §2.5 契约:方向/地址/令牌/超时/重试)、Vault 客户端(#7~#12)、H13 校时(04 §2.9)、H02 探活与降级(02 §2.5)。"""
from __future__ import annotations

import json

import pytest

from qtrade_agent.alerts import H13_CLOCK_DRIFT
from qtrade_agent.config import WinAgentConfig
from qtrade_agent.timesync import TimeSync
from qtrade_agent.vault_client import VaultUnavailable, WinAgentVault
from qtrade_agent.winagent_client import (VAULT_MAX_VALUE_BYTES, FakeWinAgent, WinAgentClient, WinAgentUnavailable,
                                          base_url_candidates, default_gateway, resolve_base_url)
from tests.conftest import Clock, make_rig


# ---------------------------------------------------------------- 地址 / 令牌
def test_resolve_base_url_order(tmp_path):
    """04 §2.6.3 的回退链:``url`` → ``host.json`` → **eth0 默认网关** → ``resolv.conf``(最后兜底)。

    🔴 默认网关**排在 resolv.conf 之前**:04 §2.6.3 逐条论证 `nameserver` 不可靠(公司 VPN 改写 Windows DNS、
    §2.6.5 的 DNS 对策会主动写公司 DNS)。原实现少了网关这一环,开 VPN 的现场会把 VPN DNS 当宿主机地址。
    """
    hint = tmp_path / "host.json"
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 172.20.0.1\n")
    cfg = WinAgentConfig(url="", host_ip_hint_file=str(hint))
    gw = lambda: "172.20.0.5"                                                        # noqa: E731

    assert resolve_base_url(cfg, resolv_conf=str(resolv), gateway=gw) == "http://172.20.0.5:17610"
    assert resolve_base_url(cfg, resolv_conf=str(resolv), gateway=lambda: None) == "http://172.20.0.1:17610"
    hint.write_text(json.dumps({"host_ip": "172.20.0.9"}))
    assert resolve_base_url(cfg, resolv_conf=str(resolv), gateway=gw) == "http://172.20.0.9:17610"
    assert resolve_base_url(WinAgentConfig(url="http://127.0.0.1:17610/", host_ip_hint_file=str(hint))) == "http://127.0.0.1:17610"


def test_base_url_candidates_are_ordered_and_deduped(tmp_path):
    hint = tmp_path / "host.json"
    hint.write_text(json.dumps({"host_ip": "172.20.0.5"}))                           # 与网关同值 ⇒ 去重
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.8.0.1\n")                                       # 典型的「VPN DNS」
    cfg = WinAgentConfig(url="", host_ip_hint_file=str(hint))
    assert base_url_candidates(cfg, resolv_conf=str(resolv), gateway=lambda: "172.20.0.5") == [
        ("host_json", "http://172.20.0.5:17610"), ("resolv_conf", "http://10.8.0.1:17610")]
    # [winagent] url 显式配置 ⇒ 唯一答案,不再自动发现
    assert base_url_candidates(WinAgentConfig(url="http://1.2.3.4:17610")) == [("url", "http://1.2.3.4:17610")]


def test_default_gateway_parses_proc_net_route(tmp_path):
    """`/proc/net/route` 的 Gateway 列是**小端 hex**;多条默认路由按 Metric 最小的取(与 `ip route show default` 一致)。"""
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0114A8C0\t0003\t0\t0\t200\t00000000\t0\t0\t0\n"      # 192.168.20.1 metric 200
        "eth0\t0014A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"        # 直连路由:跳过
        "eth0\t00000000\t0110A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")     # 192.168.16.1 metric 100
    assert default_gateway(route_file=str(route)) == "192.168.16.1"
    assert default_gateway(route_file=str(tmp_path / "nope")) is None


async def test_discover_prefers_the_reachable_candidate(tmp_path):
    """04 §2.6.3:「`host.json` 与默认网关不一致时以**能 ping 通 `/wa/v1/ping`** 的那个为准,并记 warn」。"""
    hint = tmp_path / "host.json"
    hint.write_text(json.dumps({"host_ip": "10.8.0.1"}))            # 陈旧/错的首选项(VPN 起来后 host.json 没人更新)
    cfg = WinAgentConfig(url="", host_ip_hint_file=str(hint), token_file=str(tmp_path / "t.token"))
    (tmp_path / "t.token").write_text("wa-token\n")
    wa = FakeWinAgent()
    reachable = "http://172.20.0.5:17610"

    async def only_gateway_answers(method, url, headers, body, timeout_s):
        if not url.startswith(reachable):
            raise OSError("no route to host")
        return await wa(method, url, headers, body, timeout_s)

    cl = WinAgentClient(cfg, transport=only_gateway_answers, resolv_conf=str(tmp_path / "none"),
                        gateway=lambda: "172.20.0.5")
    assert cl.base_url == "http://10.8.0.1:17610"                   # 首选项仍是 host.json
    assert await cl.discover() == reachable                         # 择优后落到网关
    assert cl.base_url_source == "gateway"
    assert (await cl.ping())["agent_id"] == "fake"


async def test_discover_keeps_first_choice_when_nothing_answers(tmp_path):
    cfg = WinAgentConfig(url="", host_ip_hint_file=str(tmp_path / "none.json"), token_file=str(tmp_path / "t.token"))

    async def dead(method, url, headers, body, timeout_s):
        raise OSError("connection refused")

    cl = WinAgentClient(cfg, transport=dead, resolv_conf=str(tmp_path / "none"), gateway=lambda: "172.20.0.5")
    assert await cl.discover() == "http://172.20.0.5:17610"
    assert cl.base_url_source == "gateway"


def test_explicit_base_url_is_never_rediscovered(tmp_path):
    """测试/`[winagent] url` 显式给的地址 ⇒ `forget_base_url()` 不动它(否则契约测试会被环境的真路由表带跑)。"""
    cl = WinAgentClient(WinAgentConfig(), transport=FakeWinAgent(), base_url="http://x:17610")
    cl.forget_base_url()
    assert cl.base_url == "http://x:17610" and cl.base_url_source == "explicit"


async def test_token_file_required_except_ping(tmp_path):
    wa = FakeWinAgent()
    cfg = WinAgentConfig(token_file=str(tmp_path / "missing.token"))
    cl = WinAgentClient(cfg, transport=wa, base_url="http://x:17610")
    assert (await cl.ping())["agent_id"] == "fake"                           # #1 无鉴权
    with pytest.raises(WinAgentUnavailable) as e:
        await cl.time()
    assert e.value.reason == "no_token"
    (tmp_path / "t.token").write_text("wa-token\n")
    cl2 = WinAgentClient(WinAgentConfig(token_file=str(tmp_path / "t.token")), transport=wa, base_url="http://x:17610")
    t = await cl2.time()
    assert t.tz_offset_min == 480 and cl2.last_version == "1.0.0"
    assert wa.calls[-1][2]["Authorization"] == "Bearer wa-token"


# ---------------------------------------------------------------- 超时 / 重试(02 §2.5)
async def test_readonly_retries_once_write_never(tmp_path):
    wa = FakeWinAgent()
    cl = WinAgentClient(WinAgentConfig(), transport=wa, base_url="http://x:17610", token="wa-token")
    wa.fail_next = 1
    assert (await cl.ping()) is not None and len(wa.calls) == 2              # 只读:1 次重试
    wa.fail_next = 2
    assert (await cl.ping()) is None and len(wa.calls) == 4                  # 两次都失败 → 放弃
    v = WinAgentVault(cl)
    wa.fail_next = 1
    with pytest.raises(VaultUnavailable):
        await v.put("account/qd01", "s")                                     # 写类不重试
    assert len(wa.calls) == 5
    wa.delay_s = 2.6
    assert await cl.ping() is None                                           # ping 2 s 超时


async def test_vault_roundtrip_via_winagent(tmp_path):
    wa = FakeWinAgent()
    cl = WinAgentClient(WinAgentConfig(), transport=wa, base_url="http://x:17610", token="wa-token")
    v = WinAgentVault(cl)
    assert await v.exists("account/qd01") is False
    await v.put("vault://account/qd01", "pw", scope="account")
    assert await v.exists("account/qd01") is True and await v.read("account/qd01", trace_id="tr1") == "pw"
    rd = [c for c in wa.calls if c[1] == "/wa/v1/vault/account/qd01/read"][0]
    assert rd[0] == "POST" and rd[2]["X-Trace-Id"] == "tr1"                  # 读有副作用故 POST + X-Trace-Id(C-06)
    await v.flag("account/qd01", suspect=True)
    assert wa.vault["account/qd01"]["suspect"] is True
    await v.delete("account/qd01")
    assert await v.read("account/qd01") is None
    wa.offline = True
    with pytest.raises(VaultUnavailable):
        await v.read("account/qd01")


# ---------------------------------------------------------------- H13
class FakeAligner:
    def __init__(self, clock, ok=True):
        self.clock, self.ok, self.calls = clock, ok, []

    async def align(self, t_win_ms):
        self.calls.append(t_win_ms)
        if self.ok:
            self.clock.now_ms = t_win_ms
        return self.ok


async def test_h13_drift_aligns_and_only_warns_when_align_fails(tmp_path):
    clock = Clock()
    wa = FakeWinAgent()
    rig = make_rig(tmp_path, clock=clock, aligner=FakeAligner(clock, ok=True))
    rig.winagent.now_ms = clock.now_ms + 5000
    # 用 rig 的 winagent(transport)但换 now:FakeWinAgent.now_ms 固定 → 对齐后本地=对端
    ts = rig.agent.timesync
    ts._aligner = FakeAligner(clock, ok=True)
    res = await ts.probe()
    assert res["aligned"] is True and res["drift"] is False and not rig.agent.health.h13_firing()
    assert not rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")
    # 对齐失败 → warn firing + checks.H13 firing
    rig.winagent.now_ms = clock.now_ms + 60_000
    ts._aligner = FakeAligner(clock, ok=False)
    res = await ts.probe()
    assert res["drift"] is True and rig.agent.health.h13_firing() and rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")
    a = rig.agent.alerts.active[(H13_CLOCK_DRIFT, "wsl")]
    assert a.severity == "warn" and a.evidence["delta_ms"] > 2000 and a.evidence["aligned"] is False
    # 回到阈值内 → resolved
    rig.winagent.now_ms = None
    clock.now_ms = __import__("time").time_ns() // 1_000_000
    res = await ts.probe()
    assert res["drift"] is False and not rig.agent.health.h13_firing()
    assert [e["payload"]["state"] for e in rig.store.list_events(event="alert") if e["payload"]["code"] == H13_CLOCK_DRIFT] == ["firing", "resolved"]
    rig.store.close()


async def test_h13_last_resume_change_triggers_on_resume(tmp_path):
    clock = Clock()
    resumed = []

    async def on_resume(ms):
        resumed.append(ms)
    wa = FakeWinAgent()
    wa.now_ms = clock.now_ms
    cl = WinAgentClient(WinAgentConfig(), transport=wa, base_url="http://x:17610", token="wa-token", clock=clock)

    class H:
        def __init__(self):
            self.v = False

        def set_h13(self, f):
            self.v = f

    class A:
        def firing(self, *a, **k): ...

        def resolve(self, *a, **k): ...
    ts = TimeSync(cl, health=H(), alerts=A(), clock=clock, aligner=FakeAligner(clock), on_resume=on_resume)
    wa.last_resume_ms = 1000
    await ts.probe()
    assert resumed == []                                                     # 首次只记基线
    await ts.probe()
    assert resumed == []
    wa.last_resume_ms = 2000
    await ts.probe()
    assert resumed == [2000]                                                 # 变化才回调
    wa.offline = True
    assert (await ts.probe())["ok"] is False and ts.last_delta_ms is not None


async def test_on_host_resume_reruns_ensure_root_for_running_qidian(tmp_path):
    rig = make_rig(tmp_path)
    rig.store.ensure_account("qd01", "qidian", state="running")
    rig.store.ensure_account("qd02", "qidian", state="stopped")
    await rig.agent.on_host_resume(123)
    s = "127.0.0.1:16001"
    assert rig.adb.calls[:2] == [("disconnect", s), ("connect", s)] and ("root", s) in rig.adb.calls
    assert not any(v == "127.0.0.1:16002" for _, v in rig.adb.calls)
    rig.store.close()


# ---------------------------------------------------------------- H02 探活 → health / pool 降级
async def test_winagent_probe_updates_health_and_windows_pool(tmp_path):
    rig = make_rig(tmp_path)
    wa, health, pool = rig.winagent, rig.agent.health, rig.agent.pool
    wa.wechat_enabled = True
    await rig.agent.winagent_probe()
    assert health.winagent_online is True and health.winagent_version == "1.0.0" and health.user_agent_online is True
    assert pool.windows_known and pool.snapshot()["pools"]["windows"]["total_mb"] == 16384 and pool.can_add("wechat")[0] is True
    assert rig.store.pool_get("wsl")["total_mb"] == 11264 and rig.store.pool_get("wsl")["source"] == "winagent"
    wa.user_agent = False
    await rig.agent.winagent_probe()
    assert health.user_agent_online is False and health.winagent_online is True
    wa.offline = True
    await rig.agent.winagent_probe()
    assert health.winagent_online is True and health.winagent_fail_streak == 1      # R-09 去抖:3 次才判离线
    await rig.agent.winagent_probe()
    await rig.agent.winagent_probe()
    assert health.winagent_online is False and pool.windows_known is False and pool.can_add("wechat") == (False, "winagent_offline", [])
    assert health.summary() == {"ok": True, "agent": True, "dockerd": False, "winagent": False, "user_agent": False}
    rig.store.close()


async def test_dockerd_probe_and_health_endpoint_checks(tmp_path):
    rig = make_rig(tmp_path)
    await rig.agent.dockerd_probe()
    assert rig.agent.health.dockerd_ok is True
    rig.containers.dockerd_ok = False
    await rig.agent.dockerd_probe()
    assert rig.agent.health.dockerd_ok is False
    rig.store.close()


# ---------------------------------------------------------------- 假后端保真度(winagent 交接 B-2 / B-3)
async def test_fake_health_reports_all_six_modules(tmp_path):
    """02 §3.6 #2:`health.modules` 恒含六个键。假后端少键会让「按 modules.wslctl 做降级」的测试假绿(B-2)。"""
    (tmp_path / "t.token").write_text("wa-token\n")
    cl = WinAgentClient(WinAgentConfig(token_file=str(tmp_path / "t.token")), transport=FakeWinAgent(),
                        base_url="http://x:17610")
    h = await cl.health()
    assert set(h["modules"]) == {"vault", "monitor", "netprobe", "power", "wslctl", "wechat"}


async def test_fake_vault_rejects_oversize_value(tmp_path):
    """05 §2.2.2:Vault 单条明文上限 4 KB(B-3)。"""
    (tmp_path / "t.token").write_text("wa-token\n")
    v = WinAgentVault(WinAgentClient(WinAgentConfig(token_file=str(tmp_path / "t.token")), transport=FakeWinAgent(),
                                     base_url="http://x:17610"))
    await v.put("account/qd01", "x" * VAULT_MAX_VALUE_BYTES)
    with pytest.raises(VaultUnavailable) as e:
        await v.put("account/qd02", "x" * (VAULT_MAX_VALUE_BYTES + 1))
    assert e.value.reason == "http_400"


async def test_fake_vault_read_checks_source(tmp_path):
    """02 §3.6 #11:读只接受 loopback / WSL 子网(B-3)。"""
    (tmp_path / "t.token").write_text("wa-token\n")
    wa = FakeWinAgent()
    v = WinAgentVault(WinAgentClient(WinAgentConfig(token_file=str(tmp_path / "t.token")), transport=wa,
                                     base_url="http://x:17610"))
    await v.put("account/qd01", "s3cr3t")
    wa.client_ip = "172.20.0.9"                       # WSL 子网:放行
    assert await v.read("account/qd01") == "s3cr3t"
    wa.client_ip = "203.0.113.7"                      # 公网来源:403
    with pytest.raises(VaultUnavailable) as e:
        await v.read("account/qd01")
    assert e.value.reason == "http_403"
