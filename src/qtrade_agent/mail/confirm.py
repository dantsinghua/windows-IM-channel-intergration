"""高危操作的双钥双通道确认 —— 规格:docs/06 §2.3.6(🔴 R-03 重写)、§3.2 #68b/#68c/#68d、02 §3.1 ``mail_confirm_reaper``。

🔴 **承重墙(基线 §11.17 ③)**:``approve``/``reject`` **只接受 ``transport ∈ {console, local}``,``transport='email'`` 一律 ``403``**
——一旦允许邮件批准,失陷邮箱就能自发自批,双钥双通道整条作废。

🔴 **R6-7:v1 只有 ``console`` 一条通道**,入参只有路径里的 ``{id}``(无 body、无 nonce)。
确认钥 ``vault://mail/hmac/confirm`` 在 v1 **只生成、只保管、无消费者**——approve 不用它签/验任何串,本模块因此完全不碰它。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .catalog import Catalog
from .codes import (ACCEPTED, AUDIT_CONFIRM_APPROVED, AUDIT_CONFIRM_ARGS_MISMATCH, AUDIT_CONFIRM_EXPIRED,
                    AUDIT_CONFIRM_REJECTED, CONFIRM_EXPIRED, CONFIRM_REQUIRED, OP_DENIED,
                    REASON_ARGS_TAMPERED, REASON_REJECTED)
from .config import MailConfig
from .ingest import MailIngest, PendingCommand
from .mail_store import MailStore
from .parser import ParsedMail, parse_command_body
from .routes import RouteTable
from .sign import args_digest

log = logging.getLogger("qtrade.mail.confirm")

#: 基线 §11.17 ③:允许调 approve/reject 的 transport
CONFIRM_TRANSPORTS = frozenset({"console", "local"})

#: ⚠️ 两个「transport」不是一回事(handoff「建议裁决 ⑦」):§2.3.6 的承重墙判的是**调用来源** ``{console, local}``,
#: 而 02 §3.1 ``audit_log.transport`` 的 CHECK 只收 ``('local','http','email','system')``。控制台是经 HTTP 调本端点的,
#: 故写审计时 ``console → 'http'``、``local → 'local'``;「是谁批的」仍由 ``actor``(= ``token:console``)承担。
_AUDIT_TRANSPORT = {"console": "http", "local": "local"}


@dataclass
class ConfirmOutcome:
    """端点要回什么(HTTP 状态 + 码);API 层按此拼 ``{ok, code, error, trace_id}``。"""
    ok: bool
    http_status: int = 200
    code: str = "OK"
    reason: str = ""
    inbox_id: Optional[int] = None


class PendingConfirms:
    """``GET /mail/pending-confirms`` + ``approve``/``reject`` + reaper 的业务面(端点接线见 handoff)。"""

    def __init__(self, mail_store: MailStore, routes: RouteTable, cfg: MailConfig, *, store: Any,
                 catalog: Catalog, ingest: MailIngest, clock: Optional[Callable[[], int]] = None):
        self.ms = mail_store
        self.routes = routes
        self.cfg = cfg
        self.store = store
        self.catalog = catalog
        self.ingest = ingest
        self.clock = clock or mail_store._now

    # ------------------------------------------------------------------ #68b
    def list(self) -> list[dict[str, Any]]:
        """出参恰八键(R6-7,两处逐字一致);**不含 ``confirm_via``/``confirm_nonce``**(v1 无此两列)。"""
        return self.ms.pending_confirms(now_ms=self.clock())

    # ------------------------------------------------------------------ #68c approve
    def approve(self, inbox_id: int, *, actor: str, transport: str) -> ConfirmOutcome:
        if transport not in CONFIRM_TRANSPORTS:
            return ConfirmOutcome(False, 403, "FORBIDDEN", "confirm_transport_denied", inbox_id)   # 承重墙
        now = self.clock()
        if not self.ms.confirm_claim(inbox_id, new_status=ACCEPTED, reason=None, now_ms=now):
            return self._expired_or_gone(inbox_id, actor=actor, now=now)
        row = self.ms.inbox_get(inbox_id)
        if row is None:
            return ConfirmOutcome(False, 404, "TARGET_NOT_FOUND", "inbox_gone", inbox_id)
        # 🔴 R6-22:approve 执行时的参数来源 = **重解析已验签的 body_text**;执行前复算 digest 必须与落库值相等(防库被改)
        item, digest = self._rebuild(row)
        if item is None or digest != (row.get("args_digest") or ""):
            self.ms.inbox_update(inbox_id, status=OP_DENIED,
                                 reason=REASON_ARGS_TAMPERED + "approve 前复算 args_digest 与落库值不等")
            self.store.insert_audit(kind="system", transport=_AUDIT_TRANSPORT[transport], actor=actor,
                                    action=AUDIT_CONFIRM_ARGS_MISMATCH, account_id=row.get("account_id"),
                                    result_code="INTERNAL",
                                    detail={"inbox_id": inbox_id, "args_digest": row.get("args_digest")}, now_ms=now)
            return ConfirmOutcome(False, 500, "INTERNAL", "args_digest_mismatch", inbox_id)
        self.ingest.pending.append(item)
        self.store.insert_audit(kind="system", transport=_AUDIT_TRANSPORT[transport], actor=actor, action=AUDIT_CONFIRM_APPROVED,
                                account_id=row.get("account_id"), result_code="OK",
                                detail={"inbox_id": inbox_id, "op": row.get("op")}, now_ms=now)
        return ConfirmOutcome(True, 200, "OK", "", inbox_id)

    # ------------------------------------------------------------------ #68d reject
    def reject(self, inbox_id: int, *, actor: str, transport: str) -> ConfirmOutcome:
        if transport not in CONFIRM_TRANSPORTS:
            return ConfirmOutcome(False, 403, "FORBIDDEN", "confirm_transport_denied", inbox_id)
        now = self.clock()
        # 🔴 R6-34:``reason = 'REJECTED:' || :actor``——与该条审计的 ``actor`` 同值(常量看不出是谁驳的)
        if not self.ms.confirm_claim(inbox_id, new_status=OP_DENIED, reason=REASON_REJECTED + actor, now_ms=now):
            return self._expired_or_gone(inbox_id, actor=actor, now=now)
        row = self.ms.inbox_get(inbox_id) or {}
        self.store.insert_audit(kind="system", transport=_AUDIT_TRANSPORT[transport], actor=actor, action=AUDIT_CONFIRM_REJECTED,
                                account_id=row.get("account_id"), result_code="OP_DENIED",
                                detail={"inbox_id": inbox_id, "op": row.get("op")}, now_ms=now)
        return ConfirmOutcome(True, 200, "OK", "", inbox_id)

    def _expired_or_gone(self, inbox_id: int, *, actor: str, now: int) -> ConfirmOutcome:
        """R6-20:``rowcount==0`` 时——若该行仍 ``CONFIRM_REQUIRED`` 而时刻已过,端点**顺手**置 ``CONFIRM_EXPIRED`` 并记审计,不等 reaper。"""
        if self.ms.confirm_expire_one(inbox_id, now_ms=now):
            row = self.ms.inbox_get(inbox_id) or {}
            self.store.insert_audit(kind="system", transport="http", actor=actor, action=AUDIT_CONFIRM_EXPIRED,
                                    account_id=row.get("account_id"), result_code=CONFIRM_EXPIRED,
                                    detail={"inbox_id": inbox_id, "op": row.get("op"),
                                            "expires_at": row.get("confirm_expires_ms"),
                                            "expired_by": "endpoint"}, now_ms=now)
        return ConfirmOutcome(False, 409, CONFIRM_EXPIRED, "confirm_expired", inbox_id)

    # ------------------------------------------------------------------ reaper(02 §3.1 规范 SQL;每 60 s)
    def reaper(self) -> int:
        """**只是兜底清扫,安全性不依赖其节拍**;逐行记审计 ``actor='system:scheduler'``、``expired_by='reaper'``。"""
        now = self.clock()
        victims = self.ms.confirm_reaper(now_ms=now)
        for v in victims:
            self.store.insert_audit(kind="system", transport="system", actor="system:scheduler",
                                    action=AUDIT_CONFIRM_EXPIRED, account_id=v.get("account_id"),
                                    result_code=CONFIRM_EXPIRED,
                                    detail={"inbox_id": v["id"], "op": v.get("op"), "from_addr": v.get("from_addr"),
                                            "expires_at": v.get("confirm_expires_ms"), "expired_by": "reaper"},
                                    now_ms=now)
        return len(victims)

    # ------------------------------------------------------------------ 重解析(唯一的参数来源)
    def _rebuild(self, row: dict[str, Any]) -> tuple[Optional[PendingCommand], str]:
        """从 ``body_text`` 重走一遍解析 + 类型转型,得到与受理时**同一份** args 及其 digest。"""
        pc = parse_command_body(row.get("body_text") or "", template=self.cfg.template_in,
                                subject=row.get("subject") or "")
        if not pc.ok:
            return None, ""
        cap = self.catalog.get(pc.op)
        if cap is None:
            return None, ""
        args, err = self.ingest._coerce_args(cap, pc)
        if err is not None:
            return None, ""
        digest = args_digest(args)
        route = next((r for r in self.routes.routes if r.id == row.get("route_id")), None) \
            or self.routes.lookup(None, None)
        if route is None:
            return None, digest
        parsed_stub = ParsedMail(rfc_message_id=row.get("rfc_message_id"), from_addr=row.get("from_addr") or "",
                                 to_addrs="", subject=row.get("subject") or "", date_ms=row.get("date_ms"),
                                 body_text=row.get("body_text") or "")
        cmd = self.ingest._build_command(pc, args, str(row.get("idempotency_key") or ""), parsed_stub, cap)
        item = PendingCommand(inbox_id=int(row["id"]), command=cmd, route=route, parsed=pc,
                              reply_to=row.get("from_addr") or "", in_reply_to=row.get("rfc_message_id"))
        return item, digest


def attach_json(row: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        v = json.loads(row.get("attach_json") or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []
