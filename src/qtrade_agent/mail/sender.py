"""出站:模板渲染 → ``mail_outbox`` 入队 → SMTP 投递 —— 规格:docs/06 §2.1(SMTP 发送/重试/限速)、§2.4(信息邮件与回执)、§2.16.2(告警邮件)。

硬口径:
- ``Message-ID`` 由本系统生成 ``<{ulid}@qtrade.local>``;回执设 ``In-Reply-To``/``References`` = 指令邮件的 ``Message-ID``(§2.1)。
- 重试 ``backoff = [30s,1m,2m,5m,10m,20m,30m,60m]``、``max_attempts = 8`` 超过置 ``DEAD`` 并发 ``MAIL_OUTBOX_DEAD``;
  **5xx 永久码(550/552/553)不重试直接 DEAD**;535 认证失败按「环境故障」退避且触发 ``MAIL_AUTH_FAILED``。
- 限速令牌桶 ``send_rate_per_min = 20``、``send_burst = 5``;服务商回「too many」类响应时速率临时减半 10 分钟。
- 队列消费单线程、按 ``next_attempt_ms`` 排序;**一封发送失败不阻塞后面**。
- 🔴 设头只走 ``headers.build_headers()``(基线 §11.17 ⑥ [HDRSAN]),**收件人恒来自路由**,占位符永不影响收件人。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..ids import ulid
from .backends import MailAuthError, SmtpPermanentError, SmtpTemporaryError
from .codes import (MAIL_ALERT_CODES, MAIL_AUTH_FAILED, MAIL_OUTBOX_DEAD, MAIL_ROUTE_UNRESOLVED,
                    MAIL_SMTP_FAILING, RECEIPT_SENT)
from .config import MailConfig
from .headers import build_headers, join_addrs, recipients_of
from .mail_store import MailStore
from .routes import MailRoute, RouteTable
from .templates import RenderedMail, render_alert, render_message_mail, render_receipt

log = logging.getLogger("qtrade.mail.sender")

#: 02 #62 / 06 §2.5 末:这三个状态的行重投 = **复制新行**(原行留痕不动)
RESEND_COPY_STATES = frozenset({"DEAD", "DISCARDED", "SENT"})
RATE_HALVE_MS = 10 * 60 * 1000          # §2.1:服务商回 421/"too many"/"频率" 类响应 ⇒ 速率临时减半 10 分钟


@dataclass
class SendStats:
    sent: int = 0
    retried: int = 0
    dead: int = 0
    skipped_rate: int = 0
    errors: list[str] = field(default_factory=list)


def new_message_id() -> str:
    return f"<{ulid()}@qtrade.local>"


class MailSender:
    """出站队列(每个 SMTP 账户一条消费线程;本类只做一轮 ``run_once()``,调度由 scheduler 给,见 handoff)。"""

    def __init__(self, mail_store: MailStore, routes: RouteTable, cfg: MailConfig, *,
                 smtp_factory: Optional[Callable[[MailRoute], Any]] = None,
                 clock: Optional[Callable[[], int]] = None, alerts: Any = None):
        self.ms = mail_store
        self.routes = routes
        self.cfg = cfg
        self.smtp_factory = smtp_factory
        self.clock = clock or mail_store._now
        self.alerts = alerts
        self._tokens: float = float(cfg.outbound.send_burst)
        self._tokens_ms: int = 0
        self._halved_until_ms: int = 0
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ 入队
    def _route_for(self, channel: Optional[str], account_id: Optional[str]) -> Optional[MailRoute]:
        route = self.routes.lookup(channel, account_id)
        if route is None or not route.outbound.enabled or not route.outbound_to:
            # §2.15.2:路由找不到 → 信息邮件不入队、记 MAIL_ROUTE_UNRESOLVED
            self._alert(MAIL_ROUTE_UNRESOLVED, subject=f"route:{channel or '*'}/{account_id or ''}")
            return None
        return route

    def _alert(self, code: str, *, subject: str, evidence: Optional[dict[str, Any]] = None,
               severity: Optional[str] = None) -> None:
        if self.alerts is not None:
            self.alerts.firing(code, subject=subject, severity=severity or MAIL_ALERT_CODES.get(code, "warn"),
                               evidence=evidence or {})

    def _enqueue(self, *, kind: str, route: MailRoute, rendered: RenderedMail, dedup_key: str,
                 to: list[str], cc: list[str], in_reply_to: Optional[str] = None,
                 ref_inbox_id: Optional[int] = None, ref_message_id: Optional[str] = None,
                 ref_trace_id: Optional[str] = None, attachments: Optional[list[dict[str, Any]]] = None,
                 now_ms: Optional[int] = None) -> Optional[int]:
        return self.ms.outbox_enqueue(
            kind=kind, route_id=route.id, to_addrs=join_addrs(to), cc_addrs=join_addrs(cc),
            subject=rendered.subject, body_text=rendered.body_text, body_html=rendered.body_html,
            attachments_json=json.dumps(attachments or [], ensure_ascii=False),
            rfc_message_id=new_message_id(), in_reply_to=in_reply_to,
            references_hdr=in_reply_to, ref_inbox_id=ref_inbox_id, ref_message_id=ref_message_id,
            ref_trace_id=ref_trace_id, template_version=self.cfg.template_version,
            template_profile=self.cfg.template_out.compat_profile, dedup_key=dedup_key, now_ms=now_ms)

    def enqueue_message(self, ctx: dict[str, Any], *, channel: str, account_id: str,
                        session_id: Optional[str] = None, ref_message_id: Optional[str] = None,
                        dedup_suffix: str = "", attachments: Optional[list[dict[str, Any]]] = None,
                        now_ms: Optional[int] = None) -> Optional[int]:
        """§2.4.1 信息邮件:**一消息一邮件**;发给谁/用哪个 SMTP/用哪套模板都按 ``mail_routes``(§2.15.1)。"""
        if channel not in self.cfg.outbound.enabled_channels:
            return None
        route = self._route_for(channel, account_id)
        if route is None:
            return None
        rendered = render_message_mail(self.cfg.template_out, ctx)
        to, cc = recipients_of(route, session_id=session_id)             # 🔴 收件人只来自路由(§2.4.1a 规则 2)
        key = f"message:{ref_message_id or ctx.get('message_id')}{(':' + dedup_suffix) if dedup_suffix else ''}"
        oid = self._enqueue(kind="message", route=route, rendered=rendered, dedup_key=key, to=to, cc=cc,
                            ref_message_id=ref_message_id, attachments=attachments, now_ms=now_ms)
        if oid is not None and rendered.render_notes and _has_render_notes(self.ms):
            self.ms.outbox_update(oid, render_notes=rendered.render_notes)
        return oid

    def enqueue_receipt(self, *, route: MailRoute, to_addr: str, ctx: dict[str, Any], values: dict[str, str],
                        inbox_id: int, in_reply_to: Optional[str], suffix: str = "",
                        now_ms: Optional[int] = None) -> Optional[int]:
        """§2.4.2 回执:用**收到指令的那条路由**的 outbound 发(``In-Reply-To`` 才能叠在一起,§2.15.1)。

        ``receipt_to_sender=true`` 时收件人 = 指令邮件的 ``From``(它已过白名单+验签,是配置认可的地址)。
        """
        rendered = render_receipt(self.cfg.template_out, ctx, values)
        to = [to_addr] if (self.cfg.outbound.receipt_to_sender and to_addr) else list(route.outbound_to)
        key = f"receipt:{inbox_id}{suffix}"
        return self._enqueue(kind="receipt", route=route, rendered=rendered, dedup_key=key, to=to, cc=[],
                             in_reply_to=in_reply_to, ref_inbox_id=inbox_id, now_ms=now_ms)

    def enqueue_alert(self, *, ctx: dict[str, Any], lines: list[tuple[str, str]], dedup_key: str,
                      now_ms: Optional[int] = None) -> Optional[int]:
        """§2.16.2:``kind='alert'``;收件人 ``alert_to`` **恒取 default 路由的 ``outbound_json``**,占位符永不影响收件人。"""
        route = self.routes.lookup(None, None)
        if route is None or not route.outbound.enabled:
            self._alert(MAIL_ROUTE_UNRESOLVED, subject="route:*/")
            return None
        rendered = render_alert(self.cfg.template_out, ctx, lines)
        to = list(route.outbound.alert_to or route.outbound_to)
        return self._enqueue(kind="alert", route=route, rendered=rendered, dedup_key=dedup_key, to=to, cc=[],
                             now_ms=now_ms)

    # ------------------------------------------------------------------ 投递
    def _take_token(self, now_ms: int) -> bool:
        """令牌桶 ``send_rate_per_min``/``send_burst``;减半期内速率折半(§2.1)。"""
        rate = self.cfg.outbound.send_rate_per_min
        if now_ms < self._halved_until_ms:
            rate = max(1, rate // 2)
        if self._tokens_ms == 0:
            self._tokens_ms = now_ms
        elapsed = max(0, now_ms - self._tokens_ms)
        self._tokens = min(float(self.cfg.outbound.send_burst), self._tokens + elapsed * rate / 60000.0)
        self._tokens_ms = now_ms
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def _backoff_ms(self, attempts: int) -> int:
        table = self.cfg.outbound.backoff_s
        return int(table[min(max(attempts - 1, 0), len(table) - 1)]) * 1000

    def build_mime(self, row: dict[str, Any], route: MailRoute) -> bytes:
        """🔴 唯一设头入口:``build_headers()``(禁止手拼 ``msg['Subject']=``,§2.4.1a 模块级强制)。"""
        extra = {}
        if row.get("in_reply_to"):
            extra["In-Reply-To"] = row["in_reply_to"]
        if row.get("references_hdr"):
            extra["References"] = row["references_hdr"]
        shim = _RouteShim(route, to=[a for a in (row.get("to_addrs") or "").split(",") if a.strip()],
                          cc=[a for a in (row.get("cc_addrs") or "").split(",") if a.strip()])
        msg = build_headers(shim, row.get("subject") or "", extra=extra)
        msg.set_content(row.get("body_text") or "")
        if row.get("body_html"):
            msg.add_alternative(row["body_html"], subtype="html")       # multipart/alternative 两份正文
        for att in json.loads(row.get("attachments_json") or "[]"):
            path = att.get("path")
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue                                                # 附件取不到不阻塞正文(§2.4.1「仍超或非图片 → 不附」同理)
            if len(data) > self.cfg.outbound.max_attachment_mb * 1024 * 1024:
                continue
            msg.add_attachment(data, maintype="application", subtype="octet-stream",
                               filename=str(att.get("name") or "attachment"))
        return msg.as_bytes()

    def run_once(self, *, now_ms: Optional[int] = None, limit: int = 50) -> SendStats:
        """消费一轮队列。一封失败只影响它自己(``attempts``/``next_attempt_ms``),后面的照发。"""
        stats = SendStats()
        now = now_ms or self.clock()
        if not (self.cfg.enabled and self.cfg.outbound.enabled):
            return stats
        by_route: dict[Optional[int], MailRoute] = {r.id: r for r in self.routes.routes}
        backend_cache: dict[Optional[int], Any] = {}
        try:
            for row in self.ms.outbox_due(now_ms=now, limit=limit):
                if not self._take_token(now):
                    stats.skipped_rate += 1
                    continue
                route = by_route.get(row.get("route_id")) or self.routes.lookup(None, None)
                if route is None:
                    self._fail_permanent(row, "路由不存在", now)
                    stats.dead += 1
                    continue
                backend = backend_cache.get(route.id)
                if backend is None:
                    if self.smtp_factory is None:
                        return stats
                    backend = self.smtp_factory(route)
                    try:
                        backend.connect_login()
                    except MailAuthError as e:
                        self._alert(MAIL_AUTH_FAILED, subject=f"smtp:{route.outbound.host}",
                                    evidence={"last_error": str(e)})
                        self._retry(row, str(e), now, host=route.outbound.host)
                        stats.retried += 1
                        continue
                    except SmtpTemporaryError as e:
                        self._retry(row, str(e), now, host=route.outbound.host)
                        stats.retried += 1
                        continue
                    backend_cache[route.id] = backend
                self.ms.outbox_update(row["id"], status="SENDING")
                try:
                    resp = backend.send(route.outbound_from,
                                        [a.strip() for a in (row.get("to_addrs") or "").split(",") if a.strip()]
                                        + [a.strip() for a in (row.get("cc_addrs") or "").split(",") if a.strip()],
                                        self.build_mime(row, route))
                except SmtpPermanentError as e:
                    self._fail_permanent(row, str(e), now)               # 5xx:重试只会重复挨拒
                    stats.dead += 1
                    continue
                except (SmtpTemporaryError, MailAuthError) as e:
                    self._retry(row, str(e), now, host=route.outbound.host)
                    stats.retried += 1
                    stats.errors.append(str(e))
                    continue
                self._succeed(row, resp, now, host=route.outbound.host)
                stats.sent += 1
        finally:
            for b in backend_cache.values():
                try:
                    b.quit()
                except Exception:                                        # noqa: BLE001
                    pass
        return stats

    def _succeed(self, row: dict[str, Any], resp: str, now: int, *, host: str = "") -> None:
        self.ms.outbox_update(row["id"], status="SENT", sent_ms=now, smtp_response=resp, last_error=None)
        self._consecutive_failures = 0
        if self.alerts is not None:
            self.alerts.resolve(MAIL_SMTP_FAILING, subject=f"smtp:{host}")
        if row.get("kind") == "receipt" and row.get("ref_inbox_id"):
            # §2.3.5 状态流转:DONE → RECEIPT_SENT(回执真的发出去了才改)
            self.ms.inbox_update(int(row["ref_inbox_id"]), status=RECEIPT_SENT)

    def _retry(self, row: dict[str, Any], err: str, now: int, *, host: str = "") -> None:
        attempts = int(row.get("attempts") or 0) + 1
        self._consecutive_failures += 1
        if "too many" in err.lower() or "频率" in err or err.startswith("421"):
            self._halved_until_ms = now + RATE_HALVE_MS
        if attempts > self.cfg.outbound.max_attempts:
            self._fail_permanent(row, err, now, attempts=attempts)
            return
        self.ms.outbox_update(row["id"], status="RETRY", attempts=attempts, last_error=err,
                              next_attempt_ms=now + self._backoff_ms(attempts))
        if self._consecutive_failures >= 3:
            # §2.7 告警表:`MAIL_SMTP_FAILING` 的 subject = **`smtp:<host>`**——
            # 多路由/多 SMTP 时各算各的去重键,`P-MAIL` 才认得出是哪台 SMTP 在失败
            self._alert(MAIL_SMTP_FAILING, subject=f"smtp:{host}",
                        evidence={"last_error": err, "consecutive": self._consecutive_failures, "host": host})

    def _fail_permanent(self, row: dict[str, Any], err: str, now: int, *, attempts: Optional[int] = None) -> None:
        self.ms.outbox_update(row["id"], status="DEAD", last_error=err,
                              attempts=attempts if attempts is not None else int(row.get("attempts") or 0) + 1)
        self._alert(MAIL_OUTBOX_DEAD, subject=f"outbox:{row['id']}", evidence={"last_error": err})

    # ------------------------------------------------------------------ P-MAIL 动作(§3.2)
    def resend(self, outbox_id: int, *, now_ms: Optional[int] = None) -> Optional[int]:
        """``POST /mail/outbox/{id}/resend``(02 #62)/ `P-MAIL`【重发回执】(§2.5 末)。

        **已经走完投递的行(``DEAD``/``DISCARDED``/``SENT``)一律「复制一行重新入队」**,``dedup_key`` 加后缀
        ``#2``、多次重发依次递增(``#3``…),**原行一个字不动** —— 原来那封的投递留痕(``sent_ms``/``smtp_response``)
        是追溯的一环,就地改回 ``QUEUED`` 会把它覆盖掉。
        还在队列里的(``QUEUED``/``RETRY``/``SENDING``)本来就没发出去,就地清零重来,不产生第二封。
        """
        row = self.ms.outbox_get(outbox_id)
        if row is None:
            return None
        now = now_ms or self.clock()
        if row["status"] not in RESEND_COPY_STATES:
            self.ms.outbox_update(outbox_id, status="QUEUED", attempts=0, next_attempt_ms=0, last_error=None)
            return outbox_id
        base = re.sub(r"#\d+$", "", row["dedup_key"])
        for n in range(2, 1000):                        # `dedup_key` 唯一:撞上就往后挪一位(#2 → #3 → …)
            new_id = self._copy_row(row, f"{base}#{n}", now)
            if new_id is not None:
                return new_id
        return None

    def _copy_row(self, row: dict[str, Any], dedup_key: str, now: int) -> Optional[int]:
        return self.ms.outbox_enqueue(
            kind=row["kind"], route_id=row.get("route_id"), to_addrs=row["to_addrs"], cc_addrs=row["cc_addrs"],
            subject=row["subject"], body_text=row["body_text"], body_html=row.get("body_html"),
            attachments_json=row.get("attachments_json"), rfc_message_id=new_message_id(),
            in_reply_to=row.get("in_reply_to"), references_hdr=row.get("references_hdr"),
            ref_inbox_id=row.get("ref_inbox_id"), ref_message_id=row.get("ref_message_id"),
            ref_trace_id=row.get("ref_trace_id"), template_id=row.get("template_id"),
            template_version=row["template_version"], template_profile=row.get("template_profile", "custom"),
            dedup_key=dedup_key, now_ms=now)

    def discard(self, outbox_id: int) -> None:
        """``POST /mail/outbox/{id}/discard``。"""
        self.ms.outbox_update(outbox_id, status="DISCARDED")


@dataclass
class _RouteShim:
    """给 ``build_headers`` 的最小面:收件人取**入队时固化**的那一份(§2.15.3:在途行不受路由改动影响)。"""
    route: MailRoute
    to: list[str]
    cc: list[str]

    @property
    def outbound_from(self) -> str:
        return self.route.outbound_from

    @property
    def outbound_to(self) -> list[str]:
        return self.to

    @property
    def outbound_cc(self) -> list[str]:
        return self.cc


def _has_render_notes(ms: MailStore) -> bool:
    """``mail_outbox.render_notes`` 是 06 §3.1 提的列,02 v1 DDL 尚未建(见 handoff「建议裁决 ⑤」)。"""
    cols = {r[1] for r in ms.con.execute("PRAGMA table_info(mail_outbox)").fetchall()}
    return "render_notes" in cols
