"""邮件定期删除 —— 规格:docs/06 §2.6(判据/归档/三种触发/容量分支/协议差异/周期互斥/归档滚动/日志/失败/磁盘水位)。

🔴 **五处 ``NEVER_DELETE`` 门(R6-1 + R6-26)**——缺一处这条就作废:
① ``eligible()``(``mail_store.inbox_eligible`` 的 SQL 里);② ② 容量循环;③ **③ 按条数 ``max_kept_count``**;
④ IMAP 终态 ``MOVE``/``COPY``+``\\Deleted``(``on_terminal``);⑤ POP3 ``cleanup_pop3`` 的 ``DELE`` 集合。
(§2.1 ``run_cycle()`` 的 OVERSIZE 分支 ``continue`` 不落 move 队列,是门 ④ 的上游保证,在 ``fetcher`` 里。)

⚠️ 两个「满」别混(§2.6.10):**邮箱满**(服务器侧配额 → 删远端)与**本地磁盘满**(→ 只清本地)。
``delete_on_server`` **只能由邮箱侧配额水位触发**,本地磁盘水位永远不触发它(基线 §11.11 [DISK] R4-2)。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .backends import MailTransientError
from .codes import (MAIL_ALERT_CODES, MAIL_CLEANUP_FAILED, MAIL_PAUSED_DISK_FULL, MAIL_QUOTA_HIGH,
                    NEVER_DELETE, OUT_OF_SCOPE, OVERSIZE)
from .config import MailConfig
from .mail_store import MailStore
from .routes import MailRoute

log = logging.getLogger("qtrade.mail.cleanup")

TRIGGER_RETENTION = "retention"
TRIGGER_CAPACITY = "capacity"
TRIGGER_MAX_KEPT = "max_kept"           # 🔴 R6-26/R6-31:第三条会删远端的路径,02 DDL 的 CHECK 已补此值
TRIGGER_MANUAL = "manual"
TRIGGER_ARCHIVE_ROTATION = "archive_rotation"


@dataclass
class CleanupStats:
    trigger: str = TRIGGER_RETENTION
    candidates: int = 0
    archived: int = 0
    deleted: int = 0
    failed: int = 0
    bytes_freed: int = 0
    quota_used_before: Optional[int] = None
    quota_used_after: Optional[int] = None
    quota_limit: Optional[int] = None
    quota_source: str = "unknown"
    archive_rotated_files: int = 0
    archive_rotated_bytes: int = 0
    status: str = "OK"
    error: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)
    paused_disk: bool = False


class MailCleanup:
    """一个邮箱一份;在 ``mail.inbound`` **同一条线程**里、每轮取信之后执行(§2.6.6,由 fetcher 的 ``cleanup_hook`` 调)。"""

    def __init__(self, mail_store: MailStore, route: MailRoute, cfg: MailConfig, *,
                 clock: Optional[Callable[[], int]] = None, alerts: Any = None,
                 disk_state: Optional[Callable[[], str]] = None):
        self.ms = mail_store
        self.route = route
        self.cfg = cfg
        self.clock = clock or mail_store._now
        self.alerts = alerts
        self.disk_state = disk_state or (lambda: "ok")
        self._fail_rounds: dict[int, int] = {}          # inbox_id → 连续失败轮数(§2.6.9:连续 3 轮告警)
        self.manual_requested = False                    # §2.6.6:手动【立即清理】只置标志,由下一轮线程执行

    @property
    def mailbox(self) -> str:
        return self.route.mailbox_key

    def _alert(self, code: str, *, evidence: Optional[dict[str, Any]] = None, severity: Optional[str] = None) -> None:
        if self.alerts is not None:
            self.alerts.firing(code, subject=f"mailbox:{self.mailbox}",
                               severity=severity or MAIL_ALERT_CODES.get(code, "warn"), evidence=evidence or {})

    # ------------------------------------------------------------------ §2.6.4 容量信息
    def quota(self, backend: Any, protocol: str) -> tuple[Optional[int], Optional[int], str]:
        """返回 ``(used, limit, source)``;``limit is None`` ⇒ 走 §2.6.3 ③「按条数」那条路。"""
        assumed = self.cfg.cleanup.mailbox_quota_mb_assumed * 1024 * 1024
        if protocol == "imap":
            try:
                q = backend.quota()
            except MailTransientError:
                q = None
            if q is not None:
                return int(q[0]), int(q[1]), "imap_quota"
            used = self.ms.inbox_size_sum(mailbox=self.mailbox)           # Σ RFC822.SIZE(范围内、未删)
            return used, (assumed or None), "estimate"
        try:
            _, octets = backend.stat()
        except Exception:                                                 # noqa: BLE001
            return None, (assumed or None), "unknown"
        return int(octets), (assumed or None), "pop3_stat"

    # ------------------------------------------------------------------ §2.6.2 归档(先归档再删)
    def archive(self, row: dict[str, Any], raw: Optional[bytes]) -> bool:
        """``mail/archive/{yyyymm}/{received_ms}_{inbox_id}.eml`` + sidecar;写盘 → fsync → 校验 ``len == size_bytes``。"""
        if raw is None:
            return False
        yyyymm = time.strftime("%Y%m", time.localtime((row.get("received_ms") or 0) / 1000))
        folder = os.path.join(self.cfg.cleanup.archive_dir, yyyymm)
        path = os.path.join(folder, f"{row.get('received_ms')}_{row['id']}.eml")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            size = row.get("size_bytes")
            if size is not None and os.path.getsize(path) != int(size):
                os.remove(path)                                            # 校验不过 → 删半成品,本轮不删该邮件
                return False
            with open(os.path.join(folder, f"{row['id']}.json"), "w", encoding="utf-8") as f:
                json.dump({k: row.get(k) for k in ("id", "status", "reason", "trace_id", "from_addr", "subject",
                                                   "received_ms", "size_bytes")}, f, ensure_ascii=False)
        except OSError as e:
            log.warning("mail: 归档失败 %s:%s", path, e)                   # §2.6.9:先判 ENOSPC → DISK_FULL(由调用方分诊)
            return False
        self.ms.inbox_update(int(row["id"]), archived_path=path, archived_ms=self.clock())
        return True

    def _raw_of(self, row: dict[str, Any], backend: Any, protocol: str,
                uidl2n: Optional[dict[str, int]] = None) -> Optional[bytes]:
        """归档要的原文:``keep_raw`` 落过盘就读盘(不重新下载),否则从服务器取。"""
        if row.get("raw_ref") and os.path.exists(row["raw_ref"]):
            try:
                with open(row["raw_ref"], "rb") as f:
                    return f.read()
            except OSError:
                return None
        try:
            if protocol == "imap":
                backend.select(row.get("folder") or "INBOX", readonly=True)
                return backend.fetch_raw(int(row["uid"]))
            n = (uidl2n or {}).get(row.get("uidl"))
            return backend.retr(n) if n else None
        except Exception:                                                  # noqa: BLE001
            return None

    def _gone_on_server(self, row: dict[str, Any], backend: Any, protocol: str,
                        uidl2n: Optional[dict[str, int]]) -> bool:
        """§2.6.9:``UID`` 不存在 / ``UIDL`` 消失 = 人先在自己的客户端删了,不算失败。"""
        if protocol == "pop3":
            return row.get("uidl") not in (uidl2n or {})
        try:
            backend.select(row.get("folder") or "INBOX", readonly=True)
            backend.fetch_size(int(row["uid"]))
        except Exception:                                                  # noqa: BLE001
            return True
        return False

    # ------------------------------------------------------------------ §2.6.5 删除动作(协议差异)
    def on_terminal(self, backend: Any, row: dict[str, Any]) -> Optional[tuple[str, int]]:
        """🔴 门 ④(§2.6.5 IMAP 第 1 步):进入终态后**先判 ``status ∉ NEVER_DELETE`` 再搬**。

        ``OUT_OF_SCOPE``/``OVERSIZE`` 既不搬进 ``processed_folder``、也不在原夹留 ``\\Deleted``,
        ``mail_inbox.folder/uid`` 保持原夹原 UID 不变。

        返回 ``(原夹, 原 UID)`` = 走了 ``COPY`` + ``\\Deleted`` 那条路(服务器无 ``MOVE``),
        原夹里留下的标删由 §2.6.5 第 3 步统一清(``_purge_source_flags``);走 ``MOVE`` 的返回 ``None``。
        """
        if row.get("status") in NEVER_DELETE:
            return None
        dest = self.route.inbound.processed_folder
        backend.create_folder(dest)
        uid = int(row["uid"])
        src = row.get("folder") or "INBOX"
        backend.select(src, readonly=False)
        caps = backend.capabilities()                                      # 每轮在已建连接上判定,不做启动时一次判
        new_uid = backend.uid_move(uid, dest) if "MOVE" in caps else backend.uid_copy(uid, dest)
        self.ms.inbox_update(int(row["id"]), folder=dest, uid=new_uid)     # 移动后 UID 变化(COPYUID 给出)
        if "MOVE" in caps:
            return None
        backend.store_deleted(uid)
        return src, uid

    def _purge_source_flags(self, backend: Any, marked: dict[str, list[int]]) -> list[dict[str, Any]]:
        """§2.6.5 第 3 步:原夹(INBOX/Junk)里 ``COPY`` 路径留下的 ``\\Deleted`` 标记怎么清。

        每轮对原夹(``folders`` 与本轮 ``COPY`` 过的夹)各看一次:
        有 ``UIDPLUS`` → ``UID EXPUNGE`` **只清我们这几封**;无 ``UIDPLUS`` → 先 ``UID SEARCH DELETED``,
        **标删集合 ⊆ 本轮我们标删的 UID 集合才 ``EXPUNGE``**,否则本轮不清、记 ``DELETE_DEFERRED`` 下轮再看
        (99c 收紧:人用客户端在同一夹标删的**别人的邮件**绝不能被我们连带真删)。
        """
        out: list[dict[str, Any]] = []
        caps = backend.capabilities()
        folders = list(dict.fromkeys(list(marked) + list(self.route.inbound.folders)))
        for folder in folders:
            uids = marked.get(folder, [])
            try:
                backend.select(folder, readonly=False)
                deleted = set(backend.search_deleted())
            except Exception:                                              # noqa: BLE001 —— 夹不存在/只读,跳过
                continue
            if not deleted:
                continue
            if "UIDPLUS" in caps:
                if uids:
                    backend.uid_expunge(uids)                              # 只清这几封,他人标删的原样留着
                continue
            if deleted <= set(uids):
                backend.expunge_folder()                                   # 夹里标删的全是我们的,等价于只删自己的
            else:
                # 无 UIDPLUS 且夹里还有**别人**标删的邮件 ⇒ 整夹 EXPUNGE 会连带真删,本轮不清、下轮再看
                out.append({"folder": folder, "action": "DELETE_DEFERRED"})
        return out

    def sweep_terminal(self, backend: Any, protocol: str) -> list[dict[str, Any]]:
        """🔴 门 ④ 的**调用点**(§2.6.5 IMAP 第 1 步):把已进入终态的邮件搬进 ``processed_folder``。

        「处理完立即 MOVE」在实现上 = **本轮取信之后、同一条连接里**(§2.6.6:清理在 ``mail.inbound`` 同一线程、
        ``run_cycle()`` 之后、进 IDLE 之前)——这样 `ACCEPTED → DONE → RECEIPT_SENT` 这类跨轮才收口的状态
        也会在下一轮被搬走,而不是永远留在 INBOX 让第 2 步「到期删除只在专用夹里做」落空。
        ``NEVER_DELETE`` 两类与 POP3(无文件夹概念)不走这条路。
        """
        if protocol != "imap":
            return []
        dest = self.route.inbound.processed_folder
        marked: dict[str, list[int]] = {}
        for row in self.ms.inbox_terminal_outside(mailbox=self.mailbox, dest=dest):
            try:
                src = self.on_terminal(backend, row)
            except Exception as e:                                         # noqa: BLE001 —— 搬不动不阻塞本轮清理
                log.warning("mail: 终态邮件搬入 %s 失败(inbox_id=%s):%s", dest, row.get("id"), e)
                continue
            if src is not None:                                            # COPY 路径:原夹留了 \Deleted
                marked.setdefault(src[0], []).append(src[1])
        return self._purge_source_flags(backend, marked)

    def _delete_imap(self, backend: Any, rows: list[dict[str, Any]]) -> tuple[int, int, list[dict[str, Any]]]:
        """§2.6.5 第 2 步:到期删除**只在专用夹里做**;无 ``UIDPLUS`` 才对**该夹**整夹 ``EXPUNGE``。"""
        deleted = bytes_freed = 0
        detail: list[dict[str, Any]] = []
        dest = self.route.inbound.processed_folder
        caps = backend.capabilities()
        by_folder: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_folder.setdefault(r.get("folder") or "INBOX", []).append(r)
        for folder, items in by_folder.items():
            backend.select(folder, readonly=False)
            marked: list[int] = []
            for r in items:
                try:
                    backend.store_deleted(int(r["uid"]))
                    marked.append(int(r["uid"]))
                except Exception as e:                                     # noqa: BLE001
                    detail.append({"inbox_id": r["id"], "uid": r.get("uid"), "action": "store", "error": str(e)})
            if not marked:
                continue
            if "UIDPLUS" in caps:
                backend.uid_expunge(marked)
            elif folder == dest:
                backend.expunge_folder()                                   # 夹内全是我们的邮件,等价于只删自己的
            else:
                # 原夹:标删集合 ⊆ 本轮我们标删的 UID 集合才 EXPUNGE,否则记 DELETE_DEFERRED 下轮再看
                if set(backend.search_deleted()) <= set(marked):
                    backend.expunge_folder()
                else:
                    detail.append({"folder": folder, "action": "DELETE_DEFERRED"})
                    continue
            for r in items:
                if int(r["uid"]) in marked:
                    self.ms.inbox_mark_deleted(int(r["id"]), now_ms=self.clock())
                    deleted += 1
                    bytes_freed += int(r.get("size_bytes") or 0)
        return deleted, bytes_freed, detail

    def cleanup_pop3(self, backend: Any, rows: list[dict[str, Any]]) -> tuple[int, int, list[dict[str, Any]]]:
        """§2.6.5 POP3 伪代码逐字;``deleted_ms`` **只在 ``QUIT`` 收到 ``+OK`` 后写**。"""
        detail: list[dict[str, Any]] = []
        uidl2n = {u: n for n, u in backend.uidl()}
        marked: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") in NEVER_DELETE:                          # 🔴 门 ⑤ 双保险
                continue
            n = uidl2n.get(row.get("uidl"))
            if n is None:
                self.ms.inbox_mark_deleted(int(row["id"]), now_ms=self.clock(), reason="gone_on_server")
                detail.append({"inbox_id": row["id"], "uidl": row.get("uidl"), "action": "gone_on_server"})
                continue
            backend.dele(n)
            marked.append(row)
        if not backend.quit():
            # QUIT 失败/断线 → 服务器撤销全部 DELE;本轮记 DELETE_FAILED,下轮重来(归档已完成,幂等)
            detail.append({"action": "DELETE_FAILED", "reason": "quit_not_ok"})
            return 0, 0, detail
        deleted = bytes_freed = 0
        for row in marked:
            self.ms.inbox_mark_deleted(int(row["id"]), now_ms=self.clock())
            deleted += 1
            bytes_freed += int(row.get("size_bytes") or 0)
        return deleted, bytes_freed, detail

    # ------------------------------------------------------------------ 一轮
    def run(self, backend: Any, protocol: str, *, trigger: str = TRIGGER_RETENTION,
            now_ms: Optional[int] = None) -> CleanupStats:
        """§2.6.3 三种触发,顺序 ② 容量 → ③ 条数(仅当拿不到 limit)→ ① 保留期。"""
        stats = CleanupStats(trigger=trigger)
        now = now_ms or self.clock()
        if not (self.cfg.enabled and self.cfg.cleanup.enabled):
            return stats
        disk = self.disk_state()
        if disk == "critical":
            # §2.6.10:整轮跳过归档与删除(本地盘满,该清的是本地,不动用户邮箱)
            stats.paused_disk = True
            stats.status = "PARTIAL"
            stats.error = "disk_critical"
            self._alert(MAIL_PAUSED_DISK_FULL, evidence={"reason": "disk_critical"})
            self._log(stats, protocol, now, now)
            return stats
        archive_enabled = self.cfg.cleanup.archive_before_delete and disk != "high"   # high:停归档

        started = now
        swept = self.sweep_terminal(backend, protocol)   # §2.6.5 第 1 步:终态邮件先搬进 processed_folder
        used, limit, source = self.quota(backend, protocol)
        stats.quota_used_before, stats.quota_limit, stats.quota_source = used, limit, source

        victims: list[dict[str, Any]] = []
        if limit and used is not None:
            victims += self._pick_capacity(used, limit)                    # ② 容量水位
            stats.trigger = TRIGGER_CAPACITY if victims else stats.trigger
        elif limit is None:
            got = self._pick_max_kept()                                    # ③ 按条数(拿不到容量信息时的最后一道)
            if got:
                victims += got
                stats.trigger = TRIGGER_MAX_KEPT
        victims += self._pick_retention(now)                               # ① 保留期
        # 去重(同一封可能被两条路径同时选中)
        seen: set[int] = set()
        cands = []
        for r in victims:
            if int(r["id"]) not in seen:
                seen.add(int(r["id"]))
                cands.append(r)
        stats.candidates = len(cands)

        uidl2n = None
        if protocol == "pop3":
            uidl2n = {u: n for n, u in backend.uidl()}
        archived_rows: list[dict[str, Any]] = []
        detail_items: list[dict[str, Any]] = list(swept)
        for row in cands:
            if archive_enabled and not row.get("archived_ms"):
                raw = self._raw_of(row, backend, protocol, uidl2n)
                if raw is None and self._gone_on_server(row, backend, protocol, uidl2n):
                    # §2.6.9:服务器上已不存在该封(人先删了)⇒ 记 deleted_ms + reason,**不算失败**
                    self.ms.inbox_mark_deleted(int(row["id"]), now_ms=self.clock(), reason="gone_on_server")
                    stats.deleted += 1
                    detail_items.append({"inbox_id": row["id"], "action": "gone_on_server"})
                    continue
                if not self.archive(row, raw):
                    stats.failed += 1
                    n = self._fail_rounds.get(int(row["id"]), 0) + 1
                    self._fail_rounds[int(row["id"])] = n
                    detail_items.append({"inbox_id": row["id"], "action": "ARCHIVE_FAILED"})
                    if n >= self.cfg.cleanup.stall_alert_rounds:
                        self._alert(MAIL_CLEANUP_FAILED, evidence={"inbox_id": row["id"], "rounds": n})
                    continue                                               # 归档失败 → 本轮不删该邮件
                stats.archived += 1
                self._fail_rounds.pop(int(row["id"]), None)
            elif not archive_enabled:
                # §2.6.10 high:只标 deleted_ms、不落 .eml/sidecar;reason += archive_skipped_disk_high
                self.ms.inbox_update(int(row["id"]),
                                     reason=(row.get("reason") or "") + ";archive_skipped_disk_high")
            archived_rows.append(self.ms.inbox_get(int(row["id"])) or row)

        if archived_rows:
            if protocol == "imap":
                d, freed, det = self._delete_imap(backend, archived_rows)
            else:
                d, freed, det = self.cleanup_pop3(backend, archived_rows)
            stats.deleted, stats.bytes_freed = d, freed
            detail_items += det

        used_after, _, _ = self.quota(backend, protocol)
        stats.quota_used_after = used_after
        if limit and used_after is not None and used_after / limit >= self.cfg.cleanup.mailbox_quota_watermark:
            # 可删的删光了还超 → 只能人来;**不拿 NEVER_DELETE 两类凑数**
            self._alert(MAIL_QUOTA_HIGH, severity="crit" if used_after / limit >= 0.95 else "warn",
                        evidence={"used": used_after, "limit": limit})
        if stats.trigger == TRIGGER_MAX_KEPT and self.ms.inbox_kept_count(mailbox=self.mailbox) > self.cfg.cleanup.max_kept_count:
            self._alert(MAIL_QUOTA_HIGH, evidence={"reason": "max_kept_exceeded"})

        stats.detail = {
            "items": detail_items,
            # 🔴 R6-26:门真的生效了的唯一可观测证据(P-MAIL 显示「本轮跳过 n 封(永不删)」,§8b 断言点)
            "skipped_oversize": self._count_never(OVERSIZE),
            "skipped_out_of_scope": self._count_never(OUT_OF_SCOPE),
        }
        if stats.failed:
            stats.status = "PARTIAL" if stats.deleted else "FAILED"
        self._log(stats, protocol, started, self.clock())
        return stats

    def _count_never(self, status: str) -> int:
        r = self.ms.con.execute(
            "SELECT COUNT(*) AS n FROM mail_inbox WHERE mailbox=? AND deleted_ms IS NULL AND status=?",
            (self.mailbox, status)).fetchone()
        return int(r["n"])

    def _pick_retention(self, now: int) -> list[dict[str, Any]]:
        """① 按保留期(每 ``cleanup_interval_min`` 一轮);``DONE`` 但回执还在队列里的不删(§2.6.1 末)。"""
        rows = self.ms.inbox_eligible(mailbox=self.mailbox,
                                      retention_ms=self.cfg.cleanup.retention_days * 86400_000, now_ms=now,
                                      archive_required=False, limit=self.cfg.cleanup.cleanup_batch)
        return [r for r in rows if not self.ms.outbox_has_pending_receipt(int(r["id"]))]

    def _pick_capacity(self, used: int, limit: int) -> list[dict[str, Any]]:
        """② 按容量水位:``usage/limit ≥ watermark`` 时删到 ``(watermark − 0.1) × limit``(滞回 10%)。"""
        wm = self.cfg.cleanup.mailbox_quota_watermark
        if limit <= 0 or used / limit < wm:
            return []
        target = (wm - 0.1) * limit
        picked: list[dict[str, Any]] = []
        for row in self.ms.inbox_terminal_oldest(mailbox=self.mailbox, limit=self.cfg.cleanup.cleanup_batch):
            if self.ms.outbox_has_pending_receipt(int(row["id"])):
                continue
            picked.append(row)
            used -= int(row.get("size_bytes") or 0)
            if used <= target:
                break
        return picked

    def _pick_max_kept(self) -> list[dict[str, Any]]:
        """③ 按条数 ``max_kept_count``:分母与候选**都**不含 ``NEVER_DELETE``(🔴 R6-26)。"""
        kept = self.ms.inbox_kept_count(mailbox=self.mailbox)
        over = kept - self.cfg.cleanup.max_kept_count
        if over <= 0:
            return []
        rows = self.ms.inbox_terminal_oldest(mailbox=self.mailbox, limit=over)
        return [r for r in rows if not self.ms.outbox_has_pending_receipt(int(r["id"]))]

    def _log(self, stats: CleanupStats, protocol: str, started: int, finished: int) -> int:
        return self.ms.cleanup_log_insert(
            started_ms=started, finished_ms=finished, trigger=stats.trigger, protocol=protocol,
            folder=self.route.inbound.folders[0] if protocol == "imap" and self.route.inbound.folders else "",
            candidates=stats.candidates, archived=stats.archived, deleted=stats.deleted, failed=stats.failed,
            bytes_freed=stats.bytes_freed, quota_used_before=stats.quota_used_before,
            quota_used_after=stats.quota_used_after, quota_limit=stats.quota_limit,
            quota_source=stats.quota_source, archive_rotated_files=stats.archive_rotated_files,
            archive_rotated_bytes=stats.archive_rotated_bytes, status=stats.status, error=stats.error,
            detail=stats.detail)

    # ------------------------------------------------------------------ §2.6.7 归档自身的滚动清理
    def rotate_archive(self, *, now_ms: Optional[int] = None) -> CleanupStats:
        """每轮清理末尾:删早于 ``archive_retention_days`` 的 ``.eml``/``.json``;``archive_max_mb`` 超出按最旧删到 80%。"""
        stats = CleanupStats(trigger=TRIGGER_ARCHIVE_ROTATION)
        now = now_ms or self.clock()
        root = self.cfg.cleanup.archive_dir
        if not os.path.isdir(root):
            return stats
        # 判据是**文件修改时间**(§2.6.7),比较对象来自文件系统,故 cutoff 也用系统时钟;
        # 行/邮件那两把尺子才用注入时钟(可拨时钟只拨业务时间,拨不动 mtime)。
        cutoff = time.time() - self.cfg.cleanup.archive_retention_days * 86400
        files: list[tuple[float, int, str]] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
        for mtime, size, p in sorted(files):
            if mtime >= cutoff:
                continue
            try:
                os.remove(p)
            except OSError:
                continue
            stats.archive_rotated_files += 1
            stats.archive_rotated_bytes += size
            self._clear_archive_ref(p)
        remain = [(m, s, p) for m, s, p in files if os.path.exists(p)]
        total = sum(s for _, s, _ in remain)
        cap = self.cfg.cleanup.archive_max_mb * 1024 * 1024
        if total > cap:
            for mtime, size, p in sorted(remain):
                if total <= cap * 0.8:
                    break
                try:
                    os.remove(p)
                except OSError:
                    continue
                total -= size
                stats.archive_rotated_files += 1
                stats.archive_rotated_bytes += size
                self._clear_archive_ref(p)
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath != root and not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)                                      # 整月目录空了删目录
                except OSError:
                    pass
        self._log(stats, self.route.inbound.protocol, now, self.clock())
        return stats

    def _clear_archive_ref(self, path: str) -> None:
        """归档过期只置空路径,行不删(P-MAIL 显示「归档已过期」;行本身到 ``mail_inbox_rows_days`` 再删)。"""
        with self.ms._store._tx() as c:
            c.execute("UPDATE mail_inbox SET archived_path=NULL WHERE archived_path=?", (path,))

    # ------------------------------------------------------------------ §2.6.7 行清理(本地行,不是服务器上的邮件)
    def purge_rows(self, *, now_ms: Optional[int] = None) -> int:
        now = now_ms or self.clock()
        older = now - self.cfg.retention.mail_inbox_rows_days * 86400_000
        return self.ms.purge_inbox_rows(owner=self.route.cursor_owner, older_than_ms=older, now_ms=now)
