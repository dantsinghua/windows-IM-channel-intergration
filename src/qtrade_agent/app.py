"""Agent 进程装配(02 §2.1 启动顺序 / §2.6 崩溃恢复与启动恢复)。

启动:打开 agent.db → 迁移 → 崩溃恢复(queued/running 指令改 failed、SENDING 幂等行改 ABANDONED)→ 装配 events/alerts/health/pool/runtime/vault/winagent/adapters/bus/accounts →
注册 scheduler 任务(企点全量轮、群缺口、outbox 保留、WinAgent 探活 H02、dockerd H03、H13 校时)→ 启动恢复(02 §2.6:按 desired_state 串行 start)→ api 开始监听。
真机后端(``DockerCliBackend``/``AdbCliBackend``/``WinAgentVault``/urllib)只在没注入假实现时才用;开发容器里一律注入 ``runtime.FakeContainers``/``FakeAdb``/``FakeVault``/``FakeWinAgent``。
``mail``、``workflow``、HMAC 公网入站、webhook 投递器、GATE 安全闸本期仍未接。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from .accounts import AccountService, LoginFn
from .adapters.base import Account
from .adapters.qidian.adapter import QidianAdapter, SendFn
from .adapters.qidian.maindb import AdbMainDb, LocalSqliteMainDb, MainDb
from .adapters.qidian.poll import QidianPoller
from .alerts import Alerts
from .bus.bus import Bus
from .config import H13_INTERVAL_S, AgentConfig
from .events import Events
from .health import Health
from .pool import Pool
from .runtime import AdbBackend, AdbCliBackend, ContainerBackend, DockerCliBackend, Runtime
from .runtime.runtime import Fs
from .scheduler import Scheduler
from .store import Store
from .timesync import Aligner, TimeSync
from .vault_client import Vault, WinAgentVault
from .winagent_client import Transport, WinAgentClient

log = logging.getLogger("qtrade.app")

H02_INTERVAL_S = 30         # 04 H02:每 30 s ping(离线后按 [winagent] probe_interval_s 重探)
H03_INTERVAL_S = 30         # 04 H03:dockerd 每 30 s


async def _sender_not_wired(acct: Account, native_id: str, text: str) -> bool:
    """默认执行层:真机 RPA 尚未接入 ⇒ 点不了发送键。返回 False → SEND_FAILED(retryable)。"""
    log.error("企点 UI 执行层未接入(account=%s),send 无法执行", acct.id)
    return False


class AgentApp:
    def __init__(self, cfg: AgentConfig, *, db_path: Optional[str] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 sender: Optional[SendFn] = None, maindb_factory: Optional[Callable[[str, Account], MainDb]] = None,
                 wsl_gateway: Optional[str] = None, containers: Optional[ContainerBackend] = None, adb: Optional[AdbBackend] = None,
                 vault: Optional[Vault] = None, winagent_transport: Optional[Transport] = None, winagent_base_url: Optional[str] = None,
                 winagent_token: Optional[str] = None, fs: Optional[Fs] = None, login_fn: Optional[LoginFn] = None,
                 aligner: Optional[Aligner] = None, wsl_total_mb: Optional[int] = None, boot_poll_s: Optional[float] = None):
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
        self._containers = containers
        self._adb = adb
        self._vault = vault
        self._wa_transport = winagent_transport
        self._wa_base_url = winagent_base_url
        self._wa_token = winagent_token
        self._fs = fs
        self._login_fn = login_fn
        self._aligner = aligner
        self._wsl_total_mb = wsl_total_mb
        self._boot_poll_s = boot_poll_s
        self.adapters: dict = {}
        self.bus: Bus
        self.poller: QidianPoller
        self.pool: Pool
        self.runtime: Runtime
        self.vault: Vault
        self.winagent: WinAgentClient
        self.timesync: TimeSync
        self.accounts: AccountService
        self._started = False

    # ------------------------------------------------------------------ 装配
    def open(self) -> "AgentApp":
        self.store.open()
        n = self.store.abandon_inflight(self.clock())            # §2.6 崩溃恢复
        if n:
            log.warning("崩溃恢复:%d 条悬挂指令改 failed", n)
        self.events = Events(self.store, queue_max=self.cfg.events.ws_queue_max)
        self.alerts = Alerts(self.events, clock=self.clock)
        self.pool = Pool(self.store, self.cfg, clock=self.clock, wsl_total_mb=self._wsl_total_mb)
        self.winagent = WinAgentClient(self.cfg.winagent, transport=self._wa_transport, base_url=self._wa_base_url, token=self._wa_token, clock=self.clock)
        self.vault = self._vault if self._vault is not None else WinAgentVault(self.winagent)
        rt_kw = {} if self._boot_poll_s is None else {"boot_poll_s": self._boot_poll_s}
        self.runtime = Runtime(containers=self._containers or DockerCliBackend(), adb=self._adb or AdbCliBackend(), cfg=self.cfg, health=self.health,
                               alerts=self.alerts, store=self.store, clock=self.clock, fs=self._fs, **rt_kw)
        self.poller = QidianPoller(store=self.store, events=self.events, alerts=self.alerts, cfg=self.cfg, h13_firing=self.health.h13_firing,
                                   clock=self.clock, maindb_factory=self._maindb_for_uid)
        self.adapters = {"qidian": QidianAdapter(self.poller, sender=self._sender, store=self.store)}
        self.bus = Bus(store=self.store, events=self.events, adapters=self.adapters, cfg=self.cfg, clock=self.clock)
        self.accounts = AccountService(store=self.store, events=self.events, pool=self.pool, runtime=self.runtime, vault=self.vault, cfg=self.cfg,
                                       adapters=self.adapters, health=self.health, clock=self.clock, login_fn=self._login_fn)
        self.timesync = TimeSync(self.winagent, health=self.health, alerts=self.alerts, clock=self.clock, aligner=self._aligner, on_resume=self.on_host_resume)
        self.scheduler.register("qidian_poll_all", self.cfg.qidian.poll_interval_s, self.qidian_poll_all)
        self.scheduler.register("qidian_gaps_all", self.cfg.qidian.gap_check_interval_s, self.qidian_gaps_all)
        self.scheduler.register("outbox_ws_retention", 3600, self.outbox_retention)
        self.scheduler.register("winagent_probe", min(H02_INTERVAL_S, self.cfg.winagent.probe_interval_s), self.winagent_probe, run_immediately=True)
        self.scheduler.register("dockerd_probe", H03_INTERVAL_S, self.dockerd_probe, run_immediately=True)
        self.scheduler.register("h13_clock_sync", H13_INTERVAL_S, self.h13_probe, run_immediately=True)
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
                               extra={"adb_serial": row.get("adb_serial") or f"127.0.0.1:{16000 + seq}"}))
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

    async def winagent_probe(self) -> None:
        """H02:``GET /wa/v1/ping``(无鉴权 2 s)→ 在线再 ``GET /wa/v1/health`` 取 user_agent / host 快照 → pool 的 windows 池输入(02 §2.5 降级 ①)。"""
        pong = await self.winagent.ping()
        if pong is None:
            self.health.set_winagent(False)
            if self.health.winagent_online is False:
                self.pool.set_windows(known=False)
            return
        h = await self.winagent.health()
        if h is None:
            self.health.set_winagent(True, version=self.winagent.last_version)
            return
        self.health.set_winagent(True, version=h.get("version") or self.winagent.last_version, user_agent=bool(h.get("user_agent")))
        host = h.get("host") or {}
        modules = h.get("modules") or {}
        self.pool.set_windows(total_mb=host.get("total_mb"), available_mb=host.get("available_mb"),
                              wechat_enabled=(modules.get("wechat") == "enabled"), known=True)
        if host.get("wsl_vm_mb"):
            self.pool.set_wsl_total(int(host["wsl_vm_mb"]), source="winagent")

    async def dockerd_probe(self) -> None:
        self.health.set_dockerd(await self.runtime.dockerd_ok())

    async def h13_probe(self) -> None:
        if self.health.winagent_online:
            await self.timesync.probe()

    async def on_host_resume(self, resume_ms: int) -> None:
        """04 §2.10 唤醒后:全部企点账号 adb 探活一轮,reconnect 成功后复跑 ``ensure_root``(R6-28;宽限窗 R6-32)。"""
        log.info("主机唤醒(last_resume_ms=%d):企点账号 adb 重连 + ensure_root", resume_ms)
        for row in self.store.list_accounts(channel="qidian", state="running"):
            try:
                await self.runtime._adb.disconnect(f"127.0.0.1:{16000 + int(row['seq'])}")
                await self.runtime._adb.connect(f"127.0.0.1:{16000 + int(row['seq'])}")
                await self.runtime.ensure_root(row)
            except Exception as e:
                log.warning("唤醒后 %s 重连/提权失败: %s", row["id"], e)

    # ------------------------------------------------------------------ 生命周期
    async def start(self, *, recover: bool = True) -> None:
        if not self._started:
            await self.scheduler.start()
            self._started = True
            if recover:
                asyncio.create_task(self.accounts.recover(), name="recover")

    async def stop(self) -> None:
        if self._started:
            await self.scheduler.stop()
            await self.bus.close()
            self._started = False
        self.store.close()

    def create_api(self):
        from .api.app import create_api
        return create_api(self)
