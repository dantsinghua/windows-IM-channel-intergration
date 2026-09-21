"""WinAgent 客户端(02 §2.5 调用契约 / §3.6 端点 #1 ping、#2 health、#4 time、#7~#12 vault)。

- 方向只有 Agent → WinAgent(C-03);Bearer = ``/etc/qtrade/winagent.token``(0600,唯一允许落盘的密钥,C-05)。
- 地址(04 §2.6.3 结论):``[winagent] url`` 非空用它;空 = 自动,回退链 ``host_ip_hint_file``(``/run/qtrade/host.json``)
  → **eth0 默认网关** → ``/etc/resolv.conf`` nameserver(**最后兜底**:VPN/DNS 对策下它不是主机地址),端口 17610;
  首选项与实际可达不一致时由 :meth:`WinAgentClient.discover` 按 ``/wa/v1/ping`` 择优并记 ``warn``。
- 超时(§2.5 表):ping/health 2 s;vault 3 s;time/metrics/alerts/net 3 s;通用 ``[winagent] timeout_ms``。
- 重试:只读类 1 次;写类(vault put/delete、wechat send、wsl 启停)**不重试**。
- 每个响应带 ``X-WA-Version``;主版本不一致只告警不拒绝(§2.5「版本」)。
- 传输层可注入(``transport(method, url, headers, body, timeout_s) -> (status, headers, body_bytes)``),默认 ``urllib``(标准库,放线程池)。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .config import WinAgentConfig

log = logging.getLogger("qtrade.winagent")

WA_PORT = 17610
Transport = Callable[[str, str, dict[str, str], Optional[bytes], float], Awaitable[tuple[int, dict[str, str], bytes]]]

VAULT_MAX_VALUE_BYTES = 4096          # 05 §2.2.2:Vault 单条明文上限 4 KB
#: 02 §3.6 #11:`POST /wa/v1/vault/<name>/read` 只接受 loopback 与 WSL 子网(WSL2 NAT 默认 172.16/12)
VAULT_READ_SOURCES = ("127.0.0.1", "::1", "172.16.0.0/12")

TIMEOUT_S = {"ping": 2.0, "health": 2.0, "time": 3.0, "vault": 3.0, "metrics": 3.0, "alerts": 3.0, "net": 3.0, "wsl": 30.0, "probes": 3.0}


def _vault_read_source_allowed(client_ip: Optional[str], sources: tuple[str, ...] = VAULT_READ_SOURCES) -> bool:
    """02 §3.6 #11 的来源白名单判定(loopback / WSL 子网);``client_ip=None`` 视作本机直连。"""
    import ipaddress
    if not client_ip:
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for s in sources:
        try:
            if ip in ipaddress.ip_network(s, strict=False):
                return True
        except ValueError:
            continue
    return False


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


#: 自动发现的候选只许落在这些本机/内网段(e2e-rootfs-2 O-2):WinAgent 只监听 ``127.0.0.1`` 与 vEthernet (WSL) 的 IPv4
#: (04 §2.6.3「监听集合」;WSL 子网取不到时退化 ``172.16.0.0/12``)。请求带 ``Authorization: Bearer`` 且是明文 HTTP,
#: 候选若是公网地址(如被改写的 resolv.conf 里的公网 DNS)就等于把 WinAgent 令牌发到公网。
_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",   # 回环 / RFC1918 / 链路本地
    "::1/128", "fe80::/10"))
_warned_rejected: set[tuple[str, str]] = set()


def _is_local_url(url: str) -> bool:
    """``url`` 的主机是 IP 字面量且落在 :data:`_LOCAL_NETS` 里才算数;主机名一律不收(不为此做 DNS 解析)。"""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        ip = ipaddress.ip_address(parts.hostname.split("%", 1)[0])
    except ValueError:
        return False
    return any(ip in n for n in _LOCAL_NETS)


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
        if not url:
            return
        if not _is_local_url(url):                           # O-2:非回环/私网/链路本地 ⇒ 丢弃,不探活、不发令牌
            host = urllib.parse.urlsplit(url).hostname or "?"
            if (source, host) not in _warned_rejected:
                _warned_rejected.add((source, host))
                log.warning("WinAgent 地址发现:丢弃候选 %s(来源 %s)——不在回环/私网/链路本地段,"
                            "不会向它发送任何请求(04 §2.6.3)", host, source)
            return
        if url not in {u for _s, u in out}:
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
                 token: Optional[str] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 gateway: Optional[Callable[[], Optional[str]]] = None, resolv_conf: str = "/etc/resolv.conf"):
        self.cfg = cfg
        self._transport = transport or urllib_transport
        self._base_url = base_url
        self._pinned = base_url is not None          # 显式给了地址(测试/[winagent] url)⇒ discover 不改它
        self._token = token
        self._clock = clock
        self._gateway = gateway
        self._resolv_conf = resolv_conf
        self.base_url_source: Optional[str] = "explicit" if base_url is not None else None
        self.last_version: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def base_url(self) -> Optional[str]:
        if self._base_url is None:
            c = self.candidates()
            if c:
                self.base_url_source, self._base_url = c[0]
        return self._base_url

    def candidates(self) -> list[tuple[str, str]]:
        return base_url_candidates(self.cfg, resolv_conf=self._resolv_conf, gateway=self._gateway)

    async def discover(self) -> Optional[str]:
        """04 §2.6.3:「Agent 先读 `host.json`,**与默认网关不一致时以能 ping 通 `/wa/v1/ping` 的那个为准**,并记 `warn`」。

        按 :func:`base_url_candidates` 的顺序逐个探 ``/wa/v1/ping``(无鉴权、2 s),第一个通的落为 ``base_url``;
        若被采用的不是首选项,记一条 `warn` 日志(首选项与实际可达的不是同一个 = 现场大概率开了 VPN 或走了 V1 对策)。
        全都不通则保留首选项(H02 会照常报离线),下一轮再发现。``[winagent] url`` 显式配置时**不做发现**。
        """
        if self._pinned or self.cfg.url:
            return self._base_url or (self.cfg.url.rstrip("/") if self.cfg.url else None)
        cands = self.candidates()
        if not cands:
            return None
        saved = self._base_url
        for i, (source, url) in enumerate(cands):
            self._base_url = url
            try:
                pong = await self.ping()
            except Exception as e:                    # 探活本身抛了:当作这个候选不可达,继续下一个
                log.info("WinAgent 地址候选 %s(%s)探活异常: %r", url, source, e)
                pong = None
            self._base_url = saved
            if pong is not None:
                self._base_url, self.base_url_source = url, source
                if i > 0:
                    log.warning("WinAgent 地址发现:首选项 %s(%s)不可达,改用 %s(%s)——现场可能开了 VPN 或 "
                                "resolv.conf 已被 DNS 对策改写(04 §2.6.3)", cands[0][1], cands[0][0], url, source)
                return url
        self.base_url_source, self._base_url = cands[0]
        log.warning("WinAgent 地址发现:%d 个候选全不可达,暂用首选项 %s(%s)", len(cands), cands[0][1], cands[0][0])
        return self._base_url

    def forget_base_url(self) -> None:
        """04 §2.6.3 末:「Agent 缓存主机 IP,**H02 失败时重新发现**(子网可能变了)」。"""
        if not self._pinned and not self.cfg.url:
            self._base_url = None
            self.base_url_source = None

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


    # ---- #15/#16 probes(A 令牌):实测采样候选的读 / 回写 / 采纳(04 §2.8.4 + §3.4;裁决 A-14~A-17)
    async def read_observed(self, *, channel: Optional[str] = None) -> tuple[list[dict[str, Any]], list[str]]:
        """``GET /wa/v1/probes?kind=observed`` → ``(observed 行, 正式 targets)``。

        行原样带 ``id``(= ``#76b`` 的 ``observed_ids`` 用的稳定 id)与派生的 ``in_config``。
        """
        path = "/wa/v1/probes?kind=observed" + (f"&channel={channel}" if channel else "")
        status, body = await self.request("GET", path, timeout_s=TIMEOUT_S["probes"], retry=True)
        if status == 404:
            raise WinAgentUnavailable("observed_endpoint_missing", "GET /wa/v1/probes?kind=observed")
        if status != 200:
            raise WinAgentUnavailable("bad_observed_response", f"status={status}")
        return list((body or {}).get("observed") or []), list((body or {}).get("targets") or [])

    async def sample_probe(self, *, duration_s: float = 5.0) -> dict[str, Any]:
        """``POST /wa/v1/probe {mode:'sample'}`` —— 04 §3.4:**只返回不落库**,回 ``{sampled_at, duration_s, rows}``。"""
        status, body = await self.request("POST", "/wa/v1/probe", json={"mode": "sample", "duration_s": duration_s},
                                          timeout_s=max(5.0, duration_s + 5.0))
        if status != 200:
            raise WinAgentUnavailable("bad_sample_response", f"status={status}")
        return dict(body or {})

    async def write_observed(self, rows: list[dict[str, Any]], *, side: str = "windows") -> int:
        """``PUT /wa/v1/probes {kind:'observed', rows}`` —— 实测采样的**唯一写入口**(04 §3.4)。

        🔴 A-17:``sample`` 的行里没有 ``side``(Windows 侧只能按 PID 反推通道),由**回写方**补 —— 三侧
        (容器 / WSL / Windows)的候选必须由 Agent 聚合后**写一次**,各写各的会让 ``hits`` 被算两遍。
        """
        payload = [dict(r) if r.get("side") else dict(r, side=side) for r in rows]
        status, body = await self.request("PUT", "/wa/v1/probes", json={"kind": "observed", "rows": payload},
                                          timeout_s=TIMEOUT_S["probes"])
        if status != 200:
            raise WinAgentUnavailable("bad_write_observed_response", f"status={status}")
        return int((body or {}).get("written") or 0)

    async def adopt_observed(self, observed_ids: list[int]) -> dict[str, Any]:
        """``PUT /wa/v1/probes/adopt {observed_ids}`` → ``{adopted, adopted_rows, targets, hosts_by_channel}``。

        🔴 A-14:``observed_ids`` 是**采纳后的全集**(04 §2.8.4「替换整表不追加」),``[]`` = 清空。
        """
        status, body = await self.request("PUT", "/wa/v1/probes/adopt", json={"observed_ids": list(observed_ids)},
                                          timeout_s=TIMEOUT_S["probes"])
        if status == 404:
            raise WinAgentUnavailable("adopt_endpoint_missing", "PUT /wa/v1/probes/adopt")
        if status != 200:
            raise WinAgentUnavailable("bad_adopt_response", f"status={status}")
        return dict(body or {})


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
        self.client_ip: Optional[str] = None         # B-3:#11 的来源校验;None = 本机直连(测试默认放行)
        # C-1 实测采样(02 §3.2 `probe_targets_observed`)与采纳登记(`settings.probe.targets`)的假表。
        # ⚠️ `observed_supported=False` 时 `GET /wa/v1/probes?kind=observed` 与 `PUT /wa/v1/probes/adopt`
        #    回 404 —— 用来复现「WinAgent 还没补这两个端点」的现状(rulings (ci))。
        self.observed_supported = True
        self.observed: list[dict[str, Any]] = []     # 行形态逐字照 02 §3.2 DDL(稳定 id = `id` 列)
        self.probe_targets: list[str] = []           # `settings['probe.targets']`,元素 = "host:port"(A-16)
        self.sample_rows: list[dict[str, Any]] = []  # `POST /probe {mode:'sample'}` 要回的行(04 §3.4:只回不落库)
        self.probe_results: list[dict[str, Any]] = []   # #15 `GET /wa/v1/probes?run_id=|latest=1` 读 `probe_results`
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
                                    # 02 §3.6 #2:modules 恒含六个键(B-2:原来只有三个,按 wslctl 做降级判断的测试会假绿)
                                    "modules": {"vault": "ok", "monitor": "ok", "netprobe": "ok", "power": "ok", "wslctl": "ok",
                                                "wechat": "enabled" if self.wechat_enabled else "disabled"},
                                    # R6-58 (ao):checks **恒八键**、没跑过的给 null(与 winagent monitor.HEALTH_CHECK_KEYS 同序)
                                    "checks": {k: None for k in ("H01", "H09", "H10", "H11", "H14", "H15", "H16", "H20")},
                                    "host": self.host}).encode()
        if path == "/wa/v1/time":
            return 200, rh, _dumps({"now_ms": self._now(), "tz_offset_min": 480, "last_resume_ms": self.last_resume_ms,
                                    "w32time": {"source": "time.windows.com", "last_sync_ms": self._now() - 3600_000}}).encode()
        bare, _, query = path.partition("?")
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv) if query else {}
        if bare == "/wa/v1/probes" and method == "GET" and q.get("kind") != "observed":
            return 200, rh, _dumps({"results": list(self.probe_results)}).encode()
        if bare == "/wa/v1/probes" and method == "GET" and q.get("kind") == "observed":
            # 04 §3.4 / 裁决 (ci):读 `probe_targets_observed`;行原样带 DDL 的 `id`,另派生 `in_config`,
            # 并**与正式目标表一起回**(`targets`)——面板要靠 `in_config` 默认只勾新增项。
            if not self.observed_supported:
                return 404, rh, b'{"ok":false,"code":"TARGET_NOT_FOUND"}'
            rows = [dict(r, in_config=self._hostport(r) in self.probe_targets) for r in self.observed]
            if q.get("channel"):
                rows = [r for r in rows if r.get("channel") == q["channel"]]
            return 200, rh, _dumps({"observed": rows, "targets": list(self.probe_targets)}).encode()
        if bare == "/wa/v1/probes" and method == "PUT" and (data.get("kind") == "observed"):
            # 04 §3.4:实测采样的**唯一写入口**。三侧候选由 Agent 聚合后写一次,否则 `hits` 会被算两遍。
            return 200, rh, _dumps({"written": self._write_observed(list(data.get("rows") or [])), "kind": "observed"}).encode()
        if bare == "/wa/v1/probe" and method == "POST" and data.get("mode") == "sample":
            # 04 §3.4:`sample` **只返回不落库**;行里没有 `side`(Windows 侧只能按 PID 反推通道),由回写方补(A-17)。
            return 200, rh, _dumps({"sampled_at": self._now(), "duration_s": float(data.get("duration_s") or 5),
                                    "rows": [dict(r) for r in self.sample_rows]}).encode()
        if bare == "/wa/v1/probes/adopt" and method == "PUT":
            # 02 #76b 的 WinAgent 半:写选中行 `adopted_ms` + 更新 `settings['probe.targets']`。
            # 🔴 A-14:`observed_ids` 是**采纳后的全集**(04 §2.8.4「替换整表不追加,让用户能删旧项」),
            #    不在集合里的已采纳行取消采纳;`[]` = 清空。A-15:`adopted` = 行 id,`targets` = "host:port"。
            if not self.observed_supported:
                return 404, rh, b'{"ok":false,"code":"TARGET_NOT_FOUND"}'
            ids = [int(x) for x in (data.get("observed_ids") or [])]
            if len(set(ids)) != len(ids):
                return 400, rh, b'{"ok":false,"code":"INVALID_ARGS","error":{"reason":"duplicate_ids"}}'
            known = {int(r["id"]) for r in self.observed}
            missing = sorted(set(ids) - known)
            if missing:
                return 404, rh, _dumps({"ok": False, "code": "TARGET_NOT_FOUND",
                                        "error": {"reason": "observed_id_not_found", "missing": missing}}).encode()
            for row in self.observed:
                rid = int(row["id"])
                if rid not in ids:
                    row["adopted_ms"] = None                       # 替换整表:掉出集合的取消采纳
                elif row.get("adopted_ms") is None:
                    row["adopted_ms"] = self._now()                # 已采纳的不刷新时刻(幂等)
            rows = [r for r in self.observed if r.get("adopted_ms") is not None]
            targets: list[str] = []
            hosts: dict[str, list[str]] = {"qidian_hosts": [], "qq_hosts": [], "wechat_hosts": []}
            for r in rows:
                hp = self._hostport(r)
                if hp not in targets:                              # 同一 host:port 可能被多个账号各采到一行
                    targets.append(hp)
                key = {"qidian": "qidian_hosts", "qq": "qq_hosts", "wechat": "wechat_hosts"}.get(r.get("channel") or "")
                if key and hp not in hosts[key]:
                    hosts[key].append(hp)
            self.probe_targets = targets
            return 200, rh, _dumps({"adopted": [int(r["id"]) for r in rows], "adopted_rows": rows,
                                    "targets": list(targets), "hosts_by_channel": hosts}).encode()
        m = re.match(r"^/wa/v1/vault/(.+?)(/read|/flag)?$", path)
        if m:
            name, op = m.group(1), m.group(2)
            if op == "/read" and method == "POST":
                if "X-Trace-Id" not in headers:
                    return 400, rh, b'{"ok":false,"code":"INVALID_ARGS"}'
                # 02 §3.6 #11:读只接受 loopback / WSL 子网(B-3:假后端此前不校验,来源限制在测试里恒真)
                if not _vault_read_source_allowed(self.client_ip):
                    return 403, rh, b'{"ok":false,"code":"FORBIDDEN","error":{"reason":"source_not_allowed"}}'
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
                # 05 §2.2.2:单条明文上限 4 KB(B-3:假后端此前不校验)
                if len(str(data.get("value", "")).encode("utf-8")) > VAULT_MAX_VALUE_BYTES:
                    return 400, rh, b'{"ok":false,"code":"INVALID_ARGS","error":{"reason":"value_too_large"}}'
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

    @staticmethod
    def _hostport(row: dict[str, Any]) -> str:
        """A-16:`targets` 与 04 §2.8.4 的 `[probe] *_hosts` 同一形状 —— `"host:port"`,不带通道前缀。"""
        return f"{row.get('remote_host') or row.get('remote_ip')}:{int(row['remote_port'])}"

    def _write_observed(self, rows: list[dict[str, Any]]) -> int:
        """按 02 §3.2 的唯一索引 ``(COALESCE(account_id,''), remote_ip, remote_port, proto)`` upsert:同键**累加 hits**。

        两套列名都收(04 §3.4 的 ``ip/port/hostname/samples`` 与 02 DDL 的 ``remote_*/hits``),与 WinAgent 侧一致。
        """
        n = 0
        for raw in rows:
            ip = raw.get("remote_ip") or raw.get("ip")
            port = int(raw.get("remote_port") or raw.get("port"))
            proto = raw.get("proto") or "tcp"
            acct = raw.get("account_id")
            host = raw.get("remote_host") or raw.get("hostname")
            hits = int(raw.get("hits") or raw.get("samples") or 1)
            now = self._now()
            hit = next((r for r in self.observed if (r.get("account_id") or "") == (acct or "")
                        and r["remote_ip"] == ip and int(r["remote_port"]) == port and r.get("proto") == proto), None)
            if hit is not None:
                hit["hits"] = int(hit.get("hits") or 0) + hits
                hit["last_seen_ms"] = now
                if host:
                    hit["remote_host"] = host          # 解析不到时不要把已有域名抹掉
            else:
                self.observed.append({"id": len(self.observed) + 1, "channel": raw.get("channel"), "account_id": acct,
                                      "remote_host": host, "remote_ip": ip, "remote_port": port, "proto": proto,
                                      "side": raw.get("side") or "windows", "hits": hits,
                                      "first_seen_ms": now, "last_seen_ms": now, "adopted_ms": None})
            n += 1
        return n
