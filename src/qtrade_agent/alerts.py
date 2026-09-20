"""告警(00 §7.5 告警类事件统一 payload;去重键 ``(code, subject)``;码由 02 §3.7 登记)。

本期只登记企点读库路会产出的四个 warn 码;``firing``/``resolve`` 只在状态翻转时发事件,重复 firing 累加 ``count``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

QIDIAN_NOT_ROOT = "QIDIAN_NOT_ROOT"
QIDIAN_DB_UNAVAILABLE = "QIDIAN_DB_UNAVAILABLE"
QIDIAN_TABLE_DECODE_STUCK = "QIDIAN_TABLE_DECODE_STUCK"
QIDIAN_MSG_GAP = "QIDIAN_MSG_GAP"

# 02 §3.7:这四个码永不 crit、不改 state、能力不减
REGISTERED = {
    QIDIAN_NOT_ROOT: "warn",
    QIDIAN_DB_UNAVAILABLE: "warn",
    QIDIAN_TABLE_DECODE_STUCK: "warn",
    QIDIAN_MSG_GAP: "warn",
}


@dataclass
class ActiveAlert:
    code: str
    subject: str
    severity: str
    first_seen_ms: int
    last_seen_ms: int
    count: int = 1
    evidence: dict[str, Any] = field(default_factory=dict)
    hint_actions: list[str] = field(default_factory=list)


class Alerts:
    def __init__(self, events, *, clock):
        self._events = events
        self._clock = clock
        self.active: dict[tuple[str, str], ActiveAlert] = {}

    def _payload(self, a: ActiveAlert, state: str) -> dict[str, Any]:
        return {"code": a.code, "severity": a.severity, "state": state, "subject": a.subject,
                "title": None, "message": None, "hint_actions": a.hint_actions,
                "first_seen_at": a.first_seen_ms, "last_seen_at": a.last_seen_ms, "count": a.count, "evidence": a.evidence}

    def firing(self, code: str, *, subject: str, severity: Optional[str] = None, evidence: Optional[dict[str, Any]] = None,
               hint_actions: Optional[list[str]] = None, account_id: Optional[str] = None) -> bool:
        """返回 True = 本次是新 firing(发了事件);False = 已在 firing,只累加。"""
        sev = severity or REGISTERED.get(code, "warn")
        now = self._clock()
        key = (code, subject)
        a = self.active.get(key)
        if a is not None:
            a.count += 1
            a.last_seen_ms = now
            a.evidence = evidence or a.evidence
            return False
        a = ActiveAlert(code, subject, sev, now, now, 1, dict(evidence or {}), list(hint_actions or []))
        self.active[key] = a
        self._events.emit("alert", payload=self._payload(a, "firing"), account_id=account_id, now_ms=now)
        return True

    def resolve(self, code: str, *, subject: str, account_id: Optional[str] = None) -> bool:
        key = (code, subject)
        a = self.active.pop(key, None)
        if a is None:
            return False
        a.last_seen_ms = self._clock()
        self._events.emit("alert", payload=self._payload(a, "resolved"), account_id=account_id, now_ms=a.last_seen_ms)
        return True

    def is_firing(self, code: str, subject: str) -> bool:
        return (code, subject) in self.active
