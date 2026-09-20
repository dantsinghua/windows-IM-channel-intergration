"""events 模块(02 §2.2.7)—— ``emit`` 非阻塞:落 ``events_outbox``(经 store)+ 内存广播队列 ``put_nowait``,绝不在调用方持锁路径里同步回调订阅方。

``message`` 事件的 payload = 00 §7.4 Message + 三个只在事件里出现、不落库的字段(R6-39/R6-40/R6-49):
``lag_s``(整数秒 = received_at − ts)、``late``(= lag_s > [messages] late_after_s)、``origin: "rpa"|"external"``(仅出向行)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .ids import ulid
from .models import Message

log = logging.getLogger("qtrade.events")

EVENT_NAMES = ("message", "account_state", "command_done", "alert", "resource", "mail", "net", "workflow", "job")


def message_payload(msg: Message, *, late_after_s: int, origin: Optional[str] = None) -> dict[str, Any]:
    """落库结构 + 事件专属三字段。``origin`` 只对出向行给(``"rpa"`` / ``"external"``);入向行不带该键。"""
    received_ms = msg.received_ms if msg.received_ms is not None else msg.ts_ms
    lag_s = max(0, (received_ms - msg.ts_ms) // 1000)
    payload: dict[str, Any] = {
        "id": msg.id, "ext_msg_id": msg.ext_msg_id, "account_id": msg.account_id, "channel": msg.channel,
        "session": {"id": msg.session_id, "name": msg.session.name, "kind": msg.session.kind},
        "dir": msg.dir, "type": msg.type, "state": msg.state,
        "text": msg.text, "text_len": len(msg.text) if msg.text is not None else None, "fingerprint": msg.fingerprint,
        "media": msg.media, "sender": {"id": msg.sender_id, "name": msg.sender_name}, "self": msg.self,
        "ts_ms": msg.ts_ms, "received_ms": received_ms, "source": msg.source, "revoked": msg.revoked, "raw_ref": msg.raw_ref,
        "lag_s": lag_s, "late": lag_s > late_after_s,
    }
    if msg.dir == "out" and origin is not None:
        payload["origin"] = origin
    return payload


class Events:
    def __init__(self, store, *, queue_max: int = 10000):
        self._store = store
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self.dropped = 0

    def emit(self, event: str, *, payload: dict[str, Any], account_id: Optional[str] = None,
             channel: Optional[str] = None, trace_id: Optional[str] = None, now_ms: Optional[int] = None) -> int:
        """写 outbox(target='ws' 规范记录)+ 入内存队列;返回 outbox ``seq``。同步、非阻塞(只做一次短事务)。"""
        if event not in EVENT_NAMES:
            raise ValueError(f"unknown event {event!r}")
        event_id = ulid(now_ms)
        seq = self._store.insert_outbox_event(event_id=event_id, target="ws", event=event, trace_id=trace_id,
                                              account_id=account_id, channel=channel,
                                              payload_json=json.dumps(payload, ensure_ascii=False), now_ms=now_ms)
        frame = {"event": event, "seq": seq, "trace_id": trace_id, "account_id": account_id, "channel": channel,
                 "payload": payload}
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            # 内存队列丢不等于事件丢:outbox 已落库,WS 客户端可 since_seq 补拉
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(frame)
            self.dropped += 1
            log.warning("events 内存广播队列已满,丢最旧一条(dropped=%d)", self.dropped)
        return seq

    async def next_frame(self) -> dict[str, Any]:
        return await self._queue.get()

    def replay(self, since_seq: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self._store.replay_outbox(since_seq, limit)
