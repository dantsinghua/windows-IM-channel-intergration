"""真实发送函数的 UIA 控件合同；Desktop、剪贴板和键盘全部隔离为假后端。"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from qtrade_winagent.errors import WaError
from qtrade_winagent.win.wechat import _click_send_in_open_wechat


TARGET = "Synthetic contact"
BODY = "test-only message"


@dataclass
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Control:
    def __init__(self, rig, role, kind, cls, name, rect, *, visible=True, enabled=True):
        self.rig, self.role = rig, role
        self.element_info = SimpleNamespace(control_type=kind, class_name=cls, name=name,
                                            runtime_id=[42, 7, id(self)], process_id=71, handle=0)
        parent_roles = {"composer": "input_view", "send": "input_view", "title": "title_single",
                        "title_single": "title_master", "title_master": "page", "input_view": "page"}
        self.parent_control = getattr(rig, parent_roles.get(role, ""), rig)
        self.name, self.rect = name, rect
        self.visible, self.enabled = visible, enabled
        self.value = ""
        self.clicks = 0

    def window_text(self):
        return self.name

    def parent(self):
        return self.parent_control

    def children(self):
        return [ctrl for ctrl in self.rig.controls + getattr(self.rig, "initial_controls", [])
                if ctrl.parent_control is self]

    def descendants(self, **kwargs):
        found = []
        for child in self.children():
            found.append(child)
            found.extend(child.descendants(**kwargs))
        return found

    def class_name(self):
        return self.element_info.class_name

    def rectangle(self):
        return self.rect

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_value(self):
        return self.value

    def click_input(self, **kwargs):
        self.clicks += 1
        self.rig.focused = self
        if self.role == "result":
            self.rig.opened = True
        if self.role == "send":
            self.rig.sent.append((self.rig.title.name, self.rig.composer.value))


class DesktopRig:
    def __init__(self, monkeypatch, *, width=1230, height=1528, section="联系人"):
        self.box = Rect(427, 73, 427 + width, 73 + height)
        self.opened = False
        self.root_clicks = []
        self.sent, self.writes = [], []
        self.clipboard = ""
        self.ctrl = False
        self.on_body = None
        self.send_stays_disabled = False
        self.element_info = SimpleNamespace(control_type="Window", class_name="mmui::MainWindow",
                                            runtime_id=[42, 7], handle=7, process_id=71)
        self.page = self.make("page", "Pane", "mmui::ChatMessagePage", "", 420, 0, width, height)
        self.input_view = self.make("input_view", "Pane", "mmui::ChatInputView", "",
                                    420, height - 270, width, height)
        self.title_master = self.make("title_master", "Pane", "mmui::ChatTitleBarMasterView", "",
                                      430, 0, width - 15, 110)
        self.title_single = self.make("title_single", "Pane", "mmui::ChatTitleBarChatSingleView", "",
                                      430, 0, width - 15, 110)
        self.title = self.make("title", "Text", "mmui::XTextView", TARGET,
                               450, 15, width - 50, 50)
        self.search = self.make("search", "Edit", "mmui::XValidatorTextEdit", "", 147, 69, 342, 99)
        self.composer = self.make("composer", "Edit", "mmui::ChatInputField", TARGET,
                                  450, height - 250, width - 30, height - 80)
        self.send = self.make("send", "Button", "mmui::XOutlineButton", "发送",
                              width - 130, height - 70, width - 30, height - 30, enabled=False)
        self.controls = [self.search, self.composer, self.send, self.page, self.input_view,
                         self.title_master, self.title_single, self.title]
        self.initial_page = self.make("initial_page", "Pane", "mmui::ChatMessagePage", "",
                                      420, 0, width, height)
        self.initial_input_view = self.make("initial_input_view", "Pane", "mmui::ChatInputView", "",
                                            420, height - 270, width, height)
        self.initial_title_master = self.make("initial_title_master", "Pane", "mmui::ChatTitleBarMasterView", "",
                                              430, 0, width - 15, 110)
        self.initial_title_single = self.make("initial_title_single", "Pane", "mmui::ChatTitleBarChatSingleView", "",
                                              430, 0, width - 15, 110)
        self.initial_title = self.make("initial_title", "Text", "mmui::XTextView", "Previous conversation",
                                       450, 15, width - 50, 50)
        self.initial_composer = self.make("initial_composer", "Edit", "mmui::ChatInputField", "",
                                          450, height - 250, width - 30, height - 80)
        self.initial_send = self.make("initial_send", "Button", "mmui::XOutlineButton", "Send",
                                      width - 130, height - 70, width - 30, height - 30, enabled=False)
        self.initial_input_view.parent_control = self.initial_page
        self.initial_title_master.parent_control = self.initial_page
        self.initial_title_single.parent_control = self.initial_title_master
        self.initial_title.parent_control = self.initial_title_single
        self.initial_composer.parent_control = self.initial_input_view
        self.initial_send.parent_control = self.initial_input_view
        self.initial_controls = [self.initial_page, self.initial_input_view, self.initial_title_master,
                                 self.initial_title_single, self.initial_title, self.initial_composer,
                                 self.initial_send]
        self.heading = self.make("section", "Text", "Text", section, 150, 120, 300, 150)
        self.result = self.make("result", "ListItem", "ListItem", TARGET, 150, 150, 350, 200)
        self.focused = self.make("unrelated", "Edit", "OtherEdit", "", 0, 0, 10, 10)
        clipboard = SimpleNamespace(
            CF_UNICODETEXT=13, OpenClipboard=lambda: None, EmptyClipboard=lambda: None,
            SetClipboardText=self.set_clipboard, CloseClipboard=lambda: None,
        )
        user32 = SimpleNamespace(SetProcessDPIAware=lambda: None, keybd_event=self.key_event,
                                 GetForegroundWindow=lambda: 7)
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
        monkeypatch.setitem(sys.modules, "win32clipboard", clipboard)
        monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(KEYEVENTF_KEYUP=2))
        monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 7))
        monkeypatch.setattr("qtrade_winagent.win.wechat._visible_window_rows",
                            lambda: [(7, "mmui::MainWindow", "Weixin.exe")])
        monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **_kw: self))
        monkeypatch.setattr("time.sleep", lambda _seconds: None)

    def make(self, role, kind, cls, name, x1, y1, x2, y2, **kwargs):
        return Control(self, role, kind, cls, name,
                       Rect(self.box.left + x1, self.box.top + y1,
                            self.box.left + x2, self.box.top + y2), **kwargs)

    def window(self, **kwargs):
        return self

    def rectangle(self):
        return self.box

    def parent(self):
        return None

    def children(self):
        return [ctrl for ctrl in self.controls if ctrl.parent_control is self]

    def descendants(self, **kwargs):
        rows = (list(self.controls) if self.opened else
                [ctrl for ctrl in self.controls if ctrl.role == "search"] + list(self.initial_controls))
        if self.search.value == TARGET and not self.opened:
            rows.extend([self.heading, self.result])
        return rows

    def click_input(self, **kwargs):
        # 历史比例搜索点落在真实 Edit 下方，不能假装把焦点放到了搜索 Edit。
        self.root_clicks.append(kwargs)
        self.focused = self.make("unrelated", "Edit", "OtherEdit", "", 0, 0, 10, 10)

    def set_clipboard(self, value, _format):
        self.clipboard = value
        self.writes.append(value)

    def key_event(self, key, _scan, flags, _extra):
        if key == 0x11:
            self.ctrl = flags == 0
        elif flags == 0 and key == 0x2E:
            self.focused.value = ""
        elif flags == 0 and key == 0x56 and self.ctrl:
            self.focused.value += self.clipboard
            if self.focused is self.composer:
                self.send.enabled = not self.send_stays_disabled
                if self.on_body:
                    self.on_body()

    def invalidate(self, role, failure):
        ctrl = getattr(self, role)
        if failure == "missing":
            self.controls.remove(ctrl)
        elif failure == "duplicate":
            self.controls.append(Control(self, role, ctrl.element_info.control_type,
                                         ctrl.element_info.class_name, ctrl.name, ctrl.rect))
        elif failure == "hidden":
            ctrl.visible = False
        elif failure == "disabled":
            ctrl.enabled = False
        elif failure == "zero-area":
            ctrl.rect = Rect(500, 150, 500, 150)
        elif failure == "outside":
            ctrl.rect = Rect(self.box.right + 1, 150, self.box.right + 100, 200)
        elif failure == "wrong-session":
            ctrl.name = "Different synthetic contact"
            ctrl.element_info.name = ctrl.name
            self.title.name = ctrl.name
            self.title.element_info.name = ctrl.name
        else:
            raise AssertionError(failure)


@pytest.mark.parametrize("width,height", [(1230, 1528), (1000, 900), (2121, 1351)])
def test_send_uses_actual_controls_across_window_sizes(monkeypatch, width, height):
    rig = DesktopRig(monkeypatch, width=width, height=height)

    _click_send_in_open_wechat(7, TARGET, BODY)

    assert rig.root_clicks == [], "搜索、正文和发送不得使用窗口比例点"
    assert rig.search.value == TARGET
    assert rig.composer.value == BODY
    assert rig.sent == [(TARGET, BODY)]
    assert rig.search.clicks == rig.composer.clicks == rig.send.clicks == 1


@pytest.mark.parametrize("failure", ["missing", "duplicate", "hidden", "disabled", "zero-area", "outside"])
def test_invalid_search_control_refuses_before_native_input(monkeypatch, failure):
    rig = DesktopRig(monkeypatch)
    rig.invalidate("search", failure)

    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)

    assert rig.writes == [], "搜索框未唯一安全定位，不能把目标粘到未知控件"
    assert rig.sent == []


@pytest.mark.parametrize("failure", ["missing", "duplicate", "hidden", "disabled", "zero-area", "outside", "wrong-session"])
def test_invalid_composer_refuses_before_body_paste(monkeypatch, failure):
    rig = DesktopRig(monkeypatch)
    rig.invalidate("composer", failure)

    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)

    assert BODY not in rig.writes
    assert rig.result.clicks == 1, "必须已经选中唯一搜索结果，才能证明输入区守卫生效"
    assert rig.sent == []


@pytest.mark.parametrize("failure", ["missing", "duplicate", "hidden", "zero-area", "outside"])
def test_invalid_send_control_refuses_before_body_paste(monkeypatch, failure):
    rig = DesktopRig(monkeypatch)
    rig.invalidate("send", failure)

    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)

    assert BODY not in rig.writes
    assert rig.result.clicks == 1, "必须到达已打开目标后的发送按钮检查"
    assert rig.sent == []


@pytest.mark.parametrize("change", ["send-disabled", "send-duplicate", "wrong-session"])
def test_after_body_paste_revalidates_button_and_session(monkeypatch, change):
    rig = DesktopRig(monkeypatch)
    if change == "send-disabled":
        rig.send_stays_disabled = True
    elif change == "send-duplicate":
        rig.on_body = lambda: rig.invalidate("send", "duplicate")
    else:
        rig.on_body = lambda: rig.invalidate("composer", "wrong-session")

    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)

    assert BODY in rig.writes, "本项必须到达粘正文后的第二道检查"
    assert rig.sent == []


@pytest.mark.parametrize("section", ["聊天记录", "搜索网络结果", ""])
def test_same_name_outside_allowed_search_section_never_pastes_body(monkeypatch, section):
    rig = DesktopRig(monkeypatch, section=section)

    with pytest.raises(WaError):
        _click_send_in_open_wechat(7, TARGET, BODY)

    assert BODY not in rig.writes
    assert rig.search.value == TARGET, "必须实际完成控件搜索后验证结果分组"
    assert rig.result.clicks == 0
    assert rig.sent == []
