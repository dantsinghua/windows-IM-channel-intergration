"""企点 adb 通道自愈(R6-111):H06 连续失败达阈值后自动重启 adb server + 重连全部企点 serial。

根因:adb server ↔ 容器 adbd 长连接僵死后协议层故意无 kill_server(backends.py:4),H06 只 disconnect/connect
单账号 serial(healthloop.py:259-261),不触碰 server;僵死后无任何恢复手段,读库永久不可用。
"""
from __future__ import annotations

import pytest

from qtrade_agent.alerts import H06_ADB_OFFLINE
from tests.conftest import Clock, make_rig

S = "127.0.0.1:16001"


async def _running_account(rig, id="qd01"):
    st = rig.store
    row = st.create_account(channel="qidian", label=id, login_mode="password", quota_mb=2560)
    aid = row["id"]
    await rig.agent.accounts.start(aid, actor="t")
    await rig.agent.accounts.wait_idle(aid)
    rig.agent.accounts.transition(aid, "running")
    return aid


async def test_h06_streak3_triggers_kill_server_and_reconnect(tmp_path):
    """H06 连续 3 次 offline → 自动 kill_server + reconnect 全部企点 serial(R6-111 自愈)。"""
    clock = Clock(auto_step_ms=0)
    rig = make_rig(tmp_path, clock=clock)
    hl, adb, alerts, st = rig.agent.healthloop, rig.adb, rig.agent.alerts, rig.store
    aid = await _running_account(rig)
    clock.advance(20_000)

    # 模拟 adb 通道僵死:连续 3 轮 offline(disconnect+connect 单账号无法恢复)
    adb.state[S] = "offline"
    adb.calls.clear()
    for i in range(3):
        await hl.check_adb()
        clock.advance(16_000)
        adb.state[S] = "offline"  # reconnect 后仍 offline(僵死)

    # streak 达 3 应触发 kill_server
    kill_calls = [c for c in adb.calls if c[0] == "kill_server"]
    assert len(kill_calls) >= 1, f"streak>=3 应触发 kill_server,实际 calls={adb.calls}"
    # kill_server 后应 reconnect 全部企点 serial
    connect_after_kill = [c for c in adb.calls if c[0] == "connect" and c[1] == S]
    assert len(connect_after_kill) >= 1, "kill_server 后应重连 serial"
    rig.store.close()


async def test_kill_server_restores_device_state(tmp_path):
    """kill_server + reconnect 后 FakeAdb 应恢复 device 态(不再设 offline)。"""
    clock = Clock(auto_step_ms=0)
    rig = make_rig(tmp_path, clock=clock)
    hl, adb, st = rig.agent.healthloop, rig.adb, rig.store
    aid = await _running_account(rig)
    clock.advance(20_000)
    adb.state[S] = "offline"
    for i in range(3):
        if i < 2:
            adb.state[S] = "offline"  # 前 2 轮仍 offline(单账号 reconnect 无效)
        await hl.check_adb()
        clock.advance(16_000)
    # 第 3 轮触发 kill_server + reconnect_all → state 恢复 device(不再强制 offline)
    assert adb.state.get(S) == "device", f"kill+reconnect 后应恢复 device,实际 {adb.state.get(S)}"
    rig.store.close()


async def test_h06_below_threshold_no_kill_server(tmp_path):
    """streak < 3 不触发 kill_server(只 disconnect+connect 单账号,保持旧行为)。"""
    clock = Clock(auto_step_ms=0)
    rig = make_rig(tmp_path, clock=clock)
    hl, adb, st = rig.agent.healthloop, rig.adb, rig.store
    aid = await _running_account(rig)
    clock.advance(20_000)
    adb.state[S] = "offline"
    adb.calls.clear()
    await hl.check_adb()  # streak=1
    clock.advance(16_000)
    kill_calls = [c for c in adb.calls if c[0] == "kill_server"]
    assert len(kill_calls) == 0, "streak<3 不应 kill_server"
    rig.store.close()
