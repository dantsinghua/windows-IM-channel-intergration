"""Owned WSL keepalive lifecycle through fake processes and controlled events."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qtrade_winagent import main_user, user, win
from qtrade_winagent.config import WinAgentConfig, WslConfig
from qtrade_winagent.fakes import FakePipeBackend, FakeWsl
from qtrade_winagent.user import UserAgent
from qtrade_winagent.win import wsl
from qtrade_winagent.wslctl import WslCtl


class HolderProcess:
    def __init__(self, *, ready=b"QTRADE_WSL_READY\n", block_ready=False, exit_on_close=True):
        self.returncode = None
        self.ready_entered = asyncio.Event()
        self.exited = asyncio.Event()
        self.block_ready = block_ready
        self.ready = ready
        self.read_calls = 0
        self.wait_entered = asyncio.Event()
        self.exit_on_close = exit_on_close
        self.stdout = SimpleNamespace(readline=self.readline)
        self.stderr = SimpleNamespace(read=AsyncMock(return_value=b""))
        self.stdin = SimpleNamespace(close=Mock(side_effect=self.close_stdin), wait_closed=AsyncMock())
        self.kill = Mock(side_effect=self.force_exit)
        self.wait_calls = 0
        self.communicate = AsyncMock(return_value=(ready, None))

    async def readline(self):
        self.ready_entered.set()
        if self.block_ready:
            await asyncio.Future()
        self.read_calls += 1
        return self.ready if self.read_calls == 1 else b""

    def close_stdin(self):
        if self.exit_on_close:
            if self.returncode is None:
                self.returncode = 0
            self.exited.set()

    def force_exit(self):
        self.returncode = -9
        self.exited.set()

    async def wait(self):
        self.wait_calls += 1
        self.wait_entered.set()
        await self.exited.wait()
        return self.returncode


def holder_backend(monkeypatch, child):
    monkeypatch.setattr(win, "is_windows", lambda: True)
    monkeypatch.setattr(wsl, "require_windows", lambda _: None)
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(wsl.asyncio, "create_subprocess_exec", launch)
    return wsl.WinWsl(distro="qtrade-test-owned"), launch


async def close_if_supported(backend):
    close = getattr(backend, "close", None)
    if close:
        await close()


async def test_concurrent_start_owns_one_hidden_persistent_child(monkeypatch):
    child = HolderProcess()
    backend, launch = holder_backend(monkeypatch, child)
    try:
        await asyncio.gather(*(backend.start("qtrade-test-owned") for _ in range(5)))
        launch.assert_awaited_once()
        argv = launch.call_args.args
        assert argv[:3] == ("wsl.exe", "-d", "qtrade-test-owned")
        assert "/opt/qtrade/agent/venv/bin/python" in argv
        assert "/bin/true" not in argv
        assert launch.call_args.kwargs["stdin"] == asyncio.subprocess.PIPE
        assert launch.call_args.kwargs.get("creationflags", 0) & 0x08000000
        child.stdin.close.assert_not_called()
        assert child.returncode is None
        await backend.close()
        child.stdin.close.assert_called_once()
        assert child.wait_calls >= 1
        child.kill.assert_not_called()
        await backend.close()
        child.stdin.close.assert_called_once()
    finally:
        await close_if_supported(backend)


@pytest.mark.parametrize("marker", [b"", b"NOT_READY\n"])
async def test_invalid_ready_marker_rejects_start_and_releases_owned_child(monkeypatch, marker):
    child = HolderProcess(ready=marker)
    backend, _ = holder_backend(monkeypatch, child)
    try:
        with pytest.raises(RuntimeError):
            await backend.start("qtrade-test-owned")
        child.stdin.close.assert_called_once()
        assert child.wait_calls >= 1
    finally:
        await close_if_supported(backend)


async def test_ready_timeout_is_bounded_and_releases_owned_child(monkeypatch):
    child = HolderProcess(block_ready=True)
    backend, _ = holder_backend(monkeypatch, child)
    assert wsl.HOLDER_START_TIMEOUT_S == 30
    monkeypatch.setattr(wsl, "HOLDER_START_TIMEOUT_S", 0.01)
    try:
        with pytest.raises((RuntimeError, asyncio.TimeoutError)):
            await backend.start("qtrade-test-owned")
        child.stdin.close.assert_called_once()
        assert child.wait_calls >= 1
    finally:
        await close_if_supported(backend)


async def test_cancelled_start_reaps_child_before_propagating_cancellation(monkeypatch):
    child = HolderProcess(block_ready=True)
    backend, _ = holder_backend(monkeypatch, child)
    task = asyncio.create_task(backend.start("qtrade-test-owned"))
    try:
        await asyncio.wait_for(child.ready_entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        child.stdin.close.assert_called_once()
        assert child.wait_calls >= 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await close_if_supported(backend)


async def test_close_kills_only_owned_child_after_graceful_exit_deadline(monkeypatch):
    child = HolderProcess(exit_on_close=False)
    backend, launch = holder_backend(monkeypatch, child)
    original = asyncio.wait_for
    deadlines = []

    async def deadline(awaitable, timeout):
        deadlines.append(timeout)
        if timeout == 5 and deadlines.count(5) == 1:
            awaitable.close()
            raise asyncio.TimeoutError()
        return await original(awaitable, timeout=timeout)

    monkeypatch.setattr(wsl.asyncio, "wait_for", deadline)
    await backend.start("qtrade-test-owned")
    await backend.close()
    assert 5 in deadlines
    child.stdin.close.assert_called_once()
    child.kill.assert_called_once()
    assert child.wait_calls >= 1
    launch.assert_awaited_once()


async def test_cancelled_close_finishes_owned_child_cleanup_before_propagating(monkeypatch):
    child = HolderProcess(exit_on_close=False)
    backend, _ = holder_backend(monkeypatch, child)
    await backend.start("qtrade-test-owned")
    task = asyncio.create_task(backend.close())
    try:
        await asyncio.wait_for(child.wait_entered.wait(), timeout=1)
        task.cancel()
        child.force_exit()
        with pytest.raises(asyncio.CancelledError):
            await task
        await backend.close()
        child.stdin.close.assert_called_once()
        assert child.wait_calls >= 1
    finally:
        child.force_exit()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["terminate", "shutdown"])
async def test_failed_explicit_stop_does_not_report_success(monkeypatch, operation):
    backend = wsl.WinWsl(distro="qtrade-test-owned")
    monkeypatch.setattr(backend, "_run", AsyncMock(return_value=(42, "WSL_ERROR")))
    with pytest.raises(RuntimeError, match="WSL_ERROR"):
        if operation == "terminate":
            await backend.terminate("qtrade-test-owned")
        else:
            await backend.shutdown()


async def test_exited_holder_is_replaced_once_on_next_start(monkeypatch):
    first, second = HolderProcess(), HolderProcess()
    backend, launch = holder_backend(monkeypatch, first)
    launch.side_effect = [first, second]
    try:
        await backend.start("qtrade-test-owned")
        first.force_exit()
        await asyncio.gather(*(backend.start("qtrade-test-owned") for _ in range(3)))
        assert launch.await_count == 2
        assert second.returncode is None
    finally:
        await close_if_supported(backend)


def control():
    backend = FakeWsl()
    backend.close = AsyncMock()
    return WslCtl(backend, WslConfig()), backend


async def test_explicit_stop_disables_automatic_ensure_until_explicit_start():
    ctl, backend = control()
    backend.start = AsyncMock()
    backend.terminate = AsyncMock()
    await ctl.stop()
    assert ctl.desired_running is False
    await ctl.ensure_running()
    backend.start.assert_not_awaited()
    await ctl.start()
    assert ctl.desired_running is True
    backend.start.assert_awaited_once_with("qtrade")
    backend.terminate.assert_awaited_once_with("qtrade")


async def test_stop_during_pending_start_wins_and_cannot_be_undone_by_retry():
    ctl, backend = control()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def start(_):
        calls.append("start")
        entered.set()
        await release.wait()

    async def terminate(_):
        calls.append("terminate")

    backend.start, backend.terminate = start, terminate
    first = asyncio.create_task(ctl.ensure_running())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        stop = asyncio.create_task(ctl.stop())
        await asyncio.sleep(0)
        assert ctl.desired_running is False
        release.set()
        await asyncio.gather(first, stop)
        await ctl.ensure_running()
        assert calls == ["start", "terminate"]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_cancelled_stop_still_releases_a_cold_start_that_becomes_ready_later():
    ctl, backend = control()
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(_):
        entered.set()
        await release.wait()

    backend.start = AsyncMock(side_effect=start)
    backend.terminate = AsyncMock()
    startup = asyncio.create_task(ctl.ensure_running())
    stopper = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        stopper = asyncio.create_task(ctl.stop())
        await asyncio.sleep(0)
        assert ctl.desired_running is False
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        release.set()
        await startup
        backend.close.assert_awaited_once()
        await ctl.ensure_running()
        assert backend.start.await_count == 1
        backend.terminate.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        if stopper is not None and not stopper.done():
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)


class RetryClock:
    def __init__(self):
        self.waiters = []
        self.entered = asyncio.Queue()
        self.actual_sleep = asyncio.sleep

    async def sleep(self, seconds):
        if seconds == 0:
            return await self.actual_sleep(0)
        gate = asyncio.Event()
        self.waiters.append(gate)
        self.entered.put_nowait(seconds)
        await gate.wait()


def session_agent(monkeypatch):
    backend = FakeWsl()
    backend.close = AsyncMock()
    ua = UserAgent(WinAgentConfig(), pipe=FakePipeBackend(), wsl=backend, backup_dir="fixture")
    monkeypatch.setattr(ua.link, "connect", AsyncMock(return_value={"ipc_version": 1}))
    monkeypatch.setattr(ua.link, "stop", AsyncMock())
    return ua, backend


async def test_slow_cold_start_does_not_block_pipe_handshake_and_stop_closes_it(monkeypatch):
    ua, backend = session_agent(monkeypatch)
    started = asyncio.Event()

    async def cold_start(_):
        started.set()
        await asyncio.Future()

    backend.start = cold_start
    try:
        assert await asyncio.wait_for(ua.start(), timeout=0.1) == {"ipc_version": 1}
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        await ua.stop()
    backend.close.assert_awaited_once()


async def test_failed_start_retries_after_sixty_seconds_without_blocking_pipe(monkeypatch):
    ua, backend = session_agent(monkeypatch)
    backend.start = AsyncMock(side_effect=[RuntimeError("boot failed"), None])
    clock = RetryClock()
    monkeypatch.setattr(user.asyncio, "sleep", clock.sleep)
    try:
        await ua.start()
        assert await asyncio.wait_for(clock.entered.get(), timeout=1) == 60
        assert backend.start.await_count == 1
        clock.waiters[0].set()
        assert await asyncio.wait_for(clock.entered.get(), timeout=1) == 60
        assert backend.start.await_count == 2
    finally:
        await ua.stop()
    backend.close.assert_awaited_once()


async def test_reconnect_keeps_stop_intent_and_does_not_duplicate_retry_loop(monkeypatch):
    ua, backend = session_agent(monkeypatch)
    backend.start = AsyncMock()
    backend.terminate = AsyncMock()
    clock = RetryClock()
    monkeypatch.setattr(user.asyncio, "sleep", clock.sleep)
    try:
        await ua.start()
        assert await asyncio.wait_for(clock.entered.get(), timeout=1) == 60
        await ua.wslctl.stop()
        await ua.start()
        assert ua.wslctl.desired_running is False
        assert len(clock.waiters) == 1
        clock.waiters[0].set()
        assert await asyncio.wait_for(clock.entered.get(), timeout=1) == 60
        assert backend.start.await_count == 1
        await ua.wslctl.start()
        assert backend.start.await_count == 2
    finally:
        await ua.stop()


async def test_run_forever_cancellation_always_closes_user_agent():
    ua = SimpleNamespace(start=AsyncMock(side_effect=asyncio.CancelledError()), stop=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await main_user.run_forever(ua)
    ua.stop.assert_awaited_once()
