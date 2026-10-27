"""Independent send preparation tests: fake native boundaries, no desktop effects."""
from __future__ import annotations

import pytest
import time

from qtrade_winagent.config import WechatConfig
from qtrade_winagent.errors import NOT_READY, WaError
from qtrade_winagent.wechat import WeChatSession
from qtrade_winagent.win import wechat as native


class NativeSendRig:
    """Exercise real session and Windows send orchestration with fake I/O only."""

    def __init__(self, monkeypatch, *, visible=True, minimized=False, reason=None):
        self.events = []
        self.group_flags = []
        self.running = True
        self.window = {"exists": True, "visible": visible, "minimized": minimized,
                       "pid": 71, "class_name": "mmui::MainWindow"}
        self.reason = reason
        self.backend = native.WinWeChat()
        monkeypatch.setattr(native, "require_windows", lambda _operation: None)
        monkeypatch.setattr(self.backend, "wechat_running", lambda: self.running)
        monkeypatch.setattr(self.backend, "main_window", lambda: dict(self.window))
        monkeypatch.setattr(self.backend, "key_state", lambda: {"ok": True})
        monkeypatch.setattr(self.backend, "ui_tree_visible", self.ui_tree)
        monkeypatch.setattr(self.backend, "launch", self.forbidden_launch)
        monkeypatch.setattr(self.backend, "_require_foreground", self.restore_existing)
        monkeypatch.setattr(self.backend, "_resolve_send_target", lambda _target: "QA Target")
        monkeypatch.setattr(self.backend, "_keyboard_send_text", self.fake_text_input)
        monkeypatch.setattr(self.backend, "_pyweixin_send", self.fake_image_input)
        monkeypatch.setattr(self.backend, "_readback_rows", self.fake_readback)
        monkeypatch.setattr(self.backend, "_send_identity", lambda: ("wxid_synthetic", 71, 1.0, 72, 2.0, "synthetic-data"))
        self.session = WeChatSession(self.backend, WechatConfig(enabled=True), clock=lambda: 12345)

    async def forbidden_launch(self):
        self.events.append("launch")
        raise AssertionError("Sending must never launch a second WeChat process")

    def restore_existing(self):
        self.events.append("prepare-existing")
        if self.reason:
            raise WaError(NOT_READY, "Synthetic unready native window", reason=self.reason)
        self.window.update(visible=True, minimized=False)
        return 7

    def ui_tree(self):
        self.events.append("ui-tree")
        return True

    def fake_text_input(self, _name, _text, expected_group=False):
        assert type(expected_group) is bool
        self.group_flags.append(expected_group)
        self.events.append("fake-text-input")

    async def fake_image_input(self, _name, _text, _image_path):
        self.events.append("fake-image-input")

    def fake_readback(self, _talker, text):
        self.events.append("fake-readback")
        baseline = {"talker": _talker, "isSelf": True, "seq": 1, "content": "Earlier native text", "type": 1, "time": 1_700_000_000}
        if "fake-text-input" in self.events or "fake-image-input" in self.events:
            return [baseline, {"talker": _talker, "isSelf": True, "seq": 2, "content": "QA synthetic only",
                "type": 3 if "fake-image-input" in self.events else 1, "time": time.time()}]
        return [baseline]


@pytest.mark.parametrize("visible,minimized", [(True, False), (True, True)],
                         ids=["visible", "minimized"])
@pytest.mark.parametrize("image", [False, True], ids=["text", "image"])
async def test_send_reuses_existing_window_without_launch(monkeypatch, visible, minimized, image):
    rig = NativeSendRig(monkeypatch, visible=visible, minimized=minimized)
    result = await rig.session.send(session_name="synthetic-peer", text="QA synthetic only",
                                    image_path="synthetic-image.png" if image else None, confirm_timeout_ms=100)
    assert result["ok"] is (not image)
    assert result["code"] == ("SEND_FAILED" if image else "DELIVERED")
    assert rig.events[0] == "prepare-existing"
    assert "launch" not in rig.events
    assert rig.window["pid"] == 71
    assert rig.window["visible"] and not rig.window["minimized"]
    input_event = "fake-image-input" if image else "fake-text-input"
    assert rig.events.count(input_event) == 1
    assert rig.group_flags == ([] if image else [False])
    if image:
        assert rig.events.index("prepare-existing") < rig.events.index("ui-tree")
        assert rig.events.index("ui-tree") < rig.events.index(input_event)


@pytest.mark.parametrize("reason", ["wechat_window_missing", "wechat_window_unverified",
                                    "wechat_window_ambiguous"])
async def test_send_window_refusal_never_launches_or_reaches_native_input(monkeypatch, reason):
    rig = NativeSendRig(monkeypatch, minimized=True, reason=reason)
    if reason == "wechat_window_missing":
        rig.running = False
        rig.window.update(exists=False, visible=False, pid=None)
    with pytest.raises(WaError) as error:
        await rig.session.send(session_name="synthetic-peer", text="QA synthetic only")
    assert error.value.code == NOT_READY
    assert error.value.reason == reason
    assert rig.events == ["prepare-existing"]


async def test_hidden_window_not_proven_by_guard_is_refused_without_launch(monkeypatch):
    rig = NativeSendRig(monkeypatch, visible=False, reason="wechat_window_missing")
    with pytest.raises(WaError) as error:
        await rig.session.send(session_name="synthetic-peer", text="QA synthetic only")
    assert error.value.reason == "wechat_window_missing"
    assert rig.events == ["prepare-existing"]


async def test_key_failure_refuses_before_window_preparation_or_launch(monkeypatch):
    rig = NativeSendRig(monkeypatch, minimized=True)
    monkeypatch.setattr(rig.backend, "key_state", lambda: {"ok": False})
    with pytest.raises(WaError) as error:
        await rig.session.send(session_name="synthetic-peer", text="QA synthetic only")
    assert error.value.code == NOT_READY
    assert error.value.reason == "key_fail"
    assert rig.events == []


async def test_explicit_login_preparation_still_can_launch_missing_wechat(monkeypatch):
    rig = NativeSendRig(monkeypatch)
    rig.running = False
    rig.window.update(exists=False, visible=False, pid=None)

    async def login_launch():
        rig.events.append("explicit-login-launch")
        rig.running = True
        rig.window.update(exists=True, visible=True, minimized=False, pid=71)
        return 71

    monkeypatch.setattr(rig.backend, "launch", login_launch)
    await rig.session._ensure_wechat_running()
    assert rig.events == ["explicit-login-launch"]
    assert rig.window["exists"]
