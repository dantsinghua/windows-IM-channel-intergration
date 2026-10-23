"""监控采样 / 降采样 / 健康项(04 §2.2、§2.3)与告警去重、重复提醒、风暴合并、本地缓冲(02 §3.7 / 04 §2.4)。"""
from __future__ import annotations

import pytest

from qtrade_winagent import alerts as A
from qtrade_winagent.alerts import AlertBuffer
from qtrade_winagent.backends import ProcInfo
from qtrade_winagent.config import AlertConfig, MonitorConfig
from qtrade_winagent.db import Db
from qtrade_winagent.fakes import FakeProbe, FakeSys
from qtrade_winagent.monitor import HEALTH_CHECK_KEYS, DiskTarget, Monitor

from tests.conftest import Clock


def mk(clock=None, cfg=None, disks=None):
    clk = clock or Clock()
    db = Db(":memory:", clock=clk).open()
    sysb = FakeSys()
    ab = AlertBuffer(AlertConfig(), clock=clk)
    m = Monitor(db, sysb, ab, cfg or MonitorConfig(), probe=FakeProbe(), clock=clk,
                disks=disks or [DiskTarget("programdata", "C:\\ProgramData\\QTrade"),
                                DiskTarget("wechat_data", "D:\\", product_level=False)])
    return db, sysb, ab, m, clk


# ---------------------------------------------------------------- 告警语义
def test_dedup_key_is_code_plus_subject():
    ab = AlertBuffer(AlertConfig(), clock=Clock())
    assert ab.firing(A.H09_CHATLOG_DOWN, subject="account:wx01") is True
    assert ab.firing(A.H09_CHATLOG_DOWN, subject="account:wx01") is False       # 同键只累加
    assert ab.firing(A.H09_CHATLOG_DOWN, subject="account:wx02") is True        # 换 subject 是另一条
    assert ab.active[(A.H09_CHATLOG_DOWN, "account:wx01")].count == 2


def test_severity_flip_re_emits():
    ab = AlertBuffer(AlertConfig(), clock=Clock())
    ab.firing(A.H20_WECHAT_AUTO_UPDATED, subject="host", severity="warn")
    assert ab.firing(A.H20_WECHAT_AUTO_UPDATED, subject="host", severity="crit") is True


def test_repeat_cadence_crit_30min_warn_6h():
    clk = Clock()
    ab = AlertBuffer(AlertConfig(), clock=clk)
    ab.firing(A.H16_WINAGENT_BIND_MISMATCH, subject="host")                     # crit
    ab.firing(A.H11_SESSION_LOCKED, subject="host")                             # warn
    clk.advance(29 * 60_000)
    assert ab.firing(A.H16_WINAGENT_BIND_MISMATCH, subject="host") is False
    clk.advance(2 * 60_000)
    assert ab.firing(A.H16_WINAGENT_BIND_MISMATCH, subject="host") is True      # 30min 到
    clk.advance(5 * 3_600_000)
    assert ab.firing(A.H11_SESSION_LOCKED, subject="host") is False
    clk.advance(2 * 3_600_000)
    assert ab.firing(A.H11_SESSION_LOCKED, subject="host") is True              # 6h 到


def test_info_never_repeats():
    clk = Clock()
    ab = AlertBuffer(AlertConfig(), clock=clk)
    ab.firing(A.H14_REBOOT_PENDING, subject="host")
    clk.advance(30 * 24 * 3_600_000)
    assert ab.firing(A.H14_REBOOT_PENDING, subject="host") is False


def test_storm_merges_beyond_20_per_minute():
    ab = AlertBuffer(AlertConfig(), clock=Clock())
    for i in range(25):
        ab.firing(A.H12_DISK_LOW, subject=f"s{i}")
    codes = [a["payload"]["code"] for a in ab.pull()["alerts"]]
    assert codes.count(A.ALERT_STORM) == 1 and len([c for c in codes if c == A.H12_DISK_LOW]) == 20
    storm = [a for a in ab.pull()["alerts"] if a["payload"]["code"] == A.ALERT_STORM][0]
    assert storm["payload"]["evidence"]["codes"] == [A.H12_DISK_LOW]


def test_buffer_caps_at_buffer_max_and_drops_oldest():
    ab = AlertBuffer(AlertConfig(buffer_max=5, storm_per_min=1000), clock=Clock())
    for i in range(8):
        ab.firing(A.H12_DISK_LOW, subject=f"s{i}")
    out = ab.pull()
    assert out["buffered"] == 5 and out["dropped"] == 3
    assert [a["payload"]["subject"] for a in out["alerts"]] == [f"s{i}" for i in range(3, 8)]


def test_pull_since_advances_without_clearing():
    ab = AlertBuffer(AlertConfig(), clock=Clock())
    ab.firing(A.H14_REBOOT_PENDING, subject="host")
    first = ab.pull()
    assert len(first["alerts"]) == 1
    assert ab.pull(since=first["next_since"])["alerts"] == []
    assert ab.pull(since=0)["alerts"]                                            # 拉走不清空,水位在调用方


def test_net_offline_suppresses_dependent_alerts():
    """04 §2.4.2 依赖抑制:``net_state=OFFLINE`` 时各 probe 失败只推一条 ``NET_OFFLINE``。"""
    ab = AlertBuffer(AlertConfig(), clock=Clock())
    ab.net_offline = True
    assert ab.firing(A.H01_AGENT_API_DOWN, subject="host") is False
    assert ab.firing(A.H15_LOCALHOST_FORWARD_LOST, subject="host") is False
    assert ab.firing(A.NET_OFFLINE, subject="host") is True


def test_registered_severities_match_02_3_7():
    assert A.REGISTERED[A.H01_AGENT_API_DOWN] == "crit"
    assert A.REGISTERED[A.H09_CHATLOG_DOWN] == "crit"
    assert A.REGISTERED[A.H11_SESSION_LOCKED] == "warn"
    assert A.REGISTERED[A.H14_REBOOT_PENDING] == "info"
    assert A.REGISTERED[A.H16_WINAGENT_BIND_MISMATCH] == "crit"
    assert A.REGISTERED[A.WECHAT_DISK_LOW] == "warn"
    assert A.REGISTERED[A.VAULT_ENTROPY_MISSING] == "crit"
    assert A.EVENT_KIND[A.NET_STATE_CHANGED] == "net"


# ---------------------------------------------------------------- 采样与降采样
def test_fast_sample_writes_host_net_and_process_rows():
    db, sysb, _ab, m, _clk = mk()
    sysb.procs = {"vmmem": ProcInfo(9, "vmmem", 11000.0), "Weixin.exe": ProcInfo(10, "Weixin.exe", 900.0),
                  "chatlog.exe": ProcInfo(11, "chatlog.exe", 120.0)}
    m.sample_fast()
    scopes = {(r["scope"], r["subject"]) for r in db.query("SELECT scope, subject FROM health_samples")}
    assert ("host", "host") in scopes and ("net", "vEthernet (WSL)") in scopes
    assert ("wsl", "vmmem") in scopes and ("process", "Weixin.exe") in scopes
    db.close()


def test_slow_items_land_as_1m_not_raw():
    """04 §2.2:60s 项**直接以 1m 粒度落库、不进 raw**。"""
    db, _s, _ab, m, _clk = mk()
    m.sample_slow()
    res = {r["resolution"] for r in db.query("SELECT resolution FROM health_samples WHERE scope='disk'")}
    assert res == {"1m"}
    db.close()


def test_aggregate_rolls_raw_to_1m_with_avg_and_max():
    db, _s, _ab, m, clk = mk()
    for _ in range(6):
        m.sample_fast()
        clk.advance(10_000)
    clk.advance(60_000)
    made = m.aggregate()
    assert made["1m"] >= 1
    row = m.metrics(scope="host", subject="host", resolution="1m")[0]
    assert row["cpu_pct"] is not None and row["agg_max"]["cpu_pct"] is not None
    assert m.aggregate()["1m"] == 0                                             # 幂等:同窗不重复聚合
    db.close()


def test_host_snapshot_feeds_windows_pool():
    db, sysb, _ab, m, _clk = mk()
    sysb.procs = {"vmmemWSL": ProcInfo(9, "vmmemWSL", 10240.0), "WeChat.exe": ProcInfo(10, "WeChat.exe", 800.0),
                  "chatlog.exe": ProcInfo(11, "chatlog.exe", 150.0)}
    snap = m.host_snapshot()
    assert set(snap) == {"total_mb", "available_mb", "wsl_vm_mb", "wechat_mb", "chatlog_mb"}
    assert snap["wsl_vm_mb"] == 10240.0 and snap["wechat_mb"] == 800.0 and snap["chatlog_mb"] == 150.0
    db.close()


# ---------------------------------------------------------------- 健康项
async def test_h01_needs_three_failures_and_h15_when_only_loopback_dead():
    db, _s, ab, m, _clk = mk()
    m._probe.results["http://127.0.0.1:17600/api/v1/system/health"] = ("http", "TCP_TIMEOUT")   # noqa: SLF001
    out = await m.check_agent_api(loopback_url="http://127.0.0.1:17600/api/v1/system/health",
                                  wsl_url="http://172.23.16.5:17600/api/v1/system/health")
    assert out["reachable_via"] == "wsl_ip" and ab.is_firing(A.H15_LOCALHOST_FORWARD_LOST, "host")
    assert not ab.is_firing(A.H01_AGENT_API_DOWN, "host")                        # 只是转发失效,不是 Agent 死
    m._probe.results["http://172.23.16.5:17600/api/v1/system/health"] = ("http", "TCP_TIMEOUT")  # noqa: SLF001
    for i in range(3):
        out = await m.check_agent_api(loopback_url="http://127.0.0.1:17600/api/v1/system/health",
                                      wsl_url="http://172.23.16.5:17600/api/v1/system/health")
    assert out["fail_streak"] == 3 and ab.is_firing(A.H01_AGENT_API_DOWN, "host")
    db.close()


def test_h12_only_our_partitions_drive_levels_wechat_disk_is_independent():
    """🔴 R4-9:微信数据根**不进产品级三级水位**,只发独立 ``WECHAT_DISK_LOW``。"""
    db, sysb, ab, m, _clk = mk()
    sysb.disks = {"C:\\ProgramData\\QTrade": (40000.0, 244140.0), "D:\\": (500.0, 500000.0)}
    out = m.check_disks(wechat_data_path="D:\\")
    assert out["level"] == "ok"                                                   # 微信盘快满了也不降级整站
    assert ab.is_firing(A.WECHAT_DISK_LOW, "host") and not ab.is_firing(A.H12_DISK_LOW, "host")
    sysb.disks["C:\\ProgramData\\QTrade"] = (1500.0, 244140.0)
    out2 = m.check_disks(wechat_data_path="D:\\")
    assert out2["level"] in ("high", "critical") and ab.is_firing(A.H12_DISK_LOW, "host")
    ev = ab.active[(A.H12_DISK_LOW, "host")].evidence
    assert set(("free_mb", "db_size_mb", "media_size_mb")) <= set(ev)              # 00 §11.11 ③ 必带三数
    db.close()


def test_h23_vhdx_growth_warns_on_diff():
    db, _s, ab, m, _clk = mk()
    m.check_disks(vhdx_size_mb=60000.0, ext4_used_mb=10000.0)
    assert ab.is_firing(A.H23_VHDX_GROWTH, "host")
    m.check_disks(vhdx_size_mb=12000.0, ext4_used_mb=10000.0)
    assert not ab.is_firing(A.H23_VHDX_GROWTH, "host")
    db.close()


def test_h14_info_then_warn_after_a_day():
    clk = Clock()
    db, sysb, ab, m, _ = mk(clock=clk)
    sysb.reboot_pending = True
    m.check_pending_reboot()
    assert ab.active[(A.H14_REBOOT_PENDING, "host")].severity == "info"
    clk.advance(86_400_001)
    m.check_pending_reboot()
    assert ab.active[(A.H14_REBOOT_PENDING, "host")].severity == "warn"
    sysb.reboot_pending = False
    m.check_pending_reboot()
    assert not ab.is_firing(A.H14_REBOOT_PENDING, "host")
    db.close()


def test_h20_version_change_is_crit_update_pkg_is_warn():
    db, sysb, ab, m, _clk = mk()
    sysb.versions["D:\\Weixin.exe"] = "4.1.13.12"
    m.check_wechat_version(exe_path="D:\\Weixin.exe", recorded="4.1.12.26", update_pkg_present=False)
    a = ab.active[(A.H20_WECHAT_AUTO_UPDATED, "host")]
    assert a.severity == "crit" and a.hint_actions == ["wechat_reinstall_bundled"]
    ab.resolve(A.H20_WECHAT_AUTO_UPDATED, subject="host")
    m.check_wechat_version(exe_path="D:\\Weixin.exe", recorded="4.1.13.12", update_pkg_present=True)
    assert ab.active[(A.H20_WECHAT_AUTO_UPDATED, "host")].severity == "warn"
    db.close()


def test_h09_h10_h11_h16_flip_with_facts():
    db, sysb, ab, m, _clk = mk()
    m.check_chatlog(account_id="wx01", http_ok=False)
    assert ab.is_firing(A.H09_CHATLOG_DOWN, "account:wx01")
    m.check_chatlog(account_id="wx01", http_ok=True)
    assert not ab.is_firing(A.H09_CHATLOG_DOWN, "account:wx01")
    m.check_wechat_process(account_id="wx01", process_exists=True, window_exists=False)
    assert ab.is_firing(A.H10_WECHAT_PROCESS_MISSING, "account:wx01")
    sysb.locked = True
    assert m.check_session_locked() is True and ab.is_firing(A.H11_SESSION_LOCKED, "host")
    m.check_bind(expected={"127.0.0.1", "172.23.16.1"}, actual={"127.0.0.1"})
    assert ab.is_firing(A.H16_WINAGENT_BIND_MISMATCH, "host")
    db.close()


# ---------------------------------------------------------------- health_checks(R6-58 (ao):恒八键)
def test_health_checks_is_fixed_eight_keys_with_none_when_unrun():
    """一项都没跑过时:``health_checks()`` 仍是那八个键,值全 ``None`` —— 不是空字典。"""
    db, _sysb, _ab, m, _clk = mk()
    assert HEALTH_CHECK_KEYS == ("H01", "H09", "H10", "H11", "H14", "H15", "H16", "H20")
    assert m.health_checks() == {k: None for k in HEALTH_CHECK_KEYS}
    db.close()


def test_health_checks_fills_in_as_checks_run_and_excludes_h12():
    """跑过的项给真布尔值,没跑过的仍是 ``None``;H12(本模块内部还有的项)不进本端点的八键。"""
    db, sysb, _ab, m, _clk = mk()
    m.check_pending_reboot()                                         # H14:未待重启 ⇒ True
    sysb.locked = True
    m.check_session_locked()                                         # H11:锁屏 ⇒ False
    m.check_disks()                                                  # H12(不在八键内)
    out = m.health_checks()
    assert out["H14"] is True and out["H11"] is False
    assert out["H01"] is None and out["H09"] is None and out["H10"] is None
    assert out["H15"] is None and out["H16"] is None and out["H20"] is None
    assert "H12" not in out and set(out) == set(HEALTH_CHECK_KEYS)
    db.close()
