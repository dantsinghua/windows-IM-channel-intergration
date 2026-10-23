"""H13 时间同步(04 §2.9 / §2.3 H13):Agent 每 60 s ``GET /wa/v1/time`` 得 ``t_win``,与本地比,``|Δ| > 2 s`` 判漂移。

- 真值 = Windows 时钟;对策 = ``hwclock -s``(WSL 的 RTC 由 Hyper-V 同步于主机),``hwclock`` 不可用退化为 ``date -s @<t_win>``(减去往返一半)。
- 对齐后再测,仍 > 2 s 才推 warn ``H13_CLOCK_DRIFT``(A5-09:对齐成功不告警,失败才 warn);回到阈值内 ``resolved``。
- ``last_resume_ms`` 变化 = 主机从睡眠唤醒(R3-5,C-03 单向化后靠轮询感知)⇒ 回调 ``on_resume``(04 §2.10:校时 + adb 探活 + 企点复跑 ``ensure_root``)。
- WinAgent 不可达:本轮不改 H13 状态(unknown),由 winagent_probe 维护在线态。
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from typing import Any, Awaitable, Callable, Optional, Protocol

from .alerts import H13_CLOCK_DRIFT
from .config import H13_DRIFT_THRESHOLD_MS
from .winagent_client import WinAgentUnavailable

log = logging.getLogger("qtrade.timesync")


class Aligner(Protocol):
    async def align(self, t_win_ms: int) -> bool: ...


class SystemAligner:
    """真机:``hwclock -s``;不可用(无 /dev/rtc0)→ ``date -s @<t_win>``。"""

    async def align(self, t_win_ms: int) -> bool:
        def go() -> bool:
            try:
                if subprocess.run(["hwclock", "-s"], capture_output=True, timeout=10).returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                return subprocess.run(["date", "-s", f"@{t_win_ms / 1000:.3f}"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        return await asyncio.to_thread(go)


class TimeSync:
    SUBJECT = "wsl"

    def __init__(self, client, *, health, alerts, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 aligner: Optional[Aligner] = None, on_resume: Optional[Callable[[int], Awaitable[None]]] = None,
                 threshold_ms: int = H13_DRIFT_THRESHOLD_MS):
        self._client = client
        self._health = health
        self._alerts = alerts
        self._clock = clock
        self._aligner: Aligner = aligner or SystemAligner()
        self._on_resume = on_resume
        self.threshold_ms = threshold_ms
        self.last_delta_ms: Optional[int] = None
        self.last_resume_ms: Optional[int] = None
        self.last_probe_ms: Optional[int] = None
        self.probes = 0
        self.aligns = 0
        self.last_result: dict[str, Any] = {}

    async def _measure(self) -> int:
        t = await self._client.time()
        local = self._clock()
        delta = t.now_ms - (local - t.rtt_ms // 2)          # 往返一半归到对端
        self._last_time = t
        return delta

    async def probe(self) -> dict[str, Any]:
        self.probes += 1
        try:
            delta = await self._measure()
        except WinAgentUnavailable as e:
            self.last_result = {"ok": False, "reason": e.reason}
            return self.last_result
        self.last_probe_ms = self._clock()
        self.last_delta_ms = delta
        aligned = False
        if abs(delta) > self.threshold_ms:
            aligned = await self._aligner.align(self._last_time.now_ms)
            self.aligns += 1
            try:
                delta = await self._measure()                # 对齐后再测
            except WinAgentUnavailable as e:
                self.last_result = {"ok": False, "reason": e.reason, "aligned": aligned}
                return self.last_result
            self.last_delta_ms = delta
        drift = abs(delta) > self.threshold_ms
        if drift:
            self._alerts.firing(H13_CLOCK_DRIFT, subject=self.SUBJECT, evidence={"delta_ms": delta, "aligned": aligned, "threshold_ms": self.threshold_ms})
            self._health.set_h13(True)
        else:
            self._alerts.resolve(H13_CLOCK_DRIFT, subject=self.SUBJECT)
            self._health.set_h13(False)
        resumed = False
        lr = self._last_time.last_resume_ms
        if lr is not None and self.last_resume_ms is not None and lr != self.last_resume_ms:
            resumed = True
            if self._on_resume is not None:
                try:
                    await self._on_resume(lr)
                except Exception as e:                       # 唤醒后处理失败不影响校时
                    log.exception("on_resume 回调异常: %s", e)
        self.last_resume_ms = lr
        self.last_result = {"ok": True, "delta_ms": delta, "drift": drift, "aligned": aligned, "resumed": resumed}
        return self.last_result
