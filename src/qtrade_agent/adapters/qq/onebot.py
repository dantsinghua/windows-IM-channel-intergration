"""OneBot v11 正向 WS 客户端 —— 规格:docs/02 §2.2.3 ``qq`` 行(「包 ``OneBotClient``,**不改它**;补 ``get_msg`` 读回、
``send_group_msg/send_private_msg`` 的确认包装、``on_message`` → ``store.ingest``」)、§2.2 技术选型(``websockets`` 库)、
docs/00 §3 端口段(``ws=16100+NN``、``http=16200+NN``)、docs/04 §2.3 H08(``meta_event.heartbeat`` 默认 5 s 一次)。

分层(本文件只做**传输 + 协议**,不碰 store/events/告警):
- ``OneBotTransport``:一条 WS 连接的最小面(``connect/send/recv/close``),可被替换。
- ``WebsocketsTransport``:真实现,**可选导入** ``websockets``(缺包只在真正 ``connect()`` 时报错,不影响 import 本模块)。
- ``FakeOneBot``:进程内假实现,按 OneBot v11 语义回 action、可推事件、可模拟断线 —— 开发容器里一律用它,
  **绝不连真实 QQ / NapCat**(SKILL §4 禁区)。
- ``OneBotClient``:echo 匹配的 ``call_action``、事件分发、心跳/事件水位记账、断线重连退避。

🔴 **不做自动重登(D-2,docs/02 §2.2.3 末条)**:本客户端的重连是**传输层重连**(我方 ↔ napcat 的本地连接),
不是账号重登;重连后若 napcat 报未登录,由上层(适配器 / H08)置 ``login_required``,客户端不重扫码、不重发凭据。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Protocol

from .config import ONEBOT_ACTION_TIMEOUT_S, RECONNECT_BACKOFF_MAX_FACTOR, QQAdapterConfig

log = logging.getLogger("qtrade.adapters.qq.onebot")

EventFn = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotClosed(Exception):
    """传输层连接已断(``recv`` 在连接关闭时抛它,``OneBotClient`` 据此进入重连退避)。"""


class OneBotError(Exception):
    """action 被 napcat 拒绝(``status='failed'``),带 OneBot 的 ``retcode``。"""

    def __init__(self, action: str, retcode: int, message: str = ""):
        super().__init__(f"{action} 失败 retcode={retcode} {message}".strip())
        self.action = action
        self.retcode = retcode
        self.message = message


class OneBotTransport(Protocol):
    """一条 OneBot 正向 WS 连接。实现方只管字节进出,重连/echo 匹配都在 ``OneBotClient``。"""

    async def connect(self, url: str, *, access_token: Optional[str] = None) -> None: ...
    async def send(self, payload: str) -> None: ...
    async def recv(self) -> str:
        """阻塞到下一帧;连接断开抛 ``OneBotClosed``。"""
        ...
    async def close(self) -> None: ...


def ws_url(ws_port: int) -> str:
    """00 §3:napcat OneBot 正向 WS 一律绑 ``127.0.0.1``(``16101–16199``,每 QQ 账号一个)。"""
    return f"ws://127.0.0.1:{ws_port}"


def http_url(http_port: int) -> str:
    """00 §3 / 04 H08 备用探测:``GET http://127.0.0.1:162NN/get_status``。"""
    return f"http://127.0.0.1:{http_port}"


# ---------------------------------------------------------------------- 真实现(可选依赖)
class WebsocketsTransport:
    """``websockets`` 库的薄包装(02 §2.2 技术选型)。开发容器里不用它 —— 一律注入 ``FakeOneBot``。"""

    def __init__(self) -> None:
        self._ws: Any = None

    async def connect(self, url: str, *, access_token: Optional[str] = None) -> None:
        try:
            import websockets                                   # 可选导入:缺包不影响本模块被 import
        except ImportError as e:                                # pragma: no cover - 开发容器不装
            raise OneBotClosed(f"未安装 websockets,无法连接 {url}: {e}") from e
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
        try:
            self._ws = await websockets.connect(url, additional_headers=headers, open_timeout=5, proxy=None)
        except Exception:
            raise OneBotClosed("OneBot 连接未就绪") from None

    async def send(self, payload: str) -> None:
        if self._ws is None:
            raise OneBotClosed("未连接")
        await self._ws.send(payload)

    async def recv(self) -> str:
        if self._ws is None:
            raise OneBotClosed("未连接")
        try:
            frame = await self._ws.recv()
        except Exception as e:                                  # pragma: no cover - 真机路径
            raise OneBotClosed(str(e)) from e
        return frame if isinstance(frame, str) else frame.decode("utf-8")

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None


# ---------------------------------------------------------------------- 假实现(开发容器唯一允许的后端)
class FakeOneBot:
    """进程内假 napcat:按 OneBot v11 语义回 action、可推事件、可模拟断线。

    已实现的 action(本期用得到的全集):``get_login_info`` · ``get_status`` · ``send_private_msg`` · ``send_group_msg`` ·
    ``get_msg`` · ``get_friend_msg_history`` · ``get_group_msg_history``。其余 action 回 ``retcode=1404``。
    """

    def __init__(self, *, self_id: int = 415011447, nickname: str = "假QQ", clock: Optional[Callable[[], int]] = None,
                 online: bool = True):
        self.self_id = self_id
        self.nickname = nickname
        self.online = online
        self.good = True
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.sent: list[dict[str, Any]] = []                    # 我方发出的消息(send_*_msg 的落点)
        self.store: dict[int, dict[str, Any]] = {}              # message_id → get_msg 能查到的消息
        self.history: dict[str, list[dict[str, Any]]] = {}      # native_id → 历史消息(补拉用)
        self.next_message_id = 1
        self.next_message_seq = 1
        self.fail_actions: dict[str, int] = {}                  # action → retcode(测试用:让某个 action 失败)
        self.drop_from_store: set[int] = set()                  # get_msg 查不到的 message_id(读回失败)
        self.connected = False
        self.connect_calls = 0
        self._q: asyncio.Queue[Optional[str]] = asyncio.Queue()

    # ---- 测试侧工具
    def push_event(self, event: dict[str, Any]) -> None:
        """把一条 OneBot 事件推给客户端(``post_type=message|notice|meta_event``)。"""
        event.setdefault("self_id", self.self_id)
        event.setdefault("time", self._clock() // 1000)
        self._q.put_nowait(json.dumps(event, ensure_ascii=False))

    def push_heartbeat(self, *, online: Optional[bool] = None) -> None:
        """04 H08:``meta_event.heartbeat``,NapCat 默认 5 s 一次。"""
        self.push_event({"post_type": "meta_event", "meta_event_type": "heartbeat", "interval": 5000,
                         "status": {"online": self.online if online is None else online, "good": self.good}})

    def drop(self) -> None:
        """模拟连接断开:``recv`` 抛 ``OneBotClosed``。"""
        self.connected = False
        self._q.put_nowait(None)

    def record_history(self, native_id: str, event: dict[str, Any]) -> None:
        self.history.setdefault(native_id, []).append(event)

    # ---- OneBotTransport
    async def connect(self, url: str, *, access_token: Optional[str] = None) -> None:
        self.connect_calls += 1
        self.connected = True
        while not self._q.empty():          # 清掉上一条连接的残留(含 drop() 的哨兵);**不换队列对象**——
            self._q.get_nowait()            # 换了会让还挂在旧队列上的 recv() 永远收不到新响应(真 ws 的 recv 是按新句柄读的)

    async def send(self, payload: str) -> None:
        if not self.connected:
            raise OneBotClosed("未连接")
        frame = json.loads(payload)
        self._q.put_nowait(json.dumps(self._handle(frame), ensure_ascii=False))

    async def recv(self) -> str:
        item = await self._q.get()
        if item is None:
            raise OneBotClosed("连接已断")
        return item

    async def close(self) -> None:
        self.connected = False

    # ---- action 分派
    def _handle(self, frame: dict[str, Any]) -> dict[str, Any]:
        action, params, echo = frame.get("action", ""), frame.get("params") or {}, frame.get("echo")
        rc = self.fail_actions.get(action)
        if rc is not None:
            return {"status": "failed", "retcode": rc, "data": None, "echo": echo}
        fn = getattr(self, f"_act_{action}", None)
        if fn is None:
            return {"status": "failed", "retcode": 1404, "data": None, "echo": echo}
        return {"status": "ok", "retcode": 0, "data": fn(params), "echo": echo}

    def _act_get_login_info(self, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self.online:
            return {}                                            # 05 §2.3.3:未登录时长期不返回 user_id
        return {"user_id": self.self_id, "nickname": self.nickname}

    def _act_get_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"online": self.online, "good": self.good}

    def _store_sent(self, native_id: str, kind: str, message: Any) -> dict[str, Any]:
        mid, seq = self.next_message_id, self.next_message_seq
        self.next_message_id += 1
        self.next_message_seq += 1
        segs = message if isinstance(message, list) else [{"type": "text", "data": {"text": str(message)}}]
        rec = {"message_id": mid, "message_seq": seq, "real_id": mid, "time": self._clock() // 1000,
               "message_type": kind, "message": segs, "self_id": self.self_id,
               "sender": {"user_id": self.self_id, "nickname": self.nickname}}
        if kind == "group":
            rec["group_id"] = int(native_id[2:])
        else:
            rec["user_id"] = int(native_id)
        self.store[mid] = rec
        self.sent.append(rec)
        return rec

    def _act_send_private_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        rec = self._store_sent(str(params["user_id"]), "private", params.get("message"))
        return {"message_id": rec["message_id"]}

    def _act_send_group_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        rec = self._store_sent(f"g_{params['group_id']}", "group", params.get("message"))
        return {"message_id": rec["message_id"]}

    def _act_get_msg(self, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        mid = int(params["message_id"])
        if mid in self.drop_from_store:
            return None
        return self.store.get(mid)

    def _act_get_friend_msg_history(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = self.history.get(str(params["user_id"]), [])
        return {"messages": rows[-int(params.get("count") or len(rows)):]}

    def _act_get_group_msg_history(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = self.history.get(f"g_{params['group_id']}", [])
        return {"messages": rows[-int(params.get("count") or len(rows)):]}


# ---------------------------------------------------------------------- 客户端
class OneBotClient:
    """echo 匹配的 ``call_action`` + 事件分发 + 心跳水位 + 断线重连退避。

    - ``echo``:每次 action 自增一个序号作 ``echo``,响应帧按它认领 future(OneBot v11 标准做法)。
    - 心跳:``meta_event.heartbeat`` 与任意事件都推进 ``last_event_ms``;心跳另记 ``last_heartbeat_ms`` 与 ``last_status``。
    - 重连:``recv`` 抛 ``OneBotClosed`` ⇒ 按 ``[adapters.qq] reconnect_delay_s`` 退避重连;重连成功调 ``on_reconnect``
      (适配器据此做 02 §2.8.1 的历史补拉)。**这是传输层重连,不是账号重登(D-2)**。
    """

    def __init__(self, url: str, *, transport: OneBotTransport, cfg: Optional[QQAdapterConfig] = None,
                 access_token: Optional[str] = None, on_event: Optional[EventFn] = None,
                 on_reconnect: Optional[Callable[[], Awaitable[None]]] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.url = url
        self.cfg = cfg or QQAdapterConfig()
        self._transport = transport
        self._access_token = access_token
        self._on_event = on_event
        self._on_reconnect = on_reconnect
        self._clock = clock
        self._echo = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self.connected = False
        self.connect_count = 0
        self.reconnects = 0
        self.last_event_ms: Optional[int] = None
        self.last_heartbeat_ms: Optional[int] = None
        self.last_status: dict[str, Any] = {}
        self.last_lifecycle: Optional[str] = None

    # ---- 生命周期
    async def start(self) -> None:
        """建连并起收帧循环;**不阻塞到登录**(02 §2.2.3 ``start()`` 语义)。首连失败不抛,交给重连循环。"""
        if self._task is not None:
            return
        self._closing = False
        try:
            await self._connect_once()
        except OneBotClosed as e:
            log.warning("OneBot 首连失败(交给重连循环)url=%s: %s", self.url, e)
        self._task = asyncio.create_task(self._run(), name=f"onebot:{self.url}")

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._transport.close()
        self.connected = False
        self._fail_pending(OneBotClosed("客户端已关闭"))

    async def _connect_once(self) -> None:
        await self._transport.connect(self.url, access_token=self._access_token)
        self.connected = True
        self.connect_count += 1
        if self.last_event_ms is None:
            self.last_event_ms = self._clock()      # **只有首连**算一次「有动静」,免得 H08 首轮拿 None 当超时;
            # 重连不刷这个水位 —— 刷了就等于「连接一抖,30 s 无心跳的账就一笔勾销」,H08 永远发现不了心跳丢失

    def backoff_s(self, fails: int) -> float:
        """``reconnect_delay_s × min(2^(fails−1), RECONNECT_BACKOFF_MAX_FACTOR)``(规格只给基础 3 s,退避形态见 handoff 张力 ②)。"""
        return self.cfg.reconnect_delay_s * min(2 ** max(0, fails - 1), RECONNECT_BACKOFF_MAX_FACTOR)

    async def reconnect(self) -> bool:
        """H08 的自愈动作:立刻断开重连一次(04 §2.3 H08「重连 WS」)。返回是否连上。"""
        await self._transport.close()
        self.connected = False
        self._fail_pending(OneBotClosed("主动重连"))
        try:
            await self._connect_once()
        except OneBotClosed as e:
            log.warning("OneBot 主动重连失败 url=%s: %s", self.url, e)
            return False
        self.reconnects += 1
        if self._on_reconnect is not None:
            await self._on_reconnect()
        return True

    async def _run(self) -> None:
        fails = 0
        while not self._closing:
            if not self.connected:
                fails += 1
                await asyncio.sleep(self.backoff_s(fails))
                if self._closing:
                    return
                try:
                    await self._connect_once()
                except OneBotClosed as e:
                    log.warning("OneBot 重连失败(第 %d 次)url=%s: %s", fails, self.url, e)
                    continue
                self.reconnects += 1
                fails = 0
                if self._on_reconnect is not None:
                    await self._on_reconnect()
            try:
                raw = await self._transport.recv()
            except OneBotClosed as e:
                self.connected = False
                self._fail_pending(OneBotClosed(str(e)))
                log.warning("OneBot 连接断开 url=%s: %s", self.url, e)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:                              # 收帧异常不杀循环
                log.exception("OneBot 收帧异常 url=%s: %s", self.url, e)
                continue
            await self._dispatch(raw)

    # ---- 收帧
    async def _dispatch(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            log.warning("OneBot 收到非 JSON 帧,丢弃(len=%d)", len(raw))
            return
        echo = frame.get("echo")
        if echo is not None and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(frame)
            return
        now = self._clock()
        self.last_event_ms = now
        if frame.get("post_type") == "meta_event":
            if frame.get("meta_event_type") == "heartbeat":
                self.last_heartbeat_ms = now
                self.last_status = dict(frame.get("status") or {})
            elif frame.get("meta_event_type") == "lifecycle":
                self.last_lifecycle = frame.get("sub_type")
        if self._on_event is not None:
            try:
                await self._on_event(frame)
            except Exception as e:                              # 单条事件的异常不杀连接(02 §2.2.3:回调只入库与发事件)
                log.exception("OneBot 事件回调异常 post_type=%s: %s", frame.get("post_type"), e)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ---- action
    async def call_action(self, action: str, params: Optional[dict[str, Any]] = None, *,
                          timeout_s: float = ONEBOT_ACTION_TIMEOUT_S) -> Any:
        """任意 action;成功回 ``data``,``status='failed'`` 抛 ``OneBotError``,连接不在/超时抛 ``OneBotClosed``。"""
        if not self.connected:
            raise OneBotClosed(f"未连接,无法调用 {action}")
        self._echo += 1
        echo = str(self._echo)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        payload = json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False)
        try:
            await self._transport.send(payload)
            frame = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise OneBotClosed(f"{action} 等待响应超时({timeout_s}s)") from None
        except Exception:
            self._pending.pop(echo, None)
            raise
        if frame.get("status") == "failed" or frame.get("retcode", 0) != 0:
            raise OneBotError(action, int(frame.get("retcode", -1)), str(frame.get("message") or frame.get("msg") or ""))
        return frame.get("data")

    # ---- 心跳水位(H08 用)
    def silence_ms(self, now_ms: int) -> Optional[int]:
        """距上一次收到任何 OneBot 帧多久(ms);从没收到过回 ``None``。"""
        return None if self.last_event_ms is None else max(0, now_ms - self.last_event_ms)
