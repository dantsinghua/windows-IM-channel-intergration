"""QQ 适配器的包内配置 —— 规格唯一出处:docs/02 §7.1 ``[adapters.qq]``(镜像对账表 docs/07 第 54 行)。

🔴 **为什么不进 ``config.py``**:本批只许动 ``adapters/qq/**``,``AgentConfig`` 由总控接线时补一个
``qq: QQAdapterConfig`` 字段(建议片段见 ``.omc/handoffs/qq-adapter.md``)。这里的默认值与 02 §7.1 逐字相同,
接线后把本 dataclass 原样挪进 ``config.py`` 即可,值不用改。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QQAdapterConfig:
    """``[adapters.qq]``:``heartbeat_timeout_s/reconnect_delay_s=40/3`` · ``history_backfill_on_reconnect=50``(02 §7.1 / 07)。"""

    heartbeat_timeout_s: int = 40
    """传输层自判:超过这么久没收到任何 OneBot 帧(含 ``meta_event.heartbeat``,NapCat 默认 5 s 一次)⇒ 客户端主动断开重连。

    ⚠️ 与 04 §2.3 H08 的「30 s 无心跳」不是同一个量:H08 是**健康项阈值**(``[health] napcat_heartbeat_timeout_s=30``,owner=04),
    判的是要不要告警;本键是**传输层重连触发点**。两值不同是规格原样,见 handoff「规格张力 ①」。
    """

    reconnect_delay_s: int = 3
    """断线重连的基础退避秒数(02 §7.1)。退避形态规格未写死,实现按 ``delay = reconnect_delay_s × min(2^(n−1), RECONNECT_BACKOFF_MAX_FACTOR)``,
    见 handoff「规格张力 ②」。"""

    history_backfill_on_reconnect: int = 50
    """P-13(02 §2.8.1 QQ 行):掉线重连后调 ``get_group_msg_history``/``get_friend_msg_history`` 补 ``cursors`` 之后的,默认 50 条。
    ``0`` = 关闭补拉。"""


# ---- 下面是「文档里是字面量、不是配置项」的常量(与 config.py 里 WS_PING_INTERVAL_S/H13_INTERVAL_S 同款处理)
H08_INTERVAL_S = 15
"""04 §2.3 H08 行字面:napcat WS 心跳每 15 s 探一次。"""

H08_OFFLINE_TO_LOGIN_REQUIRED_S = 120
"""04 §2.3 H08 / F-08 字面:``get_status.online=false`` 持续 2 min ⇒ 账号转 ``login_required`` 并推事件(**不自动重登**,D-2)。"""

RECONNECT_BACKOFF_MAX_FACTOR = 10
"""退避上限倍数(规格未写死;3 s → 6 → 12 → 24 → 30(封顶))。见 handoff「规格张力 ②」。"""

ONEBOT_ACTION_TIMEOUT_S = 10
"""单个 ``call_action`` 的等待上限(规格未写死;比 ``[bus] confirm_timeout_qq_ms=5000`` 宽,免得 action 先超时把确认窗吃掉)。
见 handoff「规格张力 ③」。"""

GET_STATE_CACHE_MS = 2000
"""02 §2.2.3 内部状态字面:「每账号一个 ``AdapterSession``(连接/hook 句柄/最近 ``get_state`` 缓存 ≤2s)」。"""
