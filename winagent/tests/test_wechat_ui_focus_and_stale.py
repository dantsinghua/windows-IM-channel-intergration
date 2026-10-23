"""发送入口的前台所有权、有界 UIA 重读与零补发合同；全部 Windows 对象为 fake。"""
from __future__ import annotations

import ctypes
import logging
import sys
from types import SimpleNamespace

import pytest

from qtrade_winagent.errors import WaError
from qtrade_winagent.win import wechat as wechat_module
from qtrade_winagent.win.wechat import WinWeChat, _click_send_in_open_wechat
from test_wechat_control_targets import BODY, TARGET, Control, DesktopRig


STALE = 0x80040201
SIGNED_STALE = STALE - (1 << 32)


class FakeComError(Exception):
    def __init__(self, hresult=SIGNED_STALE):
        super().__init__(hresult, "Synthetic COM failure", None, None)
        self.hresult = hresult


class FocusRig(DesktopRig):
    """只模拟原生事件与控件，不替换待测发送、定位或分类函数。"""

    def __init__(self, monkeypatch):
        self.foreground = 7
        self.foreign_inputs = []
        self.foreign_clicks = []
        self.keys = []
        self.actions = []
        self.on_action = lambda _stage: None
        self.popup_reads = 0
        self.popup_fault = None
        self.claims = 0
        self.claim_succeeds = True
        self.window_rows = [(7, "mmui::MainWindow", "Weixin.exe")]
        self.popup_extra = []
        super().__init__(monkeypatch)
        ctypes.windll.user32.GetForegroundWindow = lambda: self.foreground
        monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
            GetForegroundWindow=lambda: self.foreground,
            SetForegroundWindow=self.claim,
        ))
        monkeypatch.setattr(wechat_module, "_visible_window_rows", lambda: list(self.window_rows))
        monkeypatch.setattr(wechat_module, "_claim_foreground", self.claim)
        for ctrl in (self.search, self.result, self.composer, self.send):
            original = ctrl.click_input

            def click(*args, _ctrl=ctrl, _original=original, **kwargs):
                if self.foreground != 7:
                    self.foreign_clicks.append(_ctrl.role)
                _original(*args, **kwargs)
                self.action(_ctrl.role + "-click")

            monkeypatch.setattr(ctrl, "click_input", click)

    def claim(self, *_args):
        self.claims += 1
        if self.claim_succeeds:
            self.foreground = 7
        return self.claim_succeeds

    def action(self, stage):
        self.actions.append(stage)
        self.on_action(stage)

    def key_event(self, key, scan, flags, extra):
        role = self.focused.role
        self.keys.append((role, key, flags))
        if flags == 0 and self.foreground != 7:
            self.foreign_inputs.append((role, key))
        super().key_event(key, scan, flags, extra)
        # 在完整 Ctrl+A chord 后切前台，验证下一步 Del 必须重新验证。
        if key == 0x11 and flags == 2:
            previous_downs = [k for r, k, f in self.keys[:-1] if r == role and f == 0 and k != 0x11]
            if previous_downs:
                self.action(role + ("-select" if previous_downs[-1] == 0x41 else "-paste"))
        if key == 0x2E and flags == 2:
            self.action(role + "-delete")

    def descendants(self, **kwargs):
        if self.search.value == TARGET and not self.opened:
            self.popup_reads += 1
            if self.popup_fault:
                self.popup_fault()
        return super().descendants(**kwargs) + self.popup_extra


def send(rig, monkeypatch, *, entry="helper"):
    if entry == "keyboard":
        owner = object.__new__(WinWeChat)
        owner._keyboard_send_text(TARGET, BODY)
    else:
        _click_send_in_open_wechat(7, TARGET, BODY)


def assert_no_replay(rig):
    assert rig.result.clicks <= 1, "已进入正文阶段，不能重新选搜索结果"
    assert rig.writes.count(TARGET) <= 1
    assert rig.writes.count(BODY) <= 1, "正文不能重贴"
    assert rig.send.clicks <= 1, "发送点击错误不能补发"


@pytest.mark.parametrize("entry", ["helper", "keyboard"])
def test_correct_foreground_sends_once(monkeypatch, entry):
    rig = FocusRig(monkeypatch)
    send(rig, monkeypatch, entry=entry)
    assert rig.sent == [(TARGET, BODY)]
    assert not rig.foreign_inputs and not rig.foreign_clicks
    assert_no_replay(rig)


@pytest.mark.parametrize("entry", ["helper", "keyboard"])
def test_initial_browser_foreground_is_restored_to_same_wechat(monkeypatch, entry):
    rig = FocusRig(monkeypatch)
    rig.foreground = 9007
    send(rig, monkeypatch, entry=entry)
    assert rig.claims >= 1 and rig.foreground == 7
    assert rig.sent == [(TARGET, BODY)]
    assert not rig.foreign_inputs and not rig.foreign_clicks
    assert_no_replay(rig)


@pytest.mark.parametrize("entry", ["helper", "keyboard"])
def test_initial_foreground_recovery_failure_never_inputs(monkeypatch, entry):
    rig = FocusRig(monkeypatch)
    rig.foreground = 9007
    rig.claim_succeeds = False
    with pytest.raises(WaError):
        send(rig, monkeypatch, entry=entry)
    assert rig.claims >= 1
    assert rig.keys == [] and rig.writes == []
    assert rig.search.clicks == 0 and rig.sent == []


@pytest.mark.parametrize("change", ["missing", "duplicate", "changed-hwnd"])
def test_focus_recovery_refuses_missing_ambiguous_or_replaced_wechat(monkeypatch, change):
    rig = FocusRig(monkeypatch)

    def change_window(action):
        if action == "composer-paste":
            rig.foreground = 9007
            rig.window_rows = {
                "missing": [],
                "duplicate": [(7, "mmui::MainWindow", "Weixin.exe"),
                              (8, "mmui::MainWindow", "Weixin.exe")],
                "changed-hwnd": [(8, "mmui::MainWindow", "Weixin.exe")],
            }[change]

    rig.on_action = change_window
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert BODY in rig.writes, "必须到达正文已粘贴后的窗口身份变化"
    assert rig.send.clicks == 0 and rig.sent == []
    assert rig.foreign_inputs == [] and rig.foreign_clicks == []
    assert_no_replay(rig)


def test_foreground_claim_success_flag_is_not_proof_of_exact_hwnd(monkeypatch):
    rig = FocusRig(monkeypatch)
    rig.foreground = 9007

    def false_success(_hwnd):
        rig.foreground = 9008
        return True

    monkeypatch.setattr(wechat_module, "_claim_foreground", false_success)
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert rig.keys == [] and rig.writes == []
    assert rig.search.clicks == 0 and rig.sent == []


@pytest.mark.parametrize("stage", [
    "search-click", "search-select", "search-delete", "search-paste",
    "composer-click", "composer-select", "composer-delete", "composer-paste",
])
@pytest.mark.parametrize("entry", ["helper", "keyboard"])
@pytest.mark.parametrize("restore", [True, False])
def test_focus_loss_between_native_actions_restores_or_refuses_without_foreign_input(monkeypatch, stage, entry, restore):
    rig = FocusRig(monkeypatch)
    rig.claim_succeeds = restore
    lost = []

    def lose_focus(action):
        if action == stage and not lost:
            lost.append(True)
            rig.foreground = 9007  # 另一个窗口，即使也是微信也不能代替原 HWND。
            rig.focused = rig.make("foreign", "Edit", "BrowserEdit", "", 0, 0, 10, 10)

    rig.on_action = lose_focus
    if restore:
        send(rig, monkeypatch, entry=entry)
    else:
        with pytest.raises(WaError):
            send(rig, monkeypatch, entry=entry)
    assert stage in rig.actions, "故障必须发生在所声明的原生步骤"
    assert rig.foreign_inputs == []
    assert rig.foreign_clicks == []
    assert rig.claims >= 1
    assert rig.sent == ([(TARGET, BODY)] if restore else [])
    assert_no_replay(rig)


@pytest.mark.parametrize("restore", [True, False])
def test_focus_loss_during_final_button_validation_restores_or_refuses_click(monkeypatch, restore):
    rig = FocusRig(monkeypatch)
    rig.claim_succeeds = restore
    lost = []
    original = rig.send.is_enabled

    def lose_focus():
        if BODY in rig.writes and not lost:
            lost.append(True)
            rig.foreground = 9007
        return original()

    monkeypatch.setattr(rig.send, "is_enabled", lose_focus)
    if restore:
        send(rig, monkeypatch)
    else:
        with pytest.raises(WaError):
            send(rig, monkeypatch)
    assert BODY in rig.writes
    assert rig.claims >= 1
    assert rig.send.clicks == (1 if restore else 0)
    assert rig.sent == ([(TARGET, BODY)] if restore else [])
    assert_no_replay(rig)


@pytest.mark.parametrize("hresult", [STALE, SIGNED_STALE])
@pytest.mark.parametrize("failures", [1])
def test_complete_popup_snapshot_recovers_only_exact_stale(monkeypatch, hresult, failures):
    rig = FocusRig(monkeypatch)
    raised = []

    def fault():
        if len(raised) < failures:
            raised.append(True)
            raise FakeComError(hresult)

    rig.popup_fault = fault
    send(rig, monkeypatch)
    assert len(raised) == failures
    assert rig.sent == [(TARGET, BODY)]
    assert rig.writes == [TARGET, BODY]
    assert_no_replay(rig)


def test_persistent_popup_stale_is_bounded_and_never_pastes_body(monkeypatch):
    rig = FocusRig(monkeypatch)

    def fault():
        raise FakeComError()

    rig.popup_fault = fault
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert 1 <= rig.popup_reads <= 2, "初次读取后最多重取一次"
    assert rig.writes == [TARGET]
    assert rig.result.clicks == 0 and rig.sent == []
    assert_no_replay(rig)


@pytest.mark.parametrize("change", ["history", "network", "missing", "duplicate"])
def test_popup_stale_restarts_complete_classification_and_uniqueness(monkeypatch, change):
    rig = FocusRig(monkeypatch)
    reads = []

    def heading_text():
        reads.append(True)
        if len(reads) == 1:
            if change in {"history", "network"}:
                rig.heading.name = "聊天记录" if change == "history" else "搜索网络结果"
            elif change == "missing":
                rig.result.name = "Unrelated synthetic contact"
            else:
                rig.popup_extra.append(Control(rig, "duplicate", "ListItem", "ListItem", TARGET, rig.result.rect))
            raise FakeComError()
        return rig.heading.name

    monkeypatch.setattr(rig.heading, "window_text", heading_text)
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert len(reads) >= 2, "必须重读完整快照，不能继续使用此前部分列表"
    assert BODY not in rig.writes
    assert rig.result.clicks == 0 and rig.sent == []
    assert_no_replay(rig)


@pytest.mark.parametrize("role", ["search", "composer", "send"])
def test_unique_control_stale_reacquires_new_object(monkeypatch, role):
    rig = FocusRig(monkeypatch)
    old = getattr(rig, role)
    replacement = Control(rig, role, old.element_info.control_type, old.element_info.class_name,
                          old.name, old.rect, enabled=old.enabled)
    reads = []

    def expired_rectangle():
        reads.append(True)
        rig.controls[rig.controls.index(old)] = replacement
        setattr(rig, role, replacement)
        raise FakeComError()

    monkeypatch.setattr(old, "rectangle", expired_rectangle)
    send(rig, monkeypatch)
    assert len(reads) == 1, "不能继续重试失效旧控件"
    assert old.clicks == 0
    assert replacement.clicks == 1
    assert rig.sent == [(TARGET, BODY)]
    assert_no_replay(rig)


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong-session"])
def test_composer_stale_reacquisition_revalidates_target(monkeypatch, change):
    rig = FocusRig(monkeypatch)
    reads = []

    def fault():
        reads.append(True)
        if len(reads) == 1:
            rig.invalidate("composer", change)
            raise FakeComError()
        return rig.composer.rect

    monkeypatch.setattr(rig.composer, "rectangle", fault)
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert BODY not in rig.writes
    assert rig.result.clicks == 1 and rig.sent == []
    # 缺失/歧义在 rectangle 前就会拒绝；会话变更会再次访问 rectangle。
    assert reads
    assert_no_replay(rig)


def test_final_send_stale_rechecks_session_after_full_read_retry(monkeypatch):
    rig = FocusRig(monkeypatch)
    stale = []
    invalid_session_reads = []

    def enabled():
        if BODY in rig.writes and not stale:
            stale.append(True)
            rig.invalidate("composer", "wrong-session")
            raise FakeComError()
        return rig.send.enabled

    def session_name():
        if rig.title.name != TARGET:
            invalid_session_reads.append(True)
        return rig.title.name

    monkeypatch.setattr(rig.send, "is_enabled", enabled)
    monkeypatch.setattr(rig.title, "window_text", session_name)
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert stale and invalid_session_reads, "按钮重读必须重过会话门，不能沿用此前目标结果"
    assert rig.send.clicks == 0 and rig.sent == []
    assert_no_replay(rig)


def test_composer_name_stale_reacquires_and_rechecks_target(monkeypatch):
    rig = FocusRig(monkeypatch)
    reads = []

    def name():
        reads.append(True)
        if len(reads) == 1:
            rig.invalidate("composer", "wrong-session")
            raise FakeComError()
        return rig.title.name

    monkeypatch.setattr(rig.title, "window_text", name)
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert len(reads) == 2
    assert BODY not in rig.writes and rig.sent == []
    assert_no_replay(rig)


@pytest.mark.parametrize("role", ["search", "composer", "send"])
def test_persistent_unique_control_stale_never_replays_native_steps(monkeypatch, role):
    rig = FocusRig(monkeypatch)
    attempts = []

    def fault():
        attempts.append(True)
        raise FakeComError()

    monkeypatch.setattr(getattr(rig, role), "rectangle", fault)
    with pytest.raises(WaError):
        send(rig, monkeypatch)
    assert 1 <= len(attempts) <= 2
    assert BODY not in rig.writes
    assert rig.sent == []
    assert_no_replay(rig)


@pytest.mark.parametrize("hresult", [0x80004005, 0x80040200, 0x80040202])
def test_other_com_errors_are_not_stale_retries(monkeypatch, hresult):
    rig = FocusRig(monkeypatch)
    attempts = []

    def fault():
        attempts.append(True)
        raise FakeComError(hresult)

    rig.popup_fault = fault
    with pytest.raises((WaError, FakeComError)):
        send(rig, monkeypatch)
    assert len(attempts) == 1
    assert BODY not in rig.writes and rig.sent == []
    assert_no_replay(rig)


@pytest.mark.parametrize("stage", ["composer-select", "composer-delete", "composer-paste", "send-click"])
def test_native_error_after_body_input_starts_never_replays(monkeypatch, stage):
    rig = FocusRig(monkeypatch)

    def fault(action):
        if action == stage:
            raise FakeComError()

    rig.on_action = fault
    with pytest.raises((WaError, FakeComError)):
        send(rig, monkeypatch)
    assert rig.actions.count(stage) == 1
    assert len(rig.sent) == (1 if stage == "send-click" else 0)
    assert_no_replay(rig)


def test_stale_diagnostic_has_stage_hresult_and_stack_without_message_data(monkeypatch, caplog):
    rig = FocusRig(monkeypatch)
    attempts = []

    def fault():
        attempts.append(True)
        if len(attempts) == 1:
            raise FakeComError()

    rig.popup_fault = fault
    with caplog.at_level(logging.DEBUG):
        send(rig, monkeypatch)
    diagnostic = [r for r in caplog.records if r.exc_info and any(
        code in repr(r.__dict__).lower() for code in ("80040201", str(STALE), str(SIGNED_STALE))) ]
    assert diagnostic, "stale诊断必须带异常栈，不能只写泛化失败"
    for record in diagnostic:
        rendered = record.getMessage()
        metadata = repr(record.__dict__)
        assert "stage" in metadata.lower()
        assert "stack" in metadata.lower() or record.exc_info[2], "须保留调用栈或脱敏栈位置"
        assert any(code in metadata.lower() for code in ("80040201", str(STALE), str(SIGNED_STALE)))
        assert TARGET not in rendered and BODY not in rendered
        assert TARGET not in caplog.text and BODY not in caplog.text
    assert rig.sent == [(TARGET, BODY)]
    assert_no_replay(rig)
