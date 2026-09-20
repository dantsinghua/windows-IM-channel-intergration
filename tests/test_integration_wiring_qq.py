"""第五批接线:QQ 首登序列(05 §2.3.1 ⑤/⑤a/⑤b/⑦)与 H08 挂钩(04 §2.3)。

开发容器里只用 ``FakeOneBot``,绝不连真 QQ / NapCat(SKILL §4 禁区)。
"""
from __future__ import annotations

import pytest

from qtrade_agent.adapters.qq import H08_NAPCAT_HEARTBEAT_LOST
from tests.test_integration_wiring_common import close_rig, make_rig


def _new_qq(rig, aid: str = "qq01") -> dict:
    rig.store.ensure_account(aid, "qq", state="stopped", login_mode="qrcode")
    rig.store.upsert_runtime(aid, kind="napcat", desired_state="stopped")
    return rig.store.get_account_full(aid)


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    yield r
    close_rig(r)


@pytest.fixture
def offline_rig(tmp_path):
    r = make_rig(tmp_path, qq_online=False)
    yield r
    close_rig(r)


# ---------------------------------------------------------------- ⑤a 免扫码命中
async def test_qq_start_reaches_running_and_fills_identity(rig):
    """⑤ 建连 → ⑤a ``qq_quick_login_wait_s`` 内 ``get_state`` 回 running → ⑦ ``get_login_info`` 回填 ``self_uid``/``self_nick``。"""
    _new_qq(rig)
    await rig.agent.accounts.start("qq01", actor="token:console")
    await rig.agent.accounts.wait_idle("qq01")
    row = rig.store.get_account_full("qq01")
    assert row["state"] == "running"
    assert row["self_uid"] == "415011447" and row["self_nick"]
    assert rig.agent.adapters["qq"].session_of("qq01") is not None      # 连接建在适配器里、不在 accounts 里
    states = [e["payload"]["state"] for e in rig.events_of("account_state") if e["account_id"] == "qq01"]
    assert states[:2] == ["provisioning", "starting"] and states[-1] == "running"   # 序列不跳段(00 §8.1)


# ---------------------------------------------------------------- ⑤b 窗内没登上
async def test_qq_start_falls_back_to_wait_qrcode(offline_rig):
    """⑤b:``qq_quick_login_wait_s`` 内拿不到 ``user_id`` ⇒ ``login_required(WAIT_QRCODE)`` 等人,**不自动重登**(D-2)。"""
    rig = offline_rig
    _new_qq(rig)
    # AccountsConfig 是 frozen 且默认窗口 20 s,循环里 sleep(1) 是真等 ⇒ 把窗口压到 0 只验「窗内没登上走哪条分支」
    object.__setattr__(rig.agent.cfg.accounts, "qq_quick_login_wait_s", 0)
    await rig.agent.accounts.start("qq01", actor="token:console")
    await rig.agent.accounts.wait_idle("qq01")
    row = rig.store.get_account_full("qq01")
    assert row["state"] == "login_required" and row["state_code"] == "WAIT_QRCODE"
    assert rig.agent.accounts.prompt("qq01")["kind"] == "WAIT_QRCODE"


async def test_qq_stop_closes_the_onebot_connection(rig):
    _new_qq(rig)
    await rig.agent.accounts.start("qq01", actor="token:console")
    await rig.agent.accounts.wait_idle("qq01")
    assert rig.agent.adapters["qq"].session_of("qq01") is not None
    await rig.agent.stop()
    assert rig.agent.adapters["qq"].session_of("qq01") is None


# ---------------------------------------------------------------- H08 挂钩(04 §2.3)
async def test_h08_job_is_registered_and_quiet_when_healthy(rig):
    _new_qq(rig)
    await rig.agent.accounts.start("qq01", actor="token:console")
    await rig.agent.accounts.wait_idle("qq01")
    assert await rig.agent.scheduler.run_once("health_napcat") is True
    assert not rig.agent.alerts.is_firing(H08_NAPCAT_HEARTBEAT_LOST, "account:qq01")


async def test_h08_offline_two_minutes_turns_login_required(rig):
    """04 H08 / F-08:``get_status.online=false`` 持续 2 min ⇒ 转 ``login_required``(``TOKEN_EXPIRED``),**不自动重登**。"""
    _new_qq(rig)
    await rig.agent.accounts.start("qq01", actor="token:console")
    await rig.agent.accounts.wait_idle("qq01")
    rig.bots["qq01"].online = False
    rig.clock.advance(31_000)                      # 超过 [health] napcat_heartbeat_timeout_s=30 ⇒ 判心跳丢 → 探 get_status
    await rig.agent.scheduler.run_once("health_napcat")
    assert rig.agent.alerts.is_firing(H08_NAPCAT_HEARTBEAT_LOST, "account:qq01")
    assert rig.store.get_account_full("qq01")["state"] == "running"     # 未满 2 min:只告警,不动状态
    rig.clock.advance(121_000)
    await rig.agent.scheduler.run_once("health_napcat")
    row = rig.store.get_account_full("qq01")
    assert row["state"] == "login_required" and row["state_code"] == "TOKEN_EXPIRED"


# ---------------------------------------------------------------- 能力目录 × 通道(R6-57 ⑧)
def test_qq_capabilities_exclude_screenshot(rig):
    """``capabilities/screenshot.json`` 的 ``channels.qq`` 已改 ``not_applicable``(napcat 容器没有画面),
    故 QQ 的静态能力集里不含 ``screenshot``,#20 矩阵也给 ``not_applicable``。"""
    _new_qq(rig)
    assert "screenshot" not in rig.agent.adapters["qq"].capabilities
    m = rig.agent.accounts.capabilities_of("qq01")
    assert m["matrix"]["screenshot"] == "not_applicable" and "screenshot" not in m["capabilities"]
