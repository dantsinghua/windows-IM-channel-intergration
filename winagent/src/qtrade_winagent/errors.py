"""错误信封与错误码(00 §10;混合端点的 ``stage``/``partial`` 见 02 §2.4.1)。

- 统一响应体:``{ok:false, code, error:{message, reason?, retryable, needs_human}, trace_id}``(00 §10)。
- **混合执行端点**(#19/#25/#26/#27/#46/#37,执行体 ``svc+user``)失败时 ``error`` 另带
  ``{code, stage:"svc"|"user", partial:[…已完成的半]}``(02 §2.4.1「两半的时序与失败合并」)——
  让调用方知道停在哪一步、是否需要回滚;``error.code`` 与顶层 ``code`` 恒同值。
- HTTP 状态映射逐字照 00 §10。
"""
from __future__ import annotations

from typing import Any, Optional

# 00 §10 + 02 §3.6:WinAgent 侧用到的码(值域与 Agent 侧同一张表)
UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"
INVALID_ARGS = "INVALID_ARGS"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
RATE_LIMITED = "RATE_LIMITED"
NOT_READY = "NOT_READY"
DISK_FULL = "DISK_FULL"
INTERNAL = "INTERNAL"
UPGRADE_REQUIRED = "UPGRADE_REQUIRED"
TIMEOUT = "TIMEOUT"                      # 02 §2.4.1:会话代理到 deadline_ms 主动返回
VERSION_MISMATCH = "VERSION_MISMATCH"    # 02 §2.4.1:HELLO 主版本不一致
BUSY = "BUSY"                            # 02 §2.4.1:非持有者会话代理

HTTP_BY_CODE = {
    INVALID_ARGS: 400, UNAUTHORIZED: 401, FORBIDDEN: 403, TARGET_NOT_FOUND: 404,
    RESOURCE_EXHAUSTED: 409, RATE_LIMITED: 429, DISK_FULL: 507, NOT_READY: 503,
    UPGRADE_REQUIRED: 426, TIMEOUT: 504, VERSION_MISMATCH: 409, BUSY: 409, INTERNAL: 500,
}

# 02 §2.4.1「会话代理不在线」:逐字的中文 message
USER_AGENT_OFFLINE_MESSAGE = "用户会话代理未运行(用户未登录或代理被结束)"


class WaError(Exception):
    """转成 00 §10 错误信封的异常。``stage``/``partial`` 只在混合端点用(02 §2.4.1)。"""

    def __init__(self, code: str, message: str, *, reason: str = "", retryable: bool = False, needs_human: bool = False,
                 http_status: Optional[int] = None, stage: Optional[str] = None, partial: Optional[list[str]] = None,
                 extra: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.retryable = retryable
        self.needs_human = needs_human
        self.http_status = http_status if http_status is not None else HTTP_BY_CODE.get(code, 500)
        self.stage = stage
        self.partial = list(partial or [])
        self.extra = dict(extra or {})

    def body(self, trace_id: Optional[str] = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message, "reason": self.reason,
                               "retryable": self.retryable, "needs_human": self.needs_human}
        if self.stage is not None:                      # 02 §2.4.1:只有混合端点才带
            err["stage"] = self.stage
            err["partial"] = self.partial
        err.update(self.extra)
        return {"ok": False, "code": self.code, "error": err, "trace_id": trace_id}


def user_agent_offline() -> WaError:
    """02 §2.4.1 / §3.6「执行体 = user」:会话代理不在线一律 ``503 NOT_READY``,message 逐字。"""
    return WaError(NOT_READY, USER_AGENT_OFFLINE_MESSAGE, reason="user_agent_offline", retryable=True)
