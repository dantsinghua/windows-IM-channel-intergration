"""``WinNet`` —— 适配器 / 路由 / 代理识别(04 §2.6.1、§2.6.3、§2.6.4)。

- **WSL 网卡查找规则(04 §2.6.3 逐字)**:``InterfaceDescription == "Hyper-V Virtual Ethernet Adapter"``
  且 ``Name`` 以 ``vEthernet (WSL`` 开头;找不到则退化为「IPv4 属于 172.16.0.0/12 的 Hyper-V 虚拟网卡」。
- **代理四来源**:WinHTTP(机器级)、WinINET(用户级,经 ``WTSQueryUserToken`` 拿活动会话 SID 读 **HKU**——
  🔴 此绕法**只用于读用户级注册表**,C-02;不用它去跑任何进程)、PAC/WPAD、策略 ``ProxySettingsPerUser``。
"""
from __future__ import annotations

import ipaddress
import json
from typing import Any, Optional

from ..backends import Adapter, ProxyInfo
from . import require_windows, run_text

PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
WSL_DESC = "Hyper-V Virtual Ethernet Adapter"
WSL_NAME_PREFIX = "vEthernet (WSL"
WSL_FALLBACK_NET = "172.16.0.0/12"


def _ps_json(script: str, timeout: int = 15) -> Any:
    out = run_text(PS + [script + " | ConvertTo-Json -Depth 4 -Compress"], timeout=timeout).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


class WinNet:
    def adapters(self) -> list[Adapter]:
        require_windows("Get-NetAdapter")
        rows = _ps_json("Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,ifIndex")
        ips = {r["ifIndex"]: r for r in _ps_json(
            "Get-NetIPAddress -AddressFamily IPv4 | Select-Object ifIndex,IPAddress,PrefixLength")}
        mtus = {r["ifIndex"]: r.get("NlMtu") for r in _ps_json(
            "Get-NetIPInterface -AddressFamily IPv4 | Select-Object ifIndex,NlMtu")}
        routes: dict[int, list[str]] = {}
        for r in _ps_json("Get-NetRoute -AddressFamily IPv4 | Select-Object ifIndex,DestinationPrefix"):
            routes.setdefault(r["ifIndex"], []).append(r["DestinationPrefix"])
        out: list[Adapter] = []
        for r in rows:
            idx = r["ifIndex"]
            ip = ips.get(idx, {})
            prefixes = tuple(routes.get(idx, ()))
            out.append(Adapter(name=r["Name"], description=r.get("InterfaceDescription") or "",
                               up=str(r.get("Status")) == "Up", ipv4=ip.get("IPAddress"),
                               prefix_length=ip.get("PrefixLength"), mtu=mtus.get(idx),
                               is_virtual="Virtual" in (r.get("InterfaceDescription") or "")
                               or "Tunnel" in (r.get("InterfaceDescription") or ""),
                               has_default_route="0.0.0.0/0" in prefixes, route_prefixes=prefixes))
        return out

    def wsl_adapter(self) -> Optional[Adapter]:
        items = self.adapters()
        for a in items:
            if a.up and a.description == WSL_DESC and a.name.startswith(WSL_NAME_PREFIX):
                return a
        net = ipaddress.ip_network(WSL_FALLBACK_NET)
        for a in items:                                       # 退化规则(04 §2.6.3「找不到则…」)
            if a.up and a.is_virtual and a.ipv4:
                try:
                    if ipaddress.ip_address(a.ipv4) in net:
                        return a
                except ValueError:
                    continue
        return None

    def proxy(self) -> ProxyInfo:
        require_windows("代理识别")
        winhttp = None
        out = run_text(["netsh", "winhttp", "show", "proxy"], timeout=10).stdout or ""
        for line in out.splitlines():
            if ":" in line and ("Proxy Server" in line or "代理服务器" in line):
                v = line.split(":", 1)[1].strip()
                winhttp = v if v and "direct" not in v.lower() and "(none)" not in v.lower() else None
        user, pac = {}, None
        import winreg
        sid = None
        try:
            import win32ts
            s = win32ts.WTSGetActiveConsoleSessionId()
            user_name = win32ts.WTSQuerySessionInformation(None, s, win32ts.WTSUserName)
            import win32security
            sid = win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, user_name)[0])
        except Exception:
            sid = None
        if sid:
            try:
                with winreg.OpenKey(winreg.HKEY_USERS,
                                    rf"{sid}\Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
                    for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
                        try:
                            user[name] = winreg.QueryValueEx(k, name)[0]
                        except OSError:
                            pass
            except OSError:
                pass
        pac = user.get("AutoConfigURL") or None
        policy = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
                policy = int(winreg.QueryValueEx(k, "ProxySettingsPerUser")[0])
        except OSError:
            policy = None
        return ProxyInfo(winhttp=winhttp, wininet_user=user, pac=pac, policy_per_user=policy)

    def has_default_route(self) -> bool:
        return any(a.up and a.has_default_route for a in self.adapters())
