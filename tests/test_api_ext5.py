"""第五批 API 修复的开发者测试(`.omc/handoffs/backend-api-4.md` §4 F-1/F-2 + 邮件域同型扫描)。

覆盖:
- **#68b `GET /mail/pending-confirms`**:`created_at`/`expires_at` 此前**键名 `*_at`、值是毫秒整数** ⇒ 改 ISO 8601
  (02 #68b 逐字「`created_at` = `received_ms` **序列化**」「`expires_at` = `confirm_expires_ms` **序列化**」,
  00 §6「时间(API/事件/**邮件**)一律 ISO 8601 带偏移」);R6-7 的八键一个不多一个不少;`id` 与 #58 同型出字符串;
- **#65 `GET /mail/cleanup/log`**:此前 `SELECT *` 原样透出库行(`started_ms`/`finished_ms`/`detail_json` 字符串),
  且收 `limit` 不回 `next_cursor`(02 #65 写「分页」)⇒ 出参视图 + C-42 分页(游标 G-16、只在满页时非空、跨页不重不漏);
- **#56 `GET /mail/status`**(扫描时在邮件域里发现的同型):`inbound.last_success_at`、`cleanup.last_run_at`
  键名 `*_at` 值毫秒,`inbound.fallback_since` 同为毫秒 ⇒ ISO。

全程只碰 ``FakeImap``/``FakePop3``/``FakeSmtp``,不连任何真实邮箱、不出网。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from qtrade_agent.mail.backends import FakeImap, FakePop3, FakeSmtp
from qtrade_agent.mail.codes import CONFIRM_REQUIRED
from qtrade_agent.mail.fetcher import RawMail
from qtrade_agent.mail.service import MailService
from tests.test_integration_wiring_common import TOKEN_READ, H, close_rig, make_rig
from tests.test_mail_common import SECRET, build_catalog, command_mail, make_cfg

P = "/api/v1"
T0 = 1_789_000_000_000
PENDING_KEYS = {"id", "op", "from_addr", "account_id", "args_digest", "created_at", "expires_at", "remaining_ttl_s"}
CLEANUP_KEYS = {"id", "started_at", "finished_at", "trigger", "protocol", "folder", "candidates", "archived", "deleted",
                "failed", "bytes_freed", "quota_used_before", "quota_used_after", "quota_limit", "quota_source",
                "archive_rotated_files", "archive_rotated_bytes", "status", "error", "detail"}


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    r.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675", label="张三-固收")
    cfg = make_cfg(allow_ops=["*", "account_stop"], archive_dir=str(tmp_path / "mail-archive"))
    r.imap, r.pop3, r.smtp = FakeImap(), FakePop3(), FakeSmtp()
    r.agent.mail = MailService(r.store, cfg, catalog=build_catalog(), clock=r.clock, alerts=r.agent.alerts,
                               secret_of=lambda ref: SECRET,
                               imap_factory=lambda route: r.imap, pop3_factory=lambda route: r.pop3,
                               smtp_factory=lambda route: r.smtp)
    yield r
    close_rig(r)


def _pending(rig, uid: int = 1) -> int:
    """真走取信 → 解析 → 白名单路径造一条 `CONFIRM_REQUIRED`(不直插库,免得绕开 R6-7 同事务写 expires 的那一步)。"""
    raw = command_mail(req_id=f"r{uid}", op="account_stop", session="", args={})
    mailbox = next(iter(rig.agent.mail.fetchers))
    status = rig.agent.mail.ingest.ingest_raw(RawMail(mailbox=mailbox, protocol="imap", folder="INBOX",
                                                      size_bytes=len(raw), raw=raw, uid=uid, uidvalidity=1))
    assert status == CONFIRM_REQUIRED
    return rig.agent.mail.ms.inbox_list(limit=1)[0]["id"]


def _get(rig, path: str, token: str | None = None, **params: Any) -> dict[str, Any]:
    r = rig.client.get(f"{P}{path}", headers=H(token) if token else H(), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _iso_ms(s: str) -> int:
    dt = datetime.fromisoformat(s)
    assert dt.utcoffset() is not None, f"时间没带偏移:{s}"
    return int(dt.timestamp() * 1000)


def _mk_cleanup(rig, i: int, *, started_ms: int, **cols: Any) -> int:
    base = dict(started_ms=started_ms, finished_ms=started_ms + 1500, trigger="retention", protocol="imap",
                folder="INBOX", candidates=i, archived=i, deleted=i, failed=0, bytes_freed=1024 * i, status="OK",
                detail={"skipped_oversize": i, "skipped_out_of_scope": 0})
    base.update(cols)
    return rig.agent.mail.ms.cleanup_log_insert(**base)


# ══════════════════════════════════════════════════ #68b pending-confirms
def test_pending_confirms_times_are_iso_not_ms(rig):
    """改前:`{"created_at": 1789956713427, "expires_at": 1789957613427}` —— 键名 `*_at`、值毫秒整数(F-1)。"""
    _pending(rig)
    item = _get(rig, "/mail/pending-confirms")["data"][0]
    assert isinstance(item["created_at"], str) and item["created_at"].endswith("+08:00"), item
    assert isinstance(item["expires_at"], str) and item["expires_at"].endswith("+08:00"), item


def test_pending_confirms_eight_keys_and_ttl_consistent(rig):
    """R6-7 八键逐字;`expires_at - created_at` = `danger_confirm_ttl_s`(默认 900s),`remaining_ttl_s` 仍是服务端算的整数。"""
    iid = _pending(rig)
    item = _get(rig, "/mail/pending-confirms")["data"][0]
    assert set(item) == PENDING_KEYS
    row = rig.agent.mail.ms.inbox_get(iid)
    assert _iso_ms(item["created_at"]) == row["received_ms"] // 1000 * 1000
    assert _iso_ms(item["expires_at"]) == row["confirm_expires_ms"] // 1000 * 1000
    # 过期 = **受理时刻** + ttl(R6-7),受理比收信时刻略晚(假时钟每次取时自动步进)⇒ 差值落在 [900, 901) 秒
    assert 900_000 <= _iso_ms(item["expires_at"]) - _iso_ms(item["created_at"]) <= 901_000
    assert isinstance(item["remaining_ttl_s"], int) and 0 < item["remaining_ttl_s"] <= 900


def test_pending_confirms_id_is_string_and_round_trips_to_approve(rig):
    """`id` 与 #58/#59 的同一个 `mail_inbox.id` 同型出字符串(第四批 D-3);拿它原样拼 #68c 的路径照常能批。"""
    iid = _pending(rig)
    item = _get(rig, "/mail/pending-confirms")["data"][0]
    assert item["id"] == str(iid)
    r = rig.client.post(f"{P}/mail/pending-confirms/{item['id']}/approve", headers=H())
    assert r.status_code in (200, 202), r.text
    assert _get(rig, "/mail/pending-confirms")["data"] == []


def test_pending_confirms_service_list_is_the_same_view(rig):
    """`confirms.list()`(验收 M87 钉的就是它)与端点同一份视图,不存在「内部毫秒、端点 ISO」两套。"""
    _pending(rig)
    assert rig.agent.mail.confirms.list() == _get(rig, "/mail/pending-confirms")["data"]


def test_pending_confirms_missing_expiry_is_null_not_1970(rig):
    """`confirm_expires_ms` 按 R6-7 与状态同事务写、正常不为空;万一为空也不能回 1970 年,且剩余秒数 0。"""
    iid = _pending(rig)
    with rig.store._tx() as c:
        c.execute("UPDATE mail_inbox SET confirm_expires_ms=NULL WHERE id=?", (iid,))
    item = _get(rig, "/mail/pending-confirms")["data"][0]
    assert item["expires_at"] is None and item["remaining_ttl_s"] == 0


# ══════════════════════════════════════════════════ #65 cleanup/log
def test_cleanup_log_row_is_a_view(rig):
    """改前:`started_ms/finished_ms` 毫秒整数、`detail_json` 是 JSON **字符串**、`id` 整数(F-2)。"""
    _mk_cleanup(rig, 3, started_ms=T0)
    row = _get(rig, "/mail/cleanup/log")["data"][0]
    assert set(row) == CLEANUP_KEYS
    assert not [k for k in row if k.endswith("_ms") or k.endswith("_json")]
    assert row["id"] == "1"
    assert _iso_ms(row["started_at"]) == T0 and _iso_ms(row["finished_at"]) == T0 + 1000   # 秒精度
    assert row["detail"] == {"skipped_oversize": 3, "skipped_out_of_scope": 0}          # R6-26 的可观测证据是对象
    assert row["deleted"] == 3 and row["status"] == "OK" and row["trigger"] == "retention"


def test_cleanup_log_is_c42_paginated_without_dup_or_loss(rig):
    """02 #65「分页」⇒ C-42:`next_cursor` 只在满页非空;排序列撞值(`tie`)时靠 `(started_ms, id)` 双键不重不漏。"""
    ids = [_mk_cleanup(rig, i, started_ms=T0 - (i // 2) * 1000) for i in range(7)]      # 两两同毫秒
    seen: list[str] = []
    cur = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cur:
            params["cursor"] = cur
        body = _get(rig, "/mail/cleanup/log", **params)
        assert "next_cursor" in body
        seen += [r["id"] for r in body["data"]]
        pages += 1
        cur = body["next_cursor"]
        if cur is None:
            break
        assert len(body["data"]) == 3
    assert pages == 3
    assert sorted(seen, key=int) == [str(i) for i in ids] and len(seen) == len(set(seen))
    starts = [_iso_ms(r["started_at"]) for r in _get(rig, "/mail/cleanup/log", limit=500)["data"]]
    assert starts == sorted(starts, reverse=True)


def test_cleanup_log_short_page_has_null_cursor(rig):
    _mk_cleanup(rig, 1, started_ms=T0)
    assert _get(rig, "/mail/cleanup/log", limit=5)["next_cursor"] is None


def test_cleanup_log_time_window_and_bad_cursor(rig):
    for i in range(3):
        _mk_cleanup(rig, i, started_ms=T0 + i * 60_000)
    since = datetime.fromtimestamp((T0 + 60_000) / 1000).astimezone().isoformat()
    got = _get(rig, "/mail/cleanup/log", since=since)["data"]
    assert [r["candidates"] for r in got] == [2, 1]
    r = rig.client.get(f"{P}/mail/cleanup/log", headers=H(), params={"cursor": "%%%不是游标"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ARGS"


def test_cleanup_log_is_read_level(rig):
    _mk_cleanup(rig, 1, started_ms=T0)
    assert _get(rig, "/mail/cleanup/log", token=TOKEN_READ)["data"]


# ══════════════════════════════════════════════════ #56 mail/status(同型)
def test_mail_status_times_are_iso(rig):
    """改前:`inbound.last_success_at` = `last_success_ms`、`cleanup.last_run_at` = `finished_ms`,都是毫秒整数;
    控制台 `MailStatus` 把它们当字符串直接渲染(P-MAIL「最近成功」、P-DASH「最近收信」)。"""
    fetcher = next(iter(rig.agent.mail.fetchers.values()))
    st = fetcher.state()
    st.update({"last_success_ms": T0, "fallback_since": T0 - 60_000})
    fetcher._save(st)
    _mk_cleanup(rig, 1, started_ms=T0 - 5000)
    route = _get(rig, "/mail/status")["data"][0]
    assert _iso_ms(route["inbound"]["last_success_at"]) == T0
    assert _iso_ms(route["inbound"]["fallback_since"]) == T0 - 60_000
    assert _iso_ms(route["cleanup"]["last_run_at"]) == T0 - 5000 + 1000                  # finished_ms,秒精度
    assert route["cleanup"]["last_status"] == "OK"


def test_mail_status_never_run_is_null(rig):
    route = _get(rig, "/mail/status")["data"][0]
    assert route["cleanup"]["last_run_at"] is None
    assert route["inbound"]["last_success_at"] is None and route["inbound"]["fallback_since"] is None
