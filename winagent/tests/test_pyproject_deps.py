"""守住 pyproject 依赖清单:源码里 import 的每个第三方包都得在 pyproject 里声明。

2026-09-27 发现:``main_svc`` 起 uvicorn 用 ``ws="websockets"``(02 §2.4,不许 auto),但 pyproject
只依赖裸 ``uvicorn``,venv 里没有 ``websockets``;测试走 ASGI 传输用不到它,于是 pytest 全绿而 exe
一起服务就崩。本用例静态扫 ``src/qtrade_winagent/**`` 的全部 import(含函数内延迟导入 —— ``uvicorn``
正是延迟导入的),把第三方顶层包名经下面的映射表对到发行包名,断言都在 pyproject 声明里。
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tomllib

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
PKG_DIR = os.path.join(ROOT, "src", "qtrade_winagent")

# 导入名 → (发行包名, 必须在哪一组声明里)。"core" = [project].dependencies;"windows" = windows extra
# (Linux 上装不了、只在 Windows 真机装;后端一律延迟导入)。
IMPORT_TO_DIST: dict[str, tuple[str, str]] = {
    "fastapi": ("fastapi", "core"),
    "uvicorn": ("uvicorn", "core"),
    "websockets": ("websockets", "core"),
    "httpx": ("httpx", "core"),
    "psutil": ("psutil", "windows"),
    "wmi": ("wmi", "windows"),
}
# pywin32 的各模块(win32api / win32file / win32pipe / pywintypes / servicemanager …)
PYWIN32_PREFIXES = ("win32", "pywintypes", "pythoncom", "servicemanager")
# 不进 pyproject 的第三方包:build.ps1 另行安装(pywinauto/pillow 走 pip,pyweixin 走随包本地 wheel,见 03)。
OUT_OF_PYPROJECT = {"pywinauto", "PIL", "pyweixin"}


def _dist_name(req: str) -> str:
    return re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0].strip().lower().replace("_", "-")


def _declared() -> dict[str, set[str]]:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)["project"]
    core = {_dist_name(r) for r in project["dependencies"]}
    windows = {_dist_name(r) for r in project["optional-dependencies"]["windows"]}
    return {"core": core, "windows": core | windows}


def _sources() -> list[str]:
    return [os.path.join(d, fn) for d, _, files in os.walk(PKG_DIR) for fn in files if fn.endswith(".py")]


def _imported_top_names() -> dict[str, str]:
    """顶层导入名 → 第一次出现的位置(相对路径:行号)。"""
    found: dict[str, str] = {}
    for path in sorted(_sources()):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for n in names:
                found.setdefault(n.split(".")[0], f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    return found


def test_every_third_party_import_is_declared_in_pyproject() -> None:
    declared = _declared()
    missing: list[str] = []
    for top, where in sorted(_imported_top_names().items()):
        if top in sys.stdlib_module_names or top == "qtrade_winagent" or top in OUT_OF_PYPROJECT:
            continue
        if top.startswith(PYWIN32_PREFIXES):
            dist, group = "pywin32", "windows"
        elif top in IMPORT_TO_DIST:
            dist, group = IMPORT_TO_DIST[top]
        else:
            missing.append(f"{top}({where}):映射表里没有,先补映射再补 pyproject")
            continue
        if dist not in declared[group]:
            missing.append(f"{top}({where}) → {dist} 未在 pyproject 的 {group} 组声明")
    assert not missing, "\n".join(missing)


def test_uvicorn_ws_websockets_requires_websockets_dependency() -> None:
    """``ws="websockets"`` 不产生 import 语句,单独守:用了它就必须在核心依赖里声明 websockets。"""
    uses = []
    for path in _sources():
        with open(path, encoding="utf-8") as f:
            if re.search(r"""ws\s*=\s*["']websockets["']""", f.read()):
                uses.append(os.path.relpath(path, ROOT))
    assert uses, "没找到 ws=\"websockets\" —— 02 §2.4 要求显式指定,用例前提失效"
    assert "websockets" in _declared()["core"], f"{uses} 用 ws=\"websockets\",但 pyproject dependencies 缺 websockets"
