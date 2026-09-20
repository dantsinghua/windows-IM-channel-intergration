"""bus:R6-48 入口校验、出向先落库、队列外等确认(R6-38)、幂等三态、登录门 —— 对照 02 §2.2.2 / B-06 / B-07 / B-10 / B-40。"""
from __future__ import annotations

import asyncio

import pytest

from qtrade_agent.adapters.qidian.adapter import QidianAdapter
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.poll import QidianPoller
from qtrade_agent.alerts import Alerts
from qtrade_agent.bus.bus import Bus
from qtrade_agent.bus.validate import validate_send_text
from qtrade_agent.config import AgentConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Command

PEER = "415011447"


def test_validate_send_text_r6_48():
    e = validate_send_text({"session": "qd01:1", "text": "收到\u0014A"})
    assert e is not None and e.reason == "text_has_control_chars" and e.details[0]["pointer"] == "/text"
    assert validate_send_text({"session": "qd01:1", "text": "收到\x08"}).reason == "text_has_control_chars"
    assert validate_send_text({"session": "qd01:1", "text": "1Y\n1.70"}) is None
    assert validate_send_text({"session": "qd01:1", "text": "收到👌"}) is None
    assert validate_send_text({"session": "qd01:1", "text": "收到　ＯＫ"}) is None
    assert validate_send_text({"text": "x"}).reason == "missing_session"


class FakeSender:
    """假 RPA 执行层:点了发送键 → 若 `deliver` 为真,模拟企点 `latency_ms` 后把消息落进主库(issend=1)。"""

    def __init__(self, maindb, clock, *, deliver=True, latency_ms=9000):
        self.maindb, self.clock, self.deliver, self.latency_ms = maindb, clock, deliver, latency_ms
        self.calls = []

    async def __call__(self, acct, native_id, text):
        self.calls.append((acct.id, native_id, text))
        if self.deliver:
            async def land():
                await asyncio.sleep(0)                     # 让 bus 先把队列让出去
                self.clock.advance(self.latency_ms)
                self.maindb.insert_text(native_id, text, time_s=self.clock.now_s, issend=1)
            asyncio.create_task(land())
        return True


@pytest.fixture
def cfg():
    return AgentConfig(bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0), qidian=QidianAdapterConfig(confirm_poll_interval_ms=10))


@pytest.fixture
async def rig(store, clock, maindb, cfg):
    clock.auto_step_ms = 200                 # 确认窗循环靠读时钟推进;15000 ms 窗 ≈ 40 次循环 × 10 ms 真实时间
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    poller = QidianPoller(store=store, events=events, alerts=alerts, cfg=cfg, h13_firing=lambda: False, clock=clock,
                          maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path))
    sender = FakeSender(maindb, clock)
    adapter = QidianAdapter(poller, sender=sender, store=store)
    bus = Bus(store=store, events=events, adapters={"qidian": adapter}, cfg=cfg, clock=clock)
    # 测试号会话表早已存在;先跑一轮全量 poll 建 bootstrap 与表映射(登录后读循环已跑过)。
    # 首次向一个还没有表的新会话发送时,确认窗内的加速轮查不到表、要等下一个全量轮(≤ poll_interval_s)发现——属规格,不在本夹具里模拟
    maindb.insert_text(PEER, "历史", time_s=clock.now_s - 3600)
    poller.poll_maindb(adapter._view(bus._load_account("qd01")))
    yield {"bus": bus, "store": store, "clock": clock, "maindb": maindb, "sender": sender, "poller": poller, "events": events}
    await bus.close()


async def test_send_text_delivered_via_ingest_merge(rig):
    bus, st = rig["bus"], rig["store"]
    res = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "收到"}, idempotency_key="k9"))
    assert res.ok and res.code == "DELIVERED" and res.source == "qidian_db" and res.data["confirmed_by"] == "ingest_merge"
    row = st.get_message(res.data["message_id"])
    assert row["state"] == "DELIVERED" and row["ext_msg_id"].startswith("qd:") and row["trace_id"] == res.trace_id
    assert st.count_messages("qd01") == 1                                     # 读回不插第二行
    cr = st.get_command_result(res.trace_id)
    assert cr["code"] == "DELIVERED" and cr["confirmed_by"] == "ingest_merge" and cr["source"] == "qidian_db"
    idem = st.idem_get("qd01", "k9")
    assert idem["status"] == "DONE" and idem["result_code"] == "DELIVERED"
    args = st.con.execute("select args_json from commands where trace_id=?", (res.trace_id,)).fetchone()[0]
    assert "收到" not in args and "text_sha8" in args                          # P-11:args_json 不存正文
    ev = st.list_events("message", "qd01")
    assert len(ev) == 1 and ev[0]["payload"]["origin"] == "rpa"
    # B-06:同 key 同参 → IDEMPOTENT_REPLAY 且 data 逐字相同;同 key 不同参 → INVALID_ARGS
    r2 = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "收到"}, idempotency_key="k9"))
    assert r2.code == "IDEMPOTENT_REPLAY" and r2.data == res.data and r2.trace_id == res.trace_id
    r3 = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "b"}, idempotency_key="k9"))
    assert r3.code == "INVALID_ARGS" and r3.error.reason == "idempotency_args_mismatch"
    assert st.con.execute("select count(*) from commands").fetchone()[0] == 1


async def test_control_chars_rejected_before_sending_row(rig):
    bus, st = rig["bus"], rig["store"]
    for text in ("收到\u0014A", "收到\x08"):
        res = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": text}, idempotency_key="k10"))
        assert not res.ok and res.code == "INVALID_ARGS" and res.error.reason == "text_has_control_chars" and res.error.details[0]["pointer"] == "/text"
    assert st.count_messages("qd01") == 0                                              # 无 SENDING 行
    assert st.con.execute("select count(*) from commands").fetchone()[0] == 0          # 未进 commands
    assert st.idem_get("qd01", "k10") is None                                          # 未占幂等键
    res = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "1Y\n1.70"}, idempotency_key="k10"))
    assert res.code == "DELIVERED" and st.get_message(res.data["message_id"])["text"] == "1Y\n1.70"   # 出向行存原文、保留换行


async def test_unconfirmed_when_readback_never_lands(rig):
    bus, st, sender = rig["bus"], rig["store"], rig["sender"]
    sender.deliver = False
    res = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "石沉大海"}, idempotency_key="k11", timeout_ms=30000))
    assert res.code == "SEND_CALLED_BUT_UNCONFIRMED" and not res.ok
    assert st.get_message(res.data["message_id"])["state"] == "UNCONFIRMED"
    assert st.idem_get("qd01", "k11")["status"] == "SENDING"                            # B-08:留 SENDING
    # 窗内每 confirm_poll_interval_ms 投过加速轮(表映射已建,poll 被调过)
    assert rig["poller"].state_of("qd01").table_map


async def test_login_gate_rejects_send_without_queue(rig):
    bus, st = rig["bus"], rig["store"]
    st.set_account_state("qd01", "login_required", state_code="WAIT_SMS")
    res = await bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "x"}, idempotency_key="k12"))
    assert res.code == "LOGIN_REQUIRED" and res.error.needs_human and not res.error.retryable
    assert st.idem_get("qd01", "k12") is None and st.con.execute("select count(*) from commands").fetchone()[0] == 0
    res2 = await bus.submit(Command("qd01", "get_state", {}))
    assert res2.code != "LOGIN_REQUIRED"                                                   # 屏幕类放行


async def test_queue_yields_between_send_and_confirm(rig):
    """R6-38:send 点完即让出队列——确认在途时同账号下一条指令照常执行。"""
    bus, sender = rig["bus"], rig["sender"]
    sender.deliver = False
    t1 = asyncio.create_task(bus.submit(Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": "第一条"}, idempotency_key="a", timeout_ms=30000)))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(bus.submit(Command("qd01", "get_state", {})))
    r2 = await asyncio.wait_for(t2, 2)
    assert not t1.done() and r2.code in ("UNSUPPORTED", "OK")                              # 第二条不等第一条的确认窗
    r1 = await t1
    assert r1.code == "SEND_CALLED_BUT_UNCONFIRMED"
