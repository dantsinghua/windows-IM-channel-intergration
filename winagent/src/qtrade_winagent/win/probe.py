"""``WinProbe`` —— 04 §2.8.1 的逐级探测(``dns → tcp → tls → http``)与 C-1 实测采样的 Windows 侧取数。

结论码按 04 §2.8.1 那张映射表(00 §8.5 九值);**不带凭据**(只回答「网络到不到得了」)。
本类只用标准库,Linux 上也能跑(真机与开发机行为一致),故不加 ``require_windows``;
只有 ``connections()``(``netstat -ano``)是 Windows 专有。
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..backends import ProbeStep
from . import run_text


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


class WinProbe:
    async def dns(self, host: str, timeout_s: float) -> ProbeStep:
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None), timeout=timeout_s)
        except (asyncio.TimeoutError, socket.gaierror, OSError) as e:
            return ProbeStep("dns", False, _ms(t0), "DNS_FAIL", str(e)[:200])
        return ProbeStep("dns", True, _ms(t0))

    async def tcp(self, host: str, port: int, timeout_s: float) -> ProbeStep:
        t0 = time.perf_counter()
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
            w.close()
            await w.wait_closed()
        except asyncio.TimeoutError:
            return ProbeStep("tcp", False, _ms(t0), "TCP_TIMEOUT")
        except ConnectionRefusedError as e:
            # <5ms 内到达的 RST 多半是本机策略拦的(04 §2.8.1 映射表);本层只给线索,判定留给上层
            code = "BLOCKED_BY_POLICY" if _ms(t0) < 5 else "TCP_REFUSED"
            return ProbeStep("tcp", False, _ms(t0), code, str(e)[:200])
        except OSError as e:
            return ProbeStep("tcp", False, _ms(t0), "TCP_TIMEOUT", str(e)[:200])
        return ProbeStep("tcp", True, _ms(t0))

    async def tls(self, host: str, port: int, timeout_s: float) -> ProbeStep:
        t0 = time.perf_counter()
        ctx = ssl.create_default_context()
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host),
                                          timeout=timeout_s)
            cert = w.get_extra_info("peercert") or {}
            w.close()
            await w.wait_closed()
        except ssl.SSLCertVerificationError as e:
            # 证书链是公司中间人 CA 时 detail 记 mitm_ca=<issuer>(04 §2.8.1)
            return ProbeStep("tls", False, _ms(t0), "TLS_FAIL", f"mitm_ca?={e.verify_message}"[:200])
        except (asyncio.TimeoutError, ssl.SSLError, OSError) as e:
            return ProbeStep("tls", False, _ms(t0), "TLS_FAIL", str(e)[:200])
        issuer = ",".join(x[0][1] for x in cert.get("issuer", ()) if x and x[0])
        return ProbeStep("tls", True, _ms(t0), detail=issuer[:200] or None)

    async def http(self, method: str, url: str, timeout_s: float) -> ProbeStep:
        t0 = time.perf_counter()

        def go() -> tuple[int, str]:
            req = urllib.request.Request(url, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:   # noqa: S310(目标由配置给定)
                    return resp.status, ""
            except urllib.error.HTTPError as e:
                return e.code, ""
        try:
            status, _ = await asyncio.wait_for(asyncio.to_thread(go), timeout=timeout_s + 1)
        except (asyncio.TimeoutError, urllib.error.URLError, OSError) as e:
            return ProbeStep("http", False, _ms(t0), "TCP_TIMEOUT", str(e)[:200])
        if status == 407:
            return ProbeStep("http", False, _ms(t0), "PROXY_REQUIRED")
        if 400 <= status < 500:
            return ProbeStep("http", False, _ms(t0), "HTTP_4XX", f"status={status}")
        if status >= 500:
            return ProbeStep("http", False, _ms(t0), "HTTP_5XX", f"status={status}")
        return ProbeStep("http", True, _ms(t0), detail=f"status={status}")

    async def connections(self, pid_names: tuple[str, ...], duration_s: int) -> list[dict[str, Any]]:
        """C-1 实测采样的 Windows 侧:``netstat -ano -p tcp`` 同频采样按 PID 过滤。

        过滤掉 docker 池、WSL NAT 段、loopback 与本机 17600/17610、chatlog 本地口 ``127.0.0.1:5030``(04 §2.8.4)。
        **只记 IP:port 与域名,不记任何载荷。**
        """
        import psutil
        wanted = {n.lower().rstrip("*") for n in pid_names}
        seen: dict[tuple[str, int], dict[str, Any]] = {}
        end = time.time() + max(1, duration_s)
        while time.time() < end:
            pids = {p.pid: (p.info.get("name") or "")
                    for p in psutil.process_iter(["name"])
                    if any((p.info.get("name") or "").lower().startswith(w) for w in wanted)}
            for c in psutil.net_connections(kind="tcp"):
                if c.status != psutil.CONN_ESTABLISHED or c.pid not in pids or not c.raddr:
                    continue
                ip, port = c.raddr.ip, int(c.raddr.port)
                if _is_local(ip) or port in (17600, 17610, 5030):
                    continue
                key = (ip, port)
                row = seen.setdefault(key, {"ip": ip, "port": port, "proto": "tcp", "channel": "wechat",
                                            "hostname": _reverse(ip), "samples": 0})
                row["samples"] += 1
            await asyncio.sleep(1)
        return list(seen.values())


def _is_local(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return a.is_loopback or a.is_private


def _reverse(ip: str) -> Optional[str]:
    """DNS 客户端缓存正查优先于 PTR(04 §2.8.4:腾讯 IP 的 PTR 多为空或 CDN 泛名)。"""
    try:
        out = run_text(["powershell", "-NoProfile", "-Command",
                        f"(Get-DnsClientCache | Where-Object {{$_.Data -eq '{ip}'}} | Select-Object -First 1).Entry"],
                       timeout=5).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return None
