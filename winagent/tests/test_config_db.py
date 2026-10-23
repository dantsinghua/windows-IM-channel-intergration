"""配置默认值(02 §7.2 + 04 §7 + 05 §7)与 ``winagent.db``(02 §3.2 DDL、R3-20 保留期清理)。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from qtrade_winagent.config import (MonitorConfig, ProbeConfig, RetentionConfig, WinAgentConfig, WslConfig, load,
                                    wsl_memory_gb)
from qtrade_winagent.db import Db, SCHEMA_PATH, load_schema_sql

DOCS = Path(__file__).resolve().parents[2] / "docs"


# ---------------------------------------------------------------- 02 §7.2 / 04 §7 逐键对账
def test_api_and_ipc_defaults_match_02_7_2():
    c = WinAgentConfig()
    assert (c.api.port, c.api.api_version) == (17610, "1.0")
    assert c.api.agent_token_ref == "vault://winagent/agent_token"
    assert c.api.console_token_ref == "vault://winagent/console_token"
    assert c.api.pipe_console == r"\\.\pipe\qtrade-winagent"
    assert c.api.pipe_user == r"\\.\pipe\qtrade-winagent-user"      # R3-12:无 -<SID> 后缀
    assert (c.ipc.heartbeat_s, c.ipc.offline_after_s, c.ipc.max_frame_kb) == (5, 15, 1024)


def test_monitor_retention_three_levels_match_04():
    m = MonitorConfig()
    assert (m.raw_retention_h, m.m1_retention_d, m.h1_retention_d) == (24, 7, 30)    # R3-24 / R5-13(90→30)
    assert (m.disk_warn_mb, m.disk_high_mb, m.disk_critical_mb) == (5120, 2048, 1024)
    assert (m.mem_warn_mb, m.mem_critical_mb, m.auto_stop_on_pressure) == (2048, 1024, False)
    assert m.wechat_disk_warn_mb == 2048 and m.vhdx_growth_warn_gb == 20


def test_net_and_probe_defaults_match_04_7():
    c = WinAgentConfig()
    assert c.net.firewall_rule_winagent == "QTrade-WinAgent-17610-from-WSL"
    assert c.net.firewall_rule_agent_lan == "QTrade-Agent-17600-LAN"
    assert c.net.wsl_subnet_fallback == "172.16.0.0/12"
    assert "飞连" in c.net.vpn_adapter_patterns and "Sangfor" in c.net.vpn_adapter_patterns
    assert (c.probe.dns_timeout_s, c.probe.tcp_timeout_s, c.probe.tls_timeout_s, c.probe.http_timeout_s) == (3, 5, 5, 5)
    assert c.probe.results_retention_d == 30 and c.probe.sample_duration_s == 10


def test_wechat_defaults_and_hosts_marker():
    w = WinAgentConfig().wechat
    assert w.enabled is False                                   # 全系统唯一真值,默认关(C-43)
    assert w.keep_awake_mode == "powercfg"                      # C-6
    assert w.update_block_domains == ("dldir1.qq.com", "dldir1v6.qq.com")     # 验证报告 §5.3:只这 2 个
    assert w.update_block_marker == "# QTrade-wechat-update-block"            # R6-15 逐行行尾标记
    assert w.wxkey_dlls == ("wx_key2.dll", "wx_key1.dll")
    assert (w.narrator_min_seconds, w.narrator_max_rounds, w.logout_mode) == (300, 2, "process")
    assert len(w.powercfg_items) == 6 and "hibernate-timeout-dc" in w.powercfg_items


def test_wsl_defaults_and_never_shutdown_flag():
    w = WslConfig()
    assert w.distro == "qtrade" and w.autostart is True
    assert w.never_shutdown is True                              # 00 §11.6 [NOSHUTDOWN] 的只读提醒常量
    assert (w.swap_gb, w.processors, w.auto_memory_reclaim) == (2, 0, "gradual")
    assert w.backup_keep == 10 and w.overwrite_existing is True


def test_retention_wa_audit_is_30_days_not_365():
    assert RetentionConfig().wa_audit_days == 30                 # E-18 由 365 收紧到 30


@pytest.mark.parametrize("phys,wechat_on,expect", [(4, False, 5), (8, False, 5), (12, False, 8), (16, False, 11),
                                                   (16, True, 10), (24, False, 16), (32, False, 24), (64, False, 48)])
def test_wsl_memory_table_owner_is_04(phys, wechat_on, expect):
    """04 §2.7.1 分档表(R3-23:唯一出处 = 04;16G 档两列 11/10)。"""
    assert wsl_memory_gb(phys, cfg=WslConfig(), wechat_on=wechat_on) == expect


def test_load_ignores_unknown_sections_and_keys():
    c = load({"wechat": {"enabled": True, "不存在的键": 1}, "未知段": {"x": 1},
              "net": {"vpn_adapter_patterns": ["Foo"]}})
    assert c.wechat.enabled is True and c.net.vpn_adapter_patterns == ("Foo",)


# ---------------------------------------------------------------- 02 §3.2 DDL
def test_schema_sql_is_verbatim_from_docs_02_3_2():
    """``schema_winagent.sql`` 必须与 docs/02 §3.2 的代码块**逐字**一致(去掉本文件头两行抽取说明)。"""
    src = (DOCS / "02-后端与本地数据库设计.md").read_text(encoding="utf-8")
    i = src.index("### 3.2 `winagent.db` 全部 DDL")
    j = src.index("```sql", i) + len("```sql\n")
    k = src.index("\n```", j)
    doc_ddl = src[j:k]
    ours = load_schema_sql().split("\n", 2)[2].rstrip("\n")
    assert ours == doc_ddl


def test_all_tables_created(tmp_path):
    d = Db(str(tmp_path / "w.db")).open()
    names = {r["name"] for r in d.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_version", "install_state", "install_history", "health_samples", "probe_results",
            "wechat_profiles", "wechat_install", "wechat_version_matrix", "probe_targets_observed",
            "vault_index", "settings", "wa_audit_log"} <= names
    d.close()


def test_pragmas_and_schema_version(tmp_path):
    d = Db(str(tmp_path / "w.db")).open()
    assert d.con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert d.con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert d.one("SELECT version FROM schema_version")["version"] == 1
    d.close()


def test_retention_sweep_deletes_per_resolution(tmp_path, ):
    """三级降采样**逐档**各自的截止点(04 §2.2):raw 24h / 1m 7d / 1h 30d。"""
    now = 1_800_000_000_000
    d = Db(str(tmp_path / "w.db"), clock=lambda: now).open()
    day = 86_400_000
    rows = [("raw", now - 2 * 3_600_000), ("raw", now - 48 * 3_600_000),
            ("1m", now - 3 * day), ("1m", now - 9 * day),
            ("1h", now - 10 * day), ("1h", now - 40 * day)]
    with d.tx() as con:
        for res, ts in rows:
            con.execute("INSERT INTO health_samples(ts_ms, resolution, scope, subject, extra_json) VALUES (?,?,?,?, '{}')",
                        (ts, res, "host", "host"))
        con.execute("INSERT INTO probe_results(run_id, ts_ms, trigger, target, side, host, level_reached, result) "
                    "VALUES ('r', ?, 'manual', 'apk_url', 'wsl', 'h', 'tcp', 'OK')", (now - 40 * day,))
        con.execute("INSERT INTO wa_audit_log(ts_ms, actor, action, result) VALUES (?, 'agent', 'x', 'OK')",
                    (now - 40 * day,))
    deleted = d.retention_sweep(monitor=MonitorConfig(), probe=ProbeConfig(), retention=RetentionConfig())
    assert deleted == {"health_samples": 3, "probe_results": 1, "probe_targets_observed": 0, "wa_audit_log": 1}
    left = {(r["resolution"], r["ts_ms"]) for r in d.query("SELECT resolution, ts_ms FROM health_samples")}
    assert left == {("raw", now - 2 * 3_600_000), ("1m", now - 3 * day), ("1h", now - 10 * day)}
    d.close()


def test_next_sweep_is_local_0300():
    """R3-20:与 Agent 侧对齐 —— **每日 03:00 本地时间**。"""
    import datetime
    base = int(datetime.datetime(2026, 9, 20, 8, 0, 0).timestamp() * 1000)
    nxt = datetime.datetime.fromtimestamp(Db.next_sweep_ms(base) / 1000)
    assert (nxt.hour, nxt.minute) == (3, 0) and nxt > datetime.datetime.fromtimestamp(base / 1000)


def test_settings_roundtrip(tmp_path):
    d = Db(str(tmp_path / "w.db")).open()
    d.put_setting("power.backup_json", {"scheme_guid": "g", "standby-timeout-ac": 30})
    assert d.get_setting("power.backup_json")["standby-timeout-ac"] == 30
    assert d.get_setting("不存在") is None
    d.close()
