"""WinAgent 客户端(02 §2.5 调用契约 / §3.6 端点 #1 ping、#2 health、#4 time、#7~#12 vault)。

- 方向只有 Agent → WinAgent(C-03);Bearer = ``/etc/qtrade/winagent.token``(0600,唯一允许落盘的密钥,C-05)。
- 地址:``[winagent] url`` 非空用它;空 = 自动:先 ``host_ip_hint_file``(``/run/qtrade/host.json``)、再 ``/etc/resolv.conf`` nameserver(默认网关),端口 17610。
- 超时(§2.5 表):ping/health 2 s;vault 3 s;time/metrics/alerts/net 3 s;通用 ``[winagent] timeout_ms``。
- 重试:只读类 1 次;写类(vault put/delete、wechat send、wsl 启停)**不重试**。
- 每个响应带 ``X-WA-Version``;主版本不一致只告警不拒绝(§2.5「版本」)。
- 传输层可注入(``transport(method, url, headers, body, timeout_s) -> (status, headers, body_bytes)``),默认 ``urllib``(标准库,放线程池)。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .config import WinAgentConfig

WA_PORT = 17610
Transport = Callable[[str, str, dict[str, str], Optional[bytes], float], Awaitable[tuple[int, dict[str, str], bytes]]]

TIMEOUT_S = {"ping": 2.0, "health": 2.0, "time": 3.0, "vault": 3.0, "metrics": 3.0, "alerts": 3.0, "net": 3.0, "wsl": 30.0}


class WinAgentUnavailable(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class WaTime:
    now_ms: int
    tz_offset_min: int
    last_resume_ms: Optional[int]
    w32time: dict[str, Any]
    rtt_ms: int


def read_token(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        return t or None
    except OSError:
        return None


def default_gateway(*, route_file: str = "/proc/net/route") -> Optional[str]:
    """04 §2.6.3「主机 IP 的发现」的结论口径:**eth0 默认网关**(`ip -4 route show default` 的 `via`)。

    这里读 ``/proc/net/route``(`ip` 命令的同一份内核数据,不 fork 子进程、容器里也一定在):
    取 ``Destination==00000000`` 的那一行,``Gateway`` 列是**小端 hex 的 IPv4**。
    路由表里有多条默认路由时按 ``Metric`` 最小的那条(与 `ip route show default` 的选路一致)。

    可注入:调用方传 ``route_file``(测试用临时文件),或直接给 ``resolve_base_url(gateway=…)`` 传一个函数。
    """
    best: Optional[tuple[int, str]] = None
    try:
        with open(route_file, encoding="utf-8") as f:
            next(f, None)                                   # 表头
            for line in f:
                cols = line.split()
                if len(cols) < 8 or cols[1] != "00000000" or cols[2] == "00000000":
                    continue
                try:
                    raw = int(cols[2], 16)
                    metric = int(cols[6])
                except ValueError:
                    continue
                ip = ".".join(str((raw >> (8 * i)) & 0xFF) for i in range(4))   # 小端 hex → 点分十进制
                if best is None or metric < best[0]:
                    best = (metric, ip)
    except OSError:
        return None
    return best[1] if best else None


def _resolv_nameserver(resolv_conf: str) -> Optional[str]:
    try:
        with open(resolv_conf, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*nameserver\s+(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def _host_hint(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            hint = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(hint, dict):
        return None
    if hint.get("winagent_base_url"):
        return str(hint["winagent_base_url"]).rstrip("/")
    if hint.get("host_ip"):
        return f"http://{hint['host_ip']}:{WA_PORT}"
    return None


def base_url_candidates(cfg: WinAgentConfig, *, resolv_conf: str = "/etc/resolv.conf",
                        gateway: Optional[Callable[[], Optional[str]]] = None) -> list[tuple[str, str]]:
    """按优先级给出 ``[(来源, base_url), …]``(去重、保序)。来源 ∈ ``url|host_json|gateway|resolv_conf``。

    🔴 顺序依据 **04 §2.6.3**(网络册是这条结论的 owner)与 **02 §7.1 `[winagent] url` 注**(「空=自动:先
    `host_ip_hint_file`,再默认网关」),即 ``url → host.json → 默认网关 → resolv.conf``。
    04 §2.6.3 逐条论证了 ``resolv.conf`` 的 ``nameserver`` **不可靠**(①公司 VPN 改写 Windows DNS;
    ②§2.6.5 的 DNS 对策会主动 `generateResolvConf=false` 并写公司 DNS),故它**降为最后兜底**。
    (02 §2.5 的表格把两者并列写成「resolv.conf nameserver 或 ip route」且未排序,见 rulings R6-58 (bp)。)
    """
    out: list[tuple[str, str]] = []

    def add(source: str, url: Optional[str]) -> None:
        if url and url not in {u for _s, u in out}:
            out.append((source, url))

    if cfg.url:
        return [("url", cfg.url.rstrip("/"))]                # 显式配置就是唯一答案,不再自动发现
    add("host_json", _host_hint(cfg.host_ip_hint_file))
    gw = (gateway or default_gateway)()
    add("gateway", f"http://{gw}:{WA_PORT}" if gw else None)
    ns = _resolv_nameserver(resolv_conf)
    add("resolv_conf", f"http://{ns}:{WA_PORT}" if ns else None)
    return out


def resolve_base_url(cfg: WinAgentConfig, *, resolv_conf: str = "/etc/resolv.conf",
                     gateway: Optional[Callable[[], Optional[str]]] = None) -> Optional[str]:
    """回退链首选项:``url`` → ``host.json`` → **默认网关** → ``resolv.conf`` nameserver(04 §2.6.3)。

    「`host.json` 与默认网关不一致时以**能 ping 通 `/wa/v1/ping` 的那个**为准并记 `warn`」这一步是异步的,
    在 :meth:`WinAgentClient.discover` 里做(本函数只给同步的首选项,保持纯函数可测)。
    """
    c = base_url_candidates(cfg, resolv_conf=resolv_conf, gateway=gateway)
    return c[0][1] if c else None


async def urllib_transport(method: str, url: str, headers: dict[str, str], body: Optional[bytes], timeout_s: float) -> tuple[int, dict[str, str], bytes]:
    def go() -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:      # noqa: S310(内网固定地址)
                return resp.status, {k: v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, {k: v for k, v in e.headers.items()}, e.read()
    return await asyncio.to_thread(go)


class WinAgentClient:
    def __init__(self, cfg: WinAgentConfig, *, transport: Optional[Transport] = None, base_url: Optional[str] = None,
                 token: Optional[str] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.cfg = cfg
        self._transport = transport or urllib_transport
        self._base_url = base_url
        self._token = token
        self._clock = clock
        self.last_version: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def base_url(self) -> Optional[str]:
        if self._base_url is None:
            self._base_url = resolve_base_url(self.cfg)
        return self._base_url

    def token(self) -> Optional[str]:
        if self._token is None:
            self._token = read_token(self.cfg.token_file)
        return self._token

    async def request(self, method: str, path: str, *, json: Optional[dict[str, Any]] = None, timeout_s: Optional[float] = None,
                      retry: bool = False, auth: bool = True, headers: Optional[dict[str, str]] = None,
                      raw: bool = False) -> Any:
        """``raw=False``(缺省)回 ``(status, parsed_json | None)``;``raw=True`` 回 ``(status, headers, body_bytes)``。

        ``raw=True`` 供二进制端点(#40 ``wechat/media``、#42 ``wechat/screenshot``)用 —— 它们的响应体不是 JSON,
        解析会丢字节。其余语义(地址/令牌/超时/重试/版本头)两种模式完全一致。"""
        base = self.base_url
        if not base:
            raise WinAgentUnavailable("no_address")
        hdrs = {"Accept": "application/json", **(headers or {})}
        body: Optional[bytes] = None
        if json is not None:
            body = _dumps(json).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if auth:
            tok = self.token()
            if not tok:
                raise WinAgentUnavailable("no_token", self.cfg.token_file)
            hdrs["Authorization"] = f"Bearer {tok}"
        t = timeout_s if timeout_s is not None else self.cfg.timeout_ms / 1000
        attempts = 2 if retry else 1            # 只读类 1 次重试;写类不重试(02 §2.5)
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                status, rh, body_bytes = await asyncio.wait_for(self._transport(method, base + path, hdrs, body, t), timeout=t + 0.5)
            except (asyncio.TimeoutError, OSError, urllib.error.URLError) as e:
                last = e
                continue
            ver = rh.get("X-WA-Version") or rh.get("x-wa-version")
            if ver:
                self.last_version = ver
            self.last_error = None
            if raw:
                return status, rh, body_bytes            # 二进制端点:原样回字节,不做 JSON 解析
            parsed: Optional[dict[str, Any]] = None
            if body_bytes:
                try:
                    parsed = _loads(body_bytes.decode("utf-8"))
                except ValueError:
                    parsed = None
            return status, parsed
        self.last_error = repr(last)
        raise WinAgentUnavailable("unreachable", repr(last))

    # ---- #1 ping(无鉴权,2 s)
    async def ping(self) -> Optional[dict[str, Any]]:
        try:
            status, body = await self.request("GET", "/wa/v1/ping", timeout_s=TIMEOUT_S["ping"], retry=True, auth=False)
        except WinAgentUnavailable:
            return None
        return body if status == 200 else None

    # ---- #2 health(A 令牌,2 s)
    async def health(self) -> Optional[dict[str, Any]]:
        try:
            status, body = await self.request("GET", "/wa/v1/health", timeout_s=TIMEOUT_S["health"], retry=True)
        except WinAgentUnavailable:
            return None
        return body if status == 200 else None

    # ---- #4 time(A 令牌,3 s):{now_ms, tz_offset_min, last_resume_ms, w32time}
    async def time(self) -> WaTime:
        t0 = self._clock()
        status, body = await self.request("GET", "/wa/v1/time", timeout_s=TIMEOUT_S["time"], retry=True)
        rtt = max(0, self._clock() - t0)
        if status != 200 or not body or "now_ms" not in body:
            raise WinAgentUnavailable("bad_time_response", f"status={status}")
        return WaTime(now_ms=int(body["now_ms"]), tz_offset_min=int(body.get("tz_offset_min") or 0),
                      last_resume_ms=int(body["last_resume_ms"]) if body.get("last_resume_ms") is not None else None,
                      w32time=dict(body.get("w32time") or {}), rtt_ms=rtt)


def _dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)


def _loads(s: str) -> Any:
    return json.loads(s)


class FakeWinAgent:
    """可编程 WinAgent(传输层级假实现):``routes[(method, path)] = handler(headers, body) -> (status, headers, body_dict)``;
    ``offline=True`` 抛 OSError;``calls`` 记录每次请求;``delay_s`` 模拟慢响应(触发超时)。"""

    def __init__(self, *, token: str = "wa-token", version: str = "1.0.0"):
        self.token = token
        self.version = version
        self.offline = False
        self.delay_s = 0.0
        self.calls: list[tuple[str, str, dict[str, str], Optional[bytes]]] = []
        self.now_ms: Optional[int] = None            # None = 用真实时钟
        self.last_resume_ms: Optional[int] = None
        self.user_agent = True
        self.wechat_enabled = False
        self.host = {"total_mb": 16384, "available_mb": 6100, "wsl_vm_mb": 11264, "wechat_mb": 0, "chatlog_mb": 0}
        self.vault: dict[str, dict[str, Any]] = {}
        self.fail_next: int = 0                      # 接下来 N 次请求直接 OSError(测重试)

    async def __call__(self, method: str, url: str, headers: dict[str, str], body: Optional[bytes], timeout_s: float):
        path = url.split("://", 1)[-1].split("/", 1)[1] if "://" in url else url
        path = "/" + path
        self.calls.append((method, path, dict(headers), body))
        if self.offline:
            raise OSError("connection refused")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError("transient")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        rh = {"X-WA-Version": self.version, "Content-Type": "application/json"}
        if path != "/wa/v1/ping" and headers.get("Authorization") != f"Bearer {self.token}":
            return 401, rh, b'{"ok":false,"code":"UNAUTHORIZED"}'
        data = json.loads(body.decode()) if body else {}
        if path == "/wa/v1/ping":
            return 200, rh, _dumps({"agent_id": "fake", "version": self.version, "time": self._now(), "listen": ["127.0.0.1"]}).encode()
        if path == "/wa/v1/health":
            return 200, rh, _dumps({"ok": True, "version": self.version, "api_version": "1.0", "uptime_s": 10, "user_agent": self.user_agent,
                                    "modules": {"vault": "ok", "monitor": "ok", "wechat": "enabled" if self.wechat_enabled else "disabled"},
                                    "checks": {}, "host": self.host}).encode()
        if path == "/wa/v1/time":
            return 200, rh, _dumps({"now_ms": self._now(), "tz_offset_min": 480, "last_resume_ms": self.last_resume_ms,
                                    "w32time": {"source": "time.windows.com", "last_sync_ms": self._now() - 3600_000}}).encode()
        m = re.match(r"^/wa/v1/vault/(.+?)(/read|/flag)?$", path)
        if m:
            name, op = m.group(1), m.group(2)
            if op == "/read" and method == "POST":
                if "X-Trace-Id" not in headers:
                    return 400, rh, b'{"ok":false,"code":"INVALID_ARGS"}'
                e = self.vault.get(name)
                if e is None:
                    return 404, rh, b"{}"
                e["read_count"] = e.get("read_count", 0) + 1
                return 200, rh, _dumps({"value": e["value"]}).encode()
            if op == "/flag" and method == "POST":
                if name in self.vault:
                    self.vault[name]["suspect"] = bool(data.get("suspect"))
                return 200, rh, b"{}"
            if method == "PUT":
                old = self.vault.get(name)
                self.vault[name] = {"value": data["value"], "scope": data.get("scope"), "version": (old["version"] + 1) if old else 1}
                return 204, rh, b""
            if method == "DELETE":
                self.vault.pop(name, None)
                return 204, rh, b""
            if method == "HEAD":
                return (200 if name in self.vault else 404), rh, b""
        return 404, rh, b'{"ok":false,"code":"TARGET_NOT_FOUND"}'

    def _now(self) -> int:
        return self.now_ms if self.now_ms is not None else int(time.time() * 1000)
