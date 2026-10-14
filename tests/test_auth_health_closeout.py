"""Synthetic regression: no real WinAgent, accounts, or credentials."""
import json
from unittest.mock import AsyncMock
import pytest
from qtrade_agent.config import WinAgentConfig
from qtrade_agent.winagent_client import WinAgentClient, WinAgentUnavailable
from qtrade_agent.alerts import Alerts
from tests.test_integration_wiring_common import make_rig, close_rig, H

@pytest.fixture
def rig(tmp_path):
 r=make_rig(tmp_path)
 yield r
 close_rig(r)

def events(rig,code):
 return [e["payload"] for e in rig.events_of("alert") if e["payload"]["code"]==code]

@pytest.mark.parametrize("status,reason",[(401,"unauthorized"),(403,"forbidden"),(500,"http_error")])
async def test_health_failure_retains_safe_reason_and_http_status(status,reason):
 tr=AsyncMock(return_value=(status,{},b"{\"secret\":\"MUST_NOT_LEAK\"}"))
 c=WinAgentClient(WinAgentConfig(),base_url="http://fake:17610",token="synthetic",transport=tr)
 assert await c.health() is None
 assert c.last_health_error==reason and c.last_health_status==status
 assert "MUST_NOT_LEAK" not in str(c.last_health_error)

async def test_missing_token_health_is_not_just_an_unknown_silent_none(tmp_path):
 tr=AsyncMock()
 c=WinAgentClient(WinAgentConfig(token_file=str(tmp_path/"absent")),base_url="http://fake:17610",transport=tr)
 assert await c.health() is None
 assert c.last_health_error=="no_token"
 tr.assert_not_awaited()

@pytest.mark.parametrize("body",[[],{}, {"user_agent":"false"},{"user_agent":None}])
async def test_malformed_health_is_not_a_boolean_online_state(body):
 tr=AsyncMock(return_value=(200,{},json.dumps(body).encode()))
 c=WinAgentClient(WinAgentConfig(),base_url="http://fake:17610",token="synthetic",transport=tr)
 assert await c.health() is None
 assert c.last_health_error=="invalid_response"

async def test_file_credential_changes_are_observed_without_process_restart(tmp_path):
 p=tmp_path/"credential"; p.write_text("before")
 tr=AsyncMock(return_value=(200,{},b"{}"))
 c=WinAgentClient(WinAgentConfig(token_file=str(p)),base_url="http://fake:17610",transport=tr)
 await c.request("GET","/wa/v1/health")
 replacement=tmp_path/"next"; replacement.write_text("after"); replacement.replace(p)
 await c.request("GET","/wa/v1/health")
 assert [x.args[2]["Authorization"] for x in tr.await_args_list]==["Bearer before","Bearer after"]

async def test_deleted_file_credential_does_not_continue_using_old_secret(tmp_path):
 p=tmp_path/"credential"; p.write_text("before")
 tr=AsyncMock(return_value=(200,{},b"{}"))
 c=WinAgentClient(WinAgentConfig(token_file=str(p)),base_url="http://fake:17610",transport=tr)
 await c.request("GET","/wa/v1/health"); p.unlink()
 with pytest.raises(WinAgentUnavailable): await c.request("GET","/wa/v1/health")
 assert tr.await_count==1

async def test_explicit_credential_does_not_fall_back_to_file(tmp_path):
 p=tmp_path/"credential"; p.write_text("other-role")
 tr=AsyncMock(return_value=(401,{},b"{}"))
 c=WinAgentClient(WinAgentConfig(token_file=str(p)),base_url="http://fake:17610",token="injected",transport=tr)
 await c.request("POST","/wa/v1/wechat/send",json={"text":"synthetic"},retry=False)
 assert tr.await_count==1 and tr.await_args.args[2]["Authorization"]=="Bearer injected"

async def test_unauthorized_health_invalidates_old_online_state_and_alerts_once(rig,monkeypatch):
 await rig.agent.winagent_probe()
 real=rig.agent.winagent._transport
 async def transport(method,url,*args):
  if url.endswith("/health"): return 401,{},b"{\"error\":\"MUST_NOT_LEAK\"}"
  return await real(method,url,*args)
 monkeypatch.setattr(rig.agent.winagent,"_transport",transport)
 for _ in range(3): await rig.agent.winagent_probe()
 assert rig.agent.health.winagent_online is True
 assert rig.agent.health.user_agent_online is None
 assert rig.agent.pool.windows_known is False
 fired=[p for p in events(rig,"WINAGENT_AUTH_FAILED") if p["state"]=="firing"]
 assert len(fired)==1 and fired[0]["evidence"]["http_status"]==401
 assert "MUST_NOT_LEAK" not in json.dumps(fired)
 h=rig.client.get("/api/v1/system/health",headers=H()).json()
 assert h["winagent"]["user_agent"] is None
 monkeypatch.setattr(rig.agent.winagent,"_transport",real)
 await rig.agent.winagent_probe()
 assert rig.agent.health.user_agent_online is True
 assert events(rig,"WINAGENT_AUTH_FAILED")[-1]["state"]=="resolved"

async def test_offline_user_generates_host_alert_once_and_recovery(rig,monkeypatch):
 base=await rig.agent.winagent.health()
 monkeypatch.setattr(rig.agent.winagent,"health",AsyncMock(return_value={**base,"user_agent":False}))
 for _ in range(3): await rig.agent.winagent_probe()
 assert [p["state"] for p in events(rig,"WINAGENT_USER_OFFLINE")]==["firing"]
 monkeypatch.setattr(rig.agent.winagent,"health",AsyncMock(return_value=base))
 for _ in range(3): await rig.agent.winagent_probe()
 assert [p["state"] for p in events(rig,"WINAGENT_USER_OFFLINE")]==["firing","resolved"]

async def test_auth_fault_survives_restart_until_authenticated_health_success(rig,monkeypatch):
 Alerts(rig.agent.events,clock=rig.clock).firing("WINAGENT_AUTH_FAILED",subject="host")
 real=rig.agent.winagent._transport
 async def transport(method,url,*args):
  if url.endswith("/health"): return 401,{},b"{}"
  return await real(method,url,*args)
 monkeypatch.setattr(rig.agent.winagent,"_transport",transport)
 await rig.agent.winagent_probe()
 assert "resolved" not in [p["state"] for p in events(rig,"WINAGENT_AUTH_FAILED")]
 monkeypatch.setattr(rig.agent.winagent,"_transport",real)
 for _ in range(3): await rig.agent.winagent_probe()
 assert [p["state"] for p in events(rig,"WINAGENT_AUTH_FAILED")].count("resolved")==1

async def test_unavailable_health_is_not_misreported_as_user_logout(rig,monkeypatch):
 monkeypatch.setattr(rig.agent.winagent,"health",AsyncMock(return_value=None))
 await rig.agent.winagent_probe()
 assert rig.agent.health.user_agent_online is None
 assert rig.agent.alerts.is_firing("WINAGENT_HEALTH_UNAVAILABLE","host")
 assert not rig.agent.alerts.is_firing("WINAGENT_USER_OFFLINE","host")

async def test_token_file_is_bounded_and_invalid_bytes_are_a_clean_failure(tmp_path):
 p=tmp_path/"credential"; tr=AsyncMock()
 c=WinAgentClient(WinAgentConfig(token_file=str(p)),base_url="http://fake:17610",transport=tr)
 for raw in (b"a"*5000,b"\xff\xfe"):
  p.write_bytes(raw)
  assert await c.health() is None
  assert c.last_health_error=="no_token"
 tr.assert_not_awaited()