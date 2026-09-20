"""企点旁路读库读循环 —— 规格唯一出处:docs/06 §2.9.5 ``poll_maindb`` / ``check_group_gaps`` 伪代码(R6-38~R6-46),本文件逐分支照抄。

写在代码里的红线(与伪代码注释一一对应):
- 账号级内存态每账号各一份(``AccountPollState``),qd01 与 qd02 互不共享。
- 失败计数与告警的 firing/resolved **只由全量轮维护**;加速轮只读目标表,既不 fail() 也不 resolve()(⓪ 离开 running 的重置与 resolve 两种轮都做)。
- 判「首次」只认 ``qidian_bootstrap`` 标记行;换号 = 删旧水位 + 写新基准同一事务,先记审计 ``qidian.rebootstrap``。
- 历史闸:``time < qidian_bootstrap.value_int/1000 − 120`` 的行不入库、不发事件,水位照常越过;H13 firing 时不建基准,走 ``fail("clock_unsynced")``。
- 水位自检:``value.last_uniseq`` 对不上 = 库被重建 → 本表从 0 重扫;``message`` 事件只对 ``inserted or changed`` 发(重扫不重放)。
- 任何 sqlite 报错:全量轮 → ``fail(open_failed|schema_mismatch)``;加速轮 → 直接 return;已提交的表不回滚。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ...alerts import QIDIAN_DB_UNAVAILABLE, QIDIAN_MSG_GAP, QIDIAN_TABLE_DECODE_STUCK, Alerts
from ...config import AgentConfig
from ...events import Events, message_payload
from ...store import CursorUpdate, Store
from .maindb import MainDb, MainDbError, db_path_for
from .msgdata import MessageFactory
from .xor import xor_hex

log = logging.getLogger("qtrade.adapters.qidian.poll")

HISTORY_GATE_MARGIN_S = 120          # 历史闸余量(开放项 (f):余量内的行会真入库)
STUCK_ROUNDS = 12                    # 个别表连续自检不过 ≥ 12 个全量轮 → QIDIAN_TABLE_DECODE_STUCK
FAIL_ROUNDS = 3                      # 读库连续 3 轮失败 → QIDIAN_DB_UNAVAILABLE
FAIL_ROUNDS_GRACE = 12               # 首登建库期 not_found / clock_unsynced 放宽到 12 轮
GATED_WARN_AFTER_MS = 10 * 60 * 1000 # bootstrap 已过 10 分钟后单轮仍有被闸行 → WARNING
_HEX32 = re.compile(r"[0-9A-F]{32}")


@dataclass
class AccountPollState:
    """伪代码里挂在 ``acct.`` 上的内存态,每企点账号各一份(R6-40)。"""
    table_map: dict[str, str] = field(default_factory=dict)     # 表名 → native_id
    bad_rounds: dict[str, int] = field(default_factory=dict)    # 表名 → 连续自检不过的轮数
    maindb_seen: bool = False
    db_fail_rounds: int = 0
    factory: Optional[MessageFactory] = None


@dataclass
class QidianAccountView:
    """poll 只需要的账号字段:``id``/``state``/``self_uid``/``app_version``,由适配器从 store 取后传入。"""
    id: str
    state: str
    self_uid: Optional[str]
    app_version: Optional[str] = None


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("ascii")).hexdigest().upper()


def _native_id_and_kind(peer: str, istroop: int) -> tuple[str, str]:
    return (peer, "private") if istroop == 0 else ("g_" + peer, "group")


class QidianPoller:
    def __init__(self, *, store: Store, events: Events, alerts: Alerts, cfg: AgentConfig,
                 h13_firing: Callable[[], bool], clock: Callable[[], int], maindb_factory: Callable[[str], MainDb]):
        self.store = store
        self.events = events
        self.alerts = alerts
        self.cfg = cfg
        self.h13_firing = h13_firing
        self.clock = clock
        self.maindb_factory = maindb_factory      # self_uid → MainDb
        self.states: dict[str, AccountPollState] = {}

    def state_of(self, account_id: str) -> AccountPollState:
        st = self.states.get(account_id)
        if st is None:
            st = self.states[account_id] = AccountPollState()
        return st

    # ------------------------------------------------------------------ 告警辅助
    def _subject(self, acct: QidianAccountView) -> str:
        return "account:" + acct.id

    def _fail(self, acct: QidianAccountView, st: AccountPollState, reason: str) -> None:
        """读库失败的终态 ``fail(reason)``:计数器只有一个、不分 reason;阈值按本轮 reason 取。"""
        st.db_fail_rounds += 1
        grace = (reason == "not_found" and not st.maindb_seen) or reason == "clock_unsynced"
        threshold = FAIL_ROUNDS_GRACE if grace else FAIL_ROUNDS
        if st.db_fail_rounds >= threshold:
            self.alerts.firing(QIDIAN_DB_UNAVAILABLE, subject=self._subject(acct), severity="warn",
                               evidence={"db_path": db_path_for(acct.self_uid or ""), "reason": reason, "app_version": acct.app_version},
                               hint_actions=["open_env"], account_id=acct.id)
        log.info("qidian poll fail account=%s reason=%s rounds=%d/%d", acct.id, reason, st.db_fail_rounds, threshold)

    # ------------------------------------------------------------------ poll_maindb
    def poll_maindb(self, acct: QidianAccountView, only_sessions: Optional[list[str]] = None) -> None:
        st = self.state_of(acct.id)
        full = only_sessions is None
        subject = self._subject(acct)

        # ⓪ 登录前空转 —— 不是故障:不读、不告警、不推水位;离开 running 即全部重置(两种轮都做)
        if acct.state != "running" or not acct.self_uid:
            st.maindb_seen = False
            st.db_fail_rounds = 0
            st.table_map = {}
            st.bad_rounds = {}
            self.alerts.resolve(QIDIAN_DB_UNAVAILABLE, subject=subject, account_id=acct.id)
            self.alerts.resolve(QIDIAN_TABLE_DECODE_STUCK, subject=subject, account_id=acct.id)
            return
        if st.factory is None or st.factory.self_uid != acct.self_uid:
            st.factory = MessageFactory(acct.id, acct.self_uid)

        db = self.maindb_factory(acct.self_uid)
        try:
            if not db.exists():
                if full:
                    self._fail(acct, st, "not_found")
                return
            st.maindb_seen = True
            boot = self.store.cursor_get(acct.id, "qidian_bootstrap")

            if not full:
                # —— 加速轮:只查目标会话表,跳过 ①②③ ——
                if boot is None or boot.value != acct.self_uid:
                    return
                todo = [t for t, nid in st.table_map.items() if nid in (only_sessions or [])]
            else:
                # ① 表发现:每轮枚举
                names = set(db.list_message_tables())
                for t in list(st.table_map):
                    if t not in names:
                        del st.table_map[t]
                for t in list(st.bad_rounds):
                    if t not in names:
                        del st.bad_rounds[t]        # 连它的自检失败计数一起摘,否则 stuck 永不为空
                # ② 表 → native_id
                for t in names - st.table_map.keys():
                    parts = t.split("_")
                    h = parts[2] if len(parts) > 2 else ""
                    if not _HEX32.fullmatch(h):
                        continue
                    row = db.last_row_peer(t)
                    if row is None:
                        continue                    # 空表:认不出会话,有行了再认(⇒ 进了 table_map 的表一定非空)
                    fu_hex, istroop = row
                    try:
                        peer = xor_hex(fu_hex).decode("ascii")
                    except (ValueError, UnicodeDecodeError):
                        peer = ""
                    if not peer or md5_upper(peer) != h:
                        st.bad_rounds[t] = st.bad_rounds.get(t, 0) + 1
                        continue                    # 自检不过:只跳过这一张表,不停整轮
                    st.bad_rounds.pop(t, None)
                    st.table_map[t] = _native_id_and_kind(peer, istroop)[0]
                if st.bad_rounds and not st.table_map:
                    # 一张都解不出 = 密钥或 schema 变了:整库不可读;先销掉「个别表」码,同一成因只留一条告警
                    self.alerts.resolve(QIDIAN_TABLE_DECODE_STUCK, subject=subject, account_id=acct.id)
                    self._fail(acct, st, "decode_failed")
                    return
                stuck = sorted(t for t, n in st.bad_rounds.items() if n >= STUCK_ROUNDS)
                if stuck:
                    self.alerts.firing(QIDIAN_TABLE_DECODE_STUCK, subject=subject, severity="warn",
                                       evidence={"tables": stuck}, account_id=acct.id)
                else:
                    self.alerts.resolve(QIDIAN_TABLE_DECODE_STUCK, subject=subject, account_id=acct.id)
                # ③ 游标 bootstrap —— 单独一个短事务,只写 cursors
                first = (boot is None) or (boot.value != acct.self_uid)
                if first:
                    if self.h13_firing():
                        self._fail(acct, st, "clock_unsynced")   # 判定在 DELETE 之前;不静默停摆
                        return
                    now_ms = self.clock()
                    self.store.qidian_bootstrap_rebase(acct.id, acct.self_uid, old_uin=(boot.value if boot is not None else None), now_ms=now_ms)
                    boot = self.store.cursor_get(acct.id, "qidian_bootstrap")
                for t, nid in st.table_map.items():
                    if self.store.cursor_get(acct.id, "qidian_rowid:" + nid) is not None:
                        continue
                    if first:
                        m = db.last_id_uniseq(t)   # 非空(见 ②)
                        if m is None:
                            continue
                        self.store.cursor_set(acct.id, "qidian_rowid:" + nid, m[0], json.dumps({"last_uniseq": m[1]}))
                    else:
                        self.store.cursor_set(acct.id, "qidian_rowid:" + nid, 0, None)   # bootstrap 之后才出现的表 = 新会话,从 0 起
                todo = list(st.table_map)

            assert boot is not None and boot.value_int is not None
            # ④ 增量 —— 每张表一次 ingest_batch = 一个短事务(会话 → 消息 → 该表水位)
            cutoff_s = boot.value_int / 1000 - HISTORY_GATE_MARGIN_S
            gated = 0
            n_unknown = 0
            gated_detail: dict[str, list[int]] = {}       # 表名 → [min_time, max_time, n](WARNING 用,R6-51)
            for t in todo:
                nid = st.table_map[t]
                kind = "group" if nid.startswith("g_") else "private"
                cur = self.store.cursor_get(acct.id, "qidian_rowid:" + nid)
                if cur is None:
                    continue                        # 还没建水位的表留给全量轮的 ③
                last = cur.value_int or 0
                if last > 0:
                    u = db.uniseq_at(t, last)       # 水位自检:水位那一行还在、且还是同一条消息吗?
                    if u is None or cur.value is None or u != cur.value_json().get("last_uniseq"):
                        last = 0                    # 库被重建/该表被清/游标缺 last_uniseq → 本表从 0 重扫
                rows = db.rows_after(t, last)
                if not rows:
                    continue                        # 没有新行:水位不动
                msgs = []
                for r in rows:
                    if r.time < cutoff_s:
                        gated += 1
                        d = gated_detail.setdefault(t, [r.time, r.time, 0])
                        d[0], d[1], d[2] = min(d[0], r.time), max(d[1], r.time), d[2] + 1
                        continue                    # 历史行:不产出 Message(水位照常越过)
                    m, unknown = st.factory.to_message(r, table=t, native_id=nid, kind=kind)
                    if unknown:
                        n_unknown += 1
                    if m is not None:
                        msgs.append(m)
                now_ms = self.clock()
                results = self.store.ingest_batch(
                    msgs, CursorUpdate(acct.id, "qidian_rowid:" + nid, rows[-1].id, json.dumps({"last_uniseq": rows[-1].uniseq})), now_ms=now_ms)
                for inserted, changed, msg in results:
                    if inserted or changed:         # 三通道统一的发出条件;重扫撞键、内容无变化的已存在行不发
                        origin = None
                        if msg.dir == "out":
                            origin = "rpa" if self._sent_by_us(msg) else "external"
                        self.events.emit("message", payload=message_payload(msg, late_after_s=self.cfg.messages.late_after_s, origin=origin),
                                         account_id=acct.id, channel="qidian", trace_id=msg.trace_id, now_ms=now_ms)
            if gated or n_unknown:
                log.info("qidian poll counters account=%s gated=%d n_unknown=%d", acct.id, gated, n_unknown)
                if gated and self.clock() - boot.value_int > GATED_WARN_AFTER_MS:
                    detail = "; ".join(f"{t}: {n} 行, time {a}~{b}" for t, (a, b, n) in gated_detail.items())
                    log.warning("qidian 历史闸在 bootstrap 10 分钟后仍挡掉 %d 行 account=%s cutoff_s=%d(核对时钟/基准)—— %s",
                                gated, acct.id, int(cutoff_s), detail)
        except MainDbError as e:
            if full:
                self._fail(acct, st, e.reason)
            return
        if full:
            st.db_fail_rounds = 0
            self.alerts.resolve(QIDIAN_DB_UNAVAILABLE, subject=subject, account_id=acct.id)   # 只有全量轮整轮成功才代表「整库可读」

    def _sent_by_us(self, msg) -> bool:
        """06 §2.9.5 掉线续读条:``dir='out' AND trace_id IS NULL`` 且窗内无同文本带 trace_id 的出向行 ⇒ external。
        合并进出向行(changed)的读回行 id 就是那条出向行,其 trace_id 非空 ⇒ rpa。"""
        row = self.store.get_message(msg.id) if msg.id else None
        if row is None:
            return False
        if row.get("trace_id"):
            return True
        n = norm_text(row.get("text"))
        if not n:
            return False
        others = self.store.con.execute(
            "SELECT text FROM messages WHERE account_id=? AND session_id=? AND dir='out' AND trace_id IS NOT NULL AND ABS(ts_ms-?) <= ?",
            (row["account_id"], row["session_id"], row["ts_ms"], self.cfg.bus.out_merge_window_s * 1000)).fetchall()
        return any(norm_text(o["text"]) == n for o in others)

    # ------------------------------------------------------------------ check_group_gaps
    def check_group_gaps(self, acct: QidianAccountView) -> None:
        st = self.state_of(acct.id)
        subject = self._subject(acct)
        if acct.state != "running" or not st.table_map:
            return                                  # 未登录 / 进程刚重启、全量轮还没重建映射:既不产出也不 resolve
        if self.h13_firing():
            return                                  # 时钟漂移中:窗口起点不可信,本轮不判
        db = self.maindb_factory(acct.self_uid or "")
        since = self.clock() // 1000 - self.cfg.qidian.gap_window_days * 86400
        gaps = []
        try:
            for t, nid in st.table_map.items():
                if not nid.startswith("g_"):
                    continue                        # 只对群
                s = db.group_gap_stats(t, since)
                if s is None:
                    continue
                missing = s.mx - s.mn + 1 - s.cnt
                if missing >= self.cfg.qidian.gap_min_missing:
                    gaps.append({"session_id": acct.id + ":" + nid, "missing": missing, "first_ts": s.first_ts, "last_ts": s.last_ts})
        except MainDbError:
            return
        if gaps:
            gaps.sort(key=lambda g: g["missing"], reverse=True)
            self.alerts.firing(QIDIAN_MSG_GAP, subject=subject, severity="warn", account_id=acct.id,
                               evidence={"window_days": self.cfg.qidian.gap_window_days, "total_missing": sum(g["missing"] for g in gaps),
                                         "sessions": gaps[:20]})
        else:
            self.alerts.resolve(QIDIAN_MSG_GAP, subject=subject, account_id=acct.id)


def norm_text(s):
    from ...text import norm
    return norm(s)
