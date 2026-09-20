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
class AgentConfig:
    bus: BusConfig = field(default_factory=BusConfig)
    qidian: QidianAdapterConfig = field(default_factory=QidianAdapterConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    @classmethod
    def from_toml_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        """从 tomllib.load() 的字典构造;缺省键取默认值(00 §5:所有配置项有默认值,缺省即可跑)。"""
        def pick(section: dict[str, Any] | None, klass):
            section = section or {}
            names = klass.__dataclass_fields__.keys()
            return klass(**{k: section[k] for k in names if k in section})
        adapters = d.get("adapters") or {}
        return cls(
            bus=pick(d.get("bus"), BusConfig),
            qidian=pick(adapters.get("qidian"), QidianAdapterConfig),
            messages=pick(d.get("messages"), MessagesConfig),
            health=pick(d.get("health"), HealthConfig),
        )

    def confirm_timeout_ms(self, channel: str) -> int:
        return {
            "qq": self.bus.confirm_timeout_qq_ms,
            "qidian": self.bus.confirm_timeout_qidian_ms,
            "wechat": self.bus.confirm_timeout_wechat_ms,
        }[channel]
