"""告警(00 §7.5 告警类事件统一 payload;去重键 ``(code, subject)``;码由 02 §3.7 登记)。

``REGISTERED`` = 02 §3.7「severity」列;``EVENT_FAMILY`` = 同表「事件类型」列(``alert`` 以外还有
``mail`` / ``resource`` / ``net``)。``firing``/``resolve`` 只在状态翻转时发事件,重复 firing 累加 ``count``;
事件族缺省按 ``EVENT_FAMILY`` 取(未登记码回落 ``alert``),调用方也可用 ``event=`` 显式覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .events import iso8601
from .mail.codes import MAIL_ALERT_CODES


def _iso(ms: Optional[int]) -> Optional[str]:
    return iso8601(int(ms)) if ms else None

QIDIAN_NOT_ROOT = "QIDIAN_NOT_ROOT"
QIDIAN_DB_UNAVAILABLE = "QIDIAN_DB_UNAVAILABLE"
QIDIAN_TABLE_DECODE_STUCK = "QIDIAN_TABLE_DECODE_STUCK"
QIDIAN_MSG_GAP = "QIDIAN_MSG_GAP"
QIDIAN_PROFILE_FALLBACK = "QIDIAN_PROFILE_FALLBACK"
H02_WINAGENT_API_DOWN = "H02_WINAGENT_API_DOWN"
H03_DOCKERD_DOWN = "H03_DOCKERD_DOWN"
H13_CLOCK_DRIFT = "H13_CLOCK_DRIFT"
MEM_PRESSURE = "MEM_PRESSURE"
WA_VERSION_MISMATCH = "WA_VERSION_MISMATCH"
H04_CONTAINER_EXITED = "H04_CONTAINER_EXITED"
H05_BOOT_INCOMPLETE = "H05_BOOT_INCOMPLETE"
H06_ADB_OFFLINE = "H06_ADB_OFFLINE"
CONTAINER_OOM_KILLED = "CONTAINER_OOM_KILLED"
ACCOUNT_OFFLINE = "ACCOUNT_OFFLINE"
AUTO_RESTART_EXHAUSTED = "AUTO_RESTART_EXHAUSTED"
H07_SCRCPY_STALLED = "H07_SCRCPY_STALLED"
H08_NAPCAT_HEARTBEAT_LOST = "H08_NAPCAT_HEARTBEAT_LOST"
H12_DISK_LOW = "H12_DISK_LOW"
H21_WECHAT_HOSTS_BLOCK_FAILED = "H21_WECHAT_HOSTS_BLOCK_FAILED"
DB_WRITE_FAILED = "DB_WRITE_FAILED"
WEBHOOK_DEAD = "WEBHOOK_DEAD"
EVENTS_QUEUE_OVERFLOW = "EVENTS_QUEUE_OVERFLOW"
WECHAT_SWITCH_FAILED = "WECHAT_SWITCH_FAILED"
WINAGENT_USER_OFFLINE = "WINAGENT_USER_OFFLINE"
POOL_CALIBRATION_DRIFT = "POOL_CALIBRATION_DRIFT"
NET_PUBLIC_ENDPOINT_CHANGED = "NET_PUBLIC_ENDPOINT_CHANGED"

# 02 §3.7:企点四码永不 crit、不改 state、能力不减;H13 warn(04 F-13:仅对齐失败才告知);MEM_PRESSURE warn(crit 由水位升);
# H04/H05 crit;H06 warn(3 次无效升 crit,由调用方传 severity);CONTAINER_OOM_KILLED warn;ACCOUNT_OFFLINE warn(05 §2.5.4:1 小时内第 3 次升 error)
REGISTERED = {
    QIDIAN_NOT_ROOT: "warn",
    QIDIAN_DB_UNAVAILABLE: "warn",
    QIDIAN_TABLE_DECODE_STUCK: "warn",
    QIDIAN_MSG_GAP: "warn",
    QIDIAN_PROFILE_FALLBACK: "info",     # 02 §3.7:企点定位 profile 退到低版本(G-15),subject=account:<id>
    H02_WINAGENT_API_DOWN: "crit",              # 02 §3.7:WinAgent /ping 连续 3 次失败,subject=host
    H03_DOCKERD_DOWN: "crit",                   # 02 §3.7:dockerd 非 active,subject=wsl
    H13_CLOCK_DRIFT: "warn",
    MEM_PRESSURE: "warn",
    WA_VERSION_MISMATCH: "warn",
    H04_CONTAINER_EXITED: "crit",
    H05_BOOT_INCOMPLETE: "crit",
    H06_ADB_OFFLINE: "warn",
    CONTAINER_OOM_KILLED: "warn",
    ACCOUNT_OFFLINE: "warn",
    AUTO_RESTART_EXHAUSTED: "crit",             # 02 §3.7:每小时自动重启 ≥ container_restart_max 次停止自愈(§5)
    H07_SCRCPY_STALLED: "warn",                 # 02 §3.7:前台画面流 server 退出或视频/控制 socket EOF(04 H07,R6-69)
    H08_NAPCAT_HEARTBEAT_LOST: "warn",          # 02 §3.7:OneBot 30 s 无心跳(2 min 离线升 crit + login_required)
    H12_DISK_LOW: "warn",                       # 02 §3.7:三级水位(high 起由调用方传 crit)
    H21_WECHAT_HOSTS_BLOCK_FAILED: "warn",      # 02 §3.7:WinAgent 侧产生,Agent 只转发
    DB_WRITE_FAILED: "crit",
    WEBHOOK_DEAD: "warn",
    EVENTS_QUEUE_OVERFLOW: "warn",
    WECHAT_SWITCH_FAILED: "warn",               # 02 §3.7:微信切换中途失败,槽位已清空(05 §2.4.5)
    WINAGENT_USER_OFFLINE: "info",
    POOL_CALIBRATION_DRIFT: "info",             # 02 §3.7:事件族 resource(不是 alert)
    NET_PUBLIC_ENDPOINT_CHANGED: "info",        # 02 §3.7:事件族 net
    **MAIL_ALERT_CODES,                         # 06 §2.7 十四码(02 §3.7 登记同值),事件族 mail
}

#: 02 §3.7「事件类型」列:``alert`` 之外的三族。未登记的码按 ``alert``(与 ``REGISTERED`` 的 ``warn`` 回落同理)。
EVENT_FAMILY: dict[str, str] = {
    POOL_CALIBRATION_DRIFT: "resource",
    MEM_PRESSURE: "resource",
    NET_PUBLIC_ENDPOINT_CHANGED: "net",
    **{code: "mail" for code in MAIL_ALERT_CODES},
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
        # `*_at` 一律 ISO 8601(00 §6;04 §2.4.1 示例即字符串)。2026-10-10 真机:此前发毫秒整数,
        # 控制台按字符串排序告警 `last_seen_at.localeCompare` 直接抛 TypeError,告警铃整体不渲染。
        return {"code": a.code, "severity": a.severity, "state": state, "subject": a.subject,
                "title": None, "message": None, "hint_actions": a.hint_actions,
                "first_seen_at": _iso(a.first_seen_ms), "last_seen_at": _iso(a.last_seen_ms),
                "count": a.count, "evidence": a.evidence}

    @staticmethod
    def event_of(code: str, event: Optional[str] = None) -> str:
        """告警的事件族:显式 ``event=`` 优先,否则按 02 §3.7「事件类型」列,未登记回落 ``alert``。"""
        return event or EVENT_FAMILY.get(code, "alert")

    def firing(self, code: str, *, subject: str, severity: Optional[str] = None, evidence: Optional[dict[str, Any]] = None,
               hint_actions: Optional[list[str]] = None, account_id: Optional[str] = None,
               event: Optional[str] = None) -> bool:
        """返回 True = 本次是新 firing(发了事件);False = 已在 firing,只累加。``event`` 缺省按 02 §3.7 事件族。"""
        sev = severity or REGISTERED.get(code, "warn")
        ev = self.event_of(code, event)
        now = self._clock()
        key = (code, subject)
        a = self.active.get(key)
        if a is not None:
            a.count += 1
            a.last_seen_ms = now
            a.evidence = evidence or a.evidence
            if a.severity != sev:                       # 级别翻转(如 H06 三振 warn→crit、MEM_PRESSURE warn→crit)= 状态变化,再发一次 firing
                a.severity = sev
                self._events.emit(ev, payload=self._payload(a, "firing"), account_id=account_id, now_ms=now)
                return True
            return False
        a = ActiveAlert(code, subject, sev, now, now, 1, dict(evidence or {}), list(hint_actions or []))
        self.active[key] = a
        self._events.emit(ev, payload=self._payload(a, "firing"), account_id=account_id, now_ms=now)
        return True

    def resolve(self, code: str, *, subject: str, account_id: Optional[str] = None, event: Optional[str] = None) -> bool:
        key = (code, subject)
        a = self.active.pop(key, None)
        if a is None:
            return False
        a.last_seen_ms = self._clock()
        self._events.emit(self.event_of(code, event), payload=self._payload(a, "resolved"), account_id=account_id, now_ms=a.last_seen_ms)
        return True

    def is_firing(self, code: str, subject: str) -> bool:
        return (code, subject) in self.active
