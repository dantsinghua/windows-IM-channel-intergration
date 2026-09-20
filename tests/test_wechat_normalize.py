"""chatlog 行 → Message 的归一化(06 §2.9.1 微信行 + §2.9.2 去重键)。"""
from __future__ import annotations

import pytest

from qtrade_agent.adapters.wechat.normalize import TYPE_MAP, ext_msg_id, session_kind, to_message


def test_ext_msg_id_is_talker_colon_seq():
    """06 §2.9.2 C-22:微信 native 去重键 = ``{talker}:{seq}``(带 talker,与 cursors.kind=chatlog_seq:<talker> 配套)。"""
    assert ext_msg_id("wxid_peer", 17) == "wxid_peer:17"
    assert ext_msg_id("12345@chatroom", 1) == "12345@chatroom:1"


def test_session_kind_by_chatroom_suffix():
    assert session_kind("12345@chatroom") == "group"
    assert session_kind("wxid_peer") == "private"


@pytest.mark.parametrize("raw,expect", [(1, "text"), (3, "image"), (34, "voice"), (49, "file"), (43, "video"), (10000, "unknown")])
def test_type_map_five_kinds_plus_unknown(raw, expect):
    """06 §2.9.1:1=text、3=image、34=voice、49 含 file、43=video,其余 unknown。"""
    m = to_message("wx01", {"talker": "wxid_peer", "seq": 1, "type": raw, "content": "x", "time": 1_758_240_000})
    assert m.type == expect
    assert TYPE_MAP.get(raw, "unknown") == expect


def test_self_row_becomes_out_direction():
    """06 §2.12:``isSelf`` 的行按出向读回行构造(dir=out、self=True、state=DELIVERED),交给 ingest 合并。"""
    m = to_message("wx01", {"talker": "wxid_peer", "seq": 9, "type": 1, "content": "在", "time": 1_758_240_000, "isSelf": True},
                   self_uid="wxid_demo01", self_nick="安琳")
    assert (m.dir, m.self, m.state) == ("out", True, "DELIVERED")
    assert m.sender_id == "wxid_demo01" and m.sender_name == "安琳"
    other = to_message("wx01", {"talker": "wxid_peer", "seq": 10, "type": 1, "content": "在", "time": 1_758_240_000})
    assert (other.dir, other.self) == ("in", False)


def test_time_accepts_seconds_millis_and_iso():
    sec = to_message("wx01", {"talker": "t", "seq": 1, "type": 1, "content": "a", "time": 1_758_240_000})
    ms = to_message("wx01", {"talker": "t", "seq": 2, "type": 1, "content": "a", "time": 1_758_240_000_000})
    iso = to_message("wx01", {"talker": "t", "seq": 3, "type": 1, "content": "a", "time": "2025-09-19T08:00:00+08:00"})
    assert sec.ts_ms == 1_758_240_000_000 == ms.ts_ms
    assert iso.ts_ms == int(1_758_240_000_000)


def test_revoked_reads_three_spellings():
    """06 §2.9.4:collector ``message_to_row`` 读 raw.isRevoked/is_revoked/revoked。"""
    for key in ("isRevoked", "is_revoked", "revoked"):
        m = to_message("wx01", {"talker": "t", "seq": 1, "type": 1, "content": "a", "time": 1, key: True})
        assert m.revoked is True


def test_media_gives_reference_not_bytes_and_no_sha256():
    """06 §2.9.1:媒体给 ``/image/<md5>`` 引用由 Agent 经 #40 拉;这里不下载、不算 sha256
    ⇒ fingerprint 的 media_sha256s 为空(06 §2.9.2 公式)。"""
    m = to_message("wx01", {"talker": "t", "seq": 1, "type": 3, "time": 1, "md5": "abc123"})
    assert m.media == [{"kind": "image", "key": "abc123", "ref": "/wa/v1/wechat/media/abc123"}]
    assert all("sha256" not in x for x in m.media)
    assert to_message("wx01", {"talker": "t", "seq": 2, "type": 1, "time": 1, "content": "a"}).media == []


def test_clean_text_is_not_applied_to_wechat():
    """06 §2.9.2 分工:``clean_text`` 是企点读库解码侧专用,微信正文原样进库(比较侧只过 norm)。"""
    raw = "你好\u0014A世界"
    m = to_message("wx01", {"talker": "t", "seq": 1, "type": 1, "content": raw, "time": 1})
    assert m.text == raw and "[表情]" not in (m.text or "")


def test_dedup_kind_is_native_and_source_is_chatlog():
    m = to_message("wx01", {"talker": "t", "seq": 1, "type": 1, "content": "a", "time": 1})
    assert m.dedup_kind == "native" and m.source == "chatlog" and m.channel == "wechat"
    assert m.session_id == "wx01:t"
