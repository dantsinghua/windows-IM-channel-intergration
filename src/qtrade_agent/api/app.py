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
import shutil
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import __version__ as AGENT_VERSION
from ..config import WS_PING_INTERVAL_S
from ..events import iso8601
from ..ids import ulid
from ..models import Command, CommandOrigin, RESULT_CODES
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

    # ------------------------------------------------------------------ 鉴权
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
        if body.get("async"):
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
        p = _principal(request, "read")
        body = await request.json()
        return await _submit(request, p, account_id, body)

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/send")
    async def post_send(request: Request, account_id: str):
        p = _principal(request, "write")
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
