"""入站指令邮件 → ``Command`` → 总线 → 回执 —— 规格:docs/06 §2.2(三道闸)、§2.3(模板/签名/失败落库/高危确认)、§2.5(去重四层)、§2.8(字段映射)。

三道闸缺一不可(§2.2),顺序即本文件的执行顺序:
① **发件人白名单** ``allowed_senders``:不命中 → ``SENDER_DENIED``,**不回执**(否则等于给陌生人一个「这里有系统」的探针);
② **HMAC 验签**(指令钥 ``vault://mail/hmac/cmd/<短名>``):不符 → ``SIG_INVALID``,不回执;过期 ``EXPIRED``、重放 ``DUPLICATE_NONCE``;
③ **``allow_ops`` 白名单**(默认 = 所有 ``danger=false``):不在集合 → ``OP_DENIED`` + ``reason`` 前缀 ``NOT_ALLOWED:``,**回执**(发件人已验签,是真的)。

高危 op(目录 ``danger:true``)一律 ``202 待确认``(``CONFIRM_REQUIRED``),确认**不在邮件链路内闭环**(§2.3.6;v1 只有控制台一条通道)。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from ..events import iso8601
from ..maintenance import DiskFullError
from ..models import Command, CommandError, CommandOrigin, CommandResult, RESULT_CODES
from .catalog import Catalog
from .codes import (ACCEPTED, CONFIRM_REQUIRED, DONE, DUPLICATE, DUPLICATE_NONCE, EXPIRED, INGEST_ERROR,
                    MAIL_ALERT_CODES, MAIL_PARSE_FAILED, MAIL_SENDER_DENIED, MAIL_WATERMARK_STALLED,
                    OP_DENIED, OUT_OF_SCOPE, OVERSIZE, PARSE_FAILED, RECEIPT_SKIPPED, RECEIVED,
                    REASON_NOT_ALLOWED, ROUTE_MISMATCH, SENDER_DENIED, SIG_INVALID, TARGET_NOT_FOUND, UNSUPPORTED)
from .config import MailConfig
from .fetcher import RawMail
from .mail_store import MailStore
from .parser import (ParsedCommand, ParsedMail, parse_command_body, parse_mime, pick_image_attachment,
                     subject_in_scope, title_line_regex)
from .routes import MailRoute, RouteTable
from .sign import args_digest, command_canonical, receipt_canonical, sign_header, verify
from .templates import scope_subject_prefixes

log = logging.getLogger("qtrade.mail.ingest")

IDEM_KEY_MAX = 128          # 02 `idempotency` 的 CHECK:改写加前缀后仍须 ≤128(99c C-01)


def _args_hash(args: dict[str, Any]) -> str:
    """与 ``bus.canonical_args_hash`` 同一套规范化(键序升序、无多余空白、UTF-8),两处必须同一份算法。"""
    return hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass
class PendingCommand:
    """已过三道闸、等着进总线的一条(``mail_inbox.status='ACCEPTED'``)。"""
    inbox_id: int
    command: Command
    route: MailRoute
    parsed: ParsedCommand
    reply_to: str
    in_reply_to: Optional[str]


@dataclass
class IngestResult:
    status: str
    inbox_id: Optional[int] = None
    reason: str = ""
    notes: list[str] = field(default_factory=list)


class MailIngest:
    """一个 ``mailbox_key`` 一个实例;``ingest_raw`` 同步落库到终态或 ``ACCEPTED``,``dispatch`` 异步进总线。"""

    def __init__(self, mail_store: MailStore, routes: RouteTable, cfg: MailConfig, *, store: Any,
                 catalog: Catalog, sender: Any = None, clock: Optional[Callable[[], int]] = None,
                 alerts: Any = None, secret_of: Optional[Callable[[str], str]] = None):
        self.ms = mail_store
        self.routes = routes
        self.cfg = cfg
        self.store = store
        self.catalog = catalog
        self.sender = sender
        self.clock = clock or mail_store._now
        self.alerts = alerts
        self.secret_of = secret_of or (lambda ref: "")
        self.pending: list[PendingCommand] = []

    # ------------------------------------------------------------------ 工具
    def _alert(self, code: str, *, subject: str, evidence: Optional[dict[str, Any]] = None) -> None:
        if self.alerts is not None:
            self.alerts.firing(code, subject=subject, severity=MAIL_ALERT_CODES.get(code, "warn"),
                               evidence=evidence or {})

    def scope_prefixes(self) -> list[str]:
        """§2.6.4 范围圈定的主题前缀;§2.14.3 末:从生效模板的 ``subject_pattern`` 推导,不手填。

        默认 profile 下推导结果 = ``["QTRADE指令", "转发：微信消息", "QTRADE回执"]``。
        """
        return scope_subject_prefixes([self.cfg.template_out], [self.cfg.template_in.subject_pattern])

    def _finish(self, inbox_id: int, status: str, reason: str = "", **cols: Any) -> None:
        """落状态 + **追加** ``reason``(解析备注在前、状态原因在后)。

        §2.3.3 各条容错规则写的是 ``mail_inbox.reason += …``:``subject_mismatch``/``unknown_key:<键>``/
        ``duplicate_key``/``body_truncated`` 这些备注在受理路径上也必须留住(§2.3.1「以正文为准,并在
        ``mail_inbox.reason`` 记 ``subject_mismatch`` 供人看」),后续状态原因只能**追加**、不能覆盖。
        """
        row = self.ms.inbox_get(inbox_id) or {}
        merged = ";".join([p for p in ((row.get("reason") or "").strip(), reason.strip()) if p])
        self.ms.inbox_update(inbox_id, status=status, reason=merged or None, **cols)

    def _looks_like_our_template(self, body_text: str) -> bool:
        """§2.6.4 范围圈定的另一半:正文标题行是本系统两类模板之一。"""
        title_re = title_line_regex(self.cfg.template_in.title_line)
        info_title = self.cfg.template_out.info_title
        for ln in (body_text or "").splitlines():
            s = ln.strip()
            if title_re.match(ln) or (info_title and s == info_title) or s.startswith("QTrade 回执"):
                return True
        return False

    # ------------------------------------------------------------------ 入口
    def ingest_raw(self, mail: RawMail) -> str:
        """一封邮件的完整落库判定;返回 ``mail_inbox.status``。取信侧(fetcher)只管水位与 SIZE 门。"""
        try:
            return self._ingest_raw(mail).status
        except DiskFullError:
            # 02 §2.8.8:盘满不是「毒邮件」——隔离会再写一次库、且唯一键已见后盘满解除也不会重收。
            # 原样上抛:本轮取信记失败、水位不推进,下轮自然重收。
            raise
        except Exception as e:                                         # noqa: BLE001 —— §5「毒邮件」:隔离不放过
            log.exception("mail: 落库失败 %s", e)
            self._isolate(mail, e)
            return INGEST_ERROR

    def _ingest_raw(self, mail: RawMail) -> IngestResult:
        # ── §2.5 第 1 层:唯一键已见(崩溃窗口内同一封再拉到直接跳过)
        seen = (self.ms.inbox_by_uidl(mail.mailbox, mail.uidl) if mail.uidl
                else self.ms.inbox_by_uid(mail.mailbox, mail.folder, mail.uidvalidity, mail.uid or 0))
        if seen is not None:
            return IngestResult(status=str(seen["status"]), inbox_id=int(seen["id"]))

        parsed = parse_mime(mail.raw)
        now = self.clock()
        base: dict[str, Any] = {
            "mailbox": mail.mailbox, "protocol": mail.protocol, "folder": mail.folder,
            "uid": mail.uid, "uidvalidity": mail.uidvalidity, "uidl": mail.uidl,
            "rfc_message_id": parsed.message_id_or_hash(), "from_addr": parsed.from_addr,
            "to_addrs": parsed.to_addrs[:512], "subject": parsed.subject[:512], "date_ms": parsed.date_ms,
            "received_ms": now, "size_bytes": mail.size_bytes, "body_sha256": parsed.body_sha256,
        }

        # ── §2.1 SIZE 门已在 fetcher 拦下:这里只登记元数据,**不存正文**;OVERSIZE ∈ NEVER_DELETE(R6-1)
        if mail.oversize:
            inbox_id = self.ms.inbox_insert(**base, status=OVERSIZE,
                                            reason=f"oversize:单封 {mail.size_bytes} 字节超过 max_message_bytes")
            return IngestResult(status=OVERSIZE, inbox_id=inbox_id)

        in_scope = (subject_in_scope(parsed.subject, self.scope_prefixes())
                    or self._looks_like_our_template(parsed.body_text))
        if not in_scope:
            # §2.6.4:范围外邮件只登记 uidl/subject/size,**正文不存、永不删**
            inbox_id = self.ms.inbox_insert(**base, status=OUT_OF_SCOPE, reason="out_of_scope:不是本系统的邮件")
            return IngestResult(status=OUT_OF_SCOPE, inbox_id=inbox_id)

        # ── §2.5 第 2 层:``rfc_message_id`` 唯一 —— **同一封信再被拉到**(UIDVALIDITY 变了的全量重扫、回落期重叠窗口),
        #     直接跳过、不插第二行(列上有 UNIQUE,本来也插不进去)
        same = self.ms.inbox_by_message_id(base["rfc_message_id"])
        if same is not None:
            return IngestResult(status=str(same["status"]), inbox_id=int(same["id"]))
        # ── §2.5 第 3 层:同内容、不同 ``Message-ID`` 的**重投**(邮件网关重发、发起方点了两次发送)落 DUPLICATE
        dup = self.ms.inbox_by_body_sha(parsed.body_sha256)
        if dup is not None:
            inbox_id = self.ms.inbox_insert(**base, status=DUPLICATE,
                                            reason="duplicate:同正文哈希的重投", first_inbox_id=int(dup["id"]))
            self._maybe_replay_receipt(inbox_id, dup, parsed)
            return IngestResult(status=DUPLICATE, inbox_id=inbox_id)

        inbox_id = self.ms.inbox_insert(**base, status=RECEIVED, body_text=parsed.body_text,
                                        attachments=[a.to_json() for a in parsed.attachments])
        return self._classify(inbox_id, mail, parsed, base)

    # ------------------------------------------------------------------ 三道闸 + 解析 + 路由
    def _classify(self, inbox_id: int, mail: RawMail, parsed: ParsedMail, base: dict[str, Any]) -> IngestResult:
        # 模板判别在白名单之前:不是本模板就只登记不处理(不需要知道发件人是谁)
        pc = parse_command_body(parsed.body_text, template=self.cfg.template_in,
                                subject=parsed.subject, body_truncated=parsed.body_truncated)
        if pc.status == UNSUPPORTED:
            self._finish(inbox_id, UNSUPPORTED, pc.reason_text, template="none")
            return IngestResult(status=UNSUPPORTED, inbox_id=inbox_id, reason=pc.reason_text)

        route = self.routes.route_for_inbox_account(mail.mailbox, pc.account_id, pc.channel)
        if route is None:
            self._finish(inbox_id, ROUTE_MISMATCH, "route_unresolved:收到本封的邮箱没有可用路由")
            return IngestResult(status=ROUTE_MISMATCH, inbox_id=inbox_id)
        inb = route.inbound
        self.ms.inbox_update(inbox_id, route_id=route.id, effective_protocol=mail.protocol,
                             template="command", template_version=pc.template_version)

        # ── 闸 ①:发件人白名单(§2.2 第 1 闸)——不命中不处理、**不回执**
        allowed = {a.strip().lower() for a in inb.allowed_senders if a.strip()}
        if parsed.from_addr not in allowed:
            self._finish(inbox_id, SENDER_DENIED, "sender_denied:发件人不在 allowed_senders")
            self._alert(MAIL_SENDER_DENIED, subject=f"sender:{parsed.from_addr}",
                        evidence={"subject": parsed.subject[:200]})
            return IngestResult(status=SENDER_DENIED, inbox_id=inbox_id)

        if pc.status == PARSE_FAILED:
            self._finish(inbox_id, PARSE_FAILED, pc.reason_text)
            self._alert(MAIL_PARSE_FAILED, subject=f"inbox:{inbox_id}", evidence={"reason": pc.reason_text})
            if inb.reply_on_parse_failure:
                # §5「解析失败(模板对不上)」:白名单发件人的给回执 INVALID_ARGS(见 handoff「建议裁决 ④」)
                self._send_receipt(inbox_id, route, parsed, pc, code="INVALID_ARGS",
                                   error=f"指令邮件解析失败:{pc.reason_text}")
            return IngestResult(status=PARSE_FAILED, inbox_id=inbox_id, reason=pc.reason_text)

        short = inb.short_name_of(parsed.from_addr)
        # §2.3.1/§2.3.3:解析备注(``subject_mismatch``/``unknown_key:<键>``/``duplicate_key``/``body_truncated``)
        # **落库即写**——受理路径也要留住它们(`P-MAIL` 靠 `unknown_key` 看模板漂移的早期信号);
        # 之后每次落状态由 `_finish` 在其后**追加**原因,不覆盖。
        self.ms.inbox_update(inbox_id, req_id=pc.req_id, account_id=pc.account_id, op=pc.op,
                             reason=pc.reason_text or None)

        # ── 闸 ②:HMAC 验签(§2.3.4)。``nonce`` 列有 UNIQUE(from_addr, nonce):
        #     **查重通过之后**才写本行的 nonce,否则重放那一封自己就会撞唯一键(写在验签前 = 把 DUPLICATE_NONCE 变成落库异常)
        if inb.require_signature:
            err = self._verify_signature(inbox_id, route, parsed, pc, short)
            if err is not None:
                return err
            self.ms.inbox_update(inbox_id, sig_ok=1, nonce=pc.nonce or None)

        # ── 路由归属(§2.15.1):`账号` 必须属于该路由覆盖的账号集合
        acct = self.store.get_account(pc.account_id)
        channel = acct["channel"] if acct else pc.channel
        if not route.covers(pc.account_id, channel):
            self._finish(inbox_id, ROUTE_MISMATCH, "route_mismatch:该账号不属于收到本封的路由")
            self._send_receipt(inbox_id, route, parsed, pc, code="TARGET_NOT_FOUND",
                               error="该账号不属于收到本封邮件的路由")
            return IngestResult(status=ROUTE_MISMATCH, inbox_id=inbox_id)
        if acct is None:
            self._finish(inbox_id, TARGET_NOT_FOUND, "target_not_found:账号不存在")
            self._send_receipt(inbox_id, route, parsed, pc, code="TARGET_NOT_FOUND", error=f"账号不存在:{pc.account_id}")
            return IngestResult(status=TARGET_NOT_FOUND, inbox_id=inbox_id)
        if pc.channel and pc.channel != acct["channel"]:
            self._finish(inbox_id, DONE, "invalid_args:通道与账号实际通道不符")
            self._send_receipt(inbox_id, route, parsed, pc, code="INVALID_ARGS", error="通道与账号实际通道不符")
            return IngestResult(status=DONE, inbox_id=inbox_id)

        # ── op 与参数(§2.8:取值集合与参数名唯一来源 = 02 §3.10 能力目录)
        cap = self.catalog.get(pc.op)
        if cap is None:
            self._finish(inbox_id, PARSE_FAILED, "op_unknown:目录里没有这个 op")
            # §2.7:``MAIL_PARSE_FAILED`` 的触发 = 白名单发件人的指令邮件 `PARSE_FAILED`/`SIG_INVALID`/`EXPIRED`
            # ——`op_unknown`(§2.3.2)也是 `PARSE_FAILED`,同样要让 `P-MAIL` 看见(对方模板/目录对不上)
            self._alert(MAIL_PARSE_FAILED, subject=f"inbox:{inbox_id}", evidence={"reason": "op_unknown"})
            self._send_receipt(inbox_id, route, parsed, pc, code="INVALID_ARGS", error=f"未知操作:{pc.op}")
            return IngestResult(status=PARSE_FAILED, inbox_id=inbox_id, reason="op_unknown")

        # ── 闸 ③:allow_ops(默认 = 所有 danger=false;danger=true 须逐条显式配置)
        if pc.op not in self.catalog.expand_allow_ops(inb.allow_ops):
            reason = REASON_NOT_ALLOWED + f"op {pc.op} 不在 allow_ops"
            self._finish(inbox_id, OP_DENIED, reason)
            self._send_receipt(inbox_id, route, parsed, pc, code="FORBIDDEN", error="该操作未被允许经邮件触发")
            return IngestResult(status=OP_DENIED, inbox_id=inbox_id, reason=reason)

        args, arg_err = self._coerce_args(cap, pc)
        if arg_err is not None:
            self._finish(inbox_id, DONE, f"invalid_args:{arg_err.reason}")
            self._send_receipt(inbox_id, route, parsed, pc, code="INVALID_ARGS", error=arg_err.message)
            return IngestResult(status=DONE, inbox_id=inbox_id, reason=arg_err.reason)
        if cap.op.startswith("send_") and parsed.attachments:
            att = pick_image_attachment(parsed.attachments, args.get("image") if isinstance(args.get("image"), str) else None)
            if att is not None:
                args["image"] = {"media_ref": att.name, "sha256": att.sha256}   # §2.8:总线只传引用

        # ── 幂等键改写(C-10):`mail:{发件人短名}:{req_id}`
        idem_key = f"mail:{short or 'unknown'}:{pc.req_id}"
        if len(idem_key) > IDEM_KEY_MAX:
            self._finish(inbox_id, PARSE_FAILED, "req_id:改写后超过 128 字符")
            return IngestResult(status=PARSE_FAILED, inbox_id=inbox_id)
        self.ms.inbox_update(inbox_id, idempotency_key=idem_key)

        dup_res = self._idempotency_check(inbox_id, route, parsed, pc, idem_key, args)
        if dup_res is not None:
            return dup_res

        # ── 高危:一律 202 待确认(§2.3.6),不进总线执行
        if cap.danger:
            now = self.clock()
            # `args_digest` 与 `confirm_expires_ms` **同一事务写**(R6-22/R6-7)
            self._finish(inbox_id, CONFIRM_REQUIRED, "confirm_required:高危操作已受理,待控制台确认",
                         args_digest=args_digest(args),
                         confirm_expires_ms=now + inb.danger_confirm_ttl_s * 1000)
            self._send_receipt(inbox_id, route, parsed, pc, code=CONFIRM_REQUIRED,
                               error="已受理,待确认,请到控制台 P-MAIL 待确认列表批准", ok=False)
            return IngestResult(status=CONFIRM_REQUIRED, inbox_id=inbox_id)

        cmd = self._build_command(pc, args, idem_key, parsed, cap)
        self._finish(inbox_id, ACCEPTED, "", trace_id=cmd.trace_id)
        self.pending.append(PendingCommand(inbox_id=inbox_id, command=cmd, route=route,
                                           parsed=pc, reply_to=parsed.from_addr,
                                           in_reply_to=parsed.rfc_message_id))
        return IngestResult(status=ACCEPTED, inbox_id=inbox_id)

    # ------------------------------------------------------------------ 验签
    def _verify_signature(self, inbox_id: int, route: MailRoute, parsed: ParsedMail, pc: ParsedCommand,
                          short: Optional[str]) -> Optional[IngestResult]:
        inb = route.inbound
        if not (pc.signature and pc.timestamp and pc.nonce):
            self._finish(inbox_id, SIG_INVALID, "sig_invalid:缺少签名/时间戳/随机数", sig_ok=0)
            return IngestResult(status=SIG_INVALID, inbox_id=inbox_id)
        key = inb.hmac.get(short or "")
        if key is None:
            self._finish(inbox_id, SIG_INVALID, "sig_invalid:该发件人没有登记指令密钥", sig_ok=0)
            return IngestResult(status=SIG_INVALID, inbox_id=inbox_id)
        # 时间容差(§2.3.4):邮件是异步的,默认 600 s
        try:
            ts = datetime.fromisoformat(pc.timestamp.replace("Z", "+00:00"))
            drift = abs(self.clock() / 1000 - ts.timestamp())
        except ValueError:
            self._finish(inbox_id, PARSE_FAILED, "timestamp:不是合法 ISO 8601")
            return IngestResult(status=PARSE_FAILED, inbox_id=inbox_id)
        if drift > inb.sig_time_tolerance_s:
            self._finish(inbox_id, EXPIRED, f"expired:时间戳与 Agent 时钟差 {int(drift)} s")
            self._alert(MAIL_PARSE_FAILED, subject=f"inbox:{inbox_id}", evidence={"reason": "expired"})
            return IngestResult(status=EXPIRED, inbox_id=inbox_id)
        # 防重放:(from_addr, nonce) 唯一(同 nonce 不同 req_id 是重放攻击)
        seen = self.ms.inbox_nonce_seen(parsed.from_addr, pc.nonce)
        if seen is not None and int(seen["id"]) != inbox_id:
            self._finish(inbox_id, DUPLICATE_NONCE, "duplicate_nonce:随机数 24h 内重复")
            return IngestResult(status=DUPLICATE_NONCE, inbox_id=inbox_id)
        # ``args`` 用**解析出的原始对象**(展开形式值一律字符串),与发起方(附录 A)算的那份逐字相同;
        # 类型转型发生在验签之后的 ``_coerce_args``,不影响签名。
        canonical = command_canonical(
            req_id=pc.req_id, account_id=pc.account_id, op=pc.op, session=pc.session, args=pc.args,
            timestamp=pc.timestamp, nonce=pc.nonce,
            attachments=[{"name": a.name, "bytes": a.data} for a in parsed.attachments])
        if not verify(canonical, self.secret_of(key.secret_ref), pc.signature):
            self._finish(inbox_id, SIG_INVALID, "sig_invalid:签名不符", sig_ok=0)
            self._alert(MAIL_PARSE_FAILED, subject=f"inbox:{inbox_id}", evidence={"reason": "sig_invalid"})
            return IngestResult(status=SIG_INVALID, inbox_id=inbox_id)
        return None

    # ------------------------------------------------------------------ 参数校验(同一份 args_schema,与 HTTP 入口同一条路)
    def _coerce_args(self, cap: Any, pc: ParsedCommand) -> tuple[dict[str, Any], Optional[CommandError]]:
        props = cap.properties
        args: dict[str, Any] = {}
        for k, v in pc.args.items():
            if k not in props:
                if not cap.additional_properties:
                    return {}, CommandError(f"未知参数 {k}", reason="unknown_property", details=[{"pointer": f"/{k}"}])
                args[k] = v
                continue
            want = str((props[k] or {}).get("type") or "string")
            if isinstance(v, str) and want in ("integer", "number", "boolean"):
                try:
                    args[k] = (int(v) if want == "integer" else float(v) if want == "number"
                               else v.strip().lower() in ("true", "1", "是"))
                except ValueError:
                    return {}, CommandError(f"参数 {k} 类型不符", reason="type_mismatch", details=[{"pointer": f"/{k}"}])
            else:
                args[k] = v
        for req in cap.required:
            if req not in args:
                return {}, CommandError(f"缺少必填参数 {req}", reason="missing_property", details=[{"pointer": f"/{req}"}])
        return args, None

    def _build_command(self, pc: ParsedCommand, args: dict[str, Any], idem_key: str,
                       parsed: ParsedMail, cap: Any) -> Command:
        """§2.8 邮件字段 ↔ ``Command`` 字段的搬运(本表只定搬运,不定义任何 op 的语义)。"""
        inb_max = self.cfg.inbound.max_timeout_ms
        timeout = pc.timeout_ms if pc.timeout_ms is not None else 30000
        return Command(
            account_id=pc.account_id, op=pc.op, args=args, idempotency_key=idem_key,
            confirm=(pc.confirm if (pc.confirm is not None and cap.confirmable) else True),
            timeout_ms=min(timeout, inb_max),
            origin=CommandOrigin(transport="email", actor="mail:" + parsed.from_addr, ip=None),
            submitted_at_ms=self.clock())

    # ------------------------------------------------------------------ 幂等(§2.5 第 4 层)
    def _idempotency_check(self, inbox_id: int, route: MailRoute, parsed: ParsedMail, pc: ParsedCommand,
                           idem_key: str, args: dict[str, Any]) -> Optional[IngestResult]:
        # 未确认前重投仍停在同一条待确认记录(不重复开待确认,§2.3.6「幂等」行)
        row = self.ms.inbox_by_req(req_id=pc.req_id, from_addr=parsed.from_addr, exclude_id=inbox_id,
                                   status=CONFIRM_REQUIRED)
        if row is not None:
            self.ms.inbox_update(inbox_id, status=DUPLICATE, first_inbox_id=int(row["id"]),
                                 reason="duplicate:同 req_id 仍在待确认")
            return IngestResult(status=DUPLICATE, inbox_id=inbox_id)

        first = self.ms.inbox_by_req(req_id=pc.req_id, from_addr=parsed.from_addr, exclude_id=inbox_id)
        prev = self.store.idem_get(pc.account_id, idem_key)
        if first is None and prev is None:
            return None
        args_hash = _args_hash(args)
        # 「同参数」的判据 = 把**首封已验签的 body_text 重解析**出来的 args 再算一次(与 approve 同一条路,R6-22);
        # 首封行不在了(已被行清理)就退到 `idempotency.args_hash`。
        prev_hash = self._args_hash_of_row(first) if first is not None else (prev or {}).get("args_hash")
        if prev_hash is not None and prev_hash != args_hash:
            # §2.5「边界」:同 req_id 不同参数 ⇒ **两者都不执行**,回执 INVALID_ARGS
            self._finish(inbox_id, DUPLICATE, "req_id_reused_with_different_body",
                                 first_inbox_id=int(first["id"]) if first else None)
            self._send_receipt(inbox_id, route, parsed, pc, code="INVALID_ARGS",
                               error="指令ID 已被使用且内容不同,请换新 ID")
            return IngestResult(status=DUPLICATE, inbox_id=inbox_id, reason="req_id_reused_with_different_body")
        self._finish(inbox_id, DUPLICATE, "duplicate:同 req_id 同参数的重投",
                             first_inbox_id=int(first["id"]) if first else None)
        # **回执照发**(发起方重投多半是没收到回执),内容取首封的 CommandResult
        trace = (prev or {}).get("trace_id") or (first or {}).get("trace_id")
        result = self.store.get_command_result(trace) if trace else None
        self._send_receipt(inbox_id, route, parsed, pc, code="IDEMPOTENT_REPLAY",
                           message_id=((result or {}).get("data") or {}).get("message_id"), trace_id=trace)
        return IngestResult(status=DUPLICATE, inbox_id=inbox_id)

    def _args_hash_of_row(self, row: dict[str, Any]) -> Optional[str]:
        """重解析首封的 ``body_text`` 得到它当初的 args(参数来源只有这一处,不另存一份快照)。"""
        pc = parse_command_body(row.get("body_text") or "", template=self.cfg.template_in,
                                subject=row.get("subject") or "")
        if not pc.ok:
            return None
        cap = self.catalog.get(pc.op)
        if cap is None:
            return None
        args, err = self._coerce_args(cap, pc)
        return None if err is not None else _args_hash(args)

    def _maybe_replay_receipt(self, inbox_id: int, first: dict[str, Any], parsed: ParsedMail) -> None:
        """第 2/3 层去重命中时的回执:仍按「重投多半是没收到回执」补一封(§2.5 第 4 层同义)。"""
        if self.sender is None or not first.get("req_id"):
            return
        route = self.routes.lookup(None, None)
        if route is None:
            return
        pc = ParsedCommand(ok=True, req_id=str(first.get("req_id") or ""),
                           account_id=str(first.get("account_id") or ""), op=str(first.get("op") or ""))
        self._send_receipt(inbox_id, route, parsed, pc, code="IDEMPOTENT_REPLAY", trace_id=first.get("trace_id"))

    # ------------------------------------------------------------------ 回执(§2.4.2)
    def _send_receipt(self, inbox_id: int, route: MailRoute, parsed: ParsedMail, pc: ParsedCommand, *,
                      code: str, ok: Optional[bool] = None, error: Optional[str] = None,
                      source: Optional[str] = None, message_id: Optional[str] = None,
                      cost_ms: int = 0, trace_id: Optional[str] = None, suffix: str = "") -> Optional[int]:
        if self.sender is None:
            return None
        retryable, needs_human = RESULT_CODES.get(code, (False, False))
        ok_flag = ok if ok is not None else code in ("OK", "DELIVERED", "IDEMPOTENT_REPLAY")
        now = self.clock()
        executed_at = iso8601(now)
        values = {
            "指令ID": pc.req_id or "-", "账号": pc.account_id or "-", "操作": pc.op or "-",
            "会话": pc.session or "-", "送达状态": code, "结果": "成功" if ok_flag else "失败",
            "确认方式": source or "-", "消息ID": message_id or "-", "耗时毫秒": str(cost_ms),
            "追踪ID": trace_id or "-", "错误": error or "-",
            "可重试": "是" if retryable else "否", "需人工": "是" if needs_human else "否",
            "执行时间": executed_at, "签名": "-",
        }
        short = route.inbound.short_name_of(parsed.from_addr)
        key = route.inbound.hmac.get(short or "")
        if key is not None:
            canonical = receipt_canonical(req_id=pc.req_id, account_id=pc.account_id, op=pc.op,
                                          delivery_status=code, trace_id=trace_id or "-", executed_at=executed_at)
            values["签名"] = sign_header(canonical, self.secret_of(key.secret_ref))
        ctx = {"template_version": self.cfg.template_version, "account_id": pc.account_id,
               "op": pc.op, "req_id": pc.req_id, "result_code": code}
        return self.sender.enqueue_receipt(route=route, to_addr=parsed.from_addr, ctx=ctx, values=values,
                                           inbox_id=inbox_id, in_reply_to=parsed.rfc_message_id,
                                           suffix=suffix, now_ms=now)

    def send_receipt_for_result(self, item: PendingCommand, result: CommandResult, *, suffix: str = "") -> None:
        """``CommandResult`` → 回执(§2.8 末:``code`` → ``送达状态``、``ok`` → ``结果``、``source`` → ``确认方式``)。"""
        parsed_stub = ParsedMail(rfc_message_id=item.in_reply_to, from_addr=item.reply_to, to_addrs="",
                                 subject="", date_ms=None, body_text="")
        self._send_receipt(item.inbox_id, item.route, parsed_stub, item.parsed, code=result.code, ok=result.ok,
                           error=(result.error.message if result.error else None), source=result.source,
                           message_id=(result.data or {}).get("message_id"), cost_ms=result.cost_ms,
                           trace_id=result.trace_id, suffix=suffix)

    # ------------------------------------------------------------------ 进总线
    async def dispatch(self, bus: Any) -> list[CommandResult]:
        """把 ``ACCEPTED`` 的逐条投总线;拿到 ``CommandResult`` 后落 ``DONE`` 并发回执。

        D-2(§2.8 末):账号在登录阶段时 bus 立即回 ``LOGIN_REQUIRED``(``needs_human``),**不排队**;
        回执马上发出(``需人工：是``),``mail_inbox.status=DONE``。
        """
        out: list[CommandResult] = []
        items, self.pending = self.pending, []
        for item in items:
            result = await bus.submit(item.command)
            self._finish(item.inbox_id, DONE, f"done:{result.code}",
                         command_id=result.trace_id, trace_id=result.trace_id)
            if item.command.confirm is False:
                # §2.3.5:`确认=false` 或发起方不要回执 ⇒ RECEIPT_SKIPPED
                self._finish(item.inbox_id, RECEIPT_SKIPPED)
            else:
                self.send_receipt_for_result(item, result)
            out.append(result)
        return out

    # ------------------------------------------------------------------ §5 毒邮件隔离
    def _isolate(self, mail: RawMail, err: Exception) -> None:
        """照 ibquote ``process_uid``:第 1 次抛出(暂态给一次免费重试);第 2 次落 ``INGEST_ERROR`` 留痕行并推进水位;

        第 3 次 → ``MAIL_WATERMARK_STALLED`` 告警。计数落库 ``fail_count``(进程重启不归零)。
        """
        row = (self.ms.inbox_by_uidl(mail.mailbox, mail.uidl) if mail.uidl
               else self.ms.inbox_by_uid(mail.mailbox, mail.folder, mail.uidvalidity, mail.uid or 0))
        if row is None:
            self.ms.inbox_insert(
                mailbox=mail.mailbox, protocol=mail.protocol, folder=mail.folder, uid=mail.uid,
                uidvalidity=mail.uidvalidity, uidl=mail.uidl,
                rfc_message_id=f"ingest-error-{mail.mailbox}-{mail.folder}-{mail.uid or mail.uidl}",
                from_addr="", subject="", received_ms=self.clock(), size_bytes=mail.size_bytes,
                body_sha256=f"ingest-error-{mail.uid or mail.uidl}", status=INGEST_ERROR,
                reason=f"ingest_error:{type(err).__name__}", fail_count=1)
            return
        n = self.ms.inbox_fail_bump(int(row["id"]))
        self._finish(int(row["id"]), INGEST_ERROR, f"ingest_error:{type(err).__name__}")
        if n >= 3:
            self._alert(MAIL_WATERMARK_STALLED, subject=f"mailbox:{mail.mailbox}",
                        evidence={"fail_count": n, "uid": mail.uid, "uidl": mail.uidl})
