"""Background CLI calls are hidden on Windows; all process objects are fake."""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qtrade_winagent import win
from qtrade_winagent.win import wsl


NO_WINDOW = 0x08000000


def platform(monkeypatch, windows):
    monkeypatch.setattr(win, "is_windows", lambda: windows)
    # WinWsl is a Windows adapter; bypass only its platform guard to inspect
    # process arguments. The process factory itself is always a fake below.
    monkeypatch.setattr(wsl, "require_windows", lambda _: None)


def assert_visibility(kwargs, windows):
    if windows:
        assert kwargs.get("creationflags", 0) & NO_WINDOW
    else:
        assert kwargs.get("creationflags", 0) == 0


@pytest.mark.parametrize("windows", [True, False])
def test_shared_cli_hides_only_windows_console_and_keeps_output(monkeypatch, windows):
    platform(monkeypatch, windows)
    args = ["fake-cli", "argument with spaces"]
    child = subprocess.CompletedProcess(args, 9, b"\xff\xfe" + "result".encode("utf-16-le"), b"diagnostic")
    launch = Mock(return_value=child)
    monkeypatch.setattr(win.subprocess, "run", launch)
    result = win.run_text(args, timeout=4.5, check=False)
    assert_visibility(launch.call_args.kwargs, windows)
    assert launch.call_args.args == (args,)
    assert launch.call_args.kwargs["capture_output"] is True
    assert launch.call_args.kwargs["timeout"] == 4.5
    assert launch.call_args.kwargs["check"] is False
    assert (result.args, result.returncode, result.stdout, result.stderr) == (args, 9, "result", "diagnostic")


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("fake-cli", 3), subprocess.CalledProcessError(7, "fake-cli")])
def test_shared_cli_preserves_timeout_and_checked_failure(monkeypatch, error):
    platform(monkeypatch, True)
    launch = Mock(side_effect=error)
    monkeypatch.setattr(win.subprocess, "run", launch)
    with pytest.raises(type(error)) as raised:
        win.run_text(["fake-cli"], timeout=3, check=True)
    assert raised.value is error
    assert_visibility(launch.call_args.kwargs, True)
    assert launch.call_args.kwargs["timeout"] == 3
    assert launch.call_args.kwargs["check"] is True


@pytest.mark.parametrize("windows", [True, False])
async def test_wsl_cli_hides_only_windows_console_and_preserves_stdin_output(monkeypatch, windows):
    platform(monkeypatch, windows)
    child = SimpleNamespace(returncode=11, communicate=AsyncMock(return_value=(b"WSL_ERROR\n", None)))
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(wsl.asyncio, "create_subprocess_exec", launch)
    args = ["-d", "test-distro", "--exec", "fake-command"]
    assert await wsl.WinWsl()._run(args, timeout_s=7, input_data=b"public data") == (11, "WSL_ERROR")
    assert_visibility(launch.call_args.kwargs, windows)
    assert launch.call_args.args == ("wsl.exe", *args)
    assert launch.call_args.kwargs["stdin"] == asyncio.subprocess.PIPE
    assert launch.call_args.kwargs["stdout"] == asyncio.subprocess.PIPE
    assert launch.call_args.kwargs["stderr"] == asyncio.subprocess.STDOUT
    child.communicate.assert_awaited_once_with(b"public data")


async def test_wsl_timeout_reaps_hidden_child_and_preserves_timeout(monkeypatch):
    platform(monkeypatch, True)
    error = asyncio.TimeoutError()
    child = SimpleNamespace(returncode=None, kill=Mock(),
                            communicate=AsyncMock(side_effect=[error, (b"", None)]))
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(wsl.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(asyncio.TimeoutError) as raised:
        await wsl.WinWsl()._run(["--version"], timeout_s=5)
    assert raised.value is error
    assert_visibility(launch.call_args.kwargs, True)
    child.kill.assert_called_once()
    assert child.communicate.await_count == 2


async def test_wsl_cancellation_reaps_hidden_child_and_propagates(monkeypatch):
    platform(monkeypatch, True)
    started = asyncio.Event()
    calls = []

    async def communicate(data=None):
        calls.append(data)
        if len(calls) == 1:
            started.set()
            await asyncio.Future()
        return b"", None

    child = SimpleNamespace(returncode=None, kill=Mock(), communicate=communicate)
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(wsl.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(wsl.WinWsl()._run(["--version"], timeout_s=5))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert_visibility(launch.call_args.kwargs, True)
    child.kill.assert_called_once()
    assert calls == [None, None]
