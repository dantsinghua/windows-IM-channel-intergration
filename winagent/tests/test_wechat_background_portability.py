"""Background helpers stay invisible without suppressing user-facing GUI apps.

No test launches a real process or sends a real signal.
"""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qtrade_winagent import win
from qtrade_winagent.win import proc, wechat


NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200


@pytest.fixture(autouse=True)
def forbid_real_signal(monkeypatch):
    monkeypatch.setattr(wechat.os, "kill", Mock(side_effect=AssertionError("No real signals in this test")))


def pretend_windows(monkeypatch, windows=True):
    monkeypatch.setattr(win, "is_windows", lambda: windows)
    local_os = SimpleNamespace(**vars(wechat.os))
    local_os.name = "nt" if windows else "posix"
    monkeypatch.setattr(wechat, "os", local_os)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", NEW_GROUP, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(subprocess, "STARTUPINFO", lambda: SimpleNamespace(dwFlags=0, wShowWindow=1), raising=False)


def fake_cli(monkeypatch):
    call = Mock(return_value=subprocess.CompletedProcess([], 1, b"", b"Access denied"))
    monkeypatch.setattr(subprocess, "run", call)
    return call


def assert_hidden_bounded(call, args):
    assert call.call_args.args == (args,)
    assert call.call_args.kwargs["creationflags"] & NO_WINDOW
    assert call.call_args.kwargs["timeout"] == 5
    assert call.call_args.kwargs["capture_output"] is True


@pytest.mark.parametrize("force", [False, True])
async def test_pid_stop_is_hidden_bounded_and_keeps_exact_target(monkeypatch, force):
    pretend_windows(monkeypatch)
    call = fake_cli(monkeypatch)
    await proc.WinProc().stop(731, force=force)
    assert_hidden_bounded(call, ["taskkill", "/PID", "731"] + (["/F"] if force else []))


async def test_pid_stop_preserves_timeout_failure(monkeypatch):
    pretend_windows(monkeypatch)
    call = fake_cli(monkeypatch)
    error = subprocess.TimeoutExpired("taskkill", 5)
    call.side_effect = error
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        await proc.WinProc().stop(731)
    assert raised.value is error
    assert_hidden_bounded(call, ["taskkill", "/PID", "731"])


@pytest.mark.parametrize("still_running", [False, True])
async def test_narrator_stop_keeps_confirmation_and_uses_hidden_bounded_cli(monkeypatch, still_running):
    pretend_windows(monkeypatch)
    call = fake_cli(monkeypatch)
    monkeypatch.setattr(wechat.asyncio, "sleep", AsyncMock())
    adapter = wechat.WinWeChat()
    adapter._narrator_pid = 732
    monkeypatch.setattr(adapter, "narrator_running", lambda: still_running)
    assert await adapter.narrator_stop() is (not still_running)
    assert adapter._narrator_pid == (732 if still_running else None)
    assert_hidden_bounded(call, ["taskkill", "/IM", "Narrator.exe", "/F"])


@pytest.mark.parametrize("still_running", [False, True])
def test_elevated_narrator_stop_keeps_confirmation_and_uses_hidden_bounded_cli(monkeypatch, still_running):
    pretend_windows(monkeypatch)
    call = fake_cli(monkeypatch)
    monkeypatch.setattr(proc.WinProc, "find", lambda self, name: [SimpleNamespace(pid=732)] if still_running else [])
    monkeypatch.setattr(wechat.time, "sleep", lambda _: None)
    assert wechat.WinWeChat.kill_narrator_elevated() is (not still_running)
    assert_hidden_bounded(call, ["taskkill", "/IM", "Narrator.exe", "/F"])


@pytest.mark.parametrize("windows", [False, True])
async def test_chatlog_window_is_hidden_with_group_and_redacted_output(monkeypatch, tmp_path, windows):
    pretend_windows(monkeypatch, windows)
    stream = asyncio.StreamReader()
    secret = "ab" * 32
    stream.feed_data(("Data Key : " + secret + "\nordinary diagnostic\n").encode())
    stream.feed_eof()
    child = SimpleNamespace(pid=733, stdout=stream)
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    out = tmp_path / "redacted.log"
    result = await wechat.spawn_redacted(["fake-chatlog", "key"], cwd=str(tmp_path), log_path=str(out))
    await asyncio.gather(*list(wechat._PUMPS))
    assert result is child
    assert launch.call_args.args == ("fake-chatlog", "key")
    kwargs = launch.call_args.kwargs
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert not kwargs.get("creationflags", 0) & NO_WINDOW
    if windows:
        assert kwargs["creationflags"] & NEW_GROUP
        info = kwargs["startupinfo"]
        assert info.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert info.wShowWindow == subprocess.SW_HIDE
    else:
        assert kwargs.get("creationflags", 0) == 0
        assert "startupinfo" not in kwargs
    contents = out.read_text(encoding="utf-8")
    assert secret not in contents
    assert "Data Key : <redacted>" in contents
    assert "ordinary diagnostic" in contents


async def test_spawn_cancellation_does_not_swallow_cancellation_or_start_log_pump(monkeypatch, tmp_path):
    pretend_windows(monkeypatch)
    launch = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    before = set(wechat._PUMPS)
    out = tmp_path / "not-created.log"
    with pytest.raises(asyncio.CancelledError):
        await wechat.spawn_redacted(["fake-chatlog"], cwd=str(tmp_path), log_path=str(out))
    assert set(wechat._PUMPS) == before
    assert not out.exists()


async def test_log_pump_cancellation_preserves_redaction_and_releases_file(tmp_path):
    first_line = asyncio.Event()
    secret = "cd" * 16
    calls = 0

    async def readline():
        nonlocal calls
        calls += 1
        if calls == 1:
            return ("Img Key : " + secret + "\n").encode()
        first_line.set()
        await asyncio.Future()

    out = tmp_path / "cancelled.log"
    task = asyncio.create_task(wechat._pump_redacted(SimpleNamespace(readline=readline), str(out)))
    try:
        await asyncio.wait_for(first_line.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert out.read_text(encoding="utf-8") == "Img Key : <redacted>\n"
    out.rename(tmp_path / "closed.log")


async def test_generic_gui_launch_has_no_hidden_window_flags(monkeypatch):
    launch = AsyncMock(return_value=SimpleNamespace(pid=734))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    assert await proc.WinProc().start(["fake-gui.exe", "argument"], cwd="gui-dir") == 734
    assert launch.call_args.args == ("fake-gui.exe", "argument")
    assert launch.call_args.kwargs == {"cwd": "gui-dir", "stdout": asyncio.subprocess.DEVNULL,
                                      "stderr": asyncio.subprocess.DEVNULL}


async def test_wechat_gui_launch_remains_visible(monkeypatch):
    monkeypatch.setattr(wechat, "require_windows", lambda _: None)
    launch = AsyncMock(return_value=SimpleNamespace(pid=735))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    adapter = wechat.WinWeChat()
    adapter._exe = "fake-Weixin.exe"
    assert await adapter.launch() == 735
    launch.assert_awaited_once_with("fake-Weixin.exe")


def owned_process(monkeypatch, *, exits_gracefully):
    """Only a retained subprocess handle may be stopped; name-based discovery is forbidden."""
    calls = []
    child = SimpleNamespace(pid=736, returncode=None)

    async def wait():
        if not exits_gracefully and child.returncode is None:
            raise asyncio.TimeoutError
        child.returncode = 0
        return 0

    def kill():
        calls.append(("kill_handle", child.pid))
        child.returncode = -1

    child.wait, child.kill = wait, kill
    adapter = wechat.WinWeChat()
    adapter._chatlog_process = child
    adapter._chatlog_pid = child.pid
    adapter._chatlog_created = 100.5
    monkeypatch.setattr(adapter, "_process_created", lambda pid: 100.5)
    return adapter, child, calls


@pytest.mark.parametrize("exits_gracefully", [False, True])
async def test_chatlog_stop_signals_before_forced_fallback(monkeypatch, exits_gracefully):
    adapter, child, calls = owned_process(monkeypatch, exits_gracefully=exits_gracefully)
    async def interrupt(pid):
        calls.append(("signal", pid))
        return True
    find = Mock(side_effect=AssertionError("No process-name stop"))
    monkeypatch.setattr(proc, "WinProc", lambda: SimpleNamespace(find=find, interrupt=interrupt))
    await adapter.chatlog_stop()
    assert calls == ([("signal", 736)] if exits_gracefully else [("signal", 736), ("kill_handle", 736)])
    assert child.returncode is not None
    find.assert_not_called()


@pytest.mark.parametrize("error", [OSError("unavailable"), None])
async def test_chatlog_signal_failure_still_rechecks_before_existing_fallback(monkeypatch, error):
    adapter, child, calls = owned_process(monkeypatch, exits_gracefully=False)
    interrupt = AsyncMock(side_effect=error, return_value=False)
    find = Mock(side_effect=AssertionError("No process-name stop"))
    monkeypatch.setattr(proc, "WinProc", lambda: SimpleNamespace(find=find, interrupt=interrupt))
    await adapter.chatlog_stop()
    interrupt.assert_awaited_once_with(736)
    find.assert_not_called()
    assert calls == [("kill_handle", 736)]
    assert child.returncode is not None


async def test_chatlog_signal_cancellation_does_not_force_kill(monkeypatch):
    adapter, child, calls = owned_process(monkeypatch, exits_gracefully=False)
    interrupt = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(proc, "WinProc", lambda: SimpleNamespace(interrupt=interrupt))
    with pytest.raises(asyncio.CancelledError):
        await adapter.chatlog_stop()
    assert calls == []
    assert child.returncode is None
