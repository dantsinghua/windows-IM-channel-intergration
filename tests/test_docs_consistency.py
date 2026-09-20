"""代码 ↔ 文档对账:配置默认值与 02 §7.1 同值;schema 文件与 02 §3.1 的 sql 块逐字一致;能力目录会话参数一律叫 session。"""
from __future__ import annotations

import glob
import json
import os
import re

import pytest

from qtrade_agent.config import AgentConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC02 = glob.glob(os.path.join(ROOT, "docs", "02-*.md"))[0]
DOC06 = glob.glob(os.path.join(ROOT, "docs", "06-*.md"))[0]
SCHEMA = os.path.join(ROOT, "src", "qtrade_agent", "store", "schema_agent.sql")
CAPS = os.path.join(ROOT, "src", "qtrade_agent", "capabilities")


def _doc(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_schema_file_matches_doc_sql_block():
    blocks = re.findall(r"```sql\n(.*?)```", _doc(DOC02), re.S)
    assert _doc(SCHEMA).endswith(blocks[1]), "schema_agent.sql 与 02 §3.1 的 ```sql 块不一致:改表先改 02,再重新抽取"


@pytest.mark.parametrize("key, value", [
    ("confirm_timeout_qidian_ms", AgentConfig().bus.confirm_timeout_qidian_ms),
    ("out_merge_window_s", AgentConfig().bus.out_merge_window_s),
    ("late_after_s", AgentConfig().messages.late_after_s),
    ("confirm_poll_interval_ms", AgentConfig().qidian.confirm_poll_interval_ms),
    ("poll_interval_s", AgentConfig().qidian.poll_interval_s),
    ("http_sync_max_wait_ms", AgentConfig().api.http_sync_max_wait_ms),
    ("ws_queue_max", AgentConfig().events.ws_queue_max),
    ("ws_retention_hours", AgentConfig().events.ws_retention_hours),
    ("port", AgentConfig().api.port),
    ("boot_timeout_s", AgentConfig().runtime.boot_timeout_s),
    ("qidian_mem_limit_mb", AgentConfig().runtime.qidian_mem_limit_mb),
    ("qq_mem_limit_mb", AgentConfig().runtime.qq_mem_limit_mb),
    ("webui_temp_minutes", AgentConfig().runtime.webui_temp_minutes),
    ("auto_restart_max_per_hour", AgentConfig().runtime.auto_restart_max_per_hour),
    ("wsl_reserved_mb", AgentConfig().pool.wsl_reserved_mb),
    ("windows_reserved_mb", AgentConfig().pool.windows_reserved_mb),
    ("probe_interval_s", AgentConfig().winagent.probe_interval_s),
    ("timeout_ms", AgentConfig().winagent.timeout_ms),
    ("slot_pending_ttl_s", AgentConfig().wechat.slot_pending_ttl_s),
    ("slot_reaper_interval_s", AgentConfig().wechat.slot_reaper_interval_s),
    ("slot_error_takeover_s", AgentConfig().wechat.slot_error_takeover_s),
])
def test_config_defaults_match_doc_02(key, value):
    t = _doc(DOC02)
    if key == "confirm_timeout_qidian_ms":
        m = re.search(r"`confirm_timeout_qidian_ms`[^|\n]*\|\s*`\d+`\s*/\s*`(\d+)`", t)
    elif key == "confirm_poll_interval_ms":
        m = re.search(r"\| `confirm_poll_interval_ms` \| `(\d+)` \| \*\*R6-38", t)
    elif key == "poll_interval_s":
        m = re.search(r"\| `poll_interval_s` \| `(\d+)` \| 旁路读库", t)
    else:
        m = re.search(r"`%s` \| `(\d+)`" % key, t)
    assert m, f"02 §7.1 找不到 {key}"
    assert int(m.group(1)) == value


@pytest.mark.parametrize("key, value", [
    # [adapters.qq](02 §7.1;07 第 54 行镜像)
    ("history_backfill_on_reconnect", AgentConfig().qq.history_backfill_on_reconnect),
    # [adapters.wechat]
    ("switch_drain_timeout_s", AgentConfig().wechat_adapter.switch_drain_timeout_s),
    # [api] 公网入站 HMAC(§3.5)
    ("hmac_clock_skew_s", AgentConfig().hmac.hmac_clock_skew_s),
    ("nonce_ttl_s", AgentConfig().hmac.nonce_ttl_s),
    ("rate_default_per_min", AgentConfig().api.rate_default_per_min),
    # [events] webhook(§2.2.7)
    ("webhook_timeout_ms", AgentConfig().webhook.webhook_timeout_ms),
    ("webhook_max_attempts", AgentConfig().webhook.webhook_max_attempts),
    # [retention](§2.8.4 / E-18)
    ("files_days", AgentConfig().retention.files_days),
    ("messages_days", AgentConfig().retention.messages_days),
    ("media_days", AgentConfig().retention.media_days),
    ("raw_days", AgentConfig().retention.raw_days),
    ("mail_archive_days", AgentConfig().retention.mail_archive_days),
    ("commands_days", AgentConfig().retention.commands_days),
    ("audit_days", AgentConfig().retention.audit_days),
    # R6-58 (c):`[retention] events_ws_hours` 已从 02 §7.1 / docs/07 / RetentionConfig 一起删,
    # `events_outbox` 两类行统一按 `[events] ws_retention_hours`(下面 EventsConfig 那条已对账)。
    ("idempotency_days", AgentConfig().retention.idempotency_days),
    ("mail_inbox_rows_days", AgentConfig().retention.mail_inbox_rows_days),
    ("export_jobs_days", AgentConfig().retention.export_jobs_days),
    ("cleanup_batch", AgentConfig().retention.cleanup_batch),
    # [media] / [db]
    ("orphan_grace_h", AgentConfig().media.orphan_grace_h),
    # [jobs](R6-16)/ [api] 公网出口探测
    ("reclaim_after_s", AgentConfig().jobs.reclaim_after_s),
    ("reclaim_interval_s", AgentConfig().jobs.reclaim_interval_s),
    ("public_ip_check_interval_s", AgentConfig().api.public_ip_check_interval_s),
])
def test_new_section_defaults_match_doc_02(key, value):
    """第五批接线新并入 config.py 的段(02 §7.1 是这些键的真值/镜像位置)。"""
    m = re.search(r"`%s` \| `(\d+)`" % re.escape(key), _doc(DOC02))
    assert m, f"02 §7.1 找不到 {key}"
    assert int(m.group(1)) == value


def test_adapters_qq_and_wechat_pairs_match_doc_02():
    """并列写在同一行的键:`[adapters.qq] heartbeat_timeout_s/reconnect_delay_s` 与 `[adapters.wechat]` 两个周期键。"""
    t = _doc(DOC02)
    c = AgentConfig()
    m = re.search(r"`heartbeat_timeout_s` / `reconnect_delay_s` \| `(\d+)` / `(\d+)`", t)
    assert m and (int(m.group(1)), int(m.group(2))) == (c.qq.heartbeat_timeout_s, c.qq.reconnect_delay_s)
    m = re.search(r"\| `poll_interval_s` \| `(\d+)` \| 常态读库周期", t)
    assert m and int(m.group(1)) == c.wechat_adapter.poll_interval_s
    m = re.search(r"\| `confirm_poll_interval_ms` \| `(\d+)` \| 发送后临时加速", t)
    assert m and int(m.group(1)) == c.wechat_adapter.confirm_poll_interval_ms


def test_monitor_sample_interval_matches_doc_02():
    """`[monitor]` 的 owner 是 04 §7,02 §7.1 只镜像;两个节拍键并列写在一行。"""
    m = re.search(r"`sample_interval_s` / `slow_interval_s` \| `(\d+)` / `(\d+)`", _doc(DOC02))
    c = AgentConfig().monitor
    assert m and (int(m.group(1)), int(m.group(2))) == (c.sample_interval_s, c.slow_interval_s)


def test_public_ip_probe_urls_match_doc_02():
    m = re.search(r"`public_ip_probe_urls` \| `\[([^\]]+)\]`", _doc(DOC02))
    assert m
    urls = tuple(x.strip().strip('"') for x in m.group(1).split(","))
    assert urls == AgentConfig().api.public_ip_probe_urls


def test_calibration_and_backup_and_watermark_match_doc_02():
    """`[pool] calibration_*`、`[db] backup_*`、`[retention]` 三级水位与两个时刻串。"""
    t = _doc(DOC02)
    c = AgentConfig()
    m = re.search(r"`calibration_window_min` / `calibration_drift_warn_pct` / `quota_auto_lower` \| `(\d+)` / `(\d+)` / `(\w+)`", t)
    assert m and (int(m.group(1)), int(m.group(2)), m.group(3) == "true") == (
        c.calib.calibration_window_min, c.calib.calibration_drift_warn_pct, c.calib.quota_auto_lower)
    m = re.search(r'`backup_dir` / `backup_keep` \| `"([^"]+)"` / `(\d+)`', t)
    assert m and (m.group(1), int(m.group(2))) == (c.backup.backup_dir, c.backup.backup_keep)
    for key, val in (("backup_at", c.backup.backup_at), ("cleanup_at", c.retention.cleanup_at)):
        m = re.search(r'`%s` \| `"([^"]+)"`' % key, t)
        assert m and m.group(1) == val, key
    m = re.search(r"`disk_warn_mb` / `disk_high_mb` / `disk_critical_mb` \| `(\d+)` / `(\d+)` / `(\d+)`", t)
    assert m and tuple(map(int, m.groups())) == (c.retention.disk_warn_mb, c.retention.disk_high_mb, c.retention.disk_critical_mb)
    m = re.search(r"`webhook_backoff_ms` \| `\[([\d,]+)\]`", t)
    assert m and tuple(int(x) for x in m.group(1).split(",")) == c.webhook.webhook_backoff_ms
    m = re.search(r"`honor_env_proxy` \| `(\w+)`", t)
    assert m and (m.group(1) == "true") == c.net.honor_env_proxy


def test_pool_quota_and_runtime_strings_match_doc_02():
    t = _doc(DOC02)
    m = re.search(r"`quota_qidian_mb` / `quota_qq_mb` / `quota_wechat_mb` \| `(\d+)` / `(\d+)` / `(\d+)`", t)
    c = AgentConfig()
    assert tuple(map(int, m.groups())) == (c.pool.quota_qidian_mb, c.pool.quota_qq_mb, c.pool.quota_wechat_mb)
    for key, val in (("redroid_image", c.runtime.redroid_image), ("napcat_image", c.runtime.napcat_image), ("token_file", c.winagent.token_file),
                     ("apk_cache_dir", c.runtime.apk_cache_dir), ("docker_socket", c.runtime.docker_socket), ("accounts_dir", c.runtime.accounts_dir),
                     ("host_ip_hint_file", c.winagent.host_ip_hint_file)):
        m = re.search(r'`%s` \| `"([^"]*)"`' % key, t)
        assert m and m.group(1) == val, key
    m = re.search(r"`qidian_resolution` / `qidian_dpi` \| `\"([^\"]+)\"` / `(\d+)`", t)
    assert m.group(1) == c.runtime.qidian_resolution and int(m.group(2)) == c.runtime.qidian_dpi


DOC04 = glob.glob(os.path.join(ROOT, "docs", "04-*.md"))[0]
DOC05 = glob.glob(os.path.join(ROOT, "docs", "05-*.md"))[0]


@pytest.mark.parametrize("key, value", [
    ("container_check_s", AgentConfig().health.container_check_s),
    ("adb_check_s", AgentConfig().health.adb_check_s),
    ("adb_root_grace_s", AgentConfig().health.adb_root_grace_s),
    ("napcat_heartbeat_timeout_s", AgentConfig().health.napcat_heartbeat_timeout_s),
    ("scrcpy_frame_timeout_s", AgentConfig().health.scrcpy_frame_timeout_s),
    ("clock_drift_warn_s", AgentConfig().health.clock_drift_warn_s),
    ("container_mem_warn_pct", AgentConfig().health.container_mem_warn_pct),
    ("container_restart_max", AgentConfig().health.container_restart_max),
    ("mem_warn_mb", AgentConfig().pool.mem_warn_mb),
    ("mem_critical_mb", AgentConfig().pool.mem_critical_mb),
])
def test_health_and_monitor_defaults_match_doc_04(key, value):
    """04 §7 [health](agent.toml)与 [monitor](winagent.toml)是这些键的唯一出处;02 [pool] mem_* 只引用。"""
    m = re.search(r"^%s\s*=\s*(\d+)" % re.escape(key), _doc(DOC04), re.M)
    assert m, f"04 §7 找不到 {key}"
    assert int(m.group(1)) == value


def test_agent_probe_enabled_default_matches_doc_04():
    """04 §7 `[probe]` 是 `agent_probe_enabled` 的 owner(裁决 00 §15g R6-62 Ⅶ①):缺省 `false` = 缺省不出网。"""
    m = re.search(r"^agent_probe_enabled\s*=\s*(true|false)", _doc(DOC04), re.M)
    assert m, "04 §7 [probe] 找不到 agent_probe_enabled"
    default = AgentConfig().probe.agent_probe_enabled
    assert default is False
    assert m.group(1) == ("true" if default else "false")


def test_restart_backoff_matches_doc_04():
    m = re.search(r"^container_restart_backoff_s\s*=\s*\[([\d,]+)\]", _doc(DOC04), re.M)
    assert tuple(int(x) for x in m.group(1).split(",")) == AgentConfig().health.container_restart_backoff_s


@pytest.mark.parametrize("key, value", [
    ("login_timeout_s", AgentConfig().accounts.login_timeout_s),
    ("login_remind_interval_s", AgentConfig().accounts.login_remind_interval_s),
    ("qr_max_wait_s", AgentConfig().accounts.qr_max_wait_s),
    ("qq_quick_login_wait_s", AgentConfig().accounts.qq_quick_login_wait_s),
    ("qq_reconnect_grace_s", AgentConfig().accounts.qq_reconnect_grace_s),
])
def test_accounts_defaults_match_doc_05(key, value):
    """05 §7 [accounts] 是 owner。"""
    m = re.search(r"^%s\s*=\s*(\d+)" % re.escape(key), _doc(DOC05), re.M)
    assert m, f"05 §7 找不到 {key}"
    assert int(m.group(1)) == value


def test_port_plan_matches_doc_00_port_table():
    """00 §3 端口表:段基址 + NN;02 §2.2.4 唯一算法。"""
    from qtrade_agent.runtime import port_plan
    d02 = _doc(DOC02)
    m = re.search(r"qidian: adb=(\d+)\+seq\s+stream=(\d+)\+seq\s+frida=(\d+)\+seq", d02)
    assert m and port_plan("qidian", 7) == {"adb": int(m.group(1)) + 7, "stream": int(m.group(2)) + 7, "frida": int(m.group(3)) + 7, "adb_serial": f"127.0.0.1:{int(m.group(1)) + 7}"}
    m = re.search(r"qq:\s+ws=(\d+)\+seq\s+http=(\d+)\+seq\s+webui=(\d+)\+seq", d02)
    assert m and port_plan("qq", 7) == {"ws": int(m.group(1)) + 7, "http": int(m.group(2)) + 7, "webui": int(m.group(3)) + 7}


def test_pool_used_sql_matches_doc_02_pseudocode():
    """02 §2.2.5:used = Σ quota_mb of accounts where host='wsl' and enabled and state ∉ {stopped, disabled, error}。"""
    from qtrade_agent.store import Store
    d02 = _doc(DOC02)
    assert "used = Σ quota_mb of accounts where host='wsl' and enabled and state ∉ {stopped, disabled, error}" in d02
    assert "host='wsl' AND enabled=1 AND state NOT IN ('stopped','disabled','error')" in Store.POOL_USED_SQL


def test_error_since_sql_matches_doc_02():
    """02 §2.6「两个动作」的规范 SQL 逐字出现在 store.transition 里(参数占位换成 ?)。"""
    d02 = _doc(DOC02)
    src = _doc(os.path.join(ROOT, "src", "qtrade_agent", "store", "store.py"))
    for stmt in ("UPDATE account_runtime SET error_since_ms = :now_ms, updated_ms = :now_ms\n WHERE account_id = :id AND error_since_ms IS NULL",
                 "UPDATE account_runtime SET error_since_ms = NULL, updated_ms = :now_ms\n WHERE account_id = :id AND error_since_ms IS NOT NULL"):
        assert stmt in d02
        code = re.sub(r":now_ms|:id", "?", stmt).replace("\n ", " ")
        assert code in src, code


def test_gap_keys_match_doc_02():
    m = re.search(r"`gap_check_interval_s` / `gap_window_days` / `gap_min_missing` \| `(\d+)` / `(\d+)` / `(\d+)`", _doc(DOC02))
    c = AgentConfig().qidian
    assert tuple(map(int, m.groups())) == (c.gap_check_interval_s, c.gap_window_days, c.gap_min_missing)


def test_norm_definition_in_code_equals_doc_06():
    """06 §2.9.2 的 def norm 函数体逐行等于 text.py 里的实现(去掉注释与空行)。"""
    m = re.search(r"def norm\(s: str \| None\) -> str:(.*?)```", _doc(DOC06), re.S)
    assert m
    doc_lines = [re.sub(r"\s*#.*$", "", l).strip() for l in m.group(1).splitlines()]
    doc_lines = [l for l in doc_lines if l and not l.startswith('"""')]
    src = _doc(os.path.join(ROOT, "src", "qtrade_agent", "text.py"))
    body = re.search(r"def norm\(s: str \| None\) -> str:(.*?)\n\n\ndef ", src, re.S).group(1)
    body = re.sub(r'""".*?"""', "", body, flags=re.S)                # 去掉 docstring
    code_lines = [re.sub(r"\s*#.*$", "", l).strip() for l in body.splitlines()]
    code_lines = [l for l in code_lines if l]
    assert doc_lines == code_lines, (doc_lines, code_lines)


def test_capabilities_session_param_name():
    for p in glob.glob(os.path.join(CAPS, "*.json")):
        cap = json.load(open(p, encoding="utf-8"))
        assert cap["op"] == os.path.basename(p)[:-5]
        assert cap["kind"] in ("read", "write", "admin") and isinstance(cap["danger"], bool)
        for name in cap["args_schema"].get("properties", {}):
            assert name not in ("peer", "chat", "target", "talker", "group_id", "user_id"), f"{p}: 会话参数一律命名 session"
