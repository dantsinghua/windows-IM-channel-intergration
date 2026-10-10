"""微信适配器(02 §2.2.3 wechat 条;05 §2.4.7 KEY_FAIL、§2.5.4 掉线/锁屏;06 §2.12 读回确认)。"""
from __future__ import annotations

from qtrade_agent.adapters.wechat.adapter import KEY_FAIL_MESSAGE, SCREEN_LOCKED_MESSAGE
from qtrade_agent.models import Command, Message, Session, json_safe
from tests.test_wechat_rig import make_wechat_rig

T0 = 1_758_240_000_000


def cmd(op: str, **args) -> Command:
    return Command(account_id="wx01", op=op, args=args, trace_id="TR1", idempotency_key="k1", submitted_at_ms=T0)


async def test_probe_state_running(tmp_path):
    rig = make_wechat_rig(tmp_path)
    assert await rig.adapter.probe_state(rig.account()) == ("running", None, "")
    rig.store.close()


async def test_probe_state_logged_out_when_window_gone(tmp_path):
    """05 §2.5.4 微信行:主窗口消失 / ``Weixin.exe`` 退出 ⇒ ``login_required(LOGGED_OUT)``(不自动拉起、不自动登回)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.wechat["running"] = False
    state, code, _ = await rig.adapter.probe_state(rig.account())
    assert (state, code) == ("login_required", "LOGGED_OUT")
    rig.fake.wechat["running"] = True
    rig.fake.wechat["logged_in"] = False
    assert (await rig.adapter.probe_state(rig.account()))[:2] == ("login_required", "LOGGED_OUT")
    rig.store.close()


async def test_probe_state_key_fail_from_chatlog_and_from_read_streak(tmp_path):
    """两条来路都判 ``degraded(KEY_FAIL)``:#28 报 ``chatlog.key_ok=false``,或 Agent 侧 read 连续 3 次异常。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.chatlog["key_ok"] = False
    assert (await rig.adapter.probe_state(rig.account()))[:2] == ("degraded", "KEY_FAIL")
    rig.fake.chatlog["key_ok"] = True
    rig.poller.st("wx01").read_fail_streak = 3
    assert (await rig.adapter.probe_state(rig.account()))[:2] == ("degraded", "KEY_FAIL")
    rig.store.close()


async def test_probe_state_screen_locked(tmp_path):
    """C-20 / 05 §2.5.4 锁屏行:``degraded(SCREEN_LOCKED)``,state_reason 逐字。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.screen_locked = True
    assert await rig.adapter.probe_state(rig.account()) == ("degraded", "SCREEN_LOCKED", SCREEN_LOCKED_MESSAGE)
    rig.store.close()


async def test_key_fail_wins_over_screen_locked(tmp_path):
    """实现口径(已登记 handoff「建议裁决」):两者同时命中时取更严的 ``KEY_FAIL``(读写都拒 ⊃ 只拒写)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.chatlog["key_ok"] = False
    rig.fake.screen_locked = True
    assert (await rig.adapter.probe_state(rig.account()))[:2] == ("degraded", "KEY_FAIL")
    rig.store.close()


async def test_module_disabled_and_user_agent_offline(tmp_path):
    rig = make_wechat_rig(tmp_path)
    rig.fake.user_agent = False
    assert (await rig.adapter.probe_state(rig.account()))[:2] == ("degraded", "WINAGENT_USER_OFFLINE")
    rig.fake.user_agent = True
    rig.fake.enabled = False
    assert (await rig.adapter.probe_state(rig.account()))[:2] == ("degraded", "WINAGENT_USER_OFFLINE")
    rig.store.close()


async def test_write_refused_under_key_fail_with_exact_message(tmp_path):
    """05 §2.4.7:``degraded(KEY_FAIL)`` 下写直接拒 ``NOT_READY``(最危险的形态是「发了却报失败」)。"""
    rig = make_wechat_rig(tmp_path)
    rig.store.transition("wx01", "degraded", state_code="KEY_FAIL")
    res = await rig.adapter.send(rig.account(), cmd("send_text", session="wx01:p", text="x"))
    assert res.ok is False and res.code == "NOT_READY" and res.error.message == KEY_FAIL_MESSAGE
    assert rig.fake.sent == []                                   # 一个字都没发出去
    rig.store.close()


async def test_send_404_is_target_not_found_not_send_failed(tmp_path):
    """R6-92:WinAgent 回 404(会话不存在 / 显示名重名拒发)= **根本没发**,结果码 TARGET_NOT_FOUND、不可盲重试。"""
    from qtrade_agent.adapters.wechat.client import WeChatCallFailed
    rig = make_wechat_rig(tmp_path)

    async def refuse(**_kw):
        raise WeChatCallFailed(404, {"error": {"message": "会话「张三」在通讯录里不唯一", "reason": "display_name_ambiguous"}},
                               "/wa/v1/wechat/send")
    rig.adapter._client.send = refuse                                    # noqa: SLF001
    res = await rig.adapter.send(rig.account(), cmd("send_text", session="wx01:p", text="x"))
    assert res.ok is False and res.code == "TARGET_NOT_FOUND"
    assert res.error.reason == "display_name_ambiguous" and res.error.retryable is False and "不唯一" in res.error.message
    rig.store.close()


async def test_read_also_refused_under_key_fail(tmp_path):
    """05 §2.4.7 读那一条:chatlog 拿不到 Data Key ⇒ ``read_messages`` 不可用。"""
    rig = make_wechat_rig(tmp_path)
    rig.store.transition("wx01", "degraded", state_code="KEY_FAIL")
    res = await rig.adapter.execute(rig.account(), cmd("read_messages", session="wx01:p"))
    assert res.code == "NOT_READY"
    rig.store.close()


async def test_screen_locked_blocks_write_but_not_read(tmp_path):
    """05 §2.5.4 锁屏行:**读取正常**(chatlog 不依赖桌面),写类拒 ``NOT_READY`` —— 与 KEY_FAIL 的「读写都拒」不同。"""
    rig = make_wechat_rig(tmp_path)
    rig.store.transition("wx01", "degraded", state_code="SCREEN_LOCKED")
    w = await rig.adapter.send(rig.account(), cmd("send_text", session="wx01:p", text="x"))
    assert w.code == "NOT_READY" and w.error.message == SCREEN_LOCKED_MESSAGE
    r = await rig.adapter.execute(rig.account(), cmd("read_messages", session="wx01:p"))
    assert r.ok is True and r.code == "OK"
    rig.store.close()


async def test_send_delivered_merges_readback_into_sending_row(tmp_path):
    """#38 回 ``DELIVERED`` 时,适配器把读回行喂 ``store.ingest`` ⇒ 合并进 ``SENDING`` 行(06 §2.12,``confirmed_by='chatlog'``)。"""
    rig = make_wechat_rig(tmp_path)
    out = Message(account_id="wx01", channel="wechat", session=Session("wx01", "p", "private"), dir="out", type="text",
                  text="你好", ts_ms=rig.clock(), source="chatlog", self=True, state="SENDING", trace_id="TR1", idempotency_key="k1",
                  sender_id="wxid_demo01")
    sending_id = rig.store.ingest(out, now_ms=rig.clock()).id
    res = await rig.adapter.send(rig.account(), cmd("send_text", session="wx01:p", text="你好"))
    assert res.ok and res.data["ext_msg_id"] == "p:1"
    st = rig.store.message_state(sending_id)
    assert st["state"] == "DELIVERED" and st["confirmed_by"] == "chatlog" and st["ext_msg_id"] == "p:1"
    assert rig.store.count_messages("wx01") == 1                 # 不插第二行
    assert rig.fake.sent[0]["session_name"] == "p"               # session_id 前缀已剥成原生 talker
    rig.store.close()


async def test_send_failed_is_reported_as_send_failed(tmp_path):
    """05 §2.4.7 / C.4.2:微信没有 ``confirm=false``,10 s 读不到即 ``SEND_FAILED``。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": None}
    res = await rig.adapter.send(rig.account(), cmd("send_text", session="wx01:p", text="x"))
    assert res.ok is False and res.code == "SEND_FAILED" and res.error.retryable is True
    rig.store.close()


async def test_send_opens_confirm_window(tmp_path):
    """05 §2.4.4 ⑦:send 之后拉取周期加密到 1 s。"""
    rig = make_wechat_rig(tmp_path)
    assert rig.poller.interval_s("wx01") == 5.0
    await rig.adapter.send(rig.account(), cmd("send_text", session="wx01:p", text="x"))
    assert rig.poller.interval_s("wx01") == 1.0
    rig.store.close()


async def test_confirm_probe_hits_local_store_first(tmp_path):
    """B-08 幂等复核:本库里那条出向行已被合并成已确认 ⇒ 直接 True,不再问 chatlog。"""
    rig = make_wechat_rig(tmp_path)
    done = Message(account_id="wx01", channel="wechat", session=Session("wx01", "p", "private"), dir="out", type="text",
                   text="上次发过的", ts_ms=T0, source="chatlog", self=True, state="DELIVERED", ext_msg_id="p:9", trace_id="TR0")
    rig.store.ingest(done, now_ms=T0)
    rig.fake.calls.clear()
    assert await rig.adapter.confirm_probe(rig.account(), cmd("send_text", session="wx01:p", text="上次发过的")) is True
    assert rig.fake.calls == []
    rig.store.close()


async def test_confirm_probe_falls_back_to_chatlog_same_text(tmp_path):
    """02 §2.2.3:微信查 chatlog 同分钟同文本(P-14);两侧比较过 ``norm()``(全角/空白差异不影响)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="p", content="报价  3.05", seq=4, ts_ms=T0 + 1000, is_self=True)
    assert await rig.adapter.confirm_probe(rig.account(), cmd("send_text", session="wx01:p", text="报价 3.05")) is True
    assert await rig.adapter.confirm_probe(rig.account(), cmd("send_text", session="wx01:p", text="另一句")) is False
    rig.store.close()


async def test_confirm_probe_ignores_peer_rows(tmp_path):
    """只认 ``isSelf`` 的行:对端发了同样的文本不算我们发成功了。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="p", content="收到", seq=4, ts_ms=T0 + 1000, is_self=False)
    assert await rig.adapter.confirm_probe(rig.account(), cmd("send_text", session="wx01:p", text="收到")) is False
    rig.store.close()


async def test_confirm_probe_false_when_winagent_unreachable(tmp_path):
    """问不到就当没发出去:宁可按幂等键重发,也不误判「已发」(微信侧本就有 idempotency_key)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.offline = True
    assert await rig.adapter.confirm_probe(rig.account(), cmd("send_text", session="wx01:p", text="x")) is False
    rig.store.close()


async def test_execute_get_state_and_list_sessions_and_screenshot(tmp_path):
    rig = make_wechat_rig(tmp_path)
    rig.fake.session_rows = [{"talker": "12345@chatroom", "name": "报价群"}]
    g = await rig.adapter.execute(rig.account(), cmd("get_state"))
    assert g.ok and g.data["state"] == "running" and g.data["self_uid"] == "wxid_demo01"
    s = await rig.adapter.execute(rig.account(), cmd("list_sessions"))
    assert s.ok and s.data["items"] == rig.fake.session_rows
    p = await rig.adapter.execute(rig.account(), cmd("screenshot"))
    assert p.ok and p.data["mime"] == "image/png" and p.data["png_len"] == len(rig.fake.screenshot_png)
    u = await rig.adapter.execute(rig.account(), cmd("start_stream"))
    assert u.code == "UNSUPPORTED"
    rig.store.close()


async def test_capabilities_match_catalog_five_ops(tmp_path):
    """02 §3.10 目录里 ``channels.wechat='supported'`` 的五个 op(#21 目录 ⊇ 该通道声明的每个 op)。"""
    rig = make_wechat_rig(tmp_path)
    assert rig.adapter.capabilities == frozenset({"send_text", "read_messages", "get_state", "screenshot", "list_sessions"})
    assert rig.adapter.channel == "wechat"
    rig.store.close()


async def test_poll_delegates_to_poller(tmp_path):
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="p", content="a", seq=1, ts_ms=T0)
    await rig.adapter.poll(rig.account())
    assert rig.store.count_messages("wx01") == 1
    rig.store.close()


async def test_stop_does_not_log_out_wechat(tmp_path):
    """``stop`` 只停 Agent 侧拉取;登出是 #34(槽位切换/`#16 logout` 才发起)——顺手登出会误踢在用的号。"""
    rig = make_wechat_rig(tmp_path)
    await rig.adapter.stop(rig.account(), graceful=True)
    assert rig.fake.logout_calls == 0
    rig.store.close()


# ---------------------------------------------------------------------- D-1:screenshot 的 data 必须可 JSON
async def test_screenshot_data_matches_capability_result_schema(tmp_path):
    """``capabilities/screenshot.json`` 的 ``result_schema = {png_b64, width, height}``
    (02 §3.10:``result_schema`` 就是 ``CommandResult.data``);另带 ``mime``/``png_len``/``sha256`` 便于端点与审计。
    **裸 bytes 一个都不许有** —— 否则 `store.finish_command` 的 json.dumps 会炸(D-1)。"""
    import base64
    import hashlib
    import json
    rig = make_wechat_rig(tmp_path)
    png = _real_png()
    rig.fake.screenshot_png = png
    res = await rig.adapter.execute(rig.account(), cmd("screenshot"))
    assert res.ok and res.code == "OK"
    assert res.data["mime"] == "image/png" and res.data["png_len"] == len(png)
    assert res.data["sha256"] == hashlib.sha256(png).hexdigest()
    assert (res.data["width"], res.data["height"]) == (3, 5)
    assert base64.b64decode(res.data["png_b64"]) == png
    # `data['png']` 是**进程内**出口(#33 二进制端点/直调用),序列化侧由 models.json_safe() 换成占位
    assert res.data["png"] == png
    json.dumps(json_safe(res.data))                       # 与 store.finish_command 同一条路径
    rig.store.close()


async def test_screenshot_through_bus_is_ok_not_internal(tmp_path):
    """🔴 D-1 回归:``screenshot`` **经总线**要回 ``OK``(00 §8.3 只读类成功码),
    并且结果能原样落 ``command_results.data_json``。直调 ``execute`` 复现不了——炸在 ``bus._finalize``。"""
    import json
    rig = make_wechat_rig(tmp_path)
    rig.fake.screenshot_png = _real_png()
    bus = rig.make_bus()
    try:
        res = await bus.submit(Command(account_id="wx01", op="screenshot", args={}))
        assert res.ok is True and res.code == "OK", res
        saved = rig.store.get_command_result(res.trace_id)
        assert json.loads(saved["data_json"])["png_len"] == len(rig.fake.screenshot_png)
    finally:
        await bus.close()
        rig.store.close()


async def test_screenshot_through_bus_in_login_required(tmp_path):
    """00 §8.1 R-06:``login_required`` 的可做集合含 ``screenshot``(操作对象是屏幕不是业务),登录门放行。"""
    rig = make_wechat_rig(tmp_path, state="login_required")
    rig.fake.screenshot_png = _real_png()
    bus = rig.make_bus()
    try:
        res = await bus.submit(Command(account_id="wx01", op="screenshot", args={}))
        assert res.ok is True and res.code == "OK", res
    finally:
        await bus.close()
        rig.store.close()


async def test_screenshot_prefers_media_reference_over_b64(tmp_path):
    """02 §2.8.2:装配方注入 ``media_put`` 时图片体落 ``media/``,``data`` 只给引用、**不再重复塞 png_b64**。"""
    calls: list[int] = []

    def media_put(acct, png):
        calls.append(len(png))
        return {"media_id": 42, "sha256": "deadbeef"}

    rig = make_wechat_rig(tmp_path, media_put=media_put)
    rig.fake.screenshot_png = _real_png()
    res = await rig.adapter.execute(rig.account(), cmd("screenshot"))
    assert calls == [len(rig.fake.screenshot_png)]
    assert res.data["media_id"] == 42 and res.data["sha256"] == "deadbeef"
    assert "png_b64" not in res.data
    rig.store.close()


async def test_screenshot_falls_back_to_b64_when_media_put_fails(tmp_path):
    """落盘失败(``media_put`` 回空)时图片体仍以 ``png_b64`` 给出——``result_schema`` 的图片体不能没有。"""
    rig = make_wechat_rig(tmp_path, media_put=lambda acct, png: {})
    rig.fake.screenshot_png = _real_png()
    res = await rig.adapter.execute(rig.account(), cmd("screenshot"))
    assert "png_b64" in res.data and "media_id" not in res.data
    rig.store.close()


def _real_png() -> bytes:
    """一张 3×5 的合法 PNG 头(只需前 24 字节合法,``_png_size`` 读的就是 IHDR)。"""
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (3).to_bytes(4, "big") + (5).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00rest"
