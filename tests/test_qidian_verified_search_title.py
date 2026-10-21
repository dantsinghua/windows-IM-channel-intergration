"""企点可信搜索昵称回归；只用 stateful ADB fake，绝不连接真实设备。"""
from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi, load_profiles
from tests.test_qidian_ui import PKG, Clk, UiFakeAdb, acct, node, tree


UID = "9000000123"
NAME = "Synthetic authorized peer"
DECOY = "Synthetic unrelated peer"
BODY = "test-only verified alias message"


class SessionNames:
    def __init__(self, name):
        self.name = name

    def list_sessions(self, *, account_id):
        return [{"id": f"{account_id}:{UID}", "native_id": UID, "name": self.name}]


class VerifiedTitleAdb(UiFakeAdb):
    """独立模拟搜索→卡片→聊天，卡片没有尚未经实测的 chat_title 控件。"""

    def __init__(self, *, row_name=NAME, card_uid=UID, chat_name=NAME,
                 decoy="category", card_name=None, after_input_name=None):
        super().__init__()
        self.row_name = row_name
        self.card_uid = card_uid
        self.chat_name = chat_name
        self.card_name = card_name
        self.decoy = decoy
        self.after_input_name = after_input_name
        self.stage = "main"
        self.query = ""
        self.body = ""
        self.sent = []
        self.fg = f"{PKG}/com.tencent.mobileqq.activity.SearchActivity"

    def search_tree(self):
        decoys = []
        if self.decoy == "category":
            decoys.append(node(rid=f"{PKG}:id/title", text=DECOY,
                               bounds="[0,100][300,150]", clickable="false"))
        elif self.decoy == "other_row":
            decoys.append('<node resource-id="' + PKG + ':id/item2" '
                          'bounds="[0,350][300,470]" clickable="true">'
                          + node(rid=f"{PKG}:id/title", text=DECOY,
                                 bounds="[20,360][200,400]", clickable="false") + '</node>')
        inside = node(rid=f"{PKG}:id/title", text=self.row_name,
                      bounds="[20,210][200,250]", clickable="false")
        if self.decoy == "outside_bounds":
            # XML 子节点也必须在选中行 bounds 内，不能仅凭后续文档序采信。
            inside = node(rid=f"{PKG}:id/title", text=DECOY,
                          bounds="[20,360][200,400]", clickable="false") + inside
        row = ('<node resource-id="' + PKG + ':id/item1" '
               'class="android.widget.RelativeLayout" bounds="[0,200][300,320]" '
               'clickable="true">' + inside
               + node(rid=f"{PKG}:id/description", text=f"QQ:{UID}",
                      bounds="[20,260][250,300]", clickable="false") + '</node>')
        return tree(node(rid=f"{PKG}:id/et_search_keyword", text=self.query), *decoys, row)

    async def shell(self, serial, cmd):
        if cmd.startswith("am start"):
            self.cmds.append(cmd)
            self.stage = "main"
            self.fg = f"{PKG}/com.tencent.mobileqq.activity.SearchActivity"
            return ""
        if cmd.startswith("uiautomator dump"):
            self.cmds.append(cmd)
            if self.stage in ("main", "search"):
                return self.search_tree()
            if self.stage == "card":
                title = ([node(rid=f"{PKG}:id/title", text=self.card_name)]
                         if self.card_name is not None else [])
                return tree(node(rid=f"{PKG}:id/account_uin", text=self.card_uid),
                            *title, node(text="发消息", bounds="[0,380][300,460]"))
            title = self.after_input_name if self.body and self.after_input_name is not None else self.chat_name
            return tree(node(rid=f"{PKG}:id/title", text=title),
                        node(rid=f"{PKG}:id/input", text=self.body, bounds="[0,760][300,840]"),
                        node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[350,760][450,840]"))
        if cmd.startswith("input tap "):
            self.cmds.append(cmd)
            if cmd == "input tap 50 50":
                self.stage = "search"
            elif cmd == "input tap 150 260":
                self.stage = "card"
            elif cmd == "input tap 150 420":
                self.stage = "chat"
                self.fg = f"{PKG}/com.tencent.mobileqq.activity.ChatActivity"
            elif cmd == "input tap 400 800":
                self.sent.append(self.body)
            return ""
        if cmd == "am broadcast -a ADB_CLEAR_TEXT":
            self.cmds.append(cmd)
            if self.stage == "search":
                self.query = ""
            elif self.stage == "chat":
                self.body = ""
            return ""
        if cmd.startswith("am broadcast -a ADB_INPUT_B64 --es msg "):
            self.cmds.append(cmd)
            text = base64.b64decode(cmd.rsplit(" ", 1)[1]).decode()
            if self.stage == "search":
                self.query += text
            elif self.stage == "chat":
                self.body += text
            return ""
        return await super().shell(serial, cmd)


def make_ui(*, store_name=UID, **kwargs):
    clock = Clk()
    adb = VerifiedTitleAdb(**kwargs)
    profile = load_profiles()["default"]
    profile = replace(profile, timeouts={**profile.timeouts, "ui_settle_s": 0, "launch_s": 0})

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)

    ui = QidianUi(adb=adb, profiles={"default": profile}, clock=clock, sleep=sleep,
                  store=SessionNames(store_name) if store_name is not None else None)
    return ui, adb


@pytest.mark.parametrize("store_name", [UID, None, "Synthetic stale display name"])
async def test_verified_result_alias_allows_send_when_store_is_placeholder_missing_or_stale(store_name):
    ui, adb = make_ui(store_name=store_name)

    outcome = await ui.send(acct(), UID, BODY)

    assert outcome.ok, outcome
    assert adb.sent == [BODY]
    assert outcome.evidence["title"] == NAME


@pytest.mark.parametrize("decoy", ["category", "other_row", "outside_bounds"])
async def test_title_outside_selected_result_bounds_cannot_authorize_another_chat(decoy):
    ui, adb = make_ui(decoy=decoy, chat_name=DECOY)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "session_mismatch"
    assert outcome.evidence["where"] == "before_input"
    assert adb.typed == [UID]
    assert adb.sent == []


@pytest.mark.parametrize("card_uid", ["9000000999", UID + "0"])
async def test_search_alias_cannot_override_mismatching_card_identity(card_uid):
    ui, adb = make_ui(card_uid=card_uid)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "card_identity_mismatch"
    assert "input tap 150 420" not in adb.taps
    assert adb.typed == [UID]
    assert adb.sent == []


async def test_missing_trusted_alias_rejects_even_when_current_chat_has_a_title():
    ui, adb = make_ui(row_name="", store_name=UID)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "session_mismatch"
    assert adb.typed == [UID]
    assert adb.sent == []


async def test_existing_trusted_store_alias_still_sends_without_search_alias():
    ui, adb = make_ui(row_name="", store_name=NAME)

    outcome = await ui.send(acct(), UID, BODY)

    assert outcome.ok, outcome
    assert adb.sent == [BODY]


async def test_legacy_card_title_still_sends_without_search_or_store_alias():
    ui, adb = make_ui(row_name="", store_name=None, card_name=NAME)

    outcome = await ui.send(acct(), UID, BODY)

    assert outcome.ok, outcome
    assert adb.sent == [BODY]


async def test_verified_search_alias_does_not_bypass_second_session_guard():
    ui, adb = make_ui(after_input_name=DECOY)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "session_mismatch"
    assert outcome.evidence["where"] == "before_send"
    assert adb.typed == [UID, BODY]
    assert adb.sent == []
