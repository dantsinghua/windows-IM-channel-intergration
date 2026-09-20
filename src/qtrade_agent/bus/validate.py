"""bus 参数校验段 —— 规格:docs/02 §3.10(schema 校验 + R6-48 内容校验)、06 §2.12(开放项 (r) 拍板 A)。

``send_text.text`` 必须满足 ``clean_text(text) == text``(不含 U+0014、不含 \\t\\n\\r 以外的 < U+0020 字符;
\\t\\n\\r、全角、Unicode emoji 放行),三通道同一条;违例 ``400 INVALID_ARGS``、``error.reason='text_has_control_chars'``、
``error.details[0].pointer='/text'``,发生在写 SENDING 行之前——不进 commands、不占幂等键。
"""
from __future__ import annotations

from typing import Any, Optional

from ..models import CommandError
from ..text import has_control_chars

TEXT_HAS_CONTROL_CHARS = "text_has_control_chars"


def validate_send_text(args: dict[str, Any]) -> Optional[CommandError]:
    """返回 None = 通过;否则返回可直接放进 CommandResult.error 的 INVALID_ARGS 错误。"""
    if "session" not in args or not isinstance(args.get("session"), str) or not args["session"]:
        return CommandError("参数错误:缺少 session", reason="missing_session", details=[{"pointer": "/session"}])
    text = args.get("text")
    if not isinstance(text, str) or text == "":
        return CommandError("参数错误:text 必须是非空字符串", reason="missing_text", details=[{"pointer": "/text"}])
    if has_control_chars(text):
        return CommandError(
            "正文含不可见控制字符(表情转义/控制符),请删除后重发",
            reason=TEXT_HAS_CONTROL_CHARS,
            retryable=False,
            details=[{"pointer": "/text"}],
        )
    return None


VALIDATORS = {
    "send_text": validate_send_text,
}


def validate_args(op: str, args: dict[str, Any]) -> Optional[CommandError]:
    fn = VALIDATORS.get(op)
    return fn(args) if fn else None
