"""历史消息发送人：自己发出的用账号昵称，不显示成空。"""
from qtrade_agent.api.serialize import sender_display_name


def test_self_outgoing_uses_login_nick_for_every_channel():
    for nick in ("Lulu-南银理财", "dantsinghua", "安琳-盈米"):
        assert sender_display_name({
            "dir": "out", "is_self": 1, "sender_name": None,
            "account_self_nick": nick, "account_label": "别的",
        }) == nick


def test_self_outgoing_uses_account_label_when_nick_missing():
    assert sender_display_name({
        "dir": "out", "is_self": 1, "sender_name": "",
        "account_self_nick": None, "account_label": "德儿塔外",
        "session_name": "filehelper", "session_kind": "private",
    }) == "德儿塔外"


def test_private_incoming_uses_session_title():
    assert sender_display_name({
        "dir": "in", "is_self": 0, "sender_name": None,
        "session_kind": "private", "session_name": "安琳", "session_native_id": "415011447",
    }) == "安琳"


def test_group_without_sender_name_stays_empty():
    assert sender_display_name({
        "dir": "in", "is_self": 0, "sender_name": None,
        "session_kind": "group", "session_name": "某个群", "session_native_id": "g1",
    }) is None


def test_stored_sender_name_wins():
    assert sender_display_name({
        "dir": "out", "is_self": 1, "sender_name": "dantsinghua",
        "account_self_nick": "别的",
    }) == "dantsinghua"
