"""企点引用对象 ``reply`` 的读取与解码 —— 对照 docs/06 §2.9.5「引用对象 `reply`」整条(R6-116)。

覆盖:extStr 解码 ①~⑤、受限解析的输入限制与非法标记、字段映射、失败语义(WARNING 每原因一次、不含内容)、
同表确定性关联、``MainDbRow`` 7 参构造、Local / Adb 两后端第 8 列、缺 ``extStr`` 列的窄回退(总控已裁定)。
所有序列化流都由 ``tests/qidian_reply_stream.py`` 合成。
"""
from __future__ import annotations

import logging
import sqlite3
import struct
import subprocess

import pytest

from qtrade_agent.adapters.qidian import maindb as maindb_mod
from qtrade_agent.adapters.qidian import reply as reply_mod
from qtrade_agent.adapters.qidian.maindb import AdbMainDb, LocalSqliteMainDb, MainDbError
from qtrade_agent.adapters.qidian.msgdata import MainDbRow, MessageFactory
from qtrade_agent.adapters.qidian.reply import decode_reply, link_source_ext_msg_id
from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.text import clean_text
from tests.qidian_reply_stream import (CLASS_NAME, REPLY_KEY, SYN_ORIG_UID, SYN_QD_USER_NAME, SYN_SENDER_NAME, SYN_SENDER_UIN,
                                       SYN_SEQ, SYN_TEXT, SYN_TIME_S, TC_BLOCKDATA, TC_CLASS, TC_CLASSDESC, TC_ENDBLOCKDATA, TC_ENUM,
                                       TC_NULL, TC_OBJECT, TC_REFERENCE, TC_STRING, HandleRef, LongStr, Raw, RefTo, build_stream,
                                       default_fields, ext_hex, fields_with, fields_without, reply_ext_hex, stream_hex)

STORE_KEYS = ("state", "text", "sender_id", "sender_name", "ts_ms", "source_msg_seq", "source_msg_uid",
              "source_ext_msg_id", "resolved_message_id")
UNPARSED = {"state": "unparsed", **{k: None for k in STORE_KEYS[1:]}}
LOGGER = "qtrade.adapters.qidian.reply"


@pytest.fixture(autouse=True)
def _fresh_warned():
    reply_mod._warned.clear()
    yield
    reply_mod._warned.clear()


def dec(ext: str):
    return decode_reply(ext, table="mr_friend_SYNTHETIC_New", row_id=42)


def expected_ok(**over):
    base = {"state": "ok", "text": SYN_TEXT, "sender_id": str(SYN_SENDER_UIN), "sender_name": SYN_SENDER_NAME,
            "ts_ms": SYN_TIME_S * 1000, "source_msg_seq": str(SYN_SEQ), "source_msg_uid": str(SYN_ORIG_UID),
            "source_ext_msg_id": None, "resolved_message_id": None}
    base.update(over)
    return base


# ───────────────────────────────────────────── 正常 ok:全字段映射
def test_ok_full_mapping_nine_keys():
    r = dec(reply_ext_hex())
    assert r == expected_ok()
    assert len(r) == 9 and set(r) == set(STORE_KEYS)
    assert isinstance(r["source_msg_uid"], str) and int(r["source_msg_uid"]) > 2 ** 53     # 实测值 > 2^53 → 字符串
    assert isinstance(r["ts_ms"], int) and r["ts_ms"] == SYN_TIME_S * 1000                # 秒 × 1000
    assert isinstance(r["sender_id"], str) and isinstance(r["source_msg_seq"], str)


def test_lowercase_hex_value_is_also_hex():
    assert dec(reply_ext_hex(value=stream_hex(upper=False))) == expected_ok()


def test_field_order_and_count_come_from_stream_not_hardcoded():
    reordered = list(reversed(default_fields()))
    reordered.sort(key=lambda f: f[0] in ("L", "["))           # 仍是「基本类型在前」,但同类内顺序完全倒过来
    assert dec(reply_ext_hex(reordered)) == expected_ok()
    extra = default_fields() + [["Z", "mSyntheticNewFlag", True], ["L", "mSyntheticNewText", "x"]]
    assert dec(reply_ext_hex(extra)) == expected_ok()


@pytest.mark.parametrize("missing,key", [
    ("mSourceMsgText", "text"), ("mSourceMsgSenderUin", "sender_id"), ("mSourceMsgTime", "ts_ms"),
    ("mSourceMsgSeq", "source_msg_seq"), ("origUid", "source_msg_uid"),
])
def test_missing_field_maps_to_null_not_failure(missing, key):
    r = dec(reply_ext_hex(fields_without(missing)))
    assert r == expected_ok(**{key: None})


def test_missing_both_name_fields_is_null_name():
    assert dec(reply_ext_hex(fields_without("senderName", "mSourceQdMsgUserName"))) == expected_ok(sender_name=None)


@pytest.mark.parametrize("sender_name", [None, ""])
def test_sender_name_falls_back_to_qd_user_name(sender_name):
    assert dec(reply_ext_hex(fields_with(senderName=sender_name))) == expected_ok(sender_name=SYN_QD_USER_NAME)


def test_sender_name_both_empty_is_null():
    assert dec(reply_ext_hex(fields_with(senderName="", mSourceQdMsgUserName=None)))["sender_name"] is None


def test_sender_name_clean_text_and_truncated_to_128():
    long_name = "名" * 300
    assert dec(reply_ext_hex(fields_with(senderName=long_name)))["sender_name"] == "名" * 128
    assert dec(reply_ext_hex(fields_with(senderName="甲\x01乙")))["sender_name"] == clean_text("甲\x01乙") == "甲乙"


def test_text_clean_text_and_truncated_to_2000():
    raw = "摘" * 2500
    assert dec(reply_ext_hex(fields_with(mSourceMsgText=raw)))["text"] == "摘" * 2000
    assert len(dec(reply_ext_hex(fields_with(mSourceMsgText="摘" * 2000)))["text"]) == 2000
    face = "报价\u0014A\x01行2"
    assert dec(reply_ext_hex(fields_with(mSourceMsgText=face)))["text"] == clean_text(face)
    assert "\x01" not in clean_text(face) and "\u0014" not in clean_text(face)


@pytest.mark.parametrize("value", ["", None])
def test_text_empty_or_null_is_null(value):
    assert dec(reply_ext_hex(fields_with(mSourceMsgText=value)))["text"] is None


@pytest.mark.parametrize("field,key", [("mSourceMsgSenderUin", "sender_id"), ("mSourceMsgSeq", "source_msg_seq"),
                                       ("origUid", "source_msg_uid"), ("mSourceMsgTime", "ts_ms")])
@pytest.mark.parametrize("value", [0, -5])
def test_non_positive_numbers_are_null(field, key, value):
    assert dec(reply_ext_hex(fields_with(**{field: value})))[key] is None


def test_longstring_and_string_reference_values_are_allowed():
    f = fields_with(mSourceMsgText=LongStr("长串摘要"), senderName=RefTo("mSourceQdMsgUserName"))
    assert dec(reply_ext_hex(f)) == expected_ok(text="长串摘要", sender_name=SYN_QD_USER_NAME)


def test_second_byte_array_may_reference_the_seen_classdesc_and_is_skipped():
    f = default_fields() + [["[", "mSyntheticBytes2", b"\x09" * 7]]
    assert dec(reply_ext_hex(f)) == expected_ok()


def test_unused_fields_do_not_leak_into_reply():
    r = dec(reply_ext_hex(fields_with(uniseq=987654, mRichMsg="rich-synthetic", mAtInfoStr="at-synthetic",
                                      mSourceMessageByte=b"leak-synthetic")))
    assert r == expected_ok()                               # SourceMsgInfo.uniseq / mRichMsg / mAtInfoStr / mSourceMessageByte 不用


# ───────────────────────────────────────────── 无引用:None、不告警
@pytest.mark.parametrize("ext", [
    "",
    ext_hex({"qidian_svr_msg": "synthetic", "c2c_msg_seq": "9"}),          # 普通行:无该键
    ext_hex(b"not json at all"),
    ext_hex(b"[1, 2, 3]"),                                                  # 顶层不是对象
    ext_hex(b"\"just a string\""),
    ext_hex(b"\xff\xfe\xfd{}"),                                             # 非 UTF-8
])
def test_no_reply_cases_return_none_without_warning(ext, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert dec(ext) is None
    assert not [r for r in caplog.records if r.name == LOGGER]


def test_non_hex_extstr_does_not_raise():
    # 06 未列此情形(DB 的 hex() 不会产出);只锁「不抛、不影响正文」
    assert dec("ZZ-not-hex") in (None, UNPARSED)


# ───────────────────────────────────────────── unparsed:输入限制与非法标记
def _ok_stream(**kw):
    return build_stream(**kw)[0]


def _value_hex(raw: bytes) -> str:
    return raw.hex().upper()


def _pad_to_total_hex(total_hex_chars: int) -> str:
    """造一条合法流,用 byte[] 把总长度凑到恰好 ``total_hex_chars`` 个十六进制字符。"""
    base, _ = build_stream(fields_with(mSourceMessageByte=b""))
    need = total_hex_chars // 2 - len(base)
    raw, _ = build_stream(fields_with(mSourceMessageByte=b"\xab" * need))
    assert len(raw) * 2 == total_hex_chars
    return _value_hex(raw)


def test_hex_length_boundary_65536_ok_65538_unparsed():
    assert dec(reply_ext_hex(value=_pad_to_total_hex(65536)))["state"] == "ok"
    assert dec(reply_ext_hex(value=_pad_to_total_hex(65538))) == UNPARSED


def nested_object() -> Raw:
    desc = (bytes([TC_CLASSDESC]) + struct.pack(">H", 9) + b"x.Synthe1" + struct.pack(">q", 1) + b"\x02" + b"\x00\x00"
            + bytes([TC_ENDBLOCKDATA, TC_NULL]))
    return Raw(bytes([TC_OBJECT]) + desc, handles=2)


def enum_value() -> Raw:
    desc = (bytes([TC_CLASSDESC]) + struct.pack(">H", 9) + b"x.SynEnum" + struct.pack(">q", 0) + b"\x12" + b"\x00\x00"
            + bytes([TC_ENDBLOCKDATA, TC_NULL]))
    return Raw(bytes([TC_ENUM]) + desc + bytes([TC_STRING]) + struct.pack(">H", 1) + b"A", handles=3)


def int_array() -> bytes:
    desc = (bytes([TC_CLASSDESC]) + struct.pack(">H", 2) + b"[I" + struct.pack(">q", 1) + b"\x02" + b"\x00\x00"
            + bytes([TC_ENDBLOCKDATA, TC_NULL]))
    return bytes([0x75]) + desc + struct.pack(">i", 1) + struct.pack(">i", 7)


UNPARSED_CASES = {
    "value_not_string": dict(value=12345),
    "value_empty_string": dict(value=""),
    "odd_length": dict(value=_value_hex(_ok_stream()) + "A"),
    "non_hex_chars": dict(value="ZZ" + _value_hex(_ok_stream())[2:]),
    "too_long": dict(value="AB" * 32769),
    "bad_magic": dict(value=_value_hex(_ok_stream(magic=b"\xac\xed\x00\x06"))),
    "class_name_mismatch": dict(value=_value_hex(_ok_stream(class_name=CLASS_NAME + "X"))),
    "class_name_other": dict(value=_value_hex(_ok_stream(class_name="com.tencent.mobileqq.data.SyntheticOther"))),
    "trailing_bytes": dict(value=_value_hex(_ok_stream(trailing=b"\x00"))),
    "second_top_level_object": dict(value=_value_hex(_ok_stream() + _ok_stream()[4:])),
    "sc_write_method": dict(value=_value_hex(_ok_stream(flags=0x03))),
    "sc_externalizable": dict(value=_value_hex(_ok_stream(flags=0x06))),
    "not_serializable": dict(value=_value_hex(_ok_stream(flags=0x00))),
    "superclass_not_null": dict(value=_value_hex(_ok_stream(superclass=bytes([TC_REFERENCE]) + struct.pack(">i", 0x7E0000)))),
    "nested_object": dict(fields=fields_with(mRichMsg=nested_object())),
    "enum": dict(fields=fields_with(mAtInfoStr=enum_value())),
    "class": dict(fields=fields_with(mRichMsg=Raw(bytes([TC_CLASS, TC_NULL])))),
    "blockdata": dict(fields=fields_with(mRichMsg=Raw(bytes([TC_BLOCKDATA, 1, 0])))),
    "non_byte_array": dict(fields=fields_with(mSourceMessageByte=Raw(int_array(), handles=2))),
    "reference_to_classdesc": dict(fields=fields_with(mSourceMsgText=HandleRef(0))),
    "reference_out_of_range": dict(fields=fields_with(mSourceMsgText=HandleRef(200))),
    "truncated": dict(value=_value_hex(_ok_stream()[:-3])),
    "annotation_not_end": dict(value=_value_hex(_ok_stream(annotation=bytes([TC_BLOCKDATA, 1, 0, TC_ENDBLOCKDATA])))),
}


@pytest.mark.parametrize("case", sorted(UNPARSED_CASES))
def test_unparsed_cases(case):
    kw = UNPARSED_CASES[case]
    ext = reply_ext_hex(kw["fields"]) if "fields" in kw else reply_ext_hex(value=kw["value"])
    r = dec(ext)
    assert r == UNPARSED and len(r) == 9


def _with_extra_strings(n: int):
    f = default_fields() + [["L", f"mSynExtra{i:03d}", f"v{i}"] for i in range(n)]
    return f, build_stream(f)[1]


def test_handle_limit_256_ok_257_unparsed():
    _, base_handles = build_stream()
    f256, h256 = _with_extra_strings(256 - base_handles)
    f257, h257 = _with_extra_strings(257 - base_handles)
    assert (h256, h257) == (256, 257)
    assert dec(reply_ext_hex(f256))["state"] == "ok"
    assert dec(reply_ext_hex(f257)) == UNPARSED


# ───────────────────────────────────────────── 失败语义:WARNING 每原因一次、不含内容与字节
def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_warning_once_per_reason_and_without_content(caplog):
    secret = "合成机密摘要QWERTY"
    bad1 = stream_hex(fields_with(mSourceMsgText=secret), class_name=CLASS_NAME + "X")
    bad2 = stream_hex(fields_with(mSourceMsgText=secret), trailing=b"\x00")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for row_id in (101, 102, 103):
            assert decode_reply(reply_ext_hex(value=bad1), table="mr_friend_SYNA_New", row_id=row_id) == UNPARSED
        assert len(_warnings(caplog)) == 1
        assert decode_reply(reply_ext_hex(value=bad2), table="mr_friend_SYNB_New", row_id=201) == UNPARSED
        assert decode_reply(reply_ext_hex(value=bad2), table="mr_friend_SYNB_New", row_id=202) == UNPARSED
    ws = _warnings(caplog)
    assert len(ws) == 2                                        # 两种原因各一次
    first, second = ws[0].getMessage(), ws[1].getMessage()
    assert "mr_friend_SYNA_New" in first and "101" in first
    assert "mr_friend_SYNB_New" in second and "201" in second
    assert first != second
    for w in ws:
        msg = w.getMessage()
        assert secret not in msg and secret.encode("utf-8").hex().upper() not in msg.upper()
        assert bad1[:64] not in msg and bad1[-64:] not in msg and bad2[-64:] not in msg
        assert CLASS_NAME not in msg


# ───────────────────────────────────────────── MessageFactory:不看 msgtype、正文照常
def _row(msgtype: int, msgdata: bytes, ext: str = "", *, row_id: int = 7, uniseq: int = 555) -> MainDbRow:
    return MainDbRow(row_id, 0, 1_790_000_100, msgtype, uniseq, xor(b"10000000009").hex(), xor(msgdata).hex(), ext)


def _pb(fno: int, payload: bytes) -> bytes:
    return bytes([(fno << 3) | 2, len(payload)]) + payload


@pytest.mark.parametrize("msgtype", [-1000, -1051, -1049])
def test_factory_attaches_reply_for_text_family_and_text_unchanged(msgtype):
    f = MessageFactory("qd01", "3007373675")
    body = "合成回复正文 B".encode("utf-8")
    m_plain, _ = f.to_message(_row(msgtype, body), table="t", native_id="10000000009", kind="private")
    m_reply, _ = f.to_message(_row(msgtype, body, reply_ext_hex()), table="t", native_id="10000000009", kind="private")
    assert m_plain.reply is None and m_reply.reply == expected_ok()
    assert m_reply.text == m_plain.text == "合成回复正文 B"
    assert SYN_TEXT not in m_reply.text


def test_factory_mixed_1035_with_reply_is_parsed_too():
    f = MessageFactory("qd01", "3007373675")
    m, _ = f.to_message(_row(-1035, _pb(1, _pb(1, "图文".encode("utf-8"))), reply_ext_hex()), table="t", native_id="1", kind="private")
    assert m is not None and m.text == "图文" and m.reply == expected_ok()


def test_factory_unparsed_reply_keeps_message():
    f = MessageFactory("qd01", "3007373675")
    m, _ = f.to_message(_row(-1049, "合成正文".encode("utf-8"), reply_ext_hex(value="ZZ")), table="t", native_id="1", kind="private")
    assert m is not None and m.text == "合成正文" and m.reply == UNPARSED


def test_factory_non_emitting_row_still_not_emitted_with_reply():
    f = MessageFactory("qd01", "3007373675")
    m, _ = f.to_message(_row(-2000, b"\x08\x01", reply_ext_hex()), table="t", native_id="1", kind="private")
    assert m is None                                           # 「只产出文本」路由不变


# ───────────────────────────────────────────── 确定性关联(link_source_ext_msg_id)
class FakeDb:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def reply_source_uniseqs(self, table, shmsgseq, msg_uid):
        self.calls.append((table, shmsgseq, msg_uid))
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.mark.parametrize("result,expected", [([4242], "qd:4242"), ([], None), ([11, 12], None), ([0], None)])
def test_link_one_zero_two_rows_and_uniseq_zero(result, expected):
    r = expected_ok()
    db = FakeDb(result)
    link_source_ext_msg_id(db, "mr_friend_SYN_New", r)
    assert r["source_ext_msg_id"] == expected
    assert db.calls == [("mr_friend_SYN_New", SYN_SEQ, SYN_ORIG_UID)]
    assert all(type(a) is int for a in db.calls[0][1:])


@pytest.mark.parametrize("exc", [MainDbError("schema_mismatch", "no such column: msgUid"), RuntimeError("boom"), OSError("x")])
def test_link_query_error_is_null_and_does_not_raise(exc):
    r = expected_ok()
    link_source_ext_msg_id(FakeDb(exc=exc), "t", r)
    assert r["source_ext_msg_id"] is None


def test_link_db_without_method_is_null():
    r = expected_ok()
    link_source_ext_msg_id(object(), "t", r)
    assert r["source_ext_msg_id"] is None


@pytest.mark.parametrize("reply", [dict(UNPARSED), expected_ok(source_msg_seq=None), expected_ok(source_msg_uid=None)])
def test_link_preconditions_skip_query(reply):
    db = FakeDb([1])
    link_source_ext_msg_id(db, "t", reply)
    assert db.calls == [] and reply["source_ext_msg_id"] is None


def test_link_none_reply_is_noop():
    db = FakeDb([1])
    link_source_ext_msg_id(db, "t", None)
    assert db.calls == []


# ───────────────────────────────────────────── MainDbRow
def test_maindbrow_seven_positional_args_still_work():
    r = MainDbRow(1, 0, 2, -1000, 3, "AA", "BB")
    assert r.extstr_hex == ""
    assert MainDbRow(1, 0, 2, -1000, 3, "AA", "BB", "CC").extstr_hex == "CC"


# ───────────────────────────────────────────── LocalSqliteMainDb:第 8 列与同表关联
TBL = "mr_friend_" + "A" * 32 + "_New"
TBL2 = "mr_friend_" + "B" * 32 + "_New"


def make_table(path, name=TBL, *, extstr=True, msguid=True, msgdata=True):
    cols = ["_id INTEGER PRIMARY KEY AUTOINCREMENT", "issend INTEGER", "istroop INTEGER", "time INTEGER", "msgtype INTEGER",
            "uniseq INTEGER", "msgseq INTEGER", "shmsgseq INTEGER", "senderuin BLOB", "frienduin BLOB"]
    if msgdata:
        cols.append("msgData BLOB")
    if extstr:
        cols.append("extStr BLOB")
    if msguid:
        cols.append("msgUid INTEGER")
    con = sqlite3.connect(path)
    con.execute(f'CREATE TABLE "{name}" ({", ".join(cols)})')
    con.commit()
    con.close()


def add_row(path, name=TBL, *, uniseq, shmsgseq=1, msguid=1, extstr=None, text="x"):
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
    v = {"issend": 0, "istroop": 0, "time": 1_790_000_000, "msgtype": -1000, "uniseq": uniseq, "msgseq": uniseq,
         "shmsgseq": shmsgseq, "senderuin": xor(b"10000000009"), "frienduin": xor(b"10000000009"),
         "msgData": xor(text.encode()), "extStr": extstr, "msgUid": msguid}
    v = {k: x for k, x in v.items() if k in cols}
    con.execute(f'INSERT INTO "{name}"({",".join(v)}) VALUES ({",".join("?" * len(v))})', tuple(v.values()))
    con.commit()
    con.close()


def test_local_rows_after_reads_hex_extstr_as_eighth_column(tmp_path):
    p = str(tmp_path / "m.db")
    make_table(p)
    ext_plain = bytes.fromhex(reply_ext_hex())
    add_row(p, uniseq=1, extstr=ext_plain)
    add_row(p, uniseq=2, extstr=None)
    rows = LocalSqliteMainDb(p).rows_after(TBL, 0)
    assert rows[0].extstr_hex.upper() == ext_plain.hex().upper()
    assert rows[1].extstr_hex == ""                            # NULL → hex() 空串 → 无引用


def test_local_rows_after_batch_still_limited_to_500(tmp_path):
    p = str(tmp_path / "m.db")
    make_table(p)
    con = sqlite3.connect(p)
    con.executemany(f'INSERT INTO "{TBL}"(issend,istroop,time,msgtype,uniseq,shmsgseq,senderuin,frienduin,msgData) VALUES (0,0,1,-1000,?,?,?,?,?)',
                    [(i, i, b"", b"", b"") for i in range(1, 502)])
    con.commit()
    con.close()
    assert len(LocalSqliteMainDb(p).rows_after(TBL, 0)) == 500


def test_local_missing_extstr_column_falls_back_to_seven_columns(tmp_path, caplog):
    """总控裁定:报错同含 no such column 与 extStr → 该表退回 7 列、按无引用处理、正文照常。"""
    p = str(tmp_path / "m.db")
    make_table(p, extstr=False)
    add_row(p, uniseq=1, text="正文照常")
    rows = LocalSqliteMainDb(p).rows_after(TBL, 0)
    assert len(rows) == 1 and rows[0].extstr_hex == ""
    assert bytes(xor(bytes.fromhex(rows[0].msgdata_hex))).decode() == "正文照常"


def test_local_other_missing_column_still_raises(tmp_path):
    p = str(tmp_path / "m.db")
    make_table(p, msgdata=False)
    add_row(p, uniseq=1)
    with pytest.raises(MainDbError) as ei:
        LocalSqliteMainDb(p).rows_after(TBL, 0)
    assert ei.value.reason == "schema_mismatch"


def test_local_reply_source_uniseqs_same_table_only(tmp_path):
    p = str(tmp_path / "m.db")
    make_table(p)
    make_table(p, TBL2)
    add_row(p, uniseq=9001, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)
    add_row(p, uniseq=9002, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID + 1)     # uid 不同
    add_row(p, TBL2, uniseq=9003, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)  # 别的会话表
    db = LocalSqliteMainDb(p)
    assert db.reply_source_uniseqs(TBL, SYN_SEQ, SYN_ORIG_UID) == [9001]
    r = expected_ok()
    link_source_ext_msg_id(db, TBL, r)
    assert r["source_ext_msg_id"] == "qd:9001"


def test_local_reply_source_two_rows_or_zero_uniseq_is_null(tmp_path):
    p = str(tmp_path / "m.db")
    make_table(p)
    make_table(p, TBL2)
    add_row(p, uniseq=1, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)
    add_row(p, uniseq=2, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)
    add_row(p, TBL2, uniseq=0, shmsgseq=SYN_SEQ, msguid=SYN_ORIG_UID)
    db = LocalSqliteMainDb(p)
    assert len(db.reply_source_uniseqs(TBL, SYN_SEQ, SYN_ORIG_UID)) == 2
    for t in (TBL, TBL2):
        r = expected_ok()
        link_source_ext_msg_id(db, t, r)
        assert r["source_ext_msg_id"] is None


def test_local_reply_source_query_error_is_null(tmp_path):
    p = str(tmp_path / "m.db")
    make_table(p, msguid=False)
    add_row(p, uniseq=1, shmsgseq=SYN_SEQ)
    r = expected_ok()
    link_source_ext_msg_id(LocalSqliteMainDb(p), TBL, r)
    assert r["source_ext_msg_id"] is None


# ───────────────────────────────────────────── AdbMainDb:第 8 列
SERIAL = "127.0.0.1:16001"
UID = "3000008246"


@pytest.fixture
def fake_adb(monkeypatch):
    calls = []

    def install(responder):
        def run(argv, **kwargs):
            calls.append(argv)
            assert isinstance(argv, list) and not kwargs.get("shell", False)
            rc, out, err = responder(argv[6])
            return subprocess.CompletedProcess(argv, rc, out, err)
        monkeypatch.setattr(maindb_mod.subprocess, "run", run)
        return calls
    return install


def test_adb_rows_after_parses_eighth_column(fake_adb):
    calls = fake_adb(lambda cmd: (0, "7|0|1790000000|-1049|12|3132|7465|ABCD\n8|0|1790000001|-1000|13|3132|7465|\n", ""))
    rows = AdbMainDb(SERIAL, UID, adb="test-only-adb").rows_after(TBL, 6)
    assert [(r.id, r.extstr_hex) for r in rows] == [(7, "ABCD"), (8, "")]
    assert "hex(extStr)" in calls[0][6] and "?mode=ro" in calls[0][6] and "LIMIT 500" in calls[0][6]


def test_adb_missing_extstr_column_falls_back(fake_adb):
    def responder(cmd):
        if "hex(extStr)" in cmd:
            return 1, "", "Error: in prepare, no such column: extStr (1)"
        return 0, "7|0|1790000000|-1000|12|3132|7465\n", ""
    calls = fake_adb(responder)
    rows = AdbMainDb(SERIAL, UID, adb="test-only-adb").rows_after(TBL, 6)
    assert [(r.id, r.uniseq, r.extstr_hex) for r in rows] == [(7, 12, "")]
    assert len(calls) == 2


def test_adb_other_schema_error_still_raises(fake_adb):
    fake_adb(lambda cmd: (1, "", "Error: in prepare, no such column: msgData (1)"))
    with pytest.raises(MainDbError):
        AdbMainDb(SERIAL, UID, adb="test-only-adb").rows_after(TBL, 6)


def test_adb_reply_source_uniseqs_sql(fake_adb):
    calls = fake_adb(lambda cmd: (0, "9001\n", ""))
    assert AdbMainDb(SERIAL, UID, adb="test-only-adb").reply_source_uniseqs(TBL, SYN_SEQ, SYN_ORIG_UID) == [9001]
    cmd = calls[0][6]
    assert f"shmsgseq={SYN_SEQ}" in cmd and f"msgUid={SYN_ORIG_UID}" in cmd and "LIMIT 2" in cmd
    assert f'"{TBL}"' in cmd and "?mode=ro" in cmd


def test_adb_reply_link_error_is_null(fake_adb):
    fake_adb(lambda cmd: (1, "", "Error: no such column: msgUid"))
    r = expected_ok()
    link_source_ext_msg_id(AdbMainDb(SERIAL, UID, adb="test-only-adb"), TBL, r)
    assert r["source_ext_msg_id"] is None
