"""独立验收:R6-117 企点引用传到询价端的线上字段(采集端 ``xunjia_v22.observe`` 补键)。

唯一判据 = 06 §2.9.5「引用对象 `reply`」条末子条「传到询价端(R6-117)」+ 00 §15g R6-117 行
+ 00 §7.4 `reply` 视图(R6-116,上游定义)+ DECISION-2 附录 A T8。

搭建只用公开构造签名与 ``observe`` 公开接口;宿主自身状态库 ``host_event(event_id, body)`` 与
桥的 ``publish_native`` 是观察面。对端 port 为假实现:按规格只返回固定字段、绝不含 ``reply``。
全部数据合成:不含真实聊天内容、真实 uin 或真实样本值。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest

from qtrade_agent.events import Events, message_payload
from qtrade_agent.models import Message, Session
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host
from qtrade_agent.store import Store

SELF_UID = "20001"            # 绑定账号 self_uid(合成)
PEER_UID = "30002"            # 对端(合成)
ACCOUNT = "qd90"
PEER = "peer-alpha"
OTHER_PEER = "peer-beta"
TS_MS = 1_792_202_400_000
QUOTE_TS = "2026-10-28T09:30:00+08:00"
SEVEN_KEYS = {"state", "text", "source_text", "source_direction", "sender_id", "ts", "source_ext_msg_id"}
SECRET_QUOTE = "合成引用摘要-SENTINEL-Q7"
SECRET_SOURCE = "合成原消息全文-SENTINEL-S9"


# ---------------------------------------------------------------- 假边界(同 tests/acceptance/test_xunjia_v22_host.py 写法)

class Ledger:
    def __init__(self, order):
        self.order, self.pending = order, set()

    def begin_inbound(self, event_id, peer_id):
        self.order.append(("begin", event_id))
        self.pending.add(event_id)

    def stage_out(self, data, seconds):
        self.order.append(("stage_out", data["event_id"]))
        return "waiting"

    def permission(self, command):
        return not self.pending


class Port:
    """假对端 port:``normalize_native_message`` 只按固定字段生成载荷,不转发 ``reply``(R6-117 只读探查结论)。"""

    def __init__(self, binding, channel="qidian", account=ACCOUNT):
        self.binding, self.channel, self.account = binding, channel, account
        self.adapters = {}
        self.execution_permission = None
        self.calls = []
        self.rows = []

    def _account(self):
        return {"id": self.account, "self_uid": self.binding.account_uid, "state": "running"}

    def normalize_native_message(self, row, live=False):
        self.rows.append(dict(row))
        return {"event_id": "native-" + row["ext_msg_id"], "peer_id": row["session_id"].split(":", 1)[1],
                "direction": row["dir"], "text": row["text"], "kind": "message", "ext_msg_id": row["ext_msg_id"],
                "occurred_at": row["ts_ms"] / 1000, "historical": not live,
                "native_account_uid": row.get("native_account_uid"), "identity_epoch": row.get("identity_epoch"),
                "channel": self.channel}

    def native_send(self, command):
        return {"status": "failed", "evidence": "synthetic"}

    def native_reconcile(self, command):
        return {"status": "unknown", "evidence": "synthetic"}


class Bridge:
    echo_wait_seconds = 1

    def __init__(self, binding):
        self.binding = binding
        self.order, self.published = [], []
        self.ledger = Ledger(self.order)
        self.adapters = {"oss": SimpleNamespace(publish_contacts=lambda payload: None)}

    def publish_native(self, data, historical=False):
        self.order.append(("publish", data["event_id"]))
        self.published.append(deepcopy(data))
        self.ledger.pending.discard(data["event_id"])

    def run_once(self):
        return {"processed": 0, "errors": []}

    def publish_capabilities(self):
        pass


class ExplodingStore(Store):
    """真实 store,但按 id 读「被引用原消息」那一行(``get_message``,经 store 读 messages 行的公开入口)时抛异常;
    其它行照常可读,不干扰 observe 改前已有的读取。"""

    poisoned: set = set()
    hits = 0

    def get_message(self, id):
        if id in self.poisoned:
            self.hits += 1
            raise RuntimeError("synthetic store failure")
        return super().get_message(id)


def binding():
    return SimpleNamespace(owner_user_id="synthetic-owner", account_uid=SELF_UID, workbench_id="synthetic-workbench")


def new_store(tmp_path, name="store.sqlite3"):
    store = Store(str(tmp_path / name)).open()
    store.ensure_account(ACCOUNT, "qidian", state="running", self_uid=SELF_UID)
    store.ensure_account("qd91", "qidian", state="running", self_uid="20009")
    store.ensure_account("qq90", "qq", state="running", self_uid=SELF_UID)
    store.ensure_account("wx90", "wechat", state="running", self_uid="wxid_synthetic_self")
    return store


class Rig:
    def __init__(self, tmp_path, loop, store, *, channel="qidian", account=ACCOUNT, state_name="host.sqlite3"):
        self.binding = binding()
        self.bridge, self.port = Bridge(self.binding), Port(self.binding, channel, account)
        self.store, self.channel, self.account = store, channel, account
        self.state_path = tmp_path / state_name
        self.host = XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop,
                                  binding=self.binding, bridge=self.bridge, port=self.port,
                                  state_path=self.state_path, legacy_inbox_disabled=lambda: True)
        self._n = 0

    def observe(self, payload, *, historical=False, event_id=None):
        self._n += 1
        self.host.observe(event="message", payload=payload, account_id=payload.get("account_id", self.account),
                          event_id=event_id or f"evt-{self._n}", channel=self.channel, now_ms=TS_MS + self._n,
                          historical=historical)

    def bodies(self):
        con = sqlite3.connect(str(self.state_path))
        try:
            return {eid: body for eid, body in con.execute("SELECT event_id, body FROM host_event")}
        finally:
            con.close()

    def body(self, ext_msg_id):
        raw = self.bodies()["native-" + ext_msg_id]
        return raw, json.loads(raw)


def run(coro_fn):
    async def main():
        return await coro_fn(asyncio.get_running_loop())
    return asyncio.run(main())


def msg(text, ext, *, account=ACCOUNT, peer=PEER, dir="in", sender=PEER_UID, channel="qidian"):
    return Message(account_id=account, channel=channel, session=Session(account, peer, "private"),
                   ext_msg_id=ext, dir=dir, type="text", text=text, ts_ms=TS_MS, source="qidian_db",
                   sender_id=sender if dir == "in" else SELF_UID, self=(dir == "out"))


def ingest(store, m):
    return store.ingest(m, now_ms=TS_MS).id


def payload_for(m, reply="__absent__"):
    """事件 payload = 00 §7.4 Message 视图 + 事件三字段;``reply`` 覆盖为给定视图(或删除该键)。"""
    body = message_payload(m, late_after_s=120)
    if reply == "__absent__":
        body.pop("reply", None)
    else:
        body["reply"] = reply
    return body


def view(*, state="ok", text=SECRET_QUOTE, sender_id=PEER_UID, sender_name="合成联系人", ts=QUOTE_TS,
         seq="501", uid="9007199254740993", ext="qd:7001", resolved=None):
    """00 §7.4 R6-116 视图形(九个取值,sender 恒为对象,ID 一律字符串)。"""
    return {"state": state, "text": text, "sender": {"id": sender_id, "name": sender_name}, "ts": ts,
            "source_msg_seq": seq, "source_msg_uid": uid, "source_ext_msg_id": ext, "resolved_message_id": resolved}


def wire_of(rig, m, reply="__absent__", **kw):
    rig.observe(payload_for(m, reply), **kw)
    return rig.body(m.ext_msg_id)[1]


# ================================================================ 键是否存在 / 形状

def test_qidian_body_always_has_reply_key_null_when_no_quote(tmp_path):
    """原句:只对企点通道事件恒存在(无引用为 null);payload 无 reply 键或为 null → "reply": null。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        absent = msg("合成报价一", "qd:1101")
        none = msg("合成报价二", "qd:1102")
        ingest(store, absent), ingest(store, none)
        b1 = wire_of(rig, absent)                 # payload 无 reply 键
        b2 = wire_of(rig, none, None)             # payload reply = null
        assert "reply" in b1 and b1["reply"] is None
        assert "reply" in b2 and b2["reply"] is None
        await rig.host.run_once()
        assert all("reply" in d and d["reply"] is None for d in rig.bridge.published)
        store.close()
    run(go)


def test_ok_reply_is_exactly_seven_keys_with_verbatim_copies(tmp_path):
    """原句:恰好七个键;state/text/ts/source_ext_msg_id 原样取,sender_id = payload reply.sender.id;
    source_msg_seq、source_msg_uid、resolved_message_id、sender.name 不发。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        m = msg("合成报价三", "qd:1201")
        ingest(store, m)
        w = wire_of(rig, m, view(text="摘要" * 1000, ext="qd:7001", resolved=None))["reply"]
        assert isinstance(w, dict) and set(w) == SEVEN_KEYS
        assert w["state"] == "ok"
        assert w["text"] == "摘要" * 1000
        assert w["sender_id"] == PEER_UID
        assert w["ts"] == QUOTE_TS
        assert w["source_ext_msg_id"] == "qd:7001"
        for banned in ("source_msg_seq", "source_msg_uid", "resolved_message_id", "sender", "sender_name"):
            assert banned not in w
        await rig.host.run_once()
        assert rig.bridge.published[-1]["reply"] == w, "发布载荷与 host_event body 应同形"
        store.close()
    run(go)


def test_ok_reply_null_values_keep_all_seven_keys(tmp_path):
    """原句:键恒在,除 state 外值可为 null。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        m = msg("合成报价四", "qd:1301")
        ingest(store, m)
        w = wire_of(rig, m, view(text=None, sender_id=None, sender_name=None, ts=None, seq=None, uid=None,
                                 ext=None, resolved=None))["reply"]
        assert set(w) == SEVEN_KEYS
        assert w == {"state": "ok", "text": None, "source_text": None, "source_direction": None,
                     "sender_id": None, "ts": None, "source_ext_msg_id": None}
        store.close()
    run(go)


def test_unparsed_forces_other_six_keys_null(tmp_path):
    """原句:state="unparsed" → 其余六键全部 null(即使 payload 其余字段非空、原消息可读)。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        orig = msg("合成原消息", "qd:1400", dir="out")
        oid = ingest(store, orig)
        m = msg("合成报价五", "qd:1401")
        ingest(store, m)
        w = wire_of(rig, m, view(state="unparsed", sender_id=SELF_UID, ext="qd:1400", resolved=oid))["reply"]
        assert w == {"state": "unparsed", "text": None, "source_text": None, "source_direction": None,
                     "sender_id": None, "ts": None, "source_ext_msg_id": None}
        store.close()
    run(go)


@pytest.mark.parametrize("bad", ["synthetic-string", ["state", "ok"], 7, True], ids=["str", "list", "int", "bool"])
def test_non_object_payload_reply_becomes_null_and_logs_without_content(tmp_path, caplog, bad):
    """原句(T8):payload reply 既非 null 也非对象 → 线上 "reply": null,按原因每进程告警一次(日志不含引用内容)。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        m1, m2 = msg("合成报价六", "qd:1501"), msg("合成报价七", "qd:1502")
        ingest(store, m1), ingest(store, m2)
        sentinel = bad if not isinstance(bad, str) else bad + "-" + SECRET_QUOTE
        with caplog.at_level(logging.DEBUG):
            w1 = wire_of(rig, m1, sentinel)
            first = len([r for r in caplog.records if r.levelno >= logging.WARNING])
            w2 = wire_of(rig, m2, sentinel)
            second = len([r for r in caplog.records if r.levelno >= logging.WARNING])
        assert "reply" in w1 and w1["reply"] is None
        assert "reply" in w2 and w2["reply"] is None
        assert second == first, "同一原因每进程只告警一次,第二次不得再告警"
        assert all(SECRET_QUOTE not in r.getMessage() for r in caplog.records)
        await rig.host.run_once()
        assert [d["event_id"] for d in rig.bridge.published] == ["native-qd:1501", "native-qd:1502"]
        store.close()
    run(go)


# ================================================================ source_text

def test_source_text_from_resolved_row_same_account_session_epoch(tmp_path):
    """原句:resolved_message_id 非 null 时经 store 读该 messages 行(同账号、同会话、同 identity_epoch),取其 text。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        orig = msg(SECRET_SOURCE + "\n第二行\n第三行", "qd:2000")
        oid = ingest(store, orig)
        m = msg("合成报价八", "qd:2001")
        ingest(store, m)
        w = wire_of(rig, m, view(text="合成摘要前两行", ext="qd:2000", resolved=oid))["reply"]
        assert w["source_text"] == SECRET_SOURCE + "\n第二行\n第三行"
        assert w["text"] == "合成摘要前两行", "text 仍原样取摘要,不被原文覆盖"
        store.close()
    run(go)


@pytest.mark.parametrize("n,expect", [(4000, 4000), (4001, 4000), (9000, 4000), (3999, 3999)])
def test_source_text_truncated_to_4000(tmp_path, n, expect):
    """原句:超过 4000 字符截到 4000。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        full = ("价" * 7 + "\n") * (n // 8 + 1)
        full = full[:n]
        oid = ingest(store, msg(full, "qd:2100"))
        m = msg("合成报价九", "qd:2101")
        ingest(store, m)
        w = wire_of(rig, m, view(ext="qd:2100", resolved=oid))["reply"]
        stored = store.get_message(oid)["text"]
        assert len(w["source_text"]) == min(len(stored), 4000) == expect
        assert w["source_text"] == stored[:4000]
        store.close()
    run(go)


def _mismatch_case(tmp_path, loop, kind):
    store = new_store(tmp_path)
    rig = Rig(tmp_path, loop, store)
    if kind == "other_account":
        oid = ingest(store, msg(SECRET_SOURCE, "qd:2200", account="qd91", dir="out"))
    elif kind == "other_session":
        oid = ingest(store, msg(SECRET_SOURCE, "qd:2200", peer=OTHER_PEER, dir="out"))
    elif kind == "other_epoch":
        oid = ingest(store, msg(SECRET_SOURCE, "qd:2200", dir="out"))
        con = sqlite3.connect(str(tmp_path / "store.sqlite3"))   # 隔离库内合成「旧身份纪元」的原消息行
        with con:
            con.execute("UPDATE messages SET identity_epoch=identity_epoch+7 WHERE id=?", (oid,))
        con.close()
    elif kind == "missing":
        oid = "msg_01SYNTHETICNOTEXIST0000000"
    else:
        raise AssertionError(kind)
    return store, rig, oid


@pytest.mark.parametrize("kind", ["other_account", "other_session", "other_epoch", "missing"])
def test_source_text_null_when_row_missing_or_check_fails_and_direction_falls_to_sender(tmp_path, kind):
    """原句:行不存在、校验不符 → source_text null;未关联但 sender_id 非 null → 按 self_uid 判方向。
    原消息被造成 dir="out",sender_id 给对端:若错误地「关联」了,方向会是 out。"""
    async def go(loop):
        store, rig, oid = _mismatch_case(tmp_path, loop, kind)
        m = msg("合成报价十", "qd:2201")
        ingest(store, m)
        w = wire_of(rig, m, view(sender_id=PEER_UID, ext="qd:2200", resolved=oid))["reply"]
        assert w["source_text"] is None
        assert SECRET_SOURCE not in json.dumps(w, ensure_ascii=False)
        assert w["source_direction"] == "in"
        assert w["state"] == "ok" and w["source_ext_msg_id"] == "qd:2200"
        store.close()
    run(go)


def test_source_text_null_when_original_text_empty(tmp_path):
    """原句:text 为空 → source_text null。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        oid = ingest(store, msg("", "qd:2300"))
        m = msg("合成报价十一", "qd:2301")
        ingest(store, m)
        w = wire_of(rig, m, view(ext="qd:2300", resolved=oid))["reply"]
        assert w["source_text"] is None
        store.close()
    run(go)


def test_source_text_never_guessed_without_resolved_id(tmp_path):
    """原句:仅当 resolved_message_id 非 null 时读;不得按文本相似、时间邻近或「最近一条」猜原消息。
    同会话里放一条与摘要同文、同时刻、且 ext_msg_id 恰为 source_ext_msg_id 的消息,resolved 为 null 时仍须 null。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        ingest(store, msg(SECRET_QUOTE, "qd:2400", dir="out"))
        m = msg("合成报价十二", "qd:2401")
        ingest(store, m)
        w = wire_of(rig, m, view(text=SECRET_QUOTE, sender_id=None, ext="qd:2400", resolved=None))["reply"]
        assert w["source_text"] is None
        assert w["source_direction"] is None, "未关联且 sender_id 为 null → 第三分支 null,不得从猜到的行取 dir"
        store.close()
    run(go)


# ================================================================ source_direction 三分支

@pytest.mark.parametrize("orig_dir,sender,expect", [("out", PEER_UID, "out"), ("in", SELF_UID, "in")],
                         ids=["row_out_beats_sender_peer", "row_in_beats_sender_self"])
def test_direction_branch1_linked_row_dir_wins(tmp_path, orig_dir, sender, expect):
    """原句:①原消息已关联(读到且校验通过)→ 取该行 dir(与 sender_id 推断相反时仍取行 dir)。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        oid = ingest(store, msg("合成原消息方向", "qd:3000", dir=orig_dir))
        m = msg("合成报价十三", "qd:3001")
        ingest(store, m)
        w = wire_of(rig, m, view(sender_id=sender, ext="qd:3000", resolved=oid))["reply"]
        assert w["source_text"] == "合成原消息方向"
        assert w["source_direction"] == expect
        store.close()
    run(go)


@pytest.mark.parametrize("sender,expect", [(SELF_UID, "out"), (PEER_UID, "in"), ("40003", "in")])
def test_direction_branch2_sender_vs_bound_self_uid(tmp_path, sender, expect):
    """原句:②未关联但 sender_id 非 null → 等于绑定账号 self_uid 为 "out",否则 "in"。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        m = msg("合成报价十四", "qd:3101")
        ingest(store, m)
        w = wire_of(rig, m, view(sender_id=sender, resolved=None))["reply"]
        assert w["source_direction"] == expect
        assert w["sender_id"] == sender
        store.close()
    run(go)


def test_direction_branch3_null(tmp_path):
    """原句:③都没有 → null。"""
    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        m = msg("合成报价十五", "qd:3201")
        ingest(store, m)
        w = wire_of(rig, m, view(sender_id=None, resolved=None))["reply"]
        assert w["source_direction"] is None
        store.close()
    run(go)


# ================================================================ 异常容错

def test_exception_during_supplement_sets_reply_null_and_observe_unaffected(tmp_path, caplog):
    """原句:补键过程中任何异常 → reply 置 null,按原因每进程告警一次(日志不含引用内容与原文),
    不得影响 observe 原有行为(去重、ledger、发布照旧)。"""
    async def go(loop):
        store = ExplodingStore(str(tmp_path / "exploding.sqlite3")).open()
        store.ensure_account(ACCOUNT, "qidian", state="running", self_uid=SELF_UID)
        oid = ingest(store, msg(SECRET_SOURCE, "qd:4000"))
        store.poisoned = {oid}
        rig = Rig(tmp_path, loop, store)
        m = msg("合成报价十六", "qd:4001")
        ingest(store, m)
        payload = payload_for(m, view(text=SECRET_QUOTE, ext="qd:4000", resolved=oid))
        with caplog.at_level(logging.DEBUG):
            rig.observe(payload)
            rig.observe(deepcopy(payload))          # 去重:同一原生消息再来一次
        raw, body = rig.body("qd:4001")
        assert store.hits >= 1, "夹具未能在补键阶段制造异常(本条无法验证)"
        assert "reply" in body and body["reply"] is None
        assert rig.bridge.order == [("begin", "native-qd:4001")], "ledger 登记照旧且只一次"
        await rig.host.run_once()
        assert [d["event_id"] for d in rig.bridge.published] == ["native-qd:4001"]
        assert rig.bridge.published[0]["text"] == "合成报价十六"
        assert all(SECRET_QUOTE not in r.getMessage() and SECRET_SOURCE not in r.getMessage() for r in caplog.records)
        store.close()
    run(go)


# ================================================================ 不进 text / text_hash / native_key / event_id

def _identity_columns(state_path):
    """宿主状态库里所有名为 text_hash / native_key / event_id 的列值(按表排序),用作身份与摘要快照。"""
    con = sqlite3.connect(str(state_path))
    try:
        snap = {}
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            keep = [c for c in cols if c in ("text_hash", "native_key", "event_id")]
            if keep:
                rows = con.execute(f'SELECT {",".join(keep)} FROM "{table}" ORDER BY {",".join(keep)}').fetchall()
                snap[table] = (keep, rows)
        return snap
    finally:
        con.close()


def test_reply_not_in_text_text_hash_native_key_event_id(tmp_path):
    """原句:reply(含 source_text)不进 text、text_hash、native_key、event_id。
    两个隔离宿主各收同一条消息,一个带引用(含可读原文)一个不带:身份/摘要列逐值相同,body.text 相同。"""
    async def go(loop):
        store = new_store(tmp_path)
        oid = ingest(store, msg(SECRET_SOURCE, "qd:5000", dir="out"))
        m = msg("合成报价十七", "qd:5001")
        ingest(store, m)
        with_reply = Rig(tmp_path, loop, store, state_name="with.sqlite3")
        without = Rig(tmp_path, loop, store, state_name="without.sqlite3")
        bw = wire_of(with_reply, m, view(ext="qd:5000", resolved=oid))
        bn = wire_of(without, m, None)
        assert bw["reply"]["source_text"] == SECRET_SOURCE
        assert bw["text"] == bn["text"] == "合成报价十七"
        assert bw["event_id"] == bn["event_id"]
        snap_w, snap_n = _identity_columns(with_reply.state_path), _identity_columns(without.state_path)
        assert any(k in ("text_hash", "native_key") for keep, _ in snap_w.values() for k in keep), \
            "宿主状态库应可观察到 text_hash/native_key 列(否则本条无法验证)"
        assert snap_w == snap_n
        for keep, rows in snap_w.values():
            assert SECRET_SOURCE not in repr(rows) and SECRET_QUOTE not in repr(rows)
        assert {k: v for k, v in bw.items() if k != "reply"} == {k: v for k, v in bn.items() if k != "reply"}
        store.close()
    run(go)


@pytest.mark.parametrize("first,second", [("none", "quote"), ("quote", "other_quote")])
def test_same_native_message_with_different_reply_is_not_new_event_or_edit(tmp_path, first, second):
    """原句:同一原生消息只因 reply 不同不会产生 edit/retract 事件(host_native 仍按 text_hash 判重);
    已写入 host_event 的旧 body 不回补。"""
    replies = {"none": None, "quote": view(text="合成摘要甲"), "other_quote": view(text="合成摘要乙", sender_id=SELF_UID)}

    async def go(loop):
        store = new_store(tmp_path)
        rig = Rig(tmp_path, loop, store)
        m = msg("合成报价十八", "qd:5101")
        ingest(store, m)
        rig.observe(payload_for(m, replies[first]))
        raw_before = rig.bodies()
        rig.observe(payload_for(m, replies[second]))
        assert rig.bodies() == raw_before, "同一原生消息再来一次(仅 reply 不同)不得新增或改写 host_event"
        await rig.host.run_once()
        assert [d["kind"] for d in rig.bridge.published] == ["message"]
        store.close()
    run(go)


# ================================================================ 其它通道

@pytest.mark.parametrize("channel,account,ext", [("wechat", "wx90", "wx:6001"), ("qq", "qq90", "qq:6001")])
def test_non_qidian_channel_has_no_reply_key_and_bytes_unchanged(tmp_path, channel, account, ext):
    """原句:QQ、微信等其它通道不加该键,载荷字节与改前一致。
    同一条非企点消息在两个隔离宿主里各收一次(payload 带/不带 reply):body 字节逐字相同且无 reply 键。"""
    async def go(loop):
        store = new_store(tmp_path)
        m = msg("合成非企点报价", ext, account=account, channel=channel)
        m.source = "chatlog" if channel == "wechat" else "onebot"
        ingest(store, m)
        a = Rig(tmp_path, loop, store, channel=channel, account=account, state_name="a.sqlite3")
        b = Rig(tmp_path, loop, store, channel=channel, account=account, state_name="b.sqlite3")
        a.observe(payload_for(m, view(ext="qd:1")))
        b.observe(payload_for(m, "__absent__"))
        ra, rb = a.bodies(), b.bodies()
        assert ra, f"{channel} 通道事件应写入 host_event(否则本条无法验证)"
        assert ra == rb
        for raw in ra.values():
            assert "reply" not in json.loads(raw)
        store.close()
    run(go)


# ================================================================ 实时链路与重放

def test_events_wiring_and_replay_carry_reply(tmp_path):
    """原句:_replay 重放同样经过 observe,按同规则补键。
    经真实 Events(on_emit=host.observe)发出带引用的 message 事件;另起一个全新状态库的宿主,
    由其 run_once 从持久 outbox 重放,body 同样带七键 reply 与原文。"""
    async def go(loop):
        store = new_store(tmp_path)
        live = Rig(tmp_path, loop, store, state_name="live.sqlite3")
        events = Events(store, on_emit=live.host.observe)
        oid = ingest(store, msg(SECRET_SOURCE, "qd:7000", dir="out"))
        m = msg("合成报价十九", "qd:7001")
        ingest(store, m)
        events.emit("message", payload=payload_for(m, view(sender_id=PEER_UID, ext="qd:7000", resolved=oid)),
                    account_id=ACCOUNT, channel="qidian", now_ms=TS_MS + 10)
        lw = live.body("qd:7001")[1]["reply"]
        assert lw["source_text"] == SECRET_SOURCE and lw["source_direction"] == "out"
        await live.host.run_once()
        assert live.bridge.published[-1]["reply"] == lw

        replay = Rig(tmp_path, loop, store, state_name="replay.sqlite3")
        await replay.host.run_once()
        bodies = replay.bodies()
        if "native-qd:7001" not in bodies:
            pytest.skip("新状态库宿主未从 outbox 重放该事件(重放窗口属既有行为,非本条范围)")
        rw = json.loads(bodies["native-qd:7001"])["reply"]
        assert rw == lw
        store.close()
    run(go)


@pytest.mark.parametrize("module", ["qtrade_agent.adapters.qidian.history_native", "qtrade_agent.ossbox.history_publish"])
def test_history_backfill_modules_carry_no_quote(module):
    """原句:历史回补 history_native.py / history_publish.py 的输出不变、不带引用。
    静态代理:两模块既不读 extStr、也不产出/转发任何 reply 字段(行为未改的旁证;git 差异核对见交接)。"""
    import importlib
    import inspect
    source = inspect.getsource(importlib.import_module(module)).lower()
    assert "extstr" not in source
    assert "reply" not in source
