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
from fastapi import Request                                                # noqa: E402
from fastapi.responses import JSONResponse                                 # noqa: E402

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


SEED_PATH = "/__e2e/seed"


def mount_seed_api(api, agent) -> None:
    """**仅供端到端翻页用例造数**的测试口子(第三轮独立复测新增;`--seed-api` 显式开启才挂,缺省不挂)。

    为什么要它:C-42 翻页用例要「跨 ≥3 页 + 翻页中途插新行」,而控制台 store 的页大小写死 50,
    ⇒ 每条线要 >100 行。经公开 API 造不出来:建号到第 4~5 个就 `RESOURCE_EXHAUSTED`、收发件没有写入端点。
    所以在**假后端进程里**直接调产品自己的写入函数造数(不改 `src/**` 一个字):

    - ``accounts``:``Store.create_account(now_ms=…)`` 建号后 ``transition(→stopped)``(stopped 不占 wsl 额度,
      后续用例照常能经 API 建号);``created_ms`` 按 ``base_ms - i*step_ms`` 递减,``tie`` 行一组共用同一毫秒
      (专门验 G-16 的 ``(ts_ms, id)`` 双键在排序列撞值时不重不漏)。
    - ``qidian_peers``:往**假企点主库**给 ``n`` 个不同对端各写一条入向消息 → 由产品自己的企点读库 poll 真 ingest
      ⇒ 真建出 ``n`` 条会话 + ``n`` 条消息(不绕过 ingest 路径)。
    - ``inbox`` / ``outbox``:经 ``MailStore.inbox_insert`` / 直插 ``mail_outbox``(状态直接给终态
      ``DONE`` / ``SENT``,不进投递器、**不出网**)。

    鉴权:与产品一致要 admin 令牌(防误调),路径不在 ``/api/v1`` 下(不冒充产品端点)。
    """
    async def seed(request: Request):
        if request.headers.get("authorization") != f"Bearer {TOKEN_ADMIN}":
            return JSONResponse(status_code=401, content={"ok": False, "code": "UNAUTHORIZED"})
        body = await request.json()
        kind, n = body["kind"], int(body.get("n", 1))
        base_ms = int(body.get("base_ms") or int(time.time() * 1000))
        step_ms = int(body.get("step_ms", 1000))
        tie = int(body.get("tie", 1))            # 每 `tie` 行共用同一个时间戳
        tag = str(body.get("tag", "seed"))
        st = agent.store
        out: list = []
        for i in range(n):
            ts = base_ms - (i // tie) * step_ms
            if kind == "accounts":
                row = st.create_account(channel=body.get("channel", "qq"), label=f"{tag}-{i:03d}", login_mode="qrcode",
                                        quota_mb=0, now_ms=ts)
                st.transition(row["id"], "stopped", now_ms=ts)
                out.append(row["id"])
            elif kind == "qidian_peers":
                peer = f"{body.get('peer_prefix', '9100')}{i:05d}"
                agent._e2e["maindb"].insert_text(peer, f"{tag}-{i:03d}", time_s=int(ts / 1000))
                out.append(peer)
            elif kind == "inbox":
                rid = agent.mail.ms.inbox_insert(
                    mailbox="e2e@corp", protocol="imap", folder="INBOX", uid=10_000 + i,
                    rfc_message_id=f"<{tag}-{i}@e2e>", from_addr="ops@corp", subject=f"{tag}-{i:03d}",
                    received_ms=ts, body_sha256=f"{tag}-{i:064d}"[-64:], status="DONE", template="none")
                out.append(rid)
            elif kind == "outbox":
                with st._tx() as c:
                    cur = c.execute(
                        "INSERT INTO mail_outbox(kind, to_addrs, cc_addrs, subject, body_text, attachments_json, rfc_message_id,"
                        " template_version, template_profile, status, attempts, next_attempt_ms, dedup_key, created_ms)"
                        " VALUES ('receipt', 'ops@corp', '', ?, 'x', '[]', ?, '1', 'qtrade-v1', 'SENT', 1, 0, ?, ?)",
                        (f"{tag}-{i:03d}", f"<{tag}-o{i}@e2e>", f"{tag}-o{i}", ts))
                    out.append(int(cur.lastrowid))
            else:
                return JSONResponse(status_code=400, content={"ok": False, "code": "INVALID_ARGS", "kind": kind})
        return JSONResponse(content={"ok": True, "ids": out})

    api.add_api_route(SEED_PATH, seed, methods=["POST"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="serve_fake_agent")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--seed-api", action="store_true", help="挂测试造数口子 POST /__e2e/seed(翻页用例用;缺省不挂)")
    args = ap.parse_args(argv)
    import logging
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-5s %(name)-22s %(message)s")
    agent, cfg = build(args.dir)
    api = agent.create_api()
    if args.seed_api:
        mount_seed_api(api, agent)

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
