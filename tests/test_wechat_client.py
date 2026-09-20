"""WinAgent 微信端点客户端(02 §3.6 #28~#43):路径/入参/重试/503 口径。"""
from __future__ import annotations

import pytest

from qtrade_agent.adapters.wechat import WeChatCallFailed, WeChatNotReady
from tests.test_wechat_rig import make_wechat_rig


def paths(rig) -> list[str]:
    return [p.split("?", 1)[0] for _m, p, _b in rig.fake.calls]


async def test_endpoint_paths_match_doc02_section36(tmp_path):
    """逐个核对 §3.6 表里的路径(#28/#29/#30/#31/#32/#33/#33c/#34/#35/#36/#38/#39/#41)。"""
    rig = make_wechat_rig(tmp_path)
    c = rig.client
    await c.status()
    await c.version_match()
    await c.profiles()
    await c.login_start(account_id="wx01", login_session_id="ls_X")
    await c.login_status()
    await c.bind("wxid_demo01", "wx01")
    await c.key_retry()
    await c.ui_visible()
    await c.read(talker="t", since_seq=3, limit=10)
    await c.sessions(keyword="群", limit=5)
    await c.send(session_name="t", text="x", idempotency_key="k1")
    await c.login_cancel()
    await c.logout()
    assert paths(rig) == [
        "/wa/v1/wechat/status", "/wa/v1/wechat/version-match", "/wa/v1/wechat/profiles",
        "/wa/v1/wechat/login/start", "/wa/v1/wechat/login/status", "/wa/v1/wechat/bind",
        "/wa/v1/wechat/key/retry", "/wa/v1/wechat/ui-visible", "/wa/v1/wechat/read",
        "/wa/v1/wechat/sessions", "/wa/v1/wechat/send", "/wa/v1/wechat/login/cancel", "/wa/v1/wechat/logout",
    ]
    rig.store.close()


async def test_login_start_body_carries_account_id_and_login_session_id(tmp_path):
    """#31 入参 ``{account_id?:'wxNN', login_session_id?}``(R-23:恒有 account_id,不再有「无 id」窗口)。"""
    rig = make_wechat_rig(tmp_path)
    await rig.client.login_start(account_id="wx01", login_session_id="ls_ABC")
    _m, _p, body = rig.fake.calls[-1]
    assert body == {"account_id": "wx01", "login_session_id": "ls_ABC"}
    assert rig.fake.login_session_id == "ls_ABC"
    rig.store.close()


async def test_read_query_params_and_since_seq_filter(tmp_path):
    """#39 ``?talker=&since_seq=&limit=``;WinAgent 侧按 since_seq 过滤(取窗 lookback 在 WinAgent 做,06 §2.9.1)。"""
    rig = make_wechat_rig(tmp_path)
    for i in range(1, 6):
        rig.fake.add_row(talker="wxid_peer", content=f"m{i}", seq=i, ts_ms=1_758_240_000_000 + i)
    rig.fake.add_row(talker="other", content="x", seq=6, ts_ms=1_758_240_000_000)
    rows = await rig.client.read(talker="wxid_peer", since_seq=3)
    assert [r["seq"] for r in rows] == [4, 5]
    assert await rig.client.read(since_seq=0, limit=2) == rig.fake.rows[:2]
    rig.store.close()


async def test_send_is_a_write_endpoint_and_not_retried(tmp_path):
    """02 §2.5:写类**不重试**——重发一次写动作会重复发消息。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.fail_next = 1                     # 传输层抛一次 OSError
    with pytest.raises(WeChatNotReady):
        await rig.client.send(session_name="t", text="x", idempotency_key="k")
    rig.store.close()


async def test_read_is_retried_once(tmp_path):
    """02 §2.5:只读类 1 次重试。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="t", content="a", seq=1)
    rig.fake.fail_next = 1
    assert len(await rig.client.read(talker="t", since_seq=0)) == 1
    rig.store.close()


async def test_user_agent_offline_is_503_not_ready(tmp_path):
    """§3.6 表头:执行体 ``user`` 的端点在会话代理不在线时一律 ``503 NOT_READY``(#28 status 的执行体也是 user,不例外;
    「会话代理在不在」由 #2 ``GET /wa/v1/health`` 的 ``user_agent`` 布尔辨别,02 §2.4.1)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.user_agent = False
    for call in (rig.client.login_status(), rig.client.status()):
        with pytest.raises(WeChatNotReady) as e:
            await call
        assert e.value.reason == "user_agent_offline"
    rig.store.close()


async def test_module_disabled_reports_enabled_false(tmp_path):
    """#28:模块关闭时 ``enabled:false`` 其余 null。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.enabled = False
    st = await rig.client.status()
    assert st == {"enabled": False, "wechat": None, "chatlog": None, "ritual_done": None, "screen_locked": None}
    rig.store.close()


async def test_bind_conflict_is_409_with_existing_account_id(tmp_path):
    """#33c:绑了别的 id ⇒ 409(body 带 winagent.db 已有的 account_id,供 05 §2.4.2.1 的合并分支用)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.bind_conflict = "wx02"
    with pytest.raises(WeChatCallFailed) as e:
        await rig.client.bind("wxid_demo01", "wx07")
    assert e.value.status == 409 and e.value.body["account_id"] == "wx02"
    assert (await rig.client.bind("wxid_demo01", "wx02"))["ok"] is True      # 同 id 幂等
    rig.store.close()


async def test_binary_endpoints_return_bytes(tmp_path):
    """#40 media / #42 screenshot 是二进制端点(不是 JSON)。"""
    rig = make_wechat_rig(tmp_path)
    assert (await rig.client.screenshot()).startswith(b"\x89PNG")
    assert await rig.client.media("abc") == b"jpegbytes"
    rig.store.close()


async def test_send_body_has_session_name_and_confirm_timeout(tmp_path):
    """#38 入参 ``{session_name, text?, image_path?, idempotency_key, confirm_timeout_ms:10000}``。"""
    rig = make_wechat_rig(tmp_path)
    await rig.client.send(session_name="群A", text="你好", idempotency_key="k1")
    _m, _p, body = rig.fake.calls[-1]
    assert body == {"session_name": "群A", "idempotency_key": "k1", "confirm_timeout_ms": 10000, "text": "你好"}
    rig.store.close()
