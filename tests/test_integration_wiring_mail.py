"""第五批接线:邮件端点(06 §3.2)—— 重点是 **#68b/#68c/#68d 的确认流经 API 走通**,
且 ``transport`` **只能取自鉴权上下文**(承重墙,基线 §11.17 ③;调用方在 body 里写什么都没用)。

全程只碰 ``FakeImap``/``FakePop3``/``FakeSmtp``,绝不连任何真实邮箱。
"""
from __future__ import annotations

import pytest

from qtrade_agent.mail.codes import ACCEPTED, CONFIRM_REQUIRED, OP_DENIED, PARSE_FAILED, REASON_REJECTED
from qtrade_agent.mail.fetcher import RawMail
from qtrade_agent.mail.service import MailService
from tests.test_integration_wiring_common import TOKEN_READ, TOKEN_WRITE, H, close_rig, make_rig
from tests.test_mail_common import (BOX_ADDR, OPS_ADDR, SECRET, build_catalog, command_mail, make_cfg, make_mail)
from qtrade_agent.mail.backends import FakeImap, FakePop3, FakeSmtp


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    # [mail] 的真目录里现在一个 danger=true 的 op 都没有(只有五项 read/write),
    # 故这里换一份带 account_stop / messages_purge 的目录 + 假后端,才能把「高危 → 202 待确认」这条路跑出来。
    r.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675", label="张三-固收")
    cfg = make_cfg(allow_ops=["*", "account_stop"], archive_dir=str(tmp_path / "mail-archive"))
    r.imap, r.pop3, r.smtp = FakeImap(), FakePop3(), FakeSmtp()
    r.agent.mail = MailService(r.store, cfg, catalog=build_catalog(), clock=r.clock, alerts=r.agent.alerts,
                               secret_of=lambda ref: SECRET,
                               imap_factory=lambda route: r.imap, pop3_factory=lambda route: r.pop3,
                               smtp_factory=lambda route: r.smtp)
    yield r
    close_rig(r)


def _ingest(rig, raw: bytes, *, uid: int = 1) -> tuple[str, int]:
    mailbox = next(iter(rig.agent.mail.fetchers))
    status = rig.agent.mail.ingest.ingest_raw(RawMail(mailbox=mailbox, protocol="imap", folder="INBOX",
                                                      size_bytes=len(raw), raw=raw, uid=uid, uidvalidity=1))
    return status, rig.agent.mail.ms.inbox_list(limit=1)[0]["id"]


def _pending(rig) -> tuple[int, dict]:
    status, iid = _ingest(rig, command_mail(req_id="r1", op="account_stop", session="", args={}))
    assert status == CONFIRM_REQUIRED
    return iid, rig.agent.mail.ms.inbox_get(iid)


# ---------------------------------------------------------------- #56 / #58 / #59 / #61 / #65 读
def test_mail_status_and_lists(rig):
    _ingest(rig, command_mail(req_id="r-read"))
    r = rig.client.get("/api/v1/mail/status", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["routes"]      # 第六批:#56 按 02 + R6-55 顶层平铺 {ok, enabled, routes}
    r = rig.client.get("/api/v1/mail/inbox", headers=H(TOKEN_READ))
    assert r.status_code == 200 and len(r.json()["data"]) == 1
    iid = r.json()["data"][0]["id"]
    assert rig.client.get(f"/api/v1/mail/inbox/{iid}", headers=H(TOKEN_READ)).status_code == 200
    assert rig.client.get("/api/v1/mail/inbox/99999", headers=H(TOKEN_READ)).status_code == 404
    assert rig.client.get("/api/v1/mail/outbox", headers=H(TOKEN_READ)).status_code == 200
    assert rig.client.get("/api/v1/mail/cleanup/log", headers=H(TOKEN_READ)).status_code == 200


def test_mail_endpoints_need_a_token(rig):
    assert rig.client.get("/api/v1/mail/inbox").status_code == 401


# ---------------------------------------------------------------- #60 reparse
def test_reparse_reruns_classification_and_rejects_wrong_status(rig):
    """#60:只对 ``PARSE_FAILED/SIG_INVALID/UNSUPPORTED`` 重跑;别的状态 409。"""
    raw = make_mail("QTrade 指令 v1\n\n指令ID：r-broken\n账号：qd01\n",   # 缺 操作/时间戳/随机数/签名
                    subject="QTRADE指令 v1 [qd01] send_text r-broken", from_addr=OPS_ADDR, to_addr=BOX_ADDR,
                    message_id="<broken@corp.example>")
    status, iid = _ingest(rig, raw)
    assert status == PARSE_FAILED
    r = rig.client.post(f"/api/v1/mail/inbox/{iid}/reparse", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["status"] == PARSE_FAILED      # 模板没改,重跑仍是同一结论

    _, good = _ingest(rig, command_mail(req_id="r-ok"), uid=2)
    r = rig.client.post(f"/api/v1/mail/inbox/{good}/reparse", headers=H(TOKEN_WRITE))
    assert r.status_code == 409 and r.json()["error"]["reason"] == "bad_status"
    assert rig.client.post("/api/v1/mail/inbox/99999/reparse", headers=H(TOKEN_WRITE)).status_code == 404


# ---------------------------------------------------------------- #68b 列表
def test_pending_confirms_endpoint_returns_exactly_eight_keys(rig):
    """R6-7 定死八键;**不含** ``confirm_via``/``confirm_nonce``(v1 无此两列)。"""
    iid, _ = _pending(rig)
    r = rig.client.get("/api/v1/mail/pending-confirms", headers=H())
    assert r.status_code == 200
    items = r.json()["data"]
    # 出参 `id` 为字符串(与 #58/#59 的同一个 `mail_inbox.id` 同型、控制台 `PendingConfirm.id: string`;第五批 backend-api-5)
    assert len(items) == 1 and items[0]["id"] == str(iid)
    assert set(items[0]) == {"id", "op", "from_addr", "account_id", "args_digest", "created_at",
                             "expires_at", "remaining_ttl_s"}


def test_pending_confirms_needs_admin(rig):
    assert rig.client.get("/api/v1/mail/pending-confirms", headers=H(TOKEN_WRITE)).status_code == 403


# ---------------------------------------------------------------- #68c / #68d 确认流
def test_approve_through_api_uses_console_transport_from_auth_context(rig):
    """🔴 承重墙:``transport`` 取自鉴权上下文,``body`` 里写 ``email`` 也改不了结论 —— 否则邮件链路能自己批自己。"""
    iid, _ = _pending(rig)
    r = rig.client.post(f"/api/v1/mail/pending-confirms/{iid}/approve", headers=H(), json={"transport": "email"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert rig.agent.mail.ms.inbox_get(iid)["status"] == ACCEPTED          # R6-29:approve 抢到 → ACCEPTED
    actions = [a["action"] for a in rig.store.list_audit("mail.pending_confirm.approved")]
    assert actions == ["mail.pending_confirm.approved"]


def test_approve_twice_is_409(rig):
    iid, _ = _pending(rig)
    assert rig.client.post(f"/api/v1/mail/pending-confirms/{iid}/approve", headers=H()).status_code == 200
    r = rig.client.post(f"/api/v1/mail/pending-confirms/{iid}/approve", headers=H())
    assert r.status_code == 409 and r.json()["ok"] is False


def test_reject_through_api_records_the_actor(rig):
    iid, _ = _pending(rig)
    r = rig.client.post(f"/api/v1/mail/pending-confirms/{iid}/reject", headers=H())
    assert r.status_code == 200
    row = rig.agent.mail.ms.inbox_get(iid)
    assert row["status"] == OP_DENIED and row["reason"] == REASON_REJECTED + "token:console"


def test_reject_needs_admin(rig):
    iid, _ = _pending(rig)
    assert rig.client.post(f"/api/v1/mail/pending-confirms/{iid}/reject", headers=H(TOKEN_WRITE)).status_code == 403
    assert rig.agent.mail.ms.inbox_get(iid)["status"] == CONFIRM_REQUIRED   # 一步都没动


def test_expired_confirm_is_409_through_api(rig):
    iid, _ = _pending(rig)
    rig.clock.advance(rig.agent.mail.cfg.inbound.danger_confirm_ttl_s * 1000 + 1000)
    r = rig.client.post(f"/api/v1/mail/pending-confirms/{iid}/approve", headers=H())
    assert r.status_code == 409 and r.json()["code"] == "CONFIRM_EXPIRED"


# ---------------------------------------------------------------- #64 cleanup/run / #67 #68 hmac-keys / #105 routes
def test_cleanup_run_is_a_job_and_sets_the_manual_flag(rig):
    """§2.6.6:``/mail/cleanup/run`` 只置标志,真清在下一轮取信同一条连接里做。"""
    r = rig.client.post("/api/v1/mail/cleanup/run", headers=H(), json={"dry_run": True})
    assert r.status_code == 202 and r.json()["job_id"]
    assert all(c.manual_requested for c in rig.agent.mail.cleaners.values())
    assert rig.store.job_get(r.json()["job_id"])["kind"] == "mail_cleanup"
    assert rig.client.post("/api/v1/mail/cleanup/run", headers=H(TOKEN_WRITE), json={}).status_code == 403


def test_hmac_key_create_and_revoke(rig):
    """#67:短名规则 ``^[A-Za-z0-9._-]{1,32}$`` 且全局唯一;密钥写 Vault ``vault://mail/hmac/cmd/<短名>``(R6-10)。"""
    r = rig.client.post("/api/v1/mail/hmac-keys", headers=H(), json={"sender": OPS_ADDR, "short_name": "ops2"})
    assert r.status_code == 200 and r.json()["secret_ref"] == "vault://mail/hmac/cmd/ops2"
    assert "mail/hmac/cmd/ops2" in rig.agent.vault.entries
    again = rig.client.post("/api/v1/mail/hmac-keys", headers=H(), json={"sender": OPS_ADDR, "short_name": "ops2"})
    assert again.status_code == 400 and again.json()["error"]["reason"] == "short_name_taken"
    bad = rig.client.post("/api/v1/mail/hmac-keys", headers=H(), json={"sender": OPS_ADDR, "short_name": "a:b"})
    assert bad.status_code == 400 and bad.json()["error"]["reason"] == "short_name_invalid"
    assert rig.client.delete("/api/v1/mail/hmac-keys/ops2", headers=H()).status_code == 200
    assert "mail/hmac/cmd/ops2" not in rig.agent.vault.entries
    assert rig.client.delete("/api/v1/mail/hmac-keys/ops2", headers=H()).status_code == 404


def test_routes_get_and_put(rig):
    r = rig.client.get("/api/v1/settings/mail/routes", headers=H())
    assert r.status_code == 200 and isinstance(r.json()["data"], list)
    r = rig.client.put("/api/v1/settings/mail/routes", headers=H(),
                       json={"channel": "qidian", "inbound": {"host": "imap.example.com"}, "outbound": {}})
    assert r.status_code == 200 and r.json()["id"]
    bad = rig.client.put("/api/v1/settings/mail/routes", headers=H(), json={"channel": "telegram"})
    assert bad.status_code == 400 and bad.json()["error"]["reason"] == "bad_channel"


def test_template_defaults_and_preview(rig):
    r = rig.client.get("/api/v1/settings/mail/templates/defaults", headers=H())
    assert r.status_code == 200 and set(r.json()["data"]) >= {"ibquote-163-v1"}
    r = rig.client.post("/api/v1/mail/templates/default/preview", headers=H(TOKEN_READ),
                        json={"ctx": {"session_name": "固收群", "summary": "报价", "ts_cn": "09-20 10:00", "seq": 1,
                                      "text": "3M 1.52", "sender_name": "张三", "channel": "wechat",
                                      "account_id": "wx01", "seq_total": 1, "day_seq": 1, "msg_ts": "10:00",
                                      "revoked": "否"}})
    assert r.status_code == 200 and r.json()["subject"]
    # #104 路径带 {id}:空 body ⇒ 400(既没 ctx 也没 sample_message_id);老的无 id 路径已不存在 ⇒ 404 信封
    assert rig.client.post("/api/v1/mail/templates/default/preview", headers=H(TOKEN_READ), json={}).status_code == 400
    gone = rig.client.post("/api/v1/mail/templates/preview", headers=H(TOKEN_READ), json={})
    assert gone.status_code == 404 and gone.json()["code"] == "NOT_FOUND"
