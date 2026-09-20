"""webhook 投递器(02 §2.2.7):扇出只给启用且订阅的登记方、E-3 例外不受过滤、HMAC 头 ``X-QT-Webhook-*``、
指数退避、连续失败进死信 + ``WEBHOOK_DEAD``、同一 webhook 串行保序、``target='ws'`` 行不经投递器。"""
from __future__ import annotations

import json

from qtrade_agent.alerts import Alerts
from qtrade_agent.events import Events, iso8601
from qtrade_agent.hmac_inbound import canonical_string, signatures_equal, sign
from qtrade_agent.ids import ulid
from qtrade_agent.webhook import (DEFAULT_BACKOFF_MS, FakeHttp, HttpResponse, WEBHOOK_DEAD, WebhookConfig,
                                  WebhookDispatcher)

SECRET = "hook-secret"
URL = "https://biz.example/qtrade/callback"


def _hook(store, clock, *, hid="wh1", url=URL, events=None, accounts=None, enabled=1, max_attempts=10, timeout_ms=5000):
    store.con.execute(
        "INSERT INTO webhooks(id, name, url, secret_ref, events_json, accounts_json, enabled, timeout_ms, max_attempts, "
        "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (hid, hid, url, f"vault://webhook/{hid}", json.dumps(events or ["*"]), json.dumps(accounts or ["*"]),
         enabled, timeout_ms, max_attempts, clock(), clock()))
    return hid


def _dispatcher(store, clock, http, *, alerts=None, cfg=None):
    return WebhookDispatcher(store, http=http, secret_provider=lambda row: SECRET, clock=clock, alerts=alerts,
                             cfg=cfg or WebhookConfig(), nonce_fn=lambda: "n" * 16)


def _row(store, seq):
    return dict(store.con.execute("SELECT * FROM events_outbox WHERE seq=?", (seq,)).fetchone())


def _hook_row(store, hid="wh1"):
    return dict(store.con.execute("SELECT * FROM webhooks WHERE id=?", (hid,)).fetchone())


# ---------------------------------------------------------------- 扇出(订阅过滤)
def test_fanout_only_enabled_and_subscribed(store, clock):
    _hook(store, clock, hid="all")
    _hook(store, clock, hid="only_alert", events=["alert"])
    _hook(store, clock, hid="only_qq", accounts=["qq03"])
    _hook(store, clock, hid="off", enabled=0)
    d = _dispatcher(store, clock, FakeHttp())
    hit = d.fanout(event_id=ulid(clock()), event="message", payload={"x": 1}, account_id="qd01", channel="qidian")
    assert hit == ["all"]
    hit2 = d.fanout(event_id=ulid(clock()), event="alert", payload={"x": 1}, account_id="qq03", channel="qq")
    assert sorted(hit2) == ["all", "only_alert", "only_qq"]


def test_fanout_ignore_filters_is_the_e3_exception(store, clock):
    """E-3(§2.2.12):``NET_PUBLIC_ENDPOINT_CHANGED`` 向全部 enabled=1 的登记方各投一行,不受订阅过滤。"""
    _hook(store, clock, hid="only_msg", events=["message"])
    _hook(store, clock, hid="off", enabled=0)
    d = _dispatcher(store, clock, FakeHttp())
    hit = d.fanout(event_id=ulid(clock()), event="net", payload={"kind": "endpoint_changed"}, ignore_filters=True)
    assert hit == ["only_msg"]                      # 禁用的仍然不投


def test_fanout_rows_are_pending_and_ws_row_is_not(store, clock):
    _hook(store, clock)
    events = Events(store)
    d = _dispatcher(store, clock, FakeHttp())
    seq_ws = events.emit("alert", payload={"code": "X"}, now_ms=clock())
    d.fanout(event_id=ulid(clock()), event="alert", payload={"code": "X"}, now_ms=clock())
    assert _row(store, seq_ws)["status"] == "delivered"          # ws 行写入即 delivered(§2.2.7)
    due = d.due_rows()
    assert len(due) == 1 and due[0]["target"] == "webhook:wh1" and due[0]["status"] == "pending"


# ---------------------------------------------------------------- 投递成功
async def test_delivery_success_marks_delivered_and_resets_consecutive_fail(store, clock):
    _hook(store, clock)
    store.con.execute("UPDATE webhooks SET consecutive_fail=3 WHERE id='wh1'")
    http = FakeHttp()
    d = _dispatcher(store, clock, http)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"code": "X"}, now_ms=clock())
    stats = await d.deliver_due()
    assert stats == {"delivered": 1, "failed": 0, "dead": 0}
    row = d.due_rows()
    assert row == []
    assert _hook_row(store)["consecutive_fail"] == 0
    seq = int(http.requests[0]["body"] and json.loads(http.requests[0]["body"])["seq"])
    assert _row(store, seq)["status"] == "delivered" and _row(store, seq)["attempts"] == 1


async def test_body_is_the_same_frame_as_ws(store, clock):
    _hook(store, clock)
    http = FakeHttp()
    d = _dispatcher(store, clock, http)
    d.fanout(event_id=ulid(clock()), event="message", payload={"id": "msg_1"}, account_id="qd01", channel="qidian",
             trace_id="t1", now_ms=clock())
    await d.deliver_due()
    frame = json.loads(http.requests[0]["body"])
    assert frame["event"] == "message" and frame["account_id"] == "qd01" and frame["channel"] == "qidian"
    assert frame["trace_id"] == "t1" and frame["payload"] == {"id": "msg_1"}
    assert frame["ts"] == iso8601(clock()) and isinstance(frame["seq"], int)


async def test_signature_headers_use_same_canonical_as_inbound(store, clock):
    _hook(store, clock, url=URL + "?v=1")
    http = FakeHttp()
    d = _dispatcher(store, clock, http)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"code": "X"}, now_ms=clock())
    await d.deliver_due()
    req = http.requests[0]
    h = req["headers"]
    assert h["X-QT-Webhook-AppId"] == "wh1" and h["X-QT-Webhook-Nonce"] == "n" * 16
    canonical = canonical_string("POST", "/qtrade/callback", "v=1", req["body"], h["X-QT-Webhook-Timestamp"], "n" * 16)
    assert signatures_equal(sign(SECRET, canonical), h["X-QT-Webhook-Signature"])
    assert h["X-QT-Webhook-Signature"].startswith("v1=")


# ---------------------------------------------------------------- 失败 / 退避 / 死信
async def test_failure_increments_attempts_and_uses_backoff_table(store, clock):
    _hook(store, clock)
    http = FakeHttp()
    http.queue(URL, HttpResponse(500), ConnectionError("boom"))
    d = _dispatcher(store, clock, http)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"code": "X"}, now_ms=clock())
    seq = d.due_rows()[0]["seq"]
    await d.deliver_due()
    row = _row(store, seq)
    assert row["status"] == "pending" and row["attempts"] == 1 and row["last_error"] == "http_500"
    assert row["next_attempt_ms"] == clock() + DEFAULT_BACKOFF_MS[0]
    assert d.due_rows() == []                                   # 退避窗内不再取
    clock.advance(DEFAULT_BACKOFF_MS[0])
    await d.deliver_due()
    row2 = _row(store, seq)
    assert row2["attempts"] == 2 and "ConnectionError" in row2["last_error"]
    assert row2["next_attempt_ms"] == clock() + DEFAULT_BACKOFF_MS[1]


async def test_dead_after_max_attempts_disables_hook_and_alerts(store, clock):
    _hook(store, clock, max_attempts=3)
    http = FakeHttp(default=HttpResponse(503))
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    d = _dispatcher(store, clock, http, alerts=alerts)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"code": "X"}, now_ms=clock())
    seq = d.due_rows()[0]["seq"]
    for i in range(3):
        await d.deliver_due()
        clock.advance(DEFAULT_BACKOFF_MS[min(i, len(DEFAULT_BACKOFF_MS) - 1)])
    row = _row(store, seq)
    assert row["status"] == "dead" and row["attempts"] == 3
    hook = _hook_row(store)
    assert hook["enabled"] == 0 and hook["dead_ms"] is not None and hook["consecutive_fail"] == 3
    assert alerts.is_firing(WEBHOOK_DEAD, "host")               # §3.7:subject='host'
    ev = [e for e in store.list_events(event="alert")][-1]["payload"]
    assert ev["severity"] == "warn" and ev["evidence"]["webhook_id"] == "wh1" and ev["evidence"]["attempts"] == 3
    assert d.dead == 1


async def test_secret_unavailable_counts_as_failure(store, clock):
    _hook(store, clock)
    d = WebhookDispatcher(store, http=FakeHttp(), secret_provider=lambda row: None, clock=clock)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"code": "X"}, now_ms=clock())
    seq = d.due_rows()[0]["seq"]
    await d.deliver_due()
    assert _row(store, seq)["last_error"] == "secret_unavailable" and d.failed == 1


# ---------------------------------------------------------------- 保序与并行
async def test_same_hook_is_serial_and_stops_at_first_failure(store, clock):
    """§2.2.7「每个 webhook 一条串行投递(保序)」:第一条没投成,同一 webhook 后面的这轮不投,免得乱序。"""
    _hook(store, clock)
    http = FakeHttp()
    http.queue(URL, HttpResponse(500))
    d = _dispatcher(store, clock, http)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"n": 1}, now_ms=clock())
    d.fanout(event_id=ulid(clock() + 1), event="alert", payload={"n": 2}, now_ms=clock())
    seqs = [r["seq"] for r in d.due_rows()]
    await d.deliver_due()
    assert len(http.requests) == 1                               # 只投了第一条
    assert _row(store, seqs[0])["attempts"] == 1 and _row(store, seqs[1])["attempts"] == 0


async def test_different_hooks_are_independent_in_one_round(store, clock):
    _hook(store, clock, hid="a", url="https://a.example/cb")
    _hook(store, clock, hid="b", url="https://b.example/cb")
    http = FakeHttp()
    http.queue("https://a.example/cb", HttpResponse(500))
    d = _dispatcher(store, clock, http)
    d.fanout(event_id=ulid(clock()), event="alert", payload={"n": 1}, now_ms=clock())
    stats = await d.deliver_due()
    assert stats["delivered"] == 1 and stats["failed"] == 1      # b 成功不受 a 失败影响
