"""微信读循环(06 §2.9.1 入库路径 / §2.9.2 去重键 / §2.9.3 游标 / §2.12 合并;05 §2.4.4 ⑦ 加速周期)。"""
from __future__ import annotations

import json

from qtrade_agent.adapters.wechat import CURSOR_PREFIX, WechatAccountView
from qtrade_agent.adapters.wechat.poll import READ_FAIL_STREAK
from qtrade_agent.models import Message, Session
from tests.test_wechat_rig import make_wechat_rig

T0 = 1_758_240_000_000


def view(rig, id="wx01") -> WechatAccountView:
    row = rig.store.get_account_full(id)
    return WechatAccountView(row["id"], row["state"], row["self_uid"], row["self_nick"])


async def test_full_round_ingests_and_advances_per_talker_cursor(tmp_path):
    """06 §2.9.3:每会话一条 ``chatlog_seq:<talker>``,``value_int=last_seq``、``value={"last_ts_ms","last_seq"}``;
    游标推进与消息落库同一事务(每 talker 一次 ingest_batch)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="wxid_peer", content="早", seq=1, ts_ms=T0)
    rig.fake.add_row(talker="12345@chatroom", content="群里一句", seq=2, ts_ms=T0 + 1000)
    n = await rig.poller.poll(view(rig))
    assert n == 2
    c1 = rig.store.cursor_get("wx01", CURSOR_PREFIX + "wxid_peer")
    c2 = rig.store.cursor_get("wx01", CURSOR_PREFIX + "12345@chatroom")
    assert c1.value_int == 1 and json.loads(c1.value) == {"last_ts_ms": T0, "last_seq": 1}
    assert c2.value_int == 2
    rows = rig.store.list_messages("wx01")
    assert {r["ext_msg_id"] for r in rows} == {"wxid_peer:1", "12345@chatroom:2"}
    assert {r["session_id"] for r in rows} == {"wx01:wxid_peer", "wx01:12345@chatroom"}
    kinds = {s["id"]: s["kind"] for s in rig.store.list_sessions(account_id="wx01")}
    assert kinds["wx01:12345@chatroom"] == "group" and kinds["wx01:wxid_peer"] == "private"
    rig.store.close()


async def test_full_round_since_seq_is_max_of_per_talker_cursors(tmp_path):
    """05 §2.4.4 ⑦「该账号的 seq 水位」:全量轮取 max(per-talker),依据 06 §2.9.2「seq 单调、不复用」。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="a", content="1", seq=1, ts_ms=T0)
    rig.fake.add_row(talker="b", content="2", seq=7, ts_ms=T0)
    await rig.poller.poll(view(rig))
    assert rig.poller.account_seq("wx01") == 7
    rig.fake.calls.clear()
    rig.fake.add_row(talker="a", content="3", seq=8, ts_ms=T0 + 1)
    assert await rig.poller.poll(view(rig)) == 1
    assert "since_seq=7" in rig.fake.calls[0][1]
    rig.store.close()


async def test_overlap_window_duplicates_do_not_replay_events(tmp_path):
    """重叠窗(``cursor − lookback_overlap_s``,WinAgent 侧取窗)必然重复取到边界几条 —— 靠
    ``(account_id, ext_msg_id)`` 挡;重扫不重放 ``message`` 事件(R6-40 口径,三通道同)。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="a", content="hi", seq=5, ts_ms=T0)
    assert await rig.poller.poll(view(rig)) == 1
    assert len(rig.store.list_events(event="message")) == 1
    rig.store.cursor_set("wx01", CURSOR_PREFIX + "a", 0)       # 模拟 WinAgent 回退取窗把同一行又给回来
    assert await rig.poller.poll(view(rig)) == 0
    assert len(rig.store.list_events(event="message")) == 1
    assert rig.store.count_messages("wx01") == 1
    rig.store.close()


async def test_accelerated_round_only_asks_target_talker(tmp_path):
    """``only_sessions`` = 确认窗内只查目标会话(与企点加速轮同义);用各自水位、不走账号级 max。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.add_row(talker="a", content="1", seq=1, ts_ms=T0)
    rig.fake.add_row(talker="b", content="2", seq=9, ts_ms=T0)
    await rig.poller.poll(view(rig))
    rig.fake.calls.clear()
    rig.fake.add_row(talker="a", content="3", seq=10, ts_ms=T0 + 1)
    assert await rig.poller.poll(view(rig), ["a"]) == 1
    assert len(rig.fake.calls) == 1 and "talker=a" in rig.fake.calls[0][1] and "since_seq=1" in rig.fake.calls[0][1]
    rig.store.close()


async def test_self_row_merges_into_sending_row_and_confirms(tmp_path):
    """06 §2.12 微信行:``self=true`` 的读回行合并进 ``SENDING`` 行 ⇒ ``DELIVERED`` + ``confirmed_by='chatlog'``,不插第二行。"""
    rig = make_wechat_rig(tmp_path)
    out = Message(account_id="wx01", channel="wechat", session=Session("wx01", "wxid_peer", "private"), dir="out",
                  type="text", text="报价 3.05", ts_ms=T0, source="chatlog", self=True, state="SENDING", trace_id="TR1")
    sending_id = rig.store.ingest(out, now_ms=T0).id
    rig.fake.add_row(talker="wxid_peer", content="报价 3.05", seq=3, ts_ms=T0 + 2000, is_self=True)
    assert await rig.poller.poll(view(rig)) == 1
    st = rig.store.message_state(sending_id)
    assert st["state"] == "DELIVERED" and st["ext_msg_id"] == "wxid_peer:3" and st["confirmed_by"] == "chatlog"
    assert rig.store.count_messages("wx01") == 1
    rig.store.close()


async def test_out_row_origin_rpa_vs_external(tmp_path):
    """06 §2.9.5 掉线续读条(三通道同):合并进带 ``trace_id`` 的出向行 ⇒ ``origin='rpa'``;
    别的端/人用微信自己发的 ⇒ ``origin='external'``。"""
    rig = make_wechat_rig(tmp_path)
    out = Message(account_id="wx01", channel="wechat", session=Session("wx01", "p", "private"), dir="out", type="text",
                  text="我方发的", ts_ms=T0, source="chatlog", self=True, state="SENDING", trace_id="TR1")
    rig.store.ingest(out, now_ms=T0)
    rig.fake.add_row(talker="p", content="我方发的", seq=1, ts_ms=T0 + 1000, is_self=True)
    rig.fake.add_row(talker="p", content="手机上手打的", seq=2, ts_ms=T0 + 2000, is_self=True)
    await rig.poller.poll(view(rig))
    origins = [json.loads(e["payload_json"])["origin"] for e in rig.store.list_events(event="message")]
    assert origins == ["rpa", "external"]
    rig.store.close()


async def test_revoke_updates_existing_row_and_emits_once_more(tmp_path):
    """06 §2.9.4:同 seq 且 ``isRevoked`` 变真 ⇒ 更新旧行(changed)并再发一条 ``message`` 事件。"""
    rig = make_wechat_rig(tmp_path)
    row = rig.fake.add_row(talker="p", content="说错了", seq=1, ts_ms=T0)
    await rig.poller.poll(view(rig))
    row["isRevoked"] = True
    rig.store.cursor_set("wx01", CURSOR_PREFIX + "p", 0)
    assert await rig.poller.poll(view(rig)) == 1
    assert rig.store.list_messages("wx01")[0]["revoked"] == 1
    assert len(rig.store.list_events(event="message")) == 2
    rig.store.close()


async def test_read_failure_streak_reaches_key_fail_and_resets(tmp_path):
    """05 §2.5.4「微信(chatlog 挂、微信在线)」:连续 3 次 ``wechat/read`` 异常 ⇒ ``degraded(KEY_FAIL)``(**不是掉线**);
    poller 自己不改账号状态,只维护计数。"""
    rig = make_wechat_rig(tmp_path)
    rig.fake.offline = True
    for i in range(READ_FAIL_STREAK):
        assert await rig.poller.poll(view(rig)) == 0
        assert rig.poller.degraded_key_fail("wx01") is (i + 1 >= READ_FAIL_STREAK)
    assert rig.store.get_account_full("wx01")["state"] == "running"      # 状态不由 poller 改
    rig.fake.offline = False
    rig.fake.add_row(talker="p", content="回来了", seq=1, ts_ms=T0)
    await rig.poller.poll(view(rig))
    assert rig.poller.degraded_key_fail("wx01") is False
    rig.store.close()


async def test_confirm_window_switches_interval_to_one_second(tmp_path):
    """05 §2.4.4 ⑦:发送确认期(send 后 ``[bus] confirm_timeout_wechat_ms``)把 5 s 周期加密到 1 s。"""
    rig = make_wechat_rig(tmp_path)
    assert rig.poller.interval_s("wx01") == 5.0
    rig.poller.note_send("wx01")
    assert rig.poller.interval_s("wx01") == 1.0
    rig.clock.advance(rig.cfg.bus.confirm_timeout_wechat_ms + 1)
    assert rig.poller.interval_s("wx01") == 5.0
    rig.store.close()


async def test_per_account_state_is_isolated(tmp_path):
    """内存态每账号各一份(wx01 与 wx02 互不共享)。"""
    rig = make_wechat_rig(tmp_path)
    rig.store.ensure_account("wx02", "wechat", state="running", login_mode="qrcode")
    rig.poller.note_send("wx01")
    assert rig.poller.interval_s("wx01") == 1.0 and rig.poller.interval_s("wx02") == 5.0
    rig.store.close()
