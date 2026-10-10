"""资源池自校准(04 §2.5.3,里程碑 M2.9):五个量的测法、只上调不自动下调、``apply=False`` 不写库、
写回只动 ``resource_pools`` 并推 ``resource`` 事件、零账号 ≥5 min 自动重测、预算 vs 实占漂移 >30% 持续 1 h。"""
from __future__ import annotations

import json

from qtrade_agent.events import Events
from qtrade_agent.pool_calibrate import (CalibrationConfig, POOL_CALIBRATION_DRIFT, PoolCalibrator, RESERVED_BUFFER_MB,
                                         WECHAT_BUFFER_MB, mean, p95)

MIN_MS = 60_000


class StubPool:
    """只为断言「apply 后推了 resource 事件」;真 ``Pool.snapshot()`` 的形态由 tests/test_pool.py 守。"""

    def __init__(self):
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {"pools": {"wsl": {"total_mb": 11264}}, "realtime": {}, "quota_mb": {}, "can_add": {}}


def _sample(store, *, ts_ms, scope, subject, mem_mb=None, anon=None, total=None, resolution="raw"):
    store.con.execute("INSERT INTO health_samples(ts_ms, resolution, scope, subject, mem_mb, mem_anon_mb, mem_max_mb) "
                      "VALUES (?,?,?,?,?,?,?)", (ts_ms, resolution, scope, subject, mem_mb, anon, total))


def _pools(store, clock):
    store.ensure_pools(quota={"qidian": 2560, "qq": 614, "wechat": 1536}, wsl_total_mb=11264, wsl_reserved_mb=2048,
                       windows_total_mb=16384, windows_reserved_mb=4096, now_ms=clock())


def _cal(store, clock, **kw):
    return PoolCalibrator(store, clock=clock, **kw)


# ---------------------------------------------------------------- 统计小工具
def test_p95_is_nearest_rank_and_degrades_to_max_on_small_samples():
    assert p95([]) is None
    assert p95([5]) == 5
    assert p95([1, 2, 3]) == 3
    assert p95(list(range(1, 101))) == 96
    assert mean([2, 4]) == 3 and mean([]) is None


# ---------------------------------------------------------------- wsl.reserved_mb
def test_wsl_reserved_is_used_plus_own_procs_plus_512(store, clock):
    now = clock()
    for i in range(5):
        _sample(store, ts_ms=now - i * MIN_MS, scope="wsl", subject="wsl", mem_mb=1000 + i * 100, total=11264)
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="dockerd", mem_mb=200)
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="qtrade-agent", mem_mb=100)
    value, ev = _cal(store, clock).suggest_wsl_reserved_mb(now_ms=now)
    assert value == round(1200 + 300 + RESERVED_BUFFER_MB)            # 均值 1200 + 两个进程 300 + 512
    assert ev["buffer_mb"] == RESERVED_BUFFER_MB and ev["window_min"] == 5
    assert ev["own_procs_mb"] == {"dockerd": 200.0, "qtrade-agent": 100.0}


def test_wsl_reserved_without_samples_is_none(store, clock):
    value, ev = _cal(store, clock).suggest_wsl_reserved_mb(now_ms=clock())
    assert value is None and ev["reason"] == "no_wsl_samples"


def test_wsl_total_comes_from_memtotal_sample(store, clock):
    now = clock()
    _sample(store, ts_ms=now - MIN_MS, scope="wsl", subject="wsl", mem_mb=1000, total=11264)
    assert _cal(store, clock).suggest_wsl_total_mb(now_ms=now) == 11264


# ---------------------------------------------------------------- quota_mb
def test_quota_skips_account_not_stable_for_10min(store, clock):
    now = clock()
    for i in range(3):
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=2600, anon=2300)
    value, detail = _cal(store, clock).suggest_quota_mb("qidian", now_ms=now)
    assert value is None and detail["qd01"]["skipped"] == "not_stable_10min"


def test_quota_is_anon_p95_plus_start_peak_buffer_and_max_across_accounts(store, clock):
    now = clock()
    store.ensure_account("qd02", "qidian", state="running")
    _sample(store, ts_ms=now - 30 * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=3800, anon=2300)  # 冷启峰值
    for i in range(11):
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=2600, anon=2300)
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd02", mem_mb=2000, anon=1800)
    value, detail = _cal(store, clock).suggest_quota_mb("qidian", now_ms=now)
    assert detail["qd01"]["anon_p95_mb"] == 2300 and detail["qd01"]["start_peak_mb"] == 3800
    assert detail["qd01"]["quota_mb"] == 3800 and detail["qd02"]["quota_mb"] == 2000
    assert value == 3800                                              # 同通道取最大值


def test_wechat_mb_is_p95_of_two_process_rss_sum_plus_256(store, clock):
    now = clock()
    for i in range(11):
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="Weixin.exe", mem_mb=800)
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="chatlog.exe", mem_mb=200)
    value, ev = _cal(store, clock).suggest_wechat_mb(now_ms=now)
    assert value == 1000 + WECHAT_BUFFER_MB and ev["rss_sum_p95_mb"] == 1000
    assert ev["procs"] == ["Weixin.exe", "chatlog.exe"]


def test_windows_reserved_is_used_minus_vmmem_minus_wechat(store, clock):
    now = clock()
    for i in range(5):
        _sample(store, ts_ms=now - i * MIN_MS, scope="host", subject="host", mem_mb=10000, total=16384)
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="vmmem", mem_mb=6000)
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="Weixin.exe", mem_mb=800)
    total, reserved, ev = _cal(store, clock).suggest_windows(now_ms=now)
    assert total == 16384 and reserved == 10000 - 6000 - 800
    assert ev["vmmem_mb_mean"] == 6000.0


# ---------------------------------------------------------------- apply / 不 apply
def test_calibrate_without_apply_does_not_touch_db(store, clock):
    _pools(store, clock)
    now = clock()
    for i in range(11):
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=3000, anon=2900)
        _sample(store, ts_ms=now - i * MIN_MS, scope="wsl", subject="wsl", mem_mb=1000, total=11264)
    s = _cal(store, clock).calibrate(apply=False, now_ms=now)
    assert s.quota_mb["qidian"] == 3000
    assert store.pool_get("wsl")["calibrated_ms"] is None
    assert store.pool_get("wsl")["quota"]["qidian"] == 2560           # 库里没动
    assert s.as_dict()["quota_mb"]["qidian"] == 3000


def test_apply_writes_resource_pools_and_emits_resource_event(store, clock):
    _pools(store, clock)
    now = clock()
    events, pool = Events(store), StubPool()
    for i in range(11):
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=3000, anon=2900)
        _sample(store, ts_ms=now - i * MIN_MS, scope="wsl", subject="wsl", mem_mb=1000, total=12000)
        _sample(store, ts_ms=now - i * MIN_MS, scope="process", subject="dockerd", mem_mb=100)
    cal = _cal(store, clock, events=events, pool=pool)
    cal.calibrate(apply=True, now_ms=now)
    row = store.pool_get("wsl")
    assert row["quota"]["qidian"] == 3000 and row["calibrated_ms"] == now
    assert row["source"] == "calibrated"                              # 02 §3.1 CHECK 值域(04 的 'auto' 不在其中)
    assert row["total_mb"] == 12000 and row["reserved_mb"] == 1000 + 100 + RESERVED_BUFFER_MB
    assert json.loads(row["calibration_json"])["quota_mb"]["qidian"] == 3000
    assert pool.calls == 1 and store.list_events(event="resource")[-1]["payload"]["pools"]["wsl"]["total_mb"] == 11264
    assert [a["action"] for a in store.list_audit("resources.calibrate")] == ["resources.calibrate"]


def test_quota_only_goes_up_unless_auto_lower(store, clock):
    _pools(store, clock)
    now = clock()
    for i in range(11):
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=1000, anon=900)
    cal = _cal(store, clock)
    cal.calibrate(apply=True, now_ms=now)
    assert store.pool_get("wsl")["quota"]["qidian"] == 2560           # 建议 1000 < 2560,不下调
    lower = _cal(store, clock, cfg=CalibrationConfig(quota_auto_lower=True))
    lower.calibrate(apply=True, now_ms=now)
    assert store.pool_get("wsl")["quota"]["qidian"] == 1000


def test_agent_toml_is_never_written(store, clock, tmp_path):
    """C-43:自校准只写 ``resource_pools``,绝不改 ``agent.toml``(A2-01 通过判据)。"""
    toml = tmp_path / "agent.toml"
    toml.write_text("[pool]\nquota_qidian_mb = 2560\n")
    before = toml.read_text()
    _pools(store, clock)
    now = clock()
    for i in range(11):
        _sample(store, ts_ms=now - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=3000, anon=2900)
    _cal(store, clock).calibrate(apply=True, now_ms=now)
    assert toml.read_text() == before


# ---------------------------------------------------------------- 零账号自动重测(A2-01)
def test_zero_account_for_5min_triggers_auto_recalibration(store, clock):
    _pools(store, clock)
    now = clock()
    for i in range(5):
        _sample(store, ts_ms=now - i * MIN_MS, scope="wsl", subject="wsl", mem_mb=900, total=11264)
    cal = _cal(store, clock)
    cal.note_running_count(0, now_ms=now)
    assert cal.maybe_auto_calibrate(now_ms=now) is None                # 刚进零账号态,不重测
    assert cal.maybe_auto_calibrate(now_ms=now + 5 * MIN_MS) is not None
    assert store.pool_get("wsl")["reserved_mb"] == 900 + RESERVED_BUFFER_MB
    cal.note_running_count(1, now_ms=now + 6 * MIN_MS)                 # 有账号跑起来 → 计时清零
    assert cal.zero_account_since_ms is None
    assert cal.maybe_auto_calibrate(now_ms=now + 99 * MIN_MS) is None


# ---------------------------------------------------------------- 漂移告警
def _fresh(store, at_ms: int, *, qd: float, qq: float) -> None:
    """在 ``at_ms`` 之前的 10 min 窗口里铺样本 —— 真机 Sampler 每 10 s 一批,任何检查时刻窗口里都有新读数。
    (R6-92:此前用例只在开头铺一次,+61 min 时窗口为空,靠「没样本 = 实占 0」的缺陷才「通过」。)"""
    for i in range(11):
        _sample(store, ts_ms=at_ms - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=qd + 100, anon=qd)
        _sample(store, ts_ms=at_ms - i * MIN_MS, scope="container", subject="qtrade-qq03", mem_mb=qq + 50, anon=qq)


def test_drift_fires_only_after_one_hour_and_resolves(store, clock):
    _pools(store, clock)
    events = Events(store)
    now = clock()
    _fresh(store, now, qd=500, qq=150)
    cal = _cal(store, clock, events=events)
    assert cal.check_drift(now_ms=now) is None                         # 第一次只记起点
    _fresh(store, now + 59 * MIN_MS, qd=500, qq=150)
    assert cal.check_drift(now_ms=now + 59 * MIN_MS) is None            # 不足 1 h 不发
    _fresh(store, now + 61 * MIN_MS, qd=500, qq=150)
    fired = cal.check_drift(now_ms=now + 61 * MIN_MS)
    assert fired["code"] == POOL_CALIBRATION_DRIFT and fired["severity"] == "info"
    assert fired["subject"] == "pool" and fired["state"] == "firing"
    assert fired["evidence"]["threshold_pct"] == 30 and fired["evidence"]["budget_mb"] == 2560 + 614
    assert store.list_events(event="resource")[-1]["payload"]["code"] == POOL_CALIBRATION_DRIFT
    assert cal.check_drift(now_ms=now + 62 * MIN_MS) is None            # 已在 firing,不重复发

    for i in range(11):                                                 # 实占追上预算 → resolved
        _sample(store, ts_ms=now + 70 * MIN_MS - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=2600, anon=2500)
        _sample(store, ts_ms=now + 70 * MIN_MS - i * MIN_MS, scope="container", subject="qtrade-qq03", mem_mb=650, anon=600)
    assert cal.check_drift(now_ms=now + 70 * MIN_MS) is None
    assert store.list_events(event="resource")[-1]["payload"]["state"] == "resolved"


def test_drift_without_any_container_sample_is_not_judged(store, clock):
    """R6-92(2026-10-10 真机):采样缺失时此前把预算计入、实占记 0 ⇒ 漂移恒 100%,满 1 h 必报。没有读数 = 不能判。"""
    _pools(store, clock)
    events = Events(store)
    cal = _cal(store, clock, events=events)
    now = clock()
    for t in (now, now + 61 * MIN_MS, now + 125 * MIN_MS):
        assert cal.check_drift(now_ms=t) is None
    assert not [e for e in store.list_events(event="resource") if e["payload"].get("code") == POOL_CALIBRATION_DRIFT]


def test_drift_ignores_unsampled_account_instead_of_counting_zero(store, clock):
    """只有部分容器有样本时,没样本的那个不计入预算也不计入实占(否则把预算白白拉大、漂移被夸大)。"""
    _pools(store, clock)
    now = clock()
    for t in (now, now + 61 * MIN_MS):
        for i in range(11):
            _sample(store, ts_ms=t - i * MIN_MS, scope="container", subject="qtrade-qd01", mem_mb=2600, anon=2500)
    cal = _cal(store, clock, events=Events(store))
    cal.check_drift(now_ms=now)
    assert cal.check_drift(now_ms=now + 61 * MIN_MS) is None          # qd01 2500/2560 在阈值内;qq03 无样本不计


def test_drift_payload_has_chinese_title_and_numbers(store, clock):
    _pools(store, clock)
    now = clock()
    _fresh(store, now, qd=500, qq=150)
    cal = _cal(store, clock, events=Events(store))
    cal.check_drift(now_ms=now)
    _fresh(store, now + 61 * MIN_MS, qd=500, qq=150)
    fired = cal.check_drift(now_ms=now + 61 * MIN_MS)
    assert fired["title"] == "资源配额与实际占用偏差较大"
    assert "3174 MB" in fired["message"] and "资源页" in fired["message"] and fired["hint_actions"] == ["calibrate"]


def test_drift_without_running_accounts_is_noop(store, clock):
    _pools(store, clock)
    store.transition("qd01", "stopped")
    store.transition("qq03", "stopped")
    assert _cal(store, clock).check_drift(now_ms=clock()) is None
