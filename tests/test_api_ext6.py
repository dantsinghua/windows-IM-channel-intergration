"""第六批 API 修复的开发者测试(`.omc/handoffs/backend-api-6.md`)。

覆盖:
- **A:#56 `GET /mail/status` 按 02 #56 改键**(总控裁决以 02 为准):`protocol_configured/protocol_active/fallback{since_at,reason}`、
  `quota`、`last_sent_at`、`archived_mb`、`next_run_at` 等;信封按 R6-55 顶层平铺;单条 / 404 / 停用路由;
  告警 `MAIL_PROTOCOL_FALLBACK`/`MAIL_INBOUND_STALLED` 的 evidence 时间改 ISO;
- **B:S-5~S-9 出参视图**(S-4 #95 行形状待裁决、未改,只守翻页):#38~#41 工作流、#44 C-42 分页、#45 run/steps、
  #105 路由(含 `status` 摘要、**绝不回密钥明文**)、#94 `dead_ms → dead_at`。

全程只碰 ``FakeImap``/``FakePop3``/``FakeSmtp``,不连任何真实邮箱、不出网。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from qtrade_agent.mail.backends import FakeImap, FakePop3, FakeSmtp, MailConnectError
from qtrade_agent.mail.service import MailService
from tests.test_integration_wiring_common import TOKEN_READ, H, close_rig, make_rig
from tests.test_mail_common import SECRET, build_catalog, make_cfg

P = "/api/v1"
T0 = 1_789_000_000_000
MB = 1024 * 1024
ROUTE_ELEM_KEYS = {"route_id", "channel", "account_id", "route", "inbound", "outbound", "cleanup"}
ROUTE_KEYS = {"id", "channel", "account_id", "outbound_template_id", "inbound_template_id"}
INBOUND_KEYS = {"protocol_configured", "protocol_active", "fallback", "folders", "last_success_at", "last_error",
                "idle_supported", "consecutive_failures", "quota"}
OUTBOUND_KEYS = {"queued", "retrying", "dead", "last_sent_at", "consecutive_failures", "rate_per_min"}
CLEANUP_KEYS = {"last_run_at", "last_status", "archived_mb", "next_run_at"}


class Rec:
    """告警记录器(替换 fetcher.alerts,只看 evidence)。"""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict[str, Any]]] = []

    def firing(self, code: str, *, subject: str, severity: str, evidence: dict[str, Any]) -> None:
        self.fired.append((code, evidence))

    def resolve(self, code: str, *, subject: str) -> None:
        pass


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    cfg = make_cfg(archive_dir=str(tmp_path / "mail-archive"))
    r.imap, r.pop3, r.smtp = FakeImap(), FakePop3(), FakeSmtp()
    r.agent.mail = MailService(r.store, cfg, catalog=build_catalog(), clock=r.clock, alerts=r.agent.alerts,
                               secret_of=lambda ref: SECRET,
                               imap_factory=lambda route: r.imap, pop3_factory=lambda route: r.pop3,
                               smtp_factory=lambda route: r.smtp)
    yield r
    close_rig(r)


def _get(rig, path: str, token: str | None = None, **params: Any) -> dict[str, Any]:
    r = rig.client.get(f"{P}{path}", headers=H(token) if token else H(), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _iso_ms(s: str) -> int:
    dt = datetime.fromisoformat(s)
    assert dt.utcoffset() is not None, f"时间没带偏移:{s}"
    return int(dt.timestamp() * 1000)


DURATION_KEYS = {"timeout_ms", "cost_ms"}          # 时长,不是时刻 —— 不属 00 §6 时间口径


def _no_ms_keys(obj: Any) -> None:
    """递归断言:出参里没有时刻类 `*_ms` / 任何 `*_json` 键。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert str(k) in DURATION_KEYS or not str(k).endswith(("_ms", "_json")), f"透出了库列 {k}"
            _no_ms_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_ms_keys(v)


def _route0(rig) -> dict[str, Any]:
    return _get(rig, "/mail/status")["routes"][0]


# ══════════════════════════════════════════════════ A. #56
def test_56_list_shape_flat_and_exact_keys(rig):
    body = _get(rig, "/mail/status", token=TOKEN_READ)
    assert "data" not in body and body["ok"] is True and body["enabled"] is True        # R6-55 顶层平铺
    assert len(body["routes"]) == 1
    e = body["routes"][0]
    assert set(e) == ROUTE_ELEM_KEYS
    assert set(e["route"]) == ROUTE_KEYS and set(e["inbound"]) == INBOUND_KEYS
    assert set(e["outbound"]) == OUTBOUND_KEYS and set(e["cleanup"]) == CLEANUP_KEYS
    assert e["route_id"] == e["route"]["id"] and isinstance(e["route_id"], str)
    assert e["channel"] is None and e["route"]["channel"] is None                        # 全局行 = NULL,不再是 "*"
    assert e["inbound"]["protocol_configured"] == "imap" == e["inbound"]["protocol_active"]
    assert e["inbound"]["fallback"] is None
    _no_ms_keys(body)


def test_56_single_route_and_404(rig):
    rid = _route0(rig)["route_id"]
    one = _get(rig, "/mail/status", route_id=rid)
    assert {"enabled", "route", "inbound", "outbound", "cleanup"} <= set(one) and "routes" not in one
    assert one["route"]["id"] == rid and one["enabled"] is True
    assert _get(rig, "/mail/status", route=rid)["route"]["id"] == rid                    # 06 旧参数名照收
    r = rig.client.get(f"{P}/mail/status", headers=H(), params={"route_id": "99999"})
    assert r.status_code == 404 and r.json()["code"] == "TARGET_NOT_FOUND"


def test_56_disabled_route_not_listed_but_single_ok(rig):
    mail = rig.agent.mail
    rid = mail.ms.route_upsert(channel="wechat", account_id=None, inbound_json={}, outbound_json={}, enabled=False)
    mail.reload()
    assert [e["route_id"] for e in _get(rig, "/mail/status")["routes"]] == [_route0(rig)["route_id"]]
    one = _get(rig, "/mail/status", route_id=str(rid))
    assert one["enabled"] is False and one["route"]["channel"] == "wechat"


def test_56_fallback_object_and_alert_evidence_iso(rig):
    """真走回落:IMAP 连不上 3 轮 ⇒ `protocol_active=pop3`、`fallback{since_at ISO, reason}`;告警 `since` 为 ISO。"""
    f = next(iter(rig.agent.mail.fetchers.values()))
    f.alerts = rec = Rec()
    rig.imap.raise_on_connect = MailConnectError("连接超时", kind="connect_timeout")
    for _ in range(3):
        rig.agent.mail.fetch_once()
    inb = _route0(rig)["inbound"]
    assert inb["protocol_configured"] == "imap" and inb["protocol_active"] == "pop3"
    assert "连接超时" in inb["fallback"]["reason"]
    since = f.state()["fallback_since"]
    assert _iso_ms(inb["fallback"]["since_at"]) == since // 1000 * 1000
    ev = next(e for c, e in rec.fired if c == "MAIL_PROTOCOL_FALLBACK")
    assert _iso_ms(ev["since"]) == since // 1000 * 1000


def test_56_fallback_reason_cleared_on_switch_back(rig):
    f = next(iter(rig.agent.mail.fetchers.values()))
    rig.imap.raise_on_connect = MailConnectError("连接被拒", kind="connect_refused")
    for _ in range(3):
        rig.agent.mail.fetch_once()
    rig.imap.raise_on_connect = None
    rig.clock.advance(31 * 60 * 1000)
    rig.agent.mail.fetch_once()
    assert f.state()["fallback_reason"] is None
    assert _route0(rig)["inbound"]["fallback"] is None


def test_56_stalled_alert_evidence_iso(rig):
    f = next(iter(rig.agent.mail.fetchers.values()))
    f.alerts = rec = Rec()
    st = f.state()
    st["last_success_ms"] = T0
    f._save(st)
    assert f.check_stalled(now_ms=T0 + 20 * 60 * 1000) is True
    ev = next(e for c, e in rec.fired if c == "MAIL_INBOUND_STALLED")
    assert _iso_ms(ev["last_success_at"]) == T0


def test_56_quota_from_last_cleanup_round(rig):
    assert _route0(rig)["inbound"]["quota"] is None                                     # 从未清理 ⇒ null
    rig.imap.quota_used, rig.imap.quota_limit = 312 * MB, 2048 * MB
    rig.agent.mail.fetch_once()                                                         # 同一轮里跑清理(§2.6.6)
    q = _route0(rig)["inbound"]["quota"]
    assert q == {"used_mb": 312.0, "limit_mb": 2048.0, "source": "imap_quota"}
    c = _route0(rig)["cleanup"]
    assert c["last_status"] == "OK" and _iso_ms(c["last_run_at"]) and c["next_run_at"] is None


def test_56_cleanup_and_quota_not_attributed_across_mailboxes(rig):
    """清理日志无 mailbox 列 ⇒ 多邮箱时不把 A 邮箱的清理 / 配额摆到 B 名下,回 null。"""
    mail = rig.agent.mail
    rig.imap.quota_used, rig.imap.quota_limit = 1 * MB, 10 * MB
    mail.fetch_once()
    mail.ms.route_upsert(channel="qq", account_id=None,
                         inbound_json={"protocol": "imap", "host": "imap.other.example", "user": "b@other.example"},
                         outbound_json={}, enabled=True)
    mail.reload()
    routes = _get(rig, "/mail/status")["routes"]
    assert len(routes) == 2 and len(mail.fetchers) == 2
    for e in routes:
        assert e["inbound"]["quota"] is None and e["cleanup"]["last_run_at"] is None


def test_56_outbound_per_route_and_last_sent_at(rig):
    mail = rig.agent.mail
    rid = int(_route0(rig)["route_id"])
    other = mail.ms.route_upsert(channel="wechat", account_id=None, inbound_json={}, outbound_json={}, enabled=True)
    mail.reload()
    ids = [mail.ms.outbox_enqueue(kind="receipt", to_addrs="a@x", subject=f"s{i}", body_text="b", route_id=rr,
                                  rfc_message_id=f"<o{i}@x>", template_version="v1", dedup_key=f"d{i}")
           for i, rr in enumerate((rid, rid, other))]
    mail.ms.outbox_update(ids[0], status="SENT", sent_ms=T0)
    ob = next(e for e in _get(rig, "/mail/status")["routes"] if e["route_id"] == str(rid))["outbound"]
    assert ob["queued"] == 1 and ob["dead"] == 0                                        # 只数本路由(另一条路由那封不算)
    assert _iso_ms(ob["last_sent_at"]) == T0 and ob["consecutive_failures"] == 0
    ob2 = next(e for e in _get(rig, "/mail/status")["routes"] if e["route_id"] == str(other))["outbound"]
    assert ob2["queued"] == 1 and ob2["last_sent_at"] is None


def test_56_archived_mb_sums_archived_rows_of_this_mailbox(rig):
    mail = rig.agent.mail
    key = next(iter(mail.fetchers))
    assert _route0(rig)["cleanup"]["archived_mb"] == 0.0
    for i, path in enumerate(("/a/1.eml", "/a/2.eml", None)):
        mail.ms.inbox_insert(mailbox=key, protocol="imap", folder="INBOX", uidvalidity=1, uid=100 + i,
                             rfc_message_id=f"<m{i}@x>", from_addr="a@x", to_addrs="b@x", subject="s",
                             received_ms=T0, size_bytes=3 * MB, body_sha256=f"h{i}", status="DONE",
                             archived_path=path)
    assert _route0(rig)["cleanup"]["archived_mb"] == 6.0


# ══════════════════════════════════════════════════ B. S-4 #95 审计(JSON 行形状待裁决,本批未改;只守分页不回退)
def test_95_cursor_pages_still_work(rig):
    for i in range(5):
        rig.store.insert_audit(kind="system", transport="system", actor="token:console", action="pg", now_ms=T0 + i)
    seen, cur = [], None
    while True:
        params = {"action": "pg", "limit": 2, **({"cursor": cur} if cur else {})}
        body = _get(rig, "/audit", **params)
        seen += [r["id"] for r in body["data"]]
        cur = body["next_cursor"]
        if not cur:
            break
    assert len(seen) == 5 == len(set(seen))


# ══════════════════════════════════════════════════ B. S-5 #38~#41 工作流
WF_YAML = "name: wf1\nsteps:\n  - id: s1\n    op: sleep\n    args: { seconds: 0 }\n"


def _mk_wf(rig) -> dict[str, Any]:
    r = rig.client.post(f"{P}/workflows", headers=H(), json={"name": "wf1", "yaml": WF_YAML})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_38_to_41_workflow_views(rig):
    wf = _mk_wf(rig)
    keys = {"id", "name", "version", "yaml", "checksum", "enabled", "schedule_cron", "created_at", "updated_at",
            "updated_by"}
    assert set(wf) == keys and wf["enabled"] is True and _iso_ms(wf["created_at"])
    lst = _get(rig, "/workflows")["data"]
    assert set(lst[0]) == keys - {"yaml"}                                               # 02 #38 列表不含 yaml
    got = _get(rig, f"/workflows/{wf['id']}")["data"]
    assert set(got) == keys and got["yaml"] == WF_YAML
    r = rig.client.put(f"{P}/workflows/{wf['id']}", headers=H(), json={"yaml": WF_YAML})
    assert r.status_code == 200 and r.json()["data"]["version"] == 2
    _no_ms_keys(r.json()["data"])
    _no_ms_keys(lst)


# ══════════════════════════════════════════════════ B. S-6 / S-7 #44 / #45
def _mk_runs(rig, wid: str, starts: list[int]) -> list[str]:
    ids = []
    for i, s in enumerate(starts):
        rid = f"01RUN{i:021d}"
        rig.store.con.execute(
            "INSERT INTO workflow_runs(run_id, workflow_id, workflow_version, trigger, actor, args_json, status,"
            " started_ms, finished_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, wid, 1, "api", "token:console", '{"k": %d}' % i, "done", s, s + 10))
        ids.append(rid)
    rig.store.con.commit()
    return ids


def test_44_c42_paging_same_ms_no_dup_no_gap(rig):
    wid = _mk_wf(rig)["id"]
    ids = _mk_runs(rig, wid, [T0, T0, T0, T0 - 1000, T0 - 2000])            # 三个撞同一毫秒
    seen, cur, pages = [], None, 0
    while True:
        body = _get(rig, f"/workflows/{wid}/runs", limit=2, **({"cursor": cur} if cur else {}))
        pages += 1
        seen += [r["run_id"] for r in body["data"]]
        cur = body["next_cursor"]
        if not cur:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen)) and pages == 3
    starts = [_iso_ms(r["started_at"]) for r in _get(rig, f"/workflows/{wid}/runs")["data"]]
    assert starts == sorted(starts, reverse=True)


def test_44_row_view_short_page_since_and_bad_cursor(rig):
    wid = _mk_wf(rig)["id"]
    _mk_runs(rig, wid, [T0, T0 - 60_000])
    body = _get(rig, f"/workflows/{wid}/runs")
    assert body["next_cursor"] is None and len(body["data"]) == 2
    row = body["data"][0]
    assert set(row) == {"run_id", "workflow_id", "workflow_version", "trigger", "actor", "args", "status",
                        "pause_reason", "error", "started_at", "finished_at"}
    assert row["args"] == {"k": 0} and _iso_ms(row["finished_at"]) == T0             # ISO 秒精度(T0 + 10 ms)
    since = datetime.fromtimestamp((T0 - 1000) / 1000).astimezone().isoformat()
    assert len(_get(rig, f"/workflows/{wid}/runs", since=since)["data"]) == 1
    r = rig.client.get(f"{P}/workflows/{wid}/runs", headers=H(), params={"cursor": "!!bad"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_cursor"


def test_45_run_and_steps_views(rig):
    wid = _mk_wf(rig)["id"]
    rid = _mk_runs(rig, wid, [T0])[0]
    rig.store.con.execute(
        "INSERT INTO workflow_steps(step_id, run_id, idx, step_name, op, status, result_code, note, started_ms, finished_ms)"
        " VALUES ('01STEP000000000000000000', ?, 0, 's1', 'sleep', 'done', 'OK', 'sleep 0s', ?, ?)", (rid, T0, T0 + 5))
    rig.store.con.commit()
    body = _get(rig, f"/workflows/runs/{rid}")
    assert set(body) >= {"ok", "run", "steps"} and "data" not in body               # 字面键集,平铺(R6-55)
    assert body["run"]["run_id"] == rid and body["run"]["args"] == {"k": 0}
    st = body["steps"][0]
    assert set(st) == {"step_id", "run_id", "idx", "step_name", "op", "account_id", "trace_id", "status", "attempt",
                       "result_code", "note", "started_at", "finished_at"}
    assert _iso_ms(st["finished_at"]) == T0                                          # 秒精度
    _no_ms_keys(body)


# ══════════════════════════════════════════════════ B. S-8 #105 路由
def test_105_row_view_status_and_never_leaks_secret(rig):
    r = rig.client.put(f"{P}/settings/mail/routes", headers=H(), json={
        "channel": "qidian", "enabled": True,
        "inbound": {"protocol": "imap", "host": "imap.q.example", "user": "q@x", "secret": "PLAIN-IN",
                    "secret_ref": "vault://mail/route/2/imap", "fallback": {"host": "pop.q.example", "password": "PLAIN-FB"}},
        "outbound": {"host": "smtp.q.example", "smtp_password": "PLAIN-OUT", "secret_ref": "vault://mail/route/2/smtp"}})
    assert r.status_code == 200 and isinstance(r.json()["id"], str)
    rid = r.json()["id"]
    rr = rig.client.get(f"{P}/settings/mail/routes", headers=H())
    assert "PLAIN-" not in rr.text                                                      # 02 #105:secret 只写不读
    row = next(x for x in rr.json()["data"] if x["id"] == rid)
    assert set(row) == {"id", "channel", "account_id", "inbound", "outbound", "outbound_template_id",
                        "inbound_template_id", "enabled", "created_at", "updated_at", "status"}
    assert row["inbound"]["secret_ref"] == "vault://mail/route/2/imap" and row["inbound"]["host"] == "imap.q.example"
    assert row["outbound"]["secret_ref"] == "vault://mail/route/2/smtp" and row["enabled"] is True
    assert row["status"] == {k: v for k, v in _get(rig, "/mail/status", route_id=rid).items()
                             if k not in ("ok", "trace_id")}                             # = #56 单条
    _no_ms_keys(row)


def test_105_status_for_disabled_route(rig):
    rig.client.put(f"{P}/settings/mail/routes", headers=H(), json={"channel": "qq", "enabled": False,
                                                                   "inbound": {}, "outbound": {}})
    rows = _get(rig, "/settings/mail/routes")["data"]
    qq = next(x for x in rows if x["channel"] == "qq")
    assert qq["enabled"] is False and qq["status"]["enabled"] is False


# ══════════════════════════════════════════════════ B. S-9 #94 webhooks
def test_94_dead_at_iso(rig):
    r = rig.client.post(f"{P}/settings/webhooks", headers=H(), json={"name": "h", "url": "http://127.0.0.1:9/x"})
    wid = r.json()["data"]["id"]
    assert r.json()["data"]["dead_at"] is None and "dead_ms" not in r.json()["data"]
    rig.store.con.execute("UPDATE webhooks SET dead_ms=? WHERE id=?", (T0, wid))
    rig.store.con.commit()
    row = next(x for x in _get(rig, "/settings/webhooks")["data"] if x["id"] == wid)
    assert _iso_ms(row["dead_at"]) == T0
    _no_ms_keys(row)
