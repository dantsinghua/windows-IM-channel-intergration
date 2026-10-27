"""A trusted native read timeout waits for real readback, never sends twice."""
from dataclasses import replace

import pytest

from qtrade_agent.adapters.wechat.client import WeChatNotReady, WeChatCallFailed

from qtrade_agent.config import AgentConfig
from qtrade_agent.models import Command
from tests.test_wechat_rig import make_wechat_rig


TEXT = "QA synthetic delayed confirmation"
KEY = "synthetic-delayed-confirmation"


async def test_completed_native_attempt_can_confirm_from_later_exact_poll_without_resend(tmp_path, monkeypatch):
    cfg = AgentConfig()
    cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=10000,
                                  send_min_interval_ms=0, send_rand_extra_ms=0),
                  wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))
    rig = make_wechat_rig(tmp_path, cfg=cfg)
    bus = rig.make_bus()
    rig.fake.send_result = {"ok": True, "code": "DELIVERED", "ext_msg_id": "filehelper:17",
                            "confirm_ms": 10000, "idempotency_key": KEY}
    original_send, original_poll = rig.client.send, rig.adapter.poll
    polls = []

    async def attempted(**kwargs):
        rig.clock.advance(18974)
        return await original_send(**kwargs)

    async def readback(account, only_sessions=None):
        polls.append(only_sessions)
        if len(polls) == 1:
            pending = rig.store.con.execute("SELECT * FROM messages WHERE trace_id IS NOT NULL").fetchone()
            assert pending["state"] == "SENDING" and pending["ext_msg_id"] is None
            assert rig.store.get_command_result(pending["trace_id"]) is None
            rig.clock.advance(4253)
            rig.fake.add_row(talker="filehelper", content=TEXT, is_self=True,
                             seq=17, ts_ms=rig.clock())
        return await original_poll(account, only_sessions=only_sessions)

    monkeypatch.setattr(rig.client, "send", attempted)
    monkeypatch.setattr(rig.adapter, "poll", readback)
    try:
        result = await bus.submit(Command("wx01", "send_text", {"session": "wx01:filehelper", "text": TEXT},
                                          idempotency_key=KEY, timeout_ms=60000))
        assert result.ok and result.code == "DELIVERED"
        confirmed = rig.store.confirmed_outbound_for_trace(result.trace_id)
        assert confirmed["ext_msg_id"] == "filehelper:17" and confirmed["confirmed_by"] == "chatlog"
        assert len(rig.fake.sent) == 1 and rig.store.count_messages("wx01") == 1
        assert polls and result.cost_ms == 23227
    finally:
        await bus.close()
        rig.store.close()


@pytest.mark.parametrize("mismatch", ["none", "peer", "text", "incoming", "stale"])
async def test_pending_without_exact_current_native_proof_keeps_original_window_and_never_resends(tmp_path, monkeypatch, mismatch):
    cfg = AgentConfig()
    cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=10000,
                                  send_min_interval_ms=0, send_rand_extra_ms=0),
                  wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))
    rig = make_wechat_rig(tmp_path, cfg=cfg)
    bus = rig.make_bus()
    rig.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                            "confirm_ms": 10000, "idempotency_key": KEY}
    original_send, original_poll = rig.client.send, rig.adapter.poll
    polls = []
    start = rig.clock()

    async def attempted(**kwargs):
        rig.clock.advance(18974)
        return await original_send(**kwargs)

    async def readback(account, only_sessions=None):
        polls.append(only_sessions)
        if len(polls) == 1:
            rig.clock.advance(4253)
            if mismatch != "none":
                rig.fake.add_row(talker="another-peer" if mismatch == "peer" else "filehelper",
                    content="Other synthetic text" if mismatch == "text" else TEXT,
                    is_self=mismatch != "incoming", seq=17,
                    ts_ms=start - 120000 if mismatch == "stale" else rig.clock())
            await original_poll(account, only_sessions=only_sessions)
            rig.clock.advance(5748)
        return []

    monkeypatch.setattr(rig.client, "send", attempted)
    monkeypatch.setattr(rig.adapter, "poll", readback)
    try:
        command = Command("wx01", "send_text", {"session": "wx01:filehelper", "text": TEXT},
                          idempotency_key=KEY, timeout_ms=60000)
        result = await bus.submit(command)
        assert not result.ok and result.code == "SEND_FAILED"
        assert result.error.needs_human and not result.error.retryable
        assert result.error.reason == "send_outcome_unknown"
        assert polls and result.cost_ms == 28975
        assert rig.store.confirmed_outbound_for_trace(result.trace_id) is None
        row = rig.store.con.execute("SELECT * FROM messages WHERE trace_id=?", (result.trace_id,)).fetchone()
        assert row["state"] == "UNCONFIRMED" and row["ext_msg_id"] is None
        replay = await bus.submit(Command("wx01", "send_text", command.args,
                                         idempotency_key=KEY, timeout_ms=60000))
        assert replay.code == "IDEMPOTENT_REPLAY"
        assert len(rig.fake.sent) == 1
    finally:
        await bus.close()
        rig.store.close()


async def test_only_exact_trusted_timeout_response_enters_pending(tmp_path):
    cfg = AgentConfig()
    rig = make_wechat_rig(tmp_path, cfg=cfg)
    base = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
            "confirm_ms": 10000, "idempotency_key": KEY}
    try:
        for mutation in [{"ok": True}, {"ext_msg_id": "filehelper:17"}, {"confirm_ms": None},
                         {"confirm_ms": True}, {"confirm_ms": "10000"}, {"confirm_ms": 9999},
                         {"idempotency_key": "other-command"}]:
            rig.fake.send_result = {**base, **mutation}
            result = await rig.adapter.send(rig.account(), Command("wx01", "send_text",
                {"session": "wx01:filehelper", "text": TEXT}, idempotency_key=KEY))
            assert not result.ok and result.code == "SEND_FAILED"
        assert rig.store.count_messages("wx01") == 0
        assert len(rig.fake.sent) == 7
    finally:
        rig.store.close()


async def test_real_ui_identity_key_permission_refusals_are_not_pending(tmp_path, monkeypatch):
    failures = [WeChatNotReady("wechat_window_unverified"), WeChatNotReady("wechat_key_failed"),
                WeChatNotReady("wechat_identity_mismatch"),
                WeChatCallFailed(403, {"error": {"reason": "permission_denied"}}, "/wa/v1/wechat/send")]
    for index, failure in enumerate(failures):
        directory = tmp_path / str(index)
        directory.mkdir()
        rig = make_wechat_rig(directory)
        attempts = []
        async def refuse(**kwargs):
            attempts.append(kwargs)
            raise failure
        monkeypatch.setattr(rig.client, "send", refuse)
        try:
            result = await rig.adapter.send(rig.account(), Command("wx01", "send_text",
                {"session": "wx01:filehelper", "text": TEXT}, idempotency_key=KEY))
            assert not result.ok and len(attempts) == 1
            assert not result.data.get("native_id")
            assert not rig.fake.sent and rig.store.count_messages("wx01") == 0
        finally:
            rig.store.close()


@pytest.mark.parametrize("timing", ["spent65s", "missing", "future", "string", "bool"])
async def test_pending_cannot_outlive_original_submit_budget_or_use_untrusted_time(tmp_path, monkeypatch, timing):
    rig = make_wechat_rig(tmp_path)
    rig.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                            "confirm_ms": 10000, "idempotency_key": KEY}
    submitted = rig.clock()
    original_send = rig.client.send
    # Queueing and native UI together have consumed65s of the original60+10s.
    rig.clock.advance(50000 if timing == "spent65s" else 0)
    async def attempted(**kwargs):
        rig.clock.advance(15000 if timing == "spent65s" else 18974)
        return await original_send(**kwargs)
    monkeypatch.setattr(rig.client, "send", attempted)
    if timing == "missing": submitted = None
    elif timing == "future": submitted += 120000
    elif timing == "string": submitted = str(submitted)
    elif timing == "bool": submitted = True
    try:
        result = await rig.adapter.send(rig.account(), Command("wx01", "send_text",
            {"session": "wx01:filehelper", "text": TEXT}, idempotency_key=KEY,
            submitted_at_ms=submitted, timeout_ms=60000))
        assert not result.ok and result.code == "SEND_FAILED"
        assert result.error.reason == "send_outcome_unknown"
        assert result.error.needs_human and not result.error.retryable
        assert len(rig.fake.sent) == 1
        assert rig.store.count_messages("wx01") == 0
    finally:
        rig.store.close()
