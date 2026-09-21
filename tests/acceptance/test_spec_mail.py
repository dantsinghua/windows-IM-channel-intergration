"""按设计文档写的验收用例 —— 邮件摆渡(第五批代码)。

🔴 断言只依据规格,不按实现反推:
- 06 §2.1(IMAP/POP3 两条收信循环、SIZE 门、SMTP 退避/限速/死信)、§2.1.1(IMAP → POP3 回落 E-1)
- 06 §2.2(三道闸)、§2.3.1~§2.3.6(主题/正文/容错表/签名规范串/失败落库/高危双钥双通道)
- 06 §2.4.1a(邮件头注入防护,基线 §11.17 ⑥ [HDRSAN])、§2.4.2(回执)、§2.4.3(出站触发门槛)
- 06 §2.5(去重四层与幂等边界)、§2.6(清理判据/三种触发/NEVER_DELETE 五处门/协议差异/归档滚动)
- 06 §2.7(MAIL_* 告警码表)、§2.8(邮件键 → Command 搬运表)、§2.9.3(邮件游标键)
- 06 §2.14(模板 12 占位符与锁定)、§2.15(路由三级查找)、§3.1(三表列)、§3.2(端点语义)、§7(配置默认值)
- 02 §2.2.9 mail、§3.1 mail_* DDL、§3.4.5 #56~#68d 端点语义

每条 docstring 写清规格条款号;断言的是规格说的可观测结果(`mail_inbox.status`/`reason` 取值、
库里几行、`mail_outbox` 队列态、告警码与 `state`、`cursors` 水位、服务器上的邮件还在不在)。
夹具来自本文件(邮件侧有自己的假 IMAP/POP3/SMTP 后端),`clock` 复用 tests/conftest.py 的可拨时钟。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage

import pytest

from qtrade_agent.mail import codes as C
from qtrade_agent.mail import headers as H
from qtrade_agent.mail import parser as P
from qtrade_agent.mail import sign as S
from qtrade_agent.mail import templates as TPL
from qtrade_agent.mail.backends import (FakeImap, FakePop3, FakeSmtp, MailAuthError, MailConnectError,
                                        MailTransientError, SmtpPermanentError, SmtpTemporaryError)
from qtrade_agent.mail.catalog import Catalog
from qtrade_agent.mail.cleanup import MailCleanup
from qtrade_agent.mail.config import (InboundTemplateConfig, MailConfig, MailFallbackConfig, MailHmacKey,
                                      MailInboundConfig, MailOutboundConfig, OutboundTemplateConfig)
from qtrade_agent.mail.mail_store import MailStore
from qtrade_agent.mail.routes import MailRoute, RouteTable, mailbox_key
from qtrade_agent.mail.service import MailService
from qtrade_agent.models import CommandError, CommandResult
from qtrade_agent.store import Store

from tests.conftest import Clock  # 可拨时钟(同首批验收)

# ────────────────────────────────────────────────────────────── 常量与规格自抄的参考实现

SECRET = "S3CRET-ops"                 # 发件人 ops 的指令签名钥(vault://mail/hmac/cmd/ops)
SENDER = "ops@corp.example"           # allowed_senders 里唯一的白名单地址
SHORT = "ops"                         # [mail.inbound.hmac] 的键名 = 发件人短名(§2.3.4)
BOT = "bot@corp.example"
IMAP_HOST = "imap.corp.example"
SMTP_HOST = "smtp.corp.example"
MAILBOX = f"{IMAP_HOST}/{BOT}"        # §2.15.2 mailbox_key = lower(host)+"/"+lower(user)
CST = timezone(timedelta(hours=8))


def spec_canonical(*, req_id, account_id, op, session, args, timestamp, nonce, attach_sha="-"):
    """06 §2.3.4 规范串**逐字照抄**:9 行、`\\n` 分隔、无尾随换行、UTF-8。

    只签「决定做什么」的字段——`确认`/`超时`/`通道` 不签;`session` 用原文未归一、缺省空串;
    `args` 先 canonical_json(键排序、无空白、ensure_ascii=False)再 sha256;无附件写 `-`。
    """
    body = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "\n".join([
        "v1", req_id, account_id, op, session,
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
        timestamp, nonce, attach_sha,
    ])


def spec_sign(canonical: str, secret: str = SECRET) -> str:
    """06 §2.3.4:`signature = hex(HMAC-SHA256(key=secret, msg=canonical(UTF-8)))`。"""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def spec_receipt_canonical(*, req_id, account_id, op, delivery_status, trace_id, executed_at):
    """06 §2.4.2 末:回执规范串 = `"v1" \\n req_id \\n account_id \\n op \\n 送达状态 \\n 追踪ID \\n 执行时间(原文)`。"""
    return "\n".join(["v1", req_id, account_id, op, delivery_status, trace_id, executed_at])


def iso_of(ms: int) -> str:
    """06 §2.3.2「时间戳」:ISO 8601 带时区(本系统一律 +08:00,§2.4.1 消息时间同款)。"""
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ────────────────────────────────────────────────────────────── 造邮件(发起方视角,照 06 附录 A)

def build_command_mail(*, clock, req_id="20260919-ops-0001", account_id="qd01", op="read_messages",
                       session="", args=None, channel=None, confirm=None, timeout=None,
                       nonce="8f1c0e2a6b4d", timestamp=None, from_addr=SENDER, subject=None,
                       signature=None, secret=SECRET, title_line="QTrade 指令 v1",
                       greeting=(), extra_lines=(), attachments=(), body_override=None,
                       html_only=False, message_id=None, sign_fields=None):
    """按 06 §2.3.1 主题 + §2.3.2 正文 + §2.3.4 签名造一封指令邮件(发起方参考实现,附录 A)。"""
    args = {} if args is None else args
    ts = timestamp if timestamp is not None else iso_of(clock())
    lines = list(greeting) + [title_line, ""]
    lines.append(f"指令ID：{req_id}")
    lines.append(f"账号：{account_id}")
    if channel is not None:
        lines.append(f"通道：{channel}")
    lines.append(f"操作：{op}")
    if session:
        lines.append(f"会话：{session}")
    if args:
        lines.append("参数：" + json.dumps(args, ensure_ascii=False))
    if confirm is not None:
        lines.append(f"确认：{confirm}")
    if timeout is not None:
        lines.append(f"超时：{timeout}")
    lines += list(extra_lines)
    lines.append(f"时间戳：{ts}")
    lines.append(f"随机数：{nonce}")
    if signature is None:
        f = sign_fields or {}
        merged = dict(args)
        if session:
            merged["session"] = session          # §2.3.2:`args`(与 `会话` 合并,`会话` 优先)
        attach_sha = "-"
        if attachments:
            blob = b"".join(data for _, data in sorted(attachments, key=lambda a: a[0]))
            attach_sha = hashlib.sha256(blob).hexdigest()
        signature = spec_sign(spec_canonical(
            req_id=f.get("req_id", req_id), account_id=f.get("account_id", account_id),
            op=f.get("op", op), session=f.get("session", session), args=f.get("args", merged),
            timestamp=f.get("timestamp", ts), nonce=f.get("nonce", nonce), attach_sha=attach_sha), secret)
    lines.append(f"签名：hmac-sha256={signature}")
    body = body_override if body_override is not None else "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject if subject is not None else f"QTRADE指令 v1 [{account_id}] {op} {req_id}"
    msg["From"] = from_addr
    msg["To"] = BOT
    msg["Message-ID"] = message_id or f"<{req_id}.{nonce}@sender.example>"
    msg["Date"] = "Fri, 19 Sep 2025 08:00:00 +0800"
    if html_only:
        html = "<html><body>" + "<br>".join(body.split("\n")) + "</body></html>"
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg.as_bytes()


def plain_mail(*, subject="日常问候", body="随便写点什么", from_addr="someone@else.example",
               message_id=None):
    """不在清理范围、也不是本系统模板的普通邮件(§2.6.4 范围圈定 ⇒ OUT_OF_SCOPE)。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = BOT
    msg["Message-ID"] = message_id or f"<plain.{abs(hash(subject + body)) % 10**8}@else.example>"
    msg.set_content(body)
    return msg.as_bytes()


# ────────────────────────────────────────────────────────────── 夹具

class FakeEvents:
    """只记录 `Events.emit` 的调用(告警断言用;告警 payload 结构见 06 §2.7 / 基线 §7.5)。"""

    def __init__(self):
        self.emitted: list[dict] = []

    def emit(self, event, *, payload=None, account_id=None, now_ms=None, **kw):
        self.emitted.append({"event": event, "payload": payload or {}, "account_id": account_id})

    def codes(self, state=None):
        return [e["payload"].get("code") for e in self.emitted
                if state is None or e["payload"].get("state") == state]

    def of(self, code, state=None):
        return [e for e in self.emitted if e["payload"].get("code") == code
                and (state is None or e["payload"].get("state") == state)]


class FakeBus:
    """总线的最小面:`submit(Command) -> CommandResult`(06 §2.8 只定义「邮件怎么进出总线」)。"""

    def __init__(self, result=None):
        self.submitted = []
        self.result = result

    async def submit(self, command):
        self.submitted.append(command)
        if callable(self.result):
            return self.result(command)
        return self.result or CommandResult(ok=True, code="DELIVERED", trace_id="01J8TRACE0001",
                                            cost_ms=1650, source="get_msg")


CAPS = [
    {"op": "read_messages", "kind": "read", "danger": False, "confirmable": False,
     "args_schema": {"type": "object", "properties": {"session": {"type": "string"},
                                                      "limit": {"type": "integer"}},
                     "additionalProperties": False}},
    {"op": "send_text", "kind": "write", "danger": False, "confirmable": True,
     "args_schema": {"type": "object", "properties": {"session": {"type": "string"},
                                                      "text": {"type": "string"}},
                     "required": ["text"], "additionalProperties": False}},
    {"op": "send_image", "kind": "write", "danger": False, "confirmable": True,
     "args_schema": {"type": "object", "properties": {"session": {"type": "string"},
                                                      "image": {"type": "object"}},
                     "additionalProperties": False}},
    {"op": "account_stop", "kind": "admin", "danger": True, "confirmable": False,
     "args_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"op": "messages_purge", "kind": "admin", "danger": True, "confirmable": False,
     "args_schema": {"type": "object", "properties": {"before": {"type": "string"}},
                     "additionalProperties": False}},
]


def make_cfg(**kw):
    """06 §7 `[mail]` 全段;只把「邮箱地址」这类环境值填上,其余一律用规格默认值。"""
    cfg = MailConfig(enabled=True)
    cfg.inbound.host, cfg.inbound.user = IMAP_HOST, BOT
    cfg.inbound.allowed_senders = [SENDER]
    cfg.inbound.hmac = {SHORT: MailHmacKey(short_name=SHORT, from_addr=SENDER,
                                           secret_ref=f"vault://mail/hmac/cmd/{SHORT}")}
    cfg.outbound.host, cfg.outbound.user = SMTP_HOST, BOT
    cfg.outbound.from_addr = BOT
    cfg.outbound.recipients = [SENDER]
    for k, v in kw.items():
        seg, _, key = k.partition("__")
        setattr(getattr(cfg, seg) if key else cfg, key or seg, v)
    return cfg


class Rig:
    def __init__(self, *, tmp_path, clock, cfg=None, caps=None, disk="ok", store=None):
        self.clock = clock
        self.cfg = cfg or make_cfg()
        self.cfg.cleanup.archive_dir = str(tmp_path / "archive")
        # store 给了 = 与 HTTP 夹具(`mail_api`)共用同一个 agent.db,端点读到的就是本 rig 写下的行
        self.store = store if store is not None else Store(":memory:", clock=clock).open()
        self.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
        self.store.ensure_account("qq03", "qq", state="running", login_mode="qrcode", self_uid="415011447")
        self.imap, self.pop3, self.smtp = FakeImap(), FakePop3(), FakeSmtp()
        self.events = FakeEvents()
        from qtrade_agent.alerts import Alerts
        self.alerts = Alerts(self.events, clock=clock)
        self.disk = disk
        self.svc = MailService(
            self.store, self.cfg, catalog=Catalog.from_dicts(caps or CAPS), clock=clock,
            alerts=self.alerts, secret_of=lambda ref: SECRET,
            imap_factory=lambda r: self.imap, pop3_factory=lambda r: self.pop3,
            smtp_factory=lambda r: self.smtp, disk_state=lambda: self.disk)
        self.ms = MailStore(self.store)

    # ---- 便利查询(只读库,断言用)
    def rows(self, sql, *params):
        return [dict(r) for r in self.store.con.execute(sql, params).fetchall()]

    def inbox(self, **where):
        sql = "select * from mail_inbox"
        if where:
            sql += " where " + " and ".join(f"{k}=?" for k in where)
        return self.rows(sql + " order by id", *where.values())

    def one_inbox(self):
        rs = self.inbox()
        assert len(rs) == 1, f"期望 mail_inbox 恰 1 行,实得 {len(rs)}"
        return rs[0]

    def outbox(self, **where):
        sql = "select * from mail_outbox"
        if where:
            sql += " where " + " and ".join(f"{k}=?" for k in where)
        return self.rows(sql + " order by id", *where.values())

    def cleanup_logs(self):
        return self.rows("select * from mail_cleanup_log order by id")

    def cursor(self, kind, owner=f"mail:{MAILBOX}"):
        r = self.store.con.execute("select value, value_int from cursors where owner=? and kind=?",
                                   (owner, kind)).fetchone()
        return (None, None) if r is None else (r[0], r[1])

    def ingest_imap(self, raw, *, folder="INBOX", size=None):
        uid = self.imap.add(raw, folder=folder, size=size)
        self.svc.fetch_once()
        return uid

    def ingest_pop3(self, uidl, raw, *, size=None):
        self.pop3.add(uidl, raw, size=size)
        self.svc.fetch_once()
        return uidl


@pytest.fixture
def rig(tmp_path, clock):
    r = Rig(tmp_path=tmp_path, clock=clock)
    yield r
    r.store.close()


def danger_rig(tmp_path, clock):
    """06 §2.2 第 3 闸:把 `account_stop`/`messages_purge` 两个 `danger=true` 的 op 逐条写进 `allow_ops`。"""
    cfg = make_cfg()
    cfg.inbound.allow_ops = ["*", "account_stop", "messages_purge"]
    return Rig(tmp_path=tmp_path, clock=clock, cfg=cfg)


@pytest.fixture
def pop_rig(tmp_path, clock):
    cfg = make_cfg()
    cfg.inbound.protocol = "pop3"
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=cfg)
    yield r
    r.store.close()


# ────────────────────────────────────────────────────────────── HTTP 夹具(端点出参断言用)
#
# 端点出参只能在 HTTP 层断:规格(02 §3.4)定的是 `GET /api/v1/mail/...` 的响应,不是服务对象某个方法的返回值。
# 装配 = 一个真 AgentApp(鉴权、信封、trace_id 注入全走真链路)+ 本文件的 MailService(假 IMAP/POP3/SMTP),
# 两者共用 agent.db,端点读到的就是 rig 写下的行。

API_P = "/api/v1"                               # 00 §10 前缀(不叫 P:本文件 P 已是 mail.parser)
TOK_A = "tok-mail-console-admin"                # 级别 A(#68b 需 A;R 级端点 A 也能调,02 §3.4「高包含低」)
# 00 §6「时间(API/事件/邮件)= ISO 8601 带时区偏移」,样例 `2026-09-18T10:03:00+08:00`;
# 规格只写死「带偏移」,不写死小数秒 ⇒ 小数秒可有可无,偏移必须是 ±HH:MM。
ISO_OFFSET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")


def assert_iso_or_none(v, what):
    """00 §6:API 时间 = ISO 8601 带偏移的**字符串**;02 各端点写 `|null` 的允许 `null`。"""
    assert v is None or (isinstance(v, str) and ISO_OFFSET_RE.match(v)), f"{what} 应为 ISO 8601 带偏移或 null,实得 {v!r}"


def assert_iso(v, what):
    assert isinstance(v, str) and ISO_OFFSET_RE.match(v), f"{what} 应为 ISO 8601 带偏移的字符串,实得 {v!r}"


class MailApi:
    def __init__(self, tmp_path, clock, *, cfg=None):
        from qtrade_agent.app import AgentApp
        from qtrade_agent.config import AgentConfig, ApiConfig
        data_dir = tmp_path / "agent-data"
        data_dir.mkdir(exist_ok=True)
        self.agent = AgentApp(AgentConfig(api=ApiConfig()), db_path=str(tmp_path / "agent.db"), clock=clock,
                              data_dir=str(data_dir)).open()
        self.agent.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_A)
        self.rig = Rig(tmp_path=tmp_path, clock=clock, cfg=cfg, store=self.agent.store)
        self.agent.mail = self.rig.svc              # 端点经 agent.mail 取服务(未装配时 503,02 §3.4 状态映射)
        self.api = self.agent.create_api()

    def get(self, client, path, **params):
        return client.get(f"{API_P}{path}", params=params, headers={"Authorization": f"Bearer {TOK_A}"})


@pytest.fixture
def mail_api(tmp_path, clock):
    """产出 `(MailApi, TestClient)`;用例需要特殊 `[mail]` 配置时自己 `MailApi(tmp_path, clock, cfg=…)` + `MailClient(…)`。"""
    m = MailApi(tmp_path, clock)
    with MailClient(m) as c:
        yield m, c


class MailClient:
    def __init__(self, m):
        from starlette.testclient import TestClient
        self.m = m
        self.tc = TestClient(m.api, client=("127.0.0.1", 40000))

    def __enter__(self):
        return self.tc.__enter__()

    def __exit__(self, *exc):
        try:
            try:
                self.tc.portal.call(self.m.agent.bus.close)
            except Exception:
                pass
            return self.tc.__exit__(*exc)
        finally:
            self.m.agent.store.close()


def status_payload(body):
    """取 #56 的载荷,**不在这里判信封**(信封只在 M201e 一条里判,一条用例一个行为:信封红不遮住键名红)。

    02 §3.4 通用 R6-55 定的是顶层平铺;若实现包了 `data`,这里照样取出来让键名/类型断言各自生效。"""
    assert body.get("ok") is True, body
    inner = body.get("data")
    return inner if isinstance(inner, dict) else body


def status_routes(body):
    """02 #56:**不带 `route_id`** 返回 `routes:[{route_id, channel, account_id, …下同}]`(全部启用路由)。"""
    routes = status_payload(body).get("routes")
    assert isinstance(routes, list), f"02 #56:不带 route_id 应回 `routes:[…]`,实得 {body!r}"
    return routes


def status_single(client, m, route_id):
    """02 #56:**带 `route_id`** 返回单条 `{enabled, route:{…}, inbound:{…}, outbound:{…}, cleanup:{…}}`。"""
    resp = m.get(client, "/mail/status", route_id=route_id)
    assert resp.status_code == 200, resp.text
    return status_payload(resp.json())


def first_route_id(client, m):
    """路由 id 从 #56 列表形态本身取(`routes[].route_id`),不碰服务内部对象。"""
    resp = m.get(client, "/mail/status")
    assert resp.status_code == 200, resp.text
    routes = status_routes(resp.json())
    assert routes, "默认配置下应有一条启用的全局路由(02 §2.2.9 / 06 §2.15)"
    return routes[0]["route_id"]


# ══════════════════════════════════════════════════ 一、取信循环与 SIZE 门(06 §2.1 / §5 / §2.9.3)


def test_M01_imap_fetch_advances_watermark(rig):
    """06 §2.1 `run_cycle()`:过 SIZE 门后才 `FETCH BODY.PEEK[]`、`last_uid = uid` 推进水位;
    §2.9.3 `mail:<mailbox>` 的 `imap_uid:<folder>` 每文件夹一条(`value_int = last_uid`、`value = uidvalidity`)。"""
    uid = rig.ingest_imap(build_command_mail(clock=rig.clock))
    row = rig.one_inbox()
    assert row["protocol"] == "imap" and row["folder"] == "INBOX" and row["uid"] == uid
    assert row["body_text"], "过了 SIZE 门的邮件必须取正文并落 body_text(§3.1)"
    value, value_int = rig.cursor("imap_uid:INBOX")
    assert value_int == uid and str(value) == str(rig.imap.uidvalidity)


def test_M02_imap_search_filters_le_watermark(rig):
    """06 §2.1「新邮件」判据:`UID SEARCH {last_uid+1}:*` 后**再过滤 > last_uid**
    (IMAP 规定 `n:*` 至少返回最大 UID,会把最后一封重复给回来)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="A-1", nonce="n1"))
    rig.svc.fetch_once()          # 再跑一轮:同一封不得被重复登记
    rig.svc.fetch_once()
    assert len(rig.inbox()) == 1


def test_M03_imap_oversize_only_meta_no_body(rig):
    """06 §2.1 `run_cycle()` / §5(R4-18):`size > max_message_bytes` ⇒ 只登记元数据(uid/size/发件人/时间)、
    **绝不 FETCH BODY.PEEK[]、不 ingest**,`status=OVERSIZE`、`body_text` 为空、`size_bytes` 有值。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock), size=rig.cfg.inbound.max_message_bytes + 1)
    row = rig.one_inbox()
    assert row["status"] == C.OVERSIZE
    assert not row["body_text"]
    assert row["size_bytes"] == rig.cfg.inbound.max_message_bytes + 1


def test_M04_imap_oversize_alerts_once(rig):
    """06 §2.3.5「只告警一次」:OVERSIZE 分支照样推进水位,下一轮 `UID SEARCH` 搜不到它;
    §2.7 `MAIL_MSG_OVERSIZE`(`subject=inbox:<id>`、warn)只出一条 `firing`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock), size=99 * 1024 * 1024)
    rig.svc.fetch_once()
    rig.svc.fetch_once()
    firing = rig.events.of(C.MAIL_MSG_OVERSIZE, "firing")
    assert len(firing) == 1 and firing[0]["payload"]["severity"] == "warn"
    assert len(rig.inbox()) == 1


def test_M05_imap_oversize_stays_in_place(rig):
    """06 §2.6.5 门 ③ / §5:`OVERSIZE ∈ NEVER_DELETE` ⇒ 进入终态**不 `UID MOVE`/`COPY`、不 `+FLAGS (\\Deleted)`**,
    留原夹原位,`mail_inbox.folder/uid` 不变。"""
    uid = rig.ingest_imap(build_command_mail(clock=rig.clock), size=99 * 1024 * 1024)
    assert rig.imap.moved == [] and rig.imap.expunged == []
    assert uid in rig.imap.uids_in("INBOX")
    row = rig.one_inbox()
    assert row["folder"] == "INBOX" and row["uid"] == uid and row["deleted_ms"] is None


def test_M06_imap_scans_junk_folder(rig):
    """06 §2.1 表「多文件夹」:`folders = ["INBOX","Junk"]` —— 垃圾箱也扫
    (ibquote 教训:反垃圾网关把摆渡邮件判成垃圾,不扫 Junk 就漏单)。"""
    rig.imap.add(build_command_mail(clock=rig.clock, req_id="J-1", nonce="nj1"), folder="Junk")
    rig.svc.fetch_once()
    rows = rig.inbox()
    assert len(rows) == 1 and rows[0]["folder"] == "Junk"
    assert rig.cursor("imap_uid:Junk")[1] == rows[0]["uid"]


def test_M07_imap_max_retr_per_round(tmp_path, clock):
    """06 §2.1 末:`max_retr_per_round` 限单轮取信封数,**剩余下一轮继续**。"""
    cfg = make_cfg()
    cfg.inbound.max_retr_per_round = 2
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=cfg)
    for i in range(5):
        r.imap.add(build_command_mail(clock=clock, req_id=f"R-{i}", nonce=f"n{i}"))
    r.svc.fetch_once()
    assert len(r.inbox()) == 2
    r.svc.fetch_once()
    assert len(r.inbox()) == 4
    r.store.close()


def test_M08_imap_uidvalidity_change_rescans_without_dup(rig):
    """06 §2.1 表「水位失效」/ §5:`UIDVALIDITY` 变了 → 水位清零全量重扫,
    **靠 §2.5 的 Message-ID / 正文哈希挡重复**(不新增行)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="V-1", nonce="nv1"))
    before = len(rig.inbox())
    rig.imap.uidvalidity += 1
    rig.svc.fetch_once()
    assert len(rig.inbox()) == before


def test_M09_pop3_uidl_seen_skips(pop_rig):
    """06 §2.1 POP3 循环:`if uidl in mail_inbox.uidl: continue`——已见即跳过(UIDL 不保证单调,不能当水位)。"""
    pop_rig.ingest_pop3("U001", build_command_mail(clock=pop_rig.clock, req_id="P-1", nonce="np1"))
    pop_rig.svc.fetch_once()
    rows = pop_rig.inbox()
    assert len(rows) == 1 and rows[0]["uidl"] == "U001" and rows[0]["protocol"] == "pop3"


def test_M10_pop3_oversize_no_retr(pop_rig):
    """06 §2.1 POP3 循环(R5-6):先 `LIST` 只取字节数,超 `max_message_bytes` ⇒
    `record_meta` + `status=OVERSIZE` + 告警,**绝不 RETR、不 ingest**。"""
    pop_rig.ingest_pop3("U002", build_command_mail(clock=pop_rig.clock), size=99 * 1024 * 1024)
    row = pop_rig.one_inbox()
    assert row["status"] == C.OVERSIZE and not row["body_text"]
    assert "U002" in pop_rig.pop3.uidls(), "OVERSIZE ∈ NEVER_DELETE:本轮不 DELE、往后每轮也不 DELE"


def test_M11_pop3_oversize_alerts_once(pop_rig):
    """06 §2.3.5:POP3 下 OVERSIZE 先 `record_meta(uidl=…)`,下一轮凭「已见」跳过 ⇒ 只告警一次。"""
    pop_rig.ingest_pop3("U003", build_command_mail(clock=pop_rig.clock), size=99 * 1024 * 1024)
    pop_rig.svc.fetch_once()
    pop_rig.svc.fetch_once()
    assert len(pop_rig.events.of(C.MAIL_MSG_OVERSIZE, "firing")) == 1


def test_M12_pop3_stat_recorded(pop_rig):
    """06 §2.1 POP3 循环:`stat_count, stat_octets = pop.stat()` 记进 mail monitor;
    §2.9.3 `pop3_stat` = `{"count":…,"octets":…}`(只给监控/容量估算)。"""
    raw = build_command_mail(clock=pop_rig.clock, req_id="S-1", nonce="ns1")
    pop_rig.ingest_pop3("U004", raw)
    value, _ = pop_rig.cursor("pop3_stat")
    stat = json.loads(value)
    assert stat["count"] == 1 and stat["octets"] == len(raw)


def test_M13_out_of_scope_registers_meta_only(rig):
    """06 §2.3.5 / §2.6.4:不在清理范围也不是模板的邮件 ⇒ `OUT_OF_SCOPE`,**只登记元数据、不存正文**。"""
    rig.ingest_imap(plain_mail())
    row = rig.one_inbox()
    assert row["status"] == C.OUT_OF_SCOPE
    assert not row["body_text"], "§3.1 `body_text`:范围内邮件才存;OUT_OF_SCOPE 空"


def test_M14_out_of_scope_never_moved(rig):
    """06 §2.6.5 门 ③ + §2.3.5 `NEVER_DELETE`:`OUT_OF_SCOPE` 的邮件既不搬进 `processed_folder`、
    也不在原夹留 `\\Deleted`——不是我们的信,一律不碰。"""
    uid = rig.ingest_imap(plain_mail())
    assert rig.imap.moved == [] and uid in rig.imap.uids_in("INBOX")


def test_M16_disk_critical_pauses_fetch(rig):
    """06 §2.6.10 `critical`(< 1 GB):本轮取信循环**不 RETR/不 FETCH BODY、不 ingest**,
    邮件留服务器不删、**绝不推进 UID/UIDL 水位**;告警 `MAIL_PAUSED_DISK_FULL`(crit)。"""
    rig.disk = "critical"
    uid = rig.imap.add(build_command_mail(clock=rig.clock, req_id="D-1", nonce="nd1"))
    rig.svc.fetch_once()
    assert rig.inbox() == []
    assert rig.cursor("imap_uid:INBOX")[1] in (None, 0)
    assert uid in rig.imap.uids_in("INBOX")
    firing = rig.events.of(C.MAIL_PAUSED_DISK_FULL, "firing")
    assert firing and firing[0]["payload"]["severity"] == "crit"


def test_M17_disk_recovery_resumes_from_watermark(rig):
    """06 §2.6.10「恢复即补收」:磁盘回到 `high` 以上后,下一轮从**未推进的水位**继续,
    `critical` 期间积压的邮件按正常增量流程收进来,不丢单。"""
    rig.disk = "critical"
    rig.imap.add(build_command_mail(clock=rig.clock, req_id="D-2", nonce="nd2"))
    rig.svc.fetch_once()
    rig.disk = "ok"
    rig.svc.fetch_once()
    assert len(rig.inbox()) == 1


# ══════════════════════════════════════════════════ 二、IMAP → POP3 自动回落(06 §2.1.1,E-1)


def _fallback_cfg():
    cfg = make_cfg()
    cfg.inbound.protocol = "imap"
    cfg.inbound.fallback.host = "pop.corp.example"
    return cfg


def test_M18_fallback_after_three_connect_failures(tmp_path, clock):
    """06 §2.1.1:`connect_refused/connect_timeout/tls_handshake_failed/IMAP not enabled` 连续
    `fallback.after_failures`(默认 3)次 ⇒ `state = POP3_FALLBACK` + 告警 `MAIL_PROTOCOL_FALLBACK`(warn,firing)。"""
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=_fallback_cfg())
    r.imap.raise_on_connect = MailConnectError("连接被拒", kind="connect_refused")
    for _ in range(2):
        r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "imap", "两次还不到 after_failures=3"
    r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "pop3"
    firing = r.events.of(C.MAIL_PROTOCOL_FALLBACK, "firing")
    assert len(firing) == 1 and firing[0]["payload"]["severity"] == "warn"
    r.store.close()


def test_M19_auth_failure_never_falls_back(tmp_path, clock):
    """06 §2.1.1:认证失败 ⇒ `MAIL_AUTH_FAILED`(crit)、**不回落**
    (密码在 IMAP 上错,在 POP3 上一样错;回落只会把一次告警变成两次)。"""
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=_fallback_cfg())
    r.imap.raise_on_connect = MailAuthError("AUTHENTICATIONFAILED")
    for _ in range(5):
        r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "imap"
    assert r.events.of(C.MAIL_AUTH_FAILED, "firing")
    assert r.events.of(C.MAIL_PROTOCOL_FALLBACK, "firing") == []
    r.store.close()


def test_M20_transient_error_not_counted(tmp_path, clock):
    """06 §2.1.1 `else` 分支:其它错误走 §5「网络断」(sleep(10) 重连),**不计入回落计数**。"""
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=_fallback_cfg())
    r.imap.raise_on_connect = MailTransientError("socket.error")
    for _ in range(5):
        r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "imap"
    assert r.events.of(C.MAIL_PROTOCOL_FALLBACK, "firing") == []
    r.store.close()


def test_M21_no_fallback_host_only_alerts(tmp_path, clock):
    """06 §2.1.1:`host` 空 ⇒ **不回落只告警** `MAIL_INBOUND_STALLED`(回落目标不猜、要人填)。"""
    cfg = make_cfg()
    cfg.inbound.fallback.host = ""
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=cfg)
    r.imap.raise_on_connect = MailConnectError("连接被拒", kind="connect_refused")
    for _ in range(4):
        r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "imap"
    assert r.events.of(C.MAIL_PROTOCOL_FALLBACK, "firing") == []
    assert r.events.of(C.MAIL_INBOUND_STALLED, "firing")
    r.store.close()


def test_M22_status_shows_protocol_configured_and_active(tmp_path, clock):
    """回落中:`GET /mail/status?route_id=` 的 `inbound.protocol_configured="imap"`、`protocol_active="pop3"`、
    `fallback={since_at, reason}`,`since_at` 为 ISO 8601 带偏移的字符串。

    出处:02 §3.4.5 #56 响应列 `inbound:{protocol_configured, protocol_active, fallback:{since_at, reason}|null, …}`
    与同行「E-1:`protocol_active≠protocol_configured` 即处于 IMAP→POP3 回落」;回落触发条件见 06 §2.1.1
    (连续 `fallback.after_failures`=3 次 connect 类失败);时间类型见 00 §6「时间(API)= ISO 8601 带时区偏移」。
    ⚠️ 键名以 02 为准(00 §4「端点全集以 02 §3.4/§3.6 为准」;总控 2026-09-21 裁决「#56 以 02 为准」)——
    06 §2.1.1/§2.7/§3.2 与 §8b M5 行仍写 `configured_protocol/effective_protocol/fallback_since`,那是 06 未同步的旧写法,
    本用例**不**断旧键名(旧版曾断它,等于给实现现状背书)。"""
    m = MailApi(tmp_path, clock, cfg=_fallback_cfg())
    with MailClient(m) as c:
        m.rig.imap.raise_on_connect = MailConnectError("超时", kind="connect_timeout")
        for _ in range(3):
            m.rig.svc.fetch_once()
        inb = status_single(c, m, first_route_id(c, m))["inbound"]
        assert inb["protocol_configured"] == "imap"
        assert inb["protocol_active"] == "pop3"
        fb = inb["fallback"]
        assert isinstance(fb, dict), f"回落中 `fallback` 应为对象 {{since_at, reason}},实得 {fb!r}"
        assert {"since_at", "reason"} <= set(fb)
        assert_iso(fb["since_at"], "inbound.fallback.since_at")


def test_M22b_status_no_fallback_is_null(tmp_path, clock):
    """未回落:`protocol_active == protocol_configured == "imap"`、`fallback` 为 `null`。

    出处:02 §3.4.5 #56 `fallback:{since_at, reason}|null`;E-1「两者不等即回落」⇒ 相等时不在回落、无回落对象。"""
    m = MailApi(tmp_path, clock, cfg=_fallback_cfg())
    with MailClient(m) as c:
        m.rig.ingest_imap(build_command_mail(clock=m.rig.clock, req_id="NF-1", nonce="nf1"))
        inb = status_single(c, m, first_route_id(c, m))["inbound"]
        assert inb["protocol_configured"] == "imap" and inb["protocol_active"] == "imap"
        assert inb["fallback"] is None


def test_M23_recheck_switches_back_and_resolves(tmp_path, clock):
    """06 §2.1.1:回落期间每 `fallback.recheck_min`(默认 30 分钟)试连一次 IMAP,成功 ⇒ `state = IMAP`、
    同键 `state=resolved`;IMAP 水位从上次 `(uidvalidity, last_uid)` 继续。"""
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=_fallback_cfg())
    r.imap.raise_on_connect = MailConnectError("连接被拒", kind="connect_refused")
    for _ in range(3):
        r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "pop3"
    r.imap.raise_on_connect = None
    clock.advance(31 * 60 * 1000)
    r.svc.fetch_once()
    assert r.svc.fetchers[MAILBOX].effective_protocol == "imap"
    assert r.events.of(C.MAIL_PROTOCOL_FALLBACK, "resolved")
    r.store.close()


def test_M24_configured_pop3_never_upgrades(pop_rig):
    """06 §2.1.1 末:**只有配置协议是 `imap` 时才有回落**;配置为 `pop3` 就是 pop3,
    不存在「POP3 → IMAP 升级」(用户明确要 POP3 时不替他做主)。"""
    for _ in range(5):
        pop_rig.svc.fetch_once()
    assert pop_rig.svc.fetchers[MAILBOX].effective_protocol == "pop3"
    assert pop_rig.events.of(C.MAIL_PROTOCOL_FALLBACK, "firing") == []


# ══════════════════════════════════════════════════ 三、信任模型三道闸(06 §2.2 / §2.3.5 / §6)


def test_M25_sender_not_in_whitelist_denied_no_receipt(rig):
    """06 §2.2 第 1 闸:`From` 不在 `allowed_senders` ⇒ `SENDER_DENIED`,**不回执**
    (否则等于给陌生人一个「这里有系统」的探针,§6.8)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, from_addr="attacker@evil.example"))
    assert rig.one_inbox()["status"] == C.SENDER_DENIED
    assert rig.outbox() == [], "非白名单一律不回执"


def test_M26_sender_denied_alert(rig):
    """06 §2.7:非白名单发件人发来 `QTRADE指令` 前缀邮件 ⇒ `MAIL_SENDER_DENIED`(`subject=sender:<addr>`,warn)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, from_addr="attacker@evil.example"))
    firing = rig.events.of(C.MAIL_SENDER_DENIED, "firing")
    assert firing and firing[0]["payload"]["subject"] == "sender:attacker@evil.example"
    assert firing[0]["payload"]["severity"] == "warn"


def test_M27_bad_signature_invalid_no_receipt(rig):
    """06 §2.2 第 2 闸 / §2.3.4:签名不符 ⇒ `SIG_INVALID`,不回执;`sig_ok=0`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, signature="0" * 64))
    row = rig.one_inbox()
    assert row["status"] == C.SIG_INVALID and row["sig_ok"] == 0
    assert rig.outbox() == []


def test_M28_good_signature_marks_sig_ok(rig):
    """06 §3.1 `sig_ok`:0/1/NULL(未验);按 §2.3.4 规范串签出的邮件验签通过 ⇒ `sig_ok=1`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock))
    assert rig.one_inbox()["sig_ok"] == 1


def test_M29_require_signature_false_skips_verify(tmp_path, clock):
    """06 §7 `require_signature = true`;`false` 仅测试模式(§6.1)——关掉后无签名也能过第 2 闸。"""
    cfg = make_cfg()
    cfg.inbound.require_signature = False
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=cfg)
    r.ingest_imap(build_command_mail(clock=clock, signature="deadbeef"))
    assert r.one_inbox()["status"] != C.SIG_INVALID
    r.store.close()


def test_M30_star_allow_ops_excludes_danger(rig):
    """06 §2.2 第 3 闸 / §2.8:`["*"]` 语义 = **全部 `danger=false`**,不含 danger 项
    (R2-1 / 基线 §11.17 [MAILOPS] ②;`danger=true` 只有被逐字写进 `allow_ops` 才生效)。"""
    cat = Catalog.from_dicts(CAPS)
    expanded = cat.expand_allow_ops(["*"])
    assert "read_messages" in expanded and "send_text" in expanded
    assert "account_stop" not in expanded and "messages_purge" not in expanded


def test_M31_danger_op_not_allowed_is_op_denied(rig):
    """06 §2.2 第 3 闸 / §2.3.5 前缀表:不在 `allow_ops` ⇒ `OP_DENIED` + `reason` 前缀 **`NOT_ALLOWED:`**。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="account_stop"))
    row = rig.one_inbox()
    assert row["status"] == C.OP_DENIED
    assert row["reason"].startswith(C.REASON_NOT_ALLOWED)


def test_M32_op_denied_still_replies(rig):
    """06 §2.2 第 3 闸:`OP_DENIED` **回执**(发件人已验签,是真的)——与前两闸的「不回执」相反。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="account_stop"))
    outs = rig.outbox(kind="receipt")
    assert len(outs) == 1 and "QTRADE回执" in outs[0]["subject"]


def test_M33_explicit_danger_op_passes_gate3(tmp_path, clock):
    """06 §2.2 第 3 闸:`danger=true` 的能力**逐条显式配置**才可经邮件触发
    (写进 `allow_ops` 后不再是 `OP_DENIED`,转由 §2.3.6 的 202 待确认接手)。"""
    r = danger_rig(tmp_path, clock)
    r.ingest_imap(build_command_mail(clock=clock, op="account_stop"))
    assert r.one_inbox()["status"] == C.CONFIRM_REQUIRED
    r.store.close()


def test_M34_unknown_op_is_parse_failed(rig):
    """06 §2.3.2「操作」行:能力目录里没有的 op ⇒ `PARSE_FAILED: op_unknown`
    (E-2:不得另起一套中文/别名指令名,`操作：发消息` 这类一律拒)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="发消息"))
    row = rig.one_inbox()
    assert row["status"] == C.PARSE_FAILED and "op_unknown" in row["reason"]


def test_M35_unknown_account_target_not_found(rig):
    """06 §2.3.2「账号」行:`account_id` 不存在 ⇒ 回执 `TARGET_NOT_FOUND`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, account_id="qd99"))
    assert rig.one_inbox()["status"] == C.TARGET_NOT_FOUND


def test_M36_channel_mismatch_invalid_args(rig):
    """06 §2.3.2「通道」行 / §2.8 搬运表:`通道` 给了且与账号实际通道不符 ⇒ `INVALID_ARGS`
    (§2.3.5 的 status 集合里没有 `INVALID_ARGS` 这个终态,它是回执「送达状态」的取值)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, channel="wechat"))
    outs = rig.outbox(kind="receipt")
    assert len(outs) == 1 and "INVALID_ARGS" in outs[0]["body_text"]
    assert "invalid_args" in (rig.one_inbox()["reason"] or "").lower()


# ══════════════════════════════════════════════════ 四、解析器的容错表(06 §2.3.1~§2.3.3 / §2.14.4)

IN_TPL = InboundTemplateConfig()          # 06 §7 [mail.template.inbound] 默认值


def parse(body, **kw):
    """§2.3.2 正文解析 + §2.3.3 容错表(直接对解析器断言,不经取信)。"""
    return P.parse_command_body(body, template=IN_TPL, **kw)


def cmd_body(*extra, title="QTrade 指令 v1"):
    base = [title, "", "指令ID：R-1", "账号：qd01", "操作：read_messages"]
    return "\n".join(base + list(extra) + ["时间戳：2026-09-19T08:00:00+08:00", "随机数：abcd1234",
                                           "签名：hmac-sha256=" + "0" * 64])


def test_M37_subject_prefix_stripped(rig):
    """06 §2.3.1:邮件客户端可能给主题加 `回复：`/`Re:`/`转发：`/`FW:` 前缀,
    判别时先剥掉这些前缀再看是否以 `QTRADE指令` 开头。"""
    assert P.strip_subject_prefixes("Re: 回复：QTRADE指令 v1 [qd01] x y") == "QTRADE指令 v1 [qd01] x y"
    assert P.strip_subject_prefixes("FW: QTRADE回执 v1") == "QTRADE回执 v1"


def test_M38_title_line_missing_is_unsupported(rig):
    """06 §2.3.2 标题行:找不到 ⇒ `UNSUPPORTED`(不是本模板,整封只登记不处理)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, title_line="随便一行不是标题"))
    assert rig.one_inbox()["status"] == C.UNSUPPORTED


def test_M39_unknown_template_version(rig):
    """06 §2.3.2 标题行 / §2.14.4 `accepted_versions`:未知版本 ⇒ `PARSE_FAILED: template_version`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, title_line="QTrade 指令 v9"))
    row = rig.one_inbox()
    assert row["status"] == C.PARSE_FAILED and "template_version" in row["reason"]


def test_M40_halfwidth_colon_accepted():
    """06 §2.3.3:全角/半角冒号 `[：:]` 都认。"""
    got = parse(cmd_body().replace("：", ":"))
    assert got.req_id == "R-1" and got.account_id == "qd01" and got.op == "read_messages"


def test_M41_quoted_lines_ignored():
    """06 §2.3.3:引用回复(每行 `>` 开头)整行忽略。"""
    got = parse(cmd_body("> 引用的旧正文", "> 操作：account_stop"))
    assert got.op == "read_messages"


def test_M42_stop_at_original_message_marker():
    """06 §2.3.3:标题行之后再出现转发/引用分隔符即停止(`-----Original Message-----` 等)。"""
    got = parse(cmd_body("-----Original Message-----", "操作：account_stop"))
    assert got.op == "read_messages"


def test_M43_stop_at_signature_dashes():
    """06 §2.3.3:遇 `-- ` 或 `--` 单独一行停止(签名档)。"""
    got = parse(cmd_body("--", "操作：account_stop"))
    assert got.op == "read_messages"


def test_M44_unknown_key_noted_not_continuation():
    """06 §2.3.3:未知键(模板漂移/拼错)记 `reason += unknown_key:<键>`,**不**当成正文续行。"""
    got = parse(cmd_body("会话：张三-固收", "莫名其妙键：值"))
    assert any(n.startswith("unknown_key:") for n in got.notes)
    assert got.session == "张三-固收", "未知键不得把前一个键的值吞掉"


def test_M45_duplicate_key_takes_first():
    """06 §2.3.3:同一键出现两次 ⇒ 取第一次,记 `duplicate_key`。"""
    got = parse(cmd_body("会话：第一个", "会话：第二个"))
    assert got.session == "第一个"
    assert any("duplicate_key" in n for n in got.notes)


def test_M46_greeting_before_title_ignored(rig):
    """06 §2.3.3:标题行之前的内容全部忽略(也是 §2.4 扩展块放在标题行之前的依据)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, greeting=("您好，麻烦帮忙跑一下：", "")))
    assert rig.one_inbox()["status"] not in (C.UNSUPPORTED, C.PARSE_FAILED)


def test_M47_body_truncated_at_64kb():
    """06 §2.3.3:正文 > 64KB ⇒ 只解析前 64KB,记 `body_truncated`。"""
    assert P.BODY_MAX_BYTES == 64 * 1024
    got = parse(cmd_body(), body_truncated=True)
    assert any("body_truncated" in n for n in got.notes)


def test_M48_html_only_body_decoded(rig):
    """06 §2.1「取信解码」:腾讯企业邮箱程序发信只有 `text/html`,**HTML 路径不是兜底而是主路径**——
    剥标签后仍要解析成功。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, html_only=True, req_id="H-1", nonce="nh1"))
    row = rig.one_inbox()
    assert row["status"] not in (C.UNSUPPORTED, C.PARSE_FAILED)
    assert row["req_id"] == "H-1"


def test_M50_args_expanded_form():
    """06 §2.3.2:按 op 展开形式 `参数.{键}：{值}`;值一律字符串,类型按 schema 转型。"""
    got = parse(cmd_body("参数.text：今日 3M 报价 1.52"))
    assert got.args == {"text": "今日 3M 报价 1.52"}


def test_M51_args_expanded_multiline():
    """06 §2.3.2:`参数.text` 同样支持续行到下一个已知键(多行报价文本就是这么写)。"""
    got = parse(cmd_body("参数.text：1Y 1.70", "2Y 1.80"))
    assert got.args["text"].splitlines()[0] == "1Y 1.70"
    assert "2Y 1.80" in got.args["text"]


def test_M52_args_both_forms_takes_json():
    """06 §2.3.2:两种形式混用 ⇒ **取 JSON 形式**并记 `reason=args_both_forms`。"""
    got = parse(cmd_body('参数：{"text":"来自JSON"}', "参数.text：来自展开"))
    assert got.args.get("text") == "来自JSON"
    assert any("args_both_forms" in n for n in got.notes)


def test_M53_args_not_object_parse_failed():
    """06 §2.3.2:`参数` 非对象 ⇒ `PARSE_FAILED: args_json`。"""
    got = parse(cmd_body("参数：[1,2,3]"))
    assert got.status == C.PARSE_FAILED and "args_json" in got.reason_text


def test_M55_aliases_normalized():
    """06 §2.14.4:键名别名归一到规范键名再走 §2.3.3 容错(`req_id`/`account`/`op`/`session` …)。"""
    body = "\n".join(["QTrade 指令 v1", "", "req_id：A-9", "account：qd01", "op：read_messages",
                      "session：张三", "timestamp：2026-09-19T08:00:00+08:00", "nonce：zzzz1111",
                      "signature：hmac-sha256=" + "0" * 64])
    got = parse(body)
    assert (got.req_id, got.account_id, got.op, got.session) == ("A-9", "qd01", "read_messages", "张三")


def test_M56_canonical_keys_are_eleven():
    """06 §2.3.2 正文表:规范键名固定 11 项(标题行另算),别名只增不改(§2.14.4)。"""
    assert P.CANONICAL_KEYS == ("指令ID", "账号", "通道", "操作", "会话", "参数", "确认",
                                "超时", "时间戳", "随机数", "签名")


def test_M57_timeout_capped(rig):
    """06 §2.3.2「超时」:毫秒整数,缺省 30000,上限 `max_timeout_ms = 120000`(§7)。"""
    got = parse(cmd_body("超时：999999"))
    assert got.timeout_ms == 999999 or got.timeout_ms == 120000   # 解析层取原值,封顶在搬运层
    assert rig.cfg.inbound.max_timeout_ms == 120000


def test_M58_attachment_sniffed_by_magic():
    """06 §2.1「附件识别」:`Content-Type` 不可信,判定顺序 **字节头 → MIME 声明 → 扩展名**。"""
    assert P.sniff_mime(b"\xff\xd8\xff\xe0rest", declared="application/octet-stream", name="a.png") == "image/jpeg"
    assert P.sniff_mime(b"\x89PNG\r\n\x1a\nrest", declared="application/octet-stream") == "image/png"
    assert P.sniff_mime(b"%PDF-1.7") == "application/pdf"
    assert P.sniff_mime(b"\x00\x01\x02") == "application/octet-stream"


def test_M59_attachment_picked_by_suffix_match():
    """06 §2.3.2「附件」:`参数.image` 写附件文件名,按「相等 / 互为后缀」宽松匹配
    (客户端常给文件名加前缀);没写文件名时取**第一个按字节头判定为图片的附件**。"""
    a = P.Attachment(name="ATT00001_quote.png", data=b"\x89PNG\r\n\x1a\nx", mime_sniffed="image/png",
                     sha256="x")
    b = P.Attachment(name="note.txt", data=b"hello", mime_sniffed="text/plain", sha256="y")
    assert P.pick_image_attachment([b, a], "quote.png") is a
    assert P.pick_image_attachment([b, a], None) is a


def test_M60_scope_prefix_derived_from_pattern():
    """06 §2.14.3 末:`scope_subject_prefix` **从生效模板的 `subject_pattern` 推导**
    (取第一个占位符之前的字面量前缀),不再手填。"""
    assert P.scope_prefix_from_pattern("QTRADE指令 {template_version} [{account_id}] {op} {req_id}") == "QTRADE指令"
    assert P.scope_prefix_from_pattern("转发：微信消息 [{session_name}] {summary}") == "转发：微信消息"


def test_M61_default_scope_prefixes(rig):
    """06 §7 注 / §2.14.3 末:默认 profile 下推导结果仍为
    `["QTRADE指令", "转发：微信消息", "QTRADE回执"]`(02 引用本注释)。"""
    assert sorted(rig.svc.ingest.scope_prefixes()) == sorted(["QTRADE指令", "转发：微信消息", "QTRADE回执"])


# ══════════════════════════════════════════════════ 五、签名:HMAC 规范串(06 §2.3.4 / §2.4.2 末)


def test_M62_canonical_is_nine_lines():
    """06 §2.3.4:规范串固定 9 行、`\\n` 分隔、**无尾随换行**、UTF-8。"""
    c = S.command_canonical(req_id="R", account_id="qd01", op="send_text", session="s",
                            args={"text": "x"}, timestamp="T", nonce="N")
    assert c.count("\n") == 8 and not c.endswith("\n")
    assert c == spec_canonical(req_id="R", account_id="qd01", op="send_text", session="s",
                               args={"text": "x"}, timestamp="T", nonce="N")


def test_M63_canonical_json_is_sorted_compact_unicode():
    """06 §2.3.4:`canonical_json` = 键排序、无空白、`ensure_ascii=False`、UTF-8。"""
    assert S.canonical_json({"b": 1, "a": "中"}) == '{"a":"中","b":1}'


def test_M64_no_attachment_writes_dash():
    """06 §2.3.4 末行:`sha256_hex(附件1字节 || 附件2字节 …)`,**无附件写 `-`**。"""
    assert S.attachments_sha256([]) == "-"


def test_M65_attachments_hashed_in_name_order():
    """06 §2.3.4 末行:附件按**文件名字典序**拼接后整体 sha256(换图即验签失败)。"""
    atts = [{"name": "b.png", "bytes": b"BBB"}, {"name": "a.png", "bytes": b"AAA"}]
    want = hashlib.sha256(b"AAABBB").hexdigest()
    assert S.attachments_sha256(atts) == want


def test_M66_confirm_timeout_channel_not_signed(rig):
    """06 §2.3.4:只签「决定做什么」的字段——`确认`/`超时`/`通道` **不签**
    (改它们改变不了动作对象与内容,签了反而让客户端改个默认值就验不过)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", session="张三",
                                       args={"text": "hi"}, confirm="true", timeout=45000,
                                       channel="qidian", req_id="N-1", nonce="nn1"))
    assert rig.one_inbox()["sig_ok"] == 1


def test_M67_session_signed_as_is(rig):
    """06 §2.3.4:`session(原文,未归一;缺省空串)` —— 含全角空格的会话名按原文进规范串。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", session="张三　固收",
                                       args={"text": "hi"}, req_id="N-2", nonce="nn2"))
    assert rig.one_inbox()["sig_ok"] == 1


def test_M68_expired_beyond_tolerance(rig):
    """06 §2.3.2「时间戳」/ §2.3.4:与 Agent 时钟差 > `sig_time_tolerance_s`(默认 600)⇒ `EXPIRED`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="E-1", nonce="ne1",
                                       timestamp=iso_of(rig.clock() - 601_000)))
    assert rig.one_inbox()["status"] == C.EXPIRED


def test_M69_within_tolerance_ok(rig):
    """06 §2.3.4:容差之内(邮件投递常有几分钟延迟)照常受理。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="E-2", nonce="ne2",
                                       timestamp=iso_of(rig.clock() - 590_000)))
    assert rig.one_inbox()["status"] != C.EXPIRED


def test_M70_duplicate_nonce_is_replay(rig):
    """06 §2.3.4「防重放」:`(from_addr, nonce)` 唯一;同发件人 24h 内重复 ⇒ `DUPLICATE_NONCE`,不回执(§5)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="Z-1", nonce="same-nonce"))
    before = len(rig.outbox())
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="Z-2", nonce="same-nonce"))
    rows = rig.inbox()
    assert rows[-1]["status"] == C.DUPLICATE_NONCE
    assert len(rig.outbox()) == before, "重放不回执(§5:签名不符/过期/重放都不执行不回执)"


def test_M71_args_digest_is_first16(rig):
    """06 §3.1 / R6-22:`args_digest` = `hex(sha256(规范化 args JSON))` 的**前 16 位**(小写 hex)。"""
    args = {"text": "买 3M", "session": "张三"}
    want = hashlib.sha256(S.canonical_json(args).encode("utf-8")).hexdigest()[:16]
    assert S.args_digest(args) == want and len(want) == 16


def test_M72_receipt_canonical_is_seven_lines():
    """06 §2.4.2 末:回执规范串 7 行,用与指令签名**同一把** `vault://mail/hmac/cmd/<短名>` 密钥。"""
    c = S.receipt_canonical(req_id="R", account_id="qd01", op="send_text",
                            delivery_status="DELIVERED", trace_id="01J8", executed_at="T")
    assert c == spec_receipt_canonical(req_id="R", account_id="qd01", op="send_text",
                                       delivery_status="DELIVERED", trace_id="01J8", executed_at="T")
    assert c.count("\n") == 6 and not c.endswith("\n")


def test_M73_verify_accepts_prefixed_hex():
    """06 §2.3.2「签名」:字段值形如 `hmac-sha256=<hex>`(64 位十六进制);验签常量时间比较(§6.1)。"""
    c = "v1\nR\nqd01\nsend_text\n\nx\nT\nN\n-"
    sig = spec_sign(c)
    assert S.verify(c, SECRET, sig) and S.verify(c, SECRET, "hmac-sha256=" + sig)
    assert S.verify(c, SECRET, "hmac-sha256=" + sig.upper())
    assert not S.verify(c, SECRET, "hmac-sha256=" + "f" * 64)


def test_M74_vault_key_paths():
    """06 §2.3.4「密钥分发」/ §6.1:指令钥 `vault://mail/hmac/cmd/<短名>`;
    确认钥 `vault://mail/hmac/confirm` 服务端单钥、从不下发。"""
    assert S.VAULT_CMD_KEY_FMT.format(short_name="ops") == "vault://mail/hmac/cmd/ops"
    assert S.VAULT_CONFIRM_KEY == "vault://mail/hmac/confirm"


# ══════════════════════════════════════════════════ 六、去重与幂等四层(06 §2.5 / §3.1)


def test_M75_layer1_uid_unique(rig):
    """06 §2.5 第 1 层:`(mailbox, protocol, folder, uidvalidity, uid)` 唯一——崩溃窗口内同一封再拉到直接跳过。"""
    raw = build_command_mail(clock=rig.clock, req_id="L1", nonce="l1")
    uid = rig.imap.add(raw)
    rig.svc.fetch_once()
    rig.ms.imap_watermark_set(f"mail:{MAILBOX}", "INBOX", uidvalidity=rig.imap.uidvalidity, last_uid=uid - 1)
    rig.svc.fetch_once()
    assert len(rig.inbox()) == 1


def test_M76_layer2_message_id_dup(rig):
    """06 §2.5 第 2 层:`rfc_message_id` 唯一(缺失退化为整封原文 sha256,**禁止拿 UID/序号当键**)。"""
    mid = "<same-mid@sender.example>"
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="L2a", nonce="l2a", message_id=mid))
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="L2b", nonce="l2b", message_id=mid))
    rows = rig.inbox()
    assert len(rows) == 1, "同 Message-ID 的第二封不落新行"


def test_M77_layer3_body_sha_dup(rig):
    """06 §2.5 第 3 层:`body_sha256` = sha256(规范化正文 ‖ 各附件 sha256);
    同内容不同 Message-ID 的重投落 `DUPLICATE`。"""
    raw1 = build_command_mail(clock=rig.clock, req_id="L3", nonce="l3", message_id="<a@x>")
    raw2 = build_command_mail(clock=rig.clock, req_id="L3", nonce="l3", message_id="<b@x>")
    rig.ingest_imap(raw1)
    rig.ingest_imap(raw2)
    rows = rig.inbox()
    assert len(rows) == 2 and rows[1]["status"] == C.DUPLICATE
    assert rows[1]["first_inbox_id"] == rows[0]["id"]


def test_M78_layer4_same_req_same_args_is_duplicate(rig):
    """06 §2.5 第 4 层:同键**同参数**命中 ⇒ 不进总线,`status=DUPLICATE`,`first_inbox_id` 指向首封;
    **回执照发**(发起方重投多半是没收到回执),内容取首封结果(`IDEMPOTENT_REPLAY`)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "同一条"},
                                       req_id="IDEM-1", nonce="i1", message_id="<i1@x>"))
    n_before = len(rig.outbox(kind="receipt"))
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "同一条"},
                                       req_id="IDEM-1", nonce="i2", message_id="<i2@x>"))
    rows = rig.inbox()
    assert rows[-1]["status"] == C.DUPLICATE and rows[-1]["first_inbox_id"] == rows[0]["id"]
    outs = rig.outbox(kind="receipt")
    assert len(outs) == n_before + 1 and "IDEMPOTENT_REPLAY" in outs[-1]["body_text"]


def test_M79_same_req_different_body_is_invalid_args(rig):
    """06 §2.5「边界」:`req_id` 相同但正文不同 ⇒ **两者都不执行**,`status=DUPLICATE` 且
    `reason=req_id_reused_with_different_body`,回执 `送达状态：INVALID_ARGS`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "第一版"},
                                       req_id="IDEM-2", nonce="j1", message_id="<j1@x>"))
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "改过的第二版"},
                                       req_id="IDEM-2", nonce="j2", message_id="<j2@x>"))
    row = rig.inbox()[-1]
    assert row["status"] == C.DUPLICATE
    assert "req_id_reused_with_different_body" in (row["reason"] or "")
    assert "INVALID_ARGS" in rig.outbox(kind="receipt")[-1]["body_text"]


def test_M80_idempotency_key_rewritten_with_short_name(rig):
    """06 §2.3.2「指令ID」/ §2.5 第 4 层 / §2.8:进总线前由 Transport 改写为 `mail:{发件人短名}:{req_id}`
    (C-10;`指令ID` 原文另落 `mail_inbox.req_id`)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "x"},
                                       req_id="20260919-ops-0007", nonce="k1"))
    item = rig.svc.ingest.pending[-1]
    assert item.command.idempotency_key == "mail:ops:20260919-ops-0007"
    assert rig.one_inbox()["req_id"] == "20260919-ops-0007"


def test_M81_idempotency_key_length_cap(rig):
    """06 §2.3.2「指令ID」:`≤64 字符`,Transport 改写加前缀后仍须 **≤128** 才过 02 `idempotency` 的 CHECK。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "x"},
                                       req_id="A" * 64, nonce="k2"))
    item = rig.svc.ingest.pending[-1]
    assert len(item.command.idempotency_key) <= 128


def test_M82_origin_transport_is_email(rig):
    """06 §2.8 搬运表:`origin.transport = "email"`、`origin.actor = "mail:" + From 地址小写`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", args={"text": "x"},
                                       req_id="O-1", nonce="k3"))
    origin = rig.svc.ingest.pending[-1].command.origin
    assert origin.transport == "email" and origin.actor == f"mail:{SENDER}"


# ══════════════════════════════════════════════════ 七、高危 op 的双钥双通道确认(06 §2.3.6 / §3.2 #68b~#68d)


def _accept_danger(r, *, op="account_stop", args=None, req_id="DG-1", nonce="dg1"):
    r.ingest_imap(build_command_mail(clock=r.clock, op=op, args=args or {}, req_id=req_id, nonce=nonce))
    return r.one_inbox()


def test_M83_danger_op_is_202_pending(tmp_path, clock):
    """06 §2.3.6「流程」:高危 op 过白名单 + 指令验签后**一律 202 待确认**
    (`mail_inbox.status=CONFIRM_REQUIRED`,**不进总线执行**)。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    assert row["status"] == C.CONFIRM_REQUIRED
    assert r.svc.ingest.pending == [], "待确认的不得进总线队列"
    r.store.close()


def test_M84_confirm_expires_ms_written(tmp_path, clock):
    """06 §2.3.6 TTL / §3.1:`confirm_expires_ms` = 受理时刻 + `danger_confirm_ttl_s * 1000`(默认 900s)。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    assert row["confirm_expires_ms"] == clock() + 900 * 1000
    r.store.close()


def test_M85_args_digest_written_with_expires(tmp_path, clock):
    """06 §3.1 / R6-22:`args_digest` = 规范化 args 的 sha256 前 16 位,**与 `confirm_expires_ms` 同一事务写**。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r, op="messages_purge", args={"before": "2026-01-01"})
    assert row["args_digest"] and row["confirm_expires_ms"]
    assert len(row["args_digest"]) == 16
    r.store.close()


def test_M86_no_confirm_nonce_or_via_columns(rig):
    """06 §3.1 / §8b M5 验收行(R6-7):v1 **不建** `confirm_nonce`/`confirm_via` 两列,
    只有 `confirm_expires_ms` 与 `args_digest`。"""
    cols = {r[1] for r in rig.store.con.execute("pragma table_info(mail_inbox)")}
    assert "confirm_nonce" not in cols and "confirm_via" not in cols
    assert {"confirm_expires_ms", "args_digest"} <= cols


def test_M87_pending_confirms_has_exactly_eight_keys(tmp_path, clock):
    """06 §3.2 #68b(R6-7 逐字):出参恰为
    `{id, op, from_addr, account_id, args_digest, created_at, expires_at, remaining_ttl_s}` 八键,
    **不含 `confirm_via`/`confirm_nonce`**。"""
    r = danger_rig(tmp_path, clock)
    _accept_danger(r)
    items = r.svc.confirms.list()
    assert len(items) == 1
    assert set(items[0]) == {"id", "op", "from_addr", "account_id", "args_digest",
                             "created_at", "expires_at", "remaining_ttl_s"}
    r.store.close()


def test_M88_remaining_ttl_computed_by_server(tmp_path, clock):
    """06 §3.2 #68b:`remaining_ttl_s = max(0, (expires_at - now)/1000)` 由**服务端**算好
    (时钟不一致会把已过期的显示成还能批)。"""
    r = danger_rig(tmp_path, clock)
    _accept_danger(r)
    clock.advance(300 * 1000)
    assert r.svc.confirms.list()[0]["remaining_ttl_s"] == 600
    r.store.close()


def _danger_cfg():
    cfg = make_cfg()
    cfg.inbound.allow_ops = ["*", "account_stop", "messages_purge"]
    return cfg


def test_M87b_pending_confirms_http_eight_keys_iso_times(tmp_path, clock):
    """HTTP 层 `GET /mail/pending-confirms`:每项恰 R6-7 八键;`created_at`/`expires_at` 为 ISO 8601 带偏移,
    `expires_at - created_at` = `danger_confirm_ttl_s`(默认 900s);`remaining_ttl_s` 为整数。

    出处:02 §3.4.5 #68b「出参逐字定死(R6-7)」`[{id, op, from_addr, account_id, args_digest, created_at, expires_at,
    remaining_ttl_s}]`,`created_at = received_ms 序列化`、`expires_at = confirm_expires_ms 序列化`(= 受理时刻 +
    `danger_confirm_ttl_s`×1000,默认 900s);序列化口径 = 00 §6「时间(API)ISO 8601 带时区偏移」。
    信封:数组无法平铺 ⇒ 按 02 §3.4 通用 `{ok:true, data:[…]}` 取 `data`。
    ⚠️ 规格空白:`id` 的类型(02 只写「= `mail_inbox.id`」,没像 #58 那样写「出字符串」)⇒ 不断类型;
    是否分页、`next_cursor` 回不回,02 #68b 未写(R6-64 末「#68b 出参视图留下一批」)⇒ 不断。"""
    m = MailApi(tmp_path, clock, cfg=_danger_cfg())
    with MailClient(m) as c:
        _accept_danger(m.rig)
        resp = m.get(c, "/mail/pending-confirms")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is True and isinstance(body.get("data"), list), body
        assert len(body["data"]) == 1
        it = body["data"][0]
        assert set(it) == {"id", "op", "from_addr", "account_id", "args_digest",
                           "created_at", "expires_at", "remaining_ttl_s"}
        assert_iso(it["created_at"], "created_at")
        assert_iso(it["expires_at"], "expires_at")
        delta = datetime.fromisoformat(it["expires_at"]) - datetime.fromisoformat(it["created_at"])
        assert delta == timedelta(seconds=900)
        assert isinstance(it["remaining_ttl_s"], int) and not isinstance(it["remaining_ttl_s"], bool)


def test_M89_approve_from_email_is_403(tmp_path, clock):
    """06 §2.3.6「🔴 承重墙」/ §3.2 #68c(基线 §11.17 ③):approve 只接受 `transport ∈ {console, local}`,
    **`transport=email` 一律 403** ——否则失陷邮箱自发自批,双钥双通道整条作废。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    out = r.svc.confirms.approve(row["id"], actor=f"mail:{SENDER}", transport="email")
    assert out.ok is False and out.http_status == 403
    assert r.svc.confirms.list(), "被拒的调用不得改变待确认状态"
    r.store.close()


def test_M90_reject_from_email_is_403(tmp_path, clock):
    """06 §3.2 #68d:同 approve——`transport=email` 一律 `403 FORBIDDEN`。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    out = r.svc.confirms.reject(row["id"], actor=f"mail:{SENDER}", transport="email")
    assert out.ok is False and out.http_status == 403
    assert r.svc.confirms.list()
    r.store.close()


def test_M91_approve_console_turns_accepted(tmp_path, clock):
    """06 §2.3.6 R6-29 终态逐字:**approve 抢到 → `ACCEPTED`**(再进总线执行)。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    out = r.svc.confirms.approve(row["id"], actor="token:console#1", transport="console")
    assert out.ok and out.http_status in (200, 202)
    assert r.inbox()[0]["status"] == C.ACCEPTED
    r.store.close()


def test_M92_reject_writes_actor_in_reason(tmp_path, clock):
    """06 §2.3.6 R6-34 / §3.2 #68d 逐字:`reason = 'REJECTED:' || :actor`,
    `:actor` = 发起请求的控制台操作者,与审计 `actor` 同值;终态 `OP_DENIED`。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    r.svc.confirms.reject(row["id"], actor="token:console#7", transport="console")
    got = r.inbox()[0]
    assert got["status"] == C.OP_DENIED and got["reason"] == "REJECTED:token:console#7"
    r.store.close()


def test_M93_approve_after_expiry_is_409(tmp_path, clock):
    """06 §2.3.6 R6-20 原子 claim:`rowcount==1` 才继续,否则 **`409 CONFIRM_EXPIRED`**;
    行仍 `CONFIRM_REQUIRED` 而时刻已过时端点**顺手**置 `CONFIRM_EXPIRED`,**不等 reaper**。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    clock.advance(901 * 1000)
    out = r.svc.confirms.approve(row["id"], actor="token:console#1", transport="console")
    assert out.ok is False and out.http_status == 409 and out.code == C.CONFIRM_EXPIRED
    assert r.inbox()[0]["status"] == C.CONFIRM_EXPIRED
    r.store.close()


def test_M94_expires_ms_kept_after_expiry(tmp_path, clock):
    """06 §3.1 R6-19(以 02 为准,逐字):`confirm_expires_ms`「一经写入即保留,approve/reject/过期都不清」。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    want = row["confirm_expires_ms"]
    clock.advance(901 * 1000)
    r.svc.confirms.approve(row["id"], actor="token:console#1", transport="console")
    assert r.inbox()[0]["confirm_expires_ms"] == want
    r.store.close()


def test_M95_expires_ms_kept_after_reject(tmp_path, clock):
    """06 §3.1 R6-19:reject 之后同样不清 `confirm_expires_ms`(其它 status 下它只是历史留痕)。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    want = row["confirm_expires_ms"]
    r.svc.confirms.reject(row["id"], actor="token:console#2", transport="console")
    assert r.inbox()[0]["confirm_expires_ms"] == want
    r.store.close()


def test_M96_args_tampered_rejected(tmp_path, clock):
    """06 §2.3.6 R6-22 / §3.2 #68c:approve 执行前**重解析已验签的 `body_text`** 并复算 digest,
    不等即拒绝执行 ⇒ `500 INTERNAL` + `error.reason='args_digest_mismatch'`,
    该行置 `OP_DENIED` + `reason` 前缀 **`ARGS_TAMPERED:`**(§2.3.5 前缀三值表)。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r, op="messages_purge", args={"before": "2026-01-01"})
    r.store.con.execute("update mail_inbox set args_digest='0123456789abcdef' where id=?", (row["id"],))
    r.store.con.commit()
    out = r.svc.confirms.approve(row["id"], actor="token:console#1", transport="console")
    assert out.ok is False and out.http_status == 500 and out.reason == "args_digest_mismatch"
    got = r.inbox()[0]
    assert got["status"] == C.OP_DENIED and got["reason"].startswith(C.REASON_ARGS_TAMPERED)
    r.store.close()


def test_M97_reaper_expires_only_pending(tmp_path, clock):
    """02 §3.1 `mail_confirm_reaper` 规范 SQL:只把 `status='CONFIRM_REQUIRED' AND confirm_expires_ms <= now`
    的行转 `CONFIRM_EXPIRED`;幂等(转过之后不再命中)。"""
    r = danger_rig(tmp_path, clock)
    _accept_danger(r)
    assert r.svc.reap_confirms() == 0, "未到期不动"
    clock.advance(901 * 1000)
    assert r.svc.reap_confirms() == 1
    assert r.inbox()[0]["status"] == C.CONFIRM_EXPIRED
    assert r.svc.reap_confirms() == 0, "幂等:已转终态不再重复过期"
    r.store.close()


def test_M98_audit_action_names(tmp_path, clock):
    """06 §2.3.6「审计」/ §8b M5 行(R6-25 审计名定案):approve/reject/expire 三个动作各一条,
    动作名 `mail.pending_confirm.approved|rejected|expired|args_mismatch`,**不记 `confirm_via`**。"""
    assert (C.AUDIT_CONFIRM_APPROVED, C.AUDIT_CONFIRM_REJECTED, C.AUDIT_CONFIRM_EXPIRED,
            C.AUDIT_CONFIRM_ARGS_MISMATCH) == ("mail.pending_confirm.approved", "mail.pending_confirm.rejected",
                                               "mail.pending_confirm.expired", "mail.pending_confirm.args_mismatch")
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    r.svc.confirms.approve(row["id"], actor="token:console#1", transport="console")
    audits = r.rows("select action, actor, detail_json from audit_log where action like 'mail.pending_confirm%'")
    assert [a["action"] for a in audits] == [C.AUDIT_CONFIRM_APPROVED]
    assert audits[0]["actor"] == "token:console#1"
    assert "confirm_via" not in audits[0]["detail_json"]
    r.store.close()


def test_M99_reaper_audit_actor_and_expired_by(tmp_path, clock):
    """06 §2.3.6「审计」R6-29 / 02 §3.1:reaper 触发 ⇒ `actor='system:scheduler'` +
    `detail_json.expired_by='reaper'`;端点顺手触发 ⇒ 操作者 + `expired_by='endpoint'`。"""
    r = danger_rig(tmp_path, clock)
    _accept_danger(r)
    clock.advance(901 * 1000)
    r.svc.reap_confirms()
    a = r.rows("select actor, detail_json from audit_log where action=?", C.AUDIT_CONFIRM_EXPIRED)[-1]
    assert a["actor"] == "system:scheduler" and json.loads(a["detail_json"])["expired_by"] == "reaper"
    r.store.close()


def test_M100_endpoint_expired_audit_by_endpoint(tmp_path, clock):
    """06 §2.3.6 R6-20/R6-29:端点顺手置 `CONFIRM_EXPIRED` 时,审计 `actor` = 发起请求的控制台操作者、
    `detail_json.expired_by='endpoint'`(不是 `system:scheduler`——是人点了一下才发现它过期的)。"""
    r = danger_rig(tmp_path, clock)
    row = _accept_danger(r)
    clock.advance(901 * 1000)
    r.svc.confirms.approve(row["id"], actor="token:console#9", transport="console")
    a = r.rows("select actor, detail_json from audit_log where action=?", C.AUDIT_CONFIRM_EXPIRED)[-1]
    assert a["actor"] == "token:console#9" and json.loads(a["detail_json"])["expired_by"] == "endpoint"
    r.store.close()


def test_M101_receipt_says_pending_confirm(tmp_path, clock):
    """06 §2.3.6「流程」末:回执告知「已受理,待确认,请到控制台 `P-MAIL` 待确认列表批准」。"""
    r = danger_rig(tmp_path, clock)
    _accept_danger(r)
    outs = r.outbox(kind="receipt")
    assert len(outs) == 1 and C.CONFIRM_REQUIRED in outs[0]["body_text"]
    r.store.close()


def test_M102_resubmit_before_confirm_keeps_one_pending(tmp_path, clock):
    """06 §2.3.6「幂等」:未确认前重投仍停在同一条待确认记录(**不重复开待确认**)。"""
    r = danger_rig(tmp_path, clock)
    _accept_danger(r, req_id="DG-9", nonce="dg9")
    r.ingest_imap(build_command_mail(clock=clock, op="account_stop", req_id="DG-9", nonce="dg9b",
                                     message_id="<dg9b@x>"))
    assert len(r.svc.confirms.list()) == 1
    r.store.close()


def test_M103_danger_confirm_via_default_console(rig):
    """06 §7 `[mail.inbound] danger_confirm_via = "console"`;v1 **只有 console 一个取值可用**
    (`mail_out_of_band` 是 M6+ 预留、v1 不实现)。§2.3.6「通道」。"""
    assert rig.cfg.inbound.danger_confirm_via == "console"
    assert rig.cfg.inbound.danger_confirm_ttl_s == 900


def test_M104_confirm_transports_are_console_and_local():
    """06 §3.2 #68c/#68d:approve/reject **只接受 `transport ∈ {console, local}`**。"""
    from qtrade_agent.mail.confirm import CONFIRM_TRANSPORTS
    assert set(CONFIRM_TRANSPORTS) == {"console", "local"}


# ══════════════════════════════════════════════════ 八、邮件头注入防护(06 §2.4.1a,基线 §11.17 ⑥ [HDRSAN])


class _Route:
    """`build_headers` 需要的最小路由面(§2.4.1a:收件人只来自路由)。"""

    def __init__(self, to=None, cc=None, frm=BOT):
        self._to, self._cc, self._from = to or [SENDER], cc or [], frm

    @property
    def outbound_from(self):
        return self._from

    @property
    def outbound_to(self):
        return list(self._to)

    @property
    def outbound_cc(self):
        return list(self._cc)


def test_M105_crlf_in_subject_becomes_space(rig):
    """06 §2.4.1a 测试用例表第 1 行:群名 = `财报群\\r\\nBcc: attacker@x.com` ⇒
    `Subject` 里 `\\r\\n` 变空格、无第二行;实际收件人 == 路由 `to`,`Bcc` **不出现**。"""
    msg = H.build_headers(_Route(), "转发：微信消息 [财报群\r\nBcc: attacker@x.com] 摘要")
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    reparsed = message_from_bytes(msg.as_bytes())      # 真正的判据:重新按 RFC 解一遍,看有没有多出来的头
    assert reparsed["Bcc"] is None, "`\\r\\n` 若未被折成空格就会在这里变成一个真的 Bcc 头"
    assert reparsed["To"] == SENDER


def test_M106_newline_header_injection_blocked():
    """06 §2.4.1a 测试表第 2 行:昵称 = `张三\\nX-Priority: 1` ⇒ 头无 `X-Priority`,值被折成一行。"""
    msg = H.build_headers(_Route(), "昵称 张三\nX-Priority: 1")
    assert msg["X-Priority"] is None
    assert "\n" not in msg["Subject"]


def test_M107_unicode_subject_rfc2047():
    """06 §2.4.1a 规则 1 / 测试表第 3 行:会话名含中文 + emoji ⇒ `Subject` 为合法 `=?UTF-8?B?…?=`
    (编码由 `email.headerregistry`/`Header` 完成,不手拼)。"""
    msg = H.build_headers(_Route(), "转发：微信消息 [固收报价群👌]")
    raw = msg.as_string()
    assert "=?utf-8?" in raw.lower()


def test_M108_html_escaped_in_body():
    """06 §2.4.1a 规则 3 / 测试表第 4 行:`{text}` 含 `</td></tr><tr><td>` ⇒
    `text/html` 份被转义为实体,不破坏表格/MIME 结构。"""
    got = H.escape_html("</td></tr><tr><td>")
    assert "<" not in got and "&lt;" in got


def test_M109_subject_truncated_to_200():
    """06 §2.4.1a 规则 1 / 测试表第 5 行:群名超 300 字 ⇒ 截断到 ≤200 并加 `…`,不撑爆头长度限制。"""
    out = H.hdr_sanitize("长" * 300, max_len=H.SUBJECT_MAX_LEN)
    assert len(out) <= 200 and out.endswith("…")


def test_M110_placeholder_cannot_change_recipients():
    """06 §2.4.1a 规则 2 / 测试表第 6 行:占位符里写 `To: x@y` 字面量 ⇒ 收件人仍只是路由 `to`。"""
    msg = H.build_headers(_Route(to=["ops@corp.example"]), "转发：微信消息 To: x@y.example")
    assert msg["To"] == "ops@corp.example"


def test_M111_hdr_sanitize_none_is_empty():
    """06 §2.4.1a 函数体逐字:`if value is None: return ""`。"""
    assert H.hdr_sanitize(None) == ""


def test_M112_hdr_sanitize_strips_all_ctrl():
    """06 §2.4.1a 函数体逐字:`_CTRL` 含 `\\r\\n\\t\\v\\f`、Unicode 行分隔符与 `\\x00`,
    一律换空格再折叠空白、strip。"""
    assert H.hdr_sanitize("a\rb\nc\td\ve\ff g h\x00i") == "a b c d e f g h i"
    assert H.hdr_sanitize("  多余   空白  ") == "多余 空白"


def test_M113_header_max_len_defaults():
    """06 §2.4.1a 规则 1:Subject ≤ 200 字符、单个头值 ≤ 256。"""
    assert H.SUBJECT_MAX_LEN == 200 and H.HEADER_MAX_LEN == 256


def test_M114_recipients_only_from_route():
    """06 §2.4.1a 规则 2:`To/Cc/Bcc/Reply-To` 一律取自命中的 `mail_routes` 的 `outbound_json.to/cc`;
    渲染层与投递层分离。"""
    to, cc = H.recipients_of(_Route(to=["a@x.example"], cc=["b@x.example"]))
    assert to == ["a@x.example"] and cc == ["b@x.example"]


def test_M115_plain_text_keeps_newlines():
    """06 §2.4.1a 规则 3:`text/plain` 份剥控制字符(正文允许多行,不能把换行也剥了)。"""
    got = H.strip_ctrl_for_text("第一行\n第二行\r\x00")
    assert "\n" in got and "\x00" not in got


def test_M116_alert_subject_sanitized(rig):
    """06 §2.16.2(R5-7):告警主题里的 `previous_ip`/`public_ip` 来自探测响应 = **外部可控值**,
    渲染后整条主题必过 `hdr_sanitize()` 再设头(走唯一入口 `build_headers()`)。"""
    rig.svc.sender.enqueue_alert(
        ctx={"template_version": "v1", "code": "NET_PUBLIC_ENDPOINT_CHANGED",
             "previous_ip": "1.2.3.4", "public_ip": "5.6.7.8\r\nBcc: attacker@x.example"},
        lines=[("原IP", "1.2.3.4")], dedup_key="alert:endpoint:1")
    row = rig.outbox(kind="alert")[0]
    mime = rig.svc.sender.build_mime(row, rig.svc.routes.lookup(None))
    parsed = message_from_bytes(mime)
    assert parsed["Bcc"] is None, "外部可控值不得在头里另起一行变成 Bcc"
    assert parsed["To"] == SENDER


# ══════════════════════════════════════════════════ 九、模板配置(06 §2.14,E-4 / R-13)

SPEC_12 = [("channel", "channel_cn"), ("account_id", "account_label"), ("session_name", "session_id"),
           ("sender_kind",), ("sender_name", "sender_id"), ("ts", "ts_cn"), ("msg_type", "msg_type_cn"),
           ("summary",), ("media_count",), ("message_id", "ext_msg_id"), ("fingerprint",),
           ("seq", "seq_total", "day_seq")]


def test_M117_twelve_core_placeholders():
    """06 §2.14.2:12 个核心可配占位符(R-13 / N-10,由原 28 收敛),逐行取值见该表。"""
    assert len(SPEC_12) == 12
    for row in SPEC_12:
        for name in row:
            assert name in TPL.CORE_PLACEHOLDERS, f"{name} 应属 12 核心占位符"


def test_M118_compat_fields_not_core():
    """06 §2.14.2 末:兼容模板内部字段(`{text}`/`{attachments}`/`{image_kind}`/`{revoked}`/`{dir}`/
    `{media_ref}`/`{oversize}`/`{capture_text}`)**不计入 12、用户不可配**。"""
    assert TPL.COMPAT_PLACEHOLDERS == {"text", "attachments", "image_kind", "revoked", "dir",
                                       "media_ref", "oversize", "capture_text"}
    assert not (TPL.COMPAT_PLACEHOLDERS & TPL.CORE_PLACEHOLDERS)


def test_M119_receipt_placeholders_not_core():
    """06 §2.14.2:`{op}`/`{req_id}`/`{result_code}`/`{template_version}` 是回执/入站专用,不计入 12。"""
    assert TPL.RECEIPT_PLACEHOLDERS == {"op", "req_id", "result_code", "template_version"}
    assert not (TPL.RECEIPT_PLACEHOLDERS & TPL.CORE_PLACEHOLDERS)


def test_M120_unknown_placeholder_rejected():
    """06 §2.14.1:未知占位符**配置校验拒绝**(不是渲染时留空)。"""
    tpl = OutboundTemplateConfig(compat_profile=TPL.PROFILE_QTRADE,
                                 subject_pattern="QTrade {template_version} {no_such_placeholder}")
    with pytest.raises(TPL.TemplateInvalid):
        TPL.validate_template(tpl)


def test_M121_missing_template_version_rejected():
    """06 §2.14.1/§2.14.4:`{template_version}` 是主题 pattern 的**必填**占位符,配置校验拒绝没有它的 pattern。"""
    tpl = OutboundTemplateConfig(compat_profile=TPL.PROFILE_QTRADE, subject_pattern="QTrade [{account_id}]")
    with pytest.raises(TPL.TemplateInvalid):
        TPL.validate_template(tpl)


def test_M122_compat_profile_locks_info_segment():
    """06 §2.14.3:`ibquote-163-v1` 锁 `info_title` + `segment="info"` 全部项(键名、顺序、`value`、`empty`);
    不一致 ⇒ `400 TEMPLATE_LOCKED` 带哪一项。"""
    tpl = OutboundTemplateConfig(compat_profile=TPL.PROFILE_IBQUOTE)
    tpl.body_fields = [dict(f) for f in TPL.IBQUOTE_BODY_FIELDS]
    for f in tpl.body_fields:
        if f["key"] == "昵称":
            f["key"] = "发送人昵称"          # 信息段一个字都不能改(§2.4.1 末)
    with pytest.raises(TPL.TemplateLocked):
        TPL.validate_template(tpl)


def test_M123_compat_profile_locks_info_title():
    """06 §2.14.3:`info_title`(「微信群聊消息通知」)在兼容 profile 下锁定。"""
    tpl = OutboundTemplateConfig(compat_profile=TPL.PROFILE_IBQUOTE, info_title="我们自己的标题")
    with pytest.raises(TPL.TemplateLocked):
        TPL.validate_template(tpl)


def test_M124_compat_subject_is_changeable():
    """06 §2.4.1 / §2.14.3:**主题可以随便改、信息段不能**——兼容 profile 下 `subject_pattern` 可配。"""
    tpl = OutboundTemplateConfig(compat_profile=TPL.PROFILE_IBQUOTE,
                                 subject_pattern="转发：微信消息 [{channel_cn}][{session_name}] {summary}")
    TPL.validate_template(tpl)


def test_M125_collector_profile_is_html_only():
    """06 §2.14.3 表:`collector-v1` **仅 `text/html`**(collector `build_message_email_html` 同款),
    且锁 `mime=html_only`。"""
    assert TPL.defaults_for(TPL.PROFILE_COLLECTOR)["mime"] == "html_only"


def test_M126_three_builtin_profiles():
    """06 §2.14.3:三份默认模板(`GET /settings/mail/templates/defaults` 原样下发);
    R-13 删 `custom` 全开档,只剩 ibquote-163-v1 / collector-v1 / qtrade-v1。"""
    assert set(TPL.DEFAULT_TEMPLATES) == {TPL.PROFILE_IBQUOTE, TPL.PROFILE_COLLECTOR, TPL.PROFILE_QTRADE}


def test_M127_ibquote_info_segment_order():
    """06 §2.14.3 表 / §2.4.1:信息段键名与顺序逐字锁定(ibquote `parse_wechat_body` 所认)。"""
    info = [f["key"] for f in TPL.IBQUOTE_BODY_FIELDS if f["segment"] == "info"]
    assert info == ["群聊", "消息序号", "昵称", "消息时间", "每日序号", "消息ID", "消息类型",
                    "文本消息内容", "图片/文件附件", "图片类型", "是否撤回"]


def test_M128_ext_segment_before_title_line():
    """06 §2.4.1 正文:扩展块 = `segment="ext"` 的几行,放在**标题行之前**
    (ibquote 解析器从标题行**之后**开始扫,放后面会污染报价/常态触发模板漂移告警)。"""
    tpl = OutboundTemplateConfig()
    out = TPL.render_message_mail(tpl, {"account_id": "qd01", "channel": "qidian", "session_id": "qd01:g_1",
                                        "session_name": "固收报价群", "dir": "in", "message_id": "msg_1",
                                        "text": "3M 1.52", "attachments": "-", "revoked": "否",
                                        "msg_type": "text", "sender_name": "张三", "ts": "2026-09-18T10:03:00+08:00",
                                        "day_seq": "1", "ext_msg_id": "e1", "seq": "1", "seq_total": "1",
                                        "template_version": "v1"})
    body = out.body_text
    assert body.index("来源账号") < body.index(tpl.info_title) < body.index("群聊")


def test_M129_empty_dash_vs_omit():
    """06 §2.14.3:`dash` = 键在、值 `-`(ibquote 要求键存在:`文本消息内容：-`);`omit` = 整行不出现。"""
    tpl = OutboundTemplateConfig()
    out = TPL.render_message_mail(tpl, {"account_id": "qd01", "channel": "qidian", "session_id": "qd01:g_1",
                                        "session_name": "群", "dir": "in", "message_id": "m1",
                                        "text": "", "attachments": "", "revoked": "否", "msg_type": "text",
                                        "sender_name": "", "ts": "T", "day_seq": "1", "ext_msg_id": "e",
                                        "seq": "1", "seq_total": "1", "media_ref": "", "template_version": "v1"})
    assert "文本消息内容：-" in out.body_text
    assert "媒体引用" not in out.body_text, "`empty=omit` 的扩展块项为空时整行不出现"


def test_M130_scope_prefix_union_from_templates():
    """06 §2.14.3 末 / §2.6.4:`scope_subject_prefix` 从生效模板的 `subject_pattern` 推导,
    改了主题前缀清理范围自动跟随,两处不会漂。"""
    tpl = OutboundTemplateConfig(subject_pattern="我司转发 [{session_name}] {summary}")
    got = TPL.scope_subject_prefixes([tpl], ["QTRADE指令 {template_version} [{account_id}] {op} {req_id}"])
    assert "我司转发" in got and "QTRADE指令" in got


def test_M131_sender_kind_custom_labels():
    """06 §2.14.2 第 4 行:`{sender_kind}` 二值(群→`群`、单聊→`个人`),可自定义标签
    `{sender_kind:群聊|私聊}`(冒号后 `真|假` 两标签)。"""
    assert TPL.render("{sender_kind:群聊|私聊}", {"sender_kind": "group"}) == "群聊"
    assert TPL.render("{sender_kind:群聊|私聊}", {"sender_kind": "private"}) == "私聊"


def test_M132_collector_aliases():
    """06 §2.14.2:collector 兼容别名(`compat_profile="collector-v1"` 时自动启用,别处不认):
    `{target}`→`{session_name}`、`{sender}`→`{sender_name}`、`{message_id}`→`{ext_msg_id}`、`{seq_no}`→`{seq}`。"""
    assert TPL.COLLECTOR_ALIASES == {"target": "session_name", "sender": "sender_name",
                                     "message_id": "ext_msg_id", "seq_no": "seq"}


def test_M133_inbound_default_template_values(rig):
    """06 §7 `[mail.template.inbound]`:`compat_profile="qtrade-v1"`、
    `subject_pattern="QTRADE指令 {template_version} [{account_id}] {op} {req_id}"`、
    `title_line="QTrade 指令 {template_version}"`、`accepted_versions=["v1"]`。"""
    t = rig.cfg.template_in
    assert t.compat_profile == "qtrade-v1"
    assert t.subject_pattern == "QTRADE指令 {template_version} [{account_id}] {op} {req_id}"
    assert t.title_line == "QTrade 指令 {template_version}"
    assert t.accepted_versions == ["v1"]


def test_M134_outbound_default_template_values(rig):
    """06 §7 `[mail.template.outbound]`:默认 `compat_profile="ibquote-163-v1"`、
    `mime="alternative+mixed"`、`info_title="微信群聊消息通知"`、
    回执 `subject_pattern = "QTRADE回执 {template_version} [{account_id}] {op} {req_id} {result_code}"`。"""
    t = rig.cfg.template_out
    assert t.compat_profile == "ibquote-163-v1"
    assert t.subject_pattern == "转发：微信消息 [{session_name}] {summary} {ts_cn} - {seq}"
    assert t.mime == "alternative+mixed" and t.info_title == "微信群聊消息通知"
    assert t.receipt_subject_pattern == "QTRADE回执 {template_version} [{account_id}] {op} {req_id} {result_code}"


def test_M135_inbound_alias_table(rig):
    """06 §2.14.4 `[mail.template.inbound.aliases]`:默认别名表逐条。"""
    a = rig.cfg.template_in.aliases
    assert a["指令ID"] == ["req_id", "指令编号", "RequestId"]
    assert a["账号"] == ["account", "账号ID"] and a["操作"] == ["op", "指令"]
    assert a["签名"] == ["signature", "sig"] and a["随机数"] == ["nonce"]


# ══════════════════════════════════════════════════ 十、邮件路由(06 §2.15,E-5)


def _route(channel=None, account_id=None, host=IMAP_HOST, user=BOT, enabled=True, to=None, rid=1):
    inb = MailInboundConfig(host=host, user=user)
    outb = MailOutboundConfig(host=SMTP_HOST, user=user, from_addr=user, recipients=to or [SENDER])
    return MailRoute(id=rid, channel=channel, account_id=account_id, inbound=inb, outbound=outb, enabled=enabled)


def test_M136_lookup_three_levels():
    """06 §2.15.1 查找顺序:① 账号级 `(channel, account_id)` → ② 通道级 `(channel, NULL)` → ③ 全局 `('*', NULL)`。"""
    acct = _route("qidian", "qd01", rid=1)
    chan = _route("qidian", None, rid=2)
    glob = _route(None, None, rid=3)
    t = RouteTable([acct, chan, glob])
    assert t.lookup("qidian", "qd01") is acct
    assert t.lookup("qidian", "qd02") is chan
    assert t.lookup("qq", "qq03") is glob


def test_M137_disabled_route_skipped():
    """06 §2.15.2 末:`enabled=false` 的路由跳过查找。"""
    chan = _route("qidian", None, rid=2, enabled=False)
    glob = _route(None, None, rid=3)
    assert RouteTable([chan, glob]).lookup("qidian", "qd01") is glob


def test_M138_mailbox_key_lowercased():
    """06 §2.15.2:`mailbox_key = lower(host)+"/"+lower(user)`,程序算,用于读线程去重与 `cursors.owner`。"""
    assert mailbox_key("IMAP.163.COM", "Ops@163.Com") == "imap.163.com/ops@163.com"


def test_M139_cursor_owner_is_mail_mailbox_key(rig):
    """06 §2.15.3 / §2.9.3:`cursors.owner = mail:<mailbox_key>`。"""
    r = rig.svc.routes.lookup(None)
    assert r.cursor_owner == f"mail:{MAILBOX}"
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="RT-1", nonce="rt1"))
    owners = {x["owner"] for x in rig.rows("select owner from cursors")}
    assert f"mail:{MAILBOX}" in owners


def test_M140_one_thread_per_mailbox():
    """06 §2.0 / §2.15.3:**按 `mailbox_key` 去重,一个物理邮箱一条读线程**;多条路由共用同一邮箱时共用那条线程。"""
    t = RouteTable([_route("qidian", None, rid=1), _route("qq", None, rid=2),
                    _route("wechat", None, host="imap.other.example", rid=3)])
    boxes = t.mailboxes()
    assert len(boxes) == 2
    assert len(boxes[MAILBOX]) == 2


def test_M141_route_covers_account():
    """06 §2.15.1:账号级 = 那一个账号;通道级 = 该通道全部账号;全局 = 全部。"""
    assert _route("qidian", "qd01").covers("qd01", "qidian") is True
    assert _route("qidian", "qd01").covers("qd02", "qidian") is False
    assert _route("qidian", None).covers("qd02", "qidian") is True
    assert _route("qidian", None).covers("qq03", "qq") is False
    assert _route(None, None).covers("qq03", "qq") is True


def test_M142_route_mismatch_status(tmp_path, clock):
    """06 §2.15.1/§2.15.3:收到的指令邮件里 `账号` 不属于该路由覆盖的账号集合 ⇒ `ROUTE_MISMATCH`
    ——防止「给微信配的邮箱收到一封停企点账号的指令」。"""
    cfg = make_cfg()
    r = Rig(tmp_path=tmp_path, clock=clock, cfg=cfg)
    r.ms.route_upsert(channel="wechat", account_id=None,
                      inbound_json={"host": IMAP_HOST, "user": BOT, "allowed_senders": [SENDER],
                                    "hmac": {SHORT: {"from": SENDER}}},
                      outbound_json={"host": SMTP_HOST, "user": BOT, "from": BOT, "to": [SENDER]})
    r.store.con.execute("update mail_routes set enabled=0 where channel is null")
    r.store.con.commit()
    r.svc.reload()
    r.ingest_imap(build_command_mail(clock=clock, account_id="qd01", req_id="RM-1", nonce="rm1"))
    assert r.one_inbox()["status"] == C.ROUTE_MISMATCH
    r.store.close()


def test_M143_route_id_recorded(rig):
    """06 §3.1 `mail_inbox.route_id`(v0.3 E-5):收到本封的 `mail_routes.id`(回执从同路由发)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="RI-1", nonce="ri1"))
    assert rig.one_inbox()["route_id"] == rig.svc.routes.lookup(None).id


def test_M144_receipt_uses_same_route(rig):
    """06 §2.15.1:回执用**收到指令的那条路由**的 outbound 发(`In-Reply-To` 才能叠在一起)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="account_stop", req_id="RR-1", nonce="rr1"))
    out = rig.outbox(kind="receipt")[0]
    assert out["route_id"] == rig.one_inbox()["route_id"]


def test_M145_default_route_is_global_star(rig):
    """06 §2.15.1 ③ / §3.1 末:全局默认行 = `agent.toml [mail.inbound]/[mail.outbound]` 的物化视图,
    `channel` 为全局(`*`),不可删。"""
    rows = rig.rows("select channel, account_id from mail_routes")
    assert {"channel": None, "account_id": None} in rows


def test_M146_route_unique_key(rig):
    """06 §2.15.1 / 02 §3.1 `ux_mail_routes_key`:唯一约束 `(COALESCE(channel,''), COALESCE(account_id,''))`。"""
    rid1 = rig.ms.route_upsert(channel="qq", account_id=None, inbound_json={}, outbound_json={})
    rid2 = rig.ms.route_upsert(channel="qq", account_id=None, inbound_json={}, outbound_json={})
    assert rid1 == rid2


# ══════════════════════════════════════════════════ 十一、回执邮件(06 §2.4.2 / §2.8 末 / §2.4.4)


async def _run_send_text(r, *, result=None, req_id="RCP-1", nonce="rc1", text="今日 3M 报价 1.52"):
    r.ingest_imap(build_command_mail(clock=r.clock, op="send_text", session="张三-固收",
                                     args={"text": text}, req_id=req_id, nonce=nonce))
    bus = FakeBus(result)
    await r.svc.ingest.dispatch(bus)
    return bus


async def test_M147_receipt_subject_and_in_reply_to(rig):
    """06 §2.4.2:主题 `QTRADE回执 v1 [{account_id}] {op} {req_id} {送达状态}`;
    头 `In-Reply-To`/`References` = 指令邮件 `Message-ID`(§2.1 SMTP 段同句)。"""
    await _run_send_text(rig)
    out = rig.outbox(kind="receipt")[0]
    assert out["subject"].startswith("QTRADE回执 v1 [qd01] send_text RCP-1")
    assert "DELIVERED" in out["subject"]
    assert out["in_reply_to"] == "<RCP-1.rc1@sender.example>"
    assert out["references_hdr"] == out["in_reply_to"]


async def test_M148_receipt_body_fields(rig):
    """06 §2.4.2 正文:`指令ID/账号/操作/会话/送达状态/结果/确认方式/消息ID/耗时毫秒/追踪ID/错误/
    可重试/需人工/执行时间/签名` 逐栏。"""
    await _run_send_text(rig, req_id="RCP-2", nonce="rc2")
    body = rig.outbox(kind="receipt")[0]["body_text"]
    for key in TPL.RECEIPT_FIELDS:
        assert f"{key}：" in body, f"回执缺字段 {key}"


async def test_M149_receipt_maps_result_code_verbatim(rig):
    """06 §2.4.2 末:结果码 → 「送达状态」**逐字映射,不翻译、不合并**;`结果` 一栏是给人看的。"""
    res = CommandResult(ok=False, code="SEND_FAILED", trace_id="01J8T", cost_ms=10, source="get_msg")
    await _run_send_text(rig, result=res, req_id="RCP-3", nonce="rc3")
    body = rig.outbox(kind="receipt")[0]["body_text"]
    assert "送达状态：SEND_FAILED" in body and "结果：失败" in body


async def test_M150_receipt_source_is_confirm_method(rig):
    """06 §2.8 末:`CommandResult.source` → 回执「确认方式」(history/get_msg/chatlog/qidian_db/-)。"""
    await _run_send_text(rig, req_id="RCP-4", nonce="rc4")
    assert "确认方式：get_msg" in rig.outbox(kind="receipt")[0]["body_text"]


async def test_M151_receipt_has_no_message_body(rig):
    """06 §2.4.2 末:**回执不含消息正文**(发起方自己发的,回执重复一遍只是让邮箱里多一份正文)。"""
    await _run_send_text(rig, req_id="RCP-5", nonce="rc5", text="这段正文不该出现在回执里")
    assert "这段正文不该出现在回执里" not in rig.outbox(kind="receipt")[0]["body_text"]


async def test_M152_receipt_signed_with_command_key(rig):
    """06 §2.4.2 末:回执签名用与指令签名**同一把** `vault://mail/hmac/cmd/<短名>` 密钥,
    规范串 = `v1\\nreq_id\\naccount_id\\nop\\n送达状态\\n追踪ID\\n执行时间`。"""
    await _run_send_text(rig, req_id="RCP-6", nonce="rc6")
    body = rig.outbox(kind="receipt")[0]["body_text"]
    got = dict(re.findall(r"^(\S+?)：(.*)$", body, re.M))
    want = spec_sign(spec_receipt_canonical(req_id="RCP-6", account_id="qd01", op="send_text",
                                            delivery_status=got["送达状态"], trace_id=got["追踪ID"],
                                            executed_at=got["执行时间"]))
    assert got["签名"] == f"hmac-sha256={want}"


async def test_M153_needs_human_marked(rig):
    """06 §2.8 末:`needs_human=true` 的码(`LOGIN_REQUIRED/CAPTCHA_REQUIRED`)回执里 `需人工：是`。"""
    res = CommandResult(ok=False, code="LOGIN_REQUIRED", trace_id="01J8L", cost_ms=1,
                        error=CommandError(message="请先登录", retryable=False, needs_human=True))
    await _run_send_text(rig, result=res, req_id="RCP-7", nonce="rc7")
    body = rig.outbox(kind="receipt")[0]["body_text"]
    assert "需人工：是" in body and "可重试：否" in body


async def test_M154_login_required_not_queued(rig):
    """06 §2.8 D-2:账号 `login_required` 时 bus **立即**回 `LOGIN_REQUIRED`(不排队),
    回执马上发出(`需人工：是`),`mail_inbox.status=DONE`。"""
    res = CommandResult(ok=False, code="LOGIN_REQUIRED", trace_id="01J8L2", cost_ms=1,
                        error=CommandError(message="扫码", retryable=False, needs_human=True))
    await _run_send_text(rig, result=res, req_id="RCP-8", nonce="rc8")
    assert rig.one_inbox()["status"] in (C.DONE, C.RECEIPT_SENT)
    assert rig.outbox(kind="receipt")


async def test_M155_confirm_false_skips_receipt(rig):
    """06 §2.3.5 状态流:`确认=false` 或发起方不要回执 ⇒ `RECEIPT_SKIPPED`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", session="张三",
                                       args={"text": "x"}, confirm="false", req_id="RCP-9", nonce="rc9"))
    await rig.svc.ingest.dispatch(FakeBus())
    assert rig.one_inbox()["status"] == C.RECEIPT_SKIPPED
    assert rig.outbox(kind="receipt") == []


async def test_M156_status_flow_to_done(rig):
    """06 §2.3.5 状态流:`RECEIVED`(仅崩溃窗口可见)→ `ACCEPTED`(带 `trace_id`)→ `DONE`(拿到 `CommandResult`)
    → `RECEIPT_SENT`;§8b M5「指令邮件端到端」。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", session="张三",
                                       args={"text": "x"}, req_id="RCP-10", nonce="rc10"))
    assert rig.one_inbox()["status"] == C.ACCEPTED
    await rig.svc.ingest.dispatch(FakeBus())
    assert rig.one_inbox()["status"] in (C.DONE, C.RECEIPT_SENT)
    rig.svc.send_once()
    assert rig.one_inbox()["status"] == C.RECEIPT_SENT


async def test_M157_trace_id_recorded(rig):
    """06 §3.1 `trace_id`/`command_id`:进总线后的关联(§2.8:`trace_id` 进总线时生成)。"""
    await _run_send_text(rig, req_id="RCP-11", nonce="rc11")
    row = rig.one_inbox()
    assert row["trace_id"] == "01J8TRACE0001" and row["command_id"] == "01J8TRACE0001"


# ══════════════════════════════════════════════════ 十二、SMTP 发送、退避、限速与死信(06 §2.1)


def _queue_receipt(r, *, dedup="receipt:1"):
    return r.ms.outbox_enqueue(kind="receipt", to_addrs=SENDER, subject="QTRADE回执 v1 [qd01] x y DELIVERED",
                               body_text="QTrade 回执 v1", rfc_message_id=f"<{dedup}@qtrade.local>",
                               dedup_key=dedup, template_version="v1", route_id=r.svc.routes.lookup(None).id)


def test_M159_message_id_generated_locally(rig):
    """06 §2.1 SMTP 段:`Message-ID` 由本系统生成 `<{ulid}@qtrade.local>` 并落 `mail_outbox.rfc_message_id`。"""
    mid = __import__("qtrade_agent.mail.sender", fromlist=["new_message_id"]).new_message_id()
    assert mid.startswith("<") and mid.endswith("@qtrade.local>")


def test_M160_backoff_sequence(rig):
    """06 §2.1:`backoff = [30s, 1m, 2m, 5m, 10m, 20m, 30m, 60m]`;
    投递失败 `attempts+1`、`next_attempt_ms = now + backoff[attempts]`。"""
    assert rig.cfg.outbound.backoff_s == [30, 60, 120, 300, 600, 1200, 1800, 3600]
    _queue_receipt(rig)
    rig.smtp.fail_times = 1
    rig.smtp.fail_exc = SmtpTemporaryError("451 临时故障")
    rig.svc.send_once()
    row = rig.outbox()[0]
    assert row["status"] == "RETRY" and row["attempts"] == 1
    assert row["next_attempt_ms"] == rig.clock() + 30 * 1000


def test_M161_dead_after_max_attempts(rig):
    """06 §2.1:超过 `max_attempts = 8` 置 `DEAD`(死信)并发 `mail` 事件 `MAIL_OUTBOX_DEAD`。"""
    _queue_receipt(rig)
    rig.smtp.fail_times = 99
    rig.smtp.fail_exc = SmtpTemporaryError("451 临时故障")
    for _ in range(9):
        rig.svc.send_once()
        rig.clock.advance(3600 * 1000)
    row = rig.outbox()[0]
    assert row["status"] == "DEAD" and row["attempts"] >= rig.cfg.outbound.max_attempts
    firing = rig.events.of(C.MAIL_OUTBOX_DEAD, "firing")
    assert firing and firing[0]["payload"]["subject"] == f"outbox:{row['id']}"


def test_M162_permanent_5xx_dead_without_retry(rig):
    """06 §2.1:**5xx 永久码**(550 收件人不存在、552 超限、553)**不重试直接 DEAD**(重试只会重复挨拒)。"""
    _queue_receipt(rig)
    rig.smtp.fail_times = 99
    rig.smtp.fail_exc = SmtpPermanentError("550 收件人不存在")
    rig.svc.send_once()
    row = rig.outbox()[0]
    assert row["status"] == "DEAD" and row["attempts"] == 1


def test_M163_auth_failure_alerts_and_backs_off(rig):
    """06 §2.1:535 认证失败按「环境故障」退避且触发 `MAIL_AUTH_FAILED` 告警(crit)。"""
    _queue_receipt(rig)
    rig.smtp.raise_on_connect = MailAuthError("535 认证失败")
    rig.svc.send_once()
    row = rig.outbox()[0]
    assert row["status"] == "RETRY"
    firing = rig.events.of(C.MAIL_AUTH_FAILED, "firing")
    assert firing and firing[0]["payload"]["severity"] == "crit"


def test_M165_one_failure_does_not_block_others(rig):
    """06 §2.1 末:队列消费单线程、按 `next_attempt_ms` 排序;**一封发送失败不阻塞后面**
    (同 collector `send_message_emails` 的「单条失败隔离」)。"""
    _queue_receipt(rig, dedup="receipt:a")
    _queue_receipt(rig, dedup="receipt:b")
    rig.smtp.fail_times = 1
    rig.smtp.fail_exc = SmtpTemporaryError("451")
    rig.svc.send_once()
    rows = rig.outbox()
    assert rows[0]["status"] == "RETRY" and rows[1]["status"] == "SENT"


def test_M166_rate_limit_token_bucket(rig):
    """06 §2.1:限速令牌桶 `send_rate_per_min = 20`、`send_burst = 5`。"""
    assert rig.cfg.outbound.send_rate_per_min == 20 and rig.cfg.outbound.send_burst == 5
    for i in range(8):
        _queue_receipt(rig, dedup=f"receipt:r{i}")
    st = rig.svc.send_once()
    assert st.sent <= 5 and st.skipped_rate >= 1, "突发上限之外的本轮不发,留给下一轮"


def test_M167_discard_sets_status(rig):
    """06 §3.2 / 02 #63 `POST /mail/outbox/{id}/discard`:置 `DISCARDED`(队列中/重试中的)。"""
    oid = _queue_receipt(rig, dedup="receipt:d")
    rig.svc.sender.discard(oid)
    assert rig.outbox()[0]["status"] == "DISCARDED"


def test_M168_dedup_key_unique(rig):
    """06 §3.1 `mail_outbox.dedup_key`:`kind + ref_*`,唯一——防同一条消息/回执重复入队。"""
    assert _queue_receipt(rig, dedup="receipt:same") is not None
    assert _queue_receipt(rig, dedup="receipt:same") is None


def test_M169_outbox_counts_for_status(rig):
    """06 §2.7 `GET /mail/status` 的 `outbound` 段:`queued/retrying/dead`。"""
    _queue_receipt(rig, dedup="receipt:q1")
    counts = rig.ms.outbox_counts()
    assert counts["queued"] == 1 and counts["retrying"] == 0 and counts["dead"] == 0


def test_M170_sent_rows_carry_sent_ms(rig):
    """06 §3.1 `mail_outbox.sent_ms`;投递成功置 `SENT`。"""
    _queue_receipt(rig, dedup="receipt:s1")
    rig.svc.send_once()
    row = rig.outbox()[0]
    assert row["status"] == "SENT" and row["sent_ms"] == rig.clock()
    assert len(rig.smtp.sent) == 1 and rig.smtp.sent[0][1] == [SENDER]


def test_M158b_resend_dead_row_makes_new_row_with_suffix(rig):
    """06 §2.5 末 / 02 #62:死信手动重投 = **新行**,`dedup_key` 加后缀 `#2`,`attempts` 清零。"""
    oid = _queue_receipt(rig, dedup="receipt:dead1")
    rig.ms.outbox_update(oid, status="DEAD", attempts=8)
    new_id = rig.svc.sender.resend(oid)
    rows = rig.outbox(kind="receipt")
    assert new_id != oid and len(rows) == 2
    assert rows[1]["dedup_key"] == "receipt:dead1#2" and rows[1]["attempts"] == 0


# ══════════════════════════════════════════════════ 十三、邮件定期删除(06 §2.6,NEVER_DELETE 五处门)


def _terminal_rows(r, *, n=1, protocol="imap", oversize=False, out_of_scope=False):
    """造几封「已处理」的邮件:普通指令信走 SENDER_DENIED(终态、不在 NEVER_DELETE)。"""
    ids = []
    for i in range(n):
        if oversize:
            raw = build_command_mail(clock=r.clock, req_id=f"OS-{i}", nonce=f"os{i}")
            size = r.cfg.inbound.max_message_bytes + 1
        elif out_of_scope:
            raw, size = plain_mail(subject=f"别人的信 {i}"), None
        else:
            raw, size = build_command_mail(clock=r.clock, req_id=f"TM-{i}", nonce=f"tm{i}",
                                           from_addr="stranger@x.example"), None
        if protocol == "imap":
            r.ingest_imap(raw, size=size)
        else:
            r.ingest_pop3(f"U-{'os' if oversize else 'oo' if out_of_scope else 'tm'}-{i}", raw, size=size)
        ids.append(r.inbox()[-1]["id"])
    return ids


def test_M171_eligible_requires_terminal_and_retention(rig):
    """06 §2.6.1 `eligible(row)`:终态 ∧ `status ∉ NEVER_DELETE` ∧(归档已完成)∧
    `now - row.received_ms >= retention_days * 86400_000`。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 7
    assert rig.svc.cleaners[MAILBOX].run(rig.imap, "imap").candidates == 0, "未过保留期不进候选"
    rig.clock.advance(8 * 86400 * 1000)
    assert rig.svc.cleaners[MAILBOX].run(rig.imap, "imap").candidates == 1


def test_M172_done_with_queued_receipt_not_deleted(rig):
    """06 §2.6.1 末:`DONE` 但回执还在 `mail_outbox` 队列里(`QUEUED/RETRY`)的**不删**
    ——回执发不出去时还可能要人从原文核对。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="account_stop", req_id="DQ-1", nonce="dq1"))
    rig.store.con.execute("update mail_inbox set status='DONE'")
    rig.store.con.commit()
    rig.cfg.cleanup.retention_days = 0
    assert rig.ms.outbox_has_pending_receipt(rig.inbox()[0]["id"]) is True
    assert rig.svc.cleaners[MAILBOX].run(rig.imap, "imap").deleted == 0


def test_M173_archive_before_delete(rig, tmp_path):
    """06 §2.6.2:归档 `mail/archive/{yyyymm}/{received_ms}_{inbox_id}.eml` + sidecar
    `{inbox_id}.json`(mail_inbox 该行快照),写盘 → fsync → 校验 `len == size_bytes`;
    随后 `update mail_inbox.archived_path, archived_ms`。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    row = rig.inbox()[0]
    assert row["archived_path"] and row["archived_ms"]
    assert row["archived_path"].endswith(".eml")
    import os
    assert os.path.exists(row["archived_path"])
    assert os.path.exists(os.path.join(os.path.dirname(row["archived_path"]), f"{row['id']}.json"))


def test_M174_delete_marks_deleted_ms(rig):
    """06 §2.6.3 ①:`archive(row) → delete_on_server(row) → mark deleted_ms`。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    st = rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert st.deleted == 1 and rig.inbox()[0]["deleted_ms"] == rig.clock()


def test_M175_archive_failure_blocks_delete(rig):
    """06 §2.6.2/§2.6.9:归档写盘失败 ⇒ **不删**,`mail_cleanup_log` 记 `ARCHIVE_FAILED`。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.cfg.cleanup.archive_dir = "/proc/不可写目录/archive"
    st = rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert st.deleted == 0 and st.failed == 1
    assert rig.inbox()[0]["deleted_ms"] is None
    assert "ARCHIVE_FAILED" in json.dumps(rig.cleanup_logs()[-1]["detail_json"], ensure_ascii=False)


def test_M176_cleanup_failed_alert_after_three_rounds(rig):
    """06 §2.6.2/§2.6.9:连续 3 轮同一封归档失败 ⇒ `mail` 事件 `MAIL_CLEANUP_FAILED`
    (§7 `stall_alert_rounds = 3`)。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.cfg.cleanup.archive_dir = "/proc/不可写目录/archive"
    for _ in range(3):
        rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert rig.events.of(C.MAIL_CLEANUP_FAILED, "firing")


def test_M177_never_delete_set_is_two(rig):
    """06 §2.3.5(唯一出处):`NEVER_DELETE = {OUT_OF_SCOPE, OVERSIZE}`。"""
    assert set(C.NEVER_DELETE) == {"OUT_OF_SCOPE", "OVERSIZE"}


def test_M178_terminal_set_excludes_in_flight(rig):
    """06 §2.6.1:`RECEIVED/ACCEPTED/DONE(未回执)` 不算终态。"""
    assert C.RECEIVED not in C.TERMINAL and C.ACCEPTED not in C.TERMINAL
    assert {C.RECEIPT_SENT, C.OUT_OF_SCOPE, C.OVERSIZE, C.CONFIRM_EXPIRED} <= set(C.TERMINAL)
    assert C.CONFIRM_REQUIRED not in C.TERMINAL, "待确认不是终态(还等着人批)"


def test_M179_gate1_eligible_excludes_never_delete(rig):
    """06 §2.6.1 门 ①:`eligible(row)` 里 `and row.status not in NEVER_DELETE`。"""
    _terminal_rows(rig, n=1, oversize=True)
    _terminal_rows(rig, n=1, out_of_scope=True)
    rig.cfg.cleanup.retention_days = 0
    st = rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert st.deleted == 0
    assert all(r["deleted_ms"] is None for r in rig.inbox())


def test_M180_gate2_capacity_excludes_never_delete(tmp_path, clock):
    """06 §2.6.3 ② 门:按容量水位的候选选择循环同样 `where status ∈ TERMINAL and status ∉ NEVER_DELETE`
    ——邮箱再满也不删 `OUT_OF_SCOPE`/`OVERSIZE` 的信。"""
    r = Rig(tmp_path=tmp_path, clock=clock)
    r.cfg.cleanup.retention_days = 99999          # 保留期这条路走不通,只剩容量水位
    _terminal_rows(r, n=1, oversize=True)
    _terminal_rows(r, n=1, out_of_scope=True)
    r.imap.quota_used, r.imap.quota_limit = 950, 1000     # 0.95 ≥ 水位 0.8
    st = r.svc.cleaners[MAILBOX].run(r.imap, "imap")
    assert st.deleted == 0
    assert all(x["deleted_ms"] is None for x in r.inbox())
    r.store.close()


def test_M181_gate2_capacity_deletes_normal_rows(tmp_path, clock):
    """06 §2.6.3 ②:用量/上限 ≥ `mailbox_quota_watermark`(0.8)⇒ 按 `received_ms asc` 删到
    `target = (水位-0.1) * limit`(滞回 10%,避免在阈值附近每轮抖)。"""
    r = Rig(tmp_path=tmp_path, clock=clock)
    r.cfg.cleanup.retention_days = 99999
    _terminal_rows(r, n=2)
    r.imap.quota_used, r.imap.quota_limit = 900, 1000
    st = r.svc.cleaners[MAILBOX].run(r.imap, "imap")
    assert st.trigger == "capacity" and st.deleted >= 1
    r.store.close()


def test_M182_quota_high_alert_when_still_over(tmp_path, clock):
    """06 §2.6.3 ② 末 / §2.7:可删的删光了还超水位 ⇒ `MAIL_QUOTA_HIGH`(`subject=mailbox:<addr>`,
    ≥ 0.95 crit),只能人来。"""
    r = Rig(tmp_path=tmp_path, clock=clock)
    r.cfg.cleanup.retention_days = 99999
    _terminal_rows(r, n=1, oversize=True)
    r.imap.quota_used, r.imap.quota_limit = 990, 1000
    r.svc.cleaners[MAILBOX].run(r.imap, "imap")
    firing = r.events.of(C.MAIL_QUOTA_HIGH, "firing")
    assert firing and firing[0]["payload"]["subject"].startswith("mailbox:")
    r.store.close()


def test_M183_gate2b_max_kept_excludes_never_delete(tmp_path, clock):
    """06 §2.6.3 ③(R6-26 / §8b M5 验收行):`max_kept_count=1` 且拿不到容量信息 ⇒ 只删普通信;
    `OVERSIZE`/`OUT_OF_SCOPE` 两封仍在服务器原位、`deleted_ms` 为空;
    `mail_cleanup_log.trigger='max_kept'`、`detail_json.skipped_oversize=1` 且 `skipped_out_of_scope=1`。"""
    r = Rig(tmp_path=tmp_path, clock=clock)
    r.cfg.cleanup.retention_days = 99999
    r.cfg.cleanup.mailbox_quota_mb_assumed = 0       # 0 = 未知 ⇒ 只走保留期 + max_kept_count(§2.6.4 末行)
    r.imap.caps.discard("QUOTA")                     # 服务器不宣告 QUOTA ⇒ 拿不到 limit
    _terminal_rows(r, n=2)
    _terminal_rows(r, n=1, oversize=True)
    _terminal_rows(r, n=1, out_of_scope=True)
    r.cfg.cleanup.max_kept_count = 1                 # kept(不含 NEVER_DELETE)=2 > 1 ⇒ 触发 ③
    st = r.svc.cleaners[MAILBOX].run(r.imap, "imap")
    rows = {x["status"]: x for x in r.inbox()}
    assert rows[C.OVERSIZE]["deleted_ms"] is None and rows[C.OUT_OF_SCOPE]["deleted_ms"] is None
    assert st.trigger == "max_kept" and st.deleted == 1
    log = json.loads(r.cleanup_logs()[-1]["detail_json"])
    assert log["skipped_oversize"] == 1 and log["skipped_out_of_scope"] == 1
    r.store.close()


def test_M184_max_kept_denominator_excludes_never_delete(tmp_path, clock):
    """06 §2.6.3 ③(R6-26):分母 `kept` 也不含 `NEVER_DELETE` 两类
    ——它们永不删,算进分母只会把别的信提前删掉。"""
    r = Rig(tmp_path=tmp_path, clock=clock)
    r.cfg.cleanup.retention_days = 99999
    r.cfg.cleanup.max_kept_count = 2
    r.imap.caps.discard("QUOTA")
    _terminal_rows(r, n=1)
    _terminal_rows(r, n=1, oversize=True)
    _terminal_rows(r, n=1, out_of_scope=True)
    st = r.svc.cleaners[MAILBOX].run(r.imap, "imap")
    assert st.deleted == 0, "kept(只数非 NEVER_DELETE 的)=1 ≤ 2,本轮不该删任何一封"
    r.store.close()


def test_M185_skipped_counters_always_present(rig):
    """06 §2.6.8(R6-26):`detail_json` 必带计数键 `skipped_oversize` 与 `skipped_out_of_scope`
    ——门是否生效的**唯一可观测证据**。"""
    _terminal_rows(rig, n=1, oversize=True)
    rig.cfg.cleanup.retention_days = 0
    rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    log = json.loads(rig.cleanup_logs()[-1]["detail_json"])
    assert log["skipped_oversize"] == 1 and "skipped_out_of_scope" in log


def test_M186_cleanup_log_one_row_per_round(rig):
    """06 §2.6.8:`mail_cleanup_log` 每轮一行,字段 `started_ms/finished_ms/trigger/protocol/folder/
    candidates/archived/deleted/failed/bytes_freed/quota_*/archive_rotated*/status/error/detail_json`。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    log = rig.cleanup_logs()[-1]
    for k in ("started_ms", "finished_ms", "trigger", "protocol", "candidates", "archived",
              "deleted", "failed", "bytes_freed", "status", "detail_json"):
        assert k in log
    assert log["status"] in ("OK", "PARTIAL", "FAILED")


def test_M187_trigger_enum(rig):
    """06 §3.1 / 02 §3.1(R6-26/R6-31):`trigger ∈ {retention, capacity, max_kept, manual, archive_rotation}`。"""
    from qtrade_agent.mail import cleanup as CL
    assert {CL.TRIGGER_RETENTION, CL.TRIGGER_CAPACITY, CL.TRIGGER_MAX_KEPT, CL.TRIGGER_MANUAL,
            CL.TRIGGER_ARCHIVE_ROTATION} == {"retention", "capacity", "max_kept", "manual", "archive_rotation"}


def test_M188_pop3_delete_only_after_quit_ok(pop_rig):
    """06 §2.6.5 POP3:`deleted_ms` **只在 `QUIT` 收到 `+OK` 后写**;`QUIT` 失败 ⇒ 服务器撤销全部 `DELE`,
    本轮记 `DELETE_FAILED`,下轮重来(归档已完成,幂等)。"""
    _terminal_rows(pop_rig, n=1, protocol="pop3")
    pop_rig.cfg.cleanup.retention_days = 0
    pop_rig.pop3.quit_ok = False
    st = pop_rig.svc.cleaners[MAILBOX].run(pop_rig.pop3, "pop3")
    assert pop_rig.inbox()[0]["deleted_ms"] is None and st.deleted == 0
    pop_rig.pop3.quit_ok = True
    pop_rig.svc.cleaners[MAILBOX].run(pop_rig.pop3, "pop3")
    assert pop_rig.inbox()[0]["deleted_ms"] is not None


def test_M189_pop3_never_delete_not_in_dele_set(pop_rig):
    """06 §2.6.5 POP3 门 ④:`cleanup_pop3()` 里 `if row.status in NEVER_DELETE: continue`
    ——`OUT_OF_SCOPE`/`OVERSIZE` 的 `uidl` **永不进 `DELE` 集合**。"""
    _terminal_rows(pop_rig, n=1, protocol="pop3", oversize=True)
    _terminal_rows(pop_rig, n=1, protocol="pop3", out_of_scope=True)
    pop_rig.cfg.cleanup.retention_days = 0
    pop_rig.svc.cleaners[MAILBOX].run(pop_rig.pop3, "pop3")
    assert pop_rig.pop3.deleted == []
    assert len(pop_rig.pop3.uidls()) == 2, "两封原信始终在服务器上"


def test_M190_gone_on_server_is_not_failure(pop_rig):
    """06 §2.6.9:服务器已不存在该封(人先删了)⇒ 记 `deleted_ms` 并 `reason=gone_on_server`,**不算失败**。"""
    _terminal_rows(pop_rig, n=1, protocol="pop3")
    pop_rig.cfg.cleanup.retention_days = 0
    pop_rig.pop3.messages = []                      # 人在自己的客户端里先删了
    st = pop_rig.svc.cleaners[MAILBOX].run(pop_rig.pop3, "pop3")
    row = pop_rig.inbox()[0]
    assert row["deleted_ms"] is not None and "gone_on_server" in (row["reason"] or "")
    assert st.failed == 0


def test_M191_imap_uidplus_expunges_only_ours(rig):
    """06 §2.6.5 IMAP 第 2 步:有 `UIDPLUS` 用 `UID EXPUNGE uid`(只清这一封);
    §6.5:**无 `UIDPLUS` 时不对 INBOX/Junk 整夹 `EXPUNGE`**。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    other = rig.imap.add(plain_mail(subject="别人的邮件"))
    rig.imap.select("INBOX", readonly=False)
    rig.imap.store_deleted(other)                  # 人用 Outlook 标删但不清
    rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert other not in rig.imap.expunged, "他人标删的邮件不得被我们清掉"


def test_M192_no_uidplus_defers_inbox_expunge(tmp_path, clock):
    """06 §2.6.5 IMAP 第 3 步:无 `UIDPLUS` 时先 `UID SEARCH DELETED`,
    **标删集合 ⊆ 本轮我们标删的 UID 集合才 `EXPUNGE`**,否则本轮不清、记 `DELETE_DEFERRED` 下轮再看。"""
    r = Rig(tmp_path=tmp_path, clock=clock)
    r.imap.caps.discard("UIDPLUS")
    _terminal_rows(r, n=1)
    r.cfg.cleanup.retention_days = 0
    other = r.imap.add(plain_mail(subject="别人的邮件 2"))
    r.imap.select("INBOX", readonly=False)
    r.imap.store_deleted(other)
    r.svc.cleaners[MAILBOX].run(r.imap, "imap")
    assert other not in r.imap.expunged
    assert "DELETE_DEFERRED" in json.dumps(r.cleanup_logs()[-1]["detail_json"], ensure_ascii=False)
    r.store.close()


def test_M193_archive_rotation(rig, tmp_path):
    """06 §2.6.7:每轮清理末尾删除 `mail/archive/` 下修改时间早于 `archive_retention_days = 7` 的
    `.eml`/`.json`;归档目录删除也写 `mail_cleanup_log`(`trigger=archive_rotation`)。"""
    import os
    import time
    d = os.path.join(rig.cfg.cleanup.archive_dir, "202501")
    os.makedirs(d, exist_ok=True)
    old = os.path.join(d, "1_1.eml")
    with open(old, "wb") as f:
        f.write(b"old")
    old_ts = time.time() - 8 * 86400
    os.utime(old, (old_ts, old_ts))
    st = rig.svc.cleaners[MAILBOX].rotate_archive()
    assert not os.path.exists(old) and st.archive_rotated_files == 1
    assert rig.cleanup_logs()[-1]["trigger"] == "archive_rotation"


def test_M194_row_purge_keeps_uidl_for_never_delete(pop_rig):
    """06 §2.6.7 / §2.9.3(R6-26 两条硬约束):行按 `[retention] mail_inbox_rows_days` 删除时,
    把该行 `uidl` 写进 `pop3_uidl_recent`(与删行**同一事务**);下一轮 POP3 不重新登记、不重新告警。"""
    _terminal_rows(pop_rig, n=1, protocol="pop3", oversize=True)
    pop_rig.cfg.retention.mail_inbox_rows_days = 0
    pop_rig.clock.advance(1000)
    assert pop_rig.svc.cleaners[MAILBOX].purge_rows() == 1
    assert pop_rig.inbox() == []
    kept = pop_rig.ms.uidl_recent(f"mail:{MAILBOX}")
    assert "U-os-0" in kept
    pop_rig.svc.fetch_once()
    assert pop_rig.inbox() == [], "凭 pop3_uidl_recent 跳过,不重新登记"
    assert len(pop_rig.events.of(C.MAIL_MSG_OVERSIZE, "firing")) == 1


def test_M195_uidl_lru_protects_never_delete(pop_rig):
    """06 §2.9.3(R6-26 ②)/ §2.6.7:`pop3_uidl_recent` 满员时先挤普通 `uidl`;
    `OUT_OF_SCOPE`/`OVERSIZE` 的 `uidl` 对应的原信还在服务器上,**排在淘汰序列最后**。"""
    owner = f"mail:{MAILBOX}"
    pop_rig.ms.uidl_remember(owner, "PROTECTED-1", protected=True)
    from qtrade_agent.mail.mail_store import POP3_UIDL_RECENT_MAX
    for i in range(POP3_UIDL_RECENT_MAX + 50):
        pop_rig.ms.uidl_remember(owner, f"ORD-{i}")
    kept = pop_rig.ms.uidl_recent(owner)
    assert "PROTECTED-1" in kept and len(kept) <= POP3_UIDL_RECENT_MAX


def test_M196_uidl_recent_cap_is_2000(rig):
    """06 §2.9.3:`pop3_uidl_recent` = 最近 2000 个 UIDL 的 JSON 数组(快速过滤;真去重靠唯一索引)。"""
    from qtrade_agent.mail.mail_store import POP3_UIDL_RECENT_MAX
    assert POP3_UIDL_RECENT_MAX == 2000


def test_M197_disk_high_skips_archive(rig):
    """06 §2.6.10 `high`(< 2 GB):**停邮件归档**——处理完的邮件只在 `mail_inbox` 标 `deleted_ms`、
    不落 `.eml`/sidecar(`archived_path` 留空、`reason += archive_skipped_disk_high`)。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.disk = "high"
    rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    row = rig.inbox()[0]
    assert row["archived_path"] is None and row["deleted_ms"] is not None
    assert "archive_skipped_disk_high" in (row["reason"] or "")


def test_M198_disk_critical_skips_whole_round(rig):
    """06 §2.6.10 `critical`(< 1 GB):整轮**跳过归档与删除**、告警 `MAIL_PAUSED_DISK_FULL`;
    `delete_on_server` 永远不由本地磁盘水位触发(基线 §11.11 [DISK] R4-2)。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.disk = "critical"
    st = rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert st.deleted == 0 and st.archived == 0 and st.paused_disk is True
    assert rig.inbox()[0]["deleted_ms"] is None
    assert rig.events.of(C.MAIL_PAUSED_DISK_FULL, "firing")


def test_M199_cleanup_batch_caps_round(rig):
    """06 §2.6.3 ①:`cands = select … limit cleanup_batch (=200)`;§2.6.6 用 `cleanup_batch` 限每轮体量。"""
    _terminal_rows(rig, n=4)
    rig.cfg.cleanup.cleanup_batch = 2
    rig.cfg.cleanup.retention_days = 0
    st = rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert st.candidates == 2


def test_M200_cleanup_disabled_does_nothing(rig):
    """06 §7 `[mail.cleanup] enabled = true`;关掉后清理不跑。"""
    _terminal_rows(rig, n=1)
    rig.cfg.cleanup.retention_days = 0
    rig.cfg.cleanup.enabled = False
    st = rig.svc.cleaners[MAILBOX].run(rig.imap, "imap")
    assert st.candidates == 0 and st.deleted == 0


# ══════════════════════════════════════════════════ 十四、状态、重新解析与告警码表(06 §2.7 / §3.2)


def test_M201_status_all_routes_shape(mail_api):
    """不带 `route_id`:顶层 `routes:[…]`,每条至少带 `route_id/channel/account_id` 与单条形态的
    `inbound/outbound/cleanup`;默认配置只有全局路由 ⇒ 恰 1 条,`channel`/`account_id` 为 `null`。

    出处:02 §3.4.5 #56「不带 `route_id` 返回 `routes:[{route_id, channel, account_id, …下同}]` 全部启用路由」;
    信封按 02 §3.4 通用 R6-55(字面键集 ⇒ 顶层平铺);键集按 02 §3.4 通用「至少这些键」读(多出的键不判);
    全局路由 = `channel IS NULL AND account_id IS NULL`(06 §3.1 R6-58 (cf) 引 02 §3.1 DDL)。
    ⚠️ 旧版断的 `route.mailbox_key`、`outbound.template_profile` 出自 06 §2.7 示例,02 #56 没有 ⇒ 不断。
    ⚠️ 规格空白:「…下同」是否也含 `route:{…}` 子对象、`enabled` 是逐路由还是 `[mail] enabled` 全局一份
    (列表只含「启用路由」,逐路由 `enabled` 恒真,更像全局)未写死 ⇒ 列表元素里这两个都不断。"""
    m, c = mail_api
    resp = m.get(c, "/mail/status")
    assert resp.status_code == 200, resp.text
    routes = status_routes(resp.json())
    assert len(routes) == 1
    r0 = routes[0]
    assert {"route_id", "channel", "account_id", "inbound", "outbound", "cleanup"} <= set(r0)
    assert r0["channel"] is None and r0["account_id"] is None


def test_M201e_status_envelope_is_flat(mail_api):
    """信封:#56 两种形态都**顶层平铺 + `ok`**,不包 `data` —— 不带 `route_id` 顶层有 `routes`,带则顶层有
    `enabled/route/inbound/outbound/cleanup`。

    出处:02 §3.4 通用「单对象端点的信封(R6-55)」:「响应列直接给出字面键集的端点 **顶层平铺 + `ok`**,不再包 `data`」;
    #56 响应列直接给出字面键集(`routes:[{…}]` / `{enabled, route:{…}, inbound:{…}, outbound:{…}, cleanup:{…}}`),
    不是 00 §7 对象名、也不是 C-42 分页列表。
    ⚠️ 本条是验收方对 R6-55 的逐字判读;#56 行本身没单独写信封,若总控另裁(如定为包 `data`),只改本条。"""
    m, c = mail_api
    whole = m.get(c, "/mail/status").json()
    assert whole.get("ok") is True and isinstance(whole.get("routes"), list), whole
    rid = status_routes(whole)[0]["route_id"]
    one = m.get(c, "/mail/status", route_id=rid).json()
    assert one.get("ok") is True and {"enabled", "route", "inbound", "outbound", "cleanup"} <= set(one), one


def test_M202_status_single_route_shape(mail_api):
    """带 `route_id`:单条 `{enabled, route:{id, channel, account_id, outbound_template_id, inbound_template_id},
    inbound, outbound, cleanup}`,`route.id` 即所查的那条。

    出处:02 §3.4.5 #56「带则单条」及其响应列;信封 R6-55 顶层平铺。
    ⚠️ 规格空白:`route_id` 不存在时回什么(404?空?)02 #56 未写 ⇒ 不断(旧版断「空列表」是实现现状)。"""
    m, c = mail_api
    rid = first_route_id(c, m)
    body = status_single(c, m, rid)
    assert {"enabled", "route", "inbound", "outbound", "cleanup"} <= set(body)
    assert {"id", "channel", "account_id", "outbound_template_id", "inbound_template_id"} <= set(body["route"])
    assert str(body["route"]["id"]) == str(rid)


def test_M203_status_inbound_keys_and_folders(mail_api):
    """`inbound` 至少含 `protocol_configured/protocol_active/fallback/folders/last_success_at/last_error/
    idle_supported/consecutive_failures/quota`;`folders[]` 每项 `{name, uidvalidity, last_uid}` 且含 INBOX 与 Junk;
    `quota` = `{used_mb, limit_mb, source}`;成功收过一轮后 `last_success_at` 为 ISO 8601 带偏移。

    出处:02 §3.4.5 #56 `inbound:{…}` 响应列逐键;INBOX+Junk 两个文件夹见 06 §2.1(IMAP 扫垃圾箱,本文件 M06);
    时间类型 00 §6。`quota` 在 02 里没写 `|null` ⇒ 按对象断键,值不断(规格没定类型)。"""
    m, c = mail_api
    m.rig.ingest_imap(build_command_mail(clock=m.rig.clock, req_id="ST-1", nonce="st1"))
    inb = status_single(c, m, first_route_id(c, m))["inbound"]
    assert {"protocol_configured", "protocol_active", "fallback", "folders", "last_success_at", "last_error",
            "idle_supported", "consecutive_failures", "quota"} <= set(inb)
    for f in inb["folders"]:
        assert {"name", "uidvalidity", "last_uid"} <= set(f)
    assert {"INBOX", "Junk"} <= {f["name"] for f in inb["folders"]}
    assert isinstance(inb["quota"], dict) and {"used_mb", "limit_mb", "source"} <= set(inb["quota"])
    assert_iso(inb["last_success_at"], "inbound.last_success_at")


def test_M203b_status_outbound_keys_and_last_sent_at(mail_api):
    """`outbound` 至少含 `queued/retrying/dead/last_sent_at/consecutive_failures/rate_per_min`;
    发出一封后 `last_sent_at` 为 ISO 8601 带偏移。

    出处:02 §3.4.5 #56 `outbound:{…}` 响应列;时间类型 00 §6。回执由 `OP_DENIED` 触发(06 §2.2 第 3 闸,本文件 M32)。"""
    m, c = mail_api
    m.rig.ingest_imap(build_command_mail(clock=m.rig.clock, op="account_stop", req_id="SO-1", nonce="so1"))
    m.rig.svc.send_once()
    assert m.rig.outbox()[0]["status"] == "SENT"
    out = status_single(c, m, first_route_id(c, m))["outbound"]
    assert {"queued", "retrying", "dead", "last_sent_at", "consecutive_failures", "rate_per_min"} <= set(out)
    assert_iso(out["last_sent_at"], "outbound.last_sent_at")


def test_M203c_status_cleanup_keys_and_time_types(mail_api):
    """`cleanup` 至少含 `last_run_at/last_status/archived_mb/next_run_at`;两个时间键为 ISO 8601 带偏移或 `null`。

    出处:02 §3.4.5 #56 `cleanup:{last_run_at, last_status, archived_mb, next_run_at}`(「清理状态并入此处,
    不单设 `GET /mail/cleanup`」);时间类型 00 §6。未跑过清理时是否为 `null` 规格未写 ⇒ 只断「ISO 或 null」。"""
    m, c = mail_api
    cl = status_single(c, m, first_route_id(c, m))["cleanup"]
    assert {"last_run_at", "last_status", "archived_mb", "next_run_at"} <= set(cl)
    assert_iso_or_none(cl["last_run_at"], "cleanup.last_run_at")
    assert_iso_or_none(cl["next_run_at"], "cleanup.next_run_at")


def test_M203d_status_cleanup_last_run_at_after_a_round(mail_api):
    """跑过一轮清理后 `cleanup.last_run_at` 为 ISO 8601 带偏移的字符串(不是毫秒整数)。

    出处:02 §3.4.5 #56 `cleanup.last_run_at`;00 §6。清理一轮见 06 §2.6(本文件 M186 同一驱动方式)。"""
    m, c = mail_api
    _terminal_rows(m.rig, n=1)
    m.rig.cfg.cleanup.retention_days = 0
    m.rig.svc.cleaners[MAILBOX].run(m.rig.imap, "imap")
    cl = status_single(c, m, first_route_id(c, m))["cleanup"]
    assert_iso(cl["last_run_at"], "cleanup.last_run_at")


def test_M204_inbound_stalled_threshold(rig):
    """06 §2.7:`MAIL_INBOUND_STALLED` 触发 = `now - last_success_at > max(3 × poll_interval_s, 10min)`;
    > 1h 升 crit。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="SL-1", nonce="sl1"))
    f = rig.svc.fetchers[MAILBOX]
    rig.clock.advance(9 * 60 * 1000)
    assert f.check_stalled() is False
    rig.clock.advance(10 * 60 * 1000)          # 共 19 min > max(3×180s, 10min)
    assert f.check_stalled() is True
    firing = rig.events.of(C.MAIL_INBOUND_STALLED, "firing")
    assert firing and firing[0]["payload"]["subject"].startswith("mailbox:")


def test_M205_alert_code_table_is_fourteen():
    """06 §2.7 告警表:`MAIL_*` 一族共 14 个码(02 §3.7 登记同值,R6-35 七种 subject)。"""
    assert len(C.MAIL_ALERT_CODES) == 14


def test_M206_alert_severities():
    """06 §2.7 表「severity」列逐条:`MAIL_AUTH_FAILED`/`MAIL_PAUSED_DISK_FULL`/`MAIL_WATERMARK_STALLED` crit;
    `MAIL_PARSE_FAILED` info;`MAIL_ENDPOINT_CHANGED` info;其余 warn。"""
    assert C.MAIL_ALERT_CODES[C.MAIL_AUTH_FAILED] == "crit"
    assert C.MAIL_ALERT_CODES[C.MAIL_PAUSED_DISK_FULL] == "crit"
    assert C.MAIL_ALERT_CODES[C.MAIL_WATERMARK_STALLED] == "crit"
    assert C.MAIL_ALERT_CODES[C.MAIL_PARSE_FAILED] == "info"
    assert C.MAIL_ALERT_CODES[C.MAIL_ENDPOINT_CHANGED] == "info"
    for code in (C.MAIL_INBOUND_STALLED, C.MAIL_QUOTA_HIGH, C.MAIL_SMTP_FAILING, C.MAIL_OUTBOX_DEAD,
                 C.MAIL_CLEANUP_FAILED, C.MAIL_SENDER_DENIED, C.MAIL_MSG_OVERSIZE,
                 C.MAIL_PROTOCOL_FALLBACK, C.MAIL_ROUTE_UNRESOLVED):
        assert C.MAIL_ALERT_CODES[code] == "warn"


def test_M207_mail_alerts_are_event_family_mail(rig):
    """06 §2.7 / §3.2 末(C-14/C-15):`Event.event = "mail"` **只是告警**,payload = 基线 §7.5 告警类统一结构。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, from_addr="x@evil.example"))
    ev = rig.events.of(C.MAIL_SENDER_DENIED, "firing")[0]
    assert ev["event"] == "mail"
    assert {"code", "severity", "state", "subject", "first_seen_at", "last_seen_at", "count",
            "evidence"} <= set(ev["payload"])


def test_M208_alert_dedup_key_code_subject(rig):
    """06 §2.7:去重键 `(code, subject)`;同键持续触发只更新 `last_seen_at/count`,
    恢复时发一次 `state=resolved`。"""
    for i in range(3):
        rig.ingest_imap(build_command_mail(clock=rig.clock, from_addr="x@evil.example",
                                           req_id=f"EV-{i}", nonce=f"ev{i}", message_id=f"<ev{i}@x>"))
    assert len(rig.events.of(C.MAIL_SENDER_DENIED, "firing")) == 1
    assert rig.alerts.active[(C.MAIL_SENDER_DENIED, "sender:x@evil.example")].count == 3


def test_M209_parse_failed_alert_for_whitelisted(rig):
    """06 §2.7:`MAIL_PARSE_FAILED`(`subject=inbox:<id>`,info)—— 白名单发件人的指令邮件
    `PARSE_FAILED/SIG_INVALID/EXPIRED`。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, signature="0" * 64, req_id="PF-1", nonce="pf1"))
    firing = rig.events.of(C.MAIL_PARSE_FAILED, "firing")
    assert firing and firing[0]["payload"]["subject"] == f"inbox:{rig.one_inbox()['id']}"
    assert firing[0]["payload"]["severity"] == "info"


def test_M209b_expired_also_alerts(rig):
    """06 §2.7 同一行:`EXPIRED` 也计入 `MAIL_PARSE_FAILED`(可能是对方模板/时钟问题)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, timestamp=iso_of(rig.clock() - 3600_000),
                                       req_id="PF-2", nonce="pf2"))
    assert rig.one_inbox()["status"] == C.EXPIRED
    assert rig.events.of(C.MAIL_PARSE_FAILED, "firing")


def test_M210_reparse_reuses_same_row(rig):
    """06 §3.2 / 02 #60 `POST /mail/inbox/{id}/reparse`:模板修正后对老邮件重跑,
    原文从库里已存的 `body_text` 重建,**不落新行**(同一 `mail_inbox.id` 原地改 `status`/`reason`)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="account_stop", req_id="RP-1", nonce="rp1"))
    row = rig.one_inbox()
    assert row["status"] == C.OP_DENIED
    rig.cfg.inbound.allow_ops = ["*", "account_stop"]
    rig.svc.reload()
    got = rig.svc.reparse(row["id"])
    assert len(rig.inbox()) == 1 and rig.inbox()[0]["id"] == row["id"]
    assert got == rig.inbox()[0]["status"]


def test_M211_capabilities_single_source(rig):
    """06 §2.8「单一来源(E-2)」:`操作` 取值集合与每个 op 的参数名/类型/必填、
    `confirmable`/`danger` 属性全部来自 02 §3.10 能力目录;本册不复制目录。"""
    cat = Catalog.from_dicts(CAPS)
    cap = cat.get("send_text")
    assert cap.confirmable is True and cap.danger is False and cap.kind == "write"
    assert "text" in cap.properties and cap.required == ["text"]
    assert cat.get("不存在的op") is None


def test_M212_unknown_arg_property_rejected(rig):
    """06 §2.3.2「参数」:参数名逐字 = 该 op `args_schema` 的属性名(E-2),
    未知属性 ⇒ `INVALID_ARGS` 带 JSON Pointer(与 02 `bus` 校验同一条路)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text",
                                       args={"text": "x", "不存在的参数": 1}, req_id="AR-1", nonce="ar1"))
    body = rig.outbox(kind="receipt")[-1]["body_text"]
    assert "INVALID_ARGS" in body


# ══════════════════════════════════════════════════ 十五、02 的 mail_* 表语义(02 §3.1 DDL)


def test_M213_inbox_status_check_mirrors_spec(rig):
    """02 §3.1 `mail_inbox.status` CHECK 是 06 §2.3.5 的镜像,**必须逐值对齐**——
    06 一旦写这些 status,旧 CHECK 会直接 INSERT 失败。"""
    ddl = rig.store.con.execute(
        "select sql from sqlite_master where type='table' and name='mail_inbox'").fetchone()[0]
    spec_all = set(C.TERMINAL) | {C.RECEIVED, C.ACCEPTED, C.DONE, C.CONFIRM_REQUIRED}
    for st in spec_all:
        assert f"'{st}'" in ddl, f"{st} 不在 mail_inbox.status 的 CHECK 里"


def test_M214_inbox_unique_constraints(rig):
    """02 §3.1 / 06 §2.5 第 1、2 层:`UNIQUE (mailbox, folder, uidvalidity, uid)`、
    `UNIQUE (mailbox, uidl)`、`UNIQUE (rfc_message_id)`。"""
    ddl = rig.store.con.execute(
        "select sql from sqlite_master where type='table' and name='mail_inbox'").fetchone()[0].replace(" ", "")
    assert "UNIQUE(mailbox,folder,uidvalidity,uid)" in ddl
    assert "UNIQUE(mailbox,uidl)" in ddl and "UNIQUE(rfc_message_id)" in ddl


def test_M215_inbox_nonce_partial_index(rig):
    """06 §3.1 `nonce`(99c C-04):唯一 `(from_addr, nonce)`,部分索引 `WHERE nonce IS NOT NULL`
    ——没有这列 `DUPLICATE_NONCE` 判不出来,验签退化成可重放。"""
    idx = rig.store.con.execute(
        "select sql from sqlite_master where type='index' and name='ux_inbox_nonce'").fetchone()[0]
    assert "from_addr" in idx and "nonce" in idx and "WHERE nonce IS NOT NULL" in idx


def test_M216_protocol_key_check(rig):
    """02 §3.1:`CHECK ((protocol='imap' AND uid IS NOT NULL) OR (protocol='pop3' AND uidl IS NOT NULL))`。"""
    with pytest.raises(Exception):
        rig.store.con.execute(
            "insert into mail_inbox(mailbox,protocol,folder,rfc_message_id,from_addr,subject,"
            "received_ms,body_sha256) values('m','imap','INBOX','<x@x>','a@b','s',1,'h')")
        rig.store.con.commit()


def test_M217_outbox_status_enum(rig):
    """06 §3.1 / 02 §3.1:`mail_outbox.status ∈ {QUEUED,SENDING,SENT,RETRY,DEAD,DISCARDED}`。"""
    ddl = rig.store.con.execute(
        "select sql from sqlite_master where type='table' and name='mail_outbox'").fetchone()[0]
    for st in ("QUEUED", "SENDING", "SENT", "RETRY", "DEAD", "DISCARDED"):
        assert f"'{st}'" in ddl


def test_M218_outbox_kind_enum(rig):
    """06 §3.1:`mail_outbox.kind ∈ {message, receipt, alert}`(信息通知 + 回执 + 告警)。"""
    ddl = rig.store.con.execute(
        "select sql from sqlite_master where type='table' and name='mail_outbox'").fetchone()[0]
    assert "kind IN ('message','receipt','alert')" in ddl.replace('"', "")


def test_M219_cleanup_log_trigger_check(rig):
    """02 §3.1(R6-31):`trigger` CHECK 必须含 `max_kept`——缺它则该轮清理的 INSERT 必被 CHECK 拒。"""
    ddl = rig.store.con.execute(
        "select sql from sqlite_master where type='table' and name='mail_cleanup_log'").fetchone()[0]
    for t in ("retention", "capacity", "max_kept", "manual", "archive_rotation"):
        assert f"'{t}'" in ddl


def test_M220_route_account_glob_check(rig):
    """02 §3.1 `mail_routes`:账号级必须带通道,且 `account_id` 形如 `qd/qq/wx + NN` 与通道匹配。"""
    with pytest.raises(Exception):
        rig.store.con.execute(
            "insert into mail_routes(channel, account_id, created_ms, updated_ms) values('qq','qd01',1,1)")
        rig.store.con.commit()


def test_M221_confirm_pending_index(rig):
    """02 §3.1(R6-7):`ix_inbox_confirm_pending ON mail_inbox (confirm_expires_ms) WHERE status='CONFIRM_REQUIRED'`
    ——过期 reaper 每 60s 的扫描面,只覆盖待确认行。"""
    idx = rig.store.con.execute(
        "select sql from sqlite_master where type='index' and name='ix_inbox_confirm_pending'").fetchone()[0]
    assert "confirm_expires_ms" in idx and "CONFIRM_REQUIRED" in idx


def test_M222_inbox_body_text_only_in_scope(rig):
    """06 §3.1 `body_text`:解码后正文(**范围内邮件才存**;`OUT_OF_SCOPE` 空)。"""
    rig.ingest_imap(plain_mail(subject="不相干的主题"))
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="BT-1", nonce="bt1"))
    rows = rig.inbox()
    assert rows[0]["status"] == C.OUT_OF_SCOPE and not rows[0]["body_text"]
    assert rows[1]["body_text"]


# ══════════════════════════════════════════════════ 十六、配置项默认值(06 §7,唯一出处)


def test_M223_mail_top_defaults():
    """06 §7 `[mail]`:`enabled = false`(总开关;关闭时收/发/清理线程都不起)、`template_version = "v1"`。"""
    cfg = MailConfig()
    assert cfg.enabled is False and cfg.template_version == "v1"


def test_M224_inbound_defaults():
    """06 §7 `[mail.inbound]`:`protocol="imap"`、`port=993`、`ssl=true`、`level="admin"`、
    `folders=["INBOX","Junk"]`、`processed_folder="QTrade/processed"`、`poll_interval_s=180`、`idle=true`。"""
    i = MailInboundConfig()
    assert i.protocol == "imap" and i.port == 993 and i.ssl is True and i.level == "admin"
    assert i.folders == ["INBOX", "Junk"] and i.processed_folder == "QTrade/processed"
    assert i.poll_interval_s == 180 and i.idle is True


def test_M225_inbound_limits_defaults():
    """06 §7 `[mail.inbound]`:`max_retr_per_round=200`、`max_message_bytes=26214400`(25 MB)、
    `keep_raw=false`、`send_imap_id=true`、`allowed_senders=[]`(空 = 不接受任何指令)。"""
    i = MailInboundConfig()
    assert i.max_retr_per_round == 200 and i.max_message_bytes == 26214400
    assert i.keep_raw is False and i.send_imap_id is True and i.allowed_senders == []


def test_M226_inbound_security_defaults():
    """06 §7 `[mail.inbound]`:`require_signature=true`、`sig_time_tolerance_s=600`、`nonce_ttl_h=24`、
    `allow_ops=["*"]`(语义 = 全部 danger=false)、`max_timeout_ms=120000`、`reply_on_parse_failure=true`。"""
    i = MailInboundConfig()
    assert i.require_signature is True and i.sig_time_tolerance_s == 600 and i.nonce_ttl_h == 24
    assert i.allow_ops == ["*"] and i.max_timeout_ms == 120000 and i.reply_on_parse_failure is True


def test_M227_fallback_defaults():
    """06 §7 `[mail.inbound.fallback]`:`enabled=true`、`host=""`(空 = 不回落只告警)、`port=995`、
    `ssl=true`、`after_failures=3`、`recheck_min=30`。"""
    f = MailFallbackConfig()
    assert f.enabled is True and f.host == "" and f.port == 995 and f.ssl is True
    assert f.after_failures == 3 and f.recheck_min == 30


def test_M228_outbound_defaults():
    """06 §7 `[mail.outbound]`:`enabled=true`、`port=465`、`ssl=true`、`timeout_s=30`、
    `include_self=true`、`enabled_channels=["qidian","qq","wechat"]`、`only_groups=false`、
    `receipt_to_sender=true`、`alert_on_endpoint_change=true`。"""
    o = MailOutboundConfig()
    assert o.enabled is True and o.port == 465 and o.ssl is True and o.timeout_s == 30
    assert o.include_self is True and o.enabled_channels == ["qidian", "qq", "wechat"]
    assert o.only_groups is False and o.receipt_to_sender is True and o.alert_on_endpoint_change is True


def test_M229_outbound_size_defaults():
    """06 §7 / §2.4.1 末:`max_attachment_mb=20`、`max_mail_mb=40`、`downscale_oversize_images=true`。"""
    o = MailOutboundConfig()
    assert o.max_attachment_mb == 20 and o.max_mail_mb == 40 and o.downscale_oversize_images is True


def test_M230_cleanup_defaults():
    """06 §7 `[mail.cleanup]`:`retention_days=7`(E-18 不变仍 7)、`archive_before_delete=true`、
    `archive_dir="/var/lib/qtrade/mail/archive"`、`archive_retention_days=7`、`archive_max_mb=2048`、
    `cleanup_interval_min=60`、`cleanup_batch=200`、`mailbox_quota_watermark=0.8`、
    `mailbox_quota_mb_assumed=0`、`max_kept_count=5000`、`stall_alert_rounds=3`。"""
    from qtrade_agent.mail.config import MailCleanupConfig
    c = MailCleanupConfig()
    assert c.retention_days == 7 and c.archive_before_delete is True
    assert c.archive_dir == "/var/lib/qtrade/mail/archive" and c.archive_retention_days == 7
    assert c.archive_max_mb == 2048 and c.cleanup_interval_min == 60 and c.cleanup_batch == 200
    assert c.mailbox_quota_watermark == 0.8 and c.mailbox_quota_mb_assumed == 0
    assert c.max_kept_count == 5000 and c.stall_alert_rounds == 3


def test_M231_retention_three_watermarks():
    """06 §7 注 / §2.6.10(E-18,基线 §11.11 [DISK]):三级水位引用 02 `[retention]`
    `disk_warn_mb=5120 / disk_high_mb=2048 / disk_critical_mb=1024`;`mail_inbox_rows_days=30`。"""
    from qtrade_agent.mail.config import MailRetentionConfig
    r = MailRetentionConfig()
    assert r.disk_warn_mb == 5120 and r.disk_high_mb == 2048 and r.disk_critical_mb == 1024
    assert r.mail_inbox_rows_days == 30


def test_M232_secret_refs_default():
    """06 §7:密钥引用 `vault://mail/imap`(pop3 用 `vault://mail/pop3`)与 `vault://mail/smtp`;
    配置文件不落明文(§6.6)。"""
    assert MailInboundConfig().secret_ref == "vault://mail/imap"
    assert MailOutboundConfig().secret_ref == "vault://mail/smtp"


def test_M233_from_toml_dict_uses_defaults():
    """06 §7 / 00 §5:`[mail]` 子树缺省键取默认值(缺省即可跑)。"""
    cfg = MailConfig.from_toml_dict({"enabled": True,
                                     "inbound": {"host": "imap.163.com", "user": "ops@163.com"}})
    assert cfg.enabled is True and cfg.inbound.host == "imap.163.com"
    assert cfg.inbound.protocol == "imap" and cfg.cleanup.retention_days == 7


# ══════════════════════════════════════════════════ 十六、收发件与清理日志端点出参(02 §3.4.5 #58/#59/#61/#65,R6-64 ①)
#
# 只断 02 行里写死的东西:#58/#59/#61 的出参由 R6-64 ① 登记进 02(时间 ISO 的 `*_at`、库列 `*_ms` 不下发、
# `id` 字符串、列表不带正文、#61 显式 13 键与排除列);#65 只有「分页」二字 ⇒ 只断 C-42 通用分页信封与 00 §6 时间口径。

INBOX_TIME_KEYS = ("date_at", "received_at", "confirm_expires_at", "archived_at", "deleted_at")
INBOX_TIME_COLS = ("date_ms", "received_ms", "confirm_expires_ms", "archived_ms", "deleted_ms")
OUTBOX_KEYS = {"id", "kind", "route_id", "route", "to", "subject", "status", "attempts", "next_attempt_at",
               "last_error", "ref", "created_at", "sent_at"}
OUTBOX_EXCLUDED = {"body_text", "body_html", "dedup_key", "smtp_response", "rfc_message_id", "cc_addrs", "attachments_json"}


def _seed_inbox_and_receipt(r):
    """一封 `OP_DENIED` 指令信 ⇒ `mail_inbox` 1 行 + 回执 `mail_outbox` 1 行(06 §2.2 第 3 闸,本文件 M32)。"""
    r.ingest_imap(build_command_mail(clock=r.clock, op="account_stop", req_id="IO-1", nonce="io1"))
    assert len(r.inbox()) == 1 and len(r.outbox(kind="receipt")) == 1


def test_M240_inbox_list_envelope_and_no_body_text(mail_api):
    """#58 `GET /mail/inbox`:`{ok, data:[行], next_cursor}`;行**不含 `body_text`**。

    出处:02 §3.4.5 #58「`mail_inbox` 行(不含 `body_text`,详情才给)」+ 🔵 出参(R6-64 ①)`{ok, data:[行], next_cursor}`。"""
    m, c = mail_api
    _seed_inbox_and_receipt(m.rig)
    resp = m.get(c, "/mail/inbox")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True and isinstance(body.get("data"), list) and "next_cursor" in body, body
    assert len(body["data"]) == 1
    assert "body_text" not in body["data"][0]


def test_M241_inbox_row_times_are_iso_at_keys(mail_api):
    """#58 行:五个时间列换成 ISO 8601 的 `*_at`(`date_at/received_at/confirm_expires_at/archived_at/deleted_at`),
    库列 `*_ms` **不下发**;`received_at` 必有值。

    出处:02 §3.4.5 #58 🔵 出参 ①「五个时间列换成 ISO 8601 的 `*_at` …(库列 `*_ms` 不下发;基线 §6)」。"""
    m, c = mail_api
    _seed_inbox_and_receipt(m.rig)
    row = m.get(c, "/mail/inbox").json()["data"][0]
    for k in INBOX_TIME_KEYS:
        assert k in row, f"#58 行缺 `{k}`"
        assert_iso_or_none(row[k], k)
    assert_iso(row["received_at"], "received_at")
    for col in INBOX_TIME_COLS:
        assert col not in row, f"#58:库列 `{col}` 不得下发"


def test_M242_inbox_row_ids_strings_and_derived_keys(mail_api):
    """#58 行:`id` 出**字符串**(`first_inbox_id` 非空时同);`sig_ok` 为布尔或 `null`;补派生键 `route`(全局路由 =
    `"default"`)与 `archived`(布尔)。

    出处:02 §3.4.5 #58 🔵 出参 ②③④:「`id` 与 `first_inbox_id` 出字符串」「`sig_ok` 出布尔(未验签 `null`)」
    「`route` = scope 名(`default` / …;没绑路由为 `null`)」「`archived` = 已归档」。"""
    m, c = mail_api
    _seed_inbox_and_receipt(m.rig)
    row = m.get(c, "/mail/inbox").json()["data"][0]
    assert isinstance(row["id"], str)
    assert row.get("first_inbox_id") is None or isinstance(row["first_inbox_id"], str)
    assert row["sig_ok"] is None or isinstance(row["sig_ok"], bool)
    assert row["route"] == "default"
    assert isinstance(row["archived"], bool)


def test_M243_inbox_detail_same_view_plus_body_text(mail_api):
    """#59 `GET /mail/inbox/{id}`:`{ok, data:{…}}`,`data` = #58 同一视图 + `body_text`;不存在 ⇒ `404 TARGET_NOT_FOUND`。

    出处:02 §3.4.5 #59 🔵 出参(R6-64 ①)「`data` 与 #58 的行同一个视图,只多一个 `body_text`」「不存在 ⇒ 404」;
    错误码见 00 §10 / 02 §3.4 状态映射。"""
    m, c = mail_api
    _seed_inbox_and_receipt(m.rig)
    listed = m.get(c, "/mail/inbox").json()["data"][0]
    resp = m.get(c, f"/mail/inbox/{listed['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True and isinstance(body.get("data"), dict), body
    detail = body["data"]
    assert "body_text" in detail and detail["body_text"]
    assert {k: v for k, v in detail.items() if k != "body_text"} == listed
    miss = m.get(c, "/mail/inbox/99999999")
    assert miss.status_code == 404 and miss.json()["code"] == "TARGET_NOT_FOUND"


def test_M244_outbox_list_thirteen_keys_no_body(mail_api):
    """#61 `GET /mail/outbox`:`{ok, data:[行], next_cursor}`;行含显式 13 键、`id` 字符串、`to` = 收件地址、
    时间 ISO;**不带正文与投递内部列**。

    出处:02 §3.4.5 #61 🔵 出参(R6-64 ①)「行 = 显式 13 键 …(时间一律 ISO 8601)」「列表不带正文(`body_text`/`body_html`)
    与投递内部列(`dedup_key`/`smtp_response`/`rfc_message_id`/`template_*`/`cc_addrs`/`attachments_json` 等)」。"""
    m, c = mail_api
    _seed_inbox_and_receipt(m.rig)
    m.rig.svc.send_once()
    body = m.get(c, "/mail/outbox").json()
    assert body.get("ok") is True and isinstance(body.get("data"), list) and "next_cursor" in body, body
    row = body["data"][0]
    assert OUTBOX_KEYS <= set(row)
    assert not (OUTBOX_EXCLUDED & set(row)), OUTBOX_EXCLUDED & set(row)
    assert not [k for k in row if k.startswith("template_")]
    assert isinstance(row["id"], str)
    assert SENDER in (row["to"] if isinstance(row["to"], str) else json.dumps(row["to"]))
    assert_iso(row["created_at"], "created_at")
    assert_iso(row["sent_at"], "sent_at")
    assert row["route"] == "default"


def test_M245_outbox_next_attempt_zero_is_null(mail_api):
    """#61:库列 `next_attempt_ms` 为 `0`(DDL 缺省 = 没有下一次)时 `next_attempt_at` 回 **`null`**,不回 1970 年那个时刻。

    出处:02 §3.4.5 #61 🔵 出参「`next_attempt_at`:库列 `next_attempt_ms` 为 `0` … 时回 `null`」。
    夹具把该列直接置成 DDL 缺省值 0(02 §3.1),不依赖实现何时写 0。"""
    m, c = mail_api
    _seed_inbox_and_receipt(m.rig)
    m.rig.store.con.execute("update mail_outbox set next_attempt_ms=0")
    row = m.get(c, "/mail/outbox").json()["data"][0]
    assert row["next_attempt_at"] is None


def test_M246_cleanup_log_is_paginated_with_next_cursor(mail_api):
    """#65 `GET /mail/cleanup/log`:「分页」⇒ C-42 通用信封 `{ok:true, data:[…], next_cursor}`;两轮日志、`limit=1`
    ⇒ 第一页满页、`next_cursor` 非空,透传后拿到的是另一行。

    出处:02 §3.4.5 #65「分页」+ 02 §3.4 通用 C-42(`?since&until&limit&cursor`,响应 `{ok:true, data:[…], next_cursor}`,
    cursor 客户端只透传)。⚠️ 行键集规格空白(R6-64 末「#65 出参视图留下一批」)⇒ 行内只断 00 §6 时间口径(见 M247)。"""
    m, c = mail_api
    _terminal_rows(m.rig, n=2)
    m.rig.cfg.cleanup.retention_days = 0
    m.rig.svc.cleaners[MAILBOX].run(m.rig.imap, "imap")
    m.rig.clock.advance(1000)
    m.rig.svc.cleaners[MAILBOX].run(m.rig.imap, "imap")
    assert len(m.rig.cleanup_logs()) >= 2
    p1 = m.get(c, "/mail/cleanup/log", limit=1)
    assert p1.status_code == 200, p1.text
    b1 = p1.json()
    assert b1.get("ok") is True and isinstance(b1.get("data"), list) and len(b1["data"]) == 1, b1
    assert b1.get("next_cursor"), "满页必须给 next_cursor(C-42)"
    b2 = m.get(c, "/mail/cleanup/log", limit=1, cursor=b1["next_cursor"]).json()
    assert len(b2["data"]) == 1 and b2["data"][0] != b1["data"][0]


def test_M247_cleanup_log_rows_have_no_ms_time_keys(mail_api):
    """#65 行:不下发存储口径的 `*_ms` 时间键(API 时间一律 ISO 8601 带偏移)。

    出处:00 §6「时间(存储)= INTEGER 毫秒、列名后缀 `_ms`」vs「时间(API/事件/邮件)= ISO 8601 带时区偏移」;
    `*_ms` 沿用毫秒的只有 00 §7.1 R6-62 (f) 登记的两个 Account 例外键,#65 不在其列。行的具体键名规格空白,不断。"""
    m, c = mail_api
    _terminal_rows(m.rig, n=1)
    m.rig.cfg.cleanup.retention_days = 0
    m.rig.svc.cleaners[MAILBOX].run(m.rig.imap, "imap")
    rows = m.get(c, "/mail/cleanup/log").json()["data"]
    assert rows
    leaked = [k for k in rows[0] if k.endswith("_ms")]
    assert not leaked, f"#65 行下发了存储口径的毫秒时间键 {leaked}"
