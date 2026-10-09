"""WSL output contract using fake subprocesses only; never starts real WSL."""
from __future__ import annotations

import codecs
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent.win import wsl


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(b"6.6.0\n", "6.6.0", id="utf8-even-byte-count"),
        pytest.param(b"6.6.12\n", "6.6.12", id="utf8-odd-byte-count"),
        pytest.param("错误：无法启动\n".encode("utf-8"), "错误：无法启动", id="utf8-chinese-error"),
        pytest.param("6.6.0\r\n".encode("utf-16-le"), "6.6.0", id="utf16le-no-bom"),
        pytest.param(codecs.BOM_UTF16_LE + "6.6.0\r\n".encode("utf-16-le"), "6.6.0", id="utf16le-bom"),
        pytest.param(codecs.BOM_UTF8 + b"6.6.0\n", "6.6.0", id="utf8-bom"),
        pytest.param("错误：无法启动\r\n".encode("utf-16-le"), "错误：无法启动", id="utf16le-chinese-error"),
        pytest.param(b"", "", id="empty-output"),
    ],
)
async def test_run_decodes_native_output_without_mojibake(monkeypatch, payload, expected):
    child = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(payload, None)))
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(wsl, "require_windows", lambda _feature: None)
    monkeypatch.setattr(wsl.asyncio, "create_subprocess_exec", launch)

    rc, text = await wsl.WinWsl()._run(["-d", "isolated-fake", "--exec", "uname", "-r"])

    assert (rc, text) == (0, expected)
    assert launch.await_count == 1


@pytest.mark.parametrize("returncode", [1, 42, -1])
async def test_run_preserves_failure_code_and_readable_diagnostics(monkeypatch, returncode):
    child = SimpleNamespace(returncode=returncode, communicate=AsyncMock(return_value=(b"WSL_ERROR\n", None)))
    monkeypatch.setattr(wsl, "require_windows", lambda _feature: None)
    monkeypatch.setattr(wsl.asyncio, "create_subprocess_exec", AsyncMock(return_value=child))

    assert await wsl.WinWsl()._run(["--version"]) == (returncode, "WSL_ERROR")
