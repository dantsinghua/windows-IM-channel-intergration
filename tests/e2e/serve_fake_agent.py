"""端到端联调用:以**全假后端**起一个真 HTTP/WS 的 qtrade-agent(只测不改产品代码)。

用法::

    python -m tests.e2e.serve_fake_agent --port 37821 --dir /tmp/qt-e2e

- 后端一律假件:``FakeContainers``/``FakeAdb``/``FakeVault``/``FakeWeChatWinAgent``(委托 ``FakeWinAgent``)/
  ``FakeOneBot``/``FakeHttp``/``FakeDisk``/``FakeFs``,企点读库指向临时假主库文件,发送执行层是假 RPA。
- 令牌三枚(admin/write/read)固定,见下方常量,便于测试脚本直接用。
- 绝不碰真 docker / adb / WinAgent / 8081 / 17600 / 17610。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb          # noqa: E402
from qtrade_agent.adapters.qq import FakeOneBot                            # noqa: E402
from qtrade_agent.adapters.wechat.client import FakeWeChatWinAgent         # noqa: E402
from qtrade_agent.app import AgentApp                                      # noqa: E402
from qtrade_agent.config import (AgentConfig, ApiConfig, BusConfig, QidianAdapterConfig,           # noqa: E402
                                 RuntimeConfig)
from qtrade_agent.maintenance import BackupConfig, FakeDisk                # noqa: E402
from qtrade_agent.runtime import FakeAdb, FakeContainers                   # noqa: E402
from qtrade_agent.runtime.runtime import FakeFs                            # noqa: E402
from qtrade_agent.vault_client import FakeVault                            # noqa: E402
from qtrade_agent.webhook import FakeHttp                                  # noqa: E402

from tests.conftest import FakeMainDb                                      # noqa: E402

TOKEN_ADMIN = "e2e-admin-token"
TOKEN_WRITE = "e2e-write-token"
TOKEN_READ = "e2e-read-token"
TOKEN_LIMITED = "e2e-limited-token"          # write 级、只允许 qd02
SELF_UID = "3007373675"
PEER = "415011447"


def build(dirpath: str):
    os.makedirs(dirpath, exist_ok=True)
    clock = lambda: int(time.time() * 1000)          # noqa: E731  真时钟(真 HTTP 服务)
    maindb = FakeMainDb(os.path.join(dirpath, f"{SELF_UID}.db"))
    cfg = AgentConfig(
        bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0),
        qidian=QidianAdapterConfig(confirm_poll_interval_ms=50),
        api=ApiConfig(bind="127.0.0.1", http_sync_max_wait_ms=25000),
        backup=BackupConfig(backup_dir=os.path.join(dirpath, "backup")),
        runtime=RuntimeConfig(accounts_dir=os.path.join(dirpath, "accounts")),
    )
    sent: list[tuple[str, str]] = []

    async def sender(acct, native_id: str, text: str) -> bool:
        """假 RPA:点发送键 → 2s 后消息落进假主库(模拟企点出向落库滞后,读库合并成 DELIVERED)。"""
        sent.append((native_id, text))

        async def land():
            await asyncio.sleep(2.0)
            maindb.insert_text(native_id, text, time_s=int(time.time()), issend=1)

        asyncio.create_task(land())
        return True

    holder = {}

    async def login_fn(row, account, secret):
        """假登录执行层:任何凭据都算登录成功,并回填 self_uid(企点读库按 uid 找主库文件)。"""
        st = holder.get("store")
        if st is not None:
            st.transition(row["id"], row["state"], state_code=row.get("state_code") or "", self_uid=SELF_UID)
        return "running"

    wechat = FakeWeChatWinAgent()
    bots: dict[str, FakeOneBot] = {}

    def qq_transport(acct):
        bot = bots.get(acct.id)
        if bot is None:
            bot = bots[acct.id] = FakeOneBot(self_id=415011447, clock=clock, online=True)
        return bot

    agent = AgentApp(cfg, db_path=os.path.join(dirpath, "agent.db"), clock=clock, sender=sender,
                     maindb_factory=lambda uid, acct: LocalSqliteMainDb(maindb.path),
                     containers=FakeContainers(), adb=FakeAdb(), vault=FakeVault(),
                     winagent_transport=wechat, winagent_base_url="http://winagent.fake:17610",
                     winagent_token=wechat.token, fs=FakeFs(), wsl_total_mb=11264, boot_poll_s=0,
                     qq_transport_factory=qq_transport, http=FakeHttp(), disk=FakeDisk(free=100_000),
                     login_fn=login_fn, data_dir=dirpath).open()
    holder["store"] = agent.store
    agent.pool.set_windows(total_mb=16384, wechat_enabled=True, known=True)
    agent.health.set_winagent(True, version=wechat.version, user_agent=True)
    st = agent.store
    st.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOKEN_ADMIN)
    st.upsert_api_client(app_id="writer", name="可写", level="write", token=TOKEN_WRITE)
    st.upsert_api_client(app_id="reader", name="只读", level="read", token=TOKEN_READ)
    st.upsert_api_client(app_id="limited", name="限 qd02", level="write", token=TOKEN_LIMITED, allow_accounts=["qd02"])
    # 假主库先放一条历史,便于 poll 建会话映射
    maindb.insert_text(PEER, "历史消息", time_s=int(time.time()) - 3600)
    agent._e2e = {"maindb": maindb, "sent": sent, "wechat": wechat, "bots": bots}     # 给测试脚本留把手
    return agent, cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="serve_fake_agent")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--dir", required=True)
    args = ap.parse_args(argv)
    import logging
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-5s %(name)-22s %(message)s")
    agent, cfg = build(args.dir)
    api = agent.create_api()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        await agent.start()
        try:
            yield
        finally:
            await agent.stop()

    api.router.lifespan_context = lifespan
    import uvicorn
    uvicorn.run(api, host="127.0.0.1", port=args.port, ws="websockets", workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
