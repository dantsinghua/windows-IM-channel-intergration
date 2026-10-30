"""R6-118 临时目录卷告警 ``WINAGENT_TEMP_DISK_LOW``(04 §2.3 R6-118 段;02 §3.7;DECISION-3 T3/T4/T10、B4)
与 R6-118 配置键默认值(02 §7.2 / 07 总表)。

取样一律注入假探针,不读本机真实 ``%TEMP%`` 卷。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from qtrade_winagent import __version__, alerts as A, tempvol
from qtrade_winagent.config import MonitorConfig, WechatConfig, load
from qtrade_winagent.main_svc import sync_temp_volume
from qtrade_winagent.user import UserAgent

from tests.conftest import INSTALL_SID, build_rig

GiB = 1 << 30
CODE = "WINAGENT_TEMP_DISK_LOW"
THRESHOLD = 20 * GiB


# ---------------------------------------------------------------- 配置键默认值与 02 §7.2 一致
def test_wechat_recycle_and_log_defaults_match_02_7_2():
    c = WechatConfig()
    assert c.chatlog_recycle_copy_bytes == 4294967296
    assert c.chatlog_recycle_max_uptime_s == 7200
    assert c.chatlog_recycle_min_free_bytes == 21474836480
    assert c.chatlog_recycle_idle_wait_s == 30
    assert c.chatlog_log_max_bytes == 67108864
    assert c.chatlog_log_backups == 5


def test_monitor_temp_volume_default_matches_02_7_2_and_04_7():
    assert MonitorConfig().temp_volume_warn_free_bytes == 21474836480


def test_r6118_keys_load_from_winagent_toml_sections():
    cfg = load({"wechat": {"chatlog_recycle_copy_bytes": 1, "chatlog_recycle_max_uptime_s": 2,
                           "chatlog_recycle_min_free_bytes": 3, "chatlog_recycle_idle_wait_s": 4,
                           "chatlog_log_max_bytes": 5, "chatlog_log_backups": 6},
                "monitor": {"temp_volume_warn_free_bytes": 7}})
    w = cfg.wechat
    assert (w.chatlog_recycle_copy_bytes, w.chatlog_recycle_max_uptime_s, w.chatlog_recycle_min_free_bytes,
            w.chatlog_recycle_idle_wait_s, w.chatlog_log_max_bytes, w.chatlog_log_backups) == (1, 2, 3, 4, 5, 6)
    assert cfg.monitor.temp_volume_warn_free_bytes == 7


def test_code_registered_as_warn():
    assert A.REGISTERED[CODE] == "warn"


# ---------------------------------------------------------------- Monitor:发生 / 恢复 / subject / evidence / 恒 warn
@pytest.fixture
def rig_plain(tmp_path):
    r = build_rig(tmp_path)
    yield r
    r.db.close()


def emitted(rig, code=CODE):
    return [a["payload"] for a in rig.deps.alerts.pull()["alerts"] if a["payload"]["code"] == code]


def test_low_free_fires_warn_with_volume_subject_and_evidence(rig_plain):
    m = rig_plain.deps.monitor
    m.check_temp_volume(volume="C:", free_bytes=THRESHOLD - 1)
    rows = emitted(rig_plain)
    assert len(rows) == 1
    p = rows[0]
    assert p["state"] == "firing" and p["severity"] == "warn" and p["subject"] == "volume:C:"
    assert p["evidence"] == {"free_bytes": THRESHOLD - 1, "threshold_bytes": THRESHOLD}
    assert not p["hint_actions"]


def test_never_escalates_to_crit_and_not_h12(rig_plain):
    m = rig_plain.deps.monitor
    m.check_temp_volume(volume="C:", free_bytes=0)
    m.check_temp_volume(volume="C:", free_bytes=0)
    assert all(p["severity"] == "warn" for p in emitted(rig_plain))
    assert rig_plain.deps.alerts.active[(CODE, "volume:C:")].severity == "warn"
    assert not emitted(rig_plain, A.H12_DISK_LOW)                     # 不进 H12 三级水位


@pytest.mark.parametrize("recovered", [THRESHOLD, THRESHOLD + 1])
def test_next_sample_at_or_above_threshold_resolves(rig_plain, recovered):
    m = rig_plain.deps.monitor
    m.check_temp_volume(volume="C:", free_bytes=THRESHOLD - 1)
    m.check_temp_volume(volume="C:", free_bytes=recovered)
    rows = emitted(rig_plain)
    assert [r["state"] for r in rows] == ["firing", "resolved"]
    assert rows[-1]["subject"] == "volume:C:"
    assert not rig_plain.deps.alerts.is_firing(CODE, "volume:C:")


def test_at_threshold_never_fires(rig_plain):
    rig_plain.deps.monitor.check_temp_volume(volume="C:", free_bytes=THRESHOLD)
    assert not emitted(rig_plain)


@pytest.mark.parametrize("volume,free", [(None, 1), ("", 1), ("C:", None), ("C:", -1), ("C:", "1"), ("C:", True)])
def test_invalid_sample_leaves_alert_state_unchanged(rig_plain, volume, free):
    m = rig_plain.deps.monitor
    m.check_temp_volume(volume="C:", free_bytes=1)
    before = list(rig_plain.deps.alerts.active)
    m.check_temp_volume(volume=volume, free_bytes=free)
    assert list(rig_plain.deps.alerts.active) == before
    assert [r["state"] for r in emitted(rig_plain)] == ["firing"]


def test_threshold_follows_monitor_config(tmp_path):
    from qtrade_winagent.config import WinAgentConfig
    rig = build_rig(tmp_path, cfg=WinAgentConfig(monitor=MonitorConfig(temp_volume_warn_free_bytes=1000)))
    try:
        rig.deps.monitor.check_temp_volume(volume="D:", free_bytes=999)
        p = emitted(rig)[0]
        assert p["subject"] == "volume:D:" and p["evidence"] == {"free_bytes": 999, "threshold_bytes": 1000}
    finally:
        rig.db.close()


# ---------------------------------------------------------------- T4:会话代理取样,经管道交服务出告警
class Probe:
    def __init__(self, volume="C:", free=THRESHOLD - 1):
        self.volume, self.free, self.calls = volume, free, 0

    def __call__(self):
        self.calls += 1
        return {"volume": self.volume, "free_bytes": self.free, "total_bytes": 10 * THRESHOLD}


async def attach_with_probe(rig, probe):
    await rig.deps.hub.start()
    ua = UserAgent(rig.deps.cfg, pipe=rig.pipe, wsl=rig.wsl, wechat=rig.wechat, power=rig.power_backend,
                   session_id="Console", user_sid=INSTALL_SID, version=__version__,
                   backup_dir=rig.deps.installer._wsl_backup_dir, clock=rig.clock, temp_probe=probe)   # noqa: SLF001
    await ua.start()
    rig._tasks.append(asyncio.create_task(ua.run()))                   # noqa: SLF001
    await asyncio.sleep(0)
    rig.user_agent = ua
    return ua


@pytest.fixture
async def rig_probe(tmp_path):
    r = build_rig(tmp_path)
    probe = Probe()
    await attach_with_probe(r, probe)
    yield r, probe
    for t in r._tasks:                                                 # noqa: SLF001
        t.cancel()
    await r.deps.hub.stop()
    r.db.close()


async def test_service_samples_session_temp_volume_via_pipe_and_fires_then_resolves(rig_probe):
    rig, probe = rig_probe
    await sync_temp_volume(rig.deps)
    assert probe.calls == 1                                            # 取样发生在会话代理一侧
    assert rig.deps.alerts.is_firing(CODE, "volume:C:")
    probe.free = THRESHOLD + 5
    await sync_temp_volume(rig.deps)
    assert [r["state"] for r in emitted(rig)] == ["firing", "resolved"]


async def test_alert_reaches_agent_pull_endpoint(rig_probe):
    from tests.conftest import AGENT_TOKEN, client
    rig, _ = rig_probe
    await sync_temp_volume(rig.deps)
    async with client(rig, token=AGENT_TOKEN) as c:
        body = (await c.get("/wa/v1/alerts?since=0")).json()
    codes = [(a["payload"]["code"], a["payload"]["subject"]) for a in body["alerts"]]
    assert (CODE, "volume:C:") in codes


async def test_user_agent_offline_keeps_alert_state(tmp_path):
    rig = build_rig(tmp_path)
    try:
        rig.deps.monitor.check_temp_volume(volume="C:", free_bytes=1)
        await sync_temp_volume(rig.deps)                               # 会话代理不在线:不抛、不改状态
        assert rig.deps.alerts.is_firing(CODE, "volume:C:")
        assert [r["state"] for r in emitted(rig)] == ["firing"]
    finally:
        await rig.deps.hub.stop()
        rig.db.close()


async def test_probe_failure_keeps_alert_state(rig_probe):
    rig, probe = rig_probe
    await sync_temp_volume(rig.deps)

    def broken():
        raise OSError("temp dir gone")

    rig.user_agent._temp_probe = broken                                 # noqa: SLF001
    await sync_temp_volume(rig.deps)
    assert rig.deps.alerts.is_firing(CODE, "volume:C:")
    assert [r["state"] for r in emitted(rig)] == ["firing"]


# ---------------------------------------------------------------- 卷 = 会话代理(chatlog 继承)的 %TEMP% 所在卷
@pytest.mark.skipif(os.name != "nt", reason="GetTempPath 顺序只在 Windows 有意义")
def test_user_temp_dir_follows_gettemppath_order(tmp_path, monkeypatch):
    a, b = tmp_path / "tmp-a", tmp_path / "temp-b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("TMP", str(a))
    monkeypatch.setenv("TEMP", str(b))
    assert os.path.normcase(tempvol.user_temp_dir()) == os.path.normcase(str(a))
    monkeypatch.delenv("TMP")
    assert os.path.normcase(tempvol.user_temp_dir()) == os.path.normcase(str(b))


@pytest.mark.skipif(os.name != "nt", reason="Windows 卷根")
def test_volume_of_returns_drive_root_on_windows(tmp_path):
    assert tempvol.volume_of("c:\\Users\\x\\AppData\\Local\\Temp") == "C:"
    s = tempvol.sample(str(tmp_path))
    assert s["volume"] == os.path.splitdrive(str(tmp_path))[0].upper() and s["free_bytes"] >= 0
