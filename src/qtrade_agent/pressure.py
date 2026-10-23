"""运行期内存水位硬保护(E-19;02 §2.2.5「运行期内存水位硬保护」/ §5 / 05 §2.5.2;04 H24 F-33/F-34;告警 ``MEM_PRESSURE`` 02 §3.7)。

- 输入 = 整机可用内存 ``win_available_mb``(WinAgent ``GET /wa/v1/health.host.available_mb``,由 ``app.winagent_probe`` 喂入)。
- 水位 ``[pool] mem_warn_mb / mem_critical_mb``(= 04 ``[monitor]`` 唯一出处,默认 2048/1024):
  ``warn`` ⇒ ``alert MEM_PRESSURE``(warn);``critical`` ⇒ 升 crit,并**阻断** ``POST /accounts``(#2)与自动恢复 ``recover()``:``409 RESOURCE_EXHAUSTED`` ``reason='mem_pressure'``,
  ``error.alternatives`` = **LRU 建议停用名单** ``[{account_id, last_active_at, rss_mb}]``(最久没收发的排前面;``last_active_at = max(messages.received_ms, out.confirmed_ms)``),
  同一份进 ``evidence.lru_suggest[]``。**只建议不自动停**:只有 ``accounts.settings_json.auto_stop_on_pressure=true`` 的账号才按 LRU 逐个 ``stop`` 直到 ``≥ mem_warn_mb``,每次记审计。
- 余量回升到 ``mem_warn_mb`` 以上 ⇒ ``resolved``、恢复新增/自恢复。与磁盘水位独立计算、各自触发。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from .alerts import MEM_PRESSURE
from .config import AgentConfig
from .events import iso8601

log = logging.getLogger("qtrade.pressure")

SUBJECT = "host"
LRU_STATES_EXCLUDED = ("stopped", "disabled", "error", "created", "stopping", "provisioning")


class MemoryWatermark:
    def __init__(self, *, store, accounts, alerts, cfg: AgentConfig, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._store = store
        self._accounts = accounts
        self._alerts = alerts
        self.cfg = cfg
        self._clock = clock
        self.level = "unknown"                   # unknown | ok | warn | critical
        self.avail_mb: Optional[int] = None
        self.auto_stopped: list[str] = []

    # ---- 判定
    def evaluate(self, avail_mb: Optional[int]) -> str:
        if avail_mb is None:
            self.level = "unknown"
            return self.level
        self.avail_mb = int(avail_mb)
        p = self.cfg.pool
        if self.avail_mb < p.mem_critical_mb:
            level = "critical"
        elif self.avail_mb < p.mem_warn_mb:
            level = "warn"
        else:
            level = "ok"
        self.level = level
        if level == "ok":
            self._alerts.resolve(MEM_PRESSURE, subject=SUBJECT)
        else:
            self._alerts.firing(MEM_PRESSURE, subject=SUBJECT, severity="crit" if level == "critical" else "warn",
                                evidence={"avail_mb": self.avail_mb, "level": level, "mem_warn_mb": p.mem_warn_mb, "mem_critical_mb": p.mem_critical_mb,
                                          "lru_suggest": self.lru_suggest()},
                                hint_actions=["open_res"])
        return level

    def blocked(self) -> bool:
        """critical 水位:新增(#2)与自动恢复一律 409 mem_pressure。"""
        return self.level == "critical"

    # ---- LRU 建议名单
    def lru_suggest(self) -> list[dict[str, Any]]:
        rows = [r for r in self._store.list_accounts() if r["host"] == "wsl" and r["enabled"] and r["state"] not in LRU_STATES_EXCLUDED]
        if not rows:
            return []
        last: dict[str, int] = {}
        for r in self._store.con.execute(
                "SELECT account_id, MAX(MAX(COALESCE(received_ms, 0)), COALESCE(MAX(confirmed_ms), 0)) AS last_ms FROM messages GROUP BY account_id").fetchall():
            last[r["account_id"]] = int(r["last_ms"] or 0)
        out = [{"account_id": r["id"], "last_active_at": iso8601(last[r["id"]]) if last.get(r["id"]) else None,
                "last_active_ms": last.get(r["id"]) or 0, "rss_mb": r.get("container_mem_anon_mb")} for r in rows]
        out.sort(key=lambda x: (x["last_active_ms"], x["account_id"]))      # 最久没收发的排最前(从未收发 = 0 排最前)
        for x in out:
            x.pop("last_active_ms")
        return out

    # ---- 自动停(只对显式开关的账号)
    async def enforce(self, read_avail: Optional[Callable[[], Optional[int]]] = None) -> list[str]:
        """critical 时按 LRU 逐个 stop 开了 ``auto_stop_on_pressure`` 的账号,直到 ``avail ≥ mem_warn_mb``;每次 stop 记审计。返回本轮停掉的账号。"""
        stopped: list[str] = []
        if self.level != "critical":
            return stopped
        for item in self.lru_suggest():
            aid = item["account_id"]
            row = self._store.get_account_full(aid)
            if row is None:
                continue
            try:
                settings = json.loads(row.get("settings_json") or "{}")
            except ValueError:
                settings = {}
            if not settings.get("auto_stop_on_pressure", False):
                continue                                                  # 默认 false:只建议不自动停
            if read_avail is not None:                                    # R6-57 ④:每次 stop 前先读余量,已 ≥ mem_warn_mb 即停手(少停一个比多停一个好)
                new = read_avail()
                if new is not None:
                    self.evaluate(new)
                    if self.avail_mb is not None and self.avail_mb >= self.cfg.pool.mem_warn_mb:
                        break
            try:
                await self._accounts.stop(aid, graceful=True, actor="system:pool")
                await self._accounts.wait_idle(aid)
            except Exception as e:
                log.warning("内存压力自动停 %s 失败: %s", aid, e)
                continue
            stopped.append(aid)
            self.auto_stopped.append(aid)
            self._store.insert_audit(kind="system", transport="system", actor="system:pool", action="pool.auto_stop_on_pressure", account_id=aid,
                                     result_code="OK", detail={"avail_mb": self.avail_mb, "level": self.level, "last_active_at": item["last_active_at"]},
                                     now_ms=self._clock())
        return stopped
