"""目录分片上传：空通讯录不盖掉已有对象，整包 catalog.json 拒绝。"""
from __future__ import annotations

from qtrade_agent.ossbox.catalog_publish import catalog_from_rows, publish_changed
from qtrade_agent.ossbox.crypto import open_sealed


class _Box:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, name: str, body: bytes) -> None:
        self.puts.append((name, body))


def test_空企点通讯录不上传且拒绝整包():
    catalog_accounts = [
        {"id": "qd90", "channel": "qidian", "self_uid": "3007378246", "self_nick": ""},
        {"id": "wx07", "channel": "wechat", "self_uid": "wxid_a", "self_nick": "柜面"},
    ]
    sessions = [{"account_id": "wx07", "kind": "group", "native_id": "g1", "name": "报价群"}]
    pieces = []
    from qtrade_agent.ossbox.catalog_publish import pieces_for
    pieces = pieces_for("anlin-dev", catalog_accounts, sessions)
    names = [name for name, _body in pieces]
    assert "wb/anlin-dev/catalog.json" not in names
    assert "wb/anlin-dev/qidian/3007378246/contacts.json" in names
    assert "wb/anlin-dev/wechat/wxid_a/groups.json" in names
    box = _Box()
    deposit, xunjia = b"d" * 32, b"x" * 32
    state = publish_changed(box, pieces, deposit, xunjia, {})
    put_names = [name for name, _body in box.puts]
    assert "wb/anlin-dev/qidian/3007378246/contacts.json" not in put_names
    assert "wb/anlin-dev/index.json" in put_names
    assert "wb/anlin-dev/wechat/wxid_a/groups.json" in put_names
    again = publish_changed(box, pieces, deposit, xunjia, state)
    assert len(box.puts) == 2
    assert again == state
    index = next(body for name, body in box.puts if name.endswith("/index.json"))
    import base64
    import json
    plain = json.loads(open_sealed(xunjia, base64.b64decode(json.loads(index)["seals"]["xunjia"])))
    assert plain["accounts"][0]["account_uid"] == "3007378246"
    assert "org" not in plain["accounts"][0]


def test_同一号码有两个采集端才分目录():
    rows = catalog_from_rows("anlin-dev", [
        {"id": "wx05", "channel": "wechat", "self_uid": "wxid_a", "self_nick": ""},
        {"id": "wx07", "channel": "wechat", "self_uid": "wxid_a", "self_nick": ""},
    ], [])
    assert rows["accounts"][0]["collector_id"] == "wx05"
    assert rows["accounts"][1]["collector_id"] == "wx07"
