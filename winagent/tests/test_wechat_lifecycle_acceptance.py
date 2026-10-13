"""Independent R6-106 lifecycle acceptance; all OS and process inputs are fake."""
from __future__ import annotations

import asyncio
import errno
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent.config import WechatConfig
from qtrade_winagent.errors import WaError
from qtrade_winagent.fakes import FakeWeChat
from qtrade_winagent.user import UserAgent
from qtrade_winagent.wechat import LoginSession, WeChatSession
from qtrade_winagent.win.wechat import WinWeChat


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["keytry", "qrcode", "narrator"])
async def test_status_during_login_never_probes_or_cleans_active_hook(phase):
    backend = FakeWeChat(chatlog_pid=5303)
    manager = WeChatSession(backend, WechatConfig(enabled=True))
    manager.session = LoginSession("acceptance-active", "wx-test", phase=phase)
    release = threading.Event()
    probes = []
    original = backend.key_state

    def slow_key_state():
        probes.append("key_state")
        assert release.wait(3), "acceptance fixture did not release its worker"
        return original()

    backend.key_state = slow_key_state
    operation = asyncio.create_task(manager.ensure_server())
    try:
        done, _ = await asyncio.wait({operation}, timeout=0.25)
        completed_without_probe = bool(done) and not probes
    finally:
        release.set()
        outcome = await asyncio.gather(operation, return_exceptions=True)
        active = manager._operation_task
        if active is not None:
            await asyncio.gather(active, return_exceptions=True)
    assert completed_without_probe, "ordinary status entered a blocking key probe during login"
    assert outcome == [False]
    assert manager.session.phase == phase
    assert backend.chatlog_pid == 5303
    assert "chatlog_stop" not in backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("pipe_fails", [False, True])
async def test_user_agent_shutdown_cleans_owned_helper_even_if_pipe_shutdown_fails(pipe_fails):
    backend = FakeWeChat(chatlog_pid=5304)
    manager = WeChatSession(backend, WechatConfig(enabled=True))
    close = AsyncMock()
    link_stop = AsyncMock(side_effect=OSError("synthetic pipe failure") if pipe_fails else None)
    agent = SimpleNamespace(link=SimpleNamespace(stop=link_stop), _wsl_task=None,
                            wslctl=SimpleNamespace(close=close), wechat=manager)
    try:
        await UserAgent.stop(agent)
    except OSError:
        assert pipe_fails
    assert backend.chatlog_pid is None, "UserAgent exit left its owned helper running"
    assert backend.calls == ["chatlog_stop"]
    close.assert_awaited_once()


@pytest.mark.parametrize("denied_field", ["ppid", "cmdline"])
def test_inaccessible_wechat_candidate_does_not_become_a_false_unique_target(monkeypatch, denied_field):
    class ProcessError(Exception):
        pass

    class AccessDenied(ProcessError):
        pass

    psutil = SimpleNamespace(Error=ProcessError, AccessDenied=AccessDenied, NoSuchProcess=ProcessError)

    class Candidate:
        def __init__(self, pid):
            self.pid = pid
            self.info = {"pid": pid, "name": "Weixin.exe"}

        def ppid(self):
            if self.pid == 100 and denied_field == "ppid":
                raise psutil.AccessDenied(self.pid)
            return 1

        def cmdline(self):
            if self.pid == 100 and denied_field == "cmdline":
                raise psutil.AccessDenied(self.pid)
            return ["D:/Apps/Weixin.exe"]

        def name(self):
            return "Weixin.exe"

        def exe(self):
            return "D:/Apps/Weixin.exe"

        def create_time(self):
            return float(self.pid)

    candidates = [Candidate(100), Candidate(200)]
    psutil.process_iter = lambda *args, **kwargs: iter(candidates)
    psutil.Process = lambda pid: next(p for p in candidates if p.pid == pid)
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    backend = WinWeChat(exe_path="D:/Apps/Weixin.exe")
    monkeypatch.setattr(backend, "main_window", lambda: {})
    monkeypatch.setattr(backend, "_account_dir", lambda: None)
    with pytest.raises(WaError) as caught:
        backend._key_target()
    assert caught.value.reason in {"wechat_pid_access_denied", "wechat_pid_ambiguous"}


@pytest.fixture
def isolated_server(monkeypatch, tmp_path):
    import qtrade_winagent.win.wechat as native

    class ProcessError(Exception):
        pass

    class MissingProcess(ProcessError):
        pass

    class AccessDenied(ProcessError):
        pass

    exe = tmp_path / "tools" / "chatlog.exe"
    work = tmp_path / "work"
    work.mkdir()
    backend = WinWeChat(chatlog_dir=str(exe.parent), chatlog_work_dir=str(work))
    calls = []

    class OldProcess:
        def create_time(self):
            return 123.0

        def exe(self):
            return str(exe)

        def cmdline(self):
            return [str(exe), "server"]

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    process = OldProcess()
    psutil = SimpleNamespace(Error=ProcessError, NoSuchProcess=MissingProcess,
                             AccessDenied=AccessDenied, Process=lambda pid: process)
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(native, "require_windows", lambda *_: None)
    monkeypatch.setattr(backend, "_port_in_use", lambda: False, raising=False)
    monkeypatch.setattr(backend, "_prepare_server", lambda: str(exe))
    spawn = AsyncMock(return_value=SimpleNamespace(pid=5404))
    monkeypatch.setattr(backend, "_spawn_owned", spawn)
    row = {"state": "active", "operation_id": "previous-operation", "pid": 5403,
           "created": 123.0, "exe": str(exe), "mode": "server", "work_dir": str(work)}
    return SimpleNamespace(backend=backend, psutil=psutil, process=process, calls=calls,
                           spawn=spawn, row=row, record=work / "chatlog-process.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["alive", "inaccessible", "identity_mismatch", "pending"])
async def test_recreated_backend_refuses_an_unresolved_previous_helper(isolated_server, previous):
    f = isolated_server
    row = dict(f.row)
    if previous == "inaccessible":
        def denied(_pid):
            raise f.psutil.AccessDenied("synthetic protected process")
        f.psutil.Process = denied
    elif previous == "identity_mismatch":
        f.process.exe = lambda: "unrelated.exe"
    elif previous == "pending":
        row = {k: v for k, v in row.items() if k not in {"pid", "created"}}
        row["state"] = "pending"
    original = json.dumps(row)
    f.record.write_text(original)
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason in {"wechat_previous_process_running", "wechat_previous_process_unverified"}
    f.spawn.assert_not_awaited()
    assert f.calls == []
    assert f.record.read_text() == original


@pytest.mark.asyncio
async def test_unknown_port_owner_is_not_terminated_or_replaced(isolated_server, monkeypatch):
    f = isolated_server
    monkeypatch.setattr(f.backend, "_port_in_use", lambda: True, raising=False)
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason == "wechat_chatlog_port_in_use"
    f.spawn.assert_not_awaited()
    assert f.calls == []
    assert not f.record.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["exited", "pid_reused"])
async def test_proven_stale_record_allows_start_without_touching_reused_pid(isolated_server, previous):
    f = isolated_server
    f.record.write_text(json.dumps(f.row))
    if previous == "exited":
        def gone(_pid):
            raise f.psutil.NoSuchProcess("synthetic exited process")
        f.psutil.Process = gone
    else:
        f.process.create_time = lambda: 999.0
    assert await f.backend.chatlog_serve() == 5404
    f.spawn.assert_awaited_once()
    assert f.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_result", [13, 101, 10060, OSError("synthetic access error"), TimeoutError("synthetic timeout")])
async def test_uncertain_port_probe_never_claims_the_port_is_free(isolated_server, monkeypatch, connect_result):
    import qtrade_winagent.win.wechat as native
    import socket

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def settimeout(self, _seconds):
            pass

        def connect_ex(self, target):
            assert target == ("127.0.0.1", 5030)
            if isinstance(connect_result, BaseException):
                raise connect_result
            return connect_result

    f = isolated_server
    monkeypatch.setattr(native, "socket", SimpleNamespace(socket=lambda *_: Probe(),
                                                        AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM))
    monkeypatch.setattr(f.backend, "_port_in_use", WinWeChat._port_in_use.__get__(f.backend))
    with pytest.raises(WaError):
        await f.backend.chatlog_serve()
    f.spawn.assert_not_awaited()
    assert not f.record.exists()


@pytest.mark.parametrize("connect_result", [errno.ECONNREFUSED, 10061])
def test_definite_connection_refusal_means_the_port_is_available(monkeypatch, connect_result):
    import qtrade_winagent.win.wechat as native
    import socket

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def settimeout(self, _seconds):
            pass

        def connect_ex(self, target):
            assert target == ("127.0.0.1", 5030)
            return connect_result

    monkeypatch.setattr(native, "socket", SimpleNamespace(socket=lambda *_: Probe(),
                                                        AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM))
    assert WinWeChat()._port_in_use() is False
