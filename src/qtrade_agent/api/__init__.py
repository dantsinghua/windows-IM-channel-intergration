"""api 模块(02 §2.2.1):HTTP/WS 入口;请求 → Command/查询;统一错误信封(00 §10);Bearer 鉴权;每次调用记 audit_log。

``create_api`` 走 PEP 562 惰性导出:``config`` 要拿 ``hmac_inbound.HmacConfig``、而 ``hmac_inbound`` 要拿
``api.auth``,包级 eager import ``api.app``(它 import ``..config``)会成环。名字仍可 ``from qtrade_agent.api import create_api``。
"""
from typing import Any

__all__ = ["create_api"]


def __getattr__(name: str) -> Any:
    if name == "create_api":
        from .app import create_api
        return create_api
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
