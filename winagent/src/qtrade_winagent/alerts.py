"""WinAgent 侧告警:去重、抑制、风暴合并、**本地缓冲**(02 §2.4 monitor / §3.7 / 04 §2.4)。

- 码与默认级别**单一来源 = 02 §3.7**;这里只登记「产生方 = WinAgent / 两侧」的那些。
- 去重键 ``(code, subject)``;重复 firing 只累加 ``count``,级别翻转(warn→crit)算状态变化、再发一次。
- 未恢复重复提醒:``crit`` 每 ``crit_repeat_min``(30 min)、``warn`` 每 ``warn_repeat_h``(6 h)(04 §2.4.2)。
- **风暴**:每分钟 > ``storm_per_min``(20)条 ⇒ 合并成一条 ``ALERT_STORM``(``evidence.codes[]``)。
- **Agent 不在时**本地缓冲 ≤ ``[alert] buffer_max``(1000)条,恢复后经 ``GET /wa/v1/alerts?since=`` 拉走(02 §2.4;04 §2.4.3);
  满了丢**最旧**的(和 Agent 侧 ``EVENTS_QUEUE_OVERFLOW`` 同口径)。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import AlertConfig

# ---- 02 §3.7 登记:WinAgent(或两侧)产生的码 → 默认 severity
H01_AGENT_API_DOWN = "H01_AGENT_API_DOWN"
H09_CHATLOG_DOWN = "H09_CHATLOG_DOWN"
H10_WECHAT_PROCESS_MISSING = "H10_WECHAT_PROCESS_MISSING"
H11_SESSION_LOCKED = "H11_SESSION_LOCKED"
H12_DISK_LOW = "H12_DISK_LOW"
H14_REBOOT_PENDING = "H14_REBOOT_PENDING"
H15_LOCALHOST_FORWARD_LOST = "H15_LOCALHOST_FORWARD_LOST"
H16_WINAGENT_BIND_MISMATCH = "H16_WINAGENT_BIND_MISMATCH"
H20_WECHAT_AUTO_UPDATED = "H20_WECHAT_AUTO_UPDATED"
H21_WECHAT_HOSTS_BLOCK_FAILED = "H21_WECHAT_HOSTS_BLOCK_FAILED"
H23_VHDX_GROWTH = "H23_VHDX_GROWTH"
WECHAT_DISK_LOW = "WECHAT_DISK_LOW"
NET_OFFLINE = "NET_OFFLINE"
NET_STATE_CHANGED = "NET_STATE_CHANGED"
WSLCONFIG_PENDING_RESTART = "WSLCONFIG_PENDING_RESTART"
WSL_SUBNET_CHANGED = "WSL_SUBNET_CHANGED"
ALERT_STORM = "ALERT_STORM"
VAULT_ENTROPY_MISSING = "VAULT_ENTROPY_MISSING"
WA_USER_VERSION_MISMATCH = "WA_USER_VERSION_MISMATCH"
DB_WRITE_FAILED = "DB_WRITE_FAILED"
DB_INTEGRITY = "DB_INTEGRITY"
EVENTLOOP_BLOCKED = "EVENTLOOP_BLOCKED"

REGISTERED: dict[str, str] = {
    H01_AGENT_API_DOWN: "crit", H09_CHATLOG_DOWN: "crit", H10_WECHAT_PROCESS_MISSING: "crit",
    H11_SESSION_LOCKED: "warn", H12_DISK_LOW: "warn", H14_REBOOT_PENDING: "info",
    H15_LOCALHOST_FORWARD_LOST: "warn", H16_WINAGENT_BIND_MISMATCH: "crit", H20_WECHAT_AUTO_UPDATED: "warn",
    H21_WECHAT_HOSTS_BLOCK_FAILED: "warn", H23_VHDX_GROWTH: "warn", WECHAT_DISK_LOW: "warn",
    NET_OFFLINE: "crit", NET_STATE_CHANGED: "info", WSLCONFIG_PENDING_RESTART: "info",
    WSL_SUBNET_CHANGED: "info", ALERT_STORM: "warn", VAULT_ENTROPY_MISSING: "crit",
    WA_USER_VERSION_MISMATCH: "warn", DB_WRITE_FAILED: "crit", DB_INTEGRITY: "crit", EVENTLOOP_BLOCKED: "warn",
}

# 02 §3.7:事件类型(payload 的 kind);多数是 alert,net_state 翻转是 net
EVENT_KIND: dict[str, str] = {NET_STATE_CHANGED: "net"}

# 04 §2.4.2 依赖抑制:net_state=OFFLINE 时各 probe 失败统一被 NET_OFFLINE 压住,只推一条
SUPPRESSED_BY_NET_OFFLINE = frozenset({H01_AGENT_API_DOWN, H15_LOCALHOST_FORWARD_LOST})

MINUTE_MS = 60_000
HOUR_MS = 3_600_000


@dataclass
class ActiveAlert:
    code: str
    subject: str
    severity: str
    first_seen_ms: int
    last_seen_ms: int
    last_emitted_ms: int
    count: int = 1
    evidence: dict[str, Any] = field(default_factory=dict)
    hint_actions: list[str] = field(default_factory=list)


@dataclass
class BufferedAlert:
    seq: int
    ts_ms: int
    kind: str
    payload: dict[str, Any]


class AlertBuffer:
    """本地缓冲 + 去重 + 重复提醒 + 风暴合并。``pull(since_seq)`` = #6 ``GET /wa/v1/alerts?since=``。"""

    def __init__(self, cfg: AlertConfig, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._cfg = cfg
        self._clock = clock
        self.active: dict[tuple[str, str], ActiveAlert] = {}
        self._buf: deque[BufferedAlert] = deque(maxlen=cfg.buffer_max)     # 满了自动丢最旧
        self._seq = 0
        self._minute_bucket: tuple[int, list[str]] = (0, [])
        self.dropped = 0                                                   # 因缓冲满被丢弃的条数(供 #2 health 展示)
        self.net_offline = False                                           # 04 §2.4.2 依赖抑制开关

    # ---------------------------------------------------------------- 内部
    def _payload(self, a: ActiveAlert, state: str) -> dict[str, Any]:
        """00 §7.5 告警类事件统一 payload(与 Agent 侧 ``alerts.Alerts._payload`` 同形状)。"""
        return {"code": a.code, "severity": a.severity, "state": state, "subject": a.subject,
                "title": None, "message": None, "hint_actions": a.hint_actions,
                "first_seen_at": a.first_seen_ms, "last_seen_at": a.last_seen_ms, "count": a.count,
                "evidence": a.evidence}

    def _emit(self, code: str, payload: dict[str, Any], now: int) -> None:
        if len(self._buf) == self._buf.maxlen:
            self.dropped += 1
        self._seq += 1
        self._buf.append(BufferedAlert(self._seq, now, EVENT_KIND.get(code, "alert"), payload))

    def _storm_guard(self, now: int, code: str) -> bool:
        """返回 True = 本条被风暴合并吃掉(不单独入缓冲)。窗口 = 自然分钟。"""
        bucket = now // MINUTE_MS
        cur_bucket, codes = self._minute_bucket
        if bucket != cur_bucket:
            self._minute_bucket = (bucket, [code])
            return False
        codes.append(code)
        if len(codes) <= self._cfg.storm_per_min:
            return False
        if len(codes) == self._cfg.storm_per_min + 1:                      # 第一次越线时推一条 ALERT_STORM
            a = ActiveAlert(ALERT_STORM, "host", REGISTERED[ALERT_STORM], now, now, now, 1,
                            {"codes": sorted(set(codes))})
            self.active[(ALERT_STORM, "host")] = a
            self._emit(ALERT_STORM, self._payload(a, "firing"), now)
        else:
            a = self.active.get((ALERT_STORM, "host"))
            if a is not None:
                a.count += 1
                a.last_seen_ms = now
                a.evidence = {"codes": sorted(set(codes))}
        return True

    # ---------------------------------------------------------------- 对外
    def firing(self, code: str, *, subject: str, severity: Optional[str] = None,
               evidence: Optional[dict[str, Any]] = None, hint_actions: Optional[list[str]] = None) -> bool:
        """返回 True = 本次推了事件(新 firing / 级别翻转 / 到了重复提醒周期)。"""
        if self.net_offline and code in SUPPRESSED_BY_NET_OFFLINE:         # 04 §2.4.2 依赖抑制
            return False
        sev = severity or REGISTERED.get(code, "warn")
        now = self._clock()
        key = (code, subject)
        a = self.active.get(key)
        if a is None:
            a = ActiveAlert(code, subject, sev, now, now, now, 1, dict(evidence or {}), list(hint_actions or []))
            self.active[key] = a
            if self._storm_guard(now, code):
                return False
            self._emit(code, self._payload(a, "firing"), now)
            return True
        a.count += 1
        a.last_seen_ms = now
        if evidence:
            a.evidence = dict(evidence)
        if hint_actions is not None:
            a.hint_actions = list(hint_actions)
        if a.severity != sev:                                              # 级别翻转 = 状态变化,再发一次
            a.severity = sev
            a.last_emitted_ms = now
            self._emit(code, self._payload(a, "firing"), now)
            return True
        repeat = self._cfg.crit_repeat_min * MINUTE_MS if sev == "crit" else self._cfg.warn_repeat_h * HOUR_MS
        if sev != "info" and now - a.last_emitted_ms >= repeat:            # 未恢复重复提醒(04 §2.4.2)
            a.last_emitted_ms = now
            self._emit(code, self._payload(a, "firing"), now)
            return True
        return False

    def resolve(self, code: str, *, subject: str) -> bool:
        a = self.active.pop((code, subject), None)
        if a is None:
            return False
        now = self._clock()
        a.last_seen_ms = now
        self._emit(code, self._payload(a, "resolved"), now)
        return True

    def is_firing(self, code: str, subject: str) -> bool:
        return (code, subject) in self.active

    def pull(self, *, since: int = 0, limit: int = 500) -> dict[str, Any]:
        """#6:``since`` = 上次拿到的 ``seq``(0 = 从头);返回 ``{alerts, next_since, dropped, buffered}``。

        **拉走不清空**——缓冲是环形的、按 ``seq`` 推进;Agent 崩了重来能按自己的水位重取还留在环里的部分。
        """
        items = [b for b in self._buf if b.seq > since][:limit]
        return {"alerts": [{"seq": b.seq, "ts_ms": b.ts_ms, "kind": b.kind, "payload": b.payload} for b in items],
                "next_since": items[-1].seq if items else since,
                "dropped": self.dropped, "buffered": len(self._buf)}
