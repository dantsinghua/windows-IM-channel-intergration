"""Agent 侧(WSL)监控采样与两个横切执行体 —— 规格:04 §2.4.5 / §3.1 ``health_samples``、02 §7.1 ``[monitor]``/``[jobs]``、§2.2.12 / §3.4.6 #102。

三件事,都做成「可注入读数 + 缺省不出网/不碰真设备」:

1. :class:`Sampler` —— 每 ``[monitor] sample_interval_s``(10 s)写一批 ``health_samples`` 的 ``raw`` 行:
   ``scope='wsl'``(整机 mem/cpu)、``scope='container'``(每账号 cgroup v2 的 ``memory.current`` / ``memory.stat anon``)、
   ``scope='process'``(agent 自身 RSS)、``scope='disk'``(``data_dir`` 余量)。
   **读数经 ``ProcReader`` 注入**(缺省 :class:`LinuxProcReader` 读 ``/proc`` 与 cgroup 文件;测试注 :class:`FakeProcReader`)。
   🔴 采样是 ``pool_calibrate`` 与 #77 的**唯一数据源** —— 没有它,自校准在空库上恒回「无建议」。
   降采样(``1m``/``1h``)由 ``roll_up()`` 每分钟/每小时各跑一次,保留期归 ``maintenance``(E-18 三级)。

2. :class:`JobsReclaimer` —— ``[jobs] reclaim_interval_s``(60 s)一轮,按 02 §3.1 的两条规范 SQL 回收超龄
   ``running`` 作业:``attempt_count < 3`` 退回 ``queued``,``>= 3`` 判 ``failed``(**不无限重跑**)。

3. :class:`PublicEndpointProbe` —— E-3 公网出口探测(#102 / §2.2.12)。
   🔴 ``[api] public_ip_check_interval_s`` **默认 0 = 关**(§11.22 [SCOPE] 砍了「默认定时探公网 IP」),
   **不显式置 >0 就一次都不出网**;HTTP 客户端可注入(测试注 ``webhook.FakeHttp`` 同款协议)。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger("qtrade.monitor")

SAMPLE_INTERVAL_S = 10          # 02 §7.1 / 04 §7 [monitor] sample_interval_s
ROLLUP_1M_S = 60
ROLLUP_1H_S = 3600
RECLAIM_MAX_ATTEMPTS = 3        # 02 §3.1 jobs 注:attempt_count >= 3 判死,不无限重跑


# ══════════════════════════════════════════════════════════════════ 读数(可注入)
class ProcReader(Protocol):
    def meminfo(self) -> dict[str, float]: ...
    def cpu_pct(self) -> Optional[float]: ...
    def self_rss_mb(self) -> Optional[float]: ...
    def cgroup(self, container: str) -> dict[str, float]: ...


class LinuxProcReader:
    """真读数:``/proc/meminfo``、``/proc/stat``、``/proc/self/statm``、cgroup v2 的 ``memory.current``/``memory.stat``。

    找不到文件一律回空字典/``None``(**不抛**)—— 采样是尽力而为,缺一轮不该把 scheduler 的任务打红。
    """

    def __init__(self, *, proc: str = "/proc", cgroup_root: str = "/sys/fs/cgroup"):
        self._proc = proc
        self._cgroup_root = cgroup_root
        self._last_cpu: Optional[tuple[float, float]] = None

    def _read(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def meminfo(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for line in self._read(os.path.join(self._proc, "meminfo")).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                try:
                    out[parts[0][:-1]] = float(parts[1]) / 1024        # kB → MB
                except ValueError:
                    continue
        return out

    def cpu_pct(self) -> Optional[float]:
        """两次 ``/proc/stat`` 之差;第一次调用只记基线、回 ``None``(没有基线就没有百分比)。"""
        line = next((x for x in self._read(os.path.join(self._proc, "stat")).splitlines() if x.startswith("cpu ")), "")
        cols = [float(x) for x in line.split()[1:] if x.replace(".", "").isdigit()]
        if not cols:
            return None
        total, idle = sum(cols), (cols[3] if len(cols) > 3 else 0.0)
        prev, self._last_cpu = self._last_cpu, (total, idle)
        if prev is None or total <= prev[0]:
            return None
        return max(0.0, min(100.0, (1.0 - (idle - prev[1]) / (total - prev[0])) * 100.0))

    def self_rss_mb(self) -> Optional[float]:
        cols = self._read(os.path.join(self._proc, "self", "statm")).split()
        if len(cols) < 2:
            return None
        try:
            return float(cols[1]) * (os.sysconf("SC_PAGE_SIZE") / 1048576)
        except (ValueError, OSError):
            return None

    def cgroup(self, container: str) -> dict[str, float]:
        """cgroup v2:``memory.current``(容器 RSS+cache)与 ``memory.stat`` 的 ``anon``(资源池真值,04 §2.5.3)。"""
        base = os.path.join(self._cgroup_root, "system.slice", f"docker-{container}.scope")
        if not os.path.isdir(base):
            base = os.path.join(self._cgroup_root, "docker", container)
        out: dict[str, float] = {}
        cur = self._read(os.path.join(base, "memory.current")).strip()
        if cur.isdigit():
            out["mem_mb"] = float(cur) / 1048576
        mx = self._read(os.path.join(base, "memory.max")).strip()
        if mx.isdigit():
            out["mem_max_mb"] = float(mx) / 1048576
        for line in self._read(os.path.join(base, "memory.stat")).splitlines():
            k, _, v = line.partition(" ")
            if k == "anon" and v.strip().isdigit():
                out["mem_anon_mb"] = float(v) / 1048576
        return out


@dataclass
class FakeProcReader:
    """测试读数:直接给数,**不碰任何真文件**。"""
    mem: dict[str, float] = field(default_factory=lambda: {"MemTotal": 11264.0, "MemAvailable": 6100.0, "MemFree": 2048.0})
    cpu: Optional[float] = 12.5
    rss: Optional[float] = 96.0
    containers: dict[str, dict[str, float]] = field(default_factory=dict)

    def meminfo(self) -> dict[str, float]:
        return dict(self.mem)

    def cpu_pct(self) -> Optional[float]:
        return self.cpu

    def self_rss_mb(self) -> Optional[float]:
        return self.rss

    def cgroup(self, container: str) -> dict[str, float]:
        return dict(self.containers.get(container) or {})


# ══════════════════════════════════════════════════════════════════ 采样
class Sampler:
    """写 ``health_samples`` 的唯一入口(Agent 侧;WinAgent 侧自己写自己那份,R-13 不跨侧同步)。"""

    def __init__(self, store, *, reader: Optional[ProcReader] = None, data_dir: str = "/var/lib/qtrade",
                 clock: Callable[[], int] = lambda: int(time.time() * 1000), disk=None):
        self._store = store
        self._reader = reader or LinuxProcReader()
        self._data_dir = data_dir
        self._clock = clock
        self._disk = disk                     # maintenance.DiskProbe(可注入);None = shutil.disk_usage
        self._last_1m_ms = 0
        self._last_1h_ms = 0

    # ---------------------------------------------------------- 写一行
    def _insert(self, c, *, now: int, scope: str, subject: str, resolution: str = "raw", **cols: Any) -> None:
        names = ["ts_ms", "resolution", "scope", "subject", "extra_json"]
        vals: list[Any] = [now, resolution, scope, subject, json.dumps(cols.pop("extra", {}), ensure_ascii=False)]
        for k, v in cols.items():
            if v is not None:
                names.append(k)
                vals.append(float(v))
        c.execute(f"INSERT INTO health_samples({','.join(names)}) VALUES ({','.join('?' * len(names))})", vals)

    def _free_mb(self) -> Optional[float]:
        if self._disk is not None:
            try:
                return float(self._disk.free_mb(self._data_dir))
            except Exception:
                return None
        try:
            return shutil.disk_usage(self._data_dir).free / 1048576
        except OSError:
            return None

    async def sample_once(self, *, now_ms: Optional[int] = None) -> int:
        """跑一轮采样,返回写了几行。任何一项读不到就**跳过那一行**,不写 0 也不抛。"""
        now = now_ms or self._clock()
        mem = self._reader.meminfo()
        rows = 0
        with self._store._tx() as c:
            total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
            if total is not None:
                self._insert(c, now=now, scope="wsl", subject="wsl",
                             mem_mb=(total - avail) if avail is not None else None, mem_max_mb=total,
                             cpu_pct=self._reader.cpu_pct(),
                             extra={"mem_available_mb": avail} if avail is not None else {})
                rows += 1
            rss = self._reader.self_rss_mb()
            if rss is not None:
                self._insert(c, now=now, scope="process", subject="qtrade-agent", mem_mb=rss)
                rows += 1
            free = self._free_mb()
            if free is not None:
                self._insert(c, now=now, scope="disk", subject=self._data_dir, disk_free_mb=free)
                rows += 1
            for row in self._store.list_accounts():
                if row["host"] != "wsl" or row["state"] not in ("running", "degraded"):
                    continue
                name = row.get("container_name") or f"qtrade-{row['id']}"
                cg = self._reader.cgroup(name)
                if not cg:
                    continue
                self._insert(c, now=now, scope="container", subject=name, **cg)
                rows += 1
        return rows

    # ---------------------------------------------------------- 降采样(04 §3.1 三级)
    def roll_up(self, *, now_ms: Optional[int] = None) -> dict[str, int]:
        """把上一整分钟的 ``raw`` 聚成 ``1m``、上一整小时的 ``1m`` 聚成 ``1h``(均值进列、最大值进 ``agg_max_json``)。"""
        now = now_ms or self._clock()
        out = {"1m": 0, "1h": 0}
        # 窗口 = 「上次聚过之后到现在」,不是「最近 60 s」—— 后者在采样稀疏或时钟跳变时会**整窗落空**
        if now - self._last_1m_ms >= ROLLUP_1M_S * 1000:
            out["1m"] = self._aggregate("raw", "1m", self._last_1m_ms, now)
            self._last_1m_ms = now
        if now - self._last_1h_ms >= ROLLUP_1H_S * 1000:
            out["1h"] = self._aggregate("1m", "1h", self._last_1h_ms, now)
            self._last_1h_ms = now
        return out

    NUMERIC = ("cpu_pct", "mem_mb", "mem_anon_mb", "mem_max_mb", "disk_free_mb", "disk_total_mb", "net_rx_kbps", "net_tx_kbps")

    def _aggregate(self, src: str, dst: str, since: int, now: int) -> int:
        avg = ", ".join(f"AVG({c}) AS avg_{c}" for c in self.NUMERIC)
        mx = ", ".join(f"MAX({c}) AS max_{c}" for c in self.NUMERIC)
        rows = self._store.con.execute(
            f"SELECT scope, subject, {avg}, {mx} FROM health_samples WHERE resolution=? AND ts_ms >= ? AND ts_ms < ? "
            "GROUP BY scope, subject", (src, since, now)).fetchall()
        n = 0
        with self._store._tx() as c:
            for r in rows:
                d = dict(r)
                cols = {col: d[f"avg_{col}"] for col in self.NUMERIC if d[f"avg_{col}"] is not None}
                agg_max = {col: d[f"max_{col}"] for col in self.NUMERIC if d[f"max_{col}"] is not None}
                self._insert(c, now=now, scope=d["scope"], subject=d["subject"], resolution=dst, **cols)
                c.execute("UPDATE health_samples SET agg_max_json=? WHERE id=(SELECT MAX(id) FROM health_samples)",
                          (json.dumps(agg_max, ensure_ascii=False),))
                n += 1
        return n


# ══════════════════════════════════════════════════════════════════ jobs_reclaimer(02 §3.1 R6-16)
class JobsReclaimer:
    """超龄 ``running`` 作业回收:``attempt_count < 3`` 退回 ``queued``,``>= 3`` 判 ``failed``。

    两条都是**条件 UPDATE**(行级 claim,幂等);``reclaim_after_s`` 必须 > 最慢作业的单轮时长,
    配小了 = 导出被反复重启永远跑不完(02 §3.1 注)。
    """

    def __init__(self, store, *, reclaim_after_s: int = 900, events=None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._store = store
        self.reclaim_after_s = reclaim_after_s
        self._events = events
        self._clock = clock

    async def reclaim_once(self, *, now_ms: Optional[int] = None) -> dict[str, int]:
        now = now_ms or self._clock()
        cutoff = now - self.reclaim_after_s * 1000
        dead: list[dict[str, Any]] = []
        with self._store._tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT job_id, kind, attempt_count FROM jobs WHERE state='running' AND updated_ms <= ?", (cutoff,))]
            requeued = c.execute("UPDATE jobs SET state='queued', updated_ms=? "
                                 "WHERE state='running' AND updated_ms <= ? AND attempt_count < ?",
                                 (now, cutoff, RECLAIM_MAX_ATTEMPTS)).rowcount
            failed = c.execute("UPDATE jobs SET state='failed', updated_ms=?, error_json=? "
                               "WHERE state='running' AND updated_ms <= ? AND attempt_count >= ?",
                               (now, json.dumps({"code": "TIMEOUT", "message": "作业超龄且已重试 3 次,判死"}, ensure_ascii=False),
                                cutoff, RECLAIM_MAX_ATTEMPTS)).rowcount
            dead = [r for r in rows if int(r["attempt_count"]) >= RECLAIM_MAX_ATTEMPTS]
        if self._events is not None:
            for r in dead:
                self._events.emit("job", payload={"job_id": r["job_id"], "kind": r["kind"], "state": "failed",
                                                  "error": {"code": "TIMEOUT", "message": "作业超龄且已重试 3 次,判死"}}, now_ms=now)
        if requeued or failed:
            log.warning("jobs_reclaimer:退回队列 %d 条、判死 %d 条(reclaim_after_s=%d)", requeued, failed, self.reclaim_after_s)
        return {"requeued": requeued, "failed": failed}


# ══════════════════════════════════════════════════════════════════ E-3 公网出口探测(#102 / §2.2.12)
SETTINGS_KEY = "system.public_endpoint"


class PublicEndpointProbe:
    """按 ``[api] public_ip_probe_urls`` 顺序探一次,**任一成功即止**;结果落 ``settings['system.public_endpoint']``。

    🔴 ``[api] public_ip_check_interval_s`` **默认 0 = 关**(§11.22 [SCOPE]):``tick()`` 在 ``interval_s<=0`` 时
    直接返回、**一个字节都不出网**;只有显式置 >0(或手动 ``?refresh=true``)才探。
    变化时推 ``net`` 事件 ``NET_PUBLIC_ENDPOINT_CHANGED``(§2.2.12:webhook 扇出**不受订阅过滤**)。
    """

    def __init__(self, store, *, http=None, events=None, urls: tuple[str, ...] = (),
                 interval_s: int = 0, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 timeout_ms: int = 5000):
        self._store = store
        self._http = http
        self._events = events
        self.urls = tuple(urls)
        self.interval_s = interval_s
        self._clock = clock
        self._timeout_ms = timeout_ms
        self._last_ms = 0

    def snapshot(self) -> dict[str, Any]:
        return dict(self._store.settings_get(SETTINGS_KEY) or {})

    async def tick(self) -> Optional[dict[str, Any]]:
        if self.interval_s <= 0:
            return None                                  # 默认关:一次都不出网
        now = self._clock()
        if now - self._last_ms < self.interval_s * 1000:
            return None
        self._last_ms = now
        return await self.probe(now_ms=now)

    async def probe(self, *, now_ms: Optional[int] = None) -> dict[str, Any]:
        """手动探一次(#102 ``?refresh=true``)。全部探针失败 ⇒ ``unreachable_rounds+1``,``public_ip`` 保持上次的值。"""
        now = now_ms or self._clock()
        prev = self.snapshot()
        ip, used = None, None
        for url in self.urls:
            if self._http is None:
                break
            try:
                resp = await self._http.post(url, body=b"", headers={}, timeout_ms=self._timeout_ms)
            except Exception as e:                       # 出网被墙 / 超时:换下一个探针
                log.info("公网出口探测失败 %s: %r", url, e)
                continue
            if 200 <= resp.status < 300:
                text = (resp.body or b"").decode("utf-8", "replace").strip()
                if text:
                    ip, used = text.splitlines()[0].strip(), url
                    break
        st = dict(prev)
        st["probe_url"] = used or (self.urls[0] if self.urls else None)
        st["checked_ms"] = now
        if ip is None:
            st["unreachable_rounds"] = int(prev.get("unreachable_rounds") or 0) + 1
        else:
            st["unreachable_rounds"] = 0
            if ip != prev.get("public_ip"):
                st["public_ip"], st["changed_ms"] = ip, now
                self._emit_changed(prev.get("public_ip"), ip, now)
            else:
                st["public_ip"] = ip
        self._store.settings_set(SETTINGS_KEY, st, actor="system:public_endpoint", now_ms=now)
        return st

    def _emit_changed(self, old: Optional[str], new: str, now: int) -> None:
        if self._events is None:
            return
        self._events.emit("net", payload={"code": "NET_PUBLIC_ENDPOINT_CHANGED", "severity": "info", "state": "firing",
                                          "subject": "host", "previous_ip": old, "public_ip": new,
                                          "changed_at": now}, now_ms=now)


__all__ = ["FakeProcReader", "JobsReclaimer", "LinuxProcReader", "ProcReader", "PublicEndpointProbe", "Sampler",
           "RECLAIM_MAX_ATTEMPTS", "SAMPLE_INTERVAL_S", "SETTINGS_KEY"]
