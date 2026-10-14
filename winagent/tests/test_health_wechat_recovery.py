"""微信取钥恢复的独立行为回归；进程、IPC、Windows 探测均为假件。"""
from __future__ import annotations

import asyncio
import threading
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent.config import WechatConfig
from qtrade_winagent.fakes import FakeWeChat
from qtrade_winagent.errors import WaError
from qtrade_winagent.wechat import WeChatSession
from qtrade_winagent.win import wechat as native
from tests.conftest import AGENT_TOKEN, CONSOLE_TOKEN, client


@pytest.fixture
def native_rig(tmp_path, monkeypatch):
    assets = tmp_path / "D-drive assets 空格"
    assets.mkdir()
    pe = bytearray(128)
    pe[:2] = b"MZ"
    pe[0x3c:0x40] = (64).to_bytes(4, "little")
    pe[64:70] = b"PE\x00\x00\x64\x86"
    for name in ("chatlog.exe", "wx_key1.dll", "wx_key2.dll"):
        (assets / name).write_bytes(bytes(pe) + name.encode())
    work = tmp_path / "用户 空间"
    work.mkdir()
    wx = native.WinWeChat(chatlog_dir=str(assets), chatlog_work_dir=str(work))
    monkeypatch.setattr(native, "require_windows", lambda _: None)
    monkeypatch.setattr(wx, "locate", lambda: {"installed": True, "version": "4.1.12.26", "path": "D:\\Apps\\Weixin.exe"})
    monkeypatch.setattr(wx, "login_pid", lambda: 4401)
    fake_process = SimpleNamespace(name=lambda: "Weixin.exe", exe=lambda: "D:\\Apps\\Weixin.exe", create_time=lambda: 100.5)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda _pid: fake_process,
                        Error=OSError, AccessDenied=PermissionError, NoSuchProcess=ProcessLookupError))
    process = SimpleNamespace(pid=5501, returncode=None, wait=AsyncMock(return_value=0), kill=lambda: None)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(native, "spawn_redacted", spawn)
    monkeypatch.setattr(native.os, "kill", lambda *a: pytest.fail("No real signal is allowed"))
    return wx, assets, work, spawn


async def test_key_start_does_not_query_the_read_service_port(native_rig):
    wx, _assets, _work, spawn = native_rig
    calls = []

    def net_connections(kind="tcp"):
        calls.append(kind)
        raise AssertionError("取钥动作查询了读取服务端口")

    sys.modules["psutil"].net_connections = net_connections
    sys.modules["psutil"].CONN_LISTEN = "LISTEN"
    assert await wx.chatlog_start("wx_key2.dll") == 5501
    assert calls == []
    spawn.assert_awaited()


async def test_nondefault_unicode_paths_use_explicit_target_and_do_not_require_ffmpeg(native_rig):
    wx, assets, work, spawn = native_rig
    assert await wx.chatlog_start("wx_key2.dll") == 5501
    args = spawn.call_args.args[0]
    assert args[0] == str(assets / "chatlog.exe")
    assert args[args.index("--pid") + 1] == "4401"
    assert spawn.call_args.kwargs["cwd"] == str(work)
    assert not (assets / "ffmpeg.exe").exists()


@pytest.mark.parametrize("name", ["chatlog.exe", "wx_key2.dll"])
async def test_missing_required_asset_fails_before_process_creation(native_rig, name):
    wx, assets, _work, spawn = native_rig
    (assets / name).unlink()
    with pytest.raises(Exception) as failure:
        await wx.chatlog_start("wx_key2.dll")
    assert name in str(failure.value)
    spawn.assert_not_awaited()


async def test_no_trustworthy_pid_rejects_instead_of_entering_interactive_selector(native_rig, monkeypatch):
    wx, _assets, _work, spawn = native_rig
    monkeypatch.setattr(wx, "login_pid", lambda: None)
    with pytest.raises(Exception):
        await wx.chatlog_start("wx_key2.dll")
    spawn.assert_not_awaited()


async def test_process_discovery_runs_off_event_loop_and_keeps_callbacks_responsive(native_rig, monkeypatch):
    wx, _assets, _work, _spawn = native_rig
    loop_thread = threading.get_ident()
    called = []
    heartbeat = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_probe():
        called.append(threading.get_ident())
        loop.call_soon_threadsafe(heartbeat.set)
        assert heartbeat.wait(1), "Process discovery blocked the heartbeat event loop"
        return 4401

    monkeypatch.setattr(wx, "login_pid", slow_probe)
    await wx.chatlog_start("wx_key2.dll")
    assert called and all(thread_id != loop_thread for thread_id in called)


def test_ambiguous_main_processes_do_not_choose_target_by_memory_size():
    candidates = [{"pid": 41, "ppid": 1, "cmdline": "Weixin.exe", "rss": 100},
                  {"pid": 42, "ppid": 1, "cmdline": "Weixin.exe", "rss": 900}]
    assert native.pick_wechat_root_pid(candidates) is None
    assert native.pick_wechat_root_pid(candidates, 41) == 41


def test_locked_different_dll_never_claims_replacement_succeeded(tmp_path, monkeypatch):
    source, work = tmp_path / "assets", tmp_path / "work"
    source.mkdir()
    (source / "wx_key1.dll").write_bytes(b"MZ-first")
    (source / "wx_key2.dll").write_bytes(b"MZ-second")
    target = native.stage_wx_key_dll(str(source), str(work), "wx_key1.dll")
    monkeypatch.setattr(native.shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    assert native.stage_wx_key_dll(str(source), str(work), "wx_key1.dll") == target
    with pytest.raises(Exception):
        native.stage_wx_key_dll(str(source), str(work), "wx_key2.dll")
    from pathlib import Path
    assert Path(target).read_bytes() == b"MZ-first"


async def test_tool_start_failure_becomes_terminal_session_with_reason(monkeypatch):
    wx = FakeWeChat()
    wx.running_pid = 4401
    session = WeChatSession(wx, WechatConfig(enabled=True))
    monkeypatch.setattr(wx, "chatlog_start", AsyncMock(side_effect=WaError("TARGET_NOT_FOUND", "chatlog.exe missing", reason="wechat_tool_missing")))
    try:
        await session.login_start(account_id="wx01", login_session_id="failed-round")
    except Exception:
        pass
    status = session.login_status()
    assert status["phase"] == "key_failed"
    assert "chatlog.exe" in str(status["key"].get("error"))


async def test_duplicate_login_start_coalesces_single_round(monkeypatch):
    wx = FakeWeChat()
    wx.running_pid = 4401
    session = WeChatSession(wx, WechatConfig(enabled=True))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def start(dll):
        calls.append(dll)
        entered.set()
        await release.wait()
        return 5501

    monkeypatch.setattr(wx, "chatlog_start", start)
    first = asyncio.create_task(session.login_start(account_id="wx01", login_session_id="same-round"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(session.login_start(account_id="wx01", login_session_id="same-round"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert len(calls) == 1, "Identical request must not start another hook"
    assert not any(isinstance(result, Exception) for result in results)
    assert session.login_status()["login_session_id"] == "same-round"


async def test_cancelled_slow_scan_keeps_ownership_until_return_and_cannot_start_late_hook(monkeypatch):
    wx = FakeWeChat()
    wx.running_pid = 4401
    session = WeChatSession(wx, WechatConfig(enabled=True))
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    first_scan = True

    def blocked_scan():
        nonlocal first_scan
        if first_scan:
            first_scan = False
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2), "test scan release was not reached"
        return True

    monkeypatch.setattr(wx, "wechat_running", blocked_scan)
    hook = AsyncMock(return_value=5501)
    monkeypatch.setattr(wx, "chatlog_start", hook)
    first = asyncio.create_task(session.login_start(account_id="wx01", login_session_id="round-A"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        cancelled = await asyncio.wait_for(session.login_cancel(login_session_id="round-A"), 0.5)
        assert cancelled["cancelled"] is True
        with pytest.raises(WaError) as rejected:
            await session.login_start(account_id="wx02", login_session_id="round-B")
        assert rejected.value.code == "BUSY"
        assert session.login_status()["login_session_id"] == "round-A"
        hook.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    hook.assert_not_awaited()
    await session.login_start(account_id="wx02", login_session_id="round-B")
    assert session.login_status()["login_session_id"] == "round-B"
    assert hook.await_count == 1
    stale = await session.login_cancel(login_session_id="round-A")
    assert stale["cancelled"] is False
    assert session.login_status()["login_session_id"] == "round-B"


async def test_immediate_tool_exit_is_explicit_failure_not_hook_success(native_rig):
    wx, _assets, _work, spawn = native_rig
    spawn.return_value.returncode = 2
    with pytest.raises(Exception) as raised:
        await wx.chatlog_start("wx_key2.dll")
    assert "2" in str(raised.value)


async def test_cancel_during_dll_write_keeps_lock_through_second_cancel_and_no_late_spawn(native_rig, monkeypatch):
    wx, _assets, _work, spawn = native_rig
    monkeypatch.setattr(wx, "wechat_running", lambda: True)
    monkeypatch.setattr(wx, "launch", AsyncMock(return_value=4401))
    monkeypatch.setattr(wx, "key_state", lambda: {"ok": False, "img_key": False, "data_key": False})
    monkeypatch.setattr(wx, "narrator_running", lambda: False)
    session = WeChatSession(wx, WechatConfig(enabled=True))
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    writes = []

    def stage(*args):
        writes.append(args)
        if len(writes) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2), "test DLL write was not released"

    monkeypatch.setattr(native, "stage_wx_key_dll", stage)
    first = asyncio.create_task(session.login_start(account_id="wx01", login_session_id="dll-A"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await session.login_cancel(login_session_id="dll-A")
        await session.login_cancel(login_session_id="dll-A")
        with pytest.raises(WaError) as busy:
            await session.login_start(account_id="wx02", login_session_id="dll-B")
        assert busy.value.code == "BUSY"
        assert len(writes) == 1
        spawn.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    spawn.assert_not_awaited()
    await session.login_start(account_id="wx02", login_session_id="dll-B")
    assert len(writes) == 2
    assert spawn.await_count == 1
    assert session.login_status()["login_session_id"] == "dll-B"


async def test_old_cancel_response_after_new_round_started_cannot_mark_new_round_failed(monkeypatch):
    wx = FakeWeChat()
    wx.running_pid = 4401
    session = WeChatSession(wx, WechatConfig(enabled=True))
    entered, reply_pending, return_old_reply = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_wait_for = asyncio.wait_for
    first = None
    rounds = []

    async def hook(dll):
        rounds.append(dll)
        if len(rounds) == 1:
            entered.set()
            await asyncio.Future()
        return 5501

    async def delayed_cancel_reply(awaitable, timeout):
        try:
            return await real_wait_for(awaitable, timeout)
        except asyncio.CancelledError:
            if asyncio.current_task() is first:
                reply_pending.set()
                await return_old_reply.wait()
            raise

    monkeypatch.setattr(wx, "chatlog_start", hook)
    monkeypatch.setattr(asyncio, "wait_for", delayed_cancel_reply)
    first = asyncio.create_task(session.login_start(account_id="wx01", login_session_id="late-A"))
    try:
        await real_wait_for(entered.wait(), 1)
        await session.login_cancel(login_session_id="late-A")
        await real_wait_for(reply_pending.wait(), 1)
        await session.login_start(account_id="wx02", login_session_id="late-B")
    finally:
        return_old_reply.set()
        if not first.done():
            await real_wait_for(asyncio.gather(first, return_exceptions=True), 2)
    assert session.login_status()["login_session_id"] == "late-B"
    assert session.login_status()["phase"] == "qrcode"
    assert not session.login_status()["key"].get("error")


@pytest.mark.parametrize("name", ["chatlog.exe", "wx_key2.dll"])
async def test_corrupt_tool_is_rejected_before_any_process(native_rig, name):
    wx, assets, _work, spawn = native_rig
    (assets / name).write_bytes(b"MZ-not-a-valid-x64-PE")
    with pytest.raises(Exception) as failed:
        await wx.chatlog_start("wx_key2.dll")
    assert name in str(failed.value)
    spawn.assert_not_awaited()


@pytest.mark.parametrize("name", ["chatlog.exe", "wx_key2.dll"])
async def test_unreadable_tool_reports_access_problem_without_spawn(native_rig, monkeypatch, name):
    import builtins
    wx, assets, _work, spawn = native_rig
    real_open = builtins.open

    def denied(path, *args, **kwargs):
        if str(path) == str(assets / name):
            raise PermissionError("test file denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(WaError) as failed:
        await wx.chatlog_start("wx_key2.dll")
    assert failed.value.reason == "wechat_tool_access_denied"
    assert name in failed.value.message
    spawn.assert_not_awaited()


async def test_unwritable_work_directory_is_reported_without_spawn(native_rig, monkeypatch):
    wx, _assets, _work, spawn = native_rig
    monkeypatch.setattr(native.tempfile, "TemporaryFile", lambda **kwargs: (_ for _ in ()).throw(PermissionError("test denied")))
    with pytest.raises(WaError) as failed:
        await wx.chatlog_start("wx_key2.dll")
    assert failed.value.reason == "wechat_workdir_unwritable"
    spawn.assert_not_awaited()


@pytest.mark.parametrize("token,budget,advance", [(AGENT_TOKEN, 10, True), (CONSOLE_TOKEN, 3, False)])
@pytest.mark.parametrize("path", ["/wa/v1/wechat/status", "/wa/v1/wechat/login/status"])
async def test_status_pipe_budget_and_progress_permission_come_from_server_role(rig_with_user, monkeypatch, token, budget, advance, path):
    pipe = AsyncMock(return_value={"phase": "idle"})
    monkeypatch.setattr(rig_with_user.deps.hub, "call", pipe)
    async with client(rig_with_user, token=token) as http:
        response = await http.get(path, params={"advance": str(not advance).lower()})
    assert response.status_code == 200
    assert pipe.call_args.kwargs["timeout_s"] == budget
    assert pipe.call_args.args[1]["advance"] is advance


async def test_reused_pid_does_not_receive_interrupt_and_cleanup_uses_owned_handle(monkeypatch):
    from qtrade_winagent.win import proc
    child = SimpleNamespace(pid=5501, returncode=None)
    interrupt = AsyncMock()
    kill = []

    async def wait():
        if child.returncode is None:
            raise TimeoutError
        return child.returncode

    def kill_handle():
        kill.append(child)
        child.returncode = -1

    child.wait, child.kill = wait, kill_handle
    adapter = native.WinWeChat()
    adapter._chatlog_process, adapter._chatlog_pid, adapter._chatlog_created = child, 5501, 100.0
    monkeypatch.setattr(adapter, "_process_created", lambda pid: 200.0)
    monkeypatch.setattr(proc, "WinProc", lambda: SimpleNamespace(interrupt=interrupt))
    monkeypatch.setattr(native.os, "kill", lambda *a: pytest.fail("No real signal"))
    await adapter.chatlog_stop()
    interrupt.assert_not_awaited()
    assert kill == [child]


@pytest.mark.parametrize("path,mutation", [("/wa/v1/wechat/status", "ensure_server"),
                                         ("/wa/v1/wechat/login/status", "poll")])
async def test_console_snapshot_does_not_advance_wechat_but_agent_still_drives(rig_with_user, monkeypatch, path, mutation):
    wx_session = rig_with_user.user_agent._wx()
    original = getattr(wx_session, mutation)
    spy = AsyncMock(wraps=original)
    monkeypatch.setattr(wx_session, mutation, spy)
    async with client(rig_with_user, token=CONSOLE_TOKEN) as http:
        response = await http.get(path, params={"advance": "true"})
    assert response.status_code == 200
    assert spy.await_count == 0, "Console caller cannot grant itself progression permission"
    async with client(rig_with_user, token=AGENT_TOKEN) as http:
        response = await http.get(path)
    assert response.status_code == 200
    assert spy.await_count == 1
