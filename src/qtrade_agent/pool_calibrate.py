"""资源池自校准(04 §2.5.3「资源池的输入:测算与自校准」;里程碑 **M2.9**,R3-29;端点 #71/#25、预检 #70)。

规格逐条(04 §2.5.3 表):

| 量 | 测法 | 写回 |
|---|---|---|
| ``wsl.total_mb`` | S11 ``MemTotal``(**不是** ``.wslconfig`` 文件值——文件改了没重启不算) | 每次 WSL 启动写 ``resource_pools`` |
| ``wsl.reserved_mb`` | **零账号**时 ``MemTotal − MemAvailable`` + dockerd/containerd/Agent RSS 的 **5 分钟均值 + 512 MB 缓冲** | 首次 = 安装自检;之后每次「零账号运行」持续 **≥5 min** 自动重测 |
| ``quota_mb.qidian/qq`` | 每账号 ``running`` 稳定 **≥10 min** 后取 ``anon`` 的 **P95(10 min 窗)+ 启动峰值缓冲**(冷启动 ``memory.current`` 峰值 − 稳态 ``anon``);同通道多账号取**最大值**;**只上调不自动下调** | ``resource_pools.quota_json`` + 推 ``resource`` 事件 |
| ``windows.wechat_mb`` | S6 两进程 RSS 之和的 P95(10 min 窗)+ **256 MB** | 同上 |
| ``windows.reserved_mb`` | ``total − available − vmmem − 微信侧`` 的稳态均值(「除我们之外的一切」) | 同上 |
| ``windows.total_mb`` | S2 ``total`` | 安装时一次 |

- **写回只走 ``resource_pools`` 表**,**绝不写 ``agent.toml``**(C-43:``[pool]`` 只是首次启动的初始默认)。
- ``used_mb`` 按**预算**算(C.3.3),不按实时值;预算 vs 实占**差 > ``calibration_drift_warn_pct``(30%)持续 1 h**
  → ``info`` 告警 **``POOL_CALIBRATION_DRIFT``**(02 §3.7:``subject='pool'``、事件族 ``resource``)。
- 验收 04 §8b.2:A2-01 零账号 ≥5 min 自动重测并推 ``resource`` 事件、``agent.toml`` 未被改写;
  A2-03 ``POST /resources/calibrate`` 不带 ``apply`` **只返回建议值不写库**,带 ``apply=true`` 才写并推事件
  (``calibrated_ms`` 只在第二次变化)。

样本来源 = ``health_samples`` 窄表(04 §2.8.3:同一秒采 N 个容器,只有窄表能查 P95);本模块**只读**它,
不采样(采样归 monitor,本期未接 —— 测试直接往表里插行)。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("qtrade.pool_calibrate")

POOL_CALIBRATION_DRIFT = "POOL_CALIBRATION_DRIFT"     # 02 §3.7:info / subject='pool' / 事件族 resource

# 04 §2.5.3 的三个字面缓冲量(不是配置项,逐字来自表格)
RESERVED_BUFFER_MB = 512          # wsl.reserved_mb 的 +512 MB 缓冲
WECHAT_BUFFER_MB = 256            # windows.wechat_mb 的 +256 MB
RESERVED_MEAN_WINDOW_MIN = 5      # 「5 分钟均值」与「零账号持续 ≥5min 自动重测」同一个 5
QUOTA_STABLE_MIN = 10             # 「running 稳定 ≥10min 后」
DRIFT_SUSTAIN_MS = 3600_000       # 「持续 1h」

# 04 S6:微信侧两进程(4.x 与 3.x 名字不同)
WECHAT_PROCS = ("Weixin.exe", "WeChat.exe")
CHATLOG_PREFIX = "chatlog"
# 04 §2.5.3:WSL 侧「我们自己」的三个进程
WSL_OWN_PROCS = ("dockerd", "containerd", "qtrade-agent")


@dataclass(frozen=True)
class CalibrationConfig:
    """02 §7.1 ``[pool]`` 的自校准四键(逐字默认值;04 §2.5.3 owner)。"""
    calibration_window_min: int = 10
    calibration_drift_warn_pct: int = 30
    quota_auto_lower: bool = False              # 自校准只上调,下调需人工(P-SET 确认)
    autocalibrate_on_first_login: bool = True


def p95(values: list[float]) -> Optional[float]:
    """最近邻序位 P95(样本少时退化为最大值);空集回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(0.95 * len(ordered) + 0.5)) - 1))
    return float(ordered[idx])


def mean(values: list[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


@contextmanager
def _tx(store) -> Iterator[Any]:
    """薄封装:复用 ``Store`` 自己的事务与写锁(文件所有权约束:不往 ``store/`` 里加方法)。"""
    with store._tx() as c:                       # noqa: SLF001 - 见 docstring
        yield c


@dataclass
class Suggestion:
    """一次校准的建议值;``apply=False`` 时只返回它、不碰库(04 §8b.2 A2-03)。"""
    wsl_total_mb: Optional[int] = None
    wsl_reserved_mb: Optional[int] = None
    windows_total_mb: Optional[int] = None
    windows_reserved_mb: Optional[int] = None
    quota_mb: dict[str, int] = None                   # type: ignore[assignment]
    evidence: dict[str, Any] = None                   # type: ignore[assignment]
    skipped: dict[str, str] = None                    # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.quota_mb = self.quota_mb or {}
        self.evidence = self.evidence or {}
        self.skipped = self.skipped or {}

    def as_dict(self) -> dict[str, Any]:
        return {"wsl": {"total_mb": self.wsl_total_mb, "reserved_mb": self.wsl_reserved_mb},
                "windows": {"total_mb": self.windows_total_mb, "reserved_mb": self.windows_reserved_mb},
                "quota_mb": dict(self.quota_mb), "evidence": dict(self.evidence), "skipped": dict(self.skipped)}


class PoolCalibrator:
    """资源池自校准(04 §2.5.3)。

    ``pool`` 可选:给了就在 ``apply`` 后推 ``resource`` 事件(payload = 00 §7.5「§7.6 + accounts + realtime」,
    即 ``pool.snapshot()``);``alerts`` 本模块不用 —— ``POOL_CALIBRATION_DRIFT`` 的事件族是 ``resource`` 而非
    ``alert``,故直接经 ``events.emit('resource', …)`` 发告警类 payload(00 §7.5 C-14 统一结构),
    firing/resolved 的去重键 ``(code, subject)`` 在本模块内自己维护。
    """

    def __init__(self, store, *, cfg: Optional[CalibrationConfig] = None, pool=None, events=None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._store = store
        self.cfg = cfg or CalibrationConfig()
        self._pool = pool
        self._events = events
        self._clock = clock
        self.zero_account_since_ms: Optional[int] = None      # 「零账号运行」起点(≥5min 才自动重测)
        self.last_auto_ms: Optional[int] = None
        self._drift_since_ms: Optional[int] = None
        self._drift_firing = False

    # ------------------------------------------------------------ 样本读取(health_samples 窄表,只读)
    def _samples(self, *, scope: str, since_ms: int, subject: Optional[str] = None,
                 subject_like: Optional[str] = None, resolution: str = "raw") -> list[dict[str, Any]]:
        sql = ("SELECT * FROM health_samples WHERE scope=? AND resolution=? AND ts_ms >= ?")
        params: list[Any] = [scope, resolution, since_ms]
        if subject is not None:
            sql += " AND subject=?"
            params.append(subject)
        if subject_like is not None:
            sql += " AND subject LIKE ?"
            params.append(subject_like)
        return [dict(r) for r in self._store.con.execute(sql + " ORDER BY ts_ms", params)]

    @staticmethod
    def _col(rows: list[dict[str, Any]], col: str) -> list[float]:
        return [float(r[col]) for r in rows if r.get(col) is not None]

    def _subjects(self, scope: str, since_ms: int) -> list[str]:
        rows = self._store.con.execute(
            "SELECT DISTINCT subject FROM health_samples WHERE scope=? AND ts_ms >= ?", (scope, since_ms)).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------ 五个量
    def suggest_wsl_reserved_mb(self, *, now_ms: int) -> tuple[Optional[int], dict[str, Any]]:
        """零账号时 ``MemTotal − MemAvailable`` + 我们自己三个进程 RSS 的 **5 分钟均值 + 512**。"""
        since = now_ms - RESERVED_MEAN_WINDOW_MIN * 60_000
        wsl_rows = self._samples(scope="wsl", subject="wsl", since_ms=since)
        used = mean(self._col(wsl_rows, "mem_mb"))
        if used is None:
            return None, {"reason": "no_wsl_samples"}
        own = 0.0
        per_proc: dict[str, float] = {}
        for name in WSL_OWN_PROCS:
            m = mean(self._col(self._samples(scope="process", subject=name, since_ms=since), "mem_mb"))
            if m is not None:
                per_proc[name] = round(m, 1)
                own += m
        value = int(round(used + own + RESERVED_BUFFER_MB))
        return value, {"wsl_used_mb_mean": round(used, 1), "own_procs_mb": per_proc, "buffer_mb": RESERVED_BUFFER_MB,
                       "window_min": RESERVED_MEAN_WINDOW_MIN, "samples": len(wsl_rows)}

    def suggest_wsl_total_mb(self, *, now_ms: int) -> Optional[int]:
        """S11 ``MemTotal``(容器样本的 ``mem_max_mb`` 存整机 total;取窗内最后一帧)。"""
        rows = self._samples(scope="wsl", subject="wsl", since_ms=now_ms - self.cfg.calibration_window_min * 60_000)
        totals = self._col(rows, "mem_max_mb")
        return int(round(totals[-1])) if totals else None

    def suggest_quota_mb(self, channel: str, *, now_ms: int) -> tuple[Optional[int], dict[str, Any]]:
        """同通道各账号:``P95(anon, 10min 窗) + 启动峰值缓冲``,取**最大值**;账号稳定不足 10 min 跳过。"""
        window_ms = self.cfg.calibration_window_min * 60_000
        since = now_ms - window_ms
        best: Optional[int] = None
        detail: dict[str, Any] = {}
        for account_id in self._running_accounts(channel):
            subject = f"qtrade-{account_id}"
            rows = self._samples(scope="container", subject=subject, since_ms=since)
            anon = self._col(rows, "mem_anon_mb")
            if not anon or (rows[-1]["ts_ms"] - rows[0]["ts_ms"]) < QUOTA_STABLE_MIN * 60_000:
                detail[account_id] = {"skipped": "not_stable_10min", "samples": len(anon)}
                continue
            steady = p95(anon) or 0.0
            # 启动峰值缓冲 = 冷启动期 memory.current 峰值 − 稳态 anon(A.5 实测约 +1.3 G)
            peak_rows = self._store.con.execute(
                "SELECT MAX(mem_mb) FROM health_samples WHERE scope='container' AND subject=?", (subject,)).fetchone()
            peak = float(peak_rows[0]) if peak_rows and peak_rows[0] is not None else steady
            value = int(round(steady + max(0.0, peak - steady)))
            detail[account_id] = {"anon_p95_mb": round(steady, 1), "start_peak_mb": round(peak, 1), "quota_mb": value}
            best = value if best is None else max(best, value)
        if best is None:
            detail["_reason"] = "no_stable_account"
        return best, detail

    def _running_accounts(self, channel: str) -> list[str]:
        rows = self._store.con.execute(
            "SELECT id FROM accounts WHERE channel=? AND state='running' AND deleted_ms IS NULL ORDER BY seq", (channel,)).fetchall()
        return [r[0] for r in rows]

    def suggest_wechat_mb(self, *, now_ms: int) -> tuple[Optional[int], dict[str, Any]]:
        """S6 微信 + chatlog 两进程 RSS 之和的 P95(10 min 窗)+ 256。"""
        since = now_ms - self.cfg.calibration_window_min * 60_000
        by_ts: dict[int, float] = {}
        used: list[str] = []
        for subject in self._subjects("process", since):
            if subject not in WECHAT_PROCS and not subject.lower().startswith(CHATLOG_PREFIX):
                continue
            used.append(subject)
            for r in self._samples(scope="process", subject=subject, since_ms=since):
                if r.get("mem_mb") is not None:
                    by_ts[int(r["ts_ms"])] = by_ts.get(int(r["ts_ms"]), 0.0) + float(r["mem_mb"])
        total_p95 = p95(list(by_ts.values()))
        if total_p95 is None:
            return None, {"reason": "no_wechat_samples"}
        return int(round(total_p95 + WECHAT_BUFFER_MB)), {"rss_sum_p95_mb": round(total_p95, 1),
                                                          "buffer_mb": WECHAT_BUFFER_MB, "procs": sorted(used)}

    def suggest_windows(self, *, now_ms: int) -> tuple[Optional[int], Optional[int], dict[str, Any]]:
        """``windows.total_mb`` = S2 total;``windows.reserved_mb`` = ``used − vmmem − 微信侧`` 的稳态均值。"""
        since = now_ms - self.cfg.calibration_window_min * 60_000
        host = self._samples(scope="host", subject="host", since_ms=since)
        totals, useds = self._col(host, "mem_max_mb"), self._col(host, "mem_mb")
        if not totals or not useds:
            return None, None, {"reason": "no_host_samples"}
        vmmem = mean(self._col(self._samples(scope="process", subject="vmmem", since_ms=since), "mem_mb")) or 0.0
        wechat_mb, wechat_ev = self.suggest_wechat_mb(now_ms=now_ms)
        wechat_side = float(wechat_mb - WECHAT_BUFFER_MB) if wechat_mb is not None else 0.0
        reserved = max(0, int(round((mean(useds) or 0.0) - vmmem - wechat_side)))
        return int(round(totals[-1])), reserved, {"host_used_mb_mean": round(mean(useds) or 0.0, 1),
                                                  "vmmem_mb_mean": round(vmmem, 1),
                                                  "wechat_side_mb": round(wechat_side, 1), "wechat": wechat_ev}

    # ------------------------------------------------------------ 校准(#71 全局 / #25 单账号)
    def calibrate(self, *, apply: bool = False, now_ms: Optional[int] = None, source: str = "auto",
                  channels: tuple[str, ...] = ("qidian", "qq")) -> Suggestion:
        """跑一遍所有运行中账号,返回建议值;``apply=True`` 才写回 ``resource_pools`` 并推 ``resource`` 事件。"""
        now = now_ms if now_ms is not None else self._clock()
        s = Suggestion()
        s.wsl_total_mb = self.suggest_wsl_total_mb(now_ms=now)
        reserved, reserved_ev = self.suggest_wsl_reserved_mb(now_ms=now)
        s.wsl_reserved_mb = reserved
        s.evidence["wsl_reserved"] = reserved_ev
        for ch in channels:
            value, detail = self.suggest_quota_mb(ch, now_ms=now)
            s.evidence[f"quota_{ch}"] = detail
            if value is not None:
                s.quota_mb[ch] = value
            else:
                s.skipped[f"quota_{ch}"] = "no_stable_account"
        wechat_mb, wechat_ev = self.suggest_wechat_mb(now_ms=now)
        s.evidence["quota_wechat"] = wechat_ev
        if wechat_mb is not None:
            s.quota_mb["wechat"] = wechat_mb
        win_total, win_reserved, win_ev = self.suggest_windows(now_ms=now)
        s.windows_total_mb, s.windows_reserved_mb = win_total, win_reserved
        s.evidence["windows"] = win_ev
        if apply:
            self.apply(s, now_ms=now, source=source)
        return s

    def apply(self, s: Suggestion, *, now_ms: Optional[int] = None, source: str = "auto") -> dict[str, Any]:
        """写回 ``resource_pools``(表为真值,C-43);``quota_auto_lower=false`` 时**只上调**。

        🔴 ``source`` 落库取 02 §3.1 的 CHECK 值域 ``('default','winagent','manual','calibrated')`` ——
        04 写的 ``calibration_source=auto`` 不在该值域内,``auto`` 映射为 ``'calibrated'``(见 handoff「规格张力」)。
        """
        now = now_ms if now_ms is not None else self._clock()
        db_source = {"auto": "calibrated", "manual": "manual"}.get(source, "calibrated")
        wsl = self._store.pool_get("wsl") or {}
        quota = dict(wsl.get("quota") or {})
        applied: dict[str, Any] = {"quota_mb": {}, "lowered_skipped": {}}
        for ch, value in s.quota_mb.items():
            old = int(quota.get(ch, 0))
            if value < old and not self.cfg.quota_auto_lower:
                applied["lowered_skipped"][ch] = {"old": old, "suggest": value}    # 只上调,下调需人工
                continue
            quota[ch] = int(value)
            applied["quota_mb"][ch] = int(value)
        with _tx(self._store) as c:
            c.execute("UPDATE resource_pools SET quota_json=?, calibration_json=?, calibrated_ms=?, source=?, updated_ms=? WHERE pool='wsl'",
                      (json.dumps(quota, ensure_ascii=False), json.dumps(s.as_dict(), ensure_ascii=False), now, db_source, now))
            if s.wsl_total_mb is not None:
                c.execute("UPDATE resource_pools SET total_mb=? WHERE pool='wsl'", (int(s.wsl_total_mb),))
            if s.wsl_reserved_mb is not None:
                c.execute("UPDATE resource_pools SET reserved_mb=? WHERE pool='wsl'", (int(s.wsl_reserved_mb),))
            c.execute("UPDATE resource_pools SET quota_json=?, calibration_json=?, calibrated_ms=?, source=?, updated_ms=? WHERE pool='windows'",
                      (json.dumps(quota, ensure_ascii=False), json.dumps(s.as_dict(), ensure_ascii=False), now, db_source, now))
            if s.windows_total_mb is not None:
                c.execute("UPDATE resource_pools SET total_mb=? WHERE pool='windows'", (int(s.windows_total_mb),))
            if s.windows_reserved_mb is not None:
                c.execute("UPDATE resource_pools SET reserved_mb=? WHERE pool='windows'", (int(s.windows_reserved_mb),))
        self._store.insert_audit(kind="system", transport="local", actor="system:pool_calibrate", action="resources.calibrate",
                                 detail={"source": db_source, "applied": applied}, now_ms=now)
        self.last_auto_ms = now if source == "auto" else self.last_auto_ms
        self._emit_resource(now)
        return applied

    def _emit_resource(self, now_ms: int) -> None:
        """00 §7.5:``resource`` payload = §7.6 + ``accounts`` + ``realtime``(= ``pool.snapshot()``)。"""
        if self._events is None or self._pool is None:
            return
        self._events.emit("resource", payload=self._pool.snapshot(), now_ms=now_ms)

    # ------------------------------------------------------------ 零账号自动重测(04 §2.5.3 / A2-01)
    def note_running_count(self, running: int, *, now_ms: Optional[int] = None) -> None:
        """每轮健康探测报一次「当前有几个账号在跑」;零账号状态从 0→>0 会清掉计时。"""
        now = now_ms if now_ms is not None else self._clock()
        if running > 0:
            self.zero_account_since_ms = None
        elif self.zero_account_since_ms is None:
            self.zero_account_since_ms = now

    def maybe_auto_calibrate(self, *, now_ms: Optional[int] = None) -> Optional[Suggestion]:
        """「零账号运行」持续 ≥5 min 即自动重测 ``wsl.reserved_mb`` 并写回(A2-01);否则 None。"""
        now = now_ms if now_ms is not None else self._clock()
        if self.zero_account_since_ms is None or now - self.zero_account_since_ms < RESERVED_MEAN_WINDOW_MIN * 60_000:
            return None
        if self.last_auto_ms is not None and now - self.last_auto_ms < RESERVED_MEAN_WINDOW_MIN * 60_000:
            return None
        reserved, ev = self.suggest_wsl_reserved_mb(now_ms=now)
        if reserved is None:
            return None
        s = Suggestion(wsl_reserved_mb=reserved, wsl_total_mb=self.suggest_wsl_total_mb(now_ms=now),
                       evidence={"wsl_reserved": ev, "trigger": "zero_account_5min"})
        self.apply(s, now_ms=now, source="auto")
        return s

    # ------------------------------------------------------------ 预算 vs 实占漂移(04 §2.5.3 末句)
    def check_drift(self, *, now_ms: Optional[int] = None) -> Optional[dict[str, Any]]:
        """预算(``Σ quota_mb``)与实占(``Σ anon``)差 > ``calibration_drift_warn_pct`` **持续 1 h** → info ``POOL_CALIBRATION_DRIFT``。"""
        now = now_ms if now_ms is not None else self._clock()
        budget = 0
        actual = 0.0
        wsl = self._store.pool_get("wsl") or {}
        quota = wsl.get("quota") or {}
        since = now - self.cfg.calibration_window_min * 60_000
        for ch in ("qidian", "qq"):
            for account_id in self._running_accounts(ch):
                budget += int(quota.get(ch, 0))
                anon = self._col(self._samples(scope="container", subject=f"qtrade-{account_id}", since_ms=since), "mem_anon_mb")
                if anon:
                    actual += p95(anon) or 0.0
        if budget <= 0:
            self._drift_since_ms = None
            return None
        drift_pct = abs(budget - actual) / budget * 100
        if drift_pct <= self.cfg.calibration_drift_warn_pct:
            self._drift_since_ms = None
            if self._drift_firing:
                self._drift_firing = False
                self._emit_drift("resolved", drift_pct, budget, actual, now)
            return None
        if self._drift_since_ms is None:
            self._drift_since_ms = now
            return None
        if now - self._drift_since_ms < DRIFT_SUSTAIN_MS or self._drift_firing:
            return None
        self._drift_firing = True
        return self._emit_drift("firing", drift_pct, budget, actual, now)

    def _emit_drift(self, state: str, drift_pct: float, budget: int, actual: float, now_ms: int) -> dict[str, Any]:
        payload = {"code": POOL_CALIBRATION_DRIFT, "severity": "info", "state": state, "subject": "pool",
                   "title": None, "message": None, "hint_actions": ["calibrate"],
                   "first_seen_at": self._drift_since_ms, "last_seen_at": now_ms, "count": 1,
                   "evidence": {"budget_mb": budget, "actual_mb": round(actual, 1), "drift_pct": round(drift_pct, 1),
                                "threshold_pct": self.cfg.calibration_drift_warn_pct}}
        if self._events is not None:
            self._events.emit("resource", payload=payload, now_ms=now_ms)
        return payload
