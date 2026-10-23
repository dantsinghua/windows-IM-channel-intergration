"""Real UserAgent/Link with in-memory pipe peers; never contacts Windows or WSL."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent import main_user
from qtrade_winagent.backends import PipeFrame
from qtrade_winagent.config import WinAgentConfig
from qtrade_winagent.fakes import FakePipeBackend, FakeWsl
from qtrade_winagent.pipe import HELLO, PING
from qtrade_winagent.user import UserAgent


class WinError(OSError):
    def __init__(self, code):
        self.winerror = code
        super().__init__(code, "WriteFile", "injected pipe error")


def pywin32_error(code):
    """Use the vendor exception on Windows and its observed shape elsewhere."""
    try:
        import pywintypes
    except ImportError:
        error_type = type("error", (Exception,), {"__module__": "pywintypes"})
        error = error_type(code, "WriteFile", "injected pipe error")
        error.winerror = code
        return error
    return pywintypes.error(code, "WriteFile", "injected pipe error")


def build():
    backend, wsl = FakePipeBackend(), FakeWsl()
    wsl.close = AsyncMock(wraps=wsl.close)
    ua = UserAgent(WinAgentConfig(), pipe=backend, wsl=wsl, backup_dir="fixture")
    return ua, backend, wsl


async def handshake(ua, backend):
    async def serve():
        peer = await backend.serve(ua.cfg.api.pipe_user)
        hello = await peer.recv()
        assert hello.method == HELLO
        await peer.send(PipeFrame(id=hello.id, ok=True, result={"ipc_version": 1}))
        return peer

    server = asyncio.create_task(serve())
    await asyncio.wait_for(ua.start(), timeout=1)
    return await server


def fail_heartbeat(monkeypatch, conn, error, *, before_error=None):
    send = conn.send

    async def failing(frame):
        if frame.method == PING:
            if before_error:
                await before_error()
            raise error
        return await send(frame)

    monkeypatch.setattr(conn, "send", failing)


@pytest.mark.parametrize("error", [ConnectionError("lost"), BrokenPipeError("broken"), ConnectionResetError("reset"),
                                   *[WinError(code) for code in (6, 109, 232, 233, 995)],
                                   *[pywin32_error(code) for code in (6, 109, 232, 233, 995)]])
async def test_heartbeat_transport_loss_closes_link_without_stopping_wsl(monkeypatch, error):
    ua, backend, wsl = build()
    await handshake(ua, backend)
    conn, ctl = ua.link.conn, ua.wslctl
    fail_heartbeat(monkeypatch, conn, error)
    try:
        assert await asyncio.wait_for(ua.run(), timeout=1) is None
        assert conn.closed and ua.link.conn is None
        assert ua.wslctl is ctl and ctl.desired_running is True
        wsl.close.assert_not_awaited()
    finally:
        await ua.stop()


@pytest.mark.parametrize("error", [ValueError("bad business state"), RuntimeError("business failed"),
                                   ValueError(109), RuntimeError(109), WinError(5), WinError(2),
                                   pywin32_error(5), pywin32_error(2), asyncio.CancelledError()])
async def test_non_transport_error_or_cancellation_propagates_after_link_cleanup(monkeypatch, error):
    ua, backend, wsl = build()
    await handshake(ua, backend)
    conn = ua.link.conn
    fail_heartbeat(monkeypatch, conn, error)
    try:
        with pytest.raises(type(error)) as raised:
            await ua.run()
        assert raised.value is error
        assert conn.closed and ua.link.conn is None
        wsl.close.assert_not_awaited()
    finally:
        await ua.stop()


@pytest.mark.parametrize("error", [WinError(109), pywin32_error(6), BrokenPipeError("closed")])
async def test_link_inbound_probe_preserves_identified_disconnect_return(monkeypatch, error):
    ua, backend, _ = build()
    await handshake(ua, backend)

    def failed_probe():
        raise error

    monkeypatch.setattr(ua.link.conn, "inbound_ready", failed_probe)
    try:
        assert await ua.link.run() is None
    finally:
        await ua.stop()


@pytest.mark.parametrize("error", [ValueError(109), RuntimeError("probe bug"), WinError(5),
                                   pywin32_error(5), asyncio.CancelledError()])
async def test_link_inbound_probe_does_not_mislabel_unknown_error_as_disconnect(monkeypatch, error):
    ua, backend, _ = build()
    await handshake(ua, backend)

    def failed_probe():
        raise error

    monkeypatch.setattr(ua.link.conn, "inbound_ready", failed_probe)
    try:
        with pytest.raises(type(error)) as raised:
            await ua.link.run()
        assert raised.value is error
    finally:
        await ua.stop()


@pytest.mark.parametrize("direct_stop", [False, True])
async def test_old_dispatch_cancellation_finishes_before_link_cleanup_returns(monkeypatch, direct_stop):
    ua, backend, _ = build()
    peer = await handshake(ua, backend)
    conn = ua.link.conn
    entered, cleanup_started, release_cleanup, cleaned = (asyncio.Event() for _ in range(4))

    async def slow_handler(params):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleaned.set()

    ua.link.on("fixture.slow", slow_handler)
    await peer.send(PipeFrame(id=9, method="fixture.slow", params={}, deadline_ms=10000))
    if not direct_stop:
        fail_heartbeat(monkeypatch, conn, BrokenPipeError("heartbeat lost"), before_error=entered.wait)
    runner = asyncio.create_task(ua.run())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if direct_stop:
            stopping = asyncio.create_task(ua.link.stop())
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        boundary = stopping if direct_stop else runner
        assert not boundary.done(), "old requests must finish cancellation before a fresh connection is used"
        release_cleanup.set()
        await asyncio.wait_for(boundary, timeout=1)
        assert cleaned.is_set()
        assert conn.closed and ua.link.conn is None
    finally:
        release_cleanup.set()
        await ua.stop()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.parametrize("desired_running", [True, False])
async def test_real_user_agent_reconnects_after_five_seconds_without_resetting_wsl_intent(monkeypatch, desired_running):
    ua, backend, wsl = build()
    ctl = ua.wslctl
    if not desired_running:
        await ctl.stop()
    connected = []
    connect = backend.connect
    second_heartbeat = asyncio.Event()

    async def connected_pipe(*args, **kwargs):
        conn = await connect(*args, **kwargs)
        connected.append(conn)
        send = conn.send
        index = len(connected)

        async def sending(frame):
            if frame.method == PING:
                if index == 1:
                    raise BrokenPipeError("service disappeared after Peek")
                second_heartbeat.set()
            return await send(frame)

        monkeypatch.setattr(conn, "send", sending)
        return conn

    monkeypatch.setattr(backend, "connect", connected_pipe)
    real_sleep = asyncio.sleep
    retry_delays = []
    retry_entered, retry_release = asyncio.Event(), asyncio.Event()

    async def controlled_sleep(seconds):
        if seconds == 5:
            retry_delays.append(seconds)
            retry_entered.set()
            await retry_release.wait()
        else:
            await real_sleep(seconds)

    monkeypatch.setattr(main_user.asyncio, "sleep", controlled_sleep)

    async def serve_twice():
        for _ in range(2):
            peer = await backend.serve(ua.cfg.api.pipe_user)
            hello = await peer.recv()
            assert hello.method == HELLO
            await peer.send(PipeFrame(id=hello.id, ok=True, result={"ipc_version": 1}))

    server = asyncio.create_task(serve_twice())
    runner = asyncio.create_task(main_user.run_forever(ua))
    try:
        await asyncio.wait_for(retry_entered.wait(), timeout=1)
        assert retry_delays == [5]
        assert len(connected) == 1 and connected[0].closed
        assert ua.link.conn is None
        assert ua.wslctl is ctl and ctl.desired_running is desired_running
        wsl.close.assert_not_awaited()
        retry_release.set()
        await asyncio.wait_for(second_heartbeat.wait(), timeout=1)
        assert len(connected) == 2 and not connected[1].closed
        assert ua.wslctl is ctl and ctl.desired_running is desired_running
        wsl.close.assert_not_awaited()
    finally:
        retry_release.set()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    wsl.close.assert_awaited_once()
