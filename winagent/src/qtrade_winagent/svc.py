"""``svc`` —— WinAgent **服务**(LocalSystem)的 FastAPI 装配:``/wa/v1`` 全量端点、令牌鉴权、监听绑定规则。

- **HTTP 只在服务里**(02 §2.4):``uvicorn``(``ws="websockets"``),监听 ``{127.0.0.1} ∪ {vEthernet (WSL) 当前 IPv4}``,
  **不绑 ``0.0.0.0``**(00 §3)。绑定集合由 ``netprobe.desired_listen()`` 算,子网变化时 H16 自愈重绑。
- **鉴权**(02 §3.6 表头):``A`` = Agent 令牌、``C`` = 控制台令牌、``I`` = 安装器令牌(安装完即吊销)、``—`` = 无鉴权。
  端点 × 令牌矩阵逐行照 §3.6 的「令牌」列,写在 ``ROUTES`` 里,**改矩阵只改那一张表**。
- **执行体**:``svc`` 直接执行;``user`` 经 §2.4.1 管道转会话代理,**会话代理不在线一律 ``503 NOT_READY``**。
  混合端点(``svc+user``)按 R3-15 拆两半、失败带 ``stage``/``partial``。
- 每个响应带 ``X-WA-Version``(§3.8);**所有调用记 ``wa_audit_log``**(§3.6 表头最后一句)。
- 🔴 探活 ``ping``/``health`` **由服务直接回答,绝不穿管道**(R3-1);``user_agent`` 取自 ``PipeHub`` 的心跳状态。
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from . import API_VERSION, __version__
from . import alerts as A
from .alerts import AlertBuffer
from .audit import ACTOR_AGENT, ACTOR_CONSOLE, ACTOR_INSTALLER, Audit
from .config import WinAgentConfig, write_toml_keys
from .db import Db
from .errors import FORBIDDEN, INTERNAL, INVALID_ARGS, TARGET_NOT_FOUND, UNAUTHORIZED, WaError, user_agent_offline
from .ids import trace_id as new_trace_id
from .installer_ops import InstallerOps
from .logfmt import get_logger
from .monitor import Monitor
from .netprobe import PROBE_KINDS, NetProbe, ProbeTargetSpec
from .pipe import PipeHub
from .power import Power
from .vault import Vault
from .wechat import WeChatHostsBlock, WeChatStore

log = get_logger("svc")

ROLE_AGENT = "agent"
ROLE_CONSOLE = "console"
ROLE_INSTALLER = "installer"
ACTOR_BY_ROLE = {ROLE_AGENT: ACTOR_AGENT, ROLE_CONSOLE: ACTOR_CONSOLE, ROLE_INSTALLER: ACTOR_INSTALLER}

# 02 §2.5「超时」(Agent 侧的期望值;服务侧据此给管道 deadline_ms = 本值 − 1s)
TIMEOUT_S = {"ping": 2.0, "health": 2.0, "time": 3.0, "vault": 3.0, "metrics": 3.0, "alerts": 3.0, "net": 3.0,
             "probe_target": 10.0, "probe_round": 60.0, "wsl": 30.0, "wechat_read": 10.0,
             # 快捷键发送约数秒,再加上 10s 读回确认。15s 时消息已经发出,读回还没返回,服务就把会话代理判超时。
             "wechat_send": 45.0,
             "wechat_login_start": 60.0}


@dataclass
class Tokens:
    """两把令牌 + 安装器令牌(C-04/C-05)。值从 Vault 读出后常驻内存,**不写日志**。"""
    agent: Optional[str] = None
    console: Optional[str] = None
    installer: Optional[str] = None

    def role_of(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        if self.agent and token == self.agent:
            return ROLE_AGENT
        if self.console and token == self.console:
            return ROLE_CONSOLE
        if self.installer and token == self.installer:
            return ROLE_INSTALLER
        return None

    def revoke_installer(self) -> None:
        """03:安装完即吊销安装器令牌。"""
        self.installer = None


@dataclass
class SvcDeps:
    """服务装配需要的一组模块;测试里逐个注入假后端版本。"""
    cfg: WinAgentConfig
    db: Db
    audit: Audit
    vault: Vault
    monitor: Monitor
    netprobe: NetProbe
    power: Power
    hub: PipeHub
    wechat_store: WeChatStore
    hosts_block: WeChatHostsBlock
    installer: InstallerOps
    alerts: AlertBuffer
    tokens: Tokens
    agent_id: str = "winagent"
    clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    listen: tuple[str, ...] = ("127.0.0.1",)
    started_ms: int = 0
    #: `winagent.toml` 的落点(R6-88:#43 受控子集改动要写回这里 —— C-43「enabled 全系统唯一真值在 toml」,
    #: 此前只改内存 + DB 快照,服务一重启就丢;None = 不落盘(测试 / --dev)
    config_path: Optional[str] = None
    #: R6-89:服务侧(提权/SYSTEM)结束讲述人。讲述人跑在高完整性级别,会话代理 taskkill 它是拒绝访问;
    #: #33 响应带 `narrator.stop_pending=true` / cancel 带 `narrator_stop_pending=true` 时由服务调用。None = 不具备(测试默认)
    narrator_killer: Optional[Callable[[], bool]] = None


def _client_host(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _is_local_or_wsl(host: Optional[str], wsl_subnet: Optional[str]) -> bool:
    """#11 ``vault/read`` 「只接受 loopback / WSL 子网来源」(02 §3.6;05 §2.2.4)。"""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if wsl_subnet:
        try:
            return ip in ipaddress.ip_network(wsl_subnet, strict=False)
        except ValueError:
            return False
    return False


def build_app(d: SvcDeps) -> FastAPI:                       # noqa: C901 —— 端点矩阵天然长,拆开反而看不出对应关系
    app = FastAPI(title="QTrade WinAgent", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    d.started_ms = d.started_ms or d.clock()

    # ------------------------------------------------------------------ 横切:X-WA-Version + trace + 审计 + 错误信封
    @app.middleware("http")
    async def _wrap(request: Request, call_next):           # type: ignore[no-untyped-def]
        request.state.trace_id = request.headers.get("X-Trace-Id") or new_trace_id(d.clock())
        try:
            resp = await call_next(request)
        except WaError as e:                                 # 兜底(路由内一般已自行转换)
            resp = JSONResponse(e.body(request.state.trace_id), status_code=e.http_status)
        resp.headers["X-WA-Version"] = __version__
        return resp

    def fail(e: WaError, request: Request) -> JSONResponse:
        return JSONResponse(e.body(getattr(request.state, "trace_id", None)), status_code=e.http_status)

    def auth(request: Request, allow: tuple[str, ...], *, action: str, target: Optional[str] = None) -> str:
        """按 §3.6「令牌」列校验;记一行 ``wa_audit_log``。返回 role。"""
        raw = request.headers.get("Authorization") or ""
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else None
        role = d.tokens.role_of(token)
        trace = getattr(request.state, "trace_id", None)
        if role is None:
            d.audit.record(actor="system", action=action, target=target, result=UNAUTHORIZED,
                           ip=_client_host(request), trace_id=trace)
            raise WaError(UNAUTHORIZED, "缺少或无效的 Bearer 令牌", reason="bad_token")
        if role not in allow:
            d.audit.record(actor=ACTOR_BY_ROLE[role], action=action, target=target, result=FORBIDDEN,
                           ip=_client_host(request), trace_id=trace)
            raise WaError(FORBIDDEN, f"该端点只接受 {allow} 令牌", reason="token_role_not_allowed")
        d.audit.record(actor=ACTOR_BY_ROLE[role], action=action, target=target, ip=_client_host(request), trace_id=trace)
        return role

    async def via_pipe(method: str, params: dict[str, Any], *, timeout_s: float, request: Request,
                       target: Optional[str] = None) -> Any:
        """纯 ``user`` 端点:一一映射下发管道(02 §2.4.1「帧格式」行)。离线 → 503 NOT_READY。"""
        return await d.hub.call(method, params, timeout_s=timeout_s,
                                trace_id=getattr(request.state, "trace_id", None), target=target)

    async def _sync_wechat_module_on_holder(s) -> None:                    # type: ignore[no-untyped-def]
        """R6-88:会话代理一上线就把服务侧的微信模块开关(C-43 真值)推过去。
        否则会话代理只认它自己启动时读到的 toml:用户登录桌面前控制台已经启用过模块、或服务/会话代理任一方重启过,
        两边就各说各话 —— 控制台看到「微信模块:未启用 / 尚未确认已启用」,而服务 health 又说 enabled。"""
        await d.hub.call("wechat.module", {"enabled": d.cfg.wechat.enabled}, timeout_s=TIMEOUT_S["wsl"], trace_id=None)

    if _sync_wechat_module_on_holder not in d.hub.on_holder:
        d.hub.on_holder.append(_sync_wechat_module_on_holder)
    d.hub.welcome_extras = lambda: {"wechat_enabled": d.cfg.wechat.enabled}   # 握手即对齐(主路径);上面的推送是兜底

    # ================================================================== #1 ping(—,svc)
    @app.get("/wa/v1/ping")
    async def ping() -> dict[str, Any]:
        """无鉴权,故**只回这些**(04 §2.8.2 ``winagent_from_wsl`` 就探它,判据 = 200 且 body 里 ``agent_id`` 匹配)。"""
        return {"agent_id": d.agent_id, "version": __version__, "time": d.clock(), "listen": list(d.listen)}

    # ================================================================== #2 health(A/C,svc)
    @app.get("/wa/v1/health")
    async def health(request: Request):                      # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="health")
        except WaError as e:
            return fail(e, request)
        wechat_mod = "enabled" if d.cfg.wechat.enabled else "disabled"
        if d.cfg.wechat.enabled and not d.hub.user_agent_online:
            wechat_mod = "offline"                           # 会话代理不在线 ⇒ 微信模块 offline(#2 枚举第三值)
        return {"ok": True, "version": __version__, "api_version": API_VERSION,
                "uptime_s": max(0, (d.clock() - d.started_ms) // 1000),
                "user_agent": d.hub.user_agent_online,       # 🔴 取自服务维护的心跳状态,不穿管道(R3-1)
                "user_session": d.hub.user_session_view(),
                "modules": {"vault": "ok", "monitor": "ok", "netprobe": "ok", "power": "ok",
                            "wslctl": "ok" if d.hub.user_agent_online else "offline", "wechat": wechat_mod},
                "checks": d.monitor.health_checks(),        # R6-58 (ao):恒八键,没跑过的给 None(不是动态字典)
                "host": d.monitor.host_snapshot()}

    # ================================================================== #3 version(A/C,svc)
    @app.get("/wa/v1/version")
    async def version(request: Request):                     # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="version")
        except WaError as e:
            return fail(e, request)
        wsl_version = None
        if d.hub.user_agent_online:
            try:
                wsl_version = await via_pipe("wsl.version", {}, timeout_s=TIMEOUT_S["wsl"], request=request)
            except WaError:
                wsl_version = None                           # 会话代理不在线则该字段 null(#3 逐字)
        h = d.hub.holder
        return {"svc_version": __version__, "user_agent_version": h.version if h else None,
                "api_version": API_VERSION, "wsl_version": wsl_version}

    # ================================================================== #4 time(A,svc)
    @app.get("/wa/v1/time")
    async def wa_time(request: Request):                     # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="time")
        except WaError as e:
            return fail(e, request)
        sysb = d.monitor._sys                                 # noqa: SLF001 —— monitor 拥有 SysBackend,这里只读
        return {"now_ms": d.clock(), "tz_offset_min": sysb.tz_offset_min(),
                "last_resume_ms": sysb.last_resume_ms(),      # R3-5:Agent 靠轮询它感知主机唤醒
                "w32time": sysb.w32time()}

    # ================================================================== #5 metrics(A,svc)
    @app.get("/wa/v1/metrics")
    async def metrics(request: Request, scope: Optional[str] = None, subject: Optional[str] = None,
                      since: Optional[int] = None, until: Optional[int] = None, resolution: str = "raw"):  # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="metrics")
        except WaError as e:
            return fail(e, request)
        return {"samples": d.monitor.metrics(scope=scope, subject=subject, since=since, until=until,
                                             resolution=resolution)}

    # ================================================================== #6 alerts(A,svc)
    @app.get("/wa/v1/alerts")
    async def get_alerts(request: Request, since: int = 0, limit: int = 500):    # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="alerts")
        except WaError as e:
            return fail(e, request)
        return d.alerts.pull(since=since, limit=limit)

    # ================================================================== #7~#12 vault
    @app.get("/wa/v1/vault")
    async def vault_list(request: Request, scope: Optional[str] = None):          # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="vault.list")
        except WaError as e:
            return fail(e, request)
        return {"items": d.vault.list(scope=scope)}

    @app.post("/wa/v1/vault/{name:path}/read")
    async def vault_read(name: str, request: Request):                            # type: ignore[no-untyped-def]
        """#11:**仅 Agent 令牌**;控制台 → 403;只接受 loopback / WSL 子网来源;**须带 ``X-Trace-Id``**(C-06)。"""
        try:
            auth(request, (ROLE_AGENT,), action="vault.read", target=name)
            if not request.headers.get("X-Trace-Id"):
                raise WaError(INVALID_ARGS, "读密钥必须带 X-Trace-Id(读有副作用,要能追溯)", reason="missing_trace_id")
            if not _is_local_or_wsl(_client_host(request), d.netprobe.state.wsl_subnet):
                raise WaError(FORBIDDEN, "读密钥只接受 loopback 或 WSL 子网来源", reason="source_not_allowed")
            value = await d.vault.read(name, trace_id=request.headers["X-Trace-Id"], owner=ACTOR_AGENT)
        except WaError as e:
            return fail(e, request)
        if value is None:
            return JSONResponse({"ok": False, "code": TARGET_NOT_FOUND}, status_code=404)
        return {"value": value}

    @app.post("/wa/v1/vault/{name:path}/flag")
    async def vault_flag(name: str, request: Request):                            # type: ignore[no-untyped-def]
        try:
            role = auth(request, (ROLE_AGENT,), action="vault.flag", target=name)
            body = await _json(request)
        except WaError as e:
            return fail(e, request)
        d.vault.flag(name, suspect=bool(body.get("suspect", True)), owner=ACTOR_BY_ROLE[role],
                     trace_id=request.state.trace_id)
        return Response(status_code=204)

    @app.head("/wa/v1/vault/{name:path}")
    async def vault_head(name: str, request: Request):                            # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="vault.head", target=name)
        except WaError as e:
            return Response(status_code=e.http_status)
        return Response(status_code=200 if d.vault.exists(name) else 404)

    @app.put("/wa/v1/vault/{name:path}")
    async def vault_put(name: str, request: Request):                             # type: ignore[no-untyped-def]
        """#9:``{value, scope}``;**请求体不进日志、响应不回显值**。"""
        try:
            role = auth(request, (ROLE_AGENT, ROLE_CONSOLE, ROLE_INSTALLER), action="vault.write", target=name)
            body = await _json(request)
            if "value" not in body:
                raise WaError(INVALID_ARGS, "缺少 value", reason="missing_value")
            await d.vault.put(name, str(body["value"]), scope=str(body.get("scope") or "other"),
                              owner=ACTOR_BY_ROLE[role], trace_id=request.state.trace_id)
        except WaError as e:
            return fail(e, request)
        return Response(status_code=204)

    @app.delete("/wa/v1/vault/{name:path}")
    async def vault_delete(name: str, request: Request):                          # type: ignore[no-untyped-def]
        try:
            role = auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="vault.delete", target=name)
        except WaError as e:
            return fail(e, request)
        d.vault.delete(name, owner=ACTOR_BY_ROLE[role], trace_id=request.state.trace_id)
        return Response(status_code=204)

    # ================================================================== #13~#16 net / probe
    @app.get("/wa/v1/net")
    async def net(request: Request):                                              # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE, ROLE_INSTALLER), action="net")
        except WaError as e:
            return fail(e, request)
        return d.netprobe.snapshot()

    @app.post("/wa/v1/probe")
    async def probe(request: Request):                                            # type: ignore[no-untyped-def]
        """#14:跑 **Windows 侧**目标 → ``202 {run_id}``;``{mode:'sample'}`` 走 C-1 实测采样(04 §2.8.4)。"""
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE, ROLE_INSTALLER), action="probe")
            body = await _json(request)
            if body.get("mode") == "sample":
                out = await d.netprobe.sample_connections(pid_names=tuple(body.get("pid_names") or ()),
                                                          duration_s=body.get("duration_s"))
                return JSONResponse(out, status_code=200)
            specs = [ProbeTargetSpec(**t) for t in (body.get("targets") or [])]
            run_id = await d.netprobe.probe_run(specs, trigger=str(body.get("trigger") or "manual"))
        except (WaError, TypeError) as e:
            if isinstance(e, TypeError):
                return fail(WaError(INVALID_ARGS, f"targets 字段不合法:{e}", reason="bad_targets"), request)
            return fail(e, request)
        return JSONResponse({"run_id": run_id}, status_code=202)

    @app.get("/wa/v1/probes")
    async def probes_get(request: Request, run_id: Optional[str] = None, latest: int = 0,
                         kind: str = "result", since: Optional[int] = None, limit: int = 200,
                         channel: Optional[str] = None, adopted: Optional[bool] = None):   # type: ignore[no-untyped-def]
        """#15 ``?run_id=`` / ``?latest=1`` 读 ``probe_results``;**``?kind=observed`` 读 ``probe_targets_observed``**。

        ``kind=observed`` 是整合裁决 (ci) 补的上游缺口:02 #76b 要 Agent 做的
        ``GET /system/probes?kind=observed``(04 §3.4)在 ``/wa/v1`` 侧原本无入口,Agent 只能回 503。
        令牌与执行体**沿用本端点(#15)的 A/C/I + svc**,不另开一格。
        行原样带 ``id``(= ``observed_ids`` 的稳定 id,裁决 (ch))与派生的 ``in_config``。
        """
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE, ROLE_INSTALLER), action=f"probes.read.{kind}")
            if kind not in PROBE_KINDS:
                raise WaError(INVALID_ARGS, f"kind 必须是 {PROBE_KINDS} 之一", reason="bad_kind")
        except WaError as e:
            return fail(e, request)
        if kind == "observed":
            return {"observed": d.netprobe.read_observed(since=since, limit=limit, channel=channel, adopted=adopted),
                    "targets": d.netprobe.probe_targets()}
        return {"results": d.netprobe.read_results(run_id=run_id, latest=bool(latest))}

    @app.put("/wa/v1/probes")
    async def probes_put(request: Request):                                       # type: ignore[no-untyped-def]
        """#16:Agent 回写探测结果(C-31:``probe_results`` 只在 winagent.db 一份)。

        ``{kind:"observed", rows:[…]}`` 时改落 ``probe_targets_observed``(04 §3.4 逐字)——
        实测采样的**唯一写入口**:WinAgent 的 ``POST /probe {mode:'sample'}`` 只返回不落库,
        三侧(容器/WSL/Windows)的候选都由 Agent 聚合后经这里写一次,否则 ``hits`` 会被算两遍。
        """
        try:
            auth(request, (ROLE_AGENT,), action="probes.write")
            body = await _json(request)
            kind = str(body.get("kind") or "result")
            if kind not in PROBE_KINDS:
                raise WaError(INVALID_ARGS, f"kind 必须是 {PROBE_KINDS} 之一", reason="bad_kind")
            if kind == "observed":
                n = d.netprobe.write_observed(list(body.get("rows") or body.get("results") or []))
                return {"written": n, "kind": "observed"}
            n = d.netprobe.write_results(str(body["run_id"]), list(body.get("results") or body.get("rows") or []),
                                         trigger=str(body.get("trigger") or "manual"))
        except KeyError as e:
            return fail(WaError(INVALID_ARGS, f"缺少字段 {e}", reason="missing_field"), request)
        except WaError as e:
            return fail(e, request)
        return {"written": n}

    @app.put("/wa/v1/probes/adopt")
    async def probes_adopt(request: Request):                                     # type: ignore[no-untyped-def]
        """**把实测采样候选采纳为正式探测目标**(整合裁决 (ci) 补的第二个上游缺口;02 #76b 的 WinAgent 半)。

        ``{observed_ids:[…]}`` → 写选中行 ``adopted_ms`` + 更新 ``settings['probe.targets']``
        → ``{adopted:[…], targets:[…], hosts_by_channel:{…}}``。
        令牌与执行体沿用同类写端点 ``#16 PUT /wa/v1/probes`` 的 **A + svc**
        (控制台经 Agent 调,不直连 17610,C-32/C-03/R-07)。
        ``observed_ids`` 是**采纳后的全集**(04 §2.8.4「替换整表不追加」),空数组 = 清空正式目标表。
        """
        try:
            auth(request, (ROLE_AGENT,), action="probes.adopt")
            body = await _json(request)
            if "observed_ids" not in body:
                raise WaError(INVALID_ARGS, "缺少 observed_ids(02 #76b 的入参;不收 targets,见整合裁决 (cg))",
                              reason="missing_observed_ids")
            raw = body["observed_ids"]
            if not isinstance(raw, list) or any(not isinstance(i, int) or isinstance(i, bool) for i in raw):
                raise WaError(INVALID_ARGS, "observed_ids 必须是整数数组(行的稳定 id = probe_targets_observed.id)",
                              reason="bad_observed_ids")
            out = d.netprobe.adopt(raw, actor=ACTOR_AGENT)
        except WaError as e:
            return fail(e, request)
        return out

    # ================================================================== #17 / #45 firewall(唯一拥有者)
    @app.post("/wa/v1/firewall/ensure")
    async def firewall_ensure(request: Request):                                  # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE, ROLE_INSTALLER), action="firewall.ensure")
            body = await _json(request)
        except WaError as e:
            return fail(e, request)
        return d.netprobe.firewall_ensure(lan=bool(body.get("lan")), lan_remote=body.get("lan_remote"))

    @app.delete("/wa/v1/firewall")
    async def firewall_delete(request: Request, lan: int = 0):                    # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE, ROLE_INSTALLER), action="firewall.delete")
        except WaError as e:
            return fail(e, request)
        return d.netprobe.firewall_delete(lan=bool(lan))

    # ================================================================== #18 / #19 power
    @app.get("/wa/v1/power")
    async def power_get(request: Request):                                        # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="power.status")
        except WaError as e:
            return fail(e, request)
        return d.power.status()

    @app.post("/wa/v1/power/keepawake")
    async def power_keepawake(request: Request):                                  # type: ignore[no-untyped-def]
        """#19 **混合端点**:``svc``(``ES_SYSTEM_REQUIRED`` + powercfg)+ ``user``(``ES_DISPLAY_REQUIRED``)。

        ``ES_DISPLAY_REQUIRED`` 在 Session 0 无效,必须由会话代理发;会话代理不在线时 display 半降级(不整单失败)。
        """
        try:
            auth(request, (ROLE_CONSOLE,), action="power.keepawake")
            body = await _json(request)
            out = d.power.apply(str(body.get("mode") or d.cfg.wechat.keep_awake_mode))
        except WaError as e:
            return fail(e, request)
        display = {"applied": False, "reason": "user_agent_offline"}
        if out["mode"] != "off" and d.hub.user_agent_online:
            try:
                display = await via_pipe("power.display", {"on": True}, timeout_s=TIMEOUT_S["health"], request=request)
            except WaError as e:
                display = {"applied": False, "reason": e.code}
        out["display"] = display
        return out

    # ================================================================== #20~#25 wsl(纯 user / 混合)
    @app.get("/wa/v1/wsl/status")
    async def wsl_status(request: Request):                                       # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wsl.status")
            return await via_pipe("wsl.status", {}, timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.post("/wa/v1/wsl/start")
    async def wsl_start(request: Request):                                        # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE,), action="wsl.start")
            return await via_pipe("wsl.start", {}, timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.post("/wa/v1/wsl/stop")
    async def wsl_stop(request: Request):                                         # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE,), action="wsl.stop")
            return await via_pipe("wsl.stop", {}, timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.post("/wa/v1/wsl/restart")
    async def wsl_restart(request: Request):                                      # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wsl.restart")
            body = await _json(request)
            out = await via_pipe("wsl.restart", {"mode": body.get("mode") or "terminate",
                                                 "confirm": bool(body.get("confirm"))},
                                 timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)
        return JSONResponse(out, status_code=202)

    @app.get("/wa/v1/wsl/config")
    async def wsl_config_get(request: Request):                                   # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wsl.config.get")
            return await via_pipe("wsl.config.get", {}, timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.put("/wa/v1/wsl/config")
    async def wsl_config_put(request: Request):                                   # type: ignore[no-untyped-def]
        """#25 **混合端点**:``svc``(备份目录与 ACL)+ ``user``(写文件)。白名单只四键(C-32)。"""
        try:
            auth(request, (ROLE_CONSOLE,), action="wsl.config.put")
            body = await _json(request)
            d.installer.ensure_backup_dir()                                        # 服务半
            desired = {k: v for k, v in body.items() if k in ("memory", "processors", "autoMemoryReclaim", "swap")
                       and v is not None}
            out = await via_pipe("wsl.config.put", {"desired": desired}, timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)
        if out.get("pending_restart"):
            d.alerts.firing(A.WSLCONFIG_PENDING_RESTART, subject="wsl", evidence={"changed": out.get("changed")})
        return out

    # ================================================================== #26 / #27 / #46 / #47 内核与发行版
    @app.post("/wa/v1/wsl/kernel/verify")
    async def kernel_verify(request: Request):                                    # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE, ROLE_INSTALLER), action="wsl.kernel.verify")
            return await d.installer.kernel_verify(trace_id=request.state.trace_id)
        except WaError as e:
            return fail(e, request)

    @app.post("/wa/v1/wsl/kernel/rollback")
    async def kernel_rollback(request: Request):                                  # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE, ROLE_INSTALLER), action="wsl.kernel.rollback")
            body = await _json(request)
            out = await d.installer.kernel_rollback(confirm_shutdown=bool(body.get("confirm_shutdown")),
                                                    trace_id=request.state.trace_id)
        except WaError as e:
            return fail(e, request)
        return JSONResponse(out, status_code=202)

    @app.post("/wa/v1/wsl/kernel/apply")
    async def kernel_apply(request: Request):                                     # type: ignore[no-untyped-def]
        """#46:**必须带 ``{confirm_shutdown:true}``**,否则 400(00 §11.6 [NOSHUTDOWN])。"""
        try:
            auth(request, (ROLE_CONSOLE, ROLE_INSTALLER), action="wsl.kernel.apply")
            body = await _json(request)
            out = await d.installer.kernel_apply(confirm_shutdown=bool(body.get("confirm_shutdown")),
                                                 kernel_src=body.get("kernel_src"),
                                                 trace_id=request.state.trace_id)
        except WaError as e:
            return fail(e, request)
        return JSONResponse(out, status_code=202)

    @app.post("/wa/v1/wsl/distro/repair")
    async def distro_repair(request: Request):                                    # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE, ROLE_INSTALLER), action="wsl.distro.repair")
            body = await _json(request)
            out = await via_pipe("wsl.distro.repair", {"confirm": bool(body.get("confirm"))},
                                 timeout_s=TIMEOUT_S["wsl"] * 4, request=request)
        except WaError as e:
            return fail(e, request)
        return JSONResponse(out, status_code=202)

    # ================================================================== #28~#43 wechat
    def _wechat_enabled_guard() -> None:
        if not d.cfg.wechat.enabled:
            raise WaError("NOT_READY", "微信模块未启用(winagent.toml [wechat] enabled=false)", reason="wechat_disabled")

    @app.get("/wa/v1/wechat/status")
    async def wechat_status(request: Request):                                    # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wechat.status")
            if not d.cfg.wechat.enabled:
                return {"enabled": False, "wechat": None, "chatlog": None, "ritual_done": None,
                        "screen_locked": None, "login_session": None, "hosts_block": d.hosts_block.state()}
            out = await via_pipe("wechat.status", {}, timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)
        out["hosts_block"] = d.hosts_block.state()                                 # #33b:状态并入 #28
        return out

    @app.get("/wa/v1/wechat/version-match")
    async def wechat_version_match(request: Request):                             # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE, ROLE_INSTALLER), action="wechat.version_match")
        except WaError as e:
            return fail(e, request)
        # R6-86 承接 —— 事实来源 = **本机当前实际安装**(会话代理按 03 §2.9.1 三来源定位),`wechat_install` 只是上次记档:
        #   · 全新机器 / 安装器没跑过检测:行为空,原逻辑恒 NOT_INSTALLED,把装着 4.1.12.26 的机器也引去「重装」;
        #   · 用户事后自己升级或卸载了微信:行是陈旧的,按行判会放过不该放的版本;
        #   · 会话代理不在线(没登录桌面):没有事实来源,只能按记档判,不猜。
        installed = d.wechat_store.install()
        if d.hub.user_agent_online:
            try:
                loc = await via_pipe("wechat.locate", {}, timeout_s=TIMEOUT_S["wechat_read"], request=request)
            except WaError:
                loc = None                                            # 探测失败 ⇒ 退回记档,不把错误抛给向导
            if isinstance(loc, dict):
                if loc.get("installed") and loc.get("version"):
                    d.wechat_store.put_install(path=loc.get("path"), version=loc.get("version"),
                                               exe_version=loc.get("version"), data_root=loc.get("data_root"),
                                               data_dir=loc.get("data_dir"), appdata_dir=loc.get("appdata_dir"))
                    installed = d.wechat_store.install()
                elif loc.get("installed") is False:
                    installed = {}                                    # 本机确实没装(含事后卸载):旧行不作数(空 dict ≠ None,不回退查表)
        return d.wechat_store.version_match(bundled_version=_bundled_version(d), installed=installed,
                                            wxkey_dlls=d.cfg.wechat.wxkey_dlls)

    @app.get("/wa/v1/wechat/profiles")
    async def wechat_profiles(request: Request):                                  # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.profiles")
        except WaError as e:
            return fail(e, request)
        return {"profiles": d.wechat_store.profiles()}

    @app.post("/wa/v1/wechat/login/start")
    async def wechat_login_start(request: Request):                               # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.login_start")
            _wechat_enabled_guard()
            body = await _json(request)
            # R6-58 (at) ②取用顺序落地处:按 account_id 算好「该 wxid 行值 / 配置默认」随管道参数带下去,
            # 会话代理只应用(WeChatBackend.set_main_wnd_class),自己不碰 DB
            main_wnd_class = d.wechat_store.effective_main_wnd_class_for_account(
                body.get("account_id"), default=d.cfg.wechat.main_wnd_class)
            out = await via_pipe("wechat.login.start", {"account_id": body.get("account_id"),
                                                        "login_session_id": body.get("login_session_id"),
                                                        "main_wnd_class": main_wnd_class},
                                 timeout_s=TIMEOUT_S["wechat_login_start"], request=request)
        except WaError as e:
            return fail(e, request)
        return JSONResponse(out, status_code=202)

    @app.post("/wa/v1/wechat/login/cancel")
    async def wechat_login_cancel(request: Request):                              # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.login_cancel")
            body = await _json(request)
            out = await via_pipe("wechat.login.cancel", {"login_session_id": body.get("login_session_id")},
                                 timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)
        if isinstance(out, dict) and out.pop("narrator_stop_pending", False):
            await _kill_narrator_from_service(request)
        return out

    async def _kill_narrator_from_service(request: Request) -> bool:
        """R6-89:会话代理停不掉讲述人 ⇒ 服务(提权)结束;记审计。没有 killer(测试/--dev)直接 False。"""
        if d.narrator_killer is None:
            return False
        ok = await asyncio.to_thread(d.narrator_killer)
        d.audit.record(actor="system", action="wechat.narrator_stop", result="OK" if ok else "FAILED",
                       trace_id=getattr(request.state, "trace_id", None))
        return ok

    @app.get("/wa/v1/wechat/login/status")
    async def wechat_login_status(request: Request):                              # type: ignore[no-untyped-def]
        try:
            # 01 §2.5 白名单③ / 05 §3.2 R6-5:登录流只读状态控制台可直调(向导刷新/步④ 讲述人状态);写动作仍只 A
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wechat.login_status")
            out = await via_pipe("wechat.login.status", {}, timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)
        # R6-58 (au) 跟进:05 §2.4.4 ⑤⑥ 取钥成功即算一次完整登录 ——「刚进 ready」这一次(一次性标记,
        # 不是每次轮询都记)落 wechat_profiles.login_count/last_login_ms/last_wxkey_dll(结果随响应带回,
        # 由服务落库;会话代理不碰 DB)。两个内部字段用完即弹出,不进 #33 的公开响应形状。
        just_became_ready = out.pop("just_became_ready", False)
        wechat_version = out.pop("wechat_version", None)
        if (out.get("narrator") or {}).get("stop_pending"):
            if await _kill_narrator_from_service(request):
                out["narrator"] = {**out["narrator"], "state": "stopped", "stop_pending": False}
        if just_became_ready and out.get("wxid") and out.get("account_id"):
            d.wechat_store.record_login(wxid=out["wxid"], account_id=out["account_id"],
                                        wechat_version=wechat_version, wxkey_dll=(out.get("key") or {}).get("dll"))
        return out

    @app.post("/wa/v1/wechat/update-block")
    async def wechat_update_block(request: Request):                              # type: ignore[no-untyped-def]
        """#33b:**执行体 = svc**(整机 hosts 要 LocalSystem)。"""
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wechat.update_block")
            body = await _json(request)
            out = d.hosts_block.apply(bool(body.get("enable")))
        except WaError as e:
            return fail(e, request)
        if out["result"] == "blocked_by_policy":
            d.alerts.firing(A.H21_WECHAT_HOSTS_BLOCK_FAILED, subject="host",
                            evidence={"reason": out.get("reason"), "domains": out.get("domains")})
        else:
            d.alerts.resolve(A.H21_WECHAT_HOSTS_BLOCK_FAILED, subject="host")
        return out

    @app.post("/wa/v1/wechat/bind")
    async def wechat_bind(request: Request):                                      # type: ignore[no-untyped-def]
        """#33c(R3-2):``identified`` 相位后 Agent 调它把 ``wxid`` 绑到 ``wxNN``;同 ``wxid`` 重绑幂等。"""
        try:
            auth(request, (ROLE_AGENT,), action="wechat.bind")
            body = await _json(request)
            out = d.wechat_store.bind(wxid=str(body["wxid"]), account_id=str(body["account_id"]))
            if d.hub.user_agent_online:                                            # 让后续 login/status 带上 wxid
                try:
                    bind_resp = await via_pipe("wechat.bind", out, timeout_s=TIMEOUT_S["wechat_read"], request=request)
                    # R6-58 (at) ①:该 wxid 此刻实测到的主窗口类名随响应带回(经现有 main_window() 协议探测),
                    # 服务侧落库;落空(未测到)时 record_main_wnd_class 自己会跳过,不覆盖已有实测值
                    d.wechat_store.record_main_wnd_class(out["wxid"], bind_resp.get("main_wnd_class"))
                except WaError:
                    pass
        except KeyError as e:
            return fail(WaError(INVALID_ARGS, f"缺少字段 {e}", reason="missing_field"), request)
        except WaError as e:
            return fail(e, request)
        return out

    @app.post("/wa/v1/wechat/logout")
    async def wechat_logout(request: Request):                                    # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.logout")
            return await via_pipe("wechat.logout", {}, timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.post("/wa/v1/wechat/key/retry")
    async def wechat_key_retry(request: Request):                                 # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wechat.key_retry")      # 01 §2.5 ③:人工动作,控制台可调
            return await via_pipe("wechat.key.retry", {}, timeout_s=TIMEOUT_S["wechat_login_start"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.get("/wa/v1/wechat/ui-visible")
    async def wechat_ui_visible(request: Request):                                # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT, ROLE_CONSOLE), action="wechat.ui_visible")     # 01 §2.5 ③:只读,控制台可调
            return await via_pipe("wechat.ui-visible", {}, timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.post("/wa/v1/wechat/reinstall")
    async def wechat_reinstall(request: Request):                                 # type: ignore[no-untyped-def]
        """#37 **混合端点**:``user``(UI 引导)+ ``svc``(备份/安装);**每步用户确认**(00 §11.8 [WXVER])→ ``202``,进度经 #33。"""
        try:
            auth(request, (ROLE_CONSOLE,), action="wechat.reinstall")
            body = await _json(request)
            d.wechat_store.put_install(backup_dir=body.get("backup_dir"))          # 服务半:记备份目录
            out = await via_pipe("wechat.reinstall", {"installer": body.get("installer")},
                                 timeout_s=TIMEOUT_S["wsl"], request=request)
        except WaError as e:
            return fail(e, request)
        return JSONResponse(out, status_code=202)

    @app.post("/wa/v1/wechat/send")
    async def wechat_send(request: Request):                                      # type: ignore[no-untyped-def]
        """#38 **仅 A**;**不重试**(重试由上层幂等决定 —— 微信发送重试会重复发)。"""
        try:
            auth(request, (ROLE_AGENT,), action="wechat.send")
            body = await _json(request)
            return await via_pipe("wechat.send", {
                "session_name": body.get("session_name"), "text": body.get("text"),
                "image_path": body.get("image_path"), "idempotency_key": body.get("idempotency_key"),
                "confirm_timeout_ms": body.get("confirm_timeout_ms") or d.cfg.wechat.confirm_timeout_ms},
                timeout_s=TIMEOUT_S["wechat_send"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.get("/wa/v1/wechat/read")
    async def wechat_read(request: Request, talker: Optional[str] = None, since_seq: Optional[int] = None,
                          limit: Optional[int] = None):                           # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.read")
            return await via_pipe("wechat.read", {"talker": talker, "since_seq": since_seq, "limit": limit},
                                  timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.get("/wa/v1/wechat/media/{key}")
    async def wechat_media(key: str, request: Request):                           # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.media", target=key)
            out = await via_pipe("wechat.media", {"key": key}, timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)
        import base64
        return Response(content=base64.b64decode(out["b64"]), media_type=out.get("content_type", "image/jpeg"))

    @app.get("/wa/v1/wechat/sessions")
    async def wechat_sessions(request: Request, keyword: Optional[str] = None, limit: int = 100):   # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.sessions")
            return await via_pipe("wechat.sessions", {"keyword": keyword, "limit": limit},
                                  timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)

    @app.get("/wa/v1/wechat/screenshot")
    async def wechat_screenshot(request: Request):                                # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_AGENT,), action="wechat.screenshot")
            out = await via_pipe("wechat.screenshot", {}, timeout_s=TIMEOUT_S["wechat_read"], request=request)
        except WaError as e:
            return fail(e, request)
        import base64
        return Response(content=base64.b64decode(out["b64"]), media_type="image/png")

    @app.get("/wa/v1/settings/wechat")
    async def settings_wechat_get(request: Request):                              # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE,), action="settings.wechat.get")
        except WaError as e:
            return fail(e, request)
        w = d.cfg.wechat
        return {"enabled": w.enabled, "keep_awake_mode": w.keep_awake_mode,
                "narrator_min_seconds": w.narrator_min_seconds, "poll_interval_s": w.poll_interval_s}

    @app.put("/wa/v1/settings/wechat")
    async def settings_wechat_put(request: Request):                              # type: ignore[no-untyped-def]
        """#43:``winagent.toml [wechat]`` 的**受控子集**;``enabled`` 改动后通知会话代理起/停模块。"""
        allow = ("enabled", "keep_awake_mode", "narrator_min_seconds", "poll_interval_s")
        try:
            auth(request, (ROLE_CONSOLE,), action="settings.wechat.put")
            body = await _json(request)
            bad = [k for k in body if k not in allow]
            if bad:
                raise WaError(INVALID_ARGS, f"{bad} 不在 #43 的受控子集 {allow} 内", reason="key_not_allowed")
            was = d.cfg.wechat.enabled
            d.cfg = d.cfg.with_wechat(**body)
            d.db.put_setting("wechat.enabled_snapshot", d.cfg.wechat.enabled, updated_by="console")
            if d.config_path:                                                      # R6-88:真值写回 toml,重启不丢
                try:
                    await asyncio.to_thread(write_toml_keys, d.config_path, "wechat",
                                            {k: getattr(d.cfg.wechat, k) for k in body})
                except OSError as e:
                    raise WaError(INTERNAL, f"winagent.toml 写回失败:{e}", reason="config_write_failed") from e
            if was != d.cfg.wechat.enabled:
                if not d.cfg.wechat.enabled:
                    d.power.restore()                                              # 关模块即还原电源计划(04 §2.5.1)
                    d.hosts_block.apply(False)                                     # 并成对删 hosts 屏蔽行(#33b)
                if d.hub.user_agent_online:
                    try:
                        await via_pipe("wechat.module", {"enabled": d.cfg.wechat.enabled},
                                       timeout_s=TIMEOUT_S["wsl"], request=request)
                    except WaError:
                        pass
        except WaError as e:
            return fail(e, request)
        w = d.cfg.wechat
        return {"enabled": w.enabled, "keep_awake_mode": w.keep_awake_mode,
                "narrator_min_seconds": w.narrator_min_seconds, "poll_interval_s": w.poll_interval_s}

    # ================================================================== #44 audit
    @app.get("/wa/v1/audit")
    async def audit_page(request: Request, since: Optional[int] = None, until: Optional[int] = None,
                         limit: int = 100, cursor: Optional[int] = None):         # type: ignore[no-untyped-def]
        try:
            auth(request, (ROLE_CONSOLE,), action="audit.read")
        except WaError as e:
            return fail(e, request)
        return d.audit.page(since=since, until=until, limit=limit, cursor=cursor)

    return app


async def _json(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
        if not raw:
            return {}
        import json
        body = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise WaError(INVALID_ARGS, "请求体不是合法 JSON", reason="bad_json") from e
    if not isinstance(body, dict):
        raise WaError(INVALID_ARGS, "请求体必须是 JSON 对象", reason="bad_json")
    return body


def _bundled_version(d: SvcDeps) -> str:
    """随包微信版本(B-1:4.1.12.26,来源与 sha256 由 03 维护);这里从 ``settings`` 取,缺省用 03 记档值。"""
    return str(d.db.get_setting("wechat.bundled_version") or d.cfg.wechat.bundled_version)
