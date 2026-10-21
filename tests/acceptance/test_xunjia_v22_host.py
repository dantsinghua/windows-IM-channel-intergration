"""Independent collector-host contracts with injected protocol/network edges.

The host, its SQLite state, event normalization scheduling and lifecycle are
real. No external protocol package, service, account or network is required.
"""
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest

from qtrade_agent.events import Events
from qtrade_agent.store import Store
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host, collector_version


class Ledger:
    def __init__(self, order):
        self.order, self.pending = order, set()
        self.clock = time.time
    def begin_inbound(self, event_id, peer_id):
        self.order.append(("begin", event_id))
        self.pending.add(event_id)
    def stage_out(self, data, seconds):
        self.order.append(("stage_out", data["event_id"]))
        return "waiting"
    def permission(self, command):
        return not self.pending


class Port:
    def __init__(self, binding):
        self.binding = binding
        self.adapters = {}
        self.allow_native_send = False
        self.execution_permission = None
        self.calls = []
    def _account(self):
        return {"id": "qd90", "self_uid": self.binding.account_uid, "state": "running"}
    def normalize_native_message(self, row, live=False):
        return {"event_id": "native-" + row["ext_msg_id"], "peer_id": row["session_id"].split(":", 1)[1],
                "direction": row["dir"], "text": row["text"], "kind": "message", "ext_msg_id": row["ext_msg_id"],
                "occurred_at": row["ts_ms"] / 1000, "historical": not live}
    def native_send(self, command):
        if not self.allow_native_send or not self.execution_permission(command):
            return {"status": "failed", "evidence": "synthetic permission blocked"}
        self.calls.append(command)
        return {"status": "unknown", "evidence": "synthetic unknown"}
    def native_reconcile(self, command):
        return {"status": "unknown", "evidence": "synthetic read only reconciliation"}


class Bridge:
    echo_wait_seconds = 1
    def __init__(self, binding):
        self.binding = binding
        self.order, self.published = [], []
        self.ledger = Ledger(self.order)
        self.fail_publication = False
        self.runs = 0
        self.catalogs = 0
    def publish_native(self, data, historical=False):
        self.order.append(("publish", data["event_id"]))
        if self.fail_publication:
            raise OSError("synthetic network unavailable")
        self.published.append(deepcopy(data))
        self.ledger.pending.discard(data["event_id"])
    def run_once(self):
        self.runs += 1
        return {"processed": 0, "errors": []}
    def publish_capabilities(self):
        self.catalogs += 1


def build(tmp_path, loop, *, enabled=False, legacy_disabled=lambda: True, bus_guard=True):
    binding = SimpleNamespace(owner_user_id="synthetic-owner", account_uid="synthetic-account", workbench_id="synthetic-workbench")
    bridge, port = Bridge(binding), Port(binding)
    host = XunjiaV22Host(store=SimpleNamespace(outbox_seq_bounds=lambda: (0, 0)),
        bus=SimpleNamespace(supports_xunjia_v22_guard=bus_guard), loop=loop, binding=binding,
        bridge=bridge, port=port, state_path=tmp_path / "host.sqlite3",
        legacy_inbox_disabled=legacy_disabled, allow_native_send=enabled)
    return host, bridge, port


def observe(host, *, text="first quotation", revoked=False, direction="in", event_id="event-1", account="qd90"):
    host.observe(event="message", channel="qidian", account_id=account, event_id=event_id,
        payload={"account_id": account, "session": {"id": "qd90:synthetic-peer", "kind": "private"},
                 "ext_msg_id": "synthetic-native-id", "dir": direction, "text": text,
                 "ts_ms": 1_792_202_400_000, "revoked": revoked})


def test_host_rejects_legacy_overlap_and_missing_bus_guard(tmp_path):
    async def run():
        with pytest.raises(RuntimeError, match="legacy"):
            build(tmp_path, asyncio.get_running_loop(), legacy_disabled=lambda: False)
        with pytest.raises(RuntimeError, match="guard"):
            build(tmp_path, asyncio.get_running_loop(), bus_guard=False)
    asyncio.run(run())


def test_host_defaults_disable_native_send_and_start_is_single_worker(tmp_path):
    async def run():
        host, bridge, port = build(tmp_path, asyncio.get_running_loop())
        assert not host.permission({}) and not port.allow_native_send
        await host.start()
        worker = host._worker
        await host.start()
        assert host._worker is worker
        assert not host.permission({}) and not port.allow_native_send
        assert bridge.native_send({"business_version": "2.2"})["status"] == "failed"
        assert port.calls == []
        await host.stop()
        assert not port.allow_native_send and not host.permission({})
    asyncio.run(run())


def test_intake_registers_local_barrier_before_any_network_and_deduplicates(tmp_path):
    async def run():
        host, bridge, port = build(tmp_path, asyncio.get_running_loop())
        observe(host)
        assert bridge.order == [("begin", "native-synthetic-native-id")]
        assert bridge.published == [] and bridge.runs == 0
        observe(host)
        await host.run_once()
        assert len(bridge.published) == 1
        assert bridge.order.index(("begin", "native-synthetic-native-id")) < bridge.order.index(("publish", "native-synthetic-native-id"))
        assert port.calls == []
    asyncio.run(run())


def test_native_edit_and_retract_keep_one_original_reference(tmp_path):
    async def run():
        host, bridge, _ = build(tmp_path, asyncio.get_running_loop())
        observe(host)
        observe(host, text="corrected quotation", event_id="event-2")
        observe(host, text="corrected quotation", revoked=True, event_id="event-3")
        assert bridge.published == [], "Native observations must never perform network I/O inline"
        await host.run_once()
        assert [data["kind"] for data in bridge.published] == ["message", "edit", "retract"]
        first, edit, retract = bridge.published
        assert edit["target_event_id"] == retract["target_event_id"] == first["event_id"]
        assert len({data["event_id"] for data in bridge.published}) == 3
        assert all(not data["historical"] for data in bridge.published)
    asyncio.run(run())


def test_outbound_echo_is_held_locally_before_publication(tmp_path):
    async def run():
        host, bridge, _ = build(tmp_path, asyncio.get_running_loop())
        observe(host, direction="out")
        assert bridge.order == [("stage_out", "native-synthetic-native-id")]
        assert bridge.published == []
        await host.run_once()
        assert bridge.order[0][0] == "stage_out"
    asyncio.run(run())


def test_restart_replays_unpublished_native_events_without_resending(tmp_path):
    async def run():
        host, bridge, port = build(tmp_path, asyncio.get_running_loop())
        bridge.fail_publication = True
        observe(host)
        with pytest.raises(OSError):
            await host.run_once()
        restarted, second, next_port = build(tmp_path, asyncio.get_running_loop())
        await restarted.run_once()
        await restarted.run_once()
        assert len(second.published) == 1 and second.published[0]["text"] == "first quotation"
        assert port.calls == next_port.calls == []
    asyncio.run(run())


def test_host_never_accepts_old_business_commands_or_another_account_event(tmp_path):
    async def run():
        host, bridge, port = build(tmp_path, asyncio.get_running_loop(), enabled=True)
        assert bridge.native_send({"business_version": "1"})["status"] == "failed"
        observe(host, account="qd91")
        await host.run_once()
        assert not bridge.published and not port.calls
    asyncio.run(run())


def test_version_opt_in_is_explicit_and_has_no_unknown_version_fallback():
    assert collector_version({}) == "1"
    assert collector_version({"XUNJIA_COLLECTOR_VERSION": "2.2"}) == "2.2"
    with pytest.raises(ValueError):
        collector_version({"XUNJIA_COLLECTOR_VERSION": "future-version"})


def test_live_edit_then_durable_outbox_replay_never_republishes_older_text(tmp_path):
    async def run():
        loop = asyncio.get_running_loop()
        store = Store(str(tmp_path / "qtrade-outbox.sqlite3")).open()
        binding = SimpleNamespace(owner_user_id="synthetic-owner", account_uid="synthetic-account", workbench_id="synthetic-workbench")
        bridge, port = Bridge(binding), Port(binding)
        host = XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop,
            binding=binding, bridge=bridge, port=port, state_path=tmp_path / "host.sqlite3", legacy_inbox_disabled=lambda: True)
        events = Events(store, on_emit=host.observe)
        body = {"account_id": "qd90", "session": {"id": "qd90:synthetic-peer", "kind": "private"},
                "ext_msg_id": "synthetic-native-id", "dir": "in", "text": "first quotation",
                "ts_ms": 1_792_202_400_000, "revoked": False}
        try:
            events.emit("message", payload=body, account_id="qd90", channel="qidian", now_ms=1_792_202_400_000)
            events.emit("message", payload={**body, "text": "corrected quotation"}, account_id="qd90", channel="qidian", now_ms=1_792_202_401_000)
            await host.run_once()
            assert [row["text"] for row in bridge.published] == ["first quotation", "corrected quotation"], (
                "Replaying an already observed outbox event cannot create a new edit that restores an older body"
            )
            await host.run_once()
            assert len(bridge.published) == 2
        finally:
            store.close()
    asyncio.run(run())


def test_capability_is_published_only_by_active_guarded_host(tmp_path):
    async def run():
        host, bridge, port = build(tmp_path, asyncio.get_running_loop())
        announced = asyncio.Event()
        loop = asyncio.get_running_loop()
        def publish():
            bridge.catalogs += 1
            loop.call_soon_threadsafe(announced.set)
        bridge.publish_capabilities = publish
        await host.run_once()
        assert bridge.catalogs == 0
        assert callable(port.execution_permission)
        await host.start()
        try:
            await asyncio.wait_for(announced.wait(), timeout=2)
            assert bridge.catalogs == 1
            assert not port.allow_native_send, "Receiving V2 capability does not opt into native sending"
        finally:
            await host.stop()
        await host.run_once()
        assert bridge.catalogs == 1
    asyncio.run(run())


@pytest.mark.parametrize("private_config", [False, True])
def test_missing_private_config_or_key_reference_cannot_enable_host(tmp_path, monkeypatch, private_config):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    if private_config:
        path = tmp_path / "private-config.json"
        path.write_text(json.dumps({"owner_user_id": "synthetic-owner", "account_uid": "synthetic-account",
            "workbench_id": "synthetic-workbench", "seal_key_file": "missing-seal.txt",
            "sign_key_file": "missing-sign.txt", "allow_native_send": True}))
        monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", str(path))
    else:
        monkeypatch.delenv("XUNJIA_COLLECTOR_CONFIG_FILE", raising=False)
    async def run():
        with pytest.raises(ValueError, match="NOT_CONFIGURED"):
            XunjiaV22Host.from_env(store=object(), bus=object(), loop=asyncio.get_running_loop(), box=object(),
                data_dir=tmp_path, legacy_inbox_disabled=lambda: True)
    asyncio.run(run())
