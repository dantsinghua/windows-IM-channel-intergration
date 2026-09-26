"""守卫:**假后端 / 真后端的方法签名必须与 ``backends.py`` 的 Protocol 一致**(评审 A1)。

A1 的教训:``PipeHub`` 给 ``serve()`` 新加了 ``allow_sid=`` 实参,只改了真 Windows 后端,
``FakePipeBackend.serve`` 与 ``PipeBackend`` Protocol 都没同步 ⇒ accept 任务 ``TypeError`` 被吞,
4 个测试文件挂死而不是报红。``runtime_checkable`` 的 ``isinstance`` 只查方法**在不在**、不查参数,
所以这里用 ``inspect.signature`` 逐个参数比:

- Protocol 声明的每个参数,实现里都要有,且**同名、同种类**(位置 / 仅关键字);
- 实现多出来的参数必须有默认值(否则按 Protocol 调用会缺实参);
- ``async`` 与否必须一致(同步实现被 ``await`` 会炸,反之拿到的是协程对象)。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from qtrade_winagent import backends as B
from qtrade_winagent import fakes as F
from qtrade_winagent.config import IpcConfig
from qtrade_winagent.pipe import PipeHub
from qtrade_winagent.win import dpapi, firewall, hosts, netinfo, pipes, power, probe, proc, sysinfo, wechat, wsl

PAIRS = [
    (B.CryptoBackend, [F.FakeCrypto, dpapi.WinCrypto]),
    (B.SysBackend, [F.FakeSys, sysinfo.WinSys]),
    (B.NetBackend, [F.FakeNet, netinfo.WinNet]),
    (B.FirewallBackend, [F.FakeFirewall, firewall.WinFirewall]),
    (B.PowerBackend, [F.FakePower, power.WinPower]),
    (B.ProbeBackend, [F.FakeProbe, probe.WinProbe]),
    (B.ProcBackend, [F.FakeProc, proc.WinProc]),
    (B.PipeConn, [F.FakePipeConn, pipes.WinPipeConn]),
    (B.PipeBackend, [F.FakePipeBackend, pipes.WinPipeBackend]),
    (B.WslBackend, [F.FakeWsl, wsl.WinWsl]),
    (B.HostsBackend, [F.FakeHosts, hosts.WinHosts]),
    (B.WeChatBackend, [F.FakeWeChat, wechat.WinWeChat]),
]


def _proto_methods(proto: type) -> dict[str, object]:
    return {n: v for n, v in vars(proto).items()
            if not n.startswith("_") and (inspect.isfunction(v) or isinstance(v, property))}


def _params(fn: object) -> list[inspect.Parameter]:
    return [p for p in inspect.signature(fn).parameters.values() if p.name != "self"]  # type: ignore[arg-type]


CASES = [(proto, impl, name) for proto, impls in PAIRS for impl in impls for name in _proto_methods(proto)]


@pytest.mark.parametrize("proto,impl,name", CASES,
                         ids=[f"{p.__name__}-{i.__name__}-{n}" for p, i, n in CASES])
def test_impl_matches_protocol(proto, impl, name):
    want = _proto_methods(proto)[name]
    got = inspect.getattr_static(impl, name, None)
    if isinstance(want, property):
        assert got is not None, f"{impl.__name__} 缺属性 {name}"
        return
    assert got is not None and callable(got), f"{impl.__name__} 缺方法 {name}"
    assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(want), \
        f"{impl.__name__}.{name} 的 async 与 Protocol 不一致"
    wp = {p.name: p for p in _params(want)}
    gp = {p.name: p for p in _params(got)}
    for n, p in wp.items():
        assert n in gp, f"{impl.__name__}.{name} 缺参数 {n}(Protocol 有)"
        assert gp[n].kind == p.kind, f"{impl.__name__}.{name}({n}) 参数种类 {gp[n].kind} ≠ Protocol {p.kind}"
    for n, p in gp.items():
        if n not in wp and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            assert p.default is not p.empty, f"{impl.__name__}.{name} 多出的参数 {n} 没有默认值"


def test_pipe_backend_serve_takes_allow_sid():
    """A1 原样复现点:三处 ``serve`` 都得接 ``allow_sid=`` 关键字。"""
    for fn in (B.PipeBackend.serve, F.FakePipeBackend.serve, pipes.WinPipeBackend.serve):
        p = inspect.signature(fn).parameters.get("allow_sid")
        assert p is not None and p.kind is p.KEYWORD_ONLY and p.default is None, fn.__qualname__


async def test_hub_passes_install_sid_to_backend_serve():
    """``PipeHub`` 把安装用户 SID 追加进管道 ACL(02 §2.4.1)—— 不许被回退成不传。"""
    be = F.FakePipeBackend()
    hub = PipeHub(be, IpcConfig(), pipe_name="p", install_user_sid="S-1-5-21-9", version="1.0.0")
    await hub.start()
    await asyncio.sleep(0)
    assert be.served_allow_sid == "S-1-5-21-9"
    await hub.stop()
