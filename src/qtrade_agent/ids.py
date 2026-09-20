"""ID 生成 —— 规格:docs/00 §6(ULID;``msg_``/``ls_`` 前缀;trace_id/run_id 裸 ULID)。纯标准库实现。"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford Base32(ULID 规范)


def ulid(now_ms: int | None = None) -> str:
    """26 字符 ULID:48 位毫秒时间戳 + 80 位随机;同毫秒内不保证单调,本项目按 ts_ms/seq 排序,不靠 ULID 单调。"""
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rnd = int.from_bytes(os.urandom(10), "big")
    value = (ts << 80) | rnd
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def message_id(now_ms: int | None = None) -> str:
    return "msg_" + ulid(now_ms)


def trace_id(now_ms: int | None = None) -> str:
    return ulid(now_ms)


def login_session_id(now_ms: int | None = None) -> str:
    return "ls_" + ulid(now_ms)
