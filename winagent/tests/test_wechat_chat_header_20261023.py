"""Recipient identity comes from the same chat page header, never from drafts."""
from __future__ import annotations

import pytest

from qtrade_winagent.errors import WaError
from qtrade_winagent.win.wechat import _click_send_in_open_wechat
from test_wechat_control_targets import BODY, TARGET, Control, DesktopRig, Rect
from test_wechat_ui_focus_and_stale import FakeComError


class HeaderRig(DesktopRig):
    def __init__(self, monkeypatch):
        super().__init__(monkeypatch)
        self.composer.name = ""
        self.composer.element_info.name = ""

    def title_text(self, text):
        self.title.name = text
        self.title.element_info.name = text

    def target_draft(self):
        self.composer.name = TARGET
        self.composer.value = TARGET
        self.composer.element_info.name = TARGET

    def other_page(self):
        page = self.make("other_page", "Pane", "mmui::ChatMessagePage", "", 420, 0,
                         self.box.width(), self.box.height())
        master = self.make("other_master", "Pane", "mmui::ChatTitleBarMasterView", "",
                           430, 0, self.box.width() - 15, 110)
        title = self.make("other_title", "Text", "mmui::XTextView", TARGET,
                          450, 15, self.box.width() - 50, 50)
        master.parent_control = page
        title.parent_control = master
        self.controls.extend([page, master, title])
        return page


def assert_refused_before_body(rig):
    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)
    assert BODY not in rig.writes
    assert rig.send.clicks == 0
    assert rig.sent == []


def test_empty_draft_and_correct_same_page_header_sends_once(monkeypatch):
    rig = HeaderRig(monkeypatch)
    _click_send_in_open_wechat(7, TARGET, BODY)
    assert rig.sent == [(TARGET, BODY)]
    assert rig.writes == [TARGET, BODY]
    assert rig.send.clicks == 1


def test_initially_open_target_header_and_empty_draft_are_valid_before_search(monkeypatch):
    from qtrade_winagent.win.wechat import _unique_wechat_control

    rig = HeaderRig(monkeypatch)
    rig.initial_title.name = TARGET
    rig.initial_title.element_info.name = TARGET
    assert rig.initial_title in rig.descendants()
    assert rig.initial_composer.window_text() == ""
    assert _unique_wechat_control(rig, "composer", expected_name=TARGET) is rig.initial_composer
    assert _unique_wechat_control(rig, "send", require_enabled=False,
                                  expected_name=TARGET) is rig.initial_send
    _click_send_in_open_wechat(7, TARGET, BODY)
    assert rig.sent == [(TARGET, BODY)]
    assert rig.writes == [TARGET, BODY]


@pytest.mark.parametrize("draft", [TARGET, "Unrelated draft"])
@pytest.mark.parametrize("header", ["Different recipient", TARGET + " (2)", "prefix " + TARGET])
def test_draft_text_never_substitutes_for_mismatched_header(monkeypatch, draft, header):
    rig = HeaderRig(monkeypatch)
    rig.composer.name = draft
    rig.title_text(header)
    assert_refused_before_body(rig)


@pytest.mark.parametrize("failure", ["missing", "hidden", "duplicate-title", "duplicate-master"])
def test_expected_draft_cannot_bypass_missing_or_ambiguous_header(monkeypatch, failure):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    if failure == "missing":
        rig.controls.remove(rig.title)
    elif failure == "hidden":
        rig.title_master.visible = False
    elif failure == "duplicate-title":
        duplicate = Control(rig, "title", "Text", "mmui::XTextView", TARGET, rig.title.rect)
        rig.controls.append(duplicate)
    else:
        duplicate = Control(rig, "title_master", "Pane", "mmui::ChatTitleBarMasterView", "",
                            rig.title_master.rect)
        rig.controls.append(duplicate)
    assert_refused_before_body(rig)


def test_same_name_in_sidebar_does_not_validate_wrong_chat_header(monkeypatch):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    rig.title_text("Different recipient")
    sidebar = rig.make("sidebar", "Text", "mmui::XTextView", TARGET, 50, 250, 300, 290)
    rig.controls.append(sidebar)
    assert_refused_before_body(rig)


def test_same_name_header_on_another_page_does_not_authorize_composer(monkeypatch):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    rig.title_text("Different recipient")
    rig.other_page()
    assert_refused_before_body(rig)


@pytest.mark.parametrize("change", ["different-send-page", "missing-composer-page", "foreign-title-pid",
                                    "different-root", "send-outside-page", "title-outside-header",
                                    "foreign-send-pid", "title-not-leaf", "page-outside-window"])
def test_header_composer_send_identity_and_containment_must_agree(monkeypatch, change):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    if change == "different-send-page":
        rig.send.parent_control = rig.other_page()
    elif change == "missing-composer-page":
        rig.composer.parent_control = rig
    elif change == "foreign-title-pid":
        rig.title.element_info.process_id = 999
    elif change == "different-root":
        foreign = rig.make("foreign-root", "Window", "mmui::MainWindow", "", 0, 0, 500, 500)
        foreign.parent_control = None
        foreign.element_info.handle = 8
        rig.page.parent_control = foreign
    elif change == "send-outside-page":
        rig.send.rect = Rect(rig.box.left + 100, rig.box.top + 500,
                             rig.box.left + 200, rig.box.top + 550)
    elif change == "title-outside-header":
        rig.title.rect = Rect(rig.box.left + 100, rig.box.top + 200,
                              rig.box.left + 200, rig.box.top + 230)
    elif change == "foreign-send-pid":
        rig.send.element_info.process_id = 999
    elif change == "title-not-leaf":
        child = rig.make("title-child", "Text", "mmui::XTextView", "Nested", 450, 15, 500, 35)
        child.parent_control = rig.title
        rig.controls.append(child)
    else:
        rig.page.rect = Rect(rig.box.left - 1, rig.box.top, rig.box.right, rig.box.bottom)
    assert_refused_before_body(rig)


def test_header_change_after_body_paste_refuses_send_without_replay(monkeypatch):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    rig.on_body = lambda: rig.title_text("Different recipient")
    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)
    assert rig.writes == [TARGET, BODY]
    assert rig.result.clicks == 1
    assert rig.send.clicks == 0 and rig.sent == []


@pytest.mark.parametrize("changed", [False, True])
def test_stale_header_reacquisition_revalidates_recipient(monkeypatch, changed):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    reads = []

    def title_text():
        reads.append(True)
        if len(reads) == 1:
            if changed:
                rig.title_text("Different recipient")
            raise FakeComError()
        return rig.title.name

    monkeypatch.setattr(rig.title, "window_text", title_text)
    if changed:
        assert_refused_before_body(rig)
    else:
        _click_send_in_open_wechat(7, TARGET, BODY)
        assert rig.sent == [(TARGET, BODY)]
        assert rig.writes == [TARGET, BODY]
    assert len(reads) >= 2
    assert rig.result.clicks == 1


def test_final_send_stale_rechecks_changed_header_before_any_send_click(monkeypatch):
    rig = HeaderRig(monkeypatch)
    rig.target_draft()
    stale = []
    original = rig.send.is_enabled

    def enabled():
        if BODY in rig.writes and not stale:
            stale.append(True)
            rig.title_text("Different recipient")
            raise FakeComError()
        return original()

    monkeypatch.setattr(rig.send, "is_enabled", enabled)
    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)
    assert stale
    assert rig.writes == [TARGET, BODY]
    assert rig.send.clicks == 0 and rig.sent == []
