"""Validate OS-authenticated credentials without opening a Windows named pipe."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent.console_pipe import ConsoleTokenPipe
from qtrade_winagent.svc import Tokens

SID = "S-1-5-21-1111-2222-3333-1001"


def make_pipe(*, peer=SID, elevated=False, op="issue_console_token", endpoint="http://172.20.16.2:17600"):
    tokens = Tokens(agent="AGENT_ONLY", console="CONSOLE_ONLY", installer="INSTALLER_ONLY")
    provider = AsyncMock(return_value=endpoint)
    pipe = ConsoleTokenPipe(None, pipe_name="fixture", install_user_sid=SID, tokens=tokens,
                            endpoint_provider=provider, clock=lambda: 1000)
    conn = SimpleNamespace(peer_sid=peer, peer_elevated=elevated,
                           recv_json=AsyncMock(return_value={"op": op, "user_sid": SID}),
                           send_json=AsyncMock(), close=AsyncMock())
    return pipe, conn, provider


async def test_console_gets_only_console_credentials_and_current_guest_address():
    pipe, conn, provider = make_pipe()
    await pipe.handle(conn)
    reply = conn.send_json.call_args.args[0]
    assert reply == {"agent_token": "CONSOLE_ONLY", "winagent_token": "CONSOLE_ONLY",
                     "expires_at": 3601000, "agent_base_url": "http://172.20.16.2:17600"}
    provider.assert_awaited_once()
    conn.close.assert_awaited_once()


@pytest.mark.parametrize("peer", ["", "S-1-5-18", "S-1-5-21-1111-2222-3333-2002"])
async def test_claimed_sid_in_json_cannot_override_os_pipe_peer(peer):
    pipe, conn, provider = make_pipe(peer=peer)
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {"error": "FORBIDDEN"}
    conn.recv_json.assert_not_awaited()
    provider.assert_not_awaited()


async def test_bootstrap_requires_os_elevation_even_for_correct_user():
    pipe, conn, provider = make_pipe(op="issue_bootstrap", elevated=False)
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {"error": "FORBIDDEN"}
    provider.assert_not_awaited()


async def test_elevated_bootstrap_returns_the_two_intended_tokens_only():
    pipe, conn, provider = make_pipe(op="issue_bootstrap", elevated=True)
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {"console_token": "CONSOLE_ONLY", "winagent_token": "AGENT_ONLY"}
    provider.assert_not_awaited()


async def test_missing_guest_endpoint_does_not_issue_a_token_for_a_guessed_address():
    pipe, conn, _ = make_pipe(endpoint=None)
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {
        "agent_token": None, "agent_base_url": None,
        "winagent_token": "CONSOLE_ONLY", "expires_at": 3601000,
    }
    conn.close.assert_awaited_once()


@pytest.mark.parametrize("failure", [TimeoutError("discovery timed out"), OSError("guest offline")])
async def test_guest_discovery_failure_preserves_local_recovery_without_agent_credentials(failure):
    pipe, conn, provider = make_pipe()
    provider.side_effect = failure
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {
        "agent_token": None, "agent_base_url": None,
        "winagent_token": "CONSOLE_ONLY", "expires_at": 3601000,
    }
    conn.close.assert_awaited_once()


@pytest.mark.parametrize("endpoint", [None, "http://172.20.16.2:17600"])
async def test_missing_console_credential_cannot_be_replaced_by_privileged_agent_token(endpoint):
    pipe, conn, _ = make_pipe(endpoint=endpoint)
    pipe._tokens.console = ""
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {"error": "NOT_READY"}


async def test_shutdown_cancellation_closes_pipe_without_issuing_recovery_credentials():
    pipe, conn, provider = make_pipe()
    provider.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await pipe.handle(conn)
    conn.send_json.assert_not_awaited()
    conn.close.assert_awaited_once()


async def test_guest_recovery_returns_only_the_newly_discovered_address():
    pipe, conn, provider = make_pipe(endpoint=None)
    await pipe.handle(conn)
    first = conn.send_json.call_args.args[0]
    provider.return_value = "http://172.25.32.2:17600"
    await pipe.handle(conn)
    assert first["agent_base_url"] is None
    assert first["agent_token"] is None
    assert conn.send_json.call_args.args[0] == {
        "agent_token": "CONSOLE_ONLY", "agent_base_url": "http://172.25.32.2:17600",
        "winagent_token": "CONSOLE_ONLY", "expires_at": 3601000,
    }


async def test_unknown_operation_returns_no_credentials_and_closes_connection():
    pipe, conn, _ = make_pipe(op="execute_shell")
    await pipe.handle(conn)
    assert conn.send_json.call_args.args[0] == {"error": "INVALID_ARGS"}
    conn.close.assert_awaited_once()
