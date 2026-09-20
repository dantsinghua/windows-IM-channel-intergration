"""H08 napcat WS 心跳(docs/04 §2.3 H08 行 + §5 F-08 + §8 A5-07;告警码 docs/02 §3.7)。"""
from __future__ import annotations

import pytest

from qtrade_agent.adapters.qq import (H08_INTERVAL_S, H08_LOGIN_REQUIRED_STATE_CODE, H08_NAPCAT_HEARTBEAT_LOST,
                                      H08_OFFLINE_TO_LOGIN_REQUIRED_S, QQAdapterConfig, QQHealth, status_url)
from test_qq_common import make_qq_rig


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, account_id: str, state_code: str) -> None:
        self.calls.append((account_id, state_code))


@pytest.fixture
async def h08(tmp_path):
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    rec = Recorder()
    probe = QQHealth(adapter=r.adapter, store=r.store, alerts=r.alerts, cfg=r.cfg,
                     on_login_required=rec, clock=r.clock)
    yield r, probe, rec
    await r.adapter.close()
    r.close()


def firing(rig) -> bool:
    return (H08_NAPCAT_HEARTBEAT_LOST, f"account:{rig.acct.id}") in rig.alerts.active


def alert_events(rig) -> list[dict]:
    return [e["payload"] for e in rig.emitted("alert") if e["payload"]["code"] == H08_NAPCAT_HEARTBEAT_LOST]


# ---------------------------------------------------------------- 常量与地址
def test_interval_and_offline_window_match_spec():
    """04 §2.3 H08 字面:周期 15 s;``online=false`` 持续 2 min 转 ``login_required``。"""
    assert H08_INTERVAL_S == 15
    assert H08_OFFLINE_TO_LOGIN_REQUIRED_S == 120


def test_heartbeat_timeout_comes_from_health_section():
    """阈值 = ``[health] napcat_heartbeat_timeout_s=30``(owner=04,config.py 已镜像)。"""
    from qtrade_agent.config import AgentConfig
    assert AgentConfig().health.napcat_heartbeat_timeout_s == 30


def test_status_url_is_derived_from_seq():
    """04 H08 备用探测:``GET http://127.0.0.1:162NN/get_status``。"""
    assert status_url("qq03") == "http://127.0.0.1:16203/get_status"


def test_login_required_state_code_is_from_offline_group():
    """05 §2.5.4「掉线原因」组(QQ 行给 ``TOKEN_EXPIRED`` / ``KICKED``)。"""
    assert H08_LOGIN_REQUIRED_STATE_CODE == "TOKEN_EXPIRED"


# ---------------------------------------------------------------- 正常
async def test_checks_unknown_before_first_round(h08):
    _, probe, _ = h08
    assert probe.checks() == {"H08": "unknown"}


async def test_fresh_heartbeat_online_no_alert(h08):
    rig, probe, rec = h08
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    assert not firing(rig) and rec.calls == []
    assert probe.checks() == {"H08": "ok"}
    assert probe.last[rig.acct.id]["lost"] is False and probe.last[rig.acct.id]["online"] is True


async def test_no_heartbeat_yet_but_recent_connect_is_not_lost(h08):
    """刚连上算一次「有动静」(免得首轮拿 None 当超时)。"""
    rig, probe, _ = h08
    await probe.check()
    assert probe.last[rig.acct.id]["lost"] is False
    assert not firing(rig)


# ---------------------------------------------------------------- 心跳丢失
async def test_heartbeat_lost_fires_warn_and_reconnects(h08):
    """04 H08:30 s 无心跳 ⇒ warn;自愈动作 = 重连 WS(本地连接,不是重登)。"""
    rig, probe, rec = h08
    rig.bot.push_heartbeat()
    await _settle()
    rig.clock.advance(31_000)
    rig.bot.online = False
    rig.bot.drop()
    await _settle()
    await probe.check()
    assert firing(rig)
    assert rig.alerts.active[(H08_NAPCAT_HEARTBEAT_LOST, f"account:{rig.acct.id}")].severity == "warn"
    assert probe.state_of(rig.acct.id).reconnects == 1
    assert rec.calls == []                                # 未满 2 min,不置 login_required


async def test_heartbeat_lost_but_still_online_stays_warn(h08):
    """心跳丢、备用 ``get_status`` 仍报在线 ⇒ 只 warn(WS 有问题但账号没掉线)。"""
    rig, probe, rec = h08
    rig.clock.advance(31_000)
    await probe.check()
    assert firing(rig)
    assert probe.last[rig.acct.id]["online"] is True
    assert probe.state_of(rig.acct.id).offline_since_ms is None
    assert rec.calls == []


# ---------------------------------------------------------------- 离线 2 分钟 → login_required
async def test_offline_under_two_minutes_is_warn_only(h08):
    rig, probe, rec = h08
    rig.bot.online = False
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    assert rig.alerts.active[(H08_NAPCAT_HEARTBEAT_LOST, f"account:{rig.acct.id}")].severity == "warn"
    assert rec.calls == []
    assert probe.state_of(rig.acct.id).offline_since_ms == rig.clock.now_ms


async def test_offline_two_minutes_escalates_to_crit_and_login_required(h08):
    """04 H08 / F-08:``get_status.online=false`` 持续 2 min ⇒ 转 ``login_required`` 并推事件,**不自动重登**。"""
    rig, probe, rec = h08
    rig.bot.online = False
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    rig.clock.advance(H08_OFFLINE_TO_LOGIN_REQUIRED_S * 1000)
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    alert = rig.alerts.active[(H08_NAPCAT_HEARTBEAT_LOST, f"account:{rig.acct.id}")]
    assert alert.severity == "crit" and alert.evidence["offline_s"] >= 120
    assert rec.calls == [(rig.acct.id, "TOKEN_EXPIRED")]
    # R6-56 ⑦:同键级别翻转 = 状态变化,再发一次 firing
    states = [(p["state"], p["severity"]) for p in alert_events(rig)]
    assert states == [("firing", "warn"), ("firing", "crit")]


async def test_login_required_is_pushed_only_once(h08):
    rig, probe, rec = h08
    rig.bot.online = False
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    for _ in range(3):
        rig.clock.advance(H08_OFFLINE_TO_LOGIN_REQUIRED_S * 1000)
        await probe.check()
    assert rec.calls == [(rig.acct.id, "TOKEN_EXPIRED")]


async def test_recovery_resolves_alert_and_resets_state(h08):
    rig, probe, rec = h08
    rig.bot.online = False
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    assert firing(rig)
    rig.bot.online = True
    rig.bot.push_heartbeat()
    await _settle()
    await probe.check()
    assert not firing(rig)
    st = probe.state_of(rig.acct.id)
    assert st.offline_since_ms is None and st.login_required_sent is False
    assert alert_events(rig)[-1]["state"] == "resolved"


# ---------------------------------------------------------------- 备用 HTTP 探针
async def test_http_status_probe_used_when_ws_action_fails(tmp_path):
    """04 H08 备用通道:WS ``get_status`` 取不到时走 ``GET http://127.0.0.1:162NN/get_status``。"""
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)
    hits = []

    async def http_probe(acct):
        hits.append(status_url(acct.id))
        return {"online": False}

    rec = Recorder()
    probe = QQHealth(adapter=r.adapter, store=r.store, alerts=r.alerts, cfg=r.cfg,
                     on_login_required=rec, status_probe=http_probe, clock=r.clock)
    r.bot.fail_actions["get_status"] = 1404
    r.clock.advance(31_000)
    await probe.check()
    assert hits == ["http://127.0.0.1:16203/get_status"]
    assert probe.last[r.acct.id]["online"] is False
    await r.adapter.close()
    r.close()


async def test_http_probe_exception_does_not_kill_round(tmp_path):
    r = make_qq_rig(tmp_path, qq_cfg=QQAdapterConfig(reconnect_delay_s=0))
    await r.adapter.start(r.acct)

    async def boom(acct):
        raise RuntimeError("探针炸了")

    probe = QQHealth(adapter=r.adapter, store=r.store, alerts=r.alerts, cfg=r.cfg, status_probe=boom, clock=r.clock)
    r.bot.fail_actions["get_status"] = 1404
    r.clock.advance(31_000)
    await probe.check()
    assert probe.last[r.acct.id]["online"] is None
    await r.adapter.close()
    r.close()


# ---------------------------------------------------------------- 观察范围
async def test_account_without_session_is_skipped(tmp_path):
    """还没 start(适配器没建连)⇒ 不是 H08 的事。"""
    r = make_qq_rig(tmp_path)
    probe = QQHealth(adapter=r.adapter, store=r.store, alerts=r.alerts, cfg=r.cfg, clock=r.clock)
    await probe.check()
    assert probe.last == {} and not r.alerts.active
    r.close()


async def test_busy_account_is_skipped(h08):
    rig, _, _ = h08
    probe = QQHealth(adapter=rig.adapter, store=rig.store, alerts=rig.alerts, cfg=rig.cfg,
                     busy=lambda aid: True, clock=rig.clock)
    rig.clock.advance(31_000)
    await probe.check()
    assert probe.last == {}


async def test_desired_state_not_running_is_skipped(h08):
    rig, probe, _ = h08
    rig.store.upsert_runtime(rig.acct.id, kind="napcat", desired_state="stopped")
    rig.clock.advance(31_000)
    await probe.check()
    assert probe.last == {}


async def test_disabled_account_is_skipped(h08):
    rig, probe, _ = h08
    rig.store.transition(rig.acct.id, "running", enabled=False)
    rig.clock.advance(31_000)
    await probe.check()
    assert probe.last == {}


async def test_stopped_account_is_skipped(h08):
    rig, probe, _ = h08
    rig.store.transition(rig.acct.id, "stopped")
    rig.clock.advance(31_000)
    await probe.check()
    assert probe.last == {}


async def _settle():
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0)
