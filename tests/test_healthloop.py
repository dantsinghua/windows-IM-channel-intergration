"""账号级健康循环(04 §2.3 H04/H05/H06;02 §5 自愈;R6-32 两个子判据;R6-12 不 kill-server)。"""
from __future__ import annotations

import pytest

from qtrade_agent.alerts import (AUTO_RESTART_EXHAUSTED, CONTAINER_OOM_KILLED, H04_CONTAINER_EXITED, H05_BOOT_INCOMPLETE, H06_ADB_OFFLINE,
                                 QIDIAN_NOT_ROOT)
from qtrade_agent.config import AgentConfig, HealthConfig
from tests.conftest import Clock, make_rig

S = "127.0.0.1:16001"


async def _running_account(rig, id="qd01"):
    """经正常序列起到 login_required,再直接置 running(执行层未接)。"""
    st = rig.store
    row = st.create_account(channel="qidian", label=id, login_mode="password", quota_mb=2560)
    aid = row["id"]
    await rig.agent.accounts.start(aid, actor="t")
    await rig.agent.accounts.wait_idle(aid)
    rig.agent.accounts.transition(aid, "running")
    return aid


# ---------------------------------------------------------------- H04
async def test_h04_container_exit_sets_error_alerts_and_restarts_with_backoff(tmp_path):
    clock = Clock(auto_step_ms=0)
    rig = make_rig(tmp_path, clock=clock)
    hl, st, containers, alerts = rig.agent.healthloop, rig.store, rig.containers, rig.agent.alerts
    aid = await _running_account(rig)
    await hl.check_containers()
    assert st.get_account_full(aid)["state"] == "running" and not alerts.active
    containers.containers["qtrade-qd01"].running = False
    containers.containers["qtrade-qd01"].exit_code = 137
    containers.containers["qtrade-qd01"].oom_killed = True
    await hl.check_containers()
    a = st.get_account_full(aid)
    assert a["state"] == "error" and a["state_code"] == "CONTAINER_EXIT" and a["runtime_error_since_ms"]
    assert alerts.active[(H04_CONTAINER_EXITED, "account:qd01")].severity == "crit"
    assert alerts.active[(CONTAINER_OOM_KILLED, "account:qd01")].severity == "warn"
    assert alerts.active[(CONTAINER_OOM_KILLED, "account:qd01")].evidence["memory_max_mb"] == 3584
    starts_before = [c for c in containers.calls if c == ("start", "qtrade-qd01")]
    await hl.check_containers()                                                    # 退避 60 s 未到:不拉
    assert [c for c in containers.calls if c == ("start", "qtrade-qd01")] == starts_before
    clock.advance(61_000)
    await hl.check_containers()                                                    # 到点:自动 start(走正常序列)
    await rig.agent.accounts.wait_idle(aid)
    assert len([c for c in containers.calls if c == ("start", "qtrade-qd01")]) == len(starts_before) + 1
    assert hl.restart[aid].count == 1
    a = st.get_account_full(aid)
    assert a["state"] == "login_required" and a["runtime_error_since_ms"] is None      # 离开 error 清 NULL
    await hl.check_containers()
    assert not alerts.is_firing(H04_CONTAINER_EXITED, "account:qd01")               # 容器回来 → resolved
    rig.store.close()


async def test_h04_restart_limit_stops_self_heal_and_hourly_reset(tmp_path):
    clock = Clock(auto_step_ms=0)
    cfg = AgentConfig(health=HealthConfig(container_restart_backoff_s=(1, 1, 1, 1), container_restart_max=2))
    rig = make_rig(tmp_path, cfg=cfg, clock=clock)
    hl, containers, alerts = rig.agent.healthloop, rig.containers, rig.agent.alerts
    aid = await _running_account(rig)
    containers.fail_start.add("qtrade-qd01")                                       # 坏实例:每次 start 都失败
    containers.containers["qtrade-qd01"].running = False
    for _ in range(6):
        await hl.check_containers()
        await rig.agent.accounts.wait_idle(aid)
        clock.advance(1_500)
    assert hl.restart[aid].count == 2 and hl.restart[aid].exhausted is True         # 达上限停止自愈
    n_starts = len([c for c in containers.calls if c == ("start", "qtrade-qd01")])
    await hl.check_containers()
    clock.advance(1_500)
    await hl.check_containers()
    assert len([c for c in containers.calls if c == ("start", "qtrade-qd01")]) == n_starts
    assert alerts.active[(H04_CONTAINER_EXITED, "account:qd01")].evidence["exhausted"] is True
    a = alerts.active[(AUTO_RESTART_EXHAUSTED, "account:qd01")]                  # 02 §3.7 独立码(R6-57 ②)
    assert a.severity == "crit" and a.evidence["restart_count"] == 2 and a.evidence["max"] == 2
    clock.advance(3600_000)                                                        # 每小时清零 → 再给机会
    await hl.check_containers()
    assert hl.restart[aid].count == 0 and hl.restart[aid].exhausted is False
    assert not alerts.is_firing(AUTO_RESTART_EXHAUSTED, "account:qd01")
    rig.store.close()


# ---------------------------------------------------------------- H06
async def test_h06_offline_reconnects_then_ensure_root_and_three_strikes_crit(tmp_path):
    clock = Clock(auto_step_ms=0)
    rig = make_rig(tmp_path, clock=clock)
    hl, adb, alerts, st = rig.agent.healthloop, rig.adb, rig.agent.alerts, rig.store
    aid = await _running_account(rig)
    clock.advance(20_000)                                                          # 出启动时 ensure_root 的宽限窗
    adb.calls.clear()
    await hl.check_adb()
    assert not alerts.active and hl.h06_fail_streak.get(aid, 0) == 0
    assert ("shell", f"{S}: whoami") in adb.calls                                  # (b) root 态每轮查
    adb.state[S] = "offline"
    adb.calls.clear()
    await hl.check_adb()
    assert hl.h06_fail_streak[aid] == 1 and alerts.active[(H06_ADB_OFFLINE, "account:qd01")].severity == "warn"
    assert adb.calls[:2] == [("devices", ""), ("disconnect", S)] and ("connect", S) in adb.calls and ("root", S) in adb.calls   # 重连后复提权
    assert not any("kill" in k for k, _ in adb.calls)
    # 重连成功且 ensure_root 开始 → 宽限窗内下一轮不判定 (a)
    adb.state[S] = "offline"
    await hl.check_adb()
    assert hl.h06_fail_streak[aid] == 1 and hl.last["H06"][aid]["skipped"] == "rooting_grace"
    clock.advance(16_000)
    adb.state[S] = "offline"
    await hl.check_adb()
    clock.advance(16_000)
    adb.state[S] = "offline"
    await hl.check_adb()
    a = alerts.active[(H06_ADB_OFFLINE, "account:qd01")]
    assert hl.h06_fail_streak[aid] == 3 and a.severity == "crit" and a.evidence["h06_fail_streak"] == 3
    ev = [e["payload"]["severity"] for e in st.list_events(event="alert") if e["payload"]["code"] == H06_ADB_OFFLINE]
    assert ev == ["warn", "crit"]                                                  # 级别翻转再发一次 firing
    assert st.get_account_full(aid)["state"] == "running"                          # (a) 三振只告警,状态由 H04/人处理
    clock.advance(16_000)
    await hl.check_adb()                                                           # 已回 device(connect 成功)→ 清零 + resolved
    assert hl.h06_fail_streak[aid] == 0 and not alerts.is_firing(H06_ADB_OFFLINE, "account:qd01")
    rig.store.close()


async def test_h06_root_state_is_independent_and_never_crit(tmp_path):
    clock = Clock(auto_step_ms=0)
    rig = make_rig(tmp_path, clock=clock)
    hl, adb, alerts = rig.agent.healthloop, rig.adb, rig.agent.alerts
    aid = await _running_account(rig)
    adb.whoami_after_root[S] = "shell"
    adb._rooted.discard(S)
    for i in range(4):
        clock.advance(20_000)
        await hl.check_adb()
    a = alerts.active[(QIDIAN_NOT_ROOT, "account:qd01")]
    assert a.severity == "warn" and rig.agent.runtime.qidian_root_fail_streak[aid] >= 3
    assert hl.h06_fail_streak.get(aid, 0) == 0 and not alerts.is_firing(H06_ADB_OFFLINE, "account:qd01")   # (b) 不并进 (a)
    assert rig.store.get_account_full(aid)["state"] == "running"
    rig.store.close()


# ---------------------------------------------------------------- H05 稳态
async def test_h05_steady_boot_lost_and_back(tmp_path):
    rig = make_rig(tmp_path)
    hl, adb, alerts = rig.agent.healthloop, rig.adb, rig.agent.alerts
    aid = await _running_account(rig)
    await hl.check_boot()
    assert not alerts.is_firing(H05_BOOT_INCOMPLETE, "account:qd01")
    adb.boot_completed[S] = "0"
    await hl.check_boot()
    assert alerts.active[(H05_BOOT_INCOMPLETE, "account:qd01")].severity == "crit"
    adb.boot_completed[S] = "1"
    await hl.check_boot()
    assert not alerts.is_firing(H05_BOOT_INCOMPLETE, "account:qd01")
    checks = hl.checks()
    assert checks == {"H04": "unknown", "H05": "ok", "H06": "unknown"}
    rig.store.close()


def test_scheduler_registers_health_jobs_with_doc_intervals(rig3):
    jobs = rig3.agent.scheduler.jobs
    assert jobs["health_containers"].interval_s == 10 and jobs["health_adb"].interval_s == 30 and jobs["health_boot"].interval_s == 60
    assert jobs["login_remind"].interval_s == 60
