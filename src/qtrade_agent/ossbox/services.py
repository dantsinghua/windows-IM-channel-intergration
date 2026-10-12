"""采集终端对内网的标准服务。

公网上报和邮件中转必须解成同一种调用：渠道、账号、对方或群、单发或群发、
通讯录、原图、接收范围、序号对账。缩略图和表情包两边都不形成调用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

OPS = frozenset({
    "send_one",
    "send_many",
    "recv_message",
    "list_contacts",
    "fetch_original_image",
    "set_scope",
    "reconcile",
})
_CHANNELS = frozenset({"wechat", "qidian", "qq"})
_SKIP_IMAGE = frozenset({"缩略图", "表情", "表情包"})
_RESEND_LIMIT = 3


@dataclass(frozen=True)
class ServiceCall:
    op: str
    channel: str
    account_uid: str
    kind: str
    native_id: str
    targets: tuple[str, ...]
    text: str
    seq: int | None
    seq_total: int | None
    ext_msg_id: str
    image_kind: str
    scope: tuple[str, ...]

    def effect(self) -> tuple[Any, ...]:
        """两种通道比的就是这个元组。相同则最终效果相同。"""
        return (
            self.op, self.channel, self.account_uid, self.kind, self.native_id,
            self.targets, self.text, self.seq, self.seq_total, self.ext_msg_id,
            self.image_kind, self.scope,
        )


def _seq_pair(seq: Any, seq_total: Any) -> tuple[int | None, int | None]:
    if isinstance(seq, str) and "/" in seq:
        left, right = seq.split("/", 1)
        right = re.sub(r"（本轮）|\(本轮\)", "", right)
        return _as_int(left), _as_int(right)
    return _as_int(seq), _as_int(seq_total)


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    number = int(digits)
    return number or None


def _targets(value: Any, native_id: str, op: str) -> tuple[str, ...]:
    if op == "send_many":
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("群发必须给出 targets")
        return tuple(str(item) for item in value if str(item) not in ("", "None"))
    if native_id:
        return (native_id,)
    return ()


def _call(
    *,
    op: str,
    channel: str,
    account_uid: str,
    kind: str = "",
    native_id: str = "",
    targets: Any = None,
    text: str = "",
    seq: Any = None,
    seq_total: Any = None,
    ext_msg_id: str = "",
    image_kind: str = "",
    scope: Any = None,
) -> ServiceCall:
    if op == "send_text":
        op = "send_one"
    if op not in OPS:
        raise ValueError(f"未知服务 {op}")
    if channel and channel not in _CHANNELS:
        raise ValueError("channel 只能是 wechat、qidian、qq")
    if op == "fetch_original_image" and image_kind != "原图":
        raise ValueError("原图服务只接受原图")
    seq_n, total_n = _seq_pair(seq, seq_total)
    scope_ids = tuple(str(item) for item in (scope or ()) if str(item) not in ("", "None"))
    if op == "set_scope" and not scope_ids:
        raise ValueError("接收范围不能为空")
    return ServiceCall(
        op=op,
        channel=channel,
        account_uid=str(account_uid or ""),
        kind=kind,
        native_id=str(native_id or ""),
        targets=_targets(targets, str(native_id or ""), op),
        text=text or "",
        seq=seq_n,
        seq_total=total_n,
        ext_msg_id=str(ext_msg_id or ""),
        image_kind=image_kind or "",
        scope=scope_ids,
    )


def from_oss(data: dict[str, Any]) -> ServiceCall | None:
    """公网上报明文。``send_text`` 与单发相同。缩略图和表情包不形成调用。"""
    if not isinstance(data, dict):
        raise ValueError("公网上报必须是映射")
    image_kind = str(data.get("image_kind") or "")
    if image_kind in _SKIP_IMAGE:
        return None
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    return _call(
        op=str(data.get("op") or "recv_message"),
        channel=str(data.get("channel") or ""),
        account_uid=str(data.get("account_uid") or ""),
        kind=str(session.get("kind") or data.get("kind") or ""),
        native_id=str(session.get("native_id") or data.get("native_id") or ""),
        targets=data.get("targets"),
        text=str(data.get("text") or ""),
        seq=data.get("seq"),
        seq_total=data.get("seq_total"),
        ext_msg_id=str(data.get("ext_msg_id") or ""),
        image_kind=image_kind,
        scope=data.get("scope"),
    )


def from_mail(fields: dict[str, Any]) -> ServiceCall | None:
    """邮件中转字段。消息序号 ``2/5`` 与公网上报的 seq、seq_total 同一含义。"""
    if not isinstance(fields, dict):
        raise ValueError("邮件字段必须是映射")
    image_kind = str(fields.get("图片类型") or fields.get("image_kind") or "")
    if image_kind in _SKIP_IMAGE:
        return None
    seq_text = fields.get("消息序号") or fields.get("seq")
    return _call(
        op=str(fields.get("op") or "recv_message"),
        channel=str(fields.get("channel") or "wechat"),
        account_uid=str(fields.get("account_uid") or fields.get("账号") or ""),
        kind="group" if (fields.get("群聊") or fields.get("kind") == "group") else str(fields.get("kind") or "group"),
        native_id=str(fields.get("native_id") or fields.get("群聊") or ""),
        targets=fields.get("targets"),
        text=str(fields.get("文本消息内容") or fields.get("text") or ""),
        seq=seq_text,
        seq_total=fields.get("seq_total"),
        ext_msg_id=str(fields.get("消息ID") or fields.get("ext_msg_id") or ""),
        image_kind=image_kind,
        scope=fields.get("scope"),
    )


def equivalent(left: ServiceCall | None, right: ServiceCall | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.effect() == right.effect()


def reconcile(seqs: list[int], seq_total: int, *, resend_limit: int = _RESEND_LIMIT) -> dict[str, Any]:
    """序号对账。缺号不超过上限则要求采集终端补发，超过则记告警。"""
    if type(seq_total) is not int or seq_total < 1:
        raise ValueError("seq_total 必须是正整数")
    got = {int(item) for item in seqs}
    gaps = [n for n in range(1, seq_total + 1) if n not in got]
    if not gaps:
        return {"ok": True, "gaps": [], "action": "ok"}
    action = "resend" if len(gaps) <= resend_limit else "alert"
    return {"ok": False, "gaps": gaps, "action": action}
