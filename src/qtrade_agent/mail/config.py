"""邮件配置 —— 规格唯一出处:docs/06 §7(``[mail]``/``[mail.inbound]``/``[mail.outbound]``/``[mail.cleanup]``/``[mail.template.*]``)。

默认值逐字照 06 §7 的 TOML 块。本文件暂放在 mail 包内(总控接线时按 handoff 并进 ``config.AgentConfig``,
见 ``.omc/handoffs/mail-relay.md``);`` [retention]`` 三键与 ``mail_inbox_rows_days`` 的 owner 是 02 §7.1,
本文件只按 06 §2.6.7/§2.6.10 的引用给出同值副本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 06 §2.14.3 / §2.14.4:三份内置 profile(R-13 删 custom);模板常量本体在 templates.py
PROFILE_IBQUOTE = "ibquote-163-v1"
PROFILE_COLLECTOR = "collector-v1"
PROFILE_QTRADE = "qtrade-v1"


@dataclass
class MailFallbackConfig:
    """06 §7 ``[mail.inbound.fallback]``(E-1:IMAP 连不上/不支持时的 POP3 回落目标,同一邮箱账户)。"""
    enabled: bool = True
    host: str = ""                      # 如 pop.163.com;空 = 不回落只告警 MAIL_INBOUND_STALLED
    port: int = 995
    ssl: bool = True
    after_failures: int = 3             # IMAP 连续几次「连不上/不支持」才回落(认证失败不算)
    recheck_min: int = 30               # 回落期间每隔多久试回 IMAP


@dataclass
class MailHmacKey:
    """06 §7 ``[mail.inbound.hmac]`` 的一项:键名 = 发件人短名(幂等键前缀与 Vault 路径同源)。"""
    short_name: str
    from_addr: str
    secret_ref: str = ""


@dataclass
class MailInboundConfig:
    """06 §7 ``[mail.inbound]``(+ ``.fallback`` / ``.hmac`` 两个子段)。"""
    enabled: bool = True
    protocol: str = "imap"                      # imap | pop3(E-1:IMAP 优先)
    host: str = ""
    port: int = 993
    ssl: bool = True
    user: str = ""
    secret_ref: str = "vault://mail/imap"
    level: str = "admin"                        # read|write|admin(E-2「全部操作」;§2.3.6)
    folders: list[str] = field(default_factory=lambda: ["INBOX", "Junk"])
    processed_folder: str = "QTrade/processed"
    poll_interval_s: int = 180                  # imap 默认 180;pop3 或回落期建议 60
    idle: bool = True
    max_retr_per_round: int = 200
    max_message_bytes: int = 26214400           # 🔴 R4-18:25 MB;超限只登记元数据、status=OVERSIZE
    keep_raw: bool = False
    send_imap_id: bool = True                   # 163/126/yeah 登录后发 ID 命令,否则 Unsafe Login
    allowed_senders: list[str] = field(default_factory=list)      # 空 = 不接受任何指令
    require_signature: bool = True
    sig_time_tolerance_s: int = 600
    nonce_ttl_h: int = 24
    allow_ops: list[str] = field(default_factory=lambda: ["*"])   # R-03:["*"] = 全部 danger=false,不含 danger 项
    danger_confirm_via: str = "console"         # 🔴 R4-12/R6-7:v1 只有 console;mail_out_of_band 配了也视同 console
    danger_confirm_ttl_s: int = 900
    max_timeout_ms: int = 120000
    reply_on_parse_failure: bool = True
    fallback: MailFallbackConfig = field(default_factory=MailFallbackConfig)
    hmac: dict[str, MailHmacKey] = field(default_factory=dict)    # 短名 → MailHmacKey

    def short_name_of(self, from_addr: str) -> Optional[str]:
        """按 ``From``(小写纯地址)反查发件人短名(§2.3.4/§2.5:幂等键前缀 ``mail:{短名}:``)。"""
        low = (from_addr or "").lower()
        for short, k in self.hmac.items():
            if (k.from_addr or "").lower() == low:
                return short
        return None


@dataclass
class MailOutboundConfig:
    """06 §7 ``[mail.outbound]``。"""
    enabled: bool = True
    host: str = ""
    port: int = 465
    ssl: bool = True                            # true=SMTP_SSL;false=STARTTLS(失败即失败,不降级明文)
    user: str = ""
    secret_ref: str = "vault://mail/smtp"
    from_addr: str = ""                         # TOML 键名 `from`(Python 保留字,落到本字段)
    recipients: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    send_rate_per_min: int = 20
    send_burst: int = 5
    timeout_s: int = 30
    max_attempts: int = 8
    backoff_s: list[int] = field(default_factory=lambda: [30, 60, 120, 300, 600, 1200, 1800, 3600])
    max_attachment_mb: int = 20
    max_mail_mb: int = 40
    downscale_oversize_images: bool = True
    include_self: bool = True
    enabled_channels: list[str] = field(default_factory=lambda: ["qidian", "qq", "wechat"])
    only_groups: bool = False                   # §2.4.1 末:`outbound.only_groups = false`
    receipt_to_sender: bool = True
    alert_to: list[str] = field(default_factory=list)     # 空 = 同 recipients
    alert_on_endpoint_change: bool = True
    session_overrides: list[dict[str, Any]] = field(default_factory=list)   # §2.15.2 [{session_id,to,cc}]

    def effective_from(self) -> str:
        return self.from_addr or self.user


@dataclass
class MailCleanupConfig:
    """06 §7 ``[mail.cleanup]``。"""
    enabled: bool = True
    retention_days: int = 7                     # 服务器上的邮件(与本地行 30 天是两把独立尺子)
    archive_before_delete: bool = True
    archive_dir: str = "/var/lib/qtrade/mail/archive"
    archive_retention_days: int = 7
    archive_max_mb: int = 2048
    cleanup_interval_min: int = 60
    cleanup_batch: int = 200
    mailbox_quota_watermark: float = 0.8
    mailbox_quota_mb_assumed: int = 0           # 0=未知 → 只按保留期 + 条数
    max_kept_count: int = 5000
    stall_alert_rounds: int = 3


@dataclass
class MailRetentionConfig:
    """02 §7.1 ``[retention]`` 的镜像副本(owner = 02;06 §2.6.7/§2.6.10 引用)。

    总控接线时应删本类、改引 ``config.AgentConfig``——见 handoff「配置段并入」一条。
    """
    mail_inbox_rows_days: int = 30              # E-18:mail_inbox/mail_outbox/mail_cleanup_log 三表统一 30 天
    disk_warn_mb: int = 5120
    disk_high_mb: int = 2048
    disk_critical_mb: int = 1024


@dataclass
class OutboundTemplateConfig:
    """06 §7 ``[mail.template.outbound]`` + ``.receipt``(全文见 §2.14.3)。"""
    compat_profile: str = PROFILE_IBQUOTE
    subject_pattern: str = "转发：微信消息 [{session_name}] {summary} {ts_cn} - {seq}"
    mime: str = "alternative+mixed"             # collector-v1 固定 html_only
    info_title: str = "微信群聊消息通知"
    body_fields: list[dict[str, Any]] = field(default_factory=list)   # 空 = 用 templates.py 的 profile 默认表
    receipt_subject_pattern: str = "QTRADE回执 {template_version} [{account_id}] {op} {req_id} {result_code}"
    alert_subject_pattern: str = "QTRADE告警 {template_version} {code} {previous_ip}→{public_ip}"   # §2.16.2


@dataclass
class InboundTemplateConfig:
    """06 §7 ``[mail.template.inbound]`` + ``.aliases``(§2.14.4)。"""
    compat_profile: str = PROFILE_QTRADE
    subject_pattern: str = "QTRADE指令 {template_version} [{account_id}] {op} {req_id}"
    title_line: str = "QTrade 指令 {template_version}"
    accepted_versions: list[str] = field(default_factory=lambda: ["v1"])
    aliases: dict[str, list[str]] = field(default_factory=lambda: {
        "指令ID": ["req_id", "指令编号", "RequestId"],
        "账号": ["account", "账号ID"],
        "操作": ["op", "指令"],
        "会话": ["session", "会话名"],
        "参数": ["args", "参数JSON"],
        "确认": ["confirm"],
        "超时": ["timeout", "超时毫秒"],
        "时间戳": ["timestamp", "ts"],
        "随机数": ["nonce"],
        "签名": ["signature", "sig"],
    })


@dataclass
class MailConfig:
    """06 §7 ``[mail]`` 全段。``enabled=false`` 时收/发/清理线程都不起。"""
    enabled: bool = False
    template_version: str = "v1"
    inbound: MailInboundConfig = field(default_factory=MailInboundConfig)
    outbound: MailOutboundConfig = field(default_factory=MailOutboundConfig)
    cleanup: MailCleanupConfig = field(default_factory=MailCleanupConfig)
    retention: MailRetentionConfig = field(default_factory=MailRetentionConfig)
    template_out: OutboundTemplateConfig = field(default_factory=OutboundTemplateConfig)
    template_in: InboundTemplateConfig = field(default_factory=InboundTemplateConfig)

    @classmethod
    def from_toml_dict(cls, d: dict[str, Any]) -> "MailConfig":
        """从 ``tomllib.load()`` 的 ``[mail]`` 子树构造;缺省键取默认值(00 §5:缺省即可跑)。"""
        mail = d or {}

        def pick(section: Optional[dict[str, Any]], klass, rename: Optional[dict[str, str]] = None):
            section = dict(section or {})
            for src, dst in (rename or {}).items():
                if src in section:
                    section[dst] = section.pop(src)
            names = klass.__dataclass_fields__.keys()
            return klass(**{k: section[k] for k in names if k in section})

        inbound_raw = dict(mail.get("inbound") or {})
        hmac_raw = inbound_raw.pop("hmac", {}) or {}
        fallback_raw = inbound_raw.pop("fallback", {}) or {}
        inbound = pick(inbound_raw, MailInboundConfig)
        inbound.fallback = pick(fallback_raw, MailFallbackConfig)
        inbound.hmac = {
            str(short): MailHmacKey(short_name=str(short), from_addr=str((v or {}).get("from", "")),
                                    secret_ref=str((v or {}).get("secret_ref", "")))
            for short, v in hmac_raw.items() if isinstance(v, dict)
        }
        tpl = mail.get("template") or {}
        tpl_out_raw = dict(tpl.get("outbound") or {})
        receipt_raw = tpl_out_raw.pop("receipt", {}) or {}
        tpl_out = pick(tpl_out_raw, OutboundTemplateConfig)
        if "subject_pattern" in receipt_raw:
            tpl_out.receipt_subject_pattern = str(receipt_raw["subject_pattern"])
        tpl_in_raw = dict(tpl.get("inbound") or {})
        aliases_raw = tpl_in_raw.pop("aliases", None)
        tpl_in = pick(tpl_in_raw, InboundTemplateConfig)
        if isinstance(aliases_raw, dict):
            tpl_in.aliases = {str(k): [str(x) for x in (v or [])] for k, v in aliases_raw.items()}
        return cls(
            enabled=bool(mail.get("enabled", False)),
            template_version=str(mail.get("template_version", "v1")),
            inbound=inbound,
            outbound=pick(mail.get("outbound"), MailOutboundConfig, rename={"from": "from_addr"}),
            cleanup=pick(mail.get("cleanup"), MailCleanupConfig),
            retention=pick((d or {}).get("retention"), MailRetentionConfig),
            template_out=tpl_out,
            template_in=tpl_in,
        )
