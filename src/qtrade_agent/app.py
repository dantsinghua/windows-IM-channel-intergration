"""Agent 进程装配(02 §2.1 启动顺序 / §2.6 崩溃恢复)。

启动:打开 agent.db → 迁移 → 崩溃恢复(queued/running 指令改 failed、SENDING 幂等行改 ABANDONED)→ 装配 events/alerts/health/adapters/bus →
注册 scheduler 任务(企点全量轮、群缺口)→ api 开始监听。runtime(docker/redroid)、pool、mail、vault_client、WinAgent 探测本期未接:
账号恢复(§2.6)只做「读库把 running 账号交给读循环」,不拉容器。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from .adapters.base import Account
from .adapters.qidian.adapter import QidianAdapter, SendFn
from .adapters.qidian.maindb import AdbMainDb, LocalSqliteMainDb, MainDb
from .adapters.qidian.poll import QidianPoller
from .alerts import Alerts
from .bus.bus import Bus
from .config import AgentConfig
from .events import Events
from .health import Health
from .scheduler import Scheduler
from .store import Store

log = logging.getLogger("qtrade.app")


async def _sender_not_wired(acct: Account, native_id: str, text: str) -> bool:
    """默认执行层:真机 RPA 尚未接入 ⇒ 点不了发送键。返回 False → SEND_FAILED(retryable)。"""
    log.error("企点 UI 执行层未接入(account=%s),send 无法执行", acct.id)
    return False


class AgentApp:
    def __init__(self, cfg: AgentConfig, *, db_path: Optional[str] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 sender: Optional[SendFn] = None, maindb_factory: Optional[Callable[[str, Account], MainDb]] = None,
                 wsl_gateway: Optional[str] = None):
        self.cfg = cfg
        self.clock = clock
        self.wsl_gateway = wsl_gateway
        self.store = Store(db_path or cfg.db.path, clock=clock, out_merge_window_s=cfg.bus.out_merge_window_s, capture_text=cfg.messages.capture_text)
        self.events: Events
        self.alerts: Alerts
        self.health = Health(clock=clock)
        self.scheduler = Scheduler(clock=clock)
        self._sender = sender or _sender_not_wired
        self._maindb_factory = maindb_factory
        self.adapters: dict = {}
        self.bus: Bus
        self.poller: QidianPoller
        self._started = False

    # ------------------------------------------------------------------ 装配
    def open(self) -> "AgentApp":
        self.store.open()
        n = self.store.abandon_inflight(self.clock())            # §2.6 崩溃恢复
        if n:
            log.warning("崩溃恢复:%d 条悬挂指令改 failed", n)
        self.events = Events(self.store, queue_max=self.cfg.events.ws_queue_max)
        self.alerts = Alerts(self.events, clock=self.clock)
        self.poller = QidianPoller(store=self.store, events=self.events, alerts=self.alerts, cfg=self.cfg, h13_firing=self.health.h13_firing,
                                   clock=self.clock, maindb_factory=self._maindb_for_uid)
        self.adapters = {"qidian": QidianAdapter(self.poller, sender=self._sender, store=self.store)}
        self.bus = Bus(store=self.store, events=self.events, adapters=self.adapters, cfg=self.cfg, clock=self.clock)
        self.scheduler.register("qidian_poll_all", self.cfg.qidian.poll_interval_s, self.qidian_poll_all)
        self.scheduler.register("qidian_gaps_all", self.cfg.qidian.gap_check_interval_s, self.qidian_gaps_all)
        self.scheduler.register("outbox_ws_retention", 3600, self.outbox_retention)
        return self

    def _maindb_for_uid(self, self_uid: str) -> MainDb:
        acct = self._current_qidian_account
        if self._maindb_factory is not None:
            return self._maindb_factory(self_uid, acct)
        serial = acct.extra.get("adb_serial") if acct else None
        if serial:
            return AdbMainDb(serial, self_uid)
        return LocalSqliteMainDb(f"/data/data/com.tencent.qidian/databases/{self_uid}.db")

    _current_qidian_account: Optional[Account] = None

    def running_qidian_accounts(self) -> list[Account]:
        out = []
        for row in self.store.list_accounts(channel="qidian"):
            if row["state"] != "running" or not row.get("enabled"):
                continue
            seq = int(row["seq"])
            out.append(Account(id=row["id"], channel="qidian", state=row["state"], self_uid=row.get("self_uid"), self_nick=row.get("self_nick"),
                               state_code=row.get("state_code"), app_version=row.get("runtime_app_version"),
                               extra={"adb_serial": f"127.0.0.1:{16000 + seq}"}))
        return out

    # ------------------------------------------------------------------ 周期任务
    async def qidian_poll_all(self) -> None:
        for acct in self.running_qidian_accounts():
            self._current_qidian_account = acct
            try:
                await self.adapters["qidian"].poll(acct)
            finally:
                self._current_qidian_account = None

    async def qidian_gaps_all(self) -> None:
        for acct in self.running_qidian_accounts():
            self._current_qidian_account = acct
            try:
                await self.adapters["qidian"].check_group_gaps(acct)
            finally:
                self._current_qidian_account = None

    async def outbox_retention(self) -> None:
        cutoff = self.clock() - self.cfg.events.ws_retention_hours * 3600 * 1000
        n = await asyncio.to_thread(self.store.purge_outbox_ws, cutoff)
        if n:
            log.info("events_outbox ws 行保留期清理 %d 行", n)

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if not self._started:
            await self.scheduler.start()
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self.scheduler.stop()
            await self.bus.close()
            self._started = False
        self.store.close()

    def create_api(self):
        from .api.app import create_api
        return create_api(self)
