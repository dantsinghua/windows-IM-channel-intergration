"""WSL 侧四级连通性探测(04 §2.8.1 / §2.8.2;#75 ``mode:"probe"|"full"`` 的 ``agent.net_probe`` 执行体)。

实现 `api/routes_ext.LevelProbe` 协议:``async probe(target, *, timeout_s) -> dict``。逐级 ``dns → tcp → tls → http``,
**每级独立超时、失败即停**,记录到达的最深一级;结论码取 04 §2.8.1 的九值映射表(``基线 §8.5``)。

🔴 **缺省不出网**:本模块只是**执行体**,`app.py` 缺省**不装配**它 —— `#75` 那一侧于是记
``SKIPPED(agent_probe_disabled)``。要真探得由装配方显式注入(``AgentApp(net_probe=…)``)。
⚠️ 「用哪个 `agent.toml` 键开启」在 docs/07 与 02 §7.1 `[probe]` 里**尚无登记键**(该段只有
``qidian_hosts``/``qq_hosts``/``qq_login_hosts``/``periodic_interval_min``),故本批**不造键**,只把开关做成
构造参数 + 装配参数,登记新键属文档侧(见 `.omc/handoffs/agent-polish.md`)。

只用标准库(``socket`` / ``ssl`` / ``http.client``),阻塞调用一律在线程里跑(``asyncio.to_thread``),
不占事件循环;**不带任何凭据**(04 §2.8.1 末条:只回答「网络到不到得了」)。
"""
from __future__ import annotations

import asyncio
import http.client
import logging
import socket
import ssl
import time
from typing import Any, Optional

log = logging.getLogger("qtrade.netprobe")

#: 04 §2.8.1 每级超时(秒)。真值 owner = 04 §7 `[probe] dns/tcp/tls/http_timeout_s`;这里是同值缺省。
DNS_TIMEOUT_S = 3.0
TCP_TIMEOUT_S = 5.0
TLS_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 5.0
TOTAL_TIMEOUT_S = 15.0                  # 04 §2.8.1:单目标总上限 15 s

#: 无显式端口时按 scheme/端口推断要不要做 TLS 与 HTTP 两级
TLS_PORTS = (443, 465, 993, 995, 8443)
HTTP_PORTS = (80, 443, 8080, 8443)

LOCAL_RST_MS = 5.0                      # 04 §2.8.1:RST 在 <5 ms 内到达 ⇒ 疑似本机策略阻断


def parse_target(target: str) -> tuple[str, Optional[int], Optional[str]]:
    """``"host:port"`` / ``"http(s)://host[:port]/path"`` / ``"host"`` → ``(host, port, path)``。

    `path` 只在给了 URL 时非空(HTTP 级用它);``host:port`` 形态回 ``None`` ⇒ HTTP 级探 ``/``。
    """
    t = (target or "").strip()
    if not t:
        raise ValueError("探测目标为空")
    path: Optional[str] = None
    if "://" in t:
        scheme, rest = t.split("://", 1)
        host_part, _, raw_path = rest.partition("/")
        path = "/" + raw_path
        default = 443 if scheme.lower() == "https" else 80
        host, port = _split_hostport(host_part)
        return host, port or default, path
    host, port = _split_hostport(t)
    return host, port, path


def _split_hostport(s: str) -> tuple[str, Optional[int]]:
    if s.startswith("["):                                   # [v6]:port
        addr, _, tail = s[1:].partition("]")
        port = tail.lstrip(":")
        return addr, int(port) if port.isdigit() else None
    if s.count(":") == 1:
        host, _, port = s.partition(":")
        return host, int(port) if port.isdigit() else None
    return s, None                                          # 裸 IPv6 或纯主机名


class SocketLevelProbe:
    """标准库实现的四级探测器。

    可注入点全在构造里,Fake 走同一条码路:``resolve`` / ``connect`` / ``handshake`` / ``http_get``
    任一给了替身就用替身(测试据此不碰网络),缺省才用真 socket/ssl/http.client。
    """

    def __init__(self, *, dns_timeout_s: float = DNS_TIMEOUT_S, tcp_timeout_s: float = TCP_TIMEOUT_S,
                 tls_timeout_s: float = TLS_TIMEOUT_S, http_timeout_s: float = HTTP_TIMEOUT_S,
                 total_timeout_s: float = TOTAL_TIMEOUT_S, ssl_context: Optional[ssl.SSLContext] = None,
                 resolve=None, connect=None, handshake=None, http_get=None,
                 clock=lambda: time.monotonic()):
        self.dns_timeout_s = dns_timeout_s
        self.tcp_timeout_s = tcp_timeout_s
        self.tls_timeout_s = tls_timeout_s
        self.http_timeout_s = http_timeout_s
        self.total_timeout_s = total_timeout_s
        self._ctx = ssl_context
        self._resolve = resolve or self._resolve_real
        self._connect = connect or self._connect_real
        self._handshake = handshake or self._handshake_real
        self._http_get = http_get or self._http_get_real
        self._clock = clock

    # ---------------------------------------------------------------- 各级的真实现(阻塞,只在线程里跑)
    @staticmethod
    def _resolve_real(host: str, port: Optional[int], timeout_s: float) -> list[str]:
        socket.setdefaulttimeout(timeout_s)                 # getaddrinfo 没有 timeout 参数,只能走这个
        try:
            infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
        finally:
            socket.setdefaulttimeout(None)
        return sorted({i[4][0] for i in infos})

    @staticmethod
    def _connect_real(host: str, port: int, timeout_s: float) -> None:
        with socket.create_connection((host, port), timeout=timeout_s):
            pass

    def _handshake_real(self, host: str, port: int, timeout_s: float) -> dict[str, Any]:
        ctx = self._ctx or ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout_s) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                issuer = ""
                for part in cert.get("issuer") or ():
                    for k, v in part:
                        if k == "organizationName":
                            issuer = v
                return {"tls_version": tls.version() or "", "issuer": issuer}

    @staticmethod
    def _http_get_real(host: str, port: int, path: str, *, tls: bool, timeout_s: float) -> int:
        klass = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        conn = klass(host, port, timeout=timeout_s)         # type: ignore[arg-type]
        try:
            conn.request("HEAD", path or "/", headers={"User-Agent": "qtrade-agent/netprobe"})
            return conn.getresponse().status
        finally:
            conn.close()

    # ---------------------------------------------------------------- 协议方法
    async def probe(self, target: str, *, timeout_s: float = TCP_TIMEOUT_S) -> dict[str, Any]:
        """一个目标的逐级探测。``timeout_s`` 是调用方给的**单级**上限(与 04 各级缺省取较小者)。

        出参 = ``{status, level_reached, detail, addresses?, http_status?, tls?}``;
        ``status`` ∈ 04 §2.8.1 九值 + ``SKIPPED``,``level_reached`` ∈ ``none|dns|tcp|tls|http``。
        """
        try:
            host, port, path = parse_target(target)
        except ValueError as e:
            return {"status": "SKIPPED", "level_reached": "none", "detail": f"bad_target:{e}"}
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._probe_blocking, host, port, path, timeout_s),
                                          timeout=self.total_timeout_s)
        except asyncio.TimeoutError:
            return {"status": "TCP_TIMEOUT", "level_reached": "none",
                    "detail": f"单目标超过总上限 {self.total_timeout_s}s"}

    # ---------------------------------------------------------------- 线程里的逐级主体
    def _probe_blocking(self, host: str, port: Optional[int], path: Optional[str], cap_s: float) -> dict[str, Any]:
        out: dict[str, Any] = {"level_reached": "none"}
        # ① DNS
        try:
            addrs = self._resolve(host, port, min(self.dns_timeout_s, cap_s) if cap_s else self.dns_timeout_s)
        except (socket.gaierror, socket.timeout, OSError) as e:
            return {**out, "status": "DNS_FAIL", "detail": _short(e)}
        if not addrs:
            return {**out, "status": "DNS_FAIL", "detail": "解析无结果"}
        out["level_reached"] = "dns"
        out["addresses"] = addrs
        if port is None:                                     # 没有端口就只能探到 DNS 这一级(04 §2.8.2 的目标都带端口)
            return {**out, "status": "OK", "detail": "只探到 dns(目标未给端口)"}
        # ② TCP
        t0 = self._clock()
        try:
            self._connect(host, port, min(self.tcp_timeout_s, cap_s) if cap_s else self.tcp_timeout_s)
        except socket.timeout:
            return {**out, "status": "TCP_TIMEOUT", "detail": f"connect 超时 {self.tcp_timeout_s}s"}
        except ConnectionRefusedError as e:
            elapsed_ms = (self._clock() - t0) * 1000
            # 04 §2.8.1:RST 在 <5 ms 内到达(来自本机)⇒ 记 BLOCKED_BY_POLICY;Windows 侧的防火墙规则核对归 WinAgent
            status = "BLOCKED_BY_POLICY" if elapsed_ms < LOCAL_RST_MS else "TCP_REFUSED"
            return {**out, "status": status, "detail": f"{_short(e)}(RST {elapsed_ms:.1f}ms)"}
        except OSError as e:
            return {**out, "status": "TCP_TIMEOUT", "detail": _short(e)}
        out["level_reached"] = "tcp"
        want_tls = port in TLS_PORTS
        want_http = port in HTTP_PORTS
        if not want_tls and not want_http:                   # 私有协议(企点/QQ 的 8080/14000)不做更深(04 §2.8.2)
            return {**out, "status": "OK", "detail": "tcp 通(私有协议不做更深)"}
        # ③ TLS
        if want_tls:
            try:
                info = self._handshake(host, port, min(self.tls_timeout_s, cap_s) if cap_s else self.tls_timeout_s)
            except (ssl.SSLError, socket.timeout, OSError) as e:   # SSLCertVerificationError 是 SSLError 子类,一并在此
                return {**out, "status": "TLS_FAIL", "detail": _short(e)}
            out["level_reached"] = "tls"
            out["tls"] = info
            if info.get("issuer"):
                out["mitm_ca"] = info["issuer"]              # 04 §2.8.1:签发者原样记进 detail 侧字段,判不判中间人归 P-ENV
        if not want_http:
            return {**out, "status": "OK", "detail": "tls 通"}
        # ④ HTTP
        try:
            code = self._http_get(host, port, path or "/", tls=want_tls,
                                  timeout_s=min(self.http_timeout_s, cap_s) if cap_s else self.http_timeout_s)
        except (socket.timeout, OSError, http.client.HTTPException) as e:
            return {**out, "status": "HTTP_5XX" if isinstance(e, http.client.HTTPException) else "TCP_TIMEOUT",
                    "detail": _short(e)}
        out["level_reached"] = "http"
        out["http_status"] = code
        return {**out, "status": http_status_to_result(code), "detail": f"HTTP {code}"}


def http_status_to_result(code: int) -> str:
    """04 §2.8.1 的 HTTP 段映射。``401`` 单独由调用方判(docker_registry 的 401 算 OK,目标表里的例外)。"""
    if code == 407:
        return "PROXY_REQUIRED"
    if 400 <= code < 500:
        return "HTTP_4XX"
    if code >= 500:
        return "HTTP_5XX"
    return "OK"


def _short(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:200]
