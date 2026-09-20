"""鉴权与授权(02 §2.2.1 / §3.1 api_clients / 00 §10)。

- ``Authorization: Bearer <token>`` → ``api_clients``(``sha256(token) == secret_hash``、``enabled=1``、未吊销);WS 可用 ``?token=``。
- 端点级别 R/W/A:``level`` read < write < admin;不够 → ``403 FORBIDDEN``;无/错令牌 → ``401 UNAUTHORIZED``。
- ``allow_accounts_json`` 收窄账号访问(``["*"]`` = 不限)。
- ``/system/health`` 对 ``unauth_health_sources``(loopback / WSL 网关)免鉴权、只回布尔摘要(C-33)。
- HMAC(公网入站,02 §3.5)本期未接。
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any, Optional

LEVEL_RANK = {"read": 0, "write": 1, "admin": 2}


class ApiError(Exception):
    """转成 00 §10 错误信封的异常。"""

    def __init__(self, http_status: int, code: str, message: str, *, reason: str = "", retryable: bool = False,
                 needs_human: bool = False, extra: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.reason = reason
        self.retryable = retryable
        self.needs_human = needs_human
        self.extra = extra or {}


@dataclass
class Principal:
    app_id: str
    level: str
    allow_accounts: list[str]
    transport: str = "http"

    @property
    def actor(self) -> str:
        return "token:console" if self.app_id == "console" else f"app:{self.app_id}"

    def can(self, required: str) -> bool:
        return LEVEL_RANK[self.level] >= LEVEL_RANK[required]

    def allows_account(self, account_id: str) -> bool:
        return "*" in self.allow_accounts or account_id in self.allow_accounts


def principal_from_row(row: dict[str, Any], *, transport: str) -> Principal:
    try:
        allow = json.loads(row.get("allow_accounts_json") or '["*"]')
    except ValueError:
        allow = ["*"]
    return Principal(app_id=row["app_id"], level=row["level"], allow_accounts=list(allow), transport=transport)


def is_unauth_health_source(host: Optional[str], sources: tuple[str, ...], wsl_gateway: Optional[str] = None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    for s in sources:
        if s == "wsl_gateway":
            if wsl_gateway and host == wsl_gateway:
                return True
            continue
        try:
            if ip in ipaddress.ip_network(s, strict=False):
                return True
        except ValueError:
            continue
    return False


def require_level(p: Principal, required: str) -> None:
    if not p.can(required):
        raise ApiError(403, "FORBIDDEN", f"当前令牌级别 {p.level} 无权调用(需 {required})", reason="level_insufficient")


def require_account(p: Principal, account_id: str) -> None:
    if not p.allows_account(account_id):
        raise ApiError(403, "FORBIDDEN", f"当前令牌无权访问账号 {account_id}", reason="account_not_allowed")
