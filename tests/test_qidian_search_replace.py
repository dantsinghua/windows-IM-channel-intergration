"""发送定位回归：真实 QidianUi，ADB 仅模拟残留搜索和名片，绝不连接设备。"""
from __future__ import annotations

import base64

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi
from tests.test_qidian_ui import PKG, UiFakeAdb, acct, node, tree


class SearchAdb(UiFakeAdb):
    def __init__(self, query="", *, card_identity=None):
        super().__init__()
        self.query = query
        self.card_identity = card_identity
        self.stage = "main"
        self.opened_cards = []
        self.opened_chats = []
        self.fg = f"{PKG}/com.tencent.mobileqq.activity.SearchActivity"

    async def shell(self, serial, cmd):
        self.cmds.append(cmd)
        if cmd.startswith("dumpsys window"):
            return f"mCurrentFocus=Window{{abc u0 {self.fg}}}"
        if cmd.startswith("am start"):
            self.stage = "main"
        elif cmd == "input tap 50 50":
            self.stage = "search"
        elif cmd == "am broadcast -a ADB_CLEAR_TEXT":
            assert self.stage == "search", "只能在实际聚焦的搜索框清空"
            self.query = ""
        elif cmd.startswith("am broadcast -a ADB_INPUT_B64 --es msg "):
            assert self.stage == "search"
            self.query += base64.b64decode(cmd.rsplit(" ", 1)[1]).decode()
        elif cmd == "input tap 50 150":
            self.opened_cards.append(self.query)
            self.stage = "card"
        elif cmd == "input tap 50 350":
            self.opened_chats.append(self.query)
            self.stage = "chat"
        elif cmd.startswith("uiautomator dump"):
            if self.stage in ("main", "search"):
                return tree(node(rid=f"{PKG}:id/et_search_keyword", text=self.query),
                            node(rid=f"{PKG}:id/title", text="QQ好友", bounds="[0,200][100,240]"),
                            node(rid=f"{PKG}:id/item1", text=self.query, bounds="[0,100][100,200]"))
            if self.stage == "card":
                return tree(node(rid=f"{PKG}:id/account_uin", text=self.card_identity or self.query),
                            node(text="发消息", bounds="[0,300][100,400]"))
        return ""


async def no_wait(_seconds):
    pass


@pytest.mark.parametrize("previous", ["3007378246", "stale target", ""])
async def test_search_replaces_previous_query_before_opening_authorized_card(previous):
    adb = SearchAdb(previous)
    ui = QidianUi(adb=adb, sleep=no_wait)
    account = acct()

    outcome = await ui._open_chat(account, ui.profile_for(account), "415011447", None)

    assert adb.query == "415011447", "新目标必须替换残留查询，不能追加到旧账号后"
    assert outcome.ok
    assert adb.opened_cards == ["415011447"]
    assert adb.opened_chats == ["415011447"]


async def test_second_target_does_not_reuse_or_append_first_search():
    adb = SearchAdb()
    ui = QidianUi(adb=adb, sleep=no_wait)
    account = acct()
    profile = ui.profile_for(account)

    first = await ui._open_chat(account, profile, "3007378246", None)
    second = await ui._open_chat(account, profile, "415011447", None)

    assert first.ok and second.ok
    assert adb.opened_cards == ["3007378246", "415011447"]
    assert adb.opened_chats == ["3007378246", "415011447"]


async def test_replaced_query_still_rejects_wrong_card_identity():
    adb = SearchAdb("3007378246", card_identity="9000000000")
    ui = QidianUi(adb=adb, sleep=no_wait)
    account = acct()

    outcome = await ui._open_chat(account, ui.profile_for(account), "415011447", None)

    assert not outcome.ok
    assert outcome.reason == "card_identity_mismatch"
    assert adb.opened_chats == [], "身份不符时不得点名片的发消息入口"
