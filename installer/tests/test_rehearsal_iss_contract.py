# -*- coding: utf-8 -*-
"""预演契约：静态读 ``qtrade-setup-engine.iss`` 文本并断言。

独立测试方契约。不编译、不运行安装 EXE。
若当前 iss 尚未满足预演要求，本文件应红——不得为变绿而改产品或放宽断言。

跑法::

    py -3 -m pytest -q installer/tests/test_rehearsal_iss_contract.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

INSTALLER_ROOT = Path(__file__).resolve().parent.parent
ISS = INSTALLER_ROOT / "engine" / "qtrade-setup-engine.iss"


def _read_iss() -> str:
    return ISS.read_text(encoding="utf-8-sig")


def _pascal_function_body(src: str, name: str) -> str:
    """提取 ``function Name(...): ...; begin ... end;`` 函数体(含 begin/end)。"""
    m = re.search(
        rf"(?is)function\s+{re.escape(name)}\s*\(.*?\)\s*:\s*[^;]+;\s*"
        r"(?:var\b.*?)?begin\b(.*?)\bend\s*;",
        src,
    )
    assert m is not None, f"未找到 function {name}"
    return m.group(0)


def _pascal_procedure_body(src: str, name: str) -> str:
    m = re.search(
        rf"(?is)procedure\s+{re.escape(name)}\s*\(.*?\)\s*;\s*"
        r"(?:var\b.*?)?begin\b(.*?)\bend\s*;",
        src,
    )
    assert m is not None, f"未找到 procedure {name}"
    return m.group(0)


# ── 1. RunStep -File 必须指向 {tmp} 副本 ───────────────────────────────────
def test_runstep_file_uses_tmp_copy_not_app():
    body = _pascal_function_body(_read_iss(), "RunStep")
    # -File 参数段：不得展开 {app}\...\run-step.ps1
    assert not re.search(
        r"(?i)-File\b[^;\n]*\{app\}[^;\n]*run-step\.ps1",
        body,
    ), "RunStep 的 -File 不得指向 {app}\\...\\run-step.ps1"
    assert re.search(
        r"(?i)-File\b[^;\n]*\{tmp\}[^;\n]*run-step\.ps1",
        body,
    ), "RunStep 的 -File 必须指向含 {tmp} 且含 run-step.ps1 的路径"


# ── 2. RunStep -Root 必须用 {commonappdata}\QTrade ─────────────────────────
def test_runstep_root_uses_commonappdata_qtrade():
    body = _pascal_function_body(_read_iss(), "RunStep")
    assert not re.search(
        r'(?i)-Root\s+"[^"]*ExpandConstant\(\s*\'\{app\}\'\s*\)',
        body,
    ) and not re.search(
        r"(?i)-Root\b[^;\n]*ExpandConstant\(\s*'\{app\}'\s*\)",
        body,
    ), "RunStep 的 -Root 不得使用 {app}"
    # 正面：-Root 路径须含 {commonappdata} 与 QTrade
    assert re.search(
        r"(?i)-Root\b[^;\n]*\{commonappdata\}[^;\n]*QTrade",
        body,
    ) or re.search(
        r"(?i)-Root\b[^;\n]*ExpandConstant\(\s*'\{commonappdata\}\\QTrade'\s*\)",
        body,
    ), "RunStep 的 -Root 必须使用 {commonappdata}\\QTrade"


# ── 3. run-step.ps1 与 modules\*.psm1 须有 dontcopy 形式 ───────────────────
def test_files_have_dontcopy_for_wizard_extract():
    src = _read_iss()
    files_m = re.search(r"(?is)\[Files\](.*?)(?:\n\[|\Z)", src)
    assert files_m is not None, "缺少 [Files] 段"
    files = files_m.group(1)

    def _has_dontcopy_for(source_pat: str) -> bool:
        for line in files.splitlines():
            if re.search(source_pat, line, re.I) and re.search(
                r"(?i)\bdontcopy\b", line
            ):
                return True
        return False

    assert _has_dontcopy_for(
        r'(?i)Source:\s*"run-step\.ps1"'
    ), "run-step.ps1 须有 Flags 含 dontcopy 的 [Files] 项(可与 DestDir {app} 条目并存)"
    assert _has_dontcopy_for(
        r'(?i)Source:\s*"modules\\\*\.psm1"'
    ), r"modules\*.psm1 须有 Flags 含 dontcopy 的 [Files] 项(可与 DestDir {app} 条目并存)"


# ── 4. GetRunningDistros 对 wsl 检测须有超时上限 ───────────────────────────
def test_get_running_distros_has_timeout_bound():
    body = _pascal_function_body(_read_iss(), "GetRunningDistros")
    # 若对 wsl 仅用无界 ewWaitUntilTerminated 且看不到任何超时相关逻辑 → 红
    has_timeout_signal = bool(
        re.search(
            r"(?is)("
            r"WaitForSingleObject|"
            r"\btimeout\b|"
            r"\b3\s*\*\s*1000\b|"
            r"\b3000\b|"
            r"3\s*秒|"
            r"Sleep\s*\(|"
            r"GetTickCount|"
            r"deadline|"
            r"time\s*limit|"
            r"超时"
            r")",
            body,
        )
    )
    # 无界等待：Exec(..., ewWaitUntilTerminated, ...) 且涉及 wsl，又无超时信号
    unbounded_wsl_wait = bool(
        re.search(r"(?is)wsl\.exe.*?ewWaitUntilTerminated", body)
        or re.search(r"(?is)ewWaitUntilTerminated.*?wsl\.exe", body)
        or (
            "wsl" in body.lower()
            and "ewWaitUntilTerminated" in body
            and not has_timeout_signal
        )
    )
    assert has_timeout_signal, (
        "GetRunningDistros 必须能看出对 wsl 检测存在超时上限"
        "（例如 3 秒、WaitForSingleObject、超时循环等）"
    )
    if unbounded_wsl_wait and not has_timeout_signal:
        pytest.fail(
            "GetRunningDistros 不得对 wsl.exe 使用无超时的 ewWaitUntilTerminated"
        )


# ── 5. AppName = QTrade（不是「QTrade 安装程序」）──────────────────────────
def test_appname_is_qtrade_not_installer_title():
    src = _read_iss()
    # 解析 #define EngineName（若 AppName 引用它）
    define_m = re.search(
        r'(?im)^#define\s+EngineName\s+"([^"]*)"',
        src,
    )
    appname_m = re.search(r"(?im)^AppName\s*=\s*(.+)$", src)
    assert appname_m is not None, "缺少 AppName="
    raw = appname_m.group(1).strip()
    if raw.startswith("{#EngineName}"):
        assert define_m is not None, "AppName 引用 EngineName 但未找到 #define"
        effective = define_m.group(1)
    elif raw.startswith('"') and raw.endswith('"'):
        effective = raw[1:-1]
    else:
        # 字面量无引号（Inno 允许）
        effective = raw.split(";")[0].strip().strip('"')

    assert effective == "QTrade", (
        f"AppName 有效值必须是 QTrade，当前为 {effective!r}；"
        "不得为「QTrade 安装程序」。SetupAppTitle 仍可含「安装程序」。"
    )
    # SetupAppTitle 允许含安装程序——仅作存在性旁证，不强制改 EngineExeName
    setup_title = re.search(r"(?im)^SetupAppTitle\s*=\s*(.+)$", src)
    assert setup_title is not None, "缺少 SetupAppTitle（契约仅要求其仍可含安装程序）"


# ── 6a. JsonStr 须处理反斜杠转义 ───────────────────────────────────────────
def test_jsonstr_handles_backslash_escape():
    body = _pascal_function_body(_read_iss(), "JsonStr")
    # 能看出 \\ 字面或反转义逻辑即可
    has_escape = bool(
        re.search(r"(?s)\\\\", body)  # Pascal 字符串里写 '\\' 常表现为 \\
        or re.search(r"(?i)backslash|unescape|escape|反转义|转义", body)
        or re.search(r"(?i)\\x5[cC]|Chr\s*\(\s*92\s*\)", body)
        or re.search(r"(?i)Json\s*\[\s*\w+\s*\]\s*=\s*'\\\\'", body)
        or re.search(r"(?is)while\b.*?\\.*?then", body)
    )
    # 更贴切：循环读字符时遇到 '\' 再 peek 下一字符
    has_escape = has_escape or bool(
        re.search(
            r"(?is)(Json\[\w+\]\s*=\s*'\\'|Copy\([^)]*\\\\|"
            r"Pos\([^)]*\\\\|StringChange|StringReplace)",
            body,
        )
    )
    assert has_escape, (
        "function JsonStr 的函数体必须处理反斜杠转义"
        "（能看出 \\\\ 或反转义逻辑）"
    )


# ── 6b. 「诊断包已导出」不得与 JsonStr(message) 无条件再拼 ─────────────────
def test_diag_export_prefix_not_unconditionally_concat_jsonstr_message():
    src = _read_iss()
    # 在 FailWith / 失败相关过程里找「诊断包已导出」与 JsonStr(..., 'message')
    # 无条件拼接形如: '诊断包已导出' ... + ... JsonStr(LastStepJson, 'message')
    bad = re.search(
        r"(?is)['\"]诊断包已导出[^'\"]*['\"]\s*"
        r"(?:\+\s*(?:#13#10|#10|#13)\s*)*\+\s*"
        r"JsonStr\s*\(\s*LastStepJson\s*,\s*'message'\s*\)",
        src,
    )
    # 也捕获前缀在字符串常量内、随后 + JsonStr 的写法
    bad2 = re.search(
        r"(?is)诊断包已导出.{0,120}?JsonStr\s*\(\s*LastStepJson\s*,\s*'message'\s*\)",
        src,
    )
    # 若存在「诊断包已导出」且与 JsonStr(LastStepJson,'message') 在同一 MsgBox
    # 表达式里无条件相加 → 红。允许：仅显示 JsonStr 结果，或有条件分支后再拼。
    if bad or bad2:
        # 检查同一作用域是否有 if/条件保护「拼前缀」——简单启发：
        # 若匹配段在 `if ... then` 内只拼 message、前缀在 else 分支等，仍可能合法；
        # 契约钉的是「无条件再拼一次」。若整段是 MsgBox('诊断包已导出:' + ... + JsonStr(...))
        # 这种直连，一律失败。
        snippet = (bad or bad2).group(0)
        # 条件保护：snippet 前 200 字符内有对 message 的 Contains/Pos 判断再决定是否加前缀
        start = (bad or bad2).start()
        window = src[max(0, start - 400) : start]
        conditional = bool(
            re.search(
                r"(?is)(Pos\s*\(\s*['\"]诊断包已导出|ContainsText\s*\(|"
                r"if\s+.*诊断包已导出.*then)",
                window,
            )
        )
        assert conditional, (
            "失败页里「诊断包已导出」前缀不得与 "
            "JsonStr(LastStepJson, 'message') 无条件再拼一次；"
            f"发现片段: {snippet[:160]!r}"
        )
