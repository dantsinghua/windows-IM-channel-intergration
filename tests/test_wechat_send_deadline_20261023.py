"""HTTP send waits for the native service deadline, without write retries or I/O."""
from __future__ import annotations

import json
import urllib.error
from urllib.parse import urlsplit

import pytest

from qtrade_agent.adapters.wechat.client import WeChatCallFailed, WeChatNotReady, WeChatWinAgent
from qtrade_agent.config import WinAgentConfig
from qtrade_agent.winagent_client import WinAgentClient


class DeadlineTransport:
    def __init__(self, *, elapsed_s=0, failure=None, status=200, response=None):
        self.elapsed_s = elapsed_s
        self.failure = failure
        self.status = status
        self.response = response if response is not None else {
            "ok": True, "code": "DELIVERED", "ext_msg_id": "synthetic-peer:1", "confirm_ms": 16001,
        }
        self.calls = []

    async def __call__(self, method, url, headers, body, timeout_s):
        self.calls.append({"method": method, "path": urlsplit(url).path,
                           "body": json.loads(body) if body else None, "timeout_s": timeout_s})
        if self.failure is not None:
            raise self.failure
        # Deterministic virtual native latency: never sleep and never perform network I/O.
        if self.elapsed_s > timeout_s:
            raise TimeoutError("Synthetic transport deadline elapsed")
        return self.status, {"X-WA-Version": "1.0.0"}, json.dumps(self.response).encode()


def client(transport):
    wa = WinAgentClient(WinAgentConfig(), transport=transport,
                        base_url="http://127.0.0.1:17610", token="synthetic-test-token")
    return WeChatWinAgent(wa)


async def send(c, **kwargs):
    return await c.send(session_name="synthetic-peer", text="QA synthetic only",
                        idempotency_key="synthetic-command", **kwargs)


async def test_default_send_deadline_is_fifty_seconds_and_posts_once():
    transport = DeadlineTransport()
    result = await send(client(transport))
    assert result["code"] == "DELIVERED"
    assert len(transport.calls) == 1
    assert transport.calls[0] == {
        "method": "POST", "path": "/wa/v1/wechat/send", "timeout_s": 50.0,
        "body": {"session_name": "synthetic-peer", "text": "QA synthetic only",
                 "idempotency_key": "synthetic-command", "confirm_timeout_ms": 10000},
    }


@pytest.mark.parametrize("confirm_ms,expected_s", [
    (0, 50.0), (1, 50.0), (10000, 50.0), (44999, 50.0),
    (45000, 50.0), (45001, 50.001), (60000, 65.0), (120000, 125.0),
])
async def test_confirmation_timeout_has_service_floor_without_truncating_long_requests(confirm_ms, expected_s):
    transport = DeadlineTransport()
    await send(client(transport), confirm_timeout_ms=confirm_ms)
    assert len(transport.calls) == 1
    assert transport.calls[0]["timeout_s"] == pytest.approx(expected_s)
    assert transport.calls[0]["body"]["confirm_timeout_ms"] == confirm_ms


async def test_response_after_previous_fifteen_second_deadline_is_awaited_without_retry():
    transport = DeadlineTransport(elapsed_s=16.001)
    result = await send(client(transport))
    assert result["ok"] is True
    assert result["ext_msg_id"] == "synthetic-peer:1"
    assert result["confirm_ms"] == 16001
    assert len(transport.calls) == 1
    assert transport.calls[0]["timeout_s"] > transport.elapsed_s


@pytest.mark.parametrize("failure", [TimeoutError("Synthetic timeout"), OSError("Synthetic OS error"),
                                     urllib.error.URLError("Synthetic URL error")])
async def test_transport_failure_keeps_single_write_attempt(failure):
    transport = DeadlineTransport(failure=failure)
    with pytest.raises(WeChatNotReady) as error:
        await send(client(transport))
    assert error.value.reason == "winagent_unreachable"
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["body"]["idempotency_key"] == "synthetic-command"


@pytest.mark.parametrize("status,exception", [(503, WeChatNotReady), (504, WeChatCallFailed)])
async def test_native_failure_response_is_not_retried_or_converted_to_success(status, exception):
    transport = DeadlineTransport(status=status, response={
        "ok": False, "error": {"reason": "wechat_window_unverified", "message": "Synthetic refusal"},
    })
    with pytest.raises(exception):
        await send(client(transport))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("method,path,timeout_s", [
    ("read", "/wa/v1/wechat/read", 35.0),
    ("login_start", "/wa/v1/wechat/login/start", 65.0),
    ("key_retry", "/wa/v1/wechat/key/retry", 65.0),
    ("logout", "/wa/v1/wechat/logout", 30.0),
    ("status", "/wa/v1/wechat/status", 15.0),
    ("login_status", "/wa/v1/wechat/login/status", 15.0),
    ("version_match", "/wa/v1/wechat/version-match", 5.0),
])
async def test_other_wechat_endpoint_deadlines_are_preserved(method, path, timeout_s):
    transport = DeadlineTransport(response={})
    await getattr(client(transport), method)()
    assert len(transport.calls) == 1
    assert transport.calls[0]["path"] == path
    assert transport.calls[0]["timeout_s"] == timeout_s


async def test_native_send_failed_result_is_preserved_without_retry():
    response = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": 10000}
    transport = DeadlineTransport(response=response)
    assert await send(client(transport)) == response
    assert len(transport.calls) == 1
