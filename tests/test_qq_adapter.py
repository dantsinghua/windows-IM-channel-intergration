"""QQ 适配器:推型入库 / 发送读回确认 / 撤回 / 补历史 / 能力目录对齐。

规格:docs/02 §2.2.3 ``qq`` 行与适配器契约、§2.8.1 QQ 行、docs/06 §2.9.1/§2.9.2/§2.9.4/§2.12 QQ 各条、docs/00 §3 端口段。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import Clock
from qtrade_agent.adapters.qq import CAPABILITIES, QQAdapter, QQAdapterConfig
from qtrade_agent.bus.bus import Bus
from qtrade_agent.gate import Gate
from qtrade_agent.models import Command, Message, Session
from test_qq_common import GROUP, PEER, SELF_UID, group_event, make_qq_rig, private_event

TS = 1_758_240_000
CAPS_DIR = Path(__file__).resolve().parents[1] / "src" / "qtrade_agent" / "capabilities"
HOUR_MS = 3600 * 1000


@pytest.fixture
async def rig(tmp_path):
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    yield r
    await r.adapter.close()
    r.close()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- 能力目录
def test_capabilities_match_catalog():
    """R6-57 ⑧:适配器静态能力集 = 能力目录 × ``channels['qq'] == 'supported'``。"""
    from_catalog = {json.loads(p.read_text(encoding="utf-8"))["op"] for p in CAPS_DIR.glob("*.json")
                    if json.loads(p.read_text(encoding="utf-8"))["channels"].get("qq") == "supported"}
    assert CAPABILITIES == from_catalog
    assert QQAdapter.capabilities == from_catalog


def test_channel_name_is_qq():
    assert QQAdapter.channel == "qq"


# ---------------------------------------------------------------- 端口推导与生命周期
def test_ws_url_is_derived_from_seq(rig):
    """00 §3 / 02 §2.2.4:``ws = 16100 + NN``,不查表、不随机。"""
    assert rig.adapter.ws_url_for(rig.acct) == "ws://127.0.0.1:16103"


async def test_start_is_idempotent(rig):
    await rig.adapter.start(rig.acct)
    assert rig.bot.connect_calls == 1


async def test_stop_closes_session(rig):
    await rig.adapter.stop(rig.acct, graceful=True)
    assert rig.adapter.session_of(rig.acct.id) is None
    assert rig.bot.connected is False


async def test_poll_is_noop_for_push_channel(rig):
    """02 §2.2.3 协议注释:拉型通道的一轮增量,**推型为空实现**。"""
    assert await rig.adapter.poll(rig.acct) is None
    assert rig.store.count_messages(rig.acct.id) == 0


# ---------------------------------------------------------------- 推型入库(02 §2.8.1 QQ 行)
async def test_inbound_private_message_is_ingested_and_emitted(rig):
    rig.bot.push_event(private_event(message_id=7, text="你好", time_s=TS))
    await settle()
    rows = rig.messages()
    assert len(rows) == 1
    row = rows[0]
    assert row["ext_msg_id"] == f"{PEER}:7" and row["dedup_kind"] == "native"
    assert row["dir"] == "in" and row["source"] == "onebot" and row["text"] == "你好"
    assert row["session_id"] == f"qq03:{PEER}"
    ev = rig.emitted()
    assert len(ev) == 1 and ev[0]["payload"]["ext_msg_id"] == f"{PEER}:7" and ev[0]["channel"] == "qq"


async def test_inbound_group_message_creates_session_row(rig):
    rig.bot.push_event(group_event(message_id=8, time_s=TS))
    await settle()
    sessions = rig.store.list_sessions(account_id=rig.acct.id)
    assert [(s["native_id"], s["kind"]) for s in sessions] == [(f"g_{GROUP}", "group")]
    assert rig.messages()[0]["ext_msg_id"] == f"g_{GROUP}:8"


async def test_same_message_id_across_sessions_does_not_collide(rig):
    """06 §2.9.2 ①:会话编进 ext,跨群同号不撞。"""
    rig.bot.push_event(private_event(message_id=5, time_s=TS))
    rig.bot.push_event(group_event(message_id=5, time_s=TS))
    await settle()
    assert sorted(r["ext_msg_id"] for r in rig.messages()) == sorted([f"g_{GROUP}:5", f"{PEER}:5"])


async def test_duplicate_event_is_not_inserted_twice(rig):
    ev = private_event(message_id=7, time_s=TS)
    rig.bot.push_event(dict(ev))
    await settle()
    rig.bot.push_event(dict(ev))
    await settle()
    assert rig.store.count_messages(rig.acct.id) == 1
    assert len(rig.emitted()) == 1                  # 撞键且无变化 ⇒ 不重复发事件


async def test_message_id_reuse_beyond_one_hour_gets_hash_suffix(rig):
    """06 §2.9.2 ②(R6-51):同键族里 ts 最大那行比,|ts 差| > 1h ⇒ 判新消息,后缀 ``#2``。"""
    rig.bot.push_event(private_event(message_id=7, text="旧", time_s=TS))
    await settle()
    rig.bot.push_event(private_event(message_id=7, text="新", time_s=TS + 2 * 3600))
    await settle()
    assert sorted(r["ext_msg_id"] for r in rig.messages()) == [f"{PEER}:7", f"{PEER}:7#2"]


async def test_message_seq_advances_onebot_cursor(rig):
    """06 §2.9.3:``cursors(owner='qqNN', kind='onebot_seq:<会话>').value_int = message_seq``(补历史用)。"""
    rig.bot.push_event(private_event(message_id=7, time_s=TS, message_seq=100))
    await settle()
    assert rig.store.cursor_get(rig.acct.id, f"onebot_seq:{PEER}").value_int == 100
    rig.bot.push_event(private_event(message_id=8, time_s=TS + 1, message_seq=99))
    await settle()
    assert rig.store.cursor_get(rig.acct.id, f"onebot_seq:{PEER}").value_int == 100   # 水位不倒退


async def test_meta_event_advances_ws_last_event_cursor(rig):
    """06 §2.9.3:``ws_last_event`` 只用于监控「多久没事件了」,不做拉取。"""
    rig.bot.push_heartbeat()
    await settle()
    assert rig.store.cursor_get(rig.acct.id, "ws_last_event").value_int == rig.clock.now_ms


async def test_self_message_from_another_client_is_out_external(rig):
    """R6-40:非本系统发出的我方消息(人在手机 QQ 上发的)⇒ ``dir='out'``、事件 ``origin='external'``。"""
    ev = private_event(message_id=11, text="手机上发的", time_s=TS, user_id=SELF_UID)
    ev["post_type"] = "message_sent"
    ev["target_id"] = int(PEER)
    rig.bot.push_event(ev)
    await settle()
    row = rig.messages()[0]
    assert row["dir"] == "out" and row["is_self"] == 1 and row["trace_id"] is None
    assert rig.emitted()[0]["payload"]["origin"] == "external"


async def test_malformed_event_is_dropped_without_killing_connection(rig):
    rig.bot.push_event({"post_type": "message", "message_type": "private", "message_id": 1, "time": TS, "message": []})
    await settle()
    rig.bot.push_event(private_event(message_id=2, time_s=TS))
    await settle()
    assert [r["ext_msg_id"] for r in rig.messages()] == [f"{PEER}:2"]


async def test_image_message_keeps_text_and_media_ref(rig):
    ev = private_event(message_id=12, time_s=TS)
    ev["message"] = [{"type": "text", "data": {"text": "看图"}}, {"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}}]
    rig.bot.push_event(ev)
    await settle()
    row = rig.messages()[0]
    assert row["type"] == "image" and row["text"] == "看图"
    assert json.loads(row["media_json"])[0]["kind"] == "image"


# ---------------------------------------------------------------- 发送与读回确认(06 §2.12 QQ 行)
def _sending_row(rig, native: str, text: str, *, trace_id: str = "tr-1", kind: str = "private") -> str:
    """模拟 bus 的「发送前先落库」(C-21/R6-16):经 store.ingest 写 dir=out/state=SENDING 行。"""
    msg = Message(account_id=rig.acct.id, channel="qq", session=Session(rig.acct.id, native, kind, name=native),
                  dir="out", type="text", text=text, ts_ms=rig.clock.now_ms, source="onebot",
                  sender_id=rig.acct.self_uid, sender_name=rig.acct.self_nick, self=True, state="SENDING",
                  trace_id=trace_id, idempotency_key=None)
    return rig.store.ingest(msg).id


async def test_send_binds_ext_and_marks_delivered_by_get_msg(rig):
    """06 §2.12:OneBot 回 ``message_id`` → ``get_msg`` 存在 ⇒ 绑 ``ext_msg_id``、``DELIVERED``、``confirmed_by='get_msg'``。"""
    mid = _sending_row(rig, PEER, "报价 3.05")
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": f"qq03:{PEER}", "text": "报价 3.05"}, trace_id="tr-1")
    res = await rig.adapter.send(rig.acct, cmd)
    assert res.ok and res.code == "OK" and res.source == "onebot"
    st = rig.store.message_state(mid)
    assert st["state"] == "DELIVERED" and st["confirmed_by"] == "get_msg"
    assert st["ext_msg_id"] == f"{PEER}:{rig.bot.sent[0]['message_id']}"
    assert rig.store.count_messages(rig.acct.id) == 1          # 合并进那一行,不插第二行


async def test_send_group_uses_send_group_msg(rig):
    _sending_row(rig, f"g_{GROUP}", "群里说一句", kind="group")
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": f"g_{GROUP}", "text": "群里说一句"}, trace_id="tr-2")
    res = await rig.adapter.send(rig.acct, cmd)
    assert res.ok and rig.bot.sent[0]["message_type"] == "group" and rig.bot.sent[0]["group_id"] == int(GROUP)


async def test_send_emits_message_event_with_origin_rpa(rig):
    _sending_row(rig, PEER, "本系统发的")
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "本系统发的"}, trace_id="tr-3")
    await rig.adapter.send(rig.acct, cmd)
    assert rig.emitted()[0]["payload"]["origin"] == "rpa"


async def test_send_action_failure_returns_send_failed(rig):
    rig.bot.fail_actions["send_private_msg"] = 1200
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "x"}, trace_id="tr-4")
    res = await rig.adapter.send(rig.acct, cmd)
    assert res.ok is False and res.code == "SEND_FAILED" and res.error.retryable is True


async def test_send_without_connection_returns_not_ready(tmp_path):
    r = make_qq_rig(tmp_path)
    cmd = Command(account_id=r.acct.id, op="send_text", args={"session": PEER, "text": "x"}, trace_id="tr-5")
    res = await r.adapter.send(r.acct, cmd)
    assert res.code == "NOT_READY" and res.error.reason == "onebot_unreachable"
    r.close()


async def test_send_without_message_id_is_send_failed(rig):
    rig.bot._act_send_private_msg = lambda params: {}                     # type: ignore[assignment]
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "x"}, trace_id="tr-6")
    res = await rig.adapter.send(rig.acct, cmd)
    assert res.code == "SEND_FAILED"


async def test_send_keeps_sending_when_get_msg_misses(rig):
    """``get_msg`` 查不到 ⇒ 出向行留 ``SENDING``,由 bus 在 ``confirm_timeout_qq_ms`` 后判 ``UNCONFIRMED``。"""
    mid = _sending_row(rig, PEER, "读不回")
    rig.bot.fail_actions["get_msg"] = 1404
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "读不回"}, trace_id="tr-7")
    res = await rig.adapter.send(rig.acct, cmd)
    assert res.ok and res.data["confirmed"] is False
    assert rig.store.message_state(mid)["state"] == "SENDING"


# ---------------------------------------------------------------- confirm_probe(02 §2.2.3:QQ 可 get_msg)
async def test_confirm_probe_true_when_bound_row_exists(rig):
    mid = _sending_row(rig, PEER, "复核用")
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "复核用"},
                  trace_id="tr-8", submitted_at_ms=rig.clock.now_ms)
    await rig.adapter.send(rig.acct, cmd)
    assert rig.store.message_state(mid)["state"] == "DELIVERED"
    assert await rig.adapter.confirm_probe(rig.acct, cmd) is True


async def test_confirm_probe_false_when_nothing_sent(rig):
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "没发过"},
                  trace_id="tr-9", submitted_at_ms=rig.clock.now_ms)
    assert await rig.adapter.confirm_probe(rig.acct, cmd) is False


async def test_confirm_probe_true_when_napcat_unreachable_but_row_bound(rig):
    """本库已有绑定好的出向行 = 上次确实发出去了;连不上 napcat 不改这个事实。"""
    _sending_row(rig, PEER, "断连复核")
    cmd = Command(account_id=rig.acct.id, op="send_text", args={"session": PEER, "text": "断连复核"},
                  trace_id="tr-10", submitted_at_ms=rig.clock.now_ms)
    await rig.adapter.send(rig.acct, cmd)
    rig.adapter.session_of(rig.acct.id).client.connected = False
    assert await rig.adapter.confirm_probe(rig.acct, cmd) is True


# ---------------------------------------------------------------- 撤回(06 §2.9.4:标记不删)
async def test_friend_recall_marks_revoked_and_keeps_row(rig):
    rig.bot.push_event(private_event(message_id=7, text="说错了", time_s=TS))
    await settle()
    rig.bot.push_event({"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                        "message_id": 7, "operator_id": int(PEER), "time": TS + 5})
    await settle()
    row = rig.messages()[0]
    assert row["revoked"] == 1 and row["text"] == "说错了" and row["revoked_by"] == PEER
    assert rig.store.count_messages(rig.acct.id) == 1
    assert rig.emitted()[-1]["payload"]["revoked"] is True


async def test_group_recall_marks_revoked(rig):
    rig.bot.push_event(group_event(message_id=8, time_s=TS))
    await settle()
    rig.bot.push_event({"post_type": "notice", "notice_type": "group_recall", "group_id": int(GROUP),
                        "user_id": int(PEER), "operator_id": 999, "message_id": 8, "time": TS + 5})
    await settle()
    row = rig.messages()[0]
    assert row["revoked"] == 1 and row["revoked_by"] == "999"


async def test_recall_of_unknown_message_inserts_nothing(rig):
    """库里没这条(装机前发的 / 已过保留期)⇒ 不臆造行。"""
    rig.bot.push_event({"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                        "message_id": 404, "time": TS})
    await settle()
    assert rig.store.count_messages(rig.acct.id) == 0


async def test_other_notice_types_are_ignored(rig):
    rig.bot.push_event({"post_type": "notice", "notice_type": "group_increase", "group_id": int(GROUP),
                        "user_id": int(PEER), "time": TS})
    await settle()
    assert rig.store.count_messages(rig.acct.id) == 0


# ---------------------------------------------------------------- 掉线重连补历史(02 §2.8.1 P-13)
async def test_backfill_pulls_history_after_cursor(rig):
    rig.bot.push_event(private_event(message_id=1, text="第一条", time_s=TS, message_seq=10))
    await settle()
    for i, seq in enumerate((10, 11, 12), start=1):
        rig.bot.record_history(PEER, private_event(message_id=i, text=f"补{seq}", time_s=TS + seq, message_seq=seq))
    n = await rig.adapter.backfill(rig.acct)
    assert n == 2                                                   # 只补水位之后的 11/12
    assert rig.store.cursor_get(rig.acct.id, f"onebot_seq:{PEER}").value_int == 12
    assert rig.store.count_messages(rig.acct.id) == 3


async def test_backfill_disabled_when_zero(tmp_path):
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0, history_backfill_on_reconnect=0))
    await r.adapter.start(r.acct)
    r.bot.push_event(private_event(message_id=1, time_s=TS, message_seq=1))
    await settle()
    r.bot.record_history(PEER, private_event(message_id=2, time_s=TS + 1, message_seq=2))
    assert await r.adapter.backfill(r.acct) == 0
    await r.adapter.close()
    r.close()


async def test_backfill_uses_configured_count(rig):
    rig.bot.push_event(group_event(message_id=1, time_s=TS, message_seq=1))
    await settle()
    captured = {}
    real = rig.adapter.session_of(rig.acct.id).client.call_action

    async def spy(action, params=None, **kw):
        captured[action] = params
        return await real(action, params, **kw)

    rig.adapter.session_of(rig.acct.id).client.call_action = spy       # type: ignore[assignment]
    await rig.adapter.backfill(rig.acct)
    assert captured["get_group_msg_history"] == {"group_id": int(GROUP), "count": 50}


async def test_backfill_survives_action_failure(rig):
    rig.bot.push_event(private_event(message_id=1, time_s=TS, message_seq=1))
    await settle()
    rig.bot.fail_actions["get_friend_msg_history"] = 1404
    assert await rig.adapter.backfill(rig.acct) == 0


async def test_reconnect_triggers_backfill(rig):
    rig.bot.push_event(private_event(message_id=1, time_s=TS, message_seq=1))
    await settle()
    rig.bot.record_history(PEER, private_event(message_id=2, text="重连后补的", time_s=TS + 2, message_seq=2))
    assert await rig.adapter.session_of(rig.acct.id).client.reconnect() is True
    assert rig.store.count_messages(rig.acct.id) == 2


# ---------------------------------------------------------------- execute
async def test_execute_get_state_running(rig):
    cmd = Command(account_id=rig.acct.id, op="get_state", args={}, trace_id="t")
    res = await rig.adapter.execute(rig.acct, cmd)
    assert res.ok and res.data["state"] == "running" and res.data["self_uid"] == SELF_UID


async def test_execute_list_sessions(rig):
    rig.bot.push_event(group_event(message_id=1, time_s=TS))
    await settle()
    res = await rig.adapter.execute(rig.acct, Command(account_id=rig.acct.id, op="list_sessions", args={}, trace_id="t"))
    assert res.ok and [s["native_id"] for s in res.data["data"]] == [f"g_{GROUP}"]


async def test_execute_read_messages(rig):
    rig.bot.push_event(private_event(message_id=1, text="读一条", time_s=TS))
    await settle()
    res = await rig.adapter.execute(rig.acct, Command(account_id=rig.acct.id, op="read_messages",
                                                      args={"session": f"qq03:{PEER}", "limit": 10}, trace_id="t"))
    assert res.ok and [m["text"] for m in res.data["items"]] == ["读一条"]


async def test_execute_screenshot_is_not_applicable(rig):
    """主文档 B.2 能力矩阵 ``➖``:QQ 通道无画面概念 ⇒ ``NOT_APPLICABLE``、不算失败。"""
    res = await rig.adapter.execute(rig.acct, Command(account_id=rig.acct.id, op="screenshot", args={}, trace_id="t"))
    assert res.code == "NOT_APPLICABLE" and res.ok is False


async def test_execute_unknown_op_is_unsupported(rig):
    res = await rig.adapter.execute(rig.acct, Command(account_id=rig.acct.id, op="send_file", args={}, trace_id="t"))
    assert res.code == "UNSUPPORTED"


# ---------------------------------------------------------------- get_state
async def test_get_state_offline_is_login_required(rig):
    rig.bot.online = False
    assert await rig.adapter.get_state(rig.acct) == "login_required"


async def test_get_state_without_user_id_is_login_required(rig):
    """05 §2.3.3:``get_login_info`` 长期不返回 ``user_id`` = 免扫失效。"""
    rig.bot._act_get_login_info = lambda params: {}                      # type: ignore[assignment]
    assert await rig.adapter.get_state(rig.acct) == "login_required"


async def test_get_state_is_cached_for_two_seconds(rig):
    assert await rig.adapter.get_state(rig.acct) == "running"
    rig.bot.online = False
    assert await rig.adapter.get_state(rig.acct) == "running"            # 缓存 ≤2 s(02 §2.2.3)
    rig.clock.advance(2001)
    assert await rig.adapter.get_state(rig.acct) == "login_required"


async def test_get_state_without_session_falls_back_to_db_state(tmp_path):
    r = make_qq_rig(tmp_path)
    assert await r.adapter.get_state(r.acct) == "running"
    r.close()


async def test_get_state_keeps_db_state_when_action_fails(rig):
    """适配器不擅自改状态:查不到就沿用库里的(掉线判定归 04 H08)。"""
    rig.bot.fail_actions["get_status"] = 1404
    assert await rig.adapter.get_state(rig.acct) == "running"


# ---------------------------------------------------------------- 经 bus 的全链路(02 §2.2.2 七段流水)
async def test_bus_send_text_end_to_end_delivered(tmp_path):
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    bus = Bus(store=r.store, events=r.events, adapters={"qq": r.adapter}, cfg=r.cfg, clock=r.clock, gate=Gate(r.store))
    try:
        res = await bus.submit(Command(account_id=r.acct.id, op="send_text",
                                       args={"session": f"qq03:{PEER}", "text": "经总线发"}, idempotency_key="k1"))
        assert res.ok and res.code == "DELIVERED" and res.data["confirmed_by"] == "get_msg"
        assert res.data["ext_msg_id"] == f"{PEER}:1"
        rows = r.messages()
        assert len(rows) == 1 and rows[0]["state"] == "DELIVERED" and rows[0]["trace_id"] == res.trace_id
    finally:
        await bus.close()
        await r.adapter.close()
        r.close()


async def test_bus_send_text_unconfirmed_when_readback_misses(tmp_path):
    r = make_qq_rig(tmp_path, clock=Clock(auto_step_ms=3000), qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    r.bot.fail_actions["get_msg"] = 1404
    bus = Bus(store=r.store, events=r.events, adapters={"qq": r.adapter}, cfg=r.cfg, clock=r.clock, gate=Gate(r.store))
    try:
        res = await bus.submit(Command(account_id=r.acct.id, op="send_text", args={"session": PEER, "text": "读不回"}))
        assert res.code == "SEND_CALLED_BUT_UNCONFIRMED"
        assert r.store.message_state(res.data["message_id"])["state"] == "UNCONFIRMED"
    finally:
        await bus.close()
        await r.adapter.close()
        r.close()


async def test_bus_rejects_send_in_login_phase(tmp_path):
    """02 §2.2.2 登录门(D-2):登录阶段 IM 写类直接 ``LOGIN_REQUIRED``,不排队、不写 SENDING 行。"""
    r = make_qq_rig(tmp_path, state="login_required", qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    bus = Bus(store=r.store, events=r.events, adapters={"qq": r.adapter}, cfg=r.cfg, clock=r.clock, gate=Gate(r.store))
    try:
        res = await bus.submit(Command(account_id=r.acct.id, op="send_text", args={"session": PEER, "text": "x"}))
        assert res.code == "LOGIN_REQUIRED" and res.error.needs_human is True
        assert r.store.count_messages(r.acct.id) == 0 and r.bot.sent == []
    finally:
        await bus.close()
        await r.adapter.close()
        r.close()


async def test_bus_rejects_control_chars_before_sending(tmp_path):
    """R6-48:``send_text.text`` 含 U+0014 ⇒ 400 INVALID_ARGS,不写 SENDING 行、不调通道。"""
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    bus = Bus(store=r.store, events=r.events, adapters={"qq": r.adapter}, cfg=r.cfg, clock=r.clock, gate=Gate(r.store))
    try:
        res = await bus.submit(Command(account_id=r.acct.id, op="send_text", args={"session": PEER, "text": "甲\u0014A"}))
        assert res.code == "INVALID_ARGS" and res.error.reason == "text_has_control_chars"
        assert r.bot.sent == []
    finally:
        await bus.close()
        await r.adapter.close()
        r.close()
