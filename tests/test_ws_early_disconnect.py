"""D-2(e2e-rootfs-2):WS ``/events`` 客户端在首帧订阅**之前**断开 ⇒ 不得再对已关连接 ``close(4400)``。

starlette ``TestClient`` 对「断开后再 close」不报错,抓不到这个缺陷;这里直接驱动 ASGI 应用,
send 端按 uvicorn 的真实行为:收到 ``websocket.disconnect`` 之后再发 ``websocket.close`` 即抛 ``RuntimeError``
(uvicorn 原文 ``Unexpected ASGI message 'websocket.close', after sending 'websocket.close' or response already completed``)。
"""
from __future__ import annotations

import asyncio

import pytest

from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig

TOKEN = "early-disc-token-1"


class _UvicornLikeConn:
    def __init__(self, inbound: list[dict]):
        self.inbound = list(inbound)
        self.sent: list[dict] = []
        self.disconnected = False
        self._idle = asyncio.Event()

    async def receive(self) -> dict:
        if self.inbound:
            m = self.inbound.pop(0)
            if m["type"] == "websocket.disconnect":
                self.disconnected = True
            return m
        await self._idle.wait()                        # 不再来消息(模拟客户端连着但不说话)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(self, m: dict) -> None:
        if m["type"] == "websocket.close" and (self.disconnected or any(x["type"] == "websocket.close" for x in self.sent)):
            raise RuntimeError(f"Unexpected ASGI message '{m['type']}', after sending 'websocket.close' "
                               "or response already completed.")
        self.sent.append(m)


@pytest.fixture
def api(tmp_path):
    agent = AgentApp(AgentConfig(), db_path=str(tmp_path / "agent.db")).open()
    agent.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOKEN)
    yield agent.create_api()
    agent.store.close()


def _scope(qs: str) -> dict:
    return {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1",
            "path": "/api/v1/events", "raw_path": b"/api/v1/events", "query_string": qs.encode(),
            "root_path": "", "headers": [(b"host", b"127.0.0.1")], "client": ("127.0.0.1", 40000),
            "server": ("127.0.0.1", 17600), "subprotocols": [], "state": {}}


def _run(api, qs: str, inbound: list[dict]) -> _UvicornLikeConn:
    conn = _UvicornLikeConn(inbound)
    asyncio.run(asyncio.wait_for(api(_scope(qs), conn.receive, conn.send), timeout=15))   # 抛了就是缺陷
    return conn


def test_disconnect_before_first_frame_no_second_close(api):
    conn = _run(api, f"token={TOKEN}", [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1001}])
    assert [m["type"] for m in conn.sent] == ["websocket.accept"]          # 已断 ⇒ 不再 close


def test_bad_first_frame_still_closes_4400(api):
    conn = _run(api, f"token={TOKEN}", [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "not json"}])
    closes = [m for m in conn.sent if m["type"] == "websocket.close"]
    assert len(closes) == 1 and closes[0]["code"] == 4400


def test_first_frame_without_subscribe_still_closes_4400(api):
    conn = _run(api, f"token={TOKEN}", [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "{}"}])
    closes = [m for m in conn.sent if m["type"] == "websocket.close"]
    assert len(closes) == 1 and closes[0]["code"] == 4400


def test_bad_token_still_accept_then_4401(api):
    conn = _run(api, "token=nope", [{"type": "websocket.connect"}])
    assert [m["type"] for m in conn.sent] == ["websocket.accept", "websocket.close"]
    assert conn.sent[1]["code"] == 4401
