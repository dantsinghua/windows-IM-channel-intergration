"""企点真实层级的匿名类比回归：结果行与分组/昵称同名 id 分离，全 fake。"""
from __future__ import annotations

from dataclasses import replace
import base64

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi, load_profiles
from tests.test_qidian_ui import PKG, Clk, UiFakeAdb, acct, node, tree


UID = "9000000123"
NAME = "Synthetic peer"
BODY = "test-only body must never reach the wrong contact"


class SearchResultsAdb(UiFakeAdb):
    def __init__(self, clock, *, ready_after=0, empty_snapshots=0, wrong_card=False):
        super().__init__()
        self.clock = clock
        self.ready_after = ready_after
        self.empty_snapshots = empty_snapshots
        self.wrong_card = wrong_card
        self.stage = "main"
        self.query = self.body = ""
        self.submitted_ms = None
        self.search_dumps = 0
        self.result_clicked_ms = None
        self.sent = []
        self.fg = f"{PKG}/com.tencent.mobileqq.activity.SearchActivity"

    def search_tree(self):
        heading = node(rid=f"{PKG}:id/title", text="QQ好友", bounds="[0,100][300,150]")
        if self.submitted_ms is None:
            return tree(node(rid=f"{PKG}:id/et_search_keyword"), heading)
        self.search_dumps += 1
        elapsed = (self.clock.ms - self.submitted_ms) / 1000
        if elapsed < self.ready_after or self.search_dumps <= self.empty_snapshots:
            return tree(node(rid=f"{PKG}:id/et_search_keyword", text=self.query), heading)
        # item1 是可点结果整行；title 同时用于分组与行内昵称，不能拿它定位结果。
        row = (f'<node resource-id="{PKG}:id/item1" class="android.widget.RelativeLayout" '
               'text="" content-desc="" clickable="true" bounds="[0,200][300,320]">'
               + node(rid=f"{PKG}:id/title", text=NAME, bounds="[20,210][200,250]", clickable="false")
               + node(rid=f"{PKG}:id/description", text=f"QQ:{UID}",
                      bounds="[20,260][250,300]", clickable="false") + "</node>")
        return tree(node(rid=f"{PKG}:id/et_search_keyword", text=self.query), heading, row)

    async def shell(self, serial, cmd):
        if cmd.startswith("am start"):
            self.cmds.append(cmd)
            self.stage = "main"
            return ""
        if cmd.startswith("uiautomator dump"):
            self.cmds.append(cmd)
            if self.stage in ("main", "search", "heading"):
                return self.search_tree()
            if self.stage == "card":
                return tree(node(rid=f"{PKG}:id/account_uin", text="9000000999" if self.wrong_card else UID),
                            node(rid=f"{PKG}:id/title", text=NAME),
                            node(text="发消息", bounds="[0,380][300,460]"))
            return tree(node(rid=f"{PKG}:id/title", text=NAME),
                        node(rid=f"{PKG}:id/input", text=self.body, bounds="[0,760][300,840]"),
                        node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[350,760][450,840]"))
        if cmd.startswith("input tap "):
            self.cmds.append(cmd)
            point = cmd.removeprefix("input tap ")
            if point == "50 50":
                self.stage = "search"
            elif point == "150 125":
                self.stage = "heading"
            elif point == "150 260":
                self.result_clicked_ms = self.clock.ms
                self.stage = "card"
            elif point == "150 420":
                self.stage = "chat"
                self.fg = f"{PKG}/com.tencent.mobileqq.activity.ChatActivity"
            elif point == "400 800":
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
                self.submitted_ms = self.clock.ms
            elif self.stage == "chat":
                self.body += text
            return ""
        return await super().shell(serial, cmd)


def make_ui(*, actual_anchor=True, budget=3, **kwargs):
    clock = Clk()
    adb = SearchResultsAdb(clock, **kwargs)
    profile = load_profiles()["default"]
    nodes = dict(profile.nodes)
    if actual_anchor:
        nodes["search_result_first"] = {"id": f"{PKG}:id/item1"}
    profile = replace(profile, nodes=nodes, timeouts={**profile.timeouts, "search_s": budget,
                                                    "ui_settle_s": 0, "launch_s": 0,
                                                    "poll_interval_s": 0.25})

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)

    return QidianUi(adb=adb, profiles={"default": profile}, clock=clock, sleep=sleep), adb, clock, profile


async def test_default_profile_selects_result_row_not_category_or_nested_nickname():
    ui, adb, _clock, profile = make_ui(actual_anchor=False)

    outcome = await ui._open_chat(acct(), profile, UID, NAME)

    assert "input tap 150 260" in adb.taps, "default 必须点击实际 item1 整行"
    assert "input tap 150 125" not in adb.taps, "不得点击复用 title id 的 QQ好友分组"
    assert "input tap 110 230" not in adb.taps, "不得把行内昵称当可点击结果行"
    assert outcome.ok


@pytest.mark.parametrize("ready_after", [0, 0.75, 2.5])
async def test_result_is_opened_when_ready_instead_of_sleeping_entire_search_budget(ready_after):
    ui, adb, _clock, profile = make_ui(ready_after=ready_after)

    outcome = await ui._open_chat(acct(), profile, UID, NAME)

    assert outcome.ok
    elapsed = (adb.result_clicked_ms - adb.submitted_ms) / 1000
    assert ready_after <= elapsed <= ready_after + 0.25, "search_s 是总预算，不是每次固定等待"


async def test_empty_first_result_snapshot_is_retried_within_search_budget():
    ui, adb, _clock, profile = make_ui(empty_snapshots=1)

    outcome = await ui._open_chat(acct(), profile, UID, NAME)

    assert outcome.ok, "首轮无结果时必须在预算内继续观察"
    assert adb.search_dumps >= 2
    assert adb.result_clicked_ms - adb.submitted_ms <= 3000


async def test_search_timeout_does_not_click_heading_or_type_message_body():
    ui, adb, clock, _profile = make_ui(ready_after=99)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "anchor_not_found:search_result_first"
    assert 3000 <= clock.ms - adb.submitted_ms <= 3250, "搜索必须在配置预算边界停止"
    assert adb.taps == ["input tap 50 50"]
    assert adb.typed == [UID], "超时只能输过查询，不能输入待发正文"
    assert adb.sent == []


async def test_result_opening_still_requires_matching_card_before_any_body_input():
    ui, adb, _clock, _profile = make_ui(wrong_card=True)

    outcome = await ui.send(acct(), UID, BODY)

    assert not outcome.ok
    assert outcome.reason == "card_identity_mismatch"
    assert "input tap 150 260" in adb.taps
    assert "input tap 150 420" not in adb.taps
    assert adb.typed == [UID]
    assert adb.sent == []
