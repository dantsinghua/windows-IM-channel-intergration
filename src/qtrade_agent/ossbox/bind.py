"""存款群过滤、询价企点全量，以及两种去重。

红线词表不在工作台。调用方已经判定拦截时，``command_allowed`` 直接拒绝，不再扫正文。
"""
from __future__ import annotations

from typing import Any


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj[key] if key in obj else default
    try:
        return getattr(obj, key)
    except AttributeError:
        return default


def deposit_allows(binding: Any, message: Any) -> bool:
    """绑定微信号上、且群号在列表内的群消息才允许。空列表、私聊、别的号、别的通道都不允许。"""
    if _field(message, "channel") != "wechat":
        return False
    if _field(message, "account_uid") != _field(binding, "wxid"):
        return False
    session = _field(message, "session")
    if session is None or _field(session, "kind") != "group":
        return False
    groups = _field(binding, "group_native_ids")
    if isinstance(groups, (str, bytes)) or not groups:
        return False
    return _field(session, "native_id") in groups


def xunjia_allows(binding: Any, message: Any) -> bool:
    """所绑企点号的全部消息，私聊和群都要。不看有没有债券。"""
    if _field(message, "channel") != "qidian":
        return False
    uid = _field(message, "account_uid")
    if uid in (None, ""):
        return False
    bound = [item for item in (_field(binding, "account_uid"), _field(binding, "self_uid")) if item not in (None, "")]
    return uid in bound


class Dedup:
    """``ext_msg_id`` 与 ``idempotency_key`` 分属两个集合，互不影响。"""

    def __init__(self) -> None:
        self._events: set[Any] = set()
        self._commands: set[Any] = set()

    def seen_event(self, ext_msg_id: str) -> bool:
        """第一次见到该扩展消息号返回 True，之后返回 False。"""
        if ext_msg_id in self._events:
            return False
        self._events.add(ext_msg_id)
        return True

    def seen_command(self, idempotency_key: str) -> bool:
        """第一次见到该幂等键返回 True，之后返回 False。"""
        if idempotency_key in self._commands:
            return False
        self._commands.add(idempotency_key)
        return True


def command_allowed(text: str, blocked: bool) -> bool:
    """调用方已判定拦截则拒绝产生指令。未拦截时不再按正文扫一遍。"""
    del text
    if blocked:
        return False
    return True
