# -*- coding: utf-8 -*-
"""排练门禁: installer/engine 下两个 cmd 必须整文件纯 ASCII + CRLF。

验收口径(独立测试方, 含 rem 注释行, 不做「跳过 rem」放宽):

* 无 UTF-8 BOM
* 每个字节对应字符的码点 < 128(整文件纯 ASCII)
* 换行一律 CRLF(允许文件末尾无换行)
* ``run-engine.cmd`` 可执行行仍包含 ``qtrade-setup-engine.exe`` 与 ``last-exit-code.txt``
* ``precheck-disk.cmd`` 仍是 cmd 脚本(含 ``@echo off``)

跑法::

    py -3 -m pytest -q installer/tests/test_rehearsal_cmd_ascii.py
    C:\\Python312\\python.exe -m pytest -q installer/tests/test_rehearsal_cmd_ascii.py
"""
from __future__ import annotations

from pathlib import Path

import pytest

INSTALLER_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = INSTALLER_ROOT / "engine"

RUN_ENGINE = ENGINE_DIR / "run-engine.cmd"
PRECHECK_DISK = ENGINE_DIR / "precheck-disk.cmd"

CMD_FILES = (RUN_ENGINE, PRECHECK_DISK)


def _read_bytes(path: Path) -> bytes:
    assert path.is_file(), f"缺少文件: {path}"
    return path.read_bytes()


def _assert_no_bom(raw: bytes, path: Path) -> None:
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name}: 不得带 UTF-8 BOM"


def _assert_pure_ascii(raw: bytes, path: Path) -> None:
    """整文件每个字节须 < 128(含 rem 注释, 不做行级豁免)。"""
    offenders: list[tuple[int, int]] = []
    for i, b in enumerate(raw):
        if b >= 128:
            offenders.append((i, b))
            if len(offenders) >= 8:
                break
    assert not offenders, (
        f"{path.name}: 非整文件纯 ASCII; "
        f"前若干越界字节 offset/byte={offenders}"
    )


def _assert_crlf_newlines(raw: bytes, path: Path) -> None:
    """所有换行须为 CRLF; 允许文件末尾无换行符。"""
    # 去掉合法的 \r\n 后再查残留的裸 \n / 裸 \r
    stripped = raw.replace(b"\r\n", b"")
    lone_lf = stripped.count(b"\n")
    lone_cr = stripped.count(b"\r")
    assert lone_lf == 0 and lone_cr == 0, (
        f"{path.name}: 换行必须为 CRLF; "
        f"残留 lone_lf={lone_lf} lone_cr={lone_cr}"
    )


@pytest.mark.parametrize("path", CMD_FILES, ids=lambda p: p.name)
def test_cmd_no_bom(path: Path) -> None:
    _assert_no_bom(_read_bytes(path), path)


@pytest.mark.parametrize("path", CMD_FILES, ids=lambda p: p.name)
def test_cmd_whole_file_pure_ascii(path: Path) -> None:
    _assert_pure_ascii(_read_bytes(path), path)


@pytest.mark.parametrize("path", CMD_FILES, ids=lambda p: p.name)
def test_cmd_crlf_newlines(path: Path) -> None:
    _assert_crlf_newlines(_read_bytes(path), path)


def test_run_engine_still_invokes_engine_and_records_exit() -> None:
    raw = _read_bytes(RUN_ENGINE)
    # 可执行行 = 非 rem / 非空注释意图; 用解码后按行过滤(ASCII 门禁另测)
    text = raw.decode("utf-8", errors="replace")
    exec_lines = [
        ln
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().lower().startswith("rem")
    ]
    joined = "\n".join(exec_lines)
    assert "qtrade-setup-engine.exe" in joined, (
        "run-engine.cmd 可执行行须包含 qtrade-setup-engine.exe"
    )
    assert "last-exit-code.txt" in joined, (
        "run-engine.cmd 可执行行须包含 last-exit-code.txt"
    )


def test_precheck_disk_is_cmd_script() -> None:
    raw = _read_bytes(PRECHECK_DISK)
    text = raw.decode("utf-8", errors="replace")
    assert "@echo off" in text, "precheck-disk.cmd 须含 @echo off(仍是 cmd 脚本)"
