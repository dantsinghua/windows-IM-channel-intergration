"""企点启动前置：在目标 Android 内解析并连接 MSF，不依赖宿主 DNS 或 Google 验证。"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import shlex
import time
from typing import Any, Sequence

from .backends import AdbBackend


def _target(value: str) -> tuple[str, int]:
    host, port_text = value.rsplit(":", 1)
    host = host.removeprefix("[").removesuffix("]")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or ".." in host
                or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) is None):
            raise ValueError("invalid host") from None
    return host, port


def _ping_address(output: str) -> str | None:
    # ping 的标题在收 ICMP 回复之前输出。rc=1 / 100% loss 仍可证明 resolver 已成功。
    match = re.search(r"(?m)^PING[^\n]*?\(([^()\s]+)\)", output)
    if match:
        try:
            return str(ipaddress.ip_address(match[1]))
        except ValueError:
            pass
    return None


def _tool_error(output: str) -> bool:
    lower = output.lower()
    return any(text in lower for text in (
        "not found", "unknown option", "invalid option", "unrecognized option", "usage:",
        "permission denied", "operation not permitted", "device offline", "unauthorized", "nc: -q <",
    ))


async def probe_qidian_network(
    adb: AdbBackend, serial: str, hosts: Sequence[str], *, dns_timeout_s: float = 3,
    tcp_timeout_s: float = 5, target_timeout_s: float = 15, total_timeout_s: float = 20,
) -> dict[str, Any]:
    """每目标 DNS→TCP，所有目标并行、任一 TCP 成功即通过；探针异常如实记 SKIPPED。

    shell_result 保留远端退出码且可取消。TCP 只连 resolver 返回的地址，无应用层载荷，
    nc stdout 丢弃；报告仅含目标/阶段/结果/耗时，不含原始输出、密码或流量。
    """
    started = time.monotonic()
    targets: list[dict[str, Any]] = []

    async def probe(value: str) -> None:
        begin = time.monotonic()
        entry: dict[str, Any] = {
            "host": None, "port": None, "stage": "config", "result": "SKIPPED",
            "detail": "invalid_target", "elapsed_ms": 0,
        }
        targets.append(entry)
        try:
            host, port = _target(value)
        except (TypeError, ValueError, AttributeError):
            return
        entry.update(host=host, port=port, stage="dns", detail="probe_incomplete")
        try:
            async with asyncio.timeout(target_timeout_s):
                try:
                    address = str(ipaddress.ip_address(host))
                except ValueError:
                    dns = await asyncio.wait_for(adb.shell_result(
                        serial, f"ping -c 1 -W 1 {shlex.quote(host)}", timeout_s=dns_timeout_s,
                    ), dns_timeout_s)
                    if _tool_error(dns.output):
                        entry["detail"] = "dns_tool_unavailable"
                        return
                    address = _ping_address(dns.output)
                    if address is None:
                        if any(text in dns.output.lower() for text in (
                            "unknown host", "bad address", "name or service not known",
                            "temporary failure in name resolution", "no address associated", "unable to resolve",
                        )):
                            entry.update(result="DNS_FAIL", detail="android_dns_failed")
                        else:
                            entry["detail"] = "dns_output_unrecognized"
                        return
                    if dns.returncode not in (0, 1):
                        entry["detail"] = "dns_command_failed"
                        return
                entry.update(address=address, stage="tcp")
                # Android Toybox nc 不支持 -z 或 -q 0；stdin EOF，-q 1 退出且不发送业务数据。
                connect_s = max(1, math.ceil(tcp_timeout_s) - 2)
                tcp = await asyncio.wait_for(adb.shell_result(
                    serial, f"nc -w {connect_s} -q 1 {shlex.quote(address)} {port} </dev/null >/dev/null",
                    timeout_s=tcp_timeout_s,
                ), tcp_timeout_s)
                if _tool_error(tcp.output):
                    entry["detail"] = "tcp_tool_unavailable"
                elif tcp.returncode == 0:
                    entry.update(result="OK", detail="tcp_connected")
                elif "refused" in tcp.output.lower():
                    entry.update(result="TCP_REFUSED", detail="connection_refused")
                elif any(text in tcp.output.lower() for text in ("timed out", "timeout", "unreachable", "no route")):
                    entry.update(result="TCP_TIMEOUT", detail="tcp_unreachable")
                else:
                    entry["detail"] = "tcp_command_failed"
        except asyncio.TimeoutError:
            entry.update(result="DNS_FAIL" if entry["stage"] == "dns" else "TCP_TIMEOUT", detail="stage_timeout")
        except Exception:
            # 拒绝/缺工具/框架错误不能伪装成目标 TCP 超时，更不能忽略后宣布就绪。
            entry.update(result="SKIPPED", detail="probe_command_failed")
        finally:
            entry["elapsed_ms"] = round((time.monotonic() - begin) * 1000)

    if not isinstance(hosts, (list, tuple)) or not hosts:
        targets.append({"host": None, "port": None, "stage": "config", "result": "SKIPPED",
                        "detail": "no_valid_targets", "elapsed_ms": 0})
    else:
        try:
            await asyncio.wait_for(asyncio.gather(*(probe(value) for value in hosts)), total_timeout_s)
        except asyncio.TimeoutError:
            for entry in targets:
                if entry["detail"] == "probe_incomplete":
                    entry.update(result="SKIPPED", detail="round_timeout")
    ok = any(entry["result"] == "OK" for entry in targets)
    result = "OK" if ok else next(
        (code for code in ("SKIPPED", "TCP_TIMEOUT", "TCP_REFUSED", "DNS_FAIL")
         if any(entry["result"] == code for entry in targets)), "SKIPPED")
    return {"ok": ok, "result": result, "side": "android", "targets": targets,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}
