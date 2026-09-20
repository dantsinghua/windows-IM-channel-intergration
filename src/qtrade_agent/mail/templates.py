"""邮件模板渲染 —— 规格:docs/06 §2.4.1(信息邮件)、§2.4.2(回执)、§2.14(模板配置 E-4)。

两类模板别混(🔴 R-13 / 基线 N-10,§2.14.1):
- **自定义模板**(``compat_profile='qtrade-v1'``):由占位符拼装,**只能用 §2.14.2 的 12 个核心占位符**;
- **兼容模板**(``ibquote-163-v1`` / ``collector-v1``):**整体固定、逐字锁定、不可配**——信息段键名/顺序/取值格式一个字都不能动。
  理由(务必保留):把兼容模板做成可配 = 给用户一个改坏生产链路的开关;ibquote 对「图片类型：缩略图」整封弃用是安琳 2026-08-26 的业务判据。

进头的占位符值一律先过 ``headers.hdr_sanitize``(基线 §11.17 ⑥ [HDRSAN]);正文 ``text/html`` 份转义、``text/plain`` 份剥控制字符。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .config import PROFILE_COLLECTOR, PROFILE_IBQUOTE, PROFILE_QTRADE, OutboundTemplateConfig
from .headers import escape_html, hdr_sanitize, strip_ctrl_for_text

# ---------------------------------------------------------------- §2.14.2:12 个核心可配占位符(只约束自定义模板)
CORE_PLACEHOLDERS = frozenset({
    "channel", "channel_cn",                    # 1 渠道
    "account_id", "account_label",              # 2 账号
    "session_name", "session_id",               # 3 会话名
    "sender_kind",                              # 4 会话类型(可自定义标签 {sender_kind:群|个人})
    "sender_name", "sender_id",                 # 5 发送方
    "ts", "ts_cn",                              # 6 时间
    "msg_type", "msg_type_cn",                  # 7 类型
    "summary",                                  # 8 正文摘要
    "media_count",                              # 9 媒体数
    "message_id", "ext_msg_id",                 # 10 消息ID
    "fingerprint",                              # 11 指纹
    "seq", "seq_total", "day_seq",              # 12 序号
})

#: §2.14.2「回执/入站专用」(信息邮件里渲染为空并配置校验告警,不计入 12)
RECEIPT_PLACEHOLDERS = frozenset({"op", "req_id", "result_code", "template_version"})

#: §2.14.2「兼容模板内部字段」——属于整体固定的兼容模板本身,不计入 12、用户不可配
COMPAT_PLACEHOLDERS = frozenset({"text", "attachments", "image_kind", "revoked", "dir", "media_ref",
                                 "oversize", "capture_text"})

#: §2.16.2 告警邮件主题用(端点变更)
ALERT_PLACEHOLDERS = frozenset({"code", "previous_ip", "public_ip"})

ALL_PLACEHOLDERS = CORE_PLACEHOLDERS | RECEIPT_PLACEHOLDERS | COMPAT_PLACEHOLDERS | ALERT_PLACEHOLDERS

#: §2.14.2:collector 兼容别名(``compat_profile='collector-v1'`` 时自动启用,别处不认)
COLLECTOR_ALIASES = {"target": "session_name", "sender": "sender_name", "message_id": "ext_msg_id", "seq_no": "seq"}
#: collector 的批次占位符在「一消息一邮件」下无对应值,渲染为空并在保存时提示
COLLECTOR_BATCH_PLACEHOLDERS = frozenset({"minutes", "start_time", "end_time", "message_count"})

_PLACEHOLDER = re.compile(r"\{([a-z_]+)(?::([^}]*))?\}")


class TemplateLocked(ValueError):
    """``PUT /settings/mail`` 校验失败:兼容 profile 的锁定项被改(``400 TEMPLATE_LOCKED``,§2.14.3)。"""

    def __init__(self, item: str):
        super().__init__(f"TEMPLATE_LOCKED:{item}")
        self.item = item


class TemplateInvalid(ValueError):
    """未知占位符 / 缺 ``{template_version}``(§2.14.1:配置校验拒绝,不是渲染时留空)。"""


# ---------------------------------------------------------------- §2.14.3:三份内置默认模板(代码常量,不入 mail_templates 表)
def _f(key: str, value: str, segment: str, required: bool, empty: str) -> dict[str, Any]:
    return {"key": key, "value": value, "segment": segment, "required": required, "empty": empty}


#: ``ibquote-163-v1`` 的 ``body_fields``(§2.14.3 那张表逐字);``segment='info'`` 全部项锁定
IBQUOTE_BODY_FIELDS: list[dict[str, Any]] = [
    _f("来源账号", "{account_id}", "ext", True, "dash"),
    _f("通道", "{channel}", "ext", True, "dash"),
    _f("会话ID", "{session_id}", "ext", True, "dash"),
    _f("会话类型", "{sender_kind:group|private}", "ext", False, "dash"),
    _f("方向", "{dir}", "ext", True, "dash"),
    _f("消息内部ID", "{message_id}", "ext", True, "dash"),
    _f("发送方ID", "{sender_id}", "ext", False, "dash"),
    _f("媒体引用", "{media_ref}", "ext", False, "omit"),
    _f("附件超限", "{oversize}", "ext", False, "omit"),
    _f("正文留痕", "{capture_text}", "ext", False, "omit"),
    # ── 以下信息段:ibquote-163-v1 锁定(键名/顺序/取值格式一个字不能动)──
    _f("群聊", "{session_name}", "info", True, "dash"),
    _f("消息序号", "{seq}/{seq_total}", "info", True, "dash"),
    _f("昵称", "{sender_name}", "info", True, "dash"),
    _f("消息时间", "{ts}", "info", True, "dash"),
    _f("每日序号", "#{day_seq}", "info", True, "dash"),
    _f("消息ID", "{ext_msg_id}", "info", True, "dash"),
    _f("消息类型", "{msg_type}", "info", True, "dash"),
    _f("文本消息内容", "{text}", "info", True, "dash"),
    _f("图片/文件附件", "{attachments}", "info", True, "dash"),
    _f("图片类型", "{image_kind}", "info", False, "omit"),
    _f("是否撤回", "{revoked}", "info", True, "dash"),
]

#: ``collector-v1``:信息段同上但 ``图片/文件附件`` 写 ``见附件``、``消息序号`` 值带「(本轮)」;**无扩展块**、仅 ``text/html``
COLLECTOR_BODY_FIELDS: list[dict[str, Any]] = [
    _f("群聊", "{session_name}", "info", True, "dash"),
    _f("消息序号", "{seq}/{seq_total}（本轮）", "info", True, "dash"),
    _f("昵称", "{sender_name}", "info", True, "dash"),
    _f("消息时间", "{ts}", "info", True, "dash"),
    _f("每日序号", "#{day_seq}", "info", True, "dash"),
    _f("消息ID", "{ext_msg_id}", "info", True, "dash"),
    _f("消息类型", "{msg_type}", "info", True, "dash"),
    _f("文本消息内容", "{text}", "info", True, "dash"),
    _f("图片/文件附件", "见附件", "info", True, "dash"),
    _f("图片类型", "{image_kind}", "info", False, "omit"),
    _f("是否撤回", "{revoked}", "info", True, "dash"),
]

#: ``qtrade-v1``:无扩展/信息之分,字段顺序照 §2.14.3 表的第三行
QTRADE_BODY_FIELDS: list[dict[str, Any]] = [
    _f("来源账号", "{account_id}", "info", True, "dash"),
    _f("通道", "{channel}", "info", True, "dash"),
    _f("会话ID", "{session_id}", "info", True, "dash"),
    _f("会话名", "{session_name}", "info", True, "dash"),
    _f("会话类型", "{sender_kind:群|个人}", "info", True, "dash"),
    _f("方向", "{dir}", "info", True, "dash"),
    _f("发送方", "{sender_name}", "info", True, "dash"),
    _f("发送方ID", "{sender_id}", "info", True, "dash"),
    _f("消息ID", "{ext_msg_id}", "info", True, "dash"),
    _f("消息内部ID", "{message_id}", "info", True, "dash"),
    _f("消息类型", "{msg_type}", "info", True, "dash"),
    _f("消息时间", "{ts}", "info", True, "dash"),
    _f("每日序号", "#{day_seq}", "info", True, "dash"),
    _f("正文", "{text}", "info", True, "dash"),
    _f("附件", "{attachments}", "info", True, "dash"),
    _f("媒体引用", "{media_ref}", "info", False, "omit"),
    _f("是否撤回", "{revoked}", "info", True, "dash"),
    _f("正文留痕", "{capture_text}", "info", False, "omit"),
]

DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    PROFILE_IBQUOTE: {
        "subject_pattern": "转发：微信消息 [{session_name}] {summary} {ts_cn} - {seq}",
        "info_title": "微信群聊消息通知",
        "mime": "alternative+mixed",
        "body_fields": IBQUOTE_BODY_FIELDS,
    },
    PROFILE_COLLECTOR: {
        "subject_pattern": "微信群聊消息通知 - {session_name} - {sender_name} - {ext_msg_id}",
        "info_title": "微信群聊消息通知",
        "mime": "html_only",
        "body_fields": COLLECTOR_BODY_FIELDS,
    },
    PROFILE_QTRADE: {
        "subject_pattern": ("QTrade消息 {template_version} [{channel_cn}·{sender_kind:群|个人}] [{account_id}] "
                            "{session_name} {msg_type_cn} {ts_cn} #{seq}"),
        "info_title": "QTrade 消息通知 v1",
        "mime": "alternative+mixed",
        "body_fields": QTRADE_BODY_FIELDS,
    },
}

#: §2.14.3「兼容 profile 的锁定范围」
LOCKED_INFO_PROFILES = frozenset({PROFILE_IBQUOTE, PROFILE_COLLECTOR})


def defaults_for(profile: str) -> dict[str, Any]:
    """``GET /settings/mail/templates/defaults`` 原样下发的那三份(§2.14.3)。"""
    return DEFAULT_TEMPLATES.get(profile, DEFAULT_TEMPLATES[PROFILE_QTRADE])


def body_fields_of(tpl: OutboundTemplateConfig) -> list[dict[str, Any]]:
    return list(tpl.body_fields or defaults_for(tpl.compat_profile)["body_fields"])


# ---------------------------------------------------------------- 渲染
def render(pattern: str, ctx: dict[str, Any], *, profile: str = PROFILE_QTRADE) -> str:
    """占位符渲染。``{sender_kind:群聊|私聊}`` 形态:冒号后 ``真|假`` 两标签(§2.14.2 第 4 行)。

    未知占位符在**配置校验**时就被拒(``validate_template``),渲染阶段留空不炸。
    """
    aliases = COLLECTOR_ALIASES if profile == PROFILE_COLLECTOR else {}

    def sub(m: re.Match) -> str:
        name = aliases.get(m.group(1), m.group(1))
        labels = m.group(2)
        val = ctx.get(name)
        if labels is not None and "|" in labels:
            truthy, falsy = labels.split("|", 1)
            return truthy if val in (True, "group", "群", "群聊") else falsy
        if val is None:
            return ""
        if isinstance(val, bool):
            return "是" if val else "否"
        return str(val)

    return _PLACEHOLDER.sub(sub, pattern or "")


def _field_value(field: dict[str, Any], ctx: dict[str, Any], profile: str) -> Optional[str]:
    """§2.14.3 ``empty`` 两种语义:``dash`` = 键在、值 ``-``;``omit`` = 整行不出现。返回 None 表示整行省略。"""
    raw = render(str(field.get("value", "")), ctx, profile=profile).strip()
    if raw in ("", "/", "#", "-"):
        cleaned = raw.replace("/", "").replace("#", "").strip()
        if cleaned in ("", "-"):
            if str(field.get("empty", "dash")) == "omit":
                return None
            return "-"
    return raw


@dataclass
class RenderedMail:
    subject: str
    body_text: str
    body_html: Optional[str]
    render_notes: str = ""          # §3.1 ``mail_outbox.render_notes``:必填字段为空按 empty 处理的记录


def render_message_mail(tpl: OutboundTemplateConfig, ctx: dict[str, Any]) -> RenderedMail:
    """信息邮件(§2.4.1):扩展块在**标题行之前**,信息段在标题行之后(ibquote 只认标题行起的内容)。"""
    profile = tpl.compat_profile
    fields = body_fields_of(tpl)
    ext_lines: list[tuple[str, str]] = []
    info_lines: list[tuple[str, str]] = []
    notes: list[str] = []
    for f in fields:
        val = _field_value(f, ctx, profile)
        if val is None:
            continue
        if val == "-" and f.get("required"):
            notes.append(f"empty:{f.get('key')}")       # required=true 且值为空:按 empty 处理并记 render_notes,不阻塞发送
        (ext_lines if f.get("segment") == "ext" else info_lines).append((str(f["key"]), val))

    title = render(tpl.info_title or defaults_for(profile)["info_title"], ctx, profile=profile)
    text_parts: list[str] = []
    if ext_lines:
        text_parts.append("\n".join(f"{k}：{strip_ctrl_for_text(v)}" for k, v in ext_lines))
        text_parts.append("")
    text_parts.append(title)
    text_parts.append("")
    text_parts.append("\n".join(f"{k}：{strip_ctrl_for_text(v)}" for k, v in info_lines))
    body_text = "\n".join(text_parts).strip() + "\n"

    html_parts = [f"<p><b>{escape_html(k)}：</b>{escape_html(v)}</p>" for k, v in ext_lines]
    html_parts.append(f"<h2>{escape_html(title)}</h2>")
    html_parts += [f"<p><b>{escape_html(k)}：</b>{escape_html(v)}</p>" for k, v in info_lines]
    body_html = "<html><body>" + "".join(html_parts) + "</body></html>"

    subject = render(tpl.subject_pattern or defaults_for(profile)["subject_pattern"], ctx, profile=profile)
    mime = tpl.mime or defaults_for(profile)["mime"]
    if mime == "html_only":                                   # collector-v1 固定 html_only(§2.14.3)
        return RenderedMail(subject=subject, body_text=body_text, body_html=body_html, render_notes=";".join(notes))
    return RenderedMail(subject=subject, body_text=body_text, body_html=body_html, render_notes=";".join(notes))


#: §2.4.2 回执正文字段(固定,发起方按码机读,不开放改键名)
RECEIPT_FIELDS = ("指令ID", "账号", "操作", "会话", "送达状态", "结果", "确认方式", "消息ID",
                  "耗时毫秒", "追踪ID", "错误", "可重试", "需人工", "执行时间", "签名")


def render_receipt(tpl: OutboundTemplateConfig, ctx: dict[str, Any], values: dict[str, str]) -> RenderedMail:
    """回执邮件(§2.4.2):主题 ``QTRADE回执 v1 [{account_id}] {op} {req_id} {送达状态}``,正文字段固定。

    ``结果`` 一栏给人看,``送达状态`` 是基线 §8.3 结果码原文(不翻译、不合并);**回执不含消息正文**。
    """
    subject = render(tpl.receipt_subject_pattern, ctx, profile=PROFILE_QTRADE)
    lines = [f"QTrade 回执 {ctx.get('template_version', 'v1')}", ""]
    for key in RECEIPT_FIELDS:
        lines.append(f"{key}：{strip_ctrl_for_text(values.get(key, '-')) or '-'}")
    body_text = "\n".join(lines) + "\n"
    html = "<html><body>" + "".join(
        f"<p><b>{escape_html(k)}：</b>{escape_html(values.get(k, '-'))}</p>" for k in RECEIPT_FIELDS) + "</body></html>"
    return RenderedMail(subject=subject, body_text=body_text, body_html=html)


def render_alert(tpl: OutboundTemplateConfig, ctx: dict[str, Any], lines: list[tuple[str, str]]) -> RenderedMail:
    """告警邮件(§2.16.2 ``kind='alert'``):主题 pattern 里的 ``previous_ip``/``public_ip`` 是**外部可控值**,

    渲染后整条主题必过 ``hdr_sanitize()``(由 ``headers.build_headers`` 承担,勿手拼 ``Subject:``)。
    """
    subject = render(tpl.alert_subject_pattern, ctx, profile=PROFILE_QTRADE)
    body_text = "\n".join(f"{k}：{strip_ctrl_for_text(v)}" for k, v in lines) + "\n"
    html = "<html><body>" + "".join(f"<p><b>{escape_html(k)}：</b>{escape_html(v)}</p>" for k, v in lines) + "</body></html>"
    return RenderedMail(subject=subject, body_text=body_text, body_html=html)


# ---------------------------------------------------------------- 配置校验(§2.14.1/§2.14.3)
def placeholders_in(pattern: str) -> set[str]:
    return {m.group(1) for m in _PLACEHOLDER.finditer(pattern or "")}


def validate_template(tpl: OutboundTemplateConfig) -> None:
    """``PUT /settings/mail`` 的校验:未知占位符 / 缺 ``{template_version}`` / 兼容 profile 锁定项被改。

    - 主题 pattern **必须含 ``{template_version}``**——仅对自定义 profile(``qtrade-v1``)强制:
      两份兼容模板的主题是 ibquote/collector 现网形态,本就不带版本号(§2.14.3 表)。见 handoff「建议裁决 ③」。
    - 兼容 profile 锁 ``info_title`` + ``segment='info'`` 全部项(键名、顺序、``value``、``empty``);``collector-v1`` 另锁 ``mime=html_only``。
    """
    profile = tpl.compat_profile
    if profile not in DEFAULT_TEMPLATES:
        raise TemplateInvalid(f"未知 compat_profile:{profile}")
    used = placeholders_in(tpl.subject_pattern)
    for f in body_fields_of(tpl):
        used |= placeholders_in(str(f.get("value", "")))
    # ⚠️ 规格张力(handoff「建议裁决 ⑥」):§2.14.2 写「兼容模板内部字段……也不能在自定义模板(qtrade-v1)里拼」,
    #    而 §2.14.3 给出的 qtrade-v1 默认模板恰恰含「正文/附件/媒体引用/是否撤回/正文留痕」五个字段
    #    ——它们只能由 {text}/{attachments}/{media_ref}/{revoked}/{capture_text} 提供。两句直接打架。
    #    本实现取「§2.14.3 的默认模板必须能过校验」一侧:qtrade-v1 放行内部字段,12 核心仍是**用户新写**占位符的推荐面。
    allowed = ALL_PLACEHOLDERS
    if profile == PROFILE_COLLECTOR:
        allowed = allowed | set(COLLECTOR_ALIASES) | COLLECTOR_BATCH_PLACEHOLDERS
    unknown = used - allowed
    if unknown:
        raise TemplateInvalid("未知占位符:" + ",".join(sorted(unknown)))
    if profile == PROFILE_QTRADE and "template_version" not in placeholders_in(tpl.subject_pattern):
        raise TemplateInvalid("主题 pattern 必须含 {template_version}")
    if profile in LOCKED_INFO_PROFILES:
        locked = [f for f in defaults_for(profile)["body_fields"] if f["segment"] == "info"]
        got = [f for f in body_fields_of(tpl) if f.get("segment") == "info"]
        if len(got) != len(locked):
            raise TemplateLocked("info_fields")
        for want, have in zip(locked, got):
            for attr in ("key", "value", "empty"):
                if str(have.get(attr)) != str(want[attr]):
                    raise TemplateLocked(f"{want['key']}.{attr}")
        if tpl.info_title != defaults_for(profile)["info_title"]:
            raise TemplateLocked("info_title")
        if profile == PROFILE_COLLECTOR and (tpl.mime or "html_only") != "html_only":
            raise TemplateLocked("mime")


def scope_subject_prefixes(tpls: list[OutboundTemplateConfig], inbound_subject_patterns: list[str]) -> list[str]:
    """§2.14.3 末 / §2.6.4:``scope_subject_prefix`` **从生效模板的 ``subject_pattern`` 推导**,不再手填。

    默认 profile 下推导结果 = ``["QTRADE指令", "转发：微信消息", "QTRADE回执"]``。
    """
    from .parser import scope_prefix_from_pattern
    out: list[str] = []
    for p in inbound_subject_patterns:
        v = scope_prefix_from_pattern(p)
        if v and v not in out:
            out.append(v)
    for t in tpls:
        for pattern in (t.subject_pattern or defaults_for(t.compat_profile)["subject_pattern"],
                        t.receipt_subject_pattern):
            v = scope_prefix_from_pattern(pattern)
            if v and v not in out:
                out.append(v)
    return out


def subject_for_header(subject: str) -> str:
    """任何主题进头前的最后一道(§2.4.1a 规则 1);``build_headers`` 内部也会再调一次,幂等。"""
    return hdr_sanitize(subject, max_len=200)
