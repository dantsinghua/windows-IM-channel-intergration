"""企点组织架构上报。合成部门和合成号码，不连真实库，不上报 OSS，不读存款配置。

锁住：私人好友仍走通讯录接口；组织按原始层级保留；只挂当前企点号；
目录字段名是 org；校验与封套保留 org 并去掉 label；org 不进微信群，也不改存款绑定。
"""
from __future__ import annotations

import base64
import copy
import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from qtrade_agent.adapters.qidian.contacts import list_private_contacts
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.org import attach_org, build_org, load_org_sqlite
from qtrade_agent.adapters.qidian.xor import cxor, xor
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig
from qtrade_agent.ossbox.bind import deposit_allows
from qtrade_agent.ossbox.crypto import open_sealed
from qtrade_agent.ossbox.envelope import pack_piece_envelope
from qtrade_agent.ossbox.schema import validate_catalog
from tests.conftest import Clock

_SELF_A = "9100000001"
_SELF_B = "9200000001"
_FRIEND = "9100000301"
_EXTERNAL = "9100000402"
_MEMBER = "9100000101"
_MEMBER_FREE = "9100000102"
_MEMBER_B = "9100000501"
_WX = "wxid_synth"
_WECHAT_GROUPS = [
    {"native_id": "g-synth-1", "name": "合成固收群"},
    {"native_id": "g-synth-2", "name": "合成存单群"},
]
_DEPOSIT = bytes(range(32))
_XUNJIA = bytes(range(32, 64))


def _blob_name(text: str) -> bytes:
    return cxor(text).encode("utf-8")


def _blob_uin(uin: str) -> bytes:
    return xor(uin.encode("ascii"))


def _tree_a() -> dict:
    """子部门排在根前面，用来锁住不重排、不合并。"""
    return build_org(
        departments=[
            ("dept-child", "dept-root", "合成二部"),
            ("dept-root", 0, "合成一部"),
            ("", 0, "空编号部门"),
        ],
        people=[
            (_MEMBER, "合成甲", "acct-jia"),
            (_MEMBER_FREE, "合成乙", "acct-yi"),
            ("", "空号码", "acct-empty"),
        ],
        memberships=[
            ("dept-root", _MEMBER, 2),
            ("dept-child", _MEMBER, 1),
            ("dept-root", "", 8),
        ],
        edges=[("dept-child", 10), ("dept-root", 20)],
    )


def _binding() -> dict:
    return {
        "wxid": _WX,
        "group_native_ids": ["g-synth-1", "g-synth-2"],
        "workbench_id": "wb-synth",
    }


def _accounts() -> list[dict]:
    return [
        {
            "channel": "wechat",
            "account_uid": _WX,
            "account_nick": "合成微信",
            "groups": copy.deepcopy(_WECHAT_GROUPS),
        },
        {
            "channel": "qidian",
            "account_uid": _SELF_A,
            "account_nick": "合成企点甲",
            "groups": [{"native_id": "g-qd-synth", "name": "合成企点群"}],
            "contacts": [
                {"uin": _FRIEND, "name": "合成好友", "remark": "合成备注", "kind": "private"},
            ],
        },
        {
            "channel": "qidian",
            "account_uid": _SELF_B,
            "account_nick": "合成企点乙",
            "groups": [],
        },
        {
            "channel": "qq",
            "account_uid": "9300000001",
            "account_nick": "合成QQ",
            "groups": [{"native_id": "g-qq-synth", "name": "合成QQ群"}],
        },
    ]


def _catalog(accounts: list[dict]) -> dict:
    return {"workbench_id": "wb-synth", "label": "工作台备注", "accounts": accounts}


def _open_plain(body: bytes) -> dict:
    envelope = json.loads(body.decode("utf-8"))
    plains = []
    for slot, key in (("deposit", _DEPOSIT), ("xunjia", _XUNJIA)):
        blob = base64.b64decode(envelope["seals"][slot], validate=True)
        plains.append(json.loads(open_sealed(key, blob).decode("utf-8")))
    assert plains[0] == plains[1]
    return plains[0]


def _write_org_db(org_path, member_path, departments, edges, memberships, people) -> None:
    org_con = sqlite3.connect(org_path)
    org_con.execute("CREATE TABLE OrgDepartment (uid TEXT, pid TEXT, name TEXT)")
    org_con.execute("CREATE TABLE OrgD2D (did TEXT, ord INTEGER)")
    org_con.execute("CREATE TABLE OrgD2M (did TEXT, mid TEXT, ord INTEGER)")
    org_con.executemany("INSERT INTO OrgDepartment VALUES (?,?,?)", departments)
    org_con.executemany("INSERT INTO OrgD2D VALUES (?,?)", edges)
    org_con.executemany("INSERT INTO OrgD2M VALUES (?,?,?)", memberships)
    org_con.commit()
    org_con.close()
    mem_con = sqlite3.connect(member_path)
    mem_con.execute("CREATE TABLE OrgMember (uin TEXT, name TEXT, account TEXT)")
    mem_con.executemany("INSERT INTO OrgMember VALUES (?,?,?)", people)
    mem_con.commit()
    mem_con.close()


def test_私人好友接口不收组织架构成员(tmp_path):
    db_path = tmp_path / "9100000001.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE Friends (uin BLOB, remark BLOB, name BLOB)")
    con.execute("CREATE TABLE QidianExternalInfo (uin BLOB, nickname BLOB)")
    con.execute("CREATE TABLE OrgMember (uin TEXT, name TEXT, account TEXT)")
    con.execute(
        "INSERT INTO Friends VALUES (?,?,?)",
        (_blob_uin(_FRIEND), _blob_name("合成备注"), _blob_name("合成好友")),
    )
    con.execute(
        "INSERT INTO QidianExternalInfo VALUES (?,?)",
        (_blob_uin(_EXTERNAL), _blob_name("合成外部")),
    )
    con.execute("INSERT INTO OrgMember VALUES (?,?,?)", (_MEMBER, "合成甲", "acct-jia"))
    con.commit()
    con.close()

    friends = list_private_contacts(LocalSqliteMainDb(str(db_path)).list_contact_source_rows())
    assert [row["uin"] for row in friends] == [_FRIEND, _EXTERNAL]
    assert friends[0] == {"uin": _FRIEND, "name": "合成备注", "remark": "合成备注", "kind": "private"}
    assert all(row["kind"] == "private" and "department_id" not in row for row in friends)

    clock = Clock()
    agent = AgentApp(
        AgentConfig(),
        db_path=str(tmp_path / "agent.db"),
        clock=clock,
        maindb_factory=lambda uid, acct: LocalSqliteMainDb(str(db_path)),
    ).open()
    agent.store.ensure_account("qd01", "qidian", state="running", self_uid=_SELF_A, label="合成台")
    agent.store.upsert_api_client(app_id="panel", name="询价", level="read", token="panel-token")
    api = agent.create_api()
    try:
        with TestClient(api, client=("127.0.0.1", 40000)) as client:
            response = client.get(
                "/api/v1/accounts/qd01/contacts",
                headers={"Authorization": "Bearer panel-token", "X-QT-Api-Min": "1.0"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [row["uin"] for row in body["data"]] == [_FRIEND, _EXTERNAL]
        assert "org" not in body and "departments" not in body
        dumped = json.dumps(body["data"], ensure_ascii=False)
        assert _MEMBER not in dumped
        assert "合成甲" not in dumped
        assert "dept-root" not in dumped
    finally:
        agent.store.close()


def test_组织架构保留层级且多部门不合并():
    tree = _tree_a()
    assert tree["departments"] == [
        {"id": "dept-child", "parent_id": "dept-root", "name": "合成二部", "order": 10},
        {"id": "dept-root", "parent_id": "", "name": "合成一部", "order": 20},
    ]
    assert tree["members"] == [
        {
            "uin": _MEMBER,
            "name": "合成甲",
            "account": "acct-jia",
            "department_id": "dept-root",
            "order": 2,
        },
        {
            "uin": _MEMBER,
            "name": "合成甲",
            "account": "acct-jia",
            "department_id": "dept-child",
            "order": 1,
        },
        {
            "uin": _MEMBER_FREE,
            "name": "合成乙",
            "account": "acct-yi",
            "department_id": "",
            "order": 0,
        },
    ]
    same = [row for row in tree["members"] if row["uin"] == _MEMBER]
    assert len(same) == 2
    assert {row["department_id"] for row in same} == {"dept-root", "dept-child"}


def test_换库读取不会串到另一个企点号(tmp_path):
    org_a = tmp_path / "a-org.db"
    mem_a = tmp_path / "a-member.db"
    org_b = tmp_path / "b-org.db"
    mem_b = tmp_path / "b-member.db"
    _write_org_db(
        org_a,
        mem_a,
        [("dept-child", "dept-root", "合成二部"), ("dept-root", "0", "合成一部")],
        [("dept-child", 10), ("dept-root", 20)],
        [("dept-root", _MEMBER, 2), ("dept-child", _MEMBER, 1)],
        [(_MEMBER, "合成甲", "acct-jia"), (_MEMBER_FREE, "合成乙", "acct-yi")],
    )
    _write_org_db(
        org_b,
        mem_b,
        [("dept-b-only", "0", "乙号根部门")],
        [("dept-b-only", 4)],
        [("dept-b-only", _MEMBER_B, 4)],
        [(_MEMBER_B, "乙号成员", "acct-b2")],
    )
    read_a = load_org_sqlite(org_a, mem_a)
    read_b = load_org_sqlite(org_b, mem_b)
    read_a_again = load_org_sqlite(org_a, mem_a)
    assert read_a_again == read_a
    assert {row["id"] for row in read_a["departments"]} == {"dept-root", "dept-child"}
    assert {row["id"]: row["parent_id"] for row in read_a["departments"]}["dept-root"] == ""
    assert {row["id"]: row["parent_id"] for row in read_a["departments"]}["dept-child"] == "dept-root"
    assert [row["uin"] for row in read_b["members"]] == [_MEMBER_B]
    assert "dept-root" not in json.dumps(read_b, ensure_ascii=False)
    assert _MEMBER not in json.dumps(read_b, ensure_ascii=False)
    assert "dept-b-only" not in json.dumps(read_a, ensure_ascii=False)

    accounts = _accounts()
    attached = attach_org(accounts, _SELF_A, read_a)
    both = attach_org(attached, _SELF_B, read_b)
    by_uid = {row["account_uid"]: row for row in both}
    assert by_uid[_SELF_A]["org"]["departments"] == read_a["departments"]
    assert {row["uin"] for row in by_uid[_SELF_B]["org"]["members"]} == {_MEMBER_B}
    assert _MEMBER not in json.dumps(by_uid[_SELF_B]["org"], ensure_ascii=False)
    assert "org" not in by_uid[_WX]
    assert "org" not in by_uid["9300000001"]


def test_org只挂企点且不改微信群和存款绑定():
    tree = _tree_a()
    accounts = _accounts()
    binding = _binding()
    groups_before = copy.deepcopy(accounts[0]["groups"])
    qidian_groups_before = copy.deepcopy(accounts[1]["groups"])
    qq_groups_before = copy.deepcopy(accounts[3]["groups"])
    ids_before = list(binding["group_native_ids"])
    allowed = {
        "channel": "wechat",
        "account_uid": _WX,
        "session": {"kind": "group", "native_id": "g-synth-1"},
    }
    member_as_group = {
        "channel": "wechat",
        "account_uid": _WX,
        "session": {"kind": "group", "native_id": _MEMBER},
    }

    attached = attach_org(accounts, _SELF_A, tree)
    assert accounts[0]["groups"] == groups_before
    assert binding["group_native_ids"] == ids_before
    assert [row.get("channel") for row in attached if "org" in row] == ["qidian"]
    assert attached[0]["groups"] == groups_before
    assert attached[1]["groups"] == qidian_groups_before
    assert attached[3]["groups"] == qq_groups_before
    assert "org" not in attached[0] and "org" not in attached[2] and "org" not in attached[3]
    org_blob = json.dumps(attached[1]["org"], ensure_ascii=False)
    group_blob = json.dumps(attached[0]["groups"], ensure_ascii=False)
    assert _MEMBER not in group_blob
    assert "合成甲" not in group_blob
    assert "dept-root" not in group_blob
    assert _FRIEND not in org_blob
    assert attached[1]["contacts"][0]["uin"] == _FRIEND

    checked = validate_catalog(_catalog(attached))
    plain = _open_plain(pack_piece_envelope(validate_catalog(_catalog(attached)), _DEPOSIT, _XUNJIA))
    for view in (checked, plain):
        found = [row for row in view["accounts"] if "org" in row]
        assert [row["channel"] for row in found] == ["qidian"]
        assert found[0]["account_uid"] == _SELF_A
        wechat = next(row for row in view["accounts"] if row["channel"] == "wechat")
        qidian = next(row for row in view["accounts"] if row["account_uid"] == _SELF_A)
        qq = next(row for row in view["accounts"] if row["channel"] == "qq")
        assert wechat["groups"] == groups_before
        assert qidian["groups"] == qidian_groups_before
        assert qq["groups"] == qq_groups_before
        assert "org" not in wechat and "org" not in qq
        assert _MEMBER not in json.dumps(wechat["groups"], ensure_ascii=False)
        assert "group_native_ids" not in json.dumps(view, ensure_ascii=False)
        assert view["accounts"][1]["org"]["members"] == tree["members"]
    assert "label" not in json.dumps(plain, ensure_ascii=False)
    assert binding["group_native_ids"] == ids_before
    assert deposit_allows(binding, allowed) is True
    assert deposit_allows(binding, member_as_group) is False

    mixed = _catalog(attached)
    mixed["accounts"][0]["org"] = copy.deepcopy(tree)
    with pytest.raises(ValueError):
        validate_catalog(mixed)
    with pytest.raises(ValueError):
        pack_piece_envelope(validate_catalog(mixed), _DEPOSIT, _XUNJIA)
    same_uid_wechat = [
        {"channel": "wechat", "account_uid": _SELF_A, "groups": copy.deepcopy(_WECHAT_GROUPS)},
        {"channel": "qidian", "account_uid": _SELF_A, "groups": []},
    ]
    only_qidian = attach_org(same_uid_wechat, _SELF_A, tree)
    assert "org" not in only_qidian[0]
    assert only_qidian[0]["groups"] == _WECHAT_GROUPS
    assert "org" in only_qidian[1]


def test_旧目录没有org仍然通过且空列表合法():
    old = {
        "workbench_id": "wb-synth",
        "accounts": [
            {
                "channel": "wechat",
                "account_uid": _WX,
                "account_nick": "",
                "groups": copy.deepcopy(_WECHAT_GROUPS),
            },
            {
                "channel": "qidian",
                "account_uid": _SELF_A,
                "account_nick": "合成企点甲",
                "groups": [],
            },
        ],
    }
    checked = validate_catalog(old)
    assert "org" not in checked["accounts"][0]
    assert "org" not in checked["accounts"][1]
    assert checked["accounts"][0]["groups"] == _WECHAT_GROUPS
    empty = validate_catalog({
        "workbench_id": "wb-synth",
        "accounts": [{
            "channel": "qidian",
            "account_uid": _SELF_A,
            "groups": [],
            "org": {"departments": [], "members": []},
        }],
    })
    assert empty["accounts"][0]["org"] == {"departments": [], "members": []}


def test_org存在时列表和编号必须合法且不能挂到非企点():
    good = validate_catalog(_catalog(attach_org(_accounts(), _SELF_A, _tree_a())))
    assert good["accounts"][1]["org"]["departments"][1]["parent_id"] == ""
    assert good["accounts"][1]["org"]["members"][2]["department_id"] == ""

    qidian = {
        "channel": "qidian",
        "account_uid": _SELF_A,
        "account_nick": "合成企点甲",
        "groups": [],
    }
    bad_orgs = [
        {"departments": {"id": "dept-root"}, "members": []},
        {"departments": [], "members": {"uin": _MEMBER}},
        {"members": []},
        {"departments": []},
        {"departments": [{"id": "", "parent_id": "", "name": "合成一部", "order": 1}], "members": []},
        {"departments": [{"id": "   ", "parent_id": "", "name": "合成一部", "order": 1}], "members": []},
        {"departments": [{"parent_id": "", "name": "合成一部", "order": 1}], "members": []},
        {"departments": ["dept-root"], "members": []},
        {
            "departments": [],
            "members": [{
                "uin": "",
                "name": "合成甲",
                "account": "acct-jia",
                "department_id": "",
                "order": 1,
            }],
        },
        {
            "departments": [],
            "members": [{
                "uin": "   ",
                "name": "合成甲",
                "account": "acct-jia",
                "department_id": "",
                "order": 1,
            }],
        },
        {
            "departments": [],
            "members": [{
                "name": "合成甲",
                "account": "acct-jia",
                "department_id": "",
                "order": 1,
            }],
        },
        {"departments": [], "members": [_MEMBER]},
        [],
        "dept-root",
    ]
    for org in bad_orgs:
        account = dict(qidian)
        account["org"] = org
        with pytest.raises(ValueError):
            validate_catalog({"workbench_id": "wb-synth", "accounts": [account]})

    for channel in ("wechat", "qq"):
        account = {
            "channel": channel,
            "account_uid": "acct-synth",
            "groups": [],
            "org": {"departments": [], "members": []},
        }
        with pytest.raises(ValueError):
            validate_catalog({"workbench_id": "wb-synth", "accounts": [account]})


def test_封套保留org并去掉各层备注():
    tree = _tree_a()
    tree = copy.deepcopy(tree)
    tree["label"] = "树备注"
    tree["departments"][0]["label"] = "部门备注"
    tree["members"][0]["label"] = "成员备注"
    accounts = _accounts()
    accounts[1]["label"] = "账号备注"
    attached = attach_org(accounts, _SELF_A, tree)
    plain = _open_plain(pack_piece_envelope(validate_catalog(_catalog(attached)), _DEPOSIT, _XUNJIA))
    dumped = json.dumps(plain, ensure_ascii=False)
    assert "label" not in dumped
    assert "树备注" not in dumped
    assert "部门备注" not in dumped
    assert "成员备注" not in dumped
    org = next(row["org"] for row in plain["accounts"] if row["channel"] == "qidian" and "org" in row)
    assert org["departments"] == _tree_a()["departments"]
    assert org["members"] == _tree_a()["members"]
    assert sum(1 for row in org["members"] if row["uin"] == _MEMBER) == 2
    assert any(row["department_id"] == "" for row in org["members"])
    wechat = next(row for row in plain["accounts"] if row["channel"] == "wechat")
    assert wechat["groups"] == _WECHAT_GROUPS
    assert _DEPOSIT not in pack_piece_envelope(validate_catalog(_catalog(attached)), _DEPOSIT, _XUNJIA)
