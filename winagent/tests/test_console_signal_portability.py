"""Console helper tests replace every WinAPI and subprocess call."""
from __future__ import annotations

import ctypes
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qtrade_winagent import win
from qtrade_winagent.win import console_signal, proc


def fake_windows(monkeypatch, *, attached=True, generated=True):
    monkeypatch.setattr(console_signal, "sys", SimpleNamespace(platform="win32", argv=["helper"]))
    kernel = Mock()
    kernel.AttachConsole.return_value = attached
    kernel.GenerateConsoleCtrlEvent.return_value = generated
    factory = Mock(return_value=kernel)
    monkeypatch.setattr(ctypes, "WinDLL", factory, raising=False)
    return kernel, factory


@pytest.mark.parametrize("argv", [[], ["7", "8"], ["0"], ["-1"], ["+1"], ["1.2"], [" 7"], ["7 "],
                                  ["0x7"], ["nan"], ["\u0667"], ["4294967295"], ["4294967296"], ["9" * 5000]])
def test_helper_rejects_invalid_pid_without_touching_windows(monkeypatch, argv):
    _, factory = fake_windows(monkeypatch)
    assert console_signal.main(argv) == 2
    factory.assert_not_called()


def test_helper_rejects_non_windows_without_loading_winapi(monkeypatch):
    _, factory = fake_windows(monkeypatch)
    monkeypatch.setattr(console_signal.sys, "platform", "linux")
    assert console_signal.main(["737"]) == 2
    factory.assert_not_called()


@pytest.mark.parametrize("pid", ["1", "737", "4294967294"])
def test_helper_attaches_only_target_sends_group_break_then_detaches(monkeypatch, pid):
    kernel, factory = fake_windows(monkeypatch)
    assert console_signal.main([pid]) == 0
    factory.assert_called_once_with("kernel32", use_last_error=True)
    assert [(c[0], c[1]) for c in kernel.mock_calls] == [("AttachConsole", (int(pid),)),
                                                       ("GenerateConsoleCtrlEvent", (1, int(pid))),
                                                       ("FreeConsole", ())]


def test_helper_does_not_signal_or_detach_when_attach_fails(monkeypatch):
    kernel, _ = fake_windows(monkeypatch, attached=False)
    assert console_signal.main(["737"]) == 1
    kernel.GenerateConsoleCtrlEvent.assert_not_called()
    kernel.FreeConsole.assert_not_called()


def test_helper_detaches_even_when_event_is_rejected(monkeypatch):
    kernel, _ = fake_windows(monkeypatch, generated=False)
    assert console_signal.main(["737"]) == 1
    kernel.FreeConsole.assert_called_once_with()


def test_helper_detaches_even_when_event_raises(monkeypatch):
    kernel, _ = fake_windows(monkeypatch)
    kernel.GenerateConsoleCtrlEvent.side_effect = OSError("event failure")
    with pytest.raises(OSError):
        console_signal.main(["737"])
    kernel.FreeConsole.assert_called_once_with()


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("returncode", [0, 1])
async def test_interrupt_uses_hidden_bounded_child_and_reports_result(monkeypatch, frozen, returncode):
    monkeypatch.setattr(win, "is_windows", lambda: True)
    monkeypatch.setattr(proc, "sys", SimpleNamespace(executable="owned-python.exe", frozen=frozen))
    call = Mock(return_value=subprocess.CompletedProcess([], returncode, b"", b""))
    monkeypatch.setattr(subprocess, "run", call)
    assert await proc.WinProc().interrupt(737) is (returncode == 0)
    expected = (["owned-python.exe", "--console-break", "737"] if frozen else
                ["owned-python.exe", "-m", "qtrade_winagent.win.console_signal", "737"])
    assert call.call_args.args == (expected,)
    assert call.call_args.kwargs["creationflags"] & 0x08000000
    assert call.call_args.kwargs["timeout"] == 5
    assert call.call_args.kwargs["capture_output"] is True


@pytest.mark.parametrize("pid", [0, -1, 0xffffffff, True, 1.2, "737"])
async def test_interrupt_rejects_invalid_pid_before_spawning(monkeypatch, pid):
    monkeypatch.setattr(win, "is_windows", lambda: True)
    call = Mock(side_effect=AssertionError("Must not launch"))
    monkeypatch.setattr(subprocess, "run", call)
    with pytest.raises(ValueError):
        await proc.WinProc().interrupt(pid)
    call.assert_not_called()


async def test_interrupt_timeout_reports_failure_without_leaking_exception(monkeypatch):
    monkeypatch.setattr(win, "is_windows", lambda: True)
    call = Mock(side_effect=subprocess.TimeoutExpired("helper", 5))
    monkeypatch.setattr(subprocess, "run", call)
    assert await proc.WinProc().interrupt(737) is False
    assert call.call_args.kwargs["timeout"] == 5
    assert call.call_args.kwargs["creationflags"] & 0x08000000


@pytest.mark.parametrize("args", [["--console-break", "737"], ["--console-break"], ["--console-break", "737", "extra"]])
def test_frozen_entry_console_branch_never_starts_user_agent(monkeypatch, args):
    helper = Mock(return_value=2 if len(args) != 2 else 0)
    agent = Mock(side_effect=AssertionError("Must not start user agent"))
    monkeypatch.setitem(sys.modules, "qtrade_winagent.win.console_signal", SimpleNamespace(main=helper))
    monkeypatch.setitem(sys.modules, "qtrade_winagent.main_user", SimpleNamespace(main=agent))
    monkeypatch.setattr(sys, "argv", ["qtrade-winagent-user.exe", *args])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "build/entry_user.py"), run_name="__main__")
    assert raised.value.code == helper.return_value
    helper.assert_called_once_with(args[1:])
    agent.assert_not_called()
