"""企点引用对象 ``reply`` 的入库、迁移、事件与 API 视图 —— 对照 docs/06 §2.9.5、docs/02 §2.8.1 / §3.3 / #48 / §3.4.7、
docs/00 §7.4 ``reply`` 段(R6-116)。序列化流由 ``tests/qidian_reply_stream.py`` 合成。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from qtrade_agent.adapters.qidian import reply as reply_mod
from qtrade_agent.adapters.qidian.history_native import HistoryTable, NativeHistoryReader
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.msgdata import MainDbRow
from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.alerts import Alerts
from qtrade_agent.api.serialize import message_view
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, ApiConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.events import Events, iso8601, message_payload
from qtrade_agent.models import Message, Session
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host
from qtrade_agent.store import Store, store as store_mod
from tests.conftest import Clock, FakeMainDb, table_name
from tests.qidian_reply_stream import (SYN_ORIG_UID, SYN_SENDER_NAME, SYN_SENDER_UIN, SYN_SEQ, SYN_TEXT, SYN_TIME_S,
                                       fields_without, reply_ext_hex)

PEER = "10000000009"
PEER2 = "10000000010"
STORE_KEYS = {"state", "text", "sender_id", "sender_name", "ts_ms", "source_msg_seq", "source_msg_uid",
              "source_ext_msg_id", "resolved_message_id"}
VIEW_KEYS = {"state", "text", "sender", "ts", "source_msg_seq", "source_msg_uid", "source_ext_msg_id", "resolved_message_id"}
UNPARSED_VIEW = {"state": "unparsed", "text": None, "sender": {"id": None, "name": None}, "ts": None, "source_msg_seq": None,
                 "source_msg_uid": None, "source_ext_msg_id": None, "resolved_message_id": None}


@pytest.fixture(autouse=True)
def _fresh_warned():
    reply_mod._warned.clear()
    yield


def ok_reply(**over):
    r = {"state": "ok", "text": SYN_TEXT, "sender_id": str(SYN_SENDER_UIN), "sender_name": SYN_SENDER_NAME,
         "ts_ms": SYN_TIME_S * 1000, "source_msg_seq": str(SYN_SEQ), "source_msg_uid": str(SYN_ORIG_UID),
         "source_ext_msg_id": None, "resolved_message_id": None}
    r.update(over)
    return r


def qd(text, uniseq, *, peer=PEER, reply=None, ts_ms=1_790_000_100_000, dir="in", account="qd01", **kw):
    return Message(account_id=account, channel="qidian", session=Session(account, peer, "private", peer), dir=dir, type="text",
                   text=text, ts_ms=ts_ms, source="qidian_db", ext_msg_id=f"qd:{uniseq}", sender_id=peer if dir == "in" else "3007373675",
                   self=dir == "out", reply=reply, **kw)


def reply_json(st, mid):
    v = st.con.execute("SELECT reply_json FROM messages WHERE id=?", (mid,)).fetchone()[0]
    return None if v is None else json.loads(v)


# ───────────────────────────────────────────── 0003 迁移
def _hash(path):
    with open(path, encoding="utf-8") as f:
        return hashlib.sha256(f.read().encode()).hexdigest()


def test_0003_file_is_exactly_the_spec_statement():
    path = os.path.join(store_mod.MIGRATIONS_DIR, "0003_message_reply.sql")
    with open(path, "rb") as f:
        body = f.read().decode("utf-8")
    assert " ".join(body.split()) == ("ALTER TABLE messages ADD COLUMN reply_json TEXT CHECK "
                                      "(reply_json IS NULL OR json_valid(reply_json));")
    assert store_mod.code_schema_version() == 3
    with open(store_mod.SCHEMA_PATH, encoding="utf-8") as f:
        assert "reply_json" not in f.read()                                     # 0001 基线不动
    with open(os.path.join(store_mod.MIGRATIONS_DIR, "0002_native_identity_epoch.sql"), encoding="utf-8") as f:
        assert "reply_json" not in f.read()


def test_0003_upgrades_existing_0001_0002_db_and_old_rows_stay_null(tmp_path, monkeypatch):
    old_dir = tmp_path / "mig_v2"
    old_dir.mkdir()
    shutil.copy(os.path.join(store_mod.MIGRATIONS_DIR, "0002_native_identity_epoch.sql"), old_dir)
    real_dir = store_mod.MIGRATIONS_DIR
    db = str(tmp_path / "agent.db")
    clock = Clock()

    monkeypatch.setattr(store_mod, "MIGRATIONS_DIR", str(old_dir))
    s = Store(db, clock=clock).open()
    assert s.schema_version() == 2
    assert "reply_json" not in {r[1] for r in s.con.execute("PRAGMA table_info(messages)")}
    s.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    old = qd("老消息", 1)
    with s._tx() as c:
        s._upsert_session(c, old, clock())
        c.execute("INSERT INTO messages(id, ext_msg_id, account_id, channel, session_id, dir, type, text, ts_ms, received_ms, source) "
                  "VALUES ('msg_old_1','qd:1','qd01','qidian',?,'in','text','老消息',1,1,'qidian_db')", (old.session_id,))
    before = [tuple(r) for r in s.con.execute("SELECT version, name, checksum FROM schema_version ORDER BY version")]
    s.close()

    monkeypatch.setattr(store_mod, "MIGRATIONS_DIR", real_dir)
    s = Store(db, clock=clock).open()
    try:
        assert s.schema_version() == 3
        after = [tuple(r) for r in s.con.execute("SELECT version, name, checksum FROM schema_version ORDER BY version")]
        assert after[:2] == before                                               # 0001/0002 记录与校验和不变
        assert after[2][:2] == (3, "0003_message_reply")
        assert after[2][2] == _hash(os.path.join(real_dir, "0003_message_reply.sql"))
        assert before[0][2] == _hash(store_mod.SCHEMA_PATH)
        assert before[1][2] == _hash(os.path.join(real_dir, "0002_native_identity_epoch.sql"))
        assert s.con.execute("SELECT reply_json FROM messages WHERE id='msg_old_1'").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError):
            s.con.execute("UPDATE messages SET reply_json='not json' WHERE id='msg_old_1'")
        r = s.ingest(qd("新消息", 2, reply=ok_reply()))
        assert reply_json(s, r.id)["state"] == "ok"
    finally:
        s.close()


# ───────────────────────────────────────────── 入库形与 resolved_message_id
def test_store_shape_types_and_resolved_same_session(store):
    a = store.ingest(qd("原消息A", 5001))
    b = qd("回复B", 5002, reply=ok_reply(source_ext_msg_id="qd:5001"))
    rb = store.ingest(b)
    rj = reply_json(store, rb.id)
    assert set(rj) == STORE_KEYS
    assert rj == ok_reply(source_ext_msg_id="qd:5001", resolved_message_id=a.id)
    types = store.con.execute(
        "SELECT json_type(reply_json,'$.ts_ms'), json_type(reply_json,'$.source_msg_uid'), json_type(reply_json,'$.sender_id'), "
        "json_type(reply_json,'$.source_msg_seq') FROM messages WHERE id=?", (rb.id,)).fetchone()
    assert tuple(types) == ("integer", "text", "text", "text")
    assert b.reply == rj                                                          # 事件据此生成,与库同形


def test_resolved_cross_session_not_matched(store):
    store.ingest(qd("原消息A", 5001, peer=PEER2))
    rb = store.ingest(qd("回复B", 5002, reply=ok_reply(source_ext_msg_id="qd:5001")))
    rj = reply_json(store, rb.id)
    assert rj["source_ext_msg_id"] == "qd:5001" and rj["resolved_message_id"] is None


def test_resolved_cross_identity_epoch_not_matched(store):
    store.ingest(qd("原消息A", 5001))
    assert store.observe_qidian_identity("qd01", "3007373675", "3007373676") is not None
    rb = store.ingest(qd("回复B", 5002, reply=ok_reply(source_ext_msg_id="qd:5001")))
    assert reply_json(store, rb.id)["resolved_message_id"] is None


def test_resolved_only_at_ingest_time_no_backfill(store):
    rb = store.ingest(qd("回复B", 5002, reply=ok_reply(source_ext_msg_id="qd:5001")))
    store.ingest(qd("原消息A(后到)", 5001))
    assert reply_json(store, rb.id)["resolved_message_id"] is None


def test_resolved_null_without_source_ext_msg_id(store):
    store.ingest(qd("原消息A", 5001))
    rb = store.ingest(qd("回复B", 5002, reply=ok_reply()))
    rj = reply_json(store, rb.id)
    assert rj["source_ext_msg_id"] is None and rj["resolved_message_id"] is None


def test_unparsed_is_stored_with_all_other_keys_null(store):
    rb = store.ingest(qd("回复B", 5002, reply={"state": "unparsed", **{k: None for k in STORE_KEYS - {"state"}}}))
    assert reply_json(store, rb.id) == {"state": "unparsed", **{k: None for k in STORE_KEYS - {"state"}}}


def test_no_reply_is_sql_null(store):
    r = store.ingest(qd("普通", 5003))
    assert store.con.execute("SELECT reply_json FROM messages WHERE id=?", (r.id,)).fetchone()[0] is None


# ───────────────────────────────────────────── 引用不进 text / fingerprint / text_hash / FTS
def test_text_fingerprint_hash_identical_with_and_without_reply(clock):
    stores = []
    for reply in (None, ok_reply(source_ext_msg_id="qd:1")):
        s = Store(":memory:", clock=clock).open()
        s.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
        r = s.ingest(qd("1Y 1.70 回复正文", 7001, reply=reply))
        stores.append((s, r.id))
    cols = "text, text_len, text_hash, fingerprint, ext_msg_id, dedup_kind"
    rows = [tuple(s.con.execute(f"SELECT {cols} FROM messages WHERE id=?", (mid,)).fetchone()) for s, mid in stores]
    assert rows[0] == rows[1]
    s, mid = stores[1]
    assert SYN_TEXT not in rows[1][0]
    hits, _ = s.query_messages(account_id="qd01", q="被引用摘要")
    assert hits == []                                                             # FTS 不含引用内容
    hits, _ = s.query_messages(account_id="qd01", q="回复正文")
    assert [h["id"] for h in hits] == [mid]
    for s, _ in stores:
        s.close()


# ───────────────────────────────────────────── 写入时机:已存在行不改、不算 changed
def test_rescan_existing_row_never_changes_reply(store):
    r1 = store.ingest(qd("B", 5002))
    again = store.ingest(qd("B", 5002, reply=ok_reply()))
    assert (again.inserted, again.changed) == (False, False)
    assert reply_json(store, r1.id) is None
    r2 = store.ingest(qd("C", 5003, reply=ok_reply()))
    m = qd("C", 5003, reply=None)
    again2 = store.ingest(m)
    assert (again2.inserted, again2.changed) == (False, False)
    assert reply_json(store, r2.id) == ok_reply()
    assert m.reply == ok_reply()                                                 # 返回给事件的是库里的值


def _sending(store, text="收到", reply=None):
    out = Message(account_id="qd01", channel="qidian", session=Session("qd01", PEER, "private", PEER), dir="out", type="text",
                  text=text, ts_ms=store._clock(), source="ui", sender_id="3007373675", self=True, state="SENDING",
                  trace_id="T1", idempotency_key="k1", reply=reply)
    return store.ingest(out)


def test_outbound_merge_fills_reply_only_when_null(store, clock):
    sent = _sending(store)
    assert reply_json(store, sent.id) is None
    back = qd("收到", 8001, dir="out", ts_ms=clock() + 2000, reply=ok_reply())
    res = store.ingest(back)
    assert res.id == sent.id and res.inserted is False
    assert reply_json(store, sent.id) == ok_reply()
    assert back.reply == ok_reply()
    row = store.get_message(sent.id)
    assert row["state"] == "DELIVERED" and row["ext_msg_id"] == "qd:8001"


def test_outbound_merge_keeps_existing_non_null_reply(store, clock):
    old = ok_reply(text="旧引用")
    sent = _sending(store, reply=old)
    back = qd("收到", 8002, dir="out", ts_ms=clock() + 2000, reply=ok_reply(text="新引用"))
    store.ingest(back)
    assert reply_json(store, sent.id) == old
    assert back.reply == old


def test_capture_text_false_nulls_reply_text_only(clock):
    s = Store(":memory:", clock=clock, capture_text=False).open()
    try:
        s.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
        r = s.ingest(qd("正文", 9001, reply=ok_reply()))
        assert reply_json(s, r.id) == ok_reply(text=None)
        assert s.con.execute("SELECT text FROM messages WHERE id=?", (r.id,)).fetchone()[0] is None
    finally:
        s.close()


# ───────────────────────────────────────────── QQ / 微信恒 null
def test_qq_and_wechat_reply_always_null(store):
    store.ensure_account("wx01", "wechat", state="running", self_uid="wxid_synthetic")
    qq = Message(account_id="qq03", channel="qq", session=Session("qq03", PEER, "private", PEER), dir="in", type="text",
                 text="qq", ts_ms=1_790_000_000_000, source="onebot", ext_msg_id="77", sender_id=PEER, reply=ok_reply())
    wx = Message(account_id="wx01", channel="wechat", session=Session("wx01", "friend", "private", "friend"), dir="in", type="text",
                 text="wx", ts_ms=1_790_000_000_000, source="chatlog", ext_msg_id="wx-1", sender_id="friend", reply=ok_reply())
    for m in (qq, wx):
        r = store.ingest(m)
        assert reply_json(store, r.id) is None
        assert message_payload(m, late_after_s=120)["reply"] is None
        view = message_view(store.get_message_full(r.id))
        assert "reply" in view and view["reply"] is None


# ───────────────────────────────────────────── 视图:键恒在、ID 字符串、ts +08:00
def test_view_shapes_from_payload_and_row_are_identical(store):
    a = store.ingest(qd("原消息A", 5001))
    cases = [
        qd("B1", 6001, reply=ok_reply(source_ext_msg_id="qd:5001")),
        qd("B2", 6002, reply={"state": "unparsed", **{k: None for k in STORE_KEYS - {"state"}}}),
        qd("B3", 6003, reply=ok_reply(sender_id=None, sender_name=None, ts_ms=None, text=None, source_msg_seq=None)),
        qd("B4", 6004),
    ]
    for m in cases:
        r = store.ingest(m)
        ev = message_payload(m, late_after_s=120)
        api = message_view(store.get_message_full(r.id))
        assert "reply" in ev and "reply" in api
        assert ev["reply"] == api["reply"]
    v1 = message_view(store.get_message_full(cases[0].id))["reply"]
    assert set(v1) == VIEW_KEYS and set(v1["sender"]) == {"id", "name"}
    assert v1["resolved_message_id"] == a.id and v1["source_ext_msg_id"] == "qd:5001"
    assert v1["sender"] == {"id": str(SYN_SENDER_UIN), "name": SYN_SENDER_NAME}
    assert v1["source_msg_uid"] == str(SYN_ORIG_UID) and v1["source_msg_seq"] == str(SYN_SEQ)
    assert v1["ts"] == iso8601(SYN_TIME_S * 1000) and v1["ts"].endswith("+08:00")
    assert v1["text"] == SYN_TEXT and v1["state"] == "ok"
    assert message_view(store.get_message_full(cases[1].id))["reply"] == UNPARSED_VIEW
    v3 = message_view(store.get_message_full(cases[2].id))["reply"]
    assert set(v3) == VIEW_KEYS and v3["sender"] == {"id": None, "name": None} and v3["ts"] is None and v3["text"] is None
    assert message_view(store.get_message_full(cases[3].id))["reply"] is None


# ───────────────────────────────────────────── 轮询端到端(假主库带 extStr / msgUid)
class ReplyMainDb(FakeMainDb):
    """与真机同名的 extStr / msgUid 两列;其余沿用 conftest 的假主库。"""

    def ensure_table(self, peer: str, group: bool) -> str:
        t = table_name(peer, group)
        con = sqlite3.connect(self.path)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{t}" (_id INTEGER PRIMARY KEY AUTOINCREMENT, issend INTEGER, istroop INTEGER, '
                    f'time INTEGER, msgtype INTEGER, uniseq INTEGER, msgseq INTEGER, shmsgseq INTEGER, senderuin BLOB, frienduin BLOB, '
                    f'msgData BLOB, extStr BLOB, msgUid INTEGER)')
        con.commit()
        con.close()
        return t

    def insert_reply(self, peer, text, *, time_s, ext_hex="", msguid=None, msgtype=-1049, **kw) -> int:
        rid = self.insert(peer, group=False, time_s=time_s, msgtype=msgtype, msgdata=text.encode("utf-8"), **kw)
        con = sqlite3.connect(self.path)
        con.execute(f'UPDATE "{table_name(peer, False)}" SET extStr=?, msgUid=? WHERE _id=?',
                    (bytes.fromhex(ext_hex) if ext_hex else None, msguid, rid))
        con.commit()
        con.close()
        return rid


@pytest.fixture
def env(store, clock, tmp_path):
    db = ReplyMainDb(str(tmp_path / "3007373675.db"))
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    poller = QidianPoller(store=store, events=events, alerts=alerts, cfg=AgentConfig(), h13_firing=lambda: False, clock=clock,
                          maindb_factory=lambda uid, acct: LocalSqliteMainDb(db.path))
    return SimpleNamespace(store=store, clock=clock, db=db, poller=poller, alerts=alerts)


def acct():
    return QidianAccountView("qd01", "running", "3007373675")


def msg_events(st):
    return st.list_events("message", "qd01")


def test_poll_links_reply_and_emits_no_extra_events(env):
    st, clk, db, p = env.store, env.clock, env.db, env.poller
    p.poll_maindb(acct())                                                         # bootstrap
    db.insert_reply(PEER, "原消息A", time_s=clk.now_s, msgtype=-1000, uniseq=777001, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)
    db.insert_reply(PEER, "回复正文B", time_s=clk.now_s, uniseq=777002, shmsgseq=SYN_SEQ + 1, msguid=1, ext_hex=reply_ext_hex())
    db.insert_reply(PEER, "坏引用C", time_s=clk.now_s, uniseq=777003, shmsgseq=SYN_SEQ + 2, msguid=2, ext_hex=reply_ext_hex(value="Z1"))
    p.poll_maindb(acct())
    ev = msg_events(st)
    assert len(ev) == 3                                                           # 每条消息恰一个事件
    by_text = {e["payload"]["text"]: e["payload"] for e in ev}
    a, b, c = by_text["原消息A"], by_text["回复正文B"], by_text["坏引用C"]
    assert a["reply"] is None
    assert b["reply"]["source_ext_msg_id"] == "qd:777001" and b["reply"]["resolved_message_id"] == a["id"]
    assert b["reply"]["text"] == SYN_TEXT and b["text"] == "回复正文B"
    assert c["reply"] == UNPARSED_VIEW and c["text"] == "坏引用C"
    assert reply_json(st, b["id"])["resolved_message_id"] == a["id"]
    assert st.cursor_get("qd01", f"qidian_rowid:{PEER}").value_int == 3          # 水位越过 unparsed 行
    # 重扫:不改、不发
    cur = st.cursor_get("qd01", f"qidian_rowid:{PEER}")
    st.cursor_set("qd01", cur.kind, cur.value_int, json.dumps({"last_uniseq": 1}))
    before = reply_json(st, b["id"])
    p.poll_maindb(acct())
    assert len(msg_events(st)) == 3 and reply_json(st, b["id"]) == before
    assert not env.alerts.active


def test_poll_table_without_extstr_column_keeps_text(env):
    st, clk, p = env.store, env.clock, env.poller
    plain = FakeMainDb(env.db.path)                                               # conftest 的表没有 extStr / msgUid
    p.poll_maindb(acct())
    plain.insert_text(PEER2, "无 extStr 列的正文", time_s=clk.now_s)
    p.poll_maindb(acct())
    ev = msg_events(st)
    assert len(ev) == 1 and ev[0]["payload"]["text"] == "无 extStr 列的正文" and ev[0]["payload"]["reply"] is None
    assert not env.alerts.active


def test_poll_outbound_merge_with_reply_emits_single_event(env):
    st, clk, db, p = env.store, env.clock, env.db, env.poller
    db.insert_reply(PEER, "历史", time_s=clk.now_s - 3600, msgtype=-1000)
    p.poll_maindb(acct())
    sent = _sending(st, text="收到")
    clk.advance(5000)
    db.insert_reply(PEER, "收到", time_s=clk.now_s, issend=1, uniseq=880001, ext_hex=reply_ext_hex())
    p.poll_maindb(acct())
    ev = msg_events(st)
    assert len(ev) == 1 and ev[0]["payload"]["id"] == sent.id
    assert ev[0]["payload"]["reply"]["state"] == "ok"
    assert reply_json(st, sent.id)["state"] == "ok"


# ───────────────────────────────────────────── 事件 payload 与 GET /messages 同形(经真 API)
TOKEN = "devtest-console-token"


def test_event_payload_equals_get_messages_view(tmp_path):
    clock = Clock(auto_step_ms=200)
    db = ReplyMainDb(str(tmp_path / "3007373675.db"))
    cfg = AgentConfig(bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0), qidian=QidianAdapterConfig(confirm_poll_interval_ms=10),
                      api=ApiConfig(http_sync_max_wait_ms=25000))
    agent = AgentApp(cfg, db_path=str(tmp_path / "agent.db"), clock=clock,
                     wsl_env_reader=SimpleNamespace(versions=AsyncMock(return_value={})),
                     deployment_disks=SimpleNamespace(snapshot=AsyncMock(return_value={"disks": [], "error": "test_host_unknown"})),
                     maindb_factory=lambda uid, acct: LocalSqliteMainDb(db.path)).open()
    st = agent.store
    try:
        st.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
        st.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOKEN)
        db.insert_reply(PEER, "历史", time_s=clock.now_s - 3600, msgtype=-1000)
        api = agent.create_api()
        with TestClient(api, client=("127.0.0.1", 40000)) as client:
            client.portal.call(agent.qidian_poll_all)
            db.insert_reply(PEER, "原消息A", time_s=clock.now_s, msgtype=-1000, uniseq=990001, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)
            db.insert_reply(PEER, "回复B", time_s=clock.now_s, uniseq=990002, ext_hex=reply_ext_hex())
            db.insert_reply(PEER, "坏引用C", time_s=clock.now_s, uniseq=990003, ext_hex=reply_ext_hex(value="Z1"))
            db.insert_reply(PEER, "缺字段D", time_s=clock.now_s, uniseq=990004,
                            ext_hex=reply_ext_hex(fields_without("mSourceMsgTime", "senderName", "mSourceQdMsgUserName", "mSourceMsgSenderUin")))
            client.portal.call(agent.qidian_poll_all)
            evs = {e["payload"]["id"]: e["payload"] for e in st.list_events("message", "qd01")}
            assert len(evs) == 4
            data = client.get("/api/v1/messages?account_id=qd01&limit=50", headers={"Authorization": f"Bearer {TOKEN}"}).json()["data"]
            views = {m["id"]: m for m in data}
            for mid, payload in evs.items():
                assert "reply" in payload and "reply" in views[mid]
                assert payload["reply"] == views[mid]["reply"]
                one = client.get(f"/api/v1/messages/{mid}", headers={"Authorization": f"Bearer {TOKEN}"}).json()["data"]
                assert one["reply"] == payload["reply"]
            by_text = {v["text"]: v for v in views.values()}
            b = by_text["回复B"]["reply"]
            assert set(b) == VIEW_KEYS and b["resolved_message_id"] == by_text["原消息A"]["id"]
            assert b["source_ext_msg_id"] == "qd:990001" and b["ts"].endswith("+08:00")
            for k in ("source_msg_seq", "source_msg_uid", "source_ext_msg_id", "resolved_message_id"):
                assert isinstance(b[k], str)
            assert isinstance(b["sender"]["id"], str)
            assert by_text["原消息A"]["reply"] is None
            assert by_text["坏引用C"]["reply"] == UNPARSED_VIEW
            d = by_text["缺字段D"]["reply"]
            assert set(d) == VIEW_KEYS and d["ts"] is None and d["sender"] == {"id": None, "name": None}
    finally:
        st.close()


# ───────────────────────────────────────────── 询价端 observe:带 reply 的载荷行为不变
class _Port:
    channel = "qidian"

    def __init__(self, binding):
        self.binding = binding
        self.rows = []
        self.native_reconcile = lambda *a, **k: None

    def _account(self):
        return {"id": "qd01", "self_uid": "3007373675", "channel": "qidian"}

    def normalize_native_message(self, row, live=True):
        # 对端按固定字段取值(06/00 §15g R6-116:reply 不会被转发);此处只登记收到的行
        self.rows.append(dict(row))
        return {"event_id": "ev-" + row["id"], "peer_id": row["session_id"], "direction": row["dir"],
                "ext_msg_id": row["ext_msg_id"], "text": row["text"], "historical": not live, "channel": "qidian"}


def _host(store, tmp_path, name):
    binding = SimpleNamespace(owner_user_id="synthetic-owner", account_uid="3007373675", workbench_id="synthetic-wb")
    calls = []
    ledger = SimpleNamespace(begin_inbound=lambda *a: calls.append(("in",) + a), stage_out=lambda *a: calls.append(("out",)),
                             permission=lambda _: True)
    port = _Port(binding)
    bridge = SimpleNamespace(binding=binding, ledger=ledger, adapters={}, echo_wait_seconds=1, native_reconcile=port.native_reconcile)
    loop = asyncio.new_event_loop()
    host = XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop, binding=binding,
                         bridge=bridge, port=port, state_path=tmp_path / f"{name}.db", legacy_inbox_disabled=lambda: True)
    return host, calls, loop


def _host_state(host):
    with host._db() as db:
        return ([tuple(r) for r in db.execute("SELECT event_id, body, published FROM host_event ORDER BY rowid")],
                [tuple(r) for r in db.execute("SELECT * FROM host_native ORDER BY native_key")])


def test_xunjia_observe_with_reply_payload_behaves_the_same(store, tmp_path):
    a = qd("原消息A", 5001)
    store.ingest(a)
    b = qd("回复B", 5002, reply=ok_reply(source_ext_msg_id="qd:5001"))
    store.ingest(b)
    with_reply = message_payload(b, late_after_s=120)
    assert with_reply["reply"] is not None
    without = dict(with_reply, reply=None)
    results = []
    for name, payload in (("with", with_reply), ("without", without)):
        host, calls, loop = _host(store, tmp_path, name)
        try:
            host.observe(event="message", payload=payload, account_id="qd01", channel="qidian", source_seq=1)
            assert host._intake_ok is True
            results.append((_host_state(host), calls))
        finally:
            loop.close()
    # R6-117(06 §2.9.5「传到询价端」):企点 body 多出 reply 键,身份字段、ledger 与其余 body 不变
    (ev_w, nat_w), calls_w = results[0]
    (ev_o, nat_o), calls_o = results[1]
    assert nat_w == nat_o and calls_w == calls_o
    assert [(e[0], e[2]) for e in ev_w] == [(e[0], e[2]) for e in ev_o]
    body_w, body_o = json.loads(ev_w[0][1]), json.loads(ev_o[0][1])
    assert body_o["reply"] is None
    assert set(body_w["reply"]) == {"state", "text", "source_text", "source_direction", "sender_id", "ts", "source_ext_msg_id"}
    assert body_w["reply"]["source_text"] == "原消息A" and body_w["reply"]["source_direction"] == "in"
    assert {k: v for k, v in body_w.items() if k != "reply"} == {k: v for k, v in body_o.items() if k != "reply"}
    # 同一宿主:先收无引用、再收带引用的同一条(重放)→ 不判为编辑、不新增事件
    host, calls, loop = _host(store, tmp_path, "replay")
    try:
        host.observe(event="message", payload=without, account_id="qd01", channel="qidian", source_seq=1)
        host.observe(event="message", payload=with_reply, account_id="qd01", channel="qidian", source_seq=2)
        events, native = _host_state(host)
        assert len(events) == 1 and len(native) == 1 and native[0][-1] == 2
        assert host._intake_ok is True and len(calls) == 1
    finally:
        loop.close()


# ───────────────────────────────────────────── history_native:本轮不改,输出不含 reply
class _HistorySource:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def _run(self, sql):
        self.sql.append(sql)
        return self.rows


def test_history_native_page_and_message_have_no_reply():
    t = HistoryTable(table_name(PEER, False), PEER, "private")
    now = 1_790_000_000
    src = _HistorySource([(7, 0, now, -1049, 4242, xor(PEER.encode()).hex(), xor("回复B".encode()).hex())])
    rows = NativeHistoryReader(src).page(t, after=0, now=now)
    assert len(rows) == 1 and rows[0].extstr_hex == ""
    assert "extStr" not in src.sql[0]
    row = MainDbRow(7, 0, now, -1049, 4242, xor(PEER.encode()).hex(), xor("回复B".encode()).hex(), reply_ext_hex())
    out, issue = NativeHistoryReader(src).message(t, row)
    assert issue is None and out["text"] == "回复B"
    assert "reply" not in out and SYN_TEXT not in json.dumps(out, ensure_ascii=False)
    assert set(out) == {"native_id", "peer_id", "direction", "occurred_at", "text", "type", "session_kind", "media", "historical"}
