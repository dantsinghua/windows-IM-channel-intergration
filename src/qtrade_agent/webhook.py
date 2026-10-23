"""webhook 投递器(02 §2.2.7 ``events`` 的「outbox → webhook」那一半)。

规格逐条:

- **职责**(§2.2.7):统一事件的 webhook 投递 —— HMAC 签名、重试、指数退避、死信。
- **并发**(§2.2.7):投递器一个 task 轮询 ``events_outbox where status='pending' and next_attempt_ms<=now``
  **每 500 ms**;**每个 webhook 一条串行投递(保序)**;同一 webhook 连续失败 ``dead_after_attempts``
  (默认 10)次进 ``dead``,并发 ``alert``(§3.7 ``WEBHOOK_DEAD``,warn,``subject='host'``)。
- **outbox 两类行**(§2.2.7):``target='ws'`` 是规范事件记录(写入即 ``delivered``、不经投递器);
  ``target='webhook:<id>'`` 每个**启用的** webhook 一行,带各自重试状态 —— 投递器**只轮询这一类**。
- **订阅过滤**:``webhooks.events_json`` / ``accounts_json``(``["*"]`` = 不限)。
  ⚠️ **唯一例外**(§2.2.12 E-3):``NET_PUBLIC_ENDPOINT_CHANGED`` 要**向全部 ``enabled=1`` 的登记方各投一行**,
  **不受订阅过滤** —— 走 :meth:`WebhookDispatcher.fanout` 的 ``ignore_filters=True``。
- **签名**(§3.5 末行):**同一套 canonical**(``POST \\n <url path> \\n <query> \\n sha256(body) \\n ts \\n nonce``),
  头名同上**加前缀 ``X-QT-Webhook-*``**,secret 为 ``webhooks.secret_ref``(Vault)。
  canonical/签名实现不在本文件重写,一律复用 ``hmac_inbound``(单一出处)。
- **退避表**:``[events] webhook_backoff_ms``(§7.1,十档);``webhook_timeout_ms=5000``、``webhook_max_attempts=10``。
  每个 webhook 行另有自己的 ``timeout_ms``/``max_attempts``(§3.1 DDL),**行值优先**、缺省取配置。

HTTP 走注入的 :class:`HttpClient` 协议:开发容器里一律注入 :class:`FakeHttp`(**绝不真出网**);
真实现 :class:`UrllibHttp` 用标准库 ``urllib`` 在 ``asyncio.to_thread`` 里跑(00 §11.20 ⑦:阻塞 I/O 进线程池)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Optional, Protocol, Union

from .events import iso8601
from .hmac_inbound import canonical_string, signature_header, split_url

log = logging.getLogger("qtrade.webhook")

WEBHOOK_DEAD = "WEBHOOK_DEAD"                 # 02 §3.7:warn / subject='host' / event 族 alert
TARGET_PREFIX = "webhook:"

# 02 §7.1 [events]:webhook 三键(逐字默认值)
DEFAULT_TIMEOUT_MS = 5000
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_BACKOFF_MS = (1000, 2000, 5000, 10000, 30000, 60000, 120000, 300000, 600000, 600000)


@dataclass(frozen=True)
class WebhookConfig:
    """02 §7.1 ``[events]`` 的 webhook 三键。"""
    webhook_timeout_ms: int = DEFAULT_TIMEOUT_MS
    webhook_max_attempts: int = DEFAULT_MAX_ATTEMPTS
    webhook_backoff_ms: tuple[int, ...] = DEFAULT_BACKOFF_MS
    poll_interval_ms: int = 500                # §2.2.7「每 500ms」(字面量,非配置项)


# ---------------------------------------------------------------- HTTP 客户端协议 + 两个实现
@dataclass
class HttpResponse:
    status: int
    body: bytes = b""


class HttpClient(Protocol):
    async def post(self, url: str, *, body: bytes, headers: dict[str, str], timeout_ms: int) -> HttpResponse: ...


@dataclass
class FakeHttp:
    """可编程假 HTTP 客户端(开发容器唯一允许的实现:**不发任何真实出网请求**)。

    ``script[url]`` 按顺序弹出:``HttpResponse`` 直接返回,``Exception`` 实例即抛出(模拟连不上/超时)。
    脚本用尽后回 ``default``。``requests`` 记录每次调用,供断言签名头与 body。
    """
    default: HttpResponse = field(default_factory=lambda: HttpResponse(200))
    script: dict[str, list[Union[HttpResponse, Exception]]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, url: str, *items: Union[HttpResponse, Exception]) -> None:
        self.script.setdefault(url, []).extend(items)

    async def post(self, url: str, *, body: bytes, headers: dict[str, str], timeout_ms: int) -> HttpResponse:
        self.requests.append({"url": url, "body": body, "headers": dict(headers), "timeout_ms": timeout_ms})
        queue = self.script.get(url) or []
        item = queue.pop(0) if queue else self.default
        if isinstance(item, Exception):
            raise item
        return item


class UrllibHttp:
    """标准库实现:``urllib.request`` 在线程里跑(00 §11.20 ⑦ 阻塞 I/O 进线程池)。

    ``honor_env_proxy=False``(02 §7.1 ``[net] honor_env_proxy`` 默认值)时显式装空 ``ProxyHandler``,
    免得 ``HTTP(S)_PROXY`` 把本机/内网回调也绕出去。
    """

    def __init__(self, *, honor_env_proxy: bool = False, user_agent: str = "qtrade-agent"):
        self.user_agent = user_agent
        handlers = [] if honor_env_proxy else [urllib.request.ProxyHandler({})]
        self._opener = urllib.request.build_opener(*handlers)

    def _post_sync(self, url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> HttpResponse:
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("User-Agent", self.user_agent)
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                return HttpResponse(int(resp.status), resp.read())
        except urllib.error.HTTPError as e:              # 4xx/5xx 也是「投递结果」,不是异常路径
            return HttpResponse(int(e.code), e.read() or b"")

    async def post(self, url: str, *, body: bytes, headers: dict[str, str], timeout_ms: int) -> HttpResponse:
        return await asyncio.to_thread(self._post_sync, url, body, headers, timeout_ms / 1000)


SecretProvider = Callable[[dict[str, Any]], Union[None, str, bytes, Awaitable[Optional[Union[str, bytes]]]]]


# ---------------------------------------------------------------- 投递器
@contextmanager
def _tx(store) -> Iterator[Any]:
    """薄封装:复用 ``Store`` 自己的事务与写锁(文件所有权约束:不往 ``store/`` 里加方法)。"""
    with store._tx() as c:                     # noqa: SLF001 - 见 docstring
        yield c


class WebhookDispatcher:
    """``events_outbox`` → webhook 的投递器(02 §2.2.7)。

    ``secret_provider(webhook_row) -> secret``(可同步可异步):生产实现按 ``secret_ref`` 去 Vault 读。
    ``alerts`` 可选(有则死信时发 ``WEBHOOK_DEAD``)。
    """

    def __init__(self, store, *, http: HttpClient, secret_provider: SecretProvider,
                 cfg: Optional[WebhookConfig] = None, alerts=None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 nonce_fn: Callable[[], str] = lambda: secrets.token_hex(16)):
        self._store = store
        self._http = http
        self._secret_provider = secret_provider
        self.cfg = cfg or WebhookConfig()
        self._alerts = alerts
        self._clock = clock
        self._nonce = nonce_fn
        self._task: Optional[asyncio.Task] = None
        self.delivered = 0
        self.failed = 0
        self.dead = 0

    # -- 薄封装:只读/只写 webhooks 与 events_outbox 两张表(02 §3.1)
    def _webhooks(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM webhooks" + (" WHERE enabled=1" if enabled_only else "")
        return [dict(r) for r in self._store.con.execute(sql + " ORDER BY id")]

    def _webhook(self, wid: str) -> Optional[dict[str, Any]]:
        r = self._store.con.execute("SELECT * FROM webhooks WHERE id=?", (wid,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def _subscribed(raw: Optional[str], value: Optional[str]) -> bool:
        """``events_json`` / ``accounts_json``:``["*"]`` = 不限;事件不带 ``account_id`` 时账号过滤不生效。"""
        try:
            items = json.loads(raw or '["*"]')
        except ValueError:
            items = ["*"]
        if not isinstance(items, list) or "*" in items:
            return True
        if value is None:
            return False
        return value in items

    # ------------------------------------------------------------ 扇出
    def fanout(self, *, event_id: str, event: str, payload: dict[str, Any], account_id: Optional[str] = None,
               channel: Optional[str] = None, trace_id: Optional[str] = None, now_ms: Optional[int] = None,
               ignore_filters: bool = False) -> list[str]:
        """给每个**启用且订阅**的 webhook 各写一行 ``target='webhook:<id>'`` 的 pending outbox(§2.2.7)。

        返回命中的 webhook id 列表。``ignore_filters=True`` = E-3 ``NET_PUBLIC_ENDPOINT_CHANGED``
        的唯一例外:向全部 ``enabled=1`` 的登记方各投一行、不受订阅过滤(§2.2.12)。
        """
        now = now_ms if now_ms is not None else self._clock()
        payload_json = json.dumps(payload, ensure_ascii=False)
        hit: list[str] = []
        for w in self._webhooks():
            if not ignore_filters and not (self._subscribed(w["events_json"], event)
                                           and self._subscribed(w["accounts_json"], account_id)):
                continue
            self._store.insert_outbox_event(event_id=event_id, target=f"{TARGET_PREFIX}{w['id']}", event=event,
                                            trace_id=trace_id, account_id=account_id, channel=channel,
                                            payload_json=payload_json, now_ms=now)
            hit.append(w["id"])
        return hit

    # ------------------------------------------------------------ 投递
    def due_rows(self, *, now_ms: Optional[int] = None, limit: int = 500) -> list[dict[str, Any]]:
        """``status='pending' and next_attempt_ms<=now`` 的 webhook 行,按 ``seq`` 升序(保序的前提)。"""
        now = now_ms if now_ms is not None else self._clock()
        rows = self._store.con.execute(
            "SELECT * FROM events_outbox WHERE status='pending' AND target LIKE 'webhook:%' AND next_attempt_ms <= ? "
            "ORDER BY seq LIMIT ?", (now, limit)).fetchall()
        return [dict(r) for r in rows]

    async def _secret(self, w: dict[str, Any]) -> Optional[bytes]:
        val = self._secret_provider(w)
        if asyncio.isfuture(val) or asyncio.iscoroutine(val):
            val = await val                    # type: ignore[misc]
        if val is None:
            return None
        return val.encode("utf-8") if isinstance(val, str) else bytes(val)

    def body_for(self, row: dict[str, Any]) -> bytes:
        """投递体 = 与 WS 帧同形(§3.4.7):``{event, seq, ts, trace_id, account_id, channel, payload}``。"""
        frame = {"event": row["event"], "seq": row["seq"], "ts": iso8601(row["ts_ms"]), "trace_id": row["trace_id"],
                 "account_id": row["account_id"], "channel": row["channel"], "payload": json.loads(row["payload_json"])}
        return json.dumps(frame, ensure_ascii=False).encode("utf-8")

    def _headers(self, w: dict[str, Any], secret: bytes, body: bytes, now_ms: int) -> dict[str, str]:
        """§3.5 末行:同一套 canonical,头名加前缀 ``X-QT-Webhook-*``。"""
        path, query = split_url(w["url"])
        ts = now_ms // 1000
        nonce = self._nonce()
        canonical = canonical_string("POST", path, query, body, ts, nonce)
        return {
            "Content-Type": "application/json; charset=utf-8",
            "X-QT-Webhook-AppId": w["id"],
            "X-QT-Webhook-Timestamp": str(ts),
            "X-QT-Webhook-Nonce": nonce,
            "X-QT-Webhook-Signature": signature_header(secret, canonical),
        }

    def _max_attempts(self, w: dict[str, Any]) -> int:
        return int(w.get("max_attempts") or self.cfg.webhook_max_attempts)

    def _timeout_ms(self, w: dict[str, Any]) -> int:
        return int(w.get("timeout_ms") or self.cfg.webhook_timeout_ms)

    def _backoff_ms(self, attempts: int) -> int:
        table = self.cfg.webhook_backoff_ms or DEFAULT_BACKOFF_MS
        return int(table[min(attempts, len(table)) - 1])

    def _mark_delivered(self, seq: int, wid: str, now: int) -> None:
        with _tx(self._store) as c:
            c.execute("UPDATE events_outbox SET status='delivered', attempts=attempts+1, delivered_ms=?, last_error=NULL "
                      "WHERE seq=?", (now, seq))
            c.execute("UPDATE webhooks SET consecutive_fail=0, updated_ms=? WHERE id=?", (now, wid))
        self.delivered += 1

    def _mark_failed(self, row: dict[str, Any], w: dict[str, Any], err: str, now: int) -> bool:
        """失败一次:计数 + 退避;到 ``max_attempts`` 进 ``dead``。返回 True = 本次进了死信。"""
        attempts = int(row["attempts"]) + 1
        max_attempts = self._max_attempts(w)
        is_dead = attempts >= max_attempts
        with _tx(self._store) as c:
            if is_dead:
                c.execute("UPDATE events_outbox SET status='dead', attempts=?, last_error=? WHERE seq=?",
                          (attempts, err[:500], row["seq"]))
                # §3.1 webhooks.dead_ms 注:「连续失败超限自动停用时刻」
                c.execute("UPDATE webhooks SET consecutive_fail=consecutive_fail+1, dead_ms=?, enabled=0, updated_ms=? WHERE id=?",
                          (now, now, w["id"]))
            else:
                c.execute("UPDATE events_outbox SET status='pending', attempts=?, next_attempt_ms=?, last_error=? WHERE seq=?",
                          (attempts, now + self._backoff_ms(attempts), err[:500], row["seq"]))
                c.execute("UPDATE webhooks SET consecutive_fail=consecutive_fail+1, updated_ms=? WHERE id=?", (now, w["id"]))
        self.failed += 1
        if is_dead:
            self.dead += 1
            if self._alerts is not None:
                self._alerts.firing(WEBHOOK_DEAD, subject="host", severity="warn",
                                    evidence={"webhook_id": w["id"], "url": w["url"], "attempts": attempts,
                                              "last_error": err[:200], "event_id": row["event_id"]})
        return is_dead

    async def _deliver_one(self, row: dict[str, Any], w: dict[str, Any]) -> bool:
        now = self._clock()
        body = self.body_for(row)
        secret = await self._secret(w)
        if not secret:
            self._mark_failed(row, w, "secret_unavailable", now)
            return False
        headers = self._headers(w, secret, body, now)
        try:
            resp = await self._http.post(w["url"], body=body, headers=headers, timeout_ms=self._timeout_ms(w))
        except Exception as e:                  # 连不上/超时/协议错 —— 都是一次失败,进退避
            self._mark_failed(row, w, repr(e), self._clock())
            return False
        if 200 <= resp.status < 300:
            self._mark_delivered(int(row["seq"]), w["id"], self._clock())
            return True
        self._mark_failed(row, w, f"http_{resp.status}", self._clock())
        return False

    async def _deliver_serial(self, wid: str, rows: list[dict[str, Any]]) -> None:
        """同一 webhook 串行投递、保序:任一行失败即停,剩下的留给下一轮(否则会乱序)。"""
        w = self._webhook(wid)
        if w is None:
            return
        for row in rows:
            if not await self._deliver_one(row, w):
                return

    async def deliver_due(self, *, now_ms: Optional[int] = None, limit: int = 500) -> dict[str, int]:
        """跑一轮:取到期行 → 按 webhook 分组 → 组间并行、组内串行保序。返回本轮计数。"""
        before = (self.delivered, self.failed, self.dead)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self.due_rows(now_ms=now_ms, limit=limit):
            groups.setdefault(str(row["target"])[len(TARGET_PREFIX):], []).append(row)
        if groups:
            await asyncio.gather(*(self._deliver_serial(wid, rows) for wid, rows in groups.items()))
        return {"delivered": self.delivered - before[0], "failed": self.failed - before[1], "dead": self.dead - before[2]}

    async def tick(self) -> None:
        """scheduler 注册用的一轮(``register('webhook_dispatch', 0.5, dispatcher.tick)``)。"""
        await self.deliver_due()

    async def run_forever(self) -> None:
        """§2.2.7「投递器一个 task 轮询…每 500ms」;由 ``app`` 起停。"""
        while True:
            try:
                await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception as e:              # 单轮异常不杀投递器
                log.exception("webhook 投递轮异常: %s", e)
            await asyncio.sleep(self.cfg.poll_interval_ms / 1000)

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="webhook-dispatch")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
