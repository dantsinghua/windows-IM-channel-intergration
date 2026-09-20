"""ID 生成 —— 规格:docs/00 §6(ULID;``ls_`` 前缀;trace_id/run_id 裸 ULID)。与 Agent 侧 ``qtrade_agent.ids`` 同一实现。"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford Base32(ULID 规范)


def ulid(now_ms: int | None = None) -> str:
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def trace_id(now_ms: int | None = None) -> str:
    return ulid(now_ms)


def run_id(now_ms: int | None = None) -> str:
    """一轮探测一个 ULID(02 §3.2 ``probe_results.run_id``)。"""
    return ulid(now_ms)


def login_session_id(now_ms: int | None = None) -> str:
    """微信登录会话(05 §2.4.2 ``login_session_id``;00 §6 前缀 ``ls_``)。"""
    return "ls_" + ulid(now_ms)
