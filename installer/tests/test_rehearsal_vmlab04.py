# -*- coding: utf-8 -*-
"""fieldtest/vm-lab 排练脚本 —— 静态契约(vmlab04 排练门)。

只读 ``fieldtest/vm-lab`` 下 7 个 ps1 的源文本,不执行、不改写它们。
实现方改产品脚本后,本文件应能验收契约;测试红着留下,测试方不修产品。

契约:
1. 每个脚本在顶层 ``param(...)`` 结束之后必须有可执行赋值
   ``$ProgressPreference = 'SilentlyContinue'``(不得仅出现在注释里)。
2. 顶层 ``param(...)`` 的默认值表达式里不得出现 ``$PSScriptRoot``。
3. ``04-把安装包送进虚拟机.ps1`` 不得含「你在 02b 里设的那个」,
   不得含写死的「2.26 GB」或「2.26」。
4. ``04``「将要做的事」里拷贝相关 ``foreach`` 循环体内不得写死步骤号 ``2)``;
   若改用计数器,只要循环里没有字面量 ``2)`` 即通过。

跑法::

    py -3 -m pytest -q installer/tests/test_rehearsal_vmlab04.py
    # 或
    C:\\Python312\\python.exe -m pytest -q installer/tests/test_rehearsal_vmlab04.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VMLAB = REPO_ROOT / "fieldtest" / "vm-lab"

SCRIPTS = (
    "00-只读体检.ps1",
    "01-启用HyperV.ps1",
    "01b-撤销HyperV.ps1",
    "02-建测试虚拟机.ps1",
    "02b-制作应答ISO.ps1",
    "03-快照与回滚.ps1",
    "04-把安装包送进虚拟机.ps1",
)

SCRIPT_04 = "04-把安装包送进虚拟机.ps1"

_PROGRESS_ASSIGN = re.compile(
    r"\$ProgressPreference\s*=\s*['\"]SilentlyContinue['\"]",
    re.IGNORECASE,
)
_FOREACH = re.compile(
    r"foreach\s*\([^)]*\)\s*\{",
    re.IGNORECASE,
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def strip_line_comment(line: str) -> str:
    """去掉 PowerShell 行注释 ``#...``(字符串内的 # 按简单启发保留)。"""
    in_single = False
    in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
        i += 1
    return line


def strip_comments(text: str) -> str:
    return "\n".join(strip_line_comment(ln) for ln in text.splitlines())


def extract_top_level_param(text: str) -> tuple[str, int]:
    """取出脚本顶层第一个 ``param(...)`` 全文,以及闭合 ``)`` 之后的字符下标。

    用括号深度匹配,避开嵌套 ``param($d)``(那些出现在顶层 param 之后)。
    """
    # 跳过注释头里的 param 字样:从 [CmdletBinding] 或文件前部找第一个 param(
    m = re.search(r"(?m)^param\s*\(", text)
    if not m:
        raise AssertionError("找不到顶层 param(...)")
    start = m.start()
    i = m.end()  # 已过开 '('
    depth = 1
    in_single = False
    in_double = False
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    return text[start:end], end
        i += 1
    raise AssertionError("param(...) 括号未闭合")


def extract_brace_block(text: str, open_brace_index: int) -> str:
    """从 ``{`` 下标起取出配对的大括号块(含两端大括号)。"""
    assert text[open_brace_index] == "{"
    depth = 0
    in_single = False
    in_double = False
    i = open_brace_index
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_brace_index : i + 1]
        i += 1
    raise AssertionError("大括号块未闭合")


def section_after_head(text: str, head_title: str) -> str:
    """取 ``Write-Head '...'`` 之后到下一个 ``Write-Head`` 之前的文本。"""
    pat = re.compile(
        r"Write-Head\s+['\"]" + re.escape(head_title) + r"['\"]",
        re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        raise AssertionError(f"找不到 Write-Head '{head_title}'")
    rest = text[m.end() :]
    nxt = re.search(r"Write-Head\s+", rest, re.IGNORECASE)
    return rest[: nxt.start()] if nxt else rest


@pytest.fixture(scope="module")
def scripts() -> dict[str, str]:
    missing = [n for n in SCRIPTS if not (VMLAB / n).is_file()]
    if missing:
        pytest.fail(f"缺少脚本: {missing}")
    return {name: read(VMLAB / name) for name in SCRIPTS}


@pytest.mark.parametrize("name", SCRIPTS)
def test_progress_preference_after_param(scripts: dict[str, str], name: str) -> None:
    """契约 1:param 块结束后须设置 $ProgressPreference = 'SilentlyContinue'。"""
    src = scripts[name]
    _, after = extract_top_level_param(src)
    tail = src[after:]
    # 可执行代码:去注释后再匹配赋值
    code = strip_comments(tail)
    assert _PROGRESS_ASSIGN.search(code), (
        f"{name}: 顶层 param(...) 之后缺少可执行赋值 "
        "$ProgressPreference = 'SilentlyContinue'(不得只写在注释里)"
    )


@pytest.mark.parametrize("name", SCRIPTS)
def test_param_defaults_no_psscriptroot(scripts: dict[str, str], name: str) -> None:
    """契约 2:顶层 param 默认值表达式不得含 $PSScriptRoot。"""
    src = scripts[name]
    block, _ = extract_top_level_param(src)
    cleaned = strip_comments(block)
    assert "$PSScriptRoot" not in cleaned, (
        f"{name}: 顶层 param(...) 默认值表达式里出现了 $PSScriptRoot"
    )


def test_04_no_hardcoded_02b_password_hint(scripts: dict[str, str]) -> None:
    """契约 3a:04 不得含「你在 02b 里设的那个」。"""
    src = scripts[SCRIPT_04]
    assert "你在 02b 里设的那个" not in src, (
        f"{SCRIPT_04}: 不得含「你在 02b 里设的那个」"
    )


def test_04_no_hardcoded_2_26_size(scripts: dict[str, str]) -> None:
    """契约 3b:04 不得含写死的「2.26 GB」或「2.26」。"""
    src = scripts[SCRIPT_04]
    assert "2.26 GB" not in src, f"{SCRIPT_04}: 不得含写死的「2.26 GB」"
    assert "2.26" not in src, f"{SCRIPT_04}: 不得含写死的「2.26」"


def test_04_plan_foreach_no_hardcoded_step_2(scripts: dict[str, str]) -> None:
    """契约 4:「将要做的事」里 foreach 循环体不得写死步骤号「2)」。"""
    src = scripts[SCRIPT_04]
    section = section_after_head(src, "将要做的事")
    # 在该节内找每个 foreach,检查循环体
    pos = 0
    found_foreach = False
    while True:
        m = _FOREACH.search(section, pos)
        if not m:
            break
        found_foreach = True
        brace_at = m.end() - 1  # 指向 '{'
        assert section[brace_at] == "{"
        body = extract_brace_block(section, brace_at)
        body_code = strip_comments(body)
        assert "2)" not in body_code, (
            f"{SCRIPT_04}: 「将要做的事」的 foreach 循环体内写死了步骤号「2)」;"
            "应改用计数器等动态步骤号(测试不绑死变量名,只要循环里没有字面量 2))"
        )
        pos = brace_at + len(body)

    assert found_foreach, (
        f"{SCRIPT_04}: 「将要做的事」节内应有拷贝相关的 foreach 循环"
    )
