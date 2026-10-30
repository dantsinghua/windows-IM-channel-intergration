"""企点引用回复 ``extStr`` → 引用对象 ``reply``(存储形)—— 规格唯一出处:docs/06 §2.9.5「引用对象 `reply`」(R6-116)。

- ``extStr`` 与 ``msgData`` 同一把 17 字节 XOR 密钥 → UTF-8 → JSON 对象;不含 ``sens_msg_source_msg_info`` → 无引用(``None``,不告警)。
- 该键的值 = Java 序列化的 ``MessageForReplyText$SourceMsgInfo``,用**受限流读取器**取字段:纯数据读取,
  不加载任何类、不反射、不执行;只认一个类、一个顶层对象、字段值只许基本类型 / 字符串 / NULL / 字符串引用 / 只跳过的 ``byte[]``。
- 失败 → ``state="unparsed"`` 其余全 null;正文照常产出。每进程每原因 WARNING 一次,日志只含表名、``_id``、原因。
- 原消息关联只做同会话表内 ``shmsgseq`` + ``msgUid`` 恰 1 行的确定性查询;``resolved_message_id`` 由 store 入库时填。
"""
from __future__ import annotations

import json
import logging
import re
import struct
import threading
from typing import Any, Optional

from ...models import REPLY_STORE_KEYS, unparsed_reply
from ...text import clean_text
from .xor import xor_hex

log = logging.getLogger("qtrade.adapters.qidian.reply")

REPLY_EXT_KEY = "sens_msg_source_msg_info"
SOURCE_MSG_INFO_CLASS = "com.tencent.mobileqq.data.MessageForReplyText$SourceMsgInfo"
MAX_HEX_CHARS = 65536            # ≤ 32 KiB 字节
MAX_HANDLES = 256
REPLY_TEXT_MAX = 2000
SENDER_NAME_MAX = 128

STREAM_MAGIC = b"\xac\xed\x00\x05"
TC_NULL, TC_REFERENCE, TC_CLASSDESC, TC_OBJECT, TC_STRING, TC_ARRAY, TC_ENDBLOCKDATA, TC_LONGSTRING = (
    0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x78, 0x7C)
BASE_WIRE_HANDLE = 0x7E0000
SC_WRITE_METHOD, SC_SERIALIZABLE, SC_EXTERNALIZABLE, SC_ENUM = 0x01, 0x02, 0x04, 0x10

_PRIM_SIZES = {"B": 1, "C": 2, "D": 8, "F": 4, "I": 4, "J": 8, "S": 2, "Z": 1}
_INT_FMT = {"B": ">b", "C": ">H", "I": ">i", "J": ">q", "S": ">h", "Z": ">?"}
_HEX = re.compile(r"[0-9A-Fa-f]*")

_warned: set[str] = set()
_warned_lock = threading.Lock()


class SourceMsgInfoError(ValueError):
    """受限解析失败;``reason`` 进 WARNING(不含内容与字节)。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Handle:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any = None):
        self.kind = kind          # "string" | "classdesc" | "array_classdesc" | "object" | "array"
        self.value = value


class _Reader:
    def __init__(self, data: bytes):
        self.b = data
        self.i = 0
        self.handles: list[_Handle] = []

    def take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.b):
            raise SourceMsgInfoError("truncated")
        out = self.b[self.i:self.i + n]
        self.i += n
        return out

    def u1(self) -> int:
        return self.take(1)[0]

    def u2(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def s4(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def u8(self) -> int:
        return struct.unpack(">Q", self.take(8))[0]

    def assign(self, h: _Handle) -> None:
        if len(self.handles) >= MAX_HANDLES:
            raise SourceMsgInfoError("too_many_handles")
        self.handles.append(h)

    def handle(self) -> _Handle:
        idx = self.s4() - BASE_WIRE_HANDLE
        if not 0 <= idx < len(self.handles):
            raise SourceMsgInfoError("bad_reference")
        return self.handles[idx]

    def mutf8(self, n: int) -> str:
        raw = self.take(n)
        try:
            s = raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
            return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        except UnicodeError:
            raise SourceMsgInfoError("bad_utf") from None

    def utf(self) -> str:
        return self.mutf8(self.u2())

    def string_after_tc(self, tc: int) -> str:
        """``TC_STRING``/``TC_LONGSTRING`` 已读,读正文并分配句柄。"""
        n = self.u2() if tc == TC_STRING else self.u8()
        if n > len(self.b) - self.i:
            raise SourceMsgInfoError("truncated")
        s = self.mutf8(n)
        self.assign(_Handle("string", s))
        return s

    def string_or_ref(self) -> str:
        """字段描述里的类型串:只许新字符串或指向已出现字符串的引用。"""
        tc = self.u1()
        if tc in (TC_STRING, TC_LONGSTRING):
            return self.string_after_tc(tc)
        if tc == TC_REFERENCE:
            h = self.handle()
            if h.kind != "string":
                raise SourceMsgInfoError("forbidden_marker")
            return h.value
        raise SourceMsgInfoError("forbidden_marker")

    def class_tail(self) -> None:
        """classAnnotation 只许立即 ``TC_ENDBLOCKDATA``;超类描述必须为 ``TC_NULL``。"""
        if self.u1() != TC_ENDBLOCKDATA:
            raise SourceMsgInfoError("forbidden_marker")
        if self.u1() != TC_NULL:
            raise SourceMsgInfoError("bad_superclass")


def _read_fields(r: _Reader) -> list[tuple[str, str]]:
    count = r.u2()
    fields: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _ in range(count):
        code = chr(r.u1())
        name = r.utf()
        if name in seen:
            raise SourceMsgInfoError("bad_class_desc")
        seen.add(name)
        if code in _PRIM_SIZES:
            fields.append((code, name))
        elif code in ("L", "["):
            r.string_or_ref()             # 字段类型串;只用来推进流,不据此加载任何东西
            fields.append((code, name))
        else:
            raise SourceMsgInfoError("bad_class_desc")
    return fields


def _read_byte_array(r: _Reader) -> None:
    """``TC_ARRAY`` 已读:类描述只许 ``[B``(新描述或引用已出现的 ``[B`` 描述);内容只跳过、不保留。"""
    tc = r.u1()
    if tc == TC_CLASSDESC:
        h = _Handle("array_classdesc")
        r.assign(h)
        name = r.utf()
        if name != "[B":
            raise SourceMsgInfoError("forbidden_marker")
        r.u8()                            # serialVersionUID
        flags = r.u1()
        if flags != SC_SERIALIZABLE or r.u2() != 0:
            raise SourceMsgInfoError("forbidden_marker")
        r.class_tail()
        h.value = name
    elif tc == TC_REFERENCE:
        h = r.handle()
        if h.kind != "array_classdesc" or h.value != "[B":
            raise SourceMsgInfoError("forbidden_marker")
    else:
        raise SourceMsgInfoError("forbidden_marker")
    r.assign(_Handle("array"))
    n = r.s4()
    if n < 0:
        raise SourceMsgInfoError("bad_array")
    r.take(n)


def _read_value(r: _Reader, code: str) -> Any:
    if code in _PRIM_SIZES:
        raw = r.take(_PRIM_SIZES[code])
        if code == "D":
            return struct.unpack(">d", raw)[0]
        if code == "F":
            return struct.unpack(">f", raw)[0]
        return struct.unpack(_INT_FMT[code], raw)[0]
    tc = r.u1()
    if tc == TC_NULL:
        return None
    if code == "L" and tc in (TC_STRING, TC_LONGSTRING):
        return r.string_after_tc(tc)
    if code == "L" and tc == TC_REFERENCE:
        h = r.handle()
        if h.kind != "string":
            raise SourceMsgInfoError("forbidden_marker")
        return h.value
    if code == "[" and tc == TC_ARRAY:
        _read_byte_array(r)
        return None
    raise SourceMsgInfoError("forbidden_marker")


def parse_source_msg_info(value: Any) -> dict[str, Any]:
    """``sens_msg_source_msg_info`` 的值(十六进制串)→ ``{字段名: 值}``(按流内字段表;``byte[]`` 记 ``None``)。

    任何不合规 → ``SourceMsgInfoError(reason)``。不写死字段顺序与个数。
    """
    if not isinstance(value, str):
        raise SourceMsgInfoError("not_string")
    if len(value) > MAX_HEX_CHARS:
        raise SourceMsgInfoError("too_long")
    if len(value) % 2 or not _HEX.fullmatch(value):
        raise SourceMsgInfoError("bad_hex")
    r = _Reader(bytes.fromhex(value))
    if r.take(4) != STREAM_MAGIC:
        raise SourceMsgInfoError("bad_magic")
    if r.u1() != TC_OBJECT:
        raise SourceMsgInfoError("not_single_object")
    if r.u1() != TC_CLASSDESC:
        raise SourceMsgInfoError("forbidden_marker")
    r.assign(_Handle("classdesc"))
    if r.utf() != SOURCE_MSG_INFO_CLASS:
        raise SourceMsgInfoError("bad_class_name")
    r.u8()                                # serialVersionUID(实测 1;不作判据)
    flags = r.u1()
    if not flags & SC_SERIALIZABLE or flags & (SC_WRITE_METHOD | SC_EXTERNALIZABLE | SC_ENUM):
        raise SourceMsgInfoError("bad_class_flags")
    fields = _read_fields(r)
    r.class_tail()
    r.assign(_Handle("object"))
    out: dict[str, Any] = {}
    for code, name in fields:
        out[name] = _read_value(r, code)
    if r.i != len(r.b):
        raise SourceMsgInfoError("trailing_bytes")
    return out


# ---------------------------------------------------------------- 字段映射(06 §2.9.5 表)

def _int_field(fields: dict[str, Any], name: str) -> Optional[int]:
    v = fields.get(name)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _positive_str(fields: dict[str, Any], name: str) -> Optional[str]:
    v = _int_field(fields, name)
    return str(v) if v is not None and v > 0 else None


def _clean(v: Any, limit: int) -> Optional[str]:
    if not isinstance(v, str) or not v:
        return None
    s = clean_text(v)[:limit]
    return s or None


def reply_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """受限解析出的字段 → 存储形九键(``state="ok"``;关联两键先置 null)。"""
    ts = _int_field(fields, "mSourceMsgTime")
    name = None
    for key in ("senderName", "mSourceQdMsgUserName"):
        name = _clean(fields.get(key), SENDER_NAME_MAX)
        if name is not None:
            break
    out: dict[str, Any] = dict.fromkeys(REPLY_STORE_KEYS)
    out.update(
        state="ok",
        text=_clean(fields.get("mSourceMsgText"), REPLY_TEXT_MAX),
        sender_id=_positive_str(fields, "mSourceMsgSenderUin"),
        sender_name=name,
        ts_ms=ts * 1000 if ts is not None and ts > 0 else None,
        source_msg_seq=_positive_str(fields, "mSourceMsgSeq"),
        source_msg_uid=_positive_str(fields, "origUid"),
    )
    return out


def _warn_once(reason: str, table: str, row_id: Any) -> None:
    with _warned_lock:
        if reason in _warned:
            return
        _warned.add(reason)
    log.warning("qidian 引用对象无法解析 table=%s _id=%s reason=%s(每进程每原因首见一次;正文照常产出)", table, row_id, reason)


def decode_reply(extstr_hex: str, *, table: str = "", row_id: Any = None) -> Optional[dict[str, Any]]:
    """``MainDbRow.extstr_hex`` → 引用对象存储形或 ``None``(无引用)。不抛异常。"""
    if not extstr_hex:
        return None
    try:
        obj = json.loads(xor_hex(extstr_hex).decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or REPLY_EXT_KEY not in obj:
        return None
    try:
        return reply_from_fields(parse_source_msg_info(obj[REPLY_EXT_KEY]))
    except SourceMsgInfoError as e:
        _warn_once(e.reason, table, row_id)
    except Exception:                     # 防御:受限读取器之外的意外不得拖垮正文产出
        _warn_once("internal", table, row_id)
    return unparsed_reply()


def link_source_ext_msg_id(db: Any, table: str, reply: Optional[dict[str, Any]]) -> None:
    """同一会话表内 ``shmsgseq=<seq> AND msgUid=<uid>`` 恰 1 行且 ``uniseq≠0`` → ``source_ext_msg_id="qd:<uniseq>"``;
    0 行 / ≥2 行 / 查询报错 → null。不抛出。"""
    if not isinstance(reply, dict):
        return
    reply["source_ext_msg_id"] = None
    if reply.get("state") != "ok" or reply.get("source_msg_seq") is None or reply.get("source_msg_uid") is None:
        return
    try:
        rows = db.reply_source_uniseqs(table, int(reply["source_msg_seq"]), int(reply["source_msg_uid"]))
    except Exception:
        return
    if len(rows) == 1 and rows[0]:
        reply["source_ext_msg_id"] = f"qd:{int(rows[0])}"
