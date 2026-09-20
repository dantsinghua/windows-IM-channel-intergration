"""``WinFirewall`` —— 17610 入站规则的唯一拥有者(04 §2.6.3)。

幂等语义逐字:按固定名查 —— 不存在则建;存在则把 ``Program``/``LocalPort``/``InterfaceAlias``/``RemoteAddress``/``Profile``
逐项与期望值比对,任一不同就 ``Set-NetFirewallRule`` 更新,全同则不动(**不重复建、不删重建**,避免策略环境下规则计数抖动)。

组策略接管时:``New-NetFirewallRule`` 成功但 ``Get-NetFirewallRule -PolicyStore ActiveStore`` 里看不到
⇒ 判 ``blocked_by_policy``(不是异常,是一种结论)。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

from ..backends import FirewallRuleSpec
from . import require_windows
from .netinfo import PS


def _ps(script: str, timeout: int = 20) -> tuple[int, str, str]:
    p = subprocess.run(PS + [script], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


class WinFirewall:
    def get_rule(self, name: str) -> Optional[FirewallRuleSpec]:
        require_windows("Get-NetFirewallRule")
        rc, out, _ = _ps(
            f"$r=Get-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue; if($r){{"
            f"$a=$r|Get-NetFirewallApplicationFilter; $p=$r|Get-NetFirewallPortFilter; "
            f"$i=$r|Get-NetFirewallInterfaceFilter; $ad=$r|Get-NetFirewallAddressFilter; "
            f"[pscustomobject]@{{program=$a.Program;port=$p.LocalPort;protocol=$p.Protocol;"
            f"alias=$i.InterfaceAlias;remote=$ad.RemoteAddress;profile=$r.Profile;"
            f"direction=$r.Direction;action=$r.Action}}|ConvertTo-Json -Compress}}")
        if rc != 0 or not out:
            return None
        d = json.loads(out)
        return FirewallRuleSpec(name=name, program=str(d.get("program") or ""), local_port=int(d.get("port") or 0),
                                interface_alias=_first(d.get("alias")), remote_address=_first(d.get("remote")),
                                protocol=str(d.get("protocol") or "TCP"), direction=str(d.get("direction") or "Inbound"),
                                action=str(d.get("action") or "Allow"), profile=str(d.get("profile") or "Any"))

    def put_rule(self, spec: FirewallRuleSpec) -> str:
        require_windows("New/Set-NetFirewallRule")
        cur = self.get_rule(spec.name)
        if cur is not None and cur == spec:
            return "unchanged"
        common = (f"-Program '{spec.program}' -LocalPort {spec.local_port} -Protocol {spec.protocol} "
                  f"-InterfaceAlias '{spec.interface_alias}' -RemoteAddress '{spec.remote_address}' "
                  f"-Profile {spec.profile}")
        if cur is None:
            rc, _, err = _ps(f"New-NetFirewallRule -DisplayName '{spec.name}' -Direction {spec.direction} "
                             f"-Action {spec.action} {common}")
            verdict = "created"
        else:
            rc, _, err = _ps(f"Set-NetFirewallRule -DisplayName '{spec.name}' {common}")
            verdict = "updated"
        if rc != 0:
            return "blocked_by_policy"
        rc2, out2, _ = _ps(f"Get-NetFirewallRule -DisplayName '{spec.name}' -PolicyStore ActiveStore "
                           f"-ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty DisplayName")
        if rc2 != 0 or not out2:
            return "blocked_by_policy"          # 建成功却不在 ActiveStore ⇒ 组策略接管(04 §2.6.3 最后一段)
        return verdict

    def delete_rule(self, name: str) -> str:
        require_windows("Remove-NetFirewallRule")
        if self.get_rule(name) is None:
            return "absent"
        rc, _, _ = _ps(f"Remove-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue")
        if rc != 0 or self.get_rule(name) is not None:
            return "blocked_by_policy"
        return "removed"


def _first(v: Any) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v or "")
