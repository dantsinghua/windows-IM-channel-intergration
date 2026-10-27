"""P0: only persisted exact native evidence reconciles an immutable send, never text alone."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_agent.adapters.wechat import FakeWeChatWinAgent, WechatAdapter, WeChatWinAgent, WechatPoller
from qtrade_agent.bus.bus import Bus
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.gate import Gate
from qtrade_agent.models import Command, CommandError, CommandResult
from qtrade_agent.store import Store
from qtrade_agent.winagent_client import WinAgentClient
from relay.collector_bridge import CollectorBridge
from relay.protocol import Binding, ProtocolError, envelope
from relay.qtrade_native_port import QTradeNativePort
from server.receiver_ledger import ReceiverLedger


TEXT = "QA offline late receipt"
PEER = "filehelper"


class Clock:
    def __init__(self):
        self.origin = time.monotonic()
        self.offset = 0

    def __call__(self):
        return 1758240000000 + self.offset + int((time.monotonic() - self.origin) * 1000)


class Downlink:
    polls_downlink = True
    role = "collector"

    def __init__(self, binding, path):
        self.binding = binding
        self.store = SimpleNamespace(path=Path(path), source_highwater=0)
        self.pending, self.receipts, self.acks = [], [], []

    def poll(self, limit=50):
        rows, self.pending = self.pending, []
        return rows

    def ack(self, token):
        self.acks.append(token)

    def queue_receipt(self, command, result):
        self.receipts.append(dict(result))

    def flush_publications(self, **kwargs):
        pass

    def flush_events(self, **kwargs):
        pass


class LateRig:
    def __init__(self, root):
        self.root = root
        self.clock = Clock()
        cfg = AgentConfig()
        self.cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=3,
                           send_min_interval_ms=0, send_rand_extra_ms=0),
                           wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))
        self.fake = FakeWeChatWinAgent()
        self.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": 3}
        self.refusal = None
        self.http_sends = 0
        self.bus_submits = 0
        self.binding = Binding("synthetic-owner", "wxid_demo01", "synthetic-workbench",
                               b"a" * 16, b"b" * 32, channel="wechat")
        self.open()
        now = self.clock() / 1000
        self.command = {"owner_user_id": self.binding.owner_user_id, "account_uid": self.binding.account_uid,
                        "workbench_id": self.binding.workbench_id, "channel": "wechat",
                        "command_id": "offline-late-receipt", "peer_id": self.port._relay_peer(PEER),
                        "text": TEXT, "content_hash": "a" * 64, "control_version": 1,
                        "expires_at": now + 120, "transport": "oss", "business_version": "2.2",
                        "source_watermark": 0, "account_epoch": 0}

    def open(self):
        self.store = Store(str(self.root / "collector.db"), clock=self.clock).open()
        self.store.ensure_account("wx01", "wechat", state="running", login_mode="qrcode", self_uid="wxid_demo01")
        self.events = Events(self.store)
        wa = WinAgentClient(self.cfg.winagent, transport=self.transport,
                             base_url="http://127.0.0.1:17610", token=self.fake.token)
        self.client = WeChatWinAgent(wa)
        self.poller = WechatPoller(store=self.store, events=self.events, client=self.client,
                                    cfg=self.cfg, clock=self.clock)
        self.adapter = WechatAdapter(self.poller, client=self.client, store=self.store)
        self.bus = Bus(store=self.store, events=self.events, adapters={"wechat": self.adapter},
                       cfg=self.cfg, clock=self.clock, gate=Gate(self.store))
        submit = self.bus.submit

        async def counted(command):
            self.bus_submits += 1
            return await submit(command)

        self.bus.submit = counted
        self.ledger = ReceiverLedger(self.root / "receiver.db", self.binding,
                                      clock=lambda: self.clock() / 1000)
        self.port = QTradeNativePort(binding=self.binding, store=self.store, bus=self.bus,
                                    loop=asyncio.get_running_loop(), legacy_inbox_disabled=lambda: True,
                                    execution_permission=self.ledger.permission, identity_epoch=0)
        self.downlink = Downlink(self.binding, self.root / "transport.db")
        self.bridge = CollectorBridge(ledger=self.ledger, adapters={"oss": self.downlink},
                                       native_send=self.port.native_send,
                                       native_reconcile=self.port.native_reconcile,
                                       native_quiescent=self.port.quiescent)

    async def transport(self, method, url, headers, body, timeout_s):
        if method == "POST" and url.endswith("/wa/v1/wechat/send"):
            self.http_sends += 1
            if self.refusal:
                return 503, {}, json.dumps({"ok": False, "error": {
                    "reason": self.refusal, "message": "Synthetic pre-input refusal"}}).encode()
        return await self.fake(method, url, headers, body, timeout_s)

    async def run(self, kind, token):
        now = self.clock() / 1000
        self.downlink.pending.append({"source_token": token, "envelope": envelope(
            self.binding, kind, self.command, message_id="envelope-" + token,
            command_id=self.command["command_id"], peer_id=self.command["peer_id"],
            content_hash=self.command["content_hash"], control_version=1,
            created_at=now, expires_at=self.command["expires_at"])})
        tick = await asyncio.to_thread(self.bridge.run_once)
        assert tick == {"processed": 1, "errors": []}
        return self.downlink.receipts[-1]

    def original(self):
        row = self.store.idem_get("wx01", self.command["command_id"])
        return row, self.store.get_command_result(row["trace_id"])

    def placeholder(self):
        return dict(self.store.con.execute("SELECT * FROM messages WHERE trace_id IS NOT NULL").fetchone())

    def expect_exact_readback(self):
        # WinAgent identified this native row; Agent read has not observed it yet.
        self.fake.send_result = {"ok": True, "code": "DELIVERED",
                                 "ext_msg_id": f"{PEER}:31", "confirm_ms": 3}

    def pending_readback(self):
        idem, _ = self.original()
        record = self.store.settings_get("wechat.readback." + idem["trace_id"])
        assert record["status"] == "pending"
        assert record["account_id"] == "wx01" and record["self_uid"] == self.binding.account_uid
        assert record["identity_epoch"] == self.command["account_epoch"]
        assert record["native"] == PEER and record["ext"] == f"{PEER}:31"
        assert record["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
        command = self.store.get_command(idem["trace_id"])
        assert command["idempotency_key"] == self.command["command_id"]
        return record

    async def late_poll(self, *, text=TEXT, peer=PEER, is_self=True, seq=31):
        self.clock.offset += 12000
        row = self.fake.add_row(talker=peer, content=text, is_self=is_self, seq=seq, ts_ms=self.clock())
        await self.adapter.poll(self.bus._load_account("wx01"))
        return f"{peer}:{row['seq']}"

    async def close(self):
        await self.bus.close()
        self.store.close()


@pytest.fixture
async def rig(tmp_path):
    r = LateRig(tmp_path)
    try:
        yield r
    finally:
        await r.close()


async def test_exact_native_receipt_waits_for_real_late_read_before_confirming(rig):
    rig.expect_exact_readback()
    first = await rig.run("command", "first")
    assert first["status"] == "unknown"
    idem, result = rig.original()
    assert not result["ok"] and result["code"] == "SEND_FAILED"
    assert not result["error_retryable"] and result["error_needs_human"]
    assert idem["status"] == "DONE"
    placeholder = rig.placeholder()
    assert placeholder["state"] == "UNCONFIRMED"
    assert placeholder["ext_msg_id"] is None
    rig.pending_readback()
    native = await rig.late_poll()
    confirmed = rig.store.confirmed_outbound_for_trace(idem["trace_id"])
    assert confirmed["id"] == placeholder["id"] and confirmed["ext_msg_id"] == native
    assert confirmed["confirmed_by"] == "chatlog"
    assert rig.store.count_messages("wx01") == 1
    for token in ("query-one", "query-two"):
        outcome = await rig.run("query", token)
        assert outcome["status"] == "sent" and outcome["native_id"] == native
    assert rig.http_sends == len(rig.fake.sent) == rig.bus_submits == 1
    assert rig.original()[1]["ok"] == 0


async def test_restart_and_duplicate_command_or_query_never_resubmit_native_action(rig):
    rig.expect_exact_readback()
    assert (await rig.run("command", "first"))["status"] == "unknown"
    assert rig.store.idem_get("wx01", rig.command["command_id"])["status"] == "DONE"
    before = rig.pending_readback()
    await rig.close()
    rig.open()
    assert rig.pending_readback() == before
    assert (await rig.run("command", "duplicate-after-restart"))["status"] == "unknown"
    assert (await rig.run("query", "query-before-late"))["status"] == "unknown"
    native = await rig.late_poll()
    outcome = await rig.run("query", "query-after-late")
    assert outcome["status"] == "sent" and outcome["native_id"] == native
    assert rig.http_sends == len(rig.fake.sent) == rig.bus_submits == 1


async def test_done_unknown_idempotency_replay_never_calls_native_send_again(rig):
    assert (await rig.run("command", "first"))["status"] == "unknown"
    original = rig.original()[0]
    replay = await rig.bus.submit(Command("wx01", "send_text", {"session": PEER, "text": TEXT},
                                          idempotency_key=rig.command["command_id"], timeout_ms=1000))
    assert not replay.ok and replay.code == "IDEMPOTENT_REPLAY"
    assert replay.trace_id == original["trace_id"]
    assert rig.store.idem_get("wx01", rig.command["command_id"])["status"] == "DONE"
    assert rig.http_sends == len(rig.fake.sent) == 1


@pytest.mark.parametrize("change", ["peer", "text", "incoming", "native_id"])
async def test_unrelated_late_native_rows_never_confirm_original_trace(rig, change):
    rig.expect_exact_readback()
    assert (await rig.run("command", "first"))["status"] == "unknown"
    options = {"peer": {"peer": "another-peer"}, "text": {"text": "Different synthetic message"},
               "incoming": {"is_self": False}, "native_id": {"seq": 32}}[change]
    await rig.late_poll(**options)
    assert rig.store.confirmed_outbound_for_trace(rig.original()[0]["trace_id"]) is None
    assert (await rig.run("query", "query-unrelated"))["status"] == "unknown"
    assert rig.http_sends == len(rig.fake.sent) == 1


@pytest.mark.parametrize("change", ["account_uid", "owner_user_id", "workbench_id", "peer", "text", "epoch"])
async def test_reconcile_requires_exact_command_identity_epoch_peer_and_text(rig, change):
    rig.expect_exact_readback()
    await rig.run("command", "first")
    await rig.late_poll()
    assert rig.port.native_reconcile(rig.command)["status"] == "sent"
    command = dict(rig.command)
    if change == "epoch":
        rig.store.settings_set("relay.identity.wx01", {"epoch": 1, "uid": "wxid_demo01"})
    elif change == "peer":
        command["peer_id"] = rig.port._relay_peer("another-peer")
    elif change == "text":
        command["text"] = "Different synthetic text"
    else:
        command[change] = "different-synthetic-identity"
    try:
        outcome = rig.port.native_reconcile(command)
    except ProtocolError:
        outcome = {"status": "rejected"}
    assert outcome["status"] != "sent"
    assert rig.http_sends == len(rig.fake.sent) == 1


async def test_explicit_preinput_not_ready_is_failed_and_never_late_confirmable(rig):
    rig.refusal = "wechat_session_mismatch"
    assert (await rig.run("command", "first"))["status"] == "unknown"
    placeholder = rig.placeholder()
    assert placeholder["state"] == "FAILED"
    assert rig.original()[1]["code"] == "NOT_READY"
    assert len(rig.fake.sent) == 0 and rig.http_sends == 1
    await rig.late_poll()
    assert rig.store.confirmed_outbound_for_trace(placeholder["trace_id"]) is None
    assert rig.placeholder()["state"] == "FAILED"


@pytest.mark.parametrize("mutation, expected_state", [
    ("reason", "UNCONFIRMED"), ("retryable", "UNCONFIRMED"),
    ("needs_human", "UNCONFIRMED"), ("source", "UNCONFIRMED"),
    ("channel", "FAILED"), ("code-NOT_READY", "FAILED"),
    ("code-GATE_BLOCKED", "FAILED"), ("code-LOGIN_REQUIRED", "FAILED"),
])
async def test_failure_state_requires_definite_preinput_or_other_channel_evidence(
        rig, monkeypatch, mutation, expected_state):
    error = CommandError("Synthetic refusal", reason="send_outcome_unknown", retryable=False, needs_human=True)
    source = "chatlog"
    if mutation == "reason":
        error.reason = "different-failure"
    elif mutation == "retryable":
        error.retryable = True
    elif mutation == "needs_human":
        error.needs_human = False
    elif mutation == "source":
        source = "qidian_db"
    code = mutation[5:] if mutation.startswith("code-") else "SEND_FAILED"
    result = CommandResult(ok=False, code=code, trace_id="", source=source, error=error)
    if mutation == "channel":
        rig.store.ensure_account("qd01", "qidian", state="running", self_uid="300000001")
        adapter = SimpleNamespace(capabilities={"send_text"}, send=AsyncMock(return_value=result))
        rig.bus.adapters["qidian"] = adapter
        account = "qd01"
    else:
        monkeypatch.setattr(rig.adapter, "send", AsyncMock(return_value=result))
        account = "wx01"
    command = Command(account, "send_text", {"session": PEER, "text": TEXT},
                      idempotency_key="untrusted-marker", timeout_ms=1000)
    outcome = await rig.bus.submit(command)
    assert not outcome.ok
    assert rig.placeholder()["state"] == expected_state
    if expected_state == "UNCONFIRMED":
        assert outcome.error.reason == "send_outcome_unknown"
        assert not outcome.error.retryable and outcome.error.needs_human
        assert rig.store.idem_get(account, command.idempotency_key)["status"] == "DONE"
        replay = await rig.bus.submit(command)
        assert replay.code == "IDEMPOTENT_REPLAY" and replay.trace_id == outcome.trace_id
        rig.adapter.send.assert_awaited_once()
    assert rig.http_sends == 0


@pytest.mark.parametrize("native_result", [
    {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": 3},
    {"ok": True, "code": "OK"},
], ids=["send-failed-no-ext", "ok-no-ext"])
@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "after-restart"])
async def test_no_exact_receipt_keeps_same_text_unbound_without_retry(rig, native_result, restart):
    rig.fake.send_result = native_result
    assert (await rig.run("command", "first"))["status"] == "unknown"
    original = rig.placeholder()
    idem, _ = rig.original()
    assert original["state"] == "UNCONFIRMED" and idem["status"] == "DONE"
    assert rig.store.settings_get("wechat.readback." + idem["trace_id"]) is None
    if restart:
        await rig.close()
        rig.open()
    native = await rig.late_poll()
    assert rig.placeholder()["id"] == original["id"]
    assert rig.placeholder()["state"] == "UNCONFIRMED"
    assert rig.placeholder()["ext_msg_id"] is None
    assert rig.store.confirmed_outbound_for_trace(idem["trace_id"]) is None
    rows = rig.store.list_messages("wx01")
    assert len(rows) == 2
    collected = next(row for row in rows if row["ext_msg_id"] == native)
    assert collected["trace_id"] is None and collected["state"] == "DELIVERED"
    assert collected["text"] == TEXT and collected["source"] == "chatlog"
    assert (await rig.run("query", "after-late"))["status"] == "unknown"
    assert (await rig.run("command", "duplicate-after-late"))["status"] == "unknown"
    await rig.adapter.poll(rig.bus._load_account("wx01"))
    assert rig.store.count_messages("wx01") == 2
    assert rig.http_sends == len(rig.fake.sent) == rig.bus_submits == 1
