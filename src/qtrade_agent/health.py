"""health 模块(02 §2.2.12;H13/提权宽限窗的 owner = 04)—— Agent 进程内内存态:

- ``mark_rooting(account_id, grace_s)`` / ``is_rooting(account_id)``:06 §2.9.5 ``ensure_root`` 开始时打标记,H06 在 ``now < rooting_until_ms`` 期间不计连接态失败(R6-32;**不落库**)。
- ``h13_firing``:WSL 时钟漂移(04 H13:每 60 s 与 ``GET /wa/v1/time`` 比,|Δ| > 2 s);由 ``timesync.TimeSync`` 维护。
- ``winagent_online`` / ``winagent_version`` / ``user_agent_online``:由 ``app.winagent_probe``(H02,每 ``[winagent] probe_interval_s``)维护;
  ``dockerd_ok`` 由 ``dockerd_probe``(H03)维护。
- ``summary()``:``/system/health`` 免鉴权来源的布尔级摘要(02 #72)。
"""
from __future__ import annotations

import time
from typing import Callable, Optional


class Health:
    def __init__(self, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._clock = clock
        self._rooting_until: dict[str, int] = {}
        self._h13_firing = False
        self.started_ms = clock()
        self.dockerd_ok: Optional[bool] = None
        self.winagent_online: Optional[bool] = None
        self.winagent_version: Optional[str] = None
        self.user_agent_online: Optional[bool] = None
        self.winagent_fail_streak = 0                 # H02:2 s × 连续 3 次超时才判离线(R-09 去抖)
        self.winagent_checked_ms: Optional[int] = None

    # ---- 提权宽限窗(R6-32)
    def mark_rooting(self, account_id: str, grace_s: int) -> int:
        until = self._clock() + grace_s * 1000
        self._rooting_until[account_id] = until
        return until

    def is_rooting(self, account_id: str) -> bool:
        until = self._rooting_until.get(account_id)
        return until is not None and self._clock() < until

    def rooting_until(self, account_id: str) -> Optional[int]:
        return self._rooting_until.get(account_id)

    # ---- H13 时钟漂移
    def set_h13(self, firing: bool) -> None:
        self._h13_firing = firing

    def h13_firing(self) -> bool:
        return self._h13_firing

    # ---- H02 WinAgent / H03 dockerd
    def set_winagent(self, online: bool, *, version: Optional[str] = None, user_agent: Optional[bool] = None) -> None:
        self.winagent_checked_ms = self._clock()
        if online:
            self.winagent_fail_streak = 0
            self.winagent_online = True
            if version:
                self.winagent_version = version
            if user_agent is not None:
                self.user_agent_online = bool(user_agent)
        else:
            self.winagent_fail_streak += 1
            if self.winagent_fail_streak >= 3 or self.winagent_online is None:
                self.winagent_online = False
                self.user_agent_online = False

    def set_dockerd(self, ok: bool) -> None:
        self.dockerd_ok = ok

    # ---- 摘要
    def uptime_s(self) -> int:
        return max(0, (self._clock() - self.started_ms) // 1000)

    def summary(self) -> dict:
        """免鉴权来源只回布尔级摘要(02 #72:WinAgent 不持 Agent 令牌,H01 用)。"""
        return {"ok": True, "agent": True, "dockerd": bool(self.dockerd_ok), "winagent": bool(self.winagent_online),
                "user_agent": bool(self.user_agent_online)}
