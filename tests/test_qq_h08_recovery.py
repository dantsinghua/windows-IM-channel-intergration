"""QQ H08 恢复后自动回 running(R6-111):连接恢复 ≠ 自动重登(D-2),不触发扫码/重建登录会话。

根因:health._judge 恢复分支(health.py:228-234)只 resolve 告警,无 transition(running)——
NapCat 恢复后账号仍卡 login_required。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qq.config import H08_INTERVAL_S
from qtrade_agent.adapters.qq.health import H08_NAPCAT_HEARTBEAT_LOST, QQHealth
from qtrade_agent.alerts import Alerts
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.store import Store
from tests.conftest import Clock


@pytest.fixture
def qq_rig(tmp_path):
    clock = Clock()
    cfg = AgentConfig()
    store = Store(str(tmp_path / "agent.db"), clock=clock).open()
    store.ensure_account("qq01", "qq", state="login_required", login_mode="qrcode",
                         self_uid="415011447")
    store.upsert_runtime("qq01", kind=store.RUNTIME_KIND["qq"], now_ms=clock(),
                         ws_port=16101, http_port=16201, desired_state="running")
    events = Events(store)
    alerts = Alerts(events, clock=clock)

    transitions: list[tuple] = []

    class FakeAdapter:
        def session_of(self, id):
            return None

    class FakeAccounts:
        def busy(self, id): return False

    async def on_login_required(aid, code):
        transitions.append(("login_required", aid, code))

    async def on_transition(aid, state, **kw):
        transitions.append((state, aid, kw.get("state_code")))

    async def on_recovery(aid, code):
        transitions.append(("running", aid, code))

    hl = QQHealth(adapter=FakeAdapter(), store=store, alerts=alerts, cfg=cfg,
                  on_login_required=on_login_required, on_recovery=on_recovery,
                  busy=FakeAccounts().busy, clock=clock)
    hl._transition = on_transition
    return type("R", (), {"store": store, "alerts": alerts, "clock": clock, "hl": hl,
                          "transitions": transitions, "cfg": cfg})()


async def test_h08_recovery_transitions_back_to_running(qq_rig):
    """H08 心跳恢复后自动 transition(running)(不自动重登,不触发扫码)。"""
    rig = qq_rig
    st = rig.hl.state_of("qq01")
    st.login_required_sent = True
    st.offline_since_ms = rig.clock() - 200_000  # 200s ago
    rig.store.transition("qq01", "login_required", state_code="TOKEN_EXPIRED",
                         state_reason="napcat offline", now_ms=rig.clock())

    # 模拟恢复:心跳恢复、online=true
    class FakeClient:
        silence_ms = lambda self, now: 1000
        connected = True
        last_status = {"online": True}
        async def reconnect(self): return False
        async def call_action(self, action): return {"online": True}

    class FakeSession:
        acct = Account(id="qq01", channel="qq", state="login_required")
        client = FakeClient()

    class FakeAdapterRecovery:
        def session_of(self, id): return FakeSession() if id == "qq01" else None

    rig.hl._adapter = FakeAdapterRecovery()
    await rig.hl.check()

    # 应 transition 回 running
    running = [t for t in rig.transitions if t[0] == "running"]
    assert len(running) >= 1, f"应回 running,实际 transitions={rig.transitions}"
    rig.store.close()


async def test_h08_no_transition_when_already_running(qq_rig):
    """已 running 时不重复 transition(transition 去重保护)。"""
    rig = qq_rig
    rig.store.transition("qq01", "running", now_ms=rig.clock())

    class FakeClient:
        silence_ms = lambda self, now: 1000
        connected = True
        last_status = {"online": True}
        async def reconnect(self): return False
        async def call_action(self, action): return {"online": True}

    class FakeSession:
        acct = Account(id="qq01", channel="qq", state="running")
        client = FakeClient()

    class FakeAdapter:
        def session_of(self, id): return FakeSession() if id == "qq01" else None

    rig.hl._adapter = FakeAdapter()
    rig.hl.watched = lambda: [rig.store.get_account_full("qq01")]
    await rig.hl.check()
    running = [t for t in rig.transitions if t[0] == "running"]
    # transition 去重:已 running 不再 transition
    assert len(running) == 0 or running[0][1] != "qq01"
    rig.store.close()
