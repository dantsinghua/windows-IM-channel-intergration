"""netprobe(04 §2.6/§2.8:net_state、监听集合与重绑、防火墙唯一拥有者、逐级探测)与 power(04 §2.5.1 keep-awake)。"""
from __future__ import annotations

import pytest

from qtrade_winagent import alerts as A
from qtrade_winagent.alerts import AlertBuffer
from qtrade_winagent.audit import Audit
from qtrade_winagent.backends import ProxyInfo
from qtrade_winagent.config import AlertConfig, NetConfig, ProbeConfig, WechatConfig
from qtrade_winagent.db import Db
from qtrade_winagent.errors import WaError
from qtrade_winagent.fakes import FakeFirewall, FakeNet, FakePower, FakeProbe, FakeSys
from qtrade_winagent.netprobe import NetProbe, ProbeTargetSpec
from qtrade_winagent.power import BACKUP_KEY, CURRENT_KEY, Power

from tests.conftest import SVC_EXE, Clock


def mk_np(clock=None):
    clk = clock or Clock()
    db = Db(":memory:", clock=clk).open()
    net, fw, probe = FakeNet(), FakeFirewall(), FakeProbe()
    ab = AlertBuffer(AlertConfig(), clock=clk)
    np_ = NetProbe(db, net, fw, probe, NetConfig(), ProbeConfig(), ab, svc_exe=SVC_EXE, clock=clk)
    return db, net, fw, probe, ab, np_


# ---------------------------------------------------------------- net_state(04 §2.6.6 逐行)
async def test_net_state_direct():
    _db, _n, _f, _p, _a, np_ = mk_np()
    assert await np_.net_state() == "DIRECT"


async def test_net_state_offline_without_default_route():
    _db, net, _f, _p, _a, np_ = mk_np()
    net.default_route = False
    assert await np_.net_state() == "OFFLINE"


async def test_net_state_offline_when_unreachable_and_no_proxy():
    _db, _n, _f, probe, _a, np_ = mk_np()
    probe.results["msfxg.3g.qq.com:443"] = ("tcp", "TCP_TIMEOUT")
    probe.results["long.weixin.qq.com:443"] = ("tcp", "TCP_TIMEOUT")
    assert await np_.net_state() == "OFFLINE"


async def test_unreachable_with_proxy_is_system_proxy_not_offline():
    """有代理时「直连探测失败」本来就该失败,不能据此判 OFFLINE(04 §2.6.6 注释)。"""
    _db, net, _f, probe, _a, np_ = mk_np()
    net.proxy_info = ProxyInfo(winhttp="http://proxy.corp:8080")
    probe.results["msfxg.3g.qq.com:443"] = ("tcp", "TCP_TIMEOUT")
    probe.results["long.weixin.qq.com:443"] = ("tcp", "TCP_TIMEOUT")
    assert await np_.net_state() == "SYSTEM_PROXY"


async def test_user_level_wininet_proxy_alone_is_not_system_proxy():
    _db, net, _f, _p, _a, np_ = mk_np()
    net.proxy_info = ProxyInfo(wininet_user={"ProxyEnable": 1}, policy_per_user=1)
    assert await np_.net_state() == "DIRECT"
    net.proxy_info = ProxyInfo(wininet_user={"ProxyEnable": 1}, policy_per_user=0)      # 策略级才算
    assert await np_.net_state() == "SYSTEM_PROXY"


async def test_vpn_and_vpn_with_proxy():
    _db, net, _f, _p, _a, np_ = mk_np()
    net.add_vpn()
    assert await np_.net_state() == "VPN_ACTIVE"
    net.proxy_info = ProxyInfo(pac="http://wpad/wpad.dat")
    assert await np_.net_state() == "VPN_ACTIVE_WITH_PROXY"


async def test_seq_bumps_only_on_real_change_and_alerts_net_state_changed():
    _db, net, _f, _p, ab, np_ = mk_np()
    await np_.refresh()
    seq1 = np_.state.seq
    await np_.refresh()
    assert np_.state.seq == seq1                                                     # 无变化不 bump
    net.add_vpn()
    await np_.refresh()
    assert np_.state.seq == seq1 + 1 and ab.is_firing(A.NET_STATE_CHANGED, "host")


async def test_net_offline_fires_and_resolves():
    _db, net, _f, _p, ab, np_ = mk_np()
    net.default_route = False
    await np_.refresh()
    assert ab.is_firing(A.NET_OFFLINE, "host") and ab.net_offline is True
    net.default_route = True
    await np_.refresh()
    assert not ab.is_firing(A.NET_OFFLINE, "host") and ab.net_offline is False


# ---------------------------------------------------------------- 监听集合与重绑
async def test_listen_set_never_binds_0_0_0_0():
    _db, net, _f, _p, _a, np_ = mk_np()
    assert np_.desired_listen() == ("127.0.0.1", "172.23.16.1")
    net.set_wsl_ipv4(None)                                                            # WSL 未启动
    assert np_.desired_listen() == ("127.0.0.1",)
    assert "0.0.0.0" not in np_.desired_listen()


async def test_subnet_change_rebinds_updates_firewall_and_alerts():
    _db, net, fw, _p, ab, np_ = mk_np()
    await np_.refresh()
    np_.firewall_ensure()
    net.set_wsl_ipv4("172.28.0.1")
    await np_.refresh()
    out = np_.reconcile_listen(actual=("127.0.0.1", "172.23.16.1"))
    assert out["rebind"] == ["172.28.0.1"]
    assert out["firewall"]["remote_address"] == "172.28.0.0/20"                       # RemoteAddress 恒取当前子网
    assert ab.is_firing(A.WSL_SUBNET_CHANGED, "host") and ab.is_firing(A.H16_WINAGENT_BIND_MISMATCH, "host")


async def test_host_json_keys_match_agent_client_expectations():
    """Agent 侧 ``winagent_client.resolve_base_url`` 只认 ``winagent_base_url`` / ``host_ip`` 两个键。"""
    _db, _n, _f, _p, _a, np_ = mk_np()
    await np_.refresh()
    hj = np_.host_json()
    assert hj["host_ip"] == "172.23.16.1" and hj["winagent_base_url"] == "http://172.23.16.1:17610"


# ---------------------------------------------------------------- 防火墙(唯一拥有者 + 幂等)
def test_firewall_rule_shape_limits_program_port_alias_subnet():
    _db, _n, fw, _p, _a, np_ = mk_np()
    np_.state.wsl_subnet = "172.23.16.0/20"
    np_.firewall_ensure()
    rule = fw.get_rule("QTrade-WinAgent-17610-from-WSL")
    assert rule.program == SVC_EXE and "user.exe" not in rule.program                 # R-14:只限服务 exe
    assert (rule.local_port, rule.protocol, rule.direction, rule.action) == (17610, "TCP", "Inbound", "Allow")
    assert rule.interface_alias == "vEthernet (WSL)" and rule.remote_address == "172.23.16.0/20"
    assert rule.profile == "Any"                                                      # 限 Private 会不生效


def test_firewall_idempotent_created_unchanged_updated():
    _db, net, _f, _p, _a, np_ = mk_np()
    np_.state.wsl_subnet = "172.23.16.0/20"
    assert np_.firewall_ensure()["result"] == "created"
    assert np_.firewall_ensure()["result"] == "unchanged"
    np_.state.wsl_subnet = "172.28.0.0/20"
    assert np_.firewall_ensure()["result"] == "updated"


def test_firewall_fallback_subnet_when_unknown():
    _db, net, _f, _p, _a, np_ = mk_np()
    np_.state.wsl_subnet = None
    assert np_.firewall_ensure()["remote_address"] == "172.16.0.0/12"


def test_firewall_delete_is_idempotent_and_paired():
    _db, _n, _f, _p, _a, np_ = mk_np()
    np_.firewall_ensure(lan=True, lan_remote="192.168.3.0/24")
    out = np_.firewall_delete(lan=True)
    assert out["result"] == "removed" and out["lan"]["result"] == "removed"
    assert np_.firewall_delete()["result"] == "absent"


def test_firewall_blocked_by_policy():
    _db, _n, fw, _p, _a, np_ = mk_np()
    fw.blocked_by_policy = True
    assert np_.firewall_ensure()["result"] == "blocked_by_policy"
    assert np_.firewall_delete()["result"] == "blocked_by_policy"


# ---------------------------------------------------------------- 逐级探测
async def test_probe_records_deepest_level_reached():
    db, _n, _f, probe, _a, np_ = mk_np()
    probe.results["mail.corp:993"] = ("tls", "TLS_FAIL")
    rid = await np_.probe_run([ProbeTargetSpec("mail_imap", "mail.corp", 993, "windows", True)], trigger="manual")
    row = np_.read_results(run_id=rid)[0]
    assert (row["level_reached"], row["result"]) == ("tcp", "TLS_FAIL")               # 停在 tcp 之后、tls 失败
    db.close()


async def test_probe_skips_non_windows_side_with_agent_unreachable():
    """C-03:WinAgent **不**反向转发 WSL 侧;安装期 Agent 不可达时记 ``SKIPPED``。"""
    db, _n, _f, _p, _a, np_ = mk_np()
    rid = await np_.probe_run([ProbeTargetSpec("apk_url", "apk.corp", 443, "wsl", True)], trigger="install")
    row = np_.read_results(run_id=rid)[0]
    assert row["result"] == "SKIPPED" and row["detail"] == "agent_unreachable"
    db.close()


async def test_probe_results_carry_net_state_and_reject_bad_enum():
    db, _n, _f, _p, _a, np_ = mk_np()
    await np_.refresh()
    rid = await np_.probe_run([ProbeTargetSpec("wechat_servers", "long.weixin.qq.com", 443, "windows", True)],
                              trigger="periodic")
    assert np_.read_results(run_id=rid)[0]["net_state"] == "DIRECT"
    with pytest.raises(WaError):
        np_.write_results("r2", [{"target": "不存在", "side": "wsl", "host": "h", "level_reached": "tcp",
                                  "result": "OK"}], trigger="manual")
    with pytest.raises(WaError):
        await np_.probe_run([], trigger="不存在的触发")
    db.close()


async def test_put_probes_writes_agent_side_results():
    db, _n, _f, _p, _a, np_ = mk_np()
    n = np_.write_results("run-1", [{"target": "qidian_msf", "side": "container", "host": "msfxg.3g.qq.com",
                                     "port": 8080, "level_reached": "tcp", "result": "OK", "latency_ms": 12}],
                          trigger="periodic")
    assert n == 1 and np_.read_results(latest=True)[0]["side"] == "container"
    db.close()


async def test_sample_returns_rows_and_does_not_persist():
    """🔴 04 §3.4 逐字:WinAgent 的 ``{mode:'sample'}`` **返回 `{rows:[…]}` 不落库** ——
    落库由 Agent 聚合三侧后经 ``PUT /probes {kind:'observed'}`` 写一次,两侧各写一遍会把 ``hits`` 算两倍。"""
    db, _n, _f, probe, _a, np_ = mk_np()
    probe.conns = [{"ip": "203.205.254.1", "port": 443, "hostname": "long.weixin.qq.com", "channel": "wechat"}]
    out = await np_.sample_connections(pid_names=("Weixin.exe",), duration_s=99)
    assert out["duration_s"] == 30                                                     # 上限 30
    assert set(out["rows"][0]) >= {"pid_name", "ip", "port", "samples", "hostname", "resolved_by"}
    assert out["rows"][0]["pid_name"] == "Weixin.exe" and out["rows"][0]["resolved_by"] == "cache"
    assert db.query("SELECT * FROM probe_targets_observed") == []                      # 不落库
    assert db.query("SELECT * FROM probe_results") == []                               # §2.8.5:sample 不产生 probe_result
    db.close()


# ---------------------------------------------------------------- power(04 §2.5.1)
def mk_power(clock=None):
    clk = clock or Clock()
    db = Db(":memory:", clock=clk).open()
    pb, sysb = FakePower(), FakeSys()
    return db, pb, sysb, Power(db, pb, sysb, WechatConfig(), audit=Audit(db, clock=clk), clock=clk)


def test_powercfg_backs_up_original_values_once_only():
    """🔴 「**只在第一次写时存,之后不覆盖**」——否则第二次启用会把我们改成的 0 当原值备份掉。"""
    db, pb, _s, p = mk_power()
    p.apply("powercfg")
    assert db.get_setting(BACKUP_KEY)["standby-timeout-ac"] == 30
    assert all(v == 0 for v in pb.timeouts.values())
    p.apply("powercfg")
    assert db.get_setting(BACKUP_KEY)["standby-timeout-ac"] == 30                      # 没有被 0 覆盖
    db.close()


def test_six_items_are_exactly_the_04_list():
    db, pb, _s, p = mk_power()
    p.apply("powercfg")
    assert sorted(i for i, _v in pb.changes) == sorted(WechatConfig().powercfg_items)
    db.close()


def test_restore_puts_originals_back_and_removes_key():
    db, pb, _s, p = mk_power()
    p.apply("powercfg")
    out = p.restore()
    assert out["restored"] and pb.timeouts["monitor-timeout-ac"] == 10
    assert db.get_setting(BACKUP_KEY) is None and db.get_setting(CURRENT_KEY)["mode"] == "off"
    db.close()


def test_request_mode_does_not_touch_power_plan():
    db, pb, _s, p = mk_power()
    p.apply("request")
    assert pb.changes == [] and pb.system_required is True
    assert p.display_required_pending is True                                          # DISPLAY 归会话代理
    db.close()


def test_policy_locked_degrades_to_request_behaviour():
    db, pb, _s, p = mk_power()
    pb.policy_locked = True
    out = p.apply("powercfg")
    assert out["policy_degraded"] is True and pb.system_required is True               # 不绕策略,退化到电源请求
    db.close()


def test_recheck_rewrites_externally_changed_items():
    db, pb, _s, p = mk_power()
    p.apply("powercfg")
    pb.timeouts["monitor-timeout-ac"] = 15                                             # 被外部改回
    out = p.recheck()
    assert out["rewritten"] == ["monitor-timeout-ac"] and pb.timeouts["monitor-timeout-ac"] == 0
    row = db.one("SELECT * FROM wa_audit_log WHERE action='power.recheck'")
    assert row is not None                                                             # 记审计(info,不告警)
    db.close()


def test_status_shape_matches_endpoint_18():
    db, _pb, sysb, p = mk_power()
    sysb.locked = True
    p.apply("request")
    st = p.status()
    assert set(st) == {"mode", "system_required", "display_required", "session_locked"}
    assert st["session_locked"] is True
    db.close()


def test_bad_mode_is_rejected():
    db, _pb, _s, p = mk_power()
    with pytest.raises(WaError) as e:
        p.apply("不存在")
    assert e.value.reason == "bad_mode"
    db.close()


def test_recheck_is_noop_before_any_apply():
    """🔴 没 apply 过就复核 = 在**没有原值备份**的情况下把用户电源计划改成 0,关模块时还原不回去。

    (--dev 冒烟里实际踩到过:服务一起来,周期任务第一轮就把六项写成 0,而 ``power.backup_json`` 还不存在。)
    """
    db, pb, _s, p = mk_power()
    before = dict(pb.timeouts)
    out = p.recheck()
    assert out["skipped"] == "never_applied" and out["rewritten"] == []
    assert pb.timeouts == before and pb.changes == []
    assert db.get_setting(BACKUP_KEY) is None
    db.close()


def test_recheck_is_noop_after_restore():
    db, pb, _s, p = mk_power()
    p.apply("powercfg")
    p.restore()
    pb.timeouts["standby-timeout-ac"] = 30
    out = p.recheck()
    assert out["mode"] == "off" and out["rewritten"] == []          # 关模块后不再看管电源计划
    assert pb.timeouts["standby-timeout-ac"] == 30
    db.close()
