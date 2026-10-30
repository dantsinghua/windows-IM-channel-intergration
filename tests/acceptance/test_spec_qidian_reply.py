"""企点引用回复 reply(R6-116) —— 独立验收用例(只读规格撰写,不看实现函数体、不看开发者测试)。

规格出处(唯一判据):
- docs/06 §2.9.5 路由表 `-1049` 行(勘误:引用回复文本,正文解码不变);「🔴 引用对象 `reply`(R6-116…)」整条:
  读取(第 8 列 hex(extStr)、MainDbRow.extstr_hex 默认空串)、extStr 解码 ①~⑤、受限解析输入限制、实测类描述、字段映射表、
  失败语义、原消息确定性关联 ①~⑤;伪代码 ④ 的 SELECT 第 8 列与 to_message 注释。
- docs/00 §7.4 `reply` 段(键恒在、九个取值键、ID 一律字符串、ts ISO +08:00、QQ/微信恒 null、不进 text/fingerprint…)与 §15g R6-116 行。
- docs/02 §2.8.1「reply 的写入时机」①②③、§3.3 `0003_message_reply.sql`、§3.1 块外说明、#48(GET /messages 带 reply)、§3.4.7 事件 payload.message.reply。
- 总控 2026-10-28 裁定的两处补充口径(规格正文尚未写入):
  ①企点会话表缺 `extStr` 列时,该表按无引用处理、正文照常产出、水位照常;②capture_text=false 时 reply.text 入库为 null。

测试数据一律合成:按 06 §2.9.5「实测类描述」(serialVersionUID=1、flags 0x02、22 字段及类型码)自写最小 Java 序列化流;
不含真实聊天内容、真实 uin、真实样本十六进制(对端/群成员 uin 均为编造值;SELF_UID 沿用 tests/conftest.py 夹具里 qd01 的登录 uin,
假主库文件名与 bootstrap 依赖它与夹具一致)。
🔴 断言只写规格说的可观测结果;失败即「实现缺陷 / 用例误读 / 规格问题」,在交接里逐条分诊。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.msgdata import MainDbRow
from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.alerts import QIDIAN_DB_UNAVAILABLE, Alerts
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Message, Session
from qtrade_agent.store import Store

REPO = Path(__file__).resolve().parents[2]
SELF_UID = "3007373675"          # conftest 里 qd01 的登录 uin
PEER_A = "520009001"             # 编造的单聊对端(不用规格示例 uin,避免与真机样本会话表撞名)
PEER_B = "520001234"             # 编造
GROUP_G = "123456"               # 06 §2.9.5 例子里的群号
TZ8 = timezone(timedelta(hours=8))
TOK_R = "tok-reply-read"

CLASS = "com.tencent.mobileqq.data.MessageForReplyText$SourceMsgInfo"     # 06 §2.9.5 输入限制:类名逐字
KEY = "sens_msg_source_msg_info"                                          # 06 §2.9.5 extStr 解码 ③④
MAGIC = b"\xac\xed\x00\x05"
UID_BIG = 2 ** 53 + 1            # 06 §2.9.5 / 00 §7.4:origUid 实测 > 2^53,JSON 数字会丢精度 —— 用 2^53+1 正好暴露
SEQ = 64001                      # 编造的 mSourceMsgSeq / 原行 shmsgseq
SENTINEL = "哨兵引用内容QZX"     # 引用摘要里的哨兵串:用来查「绝不拼进 text」「日志不含引用内容」

# 存储 JSON 九个键(06 §2.9.5 字段映射表;02 §2.8.1)
STORE_KEYS = {"state", "text", "sender_id", "sender_name", "ts_ms", "source_msg_seq", "source_msg_uid", "source_ext_msg_id",
              "resolved_message_id"}
# API 视图对象顶层键(00 §7.4 reply 段)
VIEW_KEYS = {"state", "text", "sender", "ts", "source_msg_seq", "source_msg_uid", "source_ext_msg_id", "resolved_message_id"}
UNPARSED_STORE = {"state": "unparsed", **{k: None for k in STORE_KEYS - {"state"}}}


# ====================================================================== 一、最小 Java 序列化流构造器(只按 Java 对象序列化协议与 06 §2.9.5 实测类描述)

TC_NULL, TC_REFERENCE, TC_CLASSDESC, TC_OBJECT, TC_STRING, TC_ARRAY, TC_CLASS, TC_BLOCKDATA, TC_ENDBLOCKDATA = (
    0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78)
TC_LONGSTRING, TC_PROXYCLASSDESC, TC_ENUM = 0x7C, 0x7D, 0x7E
BASE_HANDLE = 0x7E0000
SC_WRITE_METHOD, SC_SERIALIZABLE, SC_EXTERNALIZABLE = 0x01, 0x02, 0x04
STRING_T = "Ljava/lang/String;"
SUID_BYTES = 0xACF317F8060854E0  # byte[] 的 serialVersionUID(Java 标准值,非样本)
SUID_INTS = 0x4DBA602676EAB2A5   # int[]
PRIM_FMT = {"B": ">b", "C": ">H", "D": ">d", "F": ">f", "I": ">i", "J": ">q", "S": ">h", "Z": ">?"}

# 06 §2.9.5「实测类描述」:22 字段,按流中顺序
LAYOUT: list[tuple[str, str]] = [
    ("I", "mLocalAtInfoDone"), ("J", "mSourceMsgSenderUin"), ("J", "mSourceMsgSeq"), ("I", "mSourceMsgTime"),
    ("J", "mSourceMsgToUin"), ("I", "mSourceQdMsgUser"), ("I", "mSourceSummaryFlag"), ("I", "mType"),
    ("I", "oriMsgType"), ("J", "origUid"), ("I", "replyPicHeight"), ("I", "replyPicWidth"),
    ("J", "uniseq"), ("L", "mAnonymousNickName"), ("L", "mAtInfoStr"), ("L", "mRichMsg"),
    ("[", "mSourceMessageByte"), ("L", "mSourceMsgText"), ("L", "mSourceMsgTroopName"), ("L", "mSourceQdMsgUid"),
    ("L", "mSourceQdMsgUserName"), ("L", "senderName"),
]


def mutf8(s: str) -> bytes:
    """Java modified UTF-8(按 UTF-16 码元;U+0000 → C0 80)。"""
    out = bytearray()
    units = s.encode("utf-16-be")
    for i in range(0, len(units), 2):
        u = (units[i] << 8) | units[i + 1]
        if 0x01 <= u <= 0x7F:
            out.append(u)
        elif u <= 0x7FF:
            out += bytes([0xC0 | (u >> 6), 0x80 | (u & 0x3F)])
        else:
            out += bytes([0xE0 | (u >> 12), 0x80 | ((u >> 6) & 0x3F), 0x80 | (u & 0x3F)])
    return bytes(out)


@dataclass
class LongStr:
    s: str


@dataclass
class Ref:
    field: Optional[str] = None      # 指向本流里该字段的字符串值所占的句柄
    handle: Optional[int] = None     # 或直接给句柄


@dataclass
class Raw:
    data: bytes                      # 原样写入(构造嵌套对象 / 枚举 / 类 / 块数据等禁用标记)


@dataclass
class IntArr:
    vals: list[int]


class _W:
    def __init__(self, magic: bytes):
        self.b = bytearray(magic)
        self.h = BASE_HANDLE

    def handle(self) -> int:
        h = self.h
        self.h += 1
        return h

    @property
    def n_handles(self) -> int:
        return self.h - BASE_HANDLE

    def u1(self, v: int) -> None:
        self.b.append(v)

    def u2(self, v: int) -> None:
        self.b += struct.pack(">H", v)

    def u4(self, v: int) -> None:
        self.b += struct.pack(">I", v)

    def utf(self, s: str) -> None:
        d = mutf8(s)
        self.u2(len(d))
        self.b += d

    def tstring(self, s: str) -> int:
        self.u1(TC_STRING)
        h = self.handle()
        self.utf(s)
        return h


def _classdesc(w: _W, name: str, suid: int, flags: int, fields: list, type_handles: dict, super_raw: Optional[bytes] = None) -> None:
    w.u1(TC_CLASSDESC)
    w.handle()
    w.utf(name)
    w.b += struct.pack(">Q", suid & 0xFFFFFFFFFFFFFFFF)
    w.u1(flags)
    w.u2(len(fields))
    for f in fields:
        tc, fname = f[0], f[1]
        w.u1(ord(tc))
        w.utf(fname)
        if tc in "L[":
            t = f[2] if len(f) > 2 else (STRING_T if tc == "L" else "[B")
            if t in type_handles:
                w.u1(TC_REFERENCE)
                w.u4(type_handles[t])
            else:
                type_handles[t] = w.tstring(t)
    w.u1(TC_ENDBLOCKDATA)
    if super_raw is None:
        w.u1(TC_NULL)
    else:
        w.b += super_raw


def _value(w: _W, tc: str, v: Any, name: str, str_handles: dict) -> None:
    if tc in PRIM_FMT:
        w.b += struct.pack(PRIM_FMT[tc], v if v is not None else 0)
        return
    if v is None:
        w.u1(TC_NULL)
    elif isinstance(v, str):
        str_handles[name] = w.tstring(v)
    elif isinstance(v, LongStr):
        w.u1(TC_LONGSTRING)
        str_handles[name] = w.handle()
        d = mutf8(v.s)
        w.b += struct.pack(">Q", len(d)) + d
    elif isinstance(v, Ref):
        w.u1(TC_REFERENCE)
        w.u4(v.handle if v.handle is not None else str_handles[v.field])
    elif isinstance(v, (bytes, bytearray)):
        w.u1(TC_ARRAY)
        _classdesc(w, "[B", SUID_BYTES, SC_SERIALIZABLE, [], {})
        w.handle()
        w.u4(len(v))
        w.b += bytes(v)
    elif isinstance(v, IntArr):
        w.u1(TC_ARRAY)
        _classdesc(w, "[I", SUID_INTS, SC_SERIALIZABLE, [], {})
        w.handle()
        w.u4(len(v.vals))
        for x in v.vals:
            w.b += struct.pack(">i", x)
    elif isinstance(v, Raw):
        w.b += v.data
    else:  # pragma: no cover
        raise TypeError(v)


def build_info(values: Optional[dict] = None, *, layout: Optional[list] = None, class_name: str = CLASS, suid: int = 1,
               flags: int = SC_SERIALIZABLE, super_raw: Optional[bytes] = None, magic: bytes = MAGIC, trailing: bytes = b"",
               want_handles: bool = False):
    """按 Java 序列化协议写一个 SourceMsgInfo 顶层对象。values:字段名 → 值(基本类型给 int;对象字段给 str/None/LongStr/Ref/bytes/IntArr/Raw)。"""
    vals = dict(values or {})
    fields = layout if layout is not None else LAYOUT
    w = _W(magic)
    w.u1(TC_OBJECT)
    _classdesc(w, class_name, suid, flags, fields, {}, super_raw)
    w.handle()                       # 对象本身
    str_handles: dict = {}
    for f in fields:
        _value(w, f[0], vals.get(f[1]), f[1], str_handles)
    data = bytes(w.b) + trailing
    return (data, w.n_handles) if want_handles else data


def tiny_object_raw(class_name: str = "x.Nested", flags: int = SC_SERIALIZABLE) -> bytes:
    """一个无字段的嵌套 TC_OBJECT(用于「嵌套对象 → unparsed」)。"""
    w = _W(b"")
    w.u1(TC_OBJECT)
    _classdesc(w, class_name, 7, flags, [], {})
    return bytes(w.b)


def enum_raw() -> bytes:
    w = _W(b"")
    w.u1(TC_ENUM)
    _classdesc(w, "x.Color", 0, 0x12, [], {})
    w.u1(TC_STRING)
    w.utf("RED")
    return bytes(w.b)


def class_raw() -> bytes:
    w = _W(b"")
    w.u1(TC_CLASS)
    _classdesc(w, "x.Klass", 9, SC_SERIALIZABLE, [], {})
    return bytes(w.b)


def ok_values(**over) -> dict:
    """一组「正常 ok」字段值(全部编造)。"""
    v = dict(mSourceMsgSenderUin=int(PEER_A), mSourceMsgSeq=SEQ, mSourceMsgTime=1_758_239_900, mSourceMsgToUin=520009009,
             origUid=UID_BIG, uniseq=0, mSourceMsgText=f"原报价摘要 {SENTINEL}", mSourceMessageByte=b"\x01\x02\x03",
             senderName=None, mSourceQdMsgUserName=None)
    v.update(over)
    return v


# ---------------------------------------------------------------------- extStr 构造(06 §2.9.5:与 msgData 同一把 17 字节密钥 XOR → UTF-8 → JSON)

def ext_json(obj: Any) -> bytes:
    return xor(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def ext_reply(stream_or_value: Any, *, lower: bool = False, extra: Optional[dict] = None) -> bytes:
    """extStr = XOR(JSON{sens_msg_source_msg_info: <hex 串>, ...});stream_or_value 为 bytes 时转十六进制串,否则原样作为该键的值。"""
    if isinstance(stream_or_value, (bytes, bytearray)):
        h = bytes(stream_or_value).hex()
        val: Any = h if lower else h.upper()
    else:
        val = stream_or_value
    obj = {KEY: val, "c2c_msg_seq": "77", "hasSentReceipt": "0"}
    obj.update(extra or {})
    return ext_json(obj)


# ====================================================================== 二、带 extStr / msgUid 列的假企点主库(06 §2.9.5 表名/列/XOR 编码)

def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().upper()


def table_name(peer: str, group: bool = False) -> str:
    return f"mr_{'troop' if group else 'friend'}_{md5_upper(peer)}_New"


class ReplyMainDb:
    """比 conftest.FakeMainDb 多两列:msgUid(INTEGER,关联用)、extStr(BLOB,XOR 后);可按表关掉这两列做缺列注入。"""

    def __init__(self, path: str, self_uin: str = SELF_UID):
        self.path = path
        self.self_uin = self_uin
        self._uniseq = 500_000
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS mr_data_line(_id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

    def ensure_table(self, peer: str, group: bool = False, *, with_extstr: bool = True, with_msguid: bool = True) -> str:
        t = table_name(peer, group)
        cols = ("_id INTEGER PRIMARY KEY AUTOINCREMENT, issend INTEGER, istroop INTEGER, time INTEGER, msgtype INTEGER, uniseq INTEGER, "
                "msgseq INTEGER, shmsgseq INTEGER, senderuin BLOB, frienduin BLOB, msgData BLOB")
        if with_msguid:
            cols += ", msgUid INTEGER"
        if with_extstr:
            cols += ", extStr BLOB"
        con = sqlite3.connect(self.path)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{t}" ({cols})')
        con.commit()
        con.close()
        return t

    def _cols(self, t: str) -> set[str]:
        con = sqlite3.connect(self.path)
        try:
            return {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
        finally:
            con.close()

    def insert(self, peer: str, *, time_s: int, body: Any = "收到", msgtype: int = -1000, group: bool = False, issend: int = 0,
               sender: Optional[str] = None, shmsgseq: Optional[int] = None, msg_uid: int = 0, uniseq: Optional[int] = None,
               ext: Optional[bytes] = None, row_id: Optional[int] = None) -> int:
        t = self.ensure_table(peer, group)
        cols = self._cols(t)
        self._uniseq += 1
        u = uniseq if uniseq is not None else self._uniseq
        snd = sender or (self.self_uin if issend else peer)
        data = body.encode("utf-8") if isinstance(body, str) else body
        rec: dict[str, Any] = {"issend": issend, "istroop": 1 if group else 0, "time": time_s, "msgtype": msgtype, "uniseq": u,
                               "msgseq": u, "shmsgseq": shmsgseq if shmsgseq is not None else u, "senderuin": xor(snd.encode()),
                               "frienduin": xor(peer.encode()), "msgData": xor(data)}
        if row_id is not None:
            rec["_id"] = row_id
        if "msgUid" in cols:
            rec["msgUid"] = msg_uid
        if "extStr" in cols:
            rec["extStr"] = ext
        con = sqlite3.connect(self.path)
        cur = con.execute(f'INSERT INTO "{t}"({",".join(rec)}) VALUES ({",".join("?" * len(rec))})', tuple(rec.values()))
        con.commit()
        con.close()
        return int(cur.lastrowid)

    def set_ext(self, peer: str, row_id: int, ext: Optional[bytes], group: bool = False) -> None:
        con = sqlite3.connect(self.path)
        con.execute(f'UPDATE "{table_name(peer, group)}" SET extStr=? WHERE _id=?', (ext, row_id))
        con.commit()
        con.close()


# ====================================================================== 三、公共小工具

def acct_view(state: str = "running", self_uid: Optional[str] = SELF_UID, id: str = "qd01") -> QidianAccountView:
    return QidianAccountView(id=id, state=state, self_uid=self_uid)


def make_poller(store, clock, db_dir: str) -> QidianPoller:
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    p = QidianPoller(store=store, events=events, alerts=alerts, cfg=AgentConfig(), h13_firing=lambda: False, clock=clock,
                     maindb_factory=lambda uid, acct: LocalSqliteMainDb(os.path.join(db_dir, f"{uid}.db")))
    p.alerts_ = alerts
    return p


def msg_row(store, ext_msg_id: str, account_id: str = "qd01") -> Optional[dict]:
    r = store.con.execute("SELECT * FROM messages WHERE account_id=? AND ext_msg_id=? ORDER BY identity_epoch DESC", (account_id, ext_msg_id)).fetchone()
    return dict(r) if r is not None else None


def stored_reply(store, ext_msg_id: str, account_id: str = "qd01") -> Optional[dict]:
    r = msg_row(store, ext_msg_id, account_id)
    assert r is not None, f"{ext_msg_id} 未入库"
    return None if r["reply_json"] is None else json.loads(r["reply_json"])


def msg_events(store, account_id: str = "qd01") -> list[dict]:
    return [e["payload"] for e in store.list_events(event="message", account_id=account_id)]


def iso8(ms: int) -> str:
    """00 §6:ISO 8601 带 +08:00。"""
    return datetime.fromtimestamp(ms / 1000, tz=TZ8).isoformat(timespec="seconds")


@pytest.fixture
def rdb(tmp_path) -> ReplyMainDb:
    return ReplyMainDb(str(tmp_path / f"{SELF_UID}.db"))


@pytest.fixture
def poller(store, clock, rdb):
    """bootstrap 在空主库上做(之后出现的表都从 0 起),再拨 5 s;之后插入的行 time=clock.now_s 不会被历史闸挡掉。"""
    p = make_poller(store, clock, os.path.dirname(rdb.path))
    p.poll_maindb(acct_view())
    clock.advance(5_000)
    return p


def poll(p, **kw) -> None:
    p.poll_maindb(acct_view(**kw))


# ====================================================================== A. 读取:第 8 列 / MainDbRow 默认值 / extStr 为 NULL / 缺列(06 §2.9.5「读取」;补充口径①)

class TestA_Read:
    def test_A1_maindbrow_seven_positional_args(self):
        """06 §2.9.5 读取:`MainDbRow` 新增 `extstr_hex: str = ""`——必须带默认值(既有用例按 7 个位置参数构造)。"""
        r = MainDbRow(1, 0, 1_758_240_000, -1000, 7001, "AA", "BB")
        assert r.extstr_hex == ""
        r2 = MainDbRow(id=2, issend=0, time=1, msgtype=-1049, uniseq=9, senderuin_hex="AA", msgdata_hex="BB", extstr_hex="CAFE")
        assert r2.extstr_hex == "CAFE"

    def test_A2_rows_after_eighth_column_hex_extstr(self, rdb, clock):
        """06 §2.9.5 读取 + ④ SELECT:第 8 列 = hex(extStr);extStr 为 NULL 时 hex() 得空串。"""
        ext = ext_reply(build_info(ok_values()))
        i1 = rdb.insert(PEER_A, time_s=clock.now_s, body="一", ext=ext)
        i2 = rdb.insert(PEER_A, time_s=clock.now_s, body="二", ext=None)
        rows = LocalSqliteMainDb(rdb.path).rows_after(table_name(PEER_A), 0)
        by = {r.id: r for r in rows}
        assert by[i1].extstr_hex.upper() == ext.hex().upper()
        assert by[i2].extstr_hex == ""

    def test_A3_extstr_null_is_no_reply(self, store, clock, rdb, poller):
        """06 §2.9.5 读取 / 解码 ①:extStr 为 NULL(hex 得空串)→ 无引用,reply_json 存 NULL(02 §2.8.1:无引用存 NULL)。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="普通", uniseq=710001, ext=None)
        poll(poller)
        assert stored_reply(store, "qd:710001") is None
        assert msg_events(store)[0]["reply"] is None

    def test_A4_table_without_extstr_column(self, store, clock, rdb, poller):
        """总控补充口径①:会话表缺 extStr 列 ⇒ 该表按无引用处理、正文照常产出、水位照常;且不得因此判 schema_mismatch 告警。"""
        rdb.ensure_table(PEER_B, with_extstr=False)
        last = 0
        for i in range(3):
            last = rdb.insert(PEER_B, time_s=clock.now_s, body=f"缺列表第{i}条", uniseq=711000 + i)
        rdb.insert(PEER_A, time_s=clock.now_s, body="正常表", uniseq=711100, ext=ext_reply(build_info(ok_values())))
        for _ in range(3):
            poll(poller)
            clock.advance(5_000)
        for i in range(3):
            r = msg_row(store, f"qd:{711000 + i}")
            assert r is not None and r["text"] == f"缺列表第{i}条" and r["reply_json"] is None
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_B).value_int == last
        assert stored_reply(store, "qd:711100")["state"] == "ok"
        assert [e for e in store.list_events(event="alert") if e["payload"].get("code") == QIDIAN_DB_UNAVAILABLE] == []


# ====================================================================== B. extStr 解码三分支(06 §2.9.5「extStr 解码」①~⑤)

class TestB_ExtStrDecode:
    @pytest.mark.parametrize("ext", [
        pytest.param(b"", id="空串"),
        pytest.param(xor(b"\xff\xfe\x80\x81"), id="XOR后非UTF-8"),
        pytest.param(xor("这不是 JSON".encode()), id="非JSON"),
        pytest.param(ext_json([KEY, "x"]), id="顶层数组"),
        pytest.param(ext_json("sens_msg_source_msg_info"), id="顶层字符串"),
        pytest.param(ext_json({"qidian_svr_msg": "abc", "c2c_msg_seq": "12"}), id="普通-1000键无该键"),
        pytest.param(json.dumps({KEY: build_info(ok_values()).hex().upper()}).encode(), id="未XOR的明文JSON"),
    ])
    def test_B1_no_reply_branches(self, store, clock, rdb, poller, caplog, ext):
        """06 §2.9.5 解码 ①③:为空 / 非 UTF-8 / 非 JSON / 顶层非对象 / 不含该键 → reply=None,**不告警**;正文照常产出。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="正文照常", uniseq=720001, ext=ext)
        with caplog.at_level(logging.WARNING):
            poll(poller)
        r = msg_row(store, "qd:720001")
        assert r is not None and r["text"] == "正文照常"
        assert r["reply_json"] is None
        assert [x.getMessage() for x in caplog.records if x.levelno >= logging.WARNING] == []

    @pytest.mark.parametrize("val", [123, None, [1, 2], {"a": "b"}, True], ids=["数字", "null", "数组", "对象", "布尔"])
    def test_B2_key_value_not_string_unparsed(self, store, clock, rdb, poller, val):
        """06 §2.9.5 解码 ④:含该键但值不是字符串 → state="unparsed"(其余全 null),正文照常产出。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="值不是字符串", msgtype=-1049, uniseq=720101, ext=ext_reply(val))
        poll(poller)
        assert stored_reply(store, "qd:720101") == UNPARSED_STORE
        assert msg_row(store, "qd:720101")["text"] == "值不是字符串"

    @pytest.mark.parametrize("msgtype,body", [
        (-1000, "单行文本".encode()), (-1051, "1Y 1.70\n2Y 1.80".encode()), (-1049, "@张三 好的".encode()),
    ], ids=["-1000", "-1051", "-1049"])
    def test_B3_parse_regardless_of_msgtype_text_family(self, store, clock, rdb, poller, msgtype, body):
        """06 §2.9.5 解码 ⑤:判定不看 msgtype——文本族任一类型只要 extStr 含该键就解析(state=ok)。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body=body, msgtype=msgtype, uniseq=720201, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:720201")
        assert rep is not None and rep["state"] == "ok"
        assert msg_row(store, "qd:720201")["text"] == body.decode()

    def test_B4_mixed_1035_also_parsed(self, store, clock, rdb, poller):
        """06 §2.9.5 解码 ⑤:-1035 混排(有文本段、会产出)同样解析引用。"""
        def ld(no: int, payload: bytes) -> bytes:
            return bytes([(no << 3) | 2, len(payload)]) + payload
        mixed = ld(1, ld(1, ld(1, "配文".encode())))
        rdb.insert(PEER_A, time_s=clock.now_s, body=mixed, msgtype=-1035, uniseq=720301, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        r = msg_row(store, "qd:720301")
        assert r is not None and r["text"] == "配文"
        assert json.loads(r["reply_json"])["state"] == "ok"

    def test_B5_non_emitted_row_with_reply_no_message_watermark_passes(self, store, clock, rdb, poller):
        """06 §2.9.5:reply 只加在「已按上表产出的 Message」上;不产出的行(-2000)即便 extStr 含引用也不入库、无事件,水位照常越过。"""
        rid = rdb.insert(PEER_A, time_s=clock.now_s, body=b"\x0a\x03pic", msgtype=-2000, uniseq=720401, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        assert msg_row(store, "qd:720401") is None
        assert msg_events(store) == []
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_int == rid

    def test_B6_lowercase_hex_accepted(self, store, clock, rdb, poller):
        """06 §2.9.5 输入限制:「偶数长度、纯十六进制字符串」——小写十六进制同样合法。"""
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=720501, ext=ext_reply(build_info(ok_values()), lower=True))
        poll(poller)
        assert stored_reply(store, "qd:720501")["state"] == "ok"


# ====================================================================== C. 受限解析 · 输入限制(06 §2.9.5「SourceMsgInfo 受限解析 · 输入限制」)

def _pad_to(total_bytes: int) -> bytes:
    """造一条恰好 total_bytes 字节的合法流(用 mSourceMessageByte 填充,byte[] 计入 32 KiB 总限)。"""
    base = build_info(ok_values(mSourceMessageByte=b""))
    n = total_bytes - len(base)
    assert n >= 0
    s = build_info(ok_values(mSourceMessageByte=b"\x00" * n))
    assert len(s) == total_bytes
    return s


def _extra_string_fields(k: int) -> tuple[list, dict]:
    """在实测 22 字段后追加 k 个 String 字段(名字编造、值互不相同),每个值占一个新句柄。"""
    extra = [("L", f"zz{i:04d}") for i in range(k)]
    vals = {f"zz{i:04d}": f"v{i}" for i in range(k)}
    return LAYOUT + extra, vals


UNPARSED_CASES = {
    "hex非法字符": "ZZ" + build_info(ok_values()).hex().upper()[2:],
    "hex奇数长度": build_info(ok_values()).hex().upper() + "A",
    "hex带0x前缀": "0x" + build_info(ok_values()).hex().upper(),
    "hex含空格": build_info(ok_values()).hex().upper()[:10] + " " + build_info(ok_values()).hex().upper()[10:],
    "空串": "",
    "魔数错": build_info(ok_values(), magic=b"\xac\xed\x00\x06").hex(),
    "缺魔数": build_info(ok_values())[4:].hex(),
    "尾随一个字节": build_info(ok_values(), trailing=b"\x00").hex(),
    "两个顶层对象": (build_info(ok_values()) + build_info(ok_values())[4:]).hex(),
    "顶层是字符串而非对象": (MAGIC + bytes([TC_STRING]) + struct.pack(">H", 3) + b"abc").hex(),
    "流被截断": build_info(ok_values())[:-1].hex(),
    "类名不符_外部类": build_info(ok_values(), class_name="com.tencent.mobileqq.data.MessageForReplyText").hex(),
    "类名不符_多一字符": build_info(ok_values(), class_name=CLASS + "X").hex(),
    "类名不符_大小写": build_info(ok_values(), class_name=CLASS.replace("SourceMsgInfo", "sourceMsgInfo")).hex(),
    "标志含SC_WRITE_METHOD": build_info(ok_values(), flags=SC_SERIALIZABLE | SC_WRITE_METHOD).hex(),
    "标志含SC_EXTERNALIZABLE": build_info(ok_values(), flags=SC_SERIALIZABLE | SC_EXTERNALIZABLE).hex(),
    "标志缺SC_SERIALIZABLE": build_info(ok_values(), flags=0x00).hex(),
    "超类描述非TC_NULL": build_info(ok_values(), super_raw=tiny_object_raw()[1:]).hex(),
    "字段值为嵌套对象": build_info(ok_values(mRichMsg=Raw(tiny_object_raw()))).hex(),
    "字段值为枚举": build_info(ok_values(mAtInfoStr=Raw(enum_raw()))).hex(),
    "字段值为类": build_info(ok_values(mAtInfoStr=Raw(class_raw()))).hex(),
    "字段值为TC_BLOCKDATA": build_info(ok_values(mAtInfoStr=Raw(bytes([TC_BLOCKDATA, 2, 0, 0])))).hex(),
    "非byte数组": build_info(ok_values(mSourceMessageByte=IntArr([1, 2, 3])),
                          layout=[f if f[1] != "mSourceMessageByte" else ("[", "mSourceMessageByte", "[I") for f in LAYOUT]).hex(),
    "引用指向非字符串句柄": build_info(ok_values(senderName=Ref(handle=BASE_HANDLE))).hex(),
    "顶层代理类描述": (MAGIC + bytes([TC_OBJECT, TC_PROXYCLASSDESC]) + struct.pack(">I", 1) + struct.pack(">H", 3) + b"x.I"
                    + bytes([TC_ENDBLOCKDATA, TC_NULL])).hex(),
}


class TestC_InputLimits:
    @pytest.mark.parametrize("name", list(UNPARSED_CASES))
    def test_C1_unparsed_cases(self, store, clock, rdb, poller, name):
        """06 §2.9.5 输入限制逐项:hex 非法/奇数、魔数、恰一个顶层 TC_OBJECT、恰好消费完、类名逐字、标志(含 SERIALIZABLE、不含 WRITE_METHOD/
        EXTERNALIZABLE)、超类 TC_NULL、允许标记(嵌套对象/枚举/类/代理/非 byte[] 数组/TC_BLOCKDATA/指向非字符串的引用均禁)→ unparsed,
        其余键全 null;失败语义:正文照常产出、水位照常推进。"""
        rid = rdb.insert(PEER_A, time_s=clock.now_s, body="照常产出", msgtype=-1049, uniseq=730001, ext=ext_reply(UNPARSED_CASES[name]))
        poll(poller)
        r = msg_row(store, "qd:730001")
        assert r is not None and r["text"] == "照常产出", name
        assert json.loads(r["reply_json"]) == UNPARSED_STORE, name
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_int == rid

    def test_C2_length_limit_65536_hex_chars(self, store, clock, rdb, poller):
        """06 §2.9.5:值长度 ≤ 65536 字符(≤ 32 KiB 字节);byte[] 计入总限。恰 32768 字节 → ok;32769 字节 → unparsed。"""
        ok_stream = _pad_to(32768)
        bad_stream = _pad_to(32769)
        assert len(ok_stream.hex()) == 65536 and len(bad_stream.hex()) == 65538
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730101, ext=ext_reply(ok_stream))
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730102, ext=ext_reply(bad_stream))
        poll(poller)
        assert stored_reply(store, "qd:730101")["state"] == "ok"
        assert stored_reply(store, "qd:730102") == UNPARSED_STORE

    def test_C3_handle_limit_256(self, store, clock, rdb, poller):
        """06 §2.9.5:句柄数 ≤ 256。恰 256 个句柄 → ok;257 个 → unparsed(不写死字段个数,多出的未知字段本身不判失败)。"""
        _, h0 = build_info(ok_values(), want_handles=True)
        k_ok = 256 - h0
        lay_ok, ex_ok = _extra_string_fields(k_ok)
        s_ok, n_ok = build_info(ok_values(**ex_ok), layout=lay_ok, want_handles=True)
        lay_bad, ex_bad = _extra_string_fields(k_ok + 1)
        s_bad, n_bad = build_info(ok_values(**ex_bad), layout=lay_bad, want_handles=True)
        assert (n_ok, n_bad) == (256, 257)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730201, ext=ext_reply(s_ok))
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730202, ext=ext_reply(s_bad))
        poll(poller)
        rep = stored_reply(store, "qd:730201")
        assert rep["state"] == "ok" and rep["text"] == f"原报价摘要 {SENTINEL}"
        assert stored_reply(store, "qd:730202") == UNPARSED_STORE

    def test_C4_allowed_markers_ok(self, store, clock, rdb, poller):
        """06 §2.9.5 允许标记:TC_LONGSTRING、指向已出现字符串的 TC_REFERENCE、byte[](只跳过)、TC_NULL、全部八种基本类型 → ok;
        并按流内字段表读取、**不写死字段顺序与个数**(打乱实测顺序、追加未知字段)。"""
        prims = [f for f in LAYOUT if f[0] not in "L["]
        objs = [f for f in LAYOUT if f[0] in "L["]
        extra_prims = [("B", "aExtraB"), ("C", "aExtraC"), ("D", "aExtraD"), ("F", "aExtraF"), ("S", "aExtraS"), ("Z", "aExtraZ")]
        layout = list(reversed(prims)) + extra_prims + list(reversed(objs)) + [("L", "zExtraStr")]
        # 对象字段倒序 ⇒ senderName 先写出(TC_LONGSTRING),之后 mSourceMsgText 以 TC_REFERENCE 指回它
        vals = ok_values(senderName=LongStr("长串摘要 1Y 1.70"), mSourceMsgText=Ref(field="senderName"),
                         mSourceQdMsgUserName="不该被选中", mSourceMessageByte=b"\xff" * 40, mAtInfoStr=LongStr("@某人"),
                         aExtraB=-3, aExtraC=0x4E2D, aExtraD=1.5, aExtraF=2.5, aExtraS=-7, aExtraZ=True, zExtraStr="未知字段")
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730301, ext=ext_reply(build_info(vals, layout=layout)))
        poll(poller)
        rep = stored_reply(store, "qd:730301")
        assert rep["state"] == "ok"
        assert rep["text"] == "长串摘要 1Y 1.70"
        assert rep["sender_name"] == "长串摘要 1Y 1.70"
        assert rep["sender_id"] == PEER_A and rep["source_msg_seq"] == str(SEQ) and rep["source_msg_uid"] == str(UID_BIG)
        assert rep["ts_ms"] == 1_758_239_900 * 1000

    def test_C5_longstring_text(self, store, clock, rdb, poller):
        """06 §2.9.5 允许 TC_LONGSTRING:摘要用长串编码照常取出。"""
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730401, ext=ext_reply(build_info(ok_values(mSourceMsgText=LongStr("长串 OK")))))
        poll(poller)
        assert stored_reply(store, "qd:730401")["text"] == "长串 OK"

    def test_C6_missing_fields_null_not_failure(self, store, clock, rdb, poller):
        """06 §2.9.5:「缺少下表用到的任一字段时对应输出为 null,不判失败」。"""
        drop = {"mSourceMsgText", "senderName", "mSourceQdMsgUserName", "mSourceMsgTime", "origUid"}
        layout = [f for f in LAYOUT if f[1] not in drop]
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=730501, ext=ext_reply(build_info(ok_values(), layout=layout)))
        poll(poller)
        rep = stored_reply(store, "qd:730501")
        assert rep["state"] == "ok"
        assert rep["text"] is None and rep["sender_name"] is None and rep["ts_ms"] is None and rep["source_msg_uid"] is None
        assert rep["sender_id"] == PEER_A and rep["source_msg_seq"] == str(SEQ)
        assert rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None


# ====================================================================== D. 字段映射(06 §2.9.5「字段映射」表逐行)

class TestD_FieldMapping:
    def _one(self, store, clock, rdb, poller, row_uniseq: int, **over) -> dict:
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=row_uniseq, ext=ext_reply(build_info(ok_values(**over))))
        poll(poller)
        rep = stored_reply(store, f"qd:{row_uniseq}")
        assert rep is not None and set(rep) == STORE_KEYS, rep
        return rep

    def test_D1_full_ok_row(self, store, clock, rdb, poller):
        """字段映射表:state=ok;text←mSourceMsgText;sender_id←mSourceMsgSenderUin 十进制串;ts_ms=秒×1000(整数);
        source_msg_seq←mSourceMsgSeq 十进制串;source_msg_uid←origUid 十进制**字符串**(2^53+1 不丢精度);九个键齐全。"""
        rep = self._one(store, clock, rdb, poller, 740001, senderName="张三")
        assert rep["state"] == "ok"
        assert rep["text"] == f"原报价摘要 {SENTINEL}"
        assert rep["sender_id"] == PEER_A
        assert rep["sender_name"] == "张三"
        assert rep["ts_ms"] == 1_758_239_900_000 and isinstance(rep["ts_ms"], int)
        assert rep["source_msg_seq"] == str(SEQ)
        assert rep["source_msg_uid"] == "9007199254740993"
        raw = msg_row(store, "qd:740001")["reply_json"]
        assert '"9007199254740993"' in raw.replace(" ", "")      # 存储层同样是字符串、不是 JSON 数字

    def test_D2_text_clean_text_and_2000_truncation(self, store, clock, rdb, poller):
        """字段映射 text:经 clean_text(表情 → [名称]、其余控制字符丢弃);超过 2000 字符截到 2000(先 clean 后截)。"""
        r1 = self._one(store, clock, rdb, poller, 740101, mSourceMsgText="收到\u0014A好\u0001\u0008的")
        assert r1["text"] == "收到[爱你]好的"
        r2 = self._one(store, clock, rdb, poller, 740102, mSourceMsgText="甲" * 2500)
        assert r2["text"] == "甲" * 2000
        r3 = self._one(store, clock, rdb, poller, 740103, mSourceMsgText="乙" * 2000)
        assert r3["text"] == "乙" * 2000
        r4 = self._one(store, clock, rdb, poller, 740104, mSourceMsgText="\u0014A" + "x" * 2000)
        assert r4["text"] == ("[爱你]" + "x" * 2000)[:2000]

    @pytest.mark.parametrize("val", ["", None], ids=["空串", "null"])
    def test_D3_text_empty_or_null(self, store, clock, rdb, poller, val):
        """字段映射 text:空串 / null → null(state 仍 ok)。"""
        rep = self._one(store, clock, rdb, poller, 740201, mSourceMsgText=val)
        assert rep["state"] == "ok" and rep["text"] is None

    @pytest.mark.parametrize("uin,expect", [(int(PEER_A), PEER_A), (0, None), (-5, None)], ids=[">0", "0", "<0"])
    def test_D4_sender_id(self, store, clock, rdb, poller, uin, expect):
        """字段映射 sender_id:mSourceMsgSenderUin > 0 → 十进制字符串;否则 null。"""
        assert self._one(store, clock, rdb, poller, 740301, mSourceMsgSenderUin=uin)["sender_id"] == expect

    @pytest.mark.parametrize("sname,uname,expect", [
        ("张三", "李四", "张三"), ("", "李四", "李四"), (None, "李四", "李四"), (None, None, None), ("", "", None),
        ("名" * 200, None, "名" * 128), ("\u0014A王五", None, "[爱你]王五"),
    ], ids=["取senderName", "senderName空串回退", "senderName为null回退", "两者null", "两者空串", "截到128", "clean_text"])
    def test_D5_sender_name(self, store, clock, rdb, poller, sname, uname, expect):
        """字段映射 sender_name:senderName,为空再取 mSourceQdMsgUserName;首个非空字符串,clean_text 后 ≤ 128 字符;否则 null。"""
        rep = self._one(store, clock, rdb, poller, 740401, senderName=sname, mSourceQdMsgUserName=uname)
        assert rep["sender_name"] == expect

    @pytest.mark.parametrize("t,expect", [(1_758_239_900, 1_758_239_900_000), (0, None), (-1, None)], ids=[">0", "0", "<0"])
    def test_D6_ts_ms(self, store, clock, rdb, poller, t, expect):
        """字段映射 ts_ms:mSourceMsgTime(秒)> 0 → ×1000;否则 null。"""
        assert self._one(store, clock, rdb, poller, 740501, mSourceMsgTime=t)["ts_ms"] == expect

    @pytest.mark.parametrize("seq,expect", [(SEQ, str(SEQ)), (0, None), (-9, None)], ids=[">0", "0", "<0"])
    def test_D7_source_msg_seq(self, store, clock, rdb, poller, seq, expect):
        """字段映射 source_msg_seq:mSourceMsgSeq > 0 → 十进制字符串;否则 null。"""
        assert self._one(store, clock, rdb, poller, 740601, mSourceMsgSeq=seq)["source_msg_seq"] == expect

    @pytest.mark.parametrize("uid,expect", [(UID_BIG, "9007199254740993"), (2 ** 63 - 1, str(2 ** 63 - 1)), (0, None), (-2, None)],
                             ids=["2^53+1", "long最大", "0", "<0"])
    def test_D8_source_msg_uid(self, store, clock, rdb, poller, uid, expect):
        """字段映射 source_msg_uid:origUid > 0 → 十进制**字符串**;否则 null。"""
        assert self._one(store, clock, rdb, poller, 740701, origUid=uid)["source_msg_uid"] == expect

    def test_D9_info_uniseq_not_used_as_seq(self, store, clock, rdb, poller):
        """06 §2.9.5:mSourceMsgSeq 不是 uniseq;SourceMsgInfo.uniseq 本轮不使用——给它一个能命中某行 uniseq 的值也不得据此关联。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="某条", uniseq=740801, shmsgseq=11, msg_uid=12)
        poll(poller)
        rep = self._one(store, clock, rdb, poller, 740802, uniseq=740801, mSourceMsgSeq=999_999, origUid=888_888)
        assert rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None


# ====================================================================== E. 确定性关联(06 §2.9.5「原消息确定性关联」①~⑤;02 §2.8.1 ①)

class TestE_Association:
    @pytest.mark.parametrize("orig_issend", [0, 1], ids=["原消息入向", "原消息出向"])
    def test_E1_exact_match_resolves(self, store, clock, rdb, poller, orig_issend):
        """②:同一会话表 shmsgseq=seq AND msgUid=uid 恰 1 行且 uniseq≠0 → source_ext_msg_id="qd:<uniseq>";
        ③:store 同账号同会话同 epoch 按 ext_msg_id 恰 1 行 → resolved_message_id = 该行 id(字符串)。time 不是必要条件。"""
        rdb.insert(PEER_A, time_s=clock.now_s - 10, body="原文 1Y 1.70\n第二行\n第三行", issend=orig_issend, uniseq=750001,
                   shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        orig = msg_row(store, "qd:750001")
        assert orig is not None
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, body="@张三 好的", msgtype=-1049, uniseq=750002,
                   ext=ext_reply(build_info(ok_values(mSourceMsgTime=clock.now_s - 99_999))))
        poll(poller)
        rep = stored_reply(store, "qd:750002")
        assert rep["source_ext_msg_id"] == "qd:750001"
        assert rep["resolved_message_id"] == orig["id"] and isinstance(rep["resolved_message_id"], str)

    def test_E2_zero_rows(self, store, clock, rdb, poller):
        """②:0 行 → source_ext_msg_id / resolved_message_id 均 null;不影响本条入库。"""
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750101, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:750101")
        assert rep["state"] == "ok" and rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E3_two_rows_null(self, store, clock, rdb, poller):
        """②:≥ 2 行 → null(LIMIT 2 判唯一),即便其中一行已入库。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="甲", uniseq=750201, shmsgseq=SEQ, msg_uid=UID_BIG)
        rdb.insert(PEER_A, time_s=clock.now_s, body="乙", uniseq=750202, shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750203, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:750203")
        assert rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E4_uniseq_zero_null(self, store, clock, rdb, poller):
        """②:恰 1 行但 uniseq = 0 → null。"""
        rdb.insert(PEER_A, time_s=clock.now_s - 999_999, body="零", uniseq=0, shmsgseq=SEQ, msg_uid=UID_BIG)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750301, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:750301")
        assert rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E5_other_table_not_searched(self, store, clock, rdb, poller):
        """②:只在**同一会话表**内查——别的会话表里 shmsgseq/msgUid 完全吻合的行不算。"""
        rdb.insert(PEER_B, time_s=clock.now_s, body="别的会话", uniseq=750401, shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750402, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:750402")
        assert rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E6_seq_match_uid_mismatch(self, store, clock, rdb, poller):
        """②:shmsgseq 与 msgUid 两列都要相等;只 seq 相等不算。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="同 seq 不同 uid", uniseq=750501, shmsgseq=SEQ, msg_uid=UID_BIG + 1)
        poll(poller)
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750502, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        assert stored_reply(store, "qd:750502")["source_ext_msg_id"] is None

    def test_E7_prerequisite_uid_null_no_lookup(self, store, clock, rdb, poller):
        """①:前提 source_msg_seq、source_msg_uid 均非 null——origUid=0(→ null)时即便表里有 shmsgseq=seq 且 msgUid=0 的行也不关联。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="msgUid 为 0 的行", uniseq=750601, shmsgseq=SEQ, msg_uid=0)
        poll(poller)
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750602, ext=ext_reply(build_info(ok_values(origUid=0))))
        poll(poller)
        rep = stored_reply(store, "qd:750602")
        assert rep["source_msg_uid"] is None and rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E8_unparsed_no_association(self, store, clock, rdb, poller):
        """①:前提 state="ok";unparsed 时 source_ext_msg_id/resolved_message_id 恒 null。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="原", uniseq=750701, shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750702, ext=ext_reply(build_info(ok_values(), trailing=b"\x00")))
        poll(poller)
        assert stored_reply(store, "qd:750702") == UNPARSED_STORE

    def test_E9_original_not_ingested_source_only(self, store, clock, rdb, poller):
        """②③:原行在主库里(被历史闸挡掉、从未入库)⇒ source_ext_msg_id 照填,resolved_message_id = null。"""
        rdb.insert(PEER_A, time_s=clock.now_s - 86_400, body="历史原文", uniseq=750801, shmsgseq=SEQ, msg_uid=UID_BIG)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750802, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        assert msg_row(store, "qd:750801") is None
        rep = stored_reply(store, "qd:750802")
        assert rep["source_ext_msg_id"] == "qd:750801" and rep["resolved_message_id"] is None

    def test_E10_cross_session_not_resolved(self, store, clock, rdb, poller):
        """③:同账号 + **同会话**——messages 里 ext_msg_id 相同的行在另一个会话(假库里两表 uniseq 撞值)⇒ resolved 为 null。"""
        rdb.insert(PEER_B, time_s=clock.now_s, body="B 会话里的行", uniseq=750901)
        poll(poller)
        assert msg_row(store, "qd:750901")["session_id"] == f"qd01:{PEER_B}"
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s - 86_400, body="A 会话历史原文", uniseq=750901, shmsgseq=SEQ, msg_uid=UID_BIG)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=750902, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:750902")
        assert rep["source_ext_msg_id"] == "qd:750901"
        assert rep["resolved_message_id"] is None

    def test_E11_cross_account_not_resolved(self, store, clock, rdb, poller):
        """③:**同账号**——qd02 里有 ext_msg_id 相同的行,不得被 qd01 的引用关联。"""
        store.ensure_account("qd02", "qidian", state="running", self_uid="4002")
        store.ingest(Message("qd02", "qidian", Session("qd02", PEER_A, "private"), "in", "text", "qd02 的行", clock(), "qidian_db",
                             ext_msg_id="qd:751001", dedup_kind="native", sender_id=PEER_A))
        rdb.insert(PEER_A, time_s=clock.now_s - 86_400, body="qd01 历史原文", uniseq=751001, shmsgseq=SEQ, msg_uid=UID_BIG)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=751002, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:751002")
        assert rep["source_ext_msg_id"] == "qd:751001" and rep["resolved_message_id"] is None

    def test_E12_cross_identity_epoch_not_resolved(self, store, clock, rdb, poller, tmp_path):
        """③:**同 identity_epoch**——换登录号(epoch 0 → 1)后,新 epoch 的引用即便 source_ext_msg_id 指向 epoch 0 已入库的同名行也不关联;
        对照:epoch 0 内同样的引用能关联。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="epoch0 原文", uniseq=752001, shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        orig0 = msg_row(store, "qd:752001")
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=752002, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        assert stored_reply(store, "qd:752002")["resolved_message_id"] == orig0["id"]       # 对照组
        poll(poller, state="login_required")
        clock.advance(60_000)
        db_new = ReplyMainDb(str(tmp_path / "40009.db"), self_uin="40009")
        db_new.insert(PEER_A, time_s=clock.now_s - 7_200, body="同一原文(新库里是历史)", uniseq=752001, shmsgseq=SEQ, msg_uid=UID_BIG)
        changed = store.observe_qidian_identity("qd01", SELF_UID, "40009")
        assert changed is not None and changed["self_uid"] == "40009"
        poll(poller, self_uid="40009")
        clock.advance(5_000)
        db_new.insert(PEER_A, time_s=clock.now_s, uniseq=752003, ext=ext_reply(build_info(ok_values())))
        poll(poller, self_uid="40009")
        r = store.con.execute("SELECT reply_json, identity_epoch FROM messages WHERE account_id='qd01' AND ext_msg_id='qd:752003'").fetchone()
        assert r is not None and r["identity_epoch"] == 1
        rep = json.loads(r["reply_json"])
        assert rep["source_ext_msg_id"] == "qd:752001"
        assert rep["resolved_message_id"] is None

    def test_E13_no_backfill_when_original_arrives_later(self, store, clock, rdb, poller):
        """④:关联只在入库那一刻做一次——之后原消息才出现/入库,也不回补引用行;回补也不发事件。"""
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=753001, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        before = stored_reply(store, "qd:753001")
        assert before["source_ext_msg_id"] is None and before["resolved_message_id"] is None
        n_ev = len(msg_events(store))
        clock.advance(5_000)
        rdb.insert(PEER_A, time_s=clock.now_s, body="原文后到", uniseq=753002, shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        assert msg_row(store, "qd:753002") is not None
        assert stored_reply(store, "qd:753001") == before
        assert len(msg_events(store)) == n_ev + 1          # 只有原文那一条

    def test_E14_original_earlier_in_same_batch(self, store, clock, rdb, poller):
        """02 §2.8.1 ①:resolved 由 store 在**入库这一刻**查 messages——原消息与引用同一批到达、原消息 _id 在前(先入库)⇒ 能关联。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="同批原文", uniseq=753101, shmsgseq=SEQ, msg_uid=UID_BIG)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=753102, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        rep = stored_reply(store, "qd:753102")
        assert rep["source_ext_msg_id"] == "qd:753101"
        assert rep["resolved_message_id"] == msg_row(store, "qd:753101")["id"]

    def test_E15_no_heuristics_identical_text(self, store, clock, rdb, poller):
        """⑤ 禁止启发式:同会话里有一条**文本与摘要完全相同**、时间最近的消息,但 shmsgseq 不匹配 ⇒ source/resolved 必须为 null。"""
        summary = "完全相同的报价 1Y 1.70"
        rdb.insert(PEER_A, time_s=clock.now_s, body=summary, uniseq=753201, shmsgseq=SEQ + 1, msg_uid=UID_BIG)
        poll(poller)
        clock.advance(1_000)
        rdb.insert(PEER_A, time_s=clock.now_s, uniseq=753202,
                   ext=ext_reply(build_info(ok_values(mSourceMsgText=summary, mSourceMsgTime=clock.now_s - 1))))
        poll(poller)
        rep = stored_reply(store, "qd:753202")
        assert rep["text"] == summary
        assert rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E16_missing_msguid_column_query_error_null(self, store, clock, rdb, poller):
        """②:查询报错 → null,不抛出、不影响本条入库(会话表缺 msgUid 列即关联查询必报错)。"""
        rdb.ensure_table(PEER_B, with_msguid=False)
        rdb.insert(PEER_B, time_s=clock.now_s, uniseq=753301, ext=ext_reply(build_info(ok_values())))
        poll(poller)
        r = msg_row(store, "qd:753301")
        assert r is not None
        rep = json.loads(r["reply_json"])
        assert rep["state"] == "ok" and rep["source_ext_msg_id"] is None and rep["resolved_message_id"] is None

    def test_E17_group_table(self, store, clock, rdb, poller):
        """06 §2.9.5:关联只看同一会话表,群表(mr_troop_*)同样适用。"""
        rdb.insert(GROUP_G, group=True, time_s=clock.now_s, body="群原文", sender="777001", uniseq=753401, shmsgseq=SEQ, msg_uid=UID_BIG)
        poll(poller)
        clock.advance(5_000)
        rdb.insert(GROUP_G, group=True, time_s=clock.now_s, body="@甲 好", msgtype=-1049, sender="777002", uniseq=753402,
                   ext=ext_reply(build_info(ok_values(mSourceMsgSenderUin=777001))))
        poll(poller)
        rep = stored_reply(store, "qd:753402")
        assert rep["sender_id"] == "777001"
        assert rep["source_ext_msg_id"] == "qd:753401" and rep["resolved_message_id"] == msg_row(store, "qd:753401")["id"]


# ====================================================================== F. 存储(02 §3.3 0003 迁移、§2.8.1 写入时机;00 §7.4 不进 text/fingerprint…)

class TestF_Storage:
    def test_F1_migration_file_and_baseline_untouched(self):
        """02 §3.3:0003_message_reply.sql 全文一句 ALTER TABLE … ADD COLUMN reply_json …;§3.1 块外说明:schema_agent.sql 不含 reply_json。"""
        mig = REPO / "src/qtrade_agent/store/migrations/0003_message_reply.sql"
        body = " ".join(mig.read_text(encoding="utf-8").split())
        assert body == "ALTER TABLE messages ADD COLUMN reply_json TEXT CHECK (reply_json IS NULL OR json_valid(reply_json));"
        assert "reply_json" not in (REPO / "src/qtrade_agent/store/schema_agent.sql").read_text(encoding="utf-8")
        assert "reply_json" not in (REPO / "src/qtrade_agent/store/migrations/0002_native_identity_epoch.sql").read_text(encoding="utf-8")

    def test_F2_migration_on_0001_0002_db_old_rows_null(self, tmp_path, clock):
        """02 §3.3:在已有 0001+0002 的库上应用 0003 后老行 reply_json IS NULL;列带 json_valid 约束。
        构造法:先正常建库并写一条老消息,再把库退回「未应用 0003」(删列 + 删 schema_version 里 0003 那行),重开触发迁移。"""
        path = str(tmp_path / "old_agent.db")
        s = Store(path, clock=clock).open()
        s.ensure_account("qd01", "qidian", state="running", self_uid=SELF_UID)
        old_id = s.ingest(Message("qd01", "qidian", Session("qd01", PEER_A, "private"), "in", "text", "升级前的老消息", clock(), "qidian_db",
                                  ext_msg_id="qd:760001", dedup_kind="native", sender_id=PEER_A)).id
        s.close()
        con = sqlite3.connect(path)
        v = [r for r in con.execute("SELECT version, name FROM schema_version") if "0003" in str(r[1])]
        assert len(v) == 1, "schema_version 里应有且仅有一行 0003"
        con.execute("ALTER TABLE messages DROP COLUMN reply_json")
        con.execute("DELETE FROM schema_version WHERE version=?", (v[0][0],))
        con.commit()
        assert "reply_json" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        con.close()
        s2 = Store(path, clock=clock).open()
        try:
            assert "reply_json" in {r[1] for r in s2.con.execute("PRAGMA table_info(messages)")}
            assert s2.con.execute("SELECT reply_json FROM messages WHERE id=?", (old_id,)).fetchone()[0] is None
            assert [r for r in s2.con.execute("SELECT name FROM schema_version") if "0003_message_reply" in str(r[0])]
            with pytest.raises(sqlite3.IntegrityError):
                s2.con.execute("UPDATE messages SET reply_json='{bad json' WHERE id=?", (old_id,))
        finally:
            s2.close()

    def test_F3_text_fingerprint_hash_unchanged_by_reply(self, tmp_path):
        """00 §7.4 / 06 §2.9.5:引用内容绝不落入 text,不进 fingerprint/text_hash/FTS/去重键——有无引用不改变这几项的任何取值。
        两套完全相同的环境,唯一差别是 extStr 有无引用。"""
        from conftest import Clock

        def run(sub: str, ext: Optional[bytes]) -> dict:
            d = tmp_path / sub
            d.mkdir()
            clk = Clock()
            st = Store(":memory:", clock=clk).open()
            st.ensure_account("qd01", "qidian", state="running", self_uid=SELF_UID)
            db = ReplyMainDb(str(d / f"{SELF_UID}.db"))
            p = make_poller(st, clk, str(d))
            p.poll_maindb(acct_view())
            clk.advance(5_000)
            db.insert(PEER_A, time_s=clk.now_s, body="@张三 好的 2.20", msgtype=-1049, uniseq=761001, ext=ext)
            p.poll_maindb(acct_view())
            r = msg_row(st, "qd:761001")
            fts = st.con.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?", (f'"{SENTINEL}"',)).fetchone()[0]
            st.close()
            return {**{k: r[k] for k in ("text", "text_len", "text_hash", "fingerprint", "ext_msg_id", "ts_ms", "sender_id", "dedup_kind")},
                    "fts_hits": fts, "reply_json": r["reply_json"]}

        with_r = run("with", ext_reply(build_info(ok_values())))
        without = run("without", None)
        assert json.loads(with_r["reply_json"])["state"] == "ok" and without["reply_json"] is None
        for k in ("text", "text_len", "text_hash", "fingerprint", "ext_msg_id", "ts_ms", "sender_id", "dedup_kind"):
            assert with_r[k] == without[k], k
        assert with_r["text"] == "@张三 好的 2.20" and SENTINEL not in with_r["text"]
        assert with_r["fts_hits"] == 0

    def test_F4_rescan_existing_row_not_changed_no_event(self, store, clock, rdb, poller):
        """02 §2.8.1 ③:其它已存在的行(含撞去重键的重扫行)一律不改;reply 写入/补写不算 changed、不发事件。
        注入:主库里把两行的 extStr 改掉(一行由无引用改为有引用、一行换成另一份引用),再把水位 last_uniseq 篡改逼它从 0 重扫。"""
        i1 = rdb.insert(PEER_A, time_s=clock.now_s, body="原本无引用", uniseq=762001, ext=None)
        i2 = rdb.insert(PEER_A, time_s=clock.now_s, body="原本有引用", uniseq=762002,
                        ext=ext_reply(build_info(ok_values(mSourceMsgText="第一份摘要"))))
        poll(poller)
        r2_before = stored_reply(store, "qd:762002")
        assert stored_reply(store, "qd:762001") is None and r2_before["text"] == "第一份摘要"
        n_ev = len(msg_events(store))
        rdb.set_ext(PEER_A, i1, ext_reply(build_info(ok_values())))
        rdb.set_ext(PEER_A, i2, ext_reply(build_info(ok_values(mSourceMsgText="第二份摘要"))))
        cur = store.cursor_get("qd01", "qidian_rowid:" + PEER_A)
        store.cursor_set("qd01", "qidian_rowid:" + PEER_A, cur.value_int, json.dumps({"last_uniseq": 999_999_999}))
        clock.advance(5_000)
        poll(poller)
        assert stored_reply(store, "qd:762001") is None
        assert stored_reply(store, "qd:762002") == r2_before
        assert len(msg_events(store)) == n_ev
        assert store.count_messages("qd01") == 2

    def test_F5_out_merge_backfills_null_reply(self, store, clock, rdb, poller):
        """02 §2.8.1 ②:出向读回合并到既有 SENDING 行时,既有行 reply_json 为 NULL 则补写;不插第二行;
        合并本身照常只发一条 message 事件(reply 补写不额外发事件),事件里带同一个 reply。"""
        text = "报价 1Y 1.70"
        m = Message(account_id="qd01", channel="qidian", session=Session("qd01", PEER_A, "private"), dir="out", type="text", text=text,
                    ts_ms=clock(), source="ui", ext_msg_id=None, self=True, state="SENDING", trace_id="tr_reply_1", idempotency_key="idem_r1")
        out_id = store.ingest(m).id
        assert store.get_message(out_id)["reply_json"] is None
        rdb.insert(PEER_A, time_s=clock.now_s + 3, body=text, issend=1, msgtype=-1049, uniseq=763001, ext=ext_reply(build_info(ok_values())))
        clock.advance(4_000)
        poll(poller)
        rows = store.con.execute("SELECT id, ext_msg_id, state, reply_json FROM messages WHERE account_id='qd01'").fetchall()
        assert len(rows) == 1 and rows[0]["id"] == out_id
        assert rows[0]["ext_msg_id"] == "qd:763001" and rows[0]["state"] == "DELIVERED"
        rep = json.loads(rows[0]["reply_json"])
        assert rep["state"] == "ok" and rep["text"] == f"原报价摘要 {SENTINEL}"
        evs = msg_events(store)
        assert len(evs) == 1 and evs[0]["id"] == out_id
        assert evs[0]["reply"] is not None and evs[0]["reply"]["state"] == "ok"

    def test_F6_reference_text_not_used_for_out_merge(self, store, clock, rdb, poller):
        """00 §7.4:引用内容不进出向确认匹配——SENDING 行文本 = 引用摘要、读回行正文不同 ⇒ 不合并。"""
        summary = "这是被引用的原文"
        m = Message(account_id="qd01", channel="qidian", session=Session("qd01", PEER_A, "private"), dir="out", type="text", text=summary,
                    ts_ms=clock(), source="ui", ext_msg_id=None, self=True, state="SENDING", trace_id="tr_reply_2", idempotency_key="idem_r2")
        out_id = store.ingest(m).id
        rdb.insert(PEER_A, time_s=clock.now_s + 2, body="另一句正文", issend=1, msgtype=-1049, uniseq=763101,
                   ext=ext_reply(build_info(ok_values(mSourceMsgText=summary))))
        clock.advance(3_000)
        poll(poller)
        assert store.get_message(out_id)["state"] == "SENDING" and store.get_message(out_id)["ext_msg_id"] is None
        assert store.count_messages("qd01") == 2

    def test_F7_capture_text_off_reply_text_null(self, tmp_path):
        """总控补充口径②:capture_text=false 时 reply.text 入库为 null(state 等其余键照常)。"""
        from conftest import Clock
        clk = Clock()
        st = Store(":memory:", clock=clk, capture_text=False).open()
        try:
            st.ensure_account("qd01", "qidian", state="running", self_uid=SELF_UID)
            db = ReplyMainDb(str(tmp_path / f"{SELF_UID}.db"))
            p = make_poller(st, clk, str(tmp_path))
            p.poll_maindb(acct_view())
            clk.advance(5_000)
            db.insert(PEER_A, time_s=clk.now_s, uniseq=764001, ext=ext_reply(build_info(ok_values())))
            p.poll_maindb(acct_view())
            rep = stored_reply(st, "qd:764001")
            assert rep["state"] == "ok" and rep["text"] is None
            assert rep["source_msg_seq"] == str(SEQ) and rep["source_msg_uid"] == str(UID_BIG)
            assert SENTINEL not in (msg_row(st, "qd:764001")["reply_json"] or "")
        finally:
            st.close()

    def test_F8_minus_1049_body_unchanged(self, store, clock, rdb, poller):
        """06 §2.9.5 路由表 -1049 行(R6-116 勘误):正文解码不变——整体 UTF-8 → clean_text;引用不在 msgData,不拼进 text。"""
        body = "@张三 好的\u0014A"
        rdb.insert(PEER_A, time_s=clock.now_s, body=body, msgtype=-1049, uniseq=765001, ext=ext_reply(build_info(ok_values())))
        rdb.insert(PEER_A, time_s=clock.now_s, body=body, msgtype=-1049, uniseq=765002, ext=None)
        poll(poller)
        assert msg_row(store, "qd:765001")["text"] == msg_row(store, "qd:765002")["text"] == "@张三 好的[爱你]"
        assert msg_row(store, "qd:765001")["type"] == "text"


# ====================================================================== G. 失败语义 · 日志(06 §2.9.5「失败语义」;§8b M2 R6-116 行「日志不含引用内容」)

def _log_scenario(tmpdir: str) -> dict:
    """供子进程调用(每进程首见集合必须从零开始):同一原因 3 行(类名不符)、另一原因 2 行(尾随字节)、1 行正常 ok、1 行无引用分支。"""
    from conftest import Clock
    clk = Clock()
    st = Store(":memory:", clock=clk).open()
    st.ensure_account("qd01", "qidian", state="running", self_uid=SELF_UID)
    db = ReplyMainDb(os.path.join(tmpdir, f"{SELF_UID}.db"))
    p = make_poller(st, clk, tmpdir)
    p.poll_maindb(acct_view())
    clk.advance(5_000)
    bad_cls = build_info(ok_values(), class_name=CLASS + "Y")
    bad_tail = build_info(ok_values(), trailing=b"\x00\x00")
    good = build_info(ok_values())
    for rid in (101, 102, 103):
        db.insert(PEER_A, time_s=clk.now_s, msgtype=-1049, uniseq=770000 + rid, ext=ext_reply(bad_cls), row_id=rid)
    for rid in (201, 202):
        db.insert(PEER_A, time_s=clk.now_s, msgtype=-1049, uniseq=770000 + rid, ext=ext_reply(bad_tail), row_id=rid)
    db.insert(PEER_A, time_s=clk.now_s, msgtype=-1049, uniseq=770301, ext=ext_reply(good), row_id=301)
    db.insert(PEER_A, time_s=clk.now_s, msgtype=-1000, uniseq=770401, ext=ext_json({"qidian_svr_msg": "x"}), row_id=401)
    p.poll_maindb(acct_view())
    states = {r["ext_msg_id"]: (json.loads(r["reply_json"]) if r["reply_json"] else None)
              for r in st.con.execute("SELECT ext_msg_id, reply_json FROM messages")}
    st.close()
    secrets = [SENTINEL]
    for s in (bad_cls, bad_tail, good):
        secrets += [s.hex()[8:72], s.hex().upper()[8:72], ext_reply(s).hex()[:64], ext_reply(s).hex().upper()[:64]]
    return {"table": table_name(PEER_A), "states": {k: (v or {}).get("state") for k, v in states.items()}, "secrets": secrets}


class TestG_FailureLogs:
    def test_G1_warning_once_per_reason_no_content(self, tmp_path):
        """06 §2.9.5 失败语义:每个进程按失败原因各打一次 WARNING,日志只含表名、_id、原因,**不含引用内容与原始字节**;
        无引用分支不告警;正常 ok 行也不得把引用内容写进日志(§8b M2 R6-116 行)。在**全新子进程**里跑,保证「每进程首见」从零开始。"""
        script = (
            "import sys, json, logging, importlib.util\n"
            f"sys.path[:0] = [{str(REPO / 'src')!r}, {str(REPO / 'tests')!r}]\n"
            f"spec = importlib.util.spec_from_file_location('reply_acc', {str(Path(__file__).resolve())!r})\n"
            "mod = importlib.util.module_from_spec(spec); sys.modules['reply_acc'] = mod; spec.loader.exec_module(mod)\n"
            "recs = []\n"
            "class H(logging.Handler):\n"
            "    def emit(self, r):\n"
            "        recs.append({'lvl': r.levelno, 'name': r.name, 'msg': r.getMessage()})\n"
            "root = logging.getLogger(); root.addHandler(H()); root.setLevel(logging.INFO)\n"
            f"out = mod._log_scenario({str(tmp_path)!r})\n"
            "print('@@JSON@@' + json.dumps({'recs': recs, **out}, ensure_ascii=False))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO), capture_output=True, text=True, timeout=300,
                              encoding="utf-8")
        assert proc.returncode == 0, proc.stderr[-4000:]
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("@@JSON@@")][-1]
        data = json.loads(line[len("@@JSON@@"):])
        st = data["states"]
        assert [st[f"qd:{770000 + i}"] for i in (101, 102, 103, 201, 202)] == ["unparsed"] * 5
        assert st["qd:770301"] == "ok" and st["qd:770401"] is None
        all_text = "\n".join(r["msg"] for r in data["recs"])
        for s in data["secrets"]:
            assert s not in all_text, f"日志里出现了引用内容或原始字节片段:{s[:16]}…"
        warns = [r["msg"] for r in data["recs"] if r["lvl"] >= logging.WARNING]

        def has_id(w: str, ids) -> bool:
            body = w.replace(data["table"], " ")
            return any(re.search(rf"(?<![0-9]){i}(?![0-9])", body) for i in ids)

        same_reason = [w for w in warns if has_id(w, (101, 102, 103))]
        other_reason = [w for w in warns if has_id(w, (201, 202))]
        assert len(same_reason) == 1, warns            # 同一原因 3 行只告一次
        assert len(other_reason) <= 1, warns
        assert 1 <= len(warns) <= 2, warns             # 两种失败原因各至多一次;无引用分支、ok 行不告警
        for w in warns:
            assert data["table"] in w, w               # 日志带表名
        assert not any(has_id(w, (301, 401)) for w in warns), warns


# ====================================================================== H. 视图:GET /messages、事件同形、QQ/微信恒 null(00 §7.4;02 #48/#49、§3.4.7)

@pytest.fixture
def app(tmp_path, clock):
    from starlette.testclient import TestClient
    from qtrade_agent.app import AgentApp
    from qtrade_agent.runtime.backends import FakeAdb, FakeContainers
    from qtrade_agent.winagent_client import FakeWinAgent
    db = ReplyMainDb(str(tmp_path / f"{SELF_UID}.db"))
    wa = FakeWinAgent()
    agent = AgentApp(AgentConfig(), db_path=str(tmp_path / "agent.db"), clock=clock,
                     maindb_factory=lambda uid, acct: LocalSqliteMainDb(db.path), adb=FakeAdb(), containers=FakeContainers(),
                     winagent_transport=wa, winagent_base_url="http://winagent.fake:17610", winagent_token=wa.token).open()
    s = agent.store
    s.ensure_account("qd01", "qidian", state="running", self_uid=SELF_UID)
    s.ensure_account("qq03", "qq", state="running", login_mode="qrcode", self_uid="520009002")
    s.ensure_account("wx01", "wechat", state="running", login_mode="qrcode")
    s.upsert_api_client(app_id="bot_reply_r", name="读", level="read", token=TOK_R)
    with TestClient(agent.create_api(), client=("127.0.0.1", 40000)) as c:
        try:
            yield SimpleNamespace(agent=agent, store=s, db=db, c=c, clock=clock)
        finally:
            try:
                c.portal.call(agent.bus.close)
            except Exception:
                pass
    s.close()


def _get_all(app, **params) -> list[dict]:
    r = app.c.get("/api/v1/messages", params={"limit": 100, **params}, headers={"Authorization": f"Bearer {TOK_R}"})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _poll_app(app) -> None:
    app.c.portal.call(app.agent.qidian_poll_all)


class TestH_View:
    def test_H1_get_and_event_same_shape(self, app):
        """00 §7.4:reply 键恒在(null 或对象);对象九个取值键恒在;sender 恒为对象;ID 一律字符串;ts ISO 8601 +08:00,
        来源秒数缺失时为 null;unparsed 时除 state 外全 null;GET /messages(#48)、GET /messages/{id}(#49)与 WS message 事件同形。"""
        clk, db = app.clock, app.db
        _poll_app(app)
        clk.advance(5_000)
        db.insert(PEER_A, time_s=clk.now_s, body="原文 1Y 1.70\n第二行\n第三行", uniseq=780001, shmsgseq=SEQ, msg_uid=UID_BIG)
        _poll_app(app)
        clk.advance(5_000)
        db.insert(PEER_A, time_s=clk.now_s, body="@张三 2.20", msgtype=-1049, uniseq=780002,
                  ext=ext_reply(build_info(ok_values(senderName="张三"))))
        db.insert(PEER_A, time_s=clk.now_s, body="坏引用", msgtype=-1049, uniseq=780003,
                  ext=ext_reply(build_info(ok_values(), trailing=b"\x01")))
        db.insert(PEER_A, time_s=clk.now_s, body="无引用", uniseq=780004)
        db.insert(PEER_A, time_s=clk.now_s, body="缺省值引用", msgtype=-1049, uniseq=780005,
                  ext=ext_reply(build_info(ok_values(mSourceMsgSenderUin=0, mSourceMsgTime=0, mSourceMsgSeq=0, origUid=0,
                                                     mSourceMsgText=None))))
        _poll_app(app)
        items = {m["ext_msg_id"]: m for m in _get_all(app, account_id="qd01")}
        evs = {p["ext_msg_id"]: p for p in msg_events(app.store)}
        assert {"qd:780001", "qd:780002", "qd:780003", "qd:780004", "qd:780005"} <= set(items)
        for m in items.values():
            assert "reply" in m
        orig_id = items["qd:780001"]["id"]

        ok = items["qd:780002"]["reply"]
        assert set(ok) == VIEW_KEYS and set(ok["sender"]) == {"id", "name"}
        assert ok == {"state": "ok", "text": f"原报价摘要 {SENTINEL}", "sender": {"id": PEER_A, "name": "张三"},
                      "ts": iso8(1_758_239_900_000), "source_msg_seq": str(SEQ), "source_msg_uid": "9007199254740993",
                      "source_ext_msg_id": "qd:780001", "resolved_message_id": orig_id}
        assert items["qd:780002"]["text"] == "@张三 2.20"

        bad = items["qd:780003"]["reply"]
        assert bad == {"state": "unparsed", "text": None, "sender": {"id": None, "name": None}, "ts": None, "source_msg_seq": None,
                       "source_msg_uid": None, "source_ext_msg_id": None, "resolved_message_id": None}
        assert items["qd:780003"]["text"] == "坏引用"

        assert items["qd:780004"]["reply"] is None and items["qd:780001"]["reply"] is None

        dflt = items["qd:780005"]["reply"]
        assert dflt["state"] == "ok" and set(dflt) == VIEW_KEYS
        assert dflt["sender"] == {"id": None, "name": None}
        assert dflt["ts"] is None and dflt["text"] is None and dflt["source_msg_seq"] is None and dflt["source_msg_uid"] is None
        assert dflt["source_ext_msg_id"] is None and dflt["resolved_message_id"] is None

        for k, m in items.items():
            assert k in evs, k
            assert "reply" in evs[k], k
            assert evs[k]["reply"] == m["reply"], k
        one = app.c.get(f"/api/v1/messages/{items['qd:780002']['id']}", headers={"Authorization": f"Bearer {TOK_R}"})
        assert one.status_code == 200
        body = one.json()
        single = body.get("data", body.get("message", body))
        assert single["reply"] == ok

    def test_H2_qq_wechat_reply_always_null(self, app):
        """00 §7.4 / 02 #48:QQ、微信本轮恒 reply: null(键在、值 null);事件 payload 同样带 reply: null。"""
        from qtrade_agent.events import message_payload
        s, clk = app.store, app.clock
        qq = Message("qq03", "qq", Session("qq03", "g_456", "group", "测试群"), "in", "text", "QQ 消息", clk(), "onebot",
                     ext_msg_id="g_456:501", sender_id="10001", sender_name="张三")
        wx = Message("wx01", "wechat", Session("wx01", "talker_x", "private"), "in", "text", "微信消息", clk(), "chatlog",
                     ext_msg_id="talker_x:1", sender_id="wxid_x")
        s.ingest(qq)
        s.ingest(wx)
        for acct in ("qq03", "wx01"):
            items = _get_all(app, account_id=acct)
            assert len(items) == 1 and "reply" in items[0] and items[0]["reply"] is None, acct
        for m in (qq, wx):
            p = message_payload(m, late_after_s=120)
            assert "reply" in p and p["reply"] is None


# ====================================================================== I. clean_text 后为空串 → 视为空(06 §2.9.5 字段映射表 text / sender_name 行,2026-10-28 补钉)
# 只用会被 clean_text ② 丢弃的控制字符(<U+0020,且不是 \t \n \r,也不是 U+0014——前者保留、后者变表情占位)。
CTRL_ONLY = ["\x01\x02", "\x07\x1b\x1f", "\x08"]


class TestI_CleanTextEmpty:
    @pytest.mark.parametrize("ctrl", CTRL_ONLY, ids=["01-02", "07-1b-1f", "08"])
    def test_I1_text_only_dropped_ctrl_is_null(self, store, clock, rdb, poller, ctrl):
        """06 §2.9.5 字段映射 text:「原值为空串/null、或 clean_text 后为空串 → null」——摘要只含被丢弃的控制字符 ⇒ state="ok"、text=null;
        正文 text 不受影响;事件 payload 的 reply 同样 text=null。"""
        body = "正文照常 1Y 1.70"
        rdb.insert(PEER_A, time_s=clock.now_s, body=body, msgtype=-1049, uniseq=790001,
                   ext=ext_reply(build_info(ok_values(mSourceMsgText=ctrl, senderName="合成名乙"))))
        poll(poller)
        r = msg_row(store, "qd:790001")
        assert r is not None and r["text"] == body
        rep = json.loads(r["reply_json"])
        assert set(rep) == STORE_KEYS
        assert rep["state"] == "ok" and rep["text"] is None
        assert rep["sender_id"] == PEER_A and rep["sender_name"] == "合成名乙" and rep["source_msg_seq"] == str(SEQ)
        ev = [p for p in msg_events(store) if p["ext_msg_id"] == "qd:790001"]
        assert len(ev) == 1 and ev[0]["text"] == body
        assert ev[0]["reply"]["state"] == "ok" and ev[0]["reply"]["text"] is None

    @pytest.mark.parametrize("ctrl", CTRL_ONLY, ids=["01-02", "07-1b-1f", "08"])
    def test_I2_sender_name_only_dropped_ctrl_falls_back(self, store, clock, rdb, poller, ctrl):
        """06 §2.9.5 字段映射 sender_name:「依次取，第一个经 clean_text 后非空的值(…clean_text 后为空串，都视为空、再取下一个)」——
        senderName 只含被丢弃的控制字符、mSourceQdMsgUserName 为正常合成名 ⇒ 取后者;视图 sender.name 同值;正文不受影响。"""
        body = "正文照常 2Y 1.80"
        rdb.insert(PEER_A, time_s=clock.now_s, body=body, msgtype=-1049, uniseq=790101,
                   ext=ext_reply(build_info(ok_values(senderName=ctrl, mSourceQdMsgUserName="合成名甲"))))
        poll(poller)
        r = msg_row(store, "qd:790101")
        assert r is not None and r["text"] == body
        rep = json.loads(r["reply_json"])
        assert rep["state"] == "ok" and rep["sender_name"] == "合成名甲"
        assert rep["text"] == f"原报价摘要 {SENTINEL}"
        ev = [p for p in msg_events(store) if p["ext_msg_id"] == "qd:790101"]
        assert len(ev) == 1 and ev[0]["text"] == body
        assert ev[0]["reply"]["sender"] == {"id": PEER_A, "name": "合成名甲"}

    def test_I3_both_names_only_dropped_ctrl_null(self, store, clock, rdb, poller):
        """06 §2.9.5 字段映射 sender_name:两者经 clean_text 后都为空串 ⇒「两者都为空 → null」;state 仍 ok。"""
        rdb.insert(PEER_A, time_s=clock.now_s, body="正文照常", msgtype=-1049, uniseq=790201,
                   ext=ext_reply(build_info(ok_values(senderName="\x01\x02", mSourceQdMsgUserName="\x07\x1f"))))
        poll(poller)
        rep = stored_reply(store, "qd:790201")
        assert rep["state"] == "ok" and rep["sender_name"] is None
        assert msg_row(store, "qd:790201")["text"] == "正文照常"
