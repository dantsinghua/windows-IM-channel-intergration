"""health 模块(02 §2.2.12;H13/提权宽限窗的 owner = 04)—— 本期只落 Agent 进程内内存态:

- ``mark_rooting(account_id, grace_s)`` / ``is_rooting(account_id)``:06 §2.9.5 ``ensure_root`` 开始时打标记,H06 在 ``now < rooting_until_ms`` 期间不计连接态失败(R6-32;**不落库**)。
- ``h13_firing``:WSL 时钟漂移(04 H13:每 60 s 与 ``GET /wa/v1/time`` 比,|Δ| > 2 s);本期没有 WinAgent 可探,由 ``set_h13()`` 置位(探测器接入后由它维护)。
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
        self.dockerd_ok: Optional[bool] = None       # runtime 模块接入后维护
        self.winagent_online: Optional[bool] = None  # WinAgent 探测接入后维护
        self.user_agent_online: Optional[bool] = None

    # ---- 提权宽限窗(R6-32)
    def mark_rooting(self, account_id: str, grace_s: int) -> int:
        until = self._clock() + grace_s * 1000
        self._rooting_until[account_id] = until
        return until

    def is_rooting(self, account_id: str) -> bool:
        until = self._rooting_until.get(account_id)
        return until is not None and self._clock() < until

    # ---- H13 时钟漂移
    def set_h13(self, firing: bool) -> None:
        self._h13_firing = firing

    def h13_firing(self) -> bool:
        return self._h13_firing

    # ---- 摘要
    def uptime_s(self) -> int:
        return max(0, (self._clock() - self.started_ms) // 1000)

    def summary(self) -> dict:
        """免鉴权来源只回布尔级摘要(02 #72:WinAgent 不持 Agent 令牌,H01 用)。"""
        return {"ok": True, "agent": True, "dockerd": bool(self.dockerd_ok), "winagent": bool(self.winagent_online),
                "user_agent": bool(self.user_agent_online)}
