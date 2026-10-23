"""邮件侧的码与集合 —— 规格:docs/06 §2.3.5(``mail_inbox.status``/``reason`` 唯一出处)、§2.7(``MAIL_*`` 告警码)。

本文件不复写任何 danger 清单(06 §2.2 第 3 闸:一律引用 02 §3.10 能力目录的 ``danger`` 属性)。
"""
from __future__ import annotations

# ---------------------------------------------------------------- mail_inbox.status(06 §2.3.5;02 §3.1 CHECK 是本集合的镜像)
RECEIVED = "RECEIVED"
ACCEPTED = "ACCEPTED"
DONE = "DONE"
RECEIPT_SENT = "RECEIPT_SENT"
RECEIPT_SKIPPED = "RECEIPT_SKIPPED"
UNSUPPORTED = "UNSUPPORTED"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
OVERSIZE = "OVERSIZE"
PARSE_FAILED = "PARSE_FAILED"
SENDER_DENIED = "SENDER_DENIED"
SIG_INVALID = "SIG_INVALID"
EXPIRED = "EXPIRED"
DUPLICATE = "DUPLICATE"
DUPLICATE_NONCE = "DUPLICATE_NONCE"
OP_DENIED = "OP_DENIED"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
INGEST_ERROR = "INGEST_ERROR"
CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
CONFIRM_EXPIRED = "CONFIRM_EXPIRED"
ROUTE_MISMATCH = "ROUTE_MISMATCH"

#: 06 §2.3.5:结果态(处理已收口的);``RECEIVED``/``ACCEPTED`` 不在内(§2.6.1 eligible 明确排除)。
#: ``DONE`` 在内,但 §2.6.1 另有「回执还在队列里的不删」一条,由 ``cleanup.eligible()`` 承担。
TERMINAL = frozenset({
    DONE, RECEIPT_SENT, RECEIPT_SKIPPED,
    UNSUPPORTED, OUT_OF_SCOPE, OVERSIZE, PARSE_FAILED, SENDER_DENIED, SIG_INVALID, EXPIRED,
    DUPLICATE, DUPLICATE_NONCE, OP_DENIED, TARGET_NOT_FOUND, INGEST_ERROR,
    CONFIRM_EXPIRED, ROUTE_MISMATCH,
})

#: 🔴 R6-1(06 §2.3.5 是唯一出处):这两个终态的邮件**在服务器上一律不碰**——
#: 不 UID MOVE/COPY、不 +FLAGS (\\Deleted)、不 EXPUNGE、不 DELE,留原夹原位。
#: 凡「会动远端邮件」的五处路径都以 ``row.status not in NEVER_DELETE`` 为前置(R6-26 由四处补为五处)。
NEVER_DELETE = frozenset({OUT_OF_SCOPE, OVERSIZE})

# ---------------------------------------------------------------- reason 前缀三值(🔴 R6-29,06 §2.3.5 是唯一出处)
REASON_NOT_ALLOWED = "NOT_ALLOWED:"       # op 不在 allow_ops(§2.2 第 3 闸)
REASON_REJECTED = "REJECTED:"             # 控制台人工驳回,后接 :actor(R6-34)
REASON_ARGS_TAMPERED = "ARGS_TAMPERED:"   # approve 执行前复算 args_digest 不等(库被改)

# ---------------------------------------------------------------- MAIL_* 告警码(06 §2.7;severity 同表)
MAIL_INBOUND_STALLED = "MAIL_INBOUND_STALLED"
MAIL_AUTH_FAILED = "MAIL_AUTH_FAILED"
MAIL_QUOTA_HIGH = "MAIL_QUOTA_HIGH"
MAIL_PAUSED_DISK_FULL = "MAIL_PAUSED_DISK_FULL"
MAIL_SMTP_FAILING = "MAIL_SMTP_FAILING"
MAIL_OUTBOX_DEAD = "MAIL_OUTBOX_DEAD"
MAIL_CLEANUP_FAILED = "MAIL_CLEANUP_FAILED"
MAIL_PARSE_FAILED = "MAIL_PARSE_FAILED"
MAIL_SENDER_DENIED = "MAIL_SENDER_DENIED"
MAIL_MSG_OVERSIZE = "MAIL_MSG_OVERSIZE"
MAIL_WATERMARK_STALLED = "MAIL_WATERMARK_STALLED"
MAIL_PROTOCOL_FALLBACK = "MAIL_PROTOCOL_FALLBACK"
MAIL_ROUTE_UNRESOLVED = "MAIL_ROUTE_UNRESOLVED"
MAIL_ENDPOINT_CHANGED = "MAIL_ENDPOINT_CHANGED"

#: 06 §2.7 表的 severity 列(``MAIL_INBOUND_STALLED``/``MAIL_QUOTA_HIGH`` 另有升 crit 的条件,由调用方传 severity 覆盖)
MAIL_ALERT_CODES: dict[str, str] = {
    MAIL_INBOUND_STALLED: "warn",
    MAIL_AUTH_FAILED: "crit",
    MAIL_QUOTA_HIGH: "warn",
    MAIL_PAUSED_DISK_FULL: "crit",
    MAIL_SMTP_FAILING: "warn",
    MAIL_OUTBOX_DEAD: "warn",
    MAIL_CLEANUP_FAILED: "warn",
    MAIL_PARSE_FAILED: "info",
    MAIL_SENDER_DENIED: "warn",
    MAIL_MSG_OVERSIZE: "warn",
    MAIL_WATERMARK_STALLED: "crit",
    MAIL_PROTOCOL_FALLBACK: "warn",
    MAIL_ROUTE_UNRESOLVED: "warn",
    MAIL_ENDPOINT_CHANGED: "info",
}

# ---------------------------------------------------------------- 审计动作名(02 §3.1 audit_log 是 owner;06 §2.3.6 引用)
AUDIT_CONFIRM_APPROVED = "mail.pending_confirm.approved"
AUDIT_CONFIRM_REJECTED = "mail.pending_confirm.rejected"
AUDIT_CONFIRM_EXPIRED = "mail.pending_confirm.expired"
AUDIT_CONFIRM_ARGS_MISMATCH = "mail.pending_confirm.args_mismatch"
