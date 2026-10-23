"""Only an active, user-created QR attempt may request a bounded container retry."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dataclasses import replace
import pytest
from tests.conftest import Clock, make_rig
from qtrade_agent.config import AgentConfig

def login_rig(tmp_path):
    rig=make_rig(tmp_path,clock=Clock(auto_step_ms=0))
    rig.store.ensure_account("qq01","qq",state="login_required",self_uid="12345")
    rig.store.upsert_runtime("qq01",kind="napcat",desired_state="running",now_ms=rig.clock())
    a=rig.agent.accounts
    a.transition("qq01","login_required",state_code="WAIT_QRCODE",login_session_id="synthetic-session")
    attempt={"session":"synthetic-session","deadline":rig.clock()+1_800_000,"refresh_due":0,"lock":asyncio.Lock()}
    a._qq_attempts["qq01"]=attempt
    a._qq_login_backend=SimpleNamespace(qrcode=AsyncMock(side_effect=RuntimeError("synthetic")))
    rig.agent.runtime.restart_napcat_qr=AsyncMock(return_value=True)
    return rig,a,attempt

async def test_only_tenth_failed_qr_can_request_restart(tmp_path):
    rig,a,attempt=login_rig(tmp_path)
    try:
        for _ in range(9):
            await a._qq_refresh("qq01",attempt,refresh=True)
            rig.clock.advance(30_000)
        rig.agent.runtime.restart_napcat_qr.assert_not_awaited()
        await a._qq_refresh("qq01",attempt,refresh=True)
        assert rig.agent.runtime.restart_napcat_qr.await_count==1
        assert rig.store.get_account_full("qq01")["state"]=="login_required"
        assert a._current_ls["qq01"]=="synthetic-session"
    finally: rig.store.close()

async def test_qr_opt_out_never_restarts(tmp_path):
    rig,a,attempt=login_rig(tmp_path)
    try:
        rig.store.patch_account("qq01",auto_recover=False)
        for _ in range(12):
            await a._qq_refresh("qq01",attempt,refresh=True)
            rig.clock.advance(30_000)
        rig.agent.runtime.restart_napcat_qr.assert_not_awaited()
    finally: rig.store.close()

async def test_runtime_qr_restart_rechecks_stop_intent_and_keeps_data(tmp_path):
    rig=make_rig(tmp_path)
    try:
        row=rig.store.create_account(channel="qq",label="synthetic",login_mode="qrcode",quota_mb=1200)
        await rig.agent.runtime.start(row)
        row=rig.store.get_account_full(row["id"])
        intended=[True]
        original=rig.containers.stop
        async def stop(name,timeout_s):
            await original(name,timeout_s=timeout_s)
            intended[0]=False
        rig.containers.stop=stop
        rig.containers.calls.clear()
        assert await rig.agent.runtime.restart_napcat_qr(row,lambda:intended[0]) is False
        assert not any(c[0] in ("start","remove","create") for c in rig.containers.calls)
    finally: rig.store.close()

async def test_manual_read_restart_does_not_run_login_hook(tmp_path):
    rig=make_rig(tmp_path)
    try:
        rig.store.ensure_account("wx01","wechat",state="degraded",self_uid="wxid_synthetic")
        rig.store.transition("wx01","degraded",state_code="WECHAT_READ_FAILED",desired_state="running",now_ms=rig.clock())
        ad=rig.agent.adapters["wechat"]
        ad.retry_read=AsyncMock()
        ad.probe_state=AsyncMock(return_value=("running",None,""))
        rig.agent.accounts._wechat_start=AsyncMock(side_effect=AssertionError("must not start key hook"))
        await rig.agent.accounts.restart("wx01",actor="test")
        await rig.agent.accounts.wait_idle("wx01")
        ad.retry_read.assert_awaited_once()
        rig.agent.accounts._wechat_start.assert_not_awaited()
        assert rig.store.get_account_full("wx01")["state"]=="running"
    finally: rig.store.close()
