"""backend-sec-1(S-8):邮件路由密钥「只进 Vault、库里只落 `vault://` 引用」。

规格:02 #105「`inbound.secret/outbound.secret` 只写不读(写 `vault://mail/route/<id>/imap|pop3|smtp` 并回 `*_ref`)」;
02 附录提议 10「全局路由沿用 `mail/imap|pop3|smtp`」;00 §11.1 [CRED] / §11.2 [NOLOG]。
断言一律**直接查 SQLite 原始行**(不经任何出参视图),证明明文没进库。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from qtrade_agent.mail.backends import FakeImap, FakePop3, FakeSmtp
from qtrade_agent.mail.route_secrets import migrate_plaintext
from qtrade_agent.mail.routes import MailRoute
from qtrade_agent.mail.service import MailService
from tests.test_integration_wiring_common import H, close_rig, make_rig
from tests.test_mail_common import SECRET, build_catalog, make_cfg

P = "/api/v1"
PW_IN, PW_OUT, PW_FB, PW_TOK = "S3cr3t-IN-不可见", "S3cr3t-OUT-不可见", "S3cr3t-FB-不可见", "S3cr3t-TOK-不可见"
ALL_PW = (PW_IN, PW_OUT, PW_FB, PW_TOK)


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    cfg = make_cfg(archive_dir=str(tmp_path / "mail-archive"))
    r.imap, r.pop3, r.smtp = FakeImap(), FakePop3(), FakeSmtp()
    r.pw_seen: list[tuple[str, str]] = []

    def _imap(route):                          # 与生产 `_real_imap` 同一取密路径:secret_ref → Vault
        r.pw_seen.append(("imap", r.agent._mail_secret(route.inbound.secret_ref)))
        return r.imap

    def _smtp(route):
        r.pw_seen.append(("smtp", r.agent._mail_secret(route.outbound.secret_ref)))
        return r.smtp

    r.agent.mail = MailService(r.store, cfg, catalog=build_catalog(), clock=r.clock, alerts=r.agent.alerts,
                               secret_of=lambda ref: SECRET, imap_factory=_imap,
                               pop3_factory=lambda route: r.pop3, smtp_factory=_smtp)
    yield r
    close_rig(r)


def _raw_rows(rig) -> list[dict[str, Any]]:
    return [dict(x) for x in rig.store.con.execute("SELECT * FROM mail_routes ORDER BY id")]


def _raw_text(rig) -> str:
    """mail_routes 全表原始列拼起来(明文不管藏在哪个键、哪层嵌套都搜得到)。"""
    return json.dumps(_raw_rows(rig), ensure_ascii=False)


def _vault(rig) -> dict[str, str]:
    return {k: e.value for k, e in rig.agent.vault.entries.items()}


def _row(rig, rid) -> dict[str, Any]:
    r = next(x for x in _raw_rows(rig) if str(x["id"]) == str(rid))
    return {"in": json.loads(r["inbound_json"]), "out": json.loads(r["outbound_json"]), "enabled": r["enabled"]}


def _put_route(rig, **body: Any):
    body.setdefault("channel", "qidian")
    body.setdefault("enabled", True)
    return rig.client.put(f"{P}/settings/mail/routes", headers=H(), json=body)


def _full_body() -> dict[str, Any]:
    return {"inbound": {"protocol": "imap", "host": "imap.q.example", "user": "q@x", "secret": PW_IN,
                        "fallback": {"host": "pop.q.example", "password": PW_FB}, "api_token": PW_TOK},
            "outbound": {"host": "smtp.q.example", "user": "q@x", "password": PW_OUT}}


# ══════════════════════════════════════════════════ 写入侧 #105 PUT
def test_put_route_stores_only_refs_and_vault_has_values(rig):
    r = _put_route(rig, **_full_body())
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    raw = _raw_text(rig)
    for pw in ALL_PW:
        assert pw not in raw                                    # 原始行里搜不到任何明文
    row = _row(rig, rid)
    assert row["in"]["secret_ref"] == f"vault://mail/route/{rid}/imap"
    assert row["out"]["secret_ref"] == f"vault://mail/route/{rid}/smtp"
    for blob in (row["in"], row["out"], row["in"]["fallback"]):
        assert not {"secret", "password", "api_token"} & set(blob)
    v = _vault(rig)
    assert v[f"mail/route/{rid}/imap"] == PW_IN and v[f"mail/route/{rid}/smtp"] == PW_OUT
    assert rig.agent.vault.entries[f"mail/route/{rid}/imap"].scope == "mail"
    assert PW_FB not in v.values() and PW_TOK not in v.values()  # 非规范位置的密钥键只摘不存


def test_get_never_returns_plaintext_only_refs(rig):
    rid = _put_route(rig, **_full_body()).json()["id"]
    for path in ("/settings/mail/routes", "/settings/mail"):
        t = rig.client.get(f"{P}{path}", headers=H()).text
        for pw in ALL_PW:
            assert pw not in t
    rows = rig.client.get(f"{P}/settings/mail/routes", headers=H()).json()["data"]
    me = next(x for x in rows if x["id"] == rid)
    assert me["inbound"]["secret_ref"] == f"vault://mail/route/{rid}/imap"
    assert me["outbound"]["secret_ref"] == f"vault://mail/route/{rid}/smtp"


def test_put_without_secret_keeps_existing_ref(rig):
    rid = _put_route(rig, **_full_body()).json()["id"]
    # 控制台读不回明文,整组回写(不带 secret、带或不带 secret_ref 都算)⇒ 原引用不动、Vault 值不动
    r = _put_route(rig, inbound={"protocol": "imap", "host": "imap2.q.example", "user": "q@x"},
                   outbound={"host": "smtp.q.example", "user": "q@x", "secret": ""})
    assert r.status_code == 200 and r.json()["id"] == rid
    row = _row(rig, rid)
    assert row["in"]["host"] == "imap2.q.example"
    assert row["in"]["secret_ref"] == f"vault://mail/route/{rid}/imap"
    assert row["out"]["secret_ref"] == f"vault://mail/route/{rid}/smtp"
    assert _vault(rig)[f"mail/route/{rid}/imap"] == PW_IN and _vault(rig)[f"mail/route/{rid}/smtp"] == PW_OUT
    assert "secret" not in row["out"]


def test_put_new_secret_overwrites_vault_value(rig):
    rid = _put_route(rig, **_full_body()).json()["id"]
    before = rig.agent.vault.entries[f"mail/route/{rid}/imap"].version
    _put_route(rig, inbound={"protocol": "imap", "host": "imap.q.example", "user": "q@x", "secret": "NEW-PW-1"},
               outbound={"host": "smtp.q.example"})
    e = rig.agent.vault.entries[f"mail/route/{rid}/imap"]
    assert e.value == "NEW-PW-1" and e.version == before + 1
    assert "NEW-PW-1" not in _raw_text(rig)


def test_put_null_secret_clears_ref_and_deletes_own_vault_entry(rig):
    rid = _put_route(rig, **_full_body()).json()["id"]
    r = _put_route(rig, inbound={"protocol": "imap", "host": "imap.q.example", "user": "q@x", "secret": None},
                   outbound={"host": "smtp.q.example"})
    assert r.status_code == 200
    row = _row(rig, rid)
    assert row["in"]["secret_ref"] == "" and "secret" not in row["in"]
    assert f"mail/route/{rid}/imap" not in _vault(rig) and f"mail/route/{rid}/imap" in rig.agent.vault.deleted
    assert _vault(rig)[f"mail/route/{rid}/smtp"] == PW_OUT          # 另一侧没动


def test_pop3_protocol_uses_pop3_path(rig):
    rid = _put_route(rig, channel="qq", inbound={"protocol": "pop3", "host": "pop.x", "user": "u", "secret": PW_IN},
                     outbound={}).json()["id"]
    assert _row(rig, rid)["in"]["secret_ref"] == f"vault://mail/route/{rid}/pop3"
    assert _vault(rig)[f"mail/route/{rid}/pop3"] == PW_IN


def test_foreign_secret_ref_rejected_400_and_nothing_written(rig):
    """body 自带的 secret_ref 只认原引用或邮件凭据规范路径:防借路由把 account/api 等别域密钥发去任意主机。"""
    n0, v0 = len(_raw_rows(rig)), dict(_vault(rig))
    r = _put_route(rig, channel="wechat", inbound={"host": "evil.example", "secret_ref": "vault://account/qd01"},
                   outbound={})
    err = r.json()["error"]
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ARGS"
    assert err["details"][0]["pointer"] == "/inbound/secret_ref"
    assert len(_raw_rows(rig)) == n0 and _vault(rig) == v0


def test_vault_offline_new_route_503_and_no_row_left(rig):
    n0 = len(_raw_rows(rig))
    rig.agent.vault.offline = True
    r = _put_route(rig, channel="wechat", **_full_body())
    assert r.status_code == 503 and r.json()["code"] == "NOT_READY"
    assert r.json()["error"]["reason"] == "vault_unavailable" and r.json()["error"]["retryable"] is True
    assert len(_raw_rows(rig)) == n0                                 # 占位行已删:库不落半截
    for pw in ALL_PW:
        assert pw not in _raw_text(rig) and pw not in r.text


def test_vault_offline_existing_route_row_untouched(rig):
    rid = _put_route(rig, **_full_body()).json()["id"]
    before = _raw_rows(rig)
    rig.agent.vault.offline = True
    r = _put_route(rig, inbound={"protocol": "imap", "host": "changed.example", "secret": "X-NEW"}, outbound={})
    assert r.status_code == 503
    assert _raw_rows(rig) == before and "X-NEW" not in _raw_text(rig)
    rig.agent.vault.offline = False
    assert _vault(rig)[f"mail/route/{rid}/imap"] == PW_IN


# ══════════════════════════════════════════════════ 写入侧 #89 PUT /settings/mail(scopes)
def test_put_settings_mail_scopes_seals_secrets(rig):
    body = {"scopes": {"default": {"inbound": {"protocol": "imap", "host": "imap.g.example", "user": "g@x", "secret": PW_IN},
                                   "outbound": {"host": "smtp.g.example", "user": "g@x", "secret": PW_OUT}},
                       "qq": {"inbound": {"protocol": "pop3", "host": "pop.qq.example", "user": "qq@x", "password": PW_FB},
                              "outbound": {}}}}
    r = rig.client.put(f"{P}/settings/mail", headers=H(), json=body)
    assert r.status_code == 200, r.text
    for pw in ALL_PW:
        assert pw not in _raw_text(rig)
    g = _row(rig, r.json()["written"]["default"])
    q = r.json()["written"]["qq"]
    assert g["in"]["secret_ref"] == "vault://mail/imap" and g["out"]["secret_ref"] == "vault://mail/smtp"   # 全局行沿用 mail/<proto>
    assert _row(rig, q)["in"]["secret_ref"] == f"vault://mail/route/{q}/pop3"
    v = _vault(rig)
    assert v["mail/imap"] == PW_IN and v["mail/smtp"] == PW_OUT and v[f"mail/route/{q}/pop3"] == PW_FB
    t = rig.client.get(f"{P}/settings/mail", headers=H()).text
    assert all(pw not in t for pw in ALL_PW)


def test_put_settings_mail_bad_ref_in_second_scope_writes_nothing(rig):
    before, v0 = _raw_rows(rig), dict(_vault(rig))
    body = {"scopes": {"qidian": {"inbound": {"host": "a.example", "secret": PW_IN}, "outbound": {}},
                       "qq": {"inbound": {"host": "b.example", "secret_ref": "vault://api/console"}, "outbound": {}}}}
    r = rig.client.put(f"{P}/settings/mail", headers=H(), json=body)
    assert r.status_code == 400 and r.json()["error"]["details"][0]["pointer"] == "/scopes/qq/inbound/secret_ref"
    assert _raw_rows(rig) == before and _vault(rig) == v0


# ══════════════════════════════════════════════════ 消费侧:取信 / 发信经引用从 Vault 取密
def test_fetch_and_send_resolve_password_via_ref(rig):
    rid = _put_route(rig, channel="qidian", account_id=None,
                     inbound={"protocol": "imap", "host": "imap.only-q.example", "user": "qq-box@x", "secret": PW_IN},
                     outbound={"host": "smtp.only-q.example", "user": "qq-box@x", "secret": PW_OUT}).json()["id"]
    route = MailRoute.from_row(rig.agent.mail.ms.route_find(channel="qidian", account_id=None))
    assert str(route.id) == rid
    rig.pw_seen.clear()
    fetcher = next(f for f in rig.agent.mail.fetchers.values() if f.route.id == route.id)
    fetcher.run_once()                                           # 真取信路径:工厂里经 secret_ref 读 Vault
    rig.agent.mail._smtp(route)                                  # 发信连接工厂(sender 建连同一入口)
    assert ("imap", PW_IN) in rig.pw_seen and ("smtp", PW_OUT) in rig.pw_seen


def test_consumer_vault_miss_yields_empty_password_not_plaintext_fallback(rig):
    """引用在、Vault 里没值 ⇒ 取到空串(连不上走既有失败路径),绝不回落去库里找明文。"""
    rid = _put_route(rig, **_full_body()).json()["id"]
    rig.agent.vault.entries.pop(f"mail/route/{rid}/imap")
    route = MailRoute.from_row(rig.agent.mail.ms.route_find(channel="qidian", account_id=None))
    assert rig.agent._mail_secret(route.inbound.secret_ref) == ""


# ══════════════════════════════════════════════════ 存量明文迁移
def _seed_plaintext(rig) -> int:
    """模拟修复前落的库:明文原样躺在 inbound_json/outbound_json(含嵌套)。"""
    inbound = {"protocol": "imap", "host": "imap.old.example", "user": "o@x", "secret": PW_IN,
               "secret_ref": "vault://mail/imap", "fallback": {"host": "pop.old.example", "password": PW_FB}}
    outbound = {"host": "smtp.old.example", "user": "o@x", "password": PW_OUT, "smtp_token": PW_TOK}
    return rig.agent.mail.ms.route_upsert(channel="wechat", account_id=None, inbound_json=inbound, outbound_json=outbound)


def _migrate(rig) -> int:
    return asyncio.run(migrate_plaintext(rig.agent.mail.ms, rig.agent.vault))


def test_migration_moves_plaintext_then_is_idempotent(rig):
    rid = _seed_plaintext(rig)
    assert PW_IN in _raw_text(rig)                                # 复现:修复前库里就是明文
    assert _migrate(rig) == 1
    raw1 = _raw_text(rig)
    for pw in ALL_PW:
        assert pw not in raw1
    row = _row(rig, rid)
    assert row["in"]["secret_ref"] == f"vault://mail/route/{rid}/imap"
    assert row["out"]["secret_ref"] == f"vault://mail/route/{rid}/smtp"
    assert row["in"]["host"] == "imap.old.example" and row["in"]["fallback"] == {"host": "pop.old.example"}
    v = _vault(rig)
    assert v[f"mail/route/{rid}/imap"] == PW_IN and v[f"mail/route/{rid}/smtp"] == PW_OUT
    # 再跑一次:无变化(行、Vault 版本都不动;一条写都没有)
    ver = {k: e.version for k, e in rig.agent.vault.entries.items()}
    changes = rig.store.con.total_changes
    assert _migrate(rig) == 0
    assert _raw_text(rig) == raw1 and {k: e.version for k, e in rig.agent.vault.entries.items()} == ver
    assert rig.store.con.total_changes == changes


def test_migration_vault_offline_keeps_row_intact_then_retries(rig):
    rid = _seed_plaintext(rig)
    before = _raw_rows(rig)
    rig.agent.vault.offline = True
    assert _migrate(rig) == 0
    assert _raw_rows(rig) == before                               # 不会「明文已删、Vault 没写成」
    rig.agent.vault.offline = False
    assert _migrate(rig) == 1 and PW_IN not in _raw_text(rig)
    assert _vault(rig)[f"mail/route/{rid}/imap"] == PW_IN


def test_migration_global_row_uses_mail_proto_path(rig):
    g = rig.agent.mail.ms.route_find(channel=None, account_id=None)
    inbound = json.loads(g["inbound_json"]) | {"secret": PW_IN}
    rig.agent.mail.ms.route_upsert(channel=None, account_id=None, inbound_json=inbound,
                                   outbound_json=json.loads(g["outbound_json"]))
    assert _migrate(rig) == 1
    assert _row(rig, g["id"])["in"]["secret_ref"] == "vault://mail/imap" and _vault(rig)["mail/imap"] == PW_IN


def test_migration_runs_as_scheduler_job_and_reloads(rig):
    rid = _seed_plaintext(rig)
    job = rig.agent.scheduler.jobs["mail_route_secret_migrate"]
    assert job.run_immediately is True
    asyncio.run(rig.agent.mail_secret_migrate_tick())
    assert PW_IN not in _raw_text(rig)
    route = next(r for r in rig.agent.mail.routes.routes if str(r.id) == str(rid))    # reload 后内存里也是新引用
    assert route.inbound.secret_ref == f"vault://mail/route/{rid}/imap"


# ══════════════════════════════════════════════════ 审计 / 日志无明文
def test_audit_and_logs_never_contain_plaintext(rig, caplog):
    caplog.set_level(logging.DEBUG)
    _put_route(rig, **_full_body())
    rig.client.put(f"{P}/settings/mail", headers=H(),
                   json={"scopes": {"qq": {"inbound": {"host": "q.example", "secret": PW_IN}, "outbound": {"password": PW_OUT}}}})
    rig.agent.vault.offline = True
    _put_route(rig, channel="wechat", **_full_body())            # 失败路径也不许漏
    rig.agent.vault.offline = False
    _seed_plaintext(rig)
    rig.agent.vault.offline = True
    _migrate(rig)                                                # 迁移失败日志
    rig.agent.vault.offline = False
    _migrate(rig)                                                # 迁移成功日志
    audit = json.dumps([dict(x) for x in rig.store.con.execute("SELECT * FROM audit_log")], ensure_ascii=False)
    assert "settings/mail" in audit                              # 审计确有记这些请求
    for pw in ALL_PW:
        assert pw not in audit and pw not in caplog.text


# ══════════════════════════════════════════════════ 同型扫描顺带修:WS `?token=` 进 uvicorn 日志
def test_uvicorn_ws_handshake_log_masks_query_token():
    """uvicorn WS 握手打 ``"WebSocket /api/v1/events?token=…" [accepted]``(INFO,logger=uvicorn.error)。
    过滤器必须在 ``uvicorn.Config``(内部 dictConfig)之后挂,且挂上后令牌值不进日志。"""
    import uvicorn

    from qtrade_agent.main import install_log_masking
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate, logging.getLogger(n).level,
                 list(logging.getLogger(n).filters)) for n in names}
    got: list[str] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            got.append(record.getMessage())

    try:
        uvicorn.Config(app=lambda *a: None, log_level="info")      # 与 main() 同一构造顺序:先 Config 再挂
        install_log_masking()
        install_log_masking()                                       # 幂等:不重复挂
        lg = logging.getLogger("uvicorn.error")
        assert sum(type(f).__name__ == "MaskQuerySecrets" for f in lg.filters) == 1
        keep = _Keep()
        lg.addHandler(keep)
        lg.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:50000", f"/api/v1/events?token={PW_TOK}&x=1")
        logging.getLogger("uvicorn.access").addHandler(keep)
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET",
                                                 f"/api/v1/x?a=1&Token={PW_IN}", "1.1", 200)
        assert len(got) == 2 and all(PW_TOK not in m and PW_IN not in m for m in got)
        assert "token=***&x=1" in got[0] and "Token=***" in got[1]
    finally:
        for n, (h, prop, lvl, flt) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:], lg.propagate, lg.filters[:] = h, prop, flt
            lg.setLevel(lvl)
