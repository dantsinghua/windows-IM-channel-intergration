import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from qtrade_winagent.main_svc import sync_host_hint
from qtrade_winagent.errors import WaError

@pytest.mark.asyncio
async def test_expected_user_disconnect_does_not_abort_periodic_round():
    hub=SimpleNamespace(user_agent_online=True,call=AsyncMock(side_effect=WaError("NOT_READY","synthetic user agent offline")))
    net=SimpleNamespace(wsl_adapter=lambda:SimpleNamespace(ipv4="192.0.2.1"))
    probe=SimpleNamespace(_net=net,state=SimpleNamespace(host_ip=None),host_json=lambda:{},
                          agent_endpoint=AsyncMock(return_value=None),_networking_mode=None)
    await sync_host_hint(SimpleNamespace(hub=hub,netprobe=probe))
    hub.call.assert_awaited_once()

@pytest.mark.asyncio
async def test_unexpected_hint_failure_is_not_swallowed():
    hub=SimpleNamespace(user_agent_online=True,call=AsyncMock(side_effect=RuntimeError("synthetic unexpected failure")))
    net=SimpleNamespace(wsl_adapter=lambda:SimpleNamespace(ipv4="192.0.2.1"))
    probe=SimpleNamespace(_net=net,state=SimpleNamespace(host_ip=None),host_json=lambda:{},
                          agent_endpoint=AsyncMock(return_value=None),_networking_mode=None)
    with pytest.raises(RuntimeError):
        await sync_host_hint(SimpleNamespace(hub=hub,netprobe=probe))
