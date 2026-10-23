"""发送动作耗时不侵占读回预算；使用真实总线、存储与企点读库合并。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from qtrade_agent.adapters.qidian.adapter import QidianAdapter
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.poll import QidianPoller
from qtrade_agent.alerts import Alerts
from qtrade_agent.bus.bus import Bus
from qtrade_agent.config import AgentConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Command


PEER = "415011447"


class SlowUiSender:
    """只替代设备 UI 动作；主库读回由测试在明确时点单独写入。"""

    def __init__(self, clock):
        self.clock = clock
        self.finished = asyncio.Event()

    async def __call__(self, acct, native_id, text):
        self.clock.advance(27_000)
        self.finished.set()
        return True


class ObservedQidianAdapter(QidianAdapter):
    def __init__(self, *args, clock, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock
        self.polls = asyncio.Queue()

    async def poll(self, acct, *, only_sessions=None):
        await super().poll(acct, only_sessions=only_sessions)
        self.polls.put_nowait(self.clock())


@pytest.fixture
async def confirmation_rig(store, clock, maindb):
    cfg = AgentConfig(
        bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0,
                      confirm_timeout_qidian_ms=15_000),
        qidian=QidianAdapterConfig(confirm_poll_interval_ms=1),
    )
    events = Events(store)
    poller = QidianPoller(
        store=store, events=events, alerts=Alerts(events, clock=clock),
        cfg=cfg, h13_firing=lambda: False, clock=clock,
        maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path),
    )
    sender = SlowUiSender(clock)
    adapter = ObservedQidianAdapter(poller, sender=sender, store=store, clock=clock)
    bus = Bus(store=store, events=events, adapters={"qidian": adapter}, cfg=cfg, clock=clock)
    maindb.insert_text(PEER, "登录前历史", time_s=clock.now_s - 3600)
    poller.poll_maindb(adapter._view(bus._load_account("qd01")))
    tasks = []
    rig = SimpleNamespace(bus=bus, store=store, clock=clock, maindb=maindb,
                          adapter=adapter, sender=sender, tasks=tasks)
    yield rig
    await bus.close()
    await asyncio.gather(*tasks, return_exceptions=True)


def start_send(rig, *, key):
    cmd = Command("qd01", "send_text", {"session": f"qd01:{PEER}", "text": key},
                  idempotency_key=key, timeout_ms=60_000)
    task = asyncio.create_task(rig.bus.submit(cmd))
    rig.tasks.append(task)
    return cmd, task


async def queue_barrier(rig):
    """同账号读取完成，证明 UI 已让出队列且确认任务得到调度。"""
    result = await asyncio.wait_for(rig.bus.submit(Command("qd01", "get_state", {})), 2)
    assert result.ok and result.code == "OK"


@pytest.mark.parametrize("readback_delay_ms", [5_000, 14_999])
async def test_slow_ui_keeps_full_window_for_native_readback(confirmation_rig, readback_delay_ms):
    rig = confirmation_rig
    started = rig.clock()
    cmd, task = start_send(rig, key=f"慢UI后读回-{readback_delay_ms}")
    await asyncio.wait_for(rig.sender.finished.wait(), 2)
    await queue_barrier(rig)
    assert not task.done(), "27秒UI动作后，15秒读回确认窗口应刚开始"

    rig.clock.advance(readback_delay_ms)
    rig.maindb.insert_text(PEER, cmd.args["text"], time_s=rig.clock.now_s, issend=1)
    result = await asyncio.wait_for(task, 2)

    assert result.ok and result.code == "DELIVERED"
    assert result.source == "qidian_db"
    assert result.data["confirmed_by"] == "ingest_merge"
    row = rig.store.get_message(result.data["message_id"])
    assert row["state"] == "DELIVERED" and row["ext_msg_id"].startswith("qd:")
    assert row["trace_id"] == cmd.trace_id
    assert rig.store.count_messages("qd01") == 1
    assert result.cost_ms == 27_000 + readback_delay_ms
    assert rig.clock() - started == result.cost_ms
    assert rig.store.idem_get("qd01", cmd.idempotency_key)["status"] == "DONE"


async def test_missing_readback_times_out_only_after_post_send_budget(confirmation_rig):
    rig = confirmation_rig
    cmd, task = start_send(rig, key="慢UI无读回保持待核")
    await asyncio.wait_for(rig.sender.finished.wait(), 2)
    await queue_barrier(rig)
    assert not task.done(), "UI耗时不能将尚未开始的读回预算判成超时"

    # 先耗尽预算的前14999毫秒，仍须至少执行一次目标会话读库。
    while not rig.adapter.polls.empty():
        rig.adapter.polls.get_nowait()
    rig.clock.advance(14_999)
    await asyncio.wait_for(rig.adapter.polls.get(), 2)
    assert not task.done(), "确认预算尚剩1毫秒时不能提前待核"
    rig.clock.advance(1)
    result = await asyncio.wait_for(task, 2)

    assert not result.ok and result.code == "SEND_CALLED_BUT_UNCONFIRMED"
    assert result.cost_ms == 42_000
    assert rig.store.get_message(result.data["message_id"])["state"] == "UNCONFIRMED"
    assert rig.store.idem_get("qd01", cmd.idempotency_key)["status"] == "SENDING"
    assert rig.store.get_command_result(cmd.trace_id)["code"] == "SEND_CALLED_BUT_UNCONFIRMED"


async def test_waiting_for_confirmation_leaves_same_account_queue_available(confirmation_rig):
    rig = confirmation_rig
    _, task = start_send(rig, key="确认期间队列可用")
    await asyncio.wait_for(rig.sender.finished.wait(), 2)
    await queue_barrier(rig)
    assert not task.done(), "下一条指令应在发送仍等待确认时完成"
    assert rig.store.count_messages("qd01") == 1
    row = rig.store.con.execute("SELECT state FROM messages WHERE account_id='qd01'").fetchone()
    assert row["state"] == "SENDING"
