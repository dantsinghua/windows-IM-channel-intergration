"""agent.toml 的默认值 —— 规格唯一出处:docs/02 §7.1(镜像对账表 docs/07)。

只登记本期骨架消费的键;新增键先进 02 §7.1 再加到这里。值必须与 02 §7.1 逐字相同
(docs/check-truth-tables.py ⑩ 只查文档之间,代码这份靠 tests/test_config_matches_docs.py 对账)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


@dataclass(frozen=True)
class MessagesConfig:
    late_after_s: int = 120                     # R6-39:payload.late = lag_s > late_after_s
    capture_text: bool = True
    raw_payload: bool = False


@dataclass(frozen=True)
class HealthConfig:
    adb_root_grace_s: int = 15                  # ensure_root 宽限窗(04 owner;06 §2.9.5 引用)


@dataclass(frozen=True)
class ApiConfig:
    bind: str = "0.0.0.0"
    port: int = 17600                           # 00 §3
    ws_impl: str = "websockets"                 # uvicorn ws=,不许 auto(02 §2.2)
    rate_default_per_min: int = 120
    http_sync_max_wait_ms: int = 25000          # 同步等待上限(P-10),超过转 202
    unauth_health_sources: tuple[str, ...] = ("127.0.0.1/32", "::1/128", "wsl_gateway")
    api_version: str = "1.0"                    # 只读,随代码(02 §3.8)


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

    @classmethod
    def from_toml_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        """从 tomllib.load() 的字典构造;缺省键取默认值(00 §5:所有配置项有默认值,缺省即可跑)。"""
        def pick(section: dict[str, Any] | None, klass):
            section = section or {}
            names = klass.__dataclass_fields__.keys()
            vals = {k: section[k] for k in names if k in section}
            if "unauth_health_sources" in vals and isinstance(vals["unauth_health_sources"], list):
                vals["unauth_health_sources"] = tuple(vals["unauth_health_sources"])
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
