"""一次 BACK 后有界等待新主页；有效转场树不等于搜索已就绪。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import time

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi, load_profiles
from tests.test_qidian_search_from_chat import RetainedChatAdb
from tests.test_qidian_ui import PKG, Clk, acct, node, tree
from tests.test_qidian_verified_search_title import UID, SessionNames


TRANSITION = tree(node(rid=f"{PKG}:id/transition", text="transition"))
SEARCH = node(rid=f"{PKG}:id/et_search_keyword", bounds="[480,40][580,140]")
HOME = tree(node(rid=f"{PKG}:id/recent_chat_list"), SEARCH)


class NavigatingAdb(RetainedChatAdb):
    def __init__(self, *, transition_count=1, blocked=None, never_ready=False):
        super().__init__()
        self.transition_count = transition_count
        self.blocked = blocked
        self.never_ready = never_ready
        self.after_back_reads = 0
        self.home_observed = False
        self.inputs_before_home = []

    async def shell(self, serial, cmd):
        if "uiautomator dump" in cmd and self.stage == "main" and self.back_commands:
            self.cmds.append(cmd)
            self.after_back_reads += 1
            if self.blocked:
                # 即使树里残留貌似搜索入口，也不能在外应用/登录/验证页点击。
                if self.blocked == "foreign":
                    self.fg = "org.example.other/.SplashActivity"
                    return HOME
                if self.blocked == "login":
                    self.fg = f"{PKG}/com.tencent.mobileqq.activity.LoginActivity"
                    return tree(node(rid=f"{PKG}:id/account"), SEARCH)
                if self.blocked == "unknown_activity":
                    self.fg = f"{PKG}/com.tencent.mobileqq.activity.UnknownActivity"
                    return HOME
                return tree(node(text=self.blocked), SEARCH)
            if self.never_ready or self.after_back_reads <= self.transition_count:
                return TRANSITION
            self.home_observed = True
            return HOME
        if cmd == "input tap 530 90":
            self.cmds.append(cmd)
            self.stage = "search"
            return ""
        if cmd.startswith("am broadcast -a ADB_INPUT_B64") and not self.home_observed:
            self.inputs_before_home.append(cmd)
        return await super().shell(serial, cmd)


def navigation_ui(*, frozen_clock=False, **kwargs):
    adb = NavigatingAdb(**kwargs)
    clock = Clk()
    p = load_profiles()["default"]
    p = replace(p, timeouts={**p.timeouts, "ui_settle_s": 0, "launch_s": 0,
                             "search_s": 0.2, "poll_interval_s": 0.05})

    async def sleep(seconds):
        if frozen_clock:
            await asyncio.sleep(seconds)
        else:
            clock.ms += int(seconds * 1000)
            await asyncio.sleep(0)

    ui = QidianUi(adb=adb, profiles={"default": p}, clock=clock, sleep=sleep,
                  store=SessionNames(UID))
    return ui, adb, p, clock


@pytest.mark.parametrize("transition_count", [1, 3])
async def test_back_waits_through_valid_transition_trees_then_taps_fresh_home(transition_count):
    ui, adb, p, _clock = navigation_ui(transition_count=transition_count)

    outcome = await ui._open_chat(acct(), p, UID, ui._expected_title(acct(), UID))

    assert outcome.ok, outcome
    assert len(adb.back_commands) == 1
    assert adb.taps[0] == "input tap 530 90"
    assert adb.typed == [UID]
    assert adb.inputs_before_home == []
    assert adb.sent == []


@pytest.mark.parametrize("blocked", ["foreign", "unknown_activity", "login", "短信验证", "安全验证", "设备锁"])
async def test_post_back_foreign_login_or_verification_stops_before_any_tap_or_text(blocked):
    ui, adb, p, _clock = navigation_ui(blocked=blocked)

    outcome = await ui._open_chat(acct(), p, UID, ui._expected_title(acct(), UID))

    assert not outcome.ok
    assert len(adb.back_commands) == 1
    assert adb.after_back_reads <= 1
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []


async def test_search_never_ready_exhausts_budget_without_more_back_or_text():
    ui, adb, p, clock = navigation_ui(never_ready=True)
    started = clock()

    outcome = await asyncio.wait_for(ui._open_chat(acct(), p, UID, None), timeout=1)

    assert not outcome.ok
    assert clock() - started >= 200
    assert adb.after_back_reads >= 2
    assert len(adb.back_commands) == 1
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []


async def test_frozen_injected_clock_cannot_make_navigation_wait_unbounded():
    ui, adb, p, _clock = navigation_ui(never_ready=True, frozen_clock=True)
    started = time.monotonic()

    outcome = await asyncio.wait_for(ui._open_chat(acct(), p, UID, None), timeout=1)

    assert not outcome.ok
    assert time.monotonic() - started < 0.8
    assert len(adb.back_commands) == 1
    assert adb.taps == []
    assert adb.typed == []
    assert adb.sent == []
