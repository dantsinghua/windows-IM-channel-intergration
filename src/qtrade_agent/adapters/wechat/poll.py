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

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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
        self.wechat_cfg = wechat_cfg or WechatAdapterConfig()
        self.clock = clock
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
        """一轮增量。``only_sessions`` = 确认窗内只查目标会话(与 ``Adapter.poll`` 协议同义);返回本轮入库/更新的行数。"""
        st = self.st(acct.id)
        batches: list[tuple[Optional[str], int]] = []
        if only_sessions:
            batches = [(t, self.cursor_seq(acct.id, t)) for t in only_sessions]
        else:
            batches = [(None, self.account_seq(acct.id))]
        rows: list[dict[str, Any]] = []
        try:
            for talker, since in batches:
                rows.extend(await self.client.read(talker=talker, since_seq=since, limit=READ_LIMIT))
        except (WeChatNotReady, WeChatCallFailed) as e:
            st.read_fail_streak += 1
            st.last_error = repr(e)
            log.warning("微信 read 失败 account=%s streak=%d: %s", acct.id, st.read_fail_streak, e)
            return 0
        st.read_fail_streak = 0
        st.last_error = None
        return self._ingest(acct, rows)

    def _ingest(self, acct: WechatAccountView, rows: list[dict[str, Any]]) -> int:
        """按 talker 分组:每组一次 ``ingest_batch``(会话 → 消息 → 该会话水位,同一事务)。"""
        by_talker: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_talker.setdefault(str(r["talker"]), []).append(r)
        n = 0
        for talker, group in by_talker.items():
            group.sort(key=lambda r: int(r["seq"]))
            msgs = [to_message(acct.id, r, self_uid=acct.self_uid, self_nick=acct.self_nick) for r in group]
            last_seq = int(group[-1]["seq"])
            last_ts = msgs[-1].ts_ms
            prev = self.cursor_seq(acct.id, talker)
            now_ms = self.clock()
            cursor = CursorUpdate(acct.id, CURSOR_PREFIX + talker, max(prev, last_seq),
                                  _cursor_value(last_ts, max(prev, last_seq)))
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
