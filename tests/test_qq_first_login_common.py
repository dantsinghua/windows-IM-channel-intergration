"""首登专项共用假设施：不启动服务，不准子进程或真实 socket。"""
from __future__ import annotations

import asyncio
import socket
import struct
import subprocess
import zlib
from types import SimpleNamespace

import pytest

from conftest import Clock
from qtrade_agent.adapters.qq import FakeOneBot
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AccountsConfig, AgentConfig
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs
from qtrade_agent.vault_client import FakeVault
from qtrade_agent.webhook import FakeHttp
from qtrade_agent.winagent_client import FakeWinAgent


def png(seed: int = 1) -> bytes:
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes([0, seed % 256, 20, 30]))) + chunk(b'IEND', b''))


@pytest.fixture
def no_external_io(monkeypatch):
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('real I/O attempted')
        raise AssertionError('QQ test attempted a real subprocess or network connection')
    async def deny_async(*args, **kwargs):
        deny()
    monkeypatch.setattr(subprocess, 'Popen', deny)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', deny_async)
    monkeypatch.setattr(asyncio, 'create_subprocess_shell', deny_async)
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    yield
    assert not attempts, 'Product swallowed a forbidden real I/O attempt'


class QRSource:
    def __init__(self):
        self.calls = []
        self.error = None
        self.value = None
        self.gate = None
        self.entered = asyncio.Event()

    async def qrcode(self, row, *, refresh):
        self.calls.append((row['id'], refresh))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.value or png(len(self.calls))


class TrackedBot(FakeOneBot):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.actions = []
        self.close_calls = 0
        self.received_token = None

    async def connect(self, url, *, access_token=None):
        self.received_token = access_token
        await super().connect(url, access_token=access_token)

    async def close(self):
        self.close_calls += 1
        await super().close()

    def _handle(self, frame):
        self.actions.append(frame['action'])
        return super()._handle(frame)


@pytest.fixture
async def first_login_rig(tmp_path, no_external_io):
    clock = Clock()
    containers, adb, fs, wa, qr = FakeContainers(), FakeAdb(), FakeFs(), FakeWinAgent(), QRSource()
    first = TrackedBot(self_id=123456789, clock=clock, online=False)
    second = TrackedBot(self_id=123456789, clock=clock, online=True)
    bots, issued = [first, second], []
    def transport(acct):
        bot = bots[min(len(issued), len(bots) - 1)]
        issued.append(bot)
        return bot
    cfg = AgentConfig(accounts=AccountsConfig(qq_quick_login_wait_s=0, qr_max_wait_s=120))
    agent = AgentApp(cfg, db_path=str(tmp_path / 'agent.db'), data_dir=str(tmp_path), clock=clock,
                     containers=containers, adb=adb, vault=FakeVault(), fs=fs,
                     winagent_transport=wa, winagent_base_url='http://winagent.fake:17610', winagent_token=wa.token,
                     qq_transport_factory=transport, http=FakeHttp(), wsl_total_mb=11264).open()
    # 能力注入兼容修复前冻结源；旧源忽略它时由业务断言产生 RED。
    agent.accounts._qq_login_backend = qr
    row = await agent.accounts.create({'channel': 'qq', 'label': 'QQ fake fixture',
                                       'identity': {'qq_uin': '123456789'}}, actor='test')
    r = SimpleNamespace(agent=agent, accounts=agent.accounts, store=agent.store, clock=clock,
                        containers=containers, adb=adb, fs=fs, qr=qr, first=first, second=second,
                        issued=issued, id=row['id'], tmp_path=tmp_path)
    try:
        yield r
    finally:
        for task in list(agent.accounts.tasks.values()):
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await agent.adapters['qq'].close()
        agent.store.close()


async def start_waiting(r):
    await r.accounts.start(r.id, actor='test')
    await r.accounts.wait_idle(r.id)
    return r.accounts.prompt(r.id)


def states(r):
    return [e['payload']['state'] for e in r.store.list_events(event='account_state')]
