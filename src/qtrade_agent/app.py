"""Agent 进程装配(02 §2.1 启动顺序 / §2.6 崩溃恢复与启动恢复)。

启动:打开 agent.db → 迁移 → 崩溃恢复(queued/running 指令改 failed、SENDING 幂等行改 ABANDONED)→ 装配
events/alerts/health/pool/runtime/vault/winagent/adapters(企点 + QQ + 微信)/bus/accounts/mail/webhook/maintenance/
pool 校准/workflow/HMAC → 注册 scheduler 任务 → 启动恢复(02 §2.6:按 desired_state 串行 start)→ api 开始监听。
真机后端(``DockerCliBackend``/``AdbCliBackend``/``WinAgentVault``/``WebsocketsTransport``/``UrllibHttp``/
``ImapLibBackend`` 等)只在没注入假实现时才用;开发容器里一律注入 ``runtime.FakeContainers``/``FakeAdb``/
``FakeVault``/``FakeWinAgent``/``FakeOneBot``/``FakeWeChatWinAgent``/``FakeHttp``/``FakeImap`` 等,缺省不连任何真服务
(``[mail] enabled=false``、``webhooks`` 表空、``adapters`` 只在账号 start 时才建连)。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .accounts import AccountService, LoginFn
from .adapters.base import Account
from .adapters.qidian.adapter import QidianAdapter, SendFn
from .adapters.qidian.maindb import AdbMainDb, LocalSqliteMainDb, MainDb
from .adapters.qidian.poll import QidianPoller
from .adapters.qidian.ui import QidianUi
from .adapters.qq import H08_INTERVAL_S, OneBotTransport, QQAdapter, QQHealth
from .adapters.wechat import WeChatWinAgent, WechatAdapter, WechatLoginFlow, WechatPoller
from .alerts import H02_WINAGENT_API_DOWN, H03_DOCKERD_DOWN, Alerts
from .bus.bus import Bus
from . import device_profiles as device_profiles_mod
from .config import H13_INTERVAL_S, AgentConfig
from .events import TZ_SHANGHAI, Events
from .gate import Gate
from .health import Health
from .healthloop import H05_STEADY_INTERVAL_S, HealthLoop
from .hmac_inbound import HmacVerifier
from .ids import ulid as new_trace_id
from .maintenance import DiskFullError, DiskProbe, MaintenanceService
from .media import Downloader, MediaStore, UrllibDownloader
from .monitor import JobsReclaimer, ProcReader, PublicEndpointProbe, Sampler
from .mail.backends import ImapLibBackend, PopLibBackend, SmtpLibBackend
from .mail.route_secrets import migrate_plaintext as migrate_mail_route_secrets
from .mail.service import MailService
from .pool import Pool
from .pool_calibrate import PoolCalibrator
from .pressure import MemoryWatermark
from .runtime import AdbBackend, AdbCliBackend, ContainerBackend, DockerCliBackend, Runtime
from .runtime.runtime import Fs
from .scheduler import Scheduler
from .settings_secrets import migrate_plaintext as migrate_settings_secrets
from .store import Store
from .netprobe import SocketLevelProbe
from .sysenv import DockerProxyApplier, WslEnvReader
from .timesync import Aligner, TimeSync
from .vault_client import Vault, WinAgentVault, vault_name
from .webhook import HttpClient, UrllibHttp, WebhookDispatcher
from .wechat_slot import WechatSlot
from .winagent_client import Transport, WinAgentClient
from .workflow import WorkflowEngine

log = logging.getLogger("qtrade.app")

H02_INTERVAL_S = 30         # 04 H02:每 30 s ping(离线后按 [winagent] probe_interval_s 重探)
H03_INTERVAL_S = 30         # 04 H03:dockerd 每 30 s
WEBHOOK_TICK_S = 0.5        # 02 §2.2.7 字面:投递器每 500 ms 一轮
DISK_TICK_S = 15            # §2.8.8 磁盘水位与 health 同 15 s 一轮
CALIB_TICK_S = 300          # 04 §2.5.3:漂移检查 + 零账号自动重测
MAIL_SEND_TICK_S = 5        # 06 §2.4:出站队列消费单线程
MAIL_CONFIRM_TICK_S = 60    # 02 §2.2.11 同 60 s 节拍
MAIL_SECRET_MIGRATE_S = 300  # S-8 存量明文迁移的重试节拍(无明文时只读不写);规格未定,取 5 min
DAILY_TICK_S = 60           # daily_at 包装的检查节拍(每分钟看一次到点没有)


def daily_at(hhmm: str, fn: Callable[[], Awaitable[Any]], *, store, key: str, clock) -> Callable[[], Awaitable[None]]:
    """把「每日 HH:MM 一次」包成 scheduler 认识的「固定间隔」任务(scheduler 只有间隔、没有日历触发)。

    每 ``DAILY_TICK_S`` 一轮:到点(本地 Asia/Shanghai,00 §6)且**今天还没跑过**才跑;跑过的日期落
    ``settings[key]``,重启后不会重复跑。🔴 首轮遇到「今天已过点但库里没记录」只补记日期**不补跑**——
    否则装完 Agent 的那一刻就会立刻跑一次清理/备份,不是 §2.8.4「03:00 定时」的意思。
    """
    async def tick() -> None:
        now = datetime.fromtimestamp(clock() / 1000, tz=TZ_SHANGHAI)
        today = now.strftime("%Y-%m-%d")
        if now.strftime("%H:%M") < hhmm:
            return
        last = store.settings_get(key)
        if last == today:
            return
        store.settings_set(key, today, actor="system:scheduler")
        if last is None:
            log.info("daily_at(%s) 首轮只登记日期、不补跑(key=%s)", hhmm, key)
            return
        await fn()
    return tick


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
                 aligner: Optional[Aligner] = None, wsl_total_mb: Optional[int] = None, boot_poll_s: Optional[float] = None,
                 qq_transport_factory: Optional[Callable[[Account], OneBotTransport]] = None,
                 http: Optional[HttpClient] = None, disk: Optional[DiskProbe] = None, data_dir: Optional[str] = None,
                 imap_factory: Optional[Callable[[Any], Any]] = None, pop3_factory: Optional[Callable[[Any], Any]] = None,
                 smtp_factory: Optional[Callable[[Any], Any]] = None,
                 downloader: Optional[Downloader] = None, proc_reader: Optional[ProcReader] = None,
                 config_path: Optional[str] = None, net_probe: Optional[Any] = None,
                 docker_proxy: Optional[Any] = None, wsl_env_reader: Optional[Any] = None):
        self.cfg = cfg
        self.clock = clock
        self.wsl_gateway = wsl_gateway
        self.store = Store(db_path or cfg.db.path, clock=clock, out_merge_window_s=cfg.bus.out_merge_window_s, capture_text=cfg.messages.capture_text)
        self.events: Events
        self.alerts: Alerts
        self.health = Health(clock=clock)
        self.scheduler = Scheduler(clock=clock)
        self._sender = sender          # None ⇒ open() 按后端真假决定装 QidianUi 还是 _sender_not_wired
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
        self._qq_transport_factory = qq_transport_factory
        self._http = http
        self._disk = disk
        self._data_dir = data_dir
        self._imap_factory = imap_factory
        self._pop3_factory = pop3_factory
        self._smtp_factory = smtp_factory
        self._downloader = downloader
        self._proc_reader = proc_reader
        self.config_path = config_path      # #89 写回 agent.toml 的落点;None ⇒ 只落 settings 并回 restart_required
        # ---- #74 / #75 / #85 的三个可注入执行体(协议见 api/routes_ext:LevelProbe / snapshot() / apply|disable)
        #: 🔴 **缺省不出网**:`net_probe` 不注入、且 `[probe] agent_probe_enabled` 为 false(缺省)⇒ 不装四级探测器,
        #: #75 的 WSL 侧逐目标记 SKIPPED(agent_probe_disabled)。真正的自动装配在 `open()`(要先知道是不是真机后端)。
        self._net_probe_arg = net_probe
        self.net_probe = net_probe
        self._docker_proxy_arg = docker_proxy
        self._wsl_env_reader_arg = wsl_env_reader
        self._wechat_next_due: dict[str, int] = {}
        self.jobs: dict[str, asyncio.Task] = {}      # 00 §11.21 [JOB]:在跑的作业 task,#108 取消时要真的 cancel
        self.adapters: dict = {}
        self.bus: Bus
        self.poller: QidianPoller
        self.pool: Pool
        self.runtime: Runtime
        self.vault: Vault
        self.winagent: WinAgentClient
        self.timesync: TimeSync
        self.accounts: AccountService
        self.gate: Gate
        self.pressure: MemoryWatermark
        self.healthloop: HealthLoop
        self.qqhealth: QQHealth
        self.wechat_client: WeChatWinAgent
        self.wechat_poller: WechatPoller
        self.wechat_slot: WechatSlot
        self.wechat_login: WechatLoginFlow
        self.webhooks: WebhookDispatcher
        self.maintenance: MaintenanceService
        self.calibrator: PoolCalibrator
        self.workflows: WorkflowEngine
        self.hmac: HmacVerifier
        self.mail: MailService
        self.media: MediaStore
        self.sampler: Sampler
        self.reclaimer: JobsReclaimer
        self.endpoint_probe: PublicEndpointProbe
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
        # 机型档案库(05 §2.5.1):**单一来源 = [device_profiles] library 指的随包 JSON**;读不到就回落内置小清单 + ERROR 日志
        # (回落时的 ERROR 日志由 device_profiles.Library 自己记一条,这里不重复记)
        self.device_profiles = device_profiles_mod.load(self.cfg)
        self.winagent = WinAgentClient(self.cfg.winagent, transport=self._wa_transport, base_url=self._wa_base_url, token=self._wa_token, clock=self.clock)
        self.vault = self._vault if self._vault is not None else WinAgentVault(self.winagent)
        rt_kw = {} if self._boot_poll_s is None else {"boot_poll_s": self._boot_poll_s}
        # ---- #74 的 WSL 侧只读采集 / #85 的 docker 代理执行体:只在**真后端**下自动装
        # (注了假 adb/假容器的开发容器与测试一律不装 —— 它们一个读 /proc、一个写 /etc,都不该在测试里碰真机)
        real_host = self._adb is None and self._containers is None
        self.wsl_env_reader = self._wsl_env_reader_arg or (WslEnvReader() if real_host else None)
        # 🔴 docker_proxy 只写 drop-in、**不重启 dockerd**(sysenv.DockerProxyApplier 的 last_result.restart_required)
        self.docker_proxy = self._docker_proxy_arg or (DockerProxyApplier() if real_host else None)
        # 🔴 #75 WSL 侧四级探测:**缺省不出网** —— 只有 `[probe] agent_probe_enabled=true`(04 §7 owner,
        # 裁决 00 §15g R6-62 Ⅶ①)且真后端时才自动装;显式注入的探测器优先于配置。
        self.net_probe = self._net_probe_arg or (
            SocketLevelProbe() if (real_host and self.cfg.probe.agent_probe_enabled) else None)
        adb_backend = self._adb or AdbCliBackend()          # runtime 与企点 UI 执行层共用同一条 adb 后端
        self.runtime = Runtime(containers=self._containers or DockerCliBackend(), adb=adb_backend, cfg=self.cfg, health=self.health,
                               alerts=self.alerts, store=self.store, clock=self.clock, fs=self._fs, **rt_kw)
        self.poller = QidianPoller(store=self.store, events=self.events, alerts=self.alerts, cfg=self.cfg, h13_firing=self.health.h13_firing,
                                   clock=self.clock, maindb_factory=self._maindb_for_uid)
        # ---- 三通道适配器(建连都在账号 start 时才发生,open() 只建对象)
        qq_kw = {} if self._qq_transport_factory is None else {"transport_factory": self._qq_transport_factory}
        self.wechat_client = WeChatWinAgent(self.winagent)
        # 媒体子系统(02 §2.8.2):下载器可注入;真实现经 WinAgent 的 raw 出口取 chatlog 图片,开发容器里注 FakeDownloader
        self.media = MediaStore(self.store, media_dir=os.path.join(self.data_dir, "media"), clock=self.clock,
                                downloader=self._downloader if self._downloader is not None else
                                UrllibDownloader(honor_env_proxy=self.cfg.net.honor_env_proxy,
                                                 wa_raw=self._wa_media_raw))
        self.wechat_poller = WechatPoller(store=self.store, events=self.events, client=self.wechat_client, cfg=self.cfg,
                                          wechat_cfg=self.cfg.wechat_adapter, clock=self.clock)
        # 企点 UI 执行层(05 §2.1.1 ⑥~⑪ 登录 / 06 §2.9.5 发送):**只有真机后端才自动装**,
        # 注了假 adb 的开发容器/测试一律保持未接(要测 UI 层就显式传 sender/login_fn)。凭据仍由 accounts 从 Vault 取后传进来。
        real_backend = self._adb is None
        # `on_self_uid` / `on_default_profile` 都是「执行层报事实 → AccountService 落库/迁移」的单向通道;
        # lambda 延迟取 self.accounts —— 它在本行之后才建(装配顺序:适配器 → accounts)。
        self.qidian_ui = QidianUi(adb=adb_backend, store=self.store, alerts=self.alerts, clock=self.clock,
                                  on_default_profile=lambda aid, ver: self.accounts.note_default_profile(aid, ver))
        sender = self._sender or (self.qidian_ui.send_text if real_backend else _sender_not_wired)
        login_fn = self._login_fn or (
            self.qidian_ui.login_fn(on_self_uid=lambda aid, uid: self.accounts.note_self_uid(aid, uid))
            if real_backend else None)
        self.adapters = {
            "qidian": QidianAdapter(self.poller, sender=sender, store=self.store),
            "qq": QQAdapter(store=self.store, events=self.events, cfg=self.cfg, qq_cfg=self.cfg.qq, clock=self.clock, **qq_kw),
            "wechat": WechatAdapter(self.wechat_poller, client=self.wechat_client, store=self.store,
                                    media_put=self._wechat_screenshot_media),
        }
        self.gate = Gate(self.store)
        self.bus = Bus(store=self.store, events=self.events, adapters=self.adapters, cfg=self.cfg, clock=self.clock, gate=self.gate)
        from .api.app import load_capabilities
        caps, _ = load_capabilities()
        self.accounts = AccountService(store=self.store, events=self.events, pool=self.pool, runtime=self.runtime, vault=self.vault, cfg=self.cfg,
                                       adapters=self.adapters, health=self.health, clock=self.clock, login_fn=login_fn,
                                       caps_by_op={c["op"]: c for c in caps})
        self.accounts._alerts = self.alerts
        self.accounts._bus = self.bus
        self.pressure = MemoryWatermark(store=self.store, accounts=self.accounts, alerts=self.alerts, cfg=self.cfg, clock=self.clock)
        self.accounts.pressure = self.pressure
        # ---- 微信槽位 + 登录流(要 accounts.transition,故排在 accounts 之后)
        self.wechat_slot = WechatSlot(store=self.store, events=self.events, cfg=self.cfg, clock=self.clock, client=self.wechat_client,
                                      wechat_cfg=self.cfg.wechat_adapter, purge=self.runtime._purge_ephemeral,
                                      transition=self.accounts.transition)
        self.wechat_login = WechatLoginFlow(store=self.store, client=self.wechat_client, slot=self.wechat_slot, cfg=self.cfg,
                                            transition=self.accounts.transition, clock=self.clock)
        self.accounts._wechat_slot = self.wechat_slot
        self.accounts._wechat_login = self.wechat_login
        self.healthloop = HealthLoop(store=self.store, runtime=self.runtime, accounts=self.accounts, alerts=self.alerts, health=self.health,
                                     cfg=self.cfg, clock=self.clock)
        self.healthloop.wechat_adapter = self.adapters["wechat"]           # 05 §2.5.4 微信两条健康项(KEY_FAIL / SCREEN_LOCKED)
        self.healthloop.wechat_poller = self.wechat_poller
        self.healthloop.wechat_client = self.wechat_client
        self.qqhealth = QQHealth(adapter=self.adapters["qq"], store=self.store, alerts=self.alerts, cfg=self.cfg, clock=self.clock,
                                 busy=self.accounts.busy, on_login_required=self._qq_login_required)
        self.timesync = TimeSync(self.winagent, health=self.health, alerts=self.alerts, clock=self.clock, aligner=self._aligner, on_resume=self.on_host_resume)
        # ---- 横切:webhook / 保留期与磁盘 / 资源池自校准 / 工作流 / HMAC 公网入站
        self.http = self._http if self._http is not None else UrllibHttp(honor_env_proxy=self.cfg.net.honor_env_proxy)
        self.webhooks = WebhookDispatcher(self.store, http=self.http, secret_provider=self._vault_secret, cfg=self.cfg.webhook,
                                          alerts=self.alerts, clock=self.clock)
        self.events.on_emit = self._fanout_webhooks                        # §2.2.7:emit 落 ws 行后同步扇出 webhook 副本
        self.maintenance = MaintenanceService(self.store, cfg=self.cfg.retention, backup=self.cfg.backup, data_dir=self.data_dir,
                                              disk=self._disk, alerts=self.alerts, clock=self.clock,
                                              media_orphan_grace_h=self.cfg.media.orphan_grace_h,
                                              ws_retention_hours=self.cfg.events.ws_retention_hours)
        self.store.write_guard = self.maintenance.guard_write              # §2.8.8:写入报错先判磁盘满
        self.media.write_guard = self.maintenance.guard_write              # 同上,媒体落盘这一路(`store`/`mail`/`media` 三路齐)
        self.media._disk_allows = lambda: self.maintenance.media_downloads_allowed    # high 水位起转 lazy
        self.media._ingest_allows = lambda: self.maintenance.ingest_allowed           # critical 起连元数据都不写
        self.sampler = Sampler(self.store, reader=self._proc_reader, data_dir=self.data_dir, clock=self.clock, disk=self._disk)
        self.reclaimer = JobsReclaimer(self.store, reclaim_after_s=self.cfg.jobs.reclaim_after_s,
                                       events=self.events, clock=self.clock)
        self.endpoint_probe = PublicEndpointProbe(self.store, http=self.http, events=self.events,
                                                  urls=self.cfg.api.public_ip_probe_urls,
                                                  interval_s=self.cfg.api.public_ip_check_interval_s, clock=self.clock)
        self.calibrator = PoolCalibrator(self.store, cfg=self.cfg.calib, pool=self.pool, events=self.events, clock=self.clock)
        self.workflows = WorkflowEngine(self.store, bus=self.bus, events=self.events, http=self.http, clock=self.clock,
                                        webhook_timeout_ms=self.cfg.webhook.webhook_timeout_ms)
        self.hmac = HmacVerifier(self.store, secret_provider=self._vault_secret, cfg=self.cfg.hmac, clock=self.clock)
        self.mail = MailService(self.store, self.cfg.mail, clock=self.clock, alerts=self.alerts, secret_of=self._mail_secret,
                                imap_factory=self._imap_factory or self._real_imap, pop3_factory=self._pop3_factory or self._real_pop3,
                                smtp_factory=self._smtp_factory or self._real_smtp, disk_state=lambda: self.maintenance.level)
        self.scheduler.register("qidian_poll_all", self.cfg.qidian.poll_interval_s, self.qidian_poll_all)
        self.scheduler.register("qidian_gaps_all", self.cfg.qidian.gap_check_interval_s, self.qidian_gaps_all)
        self.scheduler.register("outbox_ws_retention", 3600, self.outbox_retention)
        self.scheduler.register("winagent_probe", min(H02_INTERVAL_S, self.cfg.winagent.probe_interval_s), self.winagent_probe, run_immediately=True)
        self.scheduler.register("dockerd_probe", H03_INTERVAL_S, self.dockerd_probe, run_immediately=True)
        self.scheduler.register("h13_clock_sync", H13_INTERVAL_S, self.h13_probe, run_immediately=True)
        self.scheduler.register("health_containers", self.cfg.health.container_check_s, self.healthloop.check_containers)      # H04
        self.scheduler.register("health_adb", self.cfg.health.adb_check_s, self.healthloop.check_adb)                          # H06
        self.scheduler.register("health_boot", H05_STEADY_INTERVAL_S, self.healthloop.check_boot)                              # H05 稳态
        self.scheduler.register("login_remind", 60, self.accounts.login_remind)                                                # 05 §2.5.4
        self.scheduler.register("health_napcat", H08_INTERVAL_S, self.qqhealth.check)                                          # 04 H08,每 15 s
        self.scheduler.register("health_wechat", self.cfg.wechat_adapter.poll_interval_s, self.healthloop.check_wechat)        # 05 §2.5.4 两条
        self.scheduler.register("wechat_slot_reaper", self.cfg.wechat.slot_reaper_interval_s, self.wechat_slot.reap)           # 02 §2.2.5 ②
        self.scheduler.register("wechat_poll_all", 1, self.wechat_poll_all)                                                    # 节拍由 poller 自报
        self.scheduler.register("webhook_dispatch", WEBHOOK_TICK_S, self.webhooks.tick)                                        # 02 §2.2.7
        self.scheduler.register("disk_watermark", DISK_TICK_S, self.disk_tick)                                                 # §2.8.8
        self.scheduler.register("pool_calibrate_drift", CALIB_TICK_S, self.calib_tick)                                         # 04 §2.5.3
        self.scheduler.register("retention_cleanup", DAILY_TICK_S,
                                daily_at(self.cfg.retention.cleanup_at, self.cleanup_job, store=self.store,
                                         key="scheduler.last_retention_cleanup_date", clock=self.clock))                       # §2.8.4 03:00
        self.scheduler.register("db_backup", DAILY_TICK_S,
                                daily_at(self.cfg.backup.backup_at, self.backup_job, store=self.store,
                                         key="scheduler.last_db_backup_date", clock=self.clock))                               # §3.3 03:30
        self.scheduler.register("mail_inbound", self.cfg.mail.inbound.poll_interval_s, self.mail_inbound_tick)                 # 06 §2.1
        self.scheduler.register("mail_outbound", MAIL_SEND_TICK_S, self.mail_outbound_tick)                                    # 06 §2.4
        self.scheduler.register("mail_confirm_reaper", MAIL_CONFIRM_TICK_S, self.mail_confirm_tick)                            # 06 §2.3.6
        # S-8(backend-sec-1):存量 `mail_routes` 明文凭据迁入 Vault;要连 Vault ⇒ 放运行期(不在 `--init-db`),Vault 不在就下轮再试
        self.scheduler.register("mail_route_secret_migrate", MAIL_SECRET_MIGRATE_S, self.mail_secret_migrate_tick, run_immediately=True)
        # backend-sec-2:#89 存量 `settings['config.<group>'|'config.__all__']` 里嵌套的明文密钥迁入 Vault;同上放运行期
        self.scheduler.register("settings_secret_migrate", MAIL_SECRET_MIGRATE_S, self.settings_secret_migrate_tick, run_immediately=True)
        self.scheduler.register("monitor_sample", self.cfg.monitor.sample_interval_s, self.monitor_tick)                       # 04 §2.4.5
        self.scheduler.register("jobs_reclaimer", self.cfg.jobs.reclaim_interval_s, self.reclaimer.reclaim_once)               # 02 §3.1 R6-16
        self.scheduler.register("public_endpoint_probe", 60, self.endpoint_probe.tick)                                         # E-3;默认关
        self.scheduler.register("media_fetch", 5, self.media_tick)                                                             # 02 §2.8.2
        return self

    async def _wa_media_raw(self, path: str) -> bytes:
        """真下载器取微信 chatlog 图片的出口(#40 ``GET /wa/v1/wechat/media/<md5>``,二进制)。"""
        status, _headers, raw = await self.winagent.request("GET", path, timeout_s=30.0, raw=True)
        if status != 200:
            raise RuntimeError(f"winagent media http_{status}")
        return raw

    def _wechat_screenshot_media(self, acct: Account, png: bytes) -> dict[str, Any]:
        """微信截图落 ``media/``(02 §2.8.2),只把引用给回 ``CommandResult.data``。"""
        try:
            out = self.media.put_bytes(png, kind="image", origin={"kind": "wechat_screenshot", "account_id": acct.id})
        except Exception as e:                       # 落盘失败不影响截图本身(字节仍在 data.png 里)
            log.warning("微信截图落 media 失败 account=%s: %s", acct.id, e)
            return {}
        return {k: out[k] for k in ("media_id", "sha256") if out.get(k) is not None}

    async def media_tick(self) -> None:
        """02 §2.8.2:把 ``messages.media_json`` 里新出现的引用建成 ``media`` 行,再把 ``policy=eager`` 的下下来。

        ``lazy``(``file``/``video``)只建行、等 #96 触发;磁盘 high 水位起整轮不下载(``maintenance`` 的开关)。
        """
        await asyncio.to_thread(self.media.scan_messages)
        await self.media.fetch_eager()

    async def monitor_tick(self) -> None:
        """04 §2.4.5:每 ``[monitor] sample_interval_s`` 采一轮 ``raw``,顺带做 1m/1h 降采样。

        🔴 这是 ``pool_calibrate`` 与 #77 的**唯一数据源** —— 没有它自校准在空库上恒回「无建议」。
        """
        await self.sampler.sample_once()
        await asyncio.to_thread(self.sampler.roll_up)

    # ------------------------------------------------------------------ 装配期小工具
    @property
    def data_dir(self) -> str:
        """02 §4 固定目录根(``media/``、``cores/``、``mail/archive/``、``accounts/<id>/raw/`` 都在它下面)。
        缺省取 ``agent.db`` 所在目录 —— 开发容器里那就是 tmp,不会去碰 ``/var/lib/qtrade``。"""
        if self._data_dir:
            return self._data_dir
        if self.store.path == ":memory:":
            return os.getcwd()
        return os.path.dirname(os.path.abspath(self.store.path))

    async def _vault_secret(self, row: dict) -> Optional[str]:
        """webhook / HMAC 共用:登记行的 ``secret_ref`` → Vault 明文(基线 §11.1)。"""
        ref = row.get("secret_ref")
        if not ref:
            return None
        return await self.vault.read(vault_name(str(ref)), trace_id=new_trace_id())

    def _mail_secret(self, ref: str) -> str:
        """06 §2.0:邮件后端要的是同步取密;Vault 客户端是 async ⇒ 这里只在事件循环外的后端工厂里用。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:                       # 事件循环里不能同步等:把取密交给调用方在建连前完成
            raise RuntimeError("mail 后端的取密须在事件循环外完成(建连发生在 to_thread 里)")
        return asyncio.run(self.vault.read(vault_name(ref), trace_id=new_trace_id())) or ""

    def _real_imap(self, r):
        return ImapLibBackend(host=r.inbound.host, port=r.inbound.port, ssl=r.inbound.ssl, user=r.inbound.user,
                              password=self._mail_secret(r.inbound.secret_ref), send_id=r.inbound.send_imap_id)

    def _real_pop3(self, r):
        return PopLibBackend(host=r.inbound.fallback.host or r.inbound.host, port=r.inbound.fallback.port,
                             ssl=r.inbound.fallback.ssl, user=r.inbound.user, password=self._mail_secret(r.inbound.secret_ref))

    def _real_smtp(self, r):
        return SmtpLibBackend(host=r.outbound.host, port=r.outbound.port, ssl=r.outbound.ssl, user=r.outbound.user,
                              password=self._mail_secret(r.outbound.secret_ref), timeout_s=r.outbound.timeout_s)

    def _fanout_webhooks(self, **kw) -> None:
        """``Events.on_emit`` 钩子:每条事件给订阅方各写一行 ``target='webhook:<id>'`` 的 pending outbox。

        E-3 例外(§2.2.12):``NET_PUBLIC_ENDPOINT_CHANGED`` 必须推给**全部** ``enabled=1`` 登记方、不受订阅过滤。"""
        payload = kw.get("payload") or {}
        ignore = kw.get("event") == "net" and payload.get("code") == "NET_PUBLIC_ENDPOINT_CHANGED"
        self.webhooks.fanout(ignore_filters=ignore, **kw)

    async def _qq_login_required(self, account_id: str, state_code: str) -> None:
        """04 H08:``online=false`` 持续 2 min ⇒ 置 ``login_required`` + 推 ``account_state`` 事件,**不自动重登**(D-2)。"""
        self.accounts.transition(account_id, "login_required", state_code=state_code,
                                 state_reason="napcat 报离线超过 2 分钟(H08)")

    def _maindb_for_uid(self, self_uid: str, acct: Optional[Account] = None) -> MainDb:
        acct = acct or self._current_qidian_account
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

    async def wechat_poll_all(self) -> None:
        """05 §2.4.4 ⑦:**每账号周期是动态的** —— 平时 ``[adapters.wechat] poll_interval_s``(5 s),
        发送确认期 ``confirm_poll_interval_ms``(1 s)。scheduler 只有固定间隔,故这里每秒一轮、自己判到点。"""
        ad = self.adapters.get("wechat")
        if ad is None:
            return
        now = self.clock()
        for row in self.store.list_accounts(channel="wechat", state="running"):
            if now < self._wechat_next_due.get(row["id"], 0):
                continue
            acct = self._wechat_account(row)
            try:
                await ad.poll(acct)
            finally:
                self._wechat_next_due[row["id"]] = self.clock() + int(self.wechat_poller.interval_s(row["id"]) * 1000)

    @staticmethod
    def _wechat_account(row: dict) -> Account:
        return Account(id=row["id"], channel="wechat", state=row["state"], self_uid=row.get("self_uid"),
                       self_nick=row.get("self_nick"), state_code=row.get("state_code"))

    async def disk_tick(self) -> None:
        """§2.8.8 三级水位:每 15 s 量一次 ``data_dir`` 余量,翻档时发 ``H12_DISK_LOW`` 并切运行期开关。"""
        await asyncio.to_thread(self.maintenance.check_watermark)

    async def calib_tick(self) -> None:
        """04 §2.5.3:漂移检查(>30% 持续 1h ⇒ ``POOL_CALIBRATION_DRIFT``)+ 零账号持续 ≥5 min 自动重测。"""
        running = sum(1 for r in self.store.list_accounts(state="running") if r["host"] == "wsl")
        self.calibrator.note_running_count(running)
        self.calibrator.maybe_auto_calibrate()
        self.calibrator.check_drift()

    # ------------------------------------------------------------------ 异步作业(00 §11.21 [JOB] 统一契约)
    # ------------------------------------------------------------------ C-1 实测采样(04 §3.4 / §2.8.4 + 02 #76b 的上游半)
    #: Windows 侧进程名 → 通道。04 §3.4 的 `sample` 行只给 `pid_name`(Windows 侧只能按 PID 反推),
    #: `channel`/`account_id` 由**回写方**补(裁决 A-17);认不出的留 NULL = 宿主机进程(02 §3.2 允许)。
    PID_NAME_CHANNEL = {"wechat.exe": "wechat", "weixin.exe": "wechat"}

    async def sample_observed(self, *, duration_s: float = 5.0) -> dict[str, Any]:
        """实测采样一轮并回写候选。

        04 §3.4 逐字:WinAgent 的 ``POST /probe {mode:'sample'}`` **只返回不落库**;三侧(容器 / WSL /
        Windows)的候选由 **Agent 聚合后经 ``PUT /probes {kind:'observed', rows}`` 写一次** ——
        各写各的会让同一条连接的 ``hits`` 被算两遍(唯一索引按 ``(account_id, ip, port, proto)`` 累加)。
        回写方补 ``side``(自己从哪侧采的)与能反推的 ``channel``/``account_id``。
        """
        out = await self.winagent.sample_probe(duration_s=duration_s)
        wechat_running = [a["id"] for a in self.store.list_accounts(channel="wechat", state="running")]
        rows: list[dict[str, Any]] = []
        for raw in (out.get("rows") or []):
            row = dict(raw)
            row["side"] = row.get("side") or "windows"              # Windows 侧采的,WinAgent 不给这一列
            if not row.get("channel"):
                ch = self.PID_NAME_CHANNEL.get(str(row.get("pid_name") or "").lower())
                if ch:
                    row["channel"] = ch
            # 微信在 Windows 侧只有一个槽位(05 §2.4):恰好一个 running 才归它,否则留 NULL 不猜
            if row.get("channel") == "wechat" and not row.get("account_id") and len(wechat_running) == 1:
                row["account_id"] = wechat_running[0]
            rows.append(row)
        written = await self.winagent.write_observed(rows) if rows else 0
        log.info("实测采样回写 %d/%d 行(sampled_at=%s)", written, len(rows), out.get("sampled_at"))
        return {"sampled_at": out.get("sampled_at"), "duration_s": out.get("duration_s"), "rows": rows, "written": written}

    def spawn_job(self, job_id: str, kind: str, body: Callable[[], Awaitable[dict[str, Any]]]) -> asyncio.Task:
        """把一个作业体挂成 task 并登记进 ``self.jobs``(#108 取消时要能真的 cancel 它)。

        终态一律落 ``jobs`` 表并推 ``job`` 事件(``succeeded|failed|cancelled``,00 §7.5)。
        """
        async def run() -> None:
            self.store.job_start(job_id)
            try:
                result = await body()
            except asyncio.CancelledError:
                self.store.job_cancel(job_id, actor="system:job")
                self.events.emit("job", payload={"job_id": job_id, "kind": kind, "state": "cancelled"})
                raise
            except DiskFullError as e:
                # §2.8.8:作业体里的写失败同样**先判磁盘满** —— 落 `DISK_FULL`(不是 `INTERNAL`),
                # 带 free_mb 三数,`retryable=false`(备份/导出/清理作业盘满时自动重跑只会更满)。
                log.error("作业因磁盘满失败 job=%s kind=%s: %s", job_id, kind, e.message)
                err = {"code": "DISK_FULL", "message": e.message, "retryable": False, "needs_human": True,
                       "evidence": e.evidence()}
                self.store.job_finish(job_id, ok=False, error=err)
                self.events.emit("job", payload={"job_id": job_id, "kind": kind, "state": "failed", "error": err})
            except Exception as e:
                log.exception("作业失败 job=%s kind=%s: %s", job_id, kind, e)
                self.store.job_finish(job_id, ok=False, error={"code": "INTERNAL", "message": str(e)})
                self.events.emit("job", payload={"job_id": job_id, "kind": kind, "state": "failed",
                                                 "error": {"code": "INTERNAL", "message": str(e)}})
            else:
                self.store.job_finish(job_id, ok=True, result=result)
                self.events.emit("job", payload={"job_id": job_id, "kind": kind, "state": "succeeded", "result": result})
            finally:
                self.jobs.pop(job_id, None)

        t = asyncio.create_task(run(), name=f"job:{job_id}")
        self.jobs[job_id] = t
        return t

    async def cancel_job(self, job_id: str) -> bool:
        """#108:真把在跑的 task cancel 掉。

        返回 **True = 确实 cancel 了一个在跑的 task**(库里的状态与 ``job`` 事件由作业体收尾时写,调用方不要重复发);
        **False = 没有在跑的 task**(队列里还没起、或纯同步作业),此时只改库、``job`` 事件由调用方发。
        """
        t = self.jobs.pop(job_id, None)
        if t is not None and not t.done():
            t.cancel()
            await self._await_quietly(t, f"job {job_id}")
            return True
        self.store.job_cancel(job_id, actor="system:job")
        return False

    async def export_identity_job_body(self, job_id: str, account_id: str) -> dict[str, Any]:
        """#99 **仅 QQ**:把 ``accounts/<id>/data``(qq_data 卷)打成 tar,产物路径进 ``jobs.result_json``。

        账号已在 ``stopped``(端点前置判过),直接打包宿主目录;经 ``GET /exports/{job_id}/file`` 下载。
        """
        import tarfile
        src = self.runtime.data_dir(account_id)
        out_dir = os.path.join(self.data_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{account_id}-qq_data-{job_id}.tar")

        def pack() -> int:
            with tarfile.open(path, "w") as tf:
                tf.add(src, arcname=f"{account_id}/data")
            return os.path.getsize(path)

        size = await asyncio.to_thread(pack)
        self.store.insert_audit(kind="system", transport="system", actor="system:export", action="account.export_identity",
                                account_id=account_id, result_code="OK", detail={"job_id": job_id, "bytes": size},
                                now_ms=self.clock())
        return {"file_path": path, "bytes": size, "account_id": account_id}

    async def reconnect_adb(self, row: dict[str, Any]) -> dict[str, Any]:
        """#100 / 04 H06 自愈同一实现:``adb disconnect`` + ``connect``;回真实的 ``adb_state``。"""
        serial = row.get("adb_serial") or f"127.0.0.1:{16000 + int(row['seq'])}"
        await self.runtime._adb.disconnect(serial)
        ok = await self.runtime._adb.connect(serial)
        if ok:
            await self.runtime.ensure_root(row)                     # R6-28:reconnect 成功后复提权
        devices = await self.runtime._adb.devices()
        return {"serial": serial, "reconnected": bool(ok), "adb_state": devices.get(serial) or "missing"}

    async def restart_stream(self, row: dict[str, Any]) -> dict[str, Any]:
        """#101 / 04 H07 自愈同一实现:重建 ``adb forward`` 与 scrcpy-server。

        画面流执行体(``screen_scrcpy.ScrcpyBackend``)装配了 ⇒ 交给它:有在跑的流就真重拉、已连 WS 收
        ``{type:'restart'}``;没人在看只重建 forward、``stream_restarted:false``。
        没装配 ⇒ 只重建 ``adb forward``,如实回 ``stream_restarted:false``(rulings R6-58 (cw))。
        """
        serial = row.get("adb_serial") or f"127.0.0.1:{16000 + int(row['seq'])}"
        port = row.get("stream_port") or (16500 + int(row["seq"]))
        restart = getattr(getattr(self, "stream_backend", None), "restart", None)
        if restart is not None:
            try:
                res = await restart(row["id"])
            except Exception as e:
                log.warning("#101 重建画面流失败 account=%s: %s", row["id"], e)
                res = {"forward_rebuilt": False, "stream_restarted": False}
            return {"serial": serial, "stream_port": port, "forward_rebuilt": bool(res.get("forward_rebuilt")),
                    "stream_restarted": bool(res.get("stream_restarted"))}
        forwarded = False
        fn = getattr(self.runtime._adb, "forward", None)
        if fn is not None:
            try:
                await fn(serial, f"tcp:{port}", "localabstract:scrcpy")
                forwarded = True
            except Exception as e:
                log.warning("#101 重建 adb forward 失败 account=%s: %s", row["id"], e)
        return {"serial": serial, "stream_port": port, "forward_rebuilt": forwarded, "stream_restarted": False}

    async def cleanup_job_body(self) -> dict[str, Any]:
        rep = await asyncio.to_thread(self.maintenance.cleanup_once)
        return {"freed_mb": rep.freed_mb, "deleted": rep.deleted, "files_removed": rep.files_removed,
                "retention_days": rep.retention_days, "errors": rep.errors}

    async def calibrate_job_body(self, job_id: str, *, apply: bool = False, source: str = "manual",
                                 account_id: Optional[str] = None, channel: Optional[str] = None) -> dict[str, Any]:
        """#71 全局 / #25 单账号自校准的作业体。

        🔴 总控裁决(rulings R6-58 (ao)):**#25 与 #71 统一走 00 §11.21 [JOB] 的 `202 {job_id}`**
        (00 优先于 02 §3.4.6「#71 同步返回建议值」;02 #25 写的 `run_id` 字样同改 `job_id`)。
        """
        if account_id is None:
            return {"scope": "global", **self.calibrator.calibrate(apply=apply, source=source).as_dict()}
        value, evidence = self.calibrator.suggest_quota_mb(str(channel), now_ms=self.clock())
        # 单账号只量该通道的 quota_mb,并进 resource_pools.calibration_json.per_account;**不动 quota_json**
        self.store.pool_calibration_note(run_id=job_id, account_id=account_id, channel=str(channel),
                                         quota_mb=value, evidence=evidence)
        return {"scope": "account", "account_id": account_id, "channel": channel, "quota_mb": value, "evidence": evidence}

    async def cleanup_job(self) -> None:
        rep = await asyncio.to_thread(self.maintenance.cleanup_once)
        log.info("保留期清理完成:释放 %.1f MB,删除 %s", rep.freed_mb, rep.deleted)
        await asyncio.to_thread(self.maintenance.incremental_vacuum)

    async def backup_job(self) -> None:
        path = await asyncio.to_thread(self.maintenance.backup_once)
        n = await asyncio.to_thread(self.maintenance.prune_backups)
        log.info("在线备份完成:%s(清理旧备份 %d 份)", path, n)

    async def mail_inbound_tick(self) -> None:
        """06 §2.1 取信一轮 + §2.2 派发进总线(``[mail] enabled=false`` 时 ``fetchers`` 为空,整轮是 no-op)。"""
        if not self.cfg.mail.enabled:
            return
        await asyncio.to_thread(self.mail.fetch_once)
        await self.mail.dispatch(self.bus)

    async def mail_outbound_tick(self) -> None:
        if not self.cfg.mail.enabled:
            return
        await asyncio.to_thread(self.mail.send_once)

    async def mail_confirm_tick(self) -> None:
        if not self.cfg.mail.enabled:
            return
        await asyncio.to_thread(self.mail.reap_confirms)

    async def mail_secret_migrate_tick(self) -> None:
        """与 ``[mail] enabled`` 无关:关着邮件也不许库里躺着明文。没有明文的库只读一遍、一条语句不写。"""
        if await migrate_mail_route_secrets(self.mail.ms, self.vault):
            self.mail.reload()                      # 引用改成本路由自己的路径,取信/发信线程按新引用重建

    async def settings_secret_migrate_tick(self) -> None:
        """没有明文的库只读一遍、一条语句不写;Vault 不在 ⇒ 行原样保留、下轮再试。"""
        await migrate_settings_secrets(self.store, self.vault)

    async def outbox_retention(self) -> None:
        cutoff = self.clock() - self.cfg.events.ws_retention_hours * 3600 * 1000
        n = await asyncio.to_thread(self.store.purge_outbox_ws, cutoff)
        if n:
            log.info("events_outbox ws 行保留期清理 %d 行", n)

    async def winagent_probe(self) -> None:
        """H02:``GET /wa/v1/ping``(无鉴权 2 s)→ 在线再 ``GET /wa/v1/health`` 取 user_agent / host 快照 → pool 的 windows 池输入(02 §2.5 降级 ①)。"""
        pong = await self.winagent.ping()
        if pong is None:
            # 04 §2.6.3 末:「Agent 缓存主机 IP,**H02 失败时重新发现**(子网可能变了)」——
            # 并按同节的择优:host.json 与默认网关不一致时以能 ping 通 /wa/v1/ping 的那个为准(discover 里记 warn)。
            self.winagent.forget_base_url()
            pong = await self.winagent.discover() and await self.winagent.ping()
        if pong is None:
            self.health.set_winagent(False)
            if self.health.winagent_online is False:
                # 02 §3.7 登记 `H02_WINAGENT_API_DOWN`(crit,subject=host,事件族 alert):去抖满 3 次判离线后才发。
                # `checks.H02` 与 `alerts` 必须同源,否则 #72 同一次响应里会出现 checks.H02=firing 而 alerts=[]。
                self.alerts.firing(H02_WINAGENT_API_DOWN, subject="host")
                self.pool.set_windows(known=False)
                self.pressure.evaluate(None)                                   # 读不到整机内存 = unknown,不阻断
            return
        self.alerts.resolve(H02_WINAGENT_API_DOWN, subject="host")            # ping 通即恢复(去重键翻转才发事件)
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
        # E-19 内存水位:整机可用内存 → warn/critical;critical 时只对显式开了 auto_stop_on_pressure 的账号按 LRU 逐停
        self.pressure.evaluate(host.get("available_mb"))
        await self.pressure.enforce()

    async def dockerd_probe(self) -> None:
        ok = await self.runtime.dockerd_ok()
        self.health.set_dockerd(ok)
        # 02 §3.7 登记 `H03_DOCKERD_DOWN`(crit,subject=wsl,事件族 alert);同 H02,与 `checks.H03` 同源。
        if ok:
            self.alerts.resolve(H03_DOCKERD_DOWN, subject="wsl")
        else:
            self.alerts.firing(H03_DOCKERD_DOWN, subject="wsl")

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
        """与 ``open``/``start`` 对称地收:先停计时(不再起新轮)→ 停投递器/工作流 → 断适配器连接 → 关总线 → 关库。"""
        if self._started:
            await self.scheduler.stop()
            self._started = False
        await self._stop_quietly(self.webhooks.stop(), "webhook 投递器")
        for run_id, t in list(getattr(self.workflows, "tasks", {}).items()):
            t.cancel()
            await self._await_quietly(t, f"workflow run {run_id}")
        ad = self.adapters.get("qq")
        if ad is not None:
            await self._stop_quietly(ad.close(), "QQ OneBot 连接")
        wa = self.adapters.get("wechat")
        if wa is not None:
            for row in self.store.list_accounts(channel="wechat"):
                await self._stop_quietly(wa.stop(self._wechat_account(row), graceful=True), f"微信读循环 {row['id']}")
        sb = getattr(self, "stream_backend", None)
        if sb is not None and hasattr(sb, "aclose"):
            await self._stop_quietly(sb.aclose(), "画面流(scrcpy-server)")
        if getattr(self, "bus", None) is not None:
            await self.bus.close()
        self.store.close()

    @staticmethod
    async def _stop_quietly(coro, what: str) -> None:
        try:
            await coro
        except Exception as e:                    # 收尾的任一步失败都不许挡住后面的收尾
            log.warning("停止 %s 时出错: %s", what, e)

    @staticmethod
    async def _await_quietly(task, what: str) -> None:
        try:
            await task
        except (asyncio.CancelledError, Exception) as e:
            log.debug("收尾 %s: %r", what, e)

    def create_api(self):
        from .api.app import create_api
        return create_api(self)
