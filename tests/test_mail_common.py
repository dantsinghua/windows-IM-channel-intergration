"""mail 测试的公共夹具(本文件不含断言,只提供构造器)—— 规格:docs/06 §2.3(指令邮件)、§2.15(路由)、§7(配置默认值)。

开发容器里一律假后端:``FakeImap``/``FakePop3``/``FakeSmtp``,**绝不连任何真实邮箱**。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Optional

from qtrade_agent.alerts import Alerts
from qtrade_agent.events import Events
from qtrade_agent.mail.backends import FakeImap, FakePop3, FakeSmtp
from qtrade_agent.mail.catalog import Capability, Catalog
from qtrade_agent.mail.config import MailConfig, MailHmacKey
from qtrade_agent.mail.service import MailService
from qtrade_agent.mail.sign import command_canonical, sign_header

SECRET = "s3cr3t-ops"
OPS_ADDR = "ops@corp.example"
BOX_ADDR = "bot@corp.example"
SHORT = "ops"
#: conftest.Clock 的起点(1_758_240_000_000 ms)对应的北京时间——指令邮件默认时间戳与它同刻,免得撞 sig_time_tolerance_s
CLOCK_START_ISO = "2025-09-19T08:00:00+08:00"


def build_catalog() -> Catalog:
    """真目录五项(全 ``danger=false``)+ 02 §3.10 权威十项里的两个 ``danger=true`` 项(邮件侧不复写清单,只消费属性)。"""
    cat = Catalog.load()
    cat.caps["account_stop"] = Capability(
        op="account_stop", kind="admin", danger=True, confirmable=False,
        channels={"qidian": "supported", "qq": "supported", "wechat": "supported"},
        args_schema={"type": "object", "additionalProperties": False,
                     "properties": {"graceful": {"type": "boolean"}}})
    cat.caps["messages_purge"] = Capability(
        op="messages_purge", kind="admin", danger=True, confirmable=False,
        channels={"qidian": "supported"},
        args_schema={"type": "object", "additionalProperties": False,
                     "properties": {"before": {"type": "string"}, "mode": {"type": "string"}}})
    return cat


def make_cfg(*, protocol: str = "imap", allowed: Optional[list[str]] = None,
             allow_ops: Optional[list[str]] = None, require_signature: bool = True,
             archive_dir: str = "/tmp/qtrade-mail-archive", **inbound_kw: Any) -> MailConfig:
    cfg = MailConfig()
    cfg.enabled = True
    cfg.inbound.protocol = protocol
    cfg.inbound.host = "imap.example.com" if protocol == "imap" else "pop.example.com"
    cfg.inbound.user = BOX_ADDR
    cfg.inbound.allowed_senders = list(allowed if allowed is not None else [OPS_ADDR])
    cfg.inbound.require_signature = require_signature
    cfg.inbound.allow_ops = list(allow_ops if allow_ops is not None else ["*"])
    cfg.inbound.hmac = {SHORT: MailHmacKey(short_name=SHORT, from_addr=OPS_ADDR,
                                           secret_ref="vault://mail/hmac/cmd/ops")}
    cfg.inbound.fallback.host = "pop.example.com"
    for k, v in inbound_kw.items():
        setattr(cfg.inbound, k, v)
    cfg.outbound.host = "smtp.example.com"
    cfg.outbound.user = BOX_ADDR
    cfg.outbound.from_addr = BOX_ADDR
    cfg.outbound.recipients = ["ops-inbox@corp.example"]
    cfg.cleanup.archive_dir = archive_dir
    return cfg


@dataclass
class MailEnv:
    service: MailService
    imap: FakeImap
    pop3: FakePop3
    smtp: FakeSmtp
    cfg: MailConfig
    alerts: Alerts
    events: Events

    @property
    def ms(self):
        return self.service.ms

    @property
    def mailbox(self) -> str:
        return next(iter(self.service.fetchers))

    @property
    def fetcher(self):
        return self.service.fetchers[self.mailbox]

    @property
    def cleaner(self):
        return self.service.cleaners[self.mailbox]

    def inbox_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.ms.con.execute("SELECT * FROM mail_inbox ORDER BY id").fetchall()]

    def outbox_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.ms.con.execute("SELECT * FROM mail_outbox ORDER BY id").fetchall()]

    def alert_codes(self) -> list[str]:
        return [e["payload_json"] for e in self.ms._store.list_events(event="alert")]


def make_env(store, clock, *, cfg: Optional[MailConfig] = None, tmp_path=None,
             events: Optional[Events] = None, alerts: Optional[Alerts] = None) -> MailEnv:
    cfg = cfg or make_cfg(archive_dir=str(tmp_path / "archive") if tmp_path else "/tmp/qtrade-mail-archive")
    events = events or Events(store)
    alerts = alerts or Alerts(events, clock=clock)
    imap, pop3, smtp = FakeImap(), FakePop3(), FakeSmtp()
    svc = MailService(store, cfg, catalog=build_catalog(), clock=clock, alerts=alerts,
                      secret_of=lambda ref: SECRET,
                      imap_factory=lambda route: imap, pop3_factory=lambda route: pop3,
                      smtp_factory=lambda route: smtp)
    return MailEnv(service=svc, imap=imap, pop3=pop3, smtp=smtp, cfg=cfg, alerts=alerts, events=events)


def command_body(*, req_id: str, account_id: str, op: str, session: str = "", args: Optional[dict] = None,
                 timestamp: str = CLOCK_START_ISO, nonce: str = "8f1c0e2a6b4d",
                 secret: Optional[str] = SECRET, channel: Optional[str] = None,
                 confirm: Optional[str] = None, timeout: Optional[int] = None,
                 attachments: Optional[list[dict]] = None, expand_form: bool = False,
                 signature: Optional[str] = None, extra_lines: Optional[list[str]] = None) -> str:
    """§2.3.2 的正文形态(默认 JSON 形式参数;``expand_form=True`` 走「按 op 展开形式」)。"""
    args = dict(args or {})
    # 展开形式下发起方写进邮件的值就是字符串,签名也按字符串算——两种形式合并成同一对象后规范化结果才相同(§2.3.4)
    sign_args = {k: (str(v) if expand_form else v) for k, v in args.items()}
    if session:
        sign_args["session"] = session
    lines = ["QTrade 指令 v1", "", f"指令ID：{req_id}", f"账号：{account_id}"]
    if channel:
        lines.append(f"通道：{channel}")
    lines.append(f"操作：{op}")
    if session:
        lines.append(f"会话：{session}")
    if args:
        if expand_form:
            lines += [f"参数.{k}：{v}" for k, v in args.items()]
        else:
            lines.append("参数：" + json.dumps(args, ensure_ascii=False))
    if confirm is not None:
        lines.append(f"确认：{confirm}")
    if timeout is not None:
        lines.append(f"超时：{timeout}")
    lines += extra_lines or []
    lines.append(f"时间戳：{timestamp}")
    lines.append(f"随机数：{nonce}")
    if signature is not None:
        lines.append(f"签名：{signature}")
    elif secret is not None:
        canonical = command_canonical(req_id=req_id, account_id=account_id, op=op, session=session,
                                      args=sign_args, timestamp=timestamp, nonce=nonce,
                                      attachments=attachments)
        lines.append(f"签名：{sign_header(canonical, secret)}")
    return "\n".join(lines) + "\n"


def make_mail(body: str, *, subject: str = "QTRADE指令 v1 [qd01] send_text 20260918-ops-0007",
              from_addr: str = OPS_ADDR, to_addr: str = BOX_ADDR, message_id: str = "<m1@corp.example>",
              html: bool = False, attachments: Optional[list[dict]] = None,
              date: str = "Fri, 19 Sep 2025 08:00:00 +0800") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = message_id
    msg["Date"] = date
    if html:
        msg.set_content("请用 HTML 客户端查看")
        msg.add_alternative("<html><body>" + "<br>".join(body.splitlines()) + "</body></html>", subtype="html")
    else:
        msg.set_content(body)
    for att in attachments or []:
        msg.add_attachment(att["bytes"], maintype="application", subtype="octet-stream", filename=att["name"])
    return msg.as_bytes()


def command_mail(*, req_id: str = "20260918-ops-0007", account_id: str = "qd01", op: str = "send_text",
                 session: str = "qd01:415011447", args: Optional[dict] = None, **kw) -> bytes:
    """一封完整的、带合法签名的指令邮件。"""
    body_kw = {k: kw.pop(k) for k in list(kw) if k in (
        "timestamp", "nonce", "secret", "channel", "confirm", "timeout", "attachments", "expand_form",
        "signature", "extra_lines")}
    body = command_body(req_id=req_id, account_id=account_id, op=op, session=session,
                        args=args if args is not None else {"text": "今日 3M 报价 1.52,可谈"}, **body_kw)
    kw.setdefault("subject", f"QTRADE指令 v1 [{account_id}] {op} {req_id}")
    return make_mail(body, attachments=body_kw.get("attachments"), **kw)
