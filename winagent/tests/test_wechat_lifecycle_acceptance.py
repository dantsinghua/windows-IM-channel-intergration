"""Independent R6-106 lifecycle acceptance; all OS and process inputs are fake."""
from __future__ import annotations

import asyncio
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
                             AccessDenied=AccessDenied, Process=lambda pid: process,
                             CONN_LISTEN="LISTEN")
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(native, "require_windows", lambda *_: None)
    monkeypatch.setattr(backend, "_prepare_server", lambda: str(exe))

    async def spawn_owned(_args, _log_path):
        proc = SimpleNamespace(pid=5404, returncode=None)
        backend._chatlog_process = proc
        backend._chatlog_mode = "server"
        backend._chatlog_created = 123.0
        return proc

    spawn = AsyncMock(side_effect=spawn_owned)
    monkeypatch.setattr(backend, "_spawn_owned", spawn)

    def listener_state():
        proc = backend._chatlog_process
        if proc is not None and proc.returncode is None:
            return "occupied", proc.pid
        return "free", None

    monkeypatch.setattr(backend, "_server_listener_state", listener_state)
    monkeypatch.setattr(backend, "_chatlog_server_healthy", lambda _timeout: True)
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
async def test_occupied_read_port_blocks_server_start(isolated_server, monkeypatch):
    f = isolated_server
    monkeypatch.setattr(f.backend, "_server_listener_state", lambda: ("occupied", 9999))
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason == "wechat_chatlog_port_in_use"
    f.spawn.assert_not_awaited()
    assert f.calls == []
    assert not f.record.exists()


def test_status_read_survives_a_still_running_previous_helper(isolated_server, monkeypatch):
    f = isolated_server
    f.record.write_text(json.dumps(f.row))
    monkeypatch.setattr(f.backend, "locate", lambda: {"installed": True, "version": "4.1.12.26"})
    monkeypatch.setattr(f.backend, "main_window", lambda: {"exists": False})
    monkeypatch.setattr(f.backend, "key_state", lambda: {"ok": False})
    monkeypatch.setattr(f.backend, "current_wxid", lambda: None)
    monkeypatch.setattr(f.backend, "narrator_running", lambda: False)
    monkeypatch.setattr(f.backend, "ui_tree_visible", lambda: False)
    status = WeChatSession(f.backend, WechatConfig(enabled=True)).status()
    assert status["chatlog"]["running"] is False
    assert status["chatlog"]["port"] == 5030
    assert f.calls == []


def test_key_reservation_does_not_query_listeners(isolated_server):
    f = isolated_server
    queried = []
    f.psutil.net_connections = lambda kind="tcp": queried.append(kind) or []
    f.backend._reserve_start("key")
    assert queried == []
    assert f.record.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["exited", "pid_reused"])
async def test_proven_stale_record_allows_start_without_touching_reused_pid(isolated_server, previous):
    f = isolated_server
    f.record.write_text(json.dumps(f.row))

    def process_for(pid):
        if pid != f.row["pid"]:
            return f.process
        if previous == "exited":
            raise f.psutil.NoSuchProcess("synthetic exited process")

        class Reused:
            def create_time(self):
                return 999.0

            def exe(self):
                return f.process.exe()

            def cmdline(self):
                return f.process.cmdline()

        return Reused()

    f.psutil.Process = process_for
    assert await f.backend.chatlog_serve() == 5404
    f.spawn.assert_awaited_once()
    assert f.calls == []


@pytest.mark.asyncio
async def test_failed_listener_query_never_claims_the_port_is_free(isolated_server, monkeypatch):
    f = isolated_server
    monkeypatch.setattr(f.backend, "_server_listener_state",
                        WinWeChat._server_listener_state.__get__(f.backend))

    def net_connections(_kind):
        raise OSError("synthetic listener query failure")

    f.psutil.net_connections = net_connections
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason == "wechat_chatlog_port_unverified"
    f.spawn.assert_not_awaited()
    assert not f.record.exists()


@pytest.mark.asyncio
async def test_unknown_listener_state_blocks_server_start(isolated_server, monkeypatch):
    f = isolated_server
    monkeypatch.setattr(f.backend, "_server_listener_state", lambda: ("unknown", None))
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason == "wechat_chatlog_port_unverified"
    f.spawn.assert_not_awaited()
    assert not f.record.exists()


@pytest.mark.asyncio
async def test_transient_unknown_during_readiness_retries_until_our_listener_is_healthy(isolated_server, monkeypatch):
    f = isolated_server
    answers = [("free", None), ("unknown", None), ("unknown", None)]

    def listener():
        if answers:
            return answers.pop(0)
        proc = f.backend._chatlog_process
        return "occupied", proc.pid

    monkeypatch.setattr(f.backend, "_server_listener_state", listener)
    assert await f.backend.chatlog_serve() == 5404
    f.spawn.assert_awaited_once()
    assert answers == []


@pytest.mark.asyncio
async def test_persistent_unknown_after_launch_uses_the_remaining_budget(isolated_server, monkeypatch):
    import qtrade_winagent.win.wechat as native

    f = isolated_server
    monkeypatch.setattr(native, "CHATLOG_SERVER_START_TIMEOUT_S", 0.8)
    monkeypatch.setattr(f.backend, "_server_listener_state", lambda: ("unknown", None)
                        if f.backend._chatlog_process is not None else ("free", None))
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason == "wechat_server_owner_unverified"
    f.spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_listener_stolen_after_launch_is_not_our_ready_server(isolated_server, monkeypatch):
    f = isolated_server
    monkeypatch.setattr(f.backend, "_server_listener_state", lambda: ("occupied", 7777)
                        if f.backend._chatlog_process is not None else ("free", None))
    with pytest.raises(WaError) as caught:
        await f.backend.chatlog_serve()
    assert caught.value.reason == "wechat_server_owner_unverified"
    f.spawn.assert_awaited_once()
    assert f.calls == []


class _Addr:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port


class _Row:
    def __init__(self, ip, port, pid, status="LISTEN"):
        self.laddr = _Addr(ip, port)
        self.port = port
        self.pid = pid
        self.status = status


def test_listener_table_covers_ipv4_conflict_and_ipv6_wildcard(monkeypatch):
    import qtrade_winagent.win.wechat as native

    tables = {"tcp": [], "tcp6": []}

    def net_connections(kind):
        if kind not in tables:
            raise AssertionError(kind)
        return list(tables[kind])

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        net_connections=net_connections, CONN_LISTEN="LISTEN"))
    backend = WinWeChat()

    tables["tcp"] = [_Row("192.168.1.8", 5030, 11)]
    assert backend._server_listener_state() == ("free", None)

    tables["tcp"] = [_Row("127.0.0.1", 5030, 22), _Row("10.0.0.8", 1, 22)]
    assert backend._server_listener_state() == ("occupied", 22)

    tables["tcp"] = [_Row("0.0.0.0", 5030, 33)]
    assert backend._server_listener_state() == ("occupied", 33)

    tables["tcp"] = []
    tables["tcp6"] = [_Row("::", 5030, 44)]
    assert backend._server_listener_state() == ("unknown", None)

    tables["tcp6"] = [_Row("::ffff:127.0.0.1", 5030, 55)]
    assert backend._server_listener_state() == ("occupied", 55)

    def broken(kind):
        if kind == "tcp6":
            raise OSError("synthetic ipv6 table failure")
        return []

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        net_connections=broken, CONN_LISTEN="LISTEN"))
    assert backend._server_listener_state() == ("unknown", None)
