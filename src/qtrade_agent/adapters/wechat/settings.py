"""``agent.toml [adapters.wechat]`` 的默认值 —— 规格唯一出处:02 §7.1(镜像对账表 docs/07 §[adapters.wechat])。

``WechatAdapterConfig`` 已由总控并进 ``qtrade_agent.config``(``AgentConfig.wechat_adapter``),
本模块只做**再导出**,包内引用(``poll.py`` / ``wechat_slot.py`` 的 ``wechat_cfg or WechatAdapterConfig()``)不变。
"""
from __future__ import annotations

from ...config import WechatAdapterConfig      # noqa: F401  (再导出:02 §7.1 [adapters.wechat])

__all__ = ["WechatAdapterConfig"]
