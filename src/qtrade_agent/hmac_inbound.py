"""公网入站 HMAC 校验(02 §3.5 公网入站 HMAC 规范 D.3)。

本模块是 **canonical string 与签名算法的单一出处** —— webhook 出站(``webhook.py``)按 §3.5 末行
「同一套 canonical、头名同上加前缀 ``X-QT-Webhook-*``」复用这里的 :func:`canonical_string` / :func:`sign`。

§3.5 逐条落地:

- **Header**:``X-QT-AppId``、``X-QT-Timestamp``(秒级 epoch,整数)、``X-QT-Nonce``(16~64 字符随机串)、
  ``X-QT-Signature``(``v1=`` + 小写 hex);同时仍可带 ``Authorization: Bearer``,但 HMAC 客户端不需要。
- **canonical string**:``METHOD \\n PATH \\n CANONICAL_QUERY \\n sha256hex(BODY 或空串) \\n TIMESTAMP \\n NONCE``
  (QUERY 按 key 字典序、值 RFC3986 编码、``k=v`` 用 ``&`` 连;无 query 为空串)。
- **签名**:``hex(HMAC-SHA256(secret, canonical))``;secret 由 #91 一次性下发,服务端存 Vault
  (``api_clients.secret_ref``),``secret_hash`` 只用于轮换比对 —— 故本模块**不读 ``secret_hash`` 验签**,
  明文一律经注入的 ``secret_provider``(生产 = ``vault_client``)取。
- **时钟容差**:``|now - TIMESTAMP| <= 300s``,否则 ``401 UNAUTHORIZED``、``error.message="timestamp skew"``
  (调用方把服务端时间放进 ``Date`` 头让对方校时 —— 见 :attr:`HmacResult.server_date`)。
- **nonce**:``(app_id, nonce)`` 在内存 LRU 里 10 分钟内唯一;重复 → ``401``、``error.message="nonce replay"``。
  Agent 重启后 LRU 清空(§3.5 明写「代价接受」,不落库)。
- **失败顺序**(逐字):app_id 不存在/禁用 → 401;IP 不在白名单 → 403;时间戳 → 401;nonce → 401;
  签名 → 401;限流 → 429;权限级别/allow_ops/allow_accounts → 403。
- **不校验自己的出口 IP**、不把 IP 写进 canonical(E-3:我方公网 IP 变了签名照样有效)。

``allow_ops`` 的 ``["*"]`` 按 00 §11.17 ② ``expand_allow_ops()`` 解释:星号**只展开 ``danger=false``**,
``danger=true`` 的 op 须逐条列名才放行(R2-1);危险项清单来自能力目录 ``capabilities/*.json`` 的 ``danger`` 字段。

本模块**不改 ``api/``**:对外给纯函数 :func:`verify_request` / 类 :class:`HmacVerifier`,以及
:func:`fastapi_dependency`(鸭子类型接 ``Request``,不 import fastapi),由总控在 ``api/app.py`` 挂上去。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional, Union
from urllib.parse import quote, urlsplit

from .api.auth import ApiError, Principal, principal_from_row

# ---------------------------------------------------------------- 头名(§3.5 Header 行)
H_APP_ID = "x-qt-appid"
H_TIMESTAMP = "x-qt-timestamp"
H_NONCE = "x-qt-nonce"
H_SIGNATURE = "x-qt-signature"
HMAC_HEADERS = (H_APP_ID, H_TIMESTAMP, H_NONCE, H_SIGNATURE)

SIG_PREFIX = "v1="                      # §3.5:`v1=` + 小写 hex
NONCE_MIN_LEN, NONCE_MAX_LEN = 16, 64   # §3.5:16~64 字符随机串

CAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capabilities")


@dataclass(frozen=True)
class HmacConfig:
    """02 §7.1 ``[api]`` 里与 HMAC 有关的三键(逐字默认值)。"""
    hmac_clock_skew_s: int = 300
    nonce_ttl_s: int = 600
    rate_default_per_min: int = 120


# ---------------------------------------------------------------- canonical string(§3.5,webhook 共用)
def sha256_hex(body: Optional[Union[bytes, str]]) -> str:
    """``sha256hex(BODY 或空串)``:无 body 时算空串的 sha256(不是空串本身)。"""
    if body is None:
        body = b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _rfc3986(s: str) -> str:
    return quote(s, safe="-._~")


def canonical_query(query: Optional[Union[str, Iterable[tuple[str, str]]]]) -> str:
    """按 key 字典序、值 RFC3986 编码、``k=v`` 用 ``&`` 连;无 query 为空串(§3.5)。

    不用 ``parse_qs``——它会丢掉重复 key 的顺序信息且做 ``+`` → 空格 的表单解码;这里按 ``&``/``=`` 原样拆。
    """
    if not query:
        return ""
    if isinstance(query, str):
        raw = query[1:] if query.startswith("?") else query
        pairs: list[tuple[str, str]] = []
        for part in raw.split("&"):
            if not part:
                continue
            k, sep, v = part.partition("=")
            pairs.append((k, v if sep else ""))
    else:
        pairs = [(str(k), "" if v is None else str(v)) for k, v in query]
    if not pairs:
        return ""
    return "&".join(f"{_rfc3986(k)}={_rfc3986(v)}" for k, v in sorted(pairs, key=lambda kv: (kv[0], kv[1])))


def canonical_string(method: str, path: str, query: Optional[Union[str, Iterable[tuple[str, str]]]],
                     body: Optional[Union[bytes, str]], timestamp: Union[int, str], nonce: str) -> str:
    """``METHOD \\n PATH \\n CANONICAL_QUERY \\n sha256hex(BODY) \\n TIMESTAMP \\n NONCE``(§3.5)。"""
    return "\n".join([method.upper(), path, canonical_query(query), sha256_hex(body), str(timestamp), nonce])


def sign(secret: Union[bytes, str], canonical: str) -> str:
    """``hex(HMAC-SHA256(secret, canonical))``,小写 hex(不带 ``v1=`` 前缀)。"""
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def signature_header(secret: Union[bytes, str], canonical: str) -> str:
    """``X-QT-Signature`` 的值:``v1=`` + 小写 hex。"""
    return SIG_PREFIX + sign(secret, canonical)


def signatures_equal(expect_hex: str, got_header: str) -> bool:
    """常量时间比较;``got_header`` 允许带或不带 ``v1=`` 前缀,大小写不敏感(hex)。"""
    got = got_header[len(SIG_PREFIX):] if got_header.startswith(SIG_PREFIX) else got_header
    return hmac.compare_digest(expect_hex.lower(), got.strip().lower())


# ---------------------------------------------------------------- nonce LRU / 限流
class NonceCache:
    """``(app_id, nonce)`` 10 分钟内唯一(§3.5);内存 LRU,Agent 重启即清空(规格明写代价接受)。"""

    def __init__(self, *, ttl_s: int = 600, max_entries: int = 100_000):
        self.ttl_ms = ttl_s * 1000
        self.max_entries = max_entries
        self._seen: OrderedDict[tuple[str, str], int] = OrderedDict()

    def _evict(self, now_ms: int) -> None:
        while self._seen:
            key, expires = next(iter(self._seen.items()))
            if expires > now_ms and len(self._seen) <= self.max_entries:
                break
            self._seen.popitem(last=False)

    def check_and_put(self, app_id: str, nonce: str, now_ms: int) -> bool:
        """首见返回 True 并登记;10 分钟内重复返回 False。"""
        self._evict(now_ms)
        key = (app_id, nonce)
        expires = self._seen.get(key)
        if expires is not None and expires > now_ms:
            return False
        self._seen[key] = now_ms + self.ttl_ms
        self._seen.move_to_end(key)
        return True


class RateLimiter:
    """每 app 每分钟上限(``api_clients.rate_per_min``,缺省 ``[api] rate_default_per_min``);滑动 60 s 窗。"""

    def __init__(self):
        self._hits: dict[str, deque[int]] = {}

    def allow(self, app_id: str, per_min: int, now_ms: int) -> bool:
        if per_min <= 0:
            return True
        q = self._hits.setdefault(app_id, deque())
        floor = now_ms - 60_000
        while q and q[0] <= floor:
            q.popleft()
        if len(q) >= per_min:
            return False
        q.append(now_ms)
        return True


# ---------------------------------------------------------------- allow_ops(00 §11.17 ②)
def load_danger_ops(caps_dir: str = CAPS_DIR) -> frozenset[str]:
    """能力目录里 ``danger=true`` 的 op 集合(02 §3.10;00 §11.17 ① 十项)。"""
    ops: set[str] = set()
    if not os.path.isdir(caps_dir):
        return frozenset()
    for name in sorted(os.listdir(caps_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(caps_dir, name), encoding="utf-8") as f:
            item = json.load(f)
        if item.get("danger"):
            ops.add(item["op"])
    return frozenset(ops)


def expand_allow_ops(allow_ops: Iterable[str], all_ops: Iterable[str], danger_ops: Iterable[str]) -> set[str]:
    """00 §11.17 ②:``"*"`` 只展开 ``danger=false``;``danger=true`` 的 op 须逐条列名才在集合里(R2-1)。"""
    danger = set(danger_ops)
    out: set[str] = set()
    for item in allow_ops:
        if item == "*":
            out |= {op for op in all_ops if op not in danger}
        else:
            out.add(item)
    return out


def op_allowed(allow_ops: Iterable[str], op: str, danger_ops: Iterable[str]) -> bool:
    """只判单个 op(不需要完整目录):星号对 ``danger=true`` 无效,必须逐条列名。"""
    danger = set(danger_ops)
    for item in allow_ops:
        if item == op:
            return True
        if item == "*" and op not in danger:
            return True
    return False


# ---------------------------------------------------------------- 校验主体
SecretProvider = Callable[[dict[str, Any]], Union[None, str, bytes, Awaitable[Optional[Union[str, bytes]]]]]


@dataclass
class HmacResult:
    """校验通过的结果:``principal`` 给下游做级别/账号判定,``server_date`` 供调用方回 ``Date`` 头。"""
    principal: Principal
    app_id: str
    row: dict[str, Any]
    server_date: str = ""
    nonce: str = ""
    timestamp: int = 0


def has_hmac_headers(headers: dict[str, str]) -> bool:
    low = {str(k).lower() for k in headers}
    return all(h in low for h in HMAC_HEADERS)


def _unauthorized(message: str, reason: str) -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message, reason=reason, needs_human=True)


class HmacVerifier:
    """公网入站 HMAC 校验器(02 §3.5)。

    ``secret_provider(api_client_row) -> secret``(可同步可异步):生产实现去 Vault 读 ``secret_ref``;
    测试注入返回字面量即可。``store`` 只读 ``api_clients`` 一张表(薄封装,不新增 ``store`` 方法)。
    """

    def __init__(self, store, *, secret_provider: SecretProvider, cfg: Optional[HmacConfig] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 danger_ops: Optional[Iterable[str]] = None):
        self._store = store
        self._secret_provider = secret_provider
        self.cfg = cfg or HmacConfig()
        self._clock = clock
        self.danger_ops = frozenset(danger_ops) if danger_ops is not None else load_danger_ops()
        self.nonces = NonceCache(ttl_s=self.cfg.nonce_ttl_s)
        self.rates = RateLimiter()

    # -- 薄封装:只读 api_clients 一行(02 §3.1)
    def _client(self, app_id: str) -> Optional[dict[str, Any]]:
        row = self._store.con.execute("SELECT * FROM api_clients WHERE app_id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _json_list(raw: Optional[str], default: list[str]) -> list[str]:
        if not raw:
            return list(default)
        try:
            val = json.loads(raw)
        except ValueError:
            return list(default)
        return list(val) if isinstance(val, list) else list(default)

    @staticmethod
    def _ip_allowed(client_ip: Optional[str], cidrs: list[str]) -> bool:
        if not cidrs:                       # 空 = 不限(02 §3.1 ip_allow_json 注)
            return True
        if not client_ip:
            return False
        try:
            ip = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        for c in cidrs:
            try:
                if ip in ipaddress.ip_network(c, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def _server_date(self, now_ms: int) -> str:
        """RFC 7231 ``Date`` 头(§3.5「把服务端时间放在 Date 头让对方校时」)。"""
        return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(now_ms / 1000))

    async def _secret(self, row: dict[str, Any]) -> Optional[bytes]:
        val = self._secret_provider(row)
        if asyncio.isfuture(val) or asyncio.iscoroutine(val):
            val = await val                          # type: ignore[misc]
        if val is None:
            return None
        return val.encode("utf-8") if isinstance(val, str) else bytes(val)

    async def verify(self, *, method: str, path: str, query: Optional[Union[str, Iterable[tuple[str, str]]]] = None,
                     body: Optional[Union[bytes, str]] = None, headers: Optional[dict[str, str]] = None,
                     client_ip: Optional[str] = None, op: Optional[str] = None,
                     account_id: Optional[str] = None, required_level: Optional[str] = None) -> HmacResult:
        """按 §3.5「失败顺序」逐级校验;全过返回 :class:`HmacResult`,否则抛 ``ApiError``。"""
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        now_ms = self._clock()
        date = self._server_date(now_ms)

        app_id = (h.get(H_APP_ID) or "").strip()
        # ① app_id 不存在/禁用 → 401
        row = self._client(app_id) if app_id else None
        if row is None or not row.get("enabled") or row.get("revoked_ms") is not None:
            raise _unauthorized("app_id 不存在或已禁用", "app_unknown")
        if row.get("auth_kind") != "hmac":
            raise _unauthorized("该调用方不是 HMAC 类型", "auth_kind_mismatch")

        # ② IP 不在白名单 → 403
        if not self._ip_allowed(client_ip, self._json_list(row.get("ip_allow_json"), [])):
            raise ApiError(403, "FORBIDDEN", "来源 IP 不在白名单", reason="ip_not_allowed")

        # ③ 时间戳 → 401(error.message 逐字 "timestamp skew")
        raw_ts = (h.get(H_TIMESTAMP) or "").strip()
        try:
            ts = int(raw_ts)
        except ValueError:
            raise _unauthorized("timestamp skew", "timestamp_invalid")
        if abs(now_ms // 1000 - ts) > self.cfg.hmac_clock_skew_s:
            raise _unauthorized("timestamp skew", "timestamp_skew")

        # ④ nonce → 401(逐字 "nonce replay")
        nonce = (h.get(H_NONCE) or "").strip()
        if not (NONCE_MIN_LEN <= len(nonce) <= NONCE_MAX_LEN):
            raise _unauthorized("nonce replay", "nonce_invalid")
        if not self.nonces.check_and_put(app_id, nonce, now_ms):
            raise _unauthorized("nonce replay", "nonce_replay")

        # ⑤ 签名 → 401
        secret = await self._secret(row)
        if not secret:
            raise _unauthorized("签名校验失败", "secret_unavailable")
        expect = sign(secret, canonical_string(method, path, query, body, ts, nonce))
        if not signatures_equal(expect, h.get(H_SIGNATURE) or ""):
            raise _unauthorized("签名校验失败", "bad_signature")

        # ⑥ 限流 → 429
        per_min = int(row.get("rate_per_min") or self.cfg.rate_default_per_min)
        if not self.rates.allow(app_id, per_min, now_ms):
            raise ApiError(429, "RATE_LIMITED", f"超过每分钟 {per_min} 次上限", reason="rate_limited", retryable=True)

        # ⑦ 权限级别 / allow_ops / allow_accounts → 403
        principal = principal_from_row(row, transport="http")
        if required_level is not None and not principal.can(required_level):
            raise ApiError(403, "FORBIDDEN", f"当前令牌级别 {principal.level} 无权调用(需 {required_level})",
                           reason="level_insufficient")
        if op is not None and not op_allowed(self._json_list(row.get("allow_ops_json"), ["*"]), op, self.danger_ops):
            raise ApiError(403, "FORBIDDEN", f"能力 {op} 不在该调用方的 allow_ops 内", reason="op_not_allowed")
        if account_id is not None and not principal.allows_account(account_id):
            raise ApiError(403, "FORBIDDEN", f"当前令牌无权访问账号 {account_id}", reason="account_not_allowed")

        return HmacResult(principal=principal, app_id=app_id, row=row, server_date=date, nonce=nonce, timestamp=ts)


def fastapi_dependency(verifier: HmacVerifier, *, required_level: Optional[str] = None):
    """把 :class:`HmacVerifier` 包成 FastAPI 依赖(``Depends(fastapi_dependency(v))``)。

    鸭子类型接 ``Request``(不 import fastapi,免得本模块把 web 框架拖进来);总控在 ``api/app.py`` 挂载。
    ``ApiError`` 由 ``api/app.py`` 既有的异常处理器转成 00 §10 错误信封。
    """
    async def dependency(request) -> Principal:            # pragma: no cover - 由 api/ 接线后覆盖
        body = await request.body()
        url = request.url
        res = await verifier.verify(method=request.method, path=url.path, query=url.query, body=body,
                                    headers=dict(request.headers),
                                    client_ip=(request.client.host if request.client else None),
                                    required_level=required_level)
        return res.principal
    return dependency


def verify_request(verifier: HmacVerifier, **kw) -> Awaitable[HmacResult]:
    """函数式入口(等价 ``verifier.verify(**kw)``),给不想持有对象的调用方。"""
    return verifier.verify(**kw)


def split_url(url: str) -> tuple[str, str]:
    """``https://h/p?a=1`` → ``("/p", "a=1")``:webhook 出站签名要用 path 与 query(§3.5 末行)。"""
    parts = urlsplit(url)
    return (parts.path or "/"), parts.query
