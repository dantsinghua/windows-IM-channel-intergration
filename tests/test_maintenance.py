"""保留期清理 / VACUUM / 备份 / 磁盘三级水位 / ``DISK_FULL``(02 §2.8.4、§2.8.8、§3.3;E-18)。"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from qtrade_agent.alerts import Alerts
from qtrade_agent.events import Events
from qtrade_agent.maintenance import (ACTIONS_CRITICAL, ACTIONS_HIGH, BackupConfig, DB_WRITE_FAILED, DiskFullError,
                                      FakeDisk, H12_DISK_LOW, LEVEL_CRITICAL, LEVEL_HIGH, LEVEL_NORMAL, LEVEL_WARN,
                                      MaintenanceService, RetentionConfig, is_disk_full_error)
from qtrade_agent.models import Message, Session

DAY_MS = 86400_000


def _svc(store, clock, tmp_path, *, cfg=None, free=100_000, alerts=None, backup=None):
    return MaintenanceService(store, cfg=cfg or RetentionConfig(), data_dir=str(tmp_path),
                              disk=FakeDisk(free=free), alerts=alerts, clock=clock,
                              backup=backup or BackupConfig(backup_dir=str(tmp_path / "backup")))


def _msg(store, clock, *, ts_ms, ext, account_id="qd01", media=None, raw_ref=None) -> str:
    m = Message(account_id=account_id, channel="qidian", session=Session(account_id, "10001", "private", "对端"),
                dir="in", type="text", text="老消息", ts_ms=ts_ms, source="qidian_db", ext_msg_id=ext,
                media=media or [], raw_ref=raw_ref)
    res = store.ingest(m, now_ms=ts_ms)
    return res.id


# ---------------------------------------------------------------- 配置:E-18 上限与水位顺序
def test_clamp_truncates_data_class_days_to_30_with_warning():
    cfg, warns = RetentionConfig(messages_days=365, audit_days=90).clamp()
    assert cfg.messages_days == 30 and cfg.audit_days == 30
    assert len(warns) == 2 and "截断" in warns[0]
    assert RetentionConfig().clamp()[1] == []


def test_disk_low_watermark_alias_equals_high():
    cfg = RetentionConfig()
    assert cfg.disk_low_watermark_mb == cfg.disk_high_mb == 2048
    assert cfg.watermarks_ordered() is True
    assert RetentionConfig(disk_warn_mb=100).watermarks_ordered() is False


def test_disk_level_thresholds(store, clock, tmp_path):
    s = _svc(store, clock, tmp_path)
    assert s.disk_level(5121) == LEVEL_NORMAL
    assert s.disk_level(5119) == LEVEL_WARN
    assert s.disk_level(2047) == LEVEL_HIGH
    assert s.disk_level(1023) == LEVEL_CRITICAL


# ---------------------------------------------------------------- ① 消息 + ref_count 递减
def test_cleanup_deletes_old_messages_and_decrements_media_refcount(store, clock, tmp_path):
    now = clock()
    store.con.execute("INSERT INTO media(id, sha256, kind, mime, size, rel_path, status, ref_count, first_seen_ms, ready_ms) "
                      "VALUES (7,'abc','image','image/png',10,'media/202609/abc','ready',2,?,?)", (now, now))
    _msg(store, clock, ts_ms=now - 40 * DAY_MS, ext="old1", media=[{"media_id": 7, "state": "ready"}])
    _msg(store, clock, ts_ms=now - 1 * DAY_MS, ext="fresh")
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["messages"] == 1
    assert store.count_messages("qd01") == 1
    assert store.con.execute("SELECT ref_count FROM media WHERE id=7").fetchone()[0] == 1


def test_cleanup_writes_last_cleanup_setting_and_audit(store, clock, tmp_path):
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=clock())
    last = store.settings_get("system.last_cleanup")
    assert set(last) == {"at", "freed_mb"} and last["at"].endswith("+08:00")
    assert [a for a in store.list_audit("retention.cleanup")][0]["kind"] == "system"
    snap = s.watermark_snapshot()
    assert snap["last_cleanup_at"] == last["at"] and snap["last_cleanup_freed_mb"] == last["freed_mb"]
    assert rep.retention_days == 30


# ---------------------------------------------------------------- ② 媒体独立过期
def test_media_expires_by_its_own_days_independent_of_refcount(store, clock, tmp_path):
    now = clock()
    (tmp_path / "media" / "202609").mkdir(parents=True)
    f = tmp_path / "media" / "202609" / "abc"
    f.write_bytes(b"x" * 100)
    store.con.execute("INSERT INTO media(id, sha256, kind, mime, size, rel_path, status, ref_count, first_seen_ms, ready_ms) "
                      "VALUES (7,'abc','image','image/png',100,'media/202609/abc','ready',1,?,?)",
                      (now - 20 * DAY_MS, now - 10 * DAY_MS))
    mid = _msg(store, clock, ts_ms=now - 2 * DAY_MS, ext="withmedia", media=[{"media_id": 7, "state": "ready"}])
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["media_expired"] == 1 and not f.exists()
    assert store.con.execute("SELECT status FROM media WHERE id=7").fetchone()[0] == "expired"
    row = store.get_message(mid)
    assert json.loads(row["media_json"])[0]["state"] == "expired"     # 消息行仍在,只是图取不到


def test_orphan_media_rows_deleted_after_grace(store, clock, tmp_path):
    now = clock()
    store.con.execute("INSERT INTO media(id, sha256, kind, mime, size, rel_path, status, ref_count, first_seen_ms) "
                      "VALUES (8,'orphan','image','image/png',1,'media/202609/orphan','pending',0,?)", (now - 2 * DAY_MS,))
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["media_rows"] == 1
    assert store.con.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


# ---------------------------------------------------------------- ③④ 文件类
def test_raw_ref_files_removed_and_column_nulled(store, clock, tmp_path):
    now = clock()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.json").write_text("{}")
    mid = _msg(store, clock, ts_ms=now - 10 * DAY_MS, ext="raw1", raw_ref="raw/a.json")
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["raw_files"] == 1 and not (tmp_path / "raw" / "a.json").exists()
    assert store.get_message(mid)["raw_ref"] is None


def test_mail_archive_eml_removed_but_row_kept(store, clock, tmp_path):
    now = clock()
    (tmp_path / "mail" / "archive").mkdir(parents=True)
    (tmp_path / "mail" / "archive" / "1.eml").write_text("x")
    store.con.execute(
        "INSERT INTO mail_inbox(id, mailbox, protocol, folder, uid, uidvalidity, rfc_message_id, from_addr, subject, "
        "body_sha256, received_ms, status, archived_path, archived_ms) "
        "VALUES (1,'ops@x','imap','INBOX',1,1,'m1','a@x','s','h1',?,'RECEIVED','mail/archive/1.eml',?)",
        (now, now - 10 * DAY_MS))
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["mail_archive"] == 1
    row = store.con.execute("SELECT archived_path FROM mail_inbox WHERE id=1").fetchone()
    assert row is not None and row[0] is None


# ---------------------------------------------------------------- ⑤ R6-1:POP3 uidl 先并进游标再删行
def test_pop3_never_delete_uidls_merged_into_cursor_before_row_deleted(store, clock, tmp_path):
    now = clock()
    old = now - 40 * DAY_MS
    rows = [(1, "u-oversize", "OVERSIZE"), (2, "u-outofscope", "OUT_OF_SCOPE"), (3, "u-normal", "RECEIVED")]
    for rid, uidl, status in rows:
        store.con.execute(
            "INSERT INTO mail_inbox(id, mailbox, protocol, folder, uidl, rfc_message_id, from_addr, subject, body_sha256, "
            "received_ms, status) VALUES (?,?,'pop3','',?,?,?,?,?,?,?)",
            (rid, "ops@x", uidl, f"m{rid}", "a@x", "s", f"h{rid}", old, status))
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["mail_inbox"] == 3
    cur = store.cursor_get("mail:ops@x", "pop3_uidl_recent")
    kept = json.loads(cur.value)
    assert set(kept) == {"u-oversize", "u-outofscope"}          # 只有 NEVER_DELETE 两类进游标
    assert store.con.execute("SELECT COUNT(*) FROM mail_inbox").fetchone()[0] == 0


def test_pop3_uidl_merge_keeps_never_delete_first_when_over_cap(store, clock, tmp_path):
    from qtrade_agent.maintenance import POP3_UIDL_RECENT_MAX
    now = clock()
    store.cursor_set("mail:ops@x", "pop3_uidl_recent", None, json.dumps([f"old{i}" for i in range(POP3_UIDL_RECENT_MAX)]))
    store.con.execute(
        "INSERT INTO mail_inbox(id, mailbox, protocol, folder, uidl, rfc_message_id, from_addr, subject, body_sha256, "
        "received_ms, status) VALUES (1,'ops@x','pop3','','u-keep','m1','a@x','s','h1',?,'OVERSIZE')", (now - 40 * DAY_MS,))
    _svc(store, clock, tmp_path).cleanup_once(now_ms=now)
    kept = json.loads(store.cursor_get("mail:ops@x", "pop3_uidl_recent").value)
    assert len(kept) == POP3_UIDL_RECENT_MAX and kept[0] == "u-keep"     # 新进来的排前 = 最后被挤


def test_row_class_tables_purged(store, clock, tmp_path):
    now = clock()
    old = now - 40 * DAY_MS
    store.insert_audit(kind="api", transport="http", actor="token:console", action="x", now_ms=old)
    store.insert_command(trace_id="t-old", account_id="qd01", op="send_text", args_json="{}", idempotency_key=None,
                         confirm=True, timeout_ms=1000, transport="http", actor="token:console", ip=None, now_ms=old)
    store.con.execute("INSERT INTO health_samples(ts_ms, resolution, scope, subject) VALUES (?,'raw','wsl','wsl')",
                      (now - 2 * DAY_MS,))
    store.con.execute("INSERT INTO jobs(job_id, kind, actor, state, created_ms, updated_ms) VALUES ('j1','diagnostics','x','succeeded',?,?)",
                      (now - 10 * DAY_MS, now - 10 * DAY_MS))
    s = _svc(store, clock, tmp_path)
    rep = s.cleanup_once(now_ms=now)
    assert rep.deleted["audit_log"] >= 1 and rep.deleted["commands"] == 1
    assert rep.deleted["health_raw"] == 1 and rep.deleted["jobs"] == 1


def test_ws_outbox_rows_are_not_touched_by_this_cleanup(store, clock, tmp_path):
    """``target='ws'`` 的保留归 app 的 outbox_ws_retention(``[events] ws_retention_hours``),这里只清 webhook 副本。"""
    events = Events(store)
    old = clock() - 10 * DAY_MS
    events.emit("alert", payload={"code": "X"}, now_ms=old)
    store.insert_outbox_event(event_id="e1", target="webhook:wh1", event="alert", trace_id=None, account_id=None,
                              channel=None, payload_json="{}", now_ms=old)
    rep = _svc(store, clock, tmp_path).cleanup_once(now_ms=clock())
    assert rep.deleted["events_outbox_webhook"] == 1
    assert store.con.execute("SELECT COUNT(*) FROM events_outbox WHERE target='ws'").fetchone()[0] == 1


# ---------------------------------------------------------------- ⑥ cores
def test_cores_dir_purged_by_30_days(store, clock, tmp_path):
    cores = tmp_path / "cores"
    cores.mkdir()
    old, fresh = cores / "old.core", cores / "fresh.core"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    now = clock()
    os.utime(old, ((now - 40 * DAY_MS) / 1000, (now - 40 * DAY_MS) / 1000))
    os.utime(fresh, (now / 1000, now / 1000))
    rep = _svc(store, clock, tmp_path).cleanup_once(now_ms=now)
    assert rep.deleted["cores"] == 1 and not old.exists() and fresh.exists()


# ---------------------------------------------------------------- 三级水位
def test_warn_level_only_alerts_and_does_not_block(store, clock, tmp_path):
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    s = _svc(store, clock, tmp_path, free=3000, alerts=alerts)
    snap = s.check_watermark()
    assert snap["level"] == LEVEL_WARN and snap["actions"] == []
    assert s.media_downloads_allowed and s.ingest_allowed and s.exports_allowed
    payload = store.list_events(event="alert")[-1]["payload"]
    assert payload["code"] == H12_DISK_LOW and payload["severity"] == "warn"
    assert set(payload["evidence"]) >= {"free_mb", "db_size_mb", "media_size_mb"}     # §3.7:三数必带


def test_high_level_pauses_media_and_archive_and_cleans_now(store, clock, tmp_path):
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    s = _svc(store, clock, tmp_path, free=1500, alerts=alerts)
    snap = s.check_watermark()
    assert snap["level"] == LEVEL_HIGH and tuple(snap["actions"]) == ACTIONS_HIGH
    assert not s.media_downloads_allowed and not s.mail_archive_allowed
    assert s.ingest_allowed                                       # high 还不暂停采集入库
    assert store.list_events(event="alert")[-1]["payload"]["severity"] == "crit"      # high 起升 crit
    assert "cleanup" in snap


def test_critical_shrinks_retention_30_14_7_and_finally_alerts_db_write_failed(store, clock, tmp_path):
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    s = _svc(store, clock, tmp_path, free=500, alerts=alerts)
    snap = s.check_watermark()
    assert snap["level"] == LEVEL_CRITICAL and tuple(snap["actions"]) == ACTIONS_CRITICAL
    assert not s.ingest_allowed and not s.exports_allowed
    shrinks = [json.loads(a["detail_json"])["retention_days"] for a in store.list_audit("retention.shrink")]
    assert shrinks == [30, 14, 7]                                  # 上限 3 档
    assert s.retention_days_effective == 7 and snap["retention_shrunk_to"] == 7
    assert alerts.is_firing(DB_WRITE_FAILED, "wsl")


def test_recovery_resolves_the_alert(store, clock, tmp_path):
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    s = _svc(store, clock, tmp_path, free=1500, alerts=alerts)
    s.check_watermark()
    s.disk.free = 99_999
    snap = s.check_watermark()
    assert snap["level"] == LEVEL_NORMAL and not alerts.is_firing(H12_DISK_LOW, "wsl")


# ---------------------------------------------------------------- DISK_FULL
def test_is_disk_full_error_recognises_the_three_shapes():
    assert is_disk_full_error(sqlite3.OperationalError("database or disk is full"))
    assert is_disk_full_error(sqlite3.OperationalError("disk I/O error"))
    assert is_disk_full_error(OSError(28, "No space left on device"))
    assert not is_disk_full_error(sqlite3.OperationalError("no such table: x"))
    assert not is_disk_full_error(ValueError("nope"))


def test_guard_write_raises_disk_full_with_three_numbers_and_alerts(store, clock, tmp_path):
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    s = _svc(store, clock, tmp_path, free=800, alerts=alerts)
    with pytest.raises(DiskFullError) as e:
        with s.guard_write("store.ingest"):
            raise sqlite3.OperationalError("database or disk is full")
    err = e.value
    assert err.code == "DISK_FULL" and err.http_status == 507
    assert err.retryable is False and err.needs_human is True
    assert "剩余 800 MB" in err.message
    assert set(err.evidence()) == {"free_mb", "db_size_mb", "media_size_mb"}
    assert list(DiskFullError.hint_actions) == ["open_env", "run_cleanup"]
    assert alerts.is_firing(DB_WRITE_FAILED, "wsl")
    assert s.level == LEVEL_CRITICAL                               # 命中即按 critical 动作走一遍


def test_guard_write_passes_through_non_disk_errors(store, clock, tmp_path):
    s = _svc(store, clock, tmp_path)
    with pytest.raises(ValueError):
        with s.guard_write("x"):
            raise ValueError("别的错")


# ---------------------------------------------------------------- VACUUM / 备份
def test_incremental_vacuum_only_on_sunday(store, clock, tmp_path):
    s = _svc(store, clock, tmp_path)
    sunday = 1_758_412_800_000        # 2025-09-21 是周日(Asia/Shanghai)
    assert s.incremental_vacuum(now_ms=sunday) is True
    assert s.incremental_vacuum(now_ms=sunday + DAY_MS) is False
    assert s.incremental_vacuum(now_ms=sunday + DAY_MS, force=True) is True


def test_backup_once_writes_dated_file_and_keeps_n(store, clock, tmp_path):
    s = _svc(store, clock, tmp_path, backup=BackupConfig(backup_dir=str(tmp_path / "backup"), backup_keep=2))
    path = s.backup_once(now_ms=clock())
    assert os.path.exists(path) and os.path.basename(path).startswith("agent-")
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    for day in ("20260101", "20260102", "20260103"):
        open(os.path.join(str(tmp_path / "backup"), f"agent-{day}.db"), "w").close()
    assert s.prune_backups() == 2
    left = sorted(os.listdir(str(tmp_path / "backup")))
    assert len(left) == 2 and left[-1].startswith("agent-")
    assert [a["action"] for a in store.list_audit("db.backup")] == ["db.backup"]
