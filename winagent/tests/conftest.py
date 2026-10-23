"""测试夹具:**全假后端**的 WinAgent 服务 + 已握手的会话代理 + ASGI 传输。

硬约束(与 Agent 侧同一口径):容器/开发机里**绝不碰真 DPAPI、真防火墙、真 powercfg、真 .wslconfig、真注册表、真微信**——
所有后端一律注入 ``qtrade_winagent.fakes`` 里的假实现,因此这套测试在 Linux 上可全量跑。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pytest

from qtrade_winagent import __version__
from qtrade_winagent.alerts import AlertBuffer
from qtrade_winagent.audit import Audit
from qtrade_winagent.config import WinAgentConfig
from qtrade_winagent.db import Db
from qtrade_winagent.fakes import (FakeCrypto, FakeFirewall, FakeHosts, FakeNet, FakePipeBackend, FakePower,
                                   FakeProbe, FakeSys, FakeWeChat, FakeWsl)
from qtrade_winagent.installer_ops import InstallerOps
from qtrade_winagent.monitor import DiskTarget, Monitor
from qtrade_winagent.netprobe import NetProbe
from qtrade_winagent.pipe import PipeHub
from qtrade_winagent.power import Power
from qtrade_winagent.svc import SvcDeps, Tokens, build_app
from qtrade_winagent.user import UserAgent
from qtrade_winagent.vault import Vault
from qtrade_winagent.wechat import WeChatHostsBlock, WeChatStore

INSTALL_SID = "S-1-5-21-1111-2222-3333-1001"
OTHER_SID = "S-1-5-21-1111-2222-3333-2002"
AGENT_TOKEN = "agent-token-for-tests"
CONSOLE_TOKEN = "console-token-for-tests"
INSTALLER_TOKEN = "installer-token-for-tests"
SVC_EXE = "C:\\ProgramData\\QTrade\\winagent\\qtrade-winagent-svc.exe"


class Clock:
    """可拨时钟(与 Agent 侧 ``tests/conftest.py`` 的 ``Clock`` 同语义)。"""

    def __init__(self, start_ms: int = 1_758_240_000_000, auto_step_ms: int = 0):
        self.now_ms = start_ms
        self.auto_step_ms = auto_step_ms

    def __call__(self) -> int:
        self.now_ms += self.auto_step_ms
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture
def clock() -> Clock:
    return Clock()


@dataclass
class Rig:
    """一整套装好的 WinAgent(服务 + 可选会话代理)。测试直接拨 ``rig.sys.locked = True`` 这类开关演分支。"""
    deps: SvcDeps
    app: Any
    clock: Clock
    db: Db
    crypto: FakeCrypto
    sys: FakeSys
    net: FakeNet
    firewall: FakeFirewall
    power_backend: FakePower
    probe: FakeProbe
    pipe: FakePipeBackend
    hosts: FakeHosts
    wsl: FakeWsl
    wechat: FakeWeChat
    vault_root: str
    user_agent: Optional[UserAgent] = None
    _tasks: list[asyncio.Task] = None            # type: ignore[assignment]


def build_rig(tmp_path, *, clock: Optional[Clock] = None, cfg: Optional[WinAgentConfig] = None,
              wechat_enabled: bool = True) -> Rig:
    clk = clock or Clock(auto_step_ms=1)
    cfg = cfg or WinAgentConfig()
    if wechat_enabled:
        cfg = cfg.with_wechat(enabled=True)
    db = Db(str(tmp_path / "winagent.db"), clock=clk).open()
    audit = Audit(db, clock=clk)
    alerts = AlertBuffer(cfg.alert, clock=clk)
    crypto, sysb, netb, fwb = FakeCrypto(), FakeSys(), FakeNet(), FakeFirewall()
    pwrb, probeb, pipeb, hostsb = FakePower(), FakeProbe(), FakePipeBackend(), FakeHosts()
    wslb, wxb = FakeWsl(), FakeWeChat()
    vault_root = str(tmp_path / "vault")
    vault = Vault(db, crypto, cfg.vault, root=vault_root, audit=audit, clock=clk)
    disks = [DiskTarget("programdata", "C:\\ProgramData\\QTrade"),
             DiskTarget("vhdx", "C:\\vhdx"),
             DiskTarget("wechat_data", "D:\\", product_level=False)]
    monitor = Monitor(db, sysb, alerts, cfg.monitor, probe=probeb, disks=disks, clock=clk)
    netprobe = NetProbe(db, netb, fwb, probeb, cfg.net, cfg.probe, alerts, svc_exe=SVC_EXE, port=cfg.api.port, clock=clk)
    hub = PipeHub(pipeb, cfg.ipc, pipe_name=cfg.api.pipe_user, install_user_sid=INSTALL_SID, version=__version__,
                  alerts=alerts, audit=audit, clock=clk)
    installer = InstallerOps(db, kernel_dir=str(tmp_path / "kernel"), wsl_backup_dir=str(tmp_path / "wslbak"),
                             package_version=__version__, crypto=crypto, audit=audit, hub=hub, clock=clk)
    deps = SvcDeps(cfg=cfg, db=db, audit=audit, vault=vault, monitor=monitor, netprobe=netprobe,
                   power=Power(db, pwrb, sysb, cfg.wechat, audit=audit, clock=clk), hub=hub,
                   wechat_store=WeChatStore(db, clock=clk),
                   hosts_block=WeChatHostsBlock(hostsb, cfg.wechat, cfg.probe, clock=clk),
                   installer=installer, alerts=alerts,
                   tokens=Tokens(agent=AGENT_TOKEN, console=CONSOLE_TOKEN, installer=INSTALLER_TOKEN),
                   clock=clk, started_ms=clk())
    app = build_app(deps)
    return Rig(deps=deps, app=app, clock=clk, db=db, crypto=crypto, sys=sysb, net=netb, firewall=fwb,
               power_backend=pwrb, probe=probeb, pipe=pipeb, hosts=hostsb, wsl=wslb, wechat=wxb,
               vault_root=vault_root, _tasks=[])


async def attach_user_agent(rig: Rig, *, session_id: str = "Console", user_sid: str = INSTALL_SID,
                            version: Optional[str] = None, wechat: bool = True) -> UserAgent:
    """把一个会话代理接到服务的管道上并跑起来(握手 → 心跳 → 等请求)。"""
    await rig.deps.hub.start()
    ua = UserAgent(rig.deps.cfg, pipe=rig.pipe, wsl=rig.wsl, wechat=rig.wechat if wechat else None,
                   power=rig.power_backend, session_id=session_id, user_sid=user_sid,
                   version=version or __version__, backup_dir=rig.deps.installer._wsl_backup_dir,   # noqa: SLF001
                   clock=rig.clock)
    await ua.start()
    rig._tasks.append(asyncio.create_task(ua.run()))
    await asyncio.sleep(0)
    rig.user_agent = ua
    return ua


def client(rig: Rig, *, token: Optional[str] = AGENT_TOKEN, host: str = "127.0.0.1") -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=rig.app, client=(host, 12345)),
                             base_url="http://winagent.test", headers=headers)


@pytest.fixture
async def rig(tmp_path):
    r = build_rig(tmp_path)
    yield r
    for t in r._tasks:
        t.cancel()
    await r.deps.hub.stop()
    r.db.close()


@pytest.fixture
async def rig_with_user(tmp_path):
    r = build_rig(tmp_path)
    await attach_user_agent(r)
    yield r
    for t in r._tasks:
        t.cancel()
    await r.deps.hub.stop()
    r.db.close()


# ---------------------------------------------------------------------- Agent 侧客户端的传输适配
def agent_transport(rig: Rig, *, client_host: str = "127.0.0.1"):
    """把 Agent 侧 ``winagent_client.Transport`` 的签名接到本服务的 ASGI 上。

    签名必须与 ``qtrade_agent.winagent_client.Transport`` **逐字一致**:
    ``(method, url, headers, body, timeout_s) -> (status, headers, body_bytes)``。
    """
    transport = httpx.ASGITransport(app=rig.app, client=(client_host, 12345))

    async def go(method: str, url: str, headers: dict[str, str], body: Optional[bytes],
                 timeout_s: float) -> tuple[int, dict[str, str], bytes]:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as c:
            resp = await c.request(method, url, headers=headers, content=body)
            return resp.status_code, dict(resp.headers), resp.content
    return go
