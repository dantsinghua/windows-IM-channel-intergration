"""Independent producer name projection tests with fake native I/O and store."""
import asyncio
import base64
from copy import deepcopy
from types import MethodType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from qtrade_agent.adapters.wechat.client import WeChatWinAgent
from qtrade_agent.app import AgentApp


GROUP = "45686445455@chatroom"
ACCOUNT = {"id": "wx01", "channel": "wechat", "state": "running", "self_uid": "wxid_synthetic_owner"}


class NativeHTTP:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.requests = []
        self.uid = ACCOUNT["self_uid"]

    async def request(self, method, path, **kwargs):
        assert method == "GET", "directory resolution must remain read-only"
        self.requests.append(path)
        route = urlsplit(path)
        if route.path == "/wa/v1/wechat/status":
            return 200, {"enabled": True, "wechat": {"running": True, "logged_in": True, "wxid": self.uid},
                         "chatlog": {"running": True, "key_ok": True}}
        assert route.path == "/wa/v1/wechat/sessions"
        limit = int(parse_qs(route.query).get("limit", [100])[0])
        return 200, {"sessions": deepcopy(self.rows[:limit])}


def app_for(edge):
    app = SimpleNamespace(store=SimpleNamespace(get_account=lambda _: dict(ACCOUNT)),
                          accounts=SimpleNamespace(busy=lambda _: False),
                          wechat_client=WeChatWinAgent(edge),
                          xunjia_v22_host=SimpleNamespace(loop=asyncio.get_running_loop()))
    app._xunjia_institution_presence = MethodType(AgentApp._xunjia_institution_presence, app)
    app._xunjia_wechat_directory = MethodType(AgentApp._xunjia_wechat_directory, app)
    return app


def project(rows):
    async def run():
        edge = NativeHTTP(rows)
        result = await asyncio.to_thread(AgentApp._xunjia_institution_friends, app_for(edge), dict(ACCOUNT))
        assert edge.rows == rows
        return result
    return asyncio.run(run())


def peer_id(native):
    return "wxpeer-" + base64.urlsafe_b64encode(native.encode()).decode("ascii").rstrip("=")


def test_group_directory_uses_authoritative_name_not_last_speaker():
    rows = project([{"userName": GROUP, "nickName": "灰灰", "display_name": "Greydan"}])
    assert rows == [{"native_id": GROUP, "peer_id": peer_id(GROUP), "name": "Greydan", "kind": "group"}]


@pytest.mark.parametrize("new_field", [{}, {"display_name": None}, {"display_name": ""}, {"display_name": "   "}])
def test_old_or_empty_group_display_name_falls_back_to_native_id(new_field):
    raw = {"userName": GROUP, "nickName": "灰灰", "name": "another preview", **new_field}
    assert project([raw])[0]["name"] == GROUP


@pytest.mark.parametrize("fields, expected", [
    ({"display_name": "Contact remark", "nickName": "old nickname"}, "Contact remark"),
    ({"remark": "Contact remark", "nickName": "old nickname"}, "Contact remark"),
    ({"nickName": "Legacy contact"}, "Legacy contact"),
    ({}, "wxid_peer"),
])
def test_private_contact_preserves_trusted_name_and_legacy_compatibility(fields, expected):
    row = project([{"userName": "wxid_peer", **fields}])[0]
    assert row == {"native_id": "wxid_peer", "peer_id": peer_id("wxid_peer"), "name": expected, "kind": "private"}


def test_unnamed_native_filehelper_keeps_existing_display_name():
    assert project([{"userName": "filehelper"}])[0]["name"] == "文件传输助手"


def test_identical_names_do_not_merge_distinct_ids_or_reorder_directory():
    ids = ["second@chatroom", GROUP, "wxid_peer"]
    rows = project([{"userName": key, "display_name": "Shared", "nickName": "preview"} for key in ids])
    assert [(row["native_id"], row["peer_id"], row["name"]) for row in rows] == [
        (key, peer_id(key), "Shared") for key in ids]


def test_name_is_not_borrowed_from_adjacent_group_with_metadata():
    rows = project([{"userName": "other@chatroom", "display_name": "Greydan"},
                    {"userName": GROUP, "nickName": "Greydan"}])
    assert [row["name"] for row in rows] == ["Greydan", GROUP]


def test_special_characters_are_not_escaped_or_used_to_rekey_native_identity():
    name = '资金 & <群> "A" / α 💬'
    row = project([{"userName": GROUP, "display_name": name, "nickName": "speaker"}])[0]
    assert (row["name"], row["native_id"], row["peer_id"]) == (name, GROUP, peer_id(GROUP))


def test_real_name_change_refreshes_on_next_directory_read_with_stable_peer_id():
    async def run():
        edge = NativeHTTP([{"userName": GROUP, "display_name": "Old group", "nickName": "speaker"}])
        app = app_for(edge)
        before = await asyncio.to_thread(AgentApp._xunjia_institution_friends, app, dict(ACCOUNT))
        edge.rows[0]["display_name"] = "New group"
        after = await asyncio.to_thread(AgentApp._xunjia_institution_friends, app, dict(ACCOUNT))
        assert (before[0]["name"], after[0]["name"]) == ("Old group", "New group")
        assert before[0]["peer_id"] == after[0]["peer_id"] == peer_id(GROUP)
    asyncio.run(run())


def test_large_directory_includes_named_last_group_without_changing_read_limit():
    async def run():
        raw = [{"userName": f"wxid_peer_{i}", "nickName": f"Contact {i}"} for i in range(201)]
        raw.append({"userName": GROUP, "nickName": "speaker", "display_name": "Greydan"})
        edge = NativeHTTP(raw)
        rows = await asyncio.to_thread(AgentApp._xunjia_institution_friends, app_for(edge), dict(ACCOUNT))
        assert len(rows) == 202
        assert (rows[-1]["native_id"], rows[-1]["name"]) == (GROUP, "Greydan")
        assert edge.requests == ["/wa/v1/wechat/status", "/wa/v1/wechat/sessions?limit=5000", "/wa/v1/wechat/status"]
    asyncio.run(run())


def test_full_directory_page_still_refuses_partial_publication():
    with pytest.raises(RuntimeError, match="WECHAT_DIRECTORY_TRUNCATED"):
        project([{"userName": f"group-{i}@chatroom", "display_name": "Group"} for i in range(5000)])


def test_filehelper_native_id_display_fallback_keeps_existing_assistant_label():
    assert project([{"userName": "filehelper", "display_name": "filehelper"}])[0]["name"] == "文件传输助手"
