"""微信读循环(Agent 拉型;06 §2.9.1 微信行 + §2.9.3 ``chatlog_seq:<talker>`` 游标 + 05 §2.4.4 ⑦)。

红线(与规格一一对应):
- **Agent 拉,WinAgent 从不回调**(C-25/C-03):每 ``[adapters.wechat] poll_interval_s=5`` s 调 #39
  ``GET /wa/v1/wechat/read``;**发送确认期**(send 后 ``[bus] confirm_timeout_wechat_ms`` 窗口)周期加密到
  ``confirm_poll_interval_ms=1000``(05 §2.4.4 ⑦ / 06 §2.9.1)。
- **游标**:``cursors(owner=<wxNN>, kind='chatlog_seq:<talker>')``,``value_int = last_seq``(已见最大 seq,
  既是去重键也是 ``since_seq``),``value = {"last_ts_ms":…,"last_seq":…}``(06 §2.9.3;``last_ts_ms`` 给 chatlog
  按时间取窗用,只存 seq 就得反推时间)。**游标推进与消息落库同一事务**(06 §2.9.3 末的红线)——每个 talker 一次
  ``ingest_batch(msgs, CursorUpdate(...))``。
- **全量轮的 `since_seq`**:05 §2.4.4 ⑦ 写「该账号的 seq 水位」而 06 §2.9.3 的游标是**每会话一条**——本实现取
  ``max(per-talker value_int)`` 作账号级 ``since_seq``,依据是 06 §2.9.2「chatlog ``seq`` 单调、不复用(collector 已验证)」;
  加速轮(``only_sessions``)则**逐 talker**用各自水位。两册的张力已登记进 handoff「建议裁决」。
- **重扫不重放**:``message`` 事件只对 ``inserted or changed`` 发(与企点同一条口径,R6-40);
  出向读回行按 06 §2.9.5 掉线续读条判 ``origin='rpa'|'external'``。
- **读失败**:05 §2.5.4「微信(chatlog 挂、微信在线)」——连续 ``READ_FAIL_STREAK=3`` 次异常 **不是掉线**,
  而是 ``degraded(KEY_FAIL)``,由 ``WechatAdapter.get_state`` 按本计数判定;poller 自己不改账号状态。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ...alerts import Alerts
from ...config import AgentConfig
from ...events import Events, message_payload
from ...store import CursorUpdate, Store
from ...text import norm
from .client import WeChatCallFailed, WeChatNotReady, WeChatWinAgent
from .normalize import to_message
from .settings import WechatAdapterConfig

log = logging.getLogger("qtrade.adapters.wechat.poll")

CURSOR_PREFIX = "chatlog_seq:"
READ_FAIL_STREAK = 3          # 05 §2.5.4:连续 3 次 wechat/read 异常 ⇒ degraded(KEY_FAIL)(不是掉线)
READ_LIMIT = 200              # 单轮取回上限(#39 `limit`);剩下的下一轮继续,水位保证不丢


@dataclass
class WechatAccountView:
    """poll 只需要的账号字段,由适配器从 store 取后传入(与企点 ``QidianAccountView`` 同形)。"""
    id: str
    state: str
    self_uid: Optional[str] = None
    self_nick: Optional[str] = None


@dataclass
class AccountPollState:
    """每微信账号各一份的内存态(wx01 与 wx02 互不共享;Agent 重启即丢,规格如此)。"""
    retry_attempts: int = 0
    retry_started_ms: Optional[int] = None
    next_retry_ms: int = 0
    retry_exhausted: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    read_fail_streak: int = 0
    confirm_until_ms: int = 0                       # 发送确认期截止(> now 时用 1 s 加速周期)
    last_error: Optional[str] = None
    seen_talkers: set[str] = field(default_factory=set)


class WechatPoller:
    def __init__(self, *, store: Store, events: Events, client: WeChatWinAgent, cfg: AgentConfig,
                 wechat_cfg: Optional[WechatAdapterConfig] = None, clock: Callable[[], int]):
        self.store = store
        self.events = events
        self.client = client
        self.cfg = cfg
        self.wechat_cfg = wechat_cfg or cfg.wechat_adapter
        self.clock = clock
        self.alerts = Alerts(events, clock=clock)
        self.state: dict[str, AccountPollState] = {}

    # ------------------------------------------------------------------ 内存态与周期
    def st(self, account_id: str) -> AccountPollState:
        s = self.state.get(account_id)
        if s is None:
            s = self.state[account_id] = AccountPollState()
        return s

    def note_send(self, account_id: str) -> None:
        """``send`` 之后开发送确认期:窗口 = ``[bus] confirm_timeout_wechat_ms``(05 §2.4.4 ⑦「send 后 10s 窗口」)。"""
        self.st(account_id).confirm_until_ms = self.clock() + self.cfg.bus.confirm_timeout_wechat_ms

    def interval_s(self, account_id: str) -> float:
        """当前该账号的拉取周期:确认期内 ``confirm_poll_interval_ms``,否则 ``poll_interval_s``。"""
        s = self.st(account_id)
        if s.retry_exhausted:
            return float(self.wechat_cfg.poll_interval_s)
        if s.retry_started_ms is not None:
            return max(0.0, (s.next_retry_ms - self.clock()) / 1000)
        if self.clock() < s.confirm_until_ms:
            return self.wechat_cfg.confirm_poll_interval_ms / 1000
        return float(self.wechat_cfg.poll_interval_s)

    def degraded_key_fail(self, account_id: str) -> bool:
        """05 §2.5.4:连续 3 次 ``wechat/read`` 异常(而主窗口仍在)⇒ ``degraded(KEY_FAIL)``。"""
        return self.st(account_id).read_fail_streak >= READ_FAIL_STREAK

    # ------------------------------------------------------------------ 游标(06 §2.9.3)
    def cursor_seq(self, account_id: str, talker: str) -> int:
        cur = self.store.cursor_get(account_id, CURSOR_PREFIX + talker)
        return int(cur.value_int or 0) if cur else 0

    def account_seq(self, account_id: str) -> int:
        """全量轮的 ``since_seq`` = 该账号所有会话水位的最大值(依据 06 §2.9.2「seq 单调、不复用」)。"""
        rows = self.store.cursors_list(account_id, CURSOR_PREFIX)
        return max((int(c.value_int or 0) for c in rows), default=0)

    # ------------------------------------------------------------------ 一轮
    async def poll(self, acct: WechatAccountView, only_sessions: Optional[list[str]] = None) -> int:
        """At most one bounded attempt; retries are owned here, not by the transport."""
        st = self.st(acct.id)
        if st.lock.locked():
            return 0
        async with st.lock:
            now = self.clock()
            if st.retry_exhausted:
                return 0
            if st.retry_started_ms is not None and now - st.retry_started_ms >= self.wechat_cfg.read_retry_window_s * 1000:
                self._exhaust_read(acct.id, "retry_window_exhausted")
                return 0
            if now < st.next_retry_ms:
                return 0
            before = self.store.get_account_full(acct.id)
            started = now
            batches = [(t, self.cursor_seq(acct.id, t)) for t in only_sessions] if only_sessions else [(None, self.account_seq(acct.id))]
            rows: list[dict[str, Any]] = []
            try:
                for talker, since in batches:
                    rows.extend(await self.client.read(talker=talker, since_seq=since, limit=READ_LIMIT))
            except (WeChatNotReady, WeChatCallFailed) as error:
                self._read_failed(acct.id, error, started)
                return 0
            current = self.store.get_account_full(acct.id)
            if before and (not current or current.get("deleted_ms") or not current.get("enabled")
                           or current.get("state") in ("stopped", "stopping", "disabled")
                           or current.get("self_uid") != before.get("self_uid")
                           or current.get("desired_state") != before.get("desired_state")):
                return 0
            result = self._ingest(acct, rows)
            st.read_fail_streak = st.retry_attempts = 0
            st.retry_started_ms = None
            st.retry_exhausted = False
            st.next_retry_ms = 0
            st.last_error = None
            self.alerts.resolve("WECHAT_READ_RETRY_EXHAUSTED", subject="account:" + acct.id, account_id=acct.id)
            return result

    def _read_failed(self, account_id: str, error: Exception, started_ms: int) -> None:
        st = self.st(account_id)
        body = getattr(error, "body", None)
        detail = body.get("error") if isinstance(body, dict) else None
        reason = getattr(error, "reason", None) or (detail.get("reason") if isinstance(detail, dict) else None)
        if not isinstance(reason, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", reason):
            reason = "wechat_read_http_" + str(getattr(error, "status", "unavailable"))
        st.last_error = reason
        # A timeout cannot prove that a key is invalid; keep its actual layer/reason.
        if not (isinstance(error, WeChatCallFailed) and error.status == 504) and reason != "wechat_read_catching_up":
            st.read_fail_streak += 1
        if st.retry_started_ms is None:
            st.retry_started_ms = started_ms
        st.retry_attempts += 1
        delay = min(self.wechat_cfg.read_retry_backoff_max_s,
                    self.wechat_cfg.read_retry_backoff_base_s * (2 ** min(st.retry_attempts - 1, 16)))
        st.next_retry_ms = self.clock() + delay * 1000
        row = self.store.get_account_full(account_id)
        if row and not row.get("auto_recover"):
            self._exhaust_read(account_id, "auto_recover_disabled")
        elif st.retry_attempts >= self.wechat_cfg.read_retry_max:
            self._exhaust_read(account_id, "retry_count_exhausted")
        elif self.clock() - st.retry_started_ms >= self.wechat_cfg.read_retry_window_s * 1000:
            self._exhaust_read(account_id, "retry_window_exhausted")
        log.warning("WeChat read incomplete account=%s cause=%s attempt=%d exhausted=%s",
                    account_id, reason, st.retry_attempts, st.retry_exhausted)

    def _exhaust_read(self, account_id: str, stop_reason: str) -> None:
        st = self.st(account_id)
        st.retry_exhausted = True
        self.alerts.firing("WECHAT_READ_RETRY_EXHAUSTED", subject="account:" + account_id, account_id=account_id,
                          evidence={"cause": st.last_error, "stop_reason": stop_reason,
                                    "attempts": st.retry_attempts, "cursor_unchanged": True},
                          hint_actions=["open_logs", "restart"])

    def degraded_read_failed(self, account_id: str) -> bool:
        st = self.st(account_id)
        return st.retry_exhausted or st.read_fail_streak >= READ_FAIL_STREAK or (
            st.retry_attempts >= READ_FAIL_STREAK and st.last_error != "wechat_read_catching_up")


    def _ingest(self, acct: WechatAccountView, rows: list[dict[str, Any]]) -> int:
        """按 talker 分组:每组一次 ``ingest_batch``(会话 → 消息 → 该会话水位,同一事务)。"""
        by_talker: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_talker.setdefault(str(r["talker"]), []).append(r)
        n = 0
        for talker, group in by_talker.items():
            group.sort(key=lambda r: int(r["seq"]))
            msgs = [to_message(acct.id, r, self_uid=acct.self_uid, self_nick=acct.self_nick) for r in group]
            for message in msgs:
                message._native_account_uid = acct.self_uid
            last_seq = int(group[-1]["seq"])
            last_ts = msgs[-1].ts_ms
            prev = self.cursor_seq(acct.id, talker)
            now_ms = self.clock()
            cursor = CursorUpdate(acct.id, CURSOR_PREFIX + talker, max(prev, last_seq),
                                  _cursor_value(last_ts, max(prev, last_seq)), native_account_uid=acct.self_uid)
            results = self.store.ingest_batch(msgs, cursor, now_ms=now_ms)
            self.st(acct.id).seen_talkers.add(talker)
            for inserted, changed, msg in results:
                if not (inserted or changed):
                    continue                        # 重叠窗重复取到的边界行:不重放事件(与企点同口径,R6-40)
                n += 1
                origin = None
                if msg.dir == "out":
                    origin = "rpa" if self._sent_by_us(msg) else "external"
                self.events.emit("message", payload=message_payload(msg, late_after_s=self.cfg.messages.late_after_s, origin=origin),
                                 account_id=acct.id, channel="wechat", trace_id=msg.trace_id, now_ms=now_ms)
        return n

    def _sent_by_us(self, msg) -> bool:
        """06 §2.9.5 掉线续读条(三通道同口径):``dir='out' AND trace_id IS NULL`` 且窗内无同文本带 trace_id 的出向行 ⇒ external。
        合并进出向行(changed)的读回行 id 就是那条出向行,其 ``trace_id`` 非空 ⇒ rpa(别的端/人用微信自己发的 ⇒ external)。"""
        row = self.store.get_message(msg.id) if msg.id else None
        if row is None:
            return False
        if row.get("trace_id"):
            return True
        n = norm(row.get("text"))
        if not n:
            return False
        others = self.store.con.execute(
            "SELECT text FROM messages WHERE account_id=? AND session_id=? AND dir='out' AND trace_id IS NOT NULL AND ABS(ts_ms-?) <= ?",
            (row["account_id"], row["session_id"], row["ts_ms"], self.cfg.bus.out_merge_window_s * 1000)).fetchall()
        return any(norm(o["text"]) == n for o in others)


def _cursor_value(last_ts_ms: int, last_seq: int) -> str:
    import json
    return json.dumps({"last_ts_ms": int(last_ts_ms), "last_seq": int(last_seq)}, ensure_ascii=False)
