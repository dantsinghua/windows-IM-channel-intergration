"""邮件三表 + 邮件游标的读写 —— 规格:docs/06 §3.1(``mail_inbox``/``mail_outbox``/``mail_cleanup_log`` 列定案)、§2.9.3(``cursors`` 键)。

**薄封装**:复用 ``store.Store`` 的单写连接与 ``_tx_lock``(02 §2.2.8「唯一读写 ``agent.db`` 的地方」),
不另开连接、不改 ``store/`` 包。DDL 已在 ``store/schema_agent.sql``(逐字抽自 02 §3.1),本模块只写 SQL。

关键口径:
- 🔴 R6-20 原子 claim:``approve``/``reject`` 一条 ``UPDATE … WHERE id=:id AND status='CONFIRM_REQUIRED' AND confirm_expires_ms > :now_ms``,``rowcount==1`` 才继续。
- 🔴 R6-19:``confirm_expires_ms`` **一经写入即保留**,approve/reject/过期都不清。
- 🔴 R6-26:删 ``mail_inbox`` 行与把该行 ``uidl`` 写进 ``pop3_uidl_recent`` **必须同一事务**;LRU 挤出时 ``NEVER_DELETE`` 的 ``uidl`` 最后被挤。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

from .codes import CONFIRM_EXPIRED, CONFIRM_REQUIRED, NEVER_DELETE, TERMINAL

POP3_UIDL_RECENT_MAX = 2000         # §2.9.3:``pop3_uidl_recent`` = 最近 2000 个 UIDL
CURSOR_IMAP_UID = "imap_uid:{folder}"       # §2.9.3:**每文件夹一条**(02 写的不带夹名,多夹时须带)
CURSOR_POP3_STAT = "pop3_stat"
CURSOR_POP3_UIDL_RECENT = "pop3_uidl_recent"
CURSOR_PROTOCOL_STATE = "protocol_state"    # §2.1.1:每个 mailbox_key 一份回落状态

_IN_TERMINAL = ",".join("?" * len(TERMINAL))
_IN_NEVER = ",".join("?" * len(NEVER_DELETE))
_TERMINAL_ARGS = tuple(sorted(TERMINAL))
_NEVER_ARGS = tuple(sorted(NEVER_DELETE))


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


class MailStore:
    """邮件侧的 store 门面;所有写走 ``Store._tx()``(单写连接 + 事务整体互斥)。"""

    def __init__(self, store: Any):
        self._store = store

    @property
    def con(self) -> sqlite3.Connection:
        return self._store.con

    def _now(self) -> int:
        return self._store._clock()

    def columns(self, table: str) -> set[str]:
        """库里真有的列。

        ⚠️ 06 §3.1 提的两列在 02 v1 DDL(= ``store/schema_agent.sql``)里**还没建**:
        ``mail_inbox.effective_protocol``(v0.3 E-1)与 ``mail_outbox.render_notes``(v0.3 E-4)。
        DDL 的 owner 是 02、``store/`` 包不归本批改,故写入时按本方法过滤未建的列(见 handoff「建议裁决 ⑤」)。
        """
        key = f"_cols_{table}"
        cached = getattr(self, key, None)
        if cached is None:
            cached = {str(r[1]) for r in self.con.execute(f"PRAGMA table_info({table})").fetchall()}
            setattr(self, key, cached)
        return cached

    def _known(self, table: str, cols: dict[str, Any]) -> dict[str, Any]:
        have = self.columns(table)
        return {k: v for k, v in cols.items() if k in have}

    # ================================================================ mail_routes
    def routes_list(self) -> list[dict[str, Any]]:
        return _rows(self.con.execute("SELECT * FROM mail_routes ORDER BY id"))

    def route_upsert(self, *, channel: Optional[str], account_id: Optional[str],
                     inbound_json: dict[str, Any], outbound_json: dict[str, Any],
                     enabled: bool = True, now_ms: Optional[int] = None) -> int:
        """按 ``(COALESCE(channel,''), COALESCE(account_id,''))`` 唯一键 upsert 一条路由(02 §3.1 ``ux_mail_routes_key``)。"""
        now = now_ms or self._now()
        with self._store._tx() as c:
            row = c.execute("SELECT id FROM mail_routes WHERE COALESCE(channel,'')=? AND COALESCE(account_id,'')=?",
                            (channel or "", account_id or "")).fetchone()
            payload = (json.dumps(inbound_json, ensure_ascii=False), json.dumps(outbound_json, ensure_ascii=False),
                       1 if enabled else 0, now)
            if row is not None:
                c.execute("UPDATE mail_routes SET inbound_json=?, outbound_json=?, enabled=?, updated_ms=? WHERE id=?",
                          payload + (row["id"],))
                return int(row["id"])
            cur = c.execute(
                "INSERT INTO mail_routes(channel, account_id, inbound_json, outbound_json, enabled, created_ms, updated_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                (channel, account_id, payload[0], payload[1], payload[2], now, now))
            return int(cur.lastrowid)

    # ================================================================ mail_inbox
    def inbox_insert(self, **cols: Any) -> int:
        """插一行 ``mail_inbox``;返回 ``id``。列名逐字照 06 §3.1 / 02 DDL。"""
        cols.setdefault("received_ms", self._now())
        cols.setdefault("template", "none")
        cols.setdefault("status", "RECEIVED")
        cols.setdefault("to_addrs", "")
        cols.setdefault("folder", "")
        attach = cols.pop("attachments", None)
        if attach is not None:
            cols["attach_json"] = json.dumps(attach, ensure_ascii=False)
            cols["attach_cnt"] = len(attach)
        cols = self._known("mail_inbox", cols)
        names = list(cols.keys())
        sql = f"INSERT INTO mail_inbox({','.join(names)}) VALUES ({','.join('?' * len(names))})"
        with self._store._tx() as c:
            cur = c.execute(sql, tuple(cols[n] for n in names))
            return int(cur.lastrowid)

    def inbox_update(self, inbox_id: int, **cols: Any) -> None:
        cols = self._known("mail_inbox", cols)
        if not cols:
            return
        names = list(cols.keys())
        sql = f"UPDATE mail_inbox SET {','.join(n + '=?' for n in names)} WHERE id=?"
        with self._store._tx() as c:
            c.execute(sql, tuple(cols[n] for n in names) + (inbox_id,))

    def inbox_get(self, inbox_id: int) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM mail_inbox WHERE id=?", (inbox_id,)).fetchone()
        return dict(r) if r else None

    def inbox_by_uid(self, mailbox: str, folder: str, uidvalidity: Optional[int], uid: int) -> Optional[dict[str, Any]]:
        """§2.5 第 1 层:``(mailbox, folder, uidvalidity, uid)`` 唯一——崩溃窗口内同一封再拉到直接跳过。"""
        r = self.con.execute(
            "SELECT * FROM mail_inbox WHERE mailbox=? AND folder=? AND uidvalidity IS ? AND uid=?",
            (mailbox, folder, uidvalidity, uid)).fetchone()
        return dict(r) if r else None

    def inbox_by_uidl(self, mailbox: str, uidl: str) -> Optional[dict[str, Any]]:
        """§2.5 第 1 层(POP3):``(mailbox, uidl)`` 唯一。"""
        r = self.con.execute("SELECT * FROM mail_inbox WHERE mailbox=? AND uidl=?", (mailbox, uidl)).fetchone()
        return dict(r) if r else None

    def inbox_by_message_id(self, rfc_message_id: str) -> Optional[dict[str, Any]]:
        """§2.5 第 2 层:``rfc_message_id`` 唯一(缺失时存 ``sha256-…``)。"""
        r = self.con.execute("SELECT * FROM mail_inbox WHERE rfc_message_id=?", (rfc_message_id,)).fetchone()
        return dict(r) if r else None

    def inbox_by_body_sha(self, body_sha256: str) -> Optional[dict[str, Any]]:
        """§2.5 第 3 层:同内容不同 ``Message-ID`` 的重投落 ``DUPLICATE``。"""
        r = self.con.execute("SELECT * FROM mail_inbox WHERE body_sha256=? ORDER BY id LIMIT 1",
                             (body_sha256,)).fetchone()
        return dict(r) if r else None

    def inbox_by_req(self, *, req_id: str, from_addr: str, exclude_id: int,
                     status: Optional[str] = None) -> Optional[dict[str, Any]]:
        """§2.5 第 4 层的定位:同一发件人的同 ``req_id``(等价于同 ``mail:{短名}:{req_id}`` 幂等键)。

        ⚠️ 不用 ``idempotency_key`` 列定位:06 §3.1 提了这一列,02 v1 DDL(``schema_agent.sql``)里**还没建**
        (handoff「建议裁决 ⑤」);``(req_id, from_addr)`` 与改写后的键一一对应,不依赖那一列。
        """
        sql = "SELECT * FROM mail_inbox WHERE req_id=? AND from_addr=? AND id<>?"
        args: list[Any] = [req_id, from_addr, exclude_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        r = self.con.execute(sql + " ORDER BY id LIMIT 1", tuple(args)).fetchone()
        return dict(r) if r else None

    def inbox_nonce_seen(self, from_addr: str, nonce: str) -> Optional[dict[str, Any]]:
        """§2.3.4 防重放:``(from_addr, nonce)`` 唯一索引;同发件人 24h 内重复 → ``DUPLICATE_NONCE``。"""
        r = self.con.execute("SELECT * FROM mail_inbox WHERE from_addr=? AND nonce=? ORDER BY id LIMIT 1",
                             (from_addr, nonce)).fetchone()
        return dict(r) if r else None

    def inbox_purge_nonces(self, older_than_ms: int) -> int:
        """§2.3.4:``nonce`` 保留 ``nonce_ttl_h = 24`` 后可清(只清 nonce 列,不动行)。"""
        with self._store._tx() as c:
            cur = c.execute("UPDATE mail_inbox SET nonce=NULL WHERE nonce IS NOT NULL AND received_ms < ?",
                            (older_than_ms,))
            return cur.rowcount

    def inbox_list(self, *, status: Optional[str] = None, since_ms: Optional[int] = None,
                   until_ms: Optional[int] = None, q: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        """§2.7 收件时间线:``GET /mail/inbox?status&since&until&q&limit&cursor``(``q`` 只搜主题与发件人)。"""
        sql = ["SELECT * FROM mail_inbox WHERE 1=1"]
        args: list[Any] = []
        if status:
            sql.append("AND status=?")
            args.append(status)
        if since_ms is not None:
            sql.append("AND received_ms >= ?")
            args.append(since_ms)
        if until_ms is not None:
            sql.append("AND received_ms <= ?")
            args.append(until_ms)
        if q:
            sql.append("AND (subject LIKE ? OR from_addr LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        sql.append("ORDER BY received_ms DESC, id DESC LIMIT ?")
        args.append(limit)
        return _rows(self.con.execute(" ".join(sql), tuple(args)))

    def inbox_fail_bump(self, inbox_id: int) -> int:
        """§5「毒邮件」:``fail_count`` 落库(进程重启不归零,照 ibquote ``etl_imap_state``)。"""
        with self._store._tx() as c:
            c.execute("UPDATE mail_inbox SET fail_count = fail_count + 1 WHERE id=?", (inbox_id,))
            r = c.execute("SELECT fail_count FROM mail_inbox WHERE id=?", (inbox_id,)).fetchone()
            return int(r["fail_count"]) if r else 0

    # ---------------------------------------------------------------- 高危待确认(§2.3.6 / §3.2 #68b~#68d)
    def pending_confirms(self, *, now_ms: Optional[int] = None) -> list[dict[str, Any]]:
        """🔴 R6-7:``GET /mail/pending-confirms`` 出参恰八键 ``{id, op, from_addr, account_id, args_digest, created_at, expires_at, remaining_ttl_s}``。

        **不含 ``confirm_via``/``confirm_nonce``**(v1 无此两列)。``remaining_ttl_s`` 由服务端算好。
        """
        now = now_ms or self._now()
        rows = _rows(self.con.execute(
            "SELECT id, op, from_addr, account_id, args_digest, received_ms, confirm_expires_ms"
            "  FROM mail_inbox WHERE status=? ORDER BY confirm_expires_ms", (CONFIRM_REQUIRED,)))
        out = []
        for r in rows:
            exp = r["confirm_expires_ms"] or 0
            out.append({
                "id": r["id"], "op": r["op"], "from_addr": r["from_addr"], "account_id": r["account_id"],
                "args_digest": r["args_digest"], "created_at": r["received_ms"], "expires_at": exp,
                "remaining_ttl_s": max(0, (exp - now) // 1000),
            })
        return out

    def confirm_claim(self, inbox_id: int, *, new_status: str, reason: Optional[str], now_ms: int) -> bool:
        """🔴 R6-20 原子 claim(两册同句):``rowcount==1`` 才继续,否则 ``409 CONFIRM_EXPIRED``。

        ``new_status`` 逐字 = approve→``ACCEPTED``、reject→``OP_DENIED``(并置 ``reason`` 前缀 ``REJECTED:``,R6-34)。
        """
        with self._store._tx() as c:
            cur = c.execute(
                "UPDATE mail_inbox SET status=?, reason=COALESCE(?, reason)"
                " WHERE id=? AND status=? AND confirm_expires_ms > ?",
                (new_status, reason, inbox_id, CONFIRM_REQUIRED, now_ms))
            return cur.rowcount == 1

    def confirm_expire_one(self, inbox_id: int, *, now_ms: int) -> bool:
        """R6-20:``rowcount==0`` 且该行仍 ``CONFIRM_REQUIRED`` 而时刻已过 ⇒ 端点**顺手**置 ``CONFIRM_EXPIRED``,不等 reaper。"""
        with self._store._tx() as c:
            cur = c.execute(
                "UPDATE mail_inbox SET status=?, reason=?"
                " WHERE id=? AND status=? AND confirm_expires_ms IS NOT NULL AND confirm_expires_ms <= ?",
                (CONFIRM_EXPIRED, "CONFIRM_EXPIRED:待确认超过 danger_confirm_ttl_s 未在控制台处理",
                 inbox_id, CONFIRM_REQUIRED, now_ms))
            return cur.rowcount == 1

    def confirm_reaper(self, *, now_ms: int) -> list[dict[str, Any]]:
        """02 §3.1 的规范 SQL(``mail_confirm_reaper``,每 60 s;**只是兜底清扫,安全性不依赖其节拍**)。

        返回被过期掉的行(供调用方逐行记 ``mail.pending_confirm.expired`` 审计,``expired_by='reaper'``)。
        """
        with self._store._tx() as c:
            victims = _rows(c.execute(
                "SELECT id, op, from_addr, account_id, confirm_expires_ms FROM mail_inbox"
                " WHERE status=? AND confirm_expires_ms IS NOT NULL AND confirm_expires_ms <= ?",
                (CONFIRM_REQUIRED, now_ms)))
            if victims:
                c.execute(
                    "UPDATE mail_inbox SET status=?, reason=?"
                    " WHERE status=? AND confirm_expires_ms IS NOT NULL AND confirm_expires_ms <= ?",
                    (CONFIRM_EXPIRED, "CONFIRM_EXPIRED:待确认超过 danger_confirm_ttl_s 未在控制台处理",
                     CONFIRM_REQUIRED, now_ms))
            return victims

    # ---------------------------------------------------------------- 清理候选(§2.6;五处 NEVER_DELETE 门之三)
    def inbox_eligible(self, *, mailbox: str, retention_ms: int, now_ms: int, archive_required: bool,
                       limit: int) -> list[dict[str, Any]]:
        """§2.6.1 ``eligible(row)`` 逐字:终态 ∧ ``status ∉ NEVER_DELETE`` ∧ 归档已完成(若要求)∧ 过保留期。

        「``DONE`` 但回执还在队列里」由调用方另查 ``outbox_has_pending_receipt`` 排除(§2.6.1 末)。
        """
        sql = (f"SELECT * FROM mail_inbox WHERE mailbox=? AND deleted_ms IS NULL"
               f"   AND status IN ({_IN_TERMINAL}) AND status NOT IN ({_IN_NEVER})"
               f"   AND (? - received_ms) >= ?")
        args: list[Any] = [mailbox, *_TERMINAL_ARGS, *_NEVER_ARGS, now_ms, retention_ms]
        if archive_required:
            sql += " AND archived_ms IS NOT NULL"
        sql += " ORDER BY received_ms LIMIT ?"
        args.append(limit)
        return _rows(self.con.execute(sql, tuple(args)))

    def inbox_terminal_oldest(self, *, mailbox: str, limit: int) -> list[dict[str, Any]]:
        """§2.6.3 ②/③ 的候选选择循环:``where status ∈ TERMINAL and status ∉ NEVER_DELETE order by received_ms asc``。"""
        sql = (f"SELECT * FROM mail_inbox WHERE mailbox=? AND deleted_ms IS NULL"
               f"   AND status IN ({_IN_TERMINAL}) AND status NOT IN ({_IN_NEVER})"
               f" ORDER BY received_ms ASC LIMIT ?")
        return _rows(self.con.execute(sql, (mailbox, *_TERMINAL_ARGS, *_NEVER_ARGS, limit)))

    def inbox_terminal_outside(self, *, mailbox: str, dest: str) -> list[dict[str, Any]]:
        """§2.6.5 IMAP 第 1 步的候选:已进入终态、**还没搬进 ``processed_folder``** 的行。

        同样过 ``NEVER_DELETE`` 门(🔴 R6-1 门 ④:那两类连第一步的 MOVE/COPY 都不做,留原夹原位)。
        """
        sql = (f"SELECT * FROM mail_inbox WHERE mailbox=? AND deleted_ms IS NULL AND uid IS NOT NULL"
               f"   AND folder <> ? AND status IN ({_IN_TERMINAL}) AND status NOT IN ({_IN_NEVER})"
               f" ORDER BY id")
        return _rows(self.con.execute(sql, (mailbox, dest, *_TERMINAL_ARGS, *_NEVER_ARGS)))

    def inbox_kept_count(self, *, mailbox: str) -> int:
        """§2.6.3 ③ 的分母:``count(where status ∉ NEVER_DELETE and deleted_ms is null)``——🔴 R6-26:分母也不含这两类。"""
        r = self.con.execute(
            f"SELECT COUNT(*) AS n FROM mail_inbox WHERE mailbox=? AND deleted_ms IS NULL AND status NOT IN ({_IN_NEVER})",
            (mailbox, *_NEVER_ARGS)).fetchone()
        return int(r["n"])

    def inbox_size_sum(self, *, mailbox: str) -> int:
        """§2.6.4「IMAP 无 QUOTA」:``Σ RFC822.SIZE``(本系统范围内、未删邮件)。"""
        r = self.con.execute(
            "SELECT COALESCE(SUM(size_bytes),0) AS s FROM mail_inbox WHERE mailbox=? AND deleted_ms IS NULL",
            (mailbox,)).fetchone()
        return int(r["s"])

    def inbox_mark_deleted(self, inbox_id: int, *, now_ms: int, reason: Optional[str] = None) -> None:
        cols: dict[str, Any] = {"deleted_ms": now_ms}
        if reason:
            cols["reason"] = reason
        self.inbox_update(inbox_id, **cols)

    # ---------------------------------------------------------------- 行清理(§2.6.7 + 🔴 R6-26 两条硬约束)
    def purge_inbox_rows(self, *, owner: str, older_than_ms: int, now_ms: Optional[int] = None) -> int:
        """按 ``[retention] mail_inbox_rows_days`` 删本地行;**与把 ``uidl`` 写进 ``pop3_uidl_recent`` 同一事务**(R6-26 ①)。

        🔴 清的是本地 ``mail_inbox`` 行,**不是服务器上的邮件**(§2.6.7)。``NEVER_DELETE`` 两类的 ``uidl`` 标记为
        受保护,LRU 挤出时最后被挤(R6-26 ②)——它们的原信还在服务器上,被挤出就会复发登记与告警。
        """
        now = now_ms or self._now()
        with self._store._tx() as c:
            victims = _rows(c.execute(
                "SELECT id, uidl, status FROM mail_inbox WHERE received_ms < ? AND uidl IS NOT NULL", (older_than_ms,)))
            deleted = c.execute("DELETE FROM mail_inbox WHERE received_ms < ?", (older_than_ms,)).rowcount
            if victims:
                protected = [v["uidl"] for v in victims if v["status"] in NEVER_DELETE]
                plain = [v["uidl"] for v in victims if v["status"] not in NEVER_DELETE]
                self._uidl_remember(c, owner, plain=plain, protected=protected, now_ms=now)
            return deleted

    def _uidl_recent_raw(self, owner: str) -> tuple[list[str], int]:
        cur = self._store.cursor_get(owner, CURSOR_POP3_UIDL_RECENT)
        if cur is None or not cur.value:
            return [], 0
        try:
            arr = json.loads(cur.value)
        except ValueError:
            return [], 0
        return ([str(x) for x in arr] if isinstance(arr, list) else []), int(cur.value_int or 0)

    def _uidl_remember(self, c: sqlite3.Connection, owner: str, *, plain: Iterable[str],
                       protected: Iterable[str], now_ms: int) -> None:
        """写 ``pop3_uidl_recent``:数组**队首先被挤**,受保护的恒在队尾 ``value_int`` 个位置(R6-26 ②)。"""
        arr, prot_n = self._uidl_recent_raw(owner)
        head, tail = (arr[: len(arr) - prot_n], arr[len(arr) - prot_n:]) if prot_n else (arr, [])
        for u in plain:
            if u and u not in head and u not in tail:
                head.append(u)
        for u in protected:
            if not u:
                continue
            if u in head:
                head.remove(u)
            if u not in tail:
                tail.append(u)
        overflow = len(head) + len(tail) - POP3_UIDL_RECENT_MAX
        if overflow > 0:
            head = head[overflow:] if overflow <= len(head) else []    # 先挤普通(原信早已 DELE,挤掉无害)
        c.execute("INSERT INTO cursors(owner, kind, value, value_int, updated_ms) VALUES (?,?,?,?,?)"
                  " ON CONFLICT(owner, kind) DO UPDATE SET value=excluded.value, value_int=excluded.value_int,"
                  " updated_ms=excluded.updated_ms",
                  (owner, CURSOR_POP3_UIDL_RECENT, json.dumps(head + tail, ensure_ascii=False), len(tail), now_ms))

    def uidl_recent(self, owner: str) -> list[str]:
        """§2.9.3:快速过滤用;**真去重靠 ``mail_inbox(mailbox, uidl)`` 唯一索引**(UIDL 不单调,不能当水位)。"""
        arr, _ = self._uidl_recent_raw(owner)
        return arr

    def uidl_remember(self, owner: str, uidl: str, *, protected: bool = False, now_ms: Optional[int] = None) -> None:
        now = now_ms or self._now()
        with self._store._tx() as c:
            self._uidl_remember(c, owner, plain=[] if protected else [uidl],
                                protected=[uidl] if protected else [], now_ms=now)

    # ================================================================ mail_outbox
    def outbox_enqueue(self, *, kind: str, to_addrs: str, subject: str, body_text: str,
                       rfc_message_id: str, dedup_key: str, template_version: str,
                       cc_addrs: str = "", body_html: Optional[str] = None,
                       attachments_json: Optional[str] = None, in_reply_to: Optional[str] = None,
                       references_hdr: Optional[str] = None, ref_inbox_id: Optional[int] = None,
                       ref_message_id: Optional[str] = None, ref_trace_id: Optional[str] = None,
                       route_id: Optional[int] = None, template_id: Optional[int] = None,
                       template_profile: str = "qtrade-v1", now_ms: Optional[int] = None) -> Optional[int]:
        """入队一封出站邮件;``dedup_key`` 撞唯一键返回 ``None``(§3.1:防同一条消息/回执重复入队)。"""
        now = now_ms or self._now()
        with self._store._tx() as c:
            try:
                cur = c.execute(
                    "INSERT INTO mail_outbox(kind, route_id, to_addrs, cc_addrs, subject, body_text, body_html,"
                    " attachments_json, rfc_message_id, in_reply_to, references_hdr, ref_inbox_id, ref_message_id,"
                    " ref_trace_id, template_id, template_version, template_profile, status, attempts, next_attempt_ms,"
                    " dedup_key, created_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'QUEUED', 0, 0, ?, ?)",
                    (kind, route_id, to_addrs, cc_addrs, subject, body_text, body_html,
                     attachments_json or "[]", rfc_message_id, in_reply_to, references_hdr, ref_inbox_id,
                     ref_message_id, ref_trace_id, template_id, template_version, template_profile, dedup_key, now))
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def outbox_get(self, outbox_id: int) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM mail_outbox WHERE id=?", (outbox_id,)).fetchone()
        return dict(r) if r else None

    def outbox_due(self, *, now_ms: int, limit: int = 50) -> list[dict[str, Any]]:
        """§2.1:队列消费单线程、按 ``next_attempt_ms`` 排序;一封失败不阻塞后面。"""
        return _rows(self.con.execute(
            "SELECT * FROM mail_outbox WHERE status IN ('QUEUED','RETRY') AND next_attempt_ms <= ?"
            " ORDER BY next_attempt_ms, id LIMIT ?", (now_ms, limit)))

    def outbox_update(self, outbox_id: int, **cols: Any) -> None:
        cols = self._known("mail_outbox", cols)
        if not cols:
            return
        names = list(cols.keys())
        with self._store._tx() as c:
            c.execute(f"UPDATE mail_outbox SET {','.join(n + '=?' for n in names)} WHERE id=?",
                      tuple(cols[n] for n in names) + (outbox_id,))

    def outbox_list(self, *, status: Optional[str] = None, kind: Optional[str] = None,
                    limit: int = 100) -> list[dict[str, Any]]:
        sql = ["SELECT * FROM mail_outbox WHERE 1=1"]
        args: list[Any] = []
        if status:
            sql.append("AND status=?")
            args.append(status)
        if kind:
            sql.append("AND kind=?")
            args.append(kind)
        sql.append("ORDER BY created_ms DESC, id DESC LIMIT ?")
        args.append(limit)
        return _rows(self.con.execute(" ".join(sql), tuple(args)))

    def outbox_has_pending_receipt(self, inbox_id: int) -> bool:
        """§2.6.1 末:``DONE`` 但回执还在 ``mail_outbox`` 队列里(``QUEUED``/``RETRY``)的不删。"""
        r = self.con.execute(
            "SELECT 1 FROM mail_outbox WHERE ref_inbox_id=? AND kind='receipt' AND status IN ('QUEUED','RETRY') LIMIT 1",
            (inbox_id,)).fetchone()
        return r is not None

    def outbox_counts(self) -> dict[str, int]:
        """§2.7 ``GET /mail/status`` 的 ``outbound`` 段:``queued/retrying/dead``。"""
        rows = _rows(self.con.execute("SELECT status, COUNT(*) AS n FROM mail_outbox GROUP BY status"))
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"queued": by.get("QUEUED", 0), "retrying": by.get("RETRY", 0), "dead": by.get("DEAD", 0),
                "sent": by.get("SENT", 0), "discarded": by.get("DISCARDED", 0)}

    # ================================================================ mail_cleanup_log
    def cleanup_log_insert(self, **cols: Any) -> int:
        """§2.6.8:每轮一行;``detail_json`` 必带 ``skipped_oversize``/``skipped_out_of_scope``(R6-26 门生效的唯一可观测证据)。"""
        detail = cols.pop("detail", None)
        if detail is not None:
            cols["detail_json"] = json.dumps(detail, ensure_ascii=False)
        cols.setdefault("detail_json", "{}")
        names = list(cols.keys())
        with self._store._tx() as c:
            cur = c.execute(f"INSERT INTO mail_cleanup_log({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                            tuple(cols[n] for n in names))
            return int(cur.lastrowid)

    def cleanup_log_list(self, limit: int = 50) -> list[dict[str, Any]]:
        return _rows(self.con.execute("SELECT * FROM mail_cleanup_log ORDER BY started_ms DESC, id DESC LIMIT ?",
                                      (limit,)))

    # ================================================================ cursors(§2.9.3 邮件五行)
    def imap_watermark(self, owner: str, folder: str) -> tuple[Optional[int], int]:
        """``imap_uid:<folder>``:``value = uidvalidity``、``value_int = last_uid``;每文件夹一条。"""
        cur = self._store.cursor_get(owner, CURSOR_IMAP_UID.format(folder=folder))
        if cur is None:
            return None, 0
        uv = int(cur.value) if (cur.value or "").isdigit() else None
        return uv, int(cur.value_int or 0)

    def imap_watermark_set(self, owner: str, folder: str, *, uidvalidity: Optional[int], last_uid: int) -> None:
        self._store.cursor_set(owner, CURSOR_IMAP_UID.format(folder=folder), last_uid,
                               str(uidvalidity) if uidvalidity is not None else None)

    def pop3_stat_set(self, owner: str, *, count: int, octets: int) -> None:
        self._store.cursor_set(owner, CURSOR_POP3_STAT, octets,
                               json.dumps({"count": count, "octets": octets}, ensure_ascii=False))

    def pop3_stat(self, owner: str) -> dict[str, Any]:
        cur = self._store.cursor_get(owner, CURSOR_POP3_STAT)
        return cur.value_json() if cur is not None else {}

    def protocol_state(self, owner: str) -> dict[str, Any]:
        """§2.1.1:每个 ``mailbox_key`` 一份回落状态(``cursors(owner=mail:<mailbox>, kind=protocol_state)``)。"""
        cur = self._store.cursor_get(owner, CURSOR_PROTOCOL_STATE)
        return cur.value_json() if cur is not None else {}

    def protocol_state_set(self, owner: str, state: dict[str, Any]) -> None:
        self._store.cursor_set(owner, CURSOR_PROTOCOL_STATE, None, json.dumps(state, ensure_ascii=False))

    # ------------------------------------------------------------------ C-42 统一分页(02 §3.4 通用段;游标 G-16)
    #: 🔴 只新增,不动既有 `inbox_list` / `outbox_list`(投递器与清理器还在按原签名调)。
    #: 排序列 = 游标里的 `ts_ms` 同一列(收件按 `received_ms`、出件按 `created_ms`),翻页期间进新邮件也不重不漏。

    def inbox_list_page(self, *, status: Optional[str] = None, since_ms: Optional[int] = None,
                        until_ms: Optional[int] = None, q: Optional[str] = None, limit: int = 100,
                        before: Optional[tuple[int, str]] = None) -> list[dict[str, Any]]:
        """#58 的分页视图:``(received_ms, id)`` 降序;``before`` = 上一页末行 ``(ts_ms, id)``。"""
        sql = ["SELECT * FROM mail_inbox WHERE 1=1"]
        args: list[Any] = []
        if status:
            sql.append("AND status=?")
            args.append(status)
        if since_ms is not None:
            sql.append("AND received_ms >= ?")
            args.append(since_ms)
        if until_ms is not None:
            sql.append("AND received_ms <= ?")
            args.append(until_ms)
        if q:
            sql.append("AND (subject LIKE ? OR from_addr LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if before is not None:
            sql.append("AND (received_ms < ? OR (received_ms = ? AND id < ?))")
            args += [int(before[0]), int(before[0]), int(before[1])]
        sql.append("ORDER BY received_ms DESC, id DESC LIMIT ?")
        args.append(limit)
        return _rows(self.con.execute(" ".join(sql), tuple(args)))

    def outbox_list_page(self, *, status: Optional[str] = None, kind: Optional[str] = None,
                         since_ms: Optional[int] = None, until_ms: Optional[int] = None, limit: int = 100,
                         before: Optional[tuple[int, str]] = None) -> list[dict[str, Any]]:
        """#61 的分页视图:``(created_ms, id)`` 降序;``before`` = 上一页末行 ``(ts_ms, id)``。"""
        sql = ["SELECT * FROM mail_outbox WHERE 1=1"]
        args: list[Any] = []
        if status:
            sql.append("AND status=?")
            args.append(status)
        if kind:
            sql.append("AND kind=?")
            args.append(kind)
        if since_ms is not None:
            sql.append("AND created_ms >= ?")
            args.append(since_ms)
        if until_ms is not None:
            sql.append("AND created_ms <= ?")
            args.append(until_ms)
        if before is not None:
            sql.append("AND (created_ms < ? OR (created_ms = ? AND id < ?))")
            args += [int(before[0]), int(before[0]), int(before[1])]
        sql.append("ORDER BY created_ms DESC, id DESC LIMIT ?")
        args.append(limit)
        return _rows(self.con.execute(" ".join(sql), tuple(args)))
