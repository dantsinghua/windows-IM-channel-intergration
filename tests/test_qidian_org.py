"""企点组织架构用合成行，不读真库。"""
from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from qtrade_agent.adapters.qidian.org import attach_org, build_org, load_org_sqlite
from qtrade_agent.ossbox.crypto import open_sealed
from qtrade_agent.ossbox.envelope import pack_piece_envelope
from qtrade_agent.ossbox.schema import validate_catalog

_DEPOSIT = bytes(range(32))
_XUNJIA = bytes(range(32, 64))


def test_部门按上级串起来且根的上级为空():
    org = build_org(
        departments=[(1, 0, "根"), (2, 1, "甲部"), (3, 1, "乙部")],
        people=[("10", "甲", "a-01"), ("11", "乙", "")],
        memberships=[(2, "10", 3), (3, "11", 1)],
        edges=[(2, 20), (3, 5)],
    )
    assert org["departments"] == [
        {"id": "1", "parent_id": "", "name": "根", "order": 0},
        {"id": "2", "parent_id": "1", "name": "甲部", "order": 20},
        {"id": "3", "parent_id": "1", "name": "乙部", "order": 5},
    ]
    assert org["members"] == [
        {"uin": "10", "name": "甲", "account": "a-01", "department_id": "2", "order": 3},
        {"uin": "11", "name": "乙", "account": "", "department_id": "3", "order": 1},
    ]


def test_一人多部门各留一行且无部门的人不丢():
    org = build_org(
        departments=[("d1", "", "根")],
        people=[("u1", "甲", "acc"), ("u2", "乙", "b")],
        memberships=[("d1", "u1", 1), ("d2", "u1", 4)],
    )
    assert [row["department_id"] for row in org["members"] if row["uin"] == "u1"] == ["d1", "d2"]
    assert org["members"][-1] == {
        "uin": "u2", "name": "乙", "account": "b", "department_id": "", "order": 0,
    }


def test_归属里有但名册没有的人仍保留():
    org = build_org(
        departments=[("d1", "0", "根")],
        people=[],
        memberships=[("d1", "u9", None)],
    )
    assert org["members"] == [{
        "uin": "u9", "name": "", "account": "", "department_id": "d1", "order": 0,
    }]


def test_只把组织挂到当前企点号且不改微信群():
    accounts = [
        {"channel": "qidian", "account_uid": "111", "groups": []},
        {"channel": "qidian", "account_uid": "222", "groups": [{"native_id": "g2", "name": "乙群"}]},
        {"channel": "wechat", "account_uid": "wx", "groups": [{"native_id": "100@chatroom", "name": "固收群"}]},
    ]
    org = build_org(
        departments=[("d1", 0, "根")],
        people=[("u1", "甲", "")],
        memberships=[("d1", "u1", 0)],
    )
    attached = attach_org(accounts, "111", org)
    assert attached[0]["org"]["members"][0]["uin"] == "u1"
    assert "org" not in attached[1]
    assert "org" not in attached[2]
    assert attached[1]["groups"] == [{"native_id": "g2", "name": "乙群"}]
    assert attached[2]["groups"] == [{"native_id": "100@chatroom", "name": "固收群"}]
    assert "group_native_ids" not in json.dumps(attached, ensure_ascii=False)
    with pytest.raises(ValueError):
        attach_org(accounts, "333", org)


def test_组织不能挂到微信也不能写进群():
    with pytest.raises(ValueError, match="只能挂在企点"):
        validate_catalog({
            "workbench_id": "wb",
            "accounts": [{
                "channel": "wechat",
                "account_uid": "wx",
                "groups": [{"native_id": "100@chatroom", "name": "固收群"}],
                "org": {"departments": [{"id": "d1"}], "members": []},
            }],
        })
    with pytest.raises(ValueError, match="群配置不能写入组织"):
        attach_org(
            [
                {"channel": "qidian", "account_uid": "111", "groups": []},
                {
                    "channel": "wechat",
                    "account_uid": "wx",
                    "groups": [{"native_id": "100@chatroom", "name": "固收群", "department_id": "d1"}],
                },
            ],
            "111",
            {"departments": [{"id": "d1"}], "members": []},
        )


def test_从明文库读出同样的层级(tmp_path):
    org_path = tmp_path / "org.db"
    mem_path = tmp_path / "member.db"
    org_con = sqlite3.connect(org_path)
    org_con.execute("CREATE TABLE OrgDepartment (uid INTEGER, pid INTEGER, name TEXT)")
    org_con.execute("CREATE TABLE OrgD2D (did INTEGER, ord INTEGER)")
    org_con.execute("CREATE TABLE OrgD2M (did INTEGER, mid TEXT, ord INTEGER)")
    org_con.execute("INSERT INTO OrgDepartment VALUES (1, 0, '根')")
    org_con.execute("INSERT INTO OrgDepartment VALUES (2, 1, '甲部')")
    org_con.execute("INSERT INTO OrgD2D VALUES (2, 7)")
    org_con.execute("INSERT INTO OrgD2M VALUES (2, 'u1', 2)")
    org_con.commit()
    org_con.close()
    mem_con = sqlite3.connect(mem_path)
    mem_con.execute("CREATE TABLE OrgMember (uin TEXT, name TEXT, account TEXT)")
    mem_con.execute("INSERT INTO OrgMember VALUES ('u1', '甲', 'acc')")
    mem_con.execute("INSERT INTO OrgMember VALUES ('u2', '乙', '')")
    mem_con.commit()
    mem_con.close()
    org = load_org_sqlite(org_path, mem_path)
    assert org["departments"][1]["parent_id"] == "1"
    assert org["departments"][1]["order"] == 7
    assert [row["uin"] for row in org["members"]] == ["u1", "u2"]
    assert org["members"][1]["department_id"] == ""


def test_缺表不会当成空组织成功(tmp_path):
    org_path = tmp_path / "org.db"
    mem_path = tmp_path / "member.db"
    sqlite3.connect(org_path).close()
    sqlite3.connect(mem_path).close()
    with pytest.raises(ValueError, match="缺少表"):
        load_org_sqlite(org_path, mem_path)


def _catalog(**org):
    account = {
        "channel": "qidian",
        "account_uid": "100",
        "account_nick": "",
        "groups": [],
    }
    if org:
        account["org"] = org["org"]
    return {"workbench_id": "wb", "accounts": [account]}


def test_旧目录和空组织都能过校验():
    old = validate_catalog(_catalog())
    assert "org" not in old["accounts"][0]
    empty = validate_catalog(_catalog(org={"departments": [], "members": []}))
    assert empty["accounts"][0]["org"]["departments"] == []
    named = validate_catalog(_catalog(org={
        "departments": [{"id": "d1", "parent_id": ""}],
        "members": [{"uin": "u1", "department_id": ""}],
    }))
    assert named["accounts"][0]["org"]["departments"][0]["id"] == "d1"


def test_组织缺id或缺uin会被拒绝():
    with pytest.raises(ValueError, match="部门缺少 id"):
        validate_catalog(_catalog(org={"departments": [{"name": "根"}], "members": []}))
    with pytest.raises(ValueError, match="成员缺少 uin"):
        validate_catalog(_catalog(org={"departments": [], "members": [{"name": "甲"}]}))
    with pytest.raises(ValueError, match="必须是列表"):
        validate_catalog(_catalog(org={"departments": {}, "members": []}))


def test_封套会带上组织并去掉备注():
    body = pack_piece_envelope(validate_catalog(
        {
            "workbench_id": "wb",
            "accounts": [{
                "channel": "qidian",
                "account_uid": "100",
                "groups": [],
                "label": "不要",
                "org": {
                    "departments": [{"id": "d1", "parent_id": "", "name": "根", "order": 0, "label": "部门备注"}],
                    "members": [{"uin": "u1", "name": "甲", "account": "", "department_id": "d1", "order": 0}],
                },
            }],
        },
        ),
        _DEPOSIT,
        _XUNJIA,
    )
    envelope = json.loads(body.decode("utf-8"))
    plain = json.loads(open_sealed(_DEPOSIT, base64.b64decode(envelope["seals"]["deposit"])))
    assert plain["accounts"][0]["org"]["members"][0]["uin"] == "u1"
    assert "label" not in json.dumps(plain, ensure_ascii=False)
