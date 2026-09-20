"""日志行格式与脱敏(02 §2.9,G-03;Agent 与 WinAgent **共用同一格式**)。

一行一条、文本键值式(现场排障肉眼看,不用 JSON 行)。字段序固定::

    ts(ISO 带偏移,毫秒) level(5 宽) logger(10 宽) trace=… acct=… [op=… code=… cost_ms=…] msg="…" k=v…

- 没有的字段省略;``trace=`` 只要在指令/事件/邮件上下文里就必带(本模块用 ``contextvars`` 注入,调用方不必手传)。
- WinAgent 同格式,``logger`` **前缀 `wa.`**(如 ``wa.wslctl``);会话代理日志单独文件 ``winagent\\logs\\user-<sid>.log``。
- **脱敏字段清单 = 日志 Filter 与审计序列化器同一张表**(02 §2.9 那张表逐行照抄);命中键名即整值替换。
- **禁止**任何模块自己 ``basicConfig``(ibquote ``all_in_one`` 教训)——装配入口 ``svc``/``user`` 统一配置。
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

LOGGER_PREFIX = "wa."                                   # 02 §2.9:WinAgent 侧 logger 前缀

trace_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("wa_trace", default=None)
acct_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("wa_acct", default=None)

# ---- 02 §2.9 脱敏表(键名大小写不敏感,含嵌套)
CRED_KEYS = frozenset({"secret", "password", "passwd", "pass", "token", "authorization", "x-auth-token",
                       "cookie", "api_key", "hmac", "signature", "secret_ref"})
TEXT_KEYS = frozenset({"text", "body", "body_text", "body_html", "content", "caption", "asr_text"})
B64_KEYS = frozenset({"qrcode_png_b64", "png_base64", "image_b64"})
ADDR_KEYS = frozenset({"from_addr", "to_addrs", "cc", "recipients"})

_USERPROFILE_RE = re.compile(r"(?i)([A-Z]:\\Users\\)([^\\]+)")


def redact_path(value: str) -> str:
    """含用户名的 Windows 路径 → ``%USERPROFILE%`` 替换(02 §2.9 路径行)。"""
    return _USERPROFILE_RE.sub(lambda m: "%USERPROFILE%", value)


def _sha8(value: str) -> str:
    return f"sha8:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}:len{len(value)}"


def _mask_addr(value: str) -> str:
    """保留域名,本地部分打码 ``a***@corp``。"""
    if "@" not in value:
        return "***"
    local, _, domain = value.partition("@")
    return f"{local[:1] or 'a'}***@{domain}"


def redact_value(key: str, value: Any) -> Any:
    k = str(key).lower()
    if k in CRED_KEYS:
        # ``*_ref`` 只显示 ``vault://…`` 引用本身,不显示值(02 §2.9 凭据行括注)
        if k.endswith("_ref") and isinstance(value, str) and value.startswith("vault://"):
            return value
        return "***"
    if k.endswith("_ref") and isinstance(value, str) and value.startswith("vault://"):
        return value
    if k in TEXT_KEYS:
        return _sha8(value if isinstance(value, str) else str(value))
    if k in B64_KEYS:
        n = len(value) if isinstance(value, (str, bytes)) else 0
        return f"<b64:{n}B>"
    if k in ADDR_KEYS:
        if isinstance(value, (list, tuple)):
            return [_mask_addr(str(v)) for v in value]
        return _mask_addr(str(value))
    if isinstance(value, str):
        return redact_path(value)
    return value


_TABLE_KEYS = CRED_KEYS | TEXT_KEYS | B64_KEYS | ADDR_KEYS


def redact(obj: Any) -> Any:
    """递归脱敏(审计序列化器与日志 Filter 共用本函数 —— 02 §2.9「同一张表」)。

    命中脱敏表的键:整值按表处理、**不再递归**(已是替换串);未命中的键继续往下走(含嵌套 dict/list)。
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out[k] = redact_value(k, v) if str(k).lower() in _TABLE_KEYS or str(k).lower().endswith("_ref") else redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_path(obj)
    return obj


def iso8601(ms: int) -> str:
    """ISO 带偏移、毫秒(02 §2.9 第一字段)。"""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}" + dt.strftime("%z")[:3] + ":" + dt.strftime("%z")[3:]


def format_line(*, ts_ms: int, level: str, logger: str, msg: str, trace: Optional[str] = None, acct: Optional[str] = None,
                op: Optional[str] = None, code: Optional[str] = None, cost_ms: Optional[int] = None,
                extra: Optional[dict[str, Any]] = None) -> str:
    """02 §2.9 字段序:``ts level(5) logger(10) trace= acct= [op= code= cost_ms=] msg="…" k=v…``。"""
    parts = [iso8601(ts_ms), f"{level:<5}", f"{logger:<10}"]
    if trace:
        parts.append(f"trace={trace}")
    if acct:
        parts.append(f"acct={acct}")
    if op:
        parts.append(f"op={op}")
    if code:
        parts.append(f"code={code}")
    if cost_ms is not None:
        parts.append(f"cost_ms={cost_ms}")
    parts.append('msg="%s"' % str(msg).replace('"', "'"))
    for k, v in (extra or {}).items():
        parts.append(f"{k}={redact_value(k, v)}")
    return " ".join(parts)


class WaFormatter(logging.Formatter):
    """把 ``logging`` 记录渲染成 02 §2.9 那一行;``trace``/``acct`` 自动从 contextvars 取。"""

    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in getattr(record, "kv", {}).items()}
        line = format_line(ts_ms=int(record.created * 1000), level=record.levelname, logger=record.name,
                           msg=record.getMessage(), trace=getattr(record, "trace", None) or trace_var.get(),
                           acct=getattr(record, "acct", None) or acct_var.get(), op=getattr(record, "op", None),
                           code=getattr(record, "code", None), cost_ms=getattr(record, "cost_ms", None), extra=extra)
        if record.exc_info:                       # 多行堆栈缩进两格跟在该行后(02 §2.9)
            line += "\n" + "\n".join("  " + l for l in self.formatException(record.exc_info).splitlines())
        return line


def get_logger(name: str) -> logging.Logger:
    """``get_logger("wslctl")`` → logger ``wa.wslctl``(02 §2.9 前缀规则,调用方不必自己拼)。"""
    return logging.getLogger(name if name.startswith(LOGGER_PREFIX) else LOGGER_PREFIX + name)
