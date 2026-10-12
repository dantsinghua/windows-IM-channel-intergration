"""目录拆分：总索引不含群和通讯录，改一个账号不动其它文件。"""
from __future__ import annotations

import base64
import json

from qtrade_agent.ossbox.crypto import open_sealed
from qtrade_agent.ossbox.envelope import pack_piece_envelope
from qtrade_agent.ossbox.split import catalog_pieces


def test_索引只有账号_群和通讯录分文件且互不影响():
    base = {
        "workbench_id": "anlin-dev",
        "updated_at": 10,
        "accounts": [
            {
                "channel": "wechat",
                "account_uid": "wxid_a",
                "account_nick": "柜面",
                "collector_id": "wx01",
                "groups": [{"native_id": "g1", "name": "报价群"}],
                "label": "不要出现",
            },
            {
                "channel": "qidian",
                "account_uid": "3007378246",
                "account_nick": "企点",
                "collector_id": "qd90",
                "contacts": [{"uin": "41501", "name": "王磊"}],
                "org": {"departments": [{"id": "d1"}], "members": [{"uin": "41501"}]},
            },
        ],
    }
    changed = json.loads(json.dumps(base))
    changed["accounts"][1]["contacts"][0]["name"] = "王磊二"

    first = dict(catalog_pieces(base))
    second = dict(catalog_pieces(changed))
    index = first["wb/anlin-dev/index.json"]
    dumped = json.dumps(index, ensure_ascii=False)
    for account in index["accounts"]:
        assert "groups" not in account
        assert "contacts" not in account
        assert "org" not in account
    assert "不要出现" not in dumped
    assert [item["account_uid"] for item in index["accounts"]] == ["wxid_a", "3007378246"]
    assert index["accounts"][0]["book"] == "wx01/wechat/wxid_a/groups.json"
    assert index["accounts"][1]["book"] == "qd90/qidian/3007378246/contacts.json"

    groups_name = "wb/anlin-dev/wx01/wechat/wxid_a/groups.json"
    contacts_name = "wb/anlin-dev/qd90/qidian/3007378246/contacts.json"
    assert first[groups_name] == second[groups_name]
    assert first[contacts_name] != second[contacts_name]
    assert "contacts" not in json.dumps(first[groups_name], ensure_ascii=False)
    assert first[contacts_name]["org"]["departments"][0]["id"] == "d1"
    assert "groups" not in json.dumps(first[contacts_name], ensure_ascii=False)
    assert "catalog.json" not in first
    deposit = b"d" * 32
    xunjia = b"x" * 32
    packed = pack_piece_envelope(index, deposit, xunjia)
    assert b"catalog.json" not in packed
    opened = json.loads(open_sealed(xunjia, base64.b64decode(json.loads(packed)["seals"]["xunjia"])))
    assert opened["accounts"][1]["account_uid"] == "3007378246"
    assert "groups" not in opened["accounts"][0]
