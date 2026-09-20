"""入站邮件解析 —— 规格:docs/06 §2.1(取信解码三级 + 附件识别)、§2.3.1~§2.3.3(主题/正文/容错)、§2.14.4~§2.14.5(别名与选 parser)。

两层:
- ``parse_mime(raw)``:MIME → 头 + 正文(三级解码)+ 附件(字节头判定)+ ``body_sha256``(§2.5 第 3 层)。
- ``parse_command_body(...)``:正文 → 指令字段(标题行之后、容错表逐条、别名归一、续行规则)。

解析**不依赖主题取值**(与 ibquote 相同:主题只落库追溯);主题与正文不一致以正文为准,记 ``subject_mismatch``。
"""
from __future__ import annotations

import email
import hashlib
import json
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime, parseaddr
from typing import Any, Optional

from .config import InboundTemplateConfig

BODY_MAX_BYTES = 64 * 1024          # §2.3.3:正文 > 64KB 只解析前 64KB,记 body_truncated

# §2.3.2 规范键名(左列);顺序即文档顺序
CANONICAL_KEYS = ("指令ID", "账号", "通道", "操作", "会话", "参数", "确认", "超时", "时间戳", "随机数", "签名")
ARGS_PREFIX = "参数."                 # §2.3.2「按 op 展开形式」:参数.{键}：{值}

# §2.3.1 / §2.6.4:邮件客户端给主题加的前缀,判别时先剥掉
SUBJECT_REPLY_PREFIXES = ("回复：", "回复:", "Re:", "RE:", "re:", "答复：", "答复:")
SUBJECT_FORWARD_PREFIXES = ("转发：", "转发:", "FW:", "Fw:", "fw:", "FWD:", "Fwd:", "fwd:")

# §2.3.3:引用/转发分隔符——标题行之后再出现即停止解析
_STOP_MARKERS = (
    "-----Original Message-----",
    "------------------ 原始邮件 ------------------",
    "---原始邮件---",
    "发件人：",
)

# §2.1 附件识别:判定顺序 字节头 → MIME 声明 → 扩展名
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"%PDF", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
)
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".pdf": "application/pdf", ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def sniff_mime(data: bytes, declared: Optional[str] = None, name: Optional[str] = None) -> str:
    """§2.1「附件识别」:``Content-Type`` 不可信,判定顺序 **字节头 → MIME 声明 → 扩展名**;都判不出记 ``application/octet-stream``。"""
    head = data[:16]
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if declared and declared != "application/octet-stream":
        return declared
    low = (name or "").lower()
    for ext, mime in _EXT_MIME.items():
        if low.endswith(ext):
            return mime
    return "application/octet-stream"


@dataclass
class Attachment:
    name: str
    data: bytes
    mime_sniffed: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def is_image(self) -> bool:
        return self.mime_sniffed in _IMAGE_MIMES

    def to_json(self, media_ref: Optional[str] = None) -> dict[str, Any]:
        """``mail_inbox.attach_json`` 的一项(§3.1):``[{name,size,mime_sniffed,sha256,media_ref}]``。"""
        return {"name": self.name, "size": self.size, "mime_sniffed": self.mime_sniffed,
                "sha256": self.sha256, "media_ref": media_ref}


@dataclass
class ParsedMail:
    rfc_message_id: Optional[str]
    from_addr: str
    to_addrs: str
    subject: str
    date_ms: Optional[int]
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    body_truncated: bool = False

    @property
    def body_sha256(self) -> str:
        """§2.5 第 3 层:``sha256(规范化正文 ‖ 各附件 sha256)``。"""
        h = hashlib.sha256()
        h.update(re.sub(r"\s+", " ", self.body_text).strip().encode("utf-8"))
        for a in sorted(self.attachments, key=lambda x: x.name):
            h.update(b"|")
            h.update(a.sha256.encode("ascii"))
        return h.hexdigest()

    def message_id_or_hash(self) -> str:
        """§2.5 第 2 层:``Message-ID`` 缺失时退化为整封原文哈希,落库写 ``sha256-…``(§3.1)。"""
        return self.rfc_message_id or ("sha256-" + self.body_sha256)


def _decode_header_value(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:                                        # noqa: BLE001 —— 畸形头不该让整封失败
        return raw


def _html_to_text(html: str) -> str:
    """§2.1 三级解码的第三级:HTML 剥标签(``<br>``/块级闭标签断行、script/style 整段删、实体还原)。

    ibquote 2026-08-14 生产实证:腾讯企业邮箱程序发信只有 ``text/html``,本系统对端也只发 ``MIMEText(html)``,
    故 HTML 路径**不是兜底而是主路径**。
    """
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)\s*>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    import html as html_mod
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return "\n".join(line.strip() for line in s.splitlines())


def _payload_text(part: Message) -> str:
    data = part.get_payload(decode=True) or b""
    charset = (part.get_content_charset() or "utf-8").lower()
    for enc in (charset, "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_mime(raw: bytes) -> ParsedMail:
    """取信解码(§2.1「取信解码」三级优先):``.txt`` 附件 → ``plain`` 份 → **HTML 剥标签**。"""
    msg = email.message_from_bytes(raw)
    from_addr = parseaddr(_decode_header_value(msg.get("From")))[1].strip().lower()
    to_addrs = _decode_header_value(msg.get("To"))
    subject = _decode_header_value(msg.get("Subject"))
    mid = (msg.get("Message-ID") or "").strip() or None
    date_ms: Optional[int] = None
    if msg.get("Date"):
        try:
            date_ms = int(parsedate_to_datetime(msg["Date"]).timestamp() * 1000)
        except (TypeError, ValueError):
            date_ms = None

    txt_attach: Optional[str] = None
    plain: Optional[str] = None
    html: Optional[str] = None
    attachments: list[Attachment] = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart":
            continue
        filename = _decode_header_value(part.get_filename())
        disp = (part.get_content_disposition() or "").lower()
        ctype = (part.get_content_type() or "").lower()
        if filename and disp != "inline" or (disp == "attachment" and filename):
            data = part.get_payload(decode=True) or b""
            if filename.lower().endswith(".txt") and txt_attach is None:
                txt_attach = data.decode("utf-8", errors="replace")     # ① .txt 附件优先
            attachments.append(Attachment(name=filename, data=data,
                                          mime_sniffed=sniff_mime(data, ctype, filename),
                                          sha256=hashlib.sha256(data).hexdigest()))
            continue
        if ctype == "text/plain" and plain is None:
            plain = _payload_text(part)                                 # ② plain 份(含 alternative 下钻)
        elif ctype == "text/html" and html is None:
            html = _payload_text(part)                                  # ③ HTML 剥标签兜底(实为主路径)

    body = txt_attach if txt_attach is not None else (plain if plain is not None else (_html_to_text(html) if html else ""))
    truncated = False
    if len(body.encode("utf-8")) > BODY_MAX_BYTES:
        body = body.encode("utf-8")[:BODY_MAX_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return ParsedMail(rfc_message_id=mid, from_addr=from_addr, to_addrs=to_addrs, subject=subject,
                      date_ms=date_ms, body_text=body, attachments=attachments, body_truncated=truncated)


def strip_subject_prefixes(subject: str) -> str:
    """§2.3.1:判别前先剥 ``回复：``/``Re:``/``转发：``/``FW:`` 这类客户端前缀(可叠加多层)。"""
    s = (subject or "").strip()
    changed = True
    while changed:
        changed = False
        for p in SUBJECT_REPLY_PREFIXES + SUBJECT_FORWARD_PREFIXES:
            if s.startswith(p):
                s = s[len(p):].strip()
                changed = True
    return s


def subject_in_scope(subject: str, prefixes: list[str]) -> bool:
    """§2.6.4 范围圈定的主题一半:剥前缀后以 ``scope_subject_prefix`` 之一开头。

    ⚠️ 张力(见 handoff「建议裁决 ①」):默认前缀里的 ``转发：微信消息`` 本身以 ``转发：`` 开头,
    只看剥后串会把我们自己发的信息邮件判成范围外,故**原串与剥后串任一命中即在范围内**。
    """
    raw = (subject or "").strip()
    stripped = strip_subject_prefixes(subject)
    return any(raw.startswith(p) or stripped.startswith(p) for p in prefixes if p)


def title_line_regex(title_line: str) -> re.Pattern:
    """把 ``title_line``(如 ``QTrade 指令 {template_version}``)编成捕获模板版本的正则(§2.14.4/§2.14.5)。"""
    parts = title_line.split("{template_version}")
    escaped = r"v(\d+)".join(re.escape(p) for p in parts)
    return re.compile(r"^\s*" + escaped + r"\s*$")


def subject_pattern_regex(subject_pattern: str) -> re.Pattern:
    """§2.14.5:主题 pattern 的占位符转成捕获组,用于取 ``{template_version}`` 与做形态匹配。"""
    tokens = re.split(r"(\{[a-z_]+\})", subject_pattern)
    out = []
    for t in tokens:
        if t.startswith("{") and t.endswith("}"):
            name = t[1:-1]
            out.append(rf"(?P<{name}>" + (r"v\d+" if name == "template_version" else r".+?") + r")")
        else:
            out.append(re.escape(t))
    return re.compile(r"^\s*" + "".join(out) + r"\s*$")


def scope_prefix_from_pattern(subject_pattern: str) -> str:
    """§2.14.3 末:``scope_subject_prefix`` **从生效模板的 ``subject_pattern`` 推导**(取第一个占位符之前的字面量前缀)。"""
    idx = subject_pattern.find("{")
    prefix = subject_pattern if idx < 0 else subject_pattern[:idx]
    # 默认三份模板推导出 ["QTRADE指令", "转发：微信消息", "QTRADE回执"]:尾部的分隔符号(空格/括号/连字符)不属于前缀
    return prefix.strip().rstrip(" [(【-·—")


@dataclass
class ParsedCommand:
    """§2.3.2 解析结果;``status``/``reason`` 取值见 §2.3.5,由 ingest 落库。"""
    ok: bool = False
    status: Optional[str] = None          # None = 解析通过;否则 UNSUPPORTED / PARSE_FAILED
    reason: str = ""
    template_version: Optional[str] = None
    req_id: str = ""
    account_id: str = ""
    channel: Optional[str] = None
    op: str = ""
    session: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    confirm: Optional[bool] = None
    timeout_ms: Optional[int] = None
    timestamp: str = ""
    nonce: str = ""
    signature: str = ""
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    @property
    def reason_text(self) -> str:
        """``mail_inbox.reason`` = 机器码 + 人话;解析备注以 ``;`` 连接(§2.3.3 各条的 ``reason +=``)。"""
        parts = [p for p in ([self.reason] if self.reason else []) + self.notes if p]
        return ";".join(parts)


def _normalize_key(raw: str) -> str:
    """§2.3.3:键名前后空白剥掉、半角括号归一到全角(同 ibquote ``_KV_LINE``)。"""
    return raw.strip().replace("(", "（").replace(")", "）")


def _alias_map(template: InboundTemplateConfig) -> dict[str, str]:
    """别名 → 规范键名(§2.14.4:规范键名固定,别名只增不改;归一后再走 §2.3.3 容错)。"""
    m = {k: k for k in CANONICAL_KEYS}
    for canonical, aliases in (template.aliases or {}).items():
        for a in aliases or []:
            m[str(a)] = canonical
    return m


_KV_LINE = re.compile(r"^([^：:]{1,32})[：:](.*)$")


def parse_command_body(body_text: str, *, template: InboundTemplateConfig,
                       subject: str = "", body_truncated: bool = False) -> ParsedCommand:
    """§2.3.2 正文解析 + §2.3.3 容错表(逐条)。

    标题行找不到 → ``UNSUPPORTED``(不是本模板,整封只登记不处理);版本不在 ``accepted_versions`` → ``PARSE_FAILED: template_version``。
    """
    out = ParsedCommand()
    if body_truncated:
        out.note("body_truncated")
    title_re = title_line_regex(template.title_line)
    lines = [ln.rstrip("\r") for ln in (body_text or "").splitlines()]

    start = -1
    for i, ln in enumerate(lines):
        m = title_re.match(ln)
        if m:
            start = i
            out.template_version = "v" + m.group(1)
            break
    if start < 0:
        out.status = "UNSUPPORTED"
        out.reason = "title_line_not_found"
        return out
    if out.template_version not in (template.accepted_versions or ["v1"]):
        out.status = "PARSE_FAILED"
        out.reason = "template_version"
        return out

    aliases = _alias_map(template)
    fields: dict[str, str] = {}
    cont_key: Optional[str] = None            # 当前正在续行的键(仅 参数 / 参数.<k> 支持续行)
    warned_unknown: set[str] = set()

    def known_key(name: str) -> Optional[str]:
        if name in aliases:
            return aliases[name]
        if name.startswith(ARGS_PREFIX) and len(name) > len(ARGS_PREFIX):
            return name
        return None

    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if stripped.startswith(">"):                       # 引用回复:整行忽略
            continue
        if stripped in ("--", "-- "):                      # 签名档分隔:停止
            break
        if any(stripped.startswith(mk) for mk in _STOP_MARKERS):   # 转发/引用分隔符:停止
            break
        m = _KV_LINE.match(ln)
        if m:
            name = _normalize_key(m.group(1))
            canonical = known_key(name)
            if canonical is not None:
                if canonical in fields:
                    out.note("duplicate_key")              # 同一键出现两次:取第一次
                    cont_key = None
                    continue
                fields[canonical] = m.group(2).strip()
                cont_key = canonical if (canonical == "参数" or canonical.startswith(ARGS_PREFIX)) else None
                continue
            # 未知键:记 unknown_key,**不**当成正文续行(§2.3.3;同名键只告警一次)
            if name not in warned_unknown:
                warned_unknown.add(name)
                out.note(f"unknown_key:{name}")
            cont_key = None
            continue
        if cont_key is not None and stripped:              # 续行:拼到当前键(到下一个已知键为止)
            fields[cont_key] = (fields[cont_key] + "\n" + ln).strip("\n")
            continue
        # 最后一个已知键之后的自由文本行一律忽略(指令模板没有自由文本字段,可以严格)

    out.req_id = fields.get("指令ID", "").strip()
    out.account_id = fields.get("账号", "").strip()
    out.channel = (fields.get("通道") or "").strip() or None
    out.op = fields.get("操作", "").strip()
    out.session = fields.get("会话", "").strip()
    out.timestamp = fields.get("时间戳", "").strip()
    out.nonce = fields.get("随机数", "").strip()
    out.signature = fields.get("签名", "").strip()

    if not out.req_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", out.req_id):
        out.status = "PARSE_FAILED"
        out.reason = "req_id"
        return out
    if not out.account_id:
        out.status = "PARSE_FAILED"
        out.reason = "account_id_missing"
        return out
    if not out.op:
        out.status = "PARSE_FAILED"
        out.reason = "op_missing"
        return out

    # 参数两种形式(§2.3.2):混用取 JSON 形式并记 args_both_forms
    json_form = "参数" in fields
    expand = {k[len(ARGS_PREFIX):]: v for k, v in fields.items() if k.startswith(ARGS_PREFIX)}
    args: dict[str, Any] = {}
    if json_form:
        if expand:
            out.note("args_both_forms")
        raw = fields["参数"].strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            out.status = "PARSE_FAILED"
            out.reason = "args_json"
            return out
        if not isinstance(parsed, dict):
            out.status = "PARSE_FAILED"
            out.reason = "args_json"
            return out
        args = parsed
    elif expand:
        args = dict(expand)                                # 展开形式值一律字符串,由 op 的 schema 在校验时转型
    if out.session:
        args["session"] = out.session                      # §2.8:`会话` 是糖,两处都给以 `会话` 为准
    out.args = args

    confirm_raw = (fields.get("确认") or "").strip()
    if confirm_raw:
        out.confirm = confirm_raw in ("true", "True", "是", "1")
    timeout_raw = (fields.get("超时") or "").strip()
    if timeout_raw:
        try:
            out.timeout_ms = int(timeout_raw)
        except ValueError:
            out.status = "PARSE_FAILED"
            out.reason = "timeout"
            return out

    # 主题与正文不一致以正文为准,记 subject_mismatch 供人看(§2.3.1)
    if subject:
        s = strip_subject_prefixes(subject)
        if out.req_id and out.req_id not in s:
            out.note("subject_mismatch")

    out.ok = True
    return out


def pick_image_attachment(attachments: list[Attachment], wanted: Optional[str]) -> Optional[Attachment]:
    """§2.3.2「附件」:按 ``相等 / 互为后缀`` 宽松匹配(ibquote ``_pick_primary``);没写文件名取第一个字节头判为图片的附件。"""
    images = [a for a in attachments if a.is_image]
    if wanted:
        low = wanted.lower()
        for a in attachments:
            n = a.name.lower()
            if n == low or n.endswith(low) or low.endswith(n):
                return a
        return None
    return images[0] if images else None
