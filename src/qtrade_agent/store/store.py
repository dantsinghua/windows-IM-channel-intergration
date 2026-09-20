"""store 模块(02 §2.2.8)—— 唯一读写 ``agent.db`` 的地方;其它模块经本模块的方法,不互相读对方的表。

硬约束(逐字来自 02 §2.2.8 / §2.8.1 / 06 §2.9.2 / §2.12):
- ``ingest(msg) -> (inserted, changed, id)``;``changed`` = 撤回标记翻转 / 并进我方出向行 二者之一;
  撞上去重键、内容无变化的已存在行 ``(False, False)`` ⇒ 调用方不发事件(重扫不重放)。
- ``ingest_batch(msgs, cursor_update)`` 在**一个** ``BEGIN IMMEDIATE … COMMIT`` 里先 upsert ``sessions``、再写 ``messages``、再推 ``cursors``;
  ``msgs`` 为空而 ``cursor_update`` 非空时只推游标、不得早退。
- 出向(``dir='out'``)与入向共用本入口;``bus`` 不得绕过 store 直插 ``messages``。
- 入向轮询撞到自己发的:同账号 ``dir='out' AND state IN ('SENDING','UNCONFIRMED')`` 且(``fingerprint`` 相同 或 同 ``session_id`` + ``norm(text)`` 相等 + 两侧非空 + ``|ts 差| ≤ out_merge_window_s``)→ 合并进那一行;
  多候选取 ``|读到的行 ts − 出向行 ts|`` 最小、并列取更早写入;一条读到的行只合并一次。
- QQ ``message_id`` 复用:同键且 ``|ts 差| > 1h`` 判新消息,``ext_msg_id`` 追加 ``#n``。
- 并发:单写连接;事务整体互斥(``_tx_lock``);应用层按账号分片排队在 ``AsyncStore``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from ..ids import message_id, ulid
from ..models import Message
from ..text import norm

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_agent.sql")

PRAGMAS = (
    # 02 §2.8.4:库开 auto_vacuum=INCREMENTAL(**建库时设定,之后改不了**)⇒ 必须排在建表之前;
    # 对已存在的库是静默 no-op(SQLite 语义),不改也不报错。放在 journal_mode 之前:WAL 下它对已有库同样无效。
    "PRAGMA auto_vacuum=INCREMENTAL",
    "PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON", "PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-65536", "PRAGMA wal_autocheckpoint=2000",
)

QQ_ID_REUSE_MS = 3600 * 1000


@dataclass(frozen=True)
class IngestResult:
    inserted: bool
    changed: bool
    id: str


@dataclass(frozen=True)
class CursorUpdate:
    owner: str
    kind: str
    value_int: Optional[int]
    value: Optional[str] = None


@dataclass(frozen=True)
class Cursor:
    owner: str
    kind: str
    value: Optional[str]
    value_int: Optional[int]
    updated_ms: int

    def value_json(self) -> dict[str, Any]:
        try:
            return json.loads(self.value) if self.value else {}
        except ValueError:
            return {}


def compute_fingerprint(account_id: str, session_id: str, sender: Optional[str], text: Optional[str],
                        media_sha256s: list[str], ts_ms: int) -> str:
    """06 §2.9.2:``sha256(account_id | session_id | sender_id_or_name | norm(text) | media_sha256s | ts 取整到秒)``。"""
    raw = "|".join([account_id, session_id, sender or "", norm(text), ",".join(sorted(media_sha256s)), str(ts_ms // 1000)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str = ":memory:", *, clock: Callable[[], int] = _now_ms,
                 out_merge_window_s: int = 60, capture_text: bool = True):
        self.path = path
        self._clock = clock
        self.out_merge_window_ms = out_merge_window_s * 1000
        self.capture_text = capture_text
        self._con: Optional[sqlite3.Connection] = None
        self._tx_lock = threading.RLock()

    # ------------------------------------------------------------------ 打开 / 迁移
    def open(self) -> "Store":
        con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        con.row_factory = sqlite3.Row
        for p in PRAGMAS:
            try:
                con.execute(p)
            except sqlite3.OperationalError:
                pass    # :memory: 下 WAL 不适用
        self._con = con
        self._migrate()
        self._seed()
        return self

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._con.close()
            self._con = None

    @property
    def con(self) -> sqlite3.Connection:
        assert self._con is not None, "store not opened"
        return self._con

    def _migrate(self) -> None:
        """基线 DDL 一次性建齐(项目未发行,无历史迁移;02 §2.1 步 4:迁移失败拒绝启动)。"""
        has = self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if has:
            return
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            ddl = f.read()
        with self._tx_lock:
            # executescript 会先隐式 COMMIT 再逐条执行,不能包在 _tx() 里;DDL 失败即抛(拒绝启动)
            self.con.executescript("BEGIN;\n" + ddl + "\nCOMMIT;")
            checksum = hashlib.sha256(ddl.encode("utf-8")).hexdigest()   # 脚本 sha256,防被改过的脚本重跑(02 §3.1)
            self.con.execute("INSERT INTO schema_version(version, name, applied_ms, checksum) VALUES (?, ?, ?, ?)",
                             (1, "0001_baseline_docs02_v0.4.6", self._clock(), checksum))

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._tx_lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield self.con
            except BaseException:
                if self.con.in_transaction:
                    self.con.execute("ROLLBACK")
                raise
            else:
                self.con.execute("COMMIT")

    # ------------------------------------------------------------------ 账号(最小;完整生命周期在 api/runtime)
    def ensure_account(self, id: str, channel: str, *, label: Optional[str] = None, state: str = "created",
                       login_mode: str = "password", quota_mb: Optional[int] = None, self_uid: Optional[str] = None) -> None:
        seq = int(id[2:])
        host = "windows" if channel == "wechat" else "wsl"
        now = self._clock()
        if quota_mb is None:
            quota_mb = {"qidian": 2560, "qq": 614, "wechat": 1536}[channel]     # 02 §7.1 [pool] quota_* 初始值
        with self._tx() as c:
            c.execute(
                "INSERT INTO accounts(id, channel, seq, label, host, state, login_mode, quota_mb, self_uid, created_ms, updated_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, self_uid=COALESCE(excluded.self_uid, accounts.self_uid), updated_ms=excluded.updated_ms",
                (id, channel, seq, label or id, host, state, login_mode, quota_mb, self_uid, now, now))

    def get_account(self, id: str) -> Optional[dict[str, Any]]:
        """accounts 行 + account_runtime.app_version(告警 evidence.app_version 的来源;02 §3.7)。"""
        r = self.con.execute("SELECT a.*, r.app_version AS runtime_app_version FROM accounts a "
                             "LEFT JOIN account_runtime r ON r.account_id = a.id WHERE a.id=? AND a.deleted_ms IS NULL", (id,)).fetchone()
        return dict(r) if r else None

    def set_account_state(self, id: str, state: str, *, state_code: Optional[str] = None,
                          self_uid: Optional[str] = None, state_reason: str = "") -> None:
        self.transition(id, state, state_code=state_code, state_reason=state_reason, self_uid=self_uid)

    # ------------------------------------------------------------------ 账号生命周期(02 §3.4.1 / §2.6;00 §8.1)
    RUNTIME_KIND = {"qidian": "redroid", "qq": "napcat", "wechat": "wechat_pc"}
    ID_PREFIX = {"qidian": "qd", "qq": "qq", "wechat": "wx"}
    STATES = ("created", "provisioning", "starting", "login_required", "logging_in", "running", "degraded", "stopping", "stopped", "error", "disabled")

    def _seed(self) -> None:
        """settings.seq.* 三行(02 §3.1 注:account_id 永不复用的真值);幂等。"""
        now = self._clock()
        with self._tx() as c:
            for ch in ("qidian", "qq", "wechat"):
                c.execute("INSERT OR IGNORE INTO settings(key, value_json, updated_ms, updated_by) VALUES (?, '0', ?, 'system:store')", (f"seq.{ch}", now))

    def settings_get(self, key: str) -> Any:
        r = self.con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def settings_set(self, key: str, value: Any, *, actor: str = "system", now_ms: Optional[int] = None) -> None:
        now = now_ms or self._clock()
        with self._tx() as c:
            c.execute("INSERT INTO settings(key, value_json, updated_ms, updated_by) VALUES (?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_ms=excluded.updated_ms, updated_by=excluded.updated_by",
                      (key, json.dumps(value, ensure_ascii=False), now, actor))

    def _next_seq(self, c: sqlite3.Connection, channel: str, now: int) -> int:
        """settings.seq.<channel> 单调递增(含已删除的不复用;05 §2.1.1 ②);在调用方的 BEGIN IMMEDIATE 里。"""
        r = c.execute("SELECT value_json FROM settings WHERE key=?", (f"seq.{channel}",)).fetchone()
        cur = int(json.loads(r[0])) if r else 0
        nxt = cur + 1
        if nxt > 98:                                    # 99 保留给安装自检(C-07),seq ∈ 1..98
            raise ValueError(f"{channel} 序号已用尽(seq>98)")
        c.execute("INSERT INTO settings(key, value_json, updated_ms, updated_by) VALUES (?,?,?,'system:store') "
                  "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_ms=excluded.updated_ms, updated_by=excluded.updated_by",
                  (f"seq.{channel}", json.dumps(nxt), now))
        return nxt

    def create_account(self, *, channel: str, label: str, login_mode: str, quota_mb: int, remember: bool = False,
                       capture_text: Optional[bool] = None, identity: Optional[dict[str, Any]] = None, settings: Optional[dict[str, Any]] = None,
                       ports: Optional[dict[str, Any]] = None, container_name: Optional[str] = None, data_dir: Optional[str] = None,
                       mem_limit_mb: Optional[int] = None, now_ms: Optional[int] = None) -> dict[str, Any]:
        """#2:一个事务里分配 id(settings.seq.<channel> +1,永不复用)、落 accounts(state=created)与 account_runtime(desired_state=stopped,端口冗余)。"""
        now = now_ms or self._clock()
        host = "windows" if channel == "wechat" else "wsl"
        ports = ports or {}
        with self._tx() as c:
            seq = self._next_seq(c, channel, now)
            id = f"{self.ID_PREFIX[channel]}{seq:02d}"
            c.execute(
                "INSERT INTO accounts(id, channel, seq, label, host, state, login_mode, remember, quota_mb, capture_text, identity_json, settings_json, created_ms, updated_ms) "
                "VALUES (?,?,?,?,?,'created',?,?,?,?,?,?,?,?)",
                (id, channel, seq, label, host, login_mode, 1 if remember else 0, quota_mb, None if capture_text is None else int(capture_text),
                 json.dumps(identity or {}, ensure_ascii=False), json.dumps(settings or {}, ensure_ascii=False), now, now))
            c.execute(
                "INSERT INTO account_runtime(account_id, kind, desired_state, container_name, data_dir, adb_port, stream_port, frida_port, adb_serial, "
                "ws_port, http_port, webui_port, mem_limit_mb, updated_ms) VALUES (?,?,'stopped',?,?,?,?,?,?,?,?,?,?,?)",
                (id, self.RUNTIME_KIND[channel], container_name, data_dir, ports.get("adb"), ports.get("stream"), ports.get("frida"), ports.get("adb_serial"),
                 ports.get("ws"), ports.get("http"), ports.get("webui"), mem_limit_mb, now))
        return self.get_account_full(id)

    def transition(self, id: str, state: str, *, state_code: Optional[str] = None, state_reason: str = "", self_uid: Optional[str] = None,
                   enabled: Optional[bool] = None, desired_state: Optional[str] = None, deleted_ms: Optional[int] = None,
                   now_ms: Optional[int] = None) -> tuple[Optional[str], dict[str, Any]]:
        """账号状态迁移的唯一写点:改 accounts.state 与 account_runtime.error_since_ms 两个动作(02 §2.6 规范 SQL)在同一个 BEGIN IMMEDIATE 里。
        返回 (before_state, after_row)。"""
        if state not in self.STATES:
            raise ValueError(f"unknown state {state!r}")
        now = now_ms or self._clock()
        with self._tx() as c:
            r = c.execute("SELECT state, channel FROM accounts WHERE id=?", (id,)).fetchone()
            if r is None:
                raise KeyError(id)
            before = r["state"]
            c.execute("UPDATE accounts SET state=?, state_code=?, state_reason=?, self_uid=COALESCE(?, self_uid), updated_ms=? WHERE id=?",
                      (state, state_code, state_reason, self_uid, now, id))
            if enabled is not None:
                c.execute("UPDATE accounts SET enabled=? WHERE id=?", (1 if enabled else 0, id))
            if deleted_ms is not None:
                c.execute("UPDATE accounts SET deleted_ms=? WHERE id=?", (deleted_ms, id))
            if state == "running":
                c.execute("UPDATE accounts SET last_running_ms=?, last_seen_ms=? WHERE id=?", (now, now, id))
            c.execute("INSERT OR IGNORE INTO account_runtime(account_id, kind, updated_ms) VALUES (?,?,?)", (id, self.RUNTIME_KIND[r["channel"]], now))
            if state == "error":
                # ① 进 error(迁入 error 时写当前 epoch ms);已在 error 的重复迁入不覆盖原时刻
                c.execute("UPDATE account_runtime SET error_since_ms = ?, updated_ms = ? WHERE account_id = ? AND error_since_ms IS NULL", (now, now, id))
            else:
                # ② 离 error(迁出到任一非 error 态时清 NULL)
                c.execute("UPDATE account_runtime SET error_since_ms = NULL, updated_ms = ? WHERE account_id = ? AND error_since_ms IS NOT NULL", (now, id))
            if desired_state is not None:
                c.execute("UPDATE account_runtime SET desired_state=?, updated_ms=? WHERE account_id=?", (desired_state, now, id))
            if state == "stopped":
                c.execute("UPDATE account_runtime SET last_stopped_ms=? WHERE account_id=?", (now, id))
            elif state == "starting":
                c.execute("UPDATE account_runtime SET last_started_ms=? WHERE account_id=?", (now, id))
        return before, self.get_account_full(id)

    ACCOUNT_PATCHABLE = ("label", "quota_mb", "capture_text", "retention_days", "media_policy_json", "remember", "credential_ref", "auto_recover", "settings_json")

    def patch_account(self, id: str, *, now_ms: Optional[int] = None, **cols: Any) -> dict[str, Any]:
        bad = [k for k in cols if k not in self.ACCOUNT_PATCHABLE]
        if bad:
            raise ValueError(f"not patchable: {bad}")
        now = now_ms or self._clock()
        with self._tx() as c:
            for k, v in cols.items():
                if isinstance(v, bool):
                    v = int(v)
                c.execute(f"UPDATE accounts SET {k}=?, updated_ms=? WHERE id=?", (v, now, id))
        return self.get_account_full(id)

    def set_desired_state(self, id: str, desired: str, *, now_ms: Optional[int] = None) -> None:
        with self._tx() as c:
            c.execute("UPDATE account_runtime SET desired_state=?, updated_ms=? WHERE account_id=?", (desired, now_ms or self._clock(), id))

    def get_runtime(self, id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM account_runtime WHERE account_id=?", (id,)).fetchone()
        return dict(r) if r else None

    def list_recover_candidates(self) -> list[dict[str, Any]]:
        """02 §2.6:enabled=1 AND auto_recover=1 AND deleted_ms IS NULL AND account_runtime.desired_state='running',按 seq 升序。"""
        rows = self.con.execute("SELECT a.* FROM accounts a JOIN account_runtime r ON r.account_id=a.id "
                                "WHERE a.enabled=1 AND a.auto_recover=1 AND a.deleted_ms IS NULL AND r.desired_state='running' ORDER BY a.channel, a.seq").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 资源池(02 §2.2.5;00 §7.6)
    POOL_USED_SQL = "SELECT COALESCE(SUM(quota_mb), 0) FROM accounts WHERE host='wsl' AND enabled=1 AND state NOT IN ('stopped','disabled','error')"

    def ensure_pools(self, *, quota: dict[str, int], wsl_total_mb: int, wsl_reserved_mb: int, windows_total_mb: int, windows_reserved_mb: int,
                     now_ms: Optional[int] = None) -> None:
        """resource_pools 两行初始值(agent.toml [pool] 只是首次建表默认,C-43);已存在则不动。"""
        now = now_ms or self._clock()
        q = json.dumps({"qidian": int(quota["qidian"]), "qq": int(quota["qq"]), "wechat": int(quota["wechat"])})
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO resource_pools(pool, total_mb, reserved_mb, quota_json, updated_ms) VALUES ('wsl', ?, ?, ?, ?)",
                      (wsl_total_mb, wsl_reserved_mb, q, now))
            c.execute("INSERT OR IGNORE INTO resource_pools(pool, total_mb, reserved_mb, quota_json, updated_ms) VALUES ('windows', ?, ?, ?, ?)",
                      (windows_total_mb, windows_reserved_mb, q, now))

    def pool_get(self, pool: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM resource_pools WHERE pool=?", (pool,)).fetchone()
        return dict(r) | {"quota": json.loads(r["quota_json"])} if r else None

    def pool_set(self, pool: str, *, now_ms: Optional[int] = None, **cols: Any) -> None:
        now = now_ms or self._clock()
        with self._tx() as c:
            for k, v in cols.items():
                if isinstance(v, bool):
                    v = int(v)
                c.execute(f"UPDATE resource_pools SET {k}=?, updated_ms=? WHERE pool=?", (v, now, pool))

    def pool_used_mb(self, *, exclude_id: Optional[str] = None) -> int:
        """R6-55:``exclude_id`` = start/restart 时排除自身(created 已计入 used,不排除则预算恰好只够一个时自己起不来)。"""
        if exclude_id is None:
            return int(self.con.execute(self.POOL_USED_SQL).fetchone()[0])
        return int(self.con.execute(self.POOL_USED_SQL + " AND id<>?", (exclude_id,)).fetchone()[0])

    def pool_claim_wsl(self, quota_mb: int, *, now_ms: Optional[int] = None) -> bool:
        """行级 claim(R-08 §2.3.1 ③):条件 UPDATE + rowcount 判定,防两个并发新增同时通过预检;不用内存锁。"""
        with self._tx() as c:
            cur = c.execute(f"UPDATE resource_pools SET updated_ms=? WHERE pool='wsl' AND total_mb - reserved_mb - ({self.POOL_USED_SQL}) >= ?",
                            (now_ms or self._clock(), quota_mb))
            return cur.rowcount == 1

    def wechat_slot_claim(self, target: str, login_session_id: str, expires_ms: int, *, now_ms: Optional[int] = None) -> bool:
        """02 §2.2.5:置 pending 的规范 UPDATE(三列同一条语句),rowcount==1 判抢到。"""
        with self._tx() as c:
            cur = c.execute("UPDATE resource_pools SET slot_pending=?, slot_pending_expires_ms=?, slot_pending_login_session_id=?, updated_ms=? "
                            "WHERE pool='windows' AND slot_holder='' AND slot_pending=''", (target, expires_ms, login_session_id, now_ms or self._clock()))
            return cur.rowcount == 1

    def wechat_slot_release_pending(self, target: Optional[str] = None, *, now_ms: Optional[int] = None) -> int:
        with self._tx() as c:
            if target is None:
                cur = c.execute("UPDATE resource_pools SET slot_pending='', slot_pending_expires_ms=NULL, slot_pending_login_session_id='', updated_ms=? "
                                "WHERE pool='windows' AND slot_pending<>''", (now_ms or self._clock(),))
            else:
                cur = c.execute("UPDATE resource_pools SET slot_pending='', slot_pending_expires_ms=NULL, slot_pending_login_session_id='', updated_ms=? "
                                "WHERE slot_pending=?", (now_ms or self._clock(), target))
            return cur.rowcount

    def wechat_slot_release_holder(self, holder: str, *, now_ms: Optional[int] = None) -> int:
        with self._tx() as c:
            return c.execute("UPDATE resource_pools SET slot_holder='', updated_ms=? WHERE pool='windows' AND slot_holder=?",
                             (now_ms or self._clock(), holder)).rowcount

    def wechat_slot_promote(self, target: str, *, now_ms: Optional[int] = None) -> bool:
        """02 §2.2.5「成功才把 ``pending`` 转 ``holder``」—— 规格原句是**一条** UPDATE,rowcount==1 判成功。

        单条语句同时置 ``slot_holder`` 并清三列,天然绕开 ``resource_pools`` 第二条 CHECK(「没有 pending 就不该有
        过期时刻/残留尝试 id」)对多条 UPDATE 的写入顺序要求。"""
        with self._tx() as c:
            cur = c.execute("UPDATE resource_pools SET slot_holder=slot_pending, slot_pending='', "
                            "slot_pending_expires_ms=NULL, slot_pending_login_session_id='', updated_ms=? "
                            "WHERE pool='windows' AND slot_holder='' AND slot_pending=?", (now_ms or self._clock(), target))
            return cur.rowcount == 1

    def wechat_slot_renew_pending(self, target: str, expires_ms: int, *, now_ms: Optional[int] = None) -> bool:
        """T-11(`.omc/handoffs/wechat-channel.md`):登录流在世期间续期 ``slot_pending_expires_ms``,
        免得 ``[wechat] slot_pending_ttl_s``(600)先于 ``[accounts] qr_max_wait_s``(1800)到点被 reaper 误杀。"""
        with self._tx() as c:
            cur = c.execute("UPDATE resource_pools SET slot_pending_expires_ms=?, updated_ms=? "
                            "WHERE pool='windows' AND slot_pending=?", (int(expires_ms), now_ms or self._clock(), target))
            return cur.rowcount == 1

    def wechat_merge_account(self, temp_id: str, old_id: str, wxid: str, *,
                             create_template: Optional[dict[str, Any]] = None, now_ms: Optional[int] = None) -> None:
        """05 §2.4.2.1 第 3 步 b) / 回滚表末行的合并事务。

        ⚠️ 语句顺序被两条库约束夹住:① ``ux_accounts_wxid`` 是「未软删且未合并」的部分唯一索引 ⇒ **必须先软删临时行**
        才能把同一 ``wxid`` 落到老行;② ``merged_into REFERENCES accounts(id)`` ⇒ **老行必须先存在**。
        故顺序 = 软删临时行 → 重建老行(如缺)→ 写墓碑指针 → 回填老行 → 槽位改指。"""
        now = now_ms or self._clock()
        with self._tx() as c:
            c.execute("UPDATE accounts SET deleted_ms=?, state='stopped', updated_ms=? WHERE id=?", (now, now, temp_id))
            if create_template is not None:
                c.execute("INSERT INTO accounts(id, channel, seq, label, host, state, login_mode, remember, quota_mb, wxid, self_uid, "
                          "identity_json, settings_json, created_ms, updated_ms) "
                          "VALUES (?,'wechat',?,?,'windows','created','qrcode',0,?,?,?,'{}','{}',?,?)",
                          (old_id, int(old_id[2:]), create_template["label"], create_template["quota_mb"], wxid, wxid, now, now))
                c.execute("INSERT OR IGNORE INTO account_runtime(account_id, kind, desired_state, updated_ms) VALUES (?,?,'stopped',?)",
                          (old_id, self.RUNTIME_KIND["wechat"], now))
            c.execute("UPDATE accounts SET merged_into=?, updated_ms=? WHERE id=?", (old_id, now, temp_id))
            c.execute("UPDATE accounts SET wxid=COALESCE(wxid, ?), self_uid=COALESCE(self_uid, ?), updated_ms=? WHERE id=?",
                      (wxid, wxid, now, old_id))
            c.execute("UPDATE resource_pools SET slot_pending=?, updated_ms=? WHERE pool='windows' AND slot_pending=?", (old_id, now, temp_id))

    def bind_out_by_trace_id(self, trace_id: str, ext_msg_id: str, confirmed_by: str, *, now_ms: Optional[int] = None) -> Optional[str]:
        """06 §2.12 QQ 行「``get_msg`` 存在 ⇒ **直接绑定该行** ``ext_msg_id``」的唯一入口。

        QQ 是三通道里唯一手里有**确定** ``message_id`` 的,走 ``store.ingest`` 的模糊合并(fingerprint / 同会话
        同 ``norm(text)`` + 时间窗)会把确定性降级:``capture_text=false`` 且出向行 ``ts`` 与 ``get_msg.time`` 跨秒时
        两支判据都不成立 ⇒ 该行恒 ``UNCONFIRMED``。返回被绑定的 ``messages.id``;没有该 trace 的 ``SENDING`` 出向行回 ``None``。"""
        now = now_ms or self._clock()
        with self._tx() as c:
            r = c.execute("SELECT id FROM messages WHERE trace_id=? AND dir='out' AND state='SENDING' ORDER BY ts_ms DESC LIMIT 1",
                          (trace_id,)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE messages SET ext_msg_id=?, state='DELIVERED', confirmed_by=?, confirmed_ms=? WHERE id=?",
                      (ext_msg_id, confirmed_by, now, r["id"]))
            return str(r["id"])

    # ------------------------------------------------------------------ jobs(02 §3.1;§3.4.9 R-21:凡 202 {job_id} 的端点统一走这张表)
    def job_create(self, *, kind: str, actor: str, params: Optional[dict[str, Any]] = None,
                   account_id: Optional[str] = None, now_ms: Optional[int] = None) -> str:
        now = now_ms or self._clock()
        job_id = ulid(now)
        with self._tx() as c:
            c.execute("INSERT INTO jobs(job_id, kind, account_id, actor, state, params_json, created_ms, updated_ms) "
                      "VALUES (?,?,?,?,'queued',?,?,?)",
                      (job_id, kind, account_id, actor, json.dumps(params or {}, ensure_ascii=False), now, now))
        return job_id

    def job_start(self, job_id: str, *, now_ms: Optional[int] = None) -> None:
        now = now_ms or self._clock()
        with self._tx() as c:
            c.execute("UPDATE jobs SET state='running', attempt_count=attempt_count+1, updated_ms=? WHERE job_id=? AND state='queued'",
                      (now, job_id))

    def job_finish(self, job_id: str, *, ok: bool, result: Optional[dict[str, Any]] = None,
                   error: Optional[dict[str, Any]] = None, now_ms: Optional[int] = None) -> None:
        now = now_ms or self._clock()
        with self._tx() as c:
            c.execute("UPDATE jobs SET state=?, progress=?, result_json=?, error_json=?, updated_ms=? WHERE job_id=?",
                      ("succeeded" if ok else "failed", 100 if ok else 0,
                       json.dumps(result, ensure_ascii=False) if result is not None else None,
                       json.dumps(error, ensure_ascii=False) if error is not None else None, now, job_id))

    def job_get(self, job_id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def job_inflight(self, kind: str) -> Optional[dict[str, Any]]:
        """#109 的 60 s 防重判据之一:同 kind 还有 queued/running 的行。"""
        r = self.con.execute("SELECT * FROM jobs WHERE kind=? AND state IN ('queued','running') ORDER BY created_ms DESC LIMIT 1",
                             (kind,)).fetchone()
        return dict(r) if r else None

    def job_last(self, kind: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM jobs WHERE kind=? ORDER BY created_ms DESC LIMIT 1", (kind,)).fetchone()
        return dict(r) if r else None

    def job_cancel(self, job_id: str, *, actor: str = "token:console", now_ms: Optional[int] = None) -> bool:
        """#108:只取消 ``queued/running``;rowcount==1 判成功(终态由调用方先判并回 409)。"""
        now = now_ms or self._clock()
        with self._tx() as c:
            cur = c.execute("UPDATE jobs SET state='cancelled', updated_ms=? WHERE job_id=? AND state IN ('queued','running')",
                            (now, job_id))
            ok = cur.rowcount == 1
        if ok:
            self.insert_audit(kind="api", transport="http", actor=actor, action="job.cancel", result_code="OK",
                              detail={"job_id": job_id}, now_ms=now)
        return ok

    def pool_calibration_note(self, *, run_id: str, account_id: str, channel: str, quota_mb: Optional[int],
                              evidence: dict[str, Any], actor: str = "token:console", now_ms: Optional[int] = None) -> None:
        """#25 单账号自校准的落点:把这一轮的建议与证据并进 ``resource_pools.calibration_json`` 的 ``per_account`` 子对象。

        **不改 `quota_json`** —— 单账号量出来的只是建议(下调还要过 `quota_auto_lower`),写回整池配额是 #71 的事。
        """
        now = now_ms or self._clock()
        with self._tx() as c:
            r = c.execute("SELECT calibration_json FROM resource_pools WHERE pool='wsl'").fetchone()
            blob = json.loads(r[0]) if r and r[0] else {}
            per = dict(blob.get("per_account") or {})
            per[account_id] = {"run_id": run_id, "channel": channel, "quota_mb": quota_mb,
                               "evidence": evidence, "calibrated_ms": now}
            blob["per_account"] = per
            c.execute("UPDATE resource_pools SET calibration_json=?, updated_ms=? WHERE pool='wsl'",
                      (json.dumps(blob, ensure_ascii=False), now))
        self.insert_audit(kind="api", transport="http", actor=actor, action="resources.calibrate", account_id=account_id,
                          result_code="OK", detail={"run_id": run_id, "scope": "account", "quota_mb": quota_mb}, now_ms=now)

    # ------------------------------------------------------------------ ingest
    #: 02 §2.8.8「写入报错先判磁盘满」:装配方(``app.py``)把 ``maintenance.guard_write`` 挂上来。
    #: 缺省是空壳上下文 ⇒ 未装配 maintenance 时行为与从前逐字相同。
    write_guard: Callable[[str], Any] = staticmethod(lambda what: nullcontext())

    def ingest(self, msg: Message, *, now_ms: Optional[int] = None) -> IngestResult:
        with self.write_guard("store.ingest"), self._tx() as c:
            return self._ingest_one(c, msg, now_ms or self._clock())

    def ingest_batch(self, msgs: list[Message], cursor_update: Optional[CursorUpdate] = None,
                     *, now_ms: Optional[int] = None) -> list[tuple[bool, bool, Message]]:
        """一个事务:sessions → messages → cursors;空批只推游标,不早退(R6-41)。"""
        now = now_ms or self._clock()
        out: list[tuple[bool, bool, Message]] = []
        with self.write_guard("store.ingest_batch"), self._tx() as c:
            for m in msgs:
                r = self._ingest_one(c, m, now)
                m.id = r.id
                out.append((r.inserted, r.changed, m))
            if cursor_update is not None:
                self._cursor_set(c, cursor_update.owner, cursor_update.kind, cursor_update.value_int, cursor_update.value, now)
        return out

    # -- 内部:同一事务内的各步
    def _upsert_session(self, c: sqlite3.Connection, msg: Message, now: int) -> None:
        s = msg.session
        name = s.name or s.native_id
        c.execute(
            "INSERT INTO sessions(id, account_id, channel, native_id, name, kind, last_msg_ms, msg_count, first_seen_ms, created_ms, updated_ms) "
            "VALUES (?,?,?,?,?,?,?,0,?,?,?) "
            "ON CONFLICT(account_id, native_id) DO UPDATE SET "
            "  name = CASE WHEN excluded.name <> excluded.native_id THEN excluded.name ELSE sessions.name END, "
            "  last_msg_ms = MAX(COALESCE(sessions.last_msg_ms, 0), excluded.last_msg_ms), updated_ms = excluded.updated_ms",
            (s.id, msg.account_id, msg.channel, s.native_id, name, s.kind, msg.ts_ms, msg.ts_ms, now, now))

    def _row_values(self, msg: Message, now: int) -> dict[str, Any]:
        text = msg.text
        media_shas = [m["sha256"] for m in msg.media if m.get("sha256")]
        fp = compute_fingerprint(msg.account_id, msg.session_id, msg.sender_id or msg.sender_name, text, media_shas, msg.ts_ms)
        msg.fingerprint = fp
        msg.received_ms = now
        stored_text = text if self.capture_text else None
        return {
            "id": msg.id or message_id(now), "ext_msg_id": msg.ext_msg_id, "dedup_kind": msg.dedup_kind, "fingerprint": fp,
            "account_id": msg.account_id, "channel": msg.channel, "session_id": msg.session_id,
            "dir": msg.dir, "type": msg.type, "state": msg.state if msg.dir == "out" else "DELIVERED",
            "text": stored_text, "text_len": len(text) if text is not None else None,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None,
            "media_json": json.dumps(msg.media, ensure_ascii=False), "sender_id": msg.sender_id, "sender_name": msg.sender_name,
            "is_self": 1 if msg.self else 0, "ts_ms": msg.ts_ms, "received_ms": now, "source": msg.source,
            "revoked": 1 if msg.revoked else 0, "revoked_ms": msg.revoked_ms, "revoked_by": msg.revoked_by,
            "trace_id": msg.trace_id, "idempotency_key": msg.idempotency_key, "raw_ref": msg.raw_ref,
        }

    def _insert_message(self, c: sqlite3.Connection, msg: Message, now: int, *, ext_override: Optional[str] = None) -> str:
        v = self._row_values(msg, now)
        if ext_override is not None:
            v["ext_msg_id"] = ext_override
        cols = ",".join(v.keys())
        qs = ",".join("?" for _ in v)
        c.execute(f"INSERT INTO messages({cols}) VALUES ({qs})", tuple(v.values()))
        c.execute("UPDATE sessions SET msg_count = msg_count + 1 WHERE id = ?", (msg.session_id,))
        msg.id = v["id"]
        return v["id"]

    def _find_merge_candidate(self, c: sqlite3.Connection, msg: Message) -> Optional[sqlite3.Row]:
        """06 §2.12「入向轮询撞到自己发的」:只对 self=True 的出向读回行;返回定序后的那一行或 None。"""
        rows = c.execute(
            "SELECT id, rowid AS rid, session_id, ts_ms, text, fingerprint FROM messages "
            "WHERE account_id=? AND dir='out' AND state IN ('SENDING','UNCONFIRMED') AND ext_msg_id IS NULL",
            (msg.account_id,)).fetchall()
        media_shas = [m["sha256"] for m in msg.media if m.get("sha256")]
        my_fp = compute_fingerprint(msg.account_id, msg.session_id, msg.sender_id or msg.sender_name, msg.text, media_shas, msg.ts_ms)
        my_norm = norm(msg.text)
        cands = []
        for r in rows:
            if r["fingerprint"] == my_fp:
                cands.append(r)
                continue
            if r["session_id"] != msg.session_id:
                continue
            # 空文本守卫(R6-44):两侧任一为空时不走这一支,只认 fingerprint
            if not my_norm or not norm(r["text"]):
                continue
            if norm(r["text"]) == my_norm and abs(msg.ts_ms - r["ts_ms"]) <= self.out_merge_window_ms:
                cands.append(r)
        if not cands:
            return None
        cands.sort(key=lambda r: (abs(msg.ts_ms - r["ts_ms"]), r["rid"]))   # |time − ts| 最小,并列取更早写入
        return cands[0]

    @staticmethod
    def _confirmed_by_for(source: str) -> str:
        return {"qidian_db": "ingest_merge", "chatlog": "chatlog", "onebot": "get_msg"}.get(source, "ingest_merge")

    def _ingest_one(self, c: sqlite3.Connection, msg: Message, now: int) -> IngestResult:
        self._upsert_session(c, msg, now)

        # ① bus 出向先落库(C-21/R6-16):幂等重试命中同 idempotency_key 直接复用这一行
        if msg.dir == "out" and msg.state == "SENDING" and msg.ext_msg_id is None:
            if msg.idempotency_key:
                r = c.execute("SELECT id FROM messages WHERE account_id=? AND idempotency_key=? AND dir='out'",
                              (msg.account_id, msg.idempotency_key)).fetchone()
                if r:
                    msg.id = r["id"]
                    return IngestResult(False, False, r["id"])
            return IngestResult(True, False, self._insert_message(c, msg, now))

        assert msg.ext_msg_id is not None, "入向/读回行必须带 ext_msg_id(anchor 路用 fingerprint 作 ext)"

        # ② 去重键:先查再插(99c C-03)
        if msg.channel == "qq":
            # QQ message_id 复用(06 §2.9.2 / R6-51):与同键族(原行 + 已有 #n 行)里 **ts 最大的那一行** 比;|ts 差| > 1h 判新、后缀 = 族行数 + 1
            existing = c.execute("SELECT id, ts_ms, revoked, text FROM messages WHERE account_id=? AND (ext_msg_id=? OR ext_msg_id LIKE ?) "
                                 "ORDER BY ts_ms DESC, rowid DESC LIMIT 1", (msg.account_id, msg.ext_msg_id, msg.ext_msg_id + "#%")).fetchone()
        else:
            existing = c.execute("SELECT id, ts_ms, revoked, text FROM messages WHERE account_id=? AND ext_msg_id=?",
                                 (msg.account_id, msg.ext_msg_id)).fetchone()
        if existing is not None:
            if msg.channel == "qq" and abs(msg.ts_ms - existing["ts_ms"]) > QQ_ID_REUSE_MS:
                n = c.execute("SELECT COUNT(*) FROM messages WHERE account_id=? AND (ext_msg_id=? OR ext_msg_id LIKE ?)",
                              (msg.account_id, msg.ext_msg_id, msg.ext_msg_id + "#%")).fetchone()[0]
                return IngestResult(True, False, self._insert_message(c, msg, now, ext_override=f"{msg.ext_msg_id}#{n + 1}"))
            changed = False
            if msg.revoked and not existing["revoked"]:
                c.execute("UPDATE messages SET revoked=1, revoked_ms=COALESCE(?, revoked_ms, ?), revoked_by=COALESCE(?, revoked_by) WHERE id=?",
                          (msg.revoked_ms, now, msg.revoked_by, existing["id"]))
                changed = True
            if existing["text"] is None and msg.text is not None and self.capture_text:
                c.execute("UPDATE messages SET text=?, text_len=? WHERE id=?", (msg.text, len(msg.text), existing["id"]))
            msg.id = existing["id"]
            return IngestResult(False, changed, existing["id"])

        # ③ 入向轮询撞到自己发的(06 §2.12):只对我方出向的读回行
        if msg.self and msg.dir == "out":
            cand = self._find_merge_candidate(c, msg)
            if cand is not None:
                c.execute("UPDATE messages SET ext_msg_id=?, state='DELIVERED', confirmed_by=?, confirmed_ms=? WHERE id=?",
                          (msg.ext_msg_id, self._confirmed_by_for(msg.source), now, cand["id"]))
                msg.id = cand["id"]
                msg.received_ms = now
                return IngestResult(False, True, cand["id"])

        # ④ 真新行(含「非本系统发出的我方消息」:dir=out、trace_id NULL、state=DELIVERED)
        return IngestResult(True, False, self._insert_message(c, msg, now))

    # ------------------------------------------------------------------ 出向行状态
    def message_state(self, id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT id, state, ext_msg_id, confirmed_by, confirmed_ms, trace_id FROM messages WHERE id=?", (id,)).fetchone()
        return dict(r) if r else None

    def mark_out_state(self, id: str, state: str) -> None:
        assert state in ("UNCONFIRMED", "FAILED")
        with self._tx() as c:
            c.execute("UPDATE messages SET state=? WHERE id=? AND dir='out' AND state='SENDING'", (state, id))

    def find_out_by_text(self, account_id: str, session_id: str, text: str, window_ms: int, ts_ms: int) -> Optional[dict[str, Any]]:
        """confirm_probe 用:同会话、同 norm(text)、窗内的已确认出向行。"""
        rows = self.con.execute(
            "SELECT id, text, ts_ms, ext_msg_id, state FROM messages WHERE account_id=? AND session_id=? AND dir='out' AND ext_msg_id IS NOT NULL AND ABS(ts_ms-?) <= ?",
            (account_id, session_id, ts_ms, window_ms)).fetchall()
        n = norm(text)
        for r in rows:
            if n and norm(r["text"]) == n:
                return dict(r)
        return None

    def get_message(self, id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM messages WHERE id=?", (id,)).fetchone()
        return dict(r) if r else None

    def list_messages(self, account_id: str, *, session_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE account_id=?"
        params: list[Any] = [account_id]
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY ts_ms DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def count_messages(self, account_id: str) -> int:
        return self.con.execute("SELECT COUNT(*) FROM messages WHERE account_id=?", (account_id,)).fetchone()[0]

    # ------------------------------------------------------------------ 游标
    def cursor_get(self, owner: str, kind: str) -> Optional[Cursor]:
        r = self.con.execute("SELECT owner, kind, value, value_int, updated_ms FROM cursors WHERE owner=? AND kind=?", (owner, kind)).fetchone()
        return Cursor(*r) if r else None

    def cursors_list(self, owner: str, kind_prefix: str) -> list[Cursor]:
        rows = self.con.execute("SELECT owner, kind, value, value_int, updated_ms FROM cursors WHERE owner=? AND kind LIKE ? ORDER BY kind",
                                (owner, kind_prefix + "%")).fetchall()
        return [Cursor(*r) for r in rows]

    def _cursor_set(self, c: sqlite3.Connection, owner: str, kind: str, value_int: Optional[int], value: Optional[str], now: int) -> None:
        c.execute("INSERT INTO cursors(owner, kind, value, value_int, updated_ms) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(owner, kind) DO UPDATE SET value=excluded.value, value_int=excluded.value_int, updated_ms=excluded.updated_ms",
                  (owner, kind, value, value_int, now))

    def cursor_set(self, owner: str, kind: str, value_int: Optional[int], value: Optional[str] = None) -> None:
        with self._tx() as c:
            self._cursor_set(c, owner, kind, value_int, value, self._clock())

    def qidian_bootstrap_rebase(self, account_id: str, new_uin: str, *, old_uin: Optional[str], now_ms: int) -> int:
        """06 §2.9.5 ③:删旧水位 + 写新基准 **同一事务**;换号(old_uin 非空)时先记审计 ``qidian.rebootstrap``(R6-50)。返回删掉的水位行数。"""
        with self._tx() as c:
            deleted = 0
            if old_uin is not None:
                deleted = c.execute("SELECT COUNT(*) FROM cursors WHERE owner=? AND kind LIKE 'qidian_rowid:%'", (account_id,)).fetchone()[0]
                self._insert_audit(c, kind="system", transport="system", actor="system:qidian_adapter", action="qidian.rebootstrap",
                                   account_id=account_id, trace_id=None, result_code="OK",
                                   detail={"old_uin": old_uin, "new_uin": new_uin, "deleted_cursors": deleted}, now=now_ms)
                c.execute("DELETE FROM cursors WHERE owner=? AND kind LIKE 'qidian_rowid:%'", (account_id,))
            self._cursor_set(c, account_id, "qidian_bootstrap", now_ms, new_uin, now_ms)
            return deleted

    # ------------------------------------------------------------------ 审计 / outbox
    def _insert_audit(self, c: sqlite3.Connection, *, kind: str, transport: str, actor: str, action: str, account_id: Optional[str],
                      trace_id: Optional[str], result_code: Optional[str], detail: dict[str, Any], now: int) -> int:
        cur = c.execute(
            "INSERT INTO audit_log(ts_ms, kind, transport, actor, action, account_id, trace_id, result_code, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (now, kind, transport, actor, action, account_id, trace_id, result_code, json.dumps(detail, ensure_ascii=False)))
        return int(cur.lastrowid)

    def insert_audit(self, *, kind: str, transport: str, actor: str, action: str, account_id: Optional[str] = None,
                     trace_id: Optional[str] = None, result_code: Optional[str] = None, detail: Optional[dict[str, Any]] = None,
                     now_ms: Optional[int] = None) -> int:
        with self._tx() as c:
            return self._insert_audit(c, kind=kind, transport=transport, actor=actor, action=action, account_id=account_id,
                                      trace_id=trace_id, result_code=result_code, detail=detail or {}, now=now_ms or self._clock())

    def list_audit(self, action: Optional[str] = None) -> list[dict[str, Any]]:
        if action:
            rows = self.con.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,)).fetchall()
        else:
            rows = self.con.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def insert_outbox_event(self, *, event_id: str, target: str, event: str, trace_id: Optional[str], account_id: Optional[str],
                            channel: Optional[str], payload_json: str, now_ms: Optional[int] = None) -> int:
        """target='ws' 的行是规范事件记录(供 WS 按 seq 重放),写入即 delivered;webhook 行留 pending 给投递器(02 §2.2.7)。"""
        now = now_ms or self._clock()
        with self._tx() as c:
            if target == "ws":
                cur = c.execute(
                    "INSERT INTO events_outbox(event_id, target, event, ts_ms, trace_id, account_id, channel, payload_json, status, delivered_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,'delivered',?)",
                    (event_id, target, event, now, trace_id, account_id, channel, payload_json, now))
            else:
                cur = c.execute(
                    "INSERT INTO events_outbox(event_id, target, event, ts_ms, trace_id, account_id, channel, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, target, event, now, trace_id, account_id, channel, payload_json))
            return int(cur.lastrowid)

    def outbox_seq_bounds(self) -> tuple[Optional[int], Optional[int]]:
        r = self.con.execute("SELECT MIN(seq), MAX(seq) FROM events_outbox WHERE target='ws'").fetchone()
        return (r[0], r[1]) if r else (None, None)

    def purge_outbox_ws(self, older_than_ms: int) -> int:
        with self._tx() as c:
            return c.execute("DELETE FROM events_outbox WHERE target='ws' AND ts_ms < ?", (older_than_ms,)).rowcount

    def replay_outbox(self, since_seq: int, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.con.execute("SELECT seq, event, ts_ms, trace_id, account_id, channel, payload_json FROM events_outbox "
                                "WHERE target='ws' AND seq > ? ORDER BY seq LIMIT ?", (since_seq, limit)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in rows]

    def list_events(self, event: Optional[str] = None, account_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM events_outbox WHERE target='ws'", []
        if event:
            sql += " AND event=?"; params.append(event)
        if account_id:
            sql += " AND account_id=?"; params.append(account_id)
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in self.con.execute(sql + " ORDER BY seq", params).fetchall()]

    # ------------------------------------------------------------------ commands / idempotency(bus 用)
    def insert_command(self, *, trace_id: str, account_id: str, op: str, args_json: str, idempotency_key: Optional[str], confirm: bool,
                       timeout_ms: int, transport: str, actor: str, ip: Optional[str], now_ms: int) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO commands(trace_id, account_id, op, args_json, idempotency_key, confirm, timeout_ms, origin_transport, origin_actor, origin_ip, status, submitted_ms) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?)",
                      (trace_id, account_id, op, args_json, idempotency_key, 1 if confirm else 0, timeout_ms, transport, actor, ip, now_ms))

    def command_started(self, trace_id: str, now_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE commands SET status='running', started_ms=? WHERE trace_id=?", (now_ms, trace_id))

    def finish_command(self, *, trace_id: str, ok: bool, code: str, data: dict[str, Any], cost_ms: int, source: Optional[str],
                       error_message: Optional[str], retryable: Optional[bool], needs_human: Optional[bool],
                       confirmed_by: Optional[str], confirm_ms: Optional[int], now_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE commands SET status=?, finished_ms=? WHERE trace_id=?", ("done" if ok else "failed", now_ms, trace_id))
            c.execute("INSERT INTO command_results(trace_id, ok, code, data_json, cost_ms, source, error_message, error_retryable, error_needs_human, confirmed_by, confirm_ms, finished_ms) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trace_id) DO NOTHING",
                      (trace_id, 1 if ok else 0, code, json.dumps(data, ensure_ascii=False), cost_ms, source, error_message,
                       None if retryable is None else int(retryable), None if needs_human is None else int(needs_human), confirmed_by, confirm_ms, now_ms))

    def backfill_confirm_from_message(self, trace_id: str) -> Optional[dict[str, Any]]:
        """B-08:probe 命中后把 command_results.confirmed_by/confirm_ms 从该 trace 的出向行派生回填(只补空、code 不改)。返回那条出向行。"""
        with self._tx() as c:
            m = c.execute("SELECT id, ext_msg_id, confirmed_by, confirmed_ms, source FROM messages WHERE trace_id=? AND dir='out'", (trace_id,)).fetchone()
            if m is None or m["confirmed_by"] is None:
                return dict(m) if m else None
            c.execute("UPDATE command_results SET confirmed_by=COALESCE(confirmed_by, ?), confirm_ms=COALESCE(confirm_ms, ?) WHERE trace_id=?",
                      (m["confirmed_by"], m["confirmed_ms"], trace_id))
            return dict(m)

    def get_command_result(self, trace_id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM command_results WHERE trace_id=?", (trace_id,)).fetchone()
        return dict(r) | {"data": json.loads(r["data_json"])} if r else None

    def idem_get(self, account_id: str, key: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM idempotency WHERE account_id=? AND idem_key=?", (account_id, key)).fetchone()
        return dict(r) if r else None

    def idem_claim(self, *, account_id: str, key: str, op: str, args_hash: str, trace_id: str, now_ms: int, ttl_days: int) -> bool:
        """行级 claim:不存在才写 SENDING(§2.3.1 ③);返回 True = 本次抢到。"""
        with self._tx() as c:
            cur = c.execute("INSERT INTO idempotency(account_id, idem_key, op, args_hash, status, trace_id, created_ms, updated_ms, expires_ms) "
                            "VALUES (?,?,?,?,'SENDING',?,?,?,?) ON CONFLICT(account_id, idem_key) DO NOTHING",
                            (account_id, key, op, args_hash, trace_id, now_ms, now_ms, now_ms + ttl_days * 86400 * 1000))
            return cur.rowcount == 1

    def idem_finish(self, *, account_id: str, key: str, status: str, result_code: Optional[str], now_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE idempotency SET status=?, result_code=?, updated_ms=? WHERE account_id=? AND idem_key=?",
                      (status, result_code, now_ms, account_id, key))

    def idem_delete(self, account_id: str, key: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM idempotency WHERE account_id=? AND idem_key=?", (account_id, key))

    # ------------------------------------------------------------------ api 用的查询(账号 / 会话 / 消息 / 指令 / 调用方)
    def list_accounts(self, *, channel: Optional[str] = None, state: Optional[str] = None, enabled: Optional[bool] = None,
                      include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT a.*, r.app_version AS runtime_app_version, r.kind AS runtime_kind, r.container_name, r.adb_port, r.stream_port, r.frida_port, " \
              "r.adb_serial, r.ws_port, r.http_port, r.webui_port, r.wechat_version, r.wxkey_dll, r.error_since_ms AS runtime_error_since_ms, " \
              "r.desired_state, r.data_dir, r.mem_limit_mb, r.container_id, r.last_started_ms, r.last_stopped_ms, r.last_boot_completed_ms " \
              "FROM accounts a LEFT JOIN account_runtime r ON r.account_id = a.id WHERE 1=1"
        params: list[Any] = []
        if not include_deleted:
            sql += " AND a.deleted_ms IS NULL"
        if channel:
            sql += " AND a.channel=?"; params.append(channel)
        if state:
            sql += " AND a.state=?"; params.append(state)
        if enabled is not None:
            sql += " AND a.enabled=?"; params.append(1 if enabled else 0)
        sql += " ORDER BY a.channel, a.seq"
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def get_account_full(self, id: str) -> Optional[dict[str, Any]]:
        rows = [r for r in self.list_accounts(include_deleted=True) if r["id"] == id]
        return rows[0] if rows else None

    def upsert_runtime(self, account_id: str, *, kind: str, app_version: Optional[str] = None, now_ms: Optional[int] = None, **cols: Any) -> None:
        """account_runtime 行(runtime 模块是 owner;本期给测试与 app_version 回填用)。"""
        now = now_ms or self._clock()
        with self._tx() as c:
            c.execute("INSERT INTO account_runtime(account_id, kind, app_version, updated_ms) VALUES (?,?,?,?) "
                      "ON CONFLICT(account_id) DO UPDATE SET kind=excluded.kind, app_version=COALESCE(excluded.app_version, account_runtime.app_version), updated_ms=excluded.updated_ms",
                      (account_id, kind, app_version, now))
            for k, v in cols.items():
                c.execute(f"UPDATE account_runtime SET {k}=? WHERE account_id=?", (v, account_id))

    def list_sessions(self, *, account_id: Optional[str] = None, keyword: Optional[str] = None, kind: Optional[str] = None,
                      limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM sessions WHERE 1=1", []
        if account_id:
            sql += " AND account_id=?"; params.append(account_id)
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if keyword:
            sql += " AND (name LIKE ? OR native_id LIKE ?)"; params += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY COALESCE(last_msg_ms, 0) DESC, id LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def query_messages(self, *, account_id: Optional[str] = None, session_id: Optional[str] = None, dir: Optional[str] = None,
                       type: Optional[str] = None, state: Optional[str] = None, since_ms: Optional[int] = None, until_ms: Optional[int] = None,
                       sender: Optional[str] = None, q: Optional[str] = None, limit: int = 100,
                       before: Optional[tuple[int, str]] = None) -> tuple[list[dict[str, Any]], bool]:
        """#48:``q`` ≥ 3 字走 FTS trigram(多词 AND),否则 LIKE(``slow_match=True``);按 ts_ms DESC, id DESC;``before`` = 上一页末行 (ts_ms, id)。"""
        sql = "SELECT m.*, s.name AS session_name, s.kind AS session_kind FROM messages m JOIN sessions s ON s.id = m.session_id"
        params: list[Any] = []
        where: list[str] = []
        words = [w for w in (q or "").split() if w]
        fts_words = [w for w in words if len(w) >= 3]          # trigram:≥3 字符子串才能命中
        like_words = [w for w in words if len(w) < 3]          # 2 字回退 LIKE(02 §2.8.6);多词一律 AND
        slow = bool(like_words)
        if fts_words:
            sql += " JOIN messages_fts f ON f.rowid = m.rowid"
            params.append(" AND ".join('"' + w.replace('"', '""') + '"' for w in fts_words))
            where.append("f.messages_fts MATCH ?")
        for w in like_words:
            where.append("m.text LIKE ?"); params.append(f"%{w}%")
        if account_id:
            where.append("m.account_id=?"); params.append(account_id)
        if session_id:
            where.append("m.session_id=?"); params.append(session_id)
        if dir:
            where.append("m.dir=?"); params.append(dir)
        if type:
            where.append("m.type=?"); params.append(type)
        if state:
            where.append("m.state=?"); params.append(state)
        if since_ms is not None:
            where.append("m.ts_ms >= ?"); params.append(since_ms)
        if until_ms is not None:
            where.append("m.ts_ms <= ?"); params.append(until_ms)
        if sender:
            where.append("(m.sender_id=? OR m.sender_name=?)"); params += [sender, sender]
        if before is not None:
            where.append("(m.ts_ms < ? OR (m.ts_ms = ? AND m.id < ?))"); params += [before[0], before[0], before[1]]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.ts_ms DESC, m.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.con.execute(sql, params).fetchall()], slow

    def get_message_full(self, id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT m.*, s.name AS session_name, s.kind AS session_kind FROM messages m JOIN sessions s ON s.id = m.session_id WHERE m.id=?", (id,)).fetchone()
        return dict(r) if r else None

    def get_command(self, trace_id: str) -> Optional[dict[str, Any]]:
        r = self.con.execute("SELECT * FROM commands WHERE trace_id=?", (trace_id,)).fetchone()
        return dict(r) if r else None

    def list_commands(self, account_id: str, *, status: Optional[str] = None, op: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT c.*, r.ok, r.code, r.data_json, r.cost_ms, r.source, r.confirmed_by, r.finished_ms AS result_finished_ms FROM commands c " \
                      "LEFT JOIN command_results r ON r.trace_id = c.trace_id WHERE c.account_id=?", [account_id]
        if status:
            sql += " AND c.status=?"; params.append(status)
        if op:
            sql += " AND c.op=?"; params.append(op)
        sql += " ORDER BY c.submitted_ms DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def api_client_by_token(self, token: str) -> Optional[dict[str, Any]]:
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        r = self.con.execute("SELECT * FROM api_clients WHERE auth_kind='bearer' AND secret_hash=? AND enabled=1 AND revoked_ms IS NULL", (h,)).fetchone()
        return dict(r) if r else None

    def upsert_api_client(self, *, app_id: str, name: str, level: str, token: str, allow_accounts: Optional[list[str]] = None,
                          ip_allow: Optional[list[str]] = None, now_ms: Optional[int] = None) -> None:
        now = now_ms or self._clock()
        with self._tx() as c:
            c.execute("INSERT INTO api_clients(app_id, name, auth_kind, secret_hash, level, ip_allow_json, allow_accounts_json, created_ms, updated_ms) "
                      "VALUES (?,?,'bearer',?,?,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET secret_hash=excluded.secret_hash, level=excluded.level, "
                      "allow_accounts_json=excluded.allow_accounts_json, ip_allow_json=excluded.ip_allow_json, updated_ms=excluded.updated_ms, revoked_ms=NULL, enabled=1",
                      (app_id, name, hashlib.sha256(token.encode("utf-8")).hexdigest(), level,
                       json.dumps(ip_allow or []), json.dumps(allow_accounts or ["*"]), now, now))

    def api_client_touch(self, app_id: str, now_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE api_clients SET last_used_ms=? WHERE app_id=?", (now_ms, app_id))

    def db_size_mb(self) -> tuple[float, float]:
        if self.path == ":memory:":
            return 0.0, 0.0
        size = os.path.getsize(self.path) / 1048576 if os.path.exists(self.path) else 0.0
        wal = self.path + "-wal"
        return round(size, 2), round(os.path.getsize(wal) / 1048576, 2) if os.path.exists(wal) else 0.0

    def abandon_inflight(self, now_ms: int) -> int:
        """02 §2.6 崩溃恢复:queued/running 指令一律 failed + INTERNAL;对应 SENDING 幂等行改 ABANDONED。"""
        with self._tx() as c:
            rows = c.execute("SELECT trace_id, account_id, idempotency_key FROM commands WHERE status IN ('queued','running')").fetchall()
            for r in rows:
                c.execute("UPDATE commands SET status='failed', finished_ms=? WHERE trace_id=?", (now_ms, r["trace_id"]))
                c.execute("INSERT INTO command_results(trace_id, ok, code, data_json, cost_ms, error_message, error_retryable, error_needs_human, finished_ms) "
                          "VALUES (?,0,'INTERNAL','{}',0,'Agent 重启,指令未完成',1,0,?) ON CONFLICT(trace_id) DO NOTHING", (r["trace_id"], now_ms))
            c.execute("UPDATE idempotency SET status='ABANDONED', updated_ms=? WHERE status='SENDING'", (now_ms,))
            return len(rows)


class AsyncStore:
    """02 §2.2.8 并发:写锁按账号分片(``dict[account_id → asyncio.Lock]``),同号串行、异号不阻塞;阻塞 I/O 经 ``to_thread`` 离开事件循环。"""

    def __init__(self, store: Store):
        self.sync = store
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, account_id: str) -> asyncio.Lock:
        lk = self._locks.get(account_id)
        if lk is None:
            lk = self._locks[account_id] = asyncio.Lock()
        return lk

    async def ingest(self, msg: Message, **kw) -> IngestResult:
        async with self.lock_for(msg.account_id):
            return await asyncio.to_thread(self.sync.ingest, msg, **kw)

    async def ingest_batch(self, account_id: str, msgs: list[Message], cursor_update: Optional[CursorUpdate] = None, **kw):
        async with self.lock_for(account_id):
            return await asyncio.to_thread(self.sync.ingest_batch, msgs, cursor_update, **kw)

    async def run(self, account_id: Optional[str], fn: Callable, *args, **kw):
        """其它写操作:有账号维度的走该账号的分片锁;无账号维度直接进线程池。"""
        if account_id is None:
            return await asyncio.to_thread(fn, *args, **kw)
        async with self.lock_for(account_id):
            return await asyncio.to_thread(fn, *args, **kw)
