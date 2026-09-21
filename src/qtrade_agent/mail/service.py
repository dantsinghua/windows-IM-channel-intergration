"""邮件服务装配 —— 规格:docs/06 §2.0(总览:一个邮箱一份连接)、§2.7(``GET /mail/status``)、§2.15.3(读线程按 ``mailbox_key`` 去重)。

本类把 ``routes``/``fetcher``/``ingest``/``sender``/``cleanup`` 串起来,给总控一个接线点:
``app.py`` 装一个 ``MailService``、``scheduler`` 注册三个周期任务(取信、发队列、confirm reaper),
``api/`` 的 ``/mail/*`` 端点调本类的方法 —— 逐条接线建议见 ``.omc/handoffs/mail-relay.md``。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..events import iso8601
from .catalog import Catalog
from .cleanup import MailCleanup
from .config import MailConfig
from .confirm import PendingConfirms
from .fetcher import MailFetcher
from .ingest import MailIngest
from .mail_store import MailStore
from .routes import MailRoute, RouteTable
from .sender import MailSender

log = logging.getLogger("qtrade.mail")


class MailService:
    """一个 Agent 一份。``enabled=false`` 时收/发/清理都不跑(§7 ``[mail] enabled``)。"""

    def __init__(self, store: Any, cfg: MailConfig, *, catalog: Optional[Catalog] = None,
                 clock: Optional[Callable[[], int]] = None, alerts: Any = None,
                 secret_of: Optional[Callable[[str], str]] = None,
                 imap_factory: Optional[Callable[[MailRoute], Any]] = None,
                 pop3_factory: Optional[Callable[[MailRoute], Any]] = None,
                 smtp_factory: Optional[Callable[[MailRoute], Any]] = None,
                 disk_state: Optional[Callable[[], str]] = None):
        self.store = store
        self.cfg = cfg
        self.ms = MailStore(store)
        self.clock = clock or self.ms._now
        self.alerts = alerts
        self.catalog = catalog or Catalog.load()
        self.secret_of = secret_of or (lambda ref: "")
        self.imap_factory = imap_factory
        self.pop3_factory = pop3_factory
        self.smtp_factory = smtp_factory
        self.disk_state = disk_state or (lambda: "ok")
        self.routes = RouteTable([])
        self.sender: Optional[MailSender] = None
        self.ingest: Optional[MailIngest] = None
        self.confirms: Optional[PendingConfirms] = None
        self.fetchers: dict[str, MailFetcher] = {}
        self.cleaners: dict[str, MailCleanup] = {}
        self.reload()

    # ------------------------------------------------------------------ 装配
    def reload(self) -> None:
        """从库里读路由表;没有全局行且 ``[mail]`` 配了就物化一条(§3.1 末:default 行是 TOML 的物化视图)。"""
        rows = self.ms.routes_list()
        if not any(r["channel"] is None and r["account_id"] is None for r in rows) and self.cfg.inbound.host:
            g = MailRoute.materialize_global(self.cfg)
            self.ms.route_upsert(channel=None, account_id=None,
                                 inbound_json=_inbound_json(g), outbound_json=_outbound_json(g),
                                 enabled=self.cfg.enabled)
            rows = self.ms.routes_list()
        self.routes = RouteTable.from_rows(rows)
        self.sender = MailSender(self.ms, self.routes, self.cfg, smtp_factory=self._smtp, clock=self.clock,
                                 alerts=self.alerts)
        self.ingest = MailIngest(self.ms, self.routes, self.cfg, store=self.store, catalog=self.catalog,
                                 sender=self.sender, clock=self.clock, alerts=self.alerts,
                                 secret_of=self.secret_of)
        self.confirms = PendingConfirms(self.ms, self.routes, self.cfg, store=self.store, catalog=self.catalog,
                                        ingest=self.ingest, clock=self.clock)
        self.fetchers, self.cleaners = {}, {}
        for key, routes in self.routes.mailboxes().items():
            route = routes[0]                       # 多条路由共用同一邮箱时共用那条线程(§2.15.3)
            cleaner = MailCleanup(self.ms, route, self.cfg, clock=self.clock, alerts=self.alerts,
                                  disk_state=self.disk_state)
            self.cleaners[key] = cleaner
            self.fetchers[key] = MailFetcher(
                route, self.ms, self.cfg,
                imap_factory=(lambda r=route: self._imap(r)), pop3_factory=(lambda r=route: self._pop3(r)),
                ingest=self.ingest.ingest_raw, alerts=self.alerts, clock=self.clock,
                disk_state=self.disk_state,
                cleanup_hook=(lambda backend, proto, c=cleaner: c.run(backend, proto)))

    def _imap(self, route: MailRoute) -> Any:
        return self.imap_factory(route) if self.imap_factory else None

    def _pop3(self, route: MailRoute) -> Any:
        return self.pop3_factory(route) if self.pop3_factory else None

    def _smtp(self, route: MailRoute) -> Any:
        return self.smtp_factory(route) if self.smtp_factory else None

    # ------------------------------------------------------------------ 周期动作
    def fetch_once(self) -> dict[str, Any]:
        """每个邮箱跑一轮取信(清理在同一轮、同一连接里,§2.6.6)。"""
        out: dict[str, Any] = {}
        for key, f in self.fetchers.items():
            out[key] = f.run_once()
        return out

    async def dispatch(self, bus: Any) -> list[Any]:
        return await self.ingest.dispatch(bus) if self.ingest else []

    def reparse(self, inbox_id: int) -> Optional[str]:
        """``POST /mail/inbox/{id}/reparse``(02 #60 / 06 §3.2):模板改对之后,对老邮件**重跑解析**。

        原文从库里已存的 ``body_text`` 重建(取信时就落了;``OUT_OF_SCOPE``/``OVERSIZE`` 不存正文,所以那两态本来
        也不在 #60 的可重跑集合里)。重跑只走 ``_classify`` 那一段——三道闸 / 去重四层 / 高危 202 / 回执,
        **不重新落新行**(同一 ``mail_inbox.id`` 原地改 ``status``/``reason``)。
        """
        from .fetcher import RawMail
        from .parser import ParsedMail

        if self.ingest is None:
            return None
        row = self.ms.inbox_get(inbox_id)
        if row is None:
            return None
        parsed = ParsedMail(rfc_message_id=row.get("rfc_message_id"), from_addr=row.get("from_addr") or "",
                            to_addrs=row.get("to_addrs") or "", subject=row.get("subject") or "",
                            date_ms=row.get("date_ms"), body_text=row.get("body_text") or "")
        mail = RawMail(mailbox=row.get("mailbox") or "", protocol=row.get("protocol") or "imap",
                       folder=row.get("folder") or "INBOX", size_bytes=int(row.get("size_bytes") or 0), raw=b"",
                       uid=row.get("uid"), uidvalidity=row.get("uidvalidity"), uidl=row.get("uidl"))
        base = {"mailbox": mail.mailbox, "protocol": mail.protocol, "folder": mail.folder, "uid": mail.uid,
                "uidvalidity": mail.uidvalidity, "uidl": mail.uidl, "rfc_message_id": parsed.message_id_or_hash(),
                "from_addr": parsed.from_addr, "to_addrs": parsed.to_addrs[:512], "subject": parsed.subject[:512],
                "date_ms": parsed.date_ms, "received_ms": row.get("received_ms") or self.clock(),
                "size_bytes": mail.size_bytes, "body_sha256": parsed.body_sha256}
        return self.ingest._classify(inbox_id, mail, parsed, base).status

    def send_once(self, *, limit: int = 50) -> Any:
        return self.sender.run_once(limit=limit) if self.sender else None

    def reap_confirms(self) -> int:
        """``scheduler`` 注册的 ``mail_confirm_reaper``,每 60 s 一轮(02 §2.2.11)。"""
        return self.confirms.reaper() if self.confirms else 0

    async def poll_once(self, bus: Any) -> dict[str, Any]:
        fetched = self.fetch_once()
        results = await self.dispatch(bus)
        sent = self.send_once()
        return {"fetched": fetched, "dispatched": len(results), "sent": sent}

    # ------------------------------------------------------------------ 02 #56 GET /mail/status
    def status(self, route_id: Optional[Any] = None, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        """#56 的逐路由条目(键集 = **02 #56 逐字**,总控 2026-09-21 裁决以 02 为准)。

        不带 ``route_id`` ⇒ **全部启用路由**(02 #56「全部启用路由」;``include_disabled=True`` 连停用的一起,供 #105
        每行的 ``status`` 摘要);带 ⇒ 该条(停用的也给),没有 ⇒ ``[]``。
        顶层 ``{enabled, routes}`` / 单条 ``{enabled, route, …}`` 的外壳由 ``api/app.py`` 包。每个键的数据源见
        ``.omc/handoffs/backend-api-6.md`` §A:找不到可信源的键回 ``null``,不编值。
        """
        out = []
        # mail_cleanup_log 没有 mailbox 列(02 DDL;06 §3.1 有 `mailbox_key`,见 backend-api-5 D-3)⇒ 只有一个物理邮箱时
        # 才能把「最近一轮清理」归到它名下;多邮箱时归不了属,回 null,不把 A 邮箱的配额摆到 B 邮箱名下。
        attributable = len(self.fetchers) <= 1
        last_log = (self.ms.cleanup_log_list(limit=1) or [None])[0] if attributable else None
        last_round = self.ms.cleanup_log_last_round() if attributable else None
        consecutive = self.sender._consecutive_failures if self.sender else 0
        for r in self.routes.routes:
            if route_id is None and not r.enabled and not include_disabled:
                continue
            if route_id is not None and str(r.id) != str(route_id):
                continue
            f = self.fetchers.get(r.mailbox_key)
            inbound = f.status() if f else {
                "protocol_configured": r.inbound.protocol, "protocol_active": r.inbound.protocol, "fallback": None,
                "folders": [{"name": n, "uidvalidity": None, "last_uid": 0} for n in r.inbound.folders],
                "last_success_at": None, "last_error": None, "idle_supported": r.inbound.idle, "consecutive_failures": 0}
            inbound["quota"] = _quota_view(last_round)
            counts = self.ms.outbox_counts(r.id)
            last_sent = self.ms.outbox_last_sent_ms(r.id)
            rid = str(r.id) if r.id is not None else None
            out.append({
                "route_id": rid, "channel": r.channel, "account_id": r.account_id,
                "route": {"id": rid, "channel": r.channel, "account_id": r.account_id,
                          "outbound_template_id": r.outbound_template_id, "inbound_template_id": r.inbound_template_id},
                "inbound": inbound,
                "outbound": {"queued": counts["queued"], "retrying": counts["retrying"], "dead": counts["dead"],
                             "last_sent_at": iso8601(last_sent) if last_sent else None,
                             # 源 = 投递器的连续失败计数(全 Agent 一个投递器、内存态,重启归零;不分路由)
                             "consecutive_failures": consecutive,
                             "rate_per_min": self.cfg.outbound.send_rate_per_min},
                "cleanup": {"last_run_at": iso8601(last_log["finished_ms"]) if last_log else None,
                            "last_status": last_log["status"] if last_log else None,
                            "archived_mb": _mb(self.ms.inbox_archived_bytes(mailbox=r.mailbox_key)),
                            # 无可信源:清理实际挂在每轮取信之后跑(fetcher 的 cleanup_hook),`cleanup_interval_min`
                            # 没有被任何调度消费 ⇒ 按「上次 + interval」算出来的时刻不是真的下次,宁可 null
                            "next_run_at": None},
            })
        return out


def _mb(n: Optional[int]) -> Optional[float]:
    return None if n is None else round(int(n) / (1024 * 1024), 1)


def _quota_view(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """#56 ``inbound.quota``:取最近一轮清理量到的配额(§2.6.4 ``quota()``,清理轮之外没有别处量它)。

    ``used_mb`` 取清理**后**的量(没有则清理前);``source`` 原样 = ``imap_quota|estimate|pop3_stat|unknown``(02 DDL CHECK)。
    还没跑过清理、或那轮什么都没量到 ⇒ ``null``。
    """
    if row is None:
        return None
    used = row.get("quota_used_after") if row.get("quota_used_after") is not None else row.get("quota_used_before")
    if used is None and row.get("quota_limit") is None:
        return None
    return {"used_mb": _mb(used), "limit_mb": _mb(row.get("quota_limit")), "source": row.get("quota_source") or "unknown"}


def _inbound_json(route: MailRoute) -> dict[str, Any]:
    inb = route.inbound
    return {
        "enabled": inb.enabled, "protocol": inb.protocol, "host": inb.host, "port": inb.port, "ssl": inb.ssl,
        "user": inb.user, "secret_ref": inb.secret_ref, "level": inb.level, "folders": list(inb.folders),
        "processed_folder": inb.processed_folder, "poll_interval_s": inb.poll_interval_s, "idle": inb.idle,
        "max_retr_per_round": inb.max_retr_per_round, "max_message_bytes": inb.max_message_bytes,
        "keep_raw": inb.keep_raw, "send_imap_id": inb.send_imap_id,
        "allowed_senders": list(inb.allowed_senders), "require_signature": inb.require_signature,
        "sig_time_tolerance_s": inb.sig_time_tolerance_s, "nonce_ttl_h": inb.nonce_ttl_h,
        "allow_ops": list(inb.allow_ops), "danger_confirm_via": inb.danger_confirm_via,
        "danger_confirm_ttl_s": inb.danger_confirm_ttl_s, "max_timeout_ms": inb.max_timeout_ms,
        "reply_on_parse_failure": inb.reply_on_parse_failure,
        "fallback": {"enabled": inb.fallback.enabled, "host": inb.fallback.host, "port": inb.fallback.port,
                     "ssl": inb.fallback.ssl, "after_failures": inb.fallback.after_failures,
                     "recheck_min": inb.fallback.recheck_min},
        "hmac": {s: {"from": k.from_addr, "secret_ref": k.secret_ref} for s, k in inb.hmac.items()},
    }


def _outbound_json(route: MailRoute) -> dict[str, Any]:
    o = route.outbound
    return {
        "enabled": o.enabled, "host": o.host, "port": o.port, "ssl": o.ssl, "user": o.user,
        "secret_ref": o.secret_ref, "from": o.from_addr, "to": list(o.recipients), "cc": list(o.cc),
        "send_rate_per_min": o.send_rate_per_min, "send_burst": o.send_burst, "timeout_s": o.timeout_s,
        "max_attempts": o.max_attempts, "backoff_s": list(o.backoff_s),
        "max_attachment_mb": o.max_attachment_mb, "max_mail_mb": o.max_mail_mb,
        "include_self": o.include_self, "enabled_channels": list(o.enabled_channels),
        "receipt_to_sender": o.receipt_to_sender, "alert_to": list(o.alert_to),
        "alert_on_endpoint_change": o.alert_on_endpoint_change,
        "session_overrides": list(o.session_overrides),
    }
