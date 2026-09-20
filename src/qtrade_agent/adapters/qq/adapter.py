"""QQ 适配器(M2.5)—— 规格:docs/02 §2.2.3 ``adapters`` 契约与 ``qq`` 行、§2.8.1 QQ 入库路径、docs/06 §2.9.1/§2.9.2/§2.9.4/§2.12 QQ 各条。

四件事:
1. **推型入库**:``on_message`` → ``normalize.to_message`` → ``store.ingest``(与出向同一入口,先 upsert ``sessions`` 再写 ``messages``,R6-16)
   → ``events.emit('message')``。接收回调**只做入库与发事件、不做写动作**(02 §2.2.3 并发条)。
2. **发送与读回确认**:``send_group_msg``/``send_private_msg`` → OneBot 回 ``message_id`` → ``get_msg`` 存在 ⇒ 把出向 ``SENDING`` 行
   绑 ``ext_msg_id``、置 ``DELIVERED``、``confirmed_by='get_msg'``(06 §2.12 QQ 行)。绑定走 ``store.ingest`` 的
   「入向撞到自己发的 ⇒ 合并」这一支(``store._confirmed_by_for('onebot') == 'get_msg'``),不另开写路径。
   限速(``MIN_SEND_INTERVAL``)按 02 §2.2.3 在 ``bus`` 里,本适配器不重写。
3. **撤回**:``notice.group_recall``/``friend_recall`` ⇒ 标记不删(06 §2.9.4),按原行 ``ts`` 重放 ingest 打 ``revoked``。
4. **掉线重连补历史**:传输层重连后按 ``[adapters.qq] history_backfill_on_reconnect``(默认 50)补 ``cursors`` 之后的(02 §2.8.1 P-13)。

🔴 **不做自动重登(D-2)**:``start()`` 只建连/挂 hook、不阻塞到登录;运行期发现未登录只经 ``get_state`` 报 ``login_required``,
由人重新走 05 §2.3 的登录流。WS 重连是传输层的事,不是账号重登。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ...config import AgentConfig
from ...events import Events, message_payload
from ...models import Command, CommandError, CommandResult, Message, Session
from ...store import Store
from ..base import Account
from .config import GET_STATE_CACHE_MS, QQAdapterConfig
from .normalize import (action_for, ext_msg_id, history_action_for, is_self_event, message_seq_of, native_id_of,
                        split_session, to_message)
from .onebot import OneBotClient, OneBotClosed, OneBotError, OneBotTransport, WebsocketsTransport, ws_url

log = logging.getLogger("qtrade.adapters.qq.adapter")

CAPABILITIES = frozenset({"get_state", "list_sessions", "read_messages", "send_text"})
"""= ``capabilities/*.json`` 目录里 ``channels['qq'] == 'supported'`` 的全部 op(R6-57 ⑧:Account 静态集 = 目录 × supported)。
对账由 ``tests/test_qq_adapter.py::test_capabilities_match_catalog`` 守。"""

SEQ_OFFSET = 2                  # 00 §6:account_id = {qd|qq|wx}{NN},序号从第 3 个字符起
RECALL_NOTICES = ("group_recall", "friend_recall")


def seq_of(account_id: str) -> int:
    return int(account_id[SEQ_OFFSET:])


@dataclass
class QQSession:
    """02 §2.2.3 内部状态:每账号一个 ``AdapterSession``(连接/hook 句柄 + 最近 ``get_state`` 缓存 ≤2 s)。"""
    acct: Account
    client: OneBotClient
    state_cache: Optional[tuple[int, str]] = None       # (算出的时刻 ms, state)
    ingested: int = 0
    backfilled: int = 0
    events_seen: dict[str, int] = field(default_factory=dict)


def default_transport_factory(acct: Account) -> OneBotTransport:
    """真实现:``websockets``(02 §2.2 技术选型)。开发容器里**一律注入 ``FakeOneBot``**,绝不连真 napcat(SKILL §4 禁区)。"""
    return WebsocketsTransport()


class QQAdapter:
    channel = "qq"
    capabilities = CAPABILITIES

    def __init__(self, *, store: Store, events: Events, cfg: AgentConfig, qq_cfg: Optional[QQAdapterConfig] = None,
                 transport_factory: Callable[[Account], OneBotTransport] = default_transport_factory,
                 access_token_for: Optional[Callable[[Account], Optional[str]]] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.store = store
        self.events = events
        self.cfg = cfg
        self.qq = qq_cfg or QQAdapterConfig()
        self._transport_factory = transport_factory
        self._access_token_for = access_token_for or (lambda acct: None)
        self.clock = clock
        self._sessions: dict[str, QQSession] = {}

    # ------------------------------------------------------------------ 生命周期
    def session_of(self, account_id: str) -> Optional[QQSession]:
        return self._sessions.get(account_id)

    def ws_url_for(self, acct: Account) -> str:
        """00 §3 / 02 §2.2.4 端口推导:``ws = 16100 + NN``(不查表、不随机)。"""
        from ...runtime import port_plan
        return ws_url(port_plan("qq", seq_of(acct.id))["ws"])

    async def start(self, acct: Account) -> None:
        """建连 + 挂 hook,**不阻塞到登录**(02 §2.2.3)。已建连则幂等返回。"""
        if acct.id in self._sessions:
            return
        client = OneBotClient(self.ws_url_for(acct), transport=self._transport_factory(acct), cfg=self.qq,
                              access_token=self._access_token_for(acct), clock=self.clock,
                              on_event=lambda frame, aid=acct.id: self._on_event_by_id(aid, frame),
                              on_reconnect=lambda a=acct: self.backfill(a))
        self._sessions[acct.id] = QQSession(acct=acct, client=client)
        await client.start()

    async def stop(self, acct: Account, *, graceful: bool) -> None:
        sess = self._sessions.pop(acct.id, None)
        if sess is not None:
            await sess.client.close()

    async def close(self) -> None:
        for aid in list(self._sessions):
            sess = self._sessions.pop(aid)
            await sess.client.close()

    # ------------------------------------------------------------------ get_state
    async def get_state(self, acct: Account) -> str:
        """00 §8.1 状态。连不上 / 查不到就**沿用库里的状态**——掉线判定归 04 H08(``health.py``),适配器不擅自改状态(D-2)。"""
        sess = self._sessions.get(acct.id)
        if sess is None:
            return acct.state
        now = self.clock()
        if sess.state_cache is not None and now - sess.state_cache[0] < GET_STATE_CACHE_MS:
            return sess.state_cache[1]
        state = acct.state
        try:
            status = await sess.client.call_action("get_status")
            if isinstance(status, dict) and status.get("online") is False:
                state = "login_required"
            elif isinstance(status, dict) and status.get("online") is True:
                info = await sess.client.call_action("get_login_info")
                state = "running" if isinstance(info, dict) and info.get("user_id") else "login_required"
        except (OneBotClosed, OneBotError) as e:
            log.info("QQ get_state 未取到(沿用 %s)account=%s: %s", acct.state, acct.id, e)
        sess.state_cache = (now, state)
        return state

    # ------------------------------------------------------------------ execute(除 send_* 以外)
    async def execute(self, acct: Account, cmd: Command) -> CommandResult:
        tid = cmd.trace_id or ""
        if cmd.op == "get_state":
            state = await self.get_state(acct)
            return CommandResult(ok=True, code="OK", trace_id=tid, source="onebot",
                                 data={"state": state, "state_code": acct.state_code, "self_uid": acct.self_uid})
        if cmd.op == "list_sessions":
            from ...api.serialize import session_view
            rows = self.store.list_sessions(account_id=acct.id, keyword=cmd.args.get("keyword"),
                                            limit=int(cmd.args.get("limit") or 100))
            return CommandResult(ok=True, code="OK", trace_id=tid, source="onebot", data={"data": [session_view(r) for r in rows]})
        if cmd.op == "read_messages":
            from ...api.serialize import message_view
            native, _ = split_session(acct.id, cmd.args["session"])
            rows = self.store.list_messages(acct.id, session_id=f"{acct.id}:{native}", limit=int(cmd.args.get("limit") or 100))
            return CommandResult(ok=True, code="OK", trace_id=tid, source="onebot", data={"items": [message_view(r) for r in rows]})
        if cmd.op == "screenshot":
            # 主文档 B.2 能力矩阵 `➖` = 该通道无此概念:napcat 容器没有画面(QQ 走 OneBot 协议,不走 redroid 画面流)
            return CommandResult(ok=False, code="NOT_APPLICABLE", trace_id=tid, source="onebot",
                                 error=CommandError("QQ 通道无画面(napcat 容器不是 redroid)", reason="no_screen"))
        return CommandResult(ok=False, code="UNSUPPORTED", trace_id=tid, source="onebot",
                             error=CommandError(f"qq 不支持 {cmd.op}"))

    # ------------------------------------------------------------------ send(内含读回确认)
    async def send(self, acct: Account, cmd: Command) -> CommandResult:
        """02 §2.2.3:``send`` 内含读回确认与「先查是否已发」(企点那条例外**不适用 QQ**——QQ 的读回不依赖 ``poll``)。"""
        tid = cmd.trace_id or ""
        sess = self._sessions.get(acct.id)
        if sess is None or not sess.client.connected:
            return CommandResult(ok=False, code="NOT_READY", trace_id=tid, source="onebot",
                                 error=CommandError("OneBot 连接未建立", reason="onebot_unreachable", retryable=True))
        native, kind = split_session(acct.id, cmd.args["session"])
        action, params = action_for(native, kind, cmd.args["text"])
        try:
            data = await sess.client.call_action(action, params)
        except (OneBotClosed, OneBotError) as e:
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=tid, source="onebot",
                                 error=CommandError(f"{action} 失败:{e}", retryable=True))
        message_id = (data or {}).get("message_id") if isinstance(data, dict) else None
        if message_id is None:
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=tid, source="onebot",
                                 error=CommandError(f"{action} 未返回 message_id", retryable=True))
        confirmed = await self._readback(sess, native, kind, int(message_id), trace_id=cmd.trace_id)
        # 不论读回成没成都返回 ok —— 确认由 bus 在 [bus] confirm_timeout_qq_ms 内看出向行状态(读不到 ⇒ UNCONFIRMED)
        return CommandResult(ok=True, code="OK", trace_id=tid, source="onebot",
                             data={"native_id": native, "message_id": message_id, "confirmed": confirmed})

    async def _readback(self, sess: QQSession, native_id: str, kind: str, message_id: int,
                        *, trace_id: Optional[str] = None) -> bool:
        """06 §2.12 QQ 行:``get_msg`` 存在 ⇒ **直接绑定该行** ``ext_msg_id``、``DELIVERED``、``confirmed_by='get_msg'``。

        🔴 「该行」= **本指令的 `SENDING` 行**,入口是 ``store.bind_out_by_trace_id``(rulings R6-58 (g))——
        QQ 是三通道里唯一手里有**确定** ``message_id`` 的,走 ``store.ingest`` 的模糊合并(fingerprint /
        同会话同 ``norm(text)`` + 时间窗)是把确定性降级:``capture_text=false`` 且出向行 ``ts`` 与
        ``get_msg.time`` 跨秒时两支判据都不成立 ⇒ 该行恒 ``UNCONFIRMED``。
        拿不到 ``trace_id``(不经总线的直调)才退回 ``ingest`` 合并支。
        """
        try:
            data = await sess.client.call_action("get_msg", {"message_id": message_id})
        except (OneBotClosed, OneBotError) as e:
            log.info("QQ get_msg 读回未成(留给 bus 判 UNCONFIRMED)account=%s message_id=%s: %s", sess.acct.id, message_id, e)
            return False
        if not isinstance(data, dict) or not data:
            return False
        if trace_id:
            bound = self.store.bind_out_by_trace_id(trace_id, ext_msg_id(native_id, message_id), "get_msg")
            if bound is not None:
                row = self.store.message_state(bound)
                if row is not None:
                    self.events.emit("message", payload={"id": bound, "ext_msg_id": row["ext_msg_id"], "state": row["state"],
                                                         "confirmed_by": row["confirmed_by"], "account_id": sess.acct.id,
                                                         "channel": "qq", "dir": "out"},
                                     account_id=sess.acct.id, channel="qq", trace_id=trace_id)
                return True
        event = dict(data)
        event.setdefault("post_type", "message_sent")
        event.setdefault("self_id", sess.acct.self_uid)
        event.setdefault("message_type", kind)
        event["message_id"] = message_id
        self._fill_session_fields(event, native_id, kind)
        msg = to_message(sess.acct.id, event, self_uid=sess.acct.self_uid)
        msg.dir, msg.self, msg.state = "out", True, "DELIVERED"
        inserted, changed = self._ingest(sess, msg)
        return changed or inserted

    async def confirm_probe(self, acct: Account, cmd: Command) -> bool:
        """幂等 ``SENDING`` 态复核(02 §2.2.3:「QQ 可 ``get_msg``」)——先在**本库**找窗内同会话同 ``norm(text)`` 的已绑定出向行,
        拿它的 ``message_id`` 回 napcat 验一次是否真在。查不到行就判「没发出去」。"""
        native, _ = split_session(acct.id, cmd.args["session"])
        row = self.store.find_out_by_text(acct.id, f"{acct.id}:{native}", cmd.args["text"],
                                          self.cfg.bus.out_merge_window_s * 1000, cmd.submitted_at_ms or self.clock())
        if row is None or not row.get("ext_msg_id"):
            return False
        sess = self._sessions.get(acct.id)
        if sess is None or not sess.client.connected:
            return True                             # 本库已有绑定好的出向行 = 上次确实发出去了;连不上 napcat 不改这个事实
        try:
            data = await sess.client.call_action("get_msg", {"message_id": int(str(row["ext_msg_id"]).rsplit(":", 1)[1].split("#")[0])})
        except (OneBotClosed, OneBotError, ValueError):
            return True
        return bool(data)

    async def poll(self, acct: Account, *, only_sessions: Optional[list[str]] = None) -> None:
        """推型通道为空实现(02 §2.2.3 协议注释)。QQ 的入库靠 ``on_message`` 推,不靠轮询。"""
        return None

    # ------------------------------------------------------------------ 事件回调(在连接 task 里跑)
    async def _on_event_by_id(self, account_id: str, frame: dict[str, Any]) -> None:
        sess = self._sessions.get(account_id)
        if sess is not None:
            await self._on_event(sess, frame)

    async def _on_event(self, sess: QQSession, frame: dict[str, Any]) -> None:
        post = frame.get("post_type")
        sess.events_seen[str(post)] = sess.events_seen.get(str(post), 0) + 1
        if post in ("message", "message_sent"):
            self._ingest_event(sess, frame)
            return
        if post == "notice" and frame.get("notice_type") in RECALL_NOTICES:
            self._revoke(sess, frame)
            return
        if post == "meta_event":
            # 06 §2.9.3:``cursors(owner='qqNN', kind='ws_last_event').value_int = last_event_ms``,只用于监控「多久没事件了」,不做拉取
            self.store.cursor_set(sess.acct.id, "ws_last_event", self.clock())

    def _ingest_event(self, sess: QQSession, event: dict[str, Any]) -> None:
        raw_ref = None
        if self.cfg.messages.raw_payload:
            raw_ref = json.dumps(event, ensure_ascii=False)      # 02 §2.8 raw 落盘本期未接,先原样带在行上
        try:
            msg = to_message(sess.acct.id, event, self_uid=sess.acct.self_uid, raw_ref=raw_ref)
        except ValueError as e:
            log.warning("QQ 事件无法归一化(丢弃)account=%s: %s", sess.acct.id, e)
            return
        self._ingest(sess, msg)
        seq = message_seq_of(event)
        if seq is not None:
            cur = self.store.cursor_get(sess.acct.id, f"onebot_seq:{msg.session.native_id}")
            if cur is None or (cur.value_int or 0) < seq:
                self.store.cursor_set(sess.acct.id, f"onebot_seq:{msg.session.native_id}", seq)

    def _ingest(self, sess: QQSession, msg: Message) -> tuple[bool, bool]:
        now = self.clock()
        r = self.store.ingest(msg, now_ms=now)
        if r.inserted or r.changed:                              # 三通道统一的发出条件:重扫撞键、内容无变化的已存在行不发
            sess.ingested += 1
            self._emit(sess, msg, now)
        return r.inserted, r.changed

    def _emit(self, sess: QQSession, msg: Message, now_ms: int) -> None:
        origin = None
        if msg.dir == "out":
            row = self.store.get_message(msg.id) if msg.id else None
            origin = "rpa" if (row and row.get("trace_id")) else "external"   # R6-40:非本系统发出的我方消息 = external
        self.events.emit("message", payload=message_payload(msg, late_after_s=self.cfg.messages.late_after_s, origin=origin),
                         account_id=sess.acct.id, channel="qq", trace_id=msg.trace_id, now_ms=now_ms)

    # ------------------------------------------------------------------ 撤回(06 §2.9.4:标记不删)
    def _revoke(self, sess: QQSession, event: dict[str, Any]) -> None:
        try:
            native, kind = native_id_of(message_type="group" if event.get("group_id") is not None else "private",
                                        group_id=event.get("group_id"), user_id=event.get("user_id"))
        except ValueError as e:
            log.warning("QQ 撤回通知缺会话标识(丢弃)account=%s: %s", sess.acct.id, e)
            return
        ext = ext_msg_id(native, event.get("message_id"))
        row = self._find_family_row(sess.acct.id, ext)
        if row is None:
            # 我们没这条消息(装机前发的 / 已过保留期):不臆造行,只记一笔(06 §2.9.4「不臆造」同款态度)
            log.info("QQ 撤回通知对应的消息不在库里,跳过 account=%s ext=%s", sess.acct.id, ext)
            return
        msg = Message(account_id=sess.acct.id, channel="qq", session=Session(sess.acct.id, native, kind, name=""),
                      dir=row["dir"], type=row["type"], text=None, ts_ms=int(row["ts_ms"]), source="onebot",
                      ext_msg_id=row["ext_msg_id"], dedup_kind="native", sender_id=row["sender_id"], sender_name=row["sender_name"],
                      self=bool(row["is_self"]), state=row["state"] or "DELIVERED", revoked=True,
                      revoked_ms=int(event.get("time") or 0) * 1000 or self.clock(),
                      revoked_by=(None if event.get("operator_id") is None else str(event["operator_id"])))
        self._ingest(sess, msg)

    def _find_family_row(self, account_id: str, ext: str) -> Optional[dict[str, Any]]:
        """与 ``store._ingest_one`` QQ 支同一口径:同键族(原行 + 已有 ``#n`` 行)里取 ``ts`` 最大的一行(06 §2.9.2 R6-51)。"""
        r = self.store.con.execute(
            "SELECT * FROM messages WHERE account_id=? AND (ext_msg_id=? OR ext_msg_id LIKE ?) ORDER BY ts_ms DESC, rowid DESC LIMIT 1",
            (account_id, ext, ext + "#%")).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ 掉线重连补历史(02 §2.8.1 P-13)
    @staticmethod
    def _fill_session_fields(event: dict[str, Any], native_id: str, kind: str) -> None:
        event.setdefault("message_type", kind)
        if kind == "group":
            event.setdefault("group_id", int(native_id[2:]))
        else:
            event.setdefault("user_id", int(native_id))
            if is_self_event(event):
                event.setdefault("target_id", int(native_id))

    async def backfill(self, acct: Account) -> int:
        """重连后补 ``cursors`` 之后的历史,每会话最多 ``history_backfill_on_reconnect`` 条(默认 50;``0`` = 关)。

        补拉行的 ``received_ms`` 自然晚于 ``ts_ms``(02 §2.8.1 原话);撤回由后续 ``revoked`` 更新覆盖。
        """
        sess = self._sessions.get(acct.id)
        n = self.qq.history_backfill_on_reconnect
        if sess is None or n <= 0:
            return 0
        total = 0
        for row in self.store.list_sessions(account_id=acct.id, limit=1000):
            native, kind = row["native_id"], row["kind"]
            cur = self.store.cursor_get(acct.id, f"onebot_seq:{native}")
            since = cur.value_int if cur else None
            action, params = history_action_for(native, kind, n)
            try:
                data = await sess.client.call_action(action, params)
            except (OneBotClosed, OneBotError) as e:
                log.info("QQ 补历史未成 account=%s session=%s: %s", acct.id, native, e)
                continue
            rows = data.get("messages") if isinstance(data, dict) else data
            top = since
            for ev in (rows or []):
                if not isinstance(ev, dict):
                    continue
                ev.setdefault("self_id", acct.self_uid)
                self._fill_session_fields(ev, native, kind)
                seq = message_seq_of(ev)
                if since is not None and seq is not None and seq <= since:
                    continue                                    # 只补水位之后的
                try:
                    msg = to_message(acct.id, ev, self_uid=acct.self_uid)
                except ValueError as e:
                    log.warning("QQ 补历史行无法归一化(跳过)account=%s: %s", acct.id, e)
                    continue
                inserted, changed = self._ingest(sess, msg)
                total += 1 if (inserted or changed) else 0
                if seq is not None and (top is None or seq > top):
                    top = seq
            if top is not None and top != since:
                self.store.cursor_set(acct.id, f"onebot_seq:{native}", top)
        sess.backfilled += total
        return total
