"""monitor 回归(评审 A3 / B3):

- A3:受检目录「在不在」必须经**注入的 ``SysBackend.path_exists``** 判,不能直接 ``os.path.exists``
  (那样 Linux 上假盘 ``C:\\...`` / ``D:\\`` 全被跳过;真机上也绕过了后端)。
- B3:``host_snapshot`` 首轮采样前不能回全 0;缓存带时间戳,超过 3 个快采样周期即过期、当场现采。
"""
from __future__ import annotations

import os

from qtrade_winagent import alerts as A
from qtrade_winagent.alerts import AlertBuffer
from qtrade_winagent.backends import ProcInfo
from qtrade_winagent.config import AlertConfig, MonitorConfig
from qtrade_winagent.db import Db
from qtrade_winagent.fakes import FakeProbe, FakeSys
from qtrade_winagent.monitor import HOST_CACHE_STALE_INTERVALS, DiskTarget, Monitor

from tests.conftest import Clock

PD = "C:\\ProgramData\\QTrade"
VHDX = "C:\\Users\\vhdx"
WX = "D:\\"


def mk():
    clk = Clock()
    db = Db(":memory:", clock=clk).open()
    sysb = FakeSys()
    ab = AlertBuffer(AlertConfig(), clock=clk)
    m = Monitor(db, sysb, ab, MonitorConfig(), probe=FakeProbe(), clock=clk,
                disks=[DiskTarget("programdata", PD), DiskTarget("vhdx", VHDX),
                       DiskTarget("wechat_data", WX, product_level=False)])
    return db, sysb, ab, m, clk


def _disk_subjects(db) -> set[str]:
    return {r["subject"] for r in db.query("SELECT subject FROM health_samples WHERE scope='disk'")}


# ---------------------------------------------------------------- A3
def test_disk_existence_goes_through_sysbackend_not_os(monkeypatch):
    """即便宿主 ``os.path.exists`` 一律说「不存在」,假盘仍按 SysBackend 的答案被采样。"""
    db, _s, _ab, m, _clk = mk()
    monkeypatch.setattr(os.path, "exists", lambda _p: False)
    m.sample_slow()
    assert _disk_subjects(db) == {"programdata", "vhdx", "wechat_data"}
    db.close()


def test_missing_path_reported_by_sysbackend_is_skipped():
    db, sysb, _ab, m, _clk = mk()
    sysb.missing_paths = {WX}
    m.sample_slow()
    assert _disk_subjects(db) == {"programdata", "vhdx"}
    db.close()


def test_missing_product_partition_does_not_drive_level():
    """产品级分区之一不存在 ⇒ 不进最小值集合(不因为读不到就当 0 触发 critical)。"""
    db, sysb, ab, m, _clk = mk()
    sysb.disks = {PD: (40000.0, 244140.0), VHDX: (500.0, 244140.0)}
    sysb.missing_paths = {VHDX}
    out = m.check_disks()
    assert out["level"] == "ok" and out["worst"] == "programdata"
    assert not ab.is_firing(A.H12_DISK_LOW, "host")
    db.close()


def test_missing_wechat_root_neither_raises_nor_fires():
    db, sysb, ab, m, _clk = mk()
    sysb.missing_paths = {WX}
    out = m.check_disks(wechat_data_path=WX)
    assert out["wechat_free_mb"] is None
    assert not ab.is_firing(A.WECHAT_DISK_LOW, "host")
    db.close()


def test_present_wechat_root_low_still_fires():
    db, sysb, ab, m, _clk = mk()
    sysb.disks = {PD: (40000.0, 244140.0), VHDX: (40000.0, 244140.0), WX: (500.0, 500000.0)}
    m.check_disks(wechat_data_path=WX)
    assert ab.is_firing(A.WECHAT_DISK_LOW, "host")
    db.close()


# ---------------------------------------------------------------- B3
def _procs(vm: float, wx: float, cl: float) -> dict[str, ProcInfo]:
    return {"vmmemWSL": ProcInfo(9, "vmmemWSL", vm), "Weixin.exe": ProcInfo(10, "Weixin.exe", wx),
            "chatlog.exe": ProcInfo(11, "chatlog.exe", cl)}


def test_host_snapshot_before_first_sample_is_real_not_zero():
    db, sysb, _ab, m, _clk = mk()
    sysb.procs = _procs(8000.0, 600.0, 100.0)
    snap = m.host_snapshot()
    assert snap["wsl_vm_mb"] == 8000.0 and snap["wechat_mb"] == 600.0 and snap["chatlog_mb"] == 100.0
    assert snap["total_mb"] == sysb.total_mb and snap["available_mb"] == sysb.available_mb
    db.close()


def test_host_snapshot_uses_fresh_cache_without_rescanning():
    db, sysb, _ab, m, clk = mk()
    sysb.procs = _procs(8000.0, 600.0, 100.0)
    m.sample_fast()
    sysb.procs = _procs(1.0, 1.0, 1.0)                           # 采样之后变了,但缓存仍新鲜
    clk.advance(MonitorConfig().sample_interval_s * 1000)
    assert m.host_snapshot()["wsl_vm_mb"] == 8000.0
    db.close()


def test_host_snapshot_resamples_when_cache_stale():
    db, sysb, _ab, m, clk = mk()
    sysb.procs = _procs(8000.0, 600.0, 100.0)
    m.sample_fast()
    sysb.procs = _procs(9000.0, 700.0, 50.0)
    sysb.available_mb = 1234.0
    clk.advance(MonitorConfig().sample_interval_s * 1000 * HOST_CACHE_STALE_INTERVALS + 1)   # 采样线程停摆
    snap = m.host_snapshot()
    assert snap["wsl_vm_mb"] == 9000.0 and snap["wechat_mb"] == 700.0 and snap["available_mb"] == 1234.0
    db.close()


def test_host_snapshot_wsl_vm_override_does_not_pollute_cache():
    db, sysb, _ab, m, _clk = mk()
    sysb.procs = _procs(8000.0, 600.0, 100.0)
    m.sample_fast()
    assert m.host_snapshot(wsl_vm_mb=42.0)["wsl_vm_mb"] == 42.0
    assert m.host_snapshot()["wsl_vm_mb"] == 8000.0
    db.close()
