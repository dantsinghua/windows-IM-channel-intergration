"""Native pipe completion and cancellation with injected Win32 APIs only."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qtrade_winagent.win.pipes import _overlapped


class WinError(Exception):
    def __init__(self, code):
        self.winerror = code
        super().__init__(code)


def api(monkeypatch, get_result):
    event = SimpleNamespace(Close=Mock())
    file = SimpleNamespace(GetOverlappedResult=Mock(side_effect=get_result), CancelIo=Mock())
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(OVERLAPPED=SimpleNamespace))
    monkeypatch.setitem(sys.modules, "win32event", SimpleNamespace(CreateEvent=lambda *_: event))
    monkeypatch.setitem(sys.modules, "win32file", file)
    return event, file


async def test_pending_accept_completes_before_event_is_closed(monkeypatch):
    event, file = api(monkeypatch, [WinError(996), 0, 0])
    def begin(operation):
        assert operation.hEvent is event
        raise WinError(997)
    assert await _overlapped("fixture-handle", begin) == (0, None)
    event.Close.assert_called_once()
    assert file.GetOverlappedResult.call_args_list[0].args[-1] is False


async def test_cancel_pending_accept_cancels_io_then_closes_event(monkeypatch):
    event, file = api(monkeypatch, lambda _h, _op, wait: (_ for _ in ()).throw(WinError(995 if wait else 996)))
    entered = asyncio.Event()
    def begin(_operation):
        entered.set()
        raise WinError(997)
    task = asyncio.create_task(_overlapped("fixture-handle", begin))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    file.CancelIo.assert_called_once_with("fixture-handle")
    assert file.GetOverlappedResult.call_args_list[-1].args[-1] is True
    event.Close.assert_called_once()


async def test_client_connected_before_accept_needs_no_pending_wait(monkeypatch):
    event, file = api(monkeypatch, AssertionError("connected accept must not wait"))
    assert await _overlapped("fixture-handle", lambda _operation: 535) == (0, None)
    file.GetOverlappedResult.assert_not_called()
    file.CancelIo.assert_not_called()
    event.Close.assert_called_once()
