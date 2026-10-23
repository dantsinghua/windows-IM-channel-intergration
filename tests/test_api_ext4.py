"""第四批 API 修复的开发者测试(独立联调 `.omc/handoffs/e2e-recheck-2.md` 的 P-1/P-3/P-4/P-5)。

覆盖:
- **P-1**:`#58 /mail/inbox`、`#59 /mail/inbox/{id}`、`#61 /mail/outbox` 走**出参视图**而不是库行原样 ——
  时间一律 ISO 8601 带偏移(00 §6「时间(API/事件/**邮件**)」)、列表**不含** `body_text`(#58 逐字「详情才给」)、
  库内部列不下发;C-42 翻页行为(`next_cursor` 仅满页、游标跨页不重不漏)**不回退**;
- **P-3**:`#89 PUT /settings/{group}` 对**未知键** `400 INVALID_ARGS` + `details[].pointer`,**整个请求不落库**;
- **P-4**:建号序号用尽 → `409 RESOURCE_EXHAUSTED` 信封(不是 `500 text/plain`);任何**未捕获异常**
  → 00 §10 的 `INTERNAL` 信封 + `trace_id` + 日志,响应体不带堆栈;`/system/health` 的 trace_id 例外不被破坏;
- **P-5**:`GET /mail/hmac-keys` **不分页**,`limit` 不再静默截断短名表。

后端全是假件(复用 `test_api_ext.Rig`),**不碰真 docker / adb / WinAgent / 出网**。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from test_api_ext import Rig, H, P, TOK_W

T0 = 1_700_000_000_000


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _mk_inbox(rig, i: int, **cols: Any) -> int:
    """一行 `mail_inbox`(带正文,专门用来验「列表不给、详情才给」)。"""
    base = dict(mailbox="ops@corp", protocol="imap", folder="INBOX", uid=100 + i,
                rfc_message_id=f"<in{i}@qtrade>", from_addr=f"ops{i}@corp", subject=f"指令 {i}",
                body_sha256=f"sha-{i}", body_text=f"正文 {i}:操作:find_contact", status="DONE",
                received_ms=T0 + i * 1000, date_ms=T0 + i * 1000 - 500)
    base.update(cols)
    return rig.agent.mail.ms.inbox_insert(**base)


def _mk_outbox(rig, i: int, **cols: Any) -> int:
    base = dict(kind="receipt", to_addrs=f"ops{i}@corp", subject=f"回执 {i}", body_text="正文不该进列表",
                rfc_message_id=f"<out{i}@qtrade>", dedup_key=f"d{i}", template_version="v1", now_ms=T0 + i * 1000)
    base.update(cols)
    return rig.agent.mail.ms.outbox_enqueue(**base)


def _get(rig, path: str, **params: Any) -> dict[str, Any]:
    r = rig.client.get(f"{P}{path}", headers=H(), params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ══════════════════════════════════════════════════ P-1 收发件列表是出参视图,不是库行
def test_inbox_list_row_has_iso_time_and_no_body(rig):
    """02 #58 逐字「`mail_inbox` 行(**不含 `body_text`**,详情才给)」+ 00 §6「时间(API/事件/**邮件**)= ISO 8601 带时区偏移」。

    改前:`SELECT *` 原样透出 ⇒ 行里有 `body_text`(真实取信后就是正文全文)与
    `date_ms/received_ms/confirm_expires_ms/archived_ms/deleted_ms` 五个毫秒整数、一个 `*_at` 都没有(X-2/X-3)。
    """
    _mk_inbox(rig, 0)
    row = _get(rig, "/mail/inbox")["data"][0]
    assert "body_text" not in row, "#58 列表把邮件正文全量下发了(数据面与隐私面都不该,也与 #59 的分工冲突)"
    assert not [k for k in row if k.endswith("_ms")], f"列表还在透出库列 *_ms:{[k for k in row if k.endswith('_ms')]}"
    assert row["received_at"].endswith("+08:00") and row["date_at"].endswith("+08:00")
    assert row["confirm_expires_at"] is None and row["archived_at"] is None and row["deleted_at"] is None


def test_inbox_list_row_key_set_is_pinned(rig):
    """键集定死(转文档方登记):02 #58 的「`mail_inbox` 行」**减 `body_text`**,时间列换成 `*_at`,
    另加两个派生键 `route`(01 §2.7.8 收件时间线的 route 列)与 `archived`(同句的「是否已归档」)。"""
    _mk_inbox(rig, 0)
    row = _get(rig, "/mail/inbox")["data"][0]
    assert set(row) == {
        "id", "route_id", "route", "mailbox", "protocol", "effective_protocol", "folder",
        "uidvalidity", "uid", "uidl", "rfc_message_id", "from_addr", "to_addrs", "subject",
        "date_at", "received_at", "size_bytes", "body_sha256", "template", "template_version",
        "status", "reason", "req_id", "account_id", "op", "idempotency_key", "nonce", "args_digest",
        "confirm_expires_at", "sig_ok", "trace_id", "command_id", "first_inbox_id",
        "attach_cnt", "attach_json", "raw_ref", "archived", "archived_path", "archived_at",
        "deleted_at", "fail_count",
    }


def test_inbox_detail_gives_body_text(rig):
    """#59「详情才给」:同一个视图 + `body_text`;时间同样是 ISO。"""
    iid = _mk_inbox(rig, 0)
    row = _get(rig, f"/mail/inbox/{iid}")["data"]
    assert row["body_text"] == "正文 0:操作:find_contact"
    assert row["received_at"].endswith("+08:00") and not [k for k in row if k.endswith("_ms")]
    assert row["id"] == str(iid)


def test_inbox_row_id_is_string_and_sig_ok_is_bool(rig):
    """`id` 按控制台 `MailInboxRow.id: string` 给字符串;`sig_ok` 由 SQLite 的 0/1 转布尔(未验签时 `null`)。"""
    iid = _mk_inbox(rig, 0, sig_ok=1)
    row = _get(rig, "/mail/inbox")["data"][0]
    assert row["id"] == str(iid) and isinstance(row["id"], str)
    assert row["sig_ok"] is True
    _mk_inbox(rig, 1)
    assert _get(rig, "/mail/inbox")["data"][0]["sig_ok"] is None


def test_inbox_archived_flag_follows_archive_path(rig):
    """`archived` = 01 §2.7.8 的「是否已归档」列,由 `archived_path`/`archived_ms` 判。"""
    _mk_inbox(rig, 0)
    assert _get(rig, "/mail/inbox")["data"][0]["archived"] is False
    _mk_inbox(rig, 1, archived_path="/var/qtrade/mail/2026/x.eml", archived_ms=T0)
    row = _get(rig, "/mail/inbox")["data"][0]
    assert row["archived"] is True and row["archived_at"].endswith("+08:00")


def test_outbox_list_row_key_set_and_iso(rig):
    """#61 的行键集出处 = 01 §2.7.8 发件队列逐字 `kind/to/subject/status/attempts/next_attempt_at/last_error/ref`,
    另加 `id`、`route_id`/`route`、排序列 `created_at` 与 `sent_at`;正文与投递内部列不下发。"""
    _mk_outbox(rig, 0)
    row = _get(rig, "/mail/outbox")["data"][0]
    assert set(row) == {"id", "kind", "route_id", "route", "to", "subject", "status", "attempts",
                        "next_attempt_at", "last_error", "ref", "created_at", "sent_at"}
    assert "body_text" not in row and "dedup_key" not in row and "smtp_response" not in row
    assert row["created_at"].endswith("+08:00") and row["to"] == "ops0@corp"
    assert not [k for k in row if k.endswith("_ms")]


def test_outbox_next_attempt_zero_is_null_not_1970(rig):
    """`next_attempt_ms` 的 DDL 默认值 0 = 「没有下次」⇒ 回 `null`,不回 1970-01-01。"""
    _mk_outbox(rig, 0)
    with rig.store._tx() as c:
        c.execute("UPDATE mail_outbox SET next_attempt_ms=0, status='SENT', sent_ms=?", (T0 + 5000,))
    row = _get(rig, "/mail/outbox")["data"][0]
    assert row["next_attempt_at"] is None and row["sent_at"].endswith("+08:00")


def test_outbox_ref_prefers_trace_then_message_then_inbox(rig):
    """`ref` = 关联对象引用:`ref_trace_id` → `ref_message_id` → `ref_inbox_id`(字符串)。"""
    _mk_outbox(rig, 0, ref_inbox_id=7)
    _mk_outbox(rig, 1, ref_message_id="msg_01J8")
    _mk_outbox(rig, 2, ref_trace_id="01J8TRACE")
    by_subject = {r["subject"]: r["ref"] for r in _get(rig, "/mail/outbox")["data"]}
    assert by_subject["回执 0"] == "7" and by_subject["回执 1"] == "msg_01J8" and by_subject["回执 2"] == "01J8TRACE"


def test_mail_rows_carry_route_scope(rig):
    """`route` = 该行 `route_id` 落在哪个 scope(01 §4 `qt-mail-health-route-{scope}` 的同一套取值)。"""
    ms = rig.agent.mail.ms
    rid_global = ms.route_upsert(channel=None, account_id=None, inbound_json={}, outbound_json={})
    rid_qq = ms.route_upsert(channel="qq", account_id=None, inbound_json={}, outbound_json={})
    _mk_inbox(rig, 0, route_id=rid_global)
    _mk_inbox(rig, 1, route_id=rid_qq)
    _mk_outbox(rig, 0, route_id=rid_qq)
    scopes = {r["subject"]: r["route"] for r in _get(rig, "/mail/inbox")["data"]}
    assert scopes == {"指令 0": "default", "指令 1": "qq"}
    assert _get(rig, "/mail/outbox")["data"][0]["route"] == "qq"
    # 没绑路由的行 route 为 null(不是硬当成 default)
    _mk_inbox(rig, 2)
    assert _get(rig, "/mail/inbox")["data"][0]["route"] is None


def test_mail_pagination_survives_the_view(rig):
    """🔴 C-42 回归守卫:出参换成视图之后,`next_cursor`(仅满页非空)与游标翻页**不重不漏**照旧 ——
    游标按**库行**的 `received_ms`/`created_ms` 算,不受视图里没有 `*_ms` 影响。"""
    for i in range(5):
        _mk_inbox(rig, i)
        _mk_outbox(rig, i)
    for path in ("/mail/inbox", "/mail/outbox"):
        seen, cur, pages = [], None, 0
        while True:
            body = _get(rig, path, limit=2, **({"cursor": cur} if cur else {}))
            seen += [r["id"] for r in body["data"]]
            pages += 1
            cur = body["next_cursor"]
            if not cur:
                break
            assert pages < 10, "翻页没有终点"
        assert len(seen) == 5 and len(set(seen)) == 5, f"{path} 翻页重复或漏行:{seen}"
        assert pages == 3, f"{path} 该翻 3 页(2+2+1),实得 {pages}"
        last = _get(rig, path, limit=5)
        assert last["next_cursor"] is None or len(last["data"]) == 5


def test_mail_time_filters_still_take_iso(rig):
    """`since`/`until` 的入参口径不变(ISO 或毫秒),只有**出参**换成 ISO。"""
    _mk_inbox(rig, 0)
    _mk_inbox(rig, 9)
    body = _get(rig, "/mail/inbox", since=str(T0 + 5000))
    assert [r["subject"] for r in body["data"]] == ["指令 9"]


# ══════════════════════════════════════════════════ P-3 #89 未知键 ⇒ 400,整个请求不落库
def test_put_settings_rejects_unknown_key(rig):
    """#89 是**整组替换(缺省键回默认)**,照单全收未知键 ⇒ 一处笔误静默重置整组(独立联调 P-SET 的真实症状)。

    总控 2026-09-21 裁决:未知键 ⇒ `400 INVALID_ARGS`,`details[].pointer` 指到该键,**整个请求不落库**。
    """
    r = rig.client.put(f"{P}/settings/retention", headers=H(),
                       json={"files_days": 7, "text_days": 20, "raw_enabled": True, "audit_days": 30})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "INVALID_ARGS" and body["error"]["reason"] == "unknown_key"
    ptrs = {d["pointer"] for d in body["error"]["details"]}
    assert ptrs == {"/text_days", "/raw_enabled"}, ptrs
    assert rig.store.settings_get("config.retention") is None, "被拒的请求不许留下任何落库痕迹"


def test_put_settings_known_keys_unchanged(rig):
    """已知键的现行语义一个字不变:整组落库 + `restart_required` + E-18 截断 WARN 照旧。"""
    r = rig.client.put(f"{P}/settings/retention", headers=H(), json={"messages_days": 365, "audit_days": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["messages_days"] == 30 and body["warnings"] and body["restart_required"] is True
    assert rig.store.settings_get("config.retention")["audit_days"] == 30


def test_put_settings_unknown_key_blocks_before_public_domain_write(rig):
    """「整个请求不落库」含 `api` 组那条**先于校验**的 `public_domain` 旁路(它写的是 `settings`,不是组值)。"""
    r = rig.client.put(f"{P}/settings/api", headers=H(), json={"public_domain": "a.example", "prot": 17600})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "unknown_key"
    assert rig.store.settings_get("api.public_domain") is None, "未知键的请求把 public_domain 写进去了"


def test_put_settings_allows_write_only_secret_keys(rig):
    """密码类(`*_secret`/`*_password`/`*_token`)是「只写不读」的入参,不在读回字段里 ⇒ 按后缀放行。"""
    r = rig.client.put(f"{P}/settings/winagent", headers=H(), json={"timeout_ms": 5000, "secret": "p@ss"})
    assert r.status_code == 200, r.text
    assert r.json()["secret_refs"]["secret"].startswith("vault://settings/winagent/")


def test_put_settings_unknown_group_key_by_group(rig):
    """键集按**组**判:`messages` 组的合法键放到 `retention` 组就是未知键。"""
    ok = rig.client.put(f"{P}/settings/messages", headers=H(), json={"late_after_s": 90})
    assert ok.status_code == 200, ok.text
    bad = rig.client.put(f"{P}/settings/retention", headers=H(), json={"late_after_s": 90})
    assert bad.status_code == 400 and bad.json()["error"]["details"][0]["pointer"] == "/late_after_s"


def test_put_settings_resources_and_adapters_key_sets(rig):
    """`resources`/`adapters` 的顶层键集由 #88 定死(`pools`/`quota_mb`;三通道名)。"""
    bad = rig.client.put(f"{P}/settings/resources", headers=H(), json={"pools": {}, "quotas": {"qq": 1}})
    assert bad.status_code == 400 and bad.json()["error"]["details"][0]["pointer"] == "/quotas"
    bad2 = rig.client.put(f"{P}/settings/adapters", headers=H(), json={"telegram": {}})
    assert bad2.status_code == 400 and bad2.json()["error"]["reason"] == "unknown_key"


def test_put_settings_mail_group_unknown_top_key(rig):
    """`mail` 组的形状由 #88(R6-58 (ac))逐字定死:`{enabled, require_signature, template_version, scopes}`。

    ⚠️ `PUT /settings/mail` 这个 URL 命中的是**具体路由** `put_settings_mail`(注册在 `{group}` 兜底之前),
    `put_settings_group` 的 mail 分支走不到 —— 所以这道门必须在 `put_settings_mail` 里也有一份,本条守的就是它。
    """
    r = rig.client.put(f"{P}/settings/mail", headers=H(), json={"scopes": {}, "enabled": True, "senders": []})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "unknown_key"
    assert [d["pointer"] for d in r.json()["error"]["details"]] == ["/senders"]
    ok = rig.client.put(f"{P}/settings/mail", headers=H(), json={"scopes": {}})
    assert ok.status_code == 200, ok.text


# ══════════════════════════════════════════════════ P-4 序号用尽 / 兜底异常都走 00 §10 信封
def test_seq_exhausted_is_409_resource_exhausted(rig):
    """00 §6:`NN = 01–98` 且**分配后永不复用** ⇒ 一个通道累计建满 98 个号后必然建不了,这是可预见业务边界。

    改前:`Store._next_seq` 抛裸 `ValueError` ⇒ `500 text/plain "Internal Server Error"`,控制台拿不到
    `code`/`trace_id`,而 500 `INTERNAL` 还会被当成服务器故障**自动重试**(02 §3.4),重试多少次都建不出来。
    """
    rig.store.settings_set("seq.qq", 98, actor="test")
    r = rig.client.post(f"{P}/accounts", headers=H(),
                        json={"channel": "qq", "label": "第 99 个", "login_mode": "qrcode", "idempotency_key": "ovf-1"})
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["code"] == "RESOURCE_EXHAUSTED" and body["error"]["reason"] == "seq_exhausted"
    assert body["error"]["retryable"] is False and body["error"]["needs_human"] is True
    assert body["error"]["channel"] == "qq" and body["error"]["max_seq"] == 98
    assert body["trace_id"]


def test_seq_exhausted_does_not_burn_the_last_seq(rig):
    """用尽是在事务里判的:被拒之后 `seq` 不前进(第 98 号仍是最后一个已分配的号)。"""
    rig.store.settings_set("seq.qq", 97, actor="test")
    ok = rig.client.post(f"{P}/accounts", headers=H(),
                         json={"channel": "qq", "label": "第 98 个", "login_mode": "qrcode", "idempotency_key": "k98"})
    assert ok.status_code in (200, 201), ok.text
    assert rig.store.settings_get("seq.qq") == 98
    bad = rig.client.post(f"{P}/accounts", headers=H(),
                          json={"channel": "qq", "label": "第 99 个", "login_mode": "qrcode", "idempotency_key": "k99"})
    assert bad.status_code == 409
    assert rig.store.settings_get("seq.qq") == 98


def test_unhandled_exception_becomes_internal_envelope(rig, monkeypatch):
    """🔴 兜底异常处理器:**任何**未捕获异常 ⇒ 00 §10 信封的 `INTERNAL`(§8.3 已登记的未归类错误码)+ `trace_id`。

    改前只有 `ApiError`/`DiskFullError`/`HTTPException`/422 四条路有信封,其余一律 `500 text/plain` ——
    控制台的 `readEnvelope` 只能退化成「请求失败」,现场也无从按 trace_id 查审计。
    """
    def boom(*a, **k):
        raise RuntimeError("内部细节:secret=hunter2 /home/anlin/agent.db")
    monkeypatch.setattr(rig.store, "list_accounts_page", boom)
    r = rig.client.get(f"{P}/accounts", headers=H(), params={"limit": 5})
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is False and body["code"] == "INTERNAL"
    assert body["error"]["reason"] == "unhandled_exception" and body["error"]["retryable"] is True
    assert body["trace_id"]
    assert "hunter2" not in r.text and "RuntimeError" not in r.text, "响应体不许泄堆栈/内部细节"
    assert r.headers.get("X-QT-Api-Version"), "兜底路径也要带版本头"


def test_unhandled_exception_honours_trace_id_header_and_audit(rig, monkeypatch):
    """调用方给的 `X-Trace-Id` 照样采纳,且这一次失败**进审计**(拿着界面上那串就能查到行)。"""
    def boom(*a, **k):
        raise RuntimeError("炸")
    monkeypatch.setattr(rig.store, "list_accounts_page", boom)
    r = rig.client.get(f"{P}/accounts", headers=H(**{"X-Trace-Id": "01J8FIXEDTRACE"}))
    assert r.status_code == 500 and r.json()["trace_id"] == "01J8FIXEDTRACE"
    rows = rig.client.get(f"{P}/audit", headers=H(), params={"kind": "api", "limit": 50}).json()["data"]
    assert any(x.get("trace_id") == "01J8FIXEDTRACE" and x["result_code"] == "500" for x in rows)


def test_health_path_keeps_its_trace_id_exception_even_when_it_blows_up(rig, monkeypatch):
    """02 §3.4 例外①(R6-62 Ⅵ W1):`#72 GET /system/health` **这个路径**两种形态都不注入 `trace_id` ——
    兜底器不得把这条例外破坏掉(也仍不记审计)。"""
    def boom(*a, **k):
        raise RuntimeError("health 炸了")
    monkeypatch.setattr(rig.store, "db_size_mb", boom)
    before = len(rig.client.get(f"{P}/audit", headers=H(), params={"kind": "api", "limit": 500}).json()["data"])
    r = rig.client.get(f"{P}/system/health", headers=H())
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL"
    assert r.json()["trace_id"] is None, "health 这个路径两种形态都不注入 trace_id"
    after = rig.client.get(f"{P}/audit", headers=H(), params={"kind": "api", "limit": 500}).json()["data"]
    assert len([x for x in after if "/system/health" in x["action"]]) == 0
    assert len(after) >= before


def test_api_error_paths_are_untouched_by_the_catch_all(rig):
    """兜底器**不许**吞掉已有的分诊:404/405/422/401 仍是各自的码与状态。"""
    assert rig.client.get(f"{P}/nope", headers=H()).json()["code"] == "NOT_FOUND"
    assert rig.client.get(f"{P}/accounts", headers=H(), params={"limit": 0}).status_code == 422
    assert rig.client.get(f"{P}/accounts").status_code == 401
    assert rig.client.get(f"{P}/mail/hmac-keys", headers=H(TOK_W)).status_code == 403


# ══════════════════════════════════════════════════ P-5 GET /mail/hmac-keys 不分页
def test_hmac_keys_is_not_paginated_and_never_truncates(rig):
    """改前:接受 `limit` 却**不回 `next_cursor`** ⇒ 超出 `limit` 的短名被静默丢掉。

    短名少一行 = 那位发件人的指令邮件全被 `SENDER_DENIED`,而 P-SET 的白名单看着是全的。
    本端点至今没有编号、不在 §3.4 的 C-42 端点表里,且 settings 键值对没有 G-16 游标要的排序列与行主键
    ⇒ 定为**不分页、全量返回**(理由与出处见 app.py 该端点 docstring,转文档方随编号登记)。
    """
    plain = []
    for i in range(3):
        made = rig.client.post(f"{P}/mail/hmac-keys", headers=H(), json={"sender": f"ops{i}@corp", "short_name": f"ops{i}"})
        assert made.status_code == 200, made.text
        plain.append(made.json()["secret"])
    body = _get(rig, "/mail/hmac-keys", limit=1)
    assert len(body["data"]) == 3, "传了 limit 就把短名表截断了(静默丢行)"
    assert "next_cursor" not in body, "本端点不分页,不该回 next_cursor(前端会以为还有下一页)"
    assert {r["short_name"] for r in body["data"]} == {"ops0", "ops1", "ops2"}
    raw = json.dumps(body, ensure_ascii=False)
    assert all(pw not in raw for pw in plain), "短名表回了密钥明文"   # 只回 secret_ref
