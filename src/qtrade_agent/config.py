"""agent.toml 的默认值 —— 规格唯一出处:docs/02 §7.1(镜像对账表 docs/07)。

只登记本期骨架消费的键;新增键先进 02 §7.1 再加到这里。值必须与 02 §7.1 逐字相同
(docs/check-truth-tables.py ⑩ 只查文档之间,代码这份靠 tests/test_config_matches_docs.py 对账)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .hmac_inbound import HmacConfig
from .mail.config import MailConfig
from .maintenance import BackupConfig, RetentionConfig
from .pool_calibrate import CalibrationConfig
from .webhook import WebhookConfig


@dataclass(frozen=True)
class BusConfig:
    send_min_interval_ms: int = 1500
    send_rand_extra_ms: int = 1500
    confirm_timeout_qq_ms: int = 5000
    confirm_timeout_qidian_ms: int = 15000      # R6-38:须盖住主库出向落库滞后 7~12 s + 1 s 加速轮
    confirm_timeout_wechat_ms: int = 10000
    default_timeout_ms: int = 30000
    idempotency_ttl_days: int = 7
    queue_max_per_account: int = 200
    out_merge_window_s: int = 60                # 入向撞到自己发的合并窗(02 §2.8.1 / 06 §2.12)


@dataclass(frozen=True)
class QidianAdapterConfig:
    read_strategy: str = "auto"                 # auto|db|uiautomator|screenshot
    poll_interval_s: int = 5
    confirm_poll_interval_ms: int = 1000        # R6-38:发送确认窗内只对目标会话表加速
    gap_check_interval_s: int = 60              # R6-39:check_group_gaps 周期
    gap_window_days: int = 3
    gap_min_missing: int = 5
    #: #34 画面流四档(02 §7.1 逐字;A.2 / C-08)
    stream_profiles: dict[str, str] = field(default_factory=lambda: {
        "thumb": "540p@5", "thumb10": "540p@10", "focus": "720p@30", "focus15": "720p@15"})
    #: 随包 scrcpy-server 的位置与版本(版本须与 jar 严格一致,作 server 第一个参数)。
    #: ⚠️ 这两键 02 §7.1 尚未登记(见交接 fix-agent-scrcpy-2026-09-26),rootfs 落点见 installer/rootfs/Dockerfile
    scrcpy_server_path: str = "/opt/qtrade/scrcpy/scrcpy-server"
    scrcpy_server_version: str = "4.1"
    #: 画面流关键帧 / 掉队参数(第三轮验收 B1、缺口 G1)。⚠️ 同样 02 §7.1 尚未登记(见交接 fix-scrcpy-r4-2026-09-26)
    scrcpy_key_wait_s: float = 3.0              # RESET_VIDEO 后等关键帧,超时重拉 server(真机约 63 ms 就到)
    scrcpy_reset_min_interval_s: float = 2.0    # 同通道两次 RESET_VIDEO 的最小间隔,间隔内请求合并
    scrcpy_lag_evict_count: int = 3             # 订阅者在窗口内掉队这么多次 ⇒ 摘下、发 {type:'restart'}
    scrcpy_lag_evict_window_s: float = 10.0


@dataclass(frozen=True)
class QQAdapterConfig:
    """02 §7.1 ``[adapters.qq]``(镜像对账表 docs/07 第 54 行)。

    ``heartbeat_timeout_s`` 是**传输层**自判重连触发点,与 04 ``[health] napcat_heartbeat_timeout_s=30``
    (H08 **告警阈值**)不同源 —— 两值不同是规格原样,见 `.omc/handoffs/integrator-rulings.md` R6-58 (a)。
    """
    heartbeat_timeout_s: int = 40
    reconnect_delay_s: int = 3
    history_backfill_on_reconnect: int = 50


@dataclass(frozen=True)
class WechatAdapterConfig:
    """02 §7.1 ``[adapters.wechat]``(07 §[adapters.wechat] 镜像;C6:``switch_drain_timeout_s`` 归 agent.toml)。"""
    poll_interval_s: int = 5
    confirm_poll_interval_ms: int = 1000
    switch_drain_timeout_s: int = 60


@dataclass(frozen=True)
class MediaConfig:
    """02 §7.1 ``[media]``:本期只登记被消费的两键(``dir`` / ``orphan_grace_h``)。"""
    dir: str = "/var/lib/qtrade/media"
    orphan_grace_h: int = 24


@dataclass(frozen=True)
class NetConfig:
    """02 §7.1 ``[net]``:本期只登记被消费的 ``honor_env_proxy``(Agent 自身 HTTP 客户端是否读环境代理)。"""
    honor_env_proxy: bool = False


@dataclass(frozen=True)
class MessagesConfig:
    late_after_s: int = 120                     # R6-39:payload.late = lag_s > late_after_s
    capture_text: bool = True
    raw_payload: bool = False


@dataclass(frozen=True)
class HealthConfig:
    """04 §7 [health](owner=04;02 §7.1 镜像)。"""
    adb_root_grace_s: int = 15                  # ensure_root 宽限窗(04 owner;06 §2.9.5 引用)
    container_check_s: int = 10                 # H04 周期
    adb_check_s: int = 30                       # H06 周期
    napcat_heartbeat_timeout_s: int = 30
    scrcpy_frame_timeout_s: int = 10            # 只管开流后等首关键帧;静止画面 0 帧是常态,不当 H07
    clock_drift_warn_s: int = 2
    container_mem_warn_pct: int = 90
    container_restart_backoff_s: tuple[int, ...] = (60, 120, 300, 600)   # H04 退避重拉
    container_restart_max: int = 5              # 每小时上限,超限停止自愈(02 §5)


@dataclass(frozen=True)
class ApiConfig:
    bind: str = "0.0.0.0"
    port: int = 17600                           # 00 §3
    ws_impl: str = "websockets"                 # uvicorn ws=,不许 auto(02 §2.2)
    rate_default_per_min: int = 120
    http_sync_max_wait_ms: int = 25000          # 同步等待上限(P-10),超过转 202
    unauth_health_sources: tuple[str, ...] = ("127.0.0.1/32", "::1/128", "wsl_gateway")
    api_version: str = "1.0"                    # 只读,随代码(02 §3.8)
    public_ip_check_interval_s: int = 0         # E-3 公网出口探测周期;🔴 **默认 0 = 关**(§11.22 [SCOPE])
    public_ip_probe_urls: tuple[str, ...] = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")


@dataclass(frozen=True)
class JobsConfig:
    """02 §7.1 ``[jobs]``(R6-16):``jobs.state='running'`` 超 ``reclaim_after_s`` 由 ``jobs_reclaimer`` 回收。"""
    reclaim_after_s: int = 900
    reclaim_interval_s: int = 60


@dataclass(frozen=True)
class MonitorConfig:
    """02 §7.1 ``[monitor]``(owner=04 §7):Agent 侧 ``health_samples`` 的采样节拍。"""
    sample_interval_s: int = 10
    slow_interval_s: int = 60


@dataclass(frozen=True)
class EventsConfig:
    ws_queue_max: int = 10000
    ws_retention_hours: int = 72                # events_outbox target='ws' 保留(02 §2.2.7 / §7.1)


WS_PING_INTERVAL_S = 20                         # 02 §3.4.7 字面:心跳每 20 s(不是配置项)


@dataclass(frozen=True)
class DbConfig:
    path: str = "/var/lib/qtrade/agent.db"
    busy_timeout_ms: int = 5000
    read_pool: int = 4


@dataclass(frozen=True)
class RuntimeConfig:
    """02 §7.1 [runtime]:docker 编排参数(02 §2.2.4)。"""
    docker_socket: str = "unix:///var/run/docker.sock"
    redroid_image: str = "redroid/redroid:11.0.0-latest"
    napcat_image: str = "mlikiowa/napcat-docker:latest"
    qidian_mem_limit_mb: int = 3584             # A.5 建议 3.5G
    qq_mem_limit_mb: int = 1024
    qidian_resolution: str = "720x1280"
    qidian_dpi: int = 320
    gpu_mode: str = "guest"
    boot_timeout_s: int = 180                   # 等 boot_completed(C-43 唯一定义处;04 [health] 引用)
    start_serial: bool = True                   # 启动串行开关(不建议关)
    apk_url: str = ""                           # 企点内部下载地址(全系统唯一定义处)
    apk_sha256: str = ""
    apk_cache_dir: str = "/var/lib/qtrade/apk"
    auto_restart_max_per_hour: int = 5
    webui_temp_minutes: int = 10                # QQ WebUI 临时开启时长(C-35)
    accounts_dir: str = "/var/lib/qtrade/accounts"     # 卷目录根:<accounts_dir>/<id>/data(02 §2.2.4 字面路径)


@dataclass(frozen=True)
class PoolConfig:
    """02 §7.1 [pool]:quota_* 仅建表初始值,真值在 resource_pools.quota_json(C-43)。"""
    wsl_reserved_mb: int = 2048
    windows_reserved_mb: int = 4096
    quota_qidian_mb: int = 2560
    quota_qq_mb: int = 614
    quota_wechat_mb: int = 1536
    autocalibrate_on_first_login: bool = True
    mem_warn_mb: int = 2048                     # = 04 [monitor] mem_warn_mb(唯一出处,02 §7.1 只引用;E-19)
    mem_critical_mb: int = 1024                 # = 04 [monitor] mem_critical_mb


@dataclass(frozen=True)
class WinAgentConfig:
    """02 §7.1 [winagent] + §2.5 调用契约。"""
    url: str = ""                               # 空=自动:先 host_ip_hint_file,再默认网关
    token_file: str = "/etc/qtrade/winagent.token"
    timeout_ms: int = 3000                      # 通用;各动作按 §2.5 覆盖(ping/health 2 s、time/vault 3 s)
    probe_interval_s: int = 60                  # 离线重探
    host_ip_hint_file: str = "/run/qtrade/host.json"   # 04 [net] host_ip_hint_file(WinAgent 经会话代理写)


@dataclass(frozen=True)
class WechatConfig:
    """02 §7.1 [wechat](agent.toml 段;与 winagent.toml [wechat] 同名不同键)。"""
    slot_pending_ttl_s: int = 600
    slot_reaper_interval_s: int = 60
    slot_error_takeover_s: int = 300


@dataclass(frozen=True)
class DeviceProfilesConfig:
    """05 §7 [device_profiles](owner=05;07 §[device_profiles] 已登记这两键)。

    ``library`` = 随 Agent 包落盘的机型档案库 JSON(05 §2.5.1「随 Agent 包内置 ``device_profiles.json``,只读」)。
    ⚠️ **本默认值逐字取 05 §7**;installer 实际把该文件落在 ``/opt/qtrade/profiles/device_profiles.json``
    (`installer/rootfs/Dockerfile:72`),两处路径**不一致**——属文档/installer 侧的裁决项,见交接,本批不擅自改字面值。
    """
    library: str = "/opt/qtrade/agent/data/device_profiles.json"
    allow_template_reuse_after: int = 50


@dataclass(frozen=True)
class ProbeConfig:
    """04 §7 [probe](owner=04 §7;07 §[probe] 同登,裁决 00 §15g R6-62 Ⅶ①)。

    ``agent_probe_enabled`` = WSL 侧四级探测(dns→tcp→tls→http)的总开关,**缺省 false = 缺省不出网**;
    为 false 时 #75 ``mode:"full"`` 的 WSL 侧逐目标记 ``SKIPPED`` + ``detail = agent_probe_disabled``
    (04 §3.4;Windows 侧不受本键影响)。
    """
    agent_probe_enabled: bool = False


@dataclass(frozen=True)
class AccountsConfig:
    """05 §7 [accounts](owner=05)。"""
    login_timeout_s: int = 90                   # 自动填密后等待主界面/验证页
    login_remind_interval_s: int = 300          # login_required 期间重发提醒事件间隔
    qr_max_wait_s: int = 1800
    qq_quick_login_wait_s: int = 20
    qq_reconnect_grace_s: int = 60
    bind_retry_max: int = 12                    # 05 §2.4.2.1 第 4 步:微信 bind 重试上限(07 §[accounts] 登记为配置项)


H13_INTERVAL_S = 60                             # 04 §2.9 字面:Agent 每 60 s GET /wa/v1/time(不是配置项)
H13_DRIFT_THRESHOLD_MS = 2000                   # 04 §2.9 字面:|Δ| > 2 s 判漂移
H05_BOOT_POLL_S = 2                             # 04 H05 字面:起动期每 2 s 查 sys.boot_completed


@dataclass(frozen=True)
class AgentConfig:
    bus: BusConfig = field(default_factory=BusConfig)
    qidian: QidianAdapterConfig = field(default_factory=QidianAdapterConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    db: DbConfig = field(default_factory=DbConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    winagent: WinAgentConfig = field(default_factory=WinAgentConfig)
    wechat: WechatConfig = field(default_factory=WechatConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    qq: QQAdapterConfig = field(default_factory=QQAdapterConfig)                 # [adapters.qq]
    wechat_adapter: WechatAdapterConfig = field(default_factory=WechatAdapterConfig)   # [adapters.wechat]
    media: MediaConfig = field(default_factory=MediaConfig)                      # [media]
    net: NetConfig = field(default_factory=NetConfig)                            # [net]
    retention: RetentionConfig = field(default_factory=RetentionConfig)          # [retention](owner=maintenance)
    backup: BackupConfig = field(default_factory=BackupConfig)                   # [db] backup_*
    webhook: WebhookConfig = field(default_factory=WebhookConfig)                # [events] webhook_*
    hmac: HmacConfig = field(default_factory=HmacConfig)                         # [api] hmac_clock_skew_s / nonce_ttl_s
    calib: CalibrationConfig = field(default_factory=CalibrationConfig)          # [pool] calibration_*
    mail: MailConfig = field(default_factory=MailConfig)                         # [mail].*(owner=06 §7)
    jobs: JobsConfig = field(default_factory=JobsConfig)                         # [jobs]
    monitor: MonitorConfig = field(default_factory=MonitorConfig)                # [monitor](owner=04 §7)
    device_profiles: DeviceProfilesConfig = field(default_factory=DeviceProfilesConfig)   # [device_profiles](owner=05 §7)
    probe: ProbeConfig = field(default_factory=ProbeConfig)                      # [probe](owner=04 §7)

    @classmethod
    def from_toml_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        """从 tomllib.load() 的字典构造;缺省键取默认值(00 §5:所有配置项有默认值,缺省即可跑)。"""
        def pick(section: dict[str, Any] | None, klass):
            section = section or {}
            names = klass.__dataclass_fields__.keys()
            vals = {k: section[k] for k in names if k in section}
            for tup_key in ("unauth_health_sources", "container_restart_backoff_s", "webhook_backoff_ms", "public_ip_probe_urls"):
                if tup_key in vals and isinstance(vals[tup_key], list):
                    vals[tup_key] = tuple(vals[tup_key])
            return klass(**vals)
        adapters = d.get("adapters") or {}
        return cls(
            bus=pick(d.get("bus"), BusConfig),
            qidian=pick(adapters.get("qidian"), QidianAdapterConfig),
            messages=pick(d.get("messages"), MessagesConfig),
            health=pick(d.get("health"), HealthConfig),
            api=pick(d.get("api"), ApiConfig),
            events=pick(d.get("events"), EventsConfig),
            db=pick(d.get("db"), DbConfig),
            runtime=pick(d.get("runtime"), RuntimeConfig),
            pool=pick(d.get("pool"), PoolConfig),
            winagent=pick(d.get("winagent"), WinAgentConfig),
            wechat=pick(d.get("wechat"), WechatConfig),
            accounts=pick(d.get("accounts"), AccountsConfig),
            qq=pick(adapters.get("qq"), QQAdapterConfig),
            wechat_adapter=pick(adapters.get("wechat"), WechatAdapterConfig),
            media=pick(d.get("media"), MediaConfig),
            net=pick(d.get("net"), NetConfig),
            retention=pick(d.get("retention"), RetentionConfig),
            backup=pick(d.get("db"), BackupConfig),
            webhook=pick(d.get("events"), WebhookConfig),
            hmac=pick(d.get("api"), HmacConfig),
            calib=pick(d.get("pool"), CalibrationConfig),
            mail=MailConfig.from_toml_dict({**(d.get("mail") or {}), "retention": d.get("retention")}),
            jobs=pick(d.get("jobs"), JobsConfig),
            monitor=pick(d.get("monitor"), MonitorConfig),
            device_profiles=pick(d.get("device_profiles"), DeviceProfilesConfig),
            probe=pick(d.get("probe"), ProbeConfig),
        )

    def quota_mb(self, channel: str) -> int:
        """建表初始配额(真值在 resource_pools.quota_json,C-43;这里只供首次建行)。"""
        return {"qidian": self.pool.quota_qidian_mb, "qq": self.pool.quota_qq_mb, "wechat": self.pool.quota_wechat_mb}[channel]

    def confirm_timeout_ms(self, channel: str) -> int:
        return {
            "qq": self.bus.confirm_timeout_qq_ms,
            "qidian": self.bus.confirm_timeout_qidian_ms,
            "wechat": self.bus.confirm_timeout_wechat_ms,
        }[channel]
