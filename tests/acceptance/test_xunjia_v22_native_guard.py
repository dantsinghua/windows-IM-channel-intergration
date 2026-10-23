"""Independent V2.2 native authorization gates, entirely synthetic.

Real Bus/Store and QidianUi anchor execution are exercised. Only the external
adapter/native tap and rate-limit/UI wait boundaries are controlled fixtures.
No account service, Docker, ADB, Windows process or real transport is started.
"""
import asyncio

import pytest

from qtrade_agent.adapters.qidian.ui import Node, Profile, QidianUi
from qtrade_agent.bus.bus import Bus
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Command, CommandError, CommandResult
from qtrade_agent.store import Store


class Adapter:
    channel = "qidian"
    capabilities = frozenset({"send_text"})

    def __init__(self):
        self.calls = []

    async def send(self, account, command):
        self.calls.append(command)
        # A deterministic external boundary: never starts the confirmation loop.
        return CommandResult(False, "SEND_FAILED", command.trace_id or "", error=CommandError("synthetic adapter boundary"))


def command(key="synthetic-send"):
    return Command("qd90", "send_text", {"session": "synthetic-peer", "text": "synthetic quotation"},
                   idempotency_key=key, timeout_ms=3000)


def make_bus(tmp_path, adapter):
    store = Store(str(tmp_path / "synthetic-native.db")).open()
    store.ensure_account("qd90", "qidian", state="running", self_uid="synthetic-self")
    bus = Bus(store=store, events=Events(store), adapters={"qidian": adapter}, cfg=AgentConfig())
    return store, bus


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_native_guard_is_checked_after_rate_limit_await(tmp_path, failure):
    async def run():
        adapter = Adapter()
        store, bus = make_bus(tmp_path, adapter)
        entered, release = asyncio.Event(), asyncio.Event()
        allowed = True
        def guard():
            if not allowed and failure == "exception":
                raise OSError("synthetic permission unavailable")
            return allowed
        async def rate_limit(account_id):
            entered.set()
            await release.wait()
        bus._rate_limit = rate_limit
        cmd = command()
        cmd._xunjia_native_guard = guard
        task = asyncio.create_task(bus.submit(cmd))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            allowed = False
            release.set()
            result = await asyncio.wait_for(task, timeout=2)
            assert adapter.calls == [], "Revoked/unknown native permission must prevent adapter invocation after rate limiting"
            assert result.code == "GATE_BLOCKED" and not result.ok
            assert result.error.reason == "xunjia_native_permission_changed"
        finally:
            release.set()
            await bus.close()
            store.close()
    asyncio.run(run())


def test_native_guard_does_not_leak_to_next_unguarded_command(tmp_path):
    async def run():
        adapter = Adapter()
        store, bus = make_bus(tmp_path, adapter)
        cmd = command("guarded")
        cmd._xunjia_native_guard = lambda: False
        try:
            denied = await bus.submit(cmd)
            assert denied.code == "GATE_BLOCKED"
            assert adapter.calls == []
            result = await bus.submit(command("ordinary"))
            assert len(adapter.calls) == 1
            assert adapter.calls[0].idempotency_key == "ordinary"
            assert result.code == "SEND_FAILED"
        finally:
            await bus.close()
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("fallback", [False, True])
def test_ui_rechecks_native_guard_after_async_dump_before_final_send_tap(tmp_path, fallback):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        allowed, taps = True, []
        profile = Profile(version="synthetic", package="synthetic.package", splash_activity="", ime="",
                          activities={}, nodes={"chat_send": {"id": "synthetic-send-node"}}, markers={},
                          timeouts={"ui_settle_s": 0}, coord_fallback={"enabled": fallback, "taps": {"chat_send": "10 20"}})
        ui = QidianUi(adb=object(), profiles={"synthetic": profile})
        async def dump(account, selected_profile):
            entered.set()
            await release.wait()
            return [] if fallback else [Node("synthetic-send-node", "Send", "", "Button", (0, 0, 20, 40), True, 0)]
        async def tap(account, x, y, selected_profile):
            taps.append((x, y))
        ui._dump, ui._tap = dump, tap
        class UiAdapter(Adapter):
            async def send(self, account, cmd):
                self.calls.append(cmd)
                ok = await ui._tap_anchor(account, profile, "chat_send")
                return CommandResult(False, "SEND_FAILED", cmd.trace_id or "", error=CommandError("synthetic tap=" + str(ok)))
        adapter = UiAdapter()
        store, bus = make_bus(tmp_path, adapter)
        cmd = command()
        cmd._xunjia_native_guard = lambda: allowed
        task = asyncio.create_task(bus.submit(cmd))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert len(adapter.calls) == 1
            allowed = False
            release.set()
            result = await asyncio.wait_for(task, timeout=2)
            assert taps == [], "A source event during UI lookup must stop the irreversible send tap"
            assert result.code == "GATE_BLOCKED" and not result.ok
            assert result.error.reason == "xunjia_native_permission_changed"
        finally:
            release.set()
            await bus.close()
            store.close()
    asyncio.run(run())
