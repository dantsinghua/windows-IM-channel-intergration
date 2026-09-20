"""保留期清理 / VACUUM / 在线备份 / 磁盘三级水位与 ``DISK_FULL`` 保护(02 §2.8.4、§2.8.8、§3.3;E-18)。

规格逐条:

- **保留期与清理顺序**(§2.8.4):每日 ``[retention] cleanup_at``(03:00)一轮,**顺序固定**
  ①`messages` 超 ``messages_days`` → 删(FTS 由触发器同步),被删消息引用的 ``media.ref_count`` 递减;
  ②媒体按**自己的** ``media_days`` 过期(**独立于引用计数**)→ 删文件、``status='expired'``、
    ``messages.media_json[].state`` 同步改 ``expired``;``ref_count=0`` 且超 ``orphan_grace_h`` 的连元数据一起删;
  ③`raw_ref` 文件按 ``raw_days``;④邮件归档 ``.eml`` 按 ``mail_archive_days``(``archived_path`` 置 NULL、行保留);
  ⑤`commands/command_results/audit_log/events_outbox/mail_*` 超期行 —— 🔴 删 ``mail_inbox`` 行**之前**先把本批
    ``protocol='pop3'`` 且 ``status ∈ NEVER_DELETE={OUT_OF_SCOPE, OVERSIZE}`` 的 ``uidl`` 并进
    ``cursors(owner='mail:<mailbox>', kind='pop3_uidl_recent')`` 的最近 2000 个数组(**与删行同事务**),再删(R6-1 配套);
  ⑥崩溃转储目录 ``cores/`` 按 30 天删,进 ``cores_mb`` 统计。
  每批 ``cleanup_batch``(5000)行,批间让出事件循环。
- **例外清单**(§2.8.4):配置与身份类行(``accounts``/``sessions``/``cursors``/``webhooks``/``api_clients``/
  ``settings``/``resource_pools``/``device_profiles``/``mail_routes``/``mail_templates``/``schema_version``)**永不按期删**。
- **VACUUM**(§2.8.4):每周日清理后 ``PRAGMA incremental_vacuum``,**不做全量 VACUUM**(会复制整库、锁写)。
- **备份**(§3.3):每日 03:30(清理之后)``sqlite3.Connection.backup()`` 在线拷到
  ``<backup_dir>/agent-<yyyymmdd>.db``,保留 ``backup_keep``(7)份。不备份 ``media/`` 与 ``exports/``。
- **磁盘三级水位**(§2.8.8 / §3.3):``[retention] disk_warn_mb/disk_high_mb/disk_critical_mb`` = 5120/2048/1024,
  **逐级递进、下一级含上一级动作**;``warn`` 只告警、``high`` 停媒体下载(全转 lazy)+停邮件归档+即时全量清理、
  ``critical`` 再加「只保发送/读取的最小写入,暂停采集入库与导出」并把 ``retention_days`` **30→14→7** 递减重清
  (每档记 ``audit_log(kind='system')`` + alert,上限 3 档;到 7 天仍不达标 → ``DB_WRITE_FAILED`` crit)。
- **写失败先判磁盘满**(§2.8.8):``SQLITE_FULL`` / ``ENOSPC`` / ``disk I/O error`` → 结果码 **``DISK_FULL``**
  (HTTP **507**,不是 ``INTERNAL``),``error.message`` 带 ``free_mb``、``hint_actions=["open_env","run_cleanup"]``、
  ``retryable=false``、``needs_human=true``,并**立即按 critical 动作触发清理**;告警与日志必带
  ``free_mb``/``db_size_mb``/``media_size_mb`` 三个数。

磁盘余量经注入的 :class:`DiskProbe` 读(测试注 :class:`FakeDisk`,**不碰真盘余量**);文件删除走 ``os``,
测试一律在 ``tmp_path`` 下跑。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Optional, Protocol

from .events import TZ_SHANGHAI, iso8601

log = logging.getLogger("qtrade.maintenance")

H12_DISK_LOW = "H12_DISK_LOW"                 # 02 §3.7:warn / subject='host'|'wsl';high 起升 crit
DB_WRITE_FAILED = "DB_WRITE_FAILED"           # 02 §3.7:crit;payload 必带 free_mb/db_size_mb/media_size_mb

DATA_CLASS_MAX_DAYS = 30                      # E-18:本地数据类一律 ≤30 天,配更大按 30 截断并 WARN
RETENTION_SHRINK_STEPS = (30, 14, 7)          # §2.8.8 critical 递减循环(上限 3 档)
POP3_UIDL_RECENT_MAX = 2000                   # §2.8.4 ⑤ / §2.8.3:最近 2000 个 UIDL
MAIL_NEVER_DELETE = ("OUT_OF_SCOPE", "OVERSIZE")   # R6-1:这两类永远留在服务器,uidl 必须先并进游标
CORES_DAYS = 30                               # §2.8.4 ⑥:崩溃转储按数据类上限 30 天

LEVEL_NORMAL, LEVEL_WARN, LEVEL_HIGH, LEVEL_CRITICAL = "normal", "warn", "high", "critical"

# §2.8.8 表格的「动作」列(逐级递进,下一级含上一级)
ACTIONS_HIGH = ("media_lazy", "mail_archive_paused", "cleanup_now")
ACTIONS_CRITICAL = ACTIONS_HIGH + ("ingest_paused", "export_paused", "retention_shrink")


@dataclass(frozen=True)
class RetentionConfig:
    """02 §7.1 ``[retention]``(逐字默认值)。"""
    files_days: int = 7                       # E-10 采集侧文件统一键;下面三个是可选覆盖
    messages_days: int = 30
    media_days: int = 7
    raw_days: int = 7
    mail_archive_days: int = 7
    commands_days: int = 30
    audit_days: int = 30
    # 🔴 R6-58 (c):原 ``events_ws_hours`` **已删**。``events_outbox`` 两类行(``target='ws'`` 规范行与
    # ``target='webhook:<id>'`` 副本)的保留期**只有一把尺子** = ``[events] ws_retention_hours``(默认 72,
    # 见 ``EventsConfig``)。老 ``agent.toml`` 里残留本键不报错(``config.pick`` 按字段名过滤),但没有任何消费者。
    idempotency_days: int = 7
    mail_inbox_rows_days: int = 30
    export_jobs_days: int = 7
    health_raw_h: int = 24
    health_1m_d: int = 7
    health_1h_d: int = 30
    cleanup_at: str = "03:00"
    cleanup_batch: int = 5000
    disk_warn_mb: int = 5120
    disk_high_mb: int = 2048
    disk_critical_mb: int = 1024

    @property
    def disk_low_watermark_mb(self) -> int:
        """E-18 别名:E-10 时期各册引用的这个键 = ``high`` 档(§2.8.8/§7.1),零改动保留。"""
        return self.disk_high_mb

    def clamp(self) -> tuple["RetentionConfig", list[str]]:
        """数据类 ``*_days > 30`` → **按 30 截断并 WARN**(E-18;§7.1 #89 同款)。返回 (截断后的配置, 警告列表)。"""
        warns: list[str] = []
        fixes: dict[str, int] = {}
        for key in ("messages_days", "commands_days", "audit_days", "mail_inbox_rows_days", "health_1h_d"):
            val = getattr(self, key)
            if val > DATA_CLASS_MAX_DAYS:
                warns.append(f"[retention] {key}={val} 超过 E-18 上限 {DATA_CLASS_MAX_DAYS},已按 {DATA_CLASS_MAX_DAYS} 截断")
                fixes[key] = DATA_CLASS_MAX_DAYS
        if not fixes:
            return self, warns
        from dataclasses import replace
        return replace(self, **fixes), warns

    def watermarks_ordered(self) -> bool:
        """§7.1 #89:``disk_warn_mb >= disk_high_mb >= disk_critical_mb``,否则 400 INVALID_ARGS。"""
        return self.disk_warn_mb >= self.disk_high_mb >= self.disk_critical_mb


@dataclass(frozen=True)
class BackupConfig:
    """02 §7.1 ``[db]`` 备份三键 + §3.3「每日 03:30(清理之后)」。"""
    backup_dir: str = "/var/lib/qtrade/backup"
    backup_keep: int = 7
    backup_at: str = "03:30"


# ---------------------------------------------------------------- 磁盘余量探针
class DiskProbe(Protocol):
    def free_mb(self, path: str) -> int: ...


class RealDisk:
    def free_mb(self, path: str) -> int:
        try:
            return int(shutil.disk_usage(path).free // (1024 * 1024))
        except OSError:
            return 0


@dataclass
class FakeDisk:
    """测试用:可拨的剩余空间(开发容器里**不读真盘**)。"""
    free: int = 100_000

    def free_mb(self, path: str) -> int:
        return int(self.free)


# ---------------------------------------------------------------- DISK_FULL(§2.8.8 写失败先判磁盘满)
class DiskFullError(Exception):
    """结果码 ``DISK_FULL`` / HTTP **507**(基线 §8.3/§10,R-02);``retryable=false``、``needs_human=true``。"""

    code = "DISK_FULL"
    http_status = 507
    retryable = False
    needs_human = True
    hint_actions = ("open_env", "run_cleanup")

    def __init__(self, *, free_mb: int, db_size_mb: float, media_size_mb: float, cause: Optional[BaseException] = None):
        self.free_mb = free_mb
        self.db_size_mb = db_size_mb
        self.media_size_mb = media_size_mb
        self.cause = cause
        super().__init__(f"本地磁盘空间不足(剩余 {free_mb} MB),已暂停写入并触发清理")

    @property
    def message(self) -> str:
        return str(self)

    def evidence(self) -> dict[str, Any]:
        """§2.8.8:日志与 alert 必须带这三个数,让人一眼看出是不是满了。"""
        return {"free_mb": self.free_mb, "db_size_mb": round(self.db_size_mb, 2), "media_size_mb": round(self.media_size_mb, 2)}


def is_disk_full_error(exc: BaseException) -> bool:
    """§2.8.8 第一诊断项:``SQLITE_FULL`` / ``OSError ENOSPC`` / ``disk I/O error`` / 文件写异常。"""
    if isinstance(exc, DiskFullError):
        return True
    if isinstance(exc, OSError) and exc.errno == 28:            # ENOSPC
        return True
    if isinstance(exc, sqlite3.Error):
        text = str(exc).lower()
        return "disk is full" in text or "sqlite_full" in text or "disk i/o error" in text
    return False


def dir_size_mb(path: str) -> float:
    """目录体积(MB);目录不存在算 0。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total / (1024 * 1024)


@dataclass
class CleanupReport:
    """一轮清理的产出;``freed_mb`` 进 ``P-RES`` 的「上次清理清出 MB」(04 §2.4.5)。"""
    started_ms: int
    finished_ms: int = 0
    retention_days: int = DATA_CLASS_MAX_DAYS
    deleted: dict[str, int] = field(default_factory=dict)
    files_removed: int = 0
    freed_mb: float = 0.0
    errors: list[str] = field(default_factory=list)

    def bump(self, key: str, n: int) -> None:
        if n:
            self.deleted[key] = self.deleted.get(key, 0) + n


@contextmanager
def _tx(store) -> Iterator[Any]:
    """薄封装:复用 ``Store`` 自己的事务与写锁(文件所有权约束:不往 ``store/`` 里加方法)。"""
    with store._tx() as c:                     # noqa: SLF001 - 见 docstring
        yield c


class MaintenanceService:
    """保留期清理 / VACUUM / 备份 / 磁盘水位 的执行体(02 §2.8.4、§2.8.8、§3.3)。

    ``alerts`` 可选(有则发 ``H12_DISK_LOW`` / ``DB_WRITE_FAILED``);``data_dir`` = ``/var/lib/qtrade``
    (``media/``、``cores/``、``mail/archive/``、``accounts/<id>/raw/`` 都在它下面,02 §4 固定目录)。
    """

    def __init__(self, store, *, cfg: Optional[RetentionConfig] = None, backup: Optional[BackupConfig] = None,
                 data_dir: str = "/var/lib/qtrade", disk: Optional[DiskProbe] = None, alerts=None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 media_orphan_grace_h: int = 24, disk_subject: str = "wsl",
                 ws_retention_hours: int = 72):
        cfg = cfg or RetentionConfig()
        self.cfg, self.config_warnings = cfg.clamp()
        self.backup_cfg = backup or BackupConfig()
        self.data_dir = data_dir
        self.disk = disk or RealDisk()
        self._store = store
        self._alerts = alerts
        self._clock = clock
        self.media_orphan_grace_h = media_orphan_grace_h
        self.disk_subject = disk_subject                 # §3.7:H12_DISK_LOW 的 subject = host|wsl
        self.ws_retention_hours = ws_retention_hours     # [events] ws_retention_hours(events_outbox 两类行同一把尺子)
        # §2.8.8 三级水位的运行期开关(其它模块消费:媒体下载/邮件归档/采集入库/导出)
        self.level: str = LEVEL_NORMAL
        self.actions: tuple[str, ...] = ()
        self.retention_days_effective: int = self.cfg.messages_days
        self.retention_shrunk_to: Optional[int] = None
        for w in self.config_warnings:
            log.warning("%s", w)

    # ------------------------------------------------------------ 三级水位判定与开关
    @property
    def media_downloads_allowed(self) -> bool:
        return "media_lazy" not in self.actions

    @property
    def mail_archive_allowed(self) -> bool:
        return "mail_archive_paused" not in self.actions

    @property
    def ingest_allowed(self) -> bool:
        return "ingest_paused" not in self.actions

    @property
    def exports_allowed(self) -> bool:
        return "export_paused" not in self.actions

    def disk_level(self, free_mb: int) -> str:
        c = self.cfg
        if free_mb < c.disk_critical_mb:
            return LEVEL_CRITICAL
        if free_mb < c.disk_high_mb:
            return LEVEL_HIGH
        if free_mb < c.disk_warn_mb:
            return LEVEL_WARN
        return LEVEL_NORMAL

    def free_mb(self) -> int:
        return int(self.disk.free_mb(self.data_dir))

    def db_size_mb(self) -> float:
        db, wal = self._store.db_size_mb()
        return float(db) + float(wal)

    def media_size_mb(self) -> float:
        return dir_size_mb(os.path.join(self.data_dir, "media"))

    def sizes(self) -> dict[str, Any]:
        """§2.8.8 排查顺序的三个数(告警/日志/507 信封都要带)。"""
        return {"free_mb": self.free_mb(), "db_size_mb": round(self.db_size_mb(), 2),
                "media_size_mb": round(self.media_size_mb(), 2)}

    def watermark_snapshot(self) -> dict[str, Any]:
        """04 §2.4.5 / 02 #77 ``disk_watermark`` 的扁平形态(R6-30:``last_cleanup_at`` / ``last_cleanup_freed_mb`` 两键扁平,不嵌套)。"""
        last = self._store.settings_get("system.last_cleanup") or {}
        snap: dict[str, Any] = {"level": self.level, "free_mb": self.free_mb(), "actions": list(self.actions),
                                "last_cleanup_at": last.get("at"), "last_cleanup_freed_mb": last.get("freed_mb")}
        if self.retention_shrunk_to is not None:
            snap["retention_shrunk_to"] = self.retention_shrunk_to
        return snap

    # ------------------------------------------------------------ DISK_FULL 保护钩子
    def to_disk_full(self, exc: Optional[BaseException] = None) -> DiskFullError:
        s = self.sizes()
        return DiskFullError(free_mb=s["free_mb"], db_size_mb=s["db_size_mb"], media_size_mb=s["media_size_mb"], cause=exc)

    @contextmanager
    def guard_write(self, what: str = "write") -> Iterator[None]:
        """包住任何本地写(store/mail/media):磁盘满 → 抛 :class:`DiskFullError` + 告警 + **立即按 critical 清理**;
        非磁盘类异常原样抛出(由调用方回落 ``INTERNAL``,§2.8.8)。"""
        try:
            yield
        except Exception as e:
            if not is_disk_full_error(e):
                raise
            err = self.to_disk_full(e)
            log.error("%s 写入失败,第一诊断项 = 磁盘满:%s", what, err.evidence())
            if self._alerts is not None:
                self._alerts.firing(DB_WRITE_FAILED, subject=self.disk_subject, severity="crit",
                                    evidence=err.evidence() | {"what": what},
                                    hint_actions=list(DiskFullError.hint_actions))
            self.enforce_critical()
            raise err from e

    def enforce_critical(self) -> CleanupReport:
        """§2.8.8:命中 ``DISK_FULL`` 立即按 ``critical`` 动作走一遍(置开关 + 清理 + 递减保留期)。"""
        self.level = LEVEL_CRITICAL
        self.actions = ACTIONS_CRITICAL
        return self._cleanup_and_shrink()

    # ------------------------------------------------------------ 水位巡检(health 每轮调)
    def check_watermark(self, *, now_ms: Optional[int] = None) -> dict[str, Any]:
        """§2.8.8:读余量 → 定级 → 置开关 → 告警 → high/critical 即时清理(+ critical 递减保留期)。"""
        now = now_ms if now_ms is not None else self._clock()
        free = self.free_mb()
        level = self.disk_level(free)
        prev = self.level
        self.level = level
        self.actions = {LEVEL_NORMAL: (), LEVEL_WARN: (), LEVEL_HIGH: ACTIONS_HIGH, LEVEL_CRITICAL: ACTIONS_CRITICAL}[level]
        if level == LEVEL_NORMAL:
            self.retention_shrunk_to = None      # 余量恢复后 retention 不自动回弹的是**已改的配置**,本字段只是展示位
            if self._alerts is not None and prev != LEVEL_NORMAL:
                self._alerts.resolve(H12_DISK_LOW, subject=self.disk_subject)
        elif self._alerts is not None:
            # warn 只告警;high 起升 crit(§3.7 H12 行)——同键级别翻转会再发一次 firing(00 §7.5 / R6-56 ⑦)
            self._alerts.firing(H12_DISK_LOW, subject=self.disk_subject,
                                severity="warn" if level == LEVEL_WARN else "crit",
                                evidence=self.sizes() | {"level": level}, hint_actions=["open_env", "run_cleanup"])
        report = None
        if level in (LEVEL_HIGH, LEVEL_CRITICAL):
            report = self._cleanup_and_shrink(now_ms=now)
        snap = self.watermark_snapshot()
        if report is not None:
            snap["cleanup"] = {"freed_mb": report.freed_mb, "deleted": report.deleted}
        return snap

    def _cleanup_and_shrink(self, *, now_ms: Optional[int] = None) -> CleanupReport:
        """即时清理一轮;``critical`` 且仍不达标 → 30→14→7 递减重清(每档记审计 + alert),到 7 天仍不达标 → ``DB_WRITE_FAILED``。"""
        now = now_ms if now_ms is not None else self._clock()
        report = self.cleanup_once(now_ms=now)
        if self.level != LEVEL_CRITICAL:
            return report
        target = self.cfg.disk_high_mb           # §2.8.8:逐步清理到剩余 ≥ disk_high_mb 即达标
        for days in RETENTION_SHRINK_STEPS:
            if self.free_mb() >= target:
                return report
            if days >= self.retention_days_effective and days != RETENTION_SHRINK_STEPS[0]:
                continue
            self.retention_days_effective = days
            self.retention_shrunk_to = days
            self._store.insert_audit(kind="system", transport="local", actor="system:maintenance",
                                     action="retention.shrink", detail={"retention_days": days} | self.sizes(), now_ms=now)
            if self._alerts is not None:
                self._alerts.firing(H12_DISK_LOW, subject=self.disk_subject, severity="crit",
                                    evidence=self.sizes() | {"retention_shrunk_to": days},
                                    hint_actions=["open_env", "run_cleanup"])
            step = self.cleanup_once(now_ms=now, retention_days=days)
            report.freed_mb += step.freed_mb
            report.files_removed += step.files_removed
            for k, v in step.deleted.items():
                report.bump(k, v)
        if self.free_mb() < target and self._alerts is not None:
            self._alerts.firing(DB_WRITE_FAILED, subject=self.disk_subject, severity="crit",
                                evidence=self.sizes() | {"retention_days": RETENTION_SHRINK_STEPS[-1]},
                                hint_actions=["open_env", "run_cleanup"])
        return report

    # ------------------------------------------------------------ 清理主体(§2.8.4 的六步顺序)
    def cleanup_once(self, *, now_ms: Optional[int] = None, retention_days: Optional[int] = None) -> CleanupReport:
        now = now_ms if now_ms is not None else self._clock()
        days = retention_days if retention_days is not None else self.retention_days_effective
        rep = CleanupReport(started_ms=now, retention_days=days)
        before = self._disk_footprint_mb()

        self._purge_messages(rep, now, days)            # ①
        self._purge_media(rep, now)                     # ②
        self._purge_raw(rep, now)                       # ③
        self._purge_mail_archive(rep, now)              # ④
        self._purge_rows(rep, now, days)                # ⑤
        self._purge_cores(rep, now)                     # ⑥

        rep.finished_ms = self._clock()
        rep.freed_mb = max(0.0, before - self._disk_footprint_mb())
        # R6-30 / #109:P-RES「最近一次清理时间 / 清出 MB」的唯一来源
        self._store.settings_set("system.last_cleanup", {"at": iso8601(rep.finished_ms), "freed_mb": round(rep.freed_mb, 2)},
                                 actor="system:maintenance", now_ms=rep.finished_ms)
        self._store.insert_audit(kind="system", transport="local", actor="system:maintenance", action="retention.cleanup",
                                 detail={"retention_days": days, "deleted": rep.deleted, "files_removed": rep.files_removed,
                                         "freed_mb": round(rep.freed_mb, 2)}, now_ms=rep.finished_ms)
        return rep

    def _disk_footprint_mb(self) -> float:
        return self.db_size_mb() + dir_size_mb(os.path.join(self.data_dir, "media")) \
            + dir_size_mb(os.path.join(self.data_dir, "mail", "archive")) + dir_size_mb(os.path.join(self.data_dir, "cores"))

    # -- 批量删(每批 cleanup_batch 行;§2.8.4「每批 5000 行」)
    def _purge_by_rowid(self, table: str, ts_col: str, cutoff_ms: int, *, extra_where: str = "") -> int:
        total, batch = 0, self.cfg.cleanup_batch
        where = f"{ts_col} < ?" + (f" AND {extra_where}" if extra_where else "")
        while True:
            with _tx(self._store) as c:
                ids = [r[0] for r in c.execute(f"SELECT rowid FROM {table} WHERE {where} LIMIT ?", (cutoff_ms, batch))]
                if not ids:
                    return total
                c.execute(f"DELETE FROM {table} WHERE rowid IN ({','.join('?' * len(ids))})", ids)
            total += len(ids)
            if len(ids) < batch:
                return total

    # -- ① messages(FTS 由触发器同步;被删行引用的 media.ref_count 递减)
    def _purge_messages(self, rep: CleanupReport, now: int, days: int) -> None:
        cutoff = now - days * 86400_000
        batch = self.cfg.cleanup_batch
        while True:
            with _tx(self._store) as c:
                rows = c.execute("SELECT rowid, id, media_json FROM messages WHERE ts_ms < ? LIMIT ?", (cutoff, batch)).fetchall()
                if not rows:
                    return
                for r in rows:
                    for mid in _media_ids(r["media_json"]):
                        c.execute("UPDATE media SET ref_count = MAX(0, ref_count - 1), last_ref_ms=? WHERE id=?", (now, mid))
                c.execute(f"DELETE FROM messages WHERE rowid IN ({','.join('?' * len(rows))})", [r["rowid"] for r in rows])
            rep.bump("messages", len(rows))
            if len(rows) < batch:
                return

    # -- ② 媒体(到期独立于引用计数;孤儿按 orphan_grace_h)
    def _purge_media(self, rep: CleanupReport, now: int) -> None:
        cutoff = now - self.cfg.media_days * 86400_000
        expired: list[int] = []
        with _tx(self._store) as c:
            rows = c.execute("SELECT id, rel_path FROM media WHERE status='ready' AND ready_ms IS NOT NULL AND ready_ms < ?",
                             (cutoff,)).fetchall()
            for r in rows:
                rep.files_removed += _remove_file(os.path.join(self.data_dir, r["rel_path"] or ""))
                c.execute("UPDATE media SET status='expired' WHERE id=?", (r["id"],))
                expired.append(int(r["id"]))
            rep.bump("media_expired", len(rows))
        if expired:
            self._mark_media_json_expired(expired)
        # ref_count=0 且超过 orphan_grace_h ⇒ 连元数据一起删(无论是否到期)
        orphan_cutoff = now - self.media_orphan_grace_h * 3600_000
        with _tx(self._store) as c:
            rows = c.execute("SELECT id, rel_path FROM media WHERE ref_count=0 AND first_seen_ms < ?", (orphan_cutoff,)).fetchall()
            for r in rows:
                rep.files_removed += _remove_file(os.path.join(self.data_dir, r["rel_path"] or ""))
            if rows:
                c.execute(f"DELETE FROM media WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
            rep.bump("media_rows", len(rows))

    def _mark_media_json_expired(self, media_ids: list[int]) -> None:
        """§2.8.4 ②:``messages.media_json[].state`` 同步改 ``expired``(消息行仍在,只是图取不到 → ``410``)。"""
        ids = set(media_ids)
        with _tx(self._store) as c:
            for r in c.execute("SELECT rowid, media_json FROM messages WHERE media_json <> '[]'").fetchall():
                try:
                    items = json.loads(r["media_json"])
                except ValueError:
                    continue
                changed = False
                for item in items:
                    if isinstance(item, dict) and item.get("media_id") in ids and item.get("state") != "expired":
                        item["state"] = "expired"
                        changed = True
                if changed:
                    c.execute("UPDATE messages SET media_json=? WHERE rowid=?",
                              (json.dumps(items, ensure_ascii=False), r["rowid"]))

    # -- ③ raw_ref 文件(删文件 + 置 NULL,免得列指向不存在的文件)
    def _purge_raw(self, rep: CleanupReport, now: int) -> None:
        cutoff = now - self.cfg.raw_days * 86400_000
        with _tx(self._store) as c:
            rows = c.execute("SELECT rowid, raw_ref FROM messages WHERE raw_ref IS NOT NULL AND received_ms < ?",
                             (cutoff,)).fetchall()
            for r in rows:
                rep.files_removed += _remove_file(os.path.join(self.data_dir, r["raw_ref"]))
                c.execute("UPDATE messages SET raw_ref=NULL WHERE rowid=?", (r["rowid"],))
            rep.bump("raw_files", len(rows))

    # -- ④ 邮件归档 .eml(archived_path 置 NULL、行保留)
    def _purge_mail_archive(self, rep: CleanupReport, now: int) -> None:
        cutoff = now - self.cfg.mail_archive_days * 86400_000
        with _tx(self._store) as c:
            rows = c.execute("SELECT id, archived_path FROM mail_inbox WHERE archived_path IS NOT NULL AND archived_ms < ?",
                             (cutoff,)).fetchall()
            for r in rows:
                rep.files_removed += _remove_file(os.path.join(self.data_dir, r["archived_path"]))
                c.execute("UPDATE mail_inbox SET archived_path=NULL WHERE id=?", (r["id"],))
            rep.bump("mail_archive", len(rows))

    # -- ⑤ 行类超期(commands/results/audit/outbox/mail_*/workflow/health/jobs/idempotency)
    def _purge_rows(self, rep: CleanupReport, now: int, days: int) -> None:
        c = self.cfg
        rep.bump("commands", self._purge_by_rowid("commands", "submitted_ms", now - min(c.commands_days, days) * 86400_000))
        rep.bump("audit_log", self._purge_by_rowid("audit_log", "ts_ms", now - min(c.audit_days, days) * 86400_000))
        # events_outbox:`target='ws'` 的保留由 app 的 outbox_ws_retention 任务管;这里清 webhook 副本。
        # 🔴 总控裁决(rulings R6-58 (ac)):**两类行同一把尺子 = `[events] ws_retention_hours`**;
        # `[retention] events_ws_hours` 已废弃、不再参与判定。
        rep.bump("events_outbox_webhook",
                 self._purge_by_rowid("events_outbox", "ts_ms", now - self.ws_retention_hours * 3600_000,
                                      extra_where="target <> 'ws'"))
        self._purge_mail_inbox(rep, now, min(c.mail_inbox_rows_days, days))
        rep.bump("mail_outbox", self._purge_by_rowid("mail_outbox", "created_ms", now - min(c.mail_inbox_rows_days, days) * 86400_000))
        rep.bump("mail_cleanup_log", self._purge_by_rowid("mail_cleanup_log", "started_ms", now - min(c.mail_inbox_rows_days, days) * 86400_000))
        rep.bump("workflow_runs", self._purge_by_rowid("workflow_runs", "started_ms", now - days * 86400_000))
        rep.bump("health_raw", self._purge_by_rowid("health_samples", "ts_ms", now - c.health_raw_h * 3600_000,
                                                    extra_where="resolution='raw'"))
        rep.bump("health_1m", self._purge_by_rowid("health_samples", "ts_ms", now - c.health_1m_d * 86400_000,
                                                   extra_where="resolution='1m'"))
        rep.bump("health_1h", self._purge_by_rowid("health_samples", "ts_ms", now - min(c.health_1h_d, days) * 86400_000,
                                                   extra_where="resolution='1h'"))
        rep.bump("jobs", self._purge_by_rowid("jobs", "created_ms", now - c.export_jobs_days * 86400_000))
        # idempotency 行自带 expires_ms(created + idempotency_days),行数少、不分批(WITHOUT ROWID 表)
        with _tx(self._store) as tx:
            rep.bump("idempotency", tx.execute("DELETE FROM idempotency WHERE expires_ms < ?", (now,)).rowcount)

    def _purge_mail_inbox(self, rep: CleanupReport, now: int, days: int) -> None:
        """🔴 R6-1 配套:删 ``mail_inbox`` 行**之前**,本批 ``pop3`` 且 ``status ∈ NEVER_DELETE`` 的 ``uidl``
        先并进 ``cursors(owner='mail:<mailbox>', kind='pop3_uidl_recent')`` 的最近 2000 个数组(**同事务**),再删。
        否则行一删,``(mailbox, uidl)`` 那层去重随之消失,第 31 天 POP3 一 ``UIDL`` 就把它当新邮件重登记 + 重告警。
        """
        cutoff = now - days * 86400_000
        batch = self.cfg.cleanup_batch
        while True:
            with _tx(self._store) as c:
                rows = c.execute("SELECT id, mailbox, protocol, status, uidl FROM mail_inbox WHERE received_ms < ? LIMIT ?",
                                 (cutoff, batch)).fetchall()
                if not rows:
                    return
                keep: dict[str, list[str]] = {}
                for r in rows:
                    if r["protocol"] == "pop3" and r["status"] in MAIL_NEVER_DELETE and r["uidl"]:
                        keep.setdefault(r["mailbox"], []).append(r["uidl"])
                for mailbox, uidls in keep.items():
                    _merge_pop3_uidls(c, mailbox, uidls, now)
                c.execute(f"DELETE FROM mail_inbox WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
            rep.bump("mail_inbox", len(rows))
            if len(rows) < batch:
                return

    # -- ⑥ 崩溃转储目录
    def _purge_cores(self, rep: CleanupReport, now: int) -> None:
        cores = os.path.join(self.data_dir, "cores")
        cutoff_s = (now - CORES_DAYS * 86400_000) / 1000
        n = 0
        if not os.path.isdir(cores):
            return
        for name in os.listdir(cores):
            path = os.path.join(cores, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff_s:
                    n += _remove_file(path)
            except OSError as e:
                rep.errors.append(f"cores:{name}:{e}")
        rep.files_removed += n
        rep.bump("cores", n)

    # ------------------------------------------------------------ VACUUM(§2.8.4:每周日清理后,增量)
    def incremental_vacuum(self, *, now_ms: Optional[int] = None, force: bool = False) -> bool:
        """每周日跑 ``PRAGMA incremental_vacuum``;**不做全量 VACUUM**。返回是否真的跑了。

        ⚠️ 只有建库时设过 ``auto_vacuum=INCREMENTAL`` 才有效(§2.8.4「建库时设定,之后改不了」);
        现行 ``store.PRAGMAS`` 未设 —— 见 handoff「接线事项」。
        """
        now = now_ms if now_ms is not None else self._clock()
        if not force and datetime.fromtimestamp(now / 1000, tz=TZ_SHANGHAI).weekday() != 6:   # 6 = 周日
            return False
        self._store.con.execute("PRAGMA incremental_vacuum")
        return True

    # ------------------------------------------------------------ 在线备份(§3.3)
    def backup_once(self, *, now_ms: Optional[int] = None) -> str:
        """``sqlite3.Connection.backup()`` 在线拷到 ``<backup_dir>/agent-<yyyymmdd>.db``,保留 ``backup_keep`` 份。"""
        now = now_ms if now_ms is not None else self._clock()
        os.makedirs(self.backup_cfg.backup_dir, exist_ok=True)
        stamp = datetime.fromtimestamp(now / 1000, tz=TZ_SHANGHAI).strftime("%Y%m%d")
        path = os.path.join(self.backup_cfg.backup_dir, f"agent-{stamp}.db")
        dest = sqlite3.connect(path)
        try:
            self._store.con.backup(dest)          # 在线备份:WAL 下不阻塞读,也不需要自己开事务
        finally:
            dest.close()
        kept = self.prune_backups()
        self._store.insert_audit(kind="system", transport="local", actor="system:maintenance", action="db.backup",
                                 detail={"path": path, "kept": kept}, now_ms=now)
        return path

    def prune_backups(self) -> int:
        """只留最新 ``backup_keep`` 份(§3.3「保留 7 份」)。返回保留数。"""
        d = self.backup_cfg.backup_dir
        if not os.path.isdir(d):
            return 0
        files = sorted((f for f in os.listdir(d) if f.startswith("agent-") and f.endswith(".db")), reverse=True)
        for name in files[self.backup_cfg.backup_keep:]:
            _remove_file(os.path.join(d, name))
        return min(len(files), self.backup_cfg.backup_keep)


# ---------------------------------------------------------------- 小工具
def _remove_file(path: str) -> int:
    """存在即删、不存在跳过(幂等);返回删掉的文件数(0/1)。"""
    if not path:
        return 0
    try:
        os.remove(path)
        return 1
    except FileNotFoundError:
        return 0
    except OSError as e:
        log.warning("删除文件失败 %s: %s", path, e)
        return 0


def _media_ids(media_json: Optional[str]) -> list[int]:
    if not media_json or media_json == "[]":
        return []
    try:
        items = json.loads(media_json)
    except ValueError:
        return []
    return [int(it["media_id"]) for it in items if isinstance(it, dict) and isinstance(it.get("media_id"), int)]


def _merge_pop3_uidls(c, mailbox: str, uidls: list[str], now_ms: int) -> None:
    """并进 ``cursors(owner='mail:<mailbox>', kind='pop3_uidl_recent')``,最多 2000 个;
    名额不足按 LRU 挤出,**``NEVER_DELETE`` 的 uidl 优先保留、最后被挤**(§2.8.4 ⑤ / §2.8.3 同款注)。"""
    owner, kind = f"mail:{mailbox}", "pop3_uidl_recent"
    row = c.execute("SELECT value FROM cursors WHERE owner=? AND kind=?", (owner, kind)).fetchone()
    try:
        current = json.loads(row["value"]) if row and row["value"] else []
    except (ValueError, TypeError):
        current = []
    if not isinstance(current, list):
        current = []
    merged = [u for u in current if u not in set(uidls)]
    merged = uidls + merged                       # 新并进来的 NEVER_DELETE 排前面 = 最后被挤
    if len(merged) > POP3_UIDL_RECENT_MAX:
        merged = merged[:POP3_UIDL_RECENT_MAX]
    c.execute("INSERT INTO cursors(owner, kind, value, value_int, updated_ms) VALUES (?,?,?,NULL,?) "
              "ON CONFLICT(owner, kind) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
              (owner, kind, json.dumps(merged, ensure_ascii=False), now_ms))
