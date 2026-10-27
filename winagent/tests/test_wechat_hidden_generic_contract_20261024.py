"""Developer regressions for the explicitly approved hidden generic-Qt contract.

All Win32/UIA APIs are fake; no desktop input or live message is sent.
"""
import sys

import pytest

from qtrade_winagent.win.wechat import WeChatWindowError, begin_wechat_input
from test_wechat_window_classification_20261023 import DesktopRig, info


QT = "Qt51514QWindowIcon"


def observed(monkeypatch, *, auxiliary_first=False, visibility=None, hidden_pid=71, hidden_info=None):
    rows = [(7, QT, "Weixin.exe", 71), (8, QT, "Weixin.exe", hidden_pid)]
    if auxiliary_first:
        rows.reverse()
    return DesktopRig(monkeypatch, rows,
        uia={7: info(7), 8: hidden_info if hidden_info is not None else info(8, pid=hidden_pid, kind=QT)},
        visibility={8: False, **(visibility or {})})


@pytest.mark.parametrize("auxiliary_first", [False, True])
def test_actual_hidden_generic_qt_does_not_block_one_verified_visible_main(monkeypatch, auxiliary_first):
    rig = observed(monkeypatch, auxiliary_first=auxiliary_first)
    assert rig.classify() == [7]
    assert set(rig.reads) == {7, 8}, "Exclusion requires successful UIA identity reads of both windows"
    assert rig.inputs == []


@pytest.mark.parametrize("missing_proof", [
    "visible", "native-class", "uia-class", "uia-error", "handle", "pid",
    "cross-process", "no-visible-main", "two-visible-mains", "hidden-second-main",
])
def test_hidden_exclusion_requires_every_approved_proof(monkeypatch, missing_proof):
    rows = [(7, QT, "Weixin.exe", 71), (8, QT, "Weixin.exe", 71)]
    uia = {7: info(7), 8: info(8, kind=QT)}
    visibility = {8: False}
    if missing_proof == "visible":
        visibility[8] = True
    elif missing_proof == "native-class":
        rows[1] = (8, "mmui::LoginWindow", "Weixin.exe", 71)
    elif missing_proof == "uia-class":
        uia[8] = info(8, kind="UnknownWindow")
    elif missing_proof == "uia-error":
        uia[8] = RuntimeError("synthetic UIA read failed")
    elif missing_proof == "handle":
        uia[8] = info(99, kind=QT)
    elif missing_proof == "pid":
        uia[8] = info(8, pid=81, kind=QT)
    elif missing_proof == "cross-process":
        rows[1] = (8, QT, "Weixin.exe", 81)
        uia[8] = info(8, pid=81, kind=QT)
    elif missing_proof == "no-visible-main":
        visibility[7] = False
    else:
        rows.append((9, QT, "Weixin.exe", 71))
        uia[9] = info(9)
        visibility[9] = missing_proof != "hidden-second-main"
    rig = DesktopRig(monkeypatch, rows, uia=uia, visibility=visibility)
    with pytest.raises(WeChatWindowError):
        begin_wechat_input(3, rig.classify(), lambda _hwnd: pytest.fail("No foreground action permitted"), lambda: 3)
    assert rig.inputs == []


def test_hidden_candidate_becoming_visible_during_collection_is_rejected(monkeypatch):
    rig = observed(monkeypatch, auxiliary_first=True)
    # It becomes visible only after its own UIA read and while the later main is read.
    monkeypatch.setattr(sys.modules["win32gui"], "IsWindowVisible",
                        lambda hwnd: (7 in rig.reads) if hwnd == 8 else True)
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


def test_visible_main_disappearing_after_its_own_read_is_rejected(monkeypatch):
    rig = observed(monkeypatch)
    monkeypatch.setattr(sys.modules["win32gui"], "IsWindowVisible",
                        lambda hwnd: False if hwnd == 8 else 8 not in rig.reads)
    with pytest.raises(WeChatWindowError):
        rig.classify()
    assert rig.inputs == []


def test_next_input_guard_rejects_previously_hidden_window_now_visible(monkeypatch):
    rig = observed(monkeypatch)
    assert rig.classify() == [7]
    rig.visibility[8] = True
    with pytest.raises(WeChatWindowError):
        begin_wechat_input(7, rig.classify(), lambda _hwnd: pytest.fail("No foreground action permitted"), lambda: 7)
    assert rig.inputs == []

