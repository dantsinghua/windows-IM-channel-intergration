"""第五批接线的公共夹具(本文件不含用例,只被 ``test_integration_wiring_*.py`` import)。

一个 ``AgentApp`` 把四路新模块全装上,**全部后端都是假件**:``FakeContainers``/``FakeAdb``/``FakeVault``/
``FakeWeChatWinAgent``(它把非微信路径委托给 ``FakeWinAgent``)/``FakeOneBot``/``FakeHttp``/``FakeDisk``/
``FakeImap``·``FakePop3``·``FakeSmtp``。**绝不碰真 docker / adb / WinAgent / 邮箱 / 出网**(SKILL §4 禁区)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from starlette.testclient import TestClient

from qtrade_agent.adapters.qq import FakeOneBot
from qtrade_agent.adapters.wechat import FakeWeChatWinAgent
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig
from qtrade_agent.maintenance import BackupConfig, FakeDisk
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs
from qtrade_agent.vault_client import FakeVault
from qtrade_agent.webhook import FakeHttp
from tests.conftest import Clock

TOKEN_ADMIN = "wiring-admin-token"
TOKEN_READ = "wiring-read-token"
TOKEN_WRITE = "wiring-write-token"


@dataclass
class WireRig:
    agent: AgentApp
    client: TestClient
    clock: Clock
    store: Any
    wechat: FakeWeChatWinAgent
    http: FakeHttp
    disk: FakeDisk
    bots: dict[str, FakeOneBot] = field(default_factory=dict)

    def events_of(self, name: str) -> list[dict[str, Any]]:
        return self.store.list_events(event=name)

    def outbox(self, target_prefix: str = "webhook:") -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.con.execute(
            "SELECT * FROM events_outbox WHERE target LIKE ? ORDER BY seq", (target_prefix + "%",))]


def H(token: str = TOKEN_ADMIN, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def make_agent(tmp_path, *, cfg: Optional[AgentConfig] = None, clock: Optional[Clock] = None,
               free_mb: int = 100_000, qq_online: bool = True, qq_self_id: int = 415011447) -> tuple[AgentApp, dict[str, Any]]:
    """建一个全假后端的 ``AgentApp`` 并 ``open()``;返回 ``(agent, 假件字典)``。"""
    clock = clock or Clock(auto_step_ms=50)
    # 备份目录必须落在 tmp:缺省 `/var/lib/qtrade/backup` 在开发容器里没有写权限,也不该去碰
    cfg = cfg or AgentConfig(backup=BackupConfig(backup_dir=str(tmp_path / "backup")))
    wechat = FakeWeChatWinAgent()
    http = FakeHttp()
    disk = FakeDisk(free=free_mb)
    bots: dict[str, FakeOneBot] = {}

    def qq_transport(acct):
        bot = bots.get(acct.id)
        if bot is None:
            bot = bots[acct.id] = FakeOneBot(self_id=qq_self_id, clock=clock, online=qq_online)
        return bot

    agent = AgentApp(cfg, db_path=str(tmp_path / "agent.db"), clock=clock,
                     containers=FakeContainers(), adb=FakeAdb(), vault=FakeVault(),
                     winagent_transport=wechat, winagent_base_url="http://winagent.fake:17610",
                     winagent_token=wechat.token, fs=FakeFs(), wsl_total_mb=11264, boot_poll_s=0,
                     qq_transport_factory=qq_transport, http=http, disk=disk, data_dir=str(tmp_path)).open()
    agent.pool.set_windows(total_mb=16384, wechat_enabled=True, known=True)
    agent.health.set_winagent(True, version=wechat.version, user_agent=True)
    return agent, {"clock": clock, "wechat": wechat, "http": http, "disk": disk, "bots": bots}


def make_rig(tmp_path, **kw) -> WireRig:
    """``make_agent`` + 三个令牌 + ``TestClient``(调用方负责 ``close()``)。"""
    agent, fakes = make_agent(tmp_path, **kw)
    st = agent.store
    st.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOKEN_ADMIN)
    st.upsert_api_client(app_id="reader", name="只读", level="read", token=TOKEN_READ)
    st.upsert_api_client(app_id="writer", name="可写", level="write", token=TOKEN_WRITE)
    client = TestClient(agent.create_api(), client=("127.0.0.1", 40000))
    client.__enter__()
    return WireRig(agent=agent, client=client, clock=fakes["clock"], store=st, wechat=fakes["wechat"],
                   http=fakes["http"], disk=fakes["disk"], bots=fakes["bots"])


def close_rig(rig: WireRig) -> None:
    rig.client.__exit__(None, None, None)
    rig.store.close()
