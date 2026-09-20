"""FastAPI 应用(02 §2.2.1 / §3.4;00 §10 错误信封;§3.8 版本头)。

本期端点:``/system/version|health``、``/capabilities``、``/accounts``、``/accounts/{id}``、``/accounts/{id}/commands``(#28/#30/#31)、
``/accounts/{id}/send``(#29)、``/sessions``(#26)、``/messages``(#48)、``/messages/{id}``(#49)、``/audit``(#95 简版)、``WS /events``(#3.4.7)。
每次调用记 ``audit_log(kind='api')``;每个响应带 ``X-QT-Api-Version`` / ``X-QT-Agent-Version``;``X-QT-Api-Min`` 不满足回 ``426 UPGRADE_REQUIRED``。
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from email.utils import formatdate
from typing import Any, Optional

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import __version__ as AGENT_VERSION
from ..config import WS_PING_INTERVAL_S
from ..events import iso8601
from ..hmac_inbound import has_hmac_headers
from ..ids import ulid
from ..maintenance import DiskFullError
from ..models import Command, CommandOrigin, RESULT_CODES
from ..workflow import WorkflowParseError
from .auth import ApiError, Principal, is_unauth_health_source, principal_from_row, require_account, require_level
from .serialize import (account_view, command_view, decode_cursor, encode_cursor, message_view, result_view, session_view,
                        stored_result_view)

log = logging.getLogger("qtrade.api")

CAPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capabilities")
API_PREFIX = "/api/v1"

# 00 §10:HTTP 状态映射;不在表里的业务结果码走 200 + CommandResult(#28「同步:200 CommandResult」)
HTTP_BY_CODE = {"INVALID_ARGS": 400, "UNAUTHORIZED": 401, "FORBIDDEN": 403, "TARGET_NOT_FOUND": 404, "RESOURCE_EXHAUSTED": 409,
                "IDEMPOTENT_REPLAY": 409, "CONFIRM_EXPIRED": 409, "RATE_LIMITED": 429, "DISK_FULL": 507, "NOT_READY": 503, "INTERNAL": 500,
                "UPGRADE_REQUIRED": 426}


def load_capabilities() -> tuple[list[dict[str, Any]], str]:
    items = []
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(CAPS_DIR, "*.json"))):
        with open(p, "rb") as f:
            raw = f.read()
        h.update(raw)
        cap = json.loads(raw)
        cap["high_risk"] = cap["danger"]        # 只读别名,兼容一轮(02 #21)
        items.append(cap)
    return items, f"{h.hexdigest()[:8]}-{AGENT_VERSION}"


def _error_body(code: str, message: str, *, reason: str = "", retryable: bool = False, needs_human: bool = False,
                trace_id: Optional[str] = None, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    body = {"ok": False, "code": code, "error": {"message": message, "reason": reason, "retryable": retryable, "needs_human": needs_human},
            "trace_id": trace_id}
    if extra:
        body["error"].update(extra)
    return body


def _parse_time(v: Optional[str]) -> Optional[int]:
    if v is None or v == "":
        return None
    if v.isdigit():
        return int(v)
    if " " in v and "T" in v:            # 未编码的 "+08:00" 经 URL 解码成空格:容忍
        v = v.replace(" ", "+")
    return int(datetime.fromisoformat(v).timestamp() * 1000)


def create_api(agent) -> FastAPI:
    """``agent`` = app.AgentApp(持 store/bus/events/health/scheduler/cfg/clock/adapters)。"""
    cfg = agent.cfg
    app = FastAPI(title="QTrade Agent", version=AGENT_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    caps, caps_version = load_capabilities()
    caps_by_op = {c["op"]: c for c in caps}
    app.state.capabilities_version = caps_version

    # ------------------------------------------------------------------ 横切:版本头 / 错误信封 / 审计
    @app.middleware("http")
    async def versions_and_audit(request: Request, call_next):
        started = agent.clock()
        api_min = request.headers.get("X-QT-Api-Min")
        if api_min:
            try:
                want_major, want_minor = (int(x) for x in api_min.split(".")[:2])
                have_major, have_minor = (int(x) for x in cfg.api.api_version.split(".")[:2])
            except ValueError:
                want_major = want_minor = have_major = have_minor = 0
            if want_major != have_major or want_minor > have_minor:
                resp = JSONResponse(status_code=426, content=_error_body("UPGRADE_REQUIRED", f"需要 API {api_min},当前 {cfg.api.api_version}", reason="api_version"))
                return _with_version_headers(resp)
        try:
            response = await call_next(request)
        except ApiError as e:
            response = JSONResponse(status_code=e.http_status, content=_error_body(e.code, e.message, reason=e.reason, retryable=e.retryable,
                                                                                  needs_human=e.needs_human, extra=e.extra))
        if has_hmac_headers(dict(request.headers)):
            # §3.5 时钟容差:把服务端时间放进 Date 头让对方校时(成功与 401 skew 都要带,否则对方没法自校)
            response.headers["Date"] = formatdate(agent.clock() / 1000, usegmt=True)
        if request.url.path.startswith(API_PREFIX) and request.url.path != f"{API_PREFIX}/system/health":
            p: Optional[Principal] = getattr(request.state, "principal", None)
            try:
                agent.store.insert_audit(kind="api", transport=p.transport if p else "http", actor=p.actor if p else "anonymous",
                                         action=f"{request.method} {request.url.path}", account_id=getattr(request.state, "account_id", None),
                                         trace_id=getattr(request.state, "trace_id", None), result_code=str(response.status_code),
                                         detail={"http_status": response.status_code, "cost_ms": agent.clock() - started,
                                                 "ip": request.client.host if request.client else None}, now_ms=agent.clock())
            except Exception as e:     # 审计失败不影响响应
                log.warning("audit_log 写入失败: %s", e)
        return _with_version_headers(response)

    def _with_version_headers(resp: Response) -> Response:
        resp.headers["X-QT-Api-Version"] = cfg.api.api_version
        resp.headers["X-QT-Agent-Version"] = AGENT_VERSION
        resp.headers["X-QT-Capabilities-Version"] = caps_version     # 02 §3.10:三处下发(/capabilities、/system/version、响应头)
        return resp

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return _with_version_headers(JSONResponse(status_code=e.http_status, content=_error_body(
            e.code, e.message, reason=e.reason, retryable=e.retryable, needs_human=e.needs_human, extra=e.extra)))

    @app.exception_handler(DiskFullError)
    async def _disk_full(request: Request, e: DiskFullError):
        """§2.8.8 / §3.2 R-02:磁盘满 ⇒ HTTP **507** + 结果码 ``DISK_FULL``,信封带 evidence 三个数。"""
        return _with_version_headers(JSONResponse(status_code=507, content=_error_body(
            "DISK_FULL", e.message, reason="disk_full", retryable=False, needs_human=True,
            extra={"hint_actions": ["open_env", "run_cleanup"], "evidence": e.evidence()})))

    # ------------------------------------------------------------------ 鉴权
    async def _principal_async(request: Request, required: str = "read") -> Principal:
        """§3.5:带全套 ``X-QT-*`` 头 ⇒ 走公网入站 HMAC;否则走 Bearer。HMAC 成功时把服务端时间放进 ``Date`` 头(让对方校时)。"""
        if has_hmac_headers(dict(request.headers)) and getattr(agent, "hmac", None) is not None:
            res = await agent.hmac.verify(method=request.method, path=request.url.path, query=str(request.url.query or ""),
                                          body=await request.body(), headers=dict(request.headers),
                                          client_ip=request.client.host if request.client else None,
                                          required_level=required)
            request.state.principal = res.principal
            request.state.hmac = res
            return res.principal
        return _principal(request, required)

    def _principal(request: Request, required: str = "read") -> Principal:
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
        return _principal_from_token(token, required, request)

    def _principal_from_token(token: str, required: str, request) -> Principal:
        if not token:
            raise ApiError(401, "UNAUTHORIZED", "缺少 Bearer 令牌", reason="missing_token")
        row = agent.store.api_client_by_token(token)
        if row is None:
            raise ApiError(401, "UNAUTHORIZED", "令牌无效或已过期", reason="bad_token")
        p = principal_from_row(row, transport="local" if (request.client and request.client.host in ("127.0.0.1", "::1")) else "http")
        require_level(p, required)
        agent.store.api_client_touch(p.app_id, agent.clock())
        request.state.principal = p
        return p

    # ------------------------------------------------------------------ system
    @app.get(f"{API_PREFIX}/system/version")
    async def system_version(request: Request):
        _principal(request, "read")
        sv = agent.store.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        return {"ok": True, "agent": {"version": AGENT_VERSION}, "api_version": cfg.api.api_version, "capabilities_version": caps_version,
                "schema_version": sv, "winagent": {"version": agent.health.winagent_version, "online": agent.health.winagent_online},
                "kernel": None, "wsl": None, "docker": None, "images": {}}

    @app.get(f"{API_PREFIX}/system/health")
    async def system_health(request: Request):
        host = request.client.host if request.client else None
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            _principal(request, "read")
            db_mb, wal_mb = agent.store.db_size_mb()
            running = agent.store.con.execute("SELECT COUNT(*) FROM accounts WHERE state='running' AND deleted_ms IS NULL").fetchone()[0]
            total = agent.store.con.execute("SELECT COUNT(*) FROM accounts WHERE deleted_ms IS NULL").fetchone()[0]
            data_dir = os.path.dirname(os.path.abspath(agent.store.path)) if agent.store.path != ":memory:" else os.getcwd()
            try:
                disk_free_mb = shutil.disk_usage(data_dir).free // 1048576      # 02 #72 / B-35:agent.db 所在盘剩余
            except OSError:
                disk_free_mb = None
            return {"ok": True, "agent": {"version": AGENT_VERSION, "api_version": cfg.api.api_version, "uptime_s": agent.health.uptime_s(),
                                          "db_mb": db_mb, "wal_mb": wal_mb},
                    "dockerd": agent.health.dockerd_ok,
                    "winagent": {"online": agent.health.winagent_online, "version": agent.health.winagent_version, "user_agent": agent.health.user_agent_online},
                    "accounts": {"running": running, "n": total}, "disk_free_mb": disk_free_mb,
                    "checks": {"H13": "firing" if agent.health.h13_firing() else ("ok" if agent.timesync.last_probe_ms else "unknown"),
                               "H02": "unknown" if agent.health.winagent_online is None else ("ok" if agent.health.winagent_online else "firing"),
                               "H03": "unknown" if agent.health.dockerd_ok is None else ("ok" if agent.health.dockerd_ok else "firing"),
                               **agent.healthloop.checks(),
                               "H24": "unknown" if agent.pressure.level == "unknown" else ("ok" if agent.pressure.level == "ok" else "firing")},
                    "mem": {"level": agent.pressure.level, "avail_mb": agent.pressure.avail_mb},
                    "alerts": [{"code": a.code, "subject": a.subject, "severity": a.severity, "count": a.count} for a in agent.alerts.active.values()],
                    "scheduler": agent.scheduler.snapshot()}
        if is_unauth_health_source(host, cfg.api.unauth_health_sources, agent.wsl_gateway):
            return agent.health.summary()
        raise ApiError(401, "UNAUTHORIZED", "该来源须带令牌", reason="missing_token")

    @app.get(f"{API_PREFIX}/capabilities")
    async def capabilities(request: Request, channel: Optional[str] = None):
        _principal(request, "read")
        items = caps if not channel else [c for c in caps if c["channels"].get(channel) == "supported"]
        return {"ok": True, "capabilities_version": caps_version, "data": items}      # §3.4 通用:列表一律 data(R6-53)

    # ------------------------------------------------------------------ accounts
    def _acct_caps(row: dict[str, Any]) -> list[str]:
        ad = agent.adapters.get(row["channel"])
        return sorted(ad.capabilities) if ad else []

    @app.get(f"{API_PREFIX}/accounts")
    async def list_accounts(request: Request, channel: Optional[str] = None, state: Optional[str] = None, enabled: Optional[bool] = None,
                            include_stopped: bool = True, include_deleted: bool = False):
        p = _principal(request, "read")
        rows = agent.store.list_accounts(channel=channel, state=state, enabled=enabled, include_deleted=include_deleted)
        rows = [r for r in rows if p.allows_account(r["id"]) and (include_stopped or r["state"] != "stopped")]
        return {"ok": True, "data": [account_view(r, _acct_caps(r)) for r in rows]}

    @app.get(f"{API_PREFIX}/accounts/{{account_id}}")
    async def get_account(request: Request, account_id: str):
        p = _principal(request, "read")
        require_account(p, account_id)
        row = agent.store.get_account_full(account_id)
        if row is None or row.get("deleted_ms"):
            raise ApiError(404, "TARGET_NOT_FOUND", f"账号不存在:{account_id}")
        request.state.account_id = account_id
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    # ------------------------------------------------------------------ 账号生命周期(02 §3.4.1 #2/#4/#5/#6/#7/#9/#10/#11/#19)
    IDEM_PREFIX = "idem.accounts."

    @app.post(f"{API_PREFIX}/accounts", status_code=201)
    async def create_account(request: Request, response: Response):
        p = _principal(request, "write")
        body = await request.json()
        key = body.get("idempotency_key")
        if not key or not isinstance(key, str) or len(key) > 128:
            raise ApiError(400, "INVALID_ARGS", "POST /accounts 必带 idempotency_key(≤128 字符)", reason="idempotency_key_required",
                           extra={"details": [{"pointer": "/idempotency_key"}]})
        prior = agent.store.settings_get(IDEM_PREFIX + key)
        if prior:
            row = agent.store.get_account_full(prior)
            return JSONResponse(status_code=409, content=_error_body("IDEMPOTENT_REPLAY", f"同 idempotency_key 已创建账号 {prior}", reason="replay",
                                                                     extra={"account_id": prior}) | {"data": account_view(row, _acct_caps(row)) if row else None})
        row = await agent.accounts.create(body, actor=p.actor)
        agent.store.settings_set(IDEM_PREFIX + key, row["id"], actor=p.actor)
        request.state.account_id = row["id"]
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.patch(f"{API_PREFIX}/accounts/{{account_id}}")
    async def patch_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        row = await agent.accounts.patch(account_id, await request.json(), actor=p.actor)
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/enable")
    async def enable_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        row = await agent.accounts.enable(account_id, actor=p.actor)
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/disable")
    async def disable_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        body = await _json_or_empty(request)
        row = await agent.accounts.disable(account_id, graceful=bool(body.get("graceful", True)), actor=p.actor)
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/start")
    async def start_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        res = await agent.accounts.start(account_id, actor=p.actor)
        return JSONResponse(status_code=200 if res.get("already") else 202, content={"ok": True, **res})

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/stop")
    async def stop_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        body = await _json_or_empty(request)
        res = await agent.accounts.stop(account_id, graceful=bool(body.get("graceful", True)), actor=p.actor)
        return JSONResponse(status_code=200 if res.get("already") else 202, content={"ok": True, **res})

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/restart")
    async def restart_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        res = await agent.accounts.restart(account_id, actor=p.actor)
        return JSONResponse(status_code=202, content={"ok": True, **res})

    @app.delete(f"{API_PREFIX}/accounts/{{account_id}}")
    async def delete_account(request: Request, account_id: str, confirm: Optional[str] = None):
        p = _principal(request, "admin")
        require_account(p, account_id)
        request.state.account_id = account_id
        res = await agent.accounts.delete(account_id, confirm=confirm, actor=p.actor)
        return {"ok": True, **res}

    @app.get(f"{API_PREFIX}/accounts/{{account_id}}/state")
    async def account_state(request: Request, account_id: str):
        p = _principal(request, "read")
        require_account(p, account_id)
        request.state.account_id = account_id
        return {"ok": True, **(await agent.accounts.state_of(account_id))}

    # ------------------------------------------------------------------ 登录阶段(#12/#13/#14/#15/#16b)与 #20/#22/#23
    @app.post(f"{API_PREFIX}/accounts/batch")
    async def batch_accounts(request: Request):
        p = _principal(request, "write")
        body = await request.json()
        for aid in (body.get("ids") or []) if isinstance(body.get("ids"), list) else []:
            if isinstance(aid, str):
                require_account(p, aid)
        return {"ok": True, **(await agent.accounts.batch(body, actor=p.actor))}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/login")
    async def login_account(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        body = await _json_or_empty(request)
        res = await agent.accounts.login(account_id, body, actor=p.actor)
        return JSONResponse(status_code=202, content={"ok": True, **res})

    @app.put(f"{API_PREFIX}/accounts/{{account_id}}/credential")
    async def put_credential(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        row = await agent.accounts.set_credential(account_id, await request.json(), actor=p.actor)
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.delete(f"{API_PREFIX}/accounts/{{account_id}}/credential")
    async def delete_credential(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        row = await agent.accounts.delete_credential(account_id, actor=p.actor)
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.get(f"{API_PREFIX}/accounts/{{account_id}}/prompt")
    async def get_prompt(request: Request, account_id: str, login_session_id: Optional[str] = None):
        p = _principal(request, "read")
        require_account(p, account_id)
        request.state.account_id = account_id
        return {"ok": True, **agent.accounts.prompt(account_id, login_session_id)}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/login/cancel")
    async def login_cancel(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        body = await _json_or_empty(request)
        return {"ok": True, **(await agent.accounts.login_cancel(account_id, body.get("login_session_id"), actor=p.actor))}

    @app.get(f"{API_PREFIX}/accounts/{{account_id}}/capabilities")
    async def account_capabilities(request: Request, account_id: str):
        p = _principal(request, "read")
        require_account(p, account_id)
        request.state.account_id = account_id
        return {"ok": True, **agent.accounts.capabilities_of(account_id)}

    @app.patch(f"{API_PREFIX}/accounts/{{account_id}}/settings")
    async def patch_settings(request: Request, account_id: str):
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        row = await agent.accounts.patch_settings(account_id, await request.json(), actor=p.actor)
        return {"ok": True, "data": account_view(row, _acct_caps(row))}

    @app.get(f"{API_PREFIX}/resources")
    async def resources(request: Request):
        _principal(request, "read")
        snap = agent.pool.snapshot()
        rows = agent.store.list_accounts()
        by_ch: dict[str, int] = {}
        for r in rows:
            by_ch[r["channel"]] = by_ch.get(r["channel"], 0) + 1
        return {"ok": True, **snap, "accounts": [], "accounts_by_channel": by_ch}

    async def _json_or_empty(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "INVALID_ARGS", "请求体须为 JSON", reason="bad_json")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ commands / send
    async def _submit(request: Request, p: Principal, account_id: str, body: dict[str, Any], *, force_confirm: bool = False):
        op = body.get("op")
        args = body.get("args") or {}
        if not isinstance(op, str) or op not in caps_by_op:
            raise ApiError(400, "INVALID_ARGS", f"未知能力 op={op!r}", reason="unknown_op", extra={"details": [{"pointer": "/op"}]})
        cap = caps_by_op[op]
        key = body.get("idempotency_key")
        if cap["kind"] != "read" and not key:
            raise ApiError(400, "INVALID_ARGS", "写类指令必带 idempotency_key", reason="idempotency_key_required", extra={"details": [{"pointer": "/idempotency_key"}]})
        if key is not None and (not isinstance(key, str) or len(key) > 128):
            raise ApiError(400, "INVALID_ARGS", "idempotency_key 须为 ≤128 字符的字符串", reason="idempotency_key_invalid", extra={"details": [{"pointer": "/idempotency_key"}]})
        require_level(p, "read" if cap["kind"] == "read" else ("admin" if cap["kind"] == "admin" else "write"))
        require_account(p, account_id)
        acct = agent.store.get_account(account_id)
        if acct is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"账号不存在:{account_id}")
        confirm = True if force_confirm else bool(body.get("confirm", True))
        if acct["channel"] == "wechat" and not confirm:
            raise ApiError(400, "INVALID_ARGS", "微信不支持 confirm:false(C-30)", reason="wechat_confirm_required", extra={"details": [{"pointer": "/confirm"}]})
        timeout_ms = int(body.get("timeout_ms") or cfg.bus.default_timeout_ms)
        cmd = Command(account_id=account_id, op=op, args=args, idempotency_key=key, confirm=confirm, timeout_ms=timeout_ms,
                      origin=CommandOrigin(transport=p.transport, actor=p.actor, ip=request.client.host if request.client else None),
                      trace_id=ulid(), submitted_at_ms=agent.clock())
        request.state.account_id = account_id
        request.state.trace_id = cmd.trace_id
        # §3.5 末行:公网入站(HMAC)**默认 async:true**,结果走 webhook;显式 async:false 才同步等
        want_async = body.get("async")
        if want_async is None and getattr(request.state, "hmac", None) is not None:
            want_async = True
        if want_async:
            asyncio.create_task(agent.bus.submit(cmd), name=f"api-async:{cmd.trace_id}")
            return JSONResponse(status_code=202, content={"ok": True, "trace_id": cmd.trace_id, "accepted": True})
        task = asyncio.ensure_future(agent.bus.submit(cmd))
        try:
            res = await asyncio.wait_for(asyncio.shield(task), timeout=cfg.api.http_sync_max_wait_ms / 1000)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=202, content={"ok": True, "trace_id": cmd.trace_id, "accepted": True, "pending": True})
        status = HTTP_BY_CODE.get(res.code, 200)
        return JSONResponse(status_code=status, content=result_view(res))

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/commands")
    async def post_command(request: Request, account_id: str):
        p = await _principal_async(request, "read")
        body = await request.json()
        return await _submit(request, p, account_id, body)

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/send")
    async def post_send(request: Request, account_id: str):
        p = await _principal_async(request, "write")
        body = await request.json()
        args = {k: body[k] for k in ("session", "text", "image_ref", "file_ref") if k in body}
        op = "send_text" if "text" in args else ("send_image" if "image_ref" in args else "send_file")
        return await _submit(request, p, account_id, {"op": op, "args": args, "idempotency_key": body.get("idempotency_key"),
                                                     "timeout_ms": body.get("timeout_ms"), "async": body.get("async")}, force_confirm=True)

    @app.get(f"{API_PREFIX}/accounts/{{account_id}}/commands")
    async def list_commands(request: Request, account_id: str, status: Optional[str] = None, op: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
        p = _principal(request, "read")
        require_account(p, account_id)
        rows = agent.store.list_commands(account_id, status=status, op=op, limit=limit)
        return {"ok": True, "data": [{"command": command_view(r), "result": stored_result_view(r)} for r in rows]}

    @app.get(f"{API_PREFIX}/accounts/{{account_id}}/commands/{{trace_id}}")
    async def get_command(request: Request, account_id: str, trace_id: str):
        p = _principal(request, "read")
        require_account(p, account_id)
        c = agent.store.get_command(trace_id)
        if c is None or c["account_id"] != account_id:
            raise ApiError(404, "TARGET_NOT_FOUND", f"指令不存在:{trace_id}")
        r = agent.store.get_command_result(trace_id)
        return {"ok": True, "command": command_view(c), "result": stored_result_view(r) if r else None}

    # ------------------------------------------------------------------ sessions / messages
    @app.get(f"{API_PREFIX}/sessions")
    async def list_sessions(request: Request, account_id: Optional[str] = None, keyword: Optional[str] = None, kind: Optional[str] = None,
                            limit: int = Query(100, ge=1, le=500)):
        p = _principal(request, "read")
        rows = agent.store.list_sessions(account_id=account_id, keyword=keyword, kind=kind, limit=limit)
        return {"ok": True, "data": [session_view(r) for r in rows if p.allows_account(r["account_id"])]}

    @app.get(f"{API_PREFIX}/messages")
    async def list_messages(request: Request, account_id: Optional[str] = None, session_id: Optional[str] = None, dir: Optional[str] = None,
                            type: Optional[str] = None, state: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                            sender: Optional[str] = None, q: Optional[str] = None, limit: int = Query(100, ge=1, le=500), cursor: Optional[str] = None):
        p = _principal(request, "read")
        if account_id:
            require_account(p, account_id)
        before = None
        if cursor:
            try:
                before = decode_cursor(cursor)
            except Exception:
                raise ApiError(400, "INVALID_ARGS", "cursor 非法", reason="bad_cursor", extra={"details": [{"pointer": "/cursor"}]})
        try:
            since_ms, until_ms = _parse_time(since), _parse_time(until)
        except ValueError:
            raise ApiError(400, "INVALID_ARGS", "since/until 须为 ISO 8601 或毫秒", reason="bad_time", extra={"details": [{"pointer": "/since"}]})
        rows, slow = agent.store.query_messages(account_id=account_id, session_id=session_id, dir=dir, type=type, state=state, since_ms=since_ms,
                                                until_ms=until_ms, sender=sender, q=q, limit=limit, before=before)
        rows = [r for r in rows if p.allows_account(r["account_id"])]
        out: dict[str, Any] = {"ok": True, "data": [message_view(r) for r in rows],          # §3.4 通用 C-42:{ok, data, next_cursor}(R6-53)
                               "next_cursor": encode_cursor(rows[-1]["ts_ms"], rows[-1]["id"]) if len(rows) == limit else None}
        if slow:
            out["slow_match"] = True
        return out

    @app.get(f"{API_PREFIX}/messages/{{message_id}}")
    async def get_message(request: Request, message_id: str):
        p = _principal(request, "read")
        row = agent.store.get_message_full(message_id)
        if row is None or not p.allows_account(row["account_id"]):
            raise ApiError(404, "TARGET_NOT_FOUND", f"消息不存在:{message_id}")
        return {"ok": True, "data": message_view(row)}

    # ------------------------------------------------------------------ audit(简版)
    @app.get(f"{API_PREFIX}/audit")
    async def list_audit(request: Request, kind: Optional[str] = None, account_id: Optional[str] = None, action: Optional[str] = None,
                         limit: int = Query(100, ge=1, le=1000)):
        p = _principal(request, "read")
        sql, params = "SELECT * FROM audit_log WHERE 1=1", []
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if account_id:
            sql += " AND account_id=?"; params.append(account_id)
        if action:
            sql += " AND action=?"; params.append(action)
        if not p.can("admin"):
            sql += " AND actor=?"; params.append(p.actor)      # 非 A 级只看自己
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        rows = [dict(r) for r in agent.store.con.execute(sql, params).fetchall()]
        return {"ok": True, "data": rows}

    # ------------------------------------------------------------------ #17 / #18 微信单槽切换(02 §3.4.1)
    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/switch")
    async def switch_account(request: Request, account_id: str):
        """#17,级别 W。出参字面键集顶层平铺(R6-55):``{ok, holder_before, target, login_session_id}``。"""
        p = _principal(request, "write")
        require_account(p, account_id)
        request.state.account_id = account_id
        body = await _json_or_empty(request)
        res = await agent.accounts.switch(account_id, body, actor=p.actor)
        return JSONResponse(status_code=202, content={"ok": True, **res})

    @app.post(f"{API_PREFIX}/accounts/switch")
    async def switch_new_account(request: Request):
        """#18,级别 W;body ``{target:"new", confirm?:boolean=false}``。"""
        p = _principal(request, "write")
        body = await _json_or_empty(request)
        if body.get("target") != "new":
            raise ApiError(400, "INVALID_ARGS", "target 只接受 'new'", reason="bad_target", extra={"details": [{"pointer": "/target"}]})
        res = await agent.accounts.switch_new(body, actor=p.actor)
        request.state.account_id = res.get("target")
        return JSONResponse(status_code=202, content={"ok": True, **res})

    # ------------------------------------------------------------------ #38~#47 工作流(02 §3.4.3)
    def _wf_or_404(workflow_id: str) -> dict[str, Any]:
        wf = agent.workflows.get(workflow_id)
        if wf is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"工作流不存在:{workflow_id}")
        return wf

    def _wf_parse_error(e: WorkflowParseError) -> ApiError:
        """#39/#41/#47:解析校验失败 ``400`` **带行号**(``errors:[{line, message}]``)。"""
        return ApiError(400, "INVALID_ARGS", "工作流 YAML 校验未过", reason="workflow_invalid", extra={"errors": e.as_list()})

    @app.get(f"{API_PREFIX}/workflows")
    async def list_workflows(request: Request):
        """#38:列表**不含 yaml 全文**(G-09:工作流只存表)。"""
        _principal(request, "read")
        return {"ok": True, "data": [{k: v for k, v in w.items() if k != "yaml"} for w in agent.workflows.list()]}

    @app.post(f"{API_PREFIX}/workflows", status_code=201)
    async def create_workflow(request: Request):
        """#39,级别 A:``{name, yaml, enabled?, schedule_cron?}``。"""
        p = _principal(request, "admin")
        body = await request.json()
        if not isinstance(body.get("name"), str) or not body["name"].strip():
            raise ApiError(400, "INVALID_ARGS", "name 必填", reason="bad_name", extra={"details": [{"pointer": "/name"}]})
        if not isinstance(body.get("yaml"), str):
            raise ApiError(400, "INVALID_ARGS", "yaml 必填", reason="bad_yaml", extra={"details": [{"pointer": "/yaml"}]})
        try:
            wf = agent.workflows.upsert(name=body["name"].strip(), yaml=body["yaml"], enabled=bool(body.get("enabled", True)),
                                        schedule_cron=body.get("schedule_cron"), actor=p.actor)
        except WorkflowParseError as e:
            raise _wf_parse_error(e)
        return {"ok": True, "data": wf}

    @app.get(f"{API_PREFIX}/workflows/{{workflow_id}}")
    async def get_workflow(request: Request, workflow_id: str):
        """#40:含 yaml。"""
        _principal(request, "read")
        return {"ok": True, "data": _wf_or_404(workflow_id)}

    @app.put(f"{API_PREFIX}/workflows/{{workflow_id}}")
    async def put_workflow(request: Request, workflow_id: str):
        """#41,级别 A:整体替换,``version+1``。"""
        p = _principal(request, "admin")
        cur = _wf_or_404(workflow_id)
        body = await request.json()
        if not isinstance(body.get("yaml"), str):
            raise ApiError(400, "INVALID_ARGS", "yaml 必填", reason="bad_yaml", extra={"details": [{"pointer": "/yaml"}]})
        try:
            wf = agent.workflows.upsert(name=str(body.get("name") or cur["name"]), yaml=body["yaml"],
                                        enabled=bool(body.get("enabled", cur.get("enabled", 1))),
                                        schedule_cron=body.get("schedule_cron", cur.get("schedule_cron")), actor=p.actor)
        except WorkflowParseError as e:
            raise _wf_parse_error(e)
        return {"ok": True, "data": wf}

    @app.delete(f"{API_PREFIX}/workflows/{{workflow_id}}")
    async def delete_workflow(request: Request, workflow_id: str):
        """#42,级别 A:有**运行中** run → ``409``(终态 run 连同 steps 同事务一并删,否则 FK 让定义永远删不掉)。"""
        p = _principal(request, "admin")
        _wf_or_404(workflow_id)
        try:
            agent.workflows.delete(workflow_id, actor=p.actor)
        except RuntimeError as e:
            if "workflow_has_active_runs" in str(e):
                raise ApiError(409, "RESOURCE_EXHAUSTED", "该工作流仍有运行中的 run", reason="workflow_has_active_runs")
            raise
        return {"ok": True, "deleted": True, "id": workflow_id}

    @app.post(f"{API_PREFIX}/workflows/{{workflow_id}}/run")
    async def run_workflow(request: Request, workflow_id: str):
        """#43,级别 W:``{args, idempotency_key?}`` → ``202 {run_id}``;事件 ``workflow{status:'started'}``。"""
        p = _principal(request, "write")
        _wf_or_404(workflow_id)
        body = await _json_or_empty(request)
        run_id = await agent.workflows.run(workflow_id, body.get("args") or {}, trigger="api", actor=p.actor)
        return JSONResponse(status_code=202, content={"ok": True, "run_id": run_id})

    @app.get(f"{API_PREFIX}/workflows/{{workflow_id}}/runs")
    async def list_workflow_runs(request: Request, workflow_id: str, limit: int = Query(50, ge=1, le=500)):
        """#44:分页。"""
        _principal(request, "read")
        _wf_or_404(workflow_id)
        return {"ok": True, "data": agent.workflows.runs(workflow_id, limit=limit)}

    @app.get(f"{API_PREFIX}/workflows/runs/{{run_id}}")
    async def get_workflow_run(request: Request, run_id: str):
        """#45:``{run, steps:[…]}``,每步留痕。"""
        _principal(request, "read")
        st = agent.workflows.status(run_id)
        if st.get("run") is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"工作流运行不存在:{run_id}")
        return {"ok": True, **st}

    @app.post(f"{API_PREFIX}/workflows/runs/{{run_id}}/cancel")
    async def cancel_workflow_run(request: Request, run_id: str):
        """#46(取消半)。"""
        _principal(request, "write")
        if agent.workflows.status(run_id).get("run") is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"工作流运行不存在:{run_id}")
        return {"ok": True, "cancelled": bool(agent.workflows.cancel(run_id))}

    @app.post(f"{API_PREFIX}/workflows/runs/{{run_id}}/resume")
    async def resume_workflow_run(request: Request, run_id: str):
        """#46(续跑半):从 ``paused`` 由人续跑。"""
        p = _principal(request, "write")
        if agent.workflows.status(run_id).get("run") is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"工作流运行不存在:{run_id}")
        return {"ok": True, "resumed": bool(await agent.workflows.resume(run_id, actor=p.actor))}

    @app.post(f"{API_PREFIX}/workflows/validate")
    async def validate_workflow(request: Request):
        """#47,级别 R:``{yaml}`` → ``{ok, errors:[{line,message}]}``(控制台编辑器用)。"""
        _principal(request, "read")
        body = await request.json()
        if not isinstance(body.get("yaml"), str):
            raise ApiError(400, "INVALID_ARGS", "yaml 必填", reason="bad_yaml", extra={"details": [{"pointer": "/yaml"}]})
        from ..workflow import validate as wf_validate
        return wf_validate(body["yaml"])

    # ------------------------------------------------------------------ #70 / #71 资源预检与自校准
    @app.post(f"{API_PREFIX}/resources/precheck")
    async def resources_precheck(request: Request):
        """#70,级别 R:``{channel}`` → ``{can_add, reason, alternatives}``(不占额度)。"""
        _principal(request, "read")
        body = await _json_or_empty(request)
        channel = body.get("channel")
        if channel not in ("qidian", "qq", "wechat"):
            raise ApiError(400, "INVALID_ARGS", "channel 须为 qidian|qq|wechat", reason="bad_channel", extra={"details": [{"pointer": "/channel"}]})
        can, reason, alts = agent.pool.can_add(channel)
        return {"ok": True, "can_add": can, "reason": reason, "alternatives": alts}

    @app.post(f"{API_PREFIX}/resources/calibrate")
    async def resources_calibrate(request: Request):
        """#71,级别 A:``{apply:false}`` 只回建议值;``apply=true`` 写回 ``resource_pools``(``quota_auto_lower=false`` 时只上调)。

        🔴 同步返回(§3.4.6 #71 原句「返回建议值」);``calibrate()`` 只读 ``health_samples``,毫秒级。
        00 §11.21 [JOB] 把本端点列进「凡 202 {job_id}」与此冲突,取 §3.4.6 的逐字口径,见 rulings R6-58 (g)。"""
        _principal(request, "admin")
        body = await _json_or_empty(request)
        s = agent.calibrator.calibrate(apply=bool(body.get("apply", False)), source="manual")
        return {"ok": True, **s.as_dict()}

    # ------------------------------------------------------------------ #77 / #81 / #102 / #109 系统
    @app.get(f"{API_PREFIX}/system/metrics")
    async def system_metrics(request: Request):
        """#77,级别 R:当前快照(E-19)。``disk_watermark`` 是 **R6-30 的扁平两键**
        (``last_cleanup_at`` / ``last_cleanup_freed_mb``,不嵌套 ``last_cleanup`` 子对象)。"""
        _principal(request, "read")
        snap = agent.pool.snapshot()
        budget = [{"id": r["id"], "quota_mb": int(r["quota_mb"]), "rss_mb": None, "drift_pct": None}
                  for r in agent.store.list_accounts() if r["state"] == "running"]
        return {"ok": True, "disk_watermark": agent.maintenance.watermark_snapshot(),
                "mem_watermark": {"level": agent.pressure.level, "avail_mb": agent.pressure.avail_mb,
                                  "lru_suggest": agent.pressure.lru_suggest() if agent.pressure.blocked() else []},
                "budget_vs_actual": budget, "pools": snap["pools"], "realtime": snap["realtime"]}

    @app.post(f"{API_PREFIX}/system/backup")
    async def system_backup(request: Request):
        """#81,级别 A:立即在线备份 → ``{path, size}``。"""
        _principal(request, "admin")
        path = await asyncio.to_thread(agent.maintenance.backup_once)
        await asyncio.to_thread(agent.maintenance.prune_backups)
        return {"ok": True, "path": path, "size": os.path.getsize(path) if os.path.exists(path) else 0}

    @app.get(f"{API_PREFIX}/system/public-endpoint")
    async def system_public_endpoint(request: Request):
        """#102,级别 R:E-3 自报公网出口。``[api] public_ip_check_interval_s`` 默认 0=关 ⇒ 未探过时三个值为 null。"""
        _principal(request, "read")
        st = agent.store.settings_get("system.public_endpoint") or {}
        return {"ok": True, "public_ip": st.get("public_ip"), "public_ip_v6": st.get("public_ip_v6"),
                "configured_domain": agent.store.settings_get("api.public_domain"),
                "checked_at": iso8601(st["checked_ms"]) if st.get("checked_ms") else None,
                "changed_at": iso8601(st["changed_ms"]) if st.get("changed_ms") else None,
                "probe": {"url": st.get("probe_url"), "unreachable_rounds": int(st.get("unreachable_rounds") or 0)}}

    CLEANUP_DEDUP_MS = 60_000      # R6-24 #109:60 s 防重

    @app.post(f"{API_PREFIX}/system/cleanup/run")
    async def system_cleanup_run(request: Request):
        """#109,级别 W → ``202 {job_id}``(``jobs.kind='system_cleanup'``)。**只删本地**已过保留期的数据与临时文件,
        绝不删远端邮件(基线 §11.11 R4-2;真删服务器邮件走 #64 ``mail_cleanup``)。"""
        p = _principal(request, "write")
        now = agent.clock()
        inflight = agent.store.job_inflight("system_cleanup")
        last = agent.store.job_last("system_cleanup")
        if inflight is not None:
            raise ApiError(409, "RESOURCE_EXHAUSTED", "已有全量清理在跑", reason="cleanup_in_progress",
                           extra={"job_id": inflight["job_id"]})
        if last is not None and now - int(last["created_ms"]) < CLEANUP_DEDUP_MS:
            raise ApiError(409, "RESOURCE_EXHAUSTED", "60 秒内已跑过一次全量清理", reason="cleanup_too_frequent",
                           extra={"job_id": last["job_id"]})
        job_id = agent.store.job_create(kind="system_cleanup", actor=p.actor, params={"trigger": "manual"}, now_ms=now)
        asyncio.create_task(agent.run_cleanup_job(job_id), name=f"job:{job_id}")
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}")
    async def get_job(request: Request, job_id: str):
        """#107:凡 ``202 {job_id}`` 的端点统一用这套查(§3.4.9 R-21)。"""
        _principal(request, "read")
        row = agent.store.job_get(job_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"作业不存在:{job_id}")
        row["params"] = json.loads(row.pop("params_json") or "{}")
        row["result"] = json.loads(row.pop("result_json") or "null")
        row["error"] = json.loads(row.pop("error_json") or "null")
        return {"ok": True, "data": row}

    # ------------------------------------------------------------------ 邮件 /mail(06 §3.2;命名以 06 为准,C-29)
    def _mail_or_503():
        if getattr(agent, "mail", None) is None:
            raise ApiError(503, "NOT_READY", "邮件服务未装配", reason="mail_not_wired", retryable=True)
        return agent.mail

    def _transport_of(p: Principal) -> str:
        """🔴 承重墙(基线 §11.17 ③):``transport`` **只能取自鉴权上下文**,绝不许调用方在 body 里指定。

        控制台会话 = ``console``、loopback = ``local``、邮件入口 = ``email``(邮件入口永远走不到本端点)。
        写审计时 ``console → 'http'``(``audit_log.transport`` 的 CHECK 只收 local/http/email/system,见 rulings R6-58 (n))。"""
        return "local" if p.transport == "local" else "console"

    @app.get(f"{API_PREFIX}/mail/status")
    async def mail_status(request: Request, route: Optional[str] = None, route_id: Optional[str] = None):
        """#56,级别 R:不带 ``route_id`` 回全部路由。"""
        _principal(request, "read")
        return {"ok": True, "data": _mail_or_503().status(route_id or route)}

    @app.get(f"{API_PREFIX}/mail/inbox")
    async def mail_inbox(request: Request, status: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                         q: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
        """#58,级别 R。"""
        _principal(request, "read")
        return {"ok": True, "data": _mail_or_503().ms.inbox_list(status=status, since_ms=_parse_time(since),
                                                                 until_ms=_parse_time(until), q=q, limit=limit)}

    @app.get(f"{API_PREFIX}/mail/inbox/{{inbox_id}}")
    async def mail_inbox_get(request: Request, inbox_id: int):
        """#59,级别 R:含解析字段与 ``reason``。"""
        _principal(request, "read")
        row = _mail_or_503().ms.inbox_get(inbox_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"邮件不存在:{inbox_id}")
        return {"ok": True, "data": row}

    @app.post(f"{API_PREFIX}/mail/inbox/{{inbox_id}}/reparse")
    async def mail_reparse(request: Request, inbox_id: int):
        """#60,级别 W:对 ``PARSE_FAILED/SIG_INVALID/UNSUPPORTED`` 重新解析(模板修正后)。"""
        _principal(request, "write")
        mail = _mail_or_503()
        row = mail.ms.inbox_get(inbox_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"邮件不存在:{inbox_id}")
        if row["status"] not in ("PARSE_FAILED", "SIG_INVALID", "UNSUPPORTED"):
            raise ApiError(409, "NOT_APPLICABLE", f"当前状态 {row['status']} 不可重新解析", reason="bad_status")
        res = mail.reparse(inbox_id)
        return {"ok": True, "status": res}

    @app.get(f"{API_PREFIX}/mail/outbox")
    async def mail_outbox(request: Request, status: Optional[str] = None, kind: Optional[str] = None,
                          limit: int = Query(100, ge=1, le=500)):
        """#61,级别 R。"""
        _principal(request, "read")
        return {"ok": True, "data": _mail_or_503().ms.outbox_list(status=status, kind=kind, limit=limit)}

    @app.post(f"{API_PREFIX}/mail/outbox/{{outbox_id}}/resend")
    async def mail_resend(request: Request, outbox_id: int):
        """#62,级别 W:``DEAD`` 重投(新行,``dedup_key`` 加后缀)。"""
        _principal(request, "write")
        mail = _mail_or_503()
        if mail.ms.outbox_get(outbox_id) is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"出站邮件不存在:{outbox_id}")
        new_id = mail.sender.resend(outbox_id) if mail.sender else None
        return {"ok": True, "outbox_id": new_id}

    @app.post(f"{API_PREFIX}/mail/outbox/{{outbox_id}}/discard")
    async def mail_discard(request: Request, outbox_id: int):
        """#63,级别 W:置 ``DISCARDED``。"""
        _principal(request, "write")
        mail = _mail_or_503()
        if mail.ms.outbox_get(outbox_id) is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"出站邮件不存在:{outbox_id}")
        mail.sender.discard(outbox_id)
        return {"ok": True, "discarded": True}

    @app.post(f"{API_PREFIX}/mail/cleanup/run")
    async def mail_cleanup_run(request: Request):
        """#64,级别 A → ``202 {job_id}``(``jobs.kind='mail_cleanup'``);§2.6.6 只置标志,真清在下一轮取信同连接里。"""
        p = _principal(request, "admin")
        mail = _mail_or_503()
        body = await _json_or_empty(request)
        job_id = agent.store.job_create(kind="mail_cleanup", actor=p.actor,
                                        params={"dry_run": bool(body.get("dry_run", False)), "trigger": "manual"})
        for cleaner in mail.cleaners.values():
            cleaner.manual_requested = True
        agent.store.job_finish(job_id, ok=True, result={"requested_mailboxes": sorted(mail.cleaners)})
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    @app.get(f"{API_PREFIX}/mail/cleanup/log")
    async def mail_cleanup_log(request: Request, limit: int = Query(50, ge=1, le=500)):
        """#65,级别 R。"""
        _principal(request, "read")
        return {"ok": True, "data": _mail_or_503().ms.cleanup_log_list(limit=limit)}

    @app.post(f"{API_PREFIX}/mail/hmac-keys")
    async def mail_hmac_key_put(request: Request):
        """#67,级别 A:``{sender, short_name}`` → 写 Vault ``vault://mail/hmac/cmd/<短名>``(R6-10;确认钥无此端点)。"""
        p = _principal(request, "admin")
        body = await request.json()
        short = str(body.get("short_name") or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", short):
            raise ApiError(400, "INVALID_ARGS", "short_name 须匹配 ^[A-Za-z0-9._-]{1,32}$", reason="short_name_invalid",
                           extra={"details": [{"pointer": "/short_name"}]})
        if not body.get("sender"):
            raise ApiError(400, "INVALID_ARGS", "sender 必填", reason="bad_sender", extra={"details": [{"pointer": "/sender"}]})
        ref = f"mail/hmac/cmd/{short}"
        if agent.store.settings_get(f"mail.hmac.{short}"):
            raise ApiError(400, "INVALID_ARGS", f"短名 {short} 已被占用", reason="short_name_taken",
                           extra={"details": [{"pointer": "/short_name"}]})
        secret = ulid() + ulid()
        await agent.vault.put(ref, secret, scope="mail")
        agent.store.settings_set(f"mail.hmac.{short}", {"sender": str(body["sender"]), "secret_ref": f"vault://{ref}"}, actor=p.actor)
        return {"ok": True, "short_name": short, "secret_ref": f"vault://{ref}", "secret": secret}   # secret 一次性下发,不再回读

    @app.delete(f"{API_PREFIX}/mail/hmac-keys/{{short_name}}")
    async def mail_hmac_key_delete(request: Request, short_name: str):
        """#68,级别 A:按**短名**定位吊销(不按 sender);不存在 → 404。"""
        p = _principal(request, "admin")
        if not agent.store.settings_get(f"mail.hmac.{short_name}"):
            raise ApiError(404, "TARGET_NOT_FOUND", f"短名不存在:{short_name}")
        await agent.vault.delete(f"mail/hmac/cmd/{short_name}")
        agent.store.settings_set(f"mail.hmac.{short_name}", None, actor=p.actor)
        return {"ok": True, "revoked": True, "short_name": short_name}

    @app.get(f"{API_PREFIX}/mail/pending-confirms")
    async def mail_pending_confirms(request: Request):
        """#68b,级别 A:出参**恰八键**(R6-7 定死),直接透传。"""
        _principal(request, "admin")
        return {"ok": True, "data": _mail_or_503().confirms.list() if _mail_or_503().confirms else []}

    def _confirm_reply(outcome) -> JSONResponse:
        if outcome.ok:
            return JSONResponse(status_code=outcome.http_status, content={"ok": True, "code": outcome.code, "id": outcome.inbox_id})
        return JSONResponse(status_code=outcome.http_status,
                            content=_error_body(outcome.code, outcome.reason or outcome.code, reason=outcome.reason,
                                                needs_human=True, extra={"id": outcome.inbox_id}))

    @app.post(f"{API_PREFIX}/mail/pending-confirms/{{inbox_id}}/approve")
    async def mail_confirm_approve(request: Request, inbox_id: int):
        """#68c,级别 A:按 ``{id}`` 操作、**不核验任何 nonce**(v1 `danger_confirm_via` 恒 `console`)。"""
        p = _principal(request, "admin")
        mail = _mail_or_503()
        if mail.confirms is None:
            raise ApiError(503, "NOT_READY", "邮件确认队列未装配", reason="mail_not_wired", retryable=True)
        return _confirm_reply(mail.confirms.approve(inbox_id, actor=p.actor, transport=_transport_of(p)))

    @app.post(f"{API_PREFIX}/mail/pending-confirms/{{inbox_id}}/reject")
    async def mail_confirm_reject(request: Request, inbox_id: int):
        """#68d,级别 A。"""
        p = _principal(request, "admin")
        mail = _mail_or_503()
        if mail.confirms is None:
            raise ApiError(503, "NOT_READY", "邮件确认队列未装配", reason="mail_not_wired", retryable=True)
        return _confirm_reply(mail.confirms.reject(inbox_id, actor=p.actor, transport=_transport_of(p)))

    @app.get(f"{API_PREFIX}/settings/mail/templates/defaults")
    async def mail_templates_defaults(request: Request):
        """随包三份内置模板(06 §2.14.3;**代码常量、不入表**)。"""
        _principal(request, "admin")
        from ..mail.templates import DEFAULT_TEMPLATES
        return {"ok": True, "data": DEFAULT_TEMPLATES}

    @app.post(f"{API_PREFIX}/mail/templates/preview")
    async def mail_template_preview(request: Request):
        """#104,级别 R:用当前出站模板渲染一条消息(不入 ``mail_outbox``、不发送)。"""
        _principal(request, "read")
        body = await request.json()
        ctx = body.get("ctx")
        if not isinstance(ctx, dict):
            raise ApiError(400, "INVALID_ARGS", "ctx 必填(12 核心占位符的取值)", reason="bad_ctx", extra={"details": [{"pointer": "/ctx"}]})
        from ..mail.templates import render_message_mail
        rendered = render_message_mail(cfg.mail.template_out, ctx)
        return {"ok": True, "subject": rendered.subject, "body_text": rendered.body_text, "body_html": rendered.body_html}

    @app.get(f"{API_PREFIX}/settings/mail/routes")
    async def mail_routes_list(request: Request):
        """#105(GET 半),级别 A:路由 CRUD;``inbound.secret``/``outbound.secret`` 只写不读。"""
        _principal(request, "admin")
        return {"ok": True, "data": _mail_or_503().ms.routes_list()}

    @app.put(f"{API_PREFIX}/settings/mail/routes")
    async def mail_routes_upsert(request: Request):
        """#105(PUT 半):按 ``(channel, account_id)`` upsert,写完 ``reload()`` 立即生效。"""
        _principal(request, "admin")
        mail = _mail_or_503()
        body = await request.json()
        ch, aid = body.get("channel"), body.get("account_id")
        if ch is not None and ch not in ("qidian", "qq", "wechat"):
            raise ApiError(400, "INVALID_ARGS", "channel 须为 qidian|qq|wechat 或 null(全局行)", reason="bad_channel",
                           extra={"details": [{"pointer": "/channel"}]})
        row_id = mail.ms.route_upsert(channel=ch, account_id=aid, inbound_json=body.get("inbound") or {},
                                      outbound_json=body.get("outbound") or {}, enabled=bool(body.get("enabled", True)))
        mail.reload()
        return {"ok": True, "id": row_id}

    # ------------------------------------------------------------------ WS /events(02 §3.4.7)
    @app.websocket(f"{API_PREFIX}/events")
    async def ws_events(ws: WebSocket):
        token = ""
        auth = ws.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or ws.query_params.get("token", "")
        row = agent.store.api_client_by_token(token) if token else None
        if row is None:
            await ws.close(code=4401)
            return
        p = principal_from_row(row, transport="local" if (ws.client and ws.client.host in ("127.0.0.1", "::1")) else "http")
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close(code=4400)
            return
        if not isinstance(first, dict) or not isinstance(first.get("subscribe"), dict):   # R6-53:首帧必须是含 subscribe 对象的 JSON
            await ws.close(code=4400)
            return
        sub = first["subscribe"]
        events_f = set(sub.get("events") or [])
        accounts_f = set(sub.get("accounts") or ["*"])
        channels_f = set(sub.get("channels") or [])
        since_seq = sub.get("since_seq")
        last_seq = 0
        if since_seq is not None:
            mn, _mx = agent.store.outbox_seq_bounds()
            if mn is not None and int(since_seq) + 1 < mn:
                await ws.send_json({"replay": "truncated", "from_seq": mn})
                last_seq = mn - 1
            else:
                last_seq = int(since_seq)
        else:
            _mn, mx = agent.store.outbox_seq_bounds()
            last_seq = mx or 0                       # 不带 since_seq = 只要之后的新事件

        def allowed(frame: dict[str, Any]) -> bool:
            if events_f and frame["event"] not in events_f:
                return False
            acc = frame.get("account_id")
            if acc:
                if not p.allows_account(acc):
                    return False
                if "*" not in accounts_f and acc not in accounts_f:
                    return False
            if channels_f and frame.get("channel") and frame["channel"] not in channels_f:
                return False
            return True

        last_ping = time.monotonic()
        try:
            while True:
                rows = agent.store.replay_outbox(last_seq, 500)
                for r in rows:
                    last_seq = r["seq"]
                    frame = {"event": r["event"], "seq": r["seq"], "ts": iso8601(r["ts_ms"]), "trace_id": r["trace_id"], "account_id": r["account_id"],
                             "channel": r["channel"], "payload": r["payload"]}       # 00 §7.5 Event:ts ISO 8601(00 §6)
                    if allowed(frame):
                        await ws.send_json(frame)
                if time.monotonic() - last_ping >= WS_PING_INTERVAL_S:
                    await ws.send_json({"event": "ping"})
                    last_ping = time.monotonic()
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=0.2)   # 客户端可重发订阅帧收窄过滤(01 §2.8)
                    try:
                        data = json.loads(msg)
                        if isinstance(data, dict) and "subscribe" in data:
                            s2 = data["subscribe"] or {}
                            events_f, accounts_f, channels_f = set(s2.get("events") or []), set(s2.get("accounts") or ["*"]), set(s2.get("channels") or [])
                    except ValueError:
                        pass
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            return
        except RuntimeError:
            return

    return app
