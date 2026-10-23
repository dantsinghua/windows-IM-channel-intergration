"""第五批接线:``app.py`` 装配与起停、``daily_at`` 日历包装、``events.emit → webhook`` 扇出、写路径 ``guard_write``。

规格:02 §2.1 启动顺序、§2.2.7 outbox→webhook、§2.2.11 scheduler、§2.8.4 保留期清理 03:00、§2.8.8 磁盘水位与写入前判满、§3.3 备份 03:30。
"""
from __future__ import annotations

import sqlite3

import pytest

from qtrade_agent.adapters.base import Account
from qtrade_agent.app import daily_at
from qtrade_agent.maintenance import DiskFullError
from qtrade_agent.models import Message, Session
from qtrade_agent.webhook import HttpResponse
from tests.test_integration_wiring_common import close_rig, make_rig

SCHED_EXPECTED = {
    # 一~四批已有的
    "qidian_poll_all", "qidian_gaps_all", "outbox_ws_retention", "winagent_probe", "dockerd_probe", "h13_clock_sync",
    "health_containers", "health_adb", "health_boot", "login_remind",
    # 第五批接线新增的
    "health_napcat", "health_wechat", "wechat_slot_reaper", "wechat_poll_all", "webhook_dispatch",
    "disk_watermark", "pool_calibrate_drift", "retention_cleanup", "db_backup",
    "mail_inbound", "mail_outbound", "mail_confirm_reaper",
}


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    yield r
    close_rig(r)


# ---------------------------------------------------------------- 装配
def test_open_wires_all_five_batches(rig):
    """02 §2.1:``open()`` 之后四路新模块与三通道适配器都在,且**缺省一个真服务都没连**。"""
    a = rig.agent
    assert set(a.adapters) == {"qidian", "qq", "wechat"}
    for name in ("qqhealth", "wechat_slot", "wechat_login", "wechat_poller", "webhooks", "maintenance",
                 "calibrator", "workflows", "hmac", "mail"):
        assert getattr(a, name, None) is not None, name
    assert a.mail.cfg.enabled is False and a.mail.fetchers == {}      # [mail] enabled=false ⇒ 一个邮箱连接都不建
    assert rig.wechat.calls == [] and rig.bots == {}                  # 没有账号 start ⇒ 不建 OneBot/微信连接
    assert rig.http.requests == []                                    # webhooks 表空 ⇒ 不出网


def test_scheduler_registers_every_wired_job(rig):
    assert SCHED_EXPECTED <= set(rig.agent.scheduler.jobs)


def test_accounts_gets_slot_login_and_bus_injected(rig):
    a = rig.agent
    assert a.accounts._wechat_slot is a.wechat_slot
    assert a.accounts._wechat_login is a.wechat_login
    assert a.accounts._bus is a.bus


def test_store_write_guard_is_maintenance_guard(rig):
    assert rig.store.write_guard == rig.agent.maintenance.guard_write


# ---------------------------------------------------------------- 起停对称
async def test_start_then_stop_is_symmetric(tmp_path):
    """``start`` 起计时循环、``stop`` 依次收:scheduler → webhook 投递器 → 适配器连接 → 总线 → 库。"""
    r = make_rig(tmp_path)
    try:
        r.store.ensure_account("qq01", "qq", state="running", self_uid="415011447")
        r.store.upsert_runtime("qq01", kind="napcat", desired_state="running")
        await r.agent.start(recover=False)
        assert r.agent._started is True
        assert any(j._task is not None for j in r.agent.scheduler.jobs.values())
        await r.agent.adapters["qq"].start(Account(id="qq01", channel="qq", state="running", self_uid="415011447"))
        assert r.agent.adapters["qq"].session_of("qq01") is not None
        await r.agent.stop()
        assert r.agent._started is False
        assert all(j._task is None for j in r.agent.scheduler.jobs.values())
        assert r.agent.adapters["qq"].session_of("qq01") is None       # OneBot 连接已断
    finally:
        r.client.__exit__(None, None, None)


async def test_stop_survives_a_failing_shutdown_step(tmp_path, monkeypatch):
    """收尾的任一步失败都不许挡住后面的收尾(webhook 投递器抛了,总线与库照样关)。"""
    r = make_rig(tmp_path)
    r.client.__exit__(None, None, None)

    async def boom():
        raise RuntimeError("dispatcher stuck")
    monkeypatch.setattr(r.agent.webhooks, "stop", boom)
    await r.agent.start(recover=False)
    await r.agent.stop()
    assert r.agent._started is False and r.agent.store._con is None


# ---------------------------------------------------------------- daily_at(§2.8.4 03:00 / §3.3 03:30)
async def test_daily_at_first_round_only_records_date(rig):
    """首轮遇到「今天已过点但库里没记录」只补记日期**不补跑** —— 否则装完 Agent 就立刻跑一次清理。"""
    ran: list[int] = []
    rig.clock.set_ms(_ms("2026-09-20 10:00:00"))
    tick = daily_at("03:00", _appender(ran), store=rig.store, key="t.k", clock=rig.clock)
    await tick()
    assert ran == [] and rig.store.settings_get("t.k") == "2026-09-20"


async def test_daily_at_runs_once_per_day_after_the_hour(rig):
    ran: list[int] = []
    rig.clock.set_ms(_ms("2026-09-20 02:00:00"))
    tick = daily_at("03:00", _appender(ran), store=rig.store, key="t.k2", clock=rig.clock)
    await tick()
    assert ran == [] and rig.store.settings_get("t.k2") is None       # 没到点:不跑也不记
    rig.clock.set_ms(_ms("2026-09-20 03:00:30"))
    await tick()
    assert ran == [] and rig.store.settings_get("t.k2") == "2026-09-20"   # 首轮只记
    rig.clock.set_ms(_ms("2026-09-21 03:01:00"))
    await tick()
    assert len(ran) == 1 and rig.store.settings_get("t.k2") == "2026-09-21"
    rig.clock.set_ms(_ms("2026-09-21 23:59:00"))
    await tick()
    assert len(ran) == 1                                              # 同一天第二轮不重复跑


# ---------------------------------------------------------------- events.emit → webhook 扇出(§2.2.7)
def test_emit_fans_out_to_subscribed_webhook_only(rig):
    _webhook(rig, "wh1", events=["account_state"])
    _webhook(rig, "wh2", events=["message"])
    rig.agent.events.emit("account_state", payload={"state": "running"}, account_id="qd01", channel="qidian")
    rows = rig.outbox()
    assert [r["target"] for r in rows] == ["webhook:wh1"]              # 只给订阅方写副本
    assert [r["target"] for r in rig.outbox("ws")] == ["ws"]           # ws 行照写


def test_emit_skips_disabled_webhook(rig):
    _webhook(rig, "wh1", events=["*"], enabled=0)
    rig.agent.events.emit("alert", payload={"code": "X", "subject": "host"})
    assert rig.outbox() == []


def test_net_public_endpoint_changed_ignores_subscription_filter(rig):
    """E-3(§2.2.12):``NET_PUBLIC_ENDPOINT_CHANGED`` 必须推给**全部** enabled 登记方,不受订阅过滤。"""
    _webhook(rig, "wh1", events=["message"])                          # 没订 net
    rig.agent.events.emit("net", payload={"code": "NET_PUBLIC_ENDPOINT_CHANGED", "public_ip": "1.2.3.4"})
    assert [r["target"] for r in rig.outbox()] == ["webhook:wh1"]
    rig.agent.events.emit("net", payload={"code": "NET_STATE_CHANGED"})   # 别的 net 码仍按订阅过滤
    assert [r["target"] for r in rig.outbox()] == ["webhook:wh1"]


async def test_webhook_dispatch_tick_delivers_signed_body(rig):
    _webhook(rig, "wh1", events=["*"])
    rig.agent.events.emit("alert", payload={"code": "X", "subject": "host"})
    rig.http.default = HttpResponse(200)
    await rig.agent.scheduler.run_once("webhook_dispatch")
    assert len(rig.http.requests) == 1
    req = rig.http.requests[0]
    assert req["url"] == "https://hook.example/qt"
    assert req["headers"]["X-QT-Webhook-AppId"] == "wh1" and req["headers"]["X-QT-Webhook-Signature"].startswith("v1=")
    assert [r["status"] for r in rig.outbox()] == ["delivered"]


def test_emit_hook_failure_does_not_break_ws_row(rig, monkeypatch):
    """扇出钩子抛异常也不许拖垮 WS:ws 行照落、队列照推。"""
    def boom(**kw):
        raise RuntimeError("fanout down")
    monkeypatch.setattr(rig.agent.webhooks, "fanout", boom)
    seq = rig.agent.events.emit("alert", payload={"code": "X", "subject": "host"})
    assert seq > 0 and len(rig.outbox("ws")) == 1


# ---------------------------------------------------------------- guard_write(§2.8.8)
def test_store_ingest_disk_full_becomes_disk_full_error(rig, monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(rig.store, "_ingest_one", boom)
    msg = Message(account_id="qd01", channel="qidian", session=Session("qd01", "123", "private", name="x"),
                  dir="in", type="text", text="hi", ts_ms=rig.clock(), source="qidian_db", ext_msg_id="qd:1")
    with pytest.raises(DiskFullError):
        rig.store.ingest(msg)
    codes = [e["payload"]["code"] for e in rig.events_of("alert")]
    assert "DB_WRITE_FAILED" in codes


def test_store_ingest_passes_non_disk_errors_through(rig, monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("no such table: nope")
    monkeypatch.setattr(rig.store, "_ingest_one", boom)
    msg = Message(account_id="qd01", channel="qidian", session=Session("qd01", "123", "private", name="x"),
                  dir="in", type="text", text="hi", ts_ms=rig.clock(), source="qidian_db", ext_msg_id="qd:2")
    with pytest.raises(sqlite3.OperationalError):
        rig.store.ingest(msg)


# ---------------------------------------------------------------- 周期任务本体
async def test_disk_tick_fires_h12_when_below_warn(rig):
    rig.disk.free = 1000                                   # < disk_critical_mb(1024)
    await rig.agent.scheduler.run_once("disk_watermark")
    a = [e["payload"] for e in rig.events_of("alert") if e["payload"]["code"] == "H12_DISK_LOW"]
    assert a and a[-1]["severity"] == "crit" and rig.agent.maintenance.level == "critical"


async def test_calib_tick_is_quiet_without_samples(rig):
    await rig.agent.scheduler.run_once("pool_calibrate_drift")
    assert [e for e in rig.events_of("resource")] == []


async def test_mail_ticks_are_noop_when_mail_disabled(rig):
    for name in ("mail_inbound", "mail_outbound", "mail_confirm_reaper"):
        assert await rig.agent.scheduler.run_once(name) is True
    assert rig.agent.scheduler.jobs["mail_inbound"].errors == 0


# ---------------------------------------------------------------- 小工具
def _appender(bucket: list[int]):
    async def fn() -> None:
        bucket.append(1)
    return fn


def _ms(local: str) -> int:
    from datetime import datetime
    from qtrade_agent.events import TZ_SHANGHAI
    return int(datetime.strptime(local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_SHANGHAI).timestamp() * 1000)


def _webhook(rig, wid: str, *, events: list[str], enabled: int = 1, secret: str = "wh-secret") -> None:
    """登记一个 webhook 并把密钥塞进假保险库(``secret_ref`` → Vault 明文,基线 §11.1)。"""
    import json as _json
    from qtrade_agent.vault_client import _Entry
    rig.store.con.execute(
        "INSERT INTO webhooks(id, name, url, secret_ref, events_json, accounts_json, enabled, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (wid, wid, "https://hook.example/qt", f"vault://webhook/{wid}", _json.dumps(events), '["*"]', enabled,
         rig.clock(), rig.clock()))
    rig.agent.vault.entries[f"webhook/{wid}"] = _Entry(secret, "webhook")
