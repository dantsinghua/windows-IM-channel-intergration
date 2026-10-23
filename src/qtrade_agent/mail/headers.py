"""邮件头消毒与唯一设头入口 —— 规格:基线 §11.17 ⑥ [HDRSAN],**函数体唯一出处 = docs/06 §2.4.1a**(逐字照抄)。

🔴 模块级强制(R5-7):任何进邮件头(Subject/From/To/Cc/Reply-To/In-Reply-To/References)的值,无论来源——
占位符、探测结果、配置、拼接串——一律先过 ``hdr_sanitize()``;出站邮件构造**只此一个设头入口 ``build_headers()``**,
禁止任何路径手拼 ``msg['Subject']=``。新增任何一封出站邮件(信息/回执/告警)都必须走它。

三条硬规则(§2.4.1a):
1. 进头的占位符值:剥 CR/LF/TAB(及 ``\\v\\f`` 与 Unicode 行分隔符)→ 折叠空白 → 截断(Subject ≤ 200,单头值 ≤ 256)→ RFC 2047 编码(交给 ``email.headerregistry``,不手拼)。
2. **收件人只来自配置**:``To/Cc/Bcc/Reply-To`` 一律取自命中的 ``mail_routes`` 的 ``outbound_json.to/cc``;主题/正文占位符再怎么写都不改变收件人集合。
3. 正文占位符转义:``text/html`` 份做 HTML 转义,``text/plain`` 份剥控制字符。
"""
from __future__ import annotations

import html as _html
import re
from email.message import EmailMessage
from typing import Any, Iterable, Optional

#: §2.4.1a 伪代码里的 ``_CTRL`` 集合(逐字):CR/LF/TAB/VT/FF + Unicode 行分隔符 U+2028/U+2029 + NUL
_CTRL = {"\r", "\n", "\t", "\v", "\f", " ", " ", "\x00"}

SUBJECT_MAX_LEN = 200
HEADER_MAX_LEN = 256


def hdr_sanitize(value: Optional[str], *, max_len: int = HEADER_MAX_LEN) -> str:
    """§2.4.1a 函数体(逐字):控制字符→空格 → 折叠空白 → 截断;RFC 2047 编码交给设头对象,不在这里手拼。"""
    if value is None:
        return ""
    s = "".join(" " if c in _CTRL else c for c in value)   # ① 控制字符→空格,杜绝换行注入
    s = re.sub(r"\s+", " ", s).strip()                     # ② 折叠空白
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"                         # ③ 截断
    return s                                               # ④ RFC2047 编码交给 email.headerregistry


def build_headers(route: Any, subject_raw: str, *, extra: Optional[dict[str, str]] = None) -> EmailMessage:
    """§2.4.1a 唯一设头入口(逐字):收件人只来自路由,占位符碰不到。

    ``route`` 需提供 ``outbound_from`` / ``outbound_to`` / ``outbound_cc``(见 ``routes.MailRoute``)。
    ``extra`` 里的 ``In-Reply-To``/``References`` 等同样过 ``hdr_sanitize``。
    """
    msg = EmailMessage()
    msg["Subject"] = hdr_sanitize(subject_raw, max_len=SUBJECT_MAX_LEN)   # 头对象自动 RFC2047
    msg["From"] = route.outbound_from                                     # 配置值,不来自消息
    to = list(route.outbound_to)                                          # 列表,来自 mail_routes,不掺任何占位符
    if to:
        msg["To"] = ", ".join(to)
    if route.outbound_cc:
        msg["Cc"] = ", ".join(route.outbound_cc)
    for k, v in (extra or {}).items():
        if v:
            msg[k] = hdr_sanitize(v, max_len=HEADER_MAX_LEN)
    return msg


def escape_html(value: Optional[str]) -> str:
    """规则 3:``text/html`` 份对占位符值做 HTML 转义(``< > & " '``),防标签/MIME 边界注入。"""
    return _html.escape(value or "", quote=True)


def strip_ctrl_for_text(value: Optional[str]) -> str:
    """规则 3:``text/plain`` 份剥控制字符(保留换行——正文允许多行,只剥 CR 与其它 C0)。"""
    if value is None:
        return ""
    out = []
    for ch in value:
        if ch == "\n":
            out.append(ch)
        elif ch in _CTRL or (ch < " " and ch not in "\t"):
            out.append(" " if ch in ("\t", "\r") else "")
        else:
            out.append(ch)
    return "".join(out)


def recipients_of(route: Any, *, session_id: Optional[str] = None) -> tuple[list[str], list[str]]:
    """规则 2 的唯一取值口:收件人集合只来自路由(可被 ``session_overrides[]`` 覆盖,§2.15.2)。"""
    to, cc = list(route.outbound_to), list(route.outbound_cc)
    if session_id:
        for ov in route.session_overrides:
            if str(ov.get("session_id") or "") == session_id:
                to = [str(x) for x in (ov.get("to") or to)]
                cc = [str(x) for x in (ov.get("cc") or cc)]
                break
    return to, cc


def join_addrs(addrs: Iterable[str]) -> str:
    return ", ".join(a for a in addrs if a)
