"""企点遗留聊天页导航：真实 QidianUi + stateful fake，不连接真实设备。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi, load_profiles
from tests.test_qidian_ui import PKG, Clk, acct, node, tree
from tests.test_qidian_verified_search_title import (
    BODY, NAME, UID, SessionNames, VerifiedTitleAdb,
)


class RetainedChatAdb(VerifiedTitleAdb):
    """am start 保留旧 task，只有一次正常 BACK 才能回到带搜索入口的主页。"""

    def __init__(self, *, foreground=None, back_returns_home=True, has_chat_input=True, **kwargs):
        super().__init__(**kwargs)
        self.stage = "retained_chat"
        self.fg = foreground if foreground is not None else f"{PKG}/com.tencent.mobileqq.activity.ChatActivity"
        self.back_returns_home = back_returns_home
        self.has_chat_input = has_chat_input

    async def shell(self, serial, cmd):
        if cmd.startswith("am start"):
            self.cmds.append(cmd)
            return ""
        if cmd in ("input keyevent 4", "input keyevent KEYCODE_BACK"):
            self.cmds.append(cmd)
            if self.back_returns_home:
                self.stage = "main"
                self.fg = f"{PKG}/com.tencent.mobileqq.activity.SplashActivity"
            return ""
        if cmd.startswith("uiautomator dump") and self.stage == "retained_chat":
            self.cmds.append(cmd)
            inputs = ([node(rid=f"{PKG}:id/input", text="", bounds="[0,760][300,840]")]
                      if self.has_chat_input else [])
            return tree(node(rid=f"{PKG}:id/title", text=self.chat_name), *inputs,
                        node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[350,760][450,840]"))
        return await super().shell(serial, cmd)

    @property
    def back_commands(self):
        return [cmd for cmd in self.cmds if cmd in ("input keyevent 4", "input keyevent KEYCODE_BACK")]


def make_ui(*, store_name=UID, **kwargs):
    clock = Clk()
    adb = RetainedChatAdb(**kwargs)
    profile = load_profiles()["default"]
    profile = replace(profile, timeouts={**profile.timeouts, "ui_settle_s": 0, "launch_s": 0})

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)

    ui = QidianUi(adb=adb, profiles={"default": profile}, store=SessionNames(store_name),
                  clock=clock, sleep=sleep)
    return ui, adb, profile


@pytest.mark.parametrize("activity", ["ChatActivity", "SplashActivity"])
async def test_unverified_retained_chat_uses_one_back_then_verifies_search_card(activity):
    ui, adb, profile = make_ui(foreground=f"{PKG}/com.tencent.mobileqq.activity.{activity}")

    outcome = await ui._open_chat(acct(), profile, UID, ui._expected_title(acct(), UID))

    assert outcome.ok, outcome
    assert outcome.evidence["expect_title"] == NAME
    assert len(adb.back_commands) == 1
    assert adb.taps == ["input tap 50 50", "input tap 150 260", "input tap 150 420"]
    assert adb.typed == [UID]
    assert adb.sent == []


@pytest.mark.parametrize("foreground", [
    "org.example.other/com.tencent.mobileqq.activity.ChatActivity",
    f"{PKG}/com.tencent.mobileqq.activity.UnknownActivity",
    f"{PKG}/com.tencent.mobileqq.activity.LoginActivity",
    "unparseable foreground",
])
async def test_missing_search_on_foreign_unknown_or_login_page_never_uses_back(foreground):
    ui, adb, _profile = make_ui(foreground=foreground)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "anchor_not_found:search_entry"
    assert adb.back_commands == []
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []


@pytest.mark.parametrize("activity", ["ChatActivity", "SplashActivity"])
async def test_chat_activity_without_chat_input_marker_never_uses_back(activity):
    ui, adb, _profile = make_ui(has_chat_input=False,
                              foreground=f"{PKG}/com.tencent.mobileqq.activity.{activity}")

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "anchor_not_found:search_entry"
    assert adb.back_commands == []
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []


@pytest.mark.parametrize("chat_name,store_name", [(UID, UID), (NAME, NAME)])
async def test_proven_target_chat_is_reused_without_back_or_relaunch(chat_name, store_name):
    ui, adb, profile = make_ui(chat_name=chat_name, store_name=store_name)

    outcome = await ui._open_chat(acct(), profile, UID, ui._expected_title(acct(), UID))

    assert outcome.ok
    assert outcome.evidence["reused"] is True
    assert adb.back_commands == []
    assert not any(cmd.startswith("am start") for cmd in adb.cmds)
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []


async def test_missing_search_after_one_back_stops_without_repeated_navigation_or_body_input():
    ui, adb, _profile = make_ui(back_returns_home=False)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "anchor_not_found:search_entry"
    assert len(adb.back_commands) == 1
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []


async def test_returning_home_does_not_bypass_native_card_identity_check():
    ui, adb, _profile = make_ui(card_uid="9000000999")

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "card_identity_mismatch"
    assert len(adb.back_commands) == 1
    assert adb.typed == [UID]
    assert "input tap 150 420" not in adb.taps
    assert adb.sent == []
