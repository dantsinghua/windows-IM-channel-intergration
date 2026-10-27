"""Original native capture is required for late confirmation; all I/O is synthetic."""
from copy import deepcopy
import asyncio
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from qtrade_agent.config import AgentConfig
from qtrade_agent.models import Command
from tests.conftest import Clock
from tests.test_wechat_rig import make_wechat_rig
from tests.test_wechat_p0_result_contract import peer_result


TEXT = "Synthetic immutable late payload"
KEY = "synthetic-late-proof-key"
PEER = "p"
ANCHOR_TEXT = "Previous synthetic anchor"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config():
    cfg = AgentConfig()
    return replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=100,
                   send_min_interval_ms=0, send_rand_extra_ms=0),
                   wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))


def command():
    return Command("wx01", "send_text", {"session": "wx01:p", "text": TEXT},
                   idempotency_key=KEY, timeout_ms=60000)


def install_attempt(rig, monkeypatch, mutation=None):
    anchor_ms = rig.clock.now_ms - 20000
    rig.fake.add_row(talker=PEER, content=ANCHOR_TEXT, seq=10, ts_ms=anchor_ms, is_self=True)
    original_send = rig.client.send
    captured = {}

    async def attempted(**kwargs):
        now = rig.clock.now_ms / 1000
        proof = {"version": 1, "self_uid": "wxid_demo01", "native_pid": 4321,
                 "talker": PEER, "max_seq": 10,
                 "anchor": {"seq": 10, "time_s": anchor_ms / 1000, "text_sha256": digest(ANCHOR_TEXT)},
                 "captured_at_s": now, "send_started_at_s": now,
                 "action_completed_at_s": now, "text_sha256": digest(TEXT), "idempotency_key": KEY}
        if mutation:
            mutation(proof)
        captured.update(deepcopy(proof))
        rig.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                               "confirm_ms": 100, "idempotency_key": KEY,
                               "confirmation_proof": proof}
        return await original_send(**kwargs)

    monkeypatch.setattr(rig.client, "send", attempted)
    return captured, anchor_ms


def native_late_row(rig, **changes):
    return rig.fake.add_row(**{"talker": PEER, "content": TEXT, "seq": 11,
                               "ts_ms": rig.clock.now_ms, "is_self": True, **changes})


async def test_original_proof_confirms_within_existing_extra_window_without_resend(tmp_path, monkeypatch):
    rig = make_wechat_rig(tmp_path, cfg=config(), clock=Clock(auto_step_ms=1))
    capture, _ = install_attempt(rig, monkeypatch)
    bus = rig.make_bus()
    original_poll = rig.adapter.poll
    polls = []

    async def late_poll(account, only_sessions=None):
        if not polls:
            native_late_row(rig)
        polls.append(only_sessions)
        return await original_poll(account, only_sessions=only_sessions)

    monkeypatch.setattr(rig.adapter, "poll", late_poll)
    try:
        result = await bus.submit(command())
        assert result.ok and result.code == "DELIVERED"
        assert peer_result(result)["status"] == "sent"
        record = rig.store.settings_get("wechat.readback." + result.trace_id)
        assert record["version"] == 2 and record["proof"] == capture
        assert TEXT not in json.dumps(record) and ANCHOR_TEXT not in json.dumps(record)
        assert rig.store.confirmed_outbound_for_trace(result.trace_id)["ext_msg_id"] == "p:11"
        assert len(rig.fake.sent) == 1
        replay = await bus.submit(command())
        assert peer_result(replay)["status"] == "sent" and len(rig.fake.sent) == 1
    finally:
        await bus.close()
        rig.store.close()


@pytest.mark.parametrize("boundary", ["valid", "uid", "pid", "epoch", "anchor-missing",
    "anchor-time", "anchor-body", "duplicate-new", "old-sequence", "incoming", "wrong-peer",
    "wrong-body", "old-time", "future-time", "expired"])
async def test_original_proof_survives_restart_but_invalid_evidence_stays_unknown(tmp_path, monkeypatch, boundary):
    cfg = config()
    clock = Clock(auto_step_ms=2)
    rig = make_wechat_rig(tmp_path, cfg=cfg, clock=clock)
    captured, anchor_ms = install_attempt(rig, monkeypatch)
    if boundary == "old-sequence":
        # Old history belongs before the persisted ingestion cursor, not a late new arrival.
        rig.fake.add_row(talker=PEER, content=TEXT, seq=9, ts_ms=anchor_ms - 1000, is_self=True)
        rig.fake.rows.sort(key=lambda row: row["seq"])
    bus = rig.make_bus()
    try:
        result = await bus.submit(command())
        assert peer_result(result)["status"] == "unknown"
        assert len(rig.fake.sent) == 1
        persisted = rig.store.settings_get("wechat.readback." + result.trace_id)
        assert persisted and persisted["version"] == 2 and persisted["proof"] == captured
    finally:
        await bus.close()
        rig.store.close()

    restored = make_wechat_rig(tmp_path, cfg=cfg, clock=clock)
    try:
        if boundary != "anchor-missing":
            restored.fake.add_row(talker=PEER,
                content="Changed anchor" if boundary == "anchor-body" else ANCHOR_TEXT,
                seq=10, ts_ms=anchor_ms + (1000 if boundary == "anchor-time" else 0), is_self=True)
        if boundary == "uid":
            restored.store.con.execute("UPDATE accounts SET self_uid='wxid_changed' WHERE id='wx01'")
            restored.store.con.commit()
            restored.fake.wechat["wxid"] = "wxid_changed"
        elif boundary == "pid":
            restored.fake.wechat["pid"] = 4322
        elif boundary == "epoch":
            restored.store.settings_set("relay.identity.wx01", {"epoch": 1, "uid": "wxid_demo01"})
        elif boundary == "expired":
            clock.advance(12 * 60 * 60 * 1000 + 1000)
        changes = {}
        if boundary == "old-sequence": changes["seq"] = 9
        elif boundary == "incoming": changes["is_self"] = False
        elif boundary == "wrong-peer": changes["talker"] = "other"
        elif boundary == "wrong-body": changes["content"] = TEXT + " suffix"
        elif boundary == "old-time": changes["ts_ms"] = anchor_ms
        elif boundary == "future-time": changes["ts_ms"] = clock.now_ms + 120000
        native_late_row(restored, **changes)
        if boundary == "duplicate-new":
            native_late_row(restored, seq=12)
        await restored.adapter.poll(restored.account())
        confirmed = restored.store.confirmed_outbound_for_trace(result.trace_id)
        assert bool(confirmed) is (boundary == "valid")
        assert not restored.fake.sent
        assert restored.store.idem_get("wx01", KEY) is not None
        if boundary == "valid":
            assert confirmed["ext_msg_id"] == "p:11"
            restored_bus = restored.make_bus()
            original_result = restored.store.get_command_result(result.trace_id)
            try:
                assert peer_result(await restored_bus.submit(command()))["status"] == "sent"
            finally:
                await restored_bus.close()
            assert restored.store.get_command_result(result.trace_id) == original_result
            assert not restored.fake.sent
        elif boundary not in {"uid", "epoch", "wrong-peer", "future-time", "expired"}:
            # A rejected attribution must not discard the separate native message.
            native = restored.store.con.execute("SELECT * FROM messages WHERE ext_msg_id=?",
                ("p:9" if boundary == "old-sequence" else "p:11",)).fetchone()
            assert native and native["trace_id"] is None
        if boundary in {"duplicate-new", "anchor-time", "anchor-body"}:
            invalidated = restored.store.settings_get("wechat.readback." + result.trace_id)
            assert invalidated["status"] == "invalidated" and invalidated["proof"] == captured
            restored.fake.rows[:] = [row for row in restored.fake.rows if row["seq"] != 12]
            restored.fake.rows[0].update(content=ANCHOR_TEXT, time=anchor_ms)
            await restored.adapter.poll(restored.account())
            assert restored.store.confirmed_outbound_for_trace(result.trace_id) is None, (
                "Removing conflicting evidence later must not revive an invalidated original proof")
    finally:
        restored.store.close()


INVALID_PROOF = [
    ("version", 2), ("version", True), ("idempotency_key", "another-attempt"),
    ("self_uid", "wxid_other"), ("native_pid", 9999), ("talker", "other"),
    ("text_sha256", "0" * 64), ("captured_at_s", None), ("captured_at_s", True),
    ("captured_at_s", float("nan")), ("send_started_at_s", float("inf")),
    ("action_completed_at_s", -1), ("max_seq", -1), ("max_seq", True),
    ("max_seq", "10"), ("anchor", None),
    ("captured_at_s", 1.0), ("send_started_at_s", 1.0),
    ("action_completed_at_s", 9_999_999_999.0),
    ("anchor", {"seq": 9, "time_s": 1_758_239_980.0, "text_sha256": digest(ANCHOR_TEXT)}),
    ("anchor", {"seq": 10, "time_s": float("inf"), "text_sha256": digest(ANCHOR_TEXT)}),
    ("anchor", {"seq": 10, "time_s": 1_758_239_980.0, "text_sha256": "invalid"}),
]


@pytest.mark.parametrize("field,value", INVALID_PROOF)
async def test_malformed_proof_never_allows_same_text_attribution(tmp_path, monkeypatch, field, value):
    rig = make_wechat_rig(tmp_path, cfg=config(), clock=Clock(auto_step_ms=2))
    install_attempt(rig, monkeypatch, lambda proof: proof.update({field: value}))
    bus = rig.make_bus()
    try:
        result = await bus.submit(command())
        native_late_row(rig)
        await rig.adapter.poll(rig.account())
        assert peer_result(result)["status"] == "unknown"
        assert rig.store.confirmed_outbound_for_trace(result.trace_id) is None
        assert rig.store.idem_get("wx01", KEY) is not None and len(rig.fake.sent) == 1
        observed = rig.store.con.execute("SELECT * FROM messages WHERE ext_msg_id='p:11'").fetchone()
        assert observed and observed["trace_id"] is None
    finally:
        await bus.close()
        rig.store.close()


async def test_legacy_attempt_without_proof_never_claims_later_exact_text(tmp_path, monkeypatch):
    rig = make_wechat_rig(tmp_path, cfg=config(), clock=Clock(auto_step_ms=2))
    rig.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                            "confirm_ms": 100, "idempotency_key": KEY}
    bus = rig.make_bus()
    try:
        result = await bus.submit(command())
        native_late_row(rig)
        await rig.adapter.poll(rig.account())
        assert peer_result(result)["status"] == "unknown"
        assert rig.store.settings_get("wechat.readback." + result.trace_id) is None
        assert rig.store.confirmed_outbound_for_trace(result.trace_id) is None
        assert (await bus.submit(command())).code == "IDEMPOTENT_REPLAY"
        assert len(rig.fake.sent) == 1 and rig.store.idem_get("wx01", KEY) is not None
        observed = rig.store.con.execute("SELECT * FROM messages WHERE ext_msg_id='p:11'").fetchone()
        assert observed and observed["trace_id"] is None
    finally:
        await bus.close()
        rig.store.close()


@pytest.mark.parametrize("case", ["short-pages", "later-duplicate", "never-empty", "read-error"])
async def test_late_confirmation_requires_complete_bounded_snapshot(tmp_path, monkeypatch, case):
    rig = make_wechat_rig(tmp_path, cfg=config(), clock=Clock(auto_step_ms=2))
    captured, anchor_ms = install_attempt(rig, monkeypatch)
    bus = rig.make_bus()
    try:
        result = await bus.submit(command())
        assert rig.store.settings_get("wechat.readback." + result.trace_id)
        rows = [dict(rig.fake.rows[0]),
                {"talker": PEER, "seq": 11, "time": captured["action_completed_at_s"],
                 "content": TEXT, "type": 1, "isSelf": True}]
        if case == "later-duplicate":
            rows.append({**rows[-1], "seq": 12})
        calls = []

        async def paged_read(*, talker=None, since_seq=None, limit=500):
            calls.append((talker, since_seq, limit))
            if case == "read-error":
                raise OSError("Synthetic read failure")
            after = since_seq or 0
            page = [row for row in rows if row["seq"] > after][:1]
            if not page and case == "never-empty":
                page = [{"talker": PEER, "seq": after + 1, "time": anchor_ms,
                         "content": "Unrelated synthetic continuation", "type": 1, "isSelf": False}]
            return page

        async def ordinary_poll(*args, **kwargs):
            return None

        # Isolate the independent ordinary ingestion loop; exercise the actual pending scheduler.
        monkeypatch.setattr(rig.poller, "poll", ordinary_poll)
        monkeypatch.setattr(rig.client, "read", paged_read)
        await rig.adapter.poll(rig.account())
        confirmed = rig.store.confirmed_outbound_for_trace(result.trace_id)
        assert bool(confirmed) is (case == "short-pages")
        assert 1 <= len(calls) <= 4, "Late proof reads must remain bounded"
        if case == "short-pages":
            assert len(calls) == 3, "A short page does not prove the native snapshot is complete"
        assert len(rig.fake.sent) == 1 and rig.store.idem_get("wx01", KEY) is not None
    finally:
        await bus.close()
        rig.store.close()


async def pending_pair(tmp_path, monkeypatch, version=2):
    """Two independent real Store connections; no live or persistent application data."""
    rig = make_wechat_rig(tmp_path, cfg=config(), clock=Clock(auto_step_ms=1))
    install_attempt(rig, monkeypatch)
    bus = rig.make_bus()
    try:
        result = await bus.submit(command())
    finally:
        await bus.close()
    key = "wechat.readback." + result.trace_id
    record = rig.store.settings_get(key)
    assert record and record["status"] == "pending"
    if version == 1:
        record = {name: value for name, value in record.items() if name != "proof"}
        record.update(version=1, ext="p:11")
        rig.store.settings_set(key, record)
    native_late_row(rig)
    await rig.poller.poll(rig.adapter._view(rig.account()))
    other = make_wechat_rig(tmp_path, cfg=rig.cfg, clock=rig.clock)
    other.fake.rows[:] = deepcopy(rig.fake.rows)
    return rig, other, result.trace_id, key, record


def assert_fenced_native_is_preserved(rig, trace, key, status="invalidated"):
    assert rig.store.confirmed_outbound_for_trace(trace) is None
    assert rig.store.settings_get(key)["status"] == status
    native = rig.store.con.execute("SELECT * FROM messages WHERE ext_msg_id='p:11'").fetchone()
    assert native and native["trace_id"] is None and native["state"] == "DELIVERED"
    assert len(rig.fake.sent) == 1 and rig.store.idem_get("wx01", KEY) is not None


@pytest.mark.parametrize("version", [1, 2])
async def test_persistent_invalidation_during_native_await_fences_stale_reader(tmp_path, monkeypatch, version):
    rig, other, trace, key, record = await pending_pair(tmp_path, monkeypatch, version)
    entered, release = asyncio.Event(), asyncio.Event()
    original_read = rig.client.read
    calls, resumed = [], []

    async def paused_read(**kwargs):
        rows = await original_read(**kwargs)
        if not calls:
            calls.append(kwargs)
            entered.set()
            await release.wait()
            resumed.append(True)
        return rows

    monkeypatch.setattr(rig.client, "read", paused_read)
    task = asyncio.create_task(rig.adapter._ingest_readback(rig.account(), trace, deepcopy(record)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if version == 2:
            other.fake.rows.append({**other.fake.rows[-1], "seq": 12})
            assert not await other.adapter._ingest_readback(other.account(), trace, deepcopy(record))
        else:
            assert other.store.settings_swap(key, json.dumps(record, ensure_ascii=False),
                {**record, "status": "invalidated", "reason": "synthetic_identity_fence"})
        assert other.store.settings_get(key)["status"] == "invalidated"
        release.set()
        assert not await asyncio.wait_for(task, 2), "A stale snapshot must never overwrite a committed fence"
        assert resumed == [True], "A read timeout must not accidentally make this race test pass"
        assert_fenced_native_is_preserved(rig, trace, key)
        assert not other.fake.sent
    finally:
        release.set()
        if not task.done():
            await asyncio.wait_for(task, 2)
        other.store.close()
        rig.store.close()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("replacement", ["invalidated", "different-pending-record"])
async def test_second_store_fence_between_adapter_check_and_bind_is_atomic(tmp_path, monkeypatch, version, replacement):
    rig, other, trace, key, record = await pending_pair(tmp_path, monkeypatch, version)
    original_bind = rig.store.bind_wechat_readback
    entered = []

    def fenced_bind(*args, **kwargs):
        entered.append(True)
        changed = ({**record, "status": "invalidated", "reason": "synthetic_cross_connection_fence"}
                   if replacement == "invalidated" else {**record, "started_ms": record["started_ms"] + 1})
        assert other.store.settings_swap(key, json.dumps(record, ensure_ascii=False),
            changed)
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(rig.store, "bind_wechat_readback", fenced_bind)
    try:
        assert not await rig.adapter._ingest_readback(rig.account(), trace, deepcopy(record))
        assert entered == [True], "The competing commit must happen after adapter validation"
        assert_fenced_native_is_preserved(rig, trace, key,
            status="invalidated" if replacement == "invalidated" else "pending")
    finally:
        other.store.close()
        rig.store.close()


async def test_confirmation_commit_failure_rolls_back_binding_and_preserves_native_fact(tmp_path, monkeypatch):
    rig, other, trace, key, record = await pending_pair(tmp_path, monkeypatch)
    other.store.con.execute("""CREATE TRIGGER synthetic_confirmation_failure
        BEFORE UPDATE ON settings
        WHEN json_extract(NEW.value_json, '$.status') = 'confirmed'
        BEGIN SELECT RAISE(ABORT, 'synthetic confirmation commit failure'); END""")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic confirmation commit failure"):
            await rig.adapter._ingest_readback(rig.account(), trace, deepcopy(record))
        assert_fenced_native_is_preserved(rig, trace, key, status="pending")
        assert other.store.settings_get(key) == record
        assert other.store.confirmed_outbound_for_trace(trace) is None
    finally:
        other.store.close()
        rig.store.close()


async def test_stale_invalidator_cannot_downgrade_other_connection_confirmation(tmp_path, monkeypatch):
    from qtrade_agent.adapters.wechat.normalize import to_message
    rig, other, trace, key, record = await pending_pair(tmp_path, monkeypatch)
    rig.fake.rows.append({**rig.fake.rows[-1], "seq": 12})
    armed, won = [], []
    original_read = rig.client.read
    original_get, original_swap = rig.store.settings_get, rig.store.settings_swap

    async def conflicting_read(**kwargs):
        rows = await original_read(**kwargs)
        armed.append(True)
        return rows

    def confirm_other_connection():
        if not armed or won:
            return
        msg = to_message("wx01", other.fake.rows[-1], self_uid="wxid_demo01")
        msg._native_account_uid = "wxid_demo01"
        assert other.store.bind_wechat_readback(trace, msg, now_ms=rig.clock())
        other.store.settings_set(key, {**record, "status": "confirmed"})
        won.append(True)

    def stale_get(name):
        observed = original_get(name)
        if name == key:
            confirm_other_connection()
        return observed

    def racing_swap(name, old_json, value, **kwargs):
        if name == key:
            confirm_other_connection()
        return original_swap(name, old_json, value, **kwargs)

    monkeypatch.setattr(rig.client, "read", conflicting_read)
    monkeypatch.setattr(rig.store, "settings_get", stale_get)
    monkeypatch.setattr(rig.store, "settings_swap", racing_swap)
    try:
        assert not await rig.adapter._ingest_readback(rig.account(), trace, deepcopy(record))
        assert won == [True]
        assert original_get(key)["status"] == "confirmed", "A stale invalidator cannot downgrade the winner"
        assert rig.store.confirmed_outbound_for_trace(trace)["ext_msg_id"] == "p:11"
        assert len(rig.fake.sent) == 1 and not other.fake.sent
    finally:
        other.store.close()
        rig.store.close()


@pytest.mark.parametrize("reason,late,expected", [
    ("confirm_timeout", True, "confirm_timeout"),
    ("confirm_timeout", False, "confirm_timeout"),
    ("read_failed", False, "read_failed"),
    ("untrusted synthetic diagnostic", False, None),
    ({"message": "synthetic raw payload"}, False, None),
])
async def test_native_diagnostic_survives_final_result_without_changing_outcome(tmp_path, monkeypatch, reason, late, expected):
    rig = make_wechat_rig(tmp_path, cfg=config(), clock=Clock(auto_step_ms=1))
    if late:
        install_attempt(rig, monkeypatch)
        original_send, original_poll = rig.client.send, rig.adapter.poll
        polled = []

        async def attempted(**kwargs):
            response = await original_send(**kwargs)
            return {**response, "confirmation_reason": reason}

        async def confirm_later(account, only_sessions=None):
            if not polled:
                native_late_row(rig)
            polled.append(True)
            return await original_poll(account, only_sessions=only_sessions)

        monkeypatch.setattr(rig.client, "send", attempted)
        monkeypatch.setattr(rig.adapter, "poll", confirm_later)
    else:
        rig.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
            "confirm_ms": 100, "idempotency_key": KEY, "confirmation_reason": reason}
    bus = rig.make_bus()
    try:
        result = await bus.submit(command())
        assert peer_result(result)["status"] == ("sent" if late else "unknown")
        stored = rig.store.get_command_result(result.trace_id)
        for data in (result.data, stored["data"]):
            assert data.get("native_confirmation_reason") == expected
            if expected is None:
                assert "native_confirmation_reason" not in data
            assert "untrusted synthetic diagnostic" not in json.dumps(data)
            assert "synthetic raw payload" not in json.dumps(data)
        assert len(rig.fake.sent) == 1
    finally:
        await bus.close()
        rig.store.close()
