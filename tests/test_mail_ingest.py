"""入站三道闸 / 去重四层 / 高危待确认 / 回执 —— 规格:docs/06 §2.2、§2.3、§2.5、§2.8、§5。"""
from __future__ import annotations

import json

from qtrade_agent.mail.codes import (ACCEPTED, CONFIRM_REQUIRED, DONE, DUPLICATE, DUPLICATE_NONCE, EXPIRED,
                                     OP_DENIED, OUT_OF_SCOPE, PARSE_FAILED, RECEIPT_SKIPPED, REASON_NOT_ALLOWED,
                                     ROUTE_MISMATCH, SENDER_DENIED, SIG_INVALID, TARGET_NOT_FOUND, UNSUPPORTED)
from qtrade_agent.mail.fetcher import RawMail
from qtrade_agent.mail.sign import receipt_canonical, verify
from qtrade_agent.models import CommandError, CommandResult
from test_mail_common import OPS_ADDR, SECRET, command_body, command_mail, make_cfg, make_env, make_mail

TRACE = "01J8TRACE"


class FakeBus:
    """契约面与 ``bus.Bus.submit`` 相同:``Command`` → ``CommandResult``(登录门/幂等/安全闸都在真 bus 里,本处只验搬运)。"""

    def __init__(self, result: CommandResult | None = None):
        self.calls = []
        self.result = result

    async def submit(self, cmd):
        self.calls.append(cmd)
        return self.result or CommandResult(ok=True, code="DELIVERED", trace_id=TRACE,
                                            data={"message_id": "msg_1"}, cost_ms=1650, source="qidian_db")


def feed(env, raw: bytes, *, uid: int | None = None) -> str:
    uid = uid if uid is not None else (len(env.inbox_rows()) + 1) * 10
    return env.service.ingest.ingest_raw(RawMail(mailbox=env.mailbox, protocol="imap", folder="INBOX",
                                                 size_bytes=len(raw), raw=raw, uid=uid, uidvalidity=1))


def last_row(env) -> dict:
    return env.inbox_rows()[-1]


def receipts(env) -> list[dict]:
    return [r for r in env.outbox_rows() if r["kind"] == "receipt"]


# ================================================================ §2.2 闸 ①:发件人白名单
def test_sender_not_in_whitelist_is_denied_without_receipt(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", from_addr="stranger@evil.example")) == SENDER_DENIED
    assert receipts(env) == []                      # 不回执:否则等于给陌生人一个「这里有系统」的探针
    assert env.alerts.is_firing("MAIL_SENDER_DENIED", "sender:stranger@evil.example")


# ================================================================ §2.2 闸 ②:HMAC 验签
def test_bad_signature_is_sig_invalid_without_receipt(store, clock):
    env = make_env(store, clock)
    bad = "hmac-sha256=" + "0" * 64
    assert feed(env, command_mail(req_id="r1", signature=bad)) == SIG_INVALID
    assert receipts(env) == [] and last_row(env)["sig_ok"] == 0


def test_missing_signature_fields_is_sig_invalid(store, clock):
    env = make_env(store, clock)
    body = command_body(req_id="r1", account_id="qd01", op="read_messages", secret=None)
    body = "\n".join(ln for ln in body.splitlines() if not ln.startswith(("随机数", "时间戳")))
    assert feed(env, make_mail(body)) == SIG_INVALID


def test_timestamp_outside_tolerance_is_expired(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", timestamp="2020-01-01T00:00:00+08:00")) == EXPIRED


def test_same_nonce_twice_is_replay(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", nonce="N1")) == ACCEPTED
    assert feed(env, command_mail(req_id="r2", nonce="N1", message_id="<m2@x>")) == DUPLICATE_NONCE


def test_require_signature_false_lets_unsigned_through(store, clock):
    env = make_env(store, clock, cfg=make_cfg(require_signature=False))
    body = command_body(req_id="r1", account_id="qd01", op="read_messages", session="qd01:1", secret=None)
    assert feed(env, make_mail(body)) == ACCEPTED


def test_signature_covers_attachments(store, clock):
    """§2.3.4:附件 sha256 进签名 —— 换图即验签失败。"""
    env = make_env(store, clock)
    att = [{"name": "quote.png", "bytes": b"\xff\xd8\xffORIGINAL"}]
    raw = command_mail(req_id="r1", op="send_text", args={"text": "看图"}, attachments=att)
    assert feed(env, raw) == ACCEPTED
    swapped = command_mail(req_id="r2", op="send_text", args={"text": "看图"}, message_id="<m2@x>", nonce="n2",
                           attachments=[{"name": "quote.png", "bytes": b"\xff\xd8\xffSWAPPED"}])
    assert feed(env, swapped) == ACCEPTED
    # 用原附件签的名,配上换过的图 ⇒ 验签失败(附件 sha256 进签名)
    body = command_body(req_id="r3", account_id="qd01", op="send_text", session="qd01:415011447",
                        args={"text": "看图"}, attachments=att, nonce="n3")
    tampered = make_mail(body, message_id="<m3@x>",
                         attachments=[{"name": "quote.png", "bytes": b"\xff\xd8\xffSWAPPED"}])
    assert feed(env, tampered) == SIG_INVALID


# ================================================================ §2.2 闸 ③:allow_ops
def test_danger_op_not_allowed_by_default_star(store, clock):
    """🔴 R2-1:``["*"]`` 只展开 ``danger=false``;``danger=true`` 须逐条写进 allow_ops。"""
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", op="account_stop", session="", args={})) == OP_DENIED
    row = last_row(env)
    assert row["reason"].startswith(REASON_NOT_ALLOWED)
    assert len(receipts(env)) == 1                  # 发件人已验签,是真的 ⇒ 回执


def test_non_danger_op_runs_under_star(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", op="read_messages", args={"limit": 5})) == ACCEPTED


# ================================================================ §2.3.6 高危 = 202 待确认
def test_danger_op_explicitly_allowed_goes_to_confirm_required(store, clock):
    env = make_env(store, clock, cfg=make_cfg(allow_ops=["*", "account_stop"]))
    assert feed(env, command_mail(req_id="r1", op="account_stop", session="", args={})) == CONFIRM_REQUIRED
    row = last_row(env)
    assert row["args_digest"] and len(row["args_digest"]) == 16
    assert row["confirm_expires_ms"] == row["received_ms"] + 900 * 1000     # danger_confirm_ttl_s 默认 900
    assert env.service.ingest.pending == []                                 # 不进总线执行
    body = receipts(env)[0]["body_text"]
    assert "送达状态：CONFIRM_REQUIRED" in body and "控制台" in body


def test_pending_confirm_columns_absent_in_v1(store, clock):
    """🔴 R6-7:v1 **不建** ``confirm_nonce``/``confirm_via`` 两列(不是「有列但恒 NULL」)。"""
    env = make_env(store, clock)
    cols = {r[1] for r in env.ms.con.execute("PRAGMA table_info(mail_inbox)").fetchall()}
    assert "confirm_nonce" not in cols and "confirm_via" not in cols
    assert {"confirm_expires_ms", "args_digest"} <= cols


def test_resubmit_before_confirm_does_not_open_second_pending(store, clock):
    env = make_env(store, clock, cfg=make_cfg(allow_ops=["*", "account_stop"]))
    feed(env, command_mail(req_id="r1", op="account_stop", session="", args={}))
    again = command_mail(req_id="r1", op="account_stop", session="", args={}, message_id="<m2@x>", nonce="n2")
    assert feed(env, again) == DUPLICATE
    pend = [r for r in env.inbox_rows() if r["status"] == CONFIRM_REQUIRED]
    assert len(pend) == 1 and last_row(env)["first_inbox_id"] == pend[0]["id"]


# ================================================================ §2.5 去重四层
def test_layer1_same_uid_is_skipped(store, clock):
    env = make_env(store, clock)
    raw = command_mail(req_id="r1")
    assert feed(env, raw, uid=7) == ACCEPTED
    assert feed(env, raw, uid=7) == ACCEPTED and len(env.inbox_rows()) == 1


def test_layer2_same_message_id_inserts_no_second_row(store, clock):
    env = make_env(store, clock)
    raw = command_mail(req_id="r1")
    feed(env, raw, uid=1)
    feed(env, raw, uid=2)                           # 同 Message-ID(UIDVALIDITY 变了的重扫)
    assert len(env.inbox_rows()) == 1


def test_layer3_same_body_different_message_id_is_duplicate(store, clock):
    env = make_env(store, clock)
    feed(env, command_mail(req_id="r1"), uid=1)
    assert feed(env, command_mail(req_id="r1", message_id="<gw-resend@x>"), uid=2) == DUPLICATE
    assert last_row(env)["first_inbox_id"] == 1


async def test_layer4_same_req_id_same_args_replays_receipt(store, clock):
    env = make_env(store, clock)
    bus = FakeBus()
    feed(env, command_mail(req_id="r1"))
    await env.service.dispatch(bus)
    n_before = len(receipts(env))
    # 同 req_id、同参数、不同 Message-ID 与 nonce ⇒ 幂等命中,**回执照发**
    again = command_mail(req_id="r1", message_id="<m2@x>", nonce="n2",
                         args={"text": "今日 3M 报价 1.52,可谈"})
    assert feed(env, again) == DUPLICATE
    assert len(receipts(env)) == n_before + 1


async def test_layer4_same_req_id_different_args_is_invalid(store, clock):
    env = make_env(store, clock)
    bus = FakeBus()
    feed(env, command_mail(req_id="r1"))
    await env.service.dispatch(bus)
    changed = command_mail(req_id="r1", message_id="<m2@x>", nonce="n2", args={"text": "改了参数"})
    assert feed(env, changed) == DUPLICATE
    row = last_row(env)
    assert row["reason"] == "req_id_reused_with_different_body"
    assert "送达状态：INVALID_ARGS" in receipts(env)[-1]["body_text"]
    assert "指令ID 已被使用且内容不同" in receipts(env)[-1]["body_text"]


def test_idempotency_key_is_rewritten_with_short_name(store, clock):
    """C-10:Transport 在进总线前把 ``req_id`` 改写为 ``mail:{发件人短名}:{req_id}``(两个发起方各自从 0001 编号也不撞)。"""
    env = make_env(store, clock)
    feed(env, command_mail(req_id="20260918-ops-0007"))
    assert env.service.ingest.pending[0].command.idempotency_key == "mail:ops:20260918-ops-0007"
    row = last_row(env)
    if "idempotency_key" in row:          # 06 §3.1 提了这一列,02 v1 DDL 尚未建(handoff 建议裁决 ⑤)
        assert row["idempotency_key"] == "mail:ops:20260918-ops-0007"


# ================================================================ §2.15 路由 / 目标
def test_route_mismatch_when_account_not_covered(store, clock):
    cfg = make_cfg()
    env = make_env(store, clock, cfg=cfg)
    # 把全局路由改成「只覆盖微信通道」,再发一封停企点账号的指令
    env.ms.route_upsert(channel="wechat", account_id=None,
                        inbound_json={"enabled": True, "host": cfg.inbound.host, "user": cfg.inbound.user,
                                      "allowed_senders": [OPS_ADDR], "require_signature": False,
                                      "allow_ops": ["*"]},
                        outbound_json={"enabled": True, "host": "smtp.example.com", "from": "bot@corp.example",
                                       "to": ["ops-inbox@corp.example"]})
    env.ms.con.execute("UPDATE mail_routes SET enabled=0 WHERE channel IS NULL")
    env.service.reload()
    body = command_body(req_id="r1", account_id="qd01", op="read_messages", session="qd01:1", secret=None)
    assert feed(env, make_mail(body)) == ROUTE_MISMATCH


def test_unknown_account_is_target_not_found(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", account_id="qd99")) == TARGET_NOT_FOUND
    assert "送达状态：TARGET_NOT_FOUND" in receipts(env)[0]["body_text"]


def test_channel_mismatch_is_invalid_args(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", channel="wechat")) == DONE
    assert "送达状态：INVALID_ARGS" in receipts(env)[0]["body_text"]


# ================================================================ §2.8 op 与参数
def test_unknown_op_is_parse_failed(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", op="发消息", session="", args={})) == PARSE_FAILED
    assert last_row(env)["reason"].startswith("op_unknown")


def test_unknown_arg_property_is_invalid_args_with_pointer(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", op="read_messages", args={"limit": 5, "怪参数": 1})) == DONE
    assert "送达状态：INVALID_ARGS" in receipts(env)[0]["body_text"]


def test_expand_form_values_are_coerced_by_schema(store, clock):
    env = make_env(store, clock)
    assert feed(env, command_mail(req_id="r1", op="read_messages", args={"limit": 5}, expand_form=True)) == ACCEPTED
    cmd = env.service.ingest.pending[0].command
    assert cmd.args["limit"] == 5 and isinstance(cmd.args["limit"], int)


def test_command_origin_is_email(store, clock):
    env = make_env(store, clock)
    feed(env, command_mail(req_id="r1"))
    cmd = env.service.ingest.pending[0].command
    assert cmd.origin.transport == "email" and cmd.origin.actor == "mail:" + OPS_ADDR
    assert cmd.idempotency_key == "mail:ops:r1"


def test_timeout_is_capped_by_max_timeout_ms(store, clock):
    env = make_env(store, clock)
    feed(env, command_mail(req_id="r1", timeout=999999))
    assert env.service.ingest.pending[0].command.timeout_ms == 120000


# ================================================================ §2.3.5 / §5 解析失败与范围
def test_unsupported_template_gets_no_receipt(store, clock):
    env = make_env(store, clock)
    raw = make_mail("QTRADE指令 相关,但正文不是模板", subject="QTRADE指令 v1 [qd01] x y")
    assert feed(env, raw) == UNSUPPORTED and receipts(env) == []


def test_parse_failed_gets_receipt_for_whitelisted_sender(store, clock):
    env = make_env(store, clock)
    body = "QTrade 指令 v1\n\n指令ID：r1\n账号：qd01\n操作：\n"
    assert feed(env, make_mail(body)) == PARSE_FAILED
    assert len(receipts(env)) == 1 and "送达状态：INVALID_ARGS" in receipts(env)[0]["body_text"]


def test_out_of_scope_is_registered_only(store, clock):
    env = make_env(store, clock)
    assert feed(env, make_mail("随便一封信", subject="午餐订餐")) == OUT_OF_SCOPE
    assert last_row(env)["body_text"] is None and receipts(env) == []


# ================================================================ 进总线与回执
async def test_accepted_goes_to_bus_and_receipt_carries_result(store, clock):
    env = make_env(store, clock)
    bus = FakeBus()
    feed(env, command_mail(req_id="r1"))
    assert last_row(env)["status"] == ACCEPTED
    await env.service.dispatch(bus)
    assert len(bus.calls) == 1 and bus.calls[0].op == "send_text"
    row = last_row(env)
    assert row["status"] == DONE and row["trace_id"] == TRACE
    body = receipts(env)[0]["body_text"]
    assert "送达状态：DELIVERED" in body and "结果：成功" in body
    assert "确认方式：qidian_db" in body and "消息ID：msg_1" in body and "耗时毫秒：1650" in body


async def test_receipt_signature_verifies_with_the_command_key(store, clock):
    """§2.4.2:回执用**同一把指令钥**签,让发起方能验真是我们回的。"""
    env = make_env(store, clock)
    feed(env, command_mail(req_id="r1"))
    await env.service.dispatch(FakeBus())
    body = receipts(env)[0]["body_text"]
    got = {ln.split("：", 1)[0]: ln.split("：", 1)[1] for ln in body.splitlines() if "：" in ln}
    canonical = receipt_canonical(req_id="r1", account_id="qd01", op="send_text", delivery_status="DELIVERED",
                                  trace_id=TRACE, executed_at=got["执行时间"])
    assert verify(canonical, SECRET, got["签名"])


async def test_receipt_headers_reference_the_command_mail(store, clock):
    env = make_env(store, clock)
    feed(env, command_mail(req_id="r1", message_id="<cmd-1@corp.example>"))
    await env.service.dispatch(FakeBus())
    r = receipts(env)[0]
    assert r["in_reply_to"] == "<cmd-1@corp.example>" and r["references_hdr"] == "<cmd-1@corp.example>"
    assert r["to_addrs"] == OPS_ADDR                 # receipt_to_sender = true
    assert r["subject"] == "QTRADE回执 v1 [qd01] send_text r1 DELIVERED"


async def test_confirm_false_skips_receipt(store, clock):
    env = make_env(store, clock)
    feed(env, command_mail(req_id="r1", confirm="false"))
    await env.service.dispatch(FakeBus())
    assert last_row(env)["status"] == RECEIPT_SKIPPED and receipts(env) == []


async def test_login_required_receipt_says_needs_human(store, clock):
    """D-2(§2.8 末):登录阶段立即 ``LOGIN_REQUIRED``、不排队,回执马上发出(``需人工：是``),行落 DONE。"""
    env = make_env(store, clock)
    res = CommandResult(ok=False, code="LOGIN_REQUIRED", trace_id=TRACE,
                        error=CommandError("账号处于登录阶段", needs_human=True))
    feed(env, command_mail(req_id="r1"))
    await env.service.dispatch(FakeBus(res))
    body = receipts(env)[0]["body_text"]
    assert "送达状态：LOGIN_REQUIRED" in body and "需人工：是" in body and "结果：失败" in body
    assert last_row(env)["status"] == DONE


def test_attachment_becomes_media_reference_in_args(store, clock):
    """§2.8:附件先落 ``media/``,总线只传引用 ``args.image = {media_ref, sha256}``。"""
    env = make_env(store, clock)
    att = [{"name": "quote.png", "bytes": b"\xff\xd8\xffJPEG"}]
    feed(env, command_mail(req_id="r1", op="send_text", args={"text": "见图"}, attachments=att))
    cmd = env.service.ingest.pending[0].command
    assert cmd.args["image"]["media_ref"] == "quote.png" and len(cmd.args["image"]["sha256"]) == 64
    assert json.loads(last_row(env)["attach_json"])[0]["mime_sniffed"] == "image/jpeg"


def test_disk_full_is_not_isolated_as_poison_mail(store, clock, tmp_path, monkeypatch):
    """02 §2.8.8:盘满不是毒邮件——`DiskFullError` 原样上抛,不落 INGEST_ERROR 隔离行(否则盘满解除后也不会重收)。"""
    import pytest
    from qtrade_agent.maintenance import DiskFullError

    env = make_env(store, clock, tmp_path=tmp_path)

    def boom(mail):
        raise DiskFullError(free_mb=10, db_size_mb=1.0, media_size_mb=1.0)

    monkeypatch.setattr(env.service.ingest, "_ingest_raw", boom)
    with pytest.raises(DiskFullError):
        feed(env, command_mail())
    assert env.inbox_rows() == []
