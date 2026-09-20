"""QQ 适配器的包内配置 —— 规格唯一出处:docs/02 §7.1 ``[adapters.qq]``(镜像对账表 docs/07 第 54 行)。

``QQAdapterConfig`` 已由总控并进 ``qtrade_agent.config``(``AgentConfig.qq``),本模块只做**再导出**,
包内引用不变;下面的常量是「文档里是字面量、不是配置项」的那批,留在本包里。
"""
from __future__ import annotations

from ...config import QQAdapterConfig      # noqa: F401  (再导出:02 §7.1 [adapters.qq])

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
