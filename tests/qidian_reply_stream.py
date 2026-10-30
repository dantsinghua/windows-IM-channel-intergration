"""测试辅助:按 docs/06 §2.9.5「实测类描述」自行构造的**合成** ``SourceMsgInfo`` Java 序列化流(R6-116)。

不含任何真实聊天内容、真实 uin 或真实样本字节;所有取值都是测试里编的。
句柄按 Java 序列化规则分配:类描述先分配、其字段类型串依次分配、对象随后分配;
值里的新字符串 / ``[B`` 类描述 / 数组各占一个句柄。``handles`` 记录最终分配个数,供句柄上限的边界用例使用。
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Optional

from qtrade_agent.adapters.qidian.xor import xor

CLASS_NAME = "com.tencent.mobileqq.data.MessageForReplyText$SourceMsgInfo"
REPLY_KEY = "sens_msg_source_msg_info"
MAGIC = b"\xac\xed\x00\x05"
TC_NULL, TC_REFERENCE, TC_CLASSDESC, TC_OBJECT, TC_STRING, TC_ARRAY = 0x70, 0x71, 0x72, 0x73, 0x74, 0x75
TC_CLASS, TC_BLOCKDATA, TC_ENDBLOCKDATA, TC_LONGSTRING, TC_ENUM = 0x76, 0x77, 0x78, 0x7C, 0x7E
BASE = 0x7E0000

PRIM_FMT = {"B": ">b", "C": ">H", "D": ">d", "F": ">f", "I": ">i", "J": ">q", "S": ">h", "Z": ">?"}
TYPE_STRING = {"L": "Ljava/lang/String;", "[": "[B"}

# 合成取值(> 2^53 的 origUid 用来验证「字符串保存、不丢精度」)
SYN_SENDER_UIN = 10000000001
SYN_TO_UIN = 10000000002
SYN_SEQ = 64001
SYN_TIME_S = 1790000000
SYN_ORIG_UID = 72057594037927937          # = 2^56 + 1 > 2^53
SYN_TEXT = "合成被引用摘要 1Y 1.70"
SYN_SENDER_NAME = "合成昵称甲"
SYN_QD_USER_NAME = "合成昵称乙"


@dataclass(frozen=True)
class Raw:
    """直接写入值位置的原始字节(用来注入嵌套对象、枚举、块数据等非法标记)。``handles`` = 这些字节会让读取方分配的句柄数。"""
    data: bytes
    handles: int = 0


@dataclass(frozen=True)
class RefTo:
    """``TC_REFERENCE`` 指向某个字段已写出的字符串值。"""
    field: str


@dataclass(frozen=True)
class LongStr:
    """以 ``TC_LONGSTRING`` 编码的字符串。"""
    value: str


@dataclass(frozen=True)
class HandleRef:
    """``TC_REFERENCE`` 指向绝对句柄序号(0 = 顶层类描述)。"""
    index: int


def default_fields() -> list[list[Any]]:
    """06 §2.9.5 实测类描述:22 字段,按流中顺序;取值全部合成。"""
    return [
        ["I", "mLocalAtInfoDone", 0], ["J", "mSourceMsgSenderUin", SYN_SENDER_UIN], ["J", "mSourceMsgSeq", SYN_SEQ],
        ["I", "mSourceMsgTime", SYN_TIME_S], ["J", "mSourceMsgToUin", SYN_TO_UIN], ["I", "mSourceQdMsgUser", 0],
        ["I", "mSourceSummaryFlag", 1], ["I", "mType", 0], ["I", "oriMsgType", -1000], ["J", "origUid", SYN_ORIG_UID],
        ["I", "replyPicHeight", 0], ["I", "replyPicWidth", 0], ["J", "uniseq", 0],
        ["L", "mAnonymousNickName", None], ["L", "mAtInfoStr", None], ["L", "mRichMsg", None],
        ["[", "mSourceMessageByte", b"\x01\x02\x03\x04"], ["L", "mSourceMsgText", SYN_TEXT], ["L", "mSourceMsgTroopName", None],
        ["L", "mSourceQdMsgUid", "synthetic-qd-uid"], ["L", "mSourceQdMsgUserName", SYN_QD_USER_NAME], ["L", "senderName", SYN_SENDER_NAME],
    ]


def fields_with(**overrides: Any) -> list[list[Any]]:
    out = default_fields()
    for f in out:
        if f[1] in overrides:
            f[2] = overrides[f[1]]
    return out


def fields_without(*names: str) -> list[list[Any]]:
    return [f for f in default_fields() if f[1] not in names]


def _utf(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


class _Builder:
    def __init__(self) -> None:
        self.out = bytearray()
        self.handles = 0
        self.type_handles: dict[str, int] = {}
        self.value_handles: dict[str, int] = {}
        self.byte_array_desc: Optional[int] = None

    def assign(self) -> int:
        h = self.handles
        self.handles += 1
        return h

    def ref(self, idx: int) -> bytes:
        return bytes([TC_REFERENCE]) + struct.pack(">i", BASE + idx)

    def type_string(self, s: str) -> bytes:
        if s in self.type_handles:
            return self.ref(self.type_handles[s])
        self.type_handles[s] = self.assign()
        return bytes([TC_STRING]) + _utf(s)

    def byte_array(self, data: bytes) -> bytes:
        if self.byte_array_desc is None:
            self.byte_array_desc = self.assign()
            desc = bytes([TC_CLASSDESC]) + _utf("[B") + struct.pack(">q", -5984413125824719648) + b"\x02" + b"\x00\x00" + bytes([TC_ENDBLOCKDATA, TC_NULL])
        else:
            desc = self.ref(self.byte_array_desc)
        self.assign()
        return bytes([TC_ARRAY]) + desc + struct.pack(">i", len(data)) + data

    def value(self, code: str, name: str, v: Any) -> bytes:
        if isinstance(v, Raw):
            self.handles += v.handles
            return v.data
        if code in PRIM_FMT:
            return struct.pack(PRIM_FMT[code], v)
        if v is None:
            return bytes([TC_NULL])
        if isinstance(v, RefTo):
            return self.ref(self.value_handles[v.field])
        if isinstance(v, HandleRef):
            return self.ref(v.index)
        if code == "[":
            return self.byte_array(v)
        if isinstance(v, LongStr):
            b = v.value.encode("utf-8")
            self.value_handles[name] = self.assign()
            return bytes([TC_LONGSTRING]) + struct.pack(">Q", len(b)) + b
        b = v.encode("utf-8")
        self.value_handles[name] = self.assign()
        return bytes([TC_STRING]) + struct.pack(">H", len(b)) + b


def build_stream(fields: Optional[list[list[Any]]] = None, *, class_name: str = CLASS_NAME, suid: int = 1, flags: int = 0x02,
                 superclass: bytes = bytes([TC_NULL]), annotation: bytes = bytes([TC_ENDBLOCKDATA]), magic: bytes = MAGIC,
                 trailing: bytes = b"", type_strings: Optional[dict[str, str]] = None) -> tuple[bytes, int]:
    """→ (流字节, 读取方应分配的句柄总数)。``type_strings`` 可覆盖某字段的类型串(如把 ``[`` 字段声明成 ``[I``)。"""
    fields = default_fields() if fields is None else fields
    b = _Builder()
    out = bytearray(magic)
    out.append(TC_OBJECT)
    out.append(TC_CLASSDESC)
    b.assign()                                           # 顶层类描述
    out += _utf(class_name) + struct.pack(">q", suid) + bytes([flags]) + struct.pack(">H", len(fields))
    for code, name, _ in fields:
        out.append(ord(code))
        out += _utf(name)
        if code in ("L", "["):
            ts = (type_strings or {}).get(name, TYPE_STRING[code])
            out += b.type_string(ts)
    out += annotation + superclass
    b.assign()                                           # 对象本身
    for code, name, v in fields:
        out += b.value(code, name, v)
    out += trailing
    return bytes(out), b.handles


def stream_hex(fields: Optional[list[list[Any]]] = None, *, upper: bool = True, **kw: Any) -> str:
    raw, _ = build_stream(fields, **kw)
    return raw.hex().upper() if upper else raw.hex()


def ext_hex(obj: Any) -> str:
    """extStr 明文 JSON(或任意字节) → 主库 ``hex(extStr)`` 的样子:17 字节 XOR 后取大写十六进制。"""
    raw = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return xor(raw).hex().upper()


def reply_ext_hex(fields: Optional[list[list[Any]]] = None, *, value: Any = None, extra: Optional[dict] = None, **kw: Any) -> str:
    """含 ``sens_msg_source_msg_info`` 的 extStr(hex)。``value`` 不为 None 时直接用它作该键的值。"""
    v = value if value is not None else stream_hex(fields, **kw)
    obj = {"qidian_svr_msg": "synthetic", "c2c_msg_seq": "1", **(extra or {}), REPLY_KEY: v}
    return ext_hex(obj)
