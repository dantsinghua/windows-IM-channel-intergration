"""HMAC 验签测试 —— 规格:docs/06 §2.3.4(规范串/顺序/编码/容差/防重放/密钥分发)、§2.4.2 末(回执签名)、§3.1(``args_digest``)。"""
from __future__ import annotations

import hashlib
import json

from qtrade_agent.mail.sign import (VAULT_CMD_KEY_FMT, VAULT_CONFIRM_KEY, args_digest, args_sha256,
                                    attachments_sha256, canonical_json, command_canonical, receipt_canonical,
                                    sign, sign_header, verify)


def test_canonical_string_is_nine_lines_no_trailing_newline():
    c = command_canonical(req_id="r1", account_id="qd01", op="send_text", session="张三-固收",
                          args={"text": "hi"}, timestamp="2026-09-18T10:03:00+08:00", nonce="n1")
    lines = c.split("\n")
    assert len(lines) == 9 and not c.endswith("\n")
    assert lines[0] == "v1" and lines[1] == "r1" and lines[2] == "qd01" and lines[3] == "send_text"
    assert lines[4] == "张三-固收"                       # session 原文、未归一
    assert lines[5] == args_sha256({"text": "hi"})
    assert lines[6] == "2026-09-18T10:03:00+08:00" and lines[7] == "n1"
    assert lines[8] == "-"                                # 无附件写 "-"


def test_canonical_json_is_sorted_compact_utf8():
    s = canonical_json({"b": 1, "a": "中文"})
    assert s == '{"a":"中文","b":1}'                      # 键排序、无空白、ensure_ascii=False


def test_expand_and_json_forms_hash_identically_after_merge():
    """§2.3.4:两种写法合并成同一对象后规范化结果相同。"""
    assert args_sha256({"text": "1.52", "session": "s"}) == args_sha256({"session": "s", "text": "1.52"})


def test_attachments_hash_is_by_filename_order():
    a = [{"name": "b.png", "bytes": b"B"}, {"name": "a.png", "bytes": b"A"}]
    h = hashlib.sha256(b"A" + b"B").hexdigest()
    assert attachments_sha256(a) == h                     # 按文件名字典序拼接
    assert attachments_sha256([]) == "-"


def test_changing_attachment_breaks_signature():
    base = dict(req_id="r1", account_id="qd01", op="send_image", session="s",
                args={"image": "quote.png"}, timestamp="t", nonce="n")
    c1 = command_canonical(**base, attachments=[{"name": "quote.png", "bytes": b"IMG1"}])
    c2 = command_canonical(**base, attachments=[{"name": "quote.png", "bytes": b"IMG2"}])
    assert sign(c1, "k") != sign(c2, "k")                 # 换图即验签失败


def test_verify_accepts_prefix_and_case_and_rejects_tamper():
    c = command_canonical(req_id="r1", account_id="qd01", op="send_text", session="", args={},
                          timestamp="t", nonce="n")
    good = sign(c, "secret")
    assert verify(c, "secret", good)
    assert verify(c, "secret", "hmac-sha256=" + good.upper())
    assert not verify(c, "secret", good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert not verify(c, "other", good)
    assert not verify(c, "secret", "")


def test_sign_header_prefix():
    assert sign_header("x", "k").startswith("hmac-sha256=")


def test_receipt_canonical_is_seven_lines():
    c = receipt_canonical(req_id="r1", account_id="qd01", op="send_text", delivery_status="DELIVERED",
                          trace_id="01J8", executed_at="2026-09-18T10:03:02+08:00")
    assert c.split("\n") == ["v1", "r1", "qd01", "send_text", "DELIVERED", "01J8", "2026-09-18T10:03:02+08:00"]


def test_args_digest_is_first_16_hex_of_sha256():
    args = {"before": "2026-01-01", "mode": "all"}
    full = hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    assert args_digest(args) == full[:16] and len(args_digest(args)) == 16


def test_two_keys_are_physically_separate():
    """🔴 R-03 双钥:指令钥每发件人一把,确认钥服务端单钥;v1 里确认钥无消费者(R6-7)。"""
    assert VAULT_CMD_KEY_FMT.format(short_name="ops") == "vault://mail/hmac/cmd/ops"
    assert VAULT_CONFIRM_KEY == "vault://mail/hmac/confirm"
    assert "cmd" not in VAULT_CONFIRM_KEY
