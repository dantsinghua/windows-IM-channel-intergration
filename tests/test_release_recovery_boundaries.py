"""Release boundaries: fake transports only; no live accounts or services."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests.conftest import Clock, make_rig
from tests.test_wechat_rig import make_wechat_rig
from qtrade_agent.config import AgentConfig, HealthConfig
from qtrade_agent.adapters.wechat.poll import WechatAccountView
from qtrade_agent.adapters.wechat.client import WeChatCallFailed, WeChatWinAgent
from qtrade_agent.models import Command

async def qidian(tmp_path, *, enabled=True, recover=True):
    cfg=replace(AgentConfig(), health=replace(HealthConfig(), adb_selfheal_enabled=enabled))
    rig=make_rig(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=0))
    row=rig.store.create_account(channel="qidian",label="synthetic",login_mode="password",quota_mb=2560)
    aid=row["id"]
    await rig.agent.accounts.start(aid,actor="test")
    await rig.agent.accounts.wait_idle(aid)
    rig.agent.accounts.transition(aid,"running")
    rig.store.patch_account(aid,auto_recover=recover)
    rig.clock.advance(20_000)
    rig.adb.calls.clear()
    return rig,aid

async def failed_rounds(rig, count, seconds=61):
    for _ in range(count):
        rig.adb.state["127.0.0.1:16001"]="offline"
        await rig.agent.healthloop.check_adb()
        rig.clock.advance(seconds*1000)

@pytest.mark.parametrize("enabled,recover",[(False,True),(True,False)])
async def test_shared_recovery_respects_switches(tmp_path,enabled,recover):
    rig,aid=await qidian(tmp_path,enabled=enabled,recover=recover)
    try:
        await failed_rounds(rig,8)
        assert not any(op=="kill_server" for op,_ in rig.adb.calls)
    finally: rig.store.close()

async def test_shared_budget_is_global_persisted_and_bounded(tmp_path):
    rig,aid=await qidian(tmp_path)
    try:
        await failed_rounds(rig,10)
        assert sum(op=="kill_server" for op,_ in rig.adb.calls)==3
        from qtrade_agent.healthloop import HealthLoop
        rig.agent.healthloop=HealthLoop(store=rig.store,runtime=rig.agent.runtime,accounts=rig.agent.accounts,alerts=rig.agent.alerts,health=rig.agent.health,cfg=rig.agent.cfg,clock=rig.clock)
        await failed_rounds(rig,5)
        assert sum(op=="kill_server" for op,_ in rig.adb.calls)==3
        rig.clock.advance(3600_000)
        await failed_rounds(rig,1)
        assert sum(op=="kill_server" for op,_ in rig.adb.calls)==4
    finally: rig.store.close()

async def test_unknown_adb_serial_prevents_shared_restart(tmp_path):
    rig,aid=await qidian(tmp_path)
    try:
        rig.adb.state["usb-foreign-device"]="device"
        await failed_rounds(rig,6)
        assert not any(op=="kill_server" for op,_ in rig.adb.calls)
    finally: rig.store.close()

async def test_failed_kill_consumes_budget_and_never_reconnects(tmp_path):
    rig,aid=await qidian(tmp_path)
    try:
        kill=AsyncMock(side_effect=RuntimeError("adb_kill_failed"))
        rig.adb.kill_server=kill
        await failed_rounds(rig,9)
        assert kill.await_count==3
        assert not any(op=="reconnect_all" for op,_ in rig.adb.calls)
    finally: rig.store.close()

@pytest.mark.parametrize("value",[-1,True,1001,"3"])
def test_bad_recovery_limit_rejected(value):
    with pytest.raises(ValueError): HealthConfig(adb_selfheal_max_per_hour=value)

@pytest.mark.parametrize("method,budget",[("status",15),("login_status",15),("login_cancel",15)])
async def test_message_scan_budget_does_not_expand_state_calls(method,budget):
    request=AsyncMock(return_value=(200,{}))
    await getattr(WeChatWinAgent(SimpleNamespace(request=request)),method)()
    assert request.call_args.kwargs["timeout_s"]==budget
    assert request.call_args.kwargs["retry"] is False

def wxrig(tmp_path):
    rig=make_wechat_rig(tmp_path,clock=Clock(auto_step_ms=0))
    rig.store.set_desired_state("wx01","running")
    return rig,WechatAccountView("wx01","running","wxid_demo01")

async def test_read_retries_backoff_stop_and_keep_real_reason(tmp_path):
    rig,acct=wxrig(tmp_path)
    try:
        request=AsyncMock(side_effect=WeChatCallFailed(504,{"error":{"reason":"user_handler_timeout","message":"synthetic-secret"}},"/wa/v1/wechat/read"))
        rig.client.read=request
        delays=[]
        for _ in range(10):
            await rig.poller.poll(acct)
            st=rig.poller.st(acct.id)
            delays.append(st.next_retry_ms-rig.clock())
            rig.clock.advance(max(1,delays[-1]))
        assert delays[:5]==[1000,2000,4000,8000,16000]
        assert st.retry_exhausted is True
        assert st.last_error=="user_handler_timeout"
        for _ in range(10):
            rig.clock.advance(60000)
            await rig.poller.poll(acct)
        assert request.await_count==10
        assert rig.poller.account_seq(acct.id)==0
    finally: rig.store.close()

async def test_read_elapsed_limit_stops_before_next_call(tmp_path):
    rig,acct=wxrig(tmp_path)
    try:
        request=AsyncMock(side_effect=WeChatCallFailed(504,{"error":{"reason":"wechat_read_catching_up"}},"/wa/v1/wechat/read"))
        rig.client.read=request
        await rig.poller.poll(acct)
        rig.clock.advance(601_000)
        await rig.poller.poll(acct)
        assert request.await_count==1
        assert rig.poller.st(acct.id).retry_exhausted
    finally: rig.store.close()

async def test_read_success_clears_retry_episode(tmp_path):
    rig,acct=wxrig(tmp_path)
    try:
        rig.client.read=AsyncMock(side_effect=[WeChatCallFailed(504,{"error":{"reason":"wechat_read_timeout"}},"read"),[]])
        await rig.poller.poll(acct)
        await rig.poller.poll(acct)
        assert rig.client.read.await_count==1
        rig.clock.advance(1000)
        await rig.poller.poll(acct)
        st=rig.poller.st(acct.id)
        assert st.retry_attempts==0 and not st.retry_exhausted
        assert st.last_error is None
    finally: rig.store.close()

async def test_manual_read_recovery_does_not_restart_key_flow(tmp_path):
    rig,acct=wxrig(tmp_path)
    try:
        rig.poller.st(acct.id).read_fail_streak=3
        state,code,reason=await rig.adapter.probe_state(rig.account())
        assert (state,code)==("degraded","WECHAT_READ_FAILED")
        assert not rig.adapter.key_retry_required(acct.id)
        account=rig.account(); account.state="degraded";account.state_code="WECHAT_READ_FAILED"
        assert rig.adapter.write_guard(account) is not None
        assert rig.adapter.read_guard(account) is None
    finally: rig.store.close()

async def test_wechat_read_opt_out_stops_retry(tmp_path):
    rig,acct=wxrig(tmp_path)
    try:
        rig.store.patch_account(acct.id,auto_recover=False)
        rig.client.read=AsyncMock(side_effect=WeChatCallFailed(504,{},"read"))
        await rig.poller.poll(acct)
        rig.clock.advance(60_000)
        await rig.poller.poll(acct)
        assert rig.client.read.await_count==1
        assert rig.poller.st(acct.id).retry_exhausted
    finally: rig.store.close()

@pytest.mark.parametrize("state,enabled,desired,code",[("stopped",True,"stopped",None),("disabled",False,"stopped",None),("error",True,"running","CONTAINER_EXIT"),("login_required",True,"running","WAIT_QRCODE"),("degraded",True,"running","UI_UNEXPECTED")])
async def test_qq_recovery_does_not_override_other_states(tmp_path,state,enabled,desired,code):
    rig=make_rig(tmp_path)
    try:
        rig.store.ensure_account("qq01","qq",state=state,self_uid="12345")
        rig.store.upsert_runtime("qq01",kind="napcat",desired_state=desired,now_ms=rig.clock())
        rig.store.transition("qq01",state,state_code=code,enabled=enabled,now_ms=rig.clock())
        await rig.agent._qq_recovered("qq01","RECOVERED")
        assert rig.store.get_account_full("qq01")["state"]==state
    finally: rig.store.close()

async def test_state_dedup_keeps_a_new_login_session(tmp_path):
    rig=make_rig(tmp_path)
    try:
        rig.store.ensure_account("qq01","qq",state="login_required")
        rig.agent.accounts.transition("qq01","login_required",login_session_id="new-session")
        assert rig.agent.accounts._current_ls.get("qq01")=="new-session"
    finally: rig.store.close()
