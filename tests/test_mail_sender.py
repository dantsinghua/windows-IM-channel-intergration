"""出站队列与 SMTP 投递 —— 规格:docs/06 §2.1(SMTP 发送/重试/限速)、§2.4.1(一消息一邮件/路由)、§2.4.3(触发)、§2.16.2(告警邮件)。"""
from __future__ import annotations

import email

from qtrade_agent.mail.backends import SmtpPermanentError, SmtpTemporaryError
from qtrade_agent.mail.codes import DONE, RECEIPT_SENT
from test_mail_common import BOX_ADDR, OPS_ADDR, make_cfg, make_env

CTX = {
    "account_id": "qd01", "channel": "qidian", "channel_cn": "企点", "session_id": "qd01:g_123456",
    "session_name": "固收报价群", "sender_kind": "group", "sender_name": "张三", "sender_id": "wxid_abc",
    "ts": "2025-09-19T08:00:00+08:00", "ts_cn": "2025年9月19日 08:00", "msg_type": "text", "msg_type_cn": "文本",
    "summary": "3M 1.52 可谈", "media_count": 0, "message_id": "msg_1", "ext_msg_id": "qd:1001",
    "fingerprint": "abc", "seq": 1, "seq_total": 1, "day_seq": 7, "dir": "in", "text": "3M 1.52 可谈",
    "attachments": "-", "image_kind": "", "revoked": False, "media_ref": "", "oversize": "",
    "capture_text": "", "template_version": "v1",
}


def enqueue_msg(env, **kw):
    return env.service.sender.enqueue_message(dict(CTX, **kw.pop("ctx", {})), channel=kw.pop("channel", "qidian"),
                                              account_id=kw.pop("account_id", "qd01"),
                                              ref_message_id=kw.pop("ref_message_id", "msg_1"), **kw)


# ---------------------------------------------------------------- §2.4.1 入队
def test_one_message_one_mail_dedup_key(store, clock):
    env = make_env(store, clock)
    assert enqueue_msg(env) is not None
    assert enqueue_msg(env) is None                       # 同一条消息不重复入队(dedup_key 唯一)
    assert len(env.outbox_rows()) == 1


def test_revoked_copy_is_a_separate_row(store, clock):
    """§2.4.3:撤回事件再发一封**同消息ID**且 ``是否撤回：是`` 的邮件 ⇒ 另一行。"""
    env = make_env(store, clock)
    enqueue_msg(env)
    assert enqueue_msg(env, ctx={"revoked": True}, dedup_suffix="revoked") is not None
    assert len(env.outbox_rows()) == 2
    assert "是否撤回：是" in env.outbox_rows()[1]["body_text"]


def test_channel_not_enabled_is_skipped(store, clock):
    cfg = make_cfg()
    cfg.outbound.enabled_channels = ["wechat"]
    env = make_env(store, clock, cfg=cfg)
    assert enqueue_msg(env, channel="qidian") is None and env.outbox_rows() == []


def test_unresolved_route_alerts_and_does_not_enqueue(store, clock):
    cfg = make_cfg()
    cfg.outbound.recipients = []                          # 连 default 也没有收件人
    env = make_env(store, clock, cfg=cfg)
    assert enqueue_msg(env) is None
    assert env.alerts.is_firing("MAIL_ROUTE_UNRESOLVED", "route:qidian/qd01")


def test_recipients_come_from_route_not_from_placeholders(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env, ctx={"session_name": "群\r\nBcc: attacker@x.com"})
    row = env.outbox_rows()[0]
    assert row["to_addrs"] == "ops-inbox@corp.example" and "attacker" not in row["to_addrs"]
    env.service.sender.run_once()
    frm, to, raw = env.smtp.sent[0]
    msg = email.message_from_bytes(raw)
    # 注入的 CRLF 已在 hdr_sanitize 里变空格 ⇒ 没有第二个头;投递层的收件人也只有路由那一个
    assert msg["Bcc"] is None and len(msg.get_all("Subject")) == 1
    assert to == ["ops-inbox@corp.example"] and "attacker@x.com" not in to


# ---------------------------------------------------------------- §2.1 投递
def test_successful_delivery_marks_sent_and_generates_message_id(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env)
    stats = env.service.sender.run_once()
    row = env.outbox_rows()[0]
    assert stats.sent == 1 and row["status"] == "SENT" and row["sent_ms"] is not None
    assert row["rfc_message_id"].endswith("@qtrade.local>")
    frm, to, _ = env.smtp.sent[0]
    assert frm == BOX_ADDR and to == ["ops-inbox@corp.example"]


def test_temporary_error_retries_with_backoff(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env)
    env.smtp.fail_times = 1
    env.smtp.fail_exc = SmtpTemporaryError("451 临时故障")
    stats = env.service.sender.run_once()
    row = env.outbox_rows()[0]
    assert stats.retried == 1 and row["status"] == "RETRY" and row["attempts"] == 1
    assert row["next_attempt_ms"] == clock.now_ms + 30 * 1000       # backoff_s[0] = 30
    clock.advance(31 * 1000)
    assert env.service.sender.run_once().sent == 1


def test_permanent_5xx_goes_straight_to_dead(store, clock):
    """§2.1:5xx 永久码(550/552/553)**不重试直接 DEAD** —— 重试只会重复挨拒。"""
    env = make_env(store, clock)
    enqueue_msg(env)
    env.smtp.fail_times = 1
    env.smtp.fail_exc = SmtpPermanentError("550 收件人不存在")
    stats = env.service.sender.run_once()
    row = env.outbox_rows()[0]
    assert stats.dead == 1 and row["status"] == "DEAD" and row["attempts"] == 1
    assert env.alerts.is_firing("MAIL_OUTBOX_DEAD", f"outbox:{row['id']}")


def test_max_attempts_exhausted_goes_dead(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env)
    env.smtp.fail_times = 99
    for _ in range(9):
        env.service.sender.run_once()
        clock.advance(3600 * 1000)
    row = env.outbox_rows()[0]
    assert row["status"] == "DEAD" and row["attempts"] == 9          # max_attempts=8,第 9 次置死信


def test_three_consecutive_failures_alert(store, clock):
    env = make_env(store, clock)
    for i in range(3):
        enqueue_msg(env, ref_message_id=f"msg_{i}")
    env.smtp.fail_times = 3
    env.service.sender.run_once()
    assert env.alerts.is_firing("MAIL_SMTP_FAILING", "smtp:*")


def test_one_failure_does_not_block_the_next(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env, ref_message_id="msg_a")
    enqueue_msg(env, ref_message_id="msg_b")
    env.smtp.fail_times = 1
    stats = env.service.sender.run_once()
    assert stats.retried == 1 and stats.sent == 1


def test_rate_limit_token_bucket(store, clock):
    env = make_env(store, clock)
    for i in range(8):
        enqueue_msg(env, ref_message_id=f"msg_{i}")
    stats = env.service.sender.run_once()
    assert stats.sent == 5 and stats.skipped_rate == 3               # send_burst = 5
    clock.advance(60_000)                                            # 一分钟补 20 个令牌(封顶 burst)
    assert env.service.sender.run_once().sent == 3


# ---------------------------------------------------------------- §2.3.5 回执发出后回写
def test_receipt_delivery_flips_inbox_to_receipt_sent(store, clock):
    env = make_env(store, clock)
    iid = env.ms.inbox_insert(mailbox=env.mailbox, protocol="imap", folder="INBOX", uid=1, uidvalidity=1,
                              rfc_message_id="<c1@x>", from_addr=OPS_ADDR, subject="QTRADE指令 v1",
                              received_ms=clock.now_ms, size_bytes=100, body_sha256="sha1", status=DONE)
    route = env.service.routes.lookup(None, None)
    env.service.sender.enqueue_receipt(route=route, to_addr=OPS_ADDR,
                                       ctx={"template_version": "v1", "account_id": "qd01", "op": "send_text",
                                            "req_id": "r1", "result_code": "DELIVERED"},
                                       values={"送达状态": "DELIVERED"}, inbox_id=iid, in_reply_to="<c1@x>")
    env.service.sender.run_once()
    assert env.ms.inbox_get(iid)["status"] == RECEIPT_SENT


# ---------------------------------------------------------------- §3.2 P-MAIL 动作
def test_resend_dead_creates_a_new_row_with_suffix(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env)
    env.smtp.fail_times = 1
    env.smtp.fail_exc = SmtpPermanentError("552 超限")
    env.service.sender.run_once()
    dead = env.outbox_rows()[0]
    new_id = env.service.sender.resend(dead["id"])
    rows = env.outbox_rows()
    assert len(rows) == 2 and rows[1]["id"] == new_id
    assert rows[1]["dedup_key"] == dead["dedup_key"] + "#2" and rows[1]["status"] == "QUEUED"
    assert rows[1]["rfc_message_id"] != dead["rfc_message_id"]


def test_discard(store, clock):
    env = make_env(store, clock)
    oid = enqueue_msg(env)
    env.service.sender.discard(oid)
    assert env.outbox_rows()[0]["status"] == "DISCARDED"
    assert env.service.sender.run_once().sent == 0


# ---------------------------------------------------------------- §2.16.2 告警邮件
def test_alert_mail_uses_default_route_alert_to(store, clock):
    cfg = make_cfg()
    cfg.outbound.alert_to = ["noc@corp.example"]
    env = make_env(store, clock, cfg=cfg)
    env.service.sender.enqueue_alert(
        ctx={"template_version": "v1", "code": "NET_PUBLIC_ENDPOINT_CHANGED",
             "previous_ip": "1.1.1.1", "public_ip": "2.2.2.2\r\nBcc: x@y"},
        lines=[("旧出口", "1.1.1.1"), ("新出口", "2.2.2.2")], dedup_key="alert:endpoint:1")
    row = env.outbox_rows()[0]
    assert row["kind"] == "alert" and row["to_addrs"] == "noc@corp.example"
    env.service.sender.run_once()
    frm, to, raw = env.smtp.sent[0]
    msg = email.message_from_bytes(raw)
    # 探测来的 IP 是外部可控值,裸拼会被注入 \r\nBcc: —— 过 hdr_sanitize 后只剩一个 Subject 头
    assert msg["Bcc"] is None and len(msg.get_all("Subject")) == 1 and to == ["noc@corp.example"]


# ---------------------------------------------------------------- §2.7 status
def test_status_reports_queue_counts(store, clock):
    env = make_env(store, clock)
    enqueue_msg(env)
    st = env.service.status()[0]
    assert st["outbound"]["queued"] == 1 and st["outbound"]["template_profile"] == "ibquote-163-v1"
    assert st["route"]["channel"] == "*" and st["inbound"]["configured_protocol"] == "imap"
