"""守住 PyInstaller spec 的入口:**必须是包外薄壳**,不能是包内 ``main_*.py``。

2026-09-27 发现:两个 spec 把 ``src/qtrade_winagent/main_svc.py`` / ``main_user.py`` 当入口,
PyInstaller 按 ``__main__`` 执行它们,``from . import …`` 报 ``attempted relative import with no known
parent package``,两个 exe 启动即崩(9/21 起的包都是)。本用例在 Linux 上就能拦住这类回退。

做法:用桩替换 PyInstaller 注入 spec 的 ``Analysis``/``PYZ``/``EXE``/``COLLECT``,真执行一遍 spec,
拿到 ``Analysis`` 的实参再断言 —— 不靠正则猜 spec 的写法。
"""
from __future__ import annotations

import ast
import os
from types import SimpleNamespace
from typing import Any

import pytest

BUILD_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "build"))
PKG_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "src", "qtrade_winagent"))
SPECS = {"svc": "qtrade-winagent-svc.spec", "user": "qtrade-winagent-user.spec"}


def _run_spec(name: str) -> dict[str, Any]:
    path = os.path.join(BUILD_DIR, SPECS[name])
    captured: dict[str, Any] = {}

    def analysis(scripts: list[str], **kw: Any) -> Any:
        captured["scripts"], captured["kw"] = scripts, kw
        return SimpleNamespace(scripts=scripts, pure=None, zipped_data=None, binaries=None, zipfiles=None, datas=None)

    def stub(*_a: Any, **_k: Any) -> None:
        return None

    ns = {"SPEC": path, "Analysis": analysis, "PYZ": stub, "EXE": stub, "COLLECT": stub, "__name__": "__spec__"}
    with open(path, encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), ns)                              # noqa: S102 —— 只执行本仓库的 spec
    return captured


@pytest.mark.parametrize("name", sorted(SPECS))
def test_spec_entry_is_thin_wrapper_outside_package(name: str) -> None:
    entry = os.path.realpath(_run_spec(name)["scripts"][0])
    assert os.path.dirname(entry) == BUILD_DIR, f"{SPECS[name]} 入口不在 winagent/build/ 下:{entry}"
    assert not entry.startswith(PKG_DIR + os.sep), f"{SPECS[name]} 入口是包内模块(会因相对导入启动即崩):{entry}"
    assert os.path.isfile(entry), f"入口脚本不存在:{entry}"


@pytest.mark.parametrize("name", sorted(SPECS))
def test_entry_script_has_no_relative_import_and_supports_selfcheck(name: str) -> None:
    entry = os.path.realpath(_run_spec(name)["scripts"][0])
    with open(entry, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, entry)
    rel = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
    assert not rel, f"入口脚本里有相对导入(作为 __main__ 必崩),行:{rel}"
    assert f"qtrade_winagent.main_{name}" in src
    assert "--selfcheck" in src, "build.ps1 冒烟门依赖 --selfcheck"


def test_user_spec_does_not_exclude_fastapi() -> None:
    """main_user → main_svc → svc 顶层 ``import fastapi``;排除它 user exe 一启动就 ModuleNotFoundError。"""
    excludes = _run_spec("user")["kw"]["excludes"]
    assert "fastapi" not in excludes and "starlette" not in excludes
