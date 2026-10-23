"""按设计文档写的验收用例 —— 消息库 store + 指令总线 bus(首批代码)。

🔴 断言只依据规格,不按实现反推:
- 02 §2.2.2 bus(七段流水 / 出向先落库 / 幂等三态 / 登录门 / args_json 不存正文)
- 02 §2.2.8 store(ingest 三元组 / 同事务顺序 / 空批只推游标)
- 02 §2.8.1 每通道入库路径表(QQ id 复用 #n / 出向行 / 入向撞到自己发的)
- 02 §3.1 DDL 注释、§3.10 校验段(R6-48)、§8b 验收行 B-06/B-07/B-08/B-19/B-30/B-40
- 06 §2.9.2(唯一键 / norm() / fingerprint / QQ 规则 / 通用兜底)、§2.12 全节、§2.9.5 掉线续读条(origin 判据)、§8b M2/M2.5 行
- 00 §7.2 Command、§7.3 CommandResult、§7.4 Message、§8.3 结果码表

每个用例顶部注释写清规格条款;断言的是规格说的可观测结果(表里几行、列值、结果码、error.reason / details[].pointer、幂等行状态、事件条数)。
夹具来自 tests/conftest.py:``clock``(可拨时钟)、``store``(内存 agent.db,含 qd01/qq03)、``maindb``(假企点主库)。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata

import pytest

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qidian.adapter import QidianAdapter
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
from qtrade_agent.alerts import Alerts
from qtrade_agent.bus.bus import Bus
from qtrade_agent.bus.validate import validate_send_text
from qtrade_agent.config import AgentConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Command, Message, Session
from qtrade_agent.store import CursorUpdate, Store
from qtrade_agent.text import norm as impl_norm

# ────────────────────────────────────────────────────────────────────── 常量与规格自抄的参考实现

QD = "qd01"
SELF_UID = "3007373675"          # conftest 里 qd01 的 self_uid
PEER = "415011447"               # 企点单聊对端 uin(00 §6 / R6-21:单聊 native_id = <对端uin>)
QD_SESSION = f"{QD}:{PEER}"
T0 = 1_758_240_000_000           # conftest Clock 的起始时刻(ms)
ASYNC_TIMEOUT = 20               # 单个异步用例的真实时间上限(秒);pytest-timeout 不可用,用 wait_for 兜


def spec_norm(s):
    """06 §2.9.2 原文照抄的 norm():只做三件事——NFKC、折叠空白、去首尾空白;不剥 U+0014。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def spec_fingerprint(account_id, session_id, sender, text, ts_ms, media_sha256s=""):
    """06 §2.9.2 字面公式:sha256(account_id | session_id | sender_id_or_name | norm(text) | media_sha256s | ts 取整到秒)。

    规格只给了「用 | 分隔」的写法,没规定 media_sha256s 列表如何编码;本文件只在**无媒体**(空串)下比对。
    """
    raw = "|".join([account_id, session_id, sender or "", spec_norm(text), media_sha256s, str(ts_ms // 1000)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


_DEFAULT = object()      # qd_in 的哨兵:sender_id 未显式给 ⇒ 按 self 取 SELF_UID / 对端 uin;显式给 None ⇒ 「无 sender_id」


def qd_in(text, ts_ms, *, ext, peer=PEER, sender_id=_DEFAULT, sender_name=None, self=False,
          revoked=False, revoked_ms=None, account_id=QD, kind="private", name="对端"):
    """企点读库入向行(source=qidian_db, dedup_kind=native, ext='qd:{uniseq}')。"""
    native = peer
    if sender_id is _DEFAULT:
        sender_id = SELF_UID if self else peer
    return Message(account_id=account_id, channel="qidian", session=Session(account_id, native, kind, name), dir="in",
                   type="text", text=text, ts_ms=ts_ms, source="qidian_db", ext_msg_id=ext, dedup_kind="native",
                   sender_id=sender_id, sender_name=sender_name, self=self, revoked=revoked, revoked_ms=revoked_ms)


def qd_self_in(text, ts_ms, *, ext, peer=PEER):
    """读库拉回的「我方发的」行(issend=1):dir=in 由 store 决定合并;这里按 06 §2.12「轮询把我方消息当普通行拉回来(self=true)」构造。"""
    return Message(account_id=QD, channel="qidian", session=Session(QD, peer, "private", "对端"), dir="out", type="text",
                   text=text, ts_ms=ts_ms, source="qidian_db", ext_msg_id=ext, dedup_kind="native",
                   sender_id=SELF_UID, self=True, state="DELIVERED")


def qd_out(text, ts_ms, *, key, trace="TRACE-" + "X", peer=PEER):
    """bus 发送前先落库的出向行(06 §2.12:dir=out、self=true、state=SENDING、ext NULL、idempotency_key、trace_id、source=ui)。"""
    return Message(account_id=QD, channel="qidian", session=Session(QD, peer, "private", peer), dir="out", type="text",
                   text=text, ts_ms=ts_ms, source="ui", ext_msg_id=None, sender_id=SELF_UID, self=True, state="SENDING",
                   trace_id=trace, idempotency_key=key)


def qq_in(text, ts_ms, *, ext, revoked=False, revoked_ms=None, group="g_456"):
    return Message(account_id="qq03", channel="qq", session=Session("qq03", group, "group", "测试群"), dir="in", type="text",
                   text=text, ts_ms=ts_ms, source="onebot", ext_msg_id=ext, sender_id="10001", sender_name="张三",
                   revoked=revoked, revoked_ms=revoked_ms)


def rows(store, sql, *params):
    return [tuple(r) for r in store.con.execute(sql, params).fetchall()]


def one(store, sql, *params):
    r = store.con.execute(sql, params).fetchone()
    return tuple(r) if r is not None else None


def count(store, sql, *params):
    return store.con.execute(sql, params).fetchone()[0]


# ══════════════════════════════════════════════════════════════════════ 一、norm() 与 fingerprint(06 §2.9.2)


def test_T01_norm_three_things_only():
    """06 §2.9.2 R6-47:norm() 只做三件事——NFKC、折叠一切空白为单个空格、strip;不剥 U+0014、不改 [表情] 字面量;
    norm(None) == norm("") == "";NFKC 幂等 ⇒ norm(norm(s)) == norm(s)。"""
    # ① NFKC:全角字母/全角空格抹平
    assert impl_norm("收到　ＯＫ") == "收到 OK"
    # ② 折叠空白:\t\n\r 与多个空格 → 单个空格
    assert impl_norm("1Y\n1.70\t\t2Y \r\n 1.80") == "1Y 1.70 2Y 1.80"
    # ③ strip
    assert impl_norm("  hello \n") == "hello"
    # 不剥 U+0014、不改 [表情]/[图片]
    assert impl_norm("收到\u0014A") == "收到\u0014A"
    assert impl_norm("[表情][图片]") == "[表情][图片]"
    # 空值口径
    assert impl_norm(None) == "" and impl_norm("") == ""
    # 幂等
    for s in ("收到　ＯＫ", " a\n\nb ", "x\u0014y"):
        assert impl_norm(impl_norm(s)) == impl_norm(s)
    # 与规格原文函数体逐字一致
    for s in ("收到　ＯＫ", "1Y\n1.70", "  a  b  ", None, "", "\u0014"):
        assert impl_norm(s) == spec_norm(s)


def test_T02_fingerprint_matches_spec_formula(store):
    """06 §2.9.2:fingerprint = sha256(account_id | session_id | sender_id_or_name | norm(text) | media_sha256s | ts 取整到秒);
    02 §2.8.1「所有通道另算一份 fingerprint 存列」;00 §7.4 Message.fingerprint。无媒体、sender_id 给定的情形按字面公式比对。"""
    ts = T0 + 1234                                        # 取整到秒 = T0 // 1000 + 1
    r = store.ingest(qd_in("  收到　ＯＫ \n", ts, ext="qd:1", sender_id=PEER))
    assert r.inserted is True
    row = store.get_message(r.id)
    assert row["fingerprint"] == spec_fingerprint(QD, QD_SESSION, PEER, "  收到　ＯＫ \n", ts)


def test_T03_fingerprint_properties(store):
    """06 §2.9.2:同一秒同一人 norm(text) 相等 ⇒ 指纹相同(截图路径固有限制,宁少一条);跨秒 ⇒ 不同;
    sender_id_or_name = 有 id 用 id、无 id 才用名。"""
    a = store.ingest(qd_in("hello   world", T0 + 100, ext="qd:1", sender_id=PEER))
    b = store.ingest(qd_in("hello world", T0 + 900, ext="qd:2", sender_id=PEER))      # 同一秒、只差空白
    c = store.ingest(qd_in("hello world", T0 + 1100, ext="qd:3", sender_id=PEER))     # 下一秒
    fa, fb, fc = (store.get_message(x.id)["fingerprint"] for x in (a, b, c))
    assert fa == fb
    assert fa != fc
    assert len(fa) == 64 and re.fullmatch(r"[0-9a-f]{64}", fa), "full sha256、无前缀(99c C-03:'fp:' 前缀已作废)"
    # 无 sender_id 时取 sender_name
    d = store.ingest(qd_in("x", T0 + 5000, ext="qd:4", sender_id=None, sender_name="张三"))
    assert store.get_message(d.id)["fingerprint"] == spec_fingerprint(QD, QD_SESSION, "张三", "x", T0 + 5000)


# ══════════════════════════════════════════════════════════════════════ 二、ingest 顺序与 sessions(02 §2.2.8 / §2.8.1 R4-7)


def test_T04_new_session_first_message_upserts_session_then_message(store):
    """02 §2.8.1「入库顺序:先 upsert sessions,再写 messages(R4-7,同一事务)」;upsert 写 name / last_msg_ms=max(…) / msg_count+…;
    漏了第一步 = 新会话第一条消息 FOREIGN KEY constraint failed。"""
    assert count(store, "SELECT COUNT(*) FROM sessions WHERE id=?", QD_SESSION) == 0
    r1 = store.ingest(qd_in("第一条", T0 + 1000, ext="qd:1", name="张三"))       # 全新会话:不炸
    assert r1.inserted
    s = one(store, "SELECT account_id, native_id, kind, name, msg_count, last_msg_ms FROM sessions WHERE id=?", QD_SESSION)
    assert s == (QD, PEER, "private", "张三", 1, T0 + 1000)
    r2 = store.ingest(qd_in("第二条", T0 + 5000, ext="qd:2", name="张三改名"))
    assert r2.inserted
    assert one(store, "SELECT name, msg_count, last_msg_ms FROM sessions WHERE id=?", QD_SESSION) == ("张三改名", 2, T0 + 5000)
    # 事后补进来的更早的行(06 §2.9.5:主库里大量「_id 靠后但 time 更早」的行):last_msg_ms = max(…) 不倒退
    r3 = store.ingest(qd_in("补进来的旧行", T0 + 2000, ext="qd:3"))
    assert r3.inserted
    assert one(store, "SELECT msg_count, last_msg_ms FROM sessions WHERE id=?", QD_SESSION) == (3, T0 + 5000)


def test_T05_outbound_first_send_to_new_session_does_not_break_fk(store):
    """02 §2.8.1 R6-16 / 02 §2.2.2 C-21:出向与入向共用 store.ingest、同一事务顺序——「我方主动向一个还没收过消息的新会话发第一条」
    不得炸外键;出向 SENDING 行:dir=out、state=SENDING、ext_msg_id NULL、带 idempotency_key/trace_id、is_self=1。"""
    r = store.ingest(qd_out("你好", T0 + 1000, key="k1", trace="TR1", peer="999000111"))
    assert r.inserted
    assert count(store, "SELECT COUNT(*) FROM sessions WHERE id=?", f"{QD}:999000111") == 1
    row = store.get_message(r.id)
    assert (row["dir"], row["state"], row["ext_msg_id"], row["idempotency_key"], row["trace_id"], row["is_self"]) == \
        ("out", "SENDING", None, "k1", "TR1", 1)
    assert row["fingerprint"] is not None, "出向行也算 fingerprint(02 §2.8.1:供出向确认匹配)"


def test_T06_rescan_same_key_returns_false_false_and_no_new_row(store):
    """02 §2.2.8 R6-40/R6-41:撞上去重键、内容无变化的已存在行 ⇒ (False, False)(调用方据此不发事件,否则全表重扫会重放事件)。"""
    r1 = store.ingest(qd_in("a", T0 + 1000, ext="qd:1"))
    r2 = store.ingest(qd_in("a", T0 + 1000, ext="qd:1"))
    assert (r1.inserted, r1.changed) == (True, False)
    assert (r2.inserted, r2.changed) == (False, False)
    assert r2.id == r1.id
    assert store.count_messages(QD) == 1


def test_T07_rescan_same_key_does_not_inflate_session_counter(store):
    """02 §2.8.1 upsert 写 msg_count=msg_count+…;06 §2.9.5 ④ 水位自检会全表重扫——重扫撞键的行不是新消息,
    会话计数不该随重扫漂移(规格未逐字写 +0,这是按「msg_count 是消息条数」语义的推断,失败时列为规格歧义)。"""
    store.ingest(qd_in("a", T0 + 1000, ext="qd:1"))
    store.ingest(qd_in("a", T0 + 1000, ext="qd:1"))
    store.ingest(qd_in("a", T0 + 1000, ext="qd:1"))
    assert one(store, "SELECT msg_count FROM sessions WHERE id=?", QD_SESSION) == (1,)


def test_T08_revoke_flip_is_changed_and_row_kept(store):
    """02 §2.2.8:changed = 撤回标记翻转;02 §2.8.1 幂等更新「撤回态可更新、撤回标记不删」;DDL:revoked_ms/revoked_by 撤回标记不删。"""
    r1 = store.ingest(qd_in("要撤回的", T0 + 1000, ext="qd:7"))
    r2 = store.ingest(qd_in("要撤回的", T0 + 1000, ext="qd:7", revoked=True, revoked_ms=T0 + 9000))
    assert (r2.inserted, r2.changed, r2.id) == (False, True, r1.id)
    row = store.get_message(r1.id)
    assert (row["revoked"], row["revoked_ms"]) == (1, T0 + 9000)
    assert row["text"] == "要撤回的", "标记不删:正文仍在"
    assert store.count_messages(QD) == 1
    # 再重扫同样的撤回态:无变化 ⇒ (False, False)
    r3 = store.ingest(qd_in("要撤回的", T0 + 1000, ext="qd:7", revoked=True, revoked_ms=T0 + 9000))
    assert (r3.inserted, r3.changed) == (False, False)


def test_T09_inbound_state_is_always_delivered(store):
    """06 §2.12 末条「messages.state 只对 dir=out 有意义;in 行恒空」+ 02 §3.1 CHECK (dir='out' OR state='DELIVERED')、00 §7.4「入向恒 DELIVERED」。"""
    r = store.ingest(qd_in("in", T0 + 1000, ext="qd:1"))
    assert store.get_message(r.id)["state"] == "DELIVERED"


# ══════════════════════════════════════════════════════════════════════ 三、QQ message_id 复用(02 §2.8.1 / 06 §2.9.2 / 06 §8b M2.5)


def test_T10_qq_id_reuse_over_1h_is_new_message_with_suffix(store):
    """06 §2.9.2 QQ:同键命中还要比 ts,|ts 差| > 1 小时视为 ID 复用、当新消息入——新行 ext_msg_id = 原 ext + "#2",第 n 次复用 "#n";
    02 §2.8.1:QQ 不走 ON CONFLICT、先 SELECT 再插;06 §8b M2.5:两行都在,第二行以 #2 结尾。"""
    H = 3600 * 1000
    r1 = store.ingest(qq_in("第一次", T0, ext="g_456:1001"))
    r2 = store.ingest(qq_in("重启后复用", T0 + 2 * H, ext="g_456:1001"))
    r3 = store.ingest(qq_in("再复用", T0 + 4 * H, ext="g_456:1001"))
    assert r1.inserted and r2.inserted and r3.inserted
    assert len({r1.id, r2.id, r3.id}) == 3
    assert store.count_messages("qq03") == 3
    exts = sorted(x[0] for x in rows(store, "SELECT ext_msg_id FROM messages WHERE account_id='qq03'"))
    assert exts == ["g_456:1001", "g_456:1001#2", "g_456:1001#3"]
    assert store.get_message(r1.id)["text"] == "第一次", "旧行不变"
    assert store.get_message(r2.id)["ext_msg_id"] == "g_456:1001#2"
    assert store.get_message(r3.id)["ext_msg_id"] == "g_456:1001#3"


def test_T11_qq_id_reuse_within_1h_is_same_message(store):
    """06 §2.9.2 QQ:≤ 1 小时视为同一条(更新 revoked/text 补齐);06 §8b M2.5:相差 10min 的同号只剩一行;02 §8b C-02 同。"""
    r1 = store.ingest(qq_in("同一条", T0, ext="g_456:2001"))
    r2 = store.ingest(qq_in("同一条", T0 + 10 * 60 * 1000, ext="g_456:2001"))
    assert (r2.inserted, r2.changed, r2.id) == (False, False, r1.id)
    assert store.count_messages("qq03") == 1
    # 撤回补齐仍走同一行
    r3 = store.ingest(qq_in("同一条", T0 + 30 * 60 * 1000, ext="g_456:2001", revoked=True, revoked_ms=T0 + 30 * 60 * 1000))
    assert (r3.inserted, r3.changed, r3.id) == (False, True, r1.id)
    assert store.count_messages("qq03") == 1
    assert store.get_message(r1.id)["ext_msg_id"] == "g_456:2001"


# ══════════════════════════════════════════════════════════════════════ 四、出向 SENDING 行(06 §2.12 / 02 §2.8.1 / 06 §2.9.2 部分索引)


def test_T12_sending_rows_with_null_ext_do_not_collide_on_partial_unique_index(store):
    """06 §2.9.2 唯一键 UNIQUE (account_id, ext_msg_id) WHERE ext_msg_id IS NOT NULL——出向 SENDING 行 ext 为空,不进索引;
    02 §2.8.1 出向行「不靠唯一键(ext 为空时不进部分索引)」。两条不同 key 的 SENDING 行可并存。"""
    a = store.ingest(qd_out("a", T0 + 1000, key="k1", trace="T1"))
    b = store.ingest(qd_out("b", T0 + 2000, key="k2", trace="T2"))
    assert a.inserted and b.inserted and a.id != b.id
    assert count(store, "SELECT COUNT(*) FROM messages WHERE dir='out' AND state='SENDING' AND ext_msg_id IS NULL") == 2


def test_T13_idempotent_retry_reuses_same_sending_row(store):
    """06 §2.12「幂等重试命中同 idempotency_key 直接复用这一行」;02 §2.2.2「幂等重试与 confirm_probe 都先按这一行判」。"""
    a = store.ingest(qd_out("a", T0 + 1000, key="k1", trace="T1"))
    b = store.ingest(qd_out("a", T0 + 8000, key="k1", trace="T1"))
    assert a.inserted is True
    assert (b.inserted, b.id) == (False, a.id)
    assert count(store, "SELECT COUNT(*) FROM messages WHERE idempotency_key='k1'") == 1


# ══════════════════════════════════════════════════════════════════════ 五、入向轮询撞到自己发的(06 §2.12 / 02 §2.8.1)


def test_T14_merge_by_same_fingerprint(store):
    """06 §2.12:入库前若存在 dir=out 且 state ∈ (SENDING, UNCONFIRMED) 的行,fingerprint 相同 ⇒ 合并进那一行(补 ext、置 DELIVERED),不插第二行;
    02 §2.2.8 R6-41:并进我方出向行 ⇒ changed=True;06 §2.12 表:企点读库正线 confirmed_by=ingest_merge、ext_msg_id=qd:{uniseq}。"""
    out = store.ingest(qd_out("收到", T0 + 1000, key="k1", trace="T1"))
    back = store.ingest(qd_self_in("收到", T0 + 1500, ext="qd:88"))            # 同秒、同人、同文本 ⇒ fingerprint 相同
    assert (back.inserted, back.changed, back.id) == (False, True, out.id)
    row = store.get_message(out.id)
    assert (row["state"], row["ext_msg_id"], row["confirmed_by"]) == ("DELIVERED", "qd:88", "ingest_merge")
    assert row["confirmed_ms"] is not None
    assert row["trace_id"] == "T1" and row["idempotency_key"] == "k1", "合并进出向行:它的 trace/key 保留"
    assert store.count_messages(QD) == 1


def test_T15_merge_by_same_session_norm_text_within_60s(store):
    """06 §2.12:(同 session_id + norm(text) 相等 + |ts 差| ≤ 60s)⇒ 合并;R6-47 两侧都过 norm()(NFKC + 折叠空白 + strip)。
    ts 差 30 s ⇒ fingerprint 不同,只能走这一支。"""
    out = store.ingest(qd_out("1Y\n1.70", T0 + 1000, key="k1", trace="T1"))
    back = store.ingest(qd_self_in("1Y 1.70", T0 + 31_000, ext="qd:89"))        # 读回是多行文本折叠后相等
    assert (back.inserted, back.changed, back.id) == (False, True, out.id)
    row = store.get_message(out.id)
    assert (row["state"], row["ext_msg_id"], row["confirmed_by"]) == ("DELIVERED", "qd:89", "ingest_merge")
    assert row["text"] == "1Y\n1.70", "出向行 text = 发送原文,合并不改写(R6-48)"
    assert store.count_messages(QD) == 1


def test_T16_no_merge_outside_60s_window(store):
    """06 §2.12:|ts 差| > 60s(且 fingerprint 不同)⇒ 都不命中,按普通行入库;出向行保持 SENDING。"""
    out = store.ingest(qd_out("窗外", T0 + 1000, key="k1", trace="T1"))
    back = store.ingest(qd_self_in("窗外", T0 + 62_000, ext="qd:90"))
    assert back.inserted is True and back.id != out.id
    assert store.count_messages(QD) == 2
    assert store.get_message(out.id)["state"] == "SENDING"


def test_T17_unconfirmed_row_is_also_merged(store):
    """06 §2.12 表(企点):超时 UNCONFIRMED 后 poll 拉到仍照常合并,把 UNCONFIRMED 翻成 DELIVERED;02 §2.8.1 state IN ('SENDING','UNCONFIRMED')。"""
    out = store.ingest(qd_out("迟到确认", T0 + 1000, key="k1", trace="T1"))
    store.mark_out_state(out.id, "UNCONFIRMED")
    assert store.get_message(out.id)["state"] == "UNCONFIRMED"
    back = store.ingest(qd_self_in("迟到确认", T0 + 20_000, ext="qd:91"))
    assert (back.inserted, back.changed, back.id) == (False, True, out.id)
    assert (store.get_message(out.id)["state"], store.get_message(out.id)["ext_msg_id"]) == ("DELIVERED", "qd:91")
    assert store.count_messages(QD) == 1


def test_T18_empty_text_guard_when_capture_text_off(clock):
    """06 §2.12 R6-44 空文本守卫:capture_text=false 时入库不写 text,「同 session + norm(text) 相等 + 窗内」这一支不适用,此时只认 fingerprint 相同;
    02 §2.8.5:关闭时 text=NULL 而 fingerprint 仍写。"""
    s = Store(":memory:", clock=clock, capture_text=False).open()
    try:
        s.ensure_account(QD, "qidian", state="running", self_uid=SELF_UID)
        out = s.ingest(qd_out("正文", T0 + 1000, key="k1", trace="T1"))
        assert s.get_message(out.id)["text"] is None
        # 跨秒 ⇒ fingerprint 不同;两侧 text 都空 ⇒ norm 支不适用 ⇒ 不合并
        back = s.ingest(qd_self_in("正文", T0 + 5000, ext="qd:1"))
        assert back.inserted is True and back.id != out.id
        assert s.count_messages(QD) == 2
        assert s.get_message(out.id)["state"] == "SENDING"
        # 同秒同人同文本 ⇒ fingerprint 相同 ⇒ 仍能合并
        out2 = s.ingest(qd_out("第二条", T0 + 20_000, key="k2", trace="T2"))
        back2 = s.ingest(qd_self_in("第二条", T0 + 20_500, ext="qd:2"))
        assert (back2.inserted, back2.changed, back2.id) == (False, True, out2.id)
        assert s.get_message(out2.id)["state"] == "DELIVERED"
    finally:
        s.close()


def test_T19_multiple_candidates_pick_smallest_ts_diff(store):
    """06 §2.12 R6-39 多候选定序:同时有多行出向行命中时,取 |读到的行 time − 出向行 ts| 最小的一行。"""
    o1 = store.ingest(qd_out("同文本", T0 + 1000, key="k1", trace="T1"))
    o2 = store.ingest(qd_out("同文本", T0 + 21_000, key="k2", trace="T2"))
    back = store.ingest(qd_self_in("同文本", T0 + 23_000, ext="qd:5"))          # 与 o2 差 2s、与 o1 差 22s
    assert (back.inserted, back.changed, back.id) == (False, True, o2.id)
    assert store.get_message(o2.id)["state"] == "DELIVERED"
    assert store.get_message(o1.id)["state"] == "SENDING"
    assert store.count_messages(QD) == 2


def test_T20_multiple_candidates_tie_picks_earlier_written(store):
    """06 §2.12 R6-39:并列取更早写入的一行。"""
    o1 = store.ingest(qd_out("并列", T0 + 1000, key="k1", trace="T1"))         # 先写入
    o2 = store.ingest(qd_out("并列", T0 + 21_000, key="k2", trace="T2"))
    back = store.ingest(qd_self_in("并列", T0 + 11_000, ext="qd:6"))            # 与两者都差 10s
    assert (back.inserted, back.changed, back.id) == (False, True, o1.id)
    assert store.get_message(o1.id)["state"] == "DELIVERED"
    assert store.get_message(o2.id)["state"] == "SENDING"


def test_T21_one_read_row_merges_only_once(store):
    """06 §2.12 R6-39:一条读到的行只合并一次(合并即写 ext_msg_id,之后再撞按唯一键幂等跳过);B-07 通过判据「之后拉回该条不新增行」。"""
    o1 = store.ingest(qd_out("只并一次", T0 + 1000, key="k1", trace="T1"))
    o2 = store.ingest(qd_out("只并一次", T0 + 2000, key="k2", trace="T2"))
    first = store.ingest(qd_self_in("只并一次", T0 + 1500, ext="qd:7"))
    assert (first.inserted, first.changed, first.id) == (False, True, o1.id)
    again = store.ingest(qd_self_in("只并一次", T0 + 1500, ext="qd:7"))          # 全表重扫又拉到同一行
    assert (again.inserted, again.changed, again.id) == (False, False, o1.id)
    assert store.get_message(o2.id)["state"] == "SENDING", "第二条出向行不能被同一读到的行再并一次"
    assert store.count_messages(QD) == 2


def test_T22_failed_row_does_not_participate_in_merge(store):
    """06 §2.12:合并只看 state ∈ (SENDING, UNCONFIRMED);06 §2.9.5 掉线续读条「FAILED 行不参与 §2.12 合并」⇒ 读回行另插一行、FAILED 行不变。"""
    out = store.ingest(qd_out("失败的", T0 + 1000, key="k1", trace="T1"))
    store.mark_out_state(out.id, "FAILED")
    back = store.ingest(qd_self_in("失败的", T0 + 1500, ext="qd:8"))
    assert back.inserted is True and back.id != out.id
    assert store.get_message(out.id)["state"] == "FAILED"
    assert store.count_messages(QD) == 2


def test_T23_different_session_never_merges(store):
    """06 §2.12:norm 支要求「同 session_id」;fingerprint 含 session_id ⇒ 另一会话读回的同文本不合并。"""
    out = store.ingest(qd_out("同文本", T0 + 1000, key="k1", trace="T1", peer=PEER))
    back = store.ingest(qd_self_in("同文本", T0 + 1500, ext="qd:9", peer="415011448"))
    assert back.inserted is True and back.id != out.id
    assert store.get_message(out.id)["state"] == "SENDING"


# ══════════════════════════════════════════════════════════════════════ 六、游标与同事务(02 §2.2.8 C-24 / R4-7 / R6-41、B-19)


def test_T24_empty_batch_only_advances_cursor(store):
    """02 §2.2.8 R6-41:msgs 为空而 cursor_update 非空时,本事务只推游标(不 upsert sessions、不写 messages),不得早退。"""
    res = store.ingest_batch([], CursorUpdate(QD, f"qidian_rowid:{PEER}", 42, json.dumps({"last_uniseq": 9})))
    assert res == []
    cur = store.cursor_get(QD, f"qidian_rowid:{PEER}")
    assert cur is not None and cur.value_int == 42 and cur.value_json() == {"last_uniseq": 9}
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0
    assert count(store, "SELECT COUNT(*) FROM messages") == 0


def test_T25_B19_messages_and_cursor_in_one_transaction(store, monkeypatch):
    """02 §8b B-19 + §2.2.8:在 ingest_batch 写消息后、写游标前注入异常 ⇒ 事务回滚,「会话没建、消息没进、游标没动」;重拉后消息只入一次。"""
    def boom(*a, **k):
        raise RuntimeError("注入:推游标失败")
    monkeypatch.setattr(store, "_cursor_set", boom)
    msg = qd_in("边界消息", T0 + 1000, ext="qd:1")
    with pytest.raises(RuntimeError):
        store.ingest_batch([msg], CursorUpdate(QD, f"qidian_rowid:{PEER}", 1, None))
    assert count(store, "SELECT COUNT(*) FROM messages") == 0, "消息没进"
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0, "会话没建"
    assert store.cursor_get(QD, f"qidian_rowid:{PEER}") is None, "游标没动"
    monkeypatch.undo()
    # 重拉重放:靠唯一键幂等,只入一次
    res = store.ingest_batch([qd_in("边界消息", T0 + 1000, ext="qd:1")], CursorUpdate(QD, f"qidian_rowid:{PEER}", 1, None))
    assert [(i, c) for i, c, _ in res] == [(True, False)]
    assert count(store, "SELECT COUNT(*) FROM messages") == 1
    assert store.cursor_get(QD, f"qidian_rowid:{PEER}").value_int == 1


def test_T26_batch_writes_session_messages_cursor_together(store):
    """02 §2.2.8:ingest_batch 在一个事务里先 upsert sessions、再写 messages、再推 cursors;逐行返回 (inserted, changed, msg)。"""
    res = store.ingest_batch([qd_in("a", T0 + 1000, ext="qd:1"), qd_in("b", T0 + 2000, ext="qd:2"), qd_in("a", T0 + 1000, ext="qd:1")],
                             CursorUpdate(QD, f"qidian_rowid:{PEER}", 3, json.dumps({"last_uniseq": 2})))
    assert [(i, c) for i, c, _ in res] == [(True, False), (True, False), (False, False)]
    assert all(m.id for _, _, m in res)
    assert one(store, "SELECT msg_count, last_msg_ms FROM sessions WHERE id=?", QD_SESSION) == (2, T0 + 2000)
    assert store.cursor_get(QD, f"qidian_rowid:{PEER}").value_int == 3


# ══════════════════════════════════════════════════════════════════════ 七、capture_text(02 §2.8.5 / 06 §2.13)


def test_T27_capture_text_off_keeps_len_hash_fingerprint(clock):
    """02 §2.8.5:capture_text 关闭时 messages.text=NULL,text_hash / text_len / fingerprint 仍写(去重要用);DDL:text_hash = sha256(text)、text_len = 字符数。"""
    s = Store(":memory:", clock=clock, capture_text=False).open()
    try:
        s.ensure_account(QD, "qidian", state="running", self_uid=SELF_UID)
        text = "收到　ＯＫ👌"
        r = s.ingest(qd_in(text, T0 + 1000, ext="qd:1", sender_id=PEER))
        row = s.get_message(r.id)
        assert row["text"] is None
        assert row["text_len"] == len(text)
        assert row["text_hash"] == sha256_hex(text)
        assert row["fingerprint"] == spec_fingerprint(QD, QD_SESSION, PEER, text, T0 + 1000)
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════ 八、bus 参数校验(02 §3.10 R6-48 / 06 §2.12 / B-40)


def test_T28_validate_send_text_control_chars():
    """02 §3.10 R6-48 / 06 §2.12:send_text.text 须满足 clean_text(text)==text——不含 U+0014、不含 \\t\\n\\r 以外任何 < U+0020 的字符;
    违例 INVALID_ARGS、error.reason='text_has_control_chars'、error.details[0].pointer='/text';放行 \\t\\n\\r、全角、Unicode emoji。"""
    for bad in ("收到\u0014A", "收到\u0008", "x\u001fy", "\u0014", "a\u0000b"):
        err = validate_send_text({"session": QD_SESSION, "text": bad})
        assert err is not None, repr(bad)
        assert err.reason == "text_has_control_chars"
        assert err.details and err.details[0]["pointer"] == "/text"
        assert err.retryable is False
    for ok in ("1Y\n1.70", "a\tb", "a\r\nb", "收到👌", "收到　ＯＫ", "[表情]"):
        assert validate_send_text({"session": QD_SESSION, "text": ok}) is None, repr(ok)


# ══════════════════════════════════════════════════════════════════════ 企点读库正线的 bus 夹具


class QidianRig:
    """bus + 企点适配器 + 假主库。``sender`` 是「RPA 点发送键」的执行层:点完立即返回;另起 task 模拟企点 ~9 s 后把我方消息落进主库。"""

    def __init__(self, store, clock, maindb):
        self.store, self.clock, self.maindb = store, clock, maindb
        self.cfg = AgentConfig(bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0),
                               qidian=QidianAdapterConfig(confirm_poll_interval_ms=10))
        self.events = Events(store)
        self.alerts = Alerts(self.events, clock=clock)
        self.poller = QidianPoller(store=store, events=self.events, alerts=self.alerts, cfg=self.cfg, h13_firing=lambda: False,
                                   clock=clock, maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path))
        self.adapter = QidianAdapter(self.poller, sender=self._sender, store=store)
        self.bus = Bus(store=store, events=self.events, adapters={"qidian": self.adapter}, cfg=self.cfg, clock=clock)
        self.sent: list[tuple[str, str]] = []      # 通道里实际出现的消息(点了几次发送键)
        self.land = True                           # 假企点是否会把我方消息落进主库(False = 模拟读回断掉,B-08)
        self.land_delay_ms = 9000
        self.on_send = None                        # 点发送键时的钩子(抓 B-07 中间态)

    def view(self):
        return QidianAccountView(QD, "running", SELF_UID)

    def account(self):
        return Account(id=QD, channel="qidian", state="running", self_uid=SELF_UID)

    def bootstrap(self):
        """起 bus 前跑一轮全量:会话表须已存在,首轮建 qidian_bootstrap 与表映射(06 §2.9.5)。"""
        self.maindb.insert_text(PEER, "历史", time_s=self.clock.now_s - 3600)
        self.poller.poll_maindb(self.view())

    async def _sender(self, acct, native_id, text):
        self.sent.append((native_id, text))
        if self.on_send:
            self.on_send()
        if self.land and "@slow@" not in text:
            async def later():
                await asyncio.sleep(0)
                self.clock.advance(self.land_delay_ms)
                self.maindb.insert_text(native_id, text, time_s=self.clock.now_s, issend=1)
            asyncio.create_task(later())
        return True

    def full_poll(self):
        self.poller.poll_maindb(self.view())


@pytest.fixture
async def rig(store, clock, maindb):
    clock.auto_step_ms = 200         # 起 bus 的用例:可拨时钟每次读取自动前进,确认窗才到得了 deadline
    r = QidianRig(store, clock, maindb)
    r.bootstrap()
    yield r
    await r.bus.close()


def send_cmd(text, key=None, *, session=QD_SESSION, account_id=QD, **kw):
    return Command(account_id=account_id, op="send_text", args={"session": session, "text": text}, idempotency_key=key, **kw)


async def submit(rig, cmd, timeout=ASYNC_TIMEOUT):
    return await asyncio.wait_for(rig.bus.submit(cmd), timeout)


def out_row_by_key(store, key):
    return rows(store, "SELECT state, ext_msg_id, confirmed_by, text, id, trace_id FROM messages WHERE idempotency_key=?", key)


# ══════════════════════════════════════════════════════════════════════ 九、bus:B-40 / 06 §8b M2「含表情/控制字符的发送」


async def test_T29_B40_control_chars_rejected_at_entry_newline_passes(rig):
    """02 §8b B-40 / 02 §3.10 R6-48 / 06 §2.12:前两次 INVALID_ARGS + reason + pointer;messages 无新 SENDING 行、commands 无该行、idempotency 未占键;
    第三次 "1Y\\n1.70" 放行 DELIVERED;commands 里 k10 恰 1 行、idempotency k10 一行、messages 里 k10 只有一行且 text 含换行原文。"""
    store = rig.store
    for bad in ("收到\u0014A", "收到\u0008"):
        res = await submit(rig, send_cmd(bad, "k10"))
        assert res.ok is False and res.code == "INVALID_ARGS"
        assert res.error is not None and res.error.reason == "text_has_control_chars"
        assert res.error.details[0]["pointer"] == "/text"
    assert count(store, "SELECT COUNT(*) FROM messages WHERE dir='out'") == 0, "未写 SENDING 行"
    assert count(store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='k10'") == 0, "未进总线"
    assert store.idem_get(QD, "k10") is None, "未占幂等键"
    assert rig.sent == []

    res3 = await submit(rig, send_cmd("1Y\n1.70", "k10"))
    assert res3.code == "DELIVERED" and res3.ok is True
    assert count(store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='k10'") == 1
    assert one(store, "SELECT status, result_code FROM idempotency WHERE idem_key='k10'") == ("DONE", "DELIVERED")
    r = out_row_by_key(store, "k10")
    assert len(r) == 1
    assert r[0][3] == "1Y\n1.70", "出向行 text 逐字保留换行(不过 clean_text、不归一化)"
    assert r[0][0] == "DELIVERED" and r[0][1].startswith("qd:") and r[0][2] == "ingest_merge"
    assert len(rig.sent) == 1


async def test_T34_emoji_and_fullwidth_delivered_text_verbatim(rig):
    """06 §8b M2「含表情/控制字符的发送」④⑤:Unicode emoji "收到👌" 与全角 "收到　ＯＫ" 各在 confirm_timeout_qidian_ms 内 DELIVERED,
    行 ext_msg_id=qd:<uniseq>、confirmed_by=ingest_merge、messages.text 与发出的逐字相同、读回没有插第二行。"""
    store = rig.store
    for i, text in enumerate(("收到👌", "收到　ＯＫ")):
        key = f"emo{i}"
        res = await submit(rig, send_cmd(text, key))
        assert res.code == "DELIVERED", text
        r = out_row_by_key(store, key)
        assert len(r) == 1
        assert r[0][3] == text
        assert r[0][0] == "DELIVERED" and r[0][1].startswith("qd:") and r[0][2] == "ingest_merge"
    assert store.count_messages(QD) == 2, "两条各一行,读回没插第二行"


# ══════════════════════════════════════════════════════════════════════ 十、bus:B-07 / B-06 / B-08 / 幂等 SENDING 命中


async def test_T30_B07_outbound_row_sending_then_delivered_same_row(rig):
    """02 §8b B-07:发送中一行 SENDING,NULL,NULL;完成后同一行 DELIVERED,qd:<uniseq>,ingest_merge,行数恒 1;
    06 §2.12 表:CommandResult.source = qidian_db。"""
    store = rig.store
    mid: list = []
    rig.on_send = lambda: mid.append([tuple(x) for x in rows(store, "SELECT state, ext_msg_id, confirmed_by FROM messages WHERE idempotency_key='k9'")])
    res = await submit(rig, send_cmd("a", "k9"))
    assert mid == [[("SENDING", None, None)]], "点发送键时(调适配器之前)已有且只有一行 SENDING,NULL,NULL"
    assert res.ok is True and res.code == "DELIVERED"
    assert res.source == "qidian_db"
    after = rows(store, "SELECT state, ext_msg_id, confirmed_by, id FROM messages WHERE idempotency_key='k9'")
    assert len(after) == 1
    assert (after[0][0], after[0][2]) == ("DELIVERED", "ingest_merge")
    assert re.fullmatch(r"qd:\d+", after[0][1])
    # 「之后拉回该条不新增行」:再跑一轮全量 poll
    rig.full_poll()
    assert store.count_messages(QD) == 1
    # 00 §8.3:DELIVERED 是成功码;01 §7.3:trace_id 回填
    assert res.trace_id


async def test_T31_B06_idempotency_three_states_and_args_mismatch(rig):
    """02 §8b B-06 / 02 §2.2.2 幂等三态 / 00 §8.3:第 1 次 DELIVERED;第 2 次同 body IDEMPOTENT_REPLAY 且 data 逐字相同;
    第 3 次同 key 不同 text INVALID_ARGS(不执行);idempotency 表 k9 = DONE,DELIVERED 一行;通道里只出现一条消息。"""
    store = rig.store
    r1 = await submit(rig, send_cmd("a", "k9"))
    assert r1.code == "DELIVERED"
    r2 = await submit(rig, send_cmd("a", "k9"))
    assert r2.code == "IDEMPOTENT_REPLAY"
    assert r2.data == r1.data, "返回上次结果,data 逐字相同"
    r3 = await submit(rig, send_cmd("b", "k9"))
    assert r3.ok is False and r3.code == "INVALID_ARGS"
    assert rows(store, "SELECT status, result_code FROM idempotency WHERE idem_key='k9'") == [("DONE", "DELIVERED")]
    assert len(rig.sent) == 1, "通道里只出现一条消息"
    assert count(store, "SELECT COUNT(*) FROM messages WHERE dir='out'") == 1


async def test_T32_idempotent_sending_hit_waits_and_does_not_resend(rig):
    """02 §2.2.2 幂等三态:命中 SENDING ⇒ 等待该 trace 完成再返回同一结果、不再真发。并发提交同键同参两条。"""
    store = rig.store
    ra, rb = await asyncio.wait_for(asyncio.gather(rig.bus.submit(send_cmd("并发", "kc")), rig.bus.submit(send_cmd("并发", "kc"))), ASYNC_TIMEOUT)
    codes = sorted([ra.code, rb.code])
    assert codes == ["DELIVERED", "IDEMPOTENT_REPLAY"], codes
    assert ra.data == rb.data and ra.trace_id == rb.trace_id, "同一结果"
    assert len(rig.sent) == 1, "不再真发"
    assert count(store, "SELECT COUNT(*) FROM messages WHERE dir='out'") == 1
    assert one(store, "SELECT status FROM idempotency WHERE idem_key='kc'") == ("DONE",)


async def test_T33_B08_timeout_unconfirmed_then_probe_replays(rig):
    """02 §8b B-08 + 06 §2.12 + 00 §8.3:读回断掉 ⇒ 第一次 SEND_CALLED_BUT_UNCONFIRMED(retryable=false、needs_human=false)、
    行 state=UNCONFIRMED、idempotency.status=SENDING;恢复读回后 poll 拉到把 UNCONFIRMED 翻成 DELIVERED;
    同 key 重发先 confirm_probe 查到已发 ⇒ IDEMPOTENT_REPLAY 不重发;通道里只有一条;command_results.confirmed_by 由 probe 回填。"""
    store = rig.store
    rig.land = False                                          # 模拟企点主库始终没落我方消息
    r1 = await submit(rig, send_cmd("待核", "k8"))
    assert r1.ok is False and r1.code == "SEND_CALLED_BUT_UNCONFIRMED"
    assert r1.error is not None and r1.error.retryable is False and r1.error.needs_human is False
    r = out_row_by_key(store, "k8")
    assert len(r) == 1 and r[0][0] == "UNCONFIRMED" and r[0][1] is None
    assert one(store, "SELECT status FROM idempotency WHERE idem_key='k8'") == ("SENDING",)
    assert len(rig.sent) == 1

    # 读回恢复:企点这才把我方消息落进主库;poll 拉到 ⇒ 被动合并,UNCONFIRMED → DELIVERED(06 §2.12 R6-39 澄清)
    ts_ms = one(store, "SELECT ts_ms FROM messages WHERE idempotency_key='k8'")[0]
    rig.maindb.insert_text(PEER, "待核", time_s=ts_ms // 1000 + 10, issend=1)
    rig.full_poll()
    r = out_row_by_key(store, "k8")
    assert len(r) == 1 and r[0][0] == "DELIVERED" and r[0][2] == "ingest_merge" and re.fullmatch(r"qd:\d+", r[0][1])

    r2 = await submit(rig, send_cmd("待核", "k8"))
    assert r2.code == "IDEMPOTENT_REPLAY", "confirm_probe 查到已发 ⇒ 不重发"
    assert len(rig.sent) == 1, "通道里只有一条"
    assert count(store, "SELECT COUNT(*) FROM messages WHERE dir='out'") == 1
    assert one(store, "SELECT status FROM idempotency WHERE idem_key='k8'") == ("DONE",)
    cr = store.get_command_result(r1.trace_id)
    assert cr is not None and cr["confirmed_by"] is not None, "B-08 通过判据:command_results.confirmed_by 由 probe 回填"


# ══════════════════════════════════════════════════════════════════════ 十一、bus:事件 origin / args_json / R6-38 让出队列


async def test_T35_message_event_payload_origin_rpa_for_our_outbound(rig):
    """06 §2.9.5 掉线续读条 + 00 §7.4:message 事件 payload.origin ∈ {"rpa","external"}(仅出向行);本系统发出的出向行(带 trace_id)⇒ "rpa";
    06 §8b M2 ③④⑤:events_outbox 里该条 message 事件 payload.origin='rpa'。"""
    store = rig.store
    res = await submit(rig, send_cmd("事件", "ke"))
    assert res.code == "DELIVERED"
    msg_id = out_row_by_key(store, "ke")[0][4]
    evs = [e for e in store.list_events(event="message", account_id=QD) if e["payload"].get("id") == msg_id]
    assert len(evs) == 1, "合并(changed=True)发且只发一次 message 事件"
    p = evs[0]["payload"]
    assert p["origin"] == "rpa"
    assert p["dir"] == "out" and p["state"] == "DELIVERED" and p["ext_msg_id"].startswith("qd:")
    assert "lag_s" in p and "late" in p


async def test_T36_args_json_has_no_body_text(rig):
    """02 §2.2.2 P-11 / §2.8.5 / §3.1 commands.args_json 注释 / 02 §8b B-09 / 06 §2.13:args.text 入库前替换为 {"text_sha8","text_len"},无明文。"""
    store = rig.store
    text = "机密报价 1Y 1.70"
    res = await submit(rig, send_cmd(text, "kp"))
    assert res.code == "DELIVERED"
    raw = one(store, "SELECT args_json FROM commands WHERE trace_id=?", res.trace_id)[0]
    assert text not in raw
    args = json.loads(raw)
    assert "text" not in args
    assert args["text_sha8"] == sha256_hex(text)[:8]
    assert args["text_len"] == len(text)
    assert args["session"] == QD_SESSION, "非正文参数保留"
    # 审计不含正文(基线 §11.2 [NOLOG]):凡有行,detail_json 都不得含明文
    for a in store.list_audit():
        assert text not in (a.get("detail_json") or "")


async def test_T37_R638_send_yields_queue_next_command_does_not_wait(rig):
    """02 §2.2.3 qidian 条 R6-38 / 06 §2.12:send 点完发送键即返回并让出账号串行队列,bus 在队列外等确认;
    「等待期间不占账号 UI、同账号下一条指令照常执行」——第一条确认在途(读回断掉)时,第二条已 DELIVERED 返回。"""
    store = rig.store
    t1 = asyncio.create_task(rig.bus.submit(send_cmd("@slow@ 第一条", "s1")))    # 假企点不落库 ⇒ 确认窗要等到超时
    await asyncio.sleep(0.05)                                                     # 让第一条进入确认窗
    r2 = await submit(rig, send_cmd("第二条", "s2"))
    assert r2.code == "DELIVERED"
    assert not t1.done(), "第二条返回时第一条仍在确认窗内 ⇒ 第二条没有等它"
    r1 = await asyncio.wait_for(t1, ASYNC_TIMEOUT)
    assert r1.code == "SEND_CALLED_BUT_UNCONFIRMED"
    c1 = one(store, "SELECT started_ms, finished_ms FROM commands WHERE idempotency_key='s1'")
    c2 = one(store, "SELECT started_ms, finished_ms FROM commands WHERE idempotency_key='s2'")
    assert c2[0] is not None and c2[1] is not None and c1[1] is not None
    assert c2[0] < c1[1] and c2[1] < c1[1], "第二条在第一条收口之前就开始并结束"
    assert [t for _, t in rig.sent] == ["@slow@ 第一条", "第二条"]


# ══════════════════════════════════════════════════════════════════════ 十二、bus:登录门(02 §2.2.2 D-2 / 06 §2.12 ① / B-30 / 06 §8b M2.5)


async def test_T38_login_gate_rejects_send_text(rig):
    """02 §2.2.2 登录门:state ∈ {login_required, logging_in} 时 IM 写类返回 LOGIN_REQUIRED、error.needs_human=true、retryable=false,
    不排队、不写 idempotency;06 §2.12 ①:不写 SENDING 行(没调通道);06 §8b M2.5:立即返回、commands 无排队行;00 §8.3:LOGIN_REQUIRED needs_human=是。"""
    store = rig.store
    store.set_account_state(QD, "login_required", state_code="WAIT_SMS")
    res = await submit(rig, send_cmd("登录期间", "kl"))
    assert res.ok is False and res.code == "LOGIN_REQUIRED"
    assert res.error is not None and res.error.needs_human is True and res.error.retryable is False
    assert store.idem_get(QD, "kl") is None, "不写 idempotency"
    assert count(store, "SELECT COUNT(*) FROM messages WHERE dir='out'") == 0, "不写 SENDING 行"
    assert count(store, "SELECT COUNT(*) FROM commands WHERE status IN ('queued','running')") == 0, "不排队"
    assert count(store, "SELECT COUNT(*) FROM commands WHERE started_ms IS NOT NULL AND idempotency_key='kl'") == 0, "没进队列(started_ms 为空)"
    assert rig.sent == [], "没调通道"
    # 人登录后原键重发即可(不是 IDEMPOTENT_REPLAY)
    store.set_account_state(QD, "running")
    res2 = await submit(rig, send_cmd("登录期间", "kl"))
    assert res2.code == "DELIVERED"


async def test_T39_login_gate_allows_get_state(rig):
    """02 §2.2.2 登录门:按「操作对象是屏幕还是业务」分流——get_state 放行(不是 LOGIN_REQUIRED),进总线执行。"""
    store = rig.store
    store.set_account_state(QD, "login_required", state_code="WAIT_QRCODE")
    res = await submit(rig, Command(account_id=QD, op="get_state", args={}))
    assert res.code != "LOGIN_REQUIRED"
    assert count(store, "SELECT COUNT(*) FROM commands WHERE op='get_state'") == 1, "放行 = 进了总线"


async def test_T40_B30_login_gate_commands_row_failed_not_started(rig):
    """02 §8b B-30 字面判据:commands 有 send 行且 status='failed'、started_ms IS NULL(没进队列);idempotency 无该 key。
    ⚠️ 与 02 §2.2.2「不排队」/ 06 §8b M2.5「commands 无排队行」/ 06 §2.12 R6-48「不进 commands 与 400 同一层」的口径存在张力,见报告。"""
    store = rig.store
    store.set_account_state(QD, "login_required", state_code="WAIT_SMS")
    res = await submit(rig, send_cmd("登录期间", "kl"))
    assert res.code == "LOGIN_REQUIRED"
    r = rows(store, "SELECT status, started_ms FROM commands WHERE op='send_text' AND idempotency_key='kl'")
    assert r == [("failed", None)]
    assert store.idem_get(QD, "kl") is None


async def test_T41_login_gate_also_in_logging_in(rig):
    """02 §2.2.2:state ∈ {login_required, logging_in}(含 WAIT_QRCODE/WAIT_SMS/WAIT_SLIDER/WAIT_DEVICE_CONFIRM 一切验证环节)都拒 IM 写类。"""
    store = rig.store
    store.set_account_state(QD, "logging_in", state_code="WAIT_SLIDER")
    res = await submit(rig, send_cmd("滑块中", "ks"))
    assert res.code == "LOGIN_REQUIRED" and res.error.needs_human is True
    assert store.idem_get(QD, "ks") is None and rig.sent == []
