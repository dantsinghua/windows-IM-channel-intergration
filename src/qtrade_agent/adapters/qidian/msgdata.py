"""企点主库 ``msgData`` → 基线 §7.4 Message —— 规格唯一出处:docs/06 §2.9.5「type/text 按 msgtype 一级路由」权威表(R6-43~R6-46)。

读库路**只产出文本**;不产出的行返回 ``None``、水位照常越过(由调用方保证)。
- Java 序列化魔数 ``AC ED 00 05``(``-2017`` 群文件 / ``-2011`` 链接卡片):不产出;msgtype ∉ {-2017,-2011} 时计入 unknown + 首见 WARNING
- 文本族 ``{-1000, -1051, -1049}``:整体 UTF-8 明文 → ``clean_text(b.decode("utf-8", errors="replace"))``
- ``-1035`` 图文混排:protobuf 顶层 repeated field 1 = Elem;文本段取 Elem.1.1、非文本段写 ``[图片 W×H]`` / ``[动图 W×H]``
  (R6-66;解不出宽高写 ``[图片]``),**按 Elem 原顺序拼接**;有文本段才产出
- ``{-2000, -2006, -5040, -2018}``:不产出、不计 unknown
- 其它任何 msgtype:不产出;计数 + 每种首次出现打一条 WARNING(进程内集合)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional

from ...models import Message, Session
from ...text import IMAGE_PLACEHOLDER, clean_text
from .xor import decode_uin, xor_hex

log = logging.getLogger("qtrade.adapters.qidian.msgdata")

JAVA_SERIAL_MAGIC = b"\xac\xed\x00\x05"
TEXT_FAMILY = frozenset({-1000, -1051, -1049})
MIXED_TEXT_IMAGE = -1035
JAVA_SERIAL_TYPES = frozenset({-2017, -2011})
SILENT_TYPES = frozenset({-2000, -2006, -5040, -2018})   # 群图片 / 固定系统串 / 系统提示 / 会话事件:不产出、不计 unknown

# -1035 非文本段的内容特征(06 §2.9.5:用内容特征判、不死记字段号;企点是魔改版,与标准 msg_body.proto 对不齐)
IMAGE_FEATURES = (b"picplatform", b"/download?appid=", b"/gchatpic_new/")


@dataclass(frozen=True)
class MainDbRow:
    """``SELECT _id, issend, time, msgtype, uniseq, hex(senderuin), hex(msgData)`` 的一行(06 §2.9.5 ④)。"""
    id: int
    issend: int
    time: int          # 秒级 Unix,明文
    msgtype: int
    uniseq: int
    senderuin_hex: str
    msgdata_hex: str


@dataclass
class Decoded:
    emit: bool                 # = 06 权威表「产出 Message?」
    type: Optional[str]        # 产出时恒 "text"
    text: Optional[str]
    unknown: bool              # 命中「其它」行,或 Java 序列化行里 msgtype ∉ {-2017,-2011}
    kind: str                  # 命中的路由行,便于日志/测试:java_serial | text | mixed | silent | other


# ---------------------------------------------------------------- protobuf 最小解析(只认 varint 与 length-delimited)

def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    shift, val = 0, 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not (c & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def iter_fields(b: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """产出 (field_no, wire_type, value);wire 2 → bytes,wire 0 → int;wire 1/5 跳过定长;其它 → 停止。"""
    i = 0
    n = len(b)
    while i < n:
        key, i = _read_varint(b, i)
        fno, wt = key >> 3, key & 0x7
        if wt == 0:
            v, i = _read_varint(b, i)
            yield fno, wt, v
        elif wt == 2:
            ln, i = _read_varint(b, i)
            if i + ln > n:
                raise ValueError("truncated length-delimited")
            yield fno, wt, b[i:i + ln]
            i += ln
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")


def _strict_fields(b: bytes) -> Optional[list[tuple[int, int, bytes | int]]]:
    """整段恰好解析成 protobuf 字段序列才返回;有剩余/截断/非法 wire type → None。"""
    try:
        return list(iter_fields(b))
    except ValueError:
        return None


def _elem_text(elem: bytes) -> Optional[str]:
    """文本段取正文:① 规格写的 Elem.1.1(field 1 = Text 子消息,其 field 1 = str);
    ② R6-66 真机勘误:企点真实库里 Elem.1 **直接就是** UTF-8 正文(少一层 Text 包装,
    全库 -1035 实测如此;只按 ① 解会把文本段当成图片段、整条混排不产出)。
    先试 ①(须整段严格解析成功),不成再试 ②;都不是合法 UTF-8 → None(当非文本段)。"""
    fields = _strict_fields(elem)
    if not fields:
        return None
    for fno, wt, v in fields:
        if fno == 1 and wt == 2:
            inner = _strict_fields(v)                       # type: ignore[arg-type]
            if inner:
                for f2, w2, v2 in inner:
                    if f2 == 1 and w2 == 2:
                        try:
                            return v2.decode("utf-8")      # type: ignore[union-attr]
                        except UnicodeDecodeError:
                            break
            try:
                return v.decode("utf-8")                   # type: ignore[union-attr]
            except UnicodeDecodeError:
                return None
    return None


# R6-66:图片段里的 PicRec(Elem 内含 picplatform 特征的那个子消息)field 24/25 = 宽/高、26 = 图片类型;
# 类型 2000 = GIF 动图(实测安琳发的表情包即此类)。库里没有「这是表情包」的专属标记,故只写事实、不猜。
PIC_W, PIC_H, PIC_TYPE, PIC_TYPE_GIF = 24, 25, 26, 2000


def image_placeholder(elem: bytes) -> str:
    """图片段 → ``[图片 W×H]`` / ``[动图 W×H]``;找不到 PicRec 或宽高 → ``[图片]``(IMAGE_PLACEHOLDER)。"""
    try:
        for _, wt, v in iter_fields(elem):
            if wt != 2 or not any(f in v for f in IMAGE_FEATURES):   # type: ignore[operator]
                continue
            meta = {fno: val for fno, w2, val in iter_fields(v) if w2 == 0}  # type: ignore[arg-type]
            w, h = meta.get(PIC_W), meta.get(PIC_H)
            if not w or not h:
                return IMAGE_PLACEHOLDER
            kind = "动图" if meta.get(PIC_TYPE) == PIC_TYPE_GIF else "图片"
            return f"[{kind} {w}×{h}]"
    except ValueError:
        pass
    return IMAGE_PLACEHOLDER


def decode_mixed(b: bytes) -> Optional[str]:
    """-1035:按 Elem 原顺序拼接文本段与图片占位(``image_placeholder``);无文本段 → None(不产出)。"""
    parts: list[str] = []
    has_text = False
    try:
        for fno, wt, v in iter_fields(b):
            if fno != 1 or wt != 2:
                continue
            elem = v  # type: ignore[assignment]
            text = _elem_text(elem)  # type: ignore[arg-type]
            if text is not None and not any(f in elem for f in IMAGE_FEATURES):  # type: ignore[operator]
                parts.append(clean_text(text))
                has_text = True
            else:
                parts.append(image_placeholder(elem))   # type: ignore[arg-type]  # 目前 105/105 非文本段全是图片;将来语音/短视频再按样本分化
    except ValueError:
        return None
    if not has_text:
        return None
    return "".join(parts)


# ---------------------------------------------------------------- 路由

def decode(msgtype: int, msgdata: bytes) -> Decoded:
    """06 §2.9.5 权威表,自上而下先命中先用。``msgdata`` 是 XOR 解码后的原始字节。"""
    if msgdata[:4] == JAVA_SERIAL_MAGIC:
        return Decoded(False, None, None, msgtype not in JAVA_SERIAL_TYPES, "java_serial")
    if msgtype in TEXT_FAMILY:
        return Decoded(True, "text", clean_text(msgdata.decode("utf-8", errors="replace")), False, "text")
    if msgtype == MIXED_TEXT_IMAGE:
        text = decode_mixed(msgdata)
        return Decoded(text is not None, "text" if text is not None else None, text, False, "mixed")
    if msgtype in SILENT_TYPES:
        return Decoded(False, None, None, False, "silent")
    return Decoded(False, None, None, True, "other")


class MessageFactory:
    """``to_message(r) -> (Message | None, unknown)``(06 §2.9.5 ④);未知 msgtype 的「见过」集合是进程内状态。"""

    def __init__(self, account_id: str, self_uid: str):
        self.account_id = account_id
        self.self_uid = self_uid
        self.seen_unknown: set[int] = set()

    def to_message(self, r: MainDbRow, *, table: str, native_id: str, kind: str) -> tuple[Optional[Message], bool]:
        try:
            raw = xor_hex(r.msgdata_hex)
        except ValueError:
            log.warning("qidian msgData hex 非法 table=%s _id=%s", table, r.id)
            return None, True
        d = decode(r.msgtype, raw)
        if d.unknown and r.msgtype not in self.seen_unknown:
            self.seen_unknown.add(r.msgtype)
            log.warning("qidian 未见过的 msgtype=%s table=%s _id=%s(首见,不产出、水位照常越过)", r.msgtype, table, r.id)
        if not d.emit:
            return None, d.unknown
        try:
            sender = decode_uin(r.senderuin_hex)
        except ValueError:
            sender = ""
        is_self = r.issend == 1
        msg = Message(
            account_id=self.account_id,
            channel="qidian",
            session=Session(self.account_id, native_id, kind, name=native_id),   # 读库路没有会话标题;标题仅控件树兜底路用
            dir="out" if is_self else "in",
            type="text",
            text=d.text,
            ts_ms=r.time * 1000,
            source="qidian_db",
            ext_msg_id=f"qd:{r.uniseq}",
            dedup_kind="native",
            sender_id=sender or None,
            sender_name=None,
            self=is_self,
            state="DELIVERED",
        )
        return msg, d.unknown
