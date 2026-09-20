"""邮件定期删除 —— 规格:docs/06 §2.6 全节;重点是 🔴 **五处 ``NEVER_DELETE`` 门**(R6-1 + R6-26)与三种触发。"""
from __future__ import annotations

import json
import os

from qtrade_agent.mail.cleanup import TRIGGER_CAPACITY, TRIGGER_MAX_KEPT
from qtrade_agent.mail.codes import DONE, OUT_OF_SCOPE, OVERSIZE, RECEIPT_SENT
from test_mail_common import make_cfg, make_env

DAY = 86400_000


def env_for(store, clock, tmp_path, *, protocol="imap", **cleanup_kw):
    cfg = make_cfg(protocol=protocol, archive_dir=str(tmp_path / "archive"))
    for k, v in cleanup_kw.items():
        setattr(cfg.cleanup, k, v)
    return make_env(store, clock, cfg=cfg)


def seed_imap(env, *, status=RECEIPT_SENT, age_days=30, size=None, folder="INBOX", subject="QTRADE指令 v1"):
    """在假服务器与 ``mail_inbox`` 里各放一封同 UID 的信。"""
    raw = f"Subject: {subject}\r\nFrom: ops@corp.example\r\nMessage-ID: <{status}-{size}-{age_days}@x>\r\n\r\nbody".encode()
    uid = env.imap.add(raw, folder=folder)
    iid = env.ms.inbox_insert(mailbox=env.mailbox, protocol="imap", folder=folder, uid=uid, uidvalidity=1,
                              rfc_message_id=f"<{status}-{size}-{age_days}-{uid}@x>", from_addr="ops@corp.example",
                              subject=subject, received_ms=env.service.clock() - age_days * DAY,
                              size_bytes=len(raw) if size is None else size,
                              body_sha256=f"sha-{uid}", status=status)
    return iid, uid


def seed_pop3(env, *, status=RECEIPT_SENT, age_days=30, size=None, uidl=None):
    uidl = uidl or f"U{len(env.pop3.messages) + 1}"
    raw = f"Subject: QTRADE指令 v1\r\nFrom: ops@corp.example\r\n\r\nbody-{uidl}".encode()
    env.pop3.add(uidl, raw, size=size)
    size = size if size is not None else len(raw)
    iid = env.ms.inbox_insert(mailbox=env.mailbox, protocol="pop3", folder="", uidl=uidl,
                              rfc_message_id=f"<{uidl}@x>", from_addr="ops@corp.example",
                              subject="QTRADE指令 v1", received_ms=env.service.clock() - age_days * DAY,
                              size_bytes=size, body_sha256=f"sha-{uidl}", status=status)
    return iid, uidl


# ================================================================ §2.6.1 eligible
def test_retention_round_archives_then_deletes(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=7)
    iid, uid = seed_imap(env, age_days=30)
    env.imap.select("INBOX")
    stats = env.cleaner.run(env.imap, "imap")
    row = env.ms.inbox_get(iid)
    assert stats.candidates == 1 and stats.archived == 1 and stats.deleted == 1
    assert row["archived_path"] and os.path.exists(row["archived_path"])
    assert os.path.exists(os.path.join(os.path.dirname(row["archived_path"]), f"{iid}.json"))   # sidecar
    assert row["deleted_ms"] is not None and uid in env.imap.expunged


def test_row_not_yet_old_enough_is_not_touched(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=7)
    seed_imap(env, age_days=3)
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.candidates == 0 and env.imap.expunged == []


def test_non_terminal_status_is_not_a_candidate(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0)
    seed_imap(env, status="ACCEPTED", age_days=30)
    assert env.cleaner.run(env.imap, "imap").candidates == 0


def test_done_with_queued_receipt_is_not_deleted(store, clock, tmp_path):
    """§2.6.1 末:``DONE`` 但回执还在 ``mail_outbox`` 队列里(QUEUED/RETRY)的不删。"""
    env = env_for(store, clock, tmp_path, retention_days=0)
    iid, _ = seed_imap(env, status=DONE, age_days=30)
    env.ms.outbox_enqueue(kind="receipt", to_addrs="ops@corp.example", subject="QTRADE回执 v1",
                          body_text="x", rfc_message_id="<r1@qtrade.local>", dedup_key=f"receipt:{iid}",
                          template_version="v1", ref_inbox_id=iid)
    assert env.cleaner.run(env.imap, "imap").candidates == 0


# ================================================================ 🔴 门 ①:eligible 里的 NEVER_DELETE
def test_oversize_and_out_of_scope_never_leave_the_server(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0)
    over, over_uid = seed_imap(env, status=OVERSIZE, age_days=90, size=30 * 1024 * 1024)
    oos, oos_uid = seed_imap(env, status=OUT_OF_SCOPE, age_days=90, subject="午餐订餐")
    normal, normal_uid = seed_imap(env, status=RECEIPT_SENT, age_days=90)
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.deleted == 1 and normal_uid in env.imap.expunged
    assert over_uid not in env.imap.expunged and oos_uid not in env.imap.expunged
    assert env.ms.inbox_get(over)["deleted_ms"] is None and env.ms.inbox_get(oos)["deleted_ms"] is None
    assert env.imap.moved == []                       # 也没被搬进 processed_folder
    # 🔴 R6-26:门生效的唯一可观测证据
    assert stats.detail["skipped_oversize"] == 1 and stats.detail["skipped_out_of_scope"] == 1
    log = env.ms.cleanup_log_list()[0]
    assert json.loads(log["detail_json"])["skipped_oversize"] == 1


# ================================================================ 🔴 门 ②:容量水位
def test_capacity_watermark_deletes_oldest_but_never_the_protected(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=3650, mailbox_quota_watermark=0.8)
    env.imap.quota_used, env.imap.quota_limit = 900, 1000
    over, over_uid = seed_imap(env, status=OVERSIZE, age_days=100)
    old, old_uid = seed_imap(env, status=RECEIPT_SENT, age_days=99)
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.trigger == TRIGGER_CAPACITY
    assert old_uid in env.imap.expunged and over_uid not in env.imap.expunged
    assert env.ms.inbox_get(over)["deleted_ms"] is None      # 邮箱再满也不删 OVERSIZE


def test_capacity_high_after_cleanup_alerts_instead_of_using_protected(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=3650, mailbox_quota_watermark=0.8)
    env.imap.quota_used, env.imap.quota_limit = 990, 1000
    seed_imap(env, status=OVERSIZE, age_days=100)
    env.cleaner.run(env.imap, "imap")
    assert env.alerts.is_firing("MAIL_QUOTA_HIGH", f"mailbox:{env.mailbox}")


# ================================================================ 🔴 门 ③:按条数 max_kept_count
def test_max_kept_trigger_skips_protected_rows(store, clock, tmp_path):
    """§8b:``mailbox_quota_mb_assumed=0`` ⇒ 走 ③;``max_kept_count=1``;只有那封普通信被删。"""
    env = env_for(store, clock, tmp_path, retention_days=3650, mailbox_quota_mb_assumed=0, max_kept_count=1)
    env.imap.caps.discard("QUOTA")
    normal, normal_uid = seed_imap(env, status=RECEIPT_SENT, age_days=10)
    normal2, normal2_uid = seed_imap(env, status=RECEIPT_SENT, age_days=5)
    over, over_uid = seed_imap(env, status=OVERSIZE, age_days=100)
    oos, oos_uid = seed_imap(env, status=OUT_OF_SCOPE, age_days=100, subject="午餐订餐")
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.trigger == TRIGGER_MAX_KEPT
    assert normal_uid in env.imap.expunged                     # kept(2) - max_kept(1) = 1 封,删最旧
    assert normal2_uid not in env.imap.expunged
    assert over_uid not in env.imap.expunged and oos_uid not in env.imap.expunged
    log = env.ms.cleanup_log_list()[0]
    assert log["trigger"] == "max_kept"                         # 02 DDL 的 CHECK 收得下(R6-31 补的枚举值)
    assert json.loads(log["detail_json"])["skipped_oversize"] == 1
    assert json.loads(log["detail_json"])["skipped_out_of_scope"] == 1


def test_kept_count_excludes_protected_from_the_denominator(store, clock, tmp_path):
    """🔴 R6-26:分母也不含这两类 —— 它们永不删,算进分母只会把别的信提前删掉。"""
    env = env_for(store, clock, tmp_path, max_kept_count=2)
    seed_imap(env, status=OVERSIZE)
    seed_imap(env, status=OUT_OF_SCOPE, subject="午餐订餐")
    seed_imap(env, status=RECEIPT_SENT)
    assert env.ms.inbox_kept_count(mailbox=env.mailbox) == 1


# ================================================================ 🔴 门 ④:IMAP 终态 MOVE
def test_on_terminal_moves_normal_mail_only(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path)
    iid, uid = seed_imap(env)
    env.cleaner.on_terminal(env.imap, env.ms.inbox_get(iid))
    row = env.ms.inbox_get(iid)
    assert row["folder"] == "QTrade/processed" and row["uid"] != uid
    assert env.imap.uids_in("INBOX") == []


def test_on_terminal_leaves_protected_in_place(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path)
    for status, subject in ((OVERSIZE, "QTRADE指令 v1"), (OUT_OF_SCOPE, "午餐订餐")):
        iid, uid = seed_imap(env, status=status, subject=subject)
        env.cleaner.on_terminal(env.imap, env.ms.inbox_get(iid))
        row = env.ms.inbox_get(iid)
        assert row["folder"] == "INBOX" and row["uid"] == uid          # 原夹原 UID 不变
    assert env.imap.moved == [] and env.imap.search_deleted() == []    # 不 MOVE/COPY、不留 \Deleted


def test_move_falls_back_to_copy_plus_deleted_without_move_capability(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path)
    env.imap.caps.discard("MOVE")
    iid, uid = seed_imap(env)
    env.cleaner.on_terminal(env.imap, env.ms.inbox_get(iid))
    assert env.imap.uids_in("QTrade/processed")
    env.imap.select("INBOX")
    assert uid in env.imap.search_deleted()


# ================================================================ §2.6.5 无 UIDPLUS 的收紧
def test_without_uidplus_only_expunges_the_dedicated_folder(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0)
    env.imap.caps.discard("UIDPLUS")
    iid, uid = seed_imap(env)
    env.cleaner.on_terminal(env.imap, env.ms.inbox_get(iid))          # 先搬进 QTrade/processed
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.deleted == 1 and env.ms.inbox_get(iid)["deleted_ms"] is not None


def test_without_uidplus_foreign_deleted_flag_defers_expunge(store, clock, tmp_path):
    """99c 收紧:人用 Outlook 在同一夹标删的**别人的邮件**不能被我们连带真删。"""
    env = env_for(store, clock, tmp_path, retention_days=0)
    env.imap.caps.discard("UIDPLUS")
    iid, uid = seed_imap(env)
    foreign = env.imap.add("Subject: 别人的信\r\n\r\nx".encode())
    env.imap.select("INBOX")
    env.imap.store_deleted(foreign)                                    # 别人标删的,未清
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.deleted == 0 and foreign not in env.imap.expunged
    assert any(d.get("action") == "DELETE_DEFERRED" for d in stats.detail["items"])
    assert env.ms.inbox_get(iid)["deleted_ms"] is None


# ================================================================ 🔴 门 ⑤:POP3 cleanup_pop3
def test_pop3_deletes_only_after_quit_ok(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, protocol="pop3", retention_days=0)
    iid, uidl = seed_pop3(env)
    stats = env.cleaner.run(env.pop3, "pop3")
    assert stats.deleted == 1 and uidl in env.pop3.deleted
    assert env.ms.inbox_get(iid)["deleted_ms"] is not None


def test_pop3_quit_failure_rolls_everything_back(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, protocol="pop3", retention_days=0)
    iid, uidl = seed_pop3(env)
    env.pop3.quit_ok = False
    stats = env.cleaner.run(env.pop3, "pop3")
    assert stats.deleted == 0 and env.pop3.deleted == []
    assert env.ms.inbox_get(iid)["deleted_ms"] is None                 # deleted_ms 只在 QUIT +OK 后写
    assert any(d.get("action") == "DELETE_FAILED" for d in stats.detail["items"])


def test_pop3_never_deletes_protected(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, protocol="pop3", retention_days=0)
    over, over_uidl = seed_pop3(env, status=OVERSIZE, size=30 * 1024 * 1024)
    oos, oos_uidl = seed_pop3(env, status=OUT_OF_SCOPE)
    normal, normal_uidl = seed_pop3(env)
    env.cleaner.run(env.pop3, "pop3")
    assert env.pop3.deleted == [normal_uidl]
    assert over_uidl in env.pop3.uidls() and oos_uidl in env.pop3.uidls()


def test_gone_on_server_is_not_a_failure(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, protocol="pop3", retention_days=0)
    iid, uidl = seed_pop3(env)
    env.pop3.messages = []                                             # 人先在网页端删了
    stats = env.cleaner.run(env.pop3, "pop3")
    assert env.ms.inbox_get(iid)["reason"] == "gone_on_server" and stats.failed == 0


# ================================================================ §2.6.2 归档校验 / §2.6.9 失败
def test_archive_size_mismatch_blocks_deletion(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0)
    iid, uid = seed_imap(env, size=999999)                             # size_bytes 与真实字节数对不上
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.archived == 0 and stats.failed == 1 and stats.deleted == 0
    assert env.ms.inbox_get(iid)["archived_path"] is None
    assert uid not in env.imap.expunged


def test_three_failed_rounds_alert(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0, stall_alert_rounds=3)
    seed_imap(env, size=999999)
    for _ in range(3):
        env.cleaner.run(env.imap, "imap")
    assert env.alerts.is_firing("MAIL_CLEANUP_FAILED", f"mailbox:{env.mailbox}")


# ================================================================ §2.6.10 磁盘三级水位
def test_disk_high_skips_archiving_but_still_deletes(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0)
    env.cleaner.disk_state = lambda: "high"
    iid, uid = seed_imap(env)
    stats = env.cleaner.run(env.imap, "imap")
    row = env.ms.inbox_get(iid)
    assert stats.archived == 0 and row["archived_path"] is None
    assert "archive_skipped_disk_high" in row["reason"]
    assert row["deleted_ms"] is not None


def test_disk_critical_skips_the_whole_round(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0)
    env.cleaner.disk_state = lambda: "critical"
    iid, uid = seed_imap(env)
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.paused_disk and stats.deleted == 0 and env.imap.expunged == []
    assert env.ms.inbox_get(iid)["deleted_ms"] is None
    assert env.alerts.is_firing("MAIL_PAUSED_DISK_FULL", f"mailbox:{env.mailbox}")


# ================================================================ §2.6.7 归档滚动 + 行清理
def test_archive_rotation_removes_old_files_and_clears_path(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0, archive_retention_days=7)
    iid, _ = seed_imap(env)
    env.cleaner.run(env.imap, "imap")
    path = env.ms.inbox_get(iid)["archived_path"]
    assert os.path.exists(path)
    old = os.stat(path).st_mtime - 8 * 86400
    os.utime(path, (old, old))
    stats = env.cleaner.rotate_archive()
    assert stats.archive_rotated_files >= 1 and not os.path.exists(path)
    assert env.ms.inbox_get(iid)["archived_path"] is None               # 归档已过期,行还在
    assert env.ms.cleanup_log_list()[0]["trigger"] == "archive_rotation"


def test_row_purge_keeps_protected_uidls_in_the_same_transaction(store, clock, tmp_path):
    """🔴 R6-26 两条硬约束:与删行**同事务**写 ``pop3_uidl_recent``;LRU 挤出时 NEVER_DELETE 的 uidl 最后被挤。"""
    env = env_for(store, clock, tmp_path, protocol="pop3")
    env.cfg.retention.mail_inbox_rows_days = 0
    over, over_uidl = seed_pop3(env, status=OVERSIZE, age_days=40)
    oos, oos_uidl = seed_pop3(env, status=OUT_OF_SCOPE, age_days=40)
    plain, plain_uidl = seed_pop3(env, status=RECEIPT_SENT, age_days=40)
    assert env.cleaner.purge_rows() == 3
    recent = env.ms.uidl_recent(env.fetcher.owner)
    assert {over_uidl, oos_uidl, plain_uidl} <= set(recent)
    # 撑满 LRU:普通 uidl 先被挤,两个受保护的仍在
    for i in range(2100):
        env.ms.uidl_remember(env.fetcher.owner, f"junk{i}")
    recent = env.ms.uidl_recent(env.fetcher.owner)
    assert len(recent) == 2000
    assert over_uidl in recent and oos_uidl in recent and plain_uidl not in recent


def test_purged_protected_rows_do_not_reappear_next_round(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, protocol="pop3")
    env.cfg.retention.mail_inbox_rows_days = 0
    over, over_uidl = seed_pop3(env, status=OVERSIZE, size=30 * 1024 * 1024, age_days=40)
    env.cleaner.purge_rows()
    assert env.inbox_rows() == []
    stats = env.fetcher.run_once()
    assert stats.oversize == 0 and stats.skipped == 1                   # 不重新登记、不重新告警
    assert env.inbox_rows() == []
    assert over_uidl in env.pop3.uidls()                                # 原信始终在服务器上


# ================================================================ §2.6.6 周期与并发互斥
def test_manual_flag_is_only_a_flag(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path)
    env.cleaner.manual_requested = True
    assert env.cleaner.manual_requested is True                         # 由下一轮线程执行(§2.6.6)


def test_cleanup_disabled_does_nothing(store, clock, tmp_path):
    env = env_for(store, clock, tmp_path, retention_days=0, enabled=False)
    seed_imap(env)
    stats = env.cleaner.run(env.imap, "imap")
    assert stats.candidates == 0 and env.imap.expunged == []
