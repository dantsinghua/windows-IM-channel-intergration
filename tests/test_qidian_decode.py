"""XOR 密钥、msgtype 一级路由、-1035 混排 —— 对照 docs/06 §2.9.5 权威表。"""
import logging

from qtrade_agent.adapters.qidian.msgdata import (JAVA_SERIAL_MAGIC, MainDbRow, MessageFactory, decode, decode_mixed)
from qtrade_agent.adapters.qidian.xor import KEY, decode_uin, xor, xor_hex


def test_xor_key_is_17_ascii_bytes_and_involutive():
    assert KEY == b"02:00:00:00:00:00" and len(KEY) == 17
    b = "债券报价 1Y 1.70 😀".encode("utf-8") * 3
    assert xor(xor(b)) == b
    assert xor(b"A")[0] == ord("A") ^ ord("0")
    assert xor(bytes(18))[17] == ord("0")                  # 第 18 个字节回到密钥第 1 位(i % 17)
    assert xor_hex(xor(b"415011447").hex()) == b"415011447"
    assert decode_uin(xor(b"415011447").hex()) == "415011447"


def _pb_field(fno: int, payload: bytes) -> bytes:
    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)
    return varint((fno << 3) | 2) + varint(len(payload)) + payload


def text_elem(s: str) -> bytes:
    return _pb_field(1, _pb_field(1, _pb_field(1, s.encode("utf-8"))))      # Elem(field1=Text(field1=str))


def image_elem() -> bytes:
    body = _pb_field(4, _pb_field(1, b"{ABC}.jpg") + _pb_field(2, b"http://gchat.qpic.cn/gchatpic_new/1/2-3-4/0?term=2"))
    return _pb_field(1, body)


def test_text_family_routes_to_text_with_clean_text():
    for mt in (-1000, -1051, -1049):
        d = decode(mt, "1Y\n1.70 @张三\u0014A".encode("utf-8"))
        assert d.emit and d.type == "text" and d.text == "1Y\n1.70 @张三[爱你]" and not d.unknown and d.kind == "text"


def test_java_serial_rows_never_emit_and_count_unknown_only_for_new_types():
    d = decode(-2017, JAVA_SERIAL_MAGIC + b"\x00\x01TroopFileData")
    assert not d.emit and not d.unknown and d.kind == "java_serial"
    d2 = decode(-2099, JAVA_SERIAL_MAGIC + b"xx")
    assert not d2.emit and d2.unknown                                    # 新类型若也是 Java 序列化会先落到本行,不能静默


def test_silent_and_other_types():
    for mt in (-2000, -2006, -5040, -2018):
        d = decode(mt, b"\x08\x01whatever")
        assert not d.emit and not d.unknown and d.kind == "silent"
    d = decode(-7777, b"\x08\x01")
    assert not d.emit and d.unknown and d.kind == "other"


def test_mixed_keeps_elem_order_and_needs_text_segment():
    assert decode_mixed(image_elem() + text_elem("看这张")) == "[图片]看这张"     # 79%:图在前
    assert decode_mixed(text_elem("如图") + image_elem()) == "如图[图片]"
    assert decode_mixed(image_elem()) is None                                  # 纯图片:不产出
    d = decode(-1035, image_elem() + text_elem("报价\u0014A"))
    assert d.emit and d.text == "[图片]报价[爱你]"
    assert decode(-1035, b"\xff\xff").emit is False                            # 解析失败:不产出、不抛


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def real_text_elem(s: str) -> bytes:
    return _pb_field(1, _pb_field(1, s.encode("utf-8")))                  # R6-66 真机结构:Elem.1 直接是正文


def pic_elem(w: int, h: int, img_type: int) -> bytes:
    rec = (_pb_field(1, b"{ABC}.jpg") + _pb_field(7, b"picplatform")
           + _pb_field(8, b"/gchatpic_new/1/2-3-4/198?term=2")
           + _varint(24 << 3) + _varint(w) + _varint(25 << 3) + _varint(h) + _varint(26 << 3) + _varint(img_type))
    return _pb_field(1, _pb_field(2, rec))


def test_mixed_real_db_layout_text_segment_is_elem_1_directly():
    # 真实库 -1035:文本段 Elem.1 直接是 UTF-8 正文;修正前整条被当纯图片丢掉
    assert decode_mixed(pic_elem(320, 40, 1001) + real_text_elem("测试文本甲")) == "[图片 320×40]测试文本甲"
    assert decode_mixed(real_text_elem("如图") + pic_elem(240, 180, 2000)) == "如图[动图 240×180]"
    d = decode(-1035, real_text_elem("好的\u0014\x08") + pic_elem(64, 64, 2000))
    assert d.emit and d.text == "好的[玫瑰][动图 64×64]"
    assert decode_mixed(pic_elem(64, 64, 2000)) is None                       # 纯图片仍不产出(R6-44 不变)


def test_mixed_image_without_size_falls_back_to_plain_placeholder():
    assert decode_mixed(real_text_elem("看") + image_elem()) == "看[图片]"


def test_message_factory_builds_message_and_warns_once(caplog):
    f = MessageFactory("qd01", "3007373675")
    row = MainDbRow(id=7, issend=0, time=1_758_240_000, msgtype=-1000, uniseq=555, senderuin_hex=xor(b"415011447").hex(),
                    msgdata_hex=xor("收到".encode()).hex())
    m, unknown = f.to_message(row, table="t", native_id="415011447", kind="private")
    assert m is not None and not unknown
    assert (m.dir, m.self, m.type, m.text, m.ext_msg_id, m.source, m.ts_ms) == ("in", False, "text", "收到", "qd:555", "qidian_db", 1_758_240_000_000)
    assert m.session.id == "qd01:415011447" and m.sender_id == "415011447"
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            m2, unk = f.to_message(MainDbRow(8, 1, 1, -4242, 556, "", xor(b"x").hex()), table="t", native_id="415011447", kind="private")
            assert m2 is None and unk
    assert sum("未见过的 msgtype" in r.message for r in caplog.records) == 1     # 首见只告警一次
