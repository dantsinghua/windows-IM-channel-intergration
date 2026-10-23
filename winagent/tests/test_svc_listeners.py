"""WinAgent HTTP 监听(评审 B2):

- Windows 上绑定用 ``SO_EXCLUSIVEADDRUSE``、不用 ``SO_REUSEADDR``(后者允许他进程抢绑同端口);
- vEthernet (WSL) 地址启动时不存在 ⇒ 跳过 + warn,服务照常起;回环绑不上才算失败;
- 运行期期望集合变化 ⇒ 真的补绑 / 解绑(挂到正在跑的 uvicorn 上),H16 只报一次、对齐后解除。
"""
from __future__ import annotations

import asyncio
import errno
import socket
import types

import pytest

from qtrade_winagent.main_svc import LOOPBACK, Listeners, align_listen, bind_listener


class FakeSock:
    def __init__(self, *_a, fail_bind: bool = False):
        self.opts: list[tuple[int, int, int]] = []
        self.closed = False
        self.fail_bind = fail_bind

    def setsockopt(self, level, opt, val):
        self.opts.append((level, opt, val))

    def bind(self, addr):
        if self.fail_bind:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        self.addr = addr

    def setblocking(self, _f):
        pass

    def listen(self, _n):
        pass

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- bind_listener
def test_windows_bind_uses_exclusiveaddruse_not_reuseaddr():
    made: list[FakeSock] = []
    bind_listener("127.0.0.1", 17610, windows=True, sock_factory=lambda *a: made.append(FakeSock()) or made[-1])
    opts = [o for _lvl, o, _v in made[0].opts]
    excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)          # winsock: (int)(~SO_REUSEADDR),Windows SO_REUSEADDR=4
    assert opts == [excl] and socket.SO_REUSEADDR not in opts and 4 not in opts


def test_bind_failure_closes_socket_and_raises():
    made: list[FakeSock] = []
    with pytest.raises(OSError):
        bind_listener("172.23.16.1", 17610, windows=True,
                      sock_factory=lambda *a: made.append(FakeSock(fail_bind=True)) or made[-1])
    assert made[0].closed


def test_posix_bind_uses_reuseaddr_only():
    # 只断言分支选择、不依赖宿主 OS 语义:Windows 的 SO_REUSEADDR 允许重复绑定,真绑在 Windows 上不成立
    made: list[FakeSock] = []
    bind_listener("127.0.0.1", 17610, windows=False, sock_factory=lambda *a: made.append(FakeSock()) or made[-1])
    assert made[0].opts == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


def test_native_bind_does_not_allow_second_active_listener():
    # 走本机默认分支(Windows=SO_EXCLUSIVEADDRUSE / POSIX=SO_REUSEADDR),两种语义下第二个活跃监听都应绑不上
    a = bind_listener("127.0.0.1", 0)
    try:
        port = a.getsockname()[1]
        with pytest.raises(OSError):
            bind_listener("127.0.0.1", port)
    finally:
        a.close()


# ---------------------------------------------------------------- 启动期
def _flaky(fail: set[str]):
    """``fail`` 里的地址绑定抛 EADDRNOTAVAIL(= vEthernet 还不存在);其余返回 FakeSock。"""
    def bind(host, port):
        if host in fail:
            raise OSError(errno.EADDRNOTAVAIL, f"{host} 不存在")
        return FakeSock()
    return bind


def test_open_skips_missing_wsl_address_but_keeps_loopback():
    lst = Listeners(17610, bind=_flaky({"172.23.16.1"}))
    assert lst.open((LOOPBACK, "172.23.16.1")) == (LOOPBACK,)


def test_open_raises_when_loopback_cannot_bind():
    lst = Listeners(17610, bind=_flaky({LOOPBACK}))
    with pytest.raises(OSError):
        lst.open((LOOPBACK, "172.23.16.1"))


# ---------------------------------------------------------------- 运行期补绑 / 解绑
class _Proto(asyncio.Protocol):
    def __init__(self, **_kw):
        pass


def _fake_uvicorn(started: bool = True):
    cfg = types.SimpleNamespace(http_protocol_class=_Proto, backlog=16)
    return types.SimpleNamespace(config=cfg, server_state=object(), lifespan=types.SimpleNamespace(state={}),
                                 started=started, servers=[])


async def _can_connect(host: str, port: int) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    return True


async def test_sync_rebinds_missing_address_then_unbinds_stale_one():
    wsl = "127.0.0.2"                                          # Linux 上整个 127/8 都是回环,拿它演 vEthernet 地址
    missing = {wsl}

    def bind(host, port):
        if host in missing:
            raise OSError(errno.EADDRNOTAVAIL, "还不存在")
        return bind_listener(host, port, windows=False)

    probe = bind_listener(LOOPBACK, 0, windows=False)
    port = probe.getsockname()[1]
    probe.close()
    lst = Listeners(port, bind=bind)
    assert lst.open((LOOPBACK, wsl)) == (LOOPBACK,)            # 启动时 WSL 地址不存在 ⇒ 跳过
    lst.server = _fake_uvicorn()
    assert await lst.sync((LOOPBACK, wsl)) == (LOOPBACK,)      # 仍不存在 ⇒ 这轮补不上,不崩
    missing.clear()                                            # WSL 起来了
    assert set(await lst.sync((LOOPBACK, wsl))) == {LOOPBACK, wsl}
    assert len(lst.server.servers) == 1 and await _can_connect(wsl, port)
    assert await lst.sync((LOOPBACK,)) == (LOOPBACK,)          # 子网变了 ⇒ 旧地址解绑
    assert lst.server.servers == [] and not await _can_connect(wsl, port)
    for s in lst.sockets.values():
        s.close()


async def test_sync_does_nothing_before_uvicorn_started():
    lst = Listeners(17610, bind=_flaky(set()))
    lst.open((LOOPBACK, "172.23.16.1"))
    lst.server = _fake_uvicorn(started=False)
    assert set(await lst.sync((LOOPBACK,))) == {LOOPBACK, "172.23.16.1"}      # 未启动:不解绑也不补绑


# ---------------------------------------------------------------- H16 一段
class _NetProbe:
    def __init__(self, want):
        self.want = want
        self.reconciled: list[tuple[str, ...]] = []

    def desired_listen(self):
        return self.want

    def reconcile_listen(self, *, actual):
        self.reconciled.append(tuple(actual))
        return {"ok": set(actual) == set(self.want)}


async def test_align_reports_once_retries_silently_and_resolves_after_rebind():
    wsl = "172.23.16.1"
    fail = {wsl}
    lst = Listeners(17610, bind=_flaky(fail))
    lst.open((LOOPBACK, wsl))
    lst.server = _fake_uvicorn()

    async def attach(_sock):
        return None
    lst._attach = attach                                       # type: ignore[method-assign]  # 不真挂 uvicorn
    np_ = _NetProbe((LOOPBACK, wsl))
    d = types.SimpleNamespace(netprobe=np_, listen=lst.actual())
    reported = await align_listen(d, lst, None)
    assert np_.reconciled == [(LOOPBACK,)] and d.listen == (LOOPBACK,)          # 报一次 H16
    reported = await align_listen(d, lst, reported)
    assert np_.reconciled == [(LOOPBACK,)]                                        # 同一期望:静默重试,不重复告警
    fail.clear()
    reported = await align_listen(d, lst, reported)
    assert set(d.listen) == {LOOPBACK, wsl} and reported is None
    assert set(np_.reconciled[-1]) == {LOOPBACK, wsl}                             # 对齐后再调一次 ⇒ 解除 H16


async def test_align_without_listeners_keeps_dev_semantics():
    np_ = _NetProbe((LOOPBACK, "172.23.16.1"))
    d = types.SimpleNamespace(netprobe=np_, listen=(LOOPBACK,))
    await align_listen(d, None, None)
    assert d.listen == (LOOPBACK, "172.23.16.1") and np_.reconciled == [(LOOPBACK,)]
