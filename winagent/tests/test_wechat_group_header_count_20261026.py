"""Group count acceptance through real send orchestration, with isolated UIA and I/O."""
from __future__ import annotations

import time

import pytest

from qtrade_winagent.errors import WaError
from qtrade_winagent.win import wechat as native
from test_wechat_control_targets import BODY, TARGET, Control, DesktopRig, Rect


GROUP = "synthetic-room@chatroom"
PRIVATE = "wxid_synthetic_contact"


class GroupRig(DesktopRig):
    def __init__(self, monkeypatch, *, talker=GROUP, count="(4)"):
        super().__init__(monkeypatch, section="群聊" if talker.endswith("@chatroom") else "联系人")
        self.talker = talker
        self.composer.name = self.composer.element_info.name = ""
        self.title_single.element_info.class_name = "mmui::ChatTitleBarChatGroupView"
        self.count = self.make("count", "Text", "mmui::XTextView", count or "", 800, 15, 850, 50)
        self.count.parent_control = self.title_single
        if count is not None:
            self.controls.append(self.count)
        self.client = native.WinWeChat()
        monkeypatch.setattr(native, "require_windows", lambda _reason: None)
        monkeypatch.setattr(self.client, "_get_items", self.metadata)
        monkeypatch.setattr(self.client, "_send_identity", lambda: ("synthetic-account", 71, 1.0, 72, 1.0, "fake-db"))
        monkeypatch.setattr(self.client, "_require_foreground", lambda: 7)
        monkeypatch.setattr(self.client, "_readback_rows", self.readback)

    def metadata(self, path, _params):
        if (path.endswith("chatroom")) == self.talker.endswith("@chatroom"):
            return [{"UserName": self.talker, "NickName": TARGET}]
        return []

    def readback(self, talker, _text):
        assert talker == self.talker
        if not self.sent:
            return []
        return [{"talker": talker, "seq": 101, "time": int(time.time()),
                 "isSelf": True, "type": 1, "content": BODY}]

    async def send_message(self):
        return await self.client.send(session_name=self.talker, text=BODY, image_path=None,
                                      confirm_timeout_ms=50)

    async def assert_refused(self):
        with pytest.raises(WaError):
            await self.send_message()
        assert BODY not in self.writes, "A rejected identity must never reach body clipboard input"
        assert self.composer.clicks == self.send.clicks == 0
        assert self.sent == []


@pytest.mark.parametrize("count", ["(1)", "(4)", "(123)", "（4）"])
async def test_verified_group_accepts_one_exact_title_and_one_positive_member_count(monkeypatch, count):
    rig = GroupRig(monkeypatch, count=count)
    result = await rig.send_message()
    assert result["code"] == "DELIVERED"
    assert result["ext_msg_id"] == GROUP + ":101"
    assert rig.writes == [TARGET, BODY]
    assert rig.sent == [(TARGET, BODY)]
    assert rig.send.clicks == 1


@pytest.mark.parametrize("talker", [PRIVATE, GROUP])
async def test_verified_single_title_preserves_existing_send_behavior(monkeypatch, talker):
    rig = GroupRig(monkeypatch, talker=talker, count=None)
    result = await rig.send_message()
    assert result["code"] == "DELIVERED"
    assert rig.sent == [(TARGET, BODY)]


async def test_private_stable_id_cannot_gain_group_privilege_from_name_or_header(monkeypatch):
    rig = GroupRig(monkeypatch, talker=PRIVATE)
    await rig.assert_refused()


def test_direct_ui_call_defaults_to_private_identity(monkeypatch):
    rig = GroupRig(monkeypatch)
    with pytest.raises(WaError):
        native._click_send_in_open_wechat(7, TARGET, BODY)
    assert BODY not in rig.writes and rig.sent == []


@pytest.mark.parametrize("flag", [1, 0, "true", "false", None, []])
def test_non_boolean_group_flag_cannot_authorize_extra_title_text(monkeypatch, flag):
    rig = GroupRig(monkeypatch)
    with pytest.raises(WaError):
        native._click_send_in_open_wechat(7, TARGET, BODY, expected_group=flag)
    assert BODY not in rig.writes and rig.sent == []


@pytest.mark.parametrize("count", ["(0)", "(-1)", "(1.5)", "(04)", "( 4 )", "4", "(4人)",
                                  "(4）", "（4)", "(４)", "(٤)", "(4) extra", "", "Unknown"])
async def test_invalid_or_unknown_extra_header_text_refuses_before_body(monkeypatch, count):
    rig = GroupRig(monkeypatch, count=count)
    await rig.assert_refused()


@pytest.mark.parametrize("extra", [TARGET, "(4)", "Unknown"])
async def test_duplicate_title_count_or_third_text_refuses_before_body(monkeypatch, extra):
    rig = GroupRig(monkeypatch)
    node = Control(rig, "extra", "Text", "mmui::XTextView", extra, rig.count.rect)
    node.parent_control = rig.title_single
    rig.controls.append(node)
    await rig.assert_refused()


@pytest.mark.parametrize("header", ["Different recipient", "prefix " + TARGET, TARGET + " suffix", TARGET + " (4)"])
async def test_group_count_cannot_relax_exact_name_or_use_draft_identity(monkeypatch, header):
    rig = GroupRig(monkeypatch)
    rig.title.name = header
    rig.composer.name = rig.composer.element_info.name = rig.composer.value = TARGET
    await rig.assert_refused()


@pytest.mark.parametrize("change", ["foreign-count-pid", "count-no-runtime-id", "count-outside-master",
                                    "count-other-page", "count-other-master", "foreign-title-pid",
                                    "title-no-runtime-id", "title-hidden", "master-hidden", "duplicate-master",
                                    "page-hidden", "page-outside-root", "wrong-root", "root-no-runtime-id",
                                    "foreign-composer-pid", "send-other-page", "composer-no-page"])
async def test_count_does_not_weaken_identity_ancestry_geometry_or_visibility(monkeypatch, change):
    rig = GroupRig(monkeypatch)
    if change == "foreign-count-pid":
        rig.count.element_info.process_id = 999
    elif change == "count-no-runtime-id":
        rig.count.element_info.runtime_id = None
    elif change == "count-outside-master":
        rig.count.rect = Rect(rig.box.left + 450, rig.box.top + 120,
                              rig.box.left + 500, rig.box.top + 150)
    elif change in {"count-other-page", "count-other-master"}:
        claimed = list(rig.title_master.descendants())
        if change == "count-other-page":
            rig.count.parent_control = rig.initial_title_master
        else:
            other = Control(rig, "other-master", "Pane", "mmui::ChatTitleBarMasterView", "",
                            rig.title_master.rect)
            other.parent_control = rig.page
            rig.count.parent_control = other
        monkeypatch.setattr(rig.title_master, "descendants", lambda: claimed)
    elif change == "foreign-title-pid":
        rig.title.element_info.process_id = 999
    elif change == "title-no-runtime-id":
        rig.title.element_info.runtime_id = None
    elif change == "title-hidden":
        rig.title.visible = False
    elif change == "master-hidden":
        rig.title_master.visible = False
    elif change == "duplicate-master":
        rig.controls.append(Control(rig, "title_master", "Pane", "mmui::ChatTitleBarMasterView", "",
                                    rig.title_master.rect))
    elif change == "page-hidden":
        rig.page.visible = False
    elif change == "page-outside-root":
        rig.page.rect = Rect(rig.box.left - 1, rig.box.top, rig.box.right, rig.box.bottom)
    elif change == "wrong-root":
        rig.element_info.class_name = "mmui::OtherWindow"
    elif change == "root-no-runtime-id":
        rig.element_info.runtime_id = None
    elif change == "foreign-composer-pid":
        rig.composer.element_info.process_id = 999
    elif change == "send-other-page":
        rig.send.parent_control = rig.initial_page
    elif change == "composer-no-page":
        rig.composer.parent_control = rig
    await rig.assert_refused()


async def test_group_header_change_after_body_prevents_send_and_replay(monkeypatch):
    rig = GroupRig(monkeypatch)
    rig.on_body = lambda: setattr(rig.title, "name", "Different recipient")
    with pytest.raises(WaError):
        await rig.send_message()
    assert rig.writes == [TARGET, BODY]
    assert rig.result.clicks == 1
    assert rig.send.clicks == 0 and rig.sent == []
