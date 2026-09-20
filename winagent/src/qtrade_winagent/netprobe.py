"""``netprobe`` 模块(**服务**;02 §2.4 / 04 §2.6 本地网络 / §2.8 连通性探测规范)。

四件事:
1. **逐级探测** ``dns → tcp → tls → http/proto``,记录到达的最深一级与 00 §8.5 九值结论(+``SKIPPED``,C-18),落 ``probe_results``。
2. **``net_state`` 判定**(04 §2.6.6 伪代码逐行实现,五值)+ 代理/VPN 识别。
3. **WSL 子网跟踪与 17610 重绑**:监听集合 = ``{127.0.0.1} ∪ {vEthernet (WSL) 当前 IPv4}``,**不绑 0.0.0.0**(00 §3);
   子网变化 ⇒ 关旧 listener → 在新地址 listen → **更新防火墙规则** → 写 ``host.json`` → 推 ``WSL_SUBNET_CHANGED``。
4. **17610 防火墙规则的唯一拥有者**:``POST /firewall/ensure``(#17)幂等建/修、``DELETE /firewall``(#45)幂等删。
   🔴 **Agent 不管防火墙**;规则名固定 ``QTrade-WinAgent-17610-from-WSL``;``Program`` 限**服务 exe**(R-14),
   会话代理 exe **不开任何入站口**、绝不出现在规则里。

``seq``(#13 ``GET /wa/v1/net``)是**单调递增的网络状态序号**(R3-5):C-03 单向化后 Agent 靠轮询它感知网络翻转,
缺了它网络翻转彻底丢失 —— 所以任何影响 ``net_state``/子网/代理/VPN 的变化都必须 ``bump_seq()``。
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import alerts as A
from .alerts import AlertBuffer
from .backends import Adapter, FirewallBackend, FirewallRuleSpec, NetBackend, ProbeBackend
from .config import NetConfig, ProbeConfig
from .db import Db
from .errors import INVALID_ARGS, WaError
from .ids import run_id as new_run_id
from .logfmt import get_logger

log = get_logger("netprobe")

# 02 §3.2 probe_results.target CHECK(十个目标,04 §2.8.2 与基线枚举一一对应)
PROBE_TARGETS = ("apk_url", "mail_pop3", "mail_imap", "mail_smtp", "qidian_msf", "qq_servers", "wechat_servers",
                 "docker_registry", "winagent_from_wsl", "agent_from_windows")
# 04 §2.1:哪些目标由 Windows 侧探(其余归 Agent,经 #16 PUT /probes 回写)
WINDOWS_SIDE_TARGETS = ("wechat_servers", "agent_from_windows")
SIDES = ("windows", "wsl", "container")
TRIGGERS = ("install", "boot", "periodic", "net_event", "resume", "manual", "wizard")
LEVELS = ("none", "dns", "tcp", "tls", "http", "proto")
RESULTS = ("OK", "DNS_FAIL", "TCP_TIMEOUT", "TCP_REFUSED", "TLS_FAIL", "HTTP_4XX", "HTTP_5XX",
           "PROXY_REQUIRED", "BLOCKED_BY_POLICY", "SKIPPED")
NET_STATES = ("DIRECT", "SYSTEM_PROXY", "VPN_ACTIVE", "VPN_ACTIVE_WITH_PROXY", "OFFLINE")


@dataclass
class ProbeTargetSpec:
    target: str
    host: str
    port: Optional[int]
    side: str = "windows"
    tls: bool = False
    http_path: Optional[str] = None


@dataclass
class NetState:
    seq: int = 0
    net_state: str = "DIRECT"
    wsl_subnet: Optional[str] = None
    host_ip: Optional[str] = None
    agent_base_url: Optional[str] = None
    vpn_adapters: list[str] = field(default_factory=list)
    mtu: dict[str, Optional[int]] = field(default_factory=dict)
    listen: tuple[str, ...] = ("127.0.0.1",)


class NetProbe:
    def __init__(self, db: Db, net: NetBackend, firewall: FirewallBackend, probe: ProbeBackend,
                 net_cfg: NetConfig, probe_cfg: ProbeConfig, alerts: AlertBuffer, *,
                 svc_exe: str, port: int = 17610, agent_port: int = 17600,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._net = net
        self._fw = firewall
        self._probe = probe
        self._ncfg = net_cfg
        self._pcfg = probe_cfg
        self._alerts = alerts
        self._svc_exe = svc_exe                      # 04 §2.6.3:规则 Program 限**服务 exe**(R-14)
        self._port = port
        self._agent_port = agent_port
        self._clock = clock
        self.state = NetState()

    # ---------------------------------------------------------------- VPN / 代理 / net_state
    def vpn_adapters(self) -> list[Adapter]:
        """04 §2.6.5:``Status=Up`` 的非物理网卡,描述命中 ``vpn_adapter_patterns``,或有默认路由/≥/8 大段路由。"""
        out = []
        for a in self._net.adapters():
            if not a.up or not a.is_virtual:
                continue
            if any(p.lower() in a.description.lower() or p.lower() in a.name.lower() for p in self._ncfg.vpn_adapter_patterns):
                out.append(a)
                continue
            if a.has_default_route or any(_prefix_len(p) <= 16 for p in a.route_prefixes):
                out.append(a)
        return out

    def _proxy_active(self) -> bool:
        """04 §2.6.6:**机器级/策略级**才算系统代理;仅 HKCU 的用户级 WinINET **不算**。"""
        p = self._net.proxy()
        if p.winhttp:
            return True
        if p.pac:
            return True
        wi = p.wininet_user or {}
        return bool(wi.get("ProxyEnable") == 1 and p.policy_per_user == 0)

    async def net_state(self) -> str:
        """04 §2.6.6 判定算法(逐行照抄伪代码)。"""
        has_default_route = self._net.has_default_route()
        vpn = bool(self.vpn_adapters())
        proxy = self._proxy_active()
        if not has_default_route:
            return "OFFLINE"
        reachable = False
        for hp in self._ncfg.reachability_probe:
            host, _, port = hp.partition(":")
            step = await self._probe.tcp(host, int(port or 443), float(self._ncfg.reachability_timeout_s))
            if step.ok:
                reachable = True
                break
        if not reachable and not proxy:              # 有路由但出不去 = 离线;有代理时不能这么判(直连本来就该失败)
            return "OFFLINE"
        if vpn and proxy:
            return "VPN_ACTIVE_WITH_PROXY"
        if vpn:
            return "VPN_ACTIVE"
        if proxy:
            return "SYSTEM_PROXY"
        return "DIRECT"

    def bump_seq(self) -> int:
        """R3-5:任何影响网络状态的变化都要递增 ``seq`` —— Agent 轮询 ``seq`` 变化才会重跑探测。"""
        self.state.seq += 1
        return self.state.seq

    async def refresh(self) -> NetState:
        """重算 ``net_state`` / VPN / 子网 / MTU;有实质变化则 ``bump_seq()`` 并推 ``NET_STATE_CHANGED``。"""
        old_state, old_subnet = self.state.net_state, self.state.wsl_subnet
        ns = await self.net_state()
        wsl = self._net.wsl_adapter()
        subnet = wsl_subnet_cidr(wsl) if wsl else None
        vpns = [a.name for a in self.vpn_adapters()]
        mtu = {"vpn": next((a.mtu for a in self.vpn_adapters()), None), "eth0": wsl.mtu if wsl else None}
        self.state.net_state, self.state.wsl_subnet = ns, subnet
        self.state.vpn_adapters, self.state.mtu = vpns, mtu
        self.state.host_ip = wsl.ipv4 if wsl else None
        self.state.agent_base_url = f"http://{wsl.ipv4}:{self._agent_port}" if wsl and wsl.ipv4 else None
        self._alerts.net_offline = (ns == "OFFLINE")
        if ns == "OFFLINE":
            self._alerts.firing(A.NET_OFFLINE, subject="host", evidence={"net_state": ns})
        else:
            self._alerts.resolve(A.NET_OFFLINE, subject="host")
        if ns != old_state or subnet != old_subnet:
            self.bump_seq()
            self._alerts.firing(A.NET_STATE_CHANGED, subject="host",
                                evidence={"net_state": ns, "changed_from": old_state, "wsl_subnet": subnet})
        return self.state

    def snapshot(self) -> dict[str, Any]:
        """#13 ``GET /wa/v1/net`` 的响应体(字段名逐字照 02 §3.6)。"""
        p = self._net.proxy()
        return {"seq": self.state.seq, "net_state": self.state.net_state,
                "proxy": {"winhttp": p.winhttp, "wininet_user": p.wininet_user, "pac": p.pac, "policy": p.policy_per_user},
                "vpn_adapters": list(self.state.vpn_adapters), "wsl_subnet": self.state.wsl_subnet,
                "host_ip": self.state.host_ip, "agent_base_url": self.state.agent_base_url, "mtu": dict(self.state.mtu)}

    # ---------------------------------------------------------------- 监听集合与重绑(H16 / WSL_SUBNET_CHANGED)
    def desired_listen(self) -> tuple[str, ...]:
        """监听集合 = ``{127.0.0.1} ∪ {vEthernet (WSL) 当前 IPv4}``;**不绑 0.0.0.0**(00 §3 / 04 §2.6.3)。

        WSL 未启动时 vEthernet 可能不存在或没地址 ⇒ 只绑 ``127.0.0.1``,等适配器事件。
        """
        addrs = [self._ncfg.listen_loopback]
        if self._ncfg.listen_wsl_adapter:
            a = self._net.wsl_adapter()
            if a and a.ipv4:
                addrs.append(a.ipv4)
        return tuple(addrs)

    def reconcile_listen(self, *, actual: tuple[str, ...]) -> dict[str, Any]:
        """H16:期望与实际不一致 ⇒ 告警 + 返回需要重绑的地址;子网真的变了再推 ``WSL_SUBNET_CHANGED`` + 刷防火墙。"""
        want = self.desired_listen()
        changed_subnet = self.state.wsl_subnet
        ok = set(want) == set(actual)
        self._alerts_check_bind(want, actual)
        result: dict[str, Any] = {"desired": list(want), "actual": list(actual), "ok": ok, "rebind": []}
        if not ok:
            result["rebind"] = [a for a in want if a not in actual]
            fw = self.firewall_ensure()
            result["firewall"] = fw
            self.bump_seq()
            self._alerts.firing(A.WSL_SUBNET_CHANGED, subject="host",
                                evidence={"wsl_subnet": changed_subnet, "listen": list(want)})
        return result

    def _alerts_check_bind(self, want: tuple[str, ...], actual: tuple[str, ...]) -> None:
        if set(want) == set(actual):
            self._alerts.resolve(A.H16_WINAGENT_BIND_MISMATCH, subject="host")
        else:
            self._alerts.firing(A.H16_WINAGENT_BIND_MISMATCH, subject="host",
                                evidence={"expected": sorted(want), "actual": sorted(actual)})

    def host_json(self) -> dict[str, Any]:
        """04 §2.6.3 备用路径:会话代理把它写进 WSL 的 ``/run/qtrade/host.json``,Agent 首选读它。

        键名与 Agent 侧 ``winagent_client.resolve_base_url`` 认的两个键对齐(``winagent_base_url`` / ``host_ip``)。
        """
        ip = self.state.host_ip
        return {"host_ip": ip, "winagent_base_url": f"http://{ip}:{self._port}" if ip else None,
                "wsl_subnet": self.state.wsl_subnet, "seq": self.state.seq, "written_ms": self._clock()}

    # ---------------------------------------------------------------- #17 / #45 防火墙(唯一拥有者)
    def _rule_spec(self, *, lan: bool = False) -> FirewallRuleSpec:
        subnet = self.state.wsl_subnet or self._ncfg.wsl_subnet_fallback      # 取不到当前子网才退化 172.16.0.0/12
        alias = (self._net.wsl_adapter().name if self._net.wsl_adapter() else "vEthernet (WSL)")
        return FirewallRuleSpec(name=self._ncfg.firewall_rule_winagent, program=self._svc_exe, local_port=self._port,
                                interface_alias=alias, remote_address=subnet)

    def firewall_ensure(self, *, lan: bool = False, lan_remote: Optional[str] = None) -> dict[str, Any]:
        """#17:幂等建/更新 ``QTrade-WinAgent-17610-from-WSL``;``{lan:true}`` 时另建 ``QTrade-Agent-17600-LAN``。

        返回 ``{created|updated|unchanged|blocked_by_policy, rule_name, remote_address}``(04 §2.6.3 逐字)。
        """
        spec = self._rule_spec()
        result = self._fw.put_rule(spec)
        out: dict[str, Any] = {"result": result, "rule_name": spec.name, "remote_address": spec.remote_address}
        if lan:
            lan_spec = FirewallRuleSpec(name=self._ncfg.firewall_rule_agent_lan, program=self._svc_exe,
                                        local_port=self._agent_port, interface_alias=spec.interface_alias,
                                        remote_address=lan_remote or self._ncfg.wsl_subnet_fallback)
            out["lan"] = {"result": self._fw.put_rule(lan_spec), "rule_name": lan_spec.name,
                          "remote_address": lan_spec.remote_address}
        if result == "blocked_by_policy":
            log.warning("firewall blocked by policy", extra={"op": "firewall.ensure", "code": result})
        return out

    def firewall_delete(self, *, lan: bool = False) -> dict[str, Any]:
        """#45:按固定规则名删(与 #17 成对),幂等 → ``{result:'removed|absent|blocked_by_policy'}``。"""
        out = {"result": self._fw.delete_rule(self._ncfg.firewall_rule_winagent),
               "rule_name": self._ncfg.firewall_rule_winagent}
        if lan:
            out["lan"] = {"result": self._fw.delete_rule(self._ncfg.firewall_rule_agent_lan),
                          "rule_name": self._ncfg.firewall_rule_agent_lan}
        return out

    # ---------------------------------------------------------------- #14 / #15 / #16 探测
    async def probe_one(self, spec: ProbeTargetSpec) -> dict[str, Any]:
        """逐级探测,失败即停,记录到达的最深一级(04 §2.8.1)。"""
        level, result, latency, detail = "none", "OK", 0, None
        step = await self._probe.dns(spec.host, float(self._pcfg.dns_timeout_s))
        latency += step.latency_ms
        if not step.ok:
            return _row(spec, "none", step.result or "DNS_FAIL", latency, step.detail)
        level = "dns"
        if spec.port is not None:
            step = await self._probe.tcp(spec.host, spec.port, float(self._pcfg.tcp_timeout_s))
            latency += step.latency_ms
            if not step.ok:
                return _row(spec, level, step.result or "TCP_TIMEOUT", latency, step.detail)
            level = "tcp"
        if spec.tls:
            step = await self._probe.tls(spec.host, spec.port or 443, float(self._pcfg.tls_timeout_s))
            latency += step.latency_ms
            if not step.ok:
                return _row(spec, level, step.result or "TLS_FAIL", latency, step.detail)
            level = "tls"
        if spec.http_path:
            url = f"{'https' if spec.tls else 'http'}://{spec.host}:{spec.port}{spec.http_path}"
            step = await self._probe.http("GET", url, float(self._pcfg.http_timeout_s))
            latency += step.latency_ms
            if not step.ok:
                return _row(spec, level, step.result or "HTTP_5XX", latency, step.detail)
            level = "http"
        return _row(spec, level, result, latency, detail)

    async def probe_run(self, specs: list[ProbeTargetSpec], *, trigger: str, run_id: Optional[str] = None,
                        detail_prefix: str = "") -> str:
        """#14 ``POST /wa/v1/probe``:跑 **Windows 侧**目标 → ``202 {run_id}``。

        ⚠️ WinAgent **不**反向转发 WSL 侧(C-03);WSL/容器侧目标由 Agent 自跑后经 #16 ``PUT /probes`` 回写,
        或在「Agent 不可达」的安装期由本函数记 ``SKIPPED``(detail=``agent_unreachable``)。
        """
        if trigger not in TRIGGERS:
            raise WaError(INVALID_ARGS, f"trigger 必须是 {TRIGGERS} 之一", reason="bad_trigger")
        rid = run_id or new_run_id()
        rows = []
        for spec in specs:
            if spec.side != "windows":
                rows.append(_row(spec, "none", "SKIPPED", 0, (detail_prefix + "agent_unreachable")[:200]))
                continue
            rows.append(await self.probe_one(spec))
        self.write_results(rid, rows, trigger=trigger)
        return rid

    def write_results(self, run_id: str, rows: list[dict[str, Any]], *, trigger: str) -> int:
        """落 ``probe_results``;#16 ``PUT /wa/v1/probes``(Agent 回写 WSL/容器侧)也走本函数。"""
        now = self._clock()
        ns = self.state.net_state
        n = 0
        with self._db.tx() as con:
            for r in rows:
                if r["target"] not in PROBE_TARGETS or r["side"] not in SIDES or r["result"] not in RESULTS \
                        or r["level_reached"] not in LEVELS:
                    raise WaError(INVALID_ARGS, f"探测结果字段不在枚举内:{r}", reason="bad_enum")
                con.execute(
                    "INSERT INTO probe_results(run_id, ts_ms, trigger, target, side, host, port, level_reached, result, "
                    "latency_ms, net_state, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, r.get("ts_ms") or now, trigger, r["target"], r["side"], r["host"], r.get("port"),
                     r["level_reached"], r["result"], r.get("latency_ms"), ns, (r.get("detail") or None)))
                n += 1
        return n

    def read_results(self, *, run_id: Optional[str] = None, latest: bool = False) -> list[dict[str, Any]]:
        """#15 ``GET /wa/v1/probes?run_id=`` 或 ``?latest=1``。"""
        if run_id:
            return self._db.query("SELECT * FROM probe_results WHERE run_id=? ORDER BY id", (run_id,))
        if latest:
            row = self._db.one("SELECT run_id FROM probe_results ORDER BY ts_ms DESC, id DESC LIMIT 1")
            return self._db.query("SELECT * FROM probe_results WHERE run_id=? ORDER BY id", (row["run_id"],)) if row else []
        return self._db.query("SELECT * FROM probe_results ORDER BY ts_ms DESC LIMIT 200")

    # ---------------------------------------------------------------- C-1 实测采样(04 §2.8.4)
    async def sample_connections(self, *, pid_names: tuple[str, ...], duration_s: Optional[int] = None) -> dict[str, Any]:
        """``POST /wa/v1/probe {mode:'sample', pid_names:[…]}``:Windows 侧按 PID 过滤采远端连接。

        **只记 IP:port 与域名,不记任何载荷**(04 §2.8.4「安全」);结果落 ``probe_targets_observed``,
        **不产生 ``probe_result``**(§2.8.5 边界)。
        """
        dur = min(int(duration_s or self._pcfg.sample_duration_s), 30)     # 上限 30
        conns = await self._probe.connections(pid_names, dur)
        now = self._clock()
        rid = new_run_id()
        with self._db.tx() as con:
            for c in conns:
                con.execute(
                    "INSERT INTO probe_targets_observed(account_id, channel, remote_host, remote_ip, remote_port, proto, "
                    "side, first_seen_ms, last_seen_ms, hits) VALUES (?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(COALESCE(account_id,''), remote_ip, remote_port, proto) DO UPDATE SET "
                    "last_seen_ms=excluded.last_seen_ms, hits=hits+1, "
                    "remote_host=COALESCE(excluded.remote_host, probe_targets_observed.remote_host)",
                    (c.get("account_id"), c.get("channel"), c.get("hostname"), c["ip"], int(c["port"]),
                     c.get("proto", "tcp"), "windows", now, now))
        return {"run_id": rid, "sampled_at": now, "duration_s": dur,
                "candidates": [{"channel": c.get("channel"), "hostname": c.get("hostname"), "ip": c["ip"],
                                "port": int(c["port"]), "samples": 1} for c in conns]}


def _row(spec: ProbeTargetSpec, level: str, result: str, latency_ms: int, detail: Optional[str]) -> dict[str, Any]:
    return {"target": spec.target, "side": spec.side, "host": spec.host, "port": spec.port,
            "level_reached": level, "result": result, "latency_ms": latency_ms,
            "detail": (detail or None) if detail is None else str(detail)[:200]}      # detail ≤ 200(02 §3.2 CHECK)


def _prefix_len(cidr: str) -> int:
    try:
        return ipaddress.ip_network(cidr, strict=False).prefixlen
    except ValueError:
        return 32


def wsl_subnet_cidr(a: Adapter) -> Optional[str]:
    """由 vEthernet (WSL) 的 IPv4 + 前缀长度算出子网(防火墙 ``RemoteAddress`` 用当前子网,04 §2.6.3)。"""
    if not a.ipv4 or a.prefix_length is None:
        return None
    try:
        return str(ipaddress.ip_network(f"{a.ipv4}/{a.prefix_length}", strict=False))
    except ValueError:
        return None
