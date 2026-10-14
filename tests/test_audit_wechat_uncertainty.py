"""An unconfirmed external side effect must not be automatically sent again."""
import time
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from qtrade_agent.bus.bus import Bus
from qtrade_agent.models import Command,CommandResult

@pytest.mark.asyncio
async def test_wechat_confirmation_timeout_is_nonretryable_and_same_key_is_preserved(rig3,monkeypatch):
    s=rig3.store; s.ensure_account("wx01","wechat",state="running",login_mode="qrcode",self_uid="wxid_synthetic")
    cfg=replace(rig3.agent.cfg,bus=replace(rig3.agent.cfg.bus,confirm_timeout_wechat_ms=3,send_min_interval_ms=0,send_rand_extra_ms=0),wechat_adapter=replace(rig3.agent.cfg.wechat_adapter,confirm_poll_interval_ms=1))
    adapter=rig3.agent.adapters["wechat"]
    send=AsyncMock(return_value=CommandResult(ok=True,code="OK",trace_id="",source="chatlog"))
    monkeypatch.setattr(adapter,"send",send); monkeypatch.setattr(adapter,"write_guard",lambda _:None); monkeypatch.setattr(adapter,"poll",AsyncMock())
    bus=Bus(store=s,events=rig3.agent.events,adapters={"wechat":adapter},cfg=cfg,clock=lambda:int(time.monotonic()*1000))
    try:
        command=lambda:Command("wx01","send_text",{"session":"wxid_peer","text":"synthetic"},idempotency_key="wx-outcome-audit",timeout_ms=1000)
        first=await bus.submit(command())
        assert not first.ok and first.code=="SEND_FAILED"
        assert first.error is not None and first.error.retryable is False and first.error.needs_human is True
        assert first.error.reason=="send_outcome_unknown"
        second=await bus.submit(command())
        assert second.code=="IDEMPOTENT_REPLAY" and second.ok is False
        assert send.await_count==1
        assert s.idem_get("wx01","wx-outcome-audit")["status"]=="DONE"
    finally: await bus.close()
