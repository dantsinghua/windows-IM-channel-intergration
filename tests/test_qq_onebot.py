"""OneBot v11 正向 WS 客户端(传输层 + 协议)——docs/02 §2.2.3 ``qq`` 行、§2.2 技术选型、docs/04 §2.3 H08 心跳。"""
from __future__ import annotations

import asyncio
import json

import pytest

from conftest import Clock
from qtrade_agent.adapters.qq import FakeOneBot, OneBotClient, OneBotClosed, OneBotError, QQAdapterConfig, http_url, ws_url

FAST = QQAdapterConfig(reconnect_delay_s=0)


async def make_client(*, cfg=FAST, on_event=None, on_reconnect=None, online=True, clock=None):
    clock = clock or Clock()
    bot = FakeOneBot(self_id=415011447, clock=clock, online=online)
    client = OneBotClient("ws://127.0.0.1:16103", transport=bot, cfg=cfg, on_event=on_event,
                          on_reconnect=on_reconnect, clock=clock)
    await client.start()
    return client, bot, clock


# ---------------------------------------------------------------- 地址
def test_ws_and_http_url_bind_loopback():
    """00 §3:napcat 三段端口全部绑 127.0.0.1。"""
    assert ws_url(16103) == "ws://127.0.0.1:16103"
    assert http_url(16203) == "http://127.0.0.1:16203"


# ---------------------------------------------------------------- call_action
async def test_call_action_returns_data():
    client, bot, _ = await make_client()
    try:
        assert await client.call_action("get_login_info") == {"user_id": 415011447, "nickname": "假QQ"}
    finally:
        await client.close()


async def test_call_action_matches_echo_when_concurrent():
    """echo 匹配:两个 action 并发时各自认领自己的响应帧。"""
    client, bot, _ = await make_client()
    try:
        a, b = await asyncio.gather(
            client.call_action("send_private_msg", {"user_id": 1, "message": "甲"}),
            client.call_action("send_private_msg", {"user_id": 2, "message": "乙"}),
        )
        assert a["message_id"] != b["message_id"]
        assert [s["message"][0]["data"]["text"] for s in bot.sent] == ["甲", "乙"]
    finally:
        await client.close()


async def test_call_action_failed_raises_onebot_error():
    client, bot, _ = await make_client()
    bot.fail_actions["send_group_msg"] = 1404
    try:
        with pytest.raises(OneBotError) as e:
            await client.call_action("send_group_msg", {"group_id": 1, "message": "x"})
        assert e.value.retcode == 1404 and e.value.action == "send_group_msg"
    finally:
        await client.close()


async def test_unknown_action_is_1404():
    client, bot, _ = await make_client()
    try:
        with pytest.raises(OneBotError) as e:
            await client.call_action("set_group_kick", {})
        assert e.value.retcode == 1404
    finally:
        await client.close()


async def test_call_action_without_connection_raises_closed():
    clock = Clock()
    bot = FakeOneBot(clock=clock)
    client = OneBotClient("ws://127.0.0.1:16103", transport=bot, cfg=FAST, clock=clock)
    with pytest.raises(OneBotClosed):
        await client.call_action("get_status")


async def test_call_action_timeout_raises_closed():
    client, bot, _ = await make_client()
    bot.connected = True
    try:
        # 把 send 变成「不回响应」:直接吞掉,让 future 等到超时
        bot.send = lambda payload: asyncio.sleep(0)                      # type: ignore[assignment]
        with pytest.raises(OneBotClosed):
            await client.call_action("get_status", timeout_s=0.05)
        assert not client._pending                                        # 超时后不留悬挂 future
    finally:
        await client.close()


# ---------------------------------------------------------------- 事件分发与心跳
async def test_event_dispatch_and_heartbeat_watermark():
    seen = []

    async def on_event(frame):
        seen.append(frame)

    clock = Clock()
    client, bot, _ = await make_client(on_event=on_event, clock=clock)
    try:
        bot.push_heartbeat()
        await asyncio.sleep(0.02)
        assert seen and seen[0]["meta_event_type"] == "heartbeat"
        assert client.last_heartbeat_ms == clock.now_ms
        assert client.last_status == {"online": True, "good": True}
        assert client.silence_ms(clock.now_ms + 1000) == 1000
    finally:
        await client.close()


async def test_lifecycle_event_recorded():
    client, bot, _ = await make_client()
    try:
        bot.push_event({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect"})
        await asyncio.sleep(0.02)
        assert client.last_lifecycle == "connect"
    finally:
        await client.close()


async def test_event_callback_exception_does_not_kill_loop():
    calls = []

    async def boom(frame):
        calls.append(frame)
        raise RuntimeError("回调炸了")

    client, bot, _ = await make_client(on_event=boom)
    try:
        bot.push_heartbeat()
        await asyncio.sleep(0.02)
        bot.push_heartbeat()
        await asyncio.sleep(0.02)
        assert len(calls) == 2                       # 第一条炸了,第二条照收
    finally:
        await client.close()


async def test_non_json_frame_is_dropped():
    client, bot, _ = await make_client()
    try:
        bot._q.put_nowait("这不是 JSON")
        await asyncio.sleep(0.02)
        bot.push_heartbeat()
        await asyncio.sleep(0.02)
        assert client.last_heartbeat_ms is not None   # 循环还活着
    finally:
        await client.close()


async def test_silence_ms_is_none_before_any_frame():
    clock = Clock()
    bot = FakeOneBot(clock=clock)
    client = OneBotClient("ws://x", transport=bot, cfg=FAST, clock=clock)
    assert client.silence_ms(clock.now_ms) is None


# ---------------------------------------------------------------- 重连
def test_backoff_series_uses_reconnect_delay_s():
    """``[adapters.qq] reconnect_delay_s=3`` × min(2^(n−1), 10)。"""
    clock = Clock()
    client = OneBotClient("ws://x", transport=FakeOneBot(clock=clock), clock=clock)
    assert [client.backoff_s(n) for n in (1, 2, 3, 4, 5, 9)] == [3, 6, 12, 24, 30, 30]


async def test_reconnect_after_drop_calls_on_reconnect():
    hits = []

    async def on_reconnect():
        hits.append(1)

    client, bot, _ = await make_client(on_reconnect=on_reconnect)
    try:
        bot.drop()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if hits:
                break
        assert hits, "断线后应自动重连并回调 on_reconnect"
        assert client.connected and bot.connect_calls >= 2
    finally:
        await client.close()


async def test_manual_reconnect_returns_true_and_counts():
    client, bot, _ = await make_client()
    try:
        assert await client.reconnect() is True
        assert client.reconnects == 1 and bot.connect_calls == 2
    finally:
        await client.close()


async def test_pending_action_fails_when_connection_drops():
    client, bot, _ = await make_client()
    try:
        bot.send = lambda payload: asyncio.sleep(0)                      # type: ignore[assignment]
        task = asyncio.create_task(client.call_action("get_status", timeout_s=5))
        await asyncio.sleep(0.02)
        bot.drop()
        with pytest.raises(OneBotClosed):
            await task
    finally:
        await client.close()


async def test_close_is_idempotent_and_disconnects():
    client, bot, _ = await make_client()
    await client.close()
    await client.close()
    assert client.connected is False and bot.connected is False


async def test_start_is_idempotent():
    client, bot, _ = await make_client()
    try:
        await client.start()
        assert bot.connect_calls == 1
    finally:
        await client.close()


# ---------------------------------------------------------------- FakeOneBot 自身语义
async def test_fake_get_msg_roundtrip_and_drop():
    client, bot, _ = await make_client()
    try:
        mid = (await client.call_action("send_private_msg", {"user_id": 1, "message": "甲"}))["message_id"]
        assert (await client.call_action("get_msg", {"message_id": mid}))["message"][0]["data"]["text"] == "甲"
        bot.drop_from_store.add(mid)
        assert await client.call_action("get_msg", {"message_id": mid}) is None
    finally:
        await client.close()


async def test_fake_get_status_reflects_online_flag():
    client, bot, _ = await make_client(online=False)
    try:
        assert await client.call_action("get_status") == {"online": False, "good": True}
        assert await client.call_action("get_login_info") == {}          # 05 §2.3.3:未登录不返回 user_id
    finally:
        await client.close()


async def test_fake_history_respects_count():
    client, bot, _ = await make_client()
    try:
        for i in range(5):
            bot.record_history("g_1", {"message_id": i, "message_seq": i})
        data = await client.call_action("get_group_msg_history", {"group_id": 1, "count": 2})
        assert [m["message_id"] for m in data["messages"]] == [3, 4]
    finally:
        await client.close()


async def test_action_frame_shape_is_onebot_v11():
    """action 帧 = ``{action, params, echo}``(OneBot v11 标准)。"""
    client, bot, _ = await make_client()
    captured = []
    real = bot.send

    async def spy(payload):
        captured.append(json.loads(payload))
        await real(payload)

    bot.send = spy                                                        # type: ignore[assignment]
    try:
        await client.call_action("get_status", {"a": 1})
        assert captured[0]["action"] == "get_status" and captured[0]["params"] == {"a": 1}
        assert captured[0]["echo"] == "1"
    finally:
        await client.close()
