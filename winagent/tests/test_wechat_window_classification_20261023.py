"""Independent desktop-window classification contract; all native APIs are fake."""
from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace

import pytest

from qtrade_winagent.win.wechat import (
    WeChatWindowError,
    _visible_window_rows,
    begin_wechat_input,
    wechat_main_hwnds,
)


class DesktopRig:
    def __init__(self, monkeypatch, rows, *, uia=None, mutate=None, visibility=None):
        self.rows = {row[0]: row[1:] for row in rows}
        self.uia = uia or {}
        self.mutate = mutate
        self.visibility = visibility or {}
        self.reads = []
        self.inputs = []
        self.pid_reads = {}
        self.closed = []

        def forbidden(*args, **kwargs):
            self.inputs.append((args, kwargs))
            raise AssertionError("Window classification must not perform native input")

        def pid(hwnd):
            self.pid_reads[hwnd] = self.pid_reads.get(hwnd, 0) + 1
            value = self.rows[hwnd][2]
            if self.mutate == "pid" and self.pid_reads[hwnd] > 1:
                value += 1000
            return 1, value

        def image(handle, _flags, buf, _size):
            exe = next(row[1] for row in self.rows.values() if row[2] == handle)
            buf.value = "C:\\Synthetic\\" + exe
            return 1

        class NativeWindow:
            @property
            def element_info(inner):
                self.reads.append(inner.hwnd)
                value = self.uia.get(inner.hwnd)
                if isinstance(value, Exception):
                    raise value
                if value is None:
                    value = SimpleNamespace(
                        handle=inner.hwnd,
                        process_id=self.rows[inner.hwnd][2],
                        class_name="mmui::MainWindow",
                    )
                return value

        def window(*, handle):
            native = NativeWindow()
            native.hwnd = handle
            return native

        def desktop(*, backend):
            assert backend == "uia"
            return SimpleNamespace(window=window)

        def class_name(hwnd):
            if self.mutate == "class" and self.reads:
                return "mmui::LoginWindow"
            return self.rows[hwnd][0]

        def visible(hwnd):
            if self.mutate == "visibility" and self.reads:
                return False
            return self.visibility.get(hwnd, True)

        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
            kernel32=SimpleNamespace(
                OpenProcess=lambda _access, _inherit, process: process,
                QueryFullProcessImageNameW=image,
                CloseHandle=self.closed.append,
            ),
            user32=SimpleNamespace(keybd_event=forbidden, mouse_event=forbidden),
        ), raising=False)
        monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
            EnumWindows=lambda callback, state: [callback(hwnd, state) for hwnd in self.rows],
            GetClassName=class_name,
            IsWindowVisible=visible,
            SetForegroundWindow=forbidden,
            ShowWindow=forbidden,
        ))
        monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
            GetWindowThreadProcessId=pid,
        ))
        monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=desktop))

    def classify(self):
        return wechat_main_hwnds(_visible_window_rows())


def info(hwnd, *, pid=71, kind="mmui::MainWindow"):
    return SimpleNamespace(handle=hwnd, process_id=pid, class_name=kind)


def test_qt_login_window_is_excluded_without_input(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 81),
    ], uia={7: info(7), 8: info(8, pid=81, kind="mmui::LoginWindow")})
    assert rig.classify() == [7]
    assert rig.reads == [7, 8]
    assert rig.inputs == []


def test_one_verified_main_window_is_available_without_foreground_change(monkeypatch):
    rig = DesktopRig(monkeypatch, [(7, "Qt51514QWindowIcon", "Weixin.exe", 71)])
    calls = []
    assert begin_wechat_input(7, rig.classify(), calls.append, lambda: 7) == 7
    assert calls == []
    assert rig.inputs == []


def test_two_verified_main_windows_still_refuse_before_foreground_or_input(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "WeChat.exe", 81),
    ], uia={8: info(8, pid=81)})
    calls = []
    with pytest.raises(WeChatWindowError) as error:
        begin_wechat_input(7, rig.classify(), calls.append, lambda: 7)
    assert error.value.reason == "wechat_window_ambiguous"
    assert calls == []
    assert rig.inputs == []


@pytest.mark.parametrize("observed", [
    RuntimeError("Synthetic UIA unavailable"),
    info(7, kind="UnknownWindow"),
    info(7, kind=""),
    info(7, pid=999),
    info(99),
], ids=["uia-error", "unknown-class", "empty-class", "pid-mismatch", "handle-mismatch"])
def test_unverified_qt_candidate_blocks_the_whole_enumeration(monkeypatch, observed):
    rig = DesktopRig(monkeypatch, [
        (6, "mmui::MainWindow", "Weixin.exe", 61),
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
    ], uia={6: info(6, pid=61), 7: observed})
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


@pytest.mark.parametrize("mutate", ["pid", "class", "visibility"])
def test_candidate_changing_during_uia_read_is_not_selected(monkeypatch, mutate):
    rig = DesktopRig(monkeypatch, [(7, "Qt51514QWindowIcon", "Weixin.exe", 71)], mutate=mutate)
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


def test_explicit_win32_main_and_uia_login_disagreement_is_not_ignored(monkeypatch):
    rig = DesktopRig(monkeypatch, [(7, "mmui::MainWindow", "Weixin.exe", 71)],
                     uia={7: info(7, kind="mmui::LoginWindow")})
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


def test_non_wechat_qt_window_never_uses_uia_or_becomes_a_send_target(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "QTrade.exe", 71),
        (8, "Qt51514QWindowIcon", "Other.exe", 81),
    ])
    assert rig.classify() == []
    assert rig.reads == []
    assert rig.inputs == []


def test_verified_login_only_has_no_send_target(monkeypatch):
    rig = DesktopRig(monkeypatch, [(7, "Qt51514QWindowIcon", "Weixin.exe", 71)],
                     uia={7: info(7, kind="mmui::LoginWindow")})
    calls = []
    with pytest.raises(WeChatWindowError) as error:
        begin_wechat_input(None, rig.classify(), calls.append, lambda: None)
    assert error.value.reason == "wechat_window_missing"
    assert calls == []
    assert rig.inputs == []


def test_foreground_recheck_maps_uia_failure_to_not_ready_without_input(monkeypatch):
    from qtrade_winagent.errors import NOT_READY, WaError
    from qtrade_winagent.win.wechat import _ensure_wechat_foreground

    rig = DesktopRig(monkeypatch, [(7, "Qt51514QWindowIcon", "Weixin.exe", 71)],
                     uia={7: RuntimeError("Synthetic UIA unavailable")})
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: 3,
                        raising=False)
    with pytest.raises(WaError) as error:
        _ensure_wechat_foreground(7)
    assert error.value.code == NOT_READY
    assert error.value.reason == "wechat_window_unverified"
    assert error.value.http_status == 503
    assert rig.reads == [7]
    assert rig.inputs == []


def test_hidden_verified_main_and_login_are_classified_without_input(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 81),
    ], uia={8: info(8, pid=81, kind="mmui::LoginWindow")}, visibility={7: False, 8: False})
    assert rig.classify() == [7]
    assert rig.reads == [7, 8]
    assert rig.inputs == []


@pytest.mark.parametrize("observed", [info(7, kind="UnknownWindow"), RuntimeError("UIA unavailable")],
                         ids=["unknown-class", "uia-error"])
def test_unverified_hidden_wechat_blocks_visible_main_selection(monkeypatch, observed):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 81),
    ], uia={7: observed, 8: info(8, pid=81)}, visibility={7: False})
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


def test_two_verified_hidden_mains_still_refuse_before_restore_or_input(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 81),
    ], uia={8: info(8, pid=81)}, visibility={7: False, 8: False})
    calls = []
    with pytest.raises(WeChatWindowError) as error:
        begin_wechat_input(3, rig.classify(), calls.append, lambda: 3)
    assert error.value.reason == "wechat_window_ambiguous"
    assert calls == []
    assert rig.inputs == []


def test_other_hidden_apps_are_not_uia_inspected_or_selected(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Other.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 81),
    ], uia={8: info(8, pid=81)}, visibility={7: False, 8: False})
    assert rig.classify() == [8]
    assert rig.reads == [8]
    assert rig.inputs == []


def test_only_verified_hidden_main_is_restored_by_existing_foreground_path(monkeypatch):
    from qtrade_winagent.win.wechat import _claim_foreground

    rig = DesktopRig(monkeypatch, [(7, "Qt51514QWindowIcon", "Weixin.exe", 71)],
                     visibility={7: False})
    selected = rig.classify()
    assert rig.inputs == []
    state = {"foreground": 3}
    restored = []

    def restore(hwnd, command):
        restored.append((hwnd, command))
        rig.visibility[hwnd] = True
        state["foreground"] = hwnd

    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(SW_RESTORE=9))
    monkeypatch.setattr(sys.modules["win32gui"], "ShowWindow", restore)
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: state["foreground"],
                        raising=False)
    assert begin_wechat_input(3, selected, _claim_foreground, lambda: state["foreground"]) == 7
    assert restored == [(7, 9)]
    assert rig.inputs == []


@pytest.mark.parametrize("auxiliary_first", [False, True])
def test_observed_main_hidden_auxiliary_and_login_shape_keeps_only_real_main(monkeypatch, auxiliary_first):
    rows = [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (9, "Qt51514QWindowIcon", "Weixin.exe", 91),
    ]
    if auxiliary_first:
        rows.reverse()
    rig = DesktopRig(monkeypatch, rows, uia={8: info(8, kind="mmui::XHBoxView"),
            9: info(9, pid=91, kind="mmui::LoginWindow")}, visibility={8: False})
    assert rig.classify() == [7]
    assert set(rig.reads) == {7, 8, 9}
    assert rig.inputs == []


def test_hidden_auxiliary_without_verified_main_never_authorizes_send(monkeypatch):
    rig = DesktopRig(monkeypatch, [(7, "Qt51514QWindowIcon", "Weixin.exe", 71)],
                     uia={7: info(7, kind="mmui::XHBoxView")}, visibility={7: False})
    calls = []
    with pytest.raises(WeChatWindowError) as error:
        begin_wechat_input(3, rig.classify(), calls.append, lambda: 3)
    assert error.value.reason == "wechat_window_unverified"
    assert calls == []
    assert rig.inputs == []


@pytest.mark.parametrize("same_pid,hidden", [(False, True), (True, False)],
                         ids=["other-process", "visible-auxiliary"])
def test_auxiliary_exemption_requires_hidden_and_same_verified_main_process(monkeypatch, same_pid, hidden):
    auxiliary_pid = 71 if same_pid else 81
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", auxiliary_pid),
    ], uia={8: info(8, pid=auxiliary_pid, kind="mmui::XHBoxView")}, visibility={8: not hidden})
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


def test_unknown_mmui_hidden_class_is_not_treated_as_known_auxiliary(monkeypatch):
    rig = DesktopRig(monkeypatch, [
        (7, "Qt51514QWindowIcon", "Weixin.exe", 71),
        (8, "Qt51514QWindowIcon", "Weixin.exe", 71),
    ], uia={8: info(8, kind="mmui::UnrecognizedPanel")}, visibility={8: False})
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []
