"""选中微信群是否发 IMAP，以及按锁定的 ibquote-163-v1 渲染。

不发真实邮件。缩略图整封跳过。字段名和顺序用现有模板，不另写一套。
"""
from __future__ import annotations

from typing import Any

from ..mail.config import PROFILE_IBQUOTE, OutboundTemplateConfig
from ..mail.templates import render_message_mail
from .bind import deposit_allows


def _field(obj: Any, key: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj[key] if key in obj else default
    try:
        return getattr(obj, key)
    except AttributeError:
        return default


def should_send_imap(binding: Any, message: Any, image_kind: str) -> bool:
    """transport 为 imap 时才发信。公网上报与邮件中转不能同时开。缩略图整封跳过。"""
    if _field(binding, "transport") != "imap":
        return False
    if image_kind in ("缩略图", "表情", "表情包"):
        return False
    return deposit_allows(binding, message) is True


def render_selected_group(message: Any, image_kind: str = "") -> Any:
    """用 ibquote-163-v1 渲染。信息段标题是「微信群聊消息通知」。"""
    session = _field(message, "session", None)
    ctx = {
        "account_id": _field(message, "account_uid"),
        "channel": _field(message, "channel"),
        "session_id": _field(session, "native_id") if session is not None else "",
        "session_name": _field(session, "name") if session is not None else "",
        "sender_kind": _field(session, "kind") if session is not None else "",
        "dir": _field(message, "dir"),
        "message_id": _field(message, "event_id"),
        "sender_name": _field(message, "account_nick"),
        "ts": _field(message, "ts"),
        "ext_msg_id": _field(message, "ext_msg_id"),
        "text": _field(message, "text"),
        "image_kind": image_kind or "",
    }
    # body_fields 留空，渲染走该 profile 的锁定字段表。
    template = OutboundTemplateConfig(compat_profile=PROFILE_IBQUOTE, info_title="微信群聊消息通知")
    return render_message_mail(template, ctx)
