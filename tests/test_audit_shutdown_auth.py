import asyncio
from unittest.mock import AsyncMock
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from qtrade_agent.models import Command

@pytest.mark.parametrize("route",["/api/v1/events","/api/v1/accounts/qd01/stream"])
def test_every_websocket_enforces_bearer_source_policy(rig3,route):
    rig3.store.upsert_api_client(app_id="restricted",name="restricted",level="write",token="synthetic-restricted",ip_allow=["127.0.0.1/32"])
    with TestClient(rig3.agent.create_api(),client=("192.0.2.20",40001)) as c:
        with pytest.raises(WebSocketDisconnect) as error:
            with c.websocket_connect(route,headers={"Authorization":"Bearer synthetic-restricted"}) as ws:
                if route.endswith("events"): ws.send_json({"subscribe":{"accounts":["*"]}})
                ws.receive_json()
        assert error.value.code==4401

@pytest.mark.asyncio
async def test_bus_closure_settles_active_command(rig3,monkeypatch):
    rig3.store.ensure_account("qd01","qidian",state="running",self_uid="123456")
    entered=asyncio.Event()
    async def blocked(*args):
        entered.set(); await asyncio.Event().wait()
    monkeypatch.setattr(rig3.agent.bus.adapters["qidian"],"execute",blocked)
    task=asyncio.create_task(rig3.agent.bus.submit(Command("qd01","get_state",{},trace_id="audit-close")))
    await asyncio.wait_for(entered.wait(),1)
    await rig3.agent.bus.close(); await asyncio.sleep(0); await asyncio.sleep(0)
    try:
        assert task.done(), "active command result must not wait until its old timeout after close"
        assert rig3.store.get_command("audit-close")["status"] in ("failed","cancelled")
    finally:
        task.cancel(); await asyncio.gather(task,return_exceptions=True)

@pytest.mark.asyncio
async def test_consumer_exception_persists_terminal_result(rig3,monkeypatch):
    rig3.store.ensure_account("qd01","qidian",state="running",self_uid="123456")
    monkeypatch.setattr(rig3.agent.bus.adapters["qidian"],"execute",AsyncMock(side_effect=ValueError("synthetic failure")))
    result=await rig3.agent.bus.submit(Command("qd01","get_state",{},trace_id="audit-exception"))
    assert result.ok is False
    try:
        assert rig3.store.get_command("audit-exception")["status"]=="failed"
        assert rig3.store.get_command_result("audit-exception")["ok"]==0
    finally: await rig3.agent.bus.close()
