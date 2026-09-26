# -*- coding: utf-8 -*-
"""引擎 ``qtrade-setup-engine.iss`` 的编译门 + [Code] 段保留字静态守卫。

来由:验收 X5(2026-09-26)—— ``JsonStr`` 用 Pascal Script 保留字 ``Out`` 作局部变量名,
ISCC 报 ``Identifier expected``,整份引擎编不过,而 pytest / Pester 全绿。本文件让
「.iss 编不过」在测试里就红:

1. ``test_engine_iss_compiles``:找得到 ISCC.exe 就在临时副本里真编译;**找不到就 fail**
   (不 skip —— 缺编译器 = 这道门没验,不能算过)。可用环境变量 ``QT_ISCC`` 指定路径。
2. ``test_code_section_declarations_avoid_reserved_words``:扫描 [Code] 段的
   var / const / type(record 字段)声明与函数、过程的形参名,不得用下列保留字。
3. ``test_reserved_word_scanner_catches_x5``:用 X5 原样的声明反向验证扫描器能红。

跑法::

    ~/.venvs/qtrade/bin/python -m pytest -q installer/tests/test_engine_iss_compile.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = INSTALLER_ROOT / "engine"
ISS = ENGINE_DIR / "qtrade-setup-engine.iss"

# Pascal Script(RemObjects,Inno Setup 6 [Code] 所用)保留字 —— 不得用作变量/常量/字段/形参名。
# 前 13 个是任务明确要求的;其余为 Pascal Script 词法里同样保留的关键字。
# 注:Result 不是关键字,但它是函数的隐式返回变量,再声明一个同名变量即重复标识符,一并禁止。
PASCAL_SCRIPT_RESERVED: frozenset[str] = frozenset(
    w.lower()
    for w in (
        # 任务明确列出
        "Out", "In", "Set", "Type", "Var", "Const", "Begin", "End", "Result",
        "File", "Label", "Unit", "Uses",
        # Pascal Script 其余关键字
        "And", "Array", "As", "Case", "Class", "Constructor", "Destructor", "Div",
        "Do", "DownTo", "Else", "Except", "Export", "Finally", "For", "Forward",
        "Function", "Goto", "If", "Implementation", "Inherited", "Interface", "Is",
        "Mod", "Nil", "Not", "Object", "Of", "Or", "Private", "Procedure",
        "Program", "Property", "Protected", "Public", "Published", "Record",
        "Repeat", "Shl", "Shr", "String", "Then", "To", "Try", "Until", "While",
        "With", "Xor",
    )
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def _code_section(src: str) -> str:
    """取 [Code] 段正文(到下一个 [Section] 或文件尾)。"""
    m = re.search(r"(?im)^\[Code\]\s*$", src)
    assert m is not None, "iss 里没有 [Code] 段"
    rest = src[m.end():]
    nxt = re.search(r"(?m)^\[[A-Za-z]+\]\s*$", rest)
    return rest[: nxt.start()] if nxt else rest


def _strip_comments_and_strings(code: str) -> str:
    """去掉字符串字面量与三种注释(保留换行,便于报行号)。"""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == "'":  # 'xx''yy' 字符串
            j = i + 1
            while j < n:
                if code[j] == "'" and j + 1 < n and code[j + 1] == "'":
                    j += 2
                    continue
                if code[j] == "'" or code[j] == "\n":
                    break
                j += 1
            out.append("''")
            i = j + 1
        elif code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
        elif c == "{":
            j = code.find("}", i)
            j = n if j < 0 else j + 1
            out.append("\n" * code.count("\n", i, j))
            i = j
        elif code.startswith("(*", i):
            j = code.find("*)", i)
            j = n if j < 0 else j + 2
            out.append("\n" * code.count("\n", i, j))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def find_reserved_declarations(code_section: str) -> list[tuple[int, str, str]]:
    """返回 [(行号(相对 [Code] 段), 声明种类, 名字)] —— 用了保留字的声明。"""
    code = _strip_comments_and_strings(code_section)
    hits: list[tuple[int, str, str]] = []

    def check(names: str, kind: str, lineno: int) -> None:
        for name in names.split(","):
            name = name.strip()
            if re.fullmatch(_IDENT, name) and name.lower() in PASCAL_SCRIPT_RESERVED:
                hits.append((lineno, kind, name))

    # 1) var / const / type 块里的声明
    block = ""  # 当前所在声明块
    for lineno, line in enumerate(code.split("\n"), start=1):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"(?i)(var|const|type)\b(.*)$", s)
        if m:
            block = m.group(1).lower()
            s = m.group(2).strip()
            if not s:
                continue
        elif re.match(r"(?i)(begin|function|procedure)\b", s):
            block = ""
            continue
        if not block:
            continue
        if block == "var":
            dm = re.match(rf"({_IDENT}(?:\s*,\s*{_IDENT})*)\s*:", s)
            if dm:
                check(dm.group(1), "var", lineno)
        elif block == "const":
            dm = re.match(rf"({_IDENT})\s*(?::[^=]*)?=", s)
            if dm:
                check(dm.group(1), "const", lineno)
        else:  # type:TName = ...;record 字段 name: type;
            dm = re.match(rf"({_IDENT})\s*=", s)
            if dm:
                check(dm.group(1), "type", lineno)
                continue
            dm = re.match(rf"({_IDENT}(?:\s*,\s*{_IDENT})*)\s*:", s)
            if dm:
                check(dm.group(1), "record 字段", lineno)

    # 2) 函数 / 过程形参
    for m in re.finditer(rf"(?is)\b(?:function|procedure)\s+{_IDENT}\s*\((.*?)\)", code):
        lineno = code.count("\n", 0, m.start()) + 1
        for group in m.group(1).split(";"):
            g = re.sub(r"(?i)^\s*(const|var|out)\s+", "", group)
            if ":" in g:
                check(g.split(":", 1)[0], "形参", lineno)
    return hits


def test_reserved_word_scanner_catches_x5() -> None:
    """反向验证:X5 原样的声明(局部变量 Out)必须被扫出来。"""
    sample = (
        "function JsonStr(const Json, Key: String): String;\n"
        "var\n"
        "  P, Q, I: Integer;\n"
        "  Pat, Raw, Out, HexStr: String;\n"
        "  C: Char;\n"
        "begin\n"
        "  Result := ''; // Out 出现在注释里不算\n"
        "end;\n"
        "procedure Foo(const Set: String; var OutCode: Integer);\n"
        "begin\n"
        "end;\n"
    )
    hits = find_reserved_declarations(sample)
    names = sorted(h[2] for h in hits)
    assert names == ["Out", "Set"], hits


def test_code_section_declarations_avoid_reserved_words() -> None:
    src = ISS.read_text(encoding="utf-8-sig")
    code = _code_section(src)
    base = src[: src.index(code)].count("\n")  # [Code] 段在文件里的起始行偏移
    hits = find_reserved_declarations(code)
    assert not hits, "[Code] 段声明用了 Pascal Script 保留字(ISCC 会报 Identifier expected):\n" + "\n".join(
        f"  第 {base + ln} 行 {kind} `{name}`" for ln, kind, name in hits
    )


# ── 编译门 ──────────────────────────────────────────────────────────────────
_ISCC_CANDIDATES = (
    "/mnt/c/Program Files (x86)/Inno Setup 6/ISCC.exe",
    "/mnt/c/Program Files/Inno Setup 6/ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
)


def _find_iscc() -> str | None:
    env = os.environ.get("QT_ISCC", "").strip()
    if env:
        return env if Path(env).is_file() else None
    for name in ("ISCC.exe", "iscc"):
        hit = shutil.which(name)
        if hit:
            return hit
    for cand in _ISCC_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _to_windows_path(p: Path, iscc: str) -> str:
    """ISCC 是 Windows 程序:从 WSL 调用时把 Linux 路径转成 Windows 路径。"""
    if iscc.startswith("/"):
        return subprocess.run(
            ["wslpath", "-w", str(p)], check=True, capture_output=True, text=True
        ).stdout.strip()
    return str(p)


def test_engine_iss_compiles(tmp_path: Path) -> None:
    iscc = _find_iscc()
    if iscc is None:
        pytest.fail(
            "找不到 ISCC.exe,引擎编译门没法验(不许 skip)。装 Inno Setup 6"
            "(https://jrsoftware.org/isdl.php),或设环境变量 QT_ISCC=<ISCC.exe 路径> 后重跑。"
            f"已找过:QT_ISCC、PATH、{', '.join(_ISCC_CANDIDATES)}"
        )
    # 在临时副本里编译:产物不进仓库,也不碰 installer/out
    work = tmp_path / "engine"
    shutil.copytree(ENGINE_DIR, work)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    cmd = [
        iscc,
        "/O" + _to_windows_path(out_dir, iscc),
        "/DEngineVersion=1.0.0",  # 同 build.ps1 的传法(VersionInfoVersion 要纯数字)
        _to_windows_path(work / ISS.name, iscc),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    log = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    tail = "\n".join(log.strip().splitlines()[-30:])
    assert proc.returncode == 0, f"ISCC 编译失败(退出码 {proc.returncode}),日志末尾:\n{tail}"
    exes = list(out_dir.glob("*.exe"))
    assert exes, f"ISCC 退出码 0 但没产出 EXE,日志末尾:\n{tail}"
