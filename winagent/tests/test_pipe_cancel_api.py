"""Only real pywin32 cancellation APIs are exposed; no native pipe is opened."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qtrade_winagent.win import pipes


class WinError(Exception):
    def __init__(self, code):
        self.winerror = code
        super().__init__(code)


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(cancelled=False, closed=False, other_io_cancelled=False,
                            started=asyncio.Event(), calls=[])

    def close_event():
        state.calls.append("event_close")

    def cancel(handle):
        state.calls.append("cancel")
        state.cancelled = True
        state.other_io_cancelled = True

    def close(handle):
        state.calls.append("handle_close")
        state.closed = True

    event = SimpleNamespace(Close=Mock(side_effect=close_event))
    file = SimpleNamespace(CancelIo=Mock(side_effect=cancel), GetOverlappedResult=Mock(),
                           CloseHandle=Mock(side_effect=close))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(OVERLAPPED=SimpleNamespace))
    monkeypatch.setitem(sys.modules, "win32event", SimpleNamespace(CreateEvent=lambda *_: event))
    monkeypatch.setitem(sys.modules, "win32file", file)
    monkeypatch.setattr(pipes, "require_windows", lambda _: None)
    return state, event, file


@pytest.mark.parametrize("operation_kind", ["read", "write"])
async def test_successful_io_never_cancels_other_pending_io_on_same_handle(native, operation_kind):
    state, event, file = native
    file.GetOverlappedResult.return_value = 6
    buffer = bytearray(b"value\n")
    started = (0, buffer if operation_kind == "read" else 6)
    assert await pipes._overlapped("shared-handle", lambda op: started) == (6, started)
    file.CancelIo.assert_not_called()
    assert state.other_io_cancelled is False
    assert [call.args[-1] for call in file.GetOverlappedResult.call_args_list] == [False]
    event.Close.assert_called_once()


async def test_pending_cancel_uses_available_cancelio_before_wait_and_event_close(native):
    state, event, file = native

    def result(handle, operation, wait):
        state.calls.append("wait" if wait else "poll")
        if wait:
            assert state.cancelled, "blocking result wait before cancellation could hang SCM forever"
            raise WinError(995)
        raise WinError(996)

    def begin(operation):
        state.started.set()
        raise WinError(997)

    file.GetOverlappedResult.side_effect = result
    task = asyncio.create_task(pipes._overlapped("pending-handle", begin))
    await state.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    file.CancelIo.assert_called_once_with("pending-handle")
    assert state.calls.index("cancel") < state.calls.index("wait") < state.calls.index("event_close")
    assert not hasattr(file, "CancelIoEx")
    event.Close.assert_called_once()


@pytest.mark.parametrize("code", [6, 109, 232, 995])
async def test_terminal_completion_error_never_enters_a_blocking_cleanup_wait(native, code):
    _state, event, file = native
    file.GetOverlappedResult.side_effect = WinError(code)
    with pytest.raises(WinError) as error:
        await pipes._overlapped("terminal-handle", lambda operation: (997, bytearray(8)))
    assert error.value.winerror == code
    assert [call.args[-1] for call in file.GetOverlappedResult.call_args_list] == [False]
    file.CancelIo.assert_not_called()
    event.Close.assert_called_once()


async def test_cancel_of_already_closed_handle_never_waits_forever(native):
    state, event, file = native
    file.GetOverlappedResult.side_effect = WinError(996)
    file.CancelIo.side_effect = WinError(6)

    def begin(operation):
        state.started.set()
        raise WinError(997)

    task = asyncio.create_task(pipes._overlapped("closed-handle", begin))
    await state.started.wait()
    task.cancel()
    with pytest.raises(WinError) as failure:
        await asyncio.wait_for(task, timeout=0.5)
    assert failure.value.winerror == 6
    file.CancelIo.assert_called_once_with("closed-handle")
    assert all(call.args[-1] is False for call in file.GetOverlappedResult.call_args_list)
    event.Close.assert_called_once()


async def test_unexpected_completion_error_after_cancel_is_not_silently_ignored(native):
    state, event, file = native

    def result(handle, operation, wait):
        raise WinError(5 if wait else 996)

    def begin(operation):
        state.started.set()
        raise WinError(997)

    file.GetOverlappedResult.side_effect = result
    task = asyncio.create_task(pipes._overlapped("pending-handle", begin))
    await state.started.wait()
    task.cancel()
    with pytest.raises(WinError) as failure:
        await asyncio.wait_for(task, timeout=0.5)
    assert failure.value.winerror == 5
    file.CancelIo.assert_called_once_with("pending-handle")
    event.Close.assert_called_once()


async def test_cancel_pending_accept_closes_the_owned_handle(native, monkeypatch):
    state, event, file = native

    def connect(handle, operation):
        state.started.set()
        raise WinError(997)

    def result(handle, operation, wait):
        if wait:
            assert state.cancelled
            raise WinError(995)
        raise WinError(996)

    file.GetOverlappedResult.side_effect = result
    pipe = SimpleNamespace(PIPE_ACCESS_DUPLEX=3, PIPE_TYPE_MESSAGE=4, PIPE_READMODE_MESSAGE=2,
                           PIPE_WAIT=0, PIPE_UNLIMITED_INSTANCES=255,
                           CreateNamedPipe=Mock(return_value="owned-handle"), ConnectNamedPipe=connect)
    monkeypatch.setitem(sys.modules, "win32pipe", pipe)
    backend = pipes.WinPipeBackend()
    monkeypatch.setattr(backend, "_sa", lambda sid: object())
    task = asyncio.create_task(backend.serve("fixture-only-pipe"))
    await state.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    file.CancelIo.assert_called_once_with("owned-handle")
    file.CloseHandle.assert_called_once_with("owned-handle")
    event.Close.assert_called_once()
    assert state.calls.index("cancel") < state.calls.index("event_close") < state.calls.index("handle_close")


async def test_connection_close_unblocks_pending_read_without_blocking_result_wait(native):
    state, event, file = native
    file.AllocateReadBuffer = Mock(return_value=bytearray(64))

    def read(handle, buffer, operation):
        state.started.set()
        return 997, buffer

    def result(handle, operation, wait):
        if state.closed:
            raise WinError(6)
        raise WinError(996)

    file.ReadFile = read
    file.GetOverlappedResult.side_effect = result
    conn = pipes.WinPipeConn("read-handle", peer_sid=None, overlapped=True)
    task = asyncio.create_task(conn.recv_json())
    await state.started.wait()
    await conn.close()
    assert await asyncio.wait_for(task, timeout=0.5) is None
    assert all(call.args[-1] is False for call in file.GetOverlappedResult.call_args_list)
    file.CancelIo.assert_not_called()
    event.Close.assert_called_once()
