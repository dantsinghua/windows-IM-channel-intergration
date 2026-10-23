"""文本归一化与清洗 —— 规格唯一出处:docs/06 §2.9.2(norm)与 §2.9.5(clean_text)。

分工(R6-47 逐字定死):
- ``clean_text``:**读库解码侧**,只在企点读库路的 ``to_message()`` 里调用一次,产出落库的 ``text``。
- ``norm``:**比较侧**,对两侧已落库/待落库的 ``text`` 各算一次(``fingerprint`` 的输入、
  06 §2.12 出向合并的判据),三通道同一个函数;不剥 U+0014、不改 ``[名称]``/``[表情]``/``[图片…]`` 占位、不截断。
- 出向 ``SENDING`` 行的 ``text`` 是 ``send_*`` 的原文,入库前**不过** ``clean_text``;
  两侧能相等由 R6-48 的入口校验(``clean_text(text) == text``)保证,见 bus/validate.py。
"""
from __future__ import annotations

import re
import unicodedata

from .qidian_faces import face_name

# U+0014 = QQ/企点客户端内部的表情转义;其后 1 个字符是表情索引(码位实测 U+0000~U+00B8)
_FACE_ESC = "\u0014"
FACE_PLACEHOLDER = "[表情]"          # 索引查不到名称时的兜底占位(R6-66)
IMAGE_PLACEHOLDER = "[图片]"         # -1035 图片段解不出宽高时的兜底占位(R6-66)
_KEEP_CTRL = frozenset("\t\n\r")


def norm(s: str | None) -> str:
    """比较侧归一化(fingerprint 输入 / 出向合并判据)。只做三件事:NFKC、折叠空白、去首尾空白。

    06 §2.9.2 原句照抄;``norm(None) == norm("") == ""``,§2.12 空文本守卫的「为空」按此返回值判。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()   # 折叠一切空白为单个空格;不剥 U+0014(那是 clean_text)


def clean_text(s: str) -> str:
    """读库解码侧清洗(06 §2.9.5 ``clean_text``,R6-46 定稿)。

    在 **字符(str)层面** 自左向右、不重叠地扫描:
    ① 遇到 U+0014,连同其紧随其后的 1 个字符 ``c`` 一起替换为 ``[名称]``(R6-66:名称 =
       ``qidian_faces.FACE_NAMES[ord(c)]``,如 ``[玫瑰]``;越界查不到时写 ``[表情]``)——后继是什么
       都一并消耗(含 ``\\t``/``\\n``/``\\r``,它们此时是索引 9/10/13);U+0014 在串尾、没有后继时
       只丢掉它自己、不写占位;连续两个 U+0014 时按「不重叠」第二个被当作第一个的索引一并消耗、只出一个占位。
    ② 未被 ① 消耗的其余码位 < U+0020 的字符逐个丢弃,但 ``\\t``/``\\n``/``\\r`` 保留。
    不得在字节层面处理(索引 ≥ U+0080 占 2 字节)。
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == _FACE_ESC:
            if i + 1 < n:
                name = face_name(ord(s[i + 1]))
                out.append(f"[{name}]" if name else FACE_PLACEHOLDER)
                i += 2          # 连同后继 1 个字符一起消耗(后继是什么都消耗)
            else:
                i += 1          # 串尾孤零的 U+0014:只丢自己、不写占位
            continue
        if ch < " " and ch not in _KEEP_CTRL:
            i += 1              # 其余控制字符逐个丢弃
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def has_control_chars(s: str) -> bool:
    """R6-48 入口校验的判据:``clean_text(s) != s``(含 U+0014 或 \\t\\n\\r 以外的 < U+0020 字符)。"""
    return clean_text(s) != s
