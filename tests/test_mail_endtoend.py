"""指令邮件端到端(真 ``bus`` + 企点读库假后端)—— 规格:docs/06 §8b「M5 指令邮件端到端」「M5 幂等与改写键」、§2.8 D-2。

链路:``FakeImap`` 里放一封指令邮件 → ``fetch_once`` 取信 → 三道闸 → ``dispatch`` 投**真 Bus** →
假 RPA 点发送 → 企点主库落出向行 → 读回合并成 ``DELIVERED`` → 回执入队 → ``send_once`` 发出 → ``RECEIPT_SENT``。
"""
from __future__ import annotations

import pytest

from qtrade_agent.adapters.qidian.adapter import QidianAdapter
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.poll import QidianPoller
from qtrade_agent.alerts import Alerts
from qtrade_agent.bus.bus import Bus
from qtrade_agent.config import AgentConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.events import Events
from qtrade_agent.mail.codes import ACCEPTED, DONE, RECEIPT_SENT
from test_bus import PEER, FakeSender
from test_mail_common import OPS_ADDR, command_mail, make_env

SESSION = f"qd01:{PEER}"


@pytest.fixture
async def rig(store, clock, maindb, tmp_path):
    clock.auto_step_ms = 200                       # 确认窗循环靠读时钟推进(同 test_bus 的夹具)
    cfg = AgentConfig(bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0),
                      qidian=QidianAdapterConfig(confirm_poll_interval_ms=10))
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    poller = QidianPoller(store=store, events=events, alerts=alerts, cfg=cfg, h13_firing=lambda: False, clock=clock,
                          maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path))
    adapter = QidianAdapter(poller, sender=FakeSender(maindb, clock), store=store)
    bus = Bus(store=store, events=events, adapters={"qidian": adapter}, cfg=cfg, clock=clock)
    maindb.insert_text(PEER, "历史", time_s=clock.now_s - 3600)
    poller.poll_maindb(adapter._view(bus._load_account("qd01")))      # 建 bootstrap 与表映射
    env = make_env(store, clock, tmp_path=tmp_path, events=events, alerts=alerts)
    yield env, bus
    await bus.close()


async def test_command_mail_end_to_end(rig):
    """§8b M5:``mail_inbox.status`` 走 ``RECEIVED→ACCEPTED→DONE→RECEIPT_SENT``;发起方收到 ``QTRADE回执 v1 … DELIVERED``。"""
    env, bus = rig
    env.imap.add(command_mail(req_id="20260918-ops-0007", session=SESSION, args={"text": "今日 3M 报价 1.52"}))
    env.service.fetch_once()
    row = env.inbox_rows()[0]
    assert row["status"] == ACCEPTED and row["op"] == "send_text"

    results = await env.service.dispatch(bus)
    assert results[0].code == "DELIVERED" and results[0].source == "qidian_db"
    row = env.inbox_rows()[0]
    assert row["status"] == DONE and row["trace_id"] == results[0].trace_id

    env.service.send_once()
    row = env.inbox_rows()[0]
    receipt = [r for r in env.outbox_rows() if r["kind"] == "receipt"][0]
    assert row["status"] == RECEIPT_SENT and receipt["status"] == "SENT"
    assert receipt["subject"] == "QTRADE回执 v1 [qd01] send_text 20260918-ops-0007 DELIVERED"
    assert receipt["in_reply_to"] == "<m1@corp.example>"              # In-Reply-To 指向指令邮件
    assert "确认方式：qidian_db" in receipt["body_text"] and "可重试：否" in receipt["body_text"]
    assert env.smtp.sent and env.smtp.sent[0][1] == [OPS_ADDR]

    # 真的发到了企点:出向行合并成 DELIVERED、不插第二行
    msg = env.service.ingest.store.get_message(results[0].data["message_id"])
    assert msg["state"] == "DELIVERED" and msg["ext_msg_id"].startswith("qd:")
    assert env.service.ingest.store.count_messages("qd01") == 1

    # §8b M5「幂等与改写键」:`idempotency` 表里键形如 mail:ops:20260918-ops-0007
    idem = env.service.ingest.store.idem_get("qd01", "mail:ops:20260918-ops-0007")
    assert idem is not None and idem["status"] == "DONE" and idem["result_code"] == "DELIVERED"
    cmd_row = env.service.ingest.store.get_command(results[0].trace_id)
    assert cmd_row["origin_transport"] == "email" and cmd_row["origin_actor"] == "mail:" + OPS_ADDR


async def test_login_phase_command_mail_is_not_queued(rig, store):
    """D-2(§2.8 末 / §8b M2.5):登录阶段**立即** ``LOGIN_REQUIRED``、不进等待队列;回执马上发出。"""
    env, bus = rig
    store.set_account_state("qd01", "login_required", state_code="WAIT_QRCODE")
    env.imap.add(command_mail(req_id="r-login", session=SESSION, args={"text": "在吗"}))
    env.service.fetch_once()
    results = await env.service.dispatch(bus)
    assert results[0].code == "LOGIN_REQUIRED" and results[0].error.needs_human
    env.service.send_once()
    receipt = [r for r in env.outbox_rows() if r["kind"] == "receipt"][0]
    assert "需人工：是" in receipt["body_text"] and "送达状态：LOGIN_REQUIRED" in receipt["body_text"]
    assert env.inbox_rows()[0]["status"] == RECEIPT_SENT
    # 不排队:commands 里那一行是 failed 且没进过队列(started_ms IS NULL,B-30)
    cmd = store.get_command(results[0].trace_id)
    assert cmd["status"] == "failed" and cmd["started_ms"] is None
