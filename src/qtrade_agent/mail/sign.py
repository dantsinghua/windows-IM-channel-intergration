"""HMAC 验签 —— 规格:docs/06 §2.3.4(指令签名规范串)、§2.4.2 末(回执签名规范串)、§6.1(常量时间比较)。

双钥(🔴 R-03,基线 §11.17 [MAILOPS]):
- **指令签名钥** ``vault://mail/hmac/cmd/<短名>``:发起方持有,签指令邮件;回执也用它(让发起方验真是我们回的)。
- **确认钥** ``vault://mail/hmac/confirm``:服务端生成、从不下发任何邮箱。
  🔴 **R6-7:v1 里这把钥只生成、只保管、无消费者**——``console`` 批准不用它签/验任何东西,本模块因此**不提供**任何用它签名的函数。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

SIG_PREFIX = "hmac-sha256="
VAULT_CMD_KEY_FMT = "vault://mail/hmac/cmd/{short_name}"
VAULT_CONFIRM_KEY = "vault://mail/hmac/confirm"          # 🔴 R6-7:v1 只生成保管、无消费者


def canonical_json(args: dict[str, Any]) -> str:
    """§2.3.4:``canonical_json`` = 键排序、无空白、``ensure_ascii=False``、UTF-8。"""
    return json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def args_sha256(args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


def args_digest(args: dict[str, Any]) -> str:
    """🔴 R6-22(02 是 owner):``mail_inbox.args_digest`` = ``hex(sha256(规范化 args JSON))`` 的**前 16 位**(小写 hex)。"""
    return args_sha256(args)[:16]


def attachments_sha256(attachments: list[dict[str, Any]]) -> str:
    """§2.3.4 末行:``sha256_hex(附件1字节 || 附件2字节 … 按文件名字典序)``;**无附件写 ``-``**。"""
    if not attachments:
        return "-"
    h = hashlib.sha256()
    for a in sorted(attachments, key=lambda x: str(x.get("name") or "")):
        h.update(a.get("bytes") or b"")
    return h.hexdigest()


def command_canonical(*, req_id: str, account_id: str, op: str, session: str, args: dict[str, Any],
                      timestamp: str, nonce: str, attachments: Optional[list[dict[str, Any]]] = None) -> str:
    """§2.3.4 规范串(顺序/编码逐字):9 行、``\\n`` 分隔、**无尾随换行**、UTF-8。

    只签「决定做什么」的字段:``确认``/``超时``/``通道`` 不签(改它们改变不了动作对象与内容)。
    ``session`` 用**原文、未归一**;缺省空串。
    """
    return "\n".join([
        "v1",
        req_id,
        account_id,
        op,
        session or "",
        args_sha256(args),
        timestamp,
        nonce,
        attachments_sha256(attachments or []),
    ])


def receipt_canonical(*, req_id: str, account_id: str, op: str, delivery_status: str,
                      trace_id: str, executed_at: str) -> str:
    """§2.4.2 末:回执规范串 = ``"v1" \\n req_id \\n account_id \\n op \\n 送达状态 \\n 追踪ID \\n 执行时间(原文字符串)``。"""
    return "\n".join(["v1", req_id, account_id, op, delivery_status, trace_id, executed_at])


def sign(canonical: str, secret: str) -> str:
    """``signature = hex(HMAC-SHA256(key=secret, msg=canonical(UTF-8)))``(§2.3.4)。"""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_header(canonical: str, secret: str) -> str:
    """带 ``hmac-sha256=`` 前缀的邮件字段值(``签名：hmac-sha256=<hex>``)。"""
    return SIG_PREFIX + sign(canonical, secret)


def verify(canonical: str, secret: str, presented: str) -> bool:
    """§6.1:**常量时间比较**;``presented`` 允许带 ``hmac-sha256=`` 前缀、允许大小写混写。"""
    if not presented:
        return False
    got = presented.strip()
    if got.lower().startswith(SIG_PREFIX):
        got = got[len(SIG_PREFIX):]
    return hmac.compare_digest(sign(canonical, secret).lower(), got.strip().lower())
