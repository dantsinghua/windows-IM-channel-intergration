"""取信循环 —— 规格:docs/06 §2.1(两条循环 + SIZE 门 R5-6)、§2.1.1(IMAP → POP3 回落 E-1)、§2.6.10(磁盘水位)、§5。"""
from __future__ import annotations

from qtrade_agent.mail.backends import MailAuthError, MailConnectError
from qtrade_agent.mail.codes import MAIL_MSG_OVERSIZE, MAIL_PAUSED_DISK_FULL, MAIL_PROTOCOL_FALLBACK, OVERSIZE
from test_mail_common import BOX_ADDR, command_mail, make_cfg, make_env, make_mail

MB = 1024 * 1024


def env_imap(store, clock, **kw):
    return make_env(store, clock, cfg=make_cfg(protocol="imap", **kw))


def env_pop3(store, clock, **kw):
    return make_env(store, clock, cfg=make_cfg(protocol="pop3", **kw))


def fire_count(env, code: str) -> int:
    import json
    n = 0
    for e in env.events._store.list_events(event="mail"):      # 02 §3.7:MAIL_* 的事件族是 mail,不是 alert
        p = json.loads(e["payload_json"])
        if p["code"] == code and p["state"] == "firing":
            n += 1
    return n


# ---------------------------------------------------------------- §2.1 IMAP run_cycle()
def test_imap_cycle_ingests_and_advances_watermark(store, clock):
    env = env_imap(store, clock)
    uid = env.imap.add(command_mail(req_id="r1"))
    stats = env.fetcher.run_once()
    assert stats.fetched == 1
    rows = env.inbox_rows()
    assert len(rows) == 1 and rows[0]["uid"] == uid and rows[0]["protocol"] == "imap"
    uv, last = env.ms.imap_watermark(env.fetcher.owner, "INBOX")
    assert last == uid and uv == env.imap.uidvalidity


def test_imap_search_returns_max_uid_again_but_is_filtered(store, clock):
    """IMAP 规定 ``n:*`` 至少返回最大 UID —— 再过滤一次 ``> last_uid``(ibquote process_folder.py:35)。"""
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="r1"))
    env.fetcher.run_once()
    stats = env.fetcher.run_once()
    assert stats.fetched == 0 and len(env.inbox_rows()) == 1


def test_imap_scans_junk_folder_too(store, clock):
    """ibquote 教训:反垃圾网关把摆渡邮件判成垃圾,不扫 Junk 就漏单。"""
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="r2", message_id="<junk@x>"), folder="Junk")
    assert env.fetcher.run_once().fetched == 1
    assert env.inbox_rows()[0]["folder"] == "Junk"


def test_uidvalidity_change_rescans_but_dedup_keys_block_duplicates(store, clock):
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="r1"))
    env.fetcher.run_once()
    env.imap.uidvalidity = 99                   # 水位作废,全量重扫
    stats = env.fetcher.run_once()
    assert stats.skipped + stats.fetched >= 0
    assert len(env.inbox_rows()) == 1           # 靠 Message-ID / 正文哈希挡重复


def test_max_retr_per_round_caps_the_batch(store, clock):
    env = env_imap(store, clock, max_retr_per_round=2)
    for i in range(5):
        env.imap.add(command_mail(req_id=f"r{i}", message_id=f"<m{i}@x>"))
    assert env.fetcher.run_once().fetched == 2
    assert env.fetcher.run_once().fetched == 2


# ---------------------------------------------------------------- 🔴 R5-6 SIZE 门 + R6-1 OVERSIZE
def test_imap_oversize_never_reads_body(store, clock):
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="big"), size=30 * MB)
    env.imap.raise_on_fetch = AssertionError("OVERSIZE 分支绝不能 FETCH BODY.PEEK[]")
    stats = env.fetcher.run_once()
    row = env.inbox_rows()[0]
    assert stats.oversize == 1 and row["status"] == OVERSIZE
    assert row["body_text"] is None and row["size_bytes"] == 30 * MB
    assert row["from_addr"] == "ops@corp.example"        # 只登记元数据(头里拿得到)
    assert fire_count(env, MAIL_MSG_OVERSIZE) == 1


def test_oversize_alerts_only_once_over_three_rounds(store, clock):
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="big"), size=30 * MB)
    for _ in range(3):
        env.fetcher.run_once()
    assert fire_count(env, MAIL_MSG_OVERSIZE) == 1       # 水位已推进,下轮 SEARCH 搜不到它
    assert len(env.inbox_rows()) == 1


def test_oversize_row_is_not_moved_or_deleted(store, clock):
    """🔴 R6-1:OVERSIZE ∈ NEVER_DELETE —— 不进 MOVE 队列,留原夹原位。"""
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="big"), size=30 * MB)
    env.fetcher.run_once()
    assert env.imap.moved == [] and env.imap.expunged == []
    assert env.imap.uids_in("INBOX") == [1]
    assert env.inbox_rows()[0]["deleted_ms"] is None


def test_pop3_oversize_never_retrs(store, clock):
    env = env_pop3(store, clock)
    env.pop3.add("U1", command_mail(req_id="big"), size=30 * MB)
    stats = env.fetcher.run_once()
    row = env.inbox_rows()[0]
    assert stats.oversize == 1 and row["status"] == OVERSIZE and row["uidl"] == "U1"
    assert env.pop3.deleted == []                       # 本轮不 DELE、往后每轮也不 DELE
    for _ in range(2):
        env.fetcher.run_once()
    assert fire_count(env, MAIL_MSG_OVERSIZE) == 1      # 靠 (mailbox, uidl) 唯一键「已见」只告警一次
    assert len(env.inbox_rows()) == 1


# ---------------------------------------------------------------- §2.1 POP3 循环
def test_pop3_cycle_and_seen_skip(store, clock):
    env = env_pop3(store, clock)
    env.pop3.add("U1", command_mail(req_id="r1"))
    assert env.fetcher.run_once().fetched == 1
    stats = env.fetcher.run_once()
    assert stats.fetched == 0 and stats.skipped == 1
    assert env.ms.pop3_stat(env.fetcher.owner)["count"] == 1


# ---------------------------------------------------------------- §2.6.10 磁盘三级水位
def test_disk_critical_pauses_fetch_and_keeps_watermark(store, clock):
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="r1"))
    state = {"v": "critical"}
    env.fetcher.disk_state = lambda: state["v"]
    stats = env.fetcher.run_once()
    assert stats.paused_disk and env.inbox_rows() == []
    assert env.ms.imap_watermark(env.fetcher.owner, "INBOX") == (None, 0)   # 水位一步不动
    assert fire_count(env, MAIL_PAUSED_DISK_FULL) == 1
    state["v"] = "ok"                                    # 恢复即补收
    assert env.fetcher.run_once().fetched == 1


# ---------------------------------------------------------------- §2.1.1 IMAP → POP3 回落
def test_three_connect_failures_trigger_fallback(store, clock):
    env = env_imap(store, clock)
    env.imap.raise_on_connect = MailConnectError("refused", kind="connect_refused")
    for _ in range(2):
        env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "imap"       # after_failures = 3
    env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "pop3"
    assert fire_count(env, MAIL_PROTOCOL_FALLBACK) == 1
    st = env.fetcher.status()
    # 第六批:#56 inbound 段按 02 #56 改名(protocol_configured/protocol_active/fallback{since_at,reason});判据不松
    assert st["protocol_configured"] == "imap" and st["protocol_active"] == "pop3"
    assert st["fallback"]["since_at"] is not None and st["fallback"]["reason"]


def test_auth_failure_never_falls_back(store, clock):
    """§2.1.1:密码在 IMAP 上错,在 POP3 上一样错;回落只会把一次告警变成两次。"""
    env = env_imap(store, clock)
    env.imap.raise_on_connect = MailAuthError("NO [AUTHENTICATIONFAILED]")
    for _ in range(5):
        env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "imap"
    assert fire_count(env, MAIL_PROTOCOL_FALLBACK) == 0


def test_no_fallback_host_means_alert_only(store, clock):
    cfg = make_cfg(protocol="imap")
    cfg.inbound.fallback.host = ""                        # host 空 ⇒ 不回落只告警 MAIL_INBOUND_STALLED
    env = make_env(store, clock, cfg=cfg)
    env.imap.raise_on_connect = MailConnectError("refused", kind="connect_refused")
    for _ in range(4):
        env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "imap"
    assert fire_count(env, "MAIL_INBOUND_STALLED") >= 1


def test_recheck_switches_back_to_imap_and_resolves(store, clock):
    env = env_imap(store, clock)
    env.imap.raise_on_connect = MailConnectError("refused", kind="connect_refused")
    for _ in range(3):
        env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "pop3"
    env.pop3.add("U1", command_mail(req_id="r1"))
    env.fetcher.run_once()                                # 回落期间照常 POP3 收信
    assert len(env.inbox_rows()) == 1
    env.imap.raise_on_connect = None
    clock.advance(31 * 60 * 1000)                         # fallback.recheck_min = 30
    env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "imap"
    assert not env.alerts.is_firing(MAIL_PROTOCOL_FALLBACK, f"mailbox:{env.mailbox}")


def test_pop3_configured_never_upgrades_to_imap(store, clock):
    """§2.1.1 末:配置为 pop3 就是 pop3,不存在「POP3 → IMAP 升级」。"""
    env = env_pop3(store, clock)
    for _ in range(5):
        env.fetcher.run_once()
    assert env.fetcher.effective_protocol == "pop3"


# ---------------------------------------------------------------- §2.6.4 范围外
def test_out_of_scope_mail_is_registered_without_body(store, clock):
    env = env_imap(store, clock)
    env.imap.add(make_mail("这是一封无关的信", subject="周报", message_id="<x@y>"))
    env.fetcher.run_once()
    row = env.inbox_rows()[0]
    assert row["status"] == "OUT_OF_SCOPE" and row["body_text"] is None


def test_status_reports_folder_watermarks(store, clock):
    env = env_imap(store, clock)
    env.imap.add(command_mail(req_id="r1"))
    env.fetcher.run_once()
    st = env.fetcher.status()
    names = [f["name"] for f in st["folders"]]
    assert names == ["INBOX", "Junk"] and st["last_success_at"] is not None
    assert st["consecutive_failures"] == 0


def test_disabled_mail_starts_no_reader_thread(store, clock):
    """§7 ``[mail] enabled=false``:收/发/清理线程都不起 ⇒ 连 fetcher 都不该有。"""
    cfg = make_cfg()
    cfg.enabled = False
    env = make_env(store, clock, cfg=cfg)
    env.imap.add(command_mail(req_id="r1"))
    assert env.service.fetchers == {}
    assert env.service.fetch_once() == {} and env.inbox_rows() == []
