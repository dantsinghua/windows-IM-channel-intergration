"""邮件中转和公网上报必须解成同一种服务调用。"""
from qtrade_agent.ossbox.services import equivalent, from_mail, from_oss, reconcile


def test_同一条接收在两种通道上效果相同():
    oss = from_oss({
        "op": "recv_message",
        "channel": "wechat",
        "account_uid": "wxid_a",
        "text": "3M 1.52 可谈",
        "seq": 2,
        "seq_total": 5,
        "ext_msg_id": "m2",
        "image_kind": "原图",
        "session": {"kind": "group", "native_id": "grp-1"},
    })
    mail = from_mail({
        "channel": "wechat",
        "account_uid": "wxid_a",
        "文本消息内容": "3M 1.52 可谈",
        "消息序号": "2/5",
        "消息ID": "m2",
        "图片类型": "原图",
        "群聊": "grp-1",
        "kind": "group",
    })
    assert equivalent(oss, mail)
    assert oss is not None and oss.effect()[0] == "recv_message"


def test_缩略图和表情包两种通道都不形成调用():
    body = {
        "channel": "wechat",
        "account_uid": "wxid_a",
        "text": "x",
        "ext_msg_id": "m",
        "session": {"kind": "group", "native_id": "grp-1"},
    }
    assert from_oss({**body, "image_kind": "缩略图"}) is None
    assert from_oss({**body, "image_kind": "表情包"}) is None
    assert from_mail({"图片类型": "缩略图", "文本消息内容": "x"}) is None
    assert from_mail({"图片类型": "表情包", "文本消息内容": "x"}) is None


def test_单发群发通讯录原图和接收范围():
    one = from_oss({
        "op": "send_text",
        "channel": "qq",
        "account_uid": "415011447",
        "text": "测试",
        "session": {"kind": "private", "native_id": "3007378246"},
    })
    many = from_oss({
        "op": "send_many",
        "channel": "qq",
        "account_uid": "415011447",
        "text": "测试",
        "targets": ["3007378246", "grp-1"],
    })
    book = from_oss({"op": "list_contacts", "channel": "wechat", "account_uid": "wxid_a"})
    image = from_oss({
        "op": "fetch_original_image",
        "channel": "wechat",
        "account_uid": "wxid_a",
        "image_kind": "原图",
        "ext_msg_id": "m1",
        "session": {"kind": "private", "native_id": "filehelper"},
    })
    scope = from_oss({
        "op": "set_scope",
        "channel": "wechat",
        "account_uid": "wxid_a",
        "scope": ["grp-1"],
    })
    assert one is not None and one.op == "send_one" and one.targets == ("3007378246",)
    assert many is not None and many.targets == ("3007378246", "grp-1")
    assert book is not None and book.op == "list_contacts"
    assert image is not None and image.image_kind == "原图"
    assert scope is not None and scope.scope == ("grp-1",)
    assert equivalent(one, from_mail({
        "op": "send_one",
        "channel": "qq",
        "account_uid": "415011447",
        "文本消息内容": "测试",
        "native_id": "3007378246",
        "kind": "private",
    }))


def test_序号对账缺号少则补发多则告警():
    assert reconcile([1, 2, 3], 3)["action"] == "ok"
    missed = reconcile([1, 3], 4)
    assert missed["action"] == "resend" and missed["gaps"] == [2, 4]
    loud = reconcile([1], 8)
    assert loud["action"] == "alert" and loud["gaps"] == [2, 3, 4, 5, 6, 7, 8]
