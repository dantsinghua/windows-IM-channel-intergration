"""高危二次确认 —— 规格:docs/06 §2.3.6(双钥双通道 R-03)、§3.2 #68b/#68c/#68d、02 §3.1 ``mail_confirm_reaper``。"""
from __future__ import annotations

from qtrade_agent.mail.codes import (ACCEPTED, AUDIT_CONFIRM_APPROVED, AUDIT_CONFIRM_ARGS_MISMATCH,
                                     AUDIT_CONFIRM_EXPIRED, AUDIT_CONFIRM_REJECTED, CONFIRM_EXPIRED,
                                     CONFIRM_REQUIRED, OP_DENIED, REASON_ARGS_TAMPERED, REASON_REJECTED)
from qtrade_agent.mail.fetcher import RawMail
from test_mail_common import command_mail, make_cfg, make_env
from test_mail_ingest import FakeBus

ACTOR = "token:console"


def env_with_pending(store, clock, *, op: str = "account_stop", args=None, req_id: str = "r1"):
    env = make_env(store, clock, cfg=make_cfg(allow_ops=["*", op]))
    raw = command_mail(req_id=req_id, op=op, session="", args=args if args is not None else {})
    st = env.service.ingest.ingest_raw(RawMail(mailbox=env.mailbox, protocol="imap", folder="INBOX",
                                               size_bytes=len(raw), raw=raw, uid=1, uidvalidity=1))
    assert st == CONFIRM_REQUIRED
    return env, env.inbox_rows()[-1]["id"]


def audits(store, action: str) -> list[dict]:
    return store.list_audit(action)


# ---------------------------------------------------------------- 🔴 承重墙(基线 §11.17 ③)
def test_email_transport_can_never_approve(store, clock):
    env, iid = env_with_pending(store, clock)
    out = env.service.confirms.approve(iid, actor="mail:ops@corp.example", transport="email")
    assert (out.ok, out.http_status, out.code) == (False, 403, "FORBIDDEN")
    assert env.ms.inbox_get(iid)["status"] == CONFIRM_REQUIRED          # 一步都没动
    assert audits(store, AUDIT_CONFIRM_APPROVED) == []


def test_email_transport_can_never_reject(store, clock):
    env, iid = env_with_pending(store, clock)
    out = env.service.confirms.reject(iid, actor="mail:ops@corp.example", transport="email")
    assert out.http_status == 403 and env.ms.inbox_get(iid)["status"] == CONFIRM_REQUIRED


# ---------------------------------------------------------------- #68b 出参
def test_pending_list_has_exactly_eight_keys(store, clock):
    """🔴 R6-7:出参恰 ``{id, op, from_addr, account_id, args_digest, created_at, expires_at, remaining_ttl_s}``;

    **不含 ``confirm_via``/``confirm_nonce``**(v1 无此两列)。
    """
    env, iid = env_with_pending(store, clock)
    items = env.service.confirms.list()
    assert len(items) == 1
    assert set(items[0]) == {"id", "op", "from_addr", "account_id", "args_digest", "created_at",
                            "expires_at", "remaining_ttl_s"}
    assert items[0]["remaining_ttl_s"] == 900 and items[0]["op"] == "account_stop"


# ---------------------------------------------------------------- #68c approve
async def test_console_approve_executes_and_audits(store, clock):
    env, iid = env_with_pending(store, clock)
    out = env.service.confirms.approve(iid, actor=ACTOR, transport="console")
    assert out.ok and env.ms.inbox_get(iid)["status"] == ACCEPTED       # 🔴 R6-29:approve 抢到 → ACCEPTED
    a = audits(store, AUDIT_CONFIRM_APPROVED)
    assert len(a) == 1 and a[0]["actor"] == ACTOR                       # R6-25 审计名;带操作者、不记 confirm_via
    assert "confirm_via" not in (a[0]["detail_json"] or "")
    bus = FakeBus()
    await env.service.dispatch(bus)
    assert len(bus.calls) == 1 and bus.calls[0].op == "account_stop"


def test_approve_twice_is_409(store, clock):
    env, iid = env_with_pending(store, clock)
    env.service.confirms.approve(iid, actor=ACTOR, transport="console")
    out = env.service.confirms.approve(iid, actor=ACTOR, transport="console")
    assert (out.http_status, out.code) == (409, CONFIRM_EXPIRED)        # 原子 claim:rowcount==0


# ---------------------------------------------------------------- #68d reject
def test_reject_records_the_actor_in_reason_and_audit(store, clock):
    """🔴 R6-34:``reason = 'REJECTED:' || :actor``,与该条审计的 ``actor`` 同值。"""
    env, iid = env_with_pending(store, clock)
    out = env.service.confirms.reject(iid, actor=ACTOR, transport="console")
    row = env.ms.inbox_get(iid)
    assert out.ok and row["status"] == OP_DENIED
    assert row["reason"] == REASON_REJECTED + ACTOR
    a = audits(store, AUDIT_CONFIRM_REJECTED)
    assert len(a) == 1 and a[0]["actor"] == ACTOR


# ---------------------------------------------------------------- R6-20 过期
def test_expired_approve_is_409_and_endpoint_expires_it_without_waiting_reaper(store, clock):
    env, iid = env_with_pending(store, clock)
    before = env.ms.inbox_get(iid)["confirm_expires_ms"]
    clock.advance(901 * 1000)
    out = env.service.confirms.approve(iid, actor=ACTOR, transport="console")
    row = env.ms.inbox_get(iid)
    assert (out.http_status, out.code) == (409, CONFIRM_EXPIRED)
    assert row["status"] == CONFIRM_EXPIRED
    assert row["confirm_expires_ms"] == before                         # 🔴 R6-19:一经写入即保留,不清空
    a = audits(store, AUDIT_CONFIRM_EXPIRED)
    assert len(a) == 1 and a[0]["actor"] == ACTOR and '"expired_by": "endpoint"' in a[0]["detail_json"]


def test_reaper_expires_and_audits_as_scheduler(store, clock):
    env, iid = env_with_pending(store, clock)
    assert env.service.reap_confirms() == 0                            # 没到期,一行都不动
    clock.advance(901 * 1000)
    assert env.service.reap_confirms() == 1
    row = env.ms.inbox_get(iid)
    assert row["status"] == CONFIRM_EXPIRED and row["confirm_expires_ms"] is not None
    a = audits(store, AUDIT_CONFIRM_EXPIRED)
    assert a[0]["actor"] == "system:scheduler" and '"expired_by": "reaper"' in a[0]["detail_json"]
    assert env.service.reap_confirms() == 0                            # 幂等:不重复过期、不重复记审计


# ---------------------------------------------------------------- R6-22 防库被改
def test_tampered_args_are_refused_before_execution(store, clock):
    """approve 执行时的参数来源 = 重解析已验签的 ``body_text``;复算 digest 不等 ⇒ 拒绝执行。"""
    env, iid = env_with_pending(store, clock, op="messages_purge",
                                args={"before": "2026-01-01", "mode": "all"})
    with env.ms._store._tx() as c:                                     # 模拟有人绕过端点改了库
        c.execute("UPDATE mail_inbox SET body_text=REPLACE(body_text, 'all', 'text_only') WHERE id=?", (iid,))
    out = env.service.confirms.approve(iid, actor=ACTOR, transport="console")
    row = env.ms.inbox_get(iid)
    assert (out.http_status, out.code, out.reason) == (500, "INTERNAL", "args_digest_mismatch")
    assert row["status"] == OP_DENIED and row["reason"].startswith(REASON_ARGS_TAMPERED)
    assert len(audits(store, AUDIT_CONFIRM_ARGS_MISMATCH)) == 1
    assert env.service.ingest.pending == []                            # 绝不投总线


def test_approve_uses_no_confirm_key_at_all(store, clock):
    """🔴 R6-7:确认钥 ``vault://mail/hmac/confirm`` 在 v1 只生成保管、**无消费者** —— approve 全程不取它。"""
    env, iid = env_with_pending(store, clock)
    taken = []
    env.service.ingest.secret_of = lambda ref: (taken.append(ref), "x")[1]
    env.service.confirms.approve(iid, actor=ACTOR, transport="console")
    assert all("hmac/confirm" not in r for r in taken)
