"""mail 包(M5 邮件摆渡)—— 规格:docs/06 §2.1~§2.8、§2.14、§2.15、§3.1、§3.2、§5、§6、§7。

三件套:
- ``fetcher``:取信(IMAP 优先、POP3 自动回落 E-1),SIZE 门在循环里(R5-6),水位/去重见 ``mail_store``。
- ``ingest``:指令邮件解析 → 三道闸(白名单/验签/allow_ops)→ 去重四层 → 高危 202 待确认 → 进总线 → 回执。
- ``sender``:SMTP 队列消费(退避/死信/限速)+ ``templates`` 渲染 + ``headers`` 头消毒(基线 §11.17 ⑥ [HDRSAN])。
- ``cleanup``:归档 → 三种触发(保留期/容量水位/按条数)删远端,五处 ``NEVER_DELETE`` 门(R6-1/R6-26)。

IMAP/POP3/SMTP 全部经 ``backends`` 的可注入协议;``FakeImap``/``FakePop3``/``FakeSmtp`` 供测试,
``ImapLibBackend``/``PopLibBackend``/``SmtpLibBackend`` 是标准库真实现(测试绝不实例化、绝不连网)。
"""
from __future__ import annotations

from .codes import MAIL_ALERT_CODES, NEVER_DELETE, TERMINAL
from .config import MailConfig, MailInboundConfig, MailOutboundConfig, MailCleanupConfig
from .routes import MailRoute, RouteTable, mailbox_key

__all__ = [
    "MAIL_ALERT_CODES", "NEVER_DELETE", "TERMINAL",
    "MailConfig", "MailInboundConfig", "MailOutboundConfig", "MailCleanupConfig",
    "MailRoute", "RouteTable", "mailbox_key",
]
