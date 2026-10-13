"""POST /accounts/{id}/send 必须把对端 uin 交给企点适配器，而不是当前打开的会话。

假适配器只记录调用，不点界面、不发消息。账号用临时库里的 qd90，与正在跑的真机进程无关。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.testclient import TestClient

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, ApiConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs
from qtrade_agent.vault_client import FakeVault
from qtrade_agent.winagent_client import FakeWinAgent
from tests.conftest import Clock, FakeMainDb

ACCOUNT = "qd90"
PEER = "415011447"
OPEN_OTHER = "999000111"
SELF_UID = "3007378246"
TOKEN = "peer-uin-token"


class FakeQidianAdapter:
    """假 UI 执行层：记下适配器交来的对端 uin。deliver 时只写临时主库，供确认窗合并。"""

    def __init__(self, maindb, clock):
        self.maindb = maindb
        self.clock = clock
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, acct, native_id, text):
        self.calls.append((acct.id, native_id, text))
        async def land():
            await asyncio.sleep(0)
            self.clock.advance(9000)
            self.maindb.insert_text(native_id, text, time_s=self.clock.now_s, issend=1)
        asyncio.create_task(land())
        return True


def _client(tmp_path):
    clock = Clock(auto_step_ms=200)
    maindb = FakeMainDb(str(tmp_path / f"{SELF_UID}.db"), self_uin=SELF_UID)
    cfg = AgentConfig(
        bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0),
        qidian=QidianAdapterConfig(confirm_poll_interval_ms=10),
        api=ApiConfig(http_sync_max_wait_ms=25000),
    )
    ui = FakeQidianAdapter(maindb, clock)
    containers, adb, vault, wa, fs = FakeContainers(), FakeAdb(), FakeVault(), FakeWinAgent(), FakeFs()
    agent = AgentApp(
        cfg,
        db_path=str(tmp_path / "agent.db"),
        clock=clock,
        sender=ui,
        containers=containers,
        adb=adb,
        vault=vault,
        winagent_transport=wa,
        winagent_base_url="http://winagent.fake:17610",
        winagent_token=wa.token,
        fs=fs,
        wsl_env_reader=SimpleNamespace(versions=AsyncMock(return_value={})),
        deployment_disks=SimpleNamespace(snapshot=AsyncMock(return_value={"disks": [], "error": "test_host_unknown"})),
        maindb_factory=lambda uid, acct: LocalSqliteMainDb(maindb.path),
        boot_poll_s=0,
    ).open()
    st = agent.store
    st.ensure_account(ACCOUNT, "qidian", state="running", self_uid=SELF_UID, label="采集端单测")
    st.upsert_api_client(app_id="peer-uin", name="单测", level="admin", token=TOKEN)
    # 库里已有另一个更晚的会话。发送不得改去这个「当前打开」的对端。
    maindb.insert_text(OPEN_OTHER, "正在看的会话", time_s=clock.now_s)
    maindb.insert_text(PEER, "历史", time_s=clock.now_s - 3600)
    now = clock.now_ms
    with st._tx() as c:
        c.execute(
            "INSERT INTO sessions(id, account_id, channel, native_id, name, kind, last_msg_ms, created_ms, updated_ms) "
            "VALUES (?,?, 'qidian', ?, ?, 'private', ?, ?, ?)",
            (f"{ACCOUNT}:{OPEN_OTHER}", ACCOUNT, OPEN_OTHER, "当前打开的会话", now, now, now),
        )
    api = agent.create_api()
    client = TestClient(api, client=("127.0.0.1", 40000))
    client.__enter__()
    client.portal.call(agent.qidian_poll_all)
    return client, agent, ui


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_post_send_passes_requested_uin_not_open_session(tmp_path):
    client, agent, ui = _client(tmp_path)
    try:
        bodies = (
            {"session": f"{ACCOUNT}:{PEER}", "text": "指定会话", "idempotency_key": "peer-prefixed"},
            {"session": PEER, "text": "裸uin", "idempotency_key": "peer-bare"},
        )
        for body in bodies:
            before = len(ui.calls)
            r = client.post(f"/api/v1/accounts/{ACCOUNT}/send", headers=_headers(), json=body)
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True and r.json()["code"] == "DELIVERED"
            assert ui.calls[before:] == [(ACCOUNT, PEER, body["text"])]
            assert all(uin != OPEN_OTHER for _, uin, _ in ui.calls)
            row = agent.store.con.execute(
                "SELECT session_id FROM messages WHERE account_id=? AND dir='out' AND text=?",
                (ACCOUNT, body["text"]),
            ).fetchone()
            assert row["session_id"] == f"{ACCOUNT}:{PEER}"
    finally:
        client.__exit__(None, None, None)
        agent.store.close()


def test_post_send_rejects_missing_session_text_and_offline_without_ui(tmp_path):
    client, agent, ui = _client(tmp_path)
    st = agent.store
    try:
        missing_session = client.post(
            f"/api/v1/accounts/{ACCOUNT}/send",
            headers=_headers(),
            json={"text": "没有会话", "idempotency_key": "no-session"},
        )
        assert missing_session.status_code == 400
        assert missing_session.json()["code"] == "INVALID_ARGS"
        assert missing_session.json()["error"]["reason"] == "missing_session"

        missing_text = client.post(
            f"/api/v1/accounts/{ACCOUNT}/send",
            headers=_headers(),
            json={"session": f"{ACCOUNT}:{PEER}", "idempotency_key": "no-text"},
        )
        assert missing_text.status_code >= 400 or missing_text.json().get("ok") is False
        empty_text = client.post(
            f"/api/v1/accounts/{ACCOUNT}/send",
            headers=_headers(),
            json={"session": PEER, "text": "", "idempotency_key": "empty-text"},
        )
        assert empty_text.status_code == 400
        assert empty_text.json()["error"]["reason"] == "missing_text"

        st.set_account_state(ACCOUNT, "stopped")
        offline = client.post(
            f"/api/v1/accounts/{ACCOUNT}/send",
            headers=_headers(),
            json={"session": f"{ACCOUNT}:{PEER}", "text": "不在线", "idempotency_key": "offline"},
        )
        assert offline.json().get("ok") is False
        assert offline.status_code >= 400 or offline.json()["code"] in {"NOT_READY", "LOGIN_REQUIRED"}

        st.set_account_state(ACCOUNT, "login_required", state_code="KICKED")
        kicked = client.post(
            f"/api/v1/accounts/{ACCOUNT}/send",
            headers=_headers(),
            json={"session": PEER, "text": "被踢", "idempotency_key": "kicked"},
        )
        assert kicked.json()["ok"] is False
        assert kicked.json()["code"] == "LOGIN_REQUIRED"
        assert ui.calls == []
    finally:
        client.__exit__(None, None, None)
        st.close()
