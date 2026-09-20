"""api 模块(02 §2.2.1):HTTP/WS 入口;请求 → Command/查询;统一错误信封(00 §10);Bearer 鉴权;每次调用记 audit_log。"""
from .app import create_api  # noqa: F401
