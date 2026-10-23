"""会话代理侧 ``UserAgentLink`` 的生命周期(评审 C 节 winagent ``pipe.py``):

- ``stop()`` 必须让 ``run()`` 退出(原实现只取消恒空的 ``_tasks``、不置 ``conn=None`` ⇒ ``run()`` 永不停);
- 入站探测抛错(真后端 ``PeekNamedPipe`` 在句柄断开时抛 ``pywintypes.error``)按断开处理,``run()`` 正常返回;
- ``_dispatch`` 任务要留引用(在途可见、``stop()`` 能取消);
- 真后端 ``WinPipeConn.inbound_ready`` 在 Peek 抛错时走「recv 回 None」的断开路径。
"""
from __future__ import annotations

import asyncio
import sys
import types

from qtrade_winagent import __version__
from qtrade_winagent.config import IpcConfig
from qtrade_winagent.db import Db
from qtrade_winagent.fakes import FakePipeBackend
from qtrade_winagent.pipe import PipeHub, UserAgentLink

from tests.conftest import INSTALL_SID

PIPE = r"\\.\pipe\qtrade-winagent-user"


async def _pair():
    db = Db(":memory:").open()
    be = FakePipeBackend()
    hub = PipeHub(be, IpcConfig(), pipe_name=PIPE, install_user_sid=INSTALL_SID, version=__version__)
    await hub.start()
    link = UserAgentLink(be, IpcConfig(), pipe_name=PIPE, session_id="Console", user_sid=INSTALL_SID,
                         version=__version__, pid=1, modules=("wslctl",))
    await link.connect()
    return db, hub, link


async def test_stop_makes_run_return():
    db, hub, link = await _pair()
    t = asyncio.create_task(link.run())
    await asyncio.sleep(0.1)
    assert not t.done()
    await link.stop()
    await asyncio.wait_for(t, timeout=2)                     # 原实现这里永远等不到
    assert link.conn is None
    await hub.stop()
    db.close()


async def test_stop_cancels_in_flight_dispatch_and_keeps_reference():
    db, hub, link = await _pair()
    started = asyncio.Event()

    async def slow(_p):
        started.set()
        await asyncio.sleep(60)

    link.on("wsl.slow", slow)
    run = asyncio.create_task(link.run())
    call = asyncio.create_task(hub.call("wsl.slow", {}, timeout_s=30))
    await asyncio.wait_for(started.wait(), timeout=2)
    inflight = list(link._tasks)                              # noqa: SLF001 —— 断言在途任务被持有
    assert len(inflight) == 1 and not inflight[0].done()
    await link.stop()
    await asyncio.wait_for(run, timeout=2)
    await asyncio.sleep(0)
    assert inflight[0].cancelled() and not link._tasks          # noqa: SLF001
    call.cancel()
    await hub.stop()
    db.close()


async def test_finished_dispatch_is_released():
    db, hub, link = await _pair()

    async def echo(p):
        return p

    link.on("wsl.echo", echo)
    run = asyncio.create_task(link.run())
    assert await hub.call("wsl.echo", {"a": 1}, timeout_s=5) == {"a": 1}
    await asyncio.sleep(0)
    assert not link._tasks                                    # noqa: SLF001 —— 跑完即从集合里移除
    await link.stop()
    await asyncio.wait_for(run, timeout=2)
    await hub.stop()
    db.close()


async def test_inbound_probe_error_is_treated_as_disconnect():
    db, hub, link = await _pair()

    def boom() -> bool:
        raise OSError(109, "管道已结束")                      # 真机上是 pywintypes.error(ERROR_BROKEN_PIPE)

    link.conn.inbound_ready = boom                           # type: ignore[method-assign]
    await asyncio.wait_for(link.run(), timeout=2)            # 正常返回,不把异常冒出 run()
    await link.stop()
    await hub.stop()
    db.close()


async def test_peer_close_makes_run_return():
    db, hub, link = await _pair()
    run = asyncio.create_task(link.run())
    await asyncio.sleep(0.1)
    await hub.stop()                                          # 服务端关连接 ⇒ 客户端 recv 回 None
    await asyncio.wait_for(run, timeout=2)
    await link.stop()
    db.close()


async def test_win_pipe_conn_peek_error_goes_down_recv_none_path(monkeypatch):
    """真后端:``PeekNamedPipe`` 抛错 ⇒ ``inbound_ready`` 回 True、随后 ``recv`` 回 None(不碰 ReadFile)。"""
    from qtrade_winagent.win.pipes import WinPipeConn

    class PipeError(Exception):                               # 代替 pywintypes.error
        pass

    def peek(_h, _n):
        raise PipeError(109, "PeekNamedPipe", "管道已结束")

    def read(_h, _n):
        raise AssertionError("句柄已断后不该再 ReadFile")

    monkeypatch.setitem(sys.modules, "win32pipe", types.SimpleNamespace(PeekNamedPipe=peek))
    monkeypatch.setitem(sys.modules, "win32file", types.SimpleNamespace(ReadFile=read))
    conn = WinPipeConn(object(), peer_sid=None)
    assert conn.inbound_ready() is True
    assert await conn.recv() is None
