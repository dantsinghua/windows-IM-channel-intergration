"""Bounded guest startup diagnostics and pipe draining with fake holder children."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent import user
from qtrade_winagent.win import wsl
from tests.test_wsl_lifecycle_portability import HolderProcess, holder_backend, session_agent


class DiagnosticStream:
    def __init__(self, chunks=(), *, stay_open=False):
        self.chunks = list(chunks)
        self.stay_open = stay_open
        self.read_sizes = []
        self.entered = asyncio.Event()
        self.drained = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def read(self, size):
        self.entered.set()
        self.read_sizes.append(size)
        await asyncio.sleep(0)
        if self.chunks:
            chunk = self.chunks.pop(0)
            if len(chunk) > size:
                self.chunks.insert(0, chunk[size:])
                return chunk[:size]
            return chunk
        self.drained.set()
        if self.stay_open:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return b""


@pytest.mark.parametrize("root_cause", ["ROOT_CAUSE: WSL_NETWORK_UNAVAILABLE", "ROOT_CAUSE: 无法挂载发行版"])
async def test_failed_holder_preserves_exit_code_and_bounded_stderr_tail(monkeypatch, root_cause):
    child = HolderProcess(ready=b"")
    child.returncode = 37
    child.stderr = DiagnosticStream([b"old-output-" * 4096, root_cause.encode("utf-8")])
    backend, launch = holder_backend(monkeypatch, child)
    with pytest.raises(RuntimeError) as error:
        await backend.start("qtrade-test-owned")
    detail = str(error.value)
    assert "returncode=37" in detail and root_cause in detail
    assert len(detail) <= 4500
    assert child.stderr.drained.is_set()
    assert launch.call_args.kwargs["stderr"] == asyncio.subprocess.PIPE
    assert all(0 < size <= 4096 for size in child.stderr.read_sizes)


async def test_large_stderr_is_drained_while_start_waits_for_ready_without_success_logging(monkeypatch, caplog):
    child = HolderProcess()
    child.stderr = DiagnosticStream([b"secret-free-diagnostic-" * 4096 for _ in range(4)])

    async def ready_after_drain():
        await child.stderr.drained.wait()
        return b"QTRADE_WSL_READY\n"

    child.stdout.readline = ready_after_drain
    backend, _ = holder_backend(monkeypatch, child)
    try:
        await asyncio.wait_for(backend.start("qtrade-test-owned"), timeout=1)
        assert len(child.stderr.read_sizes) > 4
        assert all(0 < size <= 4096 for size in child.stderr.read_sizes)
        assert "secret-free-diagnostic" not in caplog.text
    finally:
        await backend.close()


async def test_ready_timeout_keeps_timeout_type_and_stderr_reason(monkeypatch):
    child = HolderProcess(block_ready=True)
    child.stderr = DiagnosticStream([b"ROOT_CAUSE: guest launch denied"])
    backend, _ = holder_backend(monkeypatch, child)
    monkeypatch.setattr(wsl, "HOLDER_START_TIMEOUT_S", 0.01)
    with pytest.raises(TimeoutError) as error:
        await backend.start("qtrade-test-owned")
    assert "ROOT_CAUSE: guest launch denied" in str(error.value)
    assert "returncode=" in str(error.value)
    child.stdin.close.assert_called_once()


async def test_close_reaps_stderr_pump_when_stream_never_reaches_eof(monkeypatch):
    child = HolderProcess()
    child.stderr = DiagnosticStream(stay_open=True)
    backend, _ = holder_backend(monkeypatch, child)
    monkeypatch.setattr(wsl, "HOLDER_CLOSE_TIMEOUT_S", 0.01)
    await backend.start("qtrade-test-owned")
    await asyncio.wait_for(child.stderr.entered.wait(), timeout=1)
    await asyncio.wait_for(backend.close(), timeout=1)
    assert child.stderr.cancelled.is_set()
    child.stdin.close.assert_called_once()


async def test_background_warning_retains_original_reason_and_tail_with_bounded_detail(monkeypatch, caplog):
    ua, backend = session_agent(monkeypatch)
    root_cause = "ROOT_CAUSE: guest filesystem could not be mounted"
    backend.start = AsyncMock(side_effect=RuntimeError("holder exited: " + "X" * 5000 + root_cause))
    waited = asyncio.Event()
    original_sleep = asyncio.sleep

    async def stop_at_retry(seconds):
        if seconds == 60:
            waited.set()
            await asyncio.Future()
        else:
            await original_sleep(seconds)

    monkeypatch.setattr(user.asyncio, "sleep", stop_at_retry)
    caplog.set_level("WARNING", logger="wa.user")
    try:
        await ua.start()
        await asyncio.wait_for(waited.wait(), timeout=1)
        records = [record for record in caplog.records if getattr(record, "op", None) == "wsl.start"]
        assert len(records) == 1
        detail = records[0].kv.get("detail", "")
        assert "holder exited" in detail and root_cause in detail
        assert len(detail) <= 4096
    finally:
        await ua.stop()
