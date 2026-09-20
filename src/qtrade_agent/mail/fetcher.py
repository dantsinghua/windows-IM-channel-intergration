"""取信 —— 规格:docs/06 §2.1(IMAP/POP3 两条收信循环 + ``run_cycle()``)、§2.1.1(IMAP → POP3 自动回落 E-1)、§2.6.10(磁盘三级水位)。

🔴 SIZE 门必须在循环里(R5-6):先只取大小(``RFC822.SIZE`` / POP3 ``LIST``),超 ``max_message_bytes`` 就
**只登记元数据、绝不取正文**,`status=OVERSIZE`、告警 ``MAIL_MSG_OVERSIZE``、``continue`` 直接进下一封
——**不落 ``on_terminal()`` 的 MOVE 队列**(R6-1:``OVERSIZE ∈ NEVER_DELETE``),水位照常推进、不卡后续。

🔴 磁盘 ``critical``(< 1 GB,§2.6.10):本轮**不 ``RETR``/不 ``FETCH BODY``、不 ``ingest``**,
**邮件留在服务器不删、绝不推进 UID/UIDL 水位、绝不标已处理**(否则恢复后这批邮件永远补收不回来 = 丢邮件)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .backends import MailAuthError, MailConnectError, MailTransientError
from .codes import (MAIL_ALERT_CODES, MAIL_AUTH_FAILED, MAIL_INBOUND_STALLED, MAIL_MSG_OVERSIZE,
                    MAIL_PAUSED_DISK_FULL, MAIL_PROTOCOL_FALLBACK, OVERSIZE)
from .config import MailConfig
from .mail_store import MailStore
from .routes import MailRoute

log = logging.getLogger("qtrade.mail.fetcher")

#: §2.1.1「判据只认『连不上 / 不支持』」——这四种 kind 才计入回落计数
FALLBACK_ERROR_KINDS = frozenset({"connect_refused", "connect_timeout", "tls_handshake_failed", "imap_not_enabled"})


@dataclass
class RawMail:
    """一封取回来的信(或超限时只有头的元数据)。``oversize=True`` 时 ``raw`` 只是邮件头。"""
    mailbox: str
    protocol: str
    folder: str
    size_bytes: int
    raw: bytes
    uid: Optional[int] = None
    uidvalidity: Optional[int] = None
    uidl: Optional[str] = None
    oversize: bool = False


@dataclass
class FetchStats:
    fetched: int = 0
    oversize: int = 0
    skipped: int = 0            # 已见(去重键命中)
    errors: int = 0
    paused_disk: bool = False
    statuses: list[str] = field(default_factory=list)


class MailFetcher:
    """一个物理邮箱(``mailbox_key``)一个实例 —— §2.15.3「一个物理邮箱一条读线程」。

    ``ingest`` 回调 = ``ingest.MailIngest.ingest_raw``,返回该封的 ``mail_inbox.status``;
    ``cleanup_hook`` 在同一轮取信之后执行(§2.6.6:IMAP 在 ``run_cycle()`` 之后、进 IDLE 之前;POP3 同一会话)。
    """

    def __init__(self, route: MailRoute, mail_store: MailStore, cfg: MailConfig, *,
                 imap_factory: Optional[Callable[[], Any]] = None,
                 pop3_factory: Optional[Callable[[], Any]] = None,
                 ingest: Optional[Callable[[RawMail], str]] = None,
                 alerts: Any = None, clock: Optional[Callable[[], int]] = None,
                 disk_state: Optional[Callable[[], str]] = None,
                 cleanup_hook: Optional[Callable[[Any, str], None]] = None):
        self.route = route
        self.ms = mail_store
        self.cfg = cfg
        self.imap_factory = imap_factory
        self.pop3_factory = pop3_factory
        self.ingest = ingest
        self.alerts = alerts
        self.clock = clock or mail_store._now
        self.disk_state = disk_state or (lambda: "ok")
        self.cleanup_hook = cleanup_hook

    # ------------------------------------------------------------------ 状态(§2.1.1;落 cursors(owner, protocol_state))
    @property
    def owner(self) -> str:
        return self.route.cursor_owner

    @property
    def mailbox(self) -> str:
        return self.route.mailbox_key

    def state(self) -> dict[str, Any]:
        st = dict(self.ms.protocol_state(self.owner))
        st.setdefault("configured_protocol", self.route.inbound.protocol)
        st.setdefault("effective_protocol", self.route.inbound.protocol)
        st.setdefault("imap_consecutive_failures", 0)
        return st

    def _save(self, st: dict[str, Any]) -> None:
        self.ms.protocol_state_set(self.owner, st)

    @property
    def effective_protocol(self) -> str:
        return str(self.state()["effective_protocol"])

    def _alert(self, code: str, *, subject: str, evidence: Optional[dict[str, Any]] = None,
               severity: Optional[str] = None) -> None:
        if self.alerts is None:
            return
        self.alerts.firing(code, subject=subject, severity=severity or MAIL_ALERT_CODES.get(code, "warn"),
                           evidence=evidence or {})

    def _resolve(self, code: str, *, subject: str) -> None:
        if self.alerts is not None:
            self.alerts.resolve(code, subject=subject)

    # ------------------------------------------------------------------ 一轮
    def run_once(self) -> FetchStats:
        """跑一轮取信(按当前生效协议),含 §2.1.1 的错误分诊与回落/回切。"""
        stats = FetchStats()
        if not (self.cfg.enabled and self.route.inbound.enabled):
            return stats
        if self.disk_state() == "critical":
            # §2.6.10 critical:不 RETR/不 FETCH BODY、不 ingest、不推进水位、不标已处理
            stats.paused_disk = True
            self._alert(MAIL_PAUSED_DISK_FULL, subject=f"mailbox:{self.mailbox}",
                        evidence={"reason": "disk_critical"})
            return stats
        self._resolve(MAIL_PAUSED_DISK_FULL, subject=f"mailbox:{self.mailbox}")

        st = self.state()
        if st["effective_protocol"] == "pop3" and st["configured_protocol"] == "imap":
            self._maybe_recheck_imap(st)
            st = self.state()

        if st["effective_protocol"] == "imap":
            return self._run_imap(st, stats)
        return self._run_pop3(st, stats)

    def _run_imap(self, st: dict[str, Any], stats: FetchStats) -> FetchStats:
        backend = self.imap_factory() if self.imap_factory else None
        if backend is None:
            return stats
        try:
            backend.connect_login()
        except MailConnectError as e:
            self._on_imap_connect_error(st, e)
            stats.errors += 1
            return stats
        except MailAuthError as e:
            # §2.1.1:认证失败**不回落**——密码在 IMAP 上错,在 POP3 上一样错;回落只会把一次告警变成两次
            st["last_error"] = str(e)
            self._save(st)
            self._alert(MAIL_AUTH_FAILED, subject=f"mailbox:{self.mailbox}", evidence={"last_error": str(e)})
            stats.errors += 1
            return stats
        except MailTransientError as e:
            st["last_error"] = str(e)
            self._save(st)
            stats.errors += 1
            return stats
        try:
            self.run_cycle_imap(backend, stats)
            if self.cleanup_hook is not None:
                self.cleanup_hook(backend, "imap")          # §2.6.6:同一线程、run_cycle() 之后、进 IDLE 之前
            st = self.state()
            st["imap_consecutive_failures"] = 0
            st["last_success_ms"] = self.clock()
            st["last_error"] = None
            self._save(st)
            self._resolve(MAIL_INBOUND_STALLED, subject=f"mailbox:{self.mailbox}")
            self._resolve(MAIL_AUTH_FAILED, subject=f"mailbox:{self.mailbox}")
        finally:
            backend.logout()
        return stats

    def _run_pop3(self, st: dict[str, Any], stats: FetchStats) -> FetchStats:
        backend = self.pop3_factory() if self.pop3_factory else None
        if backend is None:
            return stats
        try:
            backend.connect_login()
        except MailAuthError as e:
            st["last_error"] = str(e)
            self._save(st)
            self._alert(MAIL_AUTH_FAILED, subject=f"mailbox:{self.mailbox}", evidence={"last_error": str(e)})
            stats.errors += 1
            return stats
        except (MailConnectError, MailTransientError) as e:
            st["last_error"] = str(e)
            self._save(st)
            stats.errors += 1
            return stats
        try:
            self.run_cycle_pop3(backend, stats)
            if self.cleanup_hook is not None:
                self.cleanup_hook(backend, "pop3")          # §2.1 循环末尾 cleanup_pop3(pop):同一会话里 DELE
            st = self.state()
            st["last_success_ms"] = self.clock()
            st["last_error"] = None
            self._save(st)
            self._resolve(MAIL_INBOUND_STALLED, subject=f"mailbox:{self.mailbox}")
        finally:
            backend.quit()
        return stats

    # ------------------------------------------------------------------ §2.1 run_cycle()(IMAP)
    def run_cycle_imap(self, backend: Any, stats: Optional[FetchStats] = None) -> FetchStats:
        stats = stats or FetchStats()
        inb = self.route.inbound
        budget = inb.max_retr_per_round
        for folder in inb.folders:
            if budget <= 0:
                break
            uidvalidity, _ = backend.select(folder, readonly=True)       # BODY.PEEK + SELECT readonly:不改已读态
            stored_uv, last_uid = self.ms.imap_watermark(self.owner, folder)
            if stored_uv is not None and stored_uv != uidvalidity:
                last_uid = 0                                             # UIDVALIDITY 变了 → 水位清零全量重扫
                log.warning("mail: %s/%s UIDVALIDITY 由 %s 变 %s,水位清零重扫", self.mailbox, folder, stored_uv, uidvalidity)
            # IMAP 规定 `n:*` 至少返回最大 UID,会把最后一封重复给回来 —— 再过滤一次 > last_uid
            uids = [u for u in backend.search_uids(last_uid + 1) if u > last_uid]
            for uid in uids:
                if budget <= 0:
                    break
                budget -= 1
                try:
                    size = backend.fetch_size(uid)                       # 🔴 先只取大小、不取正文
                except MailTransientError:
                    stats.errors += 1
                    continue
                if size > inb.max_message_bytes:
                    raw = backend.fetch_headers(uid)                     # 只登记元数据;绝不 FETCH BODY.PEEK[]
                    status = self._deliver(RawMail(mailbox=self.mailbox, protocol="imap", folder=folder,
                                                   size_bytes=size, raw=raw, uid=uid, uidvalidity=uidvalidity,
                                                   oversize=True))
                    stats.oversize += 1
                    stats.statuses.append(status)
                    if status == OVERSIZE:
                        self._alert(MAIL_MSG_OVERSIZE, subject=f"inbox:{self.mailbox}:{uid}",
                                    evidence={"size_bytes": size, "uid": uid, "folder": folder})
                    last_uid = uid                                       # 水位照常推进(下轮 SEARCH 搜不到它,只告警一次)
                    self.ms.imap_watermark_set(self.owner, folder, uidvalidity=uidvalidity, last_uid=last_uid)
                    continue                                             # 🔴 R6-1:不落 on_terminal() 的 MOVE 队列
                raw = backend.fetch_raw(uid)                             # 过门后才取正文
                status = self._deliver(RawMail(mailbox=self.mailbox, protocol="imap", folder=folder,
                                               size_bytes=size, raw=raw, uid=uid, uidvalidity=uidvalidity))
                stats.fetched += 1
                stats.statuses.append(status)
                last_uid = uid
                self.ms.imap_watermark_set(self.owner, folder, uidvalidity=uidvalidity, last_uid=last_uid)
            self.ms.imap_watermark_set(self.owner, folder, uidvalidity=uidvalidity, last_uid=last_uid)
        return stats

    # ------------------------------------------------------------------ §2.1 POP3 收信循环
    def run_cycle_pop3(self, backend: Any, stats: Optional[FetchStats] = None) -> FetchStats:
        stats = stats or FetchStats()
        inb = self.route.inbound
        count, octets = backend.stat()
        self.ms.pop3_stat_set(self.owner, count=count, octets=octets)    # 记进 mail monitor(§2.7)
        budget = inb.max_retr_per_round
        for n, uidl in backend.uidl():
            if budget <= 0:
                break
            if self.ms.inbox_by_uidl(self.mailbox, uidl) is not None:    # 已见(真去重靠唯一索引)
                stats.skipped += 1
                continue
            if uidl in self.ms.uidl_recent(self.owner):                  # §2.9.3 快速过滤 + R6-26:行清理后不复发
                stats.skipped += 1
                continue
            budget -= 1
            size = backend.list_size(n)                                  # 🔴 先只取字节数、不取正文
            if size > inb.max_message_bytes:
                raw = backend.top(n, 0)                                  # 只登记元数据;绝不 RETR
                status = self._deliver(RawMail(mailbox=self.mailbox, protocol="pop3", folder="",
                                               size_bytes=size, raw=raw, uidl=uidl, oversize=True))
                stats.oversize += 1
                stats.statuses.append(status)
                if status == OVERSIZE:
                    self._alert(MAIL_MSG_OVERSIZE, subject=f"inbox:{self.mailbox}:{uidl}",
                                evidence={"size_bytes": size, "uidl": uidl})
                continue                                                 # 🔴 R6-1:本轮不 DELE、往后每轮也不 DELE
            raw = backend.retr(n)                                        # 无 PEEK,POP3 也没有已读态;过门后才取正文
            status = self._deliver(RawMail(mailbox=self.mailbox, protocol="pop3", folder="",
                                           size_bytes=size, raw=raw, uidl=uidl))
            stats.fetched += 1
            stats.statuses.append(status)
        return stats

    def _deliver(self, mail: RawMail) -> str:
        if self.ingest is None:
            return "RECEIVED"
        return self.ingest(mail)

    # ------------------------------------------------------------------ §2.1.1 回落状态机
    def _on_imap_cycle_error(self, st: dict[str, Any], err: Exception) -> None:
        """``on_imap_cycle_error(err)``(§2.1.1 伪代码逐分支)。"""
        if isinstance(err, MailConnectError) and err.kind in FALLBACK_ERROR_KINDS:
            self._on_imap_connect_error(st, err)
        elif isinstance(err, MailAuthError):
            self._alert(MAIL_AUTH_FAILED, subject=f"mailbox:{self.mailbox}", evidence={"last_error": str(err)})
        else:
            st["last_error"] = str(err)                                   # 走 §5「网络断」分支,不计入回落计数
            self._save(st)

    def _on_imap_connect_error(self, st: dict[str, Any], err: MailConnectError) -> None:
        fb = self.route.inbound.fallback
        st["imap_consecutive_failures"] = int(st.get("imap_consecutive_failures", 0)) + 1
        st["last_error"] = str(err)
        if st["imap_consecutive_failures"] >= fb.after_failures and fb.enabled and fb.host:
            if st["effective_protocol"] != "pop3":
                st["effective_protocol"] = "pop3"
                st["fallback_since"] = self.clock()
                self._alert(MAIL_PROTOCOL_FALLBACK, subject=f"mailbox:{self.mailbox}",
                            evidence={"last_error": str(err), "since": st["fallback_since"]})
        elif not fb.host:
            # `host` 空 ⇒ 不回落只告警 MAIL_INBOUND_STALLED(§2.1.1)
            self._alert(MAIL_INBOUND_STALLED, subject=f"mailbox:{self.mailbox}", evidence={"last_error": str(err)})
        self._save(st)

    def _maybe_recheck_imap(self, st: dict[str, Any]) -> None:
        """回落期间每 ``fallback.recheck_min``(30 min)试连一次 IMAP:成功 → 切回、同键 ``state=resolved``、水位从上次继续。"""
        now = self.clock()
        last = int(st.get("last_recheck_ms") or 0)
        if now - last < self.route.inbound.fallback.recheck_min * 60 * 1000:
            return
        st["last_recheck_ms"] = now
        self._save(st)
        if self.imap_factory is None:
            return
        backend = self.imap_factory()
        try:
            backend.connect_login()
            backend.capabilities()
        except (MailConnectError, MailAuthError, MailTransientError):
            return
        finally:
            try:
                backend.logout()
            except Exception:                                            # noqa: BLE001
                pass
        st = self.state()
        st["effective_protocol"] = "imap"
        st["imap_consecutive_failures"] = 0
        st["fallback_since"] = None
        self._save(st)
        self._resolve(MAIL_PROTOCOL_FALLBACK, subject=f"mailbox:{self.mailbox}")

    # ------------------------------------------------------------------ §2.7 GET /mail/status 的 inbound 段
    def status(self) -> dict[str, Any]:
        st = self.state()
        folders = []
        for f in self.route.inbound.folders:
            uv, last_uid = self.ms.imap_watermark(self.owner, f)
            folders.append({"name": f, "uidvalidity": uv, "last_uid": last_uid})
        return {
            "configured_protocol": st["configured_protocol"],
            "effective_protocol": st["effective_protocol"],            # 两者不等即回落中(E-1)
            "fallback_since": st.get("fallback_since"),
            "folders": folders,
            "last_success_at": st.get("last_success_ms"),
            "last_error": st.get("last_error"),
            "idle_supported": self.route.inbound.idle,
            "consecutive_failures": int(st.get("imap_consecutive_failures", 0)),
        }

    def check_stalled(self, *, now_ms: Optional[int] = None) -> bool:
        """§2.7 ``MAIL_INBOUND_STALLED``:``now - last_success_at > max(3 × poll_interval_s, 10min)``;> 1h 升 crit。"""
        now = now_ms or self.clock()
        st = self.state()
        last = int(st.get("last_success_ms") or 0)
        if last <= 0:
            return False
        idle_ms = now - last
        threshold = max(3 * self.route.inbound.poll_interval_s * 1000, 10 * 60 * 1000)
        if idle_ms <= threshold:
            return False
        self._alert(MAIL_INBOUND_STALLED, subject=f"mailbox:{self.mailbox}",
                    severity="crit" if idle_ms > 3600 * 1000 else "warn",
                    evidence={"last_success_at": last, "last_error": st.get("last_error"),
                              "protocol": st["effective_protocol"]})
        return True
