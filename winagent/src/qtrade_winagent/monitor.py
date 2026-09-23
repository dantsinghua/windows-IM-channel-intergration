"""``monitor`` 模块(**服务**;02 §2.4 / 04 §2.2 采样项 / §2.3 健康清单 / §3.1 ``health_samples``)。

职责三件:
1. **采样**:04 §2.2 的 Windows 侧项 S1/S2/S3/S4/S5/S6/S13/S14/S15,10s 与 60s 两个周期;
   60s 项**直接以 ``1m`` 粒度落库、不进 raw**(04 §2.2「周期、保留与降采样」最后一行)。
2. **三级降采样**:raw → ``1m``(avg/max)→ ``1h``,整分/整点做;保留期由 ``db.retention_sweep`` 删。
3. **健康清单里属于 WinAgent 的那些**:H01 / H09~H11 / H12 / H14~H16 / H20 / H21 / H23
   (04 §2.3;判据、周期、阈值以 04 为准,本模块只实现)。

🔴 两条容易写错的:
- **H12 产品级三级水位只取我方三分区**(R4-9:①WSL ``/var/lib/qtrade`` ②``%ProgramData%\\QTrade`` ③docker vhdx);
  **微信数据根只发独立 warn ``WECHAT_DISK_LOW``,绝不驱动 high/critical 降级**——那是用户自己的盘。
- **H01 探 Agent 不带 Bearer**(C-33:WinAgent 不持有 Agent 令牌),连续 3 次失败才 crit;
  失败后再试 WSL IP 那一路,``127.0.0.1`` 不通而 WSL IP 通 ⇒ H15 而不是 H01。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import alerts as A
from .alerts import AlertBuffer
from .backends import ProbeBackend, SysBackend
from .config import MonitorConfig
from .db import Db
from .logfmt import get_logger

log = get_logger("monitor")

MINUTE_MS = 60_000
HOUR_MS = 3_600_000

# 04 §2.2 S6:微信 4.x / 3.x 进程名不同,两名都找;chatlog 与控制台同批
WATCHED_PROCESSES = ("Weixin.exe", "WeChat.exe", "chatlog.exe", "QTrade Console.exe", "vmmem", "vmmemWSL")
WECHAT_PROCESS_NAMES = ("Weixin.exe", "WeChat.exe")
VMMEM_NAMES = ("vmmem", "vmmemWSL")                     # Win10 是 vmmem,Win11 是 vmmemWSL,两名都找(S5)

FAIL_STREAK_CRIT = 3                                    # 04 H01/H02:连续 3 次失败才 crit(R-09 去抖)

# 02 §3.6 #2 / R6-58 (ao):`GET /wa/v1/health.checks` 恒八键(逐字按文档顺序 H01/H09–H11/H14–H16/H20;
# H12 等模块内部还有别的健康项,但不进本端点的 checks——那些不是 (ao) 钉的这八个)
HEALTH_CHECK_KEYS = ("H01", "H09", "H10", "H11", "H14", "H15", "H16", "H20")


@dataclass
class DiskTarget:
    """H12 的受检目录。``product_level=True`` 才进产品级三级水位的最小值集合(R4-9)。"""
    key: str
    path: str
    product_level: bool = True


@dataclass
class MonitorState:
    """进程内内存态(不落库):连续失败计数、上次版本、最近一次各项结论。"""
    agent_fail_streak: int = 0
    agent_reachable_via: Optional[str] = None           # "loopback" | "wsl_ip" | None
    wechat_version_seen: Optional[str] = None
    last_sample_ms: int = 0
    last_slow_sample_ms: int = 0
    last_aggregate_minute: int = 0
    last_aggregate_hour: int = 0
    checks: dict[str, Any] = field(default_factory=dict)


class Monitor:
    def __init__(self, db: Db, sys: SysBackend, alerts: AlertBuffer, cfg: MonitorConfig, *,
                 probe: Optional[ProbeBackend] = None, disks: Optional[list[DiskTarget]] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._sys = sys
        self._alerts = alerts
        self._cfg = cfg
        self._probe = probe
        self._disks = disks or [DiskTarget("programdata", "C:\\ProgramData\\QTrade"),
                                DiskTarget("vhdx", "C:\\Users\\vhdx"),
                                DiskTarget("wechat_data", "D:\\", product_level=False)]
        self._clock = clock
        self.state = MonitorState()
        self._host_cache: Optional[dict[str, float]] = None

    # ---------------------------------------------------------------- 落库
    def write_sample(self, *, resolution: str, scope: str, subject: str, ts_ms: Optional[int] = None,
                     agg_max: Optional[dict[str, Any]] = None, **metrics: Any) -> None:
        """写一行 ``health_samples``(02 §3.2 列集;``extra_json`` 收纳不在固定列里的量)。"""
        import json
        cols = ("cpu_pct", "mem_mb", "mem_anon_mb", "mem_max_mb", "disk_free_mb", "disk_total_mb",
                "net_rx_kbps", "net_tx_kbps")
        vals = {c: metrics.pop(c, None) for c in cols}
        with self._db.tx() as con:
            con.execute(
                "INSERT INTO health_samples(ts_ms, resolution, scope, subject, cpu_pct, mem_mb, mem_anon_mb, mem_max_mb, "
                "disk_free_mb, disk_total_mb, net_rx_kbps, net_tx_kbps, extra_json, agg_max_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts_ms if ts_ms is not None else self._clock(), resolution, scope, subject,
                 vals["cpu_pct"], vals["mem_mb"], vals["mem_anon_mb"], vals["mem_max_mb"], vals["disk_free_mb"],
                 vals["disk_total_mb"], vals["net_rx_kbps"], vals["net_tx_kbps"],
                 json.dumps(metrics, ensure_ascii=False),
                 json.dumps(agg_max, ensure_ascii=False) if agg_max is not None else None))

    # ---------------------------------------------------------------- 采样
    def sample_fast(self) -> None:
        """10s 项:S1 CPU、S2 内存、S4 网络吞吐、S5 vmmem、S6 微信/chatlog/控制台、S13 锁屏。"""
        now = self._clock()
        mem = self._sys.memory_mb()
        self.write_sample(resolution="raw", scope="host", subject="host", ts_ms=now,
                          cpu_pct=self._sys.cpu_percent(), mem_mb=mem["available_mb"], mem_max_mb=mem["total_mb"],
                          committed_mb=mem.get("committed_mb"), session_locked=self._sys.session_locked())
        for iface, (rx, tx) in self._sys.net_throughput_kbps().items():
            self.write_sample(resolution="raw", scope="net", subject=iface, ts_ms=now, net_rx_kbps=rx, net_tx_kbps=tx)
        procs = {p.name: p for p in self._sys.processes(WATCHED_PROCESSES)}
        for p in procs.values():
            scope = "wsl" if p.name in VMMEM_NAMES else "process"
            self.write_sample(resolution="raw", scope=scope, subject=p.name, ts_ms=now,
                              cpu_pct=p.cpu_pct, mem_mb=p.rss_mb, pid=p.pid)
        self._host_cache = self._host_from(mem, procs)
        self.state.last_sample_ms = now

    def sample_slow(self) -> None:
        """60s 项:S3 磁盘余量(去重后采)。**60s 项直接以 ``1m`` 粒度落库、不进 raw**(04 §2.2)。"""
        now = self._clock()
        seen: set[str] = set()
        for t in self._disks:
            if t.path in seen or not os.path.exists(t.path):
                continue
            seen.add(t.path)
            free, total = self._sys.disk_free_mb(t.path)
            self.write_sample(resolution="1m", scope="disk", subject=t.key, ts_ms=now,
                              disk_free_mb=free, disk_total_mb=total, path=t.path, product_level=t.product_level)
        self.state.last_slow_sample_ms = now

    # ---------------------------------------------------------------- 降采样(整分 / 整点)
    def aggregate(self, *, now_ms: Optional[int] = None) -> dict[str, int]:
        """raw → ``1m``、``1m`` → ``1h``,各取 avg 入固定列、max 入 ``agg_max_json``(02 §3.2 ``agg_max_json`` 用途)。"""
        now = self._clock() if now_ms is None else now_ms
        made = {"1m": self._roll("raw", "1m", MINUTE_MS, now), "1h": self._roll("1m", "1h", HOUR_MS, now)}
        return made

    def _roll(self, src: str, dst: str, span_ms: int, now: int) -> int:
        """把 ``src`` 里**已经过完的整窗**聚合成一行 ``dst``;已存在同窗同 (scope,subject) 的行不重复做。"""
        import json
        boundary = (now // span_ms) * span_ms
        rows = self._db.query(
            "SELECT scope, subject, (ts_ms / ?) AS bucket, "
            "AVG(cpu_pct) a_cpu, MAX(cpu_pct) m_cpu, AVG(mem_mb) a_mem, MAX(mem_mb) m_mem, "
            "AVG(disk_free_mb) a_df, MIN(disk_free_mb) m_df, AVG(disk_total_mb) a_dt, "
            "AVG(net_rx_kbps) a_rx, MAX(net_rx_kbps) m_rx, AVG(net_tx_kbps) a_tx, MAX(net_tx_kbps) m_tx "
            "FROM health_samples WHERE resolution=? AND ts_ms < ? GROUP BY scope, subject, bucket",
            (span_ms, src, boundary))
        n = 0
        for r in rows:
            ts = int(r["bucket"]) * span_ms
            if self._db.one("SELECT id FROM health_samples WHERE resolution=? AND scope=? AND subject=? AND ts_ms=?",
                            (dst, r["scope"], r["subject"], ts)):
                continue
            with self._db.tx() as con:
                con.execute(
                    "INSERT INTO health_samples(ts_ms, resolution, scope, subject, cpu_pct, mem_mb, disk_free_mb, "
                    "disk_total_mb, net_rx_kbps, net_tx_kbps, extra_json, agg_max_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, dst, r["scope"], r["subject"], r["a_cpu"], r["a_mem"], r["a_df"], r["a_dt"], r["a_rx"], r["a_tx"],
                     "{}", json.dumps({"cpu_pct": r["m_cpu"], "mem_mb": r["m_mem"], "disk_free_mb_min": r["m_df"],
                                       "net_rx_kbps": r["m_rx"], "net_tx_kbps": r["m_tx"]}, ensure_ascii=False)))
            n += 1
        return n

    # ---------------------------------------------------------------- #5 GET /wa/v1/metrics
    def metrics(self, *, scope: Optional[str] = None, subject: Optional[str] = None, since: Optional[int] = None,
                until: Optional[int] = None, resolution: str = "raw", limit: int = 2000) -> list[dict[str, Any]]:
        import json
        sql = "SELECT * FROM health_samples WHERE resolution=?"
        params: list[Any] = [resolution]
        for col, val in (("scope", scope), ("subject", subject)):
            if val:
                sql, _ = sql + f" AND {col}=?", params.append(val)
        if since is not None:
            sql, _ = sql + " AND ts_ms >= ?", params.append(since)
        if until is not None:
            sql, _ = sql + " AND ts_ms <= ?", params.append(until)
        sql += " ORDER BY ts_ms ASC LIMIT ?"
        params.append(max(1, min(int(limit), 20000)))
        out = []
        for r in self._db.query(sql, tuple(params)):
            r["extra"] = json.loads(r.pop("extra_json") or "{}")
            r["agg_max"] = json.loads(r.pop("agg_max_json") or "null")
            out.append(r)
        return out

    # ---------------------------------------------------------------- 健康项(04 §2.3)
    async def check_agent_api(self, *, loopback_url: str, wsl_url: Optional[str]) -> dict[str, Any]:
        """H01 + H15。**不带 Bearer**(C-33);两路都试,只有两路都失败才计入 ``agent_fail_streak``。"""
        assert self._probe is not None, "check_agent_api 需要 ProbeBackend"
        lo = await self._probe.http("GET", loopback_url, 2.0)
        wsl = await self._probe.http("GET", wsl_url, 2.0) if wsl_url else None
        self.state.agent_reachable_via = "loopback" if lo.ok else ("wsl_ip" if (wsl and wsl.ok) else None)
        if lo.ok:
            self.state.agent_fail_streak = 0
            self._alerts.resolve(A.H01_AGENT_API_DOWN, subject="host")
            self._alerts.resolve(A.H15_LOCALHOST_FORWARD_LOST, subject="host")
        elif wsl is not None and wsl.ok:
            # 127.0.0.1 不通而 WSL IP 通 ⇒ 端口转发失效(睡眠唤醒后常见),是 H15 不是 H01
            self.state.agent_fail_streak = 0
            self._alerts.resolve(A.H01_AGENT_API_DOWN, subject="host")
            self._alerts.firing(A.H15_LOCALHOST_FORWARD_LOST, subject="host",
                                evidence={"loopback": lo.result, "wsl_ip_ok": True},
                                hint_actions=["wsl_restart_when_convenient"])
        else:
            self.state.agent_fail_streak += 1
            if self.state.agent_fail_streak >= FAIL_STREAK_CRIT:
                self._alerts.firing(A.H01_AGENT_API_DOWN, subject="host",
                                    evidence={"fail_streak": self.state.agent_fail_streak, "loopback": lo.result})
        self.state.checks["H01"] = self.state.agent_reachable_via is not None
        self.state.checks["H15"] = not self._alerts.is_firing(A.H15_LOCALHOST_FORWARD_LOST, "host")
        return {"reachable_via": self.state.agent_reachable_via, "fail_streak": self.state.agent_fail_streak}

    def check_session_locked(self) -> bool:
        """H11:会话锁屏 / keep-awake 请求丢失 ⇒ warn(微信账号同时 ``degraded(SCREEN_LOCKED)``,由 Agent 侧处理)。"""
        locked = self._sys.session_locked()
        policy = self._sys.policy_lockscreen()
        if locked:
            self._alerts.firing(A.H11_SESSION_LOCKED, subject="host",
                                evidence={"locked": True, "policy_inactivity_timeout_s": policy})
        else:
            self._alerts.resolve(A.H11_SESSION_LOCKED, subject="host")
        self.state.checks["H11"] = not locked
        return locked

    def check_pending_reboot(self) -> bool:
        """H14:``info`` → 持续升 ``warn``(04 §2.3;这里用「同一 firing 连续存在超过 1 天」判持续)。"""
        pending = self._sys.pending_reboot()
        if pending:
            a = self._alerts.active.get((A.H14_REBOOT_PENDING, "host"))
            sev = "warn" if a is not None and self._clock() - a.first_seen_ms >= 86_400_000 else "info"
            self._alerts.firing(A.H14_REBOOT_PENDING, subject="host", severity=sev, evidence={"pending": True})
        else:
            self._alerts.resolve(A.H14_REBOOT_PENDING, subject="host")
        self.state.checks["H14"] = not pending
        return pending

    def check_bind(self, *, expected: set[str], actual: set[str]) -> bool:
        """H16:vEthernet 地址与监听集合不一致 ⇒ crit + 自愈重绑(重绑动作在 ``netprobe``)。"""
        ok = expected == actual
        if ok:
            self._alerts.resolve(A.H16_WINAGENT_BIND_MISMATCH, subject="host")
        else:
            self._alerts.firing(A.H16_WINAGENT_BIND_MISMATCH, subject="host",
                                evidence={"expected": sorted(expected), "actual": sorted(actual)})
        self.state.checks["H16"] = ok
        return ok

    def check_wechat_version(self, *, exe_path: str, recorded: Optional[str], update_pkg_present: bool) -> dict[str, Any]:
        """H20:``Weixin.exe`` 文件版本 vs ``wechat_install.version``(R6-13:安装级真值在 ``wechat_install``,不是 per-wxid)。

        版本变化 ⇒ **crit**(触发版本匹配);只是有待装更新包 ⇒ ``warn``。
        """
        current = self._sys.file_version(exe_path)
        changed = bool(current and recorded and current != recorded)
        if changed:
            self._alerts.firing(A.H20_WECHAT_AUTO_UPDATED, subject="host", severity="crit",
                                evidence={"current_version": current, "recorded_version": recorded},
                                hint_actions=["wechat_reinstall_bundled"])
        elif update_pkg_present:
            self._alerts.firing(A.H20_WECHAT_AUTO_UPDATED, subject="host", severity="warn",
                                evidence={"update_pkg": True, "current_version": current})
        else:
            self._alerts.resolve(A.H20_WECHAT_AUTO_UPDATED, subject="host")
        self.state.wechat_version_seen = current
        self.state.checks["H20"] = not (changed or update_pkg_present)
        return {"current_version": current, "changed": changed, "update_pkg": update_pkg_present}

    def check_chatlog(self, *, account_id: str, http_ok: bool) -> bool:
        """H09:chatlog ``GET /api/v1/session?limit=1`` 非 200 或超时 3s ⇒ crit(``subject=account:<wxNN>``)。"""
        subject = f"account:{account_id}"
        if http_ok:
            self._alerts.resolve(A.H09_CHATLOG_DOWN, subject=subject)
        else:
            self._alerts.firing(A.H09_CHATLOG_DOWN, subject=subject, evidence={"http_ok": False})
        self.state.checks["H09"] = http_ok
        return http_ok

    def check_wechat_process(self, *, account_id: str, process_exists: bool, window_exists: bool) -> bool:
        """H10:进程无 / 窗口无 ⇒ crit。**只拉起进程、不自动重登**(D-2);拉起动作在 ``wechat`` 模块。"""
        subject = f"account:{account_id}"
        ok = process_exists and window_exists
        if ok:
            self._alerts.resolve(A.H10_WECHAT_PROCESS_MISSING, subject=subject)
        else:
            self._alerts.firing(A.H10_WECHAT_PROCESS_MISSING, subject=subject,
                                evidence={"process": process_exists, "window": window_exists})
        self.state.checks["H10"] = ok
        return ok

    def check_disks(self, *, wechat_data_path: Optional[str] = None, vhdx_size_mb: Optional[float] = None,
                    ext4_used_mb: Optional[float] = None) -> dict[str, Any]:
        """H12(三级水位)+ ``WECHAT_DISK_LOW``(R4-9 独立 warn)+ H23(vhdx 差值)。

        🔴 产品级水位 = **我方三分区的剩余空间最小值**;微信数据根**不进**这个最小值集合。
        """
        product_free: list[tuple[str, float, float]] = []
        for t in self._disks:
            if not os.path.exists(t.path):
                continue
            free, total = self._sys.disk_free_mb(t.path)
            if t.product_level:
                product_free.append((t.key, free, total))
        level = "ok"
        worst = min(product_free, key=lambda x: x[1]) if product_free else None
        if worst is not None:
            free = worst[1]
            pct = (free / worst[2] * 100) if worst[2] else 100.0
            if free < self._cfg.disk_critical_mb or pct < self._cfg.disk_crit_pct:
                level = "critical"
            elif free < self._cfg.disk_high_mb:
                level = "high"
            elif free < self._cfg.disk_warn_mb or pct < self._cfg.disk_warn_pct:
                level = "warn"
        if level == "ok":
            self._alerts.resolve(A.H12_DISK_LOW, subject="host")
        else:
            # 04 §2.3 H12:severity 随水位(warn/warn/crit);payload 必带 free_mb/db_size_mb/media_size_mb(00 §11.11 ③)
            self._alerts.firing(A.H12_DISK_LOW, subject="host", severity="crit" if level == "critical" else "warn",
                                evidence={"level": level, "partition": worst[0] if worst else None,
                                          "free_mb": worst[1] if worst else None, "db_size_mb": None,
                                          "media_size_mb": None},
                                hint_actions=["open_env", "run_cleanup"])
        wechat_free = None
        if wechat_data_path:
            wechat_free, _ = self._sys.disk_free_mb(wechat_data_path)
            if wechat_free < self._cfg.wechat_disk_warn_mb:
                self._alerts.firing(A.WECHAT_DISK_LOW, subject="host",
                                    evidence={"partition": wechat_data_path, "free_mb": wechat_free},
                                    hint_actions=["open_env"])
            else:
                self._alerts.resolve(A.WECHAT_DISK_LOW, subject="host")
        if vhdx_size_mb is not None and ext4_used_mb is not None:
            diff_gb = (vhdx_size_mb - ext4_used_mb) / 1024
            if diff_gb > self._cfg.vhdx_growth_warn_gb:
                self._alerts.firing(A.H23_VHDX_GROWTH, subject="host",
                                    evidence={"vhdx_size_mb": vhdx_size_mb, "ext4_used_mb": ext4_used_mb,
                                              "diff_gb": round(diff_gb, 1)}, hint_actions=["open_env"])
            else:
                self._alerts.resolve(A.H23_VHDX_GROWTH, subject="host")
        self.state.checks["H12"] = level == "ok"
        return {"level": level, "worst": worst[0] if worst else None,
                "free_mb": worst[1] if worst else None, "wechat_free_mb": wechat_free}

    # ---------------------------------------------------------------- #2 health 的 host 段
    def _host_from(self, mem: dict[str, float], procs: dict[str, Any]) -> dict[str, float]:
        vm = 0.0
        for n in VMMEM_NAMES:
            if n in procs:
                vm = procs[n].rss_mb
                break
        wechat_mb = sum(procs[n].rss_mb for n in WECHAT_PROCESS_NAMES if n in procs)
        chatlog = procs.get("chatlog.exe")
        return {"total_mb": mem["total_mb"], "available_mb": mem["available_mb"], "wsl_vm_mb": vm,
                "wechat_mb": wechat_mb, "chatlog_mb": chatlog.rss_mb if chatlog else 0.0}

    def host_snapshot(self, *, wsl_vm_mb: Optional[float] = None) -> dict[str, float]:
        """``GET /wa/v1/health.host``(02 §3.6 #2):**是 pool 的 windows 池输入**,字段名逐字。

        进程表扫描放在采样线程里。健康检查直接回上一轮缓存,避免在请求里扫全进程、把 2 秒探活拖超时。
        """
        cached = dict(self._host_cache) if self._host_cache else None
        if cached is None:
            mem = self._sys.memory_mb()
            cached = {"total_mb": mem["total_mb"], "available_mb": mem["available_mb"],
                      "wsl_vm_mb": 0.0, "wechat_mb": 0.0, "chatlog_mb": 0.0}
        if wsl_vm_mb is not None:
            cached["wsl_vm_mb"] = float(wsl_vm_mb)
        return cached

    def health_checks(self) -> dict[str, Optional[bool]]:
        """``GET /wa/v1/health.checks``(02 §3.6 #2)。🔴 **R6-58 (ao):恒八键,没跑过的给 ``None``** ——
        不是「只放已跑过的项」的动态字典(那样逼控制台先判 key 存在性再判值,两处判据必然分叉)。

        ``self.state.checks`` 本身仍是内部动态字典(还记着本模块不对外的 H12 等项),本方法只是
        按 ``HEALTH_CHECK_KEYS`` 固定八键裁一份出去,不改内部记账方式。
        """
        return {k: self.state.checks.get(k) for k in HEALTH_CHECK_KEYS}
