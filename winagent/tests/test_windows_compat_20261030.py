"""2026-10-30 developer regressions: real entrypoints, fake OS only."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

from qtrade_winagent.errors import WaError
from qtrade_winagent.installer_ops import InstallerOps
from qtrade_winagent.user import UserAgent
from conftest import client


@pytest.mark.parametrize("supplied", [False, True], ids=["current", "supplied"])
async def test_run05_apply_sends_staged_nonempty_path(rig, monkeypatch, supplied):
    target = r"C:\isolated-fixture\kernel\bzImage"
    hub = AsyncMock(return_value={"verify": {"ok": True}})
    monkeypatch.setattr(rig.deps.hub, "call", hub)
    monkeypatch.setattr(rig.deps.installer, "current_kernel", lambda: target)
    monkeypatch.setattr(rig.deps.installer, "stage_kernel", lambda _: target)
    result = await rig.deps.installer.kernel_apply(
        confirm_shutdown=True, kernel_src="fixture-source" if supplied else None)
    assert result["kernel"] == target
    assert hub.await_args.args[0] == "wsl.kernel.apply"
    assert hub.await_args.args[1]["kernel_path"] == target


@pytest.mark.parametrize("failure", ["missing", "stage-error", "no-consent"])
async def test_run05_failure_never_calls_user(rig, monkeypatch, failure):
    hub = AsyncMock()
    monkeypatch.setattr(rig.deps.hub, "call", hub)
    monkeypatch.setattr(rig.deps.installer, "current_kernel", lambda: None)
    monkeypatch.setattr(rig.deps.installer, "stage_kernel", Mock(side_effect=WaError("INTERNAL", "fixture")))
    with pytest.raises(WaError):
        await rig.deps.installer.kernel_apply(confirm_shutdown=failure != "no-consent",
                                              kernel_src="fixture" if failure == "stage-error" else None)
    hub.assert_not_awaited()


async def test_run05_failed_verification_rolls_back(rig, monkeypatch):
    hub = AsyncMock(return_value={"verify": {"ok": False}})
    rollback = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(rig.deps.hub, "call", hub)
    monkeypatch.setattr(rig.deps.installer, "current_kernel", lambda: r"C:\fixture\bzImage")
    monkeypatch.setattr(rig.deps.installer, "kernel_rollback", rollback)
    with pytest.raises(WaError) as exc:
        await rig.deps.installer.kernel_apply(confirm_shutdown=True)
    assert exc.value.reason == "kernel_verify_failed"
    rollback.assert_awaited_once_with(confirm_shutdown=True, trace_id=None, auto=True)


async def test_run03_locked_session_reaches_http_status(rig_with_user):
    rig_with_user.sys.locked = True
    async with client(rig_with_user) as http:
        response = await http.get("/wa/v1/wechat/status")
    assert response.status_code == 200, response.text
    assert response.json()["screen_locked"] is True


@pytest.mark.parametrize("value", [None, "error"])
async def test_run03_unknown_lock_never_becomes_unlocked(rig_with_user, monkeypatch, value):
    wx = SimpleNamespace(read_status=AsyncMock(side_effect=lambda **kw: kw))
    ua = rig_with_user.user_agent
    ua._session_locked = Mock(side_effect=OSError("fixture")) if value == "error" else lambda: None
    monkeypatch.setattr(ua, "_wx", lambda: wx)
    result = await ua._wechat_status({"screen_locked": False})
    assert result["screen_locked"] is True


@pytest.mark.parametrize("locked", [True, False])
async def test_run03_explicit_lock_fact_preserved(rig_with_user, monkeypatch, locked):
    wx = SimpleNamespace(read_status=AsyncMock(side_effect=lambda **kw: kw))
    ua = rig_with_user.user_agent
    ua._session_locked = lambda: locked
    monkeypatch.setattr(ua, "_wx", lambda: wx)
    result = await ua._wechat_status({"screen_locked": not locked})
    assert result["screen_locked"] is locked


def test_run04_no_identity_never_kills_by_image(monkeypatch):
    from qtrade_winagent.win import wechat, proc
    command = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(wechat, "run_text", command)
    monkeypatch.setattr(proc.WinProc, "find", lambda *_: [])
    try:
        wechat.WinWeChat.kill_narrator_elevated()
    except (TypeError, WaError):
        pass  # Requiring a trusted target is a valid refusal.
    assert command.call_count == 0, "RUN-04: missing PID/SID/session identity must fail closed"


@pytest.fixture
def narrator_boundary(monkeypatch):
    from qtrade_winagent.win import narrator
    target = {"pid": 901, "sid": "S-1-5-21-1-2-3-1001", "session_id": 7,
              "created": "2026-10-30T01:02:03", "exe": r"c:\windows\system32\narrator.exe"}
    handle = SimpleNamespace(Close=Mock())
    api = SimpleNamespace(OpenProcess=Mock(return_value=handle), TerminateProcess=Mock())
    event = SimpleNamespace(WaitForSingleObject=Mock(return_value=0), WAIT_OBJECT_0=0)
    monkeypatch.setitem(sys.modules, "win32api", api)
    monkeypatch.setitem(sys.modules, "win32event", event)
    monkeypatch.setattr(narrator, "identity", Mock(return_value=dict(target)))
    monkeypatch.setattr(narrator, "executable", lambda: r"C:\Windows\System32\Narrator.exe")
    return narrator, target, handle, api, event


def test_run04_verified_target_terminates_only_same_open_handle(narrator_boundary):
    narrator, target, handle, api, event = narrator_boundary
    assert narrator.stop_owned(target, owner_sid=target["sid"], owner_session_id=7) is True
    assert api.OpenProcess.call_args.args[2] == 901
    narrator.identity.assert_called_once_with(handle)
    api.TerminateProcess.assert_called_once_with(handle, 0)
    assert event.WaitForSingleObject.call_args.args[0] is handle
    handle.Close.assert_called_once()


@pytest.mark.parametrize("field,value", [("pid",902), ("sid","S-1-5-21-1-2-3-1002"),
    ("session_id",8), ("created","2026-10-30T02:00:00"), ("exe",r"c:\fixture\narrator.exe")])
def test_run04_reused_pid_or_changed_process_identity_is_not_terminated(narrator_boundary, field, value):
    narrator, target, handle, api, _ = narrator_boundary
    narrator.identity.return_value[field] = value
    assert narrator.stop_owned(target, owner_sid=target["sid"], owner_session_id=7) is False
    api.TerminateProcess.assert_not_called()
    handle.Close.assert_called_once()


@pytest.mark.parametrize("sid,session", [("S-1-5-21-1-2-3-1002",7),("S-1-5-21-1-2-3-1001",8),("",7),("S-1-5-21-1-2-3-1001",0)])
def test_run04_wrong_owner_or_session_rejected_before_open(narrator_boundary, sid, session):
    narrator, target, _, api, _ = narrator_boundary
    assert narrator.stop_owned(target, owner_sid=sid, owner_session_id=session) is False
    api.OpenProcess.assert_not_called()
    api.TerminateProcess.assert_not_called()


async def test_run04_service_refuses_pending_stop_without_target(rig_with_user, monkeypatch):
    killer = Mock(return_value=True)
    rig_with_user.deps.narrator_killer = killer
    monkeypatch.setattr(rig_with_user.deps.hub, "call", AsyncMock(return_value={
        "narrator": {"state": "running", "stop_pending": True}, "_narrator_target": None}))
    async with client(rig_with_user) as http:
        response = await http.get("/wa/v1/wechat/login/status")
    assert response.status_code == 200
    assert response.json()["narrator"]["stop_pending"] is True
    assert "_narrator_target" not in response.json()
    killer.assert_not_called()


@pytest.mark.parametrize("fact", [True, None])
async def test_run03_locked_or_unknown_blocks_screenshot_but_keeps_reads(rig_with_user, fact):
    rig_with_user.user_agent._session_locked = lambda: fact
    rig_with_user.wechat.data_key = rig_with_user.wechat.img_key = True
    async with client(rig_with_user) as http:
        read = await http.get("/wa/v1/wechat/read?talker=fixture")
        screenshot = await http.get("/wa/v1/wechat/screenshot")
    assert read.status_code == 200
    assert screenshot.status_code == 503
    assert screenshot.json()["error"]["reason"] == "screen_locked"
