"""采集端 → 询价端:``xunjia_v22.observe`` 给企点事件补 ``reply``(R6-117)—— 对照 docs/06 §2.9.5「引用对象 ``reply``」条末
子条「传到询价端(R6-117)」、docs/00 §15g R6-117、``.codex/qidian-reply-quote-20261028/DECISION-2.md`` §1 与附录 A T8。

真 ``Store(":memory:")`` + 假 port / ledger / bridge;数据全部合成。末尾一组用对端真 ``QTradeNativePort``,
缺 ``relay``(未加 PYTHONPATH)时跳过。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from qtrade_agent.adapters.qidian.history_native import HistoryTable, NativeHistoryReader
from qtrade_agent.adapters.qidian.msgdata import MainDbRow
from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.events import Events, message_payload
from qtrade_agent.models import Message, Session
from qtrade_agent.ossbox import xunjia_v22 as v22
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host
from qtrade_agent.store import Store
from tests.conftest import Clock, table_name
from tests.qidian_reply_stream import SYN_ORIG_UID, SYN_SENDER_NAME, SYN_SENDER_UIN, SYN_SEQ, SYN_TEXT, SYN_TIME_S, reply_ext_hex

SELF = "10000000099"                       # 合成绑定账号 self_uid
OTHER = str(SYN_SENDER_UIN)                # 合成对端 uin(= 引用摘要发送者)
PEER = "10000000009"
PEER2 = "10000000010"
WIRE_KEYS = {"state", "text", "source_text", "source_direction", "sender_id", "ts", "source_ext_msg_id"}
SRC_TEXT = "合成原消息全文 第一行\n第二行 1Y 1.70\n第三行(摘要里没有)"
LOGGER = "qtrade.ossbox.xunjia_v22"
OMIT = "Qidian reply omitted from V2.2 event"


@pytest.fixture(autouse=True)
def _fresh_warned():
    v22._reply_warned.clear()              # 「每进程每原因一次」:各用例从干净进程态开始
    yield
    v22._reply_warned.clear()


@pytest.fixture
def st():
    s = Store(":memory:", clock=Clock()).open()
    s.ensure_account("qd01", "qidian", state="running", self_uid=SELF)
    s.ensure_account("wx01", "wechat", state="running", self_uid="wxid_synthetic")
    s.ensure_account("qq03", "qq", state="running", login_mode="qrcode", self_uid="10000000098")
    yield s
    s.close()


@pytest.fixture
def hosts(st, tmp_path):
    loops = []

    def build(name, *, channel="qidian", store=None):
        made = _build(store or st, tmp_path, name, channel)
        loops.append(made.loop)
        return made

    yield build
    for loop in loops:
        loop.close()


ACCOUNTS = {"qidian": ("qd01", SELF), "wechat": ("wx01", "wxid_synthetic"), "qq": ("qq03", "10000000098")}


class FakePort:
    """与对端 ``normalize_native_message`` 同口径的固定字段输出(不转发 ``reply``);逐次登记输出副本。"""

    def __init__(self, binding, channel):
        self.binding, self.channel = binding, channel
        self.account_id, self.self_uid = ACCOUNTS[channel]
        self.native_reconcile = lambda *a, **k: None
        self.normalized = []

    def _account(self):
        return {"id": self.account_id, "self_uid": self.self_uid, "channel": self.channel}

    def normalize_native_message(self, row, live=False):
        prefix = row["account_id"] + ":"
        peer = row["session_id"][len(prefix):]
        out = {
            "event_id": "qtrade-" + hashlib.sha256(
                (self.binding.account_uid + "|" + peer + "|" + row["dir"] + "|" + row["ext_msg_id"]).encode()).hexdigest(),
            "peer_id": peer, "direction": row["dir"], "text": row.get("text") or "", "kind": "message",
            "channel": self.channel, "type": row.get("type", "text"), "native_account_uid": self.binding.account_uid,
            "native_message_id": row.get("id"), "identity_epoch": row.get("identity_epoch", 0),
            "session": {"kind": (row.get("session") or {}).get("kind"), "native_id": peer, "id": row["session_id"]},
            "sender": row.get("sender"), "is_recalled": bool(row.get("revoked")), "media": row.get("media") or [],
            "attachments": [], "ext_msg_id": row["ext_msg_id"], "occurred_at": row["ts_ms"] / 1000, "historical": not live,
        }
        self.normalized.append(copy.deepcopy(out))
        return out


def _build(store, tmp_path, name, channel, port=None, binding=None):
    binding = binding or SimpleNamespace(owner_user_id="synthetic-owner", account_uid=ACCOUNTS[channel][1],
                                         workbench_id="synthetic-wb")
    port = port or FakePort(binding, channel)
    calls, published = [], []
    ledger = SimpleNamespace(begin_inbound=lambda eid, peer: calls.append(("in", eid, peer)),
                             stage_out=lambda data, wait: calls.append(("out", data["event_id"], data["peer_id"])),
                             permission=lambda _: True)
    bridge = SimpleNamespace(binding=binding, ledger=ledger, adapters={}, echo_wait_seconds=1,
                             native_reconcile=port.native_reconcile,
                             publish_native=lambda data, historical=False: published.append(copy.deepcopy(data)))
    loop = asyncio.new_event_loop()
    host = XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop, binding=binding,
                         bridge=bridge, port=port, state_path=tmp_path / f"{name}.db", legacy_inbox_disabled=lambda: True)
    return SimpleNamespace(host=host, calls=calls, published=published, port=port, loop=loop)


def qd(text, uniseq, *, peer=PEER, dir="in", reply=None, account="qd01"):
    return Message(account_id=account, channel="qidian", session=Session(account, peer, "private", peer), dir=dir, type="text",
                   text=text, ts_ms=1_790_000_100_000 + uniseq, source="qidian_db", ext_msg_id=f"qd:{uniseq}",
                   sender_id=peer if dir == "in" else SELF, self=dir == "out", reply=reply)


def store_reply(**over):
    r = {"state": "ok", "text": SYN_TEXT, "sender_id": OTHER, "sender_name": SYN_SENDER_NAME, "ts_ms": SYN_TIME_S * 1000,
         "source_msg_seq": str(SYN_SEQ), "source_msg_uid": str(SYN_ORIG_UID), "source_ext_msg_id": None,
         "resolved_message_id": None}
    r.update(over)
    return r


def view_reply(*, sender_id=OTHER, **over):
    """00 §7.4 视图形(R6-116 事件 payload 里的 ``reply``)。"""
    r = {"state": "ok", "text": SYN_TEXT, "sender": {"id": sender_id, "name": SYN_SENDER_NAME},
         "ts": "2026-09-22T00:13:20+08:00", "source_msg_seq": str(SYN_SEQ), "source_msg_uid": str(SYN_ORIG_UID),
         "source_ext_msg_id": "qd:5001", "resolved_message_id": None}
    r.update(over)
    return r


def payload_of(store, text="回复B", uniseq=5002, *, reply="__none__", **kw):
    """入库一条企点消息并取其事件 payload;``reply`` 给定时直接替换 payload 里的视图(模拟任意来源值)。"""
    m = qd(text, uniseq, **kw)
    store.ingest(m)
    p = message_payload(m, late_after_s=120)
    if reply != "__none__":
        p["reply"] = reply
    return p


def observe(made, payload, seq=1, *, account_id="qd01", channel="qidian"):
    made.host.observe(event="message", payload=payload, account_id=account_id, channel=channel, source_seq=seq)
    assert made.host._intake_ok is True


def bodies(made):
    with made.host._db() as db:
        return [json.loads(r["body"]) for r in db.execute("SELECT body FROM host_event ORDER BY rowid")]


def raw_events(made):
    with made.host._db() as db:
        return [tuple(r) for r in db.execute("SELECT event_id, published FROM host_event ORDER BY rowid")]


def natives(made):
    with made.host._db() as db:
        return [tuple(r) for r in db.execute("SELECT * FROM host_native ORDER BY native_key")]


def wire(made, i=-1):
    body = bodies(made)[i]
    assert "reply" in body
    return body["reply"]


def omit_records(caplog):
    return [r for r in caplog.records if r.name == LOGGER and OMIT in r.getMessage()]


def wire_ok(**over):
    w = {"state": "ok", "text": SYN_TEXT, "source_text": None, "source_direction": "in", "sender_id": OTHER,
         "ts": "2026-09-22T00:13:20+08:00", "source_ext_msg_id": "qd:5001"}
    w.update(over)
    return w


# ───────────────────────────────────────────── 键是否存在:企点恒在、其它通道不加
@pytest.mark.parametrize("drop_key", [False, True])
def test_qidian_without_quote_has_reply_key_null(st, hosts, drop_key):
    made = hosts("h")
    p = payload_of(st)
    assert p["reply"] is None
    if drop_key:
        p.pop("reply")
    observe(made, p)
    body = bodies(made)[0]
    assert "reply" in body and body["reply"] is None
    assert {k: v for k, v in body.items() if k != "reply"} == made.port.normalized[0]


@pytest.mark.parametrize("channel,account,peer", [("wechat", "wx01", "friend"), ("qq", "qq03", PEER)])
@pytest.mark.parametrize("forged", [None, "absent", "object"])
def test_other_channels_never_get_reply_key_and_body_unchanged(st, hosts, channel, account, peer, forged):
    made = hosts("o", channel=channel)
    m = Message(account_id=account, channel=channel, session=Session(account, peer, "private", peer), dir="in", type="text",
                text="合成其它通道正文", ts_ms=1_790_000_000_000, source="chatlog" if channel == "wechat" else "onebot",
                ext_msg_id=f"{channel}-1", sender_id=peer)
    st.ingest(m)
    p = message_payload(m, late_after_s=120)
    if forged == "absent":
        p.pop("reply")
    elif forged == "object":
        p["reply"] = view_reply()
    observe(made, p, account_id=account, channel=channel)
    body = bodies(made)[0]
    assert "reply" not in body
    assert body == made.port.normalized[0]                                       # 与改前一致:写入的就是规范化原样输出
    assert SYN_TEXT not in json.dumps(body, ensure_ascii=False)


# ───────────────────────────────────────────── 七键形状与搬运
def test_resolved_quote_wire_shape_and_values(st, hosts):
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    b = qd("回复B", 5002, reply=store_reply(source_ext_msg_id="qd:5001"))
    st.ingest(b)
    p = message_payload(b, late_after_s=120)
    assert p["reply"]["resolved_message_id"] == a.id                            # 前置:R6-116 入库已关联
    made = hosts("h")
    observe(made, p)
    body = bodies(made)[0]
    r = body["reply"]
    assert set(r) == WIRE_KEYS
    assert r == {"state": "ok", "text": SYN_TEXT, "source_text": SRC_TEXT, "source_direction": "in", "sender_id": OTHER,
                 "ts": p["reply"]["ts"], "source_ext_msg_id": "qd:5001"}
    assert r["ts"].endswith("+08:00")
    dumped = json.dumps(body, ensure_ascii=False)
    for absent in ("source_msg_seq", "source_msg_uid", "resolved_message_id", str(SYN_ORIG_UID), SYN_SENDER_NAME, a.id):
        assert absent not in dumped                                             # 不发 seq/uid/resolved/sender.name
    assert body["text"] == "回复B" and SRC_TEXT not in body["text"] and SYN_TEXT not in body["text"]
    assert {k: v for k, v in body.items() if k != "reply"} == made.port.normalized[0]


def test_carried_values_taken_verbatim_from_payload(st, hosts):
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(text="另一段合成摘要", ts="2026-01-02T03:04:05+08:00",
                                                    source_ext_msg_id="qd:424242", sender_id="10000000077")))
    assert wire(made) == wire_ok(text="另一段合成摘要", ts="2026-01-02T03:04:05+08:00", source_ext_msg_id="qd:424242",
                                 sender_id="10000000077")


def test_null_carried_values_stay_null(st, hosts):
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(text=None, ts=None, source_ext_msg_id=None, sender_id=None)))
    assert wire(made) == {"state": "ok", **{k: None for k in WIRE_KEYS - {"state"}}}


# ───────────────────────────────────────────── source_text:同账号同会话同纪元、截断、空
def test_source_text_cross_session_real_row_is_null(st, hosts):
    a = qd(SRC_TEXT, 5001, peer=PEER2)
    st.ingest(a)
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id=a.id)))
    assert wire(made) == wire_ok(source_text=None, source_direction="in")      # 未关联 → 按 sender_id 分支


@pytest.mark.parametrize("field,value", [("account_id", "qd02"), ("session_id", f"qd01:{PEER2}"), ("identity_epoch", 1)])
def test_source_text_requires_same_account_session_epoch(st, hosts, monkeypatch, field, value):
    a = qd(SRC_TEXT, 5001, dir="out")
    st.ingest(a)
    real = st.get_message

    def lookup(mid):
        row = real(mid)
        return dict(row, **{field: value}) if mid == a.id and row else row

    monkeypatch.setattr(st, "get_message", lookup)
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id=a.id)))
    r = wire(made)
    assert r["source_text"] is None
    assert r["source_direction"] == "in"                                       # 不取原行 dir=out,走 sender_id 分支


def test_source_text_missing_row_is_null(st, hosts):
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id="msg_synthetic_absent")))
    assert wire(made) == wire_ok(source_text=None, source_direction="in")


def test_source_text_not_guessed_without_resolved_id(st, hosts):
    st.ingest(qd(SYN_TEXT, 5001))                                               # 文本与摘要完全相同、ext 也相同,仍不得猜
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id=None)))
    assert wire(made)["source_text"] is None


@pytest.mark.parametrize("length,expected", [(3999, 3999), (4000, 4000), (4001, 4000), (6500, 4000)])
def test_source_text_truncated_to_4000_chars(st, hosts, length, expected):
    a = qd("占位", 5001)
    st.ingest(a)
    long = ("价" * 3 + "a") * (length // 4) + "价" * (length % 4)
    assert len(long) == length
    st.con.execute("UPDATE messages SET text=? WHERE id=?", (long, a.id))
    assert st.get_message(a.id)["text"] == long
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id=a.id)))
    r = wire(made)
    assert r["source_text"] == long[:4000] and len(r["source_text"]) == expected


@pytest.mark.parametrize("empty", ["", None])
def test_source_text_empty_is_null_but_direction_from_row(st, hosts, empty):
    a = qd("占位", 5001, dir="out")
    st.ingest(a)
    st.con.execute("UPDATE messages SET text=? WHERE id=?", (empty, a.id))
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id=a.id)))
    r = wire(made)
    assert r["source_text"] is None and r["source_direction"] == "out"


# ───────────────────────────────────────────── source_direction 三分支
@pytest.mark.parametrize("row_dir,sender", [("out", OTHER), ("in", SELF), ("out", None), ("in", None)])
def test_direction_branch1_from_resolved_row_overrides_sender(st, hosts, row_dir, sender):
    a = qd(SRC_TEXT, 5001, dir=row_dir)
    st.ingest(a)
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(resolved_message_id=a.id, sender_id=sender)))
    r = wire(made)
    assert r["source_direction"] == row_dir and r["source_text"] == SRC_TEXT and r["sender_id"] == sender


@pytest.mark.parametrize("sender,expected", [(SELF, "out"), (OTHER, "in"), ("10000000077", "in")])
@pytest.mark.parametrize("resolved", [None, "msg_synthetic_absent"])
def test_direction_branch2_from_sender_vs_self_uid(st, hosts, sender, expected, resolved):
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(sender_id=sender, resolved_message_id=resolved)))
    r = wire(made)
    assert r["source_direction"] == expected and r["source_text"] is None


@pytest.mark.parametrize("resolved", [None, "msg_synthetic_absent"])
def test_direction_branch3_null(st, hosts, resolved):
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(sender_id=None, resolved_message_id=resolved)))
    assert wire(made)["source_direction"] is None


# ───────────────────────────────────────────── unparsed
def test_unparsed_from_store_all_other_keys_null(st, hosts):
    b = qd("坏引用C", 5002, reply={"state": "unparsed", **{k: None for k in store_reply() if k != "state"}})
    st.ingest(b)
    made = hosts("h")
    observe(made, message_payload(b, late_after_s=120))
    assert wire(made) == {"state": "unparsed", **{k: None for k in WIRE_KEYS - {"state"}}}


def test_unparsed_ignores_any_other_values(st, hosts):
    a = qd(SRC_TEXT, 5001, dir="out")
    st.ingest(a)
    made = hosts("h")
    observe(made, payload_of(st, reply=view_reply(state="unparsed", resolved_message_id=a.id, sender_id=SELF)))
    assert wire(made) == {"state": "unparsed", **{k: None for k in WIRE_KEYS - {"state"}}}


# ───────────────────────────────────────────── T8:payload reply 非对象 → null + 每原因告警一次
def test_non_object_reply_is_null_and_warned_once(st, hosts, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    values = ["合成非对象引用串", 12345, ["合成列表元素"], True, 1.5]
    for i, value in enumerate(values):
        made = hosts(f"t8-{i}")
        observe(made, payload_of(st, f"正文{i}", 6000 + i, reply=value))
        assert wire(made) is None
        assert len(made.calls) == 1 and len(bodies(made)) == 1
    recs = omit_records(caplog)
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING
    for leak in ("合成非对象引用串", "合成列表元素", "12345"):
        assert leak not in caplog.text


# ───────────────────────────────────────────── 异常容错:reply null,去重/ledger/发布照旧
def _raise_on(st, monkeypatch, target_id, exc):
    real = st.get_message

    def lookup(mid):
        if mid == target_id:
            raise exc
        return real(mid)

    monkeypatch.setattr(st, "get_message", lookup)


def test_exception_reply_null_and_observe_unaffected(st, hosts, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    p = payload_of(st, reply=view_reply(resolved_message_id=a.id))
    plain = dict(p, reply=None)
    ref = hosts("ref")
    observe(ref, plain)
    _raise_on(st, monkeypatch, a.id, RuntimeError("合成异常 " + SRC_TEXT + " " + a.id))
    made = hosts("h")
    observe(made, p)
    assert wire(made) is None
    assert natives(made) == natives(ref) and raw_events(made) == raw_events(ref) and made.calls == ref.calls
    assert bodies(made) == bodies(ref)
    made.host._flush_native()
    assert len(made.published) == 1 and made.published[0]["reply"] is None
    assert raw_events(made)[0][1] == 1
    observe(made, p, seq=2)                                                     # 重放:仍判重、不新增事件
    assert len(bodies(made)) == 1 and natives(made)[0][-1] == 2 and len(made.calls) == 1
    recs = omit_records(caplog)
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING
    assert "合成异常" not in caplog.text and SRC_TEXT not in caplog.text and a.id not in caplog.text


def test_exception_warning_once_per_reason(st, hosts, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    p1 = payload_of(st, "正文1", 5002, reply=view_reply(resolved_message_id=a.id))
    p2 = payload_of(st, "正文2", 5003, reply=view_reply(resolved_message_id=a.id))
    p3 = payload_of(st, "正文3", 5004, reply=view_reply(resolved_message_id=a.id))
    _raise_on(st, monkeypatch, a.id, RuntimeError("x"))
    made = hosts("h")
    observe(made, p1, 1)
    observe(made, p2, 2)
    assert len(omit_records(caplog)) == 1
    _raise_on(st, monkeypatch, a.id, KeyError("y"))
    observe(made, p3, 3)
    assert len(omit_records(caplog)) == 2                                       # 另一原因再告警一次
    assert [b["reply"] for b in bodies(made)] == [None, None, None]
    assert len(made.calls) == 3


# ───────────────────────────────────────────── text_hash / native_key / event_id 不受引用影响
@pytest.mark.parametrize("dir", ["in", "out"])
def test_identity_fields_and_ledger_same_with_and_without_quote(st, hosts, dir):
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    p = payload_of(st, dir=dir, reply=view_reply(resolved_message_id=a.id))
    with_q, without = hosts("with"), hosts("without")
    observe(with_q, p)
    observe(without, dict(p, reply=None))
    assert natives(with_q) == natives(without)
    assert raw_events(with_q) == raw_events(without)
    assert with_q.calls == without.calls and len(with_q.calls) == 1 and with_q.calls[0][0] == dir
    bw, bo = bodies(with_q)[0], bodies(without)[0]
    assert {k: v for k, v in bw.items() if k != "reply"} == {k: v for k, v in bo.items() if k != "reply"}
    assert bw["reply"]["source_text"] == SRC_TEXT and bo["reply"] is None
    native_key, _, text_hash, revoked, _ = natives(with_q)[0]
    assert native_key == json.dumps([PEER, dir, "qd:5002"])
    assert text_hash == hashlib.sha256("回复B".encode()).hexdigest() and revoked == 0
    assert bw["event_id"] == made_event_id(dir)


def made_event_id(dir):
    return "qtrade-" + hashlib.sha256((SELF + "|" + PEER + "|" + dir + "|qd:5002").encode()).hexdigest()


@pytest.mark.parametrize("first,second", [("none", "quote"), ("quote", "none"), ("quote", "other_quote")])
def test_same_native_only_reply_differs_is_not_an_edit(st, hosts, first, second):
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    base = payload_of(st)
    variants = {"none": None, "quote": view_reply(resolved_message_id=a.id),
                "other_quote": view_reply(text="另一摘要", sender_id=SELF)}
    made = hosts("h")
    observe(made, dict(base, reply=variants[first]), 1)
    before = bodies(made)
    observe(made, dict(base, reply=variants[second]), 2)
    assert bodies(made) == before                                              # 不新增事件、旧 body 不回补
    assert len(made.calls) == 1 and natives(made)[0][-1] == 2
    assert all("kind" not in b or b["kind"] == "message" for b in bodies(made))


def test_real_text_edit_still_detected_and_carries_reply(st, hosts):
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    base = payload_of(st)
    made = hosts("h")
    observe(made, base, 1)
    observe(made, dict(base, text="回复B(改)", reply=view_reply(resolved_message_id=a.id)), 2)
    evs = bodies(made)
    assert len(evs) == 2 and evs[1]["kind"] == "edit" and evs[1]["target_event_id"] == evs[0]["event_id"]
    assert evs[1]["reply"]["source_text"] == SRC_TEXT and evs[0]["reply"] is None


# ───────────────────────────────────────────── _replay 与发布带 reply
def test_replay_from_outbox_carries_reply_and_publishes_it(st, hosts):
    made = hosts("h")                                                          # 先装宿主:installation_seq=0,后续事件为实时
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    b = qd("回复B", 5002, reply=store_reply(source_ext_msg_id="qd:5001"))
    st.ingest(b)
    events = Events(st)
    events.emit("message", account_id="qd01", channel="qidian", payload=message_payload(a, late_after_s=120))
    events.emit("message", account_id="qd01", channel="qidian", payload=message_payload(b, late_after_s=120))
    made.host._flush_native()
    assert [x["text"] for x in made.published] == [SRC_TEXT, "回复B"]
    assert made.published[0]["reply"] is None
    assert made.published[1]["reply"] == wire_ok(source_text=SRC_TEXT, ts=message_payload(b, late_after_s=120)["reply"]["ts"])
    assert [b["reply"] for b in bodies(made)] == [p["reply"] for p in made.published]
    assert all(not p["historical"] for p in made.published)
    made.host._flush_native()
    assert len(made.published) == 2                                            # 已发布不重发


# ───────────────────────────────────────────── 日志不含引用内容
def test_logs_never_contain_quote_content(st, hosts, caplog):
    caplog.set_level(logging.DEBUG)
    a = qd(SRC_TEXT, 5001)
    st.ingest(a)
    made = hosts("h")
    for i, reply in enumerate([view_reply(resolved_message_id=a.id), view_reply(state="unparsed"), "合成坏值", None,
                               view_reply(resolved_message_id="msg_synthetic_absent", sender_id=SELF)]):
        observe(made, payload_of(st, f"正文{i}", 7000 + i, reply=reply), i + 1)
    made.host._flush_native()
    for leak in (SRC_TEXT, "第三行", SYN_TEXT, OTHER, SYN_SENDER_NAME, a.id, "合成坏值"):
        assert leak not in caplog.text


# ───────────────────────────────────────────── 历史回补输出不变、不带 reply
def test_history_native_output_identical_with_or_without_extstr():
    t = HistoryTable(table_name(PEER, False), PEER, "private")
    now = 1_790_000_000
    cols = (7, 0, now, -1049, 4242, xor(PEER.encode()).hex(), xor("合成回复B".encode()).hex())
    src = SimpleNamespace(_run=lambda sql: [])
    plain, issue1 = NativeHistoryReader(src).message(t, MainDbRow(*cols))
    quoted, issue2 = NativeHistoryReader(src).message(t, MainDbRow(*cols, reply_ext_hex()))
    assert issue1 is None and issue2 is None
    assert json.dumps(plain, sort_keys=True, ensure_ascii=False) == json.dumps(quoted, sort_keys=True, ensure_ascii=False)
    assert "reply" not in quoted and SYN_TEXT not in json.dumps(quoted, ensure_ascii=False)


# ───────────────────────────────────────────── 对端真 QTradeNativePort(需 PYTHONPATH 指向 producer-peer)
@pytest.fixture
def relay_port():
    protocol = pytest.importorskip("relay.protocol")
    native_port = pytest.importorskip("relay.qtrade_native_port")
    loops = []

    def make(store, channel):
        account_uid = ACCOUNTS[channel][1]
        kw = {} if channel == "qidian" else {"channel": channel}
        binding = protocol.Binding("synthetic-owner", account_uid, "synthetic-wb", b"a" * 16, b"b" * 32, **kw)
        loop = asyncio.new_event_loop()
        loops.append(loop)
        port = native_port.QTradeNativePort(binding=binding, store=store, bus=None, loop=loop, legacy_inbox_disabled=lambda: True)
        port.normalized = []
        real = port.normalize_native_message

        def recording(row, **kw):
            out = real(row, **kw)
            port.normalized.append(copy.deepcopy(out))
            return out

        port.normalize_native_message = recording
        return binding, port

    yield make
    for loop in loops:
        loop.close()


def test_real_port_qidian_reply_added_after_normalize(st, tmp_path, relay_port):
    binding, port = relay_port(st, "qidian")
    made = _build(st, tmp_path, "real", "qidian", port=port, binding=binding)
    try:
        a = qd(SRC_TEXT, 5001)
        st.ingest(a)
        b = qd("回复B", 5002, reply=store_reply(source_ext_msg_id="qd:5001"))
        st.ingest(b)
        Events(st).emit("message", account_id="qd01", channel="qidian", payload=message_payload(b, late_after_s=120))
        made.host._flush_native()
        assert len(made.published) == 1
        pub = made.published[0]
        assert "reply" not in port.normalized[0]                                # 对端规范化本身不转发 reply
        assert {k: v for k, v in pub.items() if k != "reply"} == port.normalized[0]
        assert set(pub["reply"]) == WIRE_KEYS and pub["reply"]["source_text"] == SRC_TEXT
        assert pub["reply"]["source_direction"] == "in" and pub["reply"]["sender_id"] == OTHER
        assert pub["text"] == "回复B"
    finally:
        made.loop.close()


def test_real_port_wechat_has_no_reply_key(st, tmp_path, relay_port):
    binding, port = relay_port(st, "wechat")
    made = _build(st, tmp_path, "real-wx", "wechat", port=port, binding=binding)
    try:
        m = Message(account_id="wx01", channel="wechat", session=Session("wx01", "friend", "private", "friend"), dir="in",
                    type="text", text="合成微信正文", ts_ms=1_790_000_000_000, source="chatlog", ext_msg_id="wx-1",
                    sender_id="friend")
        st.ingest(m)
        p = message_payload(m, late_after_s=120)
        p["reply"] = view_reply()
        observe(made, p, account_id="wx01", channel="wechat")
        body = bodies(made)[0]
        assert "reply" not in body and body == port.normalized[0]
    finally:
        made.loop.close()
