"""QQ 通道适配器(M2.5;docs/02 §2.2.3 ``qq`` 行 + docs/06 §2.9.x QQ 各条 + docs/04 §2.3 H08)。

开发容器里**一律注入 ``FakeOneBot``**,绝不连真实 QQ / NapCat(``.claude/skills/qtrade-redroid-resume/SKILL.md`` §4 禁区)。
总控接线(``app.py`` 装配、``config.py`` ``[adapters.qq]`` 段、05 §2.3 QQ start 序列挂钩、H08 注册)见 ``.omc/handoffs/qq-adapter.md``。
"""
from .adapter import CAPABILITIES, QQAdapter, QQSession, seq_of
from .config import (GET_STATE_CACHE_MS, H08_INTERVAL_S, H08_OFFLINE_TO_LOGIN_REQUIRED_S, ONEBOT_ACTION_TIMEOUT_S,
                     RECONNECT_BACKOFF_MAX_FACTOR, QQAdapterConfig)
from .health import H08_LOGIN_REQUIRED_STATE_CODE, H08_NAPCAT_HEARTBEAT_LOST, H08State, QQHealth, status_url
from .normalize import ext_msg_id, native_id_of, split_session, text_type_media, to_message
from .onebot import FakeOneBot, OneBotClient, OneBotClosed, OneBotError, OneBotTransport, WebsocketsTransport, http_url, ws_url

__all__ = [
    "CAPABILITIES", "QQAdapter", "QQAdapterConfig", "QQSession", "seq_of",
    "GET_STATE_CACHE_MS", "H08_INTERVAL_S", "H08_OFFLINE_TO_LOGIN_REQUIRED_S", "ONEBOT_ACTION_TIMEOUT_S",
    "RECONNECT_BACKOFF_MAX_FACTOR",
    "H08_LOGIN_REQUIRED_STATE_CODE", "H08_NAPCAT_HEARTBEAT_LOST", "H08State", "QQHealth", "status_url",
    "ext_msg_id", "native_id_of", "split_session", "text_type_media", "to_message",
    "FakeOneBot", "OneBotClient", "OneBotClosed", "OneBotError", "OneBotTransport", "WebsocketsTransport",
    "http_url", "ws_url",
]
