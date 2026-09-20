"""企点旁路读库正线 —— 按设计文档撰写的验收用例(独立于实现与开发者自测)。

规格出处(只认这些):
- docs/06 §2.9.2「去重键」里 norm() 的可抄定义与分工(R6-47)
- docs/06 §2.9.5 全节:列编码 XOR(17 字节)、按会话分表、type/text 按 msgtype 一级路由权威表、clean_text 段(R6-43~R6-46)、
  poll_maindb 伪代码(表发现 / bootstrap / 历史闸 / 水位自检 / 加速轮 / fail 终态 / 重扫不重放)、掉线续读条、check_group_gaps 伪代码
- docs/06 §8b 验收表 M2 企点各行
- docs/02 §3.7 QIDIAN_DB_UNAVAILABLE / QIDIAN_TABLE_DECODE_STUCK / QIDIAN_MSG_GAP 三行
- docs/00 §7.4 事件专属 lag_s / late / origin

写法约定:每个用例顶部注释写清条款;断言只写规格说的可观测结果(库里几行、事件几条、告警码与 evidence、水位值、text 逐字)。
🔴 断言不按实现反推;失败即视为「实现缺陷或规格歧义」,在报告里逐条判定。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

import pytest

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.msgdata import MainDbRow, MessageFactory, decode, decode_mixed
from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
from qtrade_agent.adapters.qidian.xor import KEY, decode_uin, xor, xor_hex
from qtrade_agent.alerts import QIDIAN_DB_UNAVAILABLE, QIDIAN_MSG_GAP, QIDIAN_TABLE_DECODE_STUCK, Alerts
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Message, Session
from qtrade_agent.text import FACE_PLACEHOLDER, IMAGE_PLACEHOLDER, clean_text, has_control_chars, norm

SELF_UID = "3007373675"          # conftest 里 qd01 的登录 uin
PEER_A = "415011447"             # 06 §2.9.5 例子里的单聊对端
PEER_B = "520001234"
GROUP_G = "123456"               # 06 §2.9.5 例子里的群号(native_id = g_123456)
DB_DIR = "/data/data/com.tencent.qidian/databases/"
FACE = "\u0014"


# ====================================================================== 公共小工具(只造数据、不碰实现内部)

def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().upper()


def table_name(peer: str, group: bool = False) -> str:
    """06 §2.9.5 按会话分表:单聊 mr_friend_{MD5(对端uin)大写}_New、群 mr_troop_{MD5(群号)大写}_New。"""
    return f"mr_{'troop' if group else 'friend'}_{md5_upper(peer)}_New"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def ld(field_no: int, payload: bytes) -> bytes:
    """protobuf length-delimited 字段。"""
    return _varint((field_no << 3) | 2) + _varint(len(payload)) + payload


def elem_text(s: str) -> bytes:
    """-1035 的文本 Elem:顶层 field 1 = Elem,文本段取 Elem.1.1(06 §2.9.5 路由表)。"""
    return ld(1, ld(1, ld(1, s.encode("utf-8"))))


def elem_image() -> bytes:
    """-1035 的图片 Elem:非文本段,含 06 §2.9.5 列出的内容特征(picplatform / /download?appid= / /gchatpic_new/)与宽高字段。"""
    meta = ld(1, b"/gchatpic_new/3007373675/0-0-ABCDEF/0?picplatform=1&/download?appid=1407") + ld(2, _varint(300)) + ld(3, _varint(200))
    return ld(1, ld(2, meta))


def row(msgtype: int, data: bytes, *, id: int = 1, issend: int = 0, time: int = 1_758_240_000, uniseq: int = 7001, sender: str = PEER_A) -> MainDbRow:
    """按 06 §2.9.5 ④ 的 SELECT 列造一行:senderuin/msgData 都是 XOR 后的 hex。"""
    return MainDbRow(id=id, issend=issend, time=time, msgtype=msgtype, uniseq=uniseq,
                     senderuin_hex=xor(sender.encode()).hex().upper(), msgdata_hex=xor(data).hex().upper())


def new_poller(store, clock, factory, cfg: AgentConfig | None = None):
    """造一台 poller;返回 (poller, alerts, h13 开关字典)。"""
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    h13 = {"on": False}
    p = QidianPoller(store=store, events=events, alerts=alerts, cfg=cfg or AgentConfig(),
                     h13_firing=lambda: h13["on"], clock=clock, maindb_factory=factory)
    return p, alerts, h13


def acct_view(state: str = "running", self_uid: str | None = SELF_UID, id: str = "qd01") -> QidianAccountView:
    return QidianAccountView(id=id, state=state, self_uid=self_uid)


def msg_events(store, account_id: str = "qd01") -> list[dict]:
    return [e["payload"] for e in store.list_events(event="message", account_id=account_id)]


def alert_events(store, code: str, state: str | None = None) -> list[dict]:
    out = []
    for e in store.list_events(event="alert"):
        p = e["payload"]
        if p.get("code") == code and (state is None or p.get("state") == state):
            out.append(p)
    return out


def all_messages(store, account_id: str = "qd01") -> list[dict]:
    return [dict(r) for r in store.con.execute("SELECT * FROM messages WHERE account_id=? ORDER BY received_ms, id", (account_id,)).fetchall()]


def rowid_cursors(store, owner: str = "qd01") -> dict[str, tuple]:
    return {c.kind: (c.value_int, c.value_json()) for c in store.cursors_list(owner, "qidian_rowid:")}


def create_raw_table(path: str, name: str, *, with_msgdata: bool = True, peer: str = PEER_A, istroop: int = 0, time_s: int = 1_758_240_000) -> None:
    """手工造一张会话表(用于「表名 MD5 自检不过」或「缺列」两种故障注入),并插 1 行让它非空。"""
    cols = "_id INTEGER PRIMARY KEY AUTOINCREMENT, issend INTEGER, istroop INTEGER, time INTEGER, msgtype INTEGER, uniseq INTEGER, msgseq INTEGER, shmsgseq INTEGER, senderuin BLOB, frienduin BLOB"
    if with_msgdata:
        cols += ", msgData BLOB"
    con = sqlite3.connect(path)
    con.execute(f'CREATE TABLE "{name}" ({cols})')
    if with_msgdata:
        con.execute(f'INSERT INTO "{name}"(issend,istroop,time,msgtype,uniseq,msgseq,shmsgseq,senderuin,frienduin,msgData) VALUES (0,?,?,-1000,90001,1,1,?,?,?)',
                    (istroop, time_s, xor(peer.encode()), xor(peer.encode()), xor("x".encode())))
    else:
        con.execute(f'INSERT INTO "{name}"(issend,istroop,time,msgtype,uniseq,msgseq,shmsgseq,senderuin,frienduin) VALUES (0,?,?,-1000,90001,1,1,?,?)',
                    (istroop, time_s, xor(peer.encode()), xor(peer.encode())))
    con.commit()
    con.close()


def drop_table(path: str, name: str) -> None:
    con = sqlite3.connect(path)
    con.execute(f'DROP TABLE IF EXISTS "{name}"')
    con.commit()
    con.close()


def out_sending(store, clock, text: str, *, peer: str = PEER_A, trace_id: str = "tr_0001", state: str = "SENDING", ts_ms: int | None = None) -> str:
    """06 §2.12:总线调通道前先写 dir=out/self=true/state=SENDING/ext_msg_id=null/trace_id 的行,text = 发送原文(不过 clean_text)。
    超时态(UNCONFIRMED/FAILED)是 bus 对同一行的 UPDATE(§2.12「state 只对 dir=out 有意义:SENDING → …」),故先落 SENDING 再翻态。"""
    m = Message(account_id="qd01", channel="qidian", session=Session("qd01", peer, "private"), dir="out", type="text", text=text,
                ts_ms=ts_ms if ts_ms is not None else clock(), source="ui", ext_msg_id=None, self=True, state="SENDING",
                trace_id=trace_id, idempotency_key="idem_" + trace_id)
    r = store.ingest(m)
    assert r.inserted
    if state != "SENDING":
        store.mark_out_state(r.id, state)
        assert store.get_message(r.id)["state"] == state
    return r.id


@pytest.fixture
def local_factory(maindb):
    return lambda uid: LocalSqliteMainDb(maindb.path)


@pytest.fixture
def poller(store, clock, local_factory):
    p, alerts, h13 = new_poller(store, clock, local_factory)
    p.alerts_ = alerts
    p.h13_ = h13
    return p


# ====================================================================== A. norm()(06 §2.9.2,R6-47)

class TestA_Norm:
    def test_A1_none_and_empty(self):
        """§2.9.2 R6-47:norm(None) == norm("") == "";§2.12 空文本守卫的「为空」按这个返回值判。"""
        assert norm(None) == ""
        assert norm("") == ""
        assert norm("   \t\n ") == ""

    def test_A2_nfkc(self):
        """§2.9.2 R6-47:只做三件事之一 NFKC —— 全角字母/全角空格被抹平(§2.12:「全角/半角与组合字符(NFKC 抹平)」)。"""
        assert norm("收到　ＯＫ") == "收到 OK"
        assert norm("１Ｙ") == "1Y"

    def test_A3_collapse_and_strip(self):
        """§2.9.2 R6-47:折叠一切空白(含 \\t\\n\\r 与全角空格 NFKC 后的 U+0020)为单个空格,并去首尾空白。"""
        assert norm("1Y\n1.70\n2Y 1.80") == "1Y 1.70 2Y 1.80"
        assert norm("  a \t\t b\r\n c  ") == "a b c"

    def test_A4_keeps_u0014(self):
        """§2.9.2 R6-47:「不剥 U+0014(那是 clean_text 的事)」。"""
        assert norm("收到\u0014A") == "收到\u0014A"
        assert "\u0014" in norm("\u0014A")

    def test_A5_keeps_placeholders(self):
        """§2.9.2 R6-47 分工:norm 不改 [表情]/[图片] 字面量、不截断。"""
        s = "[图片]报价 1.70[表情]" + "x" * 5000
        assert norm(s) == s

    def test_A6_idempotent(self):
        """§2.9.2 R6-47:「NFKC 是幂等的,norm(norm(s)) == norm(s) 成立」。"""
        for s in ["收到　ＯＫ", " a\tb ", "\u0014A  b", "ｶﾞ"]:
            assert norm(norm(s)) == norm(s)


# ====================================================================== B. clean_text(06 §2.9.5 clean_text 段,R6-46)

class TestB_CleanText:
    def test_B1_face_to_placeholder(self):
        """§2.9.5 ①:遇到 U+0014,连同其后 1 个字符替换为字面量 [表情];常量拼写 = 「[表情]」/「[图片]」。"""
        assert FACE_PLACEHOLDER == "[表情]"
        assert IMAGE_PLACEHOLDER == "[图片]"
        assert clean_text("收到\u0014A") == "收到[表情]"
        assert clean_text("a\u00148b") == "a[表情]b"

    def test_B2_face_only_not_empty(self):
        """§2.9.5:「17 条消息整条只由表情组成 … 写成 [表情] 后 text 就是 [表情]」,一个/两个小黄脸各出一个。"""
        assert clean_text("\u0014A") == "[表情]"
        assert clean_text("\u0014A\u0014B") == "[表情][表情]"
        assert clean_text("\u0014A\u0014B") != ""

    def test_B3_trailing_lone_face(self):
        """§2.9.5 ①:U+0014 在串尾、没有后继时只丢掉它自己、不写占位。"""
        assert clean_text("你好\u0014") == "你好"
        assert clean_text("\u0014") == ""

    def test_B4_face_consumes_tab(self):
        """§2.9.5 ①:后继是什么都一并消耗——包括 \\t/\\n/\\r(实测 6 个后继恰是 \\t,它们是索引不是正文)。"""
        assert clean_text("a\u0014\tb") == "a[表情]b"
        assert clean_text("a\u0014\nb") == "a[表情]b"
        assert clean_text("a\u0014\rb") == "a[表情]b"

    def test_B5_two_consecutive_faces(self):
        """§2.9.5 ①:连续两个 U+0014 按「不重叠」第二个被当作第一个的索引一并消耗、只出一个 [表情]。"""
        assert clean_text("a\u0014\u0014b") == "a[表情]b"

    def test_B6_index_ge_0x80_char_level(self):
        """§2.9.5:索引码位实测 U+0000~U+00B8,≥ U+0080 时 UTF-8 占 2 字节,必须在字符层面处理、不得切断多字节序列。"""
        s = "a\u0014¸b"
        decoded = s.encode("utf-8").decode("utf-8", errors="replace")     # 与路由表同一条解码路径
        assert clean_text(decoded) == "a[表情]b"
        assert "�" not in clean_text(decoded)
        assert clean_text("报价\u0014¸") == "报价[表情]"

    def test_B7_other_ctrl_dropped_keep_tnr(self):
        """§2.9.5 ②:未被 ① 消耗的其余码位 < U+0020 逐个丢弃(实测 U+0000/U+0003/U+0008),但 \\t \\n \\r 保留。"""
        assert clean_text("a\u0000b\u0003c\u0008d") == "abcd"
        assert clean_text("1Y\n1.70\r\n2Y\t1.80") == "1Y\n1.70\r\n2Y\t1.80"
        assert clean_text("\u001fx\u001e") == "x"

    def test_B8_has_control_chars(self):
        """§2.12 R6-48:入口校验判据 = clean_text(text) == text;放行 \\t\\n\\r、全角、Unicode emoji;拒 U+0014、U+0008。"""
        assert has_control_chars("收到\u0014A") is True
        assert has_control_chars("收到\u0008") is True
        assert has_control_chars("1Y\n1.70\n2Y 1.80") is False
        assert has_control_chars("收到👌") is False
        assert has_control_chars("收到　ＯＫ") is False

    def test_B9_at_text_untouched(self):
        """§2.9.5:-1049 的 @ 就是裸的 @ + 名字明文,没有任何属性控制字节 —— clean_text 不动它。"""
        assert clean_text("@张三 报价 1.70") == "@张三 报价 1.70"


# ====================================================================== C. 列编码 XOR(06 §2.9.5「列编码 = 逐字节 XOR」,R6-36/R6-39)

class TestC_Xor:
    def test_C1_key_is_17_ascii_bytes(self):
        """§2.9.5:密钥 = ASCII 字符串 "02:00:00:00:00:00" 的 17 个字节(含冒号)。"""
        assert KEY == b"02:00:00:00:00:00"
        assert len(KEY) == 17

    def test_C2_bytewise_formula_and_involution(self):
        """§2.9.5:out[i] = in[i] ^ KEY[i % 17];非加密、无盐、恒定 ⇒ 自反。"""
        data = bytes(range(0, 40)) + "报价 1.70".encode("utf-8")
        out = xor(data)
        assert len(out) == len(data)
        for i, b in enumerate(data):
            assert out[i] == b ^ KEY[i % 17]
        assert xor(out) == data
        assert xor(b"") == b""

    def test_C3_hex_and_uin(self):
        """§2.9.5:senderuin/frienduin 列 XOR 后是明文 uin;设备上 hex(col) 取回 ⇒ xor_hex / decode_uin。"""
        enc = xor(PEER_A.encode())
        assert xor_hex(enc.hex()) == PEER_A.encode()
        assert xor_hex(enc.hex().upper()) == PEER_A.encode()
        assert decode_uin(enc) == PEER_A
        assert decode_uin(enc.hex()) == PEER_A

    def test_C4_not_six_byte_mac(self):
        """§2.9.5 R6-39 勘误:按 MAC 原始 6 字节取模的口径是错的——对长度 > 6 的输入两种密钥解出的结果必须不同。"""
        raw_mac = bytes([0x02, 0, 0, 0, 0, 0])
        data = "这是一条超过十七字节的中文报价消息".encode("utf-8")
        wrong = bytes(b ^ raw_mac[i % 6] for i, b in enumerate(data))
        assert xor(data) != wrong


# ====================================================================== D. msgtype 一级路由权威表(06 §2.9.5,R6-43~R6-46)

class TestD_Route:
    @pytest.mark.parametrize("msgtype,text", [(-1000, "收到"), (-1051, "1Y\n1.70\n2Y 1.80"), (-1049, "@张三 报价 1.70")])
    def test_D1_text_family_emits_verbatim(self, msgtype, text):
        """路由表第 2 行:msgtype ∈ {-1000,-1051,-1049} 整体 UTF-8 明文 → 产出、type=text、text 逐字(多行保留换行、@ 保留)。"""
        d = decode(msgtype, text.encode("utf-8"))
        assert d.emit is True
        assert d.type == "text"
        assert d.text == text
        assert d.unknown is False

    def test_D2_text_family_face(self):
        """路由表第 2 行 + clean_text:文本族里的表情 → [表情](按原位置),不含 < U+0020 的字符(\\t\\n\\r 除外)。"""
        d = decode(-1000, "文字\u0014A".encode("utf-8"))
        assert d.emit and d.text == "文字[表情]"
        d2 = decode(-1000, "\u0014A".encode("utf-8"))
        assert d2.emit and d2.text == "[表情]"                      # 纯表情:入库且 text='[表情]',不是空串
        assert not any(ord(c) < 0x20 and c not in "\t\n\r" for c in d.text)

    def test_D3_mixed_order_image_first(self):
        """路由表第 3 行:-1035 各 Elem 按原顺序拼接,先发图后配文字 ⇒ text = "[图片]文字";不含乱码/URL 字样。"""
        d = decode(-1035, elem_image() + elem_text("这是配文"))
        assert d.emit is True and d.type == "text"
        assert d.text == "[图片]这是配文"
        assert "gchatpic" not in d.text and "picplatform" not in d.text and "�" not in d.text

    def test_D3b_mixed_order_text_first(self):
        """路由表第 3 行:🔴 必须按 Elem 原顺序、不得把占位符统一追加到末尾——文字在前即 "文字[图片]";两图两文各归其位。"""
        assert decode(-1035, elem_text("先说") + elem_image()).text == "先说[图片]"
        assert decode(-1035, elem_text("一") + elem_image() + elem_text("二") + elem_image()).text == "一[图片]二[图片]"
        assert decode_mixed(elem_image() + elem_text("x") + elem_image()) == "[图片]x[图片]"

    def test_D3c_mixed_text_segment_goes_through_clean_text(self):
        """路由表第 3 行:「文本段同样过 clean_text」。"""
        assert decode(-1035, elem_image() + elem_text("配文\u0014A")).text == "[图片]配文[表情]"

    def test_D4_mixed_without_text_not_emitted(self):
        """路由表第 3 行:有文本段才产出(全库 91 条里 6 条纯图片、无文本段 ⇒ 不产出);不计 unknown。"""
        d = decode(-1035, elem_image() + elem_image())
        assert d.emit is False
        assert d.unknown is False
        assert decode_mixed(elem_image()) is None

    @pytest.mark.parametrize("msgtype", [-2017, -2011])
    def test_D5_java_serial_registered_types(self, msgtype):
        """路由表第 1 行:b[:4] == AC ED 00 05(-2017 群文件 / -2011 链接卡片)→ 不产出;登记类型不计 unknown。"""
        d = decode(msgtype, b"\xac\xed\x00\x05" + b"sr\x00\x0eTroopFileData")
        assert d.emit is False
        assert d.unknown is False

    def test_D5b_java_serial_unregistered_type_counts_unknown(self):
        """路由表第 1 行:判据是内容魔数、先命中先用——msgtype ∉ {-2017,-2011} 的 Java 序列化行同「其它」行:不产出 + 计数(unknown)。"""
        d = decode(-1000, b"\xac\xed\x00\x05" + "假装是文本".encode())
        assert d.emit is False
        assert d.unknown is True
        d2 = decode(-9999, b"\xac\xed\x00\x05abc")
        assert d2.emit is False and d2.unknown is True

    @pytest.mark.parametrize("msgtype", [-2000, -2006, -5040, -2018])
    def test_D6_silent_types(self, msgtype):
        """路由表第 4 行:{-2000,-2006,-5040,-2018}(群图片/固定系统串/系统提示/会话事件)→ 不产出;不是「其它」行、不计 unknown。"""
        d = decode(msgtype, "任意内容".encode("utf-8"))
        assert d.emit is False
        assert d.unknown is False

    @pytest.mark.parametrize("msgtype", [-2002, -2022, -2007, 0, 1, -1234])
    def test_D7_other_types_unknown(self, msgtype):
        """路由表第 5 行:其它任何 msgtype → 不产出;fail-safe 计数(unknown=True),不抛异常。"""
        d = decode(msgtype, b"\x08\x01\x12\x03abc")
        assert d.emit is False
        assert d.unknown is True

    def test_D8_unknown_first_seen_warns_once(self, caplog):
        """§2.9.5「未知 msgtype 的计数/首见落点」:每种未见过的 msgtype 首次出现打一条 WARNING(带 msgtype、表名、_id);「见过」= 进程内集合。"""
        f = MessageFactory("qd01", SELF_UID)
        t = table_name(PEER_A)
        with caplog.at_level(logging.WARNING):
            m1, u1 = f.to_message(row(-2002, b"\x0a\x01x", id=11, uniseq=7011), table=t, native_id=PEER_A, kind="private")
            m2, u2 = f.to_message(row(-2002, b"\x0a\x01y", id=12, uniseq=7012), table=t, native_id=PEER_A, kind="private")
            m3, u3 = f.to_message(row(-2022, b"\x0a\x01z", id=13, uniseq=7013), table=t, native_id=PEER_A, kind="private")
        assert (m1, u1) == (None, True) and (m2, u2) == (None, True) and (m3, u3) == (None, True)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 2, [r.getMessage() for r in warns]           # -2002 只告警一次、-2022 一次
        first = warns[0].getMessage()
        assert "-2002" in first and t in first and "11" in first

    def test_D8b_silent_and_registered_java_no_warning(self, caplog):
        """路由表第 1/4 行:登记的 Java 序列化类型与四个 silent 类型不是「未见过的 msgtype」,不打 WARNING(§8b M2 路由行「全程日志无未见过的 msgtype WARNING」)。"""
        f = MessageFactory("qd01", SELF_UID)
        t = table_name(PEER_A)
        with caplog.at_level(logging.WARNING):
            for i, (mt, data) in enumerate([(-2000, b"\x0a\x01p"), (-2006, b"sys"), (-5040, b"\x0a\x01q"), (-2018, b"\x0a\x01r"),
                                            (-2017, b"\xac\xed\x00\x05x"), (-2011, b"\xac\xed\x00\x05y")]):
                m, u = f.to_message(row(mt, data, id=20 + i, uniseq=7020 + i), table=t, native_id=PEER_A, kind="private")
                assert m is None and u is False
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_D9_field_mapping_private(self):
        """§2.9.5「字段 → 基线 §7.4 Message」:dir/self ← issend;sender.id ← XOR(senderuin);session.id = qd01:<对端uin>、kind=private;
        ts ← time(秒);ext_msg_id = "qd:"+uniseq、dedup_kind=native;type=text;source=qidian_db;入向 state 恒 DELIVERED。"""
        f = MessageFactory("qd01", SELF_UID)
        m, unknown = f.to_message(row(-1000, "报价 1.70".encode(), id=5, issend=0, time=1_758_240_100, uniseq=8001, sender=PEER_A),
                                  table=table_name(PEER_A), native_id=PEER_A, kind="private")
        assert unknown is False and m is not None
        assert m.dir == "in" and m.self is False
        assert m.sender_id == PEER_A
        assert m.session.id == f"qd01:{PEER_A}" and m.session.kind == "private"
        assert m.ts_ms == 1_758_240_100 * 1000
        assert m.ext_msg_id == "qd:8001" and m.dedup_kind == "native"
        assert m.type == "text" and m.text == "报价 1.70"
        assert m.source == "qidian_db" and m.channel == "qidian" and m.account_id == "qd01"
        assert m.state == "DELIVERED"

    def test_D9b_field_mapping_group_and_out(self):
        """§2.9.5:群 native_id = g_<群号>、kind=group;issend=1 → dir=out、self=true;群里 sender.id = 发言的群成员 uin。"""
        f = MessageFactory("qd01", SELF_UID)
        m, _ = f.to_message(row(-1049, "@我 1.70".encode(), id=9, issend=0, uniseq=8002, sender="777001"),
                            table=table_name(GROUP_G, True), native_id="g_" + GROUP_G, kind="group")
        assert m.session.id == f"qd01:g_{GROUP_G}" and m.session.kind == "group"
        assert m.sender_id == "777001" and m.dir == "in"
        m2, _ = f.to_message(row(-1000, "我方回复".encode(), id=10, issend=1, uniseq=8003, sender=SELF_UID),
                             table=table_name(PEER_A), native_id=PEER_A, kind="private")
        assert m2.dir == "out" and m2.self is True and m2.sender_id == SELF_UID

    def test_D9c_not_emitted_rows_return_none(self):
        """§2.9.5 ④:to_message 对不产出的行返回 (None, unknown),不抛异常。"""
        f = MessageFactory("qd01", SELF_UID)
        assert f.to_message(row(-2000, b"\x0a\x01p", uniseq=8010), table="t", native_id=PEER_A, kind="private") == (None, False)
        assert f.to_message(row(-1035, elem_image(), uniseq=8011), table="t", native_id=PEER_A, kind="private") == (None, False)
        assert f.to_message(row(-2017, b"\xac\xed\x00\x05", uniseq=8012), table="t", native_id=PEER_A, kind="private") == (None, False)


# ====================================================================== E. 首登历史闸 / bootstrap / 新会话第一条(06 §2.9.5 ③④,§8b M2 1607/1608 行)

class TestE_Bootstrap:
    def test_E1_first_login_history_gated(self, store, clock, maindb, poller):
        """§8b M2「首次 bootstrap 不回灌历史」:已有大量历史 ⇒ messages 0 行、无 message 事件;cursors 有 qidian_bootstrap(value=登录 uin,
        value_int=建立时刻 ms);已有表水位 = 当时 MAX(_id)、value.last_uniseq = 那一行的 uniseq(§2.9.3 表 / §2.9.5 ③)。"""
        t0 = clock.now_s
        for i in range(3):
            last_id = maindb.insert_text(PEER_A, f"历史{i}", time_s=t0 - 86400 + i)
        last_uniseq = sqlite3.connect(maindb.path).execute(f'SELECT uniseq FROM "{table_name(PEER_A)}" WHERE _id=?', (last_id,)).fetchone()[0]
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 0
        assert msg_events(store) == []
        boot = store.cursor_get("qd01", "qidian_bootstrap")
        assert boot is not None and boot.value == SELF_UID and boot.value_int == clock()
        cur = store.cursor_get("qd01", "qidian_rowid:" + PEER_A)
        assert cur is not None and cur.value_int == last_id == 3
        assert cur.value_json() == {"last_uniseq": last_uniseq}
        assert len(store.cursors_list("qd01", "qidian_bootstrap")) == 1

    def test_E2_late_arriving_history_gated_watermark_passes(self, store, clock, maindb, poller):
        """§2.9.5「历史闸才是不回灌历史的保证」:bootstrap 之后企点才同步进来的漫游历史(_id 更大、time 更早)不入库、不发事件,水位照常越过。"""
        maindb.insert_text(PEER_A, "老的", time_s=clock.now_s - 3600)
        poller.poll_maindb(acct_view())
        clock.advance(30_000)
        new_id = maindb.insert_text(PEER_A, "后到的历史", time_s=clock.now_s - 7200)
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 0
        assert msg_events(store) == []
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_int == new_id

    def test_E3_new_table_full_of_history_starts_at_zero_but_zero_ingested(self, store, clock, maindb, poller):
        """§8b M2 1607 行末句:期间新建的表其 qidian_rowid:* 水位从 0 起但同样 0 入库(整表历史被闸);新表要被每轮表发现认出。"""
        poller.poll_maindb(acct_view())                       # 空库首登:只建 bootstrap
        clock.advance(20_000)
        ids = [maindb.insert_text(PEER_B, f"漫游{i}", time_s=clock.now_s - 86400 + i) for i in range(5)]
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 0
        assert msg_events(store) == []
        cur = store.cursor_get("qd01", "qidian_rowid:" + PEER_B)
        assert cur is not None and cur.value_int == ids[-1]     # 从 0 起读过、越过了全部历史

    def test_E4_gate_boundary_is_bootstrap_minus_120s(self, store, clock, maindb, poller):
        """§2.9.5 ④:cutoff_s = qidian_bootstrap.value_int/1000 − 120,time < cutoff 的行算历史;恰好 = cutoff 的行入库(开放项 (f):余量内的行会真入库)。"""
        poller.poll_maindb(acct_view())
        boot_s = store.cursor_get("qd01", "qidian_bootstrap").value_int // 1000
        maindb.insert_text(PEER_A, "刚好在闸内", time_s=boot_s - 120)
        maindb.insert_text(PEER_A, "早一秒被闸", time_s=boot_s - 121)
        poller.poll_maindb(acct_view())
        rows = all_messages(store)
        assert [r["text"] for r in rows] == ["刚好在闸内"]
        assert len(msg_events(store)) == 1

    def test_E5_new_session_first_message_not_lost(self, store, clock, maindb, poller):
        """§8b M2「bootstrap 之后的新会话不丢第一条」:从未聊过的对端发来第一条 ⇒ 入库 1 行(ext_msg_id='qd:<uniseq>');
        cursors 新增该会话的 qidian_rowid:<native_id>;无第二行 qidian_bootstrap。"""
        maindb.insert_text(PEER_A, "老会话历史", time_s=clock.now_s - 3600)
        poller.poll_maindb(acct_view())
        boot_before = store.cursor_get("qd01", "qidian_bootstrap")
        clock.advance(15_000)
        new_id = maindb.insert_text(PEER_B, "你好,第一次联系", time_s=clock.now_s, uniseq=55501)
        poller.poll_maindb(acct_view())
        rows = all_messages(store)
        assert len(rows) == 1
        assert rows[0]["ext_msg_id"] == "qd:55501" and rows[0]["text"] == "你好,第一次联系"
        assert rows[0]["session_id"] == f"qd01:{PEER_B}" and rows[0]["dir"] == "in"
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_B).value_int == new_id
        assert len(store.cursors_list("qd01", "qidian_bootstrap")) == 1
        boot_after = store.cursor_get("qd01", "qidian_bootstrap")
        assert (boot_after.value, boot_after.value_int) == (boot_before.value, boot_before.value_int)
        assert len(msg_events(store)) == 1 and msg_events(store)[0]["ext_msg_id"] == "qd:55501"

    def test_E6_first_is_judged_by_bootstrap_row_only(self, store, clock, maindb, poller):
        """§2.9.5:判「首次」只认 qidian_bootstrap 行,不得用「有没有 qidian_rowid:* 行」代替——全新账号首轮一张表都没有,
        第一个新会话出现时不得再做一次「首次」把水位置到 MAX(_id)、丢第一条。"""
        poller.poll_maindb(acct_view())                        # 一张会话表都没有
        assert store.cursors_list("qd01", "qidian_rowid:") == []
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "第一条", time_s=clock.now_s, uniseq=61001)
        poller.poll_maindb(acct_view())
        assert [r["ext_msg_id"] for r in all_messages(store)] == ["qd:61001"]

    def test_E7_non_text_rows_pass_watermark_without_message(self, store, clock, maindb, poller):
        """§8b M2 路由行 ④ + §2.9.5:纯图片(-2000)不入库、无 message 事件,但该会话水位已越过它;任何 msgtype 的行水位都照常越过。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "先来一条文本", time_s=clock.now_s)
        poller.poll_maindb(acct_view())
        before = store.count_messages("qd01")
        pic_id = maindb.insert(PEER_A, group=False, time_s=clock.now_s, msgtype=-2000, msgdata=b"\x0a\x10picmeta")
        file_id = maindb.insert(PEER_A, group=False, time_s=clock.now_s, msgtype=-2017, msgdata=b"\xac\xed\x00\x05TroopFileData")
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == before == 1
        assert len(msg_events(store)) == 1
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_int == file_id > pic_id

    def test_E8_not_message_tables_ignored_and_empty_table_skipped(self, store, clock, maindb, poller):
        """§2.9.5 按会话分表:mr_data_line/mr_fileManager 不是消息表,不读;② 空表认不出会话、本轮跳过,有行了再认。"""
        con = sqlite3.connect(maindb.path)
        con.execute("CREATE TABLE IF NOT EXISTS mr_fileManager(_id INTEGER PRIMARY KEY, frienduin BLOB, istroop INTEGER)")
        con.commit(); con.close()
        maindb.ensure_table(PEER_B, False)                    # 空会话表
        poller.poll_maindb(acct_view())
        st = poller.state_of("qd01")
        assert set(st.table_map) == set()
        assert store.cursors_list("qd01", "qidian_rowid:") == []
        clock.advance(5_000)
        maindb.insert_text(PEER_B, "现在有行了", time_s=clock.now_s)
        poller.poll_maindb(acct_view())
        assert st.table_map == {table_name(PEER_B): PEER_B}
        assert store.count_messages("qd01") == 1

    def test_E9_sessions_row_created_before_message(self, store, clock, maindb, poller):
        """02 §2.2.8 / §2.8.1:入库先 upsert sessions 再写 messages(同一事务),新会话第一条不得炸外键;会话行 id = qd01:<native_id>。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(GROUP_G, "群里第一条", time_s=clock.now_s, group=True, sender="777001")
        poller.poll_maindb(acct_view())
        sess = store.con.execute("SELECT * FROM sessions WHERE account_id='qd01'").fetchall()
        assert [dict(s)["id"] for s in sess] == [f"qd01:g_{GROUP_G}"]
        assert all_messages(store)[0]["session_id"] == f"qd01:g_{GROUP_G}"


# ====================================================================== F. 水位自检 → 重扫不重放(06 §2.9.5 ④ / R6-40,§8b M2 1610 行 ①)

class TestF_Rescan:
    def test_F1_tampered_last_uniseq_rescans_without_replay(self, store, clock, maindb, poller):
        """§8b M2 1610 ①:把某会话 qidian_rowid 的 value.last_uniseq 改成不存在的值 ⇒ 该表从 0 重扫:messages 行数不变、events_outbox 无新增 message、
        游标 value.last_uniseq 被改回真实值。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "一", time_s=clock.now_s, uniseq=70001)
        last_id = maindb.insert_text(PEER_A, "二", time_s=clock.now_s, uniseq=70002)
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 2 and len(msg_events(store)) == 2
        cur = store.cursor_get("qd01", "qidian_rowid:" + PEER_A)
        store.cursor_set("qd01", "qidian_rowid:" + PEER_A, cur.value_int, json.dumps({"last_uniseq": 999_999}))
        clock.advance(5_000)
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 2
        assert len(msg_events(store)) == 2
        cur2 = store.cursor_get("qd01", "qidian_rowid:" + PEER_A)
        assert cur2.value_int == last_id and cur2.value_json() == {"last_uniseq": 70002}

    def test_F2_table_rebuilt_ids_restart(self, store, clock, maindb, poller):
        """§2.9.5 ④ 水位自检:水位那一行不在了(该表被清、_id 从头来)⇒ 本表从 0 重扫;真新行入库、旧行靠 qd:{uniseq} 不重复、不重放。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "旧一", time_s=clock.now_s, uniseq=71001)
        maindb.insert_text(PEER_A, "旧二", time_s=clock.now_s, uniseq=71002)
        poller.poll_maindb(acct_view())
        maindb.clear_table(PEER_A)
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "旧一", time_s=clock.now_s, uniseq=71001)     # 重建后同一条(uniseq 不变的前提)
        maindb.insert_text(PEER_A, "新三", time_s=clock.now_s, uniseq=71003)     # 真新消息
        poller.poll_maindb(acct_view())
        texts = sorted(r["text"] for r in all_messages(store))
        assert texts == ["新三", "旧一", "旧二"]
        assert len(msg_events(store)) == 3
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_json() == {"last_uniseq": 71003}

    def test_F3_missing_last_uniseq_in_cursor_rescans(self, store, clock, maindb, poller):
        """§2.9.5 ④:游标缺 last_uniseq(value 为空)⇒ 本表从 0 重扫,同样不重放。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "一", time_s=clock.now_s, uniseq=72001)
        poller.poll_maindb(acct_view())
        cur = store.cursor_get("qd01", "qidian_rowid:" + PEER_A)
        store.cursor_set("qd01", "qidian_rowid:" + PEER_A, cur.value_int, None)
        poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 1 and len(msg_events(store)) == 1
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_json() == {"last_uniseq": 72001}


# ====================================================================== G. 掉线/被踢后重登的续读(06 §2.9.5 掉线续读条,00 §7.4,§8b M2 1609 行)

class TestG_KickedResume:
    def test_G1_resume_after_kick(self, store, clock, maindb, poller):
        """§8b M2 1609:掉线期间无 QIDIAN_DB_UNAVAILABLE;重登后三条全部入库且不重复:对端两条 dir=in,人回的那条 dir=out、trace_id IS NULL;
        三条 message 事件 payload.late=true、payload.lag_s 齐全;人回的那条另有 payload.origin='external'(入向行不带该字段,00 §7.4)。
        另:该出向行 self=true、source=qidian_db、state=DELIVERED、trace_id/idempotency_key 为 NULL(§2.9.5 掉线续读条)。"""
        maindb.insert_text(PEER_A, "老历史", time_s=clock.now_s - 3600)
        poller.poll_maindb(acct_view())
        alerts = poller.alerts_
        # 被踢:login_required 期间读循环空转
        for _ in range(6):
            clock.advance(5_000)
            poller.poll_maindb(acct_view(state="login_required"))
        assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == []
        assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        # 掉线期间:对端在两个私聊各发 1 条、人在别的端回 1 条
        t_kick = clock.now_s
        maindb.insert_text(PEER_A, "在吗?报个价", time_s=t_kick + 600, uniseq=80001)
        maindb.insert_text(PEER_B, "1Y 1.70 可以吗", time_s=t_kick + 700, uniseq=80002)
        maindb.insert_text(PEER_A, "手机上回你:可以", time_s=t_kick + 800, issend=1, uniseq=80003)
        clock.advance(2 * 3600 * 1000)                          # 人两小时后重登
        poller.poll_maindb(acct_view())
        poller.poll_maindb(acct_view())                         # 再跑一轮验证不重复
        rows = {r["ext_msg_id"]: r for r in all_messages(store)}
        assert set(rows) == {"qd:80001", "qd:80002", "qd:80003"}
        assert rows["qd:80001"]["dir"] == "in" and rows["qd:80002"]["dir"] == "in"
        out = rows["qd:80003"]
        assert out["dir"] == "out" and out["trace_id"] is None and out["idempotency_key"] is None
        assert out["is_self"] == 1 and out["source"] == "qidian_db" and out["state"] == "DELIVERED"
        evs = {e["ext_msg_id"]: e for e in msg_events(store)}
        assert set(evs) == {"qd:80001", "qd:80002", "qd:80003"}
        now_s = clock.now_s
        for u, t in [("qd:80001", t_kick + 600), ("qd:80002", t_kick + 700), ("qd:80003", t_kick + 800)]:
            assert evs[u]["late"] is True
            assert evs[u]["lag_s"] == now_s - t
        assert evs["qd:80003"]["origin"] == "external"
        assert "origin" not in evs["qd:80001"] and "origin" not in evs["qd:80002"]

    def test_G2_late_threshold_120s(self, store, clock, maindb, poller):
        """00 §7.4 / §2.9.5:late = lag_s > [messages] late_after_s(默认 120);lag_s = received_at − ts(整数秒)。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        maindb.insert_text(PEER_A, "正好 120", time_s=clock.now_s - 120, uniseq=81001)
        maindb.insert_text(PEER_A, "121 秒", time_s=clock.now_s - 121, uniseq=81002)
        maindb.insert_text(PEER_A, "刚到", time_s=clock.now_s - 3, uniseq=81003)
        poller.poll_maindb(acct_view())
        evs = {e["ext_msg_id"]: e for e in msg_events(store)}
        assert evs["qd:81001"]["lag_s"] == 120 and evs["qd:81001"]["late"] is False
        assert evs["qd:81002"]["lag_s"] == 121 and evs["qd:81002"]["late"] is True
        assert evs["qd:81003"]["lag_s"] == 3 and evs["qd:81003"]["late"] is False

    def test_G3_watermarks_per_session_independent(self, store, clock, maindb, poller):
        """§2.9.5 掉线续读条:水位不丢、各会话各管各的——多个会话各从各自断点 _id > 水位 续读,互不影响;掉线期间新出现的会话从 0 起。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "A1", time_s=clock.now_s)
        maindb.insert_text(GROUP_G, "G1", time_s=clock.now_s, group=True, sender="777001")
        poller.poll_maindb(acct_view())
        poller.poll_maindb(acct_view(state="login_required"))
        assert poller.state_of("qd01").table_map == {}          # ⓪ 离开 running 重置内存态;水位在 cursors 里
        clock.advance(600_000)
        maindb.insert_text(PEER_A, "A2", time_s=clock.now_s)
        maindb.insert_text(GROUP_G, "G2", time_s=clock.now_s, group=True, sender="777002")
        maindb.insert_text(PEER_B, "B1 新会话", time_s=clock.now_s)
        poller.poll_maindb(acct_view())
        assert sorted(r["text"] for r in all_messages(store)) == ["A1", "A2", "B1 新会话", "G1", "G2"]
        assert len(msg_events(store)) == 5


# ====================================================================== H. 出向 SENDING 行被读库合并(06 §2.12 企点行 + 02 §2.8.1 出向行,§8b M2 1617 行)

class TestH_OutMerge:
    def test_H1_merge_into_sending_row(self, store, clock, maindb, poller):
        """§2.12 企点(读库正线):poll 拉到同 session_id、issend=1、norm(text) 相等、|time−ts| ≤ out_merge_window_s 的行 ⇒ 合并进 SENDING 行:
        补 ext_msg_id=qd:{uniseq}、state=DELIVERED、confirmed_by=ingest_merge;不插第二行;messages.text 与发出的逐字相同(保留换行);
        该条 message 事件 payload.origin='rpa'。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        text = "1Y\n1.70\n2Y 1.80"
        out_id = out_sending(store, clock, text, trace_id="tr_h1")
        maindb.insert(PEER_A, group=False, time_s=clock.now_s + 8, msgtype=-1051, msgdata=text.encode(), issend=1, uniseq=90001)
        clock.advance(9_000)
        poller.poll_maindb(acct_view())
        rows = all_messages(store)
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == out_id
        assert r["state"] == "DELIVERED" and r["ext_msg_id"] == "qd:90001" and r["confirmed_by"] == "ingest_merge"
        assert r["text"] == text and r["trace_id"] == "tr_h1"
        evs = msg_events(store)
        assert len(evs) == 1
        assert evs[0]["id"] == out_id and evs[0]["origin"] == "rpa" and evs[0]["state"] == "DELIVERED"

    def test_H2_fullwidth_text_kept_verbatim_and_merged(self, store, clock, maindb, poller):
        """§8b M2 1617 ⑤ + §2.12:全角 "收到　ＯＫ" 出向行不归一化、逐字保留;读回行 norm() 后相等 ⇒ 合并、confirmed_by=ingest_merge。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        text = "收到　ＯＫ"
        out_id = out_sending(store, clock, text, trace_id="tr_h2")
        maindb.insert_text(PEER_A, text, time_s=clock.now_s + 5, issend=1, uniseq=90002)
        clock.advance(6_000)
        poller.poll_maindb(acct_view())
        rows = all_messages(store)
        assert len(rows) == 1 and rows[0]["id"] == out_id
        assert rows[0]["text"] == "收到　ＯＫ" and rows[0]["state"] == "DELIVERED" and rows[0]["confirmed_by"] == "ingest_merge"

    def test_H3_unconfirmed_row_also_merges(self, store, clock, maindb, poller):
        """§2.12:超时 UNCONFIRMED 之后 poll 拉到仍照常合并,把 UNCONFIRMED 翻成 DELIVERED(被动 ingest 合并不被 D-2 ② 禁)。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        out_id = out_sending(store, clock, "收到", trace_id="tr_h3", state="UNCONFIRMED")
        maindb.insert_text(PEER_A, "收到", time_s=clock.now_s + 20, issend=1, uniseq=90003)
        clock.advance(21_000)
        poller.poll_maindb(acct_view())
        r = store.get_message(out_id)
        assert r["state"] == "DELIVERED" and r["ext_msg_id"] == "qd:90003" and r["confirmed_by"] == "ingest_merge"
        assert store.count_messages("qd01") == 1
        assert msg_events(store)[0]["origin"] == "rpa"

    def test_H4_multi_candidate_closest_time_wins(self, store, clock, maindb, poller):
        """§2.12 多候选定序(R6-39):同时有多行出向行命中时取 |读到的行 time − 出向行 ts| 最小的一行;一条读到的行只合并一次。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        far_id = out_sending(store, clock, "同样的话", trace_id="tr_far", ts_ms=clock() - 40_000)
        near_id = out_sending(store, clock, "同样的话", trace_id="tr_near", ts_ms=clock() - 3_000)
        maindb.insert_text(PEER_A, "同样的话", time_s=clock.now_s, issend=1, uniseq=90004)
        clock.advance(1_000)
        poller.poll_maindb(acct_view())
        assert store.get_message(near_id)["state"] == "DELIVERED" and store.get_message(near_id)["ext_msg_id"] == "qd:90004"
        assert store.get_message(far_id)["state"] == "SENDING" and store.get_message(far_id)["ext_msg_id"] is None
        assert store.count_messages("qd01") == 2

    def test_H5_no_match_becomes_external(self, store, clock, maindb, poller):
        """§2.9.5 掉线续读条:都不命中才按「非本系统发出」入库——dir=out、trace_id IS NULL、origin='external';SENDING 行原封不动。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        out_id = out_sending(store, clock, "我发的 A", trace_id="tr_h5")
        maindb.insert_text(PEER_A, "手机上发的 B", time_s=clock.now_s + 2, issend=1, uniseq=90005)
        clock.advance(3_000)
        poller.poll_maindb(acct_view())
        rows = {r["id"]: r for r in all_messages(store)}
        assert len(rows) == 2
        assert rows[out_id]["state"] == "SENDING" and rows[out_id]["ext_msg_id"] is None
        ext = [r for r in rows.values() if r["id"] != out_id][0]
        assert ext["dir"] == "out" and ext["trace_id"] is None and ext["ext_msg_id"] == "qd:90005" and ext["source"] == "qidian_db"
        evs = msg_events(store)
        assert len(evs) == 1 and evs[0]["origin"] == "external"

    def test_H6_external_judgement_excludes_failed_with_trace(self, store, clock, maindb, poller):
        """§2.9.5 掉线续读条(R6-40 收紧):dir='out' AND trace_id IS NULL 且同会话 out_merge_window_s 内不存在 norm(text) 相等、带 trace_id 的出向行
        (任何 state,含 FAILED)才是 external;存在 FAILED 同文本行 ⇒ 不是 external(origin='rpa')。"""
        poller.poll_maindb(acct_view())
        clock.advance(1_000)
        failed_id = out_sending(store, clock, "其实送达了", trace_id="tr_h6", state="FAILED")
        maindb.insert_text(PEER_A, "其实送达了", time_s=clock.now_s + 2, issend=1, uniseq=90006)
        clock.advance(3_000)
        poller.poll_maindb(acct_view())
        evs = msg_events(store)
        assert len(evs) == 1
        assert evs[0].get("origin") == "rpa", evs[0]
        assert store.get_message(failed_id)["state"] == "FAILED"     # FAILED 行不参与合并,只影响 origin 判定


# ====================================================================== I. 加速轮(06 §2.9.5 full / only_sessions,R6-38/R6-40;§8b M2 1611 行 ②)

class TestI_FastPoll:
    def test_I1_fast_rounds_do_not_reset_fail_counter(self, store, clock, maindb):
        """§8b M2 1611 ②:读库指向不存在的库文件名,同时持续加速轮 ⇒ 3 个读取周期(全量轮)后照常发 QIDIAN_DB_UNAVAILABLE(加速轮不把失败计数清零)。"""
        paths = {"p": maindb.path}
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(paths["p"]))
        maindb.insert_text(PEER_A, "老", time_s=clock.now_s - 3600)
        p.poll_maindb(acct_view())
        paths["p"] = maindb.path + ".moved"
        for _ in range(2):
            clock.advance(5_000); p.poll_maindb(acct_view())
        assert p.state_of("qd01").db_fail_rounds == 2
        for _ in range(5):
            clock.advance(1_000); p.poll_maindb(acct_view(), only_sessions=[PEER_A])
        assert p.state_of("qd01").db_fail_rounds == 2
        assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == []
        clock.advance(5_000); p.poll_maindb(acct_view())
        fired = alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")
        assert len(fired) == 1 and fired[0]["evidence"]["reason"] == "not_found"

    def test_I2_fast_rounds_do_not_resolve(self, store, clock, maindb):
        """§2.9.5 R6-40:加速轮既不 fail() 也不 resolve()——库恢复后加速轮不销告警,全量轮整轮成功才 resolved。"""
        paths = {"p": maindb.path}
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(paths["p"]))
        p.poll_maindb(acct_view())
        paths["p"] = maindb.path + ".moved"
        for _ in range(3):
            clock.advance(5_000); p.poll_maindb(acct_view())
        assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        paths["p"] = maindb.path
        for _ in range(3):
            clock.advance(1_000); p.poll_maindb(acct_view(), only_sessions=[PEER_A])
        assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert alert_events(store, QIDIAN_DB_UNAVAILABLE, "resolved") == []
        clock.advance(5_000); p.poll_maindb(acct_view())
        assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "resolved")) == 1
        assert p.state_of("qd01").db_fail_rounds == 0

    def test_I3_fast_round_without_bootstrap_does_nothing(self, store, clock, maindb, poller):
        """§2.9.5 加速轮:还没 bootstrap ⇒ 没有闸基准,加速轮不做(不建 bootstrap、不入库、不建映射),等全量轮。"""
        maindb.insert_text(PEER_A, "x", time_s=clock.now_s)
        poller.poll_maindb(acct_view(), only_sessions=[PEER_A])
        assert store.cursor_get("qd01", "qidian_bootstrap") is None
        assert store.count_messages("qd01") == 0
        assert poller.state_of("qd01").table_map == {}

    def test_I4_fast_round_reads_only_target_tables(self, store, clock, maindb, poller):
        """§2.9.5 加速轮:只查目标会话表、跳过 ①②③——非目标会话的新行与新出现的表都留给全量轮。"""
        maindb.insert_text(PEER_A, "a0", time_s=clock.now_s - 3600)
        maindb.insert_text(PEER_B, "b0", time_s=clock.now_s - 3600)
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "a1", time_s=clock.now_s)
        maindb.insert_text(PEER_B, "b1", time_s=clock.now_s)
        maindb.insert_text(GROUP_G, "g1", time_s=clock.now_s, group=True, sender="777001")
        poller.poll_maindb(acct_view(), only_sessions=[PEER_A])
        assert [r["text"] for r in all_messages(store)] == ["a1"]
        assert table_name(GROUP_G, True) not in poller.state_of("qd01").table_map
        clock.advance(5_000)
        poller.poll_maindb(acct_view())
        assert sorted(r["text"] for r in all_messages(store)) == ["a1", "b1", "g1"]


# ====================================================================== J. 读库不可用告警(06 §2.9.5 fail() 条,02 §3.7 QIDIAN_DB_UNAVAILABLE,§8b M2 1606 行)

class TestJ_DbUnavailable:
    def test_J1_three_rounds_then_warn_with_evidence(self, store, clock, maindb):
        """§2.9.5 fail() + 02 §3.7:maindb_seen 之后库文件不存在 ⇒ 连续 3 轮失败才发 warn QIDIAN_DB_UNAVAILABLE(subject=account:qd01,
        evidence{db_path, reason='not_found', app_version}, hint_actions=['open_env']);前 2 轮不发;重复 firing 不重复发事件。"""
        paths = {"p": maindb.path}
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(paths["p"]))
        p.poll_maindb(acct_view())
        paths["p"] = maindb.path + ".moved"
        for i in range(2):
            clock.advance(5_000); p.poll_maindb(acct_view())
            assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == [], f"第 {i + 1} 轮不该告警"
        clock.advance(5_000); p.poll_maindb(acct_view())
        fired = alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")
        assert len(fired) == 1
        a = fired[0]
        assert a["severity"] == "warn" and a["subject"] == "account:qd01"
        assert a["evidence"]["reason"] == "not_found"
        assert a["evidence"]["db_path"] == DB_DIR + SELF_UID + ".db"
        assert "app_version" in a["evidence"]
        assert a["hint_actions"] == ["open_env"]
        clock.advance(5_000); p.poll_maindb(acct_view())
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")) == 1

    def test_J2_first_login_grace_12_rounds(self, store, clock, maindb):
        """§2.9.5 fail() 唯一例外:reason=not_found 且本次 running 期内从未见过主库 ⇒ 阈值放宽到 12 轮;第 12 轮才发。"""
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(maindb.path + ".notyet"))
        for i in range(11):
            clock.advance(5_000); p.poll_maindb(acct_view())
            assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == [], f"第 {i + 1} 轮在首登宽限内不该告警"
        clock.advance(5_000); p.poll_maindb(acct_view())
        fired = alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")
        assert len(fired) == 1 and fired[0]["evidence"]["reason"] == "not_found"

    def test_J3_recovery_resolves_and_resumes_from_watermark(self, store, clock, maindb):
        """§8b M2 1606 ④:恢复后告警 resolved、水位 qidian_rowid:<native_id> 从断点续读不丢不重。"""
        paths = {"p": maindb.path}
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(paths["p"]))
        p.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "断前", time_s=clock.now_s, uniseq=100_001)
        p.poll_maindb(acct_view())
        paths["p"] = maindb.path + ".moved"
        for _ in range(3):
            clock.advance(5_000); p.poll_maindb(acct_view())
        assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        maindb.insert_text(PEER_A, "断中", time_s=clock.now_s, uniseq=100_002)
        paths["p"] = maindb.path
        clock.advance(5_000); p.poll_maindb(acct_view())
        assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "resolved")) == 1
        assert sorted(r["ext_msg_id"] for r in all_messages(store)) == ["qd:100001", "qd:100002"]
        assert len(msg_events(store)) == 2

    def test_J4_schema_mismatch_reason(self, store, clock, maindb):
        """§8b M2 1606:换成缺列的库副本(模拟 schema 变更)⇒ 3 轮后 warn QIDIAN_DB_UNAVAILABLE、evidence.reason='schema_mismatch'。"""
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(maindb.path))
        create_raw_table(maindb.path, table_name(PEER_A), with_msgdata=False, time_s=clock.now_s)
        for i in range(2):
            clock.advance(5_000); p.poll_maindb(acct_view())
            assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == []
        clock.advance(5_000); p.poll_maindb(acct_view())
        fired = alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")
        assert len(fired) == 1 and fired[0]["evidence"]["reason"] == "schema_mismatch"

    def test_J5_leaving_running_resets_and_resolves(self, store, clock, maindb):
        """§2.9.5 ⓪:离开 running 即全部重置并 resolve;下次登录重新享受建库宽限(被踢重登时不得误告复发)。"""
        paths = {"p": maindb.path}
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(paths["p"]))
        p.poll_maindb(acct_view())
        paths["p"] = maindb.path + ".moved"
        for _ in range(3):
            clock.advance(5_000); p.poll_maindb(acct_view())
        assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        p.poll_maindb(acct_view(state="login_required"))
        assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "resolved")) == 1
        st = p.state_of("qd01")
        assert st.db_fail_rounds == 0 and st.maindb_seen is False and st.table_map == {} and st.bad_rounds == {}
        for _ in range(11):                                       # 重登、库还没建好:12 轮宽限重新生效
            clock.advance(5_000); p.poll_maindb(acct_view())
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")) == 1

    def test_J6_not_running_never_alerts_and_account_state_untouched(self, store, clock, maindb):
        """02 §3.7:未登录(state != running 或 self_uid 为空)读循环只空转、不发本码;R6-32:告警期间 accounts.state 仍 running(poll 不改账号态)。"""
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(maindb.path + ".none"))
        for _ in range(15):
            clock.advance(5_000)
            p.poll_maindb(acct_view(state="login_required"))
            p.poll_maindb(acct_view(state="running", self_uid=None))
        assert store.list_events(event="alert") == []
        for _ in range(12):
            clock.advance(5_000); p.poll_maindb(acct_view())
        assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert store.get_account("qd01")["state"] == "running"


# ====================================================================== K. H13 时钟漂移守卫(06 §2.9.5 ③ / R6-41,§8b M2 1612 行)

class TestK_ClockUnsynced:
    def test_K1_deleted_bootstrap_under_h13(self, store, clock, maindb, poller):
        """§8b M2 1612 ①:已建库的 qd01 手工删掉 qidian_bootstrap 行、H13 持续 firing ⇒ 没有新 bootstrap 写入、qidian_rowid:* 不删、audit_log 无 rebootstrap;
        ≥ 12 轮后 warn QIDIAN_DB_UNAVAILABLE(evidence.reason='clock_unsynced');撤开关后下一轮建基准、告警 resolved。"""
        maindb.insert_text(PEER_A, "老", time_s=clock.now_s - 3600)
        poller.poll_maindb(acct_view())
        wm_before = rowid_cursors(store)
        assert wm_before
        store.con.execute("DELETE FROM cursors WHERE owner='qd01' AND kind='qidian_bootstrap'"); store.con.commit()
        poller.h13_["on"] = True
        for i in range(11):
            clock.advance(5_000); poller.poll_maindb(acct_view())
            assert store.cursor_get("qd01", "qidian_bootstrap") is None
            assert rowid_cursors(store) == wm_before
            assert store.list_audit("qidian.rebootstrap") == []
            assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == [], f"第 {i + 1} 轮不该告警"
        clock.advance(5_000); poller.poll_maindb(acct_view())
        fired = alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")
        assert len(fired) == 1 and fired[0]["evidence"]["reason"] == "clock_unsynced"
        assert store.cursor_get("qd01", "qidian_bootstrap") is None
        assert store.count_messages("qd01") == 0
        poller.h13_["on"] = False
        clock.advance(5_000); poller.poll_maindb(acct_view())
        boot = store.cursor_get("qd01", "qidian_bootstrap")
        assert boot is not None and boot.value == SELF_UID and boot.value_int == clock()
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "resolved")) == 1
        assert store.list_audit("qidian.rebootstrap") == []
        assert rowid_cursors(store) == wm_before

    def test_K2_uin_switch_under_h13_deletes_nothing_until_clear(self, store, clock, tmp_path):
        """§8b M2 1612 ②:已 bootstrap 的 qd02 换 uin 登录、H13 firing ⇒ 旧 qidian_rowid:* 一行没被删、无 audit、无新 bootstrap;≥12 轮 clock_unsynced;
        撤开关后下一轮旧水位此时才删且只 audit 一次、bootstrap.value=新 uin、告警 resolved。"""
        from conftest import FakeMainDb  # noqa: WPS433  夹具类,只用它造第二个库
        store.ensure_account("qd02", "qidian", state="running", self_uid="4001")
        db_old = FakeMainDb(str(tmp_path / "4001.db"), self_uin="4001")
        db_new = FakeMainDb(str(tmp_path / "4002.db"), self_uin="4002")
        p, alerts, h13 = new_poller(store, clock, lambda uid: LocalSqliteMainDb(str(tmp_path / f"{uid}.db")))
        db_old.insert_text(PEER_A, "旧库", time_s=clock.now_s - 3600)
        db_new.insert_text(PEER_B, "新库历史", time_s=clock.now_s - 3600)
        p.poll_maindb(acct_view(id="qd02", self_uid="4001"))
        old_wm = rowid_cursors(store, "qd02")
        assert set(old_wm) == {"qidian_rowid:" + PEER_A}
        h13["on"] = True
        for i in range(11):
            clock.advance(5_000); p.poll_maindb(acct_view(id="qd02", self_uid="4002"))
            assert rowid_cursors(store, "qd02") == old_wm
            assert store.list_audit("qidian.rebootstrap") == []
            assert store.cursor_get("qd02", "qidian_bootstrap").value == "4001"
            assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == []
        clock.advance(5_000); p.poll_maindb(acct_view(id="qd02", self_uid="4002"))
        fired = alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")
        assert len(fired) == 1 and fired[0]["evidence"]["reason"] == "clock_unsynced" and fired[0]["subject"] == "account:qd02"
        assert rowid_cursors(store, "qd02") == old_wm
        h13["on"] = False
        clock.advance(5_000); p.poll_maindb(acct_view(id="qd02", self_uid="4002"))
        assert set(rowid_cursors(store, "qd02")) == {"qidian_rowid:" + PEER_B}
        boot = store.cursor_get("qd02", "qidian_bootstrap")
        assert boot.value == "4002" and boot.value_int == clock()
        audits = store.list_audit("qidian.rebootstrap")
        assert len(audits) == 1 and audits[0]["account_id"] == "qd02"
        assert json.loads(audits[0]["detail_json"])["old_uin"] == "4001" and json.loads(audits[0]["detail_json"])["new_uin"] == "4002"
        assert len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "resolved")) == 1
        assert store.count_messages("qd02") == 0

    def test_K3_h13_only_matters_when_building_baseline(self, store, clock, maindb, poller):
        """§2.9.5 ③:H13 守卫只在「需要建/重建闸基准」时生效——基准已在时 H13 firing 不影响照常读库、不告警。"""
        poller.poll_maindb(acct_view())
        poller.h13_["on"] = True
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "H13 期间的新消息", time_s=clock.now_s)
        for _ in range(13):
            clock.advance(5_000); poller.poll_maindb(acct_view())
        assert store.count_messages("qd01") == 1
        assert store.list_events(event="alert") == []


# ====================================================================== L. 个别表自检不过 → 独立码(06 §2.9.5 ②,02 §3.7 QIDIAN_TABLE_DECODE_STUCK,§8b M2 1613 行)

class TestL_TableDecodeStuck:
    BAD = "mr_friend_" + "A" * 32 + "_New"

    def test_L1_stuck_after_12_rounds_independent_code(self, store, clock, maindb, poller):
        """§8b M2 1613:某一张表 MD5 自检持续不过(其余正常)等 12 轮 ⇒ warn QIDIAN_TABLE_DECODE_STUCK(evidence.tables 含该表名);
        不发 QIDIAN_DB_UNAVAILABLE;其余会话照常入库;该表被删后的下一个全量轮 resolved。"""
        create_raw_table(maindb.path, self.BAD, time_s=clock.now_s)
        maindb.insert_text(PEER_A, "老", time_s=clock.now_s - 3600)
        alerts = poller.alerts_
        for i in range(11):
            clock.advance(5_000); poller.poll_maindb(acct_view())
            assert alert_events(store, QIDIAN_TABLE_DECODE_STUCK) == [], f"第 {i + 1} 轮不该告警"
        maindb.insert_text(PEER_A, "其余会话照常", time_s=clock.now_s)
        clock.advance(5_000); poller.poll_maindb(acct_view())
        fired = alert_events(store, QIDIAN_TABLE_DECODE_STUCK, "firing")
        assert len(fired) == 1
        assert fired[0]["severity"] == "warn" and fired[0]["subject"] == "account:qd01"
        assert fired[0]["evidence"]["tables"] == [self.BAD]
        assert fired[0]["hint_actions"] == []
        assert alert_events(store, QIDIAN_DB_UNAVAILABLE) == []
        assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert [r["text"] for r in all_messages(store)] == ["其余会话照常"]
        drop_table(maindb.path, self.BAD)
        clock.advance(5_000); poller.poll_maindb(acct_view())
        assert not alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
        assert len(alert_events(store, QIDIAN_TABLE_DECODE_STUCK, "resolved")) == 1
        assert self.BAD not in poller.state_of("qd01").bad_rounds

    def test_L2_stuck_resolves_when_leaving_running(self, store, clock, maindb, poller):
        """02 §3.7 QIDIAN_TABLE_DECODE_STUCK:离开 running 亦 resolved。"""
        create_raw_table(maindb.path, self.BAD, time_s=clock.now_s)
        maindb.insert_text(PEER_A, "老", time_s=clock.now_s - 3600)
        for _ in range(12):
            clock.advance(5_000); poller.poll_maindb(acct_view())
        assert poller.alerts_.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
        poller.poll_maindb(acct_view(state="login_required"))
        assert not poller.alerts_.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")


# ====================================================================== M. 整库解不出只留一条告警(06 §2.9.5 ② / R6-42,§8b M2 1614 行)

class TestM_DecodeFailed:
    BAD1 = "mr_friend_" + "B" * 32 + "_New"
    BAD2 = "mr_troop_" + "C" * 32 + "_New"

    def test_M1_all_tables_bad_db_unavailable_only(self, store, clock, maindb, poller):
        """§8b M2 1614 ①:全部会话表自检不过,等 13 个全量轮 ⇒ 第 3 轮起 warn QIDIAN_DB_UNAVAILABLE(evidence.reason='decode_failed'),
        直到第 13 轮 QIDIAN_TABLE_DECODE_STUCK 始终未 firing。"""
        create_raw_table(maindb.path, self.BAD1, time_s=clock.now_s)
        create_raw_table(maindb.path, self.BAD2, istroop=1, peer=GROUP_G, time_s=clock.now_s)
        alerts = poller.alerts_
        for i in range(13):
            clock.advance(5_000); poller.poll_maindb(acct_view())
            n = len(alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing"))
            assert n == (0 if i < 2 else 1), f"第 {i + 1} 轮 DB_UNAVAILABLE firing 事件数 = {n}"
            assert not alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
        assert alert_events(store, QIDIAN_TABLE_DECODE_STUCK) == []
        assert alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")[0]["evidence"]["reason"] == "decode_failed"

    def test_M2_stuck_then_whole_db_bad_collapses_to_one_alert(self, store, clock, maindb, poller):
        """§8b M2 1614 ②:先单表不过直到 STUCK 亮起,再让整库全部不过 ⇒ 整库全挂那一轮 STUCK 转 resolved;3 轮后只剩 QIDIAN_DB_UNAVAILABLE 一条。"""
        create_raw_table(maindb.path, self.BAD1, time_s=clock.now_s)
        maindb.insert_text(PEER_A, "好表", time_s=clock.now_s - 3600)
        alerts = poller.alerts_
        for _ in range(12):
            clock.advance(5_000); poller.poll_maindb(acct_view())
        assert alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
        maindb.clear_table(PEER_A)                                 # 只剩坏表 = 整库全部不过
        clock.advance(5_000); poller.poll_maindb(acct_view())
        assert not alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
        assert len(alert_events(store, QIDIAN_TABLE_DECODE_STUCK, "resolved")) == 1
        for _ in range(2):
            clock.advance(5_000); poller.poll_maindb(acct_view())
        assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
        assert alert_events(store, QIDIAN_DB_UNAVAILABLE, "firing")[0]["evidence"]["reason"] == "decode_failed"
        assert set(alerts.active) == {(QIDIAN_DB_UNAVAILABLE, "account:qd01")}


# ====================================================================== N. 群缺口探测(06 §2.9.5 check_group_gaps,02 §3.7 QIDIAN_MSG_GAP,§8b M2 1618 行)

class TestN_GroupGaps:
    def _seed_group_with_gap(self, maindb, clock, *, group=GROUP_G, missing=20, when_s=None):
        when = when_s if when_s is not None else clock.now_s - 3600
        for seq in range(1, 11):
            maindb.insert_text(group, f"g{seq}", time_s=when + seq, group=True, sender="777001", shmsgseq=seq)
        for seq in range(11 + missing, 21 + missing):
            maindb.insert_text(group, f"g{seq}", time_s=when + seq, group=True, sender="777001", shmsgseq=seq)
        return when + 1, when + 20 + missing

    def test_N1_gap_fires_with_evidence(self, store, clock, maindb, poller):
        """§8b M2 1618 + 02 §3.7:群缺 ≥ gap_min_missing(5)条 ⇒ warn QIDIAN_MSG_GAP(subject=account:qd01,
        evidence{window_days=3, total_missing, sessions[{session_id='qd01:g_<群号>', missing, first_ts, last_ts}]}, hint_actions=[]);账号 state 仍 running。"""
        poller.poll_maindb(acct_view())
        first_ts, last_ts = self._seed_group_with_gap(maindb, clock, missing=20)
        poller.poll_maindb(acct_view())                        # 让 table_map 认出群表
        poller.check_group_gaps(acct_view())
        fired = alert_events(store, QIDIAN_MSG_GAP, "firing")
        assert len(fired) == 1
        a = fired[0]
        assert a["severity"] == "warn" and a["subject"] == "account:qd01" and a["hint_actions"] == []
        ev = a["evidence"]
        assert ev["window_days"] == 3 and ev["total_missing"] == 20
        assert ev["sessions"] == [{"session_id": f"qd01:g_{GROUP_G}", "missing": 20, "first_ts": first_ts, "last_ts": last_ts}]
        assert store.get_account("qd01")["state"] == "running"

    def test_N2_private_gap_ignored(self, store, clock, maindb, poller):
        """§2.9.5 check_group_gaps:只对群——私聊的 shmsgseq 不能用,私聊表有再大的缺口也不告警。"""
        poller.poll_maindb(acct_view())
        when = clock.now_s - 3600
        for seq in [1, 2, 3, 500, 501]:
            maindb.insert_text(PEER_A, f"p{seq}", time_s=when + seq, shmsgseq=seq)
        poller.poll_maindb(acct_view())
        poller.check_group_gaps(acct_view())
        assert alert_events(store, QIDIAN_MSG_GAP) == []
        assert not poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")

    def test_N3_below_min_missing_no_alert(self, store, clock, maindb, poller):
        """§2.9.5 check_group_gaps:missing < gap_min_missing(默认 5)不产出;缺 4 条不告警、缺 5 条告警。"""
        poller.poll_maindb(acct_view())
        self._seed_group_with_gap(maindb, clock, group=GROUP_G, missing=4)
        poller.poll_maindb(acct_view())
        poller.check_group_gaps(acct_view())
        assert alert_events(store, QIDIAN_MSG_GAP) == []
        self._seed_group_with_gap(maindb, clock, group="654321", missing=5)
        poller.poll_maindb(acct_view())
        poller.check_group_gaps(acct_view())
        fired = alert_events(store, QIDIAN_MSG_GAP, "firing")
        assert len(fired) == 1 and fired[0]["evidence"]["sessions"][0]["session_id"] == "qd01:g_654321"
        assert fired[0]["evidence"]["sessions"][0]["missing"] == 5 and fired[0]["evidence"]["total_missing"] == 5

    def test_N4_gap_sliding_out_of_window_resolves(self, store, clock, maindb, poller):
        """§8b M2 1618 / 02 §3.7:无状态窗口统计——缺口滑出 gap_window_days(3)天窗口后告警自动 resolved。"""
        poller.poll_maindb(acct_view())
        self._seed_group_with_gap(maindb, clock, missing=20)
        poller.poll_maindb(acct_view())
        poller.check_group_gaps(acct_view())
        assert poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")
        clock.advance(4 * 86400 * 1000)
        poller.check_group_gaps(acct_view())
        assert not poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")
        assert len(alert_events(store, QIDIAN_MSG_GAP, "resolved")) == 1

    def test_N5_sessions_sorted_desc_and_state_running(self, store, clock, maindb, poller):
        """02 §3.7:sessions 按 missing 降序;total_missing 为各群之和;永不 crit。"""
        poller.poll_maindb(acct_view())
        self._seed_group_with_gap(maindb, clock, group="111111", missing=6)
        self._seed_group_with_gap(maindb, clock, group="222222", missing=30)
        poller.poll_maindb(acct_view())
        poller.check_group_gaps(acct_view())
        ev = alert_events(store, QIDIAN_MSG_GAP, "firing")[0]["evidence"]
        assert [s["session_id"] for s in ev["sessions"]] == ["qd01:g_222222", "qd01:g_111111"]
        assert ev["total_missing"] == 36
        assert all(a["severity"] != "crit" for a in alert_events(store, QIDIAN_MSG_GAP))

    def test_N6_no_table_map_neither_fires_nor_resolves(self, store, clock, maindb, poller):
        """§2.9.5 check_group_gaps 首句:进程刚重启、全量轮还没重建映射(table_map 空)⇒ 既不产出也不 resolve(否则会把真实缺口误销)。"""
        poller.poll_maindb(acct_view())
        self._seed_group_with_gap(maindb, clock, missing=20)
        poller.poll_maindb(acct_view())
        poller.check_group_gaps(acct_view())
        assert poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")
        p2 = QidianPoller(store=store, events=poller.events, alerts=poller.alerts_, cfg=AgentConfig(), h13_firing=lambda: False,
                          clock=clock, maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path))   # 进程重启:同一告警态、空映射
        n_before = len(store.list_events(event="alert"))
        p2.check_group_gaps(acct_view())
        assert len(store.list_events(event="alert")) == n_before
        assert poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")

    def test_N7_h13_or_not_running_skips(self, store, clock, maindb, poller):
        """§2.9.5 check_group_gaps:H13 firing 时本轮不判;state != running 时既不产出也不 resolve。"""
        poller.poll_maindb(acct_view())
        self._seed_group_with_gap(maindb, clock, missing=20)
        poller.poll_maindb(acct_view())
        poller.h13_["on"] = True
        poller.check_group_gaps(acct_view())
        assert store.list_events(event="alert") == []
        poller.h13_["on"] = False
        poller.check_group_gaps(acct_view(state="login_required"))
        assert store.list_events(event="alert") == []
        poller.check_group_gaps(acct_view())
        assert poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")
        poller.h13_["on"] = True
        clock.advance(4 * 86400 * 1000)
        poller.check_group_gaps(acct_view())                   # 已滑出窗口但 H13 firing:不判 ⇒ 也不 resolve
        assert poller.alerts_.is_firing(QIDIAN_MSG_GAP, "account:qd01")


# ====================================================================== O. 换登录号重做 bootstrap(06 §2.9.5 ③,02 §3.1 audit_log R6-50,§8b M2 1616 行)

class TestO_Rebootstrap:
    def test_O1_uin_switch_rebuilds_cursors_and_audits_once(self, store, clock, maindb, tmp_path):
        """§8b M2 1616:qd01 退出后用另一个 uin 登录 ⇒ 原 qidian_rowid:* 全部删除后按新库重建;qidian_bootstrap.value=新 uin、value_int 刷新;
        新库历史 0 入库;audit_log 恰一行 action='qidian.rebootstrap'(kind='system'、account_id='qd01'、detail_json.old_uin/new_uin 为两个 uin;
        02 §3.1:transport='system'、actor='system:qidian_adapter'、trace_id NULL、result_code='OK'、detail_json.deleted_cursors=被删行数)。"""
        from conftest import FakeMainDb
        db_new = FakeMainDb(str(tmp_path / "4009.db"), self_uin="4009")
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(str(tmp_path / f"{uid}.db")))
        maindb.insert_text(PEER_A, "旧库 A", time_s=clock.now_s - 3600)
        maindb.insert_text(GROUP_G, "旧库 G", time_s=clock.now_s - 3600, group=True, sender="777001")
        p.poll_maindb(acct_view())
        assert set(rowid_cursors(store)) == {"qidian_rowid:" + PEER_A, "qidian_rowid:g_" + GROUP_G}
        boot_old = store.cursor_get("qd01", "qidian_bootstrap")
        p.poll_maindb(acct_view(state="login_required"))       # 退出
        clock.advance(60_000)
        new_last = db_new.insert_text(PEER_B, "新库历史", time_s=clock.now_s - 7200)
        p.poll_maindb(acct_view(self_uid="4009"))
        wm = rowid_cursors(store)
        assert set(wm) == {"qidian_rowid:" + PEER_B}
        assert wm["qidian_rowid:" + PEER_B][0] == new_last
        boot = store.cursor_get("qd01", "qidian_bootstrap")
        assert boot.value == "4009" and boot.value_int == clock() and boot.value_int != boot_old.value_int
        assert store.count_messages("qd01") == 0 and msg_events(store) == []
        audits = store.list_audit("qidian.rebootstrap")
        assert len(audits) == 1
        a = audits[0]
        assert a["kind"] == "system" and a["account_id"] == "qd01" and a["transport"] == "system"
        assert a["actor"] == "system:qidian_adapter" and a["trace_id"] is None and a["result_code"] == "OK"
        d = json.loads(a["detail_json"])
        assert d["old_uin"] == SELF_UID and d["new_uin"] == "4009" and d["deleted_cursors"] == 2
        clock.advance(5_000); p.poll_maindb(acct_view(self_uid="4009"))
        assert len(store.list_audit("qidian.rebootstrap")) == 1  # 只 audit 一次

    def test_O2_same_uin_relogin_no_rebootstrap(self, store, clock, maindb, poller):
        """§2.9.5 ③:同一 uin 重登 ⇒ 不是「首次」,不删水位、不写 audit、bootstrap 行不变。"""
        maindb.insert_text(PEER_A, "老", time_s=clock.now_s - 3600)
        poller.poll_maindb(acct_view())
        boot = store.cursor_get("qd01", "qidian_bootstrap")
        wm = rowid_cursors(store)
        poller.poll_maindb(acct_view(state="login_required"))
        clock.advance(60_000)
        poller.poll_maindb(acct_view())
        assert store.list_audit("qidian.rebootstrap") == []
        b2 = store.cursor_get("qd01", "qidian_bootstrap")
        assert (b2.value, b2.value_int) == (boot.value, boot.value_int)
        assert rowid_cursors(store) == wm


# ====================================================================== P. 多企点账号内存态隔离(06 §2.9.5 首行,§8b M2 1611 行 ①)

class TestP_MultiAccount:
    def test_P1_two_accounts_isolated(self, store, clock, maindb, tmp_path):
        """§8b M2 1611 ①:qd01/qd02 同时 running、各收一条 ⇒ 两号各入库 1 行,互不影响;qd02 的 poll 不会清掉 qd01 的表映射。"""
        from conftest import FakeMainDb
        store.ensure_account("qd02", "qidian", state="running", self_uid="4002")
        db2 = FakeMainDb(str(tmp_path / "4002.db"), self_uin="4002")
        p, alerts, _ = new_poller(store, clock, lambda uid: LocalSqliteMainDb(str(tmp_path / f"{uid}.db")))
        a1, a2 = acct_view(), acct_view(id="qd02", self_uid="4002")
        p.poll_maindb(a1); p.poll_maindb(a2)
        clock.advance(5_000)
        maindb.insert_text(PEER_A, "给 qd01 的", time_s=clock.now_s, uniseq=120_001)
        db2.insert_text(PEER_A, "给 qd02 的", time_s=clock.now_s, uniseq=120_001)    # 同 uniseq:去重键是 (account_id, ext_msg_id)
        p.poll_maindb(a1)
        map1 = dict(p.state_of("qd01").table_map)
        p.poll_maindb(a2)
        assert p.state_of("qd01").table_map == map1 and map1
        assert [r["text"] for r in all_messages(store, "qd01")] == ["给 qd01 的"]
        assert [r["text"] for r in all_messages(store, "qd02")] == ["给 qd02 的"]
        assert store.cursor_get("qd01", "qidian_bootstrap").value == SELF_UID
        assert store.cursor_get("qd02", "qidian_bootstrap").value == "4002"
        assert len(msg_events(store, "qd01")) == 1 and len(msg_events(store, "qd02")) == 1


# ====================================================================== Q. 未知 msgtype 走完整读循环(06 §2.9.5 ④ / 路由表第 5 行)

class TestQ_UnknownInPoll:
    def test_Q1_unknown_row_no_message_watermark_passes_one_warning(self, store, clock, maindb, poller, caplog):
        """§2.9.5 ④:未知 msgtype 的行不产出、水位照常越过、不抛异常、不停整轮;首见打一条 WARNING;同轮其它文本行照常入库。"""
        poller.poll_maindb(acct_view())
        clock.advance(5_000)
        maindb.insert(PEER_A, group=False, time_s=clock.now_s, msgtype=-2002, msgdata=b"\x0a\x05voice")
        maindb.insert(PEER_A, group=False, time_s=clock.now_s, msgtype=-2002, msgdata=b"\x0a\x05voic2")
        last = maindb.insert_text(PEER_A, "之后的文本", time_s=clock.now_s)
        with caplog.at_level(logging.WARNING):
            poller.poll_maindb(acct_view())
        assert [r["text"] for r in all_messages(store)] == ["之后的文本"]
        assert len(msg_events(store)) == 1
        assert store.cursor_get("qd01", "qidian_rowid:" + PEER_A).value_int == last
        warns = [r for r in caplog.records if r.levelno == logging.WARNING and "-2002" in r.getMessage()]
        assert len(warns) == 1
