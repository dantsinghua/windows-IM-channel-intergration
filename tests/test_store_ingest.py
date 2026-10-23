"""store.ingest 三元组、同事务顺序、出向合并定序、QQ id 复用、空批推游标 —— 对照 02 §2.2.8 / §2.8.1、06 §2.9.2 / §2.12。"""
import sqlite3

import pytest

from qtrade_agent.models import Message, Session
from qtrade_agent.store import CursorUpdate, Store


def qd_msg(text, *, ts_ms, ext, dir="in", self=False, native="415011447", sender=None, state="DELIVERED", trace_id=None, idem=None):
    return Message(account_id="qd01", channel="qidian", session=Session("qd01", native, "group" if native.startswith("g_") else "private", name=native),
                   dir=dir, type="text", text=text, ts_ms=ts_ms, source="qidian_db" if ext else "ui", ext_msg_id=ext,
                   sender_id=sender or ("3007373675" if self else native), self=self, state=state, trace_id=trace_id, idempotency_key=idem)


def test_schema_applies_and_fts_trigram(store: Store):
    n = store.con.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
    assert n >= 27
    assert store.con.execute("select version from schema_version").fetchone()[0] == 1


def test_ingest_inserts_session_first_then_message(store: Store, clock):
    r = store.ingest(qd_msg("报价 1.70", ts_ms=clock() - 5000, ext="qd:1"))
    assert r.inserted and not r.changed and r.id.startswith("msg_")
    s = store.con.execute("select id, kind, msg_count from sessions where account_id='qd01'").fetchone()
    assert s["id"] == "qd01:415011447" and s["kind"] == "private" and s["msg_count"] == 1
    m = store.get_message(r.id)
    assert m["fingerprint"] and m["text_len"] == 7 and m["state"] == "DELIVERED" and m["received_ms"] == clock()
    # FTS trigram:≥3 字符子串命中(02 §2.8.6:2 字走 LIKE,UI 提示「关键字至少 3 个字」)
    assert store.con.execute("""select count(*) from messages_fts where messages_fts match '"1.70"'""").fetchone()[0] == 1
    assert store.con.execute("""select count(*) from messages_fts where messages_fts match '"报价 1"'""").fetchone()[0] == 1


def test_rescan_same_key_no_change_is_false_false(store: Store, clock):
    m = qd_msg("重扫", ts_ms=clock(), ext="qd:2")
    assert store.ingest(m).inserted
    r2 = store.ingest(qd_msg("重扫", ts_ms=clock(), ext="qd:2"))
    assert (r2.inserted, r2.changed) == (False, False)              # 重扫不重放
    assert store.count_messages("qd01") == 1


def test_revoke_flip_is_changed(store: Store, clock):
    store.ingest(qd_msg("撤", ts_ms=clock(), ext="qd:3"))
    m = qd_msg("撤", ts_ms=clock(), ext="qd:3")
    m.revoked = True
    r = store.ingest(m)
    assert (r.inserted, r.changed) == (False, True)
    assert store.get_message(r.id)["revoked"] == 1


def test_outbound_sending_row_then_merge_on_readback(store: Store, clock):
    out = qd_msg("收到", ts_ms=clock(), ext=None, dir="out", self=True, state="SENDING", trace_id="T1", idem="k1")
    r = store.ingest(out)
    assert r.inserted and store.get_message(r.id)["state"] == "SENDING" and store.get_message(r.id)["ext_msg_id"] is None
    # 幂等重试命中同 idempotency_key 复用这一行
    r_again = store.ingest(qd_msg("收到", ts_ms=clock(), ext=None, dir="out", self=True, state="SENDING", trace_id="T1", idem="k1"))
    assert (r_again.inserted, r_again.id) == (False, r.id)
    # 读库 9 s 后拉回 issend=1 的同文本行 → 合并进 SENDING 行,不插第二行
    back = qd_msg("收到", ts_ms=clock() + 9000, ext="qd:900", dir="out", self=True)
    rb = store.ingest(back)
    assert (rb.inserted, rb.changed, rb.id) == (False, True, r.id)
    row = store.get_message(r.id)
    assert row["state"] == "DELIVERED" and row["ext_msg_id"] == "qd:900" and row["confirmed_by"] == "ingest_merge"
    assert store.count_messages("qd01") == 1
    # 同一条读到的行再撞:按唯一键幂等跳过
    assert store.ingest(back) == store.ingest(back)


def test_merge_uses_norm_and_window_and_empty_text_guard(store: Store, clock):
    store.ingest(qd_msg("收到　ＯＫ", ts_ms=clock(), ext=None, dir="out", self=True, state="SENDING", trace_id="T2"))
    # 全角 vs 半角、多余空白:norm 后相等 ⇒ 合并
    rb = store.ingest(qd_msg("收到 OK", ts_ms=clock() + 3000, ext="qd:901", dir="out", self=True))
    assert rb.changed
    # 窗外(61 s)不合并 ⇒ 当「非本系统发出的我方消息」插新行(trace_id NULL)
    store.ingest(qd_msg("晚了", ts_ms=clock(), ext=None, dir="out", self=True, state="SENDING", trace_id="T3"))
    rb2 = store.ingest(qd_msg("晚了", ts_ms=clock() + 61_000, ext="qd:902", dir="out", self=True))
    assert rb2.inserted and store.get_message(rb2.id)["trace_id"] is None
    # 空文本守卫(R6-44):两侧任一为空时不走「同会话 + norm 相等 + 窗内」这一支,只认 fingerprint 相同
    store.ingest(qd_msg(None, ts_ms=clock(), ext=None, dir="out", self=True, state="SENDING", trace_id="T4", native="g_1"))
    rb3 = store.ingest(qd_msg(None, ts_ms=clock() + 5000, ext="qd:903", dir="out", self=True, native="g_1"))
    assert rb3.inserted                                                     # 不同秒 ⇒ fingerprint 不同 ⇒ 空对空不平凡合并
    rb4 = store.ingest(qd_msg(None, ts_ms=clock(), ext="qd:904", dir="out", self=True, native="g_1"))
    assert rb4.changed                                                      # 同秒同人同会话 ⇒ fingerprint 相同 ⇒ 合并(规格允许)


def test_merge_orders_by_time_distance_then_earlier_rowid(store: Store, clock):
    a = store.ingest(qd_msg("同文", ts_ms=clock(), ext=None, dir="out", self=True, state="SENDING", trace_id="A"))
    b = store.ingest(qd_msg("同文", ts_ms=clock() + 20_000, ext=None, dir="out", self=True, state="SENDING", trace_id="B"))
    rb = store.ingest(qd_msg("同文", ts_ms=clock() + 25_000, ext="qd:904", dir="out", self=True))
    assert rb.id == b.id                                             # |25−20| < |25−0|
    rb2 = store.ingest(qd_msg("同文", ts_ms=clock() + 1000, ext="qd:905", dir="out", self=True))
    assert rb2.id == a.id


def test_qq_message_id_reuse_after_one_hour(store: Store, clock):
    def qq(ts):
        return Message(account_id="qq03", channel="qq", session=Session("qq03", "g_123", "group", "群"), dir="in", type="text", text="x",
                       ts_ms=ts, source="onebot", ext_msg_id="g_123:77", sender_id="1")
    assert store.ingest(qq(clock())).inserted
    r = store.ingest(qq(clock() + 10 * 60_000))
    assert (r.inserted, r.changed) == (False, False)                   # ≤1h 同一条
    r2 = store.ingest(qq(clock() + 2 * 3600_000))
    assert r2.inserted and store.get_message(r2.id)["ext_msg_id"] == "g_123:77#2"
    r3 = store.ingest(qq(clock() + 5 * 3600_000))
    assert r3.inserted and store.get_message(r3.id)["ext_msg_id"] == "g_123:77#3"


def test_ingest_batch_empty_only_moves_cursor(store: Store, clock):
    out = store.ingest_batch([], CursorUpdate("qd01", "qidian_rowid:415011447", 42, '{"last_uniseq": 9}'))
    assert out == []
    c = store.cursor_get("qd01", "qidian_rowid:415011447")
    assert c.value_int == 42 and c.value_json() == {"last_uniseq": 9}


def test_ingest_batch_is_one_transaction(store: Store, clock, monkeypatch):
    msgs = [qd_msg("a", ts_ms=clock(), ext="qd:10"), qd_msg("b", ts_ms=clock(), ext="qd:11")]
    original = store._cursor_set

    def boom(*a, **k):
        raise sqlite3.OperationalError("injected before cursor")
    monkeypatch.setattr(store, "_cursor_set", boom)
    with pytest.raises(sqlite3.OperationalError):
        store.ingest_batch(msgs, CursorUpdate("qd01", "qidian_rowid:415011447", 2))
    monkeypatch.setattr(store, "_cursor_set", original)
    assert store.count_messages("qd01") == 0 and store.cursor_get("qd01", "qidian_rowid:415011447") is None   # 消息与游标都没动
    out = store.ingest_batch(msgs, CursorUpdate("qd01", "qidian_rowid:415011447", 2))
    assert [o[0] for o in out] == [True, True] and store.cursor_get("qd01", "qidian_rowid:415011447").value_int == 2


def test_capture_text_off_keeps_len_hash_fingerprint(clock):
    s = Store(":memory:", clock=clock, capture_text=False).open()
    s.ensure_account("qd01", "qidian", state="running", self_uid="1")
    r = s.ingest(qd_msg("秘密", ts_ms=clock(), ext="qd:1"))
    m = s.get_message(r.id)
    assert m["text"] is None and m["text_len"] == 2 and m["text_hash"] and m["fingerprint"]
    s.close()


def test_bootstrap_rebase_audits_and_deletes_in_one_tx(store: Store, clock):
    store.cursor_set("qd01", "qidian_rowid:1", 5, None)
    store.cursor_set("qd01", "qidian_rowid:g_2", 7, None)
    deleted = store.qidian_bootstrap_rebase("qd01", "999", old_uin="3007373675", now_ms=clock())
    assert deleted == 2 and store.cursors_list("qd01", "qidian_rowid:") == []
    boot = store.cursor_get("qd01", "qidian_bootstrap")
    assert boot.value == "999" and boot.value_int == clock()
    a = store.list_audit("qidian.rebootstrap")
    assert len(a) == 1 and a[0]["kind"] == "system" and a[0]["actor"] == "system:qidian_adapter"
    assert '"deleted_cursors": 2' in a[0]["detail_json"] and '"old_uin": "3007373675"' in a[0]["detail_json"]
    # 首次(old_uin=None):不记审计
    store.qidian_bootstrap_rebase("qd01", "999", old_uin=None, now_ms=clock())
    assert len(store.list_audit("qidian.rebootstrap")) == 1
