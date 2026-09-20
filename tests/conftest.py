"""测试夹具:内存 agent.db、假企点主库(按 06 §2.9.5 的表名/列/XOR 编码造)、可拨的时钟。"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

import pytest

from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.store import Store


class Clock:
    def __init__(self, start_ms: int = 1_758_240_000_000, auto_step_ms: int = 0):   # 2025-09-19 前后的一个固定时刻
        self.now_ms = start_ms
        self.auto_step_ms = auto_step_ms      # >0:每次读时钟自动前进(给 bus 的确认窗循环用,否则可拨时钟永不到 deadline)

    def __call__(self) -> int:
        self.now_ms += self.auto_step_ms
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms

    @property
    def now_s(self) -> int:
        return self.now_ms // 1000


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Store:
    s = Store(":memory:", clock=clock).open()
    s.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    s.ensure_account("qq03", "qq", state="running", login_mode="qrcode", self_uid="415011447")
    yield s
    s.close()


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().upper()


def table_name(peer: str, group: bool) -> str:
    return f"mr_{'troop' if group else 'friend'}_{md5_upper(peer)}_New"


@dataclass
class FakeMainDb:
    """假主库文件:``mr_friend_{MD5}_New`` / ``mr_troop_{MD5}_New``,列与真机一致;msgData/senderuin/frienduin 逐字节 XOR。"""
    path: str
    self_uin: str = "3007373675"
    _uniseq: int = field(default=1000, init=False)

    def __post_init__(self):
        con = sqlite3.connect(self.path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS mr_data_line(_id INTEGER PRIMARY KEY)")   # 不是消息表,不该被读
        con.commit()
        con.close()

    def ensure_table(self, peer: str, group: bool) -> str:
        t = table_name(peer, group)
        con = sqlite3.connect(self.path)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{t}" (_id INTEGER PRIMARY KEY AUTOINCREMENT, issend INTEGER, istroop INTEGER, '
                    f'time INTEGER, msgtype INTEGER, uniseq INTEGER, msgseq INTEGER, shmsgseq INTEGER, senderuin BLOB, frienduin BLOB, msgData BLOB)')
        con.commit()
        con.close()
        return t

    def insert(self, peer: str, *, group: bool, time_s: int, msgtype: int, msgdata: bytes, issend: int = 0,
               sender: str | None = None, shmsgseq: int | None = None, uniseq: int | None = None) -> int:
        t = self.ensure_table(peer, group)
        self._uniseq += 1
        u = uniseq if uniseq is not None else self._uniseq
        snd = sender or (self.self_uin if issend else peer)
        con = sqlite3.connect(self.path)
        cur = con.execute(f'INSERT INTO "{t}"(issend, istroop, time, msgtype, uniseq, msgseq, shmsgseq, senderuin, frienduin, msgData) VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (issend, 1 if group else 0, time_s, msgtype, u, u, shmsgseq if shmsgseq is not None else u,
                           xor(snd.encode()), xor(peer.encode()), xor(msgdata)))
        con.commit()
        con.close()
        return int(cur.lastrowid)

    def insert_text(self, peer: str, text: str, *, time_s: int, group: bool = False, issend: int = 0, **kw) -> int:
        return self.insert(peer, group=group, time_s=time_s, msgtype=-1000, msgdata=text.encode("utf-8"), issend=issend, **kw)

    def clear_table(self, peer: str, group: bool = False) -> None:
        con = sqlite3.connect(self.path)
        con.execute(f'DROP TABLE IF EXISTS "{table_name(peer, group)}"')
        con.commit()
        con.close()


@pytest.fixture
def maindb(tmp_path) -> FakeMainDb:
    return FakeMainDb(str(tmp_path / "3007373675.db"))


# ---------------------------------------------------------------------- 第三批:全假后端的 AgentApp(容器里绝不碰真 docker/adb/WinAgent)
@dataclass
class Rig:
    agent: object
    store: object
    clock: Clock
    containers: object
    adb: object
    vault: object
    winagent: object
    fs: object


def make_rig(tmp_path, *, cfg=None, clock: Clock | None = None, wsl_total_mb: int = 11264, login_fn=None, aligner=None, db_name: str = "agent.db"):
    from qtrade_agent.app import AgentApp
    from qtrade_agent.config import AgentConfig
    from qtrade_agent.runtime import FakeAdb, FakeContainers
    from qtrade_agent.runtime.runtime import FakeFs
    from qtrade_agent.vault_client import FakeVault
    from qtrade_agent.winagent_client import FakeWinAgent

    clock = clock or Clock(auto_step_ms=50)
    containers, adb, vault, wa, fs = FakeContainers(), FakeAdb(), FakeVault(), FakeWinAgent(), FakeFs()
    agent = AgentApp(cfg or AgentConfig(), db_path=str(tmp_path / db_name), clock=clock, containers=containers, adb=adb, vault=vault,
                     winagent_transport=wa, winagent_base_url="http://winagent.fake:17610", winagent_token=wa.token, fs=fs, login_fn=login_fn,
                     aligner=aligner, wsl_total_mb=wsl_total_mb, boot_poll_s=0).open()
    return Rig(agent=agent, store=agent.store, clock=clock, containers=containers, adb=adb, vault=vault, winagent=wa, fs=fs)


@pytest.fixture
def rig3(tmp_path) -> Rig:
    r = make_rig(tmp_path)
    yield r
    r.store.close()
