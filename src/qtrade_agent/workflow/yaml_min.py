"""工作流 YAML 的**最小子集**解析器(02 §2.2.6 / #39「解析校验失败 ``400`` 带行号」)。

为什么自己写:Agent 本期依赖**只有标准库**(``pyproject.toml``:「本期骨架只用标准库,便于在无网络环境单测」),
而 02 §2.2.6 的 YAML 形态被 R-13 砍成**直线流程**后极其简单 —— 标量、块映射、块序列、流式 ``{}``/``[]``,
没有锚点/别名/多文档/折叠标量。解析器**故意只认这个子集**,遇到子集外的写法报错(带行号),
不去猜 —— 猜错的后果是「YAML 在这里能过、在别处含义不同」。

装了 PyYAML 的部署可以把 ``yaml.safe_load`` 作为 ``loader`` 注入
(``parse_workflow(text, loader=yaml.safe_load)``),本解析器只是缺省实现。

支持:
- ``key: value`` 块映射(按缩进嵌套)、``- item`` 块序列(item 可以是标量或 ``key: value`` 起头的映射);
- 流式 ``{a: 1, b: "x"}`` / ``[1, "x"]``(单行);
- 标量:单/双引号字符串、整数、浮点、``true|false``、``null|~``,其余按字符串;
- ``#`` 行内注释(引号内的 ``#`` 不算)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class YamlError(ValueError):
    """带行号的解析错误(#39 要求 ``400`` 带行号)。"""

    def __init__(self, message: str, line: int):
        super().__init__(f"第 {line} 行:{message}")
        self.message = message
        self.line = line


@dataclass
class _Line:
    indent: int
    text: str
    lineno: int


def _strip_comment(raw: str) -> str:
    """去掉行内注释;引号内的 ``#`` 不是注释。"""
    out, quote = [], ""
    for ch in raw:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scan(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith("#"):
            continue
        body = _strip_comment(raw)
        if not body.strip():
            continue
        if "\t" in body[: len(body) - len(body.lstrip())]:
            raise YamlError("缩进不得使用制表符(YAML 规范禁止)", i)
        lines.append(_Line(indent=len(body) - len(body.lstrip(" ")), text=body.strip(), lineno=i))
    return lines


def _split_flow(body: str, lineno: int) -> list[str]:
    """按顶层逗号切分流式集合内部(忽略嵌套 ``{}``/``[]`` 与引号里的逗号)。"""
    parts, depth, quote, cur = [], 0, "", []
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                raise YamlError("流式集合括号不匹配", lineno)
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if quote:
        raise YamlError("引号未闭合", lineno)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_scalar(token: str, lineno: int) -> Any:
    t = token.strip()
    if t == "":
        return ""
    if t[0] in "'\"":
        if len(t) < 2 or t[-1] != t[0]:
            raise YamlError("引号未闭合", lineno)
        return t[1:-1]
    if t.startswith("{") or t.startswith("["):
        return _parse_flow(t, lineno)
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _parse_flow(token: str, lineno: int) -> Any:
    t = token.strip()
    if t.startswith("{"):
        if not t.endswith("}"):
            raise YamlError("流式映射未闭合(本解析器只认单行 `{…}`)", lineno)
        out: dict[str, Any] = {}
        for part in _split_flow(t[1:-1], lineno):
            key, sep, val = part.partition(":")
            if not sep:
                raise YamlError(f"流式映射项缺少冒号:{part!r}", lineno)
            out[str(_parse_scalar(key, lineno))] = _parse_scalar(val, lineno)
        return out
    if t.startswith("["):
        if not t.endswith("]"):
            raise YamlError("流式序列未闭合(本解析器只认单行 `[…]`)", lineno)
        return [_parse_scalar(p, lineno) for p in _split_flow(t[1:-1], lineno)]
    raise YamlError(f"无法解析的流式值:{t!r}", lineno)


def _parse_block(lines: list[_Line], i: int, indent: int) -> tuple[Any, int]:
    if i >= len(lines):
        return None, i
    if lines[i].text.startswith("- "):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_seq(lines: list[_Line], i: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while i < len(lines) and lines[i].indent == indent and lines[i].text.startswith("- "):
        ln = lines[i]
        body = ln.text[2:].strip()
        inner_indent = ln.indent + 2
        key, sep, rest = body.partition(":")
        if sep and not body.startswith(("{", "[", "'", '"')):
            # `- id: s1` 起头的块映射:把这一行当作该映射的第一对
            item: dict[str, Any] = {}
            i += 1
            value_text = rest.strip()
            if value_text:
                item[key.strip()] = _parse_scalar(value_text, ln.lineno)
            elif i < len(lines) and lines[i].indent > inner_indent:
                item[key.strip()], i = _parse_block(lines, i, lines[i].indent)
            else:
                item[key.strip()] = None
            if i < len(lines) and lines[i].indent == inner_indent and not lines[i].text.startswith("- "):
                more, i = _parse_map(lines, i, inner_indent)     # 同一项里余下的 key: value
                item.update(more)
            items.append(item)
            continue
        items.append(_parse_scalar(body, ln.lineno))
        i += 1
    return items, i


def _parse_map(lines: list[_Line], i: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while i < len(lines) and lines[i].indent == indent:
        ln = lines[i]
        if ln.text.startswith("- "):
            break
        key, sep, rest = ln.text.partition(":")
        if not sep:
            raise YamlError(f"不是 `key: value` 形式:{ln.text!r}", ln.lineno)
        key = key.strip()
        if key in out:
            raise YamlError(f"重复的键 {key!r}", ln.lineno)
        value_text = rest.strip()
        i += 1
        if value_text:
            out[key] = _parse_scalar(value_text, ln.lineno)
            continue
        if i < len(lines) and lines[i].indent > indent:
            out[key], i = _parse_block(lines, i, lines[i].indent)
        elif i < len(lines) and lines[i].indent == indent and lines[i].text.startswith("- "):
            out[key], i = _parse_seq(lines, i, indent)          # 序列与其父键同缩进(YAML 允许)
        else:
            out[key] = None
    return out, i


def safe_load(text: str) -> Optional[Any]:
    """解析最小子集;顶层返回映射(工作流恒是映射),空文档回 None。"""
    lines = _scan(text)
    if not lines:
        return None
    if lines[0].indent != 0:
        raise YamlError("首行不得缩进", lines[0].lineno)
    value, idx = _parse_block(lines, 0, 0)
    if idx != len(lines):
        raise YamlError("缩进不一致,解析在此处中断", lines[idx].lineno)
    return value
