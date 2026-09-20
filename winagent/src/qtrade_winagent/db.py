"""``winagent.db`` —— WinAgent **服务**是它的唯一写方(02 §2.7「两库边界」;Agent 绝不直写对侧库)。

- DDL 逐字抽自 02 §3.2,存 ``schema_winagent.sql``(抽取脚本见该文件头两行注释)。
- 必开 PRAGMA 与 ``agent.db`` 同一套(02 §2.7「必开参数」);``winagent.db`` 另有 ``auto_vacuum=INCREMENTAL``(§3.2 首行)。
- 🔴 **保留期清理责任方(R3-20)**:``wa_audit_log``/``health_samples``/``probe_results``/``probe_targets_observed``
  由**服务自身**定时做,周期与 Agent 侧对齐 —— **每日 03:00 本地时间**;批删后 ``PRAGMA incremental_vacuum``(04 §2.2)。
- 三级降采样保留:``raw`` 24h / ``1m`` 7d / ``1h`` 30d(04 §2.2;R3-24 两库逐档相等)。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

from .config import MonitorConfig, ProbeConfig, RetentionConfig

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_winagent.sql")

# 02 §2.7「必开参数」(打开连接即执行)
PRAGMAS = (
    "PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON", "PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-65536",
    "PRAGMA wal_autocheckpoint=2000", "PRAGMA auto_vacuum=INCREMENTAL",
)

SCHEMA_VERSION = 1
SCHEMA_NAME = "winagent-initial(02 §3.2)"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def load_schema_sql() -> str:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return f.read()


class Db:
    """单写连接 + 事务整体互斥(02 §2.3.1 并发规约:SQLite 单写)。所有 ``/wa/v1`` 落库都经本类。"""

    def __init__(self, path: str, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.path = path
        self._clock = clock
        self._con: Optional[sqlite3.Connection] = None
        self._tx_lock = threading.RLock()

    # ---------------------------------------------------------------- 生命周期
    def open(self) -> "Db":
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        con = sqlite3.connect(self.path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        for p in PRAGMAS:
            con.execute(p)
        self._con = con
        self._migrate()
        return self

    def close(self) -> None:
        if self._con is not None:
            with self._tx_lock:
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._con.close()
            self._con = None

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            raise RuntimeError("winagent.db 未打开:先调 Db.open()")
        return self._con

    def _migrate(self) -> None:
        row = self.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if row is None:
            self.con.executescript(load_schema_sql())
            # ``checksum`` 只用于「库版本 vs 代码 SUPPORTED_SCHEMA」的比对(02 §3.8),不做加密用途
            import hashlib
            checksum = hashlib.sha256(load_schema_sql().encode("utf-8")).hexdigest()[:16]
            self.con.execute("INSERT INTO schema_version(version, name, applied_ms, checksum) VALUES (?,?,?,?)",
                             (SCHEMA_VERSION, SCHEMA_NAME, self._clock(), checksum))
            self.con.commit()

    # ---------------------------------------------------------------- 事务 / 查询
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE … COMMIT``;整体互斥(§2.3.1)。"""
        with self._tx_lock:
            con = self.con
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.rollback()
                raise
            con.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        r = self.con.execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    # ---------------------------------------------------------------- settings(02 §3.2 settings;与 agent.db 互不同步)
    def get_setting(self, key: str) -> Optional[Any]:
        import json
        row = self.one("SELECT value_json FROM settings WHERE key=?", (key,))
        return json.loads(row["value_json"]) if row else None

    def put_setting(self, key: str, value: Any, *, updated_by: str = "svc") -> None:
        import json
        with self.tx() as con:
            con.execute("INSERT INTO settings(key, value_json, updated_ms, updated_by) VALUES (?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_ms=excluded.updated_ms, "
                        "updated_by=excluded.updated_by",
                        (key, json.dumps(value, ensure_ascii=False), self._clock(), updated_by))

    # ---------------------------------------------------------------- 保留期清理(R3-20;每日 03:00 本地)
    def retention_sweep(self, *, monitor: MonitorConfig, probe: ProbeConfig, retention: RetentionConfig,
                        now_ms: Optional[int] = None) -> dict[str, int]:
        """按 winagent.toml ``[retention]``/``[monitor]``/``[probe]`` 批删过期行,返回每表删除条数。

        三级降采样逐档删(04 §2.2):``raw`` 24h、``1m`` 7d、``1h`` 30d —— 每档各自的截止点,不是一刀切。
        """
        now = self._clock() if now_ms is None else now_ms
        deleted: dict[str, int] = {}
        with self.tx() as con:
            cuts = (("raw", now - monitor.raw_retention_h * HOUR_MS),
                    ("1m", now - monitor.m1_retention_d * DAY_MS),
                    ("1h", now - monitor.h1_retention_d * DAY_MS))
            n = 0
            for resolution, cut in cuts:
                n += con.execute("DELETE FROM health_samples WHERE resolution=? AND ts_ms < ?", (resolution, cut)).rowcount
            deleted["health_samples"] = n
            deleted["probe_results"] = con.execute(
                "DELETE FROM probe_results WHERE ts_ms < ?", (now - probe.results_retention_d * DAY_MS,)).rowcount
            deleted["probe_targets_observed"] = con.execute(       # 04 §2.8.4:与 probe_results 同 30 天
                "DELETE FROM probe_targets_observed WHERE last_seen_ms < ?", (now - probe.results_retention_d * DAY_MS,)).rowcount
            deleted["wa_audit_log"] = con.execute(
                "DELETE FROM wa_audit_log WHERE ts_ms < ?", (now - retention.wa_audit_days * DAY_MS,)).rowcount
        self.con.execute("PRAGMA incremental_vacuum")
        return deleted

    @staticmethod
    def next_sweep_ms(now_ms: int) -> int:
        """下一次 **03:00 本地时间** 的 epoch ms(R3-20:与 Agent 侧 ``agent.db`` 清理对齐)。"""
        now = datetime.fromtimestamp(now_ms / 1000).astimezone()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target.fromtimestamp(target.timestamp() + 86400).astimezone()
        return int(target.timestamp() * 1000)
