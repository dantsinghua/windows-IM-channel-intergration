"""解析层测试 —— 规格:docs/06 §2.1(解码三级 + 附件识别)、§2.3.1~§2.3.3(主题/正文/容错表)、§2.14.4(别名)。"""
from __future__ import annotations

from email.message import EmailMessage

import pytest

from qtrade_agent.mail.config import InboundTemplateConfig
from qtrade_agent.mail.parser import (parse_command_body, parse_mime, pick_image_attachment,
                                      scope_prefix_from_pattern, sniff_mime, strip_subject_prefixes,
                                      subject_in_scope)
from test_mail_common import command_body, make_mail

TPL = InboundTemplateConfig()


def parse(body: str, **kw):
    return parse_command_body(body, template=TPL, **kw)


# ---------------------------------------------------------------- §2.1 取信解码三级
def test_plain_body_is_first_choice():
    raw = make_mail(command_body(req_id="r1", account_id="qd01", op="send_text", args={"text": "hi"}))
    pm = parse_mime(raw)
    assert "QTrade 指令 v1" in pm.body_text
    assert pm.from_addr == "ops@corp.example"
    assert pm.rfc_message_id == "<m1@corp.example>"


def test_html_only_mail_is_the_main_path_not_a_fallback():
    """ibquote 2026-08-14 生产实证:对端只发 ``MIMEText(html)``,HTML 剥标签是主路径。"""
    body = command_body(req_id="r1", account_id="qd01", op="send_text", args={"text": "hi"})
    msg = EmailMessage()
    msg["Subject"] = "QTRADE指令 v1 [qd01] send_text r1"
    msg["From"] = "ops@corp.example"
    msg["To"] = "bot@corp.example"
    msg["Message-ID"] = "<h1@corp.example>"
    msg.add_header("Content-Type", "text/html; charset=utf-8")
    msg.set_payload("<html><head><style>p{}</style></head><body>"
                    + "<br>".join(body.splitlines()) + "</body></html>", charset="utf-8")
    pm = parse_mime(msg.as_bytes())
    pc = parse(pm.body_text)
    assert pc.ok and pc.req_id == "r1" and pc.args["text"] == "hi"


def test_txt_attachment_wins_over_plain():
    body = command_body(req_id="rx", account_id="qd01", op="send_text", args={"text": "from-txt"})
    msg = EmailMessage()
    msg["Subject"] = "QTRADE指令 v1"
    msg["From"] = "ops@corp.example"
    msg["Message-ID"] = "<t1@x>"
    msg.set_content("这是 plain 份,应被 .txt 附件顶替")
    msg.add_attachment(body.encode("utf-8"), maintype="text", subtype="plain", filename="cmd.txt")
    pm = parse_mime(msg.as_bytes())
    assert "from-txt" in pm.body_text


def test_body_over_64kb_is_truncated_and_noted():
    body = command_body(req_id="r1", account_id="qd01", op="send_text", args={"text": "x"}) + ("y" * 70000)
    pm = parse_mime(make_mail(body))
    assert pm.body_truncated is True
    pc = parse(pm.body_text, body_truncated=True)
    assert "body_truncated" in pc.notes


# ---------------------------------------------------------------- §2.1 附件识别(字节头 → MIME → 扩展名)
@pytest.mark.parametrize("head,expect", [
    (b"\xff\xd8\xff\xe0abc", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\nabc", "image/png"),
    (b"GIF89a...", "image/gif"),
    (b"%PDF-1.7", "application/pdf"),
    (b"PK\x03\x04zip", "application/zip"),
])
def test_magic_number_beats_declared_mime(head, expect):
    # ibquote 金样本:`.png` 文件名实为 JPEG 字节 ⇒ Content-Type 与扩展名都不可信
    assert sniff_mime(head, "application/octet-stream", "whatever.png") == expect


def test_webp_and_unknown_fallbacks():
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPxx", None, "a.bin") == "image/webp"
    assert sniff_mime(b"\x01\x02\x03", None, "a.jpg") == "image/jpeg"          # 退到扩展名
    assert sniff_mime(b"\x01\x02\x03", None, "a.bin") == "application/octet-stream"


def test_pick_image_attachment_loose_match():
    raw = make_mail(command_body(req_id="r1", account_id="qd01", op="send_image"),
                    attachments=[{"name": "IMG_quote.png", "bytes": b"\xff\xd8\xffJPEGBYTES"}])
    pm = parse_mime(raw)
    assert pm.attachments[0].mime_sniffed == "image/jpeg" and pm.attachments[0].is_image
    assert pick_image_attachment(pm.attachments, "quote.png").name == "IMG_quote.png"   # 互为后缀
    assert pick_image_attachment(pm.attachments, None).name == "IMG_quote.png"          # 不写文件名取第一张图
    assert pick_image_attachment(pm.attachments, "other.png") is None


def test_body_sha256_covers_attachments():
    a = parse_mime(make_mail("x", attachments=[{"name": "a.bin", "bytes": b"1"}]))
    b = parse_mime(make_mail("x", attachments=[{"name": "a.bin", "bytes": b"2"}]))
    assert a.body_sha256 != b.body_sha256          # §2.5 第 3 层:规范化正文 ‖ 各附件 sha256


def test_message_id_missing_falls_back_to_hash():
    msg = EmailMessage()
    msg["From"] = "ops@corp.example"
    msg.set_content("QTrade 指令 v1")
    pm = parse_mime(msg.as_bytes())
    assert pm.rfc_message_id is None and pm.message_id_or_hash().startswith("sha256-")


# ---------------------------------------------------------------- §2.3.1 主题
def test_strip_subject_prefixes_handles_stacking():
    assert strip_subject_prefixes("回复：Re: 转发：QTRADE指令 v1") == "QTRADE指令 v1"


def test_subject_in_scope_accepts_our_forward_prefix():
    prefixes = ["QTRADE指令", "转发：微信消息", "QTRADE回执"]
    assert subject_in_scope("QTRADE指令 v1 [qd01] send_text r1", prefixes)
    assert subject_in_scope("Re: QTRADE指令 v1", prefixes)
    assert subject_in_scope("转发：微信消息 [群] 摘要", prefixes)       # 原串命中(剥前缀后不再以它开头)
    assert not subject_in_scope("周报", prefixes)


def test_scope_prefix_derived_from_pattern():
    assert scope_prefix_from_pattern("QTRADE指令 {template_version} [{account_id}] {op} {req_id}") == "QTRADE指令"
    assert scope_prefix_from_pattern("转发：微信消息 [{session_name}] {summary}") == "转发：微信消息"


# ---------------------------------------------------------------- §2.3.2 正文字段
def test_full_command_body():
    pc = parse(command_body(req_id="20260918-ops-0007", account_id="qd01", op="send_text",
                            session="张三-固收", args={"text": "今日 3M 报价 1.52"}, channel="qidian",
                            confirm="true", timeout=30000))
    assert pc.ok and pc.template_version == "v1"
    assert (pc.req_id, pc.account_id, pc.op, pc.channel) == ("20260918-ops-0007", "qd01", "send_text", "qidian")
    assert pc.args == {"text": "今日 3M 报价 1.52", "session": "张三-固收"}    # `会话` 是糖,合并进 args
    assert pc.confirm is True and pc.timeout_ms == 30000


def test_expand_form_values_are_strings():
    pc = parse(command_body(req_id="r1", account_id="qd01", op="read_messages", session="s1",
                            args={"limit": 20}, expand_form=True))
    assert pc.args["limit"] == "20"                 # 展开形式值一律字符串,转型在 schema 校验时做


def test_both_forms_take_json_and_note():
    body = command_body(req_id="r1", account_id="qd01", op="send_text", args={"text": "json形式"})
    body = body.replace("时间戳：", "参数.text：展开形式\n时间戳：")
    pc = parse(body)
    assert pc.args["text"] == "json形式" and "args_both_forms" in pc.notes


def test_bad_json_args_is_parse_failed():
    body = command_body(req_id="r1", account_id="qd01", op="send_text", args={"text": "x"}).replace(
        "参数：", "参数：{不是 json}\n原参数：")
    pc = parse(body)
    assert pc.status == "PARSE_FAILED" and pc.reason == "args_json"


# ---------------------------------------------------------------- §2.3.3 容错表(逐条)
def test_missing_title_line_is_unsupported():
    pc = parse("你好\n指令ID：r1\n账号：qd01\n操作：send_text\n")
    assert pc.status == "UNSUPPORTED" and pc.reason == "title_line_not_found"


def test_unknown_template_version_is_parse_failed():
    pc = parse("QTrade 指令 v9\n\n指令ID：r1\n账号：qd01\n操作：send_text\n")
    assert pc.status == "PARSE_FAILED" and pc.reason == "template_version"


def test_halfwidth_colon_and_parens_and_crlf():
    pc = parse("QTrade 指令 v1\r\n\r\n指令ID: r1\r\n 账号 ：qd01\r\n操作:read_messages\r\n")
    assert pc.ok and pc.req_id == "r1" and pc.account_id == "qd01" and pc.op == "read_messages"


def test_quoted_lines_and_forward_separator_stop_parsing():
    body = ("QTrade 指令 v1\n\n指令ID：r1\n账号：qd01\n操作：read_messages\n"
            "> 引用的行应整行忽略\n"
            "------------------ 原始邮件 ------------------\n"
            "操作：send_text\n")
    pc = parse(body)
    assert pc.ok and pc.op == "read_messages"       # 分隔符之后不再解析


def test_signature_block_stops_parsing():
    body = "QTrade 指令 v1\n\n指令ID：r1\n账号：qd01\n操作：read_messages\n-- \n操作：send_text\n"
    assert parse(body).op == "read_messages"


def test_unknown_key_noted_once_and_not_swallowed_as_continuation():
    body = ("QTrade 指令 v1\n\n指令ID：r1\n账号：qd01\n操作：read_messages\n"
            "陌生键：x\n陌生键：y\n")
    pc = parse(body)
    assert pc.ok and [n for n in pc.notes if n.startswith("unknown_key:")] == ["unknown_key:陌生键"]


def test_duplicate_key_takes_first():
    body = "QTrade 指令 v1\n\n指令ID：r1\n指令ID：r2\n账号：qd01\n操作：read_messages\n"
    pc = parse(body)
    assert pc.req_id == "r1" and "duplicate_key" in pc.notes


def test_greeting_before_title_is_ignored():
    body = "您好，请执行：\n\nQTrade 指令 v1\n\n指令ID：r1\n账号：qd01\n操作：read_messages\n"
    assert parse(body).ok


def test_args_continuation_until_next_known_key():
    body = ("QTrade 指令 v1\n\n指令ID：r1\n账号：qd01\n操作：send_text\n"
            "参数.text：1Y 1.70\n2Y 1.80\n3Y 1.95\n时间戳：2026-09-18T10:03:00+08:00\n")
    pc = parse(body)
    assert pc.args["text"] == "1Y 1.70\n2Y 1.80\n3Y 1.95"
    assert pc.timestamp == "2026-09-18T10:03:00+08:00"


def test_subject_mismatch_noted():
    body = command_body(req_id="r1", account_id="qd01", op="send_text")
    pc = parse(body, subject="QTRADE指令 v1 [qd01] send_text 别的ID")
    assert "subject_mismatch" in pc.notes


def test_bad_req_id_rejected():
    assert parse("QTrade 指令 v1\n\n指令ID：有冒号:的\n账号：qd01\n操作：x\n").reason == "req_id"
    long_id = "a" * 65
    assert parse(f"QTrade 指令 v1\n\n指令ID：{long_id}\n账号：qd01\n操作：x\n").reason == "req_id"


# ---------------------------------------------------------------- §2.14.4 别名
def test_aliases_are_normalized_to_canonical_keys():
    body = ("QTrade 指令 v1\n\nreq_id：r1\naccount：qd01\nop：read_messages\nsession：s1\n"
            "args：{\"limit\": 5}\ntimestamp：2026-09-18T10:03:00+08:00\nnonce：n1\nsig：hmac-sha256=ab\n")
    pc = parse(body)
    assert pc.ok and pc.req_id == "r1" and pc.op == "read_messages" and pc.args["limit"] == 5
    assert pc.nonce == "n1" and pc.signature == "hmac-sha256=ab"
