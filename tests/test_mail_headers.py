"""邮件头注入防护 —— 规格:docs/06 §2.4.1a(基线 §11.17 ⑥ [HDRSAN]);**六条测试用例逐条照 §2.4.1a 的表**。"""
from __future__ import annotations

from dataclasses import dataclass, field

from qtrade_agent.mail.headers import (SUBJECT_MAX_LEN, build_headers, escape_html, hdr_sanitize,
                                       recipients_of, strip_ctrl_for_text)
from qtrade_agent.mail.templates import render


@dataclass
class Route:
    outbound_from: str = "bot@corp.example"
    outbound_to: list = field(default_factory=lambda: ["ops-inbox@corp.example"])
    outbound_cc: list = field(default_factory=list)
    session_overrides: list = field(default_factory=list)


# ---------------------------------------------------------------- §2.4.1a 测试用例表(六行)
def test_group_name_with_crlf_bcc_injection():
    """群名 = ``财报群\\r\\nBcc: attacker@x.com`` ⇒ Subject 里 \\r\\n 变空格、无第二行;收件人仍只是路由 to。"""
    route = Route()
    subject = render("转发：微信消息 [{session_name}]", {"session_name": "财报群\r\nBcc: attacker@x.com"})
    msg = build_headers(route, subject)
    assert "\r" not in msg["Subject"] and "\n" not in str(msg["Subject"])
    assert msg["Bcc"] is None
    assert msg["To"] == "ops-inbox@corp.example"
    assert "attacker@x.com" not in (msg["To"] or "")


def test_nickname_with_newline_header_injection():
    msg = build_headers(Route(), render("[{sender_name}]", {"sender_name": "张三\nX-Priority: 1"}))
    assert msg["X-Priority"] is None
    assert "\n" not in str(msg["Subject"])


def test_chinese_and_emoji_subject_is_rfc2047():
    msg = build_headers(Route(), "转发：微信消息 [固收报价群 👌]")
    raw = msg.as_bytes().decode("ascii", errors="replace")
    assert "=?utf-8?" in raw.lower()


def test_html_part_escapes_table_breaking_text():
    assert escape_html("</td></tr><tr><td>") == "&lt;/td&gt;&lt;/tr&gt;&lt;tr&gt;&lt;td&gt;"


def test_long_group_name_truncated_to_200():
    long_name = "群" * 300
    msg = build_headers(Route(), long_name)
    assert len(str(msg["Subject"])) <= SUBJECT_MAX_LEN and str(msg["Subject"]).endswith("…")


def test_literal_to_in_placeholder_cannot_change_recipients():
    msg = build_headers(Route(), render("{session_name}", {"session_name": "To: x@y"}))
    assert msg["To"] == "ops-inbox@corp.example"


# ---------------------------------------------------------------- 函数体逐字(§2.4.1a 三步)
def test_hdr_sanitize_steps():
    assert hdr_sanitize(None) == ""
    assert hdr_sanitize("a\r\nb\tc") == "a b c"                  # ① 控制字符→空格 ② 折叠空白
    assert hdr_sanitize("a b c\x00d") == "a b c d"     # Unicode 行分隔符与 NUL 也算
    assert hdr_sanitize("  x  ") == "x"
    assert hdr_sanitize("y" * 300, max_len=10) == "y" * 9 + "…"  # ③ 截断


def test_extra_headers_also_sanitized():
    msg = build_headers(Route(), "s", extra={"In-Reply-To": "<a@b>\r\nBcc: x@y"})
    assert "\n" not in str(msg["In-Reply-To"]) and msg["Bcc"] is None


def test_cc_only_from_route():
    route = Route(outbound_cc=["cc@corp.example"])
    msg = build_headers(route, "s")
    assert msg["Cc"] == "cc@corp.example"


def test_session_overrides_change_recipients_only_via_route():
    route = Route(session_overrides=[{"session_id": "qd01:g1", "to": ["group-box@corp.example"]}])
    to, cc = recipients_of(route, session_id="qd01:g1")
    assert to == ["group-box@corp.example"]
    to2, _ = recipients_of(route, session_id="qd01:other")
    assert to2 == ["ops-inbox@corp.example"]


def test_plain_part_strips_ctrl_but_keeps_newlines():
    assert strip_ctrl_for_text("1Y\n1.70\r2Y\x01") == "1Y\n1.70 2Y"
