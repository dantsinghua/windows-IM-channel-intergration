"""邮件服务装配 —— 规格:docs/06 §2.0(总览:一个邮箱一份连接)、§2.7(``GET /mail/status``)、§2.15.3(读线程按 ``mailbox_key`` 去重)。

本类把 ``routes``/``fetcher``/``ingest``/``sender``/``cleanup`` 串起来,给总控一个接线点:
``app.py`` 装一个 ``MailService``、``scheduler`` 注册三个周期任务(取信、发队列、confirm reaper),
``api/`` 的 ``/mail/*`` 端点调本类的方法 —— 逐条接线建议见 ``.omc/handoffs/mail-relay.md``。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

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

    # ------------------------------------------------------------------ §2.7 GET /mail/status
    def status(self, route_id: Optional[Any] = None) -> list[dict[str, Any]]:
        """不带 ``route`` 返回**全部路由**的数组;带 ``route=<id>`` 返回一条(E-5)。"""
        out = []
        counts = self.ms.outbox_counts()
        logs = self.ms.cleanup_log_list(limit=1)
        for r in self.routes.routes:
            if route_id is not None and str(r.id) != str(route_id):
                continue
            f = self.fetchers.get(r.mailbox_key)
            out.append({
                "route": {"id": r.id, "name": r.name, "channel": r.channel or "*", "account_id": r.account_id,
                          "mailbox_key": r.mailbox_key},
                "inbound": f.status() if f else {"configured_protocol": r.inbound.protocol,
                                                 "effective_protocol": r.inbound.protocol},
                "outbound": {**counts, "rate_per_min": self.cfg.outbound.send_rate_per_min,
                             "template_profile": self.cfg.template_out.compat_profile},
                "cleanup": {"last_run_at": logs[0]["finished_ms"] if logs else None,
                            "last_status": logs[0]["status"] if logs else None},
            })
        return out


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
