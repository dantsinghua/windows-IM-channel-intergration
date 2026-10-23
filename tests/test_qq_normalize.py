"""归一化:OneBot 事件 → 基线 §7.4 Message(docs/06 §2.9.1 / §2.9.2 QQ 行、docs/02 §2.8.1 QQ 行)。"""
from __future__ import annotations

import pytest

from qtrade_agent.adapters.qq.normalize import (action_for, ext_msg_id, history_action_for, is_self_event,
                                                message_seq_of, native_id_of, segments, sender_of, split_session,
                                                text_type_media, to_message)
from test_qq_common import PEER, SELF_UID, group_event, private_event

TS = 1_758_240_000


# ---------------------------------------------------------------- native_id / ext_msg_id
def test_native_id_group_prefix():
    """06 §2.9.1:群 = ``g_<群号>``(与企点同惯例,防群号与对端 uin 数值相撞)。"""
    assert native_id_of(message_type="group", group_id=123456) == ("g_123456", "group")


def test_native_id_private_is_peer_uin():
    assert native_id_of(message_type="private", user_id=415011447) == ("415011447", "private")


def test_native_id_self_private_prefers_target_id():
    """``message_sent`` 里 ``user_id`` 是自己、对端在 ``target_id``。"""
    assert native_id_of(message_type="private", user_id=1, target_id=999, is_self=True) == ("999", "private")


def test_native_id_self_private_falls_back_to_user_id():
    assert native_id_of(message_type="private", user_id=999, is_self=True) == ("999", "private")


def test_native_id_group_without_group_id_raises():
    with pytest.raises(ValueError):
        native_id_of(message_type="group")


def test_native_id_private_without_peer_raises():
    with pytest.raises(ValueError):
        native_id_of(message_type="private")


def test_ext_msg_id_embeds_session():
    """06 §2.9.2 R6-51 订正:会话编进 ext,跨群同号不撞。"""
    assert ext_msg_id("g_123456", 42) == "g_123456:42"
    assert ext_msg_id("415011447", 42) == "415011447:42"


# ---------------------------------------------------------------- 段 → 正文/type/media
def test_segments_accepts_string_message():
    assert segments("hi") == [{"type": "text", "data": {"text": "hi"}}]


def test_segments_accepts_none_and_dict():
    assert segments(None) == []
    assert segments({"type": "text", "data": {"text": "a"}})[0]["type"] == "text"


def test_text_only_is_type_text():
    text, mtype, media = text_type_media([{"type": "text", "data": {"text": "甲"}}, {"type": "text", "data": {"text": "乙"}}])
    assert (text, mtype, media) == ("甲乙", "text", [])


def test_empty_message_is_type_text():
    assert text_type_media([]) == ("", "text", [])


def test_mixed_takes_first_non_text_type_and_keeps_text():
    """06 §2.9.1:多段混合取首个非 text 段的类型,正文保留 text 段。"""
    text, mtype, media = text_type_media([
        {"type": "text", "data": {"text": "看图"}},
        {"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}},
        {"type": "video", "data": {"file": "b.mp4"}},
    ])
    assert text == "看图"
    assert mtype == "image"
    assert [m["kind"] for m in media] == ["image", "video"]
    assert media[0]["url"] == "http://x/a.jpg" and media[0]["state"] == "pending"


def test_record_maps_to_voice():
    assert text_type_media([{"type": "record", "data": {"file": "v.amr"}}])[1] == "voice"


def test_file_segment_maps_to_file():
    assert text_type_media([{"type": "file", "data": {"file": "x.pdf"}}])[1] == "file"


def test_unknown_segment_type_is_unknown_and_not_media():
    text, mtype, media = text_type_media([{"type": "forward", "data": {"id": "1"}}])
    assert (text, mtype, media) == ("", "unknown", [])


# ---------------------------------------------------------------- sender / self
def test_sender_prefers_card_over_nickname():
    assert sender_of({"user_id": 1, "sender": {"user_id": 1, "nickname": "昵称", "card": "名片"}}) == ("1", "名片")


def test_sender_falls_back_to_event_user_id():
    assert sender_of({"user_id": 7}) == ("7", None)


def test_is_self_by_user_id_equals_self_id():
    assert is_self_event({"self_id": 5, "user_id": 5}) is True
    assert is_self_event({"self_id": 5, "user_id": 6}) is False


def test_is_self_by_message_sent_post_type():
    assert is_self_event({"post_type": "message_sent", "self_id": 5, "user_id": 5}) is True


# ---------------------------------------------------------------- to_message
def test_to_message_inbound_private_fields():
    m = to_message("qq03", private_event(message_id=7, text="你好", time_s=TS))
    assert m.channel == "qq" and m.source == "onebot" and m.dir == "in" and m.self is False
    assert m.session.id == f"qq03:{PEER}" and m.session.kind == "private"
    assert m.ext_msg_id == f"{PEER}:7" and m.dedup_kind == "native"
    assert m.ts_ms == TS * 1000 and m.text == "你好" and m.type == "text"
    assert m.state == "DELIVERED"


def test_to_message_inbound_group_session_kind():
    m = to_message("qq03", group_event(message_id=8, time_s=TS))
    assert m.session.native_id == "g_123456" and m.session.kind == "group"
    assert m.ext_msg_id == "g_123456:8" and m.sender_name == "群名片"


def test_to_message_self_is_out_direction():
    ev = private_event(message_id=9, time_s=TS, user_id=SELF_UID)
    ev["target_id"] = int(PEER)
    ev["post_type"] = "message_sent"
    m = to_message("qq03", ev, self_uid=SELF_UID)
    assert m.dir == "out" and m.self is True and m.session.native_id == PEER
    assert m.sender_id == SELF_UID


def test_to_message_does_not_run_clean_text():
    """06 §2.9.2 分工:``clean_text`` 只在企点读库路调用一次;QQ 推型不清洗、不改写正文。"""
    m = to_message("qq03", private_event(message_id=10, text="甲\u0014A乙", time_s=TS))
    assert m.text == "甲\u0014A乙"


def test_to_message_carries_raw_ref():
    m = to_message("qq03", private_event(message_id=11, time_s=TS), raw_ref="{}")
    assert m.raw_ref == "{}"


# ---------------------------------------------------------------- session 参数 / action 构造
def test_split_session_accepts_session_id_and_native():
    assert split_session("qq03", f"qq03:{PEER}") == (PEER, "private")
    assert split_session("qq03", PEER) == (PEER, "private")
    assert split_session("qq03", "qq03:g_123456") == ("g_123456", "group")


def test_split_session_rejects_empty_native():
    with pytest.raises(ValueError):
        split_session("qq03", "qq03:")


def test_action_for_group_and_private():
    assert action_for("g_123456", "group", "hi") == ("send_group_msg", {"group_id": 123456, "message": [{"type": "text", "data": {"text": "hi"}}]})
    assert action_for(PEER, "private", "hi")[0] == "send_private_msg"


def test_history_action_for_matches_spec_names():
    """02 §2.8.1 QQ 行(P-13):``get_group_msg_history`` / ``get_friend_msg_history``。"""
    assert history_action_for("g_123456", "group", 50) == ("get_group_msg_history", {"group_id": 123456, "count": 50})
    assert history_action_for(PEER, "private", 50) == ("get_friend_msg_history", {"user_id": int(PEER), "count": 50})


def test_message_seq_of_reads_known_keys():
    assert message_seq_of({"message_seq": 12}) == 12
    assert message_seq_of({"real_seq": "13"}) == 13
    assert message_seq_of({}) is None
