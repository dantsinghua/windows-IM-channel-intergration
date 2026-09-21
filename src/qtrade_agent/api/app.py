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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__ as AGENT_VERSION
from ..config import WS_PING_INTERVAL_S
from ..events import iso8601
from ..hmac_inbound import has_hmac_headers
from ..ids import ulid
from ..mail.route_secrets import BadSecretRef, check_route, upsert_route
from ..maintenance import DiskFullError
from ..models import Command, CommandOrigin, RESULT_CODES
from ..store import SeqExhausted
from ..vault_client import VaultUnavailable
from ..workflow import WorkflowParseError
from .auth import ApiError, Principal, is_unauth_health_source, principal_from_row, require_account, require_level
from .routes_ext import register_ext
from .routes_ext2 import job_is_irreversible, register_ext2
from .serialize import (account_view, command_view, decode_cursor, encode_cursor,
                        mail_cleanup_log_row_view, mail_inbox_row_view, mail_outbox_row_view, mail_route_row_view,
                        message_view, result_view, session_view, stored_result_view, strip_secrets, workflow_run_view,
                        workflow_step_view, workflow_view)

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


#: `settings` 里存「用户填的对外域名」的唯一键(02 #102 逐字 `settings api.public_domain`;07 §2:不在 agent.toml 里)。
#: #88/#89 的 `api` 组与 #102 的 `configured_domain` **必须用同一把键**,否则写得进读不回。
API_PUBLIC_DOMAIN_KEY = "api.public_domain"


def _parse_time(v: Optional[str]) -> Optional[int]:
    if v is None or v == "":
        return None
    if v.isdigit():
        return int(v)
    if " " in v and "T" in v:            # 未编码的 "+08:00" 经 URL 解码成空格:容忍
        v = v.replace(" ", "+")
    return int(datetime.fromisoformat(v).timestamp() * 1000)


def _page_window(cursor: Optional[str], since: Optional[str] = None, until: Optional[str] = None
                 ) -> tuple[Optional[tuple[int, str]], Optional[int], Optional[int]]:
    """C-42 统一分页/时间参数的入参解析(02 §3.4 通用段;游标格式 G-16)。

    `cursor` 解不开 → ``400 INVALID_ARGS(bad_cursor)``(客户端只能透传、不能自己构造,格式变了就从头拉);
    `since`/`until` 非法 → ``400 INVALID_ARGS(bad_time)``。
    🔴 `limit` 由各端点的 ``Query(…, ge=1, le=…)`` 把关 —— **不认识就静默忽略**是最危险的那种(页面会以为自己限过量)。
    """
    before: Optional[tuple[int, str]] = None
    if cursor:
        try:
            before = decode_cursor(cursor)
        except Exception:
            raise ApiError(400, "INVALID_ARGS", "cursor 非法", reason="bad_cursor", extra={"details": [{"pointer": "/cursor"}]})
    try:
        return before, _parse_time(since), _parse_time(until)
    except ValueError:
        raise ApiError(400, "INVALID_ARGS", "since/until 须为 ISO 8601 或毫秒", reason="bad_time",
                       extra={"details": [{"pointer": "/since"}]})


def _next_cursor(rows: list[dict[str, Any]], limit: int, *, ts_key: str, id_key: str = "id") -> Optional[str]:
    """C-42 的 ``next_cursor``:**仅在本页满 `limit` 时非空**(#48 逐字),取值 = 本页末行的 ``(排序列, 主键)``。

    🔴 用的是**权限过滤之前**的那一页:过滤后行数变少不代表没有下一页,拿过滤后的行判满页会把后面的页整段吞掉。
    """
    if not rows or len(rows) < limit:
        return None
    last = rows[-1]
    return encode_cursor(int(last.get(ts_key) or 0), str(last[id_key]))


async def _ws_reject(ws: WebSocket, code: int, reason: str) -> None:
    """§3.4.7:握手被拒时也要让客户端**看得到关闭码**(本册只定义 **4401 / 4400**,**没有 4403**;R6-62 (a))。

    🔴 在 ``accept()`` **之前** ``close()``,Starlette/uvicorn 会退化成「拒绝握手」(HTTP 403),
    浏览器与 ws 客户端只看得到 **1006**(异常关闭)—— 控制台 `ws.ts` 的 `onAuthFailed` 因此永远不触发,
    只会按退避无限重连(联调交接 E-05)。所以这里先 ``accept()`` 建连,再带码关闭。
    """
    try:
        await ws.accept()
    except Exception:                    # 对端已经走了:没有可送达的关闭码,直接收手
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


def create_api(agent) -> FastAPI:
    """``agent`` = app.AgentApp(持 store/bus/events/health/scheduler/cfg/clock/adapters)。"""
    cfg = agent.cfg
    app = FastAPI(title="QTrade Agent", version=AGENT_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    caps, caps_version = load_capabilities()
    caps_by_op = {c["op"]: c for c in caps}
    app.state.capabilities_version = caps_version
    # #52 的 `expires_at` 来源:`jobs.expires_ms` 由 `Store.job_create` 按本值填(R4-13「jobs 随产物」;
    # 键 = `[retention] export_jobs_days`,docs/07 已登记,缺省 7)。此前该列全程没有写点 ⇒ `expires_at` 恒 null。
    agent.store.job_retention_days = int(getattr(cfg.retention, "export_jobs_days", 7))

    # ------------------------------------------------------------------ 横切:版本头 / 错误信封 / 审计
    @app.middleware("http")
    async def versions_and_audit(request: Request, call_next):
        started = agent.clock()
        # 🔴 每个请求一个 trace_id:调用方给了 `X-Trace-Id` 就采纳它,否则本地生成。
        # 它同时进**审计行**与**响应体**(成功响应也带,见 `_inject_trace_id`),这样现场拿着界面上那串
        # 就能直接在 audit_log 里找到对应行(联调交接 S-16:此前成功响应不回 trace_id,两边对不上)。
        request.state.trace_id = (request.headers.get("X-Trace-Id") or "").strip() or ulid(started)
        api_min = request.headers.get("X-QT-Api-Min")
        if api_min:
            try:
                want_major, want_minor = (int(x) for x in api_min.split(".")[:2])
                have_major, have_minor = (int(x) for x in cfg.api.api_version.split(".")[:2])
            except ValueError:
                want_major = want_minor = have_major = have_minor = 0
            if want_major != have_major or want_minor > have_minor:
                resp = JSONResponse(status_code=426, content=_error_body("UPGRADE_REQUIRED", f"需要 API {api_min},当前 {cfg.api.api_version}",
                                                                         reason="api_version", trace_id=request.state.trace_id))
                return _with_version_headers(resp)
        # §3.4 通用行:「鉴权 `Authorization: Bearer <token>`;**公网入站加 HMAC(§3.5)**」——
        # HMAC 是与 Bearer **并列的鉴权方式,适用于 `/api/v1` 全部端点**,不只是写指令那两个。
        # 这里在进路由前把签验一次、把 Principal 挂到 request.state,各端点的 `_principal()` 直接取用;
        # **级别判定仍留在端点**(middleware 不知道该端点要 R/W/A),所以这里 `required_level=None`。
        if request.url.path.startswith(API_PREFIX) and has_hmac_headers(dict(request.headers)) and getattr(agent, "hmac", None) is not None:
            try:
                res = await agent.hmac.verify(method=request.method, path=request.url.path, query=str(request.url.query or ""),
                                              body=await request.body(), headers=dict(request.headers),
                                              client_ip=request.client.host if request.client else None)
                request.state.principal = res.principal
                request.state.hmac = res
            except ApiError as e:
                resp = JSONResponse(status_code=e.http_status, content=_error_body(e.code, e.message, reason=e.reason,
                                                                                   retryable=e.retryable, needs_human=e.needs_human,
                                                                                   trace_id=request.state.trace_id, extra=e.extra))
                resp.headers["Date"] = formatdate(agent.clock() / 1000, usegmt=True)      # §3.5:让对方校时
                return _with_version_headers(resp)
        try:
            response = await call_next(request)
        except ApiError as e:
            response = JSONResponse(status_code=e.http_status, content=_error_body(e.code, e.message, reason=e.reason, retryable=e.retryable,
                                                                                  needs_human=e.needs_human, trace_id=request.state.trace_id,
                                                                                  extra=e.extra))
        except Exception as e:
            # 🔴 **兜底异常处理器**(独立联调 P-4):任何没被上面各 handler 接住的异常,一律包成 00 §10 信封的
            # `INTERNAL`(§8.3 已登记的那个未归类错误码,**不新造原因码**)+ `trace_id`,并把堆栈记进日志。
            # 为什么必须有:裸异常会让 FastAPI 回 `500 text/plain "Internal Server Error"` —— 控制台的
            # `readEnvelope` 拿不到 `code`/`trace_id`(只能显示兜底文案、现场无从按 trace 查审计),
            # 02 §3.4 还注明 500 `INTERNAL` 会触发**自动重试**,而此时连「重试了什么」都查不到。
            # 🔴 **响应体不带堆栈**(§11.9 同源口径:细节只进日志,不进响应);
            # `/system/health` 这个路径按 §3.4 例外① 两种形态都不注入 `trace_id`,这里同样不带。
            log.exception("未捕获异常:%s %s trace_id=%s", request.method, request.url.path, request.state.trace_id)
            is_health = request.url.path == f"{API_PREFIX}/system/health"
            response = JSONResponse(status_code=500, content=_error_body(
                "INTERNAL", "内部错误,请把 trace_id 交给运维排查" if not is_health else "内部错误",
                reason="unhandled_exception", retryable=True,
                trace_id=None if is_health else request.state.trace_id))
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
        # `#72 /system/health`:02 §3.4 例外①(R6-62 (b) + Ⅵ W1)逐字 =「**这个路径**(免鉴权摘要与带令牌全量
        # **两种形态都不注入** `trace_id`)」—— 排除**按 path 整端点**做,与带不带令牌无关(两种形态的键集
        # 都由 02 #72 行逐字定死)。总控 2026-09-21 复核裁决:维持本实现,不收窄成「只限免鉴权摘要」。
        if request.url.path == f"{API_PREFIX}/system/health":
            return _with_version_headers(response)
        return _with_version_headers(await _inject_trace_id(response, request.state.trace_id))

    async def _inject_trace_id(response: Response, trace_id: str) -> Response:
        """给 JSON 响应体补 ``trace_id``(**成功响应也带**,总控裁决;与错误信封同名字段)。

        已经自带非空 ``trace_id`` 的(``CommandResult``、指令 202)原样不动 —— 那是指令的 trace,比请求级的更准。
        非 JSON 响应(截图字节、CSV 导出)一个字节都不碰。``call_next`` 回的是 streaming 响应,
        读完 ``body_iterator`` 后必须重建一个普通 ``Response``(并去掉旧的 ``content-length``,否则长度对不上)。
        """
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response
        if hasattr(response, "body_iterator"):
            raw = b"".join([chunk async for chunk in response.body_iterator])       # type: ignore[attr-defined]
            rebuilt = True
        else:
            raw, rebuilt = bytes(response.body), False
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict) and not data.get("trace_id"):
            data["trace_id"] = trace_id
            raw = json.dumps(data, ensure_ascii=False).encode()
            rebuilt = True
        if not rebuilt:
            return response
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=raw, status_code=response.status_code, headers=headers)

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
            trace_id=getattr(request.state, "trace_id", None),
            extra={"hint_actions": ["open_env", "run_cleanup"], "evidence": e.evidence()})))

    @app.exception_handler(SeqExhausted)
    async def _seq_exhausted(request: Request, e: SeqExhausted):
        """00 §6 `NN=01–98` 且**永不复用** ⇒ 一个通道累计建满 98 个号后必然建不了。

        这是**可预见的业务边界**,按 00 §8.3/§10 回 `RESOURCE_EXHAUSTED`(资源池不足,**HTTP 409**,
        `retryable=false`);此前 `Store._next_seq` 抛裸 `ValueError` ⇒ `500 text/plain`,控制台只能显示
        「请求失败」,而 500 `INTERNAL` 还会被当成服务器故障**自动重试**(02 §3.4),重试多少次都建不出来。
        `needs_human=true`:要人去清理/归并旧号(软删的号也占着 seq,这是 00 §6 的设计,不是 bug)。
        """
        return _with_version_headers(JSONResponse(status_code=409, content=_error_body(
            "RESOURCE_EXHAUSTED", f"{e.channel} 通道的账号序号已用尽(NN 上限 {e.max_seq},分配后永不复用)",
            reason="seq_exhausted", retryable=False, needs_human=True,
            trace_id=getattr(request.state, "trace_id", None), extra={"channel": e.channel, "max_seq": e.max_seq})))

    #: 未知路由 / 方法不对时的结果码(00 §10;FastAPI 默认的 `{"detail": "Not Found"}` **不是**本项目的信封,
    #: 控制台的 `readEnvelope` 见到它只能退化成「请求失败(HTTP 404)」,trace_id 也对不上日志)
    HTTP_EXC_CODES = {400: "INVALID_ARGS", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                      405: "METHOD_NOT_ALLOWED", 409: "RESOURCE_EXHAUSTED", 429: "RATE_LIMITED", 503: "NOT_READY"}

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, e: StarletteHTTPException):
        """把框架自己抛的 HTTPException(未知路由 404、方法不对 405…)包成 00 §10 信封。"""
        code = HTTP_EXC_CODES.get(e.status_code, "INTERNAL" if e.status_code >= 500 else "INVALID_ARGS")
        detail = e.detail if isinstance(e.detail, str) else json.dumps(e.detail, ensure_ascii=False)
        message = {404: f"路由不存在:{request.method} {request.url.path}",
                   405: f"方法不被允许:{request.method} {request.url.path}"}.get(e.status_code, detail)
        reason = {404: "unknown_route", 405: "method_not_allowed"}.get(e.status_code, "http_error")
        resp = JSONResponse(status_code=e.status_code, content=_error_body(
            code, message, reason=reason, trace_id=getattr(request.state, "trace_id", None)))
        for k, v in (getattr(e, "headers", None) or {}).items():       # 405 的 Allow 头要留着
            resp.headers[k] = v
        return _with_version_headers(resp)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, e: RequestValidationError):
        """422 校验错误同样包成信封:``INVALID_ARGS`` + 每条 ``details[].pointer``(00 §10 的 pointer 口径)。"""
        details = []
        for err in e.errors():
            loc = [str(x) for x in (err.get("loc") or []) if str(x) not in ("body", "query", "path", "header")]
            details.append({"pointer": "/" + "/".join(loc), "message": err.get("msg", ""), "kind": err.get("type", "")})
        return _with_version_headers(JSONResponse(status_code=422, content=_error_body(
            "INVALID_ARGS", "入参校验未过", reason="validation_error",
            trace_id=getattr(request.state, "trace_id", None), extra={"details": details})))

    # ------------------------------------------------------------------ 鉴权
    def _principal(request: Request, required: str = "read") -> Principal:
        """Bearer 与 HMAC 两条路都收口在这里:HMAC 已由 middleware 验过并挂在 ``request.state.principal``,
        **级别判定在这里做**(middleware 不知道各端点要 R/W/A)。"""
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is not None:
            require_level(p, required)
            return p
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
        return _principal_from_token(token, required, request)

    async def _principal_async(request: Request, required: str = "read") -> Principal:
        """兼容别名:HMAC 的签验已提前到 middleware,这里只是保留调用点的 ``await`` 形态。"""
        return _principal(request, required)

    def _grace_client(token: str) -> Optional[dict[str, Any]]:
        """#92 轮换的**旧凭据宽限期**:``api_clients`` 只有一列 ``secret_hash``,旧 hash 与到期时刻
        统一挂在 ``settings['api_clients.grace']`` 的 ``{app_id: {old_hash, until_ms}}`` 里(单键 map,
        免得为了查一次宽限去扫 settings 的键前缀)。过期、已吊销、已停用的一律不认。"""
        grace = agent.store.settings_get("api_clients.grace") or {}
        if not isinstance(grace, dict) or not grace:
            return None
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = agent.clock()
        for app_id, blob in grace.items():
            if not isinstance(blob, dict) or blob.get("old_hash") != h:
                continue
            if int(blob.get("until_ms") or 0) <= now:
                return None
            row = agent.store.api_client_get(app_id)
            if row and row.get("enabled") and not row.get("revoked_ms"):
                return row
        return None

    def _principal_from_token(token: str, required: str, request) -> Principal:
        if not token:
            raise ApiError(401, "UNAUTHORIZED", "缺少 Bearer 令牌", reason="missing_token")
        row = agent.store.api_client_by_token(token) or _grace_client(token)
        if row is None:
            raise ApiError(401, "UNAUTHORIZED", "令牌无效或已过期", reason="bad_token")
        p = principal_from_row(row, transport="local" if (request.client and request.client.host in ("127.0.0.1", "::1")) else "http")
        require_level(p, required)
        agent.store.api_client_touch(p.app_id, agent.clock())
        request.state.principal = p
        return p

    # ------------------------------------------------------------------ system
    ACCOUNT_HEALTH_CODES = {"H04": "H04_CONTAINER_EXITED", "H05": "H05_BOOT_INCOMPLETE", "H06": "H06_ADB_OFFLINE",
                            "H07": "H07_SCRCPY_STALLED", "H08": "H08_NAPCAT_HEARTBEAT_LOST"}

    def _per_account_checks() -> dict[str, dict[str, str]]:
        """``#72 checks.accounts``:每账号的 H04/H05/H06/H07/H08(01 §2.7.3.4 的五个状态点)。

        判据与全局 ``checks`` 同源:该码在 ``alerts.active`` 里对 ``subject='account:<id>'`` 有行 ⇒ ``firing``;
        该健康项**跑过**(健康循环的 ``last[…]`` 里有这个账号)⇒ ``ok``;否则 ``unknown``。
        H07(前台流 10 s 无帧)本期没有执行体 ⇒ 恒 ``unknown``,**不假装 ok**。
        """
        firing = {(code, subject) for code, subject in agent.alerts.active}
        ran = {"H04": set(agent.healthloop.last["H04"]), "H05": set(agent.healthloop.last["H05"]),
               "H06": set(agent.healthloop.last["H06"]), "H07": set(),
               "H08": set(getattr(agent, "qqhealth", None).last) if getattr(agent, "qqhealth", None) else set()}
        out: dict[str, dict[str, str]] = {}
        for row in agent.store.list_accounts():
            aid = row["id"]
            out[aid] = {h: ("firing" if (code, f"account:{aid}") in firing else ("ok" if aid in ran[h] else "unknown"))
                        for h, code in ACCOUNT_HEALTH_CODES.items()}
        return out

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
                               "H24": "unknown" if agent.pressure.level == "unknown" else ("ok" if agent.pressure.level == "ok" else "firing"),
                               # 01 §2.7.3.4 运行时 Tab 要的 per-account 维度:H04/H05/H06/H07/H08 本来就是
                               # `subject=account:<id>` 级别的健康项(02 §3.7),而 #72 原来只给全局映射。
                               # 全局那几个键**原样保留**(02 为准),per-account 作为**补充子键**挂在 checks.accounts 下。
                               "accounts": _per_account_checks()},
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
                            include_stopped: bool = True, include_deleted: bool = False,
                            since: Optional[str] = None, until: Optional[str] = None,
                            limit: int = Query(100, ge=1, le=500), cursor: Optional[str] = None):
        """#1,级别 R。**C-42 统一分页**:``?since&until&limit&cursor`` → ``{ok, data, next_cursor}``
        (排序 = ``created_ms`` 降序,游标 G-16;`limit` 真生效,不静默忽略)。"""
        p = _principal(request, "read")
        before, since_ms, until_ms = _page_window(cursor, since, until)
        rows = agent.store.list_accounts_page(channel=channel, state=state, enabled=enabled, include_deleted=include_deleted,
                                              since_ms=since_ms, until_ms=until_ms, limit=limit, before=before)
        nxt = _next_cursor(rows, limit, ts_key="created_ms")
        rows = [r for r in rows if p.allows_account(r["id"]) and (include_stopped or r["state"] != "stopped")]
        return {"ok": True, "data": [account_view(r, _acct_caps(r)) for r in rows], "next_cursor": nxt}

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
                            since: Optional[str] = None, until: Optional[str] = None,
                            limit: int = Query(100, ge=1, le=500), cursor: Optional[str] = None):
        """#26,级别 R。**C-42 统一分页**:排序 = ``last_msg_ms`` 降序(空值按 0),游标 G-16。"""
        p = _principal(request, "read")
        before, since_ms, until_ms = _page_window(cursor, since, until)
        rows = agent.store.list_sessions_page(account_id=account_id, keyword=keyword, kind=kind,
                                              since_ms=since_ms, until_ms=until_ms, limit=limit, before=before)
        nxt = _next_cursor(rows, limit, ts_key="last_msg_ms")
        return {"ok": True, "data": [session_view(r) for r in rows if p.allows_account(r["account_id"])], "next_cursor": nxt}

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

    # ------------------------------------------------------------------ #95 审计(02 §3.4.6)
    AUDIT_KINDS = ("command", "api", "system", "stream_input")      # C-40 加 kind;stream_input = R-06 画面注入留痕

    @app.get(f"{API_PREFIX}/audit")
    async def list_audit(request: Request, kind: Optional[str] = None, actor: Optional[str] = None,
                         account_id: Optional[str] = None, action: Optional[str] = None,
                         since: Optional[str] = None, until: Optional[str] = None,
                         limit: int = Query(100, ge=1, le=1000), cursor: Optional[str] = None,
                         fmt: Optional[str] = None):
        """#95,级别 R(**A 可看全部 actor**,非 A 只看自己)。``?fmt=csv`` 导出;分页用 ``cursor``(G-16 同款)。"""
        p = _principal(request, "read")
        if kind is not None and kind not in AUDIT_KINDS:
            raise ApiError(400, "INVALID_ARGS", f"kind 须为 {'|'.join(AUDIT_KINDS)}", reason="bad_kind",
                           extra={"details": [{"pointer": "/kind"}]})
        try:
            since_ms, until_ms = _parse_time(since), _parse_time(until)
        except ValueError:
            raise ApiError(400, "INVALID_ARGS", "since/until 须为 ISO 8601 或毫秒", reason="bad_time",
                           extra={"details": [{"pointer": "/since"}]})
        sql, params = "SELECT * FROM audit_log WHERE 1=1", []
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if account_id:
            require_account(p, account_id)
            sql += " AND account_id=?"; params.append(account_id)
        if action:
            sql += " AND action=?"; params.append(action)
        if since_ms is not None:
            sql += " AND ts_ms >= ?"; params.append(since_ms)
        if until_ms is not None:
            sql += " AND ts_ms <= ?"; params.append(until_ms)
        if p.can("admin"):
            if actor:
                sql += " AND actor=?"; params.append(actor)
        else:
            sql += " AND actor=?"; params.append(p.actor)          # 非 A 级只看自己(``?actor=`` 无效)
        if cursor:
            try:
                _ts, last_id = decode_cursor(cursor)
            except Exception:
                raise ApiError(400, "INVALID_ARGS", "cursor 非法", reason="bad_cursor", extra={"details": [{"pointer": "/cursor"}]})
            sql += " AND id < ?"; params.append(int(last_id))
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        rows = [dict(r) for r in agent.store.con.execute(sql, params).fetchall()]
        if fmt == "csv":
            return Response(content=_audit_csv(rows), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="audit.csv"'})
        if fmt not in (None, "json"):
            raise ApiError(400, "INVALID_ARGS", "fmt 须为 json|csv", reason="bad_fmt", extra={"details": [{"pointer": "/fmt"}]})
        nxt = encode_cursor(rows[-1]["ts_ms"], str(rows[-1]["id"])) if len(rows) == limit else None
        return {"ok": True, "data": rows, "next_cursor": nxt}

    AUDIT_CSV_COLS = ("id", "ts_ms", "kind", "transport", "actor", "action", "account_id", "trace_id", "result_code", "detail_json")

    def _audit_csv(rows: list[dict[str, Any]]) -> str:
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(AUDIT_CSV_COLS)
        for r in rows:
            w.writerow([("" if r.get(c) is None else r.get(c)) for c in AUDIT_CSV_COLS])
        return buf.getvalue()

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
        return {"ok": True, "data": [workflow_view(w, with_yaml=False) for w in agent.workflows.list()]}

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
        return {"ok": True, "data": workflow_view(wf)}

    @app.get(f"{API_PREFIX}/workflows/{{workflow_id}}")
    async def get_workflow(request: Request, workflow_id: str):
        """#40:含 yaml。"""
        _principal(request, "read")
        return {"ok": True, "data": workflow_view(_wf_or_404(workflow_id))}

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
        return {"ok": True, "data": workflow_view(wf)}

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
    async def list_workflow_runs(request: Request, workflow_id: str, since: Optional[str] = None,
                                 until: Optional[str] = None, limit: int = Query(50, ge=1, le=500),
                                 cursor: Optional[str] = None):
        """#44:分页(02 #44「分页」= C-42 ``since/until/limit/cursor`` → ``{data, next_cursor}``;此前收 ``limit`` 却不回
        ``next_cursor`` ⇒ 静默截断)。排序 ``(started_ms, run_id)`` 降序;游标**先按库行算、再转视图**。"""
        _principal(request, "read")
        _wf_or_404(workflow_id)
        before, since_ms, until_ms = _page_window(cursor, since, until)
        rows = agent.workflows.runs(workflow_id, limit=limit, since_ms=since_ms, until_ms=until_ms, before=before)
        nxt = _next_cursor(rows, limit, ts_key="started_ms", id_key="run_id")
        return {"ok": True, "data": [workflow_run_view(r) for r in rows], "next_cursor": nxt}

    @app.get(f"{API_PREFIX}/workflows/runs/{{run_id}}")
    async def get_workflow_run(request: Request, run_id: str):
        """#45:``{run, steps:[…]}``,每步留痕。"""
        _principal(request, "read")
        st = agent.workflows.status(run_id)
        if st.get("run") is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"工作流运行不存在:{run_id}")
        return {"ok": True, "run": workflow_run_view(st["run"]), "steps": [workflow_step_view(x) for x in st["steps"]]}

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
        """#71,级别 A:``{apply:false}`` 只算建议值;``apply=true`` 写回 ``resource_pools``(``quota_auto_lower=false`` 时只上调)。

        🔴 **总控裁决(rulings R6-58 (ao)):按 00 §11.21 [JOB] 统一走 ``202 {job_id}``**(00 优先于 02 §3.4.6
        「返回建议值」的同步写法);结果经 #107 ``GET /jobs/{job_id}`` 取,终态推 ``job`` 事件。"""
        p = _principal(request, "admin")
        body = await _json_or_empty(request)
        apply = bool(body.get("apply", False))
        job_id = agent.store.job_create(kind="resources_calibrate", actor=p.actor, params={"apply": apply, "scope": "global"})
        agent.spawn_job(job_id, "resources_calibrate", lambda: agent.calibrate_job_body(job_id, apply=apply, source="manual"))
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    # ------------------------------------------------------------------ #77 / #81 / #102 / #109 系统
    @app.get(f"{API_PREFIX}/system/metrics")
    async def system_metrics(request: Request):
        """#77,级别 R:当前快照(E-19)。``disk_watermark`` 是 **R6-30 的扁平两键**
        (``last_cleanup_at`` / ``last_cleanup_freed_mb``,不嵌套 ``last_cleanup`` 子对象)。"""
        _principal(request, "read")
        snap = agent.pool.snapshot()
        budget = [{"id": r["id"], "quota_mb": int(r["quota_mb"]), "rss_mb": None, "drift_pct": None}
                  for r in agent.store.list_accounts() if r["state"] == "running"]
        # 01 §2.7.2a 右栏 CPU 要「每容器/每进程 cpu_pct」,而 02 #77 的 ours.procs 只有三个内存键。
        # 这里**保留 02 的三个内存键不动**(02 为准),另补 `procs_detail:[{name, rss_mb, cpu_pct}]`;
        # monitor 采样(health_samples 的写入方)本期没人做 ⇒ 值一律 null,**不编造**(04 §2.4.5)。
        procs = {"agent_mb": None, "winagent_mb": None, "console_mb": None}
        procs_detail = [{"name": n, "rss_mb": None, "cpu_pct": None} for n in ("agent", "winagent", "console")]
        watermark = dict(agent.maintenance.watermark_snapshot())
        watermark.setdefault("vhdx_grown_mb", None)       # 02 #77 列了这一键;VHDX 增量没有采集执行体 ⇒ null
        return {"ok": True, "disk_watermark": watermark,
                "mem_watermark": {"level": agent.pressure.level, "avail_mb": agent.pressure.avail_mb,
                                  "lru_suggest": agent.pressure.lru_suggest() if agent.pressure.blocked() else []},
                "budget_vs_actual": budget, "pools": snap["pools"], "realtime": snap["realtime"],
                "hardware": _hardware_snapshot(),
                "ours": {"procs": procs, "procs_detail": procs_detail,
                         # 02 #77 的 `ours.accounts[]` 列的是 anon_mb/current_mb/cpu_pct/quota_mb
                         # (`rss_mb` 是 budget_vs_actual 那一组的键);采样缺失时一律 null,不编造
                         "accounts": [{"id": b["id"], "quota_mb": b["quota_mb"], "anon_mb": None, "current_mb": None,
                                       "rss_mb": None, "cpu_pct": None} for b in budget],
                         "wechat": {"chatlog_mb": None, "wechat_pc_mb": None},      # 微信侧体积没有采集方(04 §2.4.5)
                         "storage": _storage_snapshot()}}

    def _hardware_snapshot() -> dict[str, Any]:
        """#77 的 ``hardware`` 组(整机):``{mem, cpu, disks}``。

        数据源 = monitor 采样器的 ``ProcReader``(读 ``/proc/meminfo``);拿不到的项一律 ``null``,
        ``vmmem_mb`` 是 Windows 侧的量、Agent 这边根本没有来源 ⇒ 恒 ``null``(04 §2.4.5「不编造」)。
        """
        reader = getattr(agent.sampler, "_reader", None)
        mem: dict[str, Any] = {"total_mb": None, "used_mb": None, "avail_mb": agent.pressure.avail_mb, "vmmem_mb": None}
        load_pct: Optional[float] = None
        if reader is not None:
            try:
                raw = reader.meminfo() or {}         # 键 = /proc/meminfo 原名(MemTotal/MemAvailable),单位已换算成 MB
                total, avail = raw.get("MemTotal"), raw.get("MemAvailable")
                mem["total_mb"] = total
                mem["avail_mb"] = avail if avail is not None else mem["avail_mb"]
                mem["used_mb"] = round(total - avail, 1) if (total is not None and avail is not None) else None
            except Exception as e:
                log.debug("#77 读整机内存失败:%s", e)
            try:
                load_pct = reader.cpu_pct()
            except Exception:
                load_pct = None
        disks: list[dict[str, Any]] = []
        data_dir = agent.data_dir
        try:
            du = shutil.disk_usage(data_dir)
            disks.append({"mount": data_dir, "total_mb": du.total // 1048576, "free_mb": du.free // 1048576})
        except OSError:
            pass
        return {"mem": mem, "cpu": {"logical_cores": os.cpu_count(), "load_pct": load_pct}, "disks": disks}

    def _storage_snapshot() -> dict[str, Any]:
        """#77 的 ``ours.storage``:只报**量得到**的两项(agent.db、media),其余无采集方 ⇒ ``null``。"""
        try:
            sizes = agent.maintenance.sizes()
        except Exception:
            sizes = {}
        return {"db_mb": sizes.get("db_size_mb"), "media_mb": sizes.get("media_size_mb"),
                "mail_mb": None, "accounts_mb": None, "backup_mb": None, "vhdx_mb": None}

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
                "configured_domain": agent.store.settings_get(API_PUBLIC_DOMAIN_KEY),
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
        agent.spawn_job(job_id, "system_cleanup", agent.cleanup_job_body)
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}")
    async def get_job(request: Request, job_id: str):
        """#107:凡 ``202 {job_id}`` 的端点统一用这套查(§3.4.9 R-21)。

        🔴 **出参的时间键是 `*_at`(ISO 8601 带时区偏移)**,不是库列的 `*_ms` —— 02 #107 逐字定死
        ``created_at/updated_at/expires_at``,00 §6/§7 也明写「API 与事件的时间一律 ISO 8601」;
        毫秒整数只活在 ``jobs`` 表的列上(联调交接 S-05)。``actor/attempt_count/params/account_id``
        是 02 没列但对排障有用的补充键,原样保留。
        """
        _principal(request, "read")
        row = agent.store.job_get(job_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"作业不存在:{job_id}")
        row["params"] = json.loads(row.pop("params_json") or "{}")
        row["result"] = json.loads(row.pop("result_json") or "null")
        row["error"] = json.loads(row.pop("error_json") or "null")
        for col, key in (("created_ms", "created_at"), ("updated_ms", "updated_at"), ("expires_ms", "expires_at")):
            ms = row.pop(col, None)
            row[key] = iso8601(ms) if ms is not None else None
        return {"ok": True, "data": row}

    @app.post(f"{API_PREFIX}/jobs/{{job_id}}/cancel")
    async def cancel_job(request: Request, job_id: str):
        """#108,级别 W:取消 ``queued/running`` 作业;**不可取消的终态 → 409**(00 §11.21 [JOB])。"""
        p = _principal(request, "write")
        row = agent.store.job_get(job_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"作业不存在:{job_id}")
        if row["state"] not in ("queued", "running"):
            raise ApiError(409, "NOT_CANCELLABLE", f"作业已处于终态 {row['state']},不可取消", reason="job_terminal",
                           extra={"job_id": job_id, "state": row["state"]})
        # 02 #108 逐字:`account_purge`/`wechat_reinstall` 进入**不可逆阶段**后 `409 NOT_CANCELLABLE`
        # (判据由 routes_ext2 的 `_mark_irreversible` 打在 params 上;`messages_purge` 同理)
        if job_is_irreversible(row):
            raise ApiError(409, "NOT_CANCELLABLE", f"{row['kind']} 已进入不可逆阶段,不可取消", reason="irreversible",
                           extra={"job_id": job_id, "state": row["state"]})
        cancelled_task = await agent.cancel_job(job_id)          # 在跑的 task 真 cancel(`job` 事件由作业体收尾时发)
        if not cancelled_task:
            agent.events.emit("job", payload={"job_id": job_id, "kind": row["kind"], "state": "cancelled"})
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="job.cancel",
                                 result_code="OK", detail={"job_id": job_id, "kind": row["kind"]}, now_ms=agent.clock())
        return {"ok": True, "job_id": job_id, "state": "cancelled"}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/calibrate")
    async def calibrate_account(request: Request, account_id: str):
        """#25,级别 A:**单账号**自校准 → ``202 {job_id}``,结果写 ``resource_pools.calibration_json``(与 #71 全局校准并存)。

        单账号只量该账号所在通道的 ``quota_mb``(04 §2.5.3 的「running 稳定 ≥10min 后取 p95」),
        不动 ``wsl/windows`` 的 total/reserved —— 那是整机量,归 #71。
        """
        p = _principal(request, "admin")
        require_account(p, account_id)
        row = agent.accounts.get(account_id)
        request.state.account_id = account_id
        job_id = agent.store.job_create(kind="resources_calibrate", actor=p.actor, account_id=account_id,
                                        params={"scope": "account", "channel": row["channel"]})
        agent.spawn_job(job_id, "resources_calibrate",
                        lambda: agent.calibrate_job_body(job_id, account_id=account_id, channel=row["channel"]))
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    # ------------------------------------------------------------------ #76b / #79 探测采样与自检(console 交接的两条端点冲突)
    @app.get(f"{API_PREFIX}/system/probes")
    async def system_probes(request: Request, kind: str = "result", run_id: Optional[str] = None):
        """#76:``?kind=result`` 读 ``probe_results``(转 WinAgent ``GET /wa/v1/probes``);
        ``?kind=observed`` 读 ``probe_targets_observed``(C-1 实测采样)。

        🔴 **采样行的稳定 id = `id` 列**(02 §3.2 `probe_targets_observed` DDL 的 `INTEGER PRIMARY KEY`):
        #76b 的 `observed_ids` 就是它,控制台不得用「行下标」顶(见 rulings R6-58 (bq))。
        """
        _principal(request, "read")
        if kind not in ("result", "observed"):
            raise ApiError(400, "INVALID_ARGS", "kind 须为 result|observed", reason="bad_kind", extra={"details": [{"pointer": "/kind"}]})
        if kind == "observed":
            # 🔴 `probe_targets_observed` 在 **winagent.db**、Agent 够不着 ⇒ 只能转发。
            # WinAgent 侧补上 `GET /wa/v1/probes?kind=observed`(rulings (ci))就自然走通;
            # 还没补时它回 404 ⇒ **诚实报上游缺口**(503 + 明确 reason),不假装有数据。
            body = await _wa_probes("GET", "/wa/v1/probes?kind=observed", missing_reason="wa_observed_endpoint_missing",
                                    missing_hint="需补 GET /wa/v1/probes?kind=observed")
            # `targets` = 正式探测目标(`settings['probe.targets']`,元素 "host:port",裁决 A-16);
            # 行上的 `in_config` 由 WinAgent 派生 —— 04 §2.8.4 的面板靠它默认只勾新增项。
            return {"ok": True, "kind": "observed", "data": (body or {}).get("observed") or [],
                    "targets": (body or {}).get("targets") or []}
        body = await _wa_probes("GET", "/wa/v1/probes" + (f"?run_id={run_id}" if run_id else "?latest=1"))
        return {"ok": True, "kind": "result", "data": (body or {}).get("results") or []}

    async def _wa_probes(method: str, path: str, *, json_body: Optional[dict[str, Any]] = None,
                         missing_reason: Optional[str] = None, missing_hint: str = "") -> Optional[dict[str, Any]]:
        """转发到 WinAgent 的探测端点;**404 与「不可达 / 其它错」分成两个 reason**,现场一眼能分清是
        「上游还没这个端点」还是「上游挂了」。"""
        try:
            res = await agent.winagent.request(method, path, json=json_body, timeout_s=3.0, retry=(method == "GET"))
        except Exception as e:
            raise ApiError(503, "NOT_READY", f"WinAgent 不可达:{e}", reason="winagent_offline", retryable=True)
        status, body = res
        if status == 404 and missing_reason:
            raise ApiError(503, "NOT_READY", f"WinAgent 尚未提供该端点({missing_hint})", reason=missing_reason,
                           retryable=False, needs_human=True)
        if status != 200:
            raise ApiError(503, "NOT_READY", f"WinAgent 回 {status}", reason="winagent_error", retryable=True)
        return body

    @app.put(f"{API_PREFIX}/settings/probe")
    async def adopt_probe_targets(request: Request):
        """#76b,级别 A:``{observed_ids:[…]}`` → 写选中行 ``adopted_ms`` 并更新 ``probe.targets``,回 ``{adopted, targets}``。

        🔴 **入参形态以 02 #76b 为准**(端点 owner 是 02):给的是**采样行 id**,不是 01 §2.7.9 写的 `{targets:[…]}`。
        传 `targets` 一律 400 并指明改用 `observed_ids`,免得前端按 01 写完才在真机上发现对不上。
        """
        p = _principal(request, "admin")
        body = await _json_or_empty(request)
        if "targets" in body and "observed_ids" not in body:
            raise ApiError(400, "INVALID_ARGS", "本端点收的是采样行 id:{observed_ids:[…]}(02 #76b),不是 {targets:[…]}",
                           reason="use_observed_ids", extra={"details": [{"pointer": "/observed_ids"}]})
        ids = body.get("observed_ids")
        # 🔴 裁决 A-14(04 §2.8.4「替换整表不追加,让用户能删旧项」):`observed_ids` 是**采纳后的全集**,
        #    不在里面的已采纳行会被取消采纳;**`[]` 是合法入参 = 清空正式目标表**,不能当「缺参」挡掉。
        if not isinstance(ids, list) or any(not isinstance(x, int) or isinstance(x, bool) for x in ids):
            raise ApiError(400, "INVALID_ARGS", "observed_ids 须为整数数组(采纳后的全集;[] = 清空)",
                           reason="bad_observed_ids", extra={"details": [{"pointer": "/observed_ids"}]})
        if len(set(ids)) != len(ids):
            raise ApiError(400, "INVALID_ARGS", "observed_ids 有重复", reason="duplicate_ids",
                           extra={"details": [{"pointer": "/observed_ids"}]})
        body = await _wa_probes("PUT", "/wa/v1/probes/adopt", json_body={"observed_ids": ids},
                                missing_reason="wa_adopt_endpoint_missing",
                                missing_hint="需补 PUT /wa/v1/probes/adopt 写 adopted_ms 与 probe.targets")
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "probe", "observed_ids": ids}, now_ms=agent.clock())
        return {"ok": True, "adopted": (body or {}).get("adopted") or [], "targets": (body or {}).get("targets") or [],
                "hosts_by_channel": (body or {}).get("hosts_by_channel") or {},
                "adopted_rows": (body or {}).get("adopted_rows") or []}

    @app.get(f"{API_PREFIX}/system/selftest")
    async def system_selftest_latest(request: Request, run_id: Optional[str] = None):
        """01 §2.7.9 要的「页面直开就能看上次结果」:不带 ``run_id`` = **最近一轮**;带则等价于 #79。

        02 只有 ``#79 GET /system/selftest/{run_id}``,没有「最近一轮」入口(见 rulings R6-58 (br));
        本端点是**补的兄弟端点**,#79 原样保留。没跑过 ⇒ ``{ok:true, data:null}``(不是 404,页面要能显示「暂无」)。
        """
        _principal(request, "read")
        rid = run_id or agent.store.settings_get("system.selftest.last")
        return {"ok": True, "run_id": rid, "data": agent.store.settings_get(f"system.selftest.{rid}") if rid else None}

    @app.get(f"{API_PREFIX}/system/selftest/{{run_id}}")
    async def system_selftest_run(request: Request, run_id: str):
        """#79:按 ``run_id`` 读自检结果;不存在 404。"""
        _principal(request, "read")
        data = agent.store.settings_get(f"system.selftest.{run_id}")
        if data is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"自检轮次不存在:{run_id}")
        return {"ok": True, "run_id": run_id, "data": data}

    # ------------------------------------------------------------------ #88 / #89 配置分组(02 §3.4.6)
    #: §3.4.6 #88 的 ``group`` 枚举;值 = ``AgentConfig`` 上的字段名(``adapters``/``log``/``resources`` 另行组装)
    SETTINGS_GROUPS: dict[str, Optional[str]] = {
        "api": "api", "winagent": "winagent", "runtime": "runtime", "pool": "pool", "bus": "bus",
        "adapters": None, "asr": None, "ocr": None, "messages": "messages", "media": "media",
        "retention": "retention", "events": "events", "mail": None, "log": None, "resources": None,
    }
    ADMIN_GROUPS = ("api", "mail")           # #88 的级别列:`api|mail` 组要 A,其余 R
    SECRET_KEYS = ("secret", "password", "token")
    DATA_CLASS_DAYS_MAX = 30                 # E-18:数据类 *_days 上限 30(files_days/export_jobs_days 等更严的不受此限)
    DATA_CLASS_DAYS_KEYS = ("messages_days", "commands_days", "audit_days", "mail_inbox_rows_days")

    def _as_dict(obj: Any) -> dict[str, Any]:
        import dataclasses
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            v = getattr(obj, f.name)
            out[f.name] = list(v) if isinstance(v, tuple) else v
        return out

    def _group_view(group: str) -> dict[str, Any]:
        """``*_ref`` 只回引用不回值;密码类字段一个都不出现(#88)。"""
        if group == "adapters":
            return {"qidian": _as_dict(cfg.qidian), "qq": _as_dict(cfg.qq), "wechat": _as_dict(cfg.wechat_adapter)}
        if group in ("asr", "ocr", "log"):
            # 02 §7.1 有这三段但本期没有消费者(ASR/OCR 是 A.4/A-1 的后续里程碑,log 归 §2.9)⇒ 如实回空对象,不编默认值
            return {}
        if group == "resources":
            snap = agent.pool.snapshot()
            return {"pools": snap["pools"], "quota_mb": snap["quota_mb"]}
        if group == "mail":
            rows = _mail_or_503().ms.routes_list()
            return {"enabled": cfg.mail.enabled, "require_signature": cfg.mail.inbound.require_signature,
                    "template_version": cfg.mail.template_version,
                    "scopes": {s: _mail_scope_view(None if s == "default" else s, rows) for s in MAIL_SCOPES}}
        field = SETTINGS_GROUPS[group]
        out = {k: v for k, v in _as_dict(getattr(cfg, str(field))).items() if not any(k.endswith(x) for x in SECRET_KEYS)}
        if group == "api":
            # 🔴 `public_domain` **不在 agent.toml 里**(07 §2 `[api]` 行逐字:运行期可改、随 settings 备份),
            #    它的唯一存放处 = `settings['api.public_domain']`(02 #102 逐字)。读路径此前只映射 `AgentConfig.api`
            #    的字段 ⇒ `PUT /settings/api {public_domain}` 写得进、`GET /settings/api` 读不回,P-SET 表单回填不了。
            out["public_domain"] = agent.store.settings_get(API_PUBLIC_DOMAIN_KEY)
        return out

    def _known_setting_keys(group: str) -> Optional[set[str]]:
        """#89 整组替换时的**已知键集**;``None`` = 本组的键集在规格里没有出处 ⇒ 不做未知键判定。

        - 有配置段的组:键集 = 该段 dataclass 的字段名(``AgentConfig`` 是 02 §7.1 配置总表的落地);
          ``api`` 组另加 ``public_domain``(它不在 ``agent.toml`` 里,唯一存放处是 ``settings``,02 #102)。
        - ``adapters``/``resources``/``mail``:形状由 #88 逐字定死,按那三套顶层键判。
        - ``asr``/``ocr``/``log``:02 §7.1 有段但本期**没有消费者**(``_group_view`` 如实回 ``{}``),
          键集无出处 ⇒ 回 ``None``,维持现状不判(待文档方登记后再收紧)。
        """
        import dataclasses
        if group == "resources":
            return {"pools", "quota_mb"}
        if group == "adapters":
            return {"qidian", "qq", "wechat"}
        if group == "mail":
            return {"enabled", "require_signature", "template_version", "scopes"}       # #88 R6-58 (ac) 逐字
        field = SETTINGS_GROUPS[group]
        if field is None:
            return None
        keys = {f.name for f in dataclasses.fields(getattr(cfg, str(field)))}
        if group == "api":
            keys.add("public_domain")
        return keys

    def _reject_unknown_keys(group: str, body: dict[str, Any]) -> None:
        """#89 **未知键 ⇒ `400 INVALID_ARGS`,整个请求不落库**(总控 2026-09-21 裁决,独立联调 P-3)。

        为什么不能「照单全收」:#89 是**整组替换(缺省键回默认)**,一处键名笔误既不会被写进去、又会把
        同组其它键**静默洗回默认值**(实测 `PUT /settings/retention {"text_days":20}` 回 200 且回显,
        而 `messages_days` 等同时被重置)。显式优于隐式 ⇒ 认不出来的键一律拒,**在任何落库动作之前**拒。
        已知键的语义一个字不变;密码类(``*_secret``/``*_password``/``*_token``)是「只写不读」的入参,
        不在读回的字段里,故按后缀放行(``_stash_secrets`` 收走它们)。
        """
        known = _known_setting_keys(group)
        if known is None:
            return
        unknown = [k for k in body if k not in known and not any(k.endswith(x) for x in SECRET_KEYS)]
        if not unknown:
            return
        details = [{"pointer": "/" + str(k).replace("~", "~0").replace("/", "~1"),     # RFC 6901 转义
                    "message": f"{group} 组没有这个配置键", "kind": "unknown_key"} for k in unknown]
        raise ApiError(400, "INVALID_ARGS",
                       f"未知配置键:{', '.join(map(str, unknown))}(#89 是整组替换,收下笔误等于把整组洗回默认)",
                       reason="unknown_key", extra={"details": details})

    def _validate_retention(body: dict[str, Any]) -> dict[str, Any]:
        """#89:数据类 ``*_days > 30`` **按 30 截断并 WARN**(E-18);三级水位顺序非法 → 400。"""
        out, warnings = dict(body), []
        for k in DATA_CLASS_DAYS_KEYS:
            v = out.get(k)
            if isinstance(v, int) and v > DATA_CLASS_DAYS_MAX:
                out[k] = DATA_CLASS_DAYS_MAX
                warnings.append(f"{k}={v} 超过 E-18 上限 30,已按 30 截断")
        warn, high, crit = (out.get("disk_warn_mb"), out.get("disk_high_mb"), out.get("disk_critical_mb"))
        cur = cfg.retention
        warn = cur.disk_warn_mb if warn is None else warn
        high = cur.disk_high_mb if high is None else high
        crit = cur.disk_critical_mb if crit is None else crit
        if not (warn >= high >= crit):
            raise ApiError(400, "INVALID_ARGS", "须满足 disk_warn_mb ≥ disk_high_mb ≥ disk_critical_mb",
                           reason="watermark_order", extra={"details": [{"pointer": "/disk_high_mb"}]})
        return {"value": out, "warnings": warnings}

    def _put_resources(body: dict[str, Any], actor: str) -> dict[str, Any]:
        """``resources`` 组直接写 ``resource_pools``(C-40:替代原 `PATCH /resources`)。"""
        quota = body.get("quota_mb") or {}
        if quota and (not isinstance(quota, dict) or any(k not in ("qidian", "qq", "wechat") for k in quota)):
            raise ApiError(400, "INVALID_ARGS", "quota_mb 的键须为 qidian|qq|wechat", reason="bad_quota",
                           extra={"details": [{"pointer": "/quota_mb"}]})
        now = agent.clock()
        for pool in ("wsl", "windows"):
            cols: dict[str, Any] = {}
            blob = (body.get("pools") or {}).get(pool) or {}
            for k in ("total_mb", "reserved_mb"):
                if isinstance(blob.get(k), int):
                    cols[k] = blob[k]
            if quota:
                cur = dict(agent.store.pool_get(pool)["quota"])
                cur.update({k: int(v) for k, v in quota.items()})
                cols["quota_json"] = json.dumps(cur, ensure_ascii=False)
            if cols:
                cols["source"] = "manual"
                agent.store.pool_set(pool, now_ms=now, **cols)
        agent.store.insert_audit(kind="api", transport="http", actor=actor, action="settings.update",
                                 result_code="OK", detail={"group": "resources"}, now_ms=now)
        return {"data": _group_view("resources"), "restart_required": False}

    async def _stash_secrets(group: str, body: dict[str, Any], actor: str) -> dict[str, str]:
        """密码类字段**只写不读**:body 里给 ``secret`` 即写 Vault 并回 ``secret_ref``,原值从 body 里摘掉。"""
        refs: dict[str, str] = {}
        for key in [k for k in list(body) if any(k.endswith(x) for x in SECRET_KEYS)]:
            value = body.pop(key)
            if not isinstance(value, str) or not value:
                continue
            name = f"settings/{group}/{key}"
            await agent.vault.put(name, value, scope="settings")
            agent.store.settings_set(f"config.{group}.{key}_ref", f"vault://{name}", actor=actor)
            refs[key] = f"vault://{name}"
        return refs

    def _write_agent_toml(group: str, body: dict[str, Any]) -> bool:
        """原子写(临时文件 + rename);``config_path`` 没给(开发容器 / 测试)就只落 ``settings``,回 False。"""
        path = getattr(agent, "config_path", None)
        if not path:
            return False
        try:
            merged = dict(agent.store.settings_get("config.__all__") or {})
            merged[group] = body
            agent.store.settings_set("config.__all__", merged, actor="system:settings")
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(_toml_dump(merged))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except OSError as e:
            log.error("写回 agent.toml 失败(%s):配置已落 settings,重启前不会生效", e)
            return False

    def _toml_dump(groups: dict[str, Any]) -> str:
        """只发射本项目 `agent.toml` 用得到的标量/数组/字符串(不引第三方 toml 写库)。"""
        def val(v: Any) -> str:
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, (list, tuple)):
                return "[" + ", ".join(val(x) for x in v) + "]"
            return json.dumps(str(v), ensure_ascii=False)
        lines = ["# 由 PUT /api/v1/settings/{group} 原子写回(02 §3.4.6 #89);手改后重启生效"]
        for name, blob in groups.items():
            if not isinstance(blob, dict):
                continue
            lines.append(f"\n[{name}]")
            lines += [f"{k} = {val(v)}" for k, v in blob.items() if not isinstance(v, dict)]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ #94 webhooks CRUD(02 §3.4.6)
    def _webhook_view(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "url": row["url"], "secret_ref": row["secret_ref"],
                "events": json.loads(row["events_json"] or '["*"]'), "accounts": json.loads(row["accounts_json"] or '["*"]'),
                "enabled": bool(row["enabled"]), "timeout_ms": row["timeout_ms"], "max_attempts": row["max_attempts"],
                "consecutive_fail": row["consecutive_fail"],
                # ISO(00 §6;R6-62 (f) 的 `*_ms` 例外只有 Account 两键);库列 `dead_ms` 不变,R6-58 (af) PATCH 照旧清它
                "dead_at": iso8601(row["dead_ms"]) if row["dead_ms"] is not None else None,
                "created_at": iso8601(row["created_ms"]), "updated_at": iso8601(row["updated_ms"])}

    def _webhook_or_404(wid: str) -> dict[str, Any]:
        r = agent.store.con.execute("SELECT * FROM webhooks WHERE id=?", (wid,)).fetchone()
        if r is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"webhook 不存在:{wid}")
        return dict(r)

    @app.get(f"{API_PREFIX}/settings/webhooks")
    async def list_webhooks(request: Request):
        """#94(列表),级别 A;``secret`` 只回 ``secret_ref``。"""
        _principal(request, "admin")
        rows = [dict(r) for r in agent.store.con.execute("SELECT * FROM webhooks ORDER BY id")]
        return {"ok": True, "data": [_webhook_view(r) for r in rows]}

    @app.post(f"{API_PREFIX}/settings/webhooks", status_code=201)
    async def create_webhook(request: Request):
        """#94(建),级别 A:``{name, url, events?, accounts?, timeout_ms?, max_attempts?}`` → **一次性**返回 ``secret``。"""
        p = _principal(request, "admin")
        body = await request.json()
        for key in ("name", "url"):
            if not isinstance(body.get(key), str) or not body[key].strip():
                raise ApiError(400, "INVALID_ARGS", f"{key} 必填", reason=f"bad_{key}", extra={"details": [{"pointer": f"/{key}"}]})
        if not str(body["url"]).startswith(("http://", "https://")):
            raise ApiError(400, "INVALID_ARGS", "url 须为 http(s)://", reason="bad_url", extra={"details": [{"pointer": "/url"}]})
        wid, secret, now = ulid(agent.clock()), ulid() + ulid(), agent.clock()
        await agent.vault.put(f"webhook/{wid}", secret, scope="webhook")
        agent.store.con.execute(
            "INSERT INTO webhooks(id, name, url, secret_ref, events_json, accounts_json, enabled, timeout_ms, max_attempts, "
            "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, body["name"].strip(), body["url"], f"vault://webhook/{wid}",
             json.dumps(body.get("events") or ["*"]), json.dumps(body.get("accounts") or ["*"]),
             1 if body.get("enabled", True) else 0, int(body.get("timeout_ms") or cfg.webhook.webhook_timeout_ms),
             int(body.get("max_attempts") or cfg.webhook.webhook_max_attempts), now, now))
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "webhooks", "op": "create", "id": wid}, now_ms=now)
        return {"ok": True, "data": _webhook_view(_webhook_or_404(wid)), "secret": secret}   # secret 一次性,不再回读

    @app.patch(f"{API_PREFIX}/settings/webhooks/{{webhook_id}}")
    async def patch_webhook(request: Request, webhook_id: str):
        """#94(改),级别 A。🔴 ``enabled=1`` 同时清 ``consecutive_fail``/``dead_ms``(rulings R6-58 (al)):
        死信自动停用后若不清这两列,重新启用的 webhook 会带着旧的失败计数,下一次失败立刻又进死信。"""
        p = _principal(request, "admin")
        row = _webhook_or_404(webhook_id)
        body = await request.json()
        sets, vals = [], []
        for key, col in (("name", "name"), ("url", "url"), ("timeout_ms", "timeout_ms"), ("max_attempts", "max_attempts")):
            if key in body:
                sets.append(f"{col}=?")
                vals.append(body[key])
        for key, col in (("events", "events_json"), ("accounts", "accounts_json")):
            if key in body:
                sets.append(f"{col}=?")
                vals.append(json.dumps(body[key]))
        if "enabled" in body:
            sets.append("enabled=?")
            vals.append(1 if body["enabled"] else 0)
            if body["enabled"]:
                sets += ["consecutive_fail=0", "dead_ms=NULL"]
        if not sets:
            raise ApiError(400, "INVALID_ARGS", "没有可改的字段", reason="empty_patch")
        sets.append("updated_ms=?")
        vals += [agent.clock(), webhook_id]
        agent.store.con.execute(f"UPDATE webhooks SET {', '.join(sets)} WHERE id=?", vals)
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "webhooks", "op": "patch", "id": webhook_id,
                                                           "keys": sorted(body)}, now_ms=agent.clock())
        return {"ok": True, "data": _webhook_view(_webhook_or_404(webhook_id))}

    @app.delete(f"{API_PREFIX}/settings/webhooks/{{webhook_id}}")
    async def delete_webhook(request: Request, webhook_id: str):
        """#94(删),级别 A:连带把该 webhook 还没投的 outbox 副本删掉(否则投递器会一直找不到登记方)。"""
        p = _principal(request, "admin")
        _webhook_or_404(webhook_id)
        with agent.store._tx() as c:
            c.execute("DELETE FROM events_outbox WHERE target=?", (f"webhook:{webhook_id}",))
            c.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
        await agent.vault.delete(f"webhook/{webhook_id}")
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "webhooks", "op": "delete", "id": webhook_id}, now_ms=agent.clock())
        return {"ok": True, "deleted": True, "id": webhook_id}

    @app.post(f"{API_PREFIX}/settings/webhooks/{{webhook_id}}/test")
    async def test_webhook(request: Request, webhook_id: str):
        """#94(测),级别 A:给该 webhook 投一条测试事件(走正常的 outbox → 投递器,**不绕过签名**)。"""
        _principal(request, "admin")
        w = _webhook_or_404(webhook_id)
        now = agent.clock()
        seq = agent.store.insert_outbox_event(event_id=ulid(now), target=f"webhook:{webhook_id}", event="alert",
                                              trace_id=None, account_id=None, channel=None,
                                              payload_json=json.dumps({"code": "WEBHOOK_TEST", "severity": "info",
                                                                       "state": "firing", "subject": "host"}, ensure_ascii=False),
                                              now_ms=now)
        res = await agent.webhooks.deliver_due(now_ms=now)
        row = agent.store.con.execute("SELECT status, last_error FROM events_outbox WHERE seq=?", (seq,)).fetchone()
        return {"ok": True, "id": webhook_id, "url": w["url"], "seq": seq,
                "status": row["status"] if row else None, "last_error": row["last_error"] if row else None, **res}

    # ------------------------------------------------------------------ #96~#101 媒体与运行时动作
    @app.post(f"{API_PREFIX}/messages/{{message_id}}/media/{{idx}}/fetch")
    async def fetch_media(request: Request, message_id: str, idx: int):
        """#96,级别 W:触发懒加载媒体下载,立即回 ``{media_id, sha256?, state}``;已就绪直接带 ``sha256``。"""
        p = _principal(request, "write")
        row = agent.store.get_message_full(message_id)
        if row is None or not p.allows_account(row["account_id"]):
            raise ApiError(404, "TARGET_NOT_FOUND", f"消息不存在:{message_id}")
        try:
            media = json.loads(row.get("media_json") or "[]")
        except ValueError:
            media = []
        if idx < 0 or idx >= len(media):
            raise ApiError(404, "TARGET_NOT_FOUND", f"消息 {message_id} 没有第 {idx} 个媒体")
        item = media[idx]
        mid = item.get("media_id")
        if mid is None:
            mid = agent.media.note(kind=item.get("kind") or "other",
                                   origin={k: item[k] for k in ("url", "wa_path", "file", "ref") if k in item},
                                   message_id=message_id)
            if mid is None:
                raise ApiError(507, "DISK_FULL", "磁盘 critical 水位,暂停媒体入库", reason="disk_critical", needs_human=True)
        out = await agent.media.fetch_into(int(mid))
        return {"ok": True, "media_id": out.get("media_id"), "sha256": out.get("sha256"), "state": out.get("status"),
                "fail_reason": out.get("fail_reason")}

    @app.get(f"{API_PREFIX}/media/{{sha256}}")
    async def get_media(request: Request, sha256: str):
        """#55:按 hash 取媒体字节;到期删了文件(``expired``)→ **410**。"""
        _principal(request, "read")
        row = agent.media.by_sha256(sha256)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"媒体不存在:{sha256}")
        data = agent.media.read_file(int(row["id"]))
        if data is None:
            raise ApiError(410, "TARGET_NOT_FOUND", "媒体文件已按保留期删除", reason="expired")
        return Response(content=data, media_type=row["mime"] or "application/octet-stream")

    def _runtime_action_guard(p: Principal, account_id: str, channels: tuple[str, ...]) -> dict[str, Any]:
        require_account(p, account_id)
        row = agent.accounts.get(account_id)
        if row["channel"] not in channels:
            raise ApiError(409, "NOT_APPLICABLE", f"{row['channel']} 通道不支持本操作", reason="not_applicable")
        return row

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/webui/open")
    async def webui_open(request: Request, account_id: str):
        """#97,级别 W:QQ 临时开 NapCat WebUI(C-35);``running`` 态下开需重启容器,响应带 ``restart:true``。"""
        p = _principal(request, "write")
        row = _runtime_action_guard(p, account_id, ("qq",))
        request.state.account_id = account_id
        body = await _json_or_empty(request)
        minutes = int(body.get("minutes") or cfg.runtime.webui_temp_minutes)
        until = agent.clock() + minutes * 60_000
        res = await agent.accounts.set_webui(account_id, True, until_ms=until, actor=p.actor)
        port = row.get("webui_port") or (16300 + int(row["seq"]))
        return {"ok": True, "url": f"http://127.0.0.1:{port}/", "until": iso8601(until), **res}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/webui/close")
    async def webui_close(request: Request, account_id: str):
        """#98,级别 W:关 WebUI + 重启容器;已关 → 200 no-op。"""
        p = _principal(request, "write")
        _runtime_action_guard(p, account_id, ("qq",))
        request.state.account_id = account_id
        return {"ok": True, **(await agent.accounts.set_webui(account_id, False, actor=p.actor))}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/export-identity")
    async def export_identity(request: Request, account_id: str):
        """#99,级别 A,**仅 QQ**:打包 ``accounts/<id>/data`` → ``202 {job_id}``;账号须 ``stopped``(卷一致性)否则 409。

        企点数据卷**不提供导出**(设备档案不可迁移)⇒ ``NOT_APPLICABLE``。"""
        p = _principal(request, "admin")
        row = _runtime_action_guard(p, account_id, ("qq",))
        request.state.account_id = account_id
        if row["state"] != "stopped":
            raise ApiError(409, "NOT_APPLICABLE", f"导出前须先停止账号(当前 {row['state']})", reason="not_stopped")
        job_id = agent.store.job_create(kind="identity_export", actor=p.actor, account_id=account_id)
        agent.spawn_job(job_id, "identity_export", lambda: agent.export_identity_job_body(job_id, account_id))
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/runtime/reconnect-adb")
    async def reconnect_adb(request: Request, account_id: str):
        """#100,级别 W,**仅企点**:``adb disconnect`` + ``connect``(与 04 H06 自愈同一实现)→ ``{ok, adb_state}``。"""
        p = _principal(request, "write")
        row = _runtime_action_guard(p, account_id, ("qidian",))
        request.state.account_id = account_id
        return {"ok": True, **(await agent.reconnect_adb(row))}

    @app.post(f"{API_PREFIX}/accounts/{{account_id}}/runtime/restart-stream")
    async def restart_stream(request: Request, account_id: str):
        """#101,级别 W,**仅企点**:重建 ``adb forward`` 与 scrcpy-server(04 H07 自愈同一实现)。

        ⚠️ 画面流(#34 WS)本期没有执行体 ⇒ 这里只做 ``adb forward`` 的重建并如实回 ``stream_restarted:false``,
        **不假装重建了 scrcpy**(见 rulings R6-58 (cw))。"""
        p = _principal(request, "write")
        row = _runtime_action_guard(p, account_id, ("qidian",))
        request.state.account_id = account_id
        return {"ok": True, **(await agent.restart_stream(row))}

    # ------------------------------------------------------------------ 邮件路由写入(S-8:密钥只进 Vault,库里只落 `secret_ref`)
    def _check_route_or_400(mail, channel: Optional[str], account_id: Optional[str], blob: dict[str, Any], *, pointer: str) -> None:
        try:
            check_route(mail.ms, channel=channel, account_id=account_id,
                        inbound=blob.get("inbound") or {}, outbound=blob.get("outbound") or {})
        except BadSecretRef as e:
            raise ApiError(400, "INVALID_ARGS", str(e), reason="bad_secret_ref",
                           extra={"details": [{"pointer": f"{pointer}/{e.side}/secret_ref"}]})

    async def _upsert_route_or_503(mail, channel: Optional[str], account_id: Optional[str], blob: dict[str, Any]) -> int:
        """Vault 写不进 ⇒ 整个请求 503(与账号凭据同一口径 `NOT_READY`/`vault_unavailable`),库不落半截。"""
        try:
            return await upsert_route(mail.ms, agent.vault, channel=channel, account_id=account_id,
                                      inbound=blob.get("inbound") or {}, outbound=blob.get("outbound") or {},
                                      enabled=bool(blob.get("enabled", True)))
        except VaultUnavailable as e:
            raise ApiError(503, "NOT_READY", f"凭据保险库不可用({e.reason}),邮件路由未保存",
                           reason="vault_unavailable", retryable=True)

    # ------------------------------------------------------------------ #88 / #89 的 mail 组(01 §2.7.10 的四块同构卡片)
    MAIL_SCOPES = ("default", "qidian", "qq", "wechat")

    def _mail_scope_view(channel: Optional[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
        """一块卡片 = 一条 ``mail_routes`` 行(全局行 = ``channel IS NULL``);``*_ref``/密码**只回引用不回值**(#88)。"""
        row = next((r for r in rows if (r["channel"] or None) == channel and r["account_id"] is None), None)
        inbound = strip_secrets(json.loads((row or {}).get("inbound_json") or "{}"))
        outbound = strip_secrets(json.loads((row or {}).get("outbound_json") or "{}"))
        return {"override": row is not None, "route_id": (row or {}).get("id"),
                "enabled": bool((row or {}).get("enabled", 1)), "inbound": inbound, "outbound": outbound}

    @app.get(f"{API_PREFIX}/settings/mail")
    async def get_settings_mail(request: Request):
        """#88 的 ``mail`` 组(级别 A):``[mail]`` 总开关 + 四块同构 ``scopes``(全局默认 + 三通道)。

        02 #88 只写「只含 `[mail]` 总开关与全局路由的默认值」、没给 ``scopes`` 的逐字形状;
        这里按 01 §2.7.10 的四块卡片给,形态登记在 rulings R6-58 (bs)。``senders[].shortname`` 走 ``/mail/hmac-keys`` 侧反查,不随组下发。
        """
        _principal(request, "admin")
        rows = _mail_or_503().ms.routes_list()
        return {"ok": True, "data": {"enabled": cfg.mail.enabled,
                                     "require_signature": cfg.mail.inbound.require_signature,
                                     "template_version": cfg.mail.template_version,
                                     "scopes": {s: _mail_scope_view(None if s == "default" else s, rows) for s in MAIL_SCOPES}}}

    @app.put(f"{API_PREFIX}/settings/mail")
    async def put_settings_mail(request: Request):
        """#89 的 ``mail`` 组(级别 A):按 ``scopes`` 逐块 upsert ``mail_routes``(全局行 ``channel=NULL``),写完 ``reload()``。"""
        _principal(request, "admin")
        mail = _mail_or_503()
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError(400, "INVALID_ARGS", "请求体须为对象", reason="bad_body")
        _reject_unknown_keys("mail", body)         # 与 `PUT /settings/{group}` 同一道门(P-3)
        scopes = body.get("scopes")
        if not isinstance(scopes, dict) or any(s not in MAIL_SCOPES for s in scopes):
            raise ApiError(400, "INVALID_ARGS", f"scopes 的键须为 {'|'.join(MAIL_SCOPES)}", reason="bad_scopes",
                           extra={"details": [{"pointer": "/scopes"}]})
        todo = [(name, blob) for name, blob in scopes.items() if isinstance(blob, dict) and blob.get("override", True)]
        for name, blob in todo:                    # 先全部校验、再逐块写(非法 secret_ref 不能留下前几块已落库的半截)
            _check_route_or_400(mail, None if name == "default" else name, None, blob, pointer=f"/scopes/{name}")
        written: dict[str, int] = {}
        for name, blob in todo:
            written[name] = await _upsert_route_or_503(mail, None if name == "default" else name, None, blob)
        mail.reload()
        return {"ok": True, "written": written}

    # ---- 02 §3.4 里此前没有执行体的那批端点(#24/#36/#51/#74/#75/#78/#80/#84/#85/#86/#87/#90~#93)。
    #      🔴 注册点**必须在 `/settings/{group}` 兜底路由之前**:`/settings/api-clients` 与 `{group}` 都是
    #      FULL 匹配,FastAPI 按注册顺序取先到的,晚注册就会被 `{group}` 当成「未知配置组」吃掉。
    register_ext(app, agent=agent, cfg=cfg, prefix=API_PREFIX, principal=_principal,
                 json_or_empty=_json_or_empty, caps_by_op=caps_by_op)
    # ---- 第二批(#8/#16/#27/#33/#34/#35/#37/#50/#52/#53/#54/#82/#83);同样必须排在 `/settings/{group}` 之前。
    register_ext2(app, agent=agent, cfg=cfg, prefix=API_PREFIX, principal=_principal,
                  json_or_empty=_json_or_empty, caps_by_op=caps_by_op)

    # ---- #88/#89 的 `{group}` 兜底路由必须**排在全部具体 `/settings/*` 路由之后**(FastAPI 按注册顺序匹配,
    #      否则 `/settings/webhooks` 会先被 `{group}` 吃掉)
    @app.get(f"{API_PREFIX}/settings/{{group}}")
    async def get_settings_group(request: Request, group: str):
        """#88:分组读;``api``/``mail`` 组要 A,其余 R。``*_ref`` 只回引用、密码类字段不出现。"""
        if group not in SETTINGS_GROUPS:
            raise ApiError(404, "TARGET_NOT_FOUND", f"未知配置组:{group}")
        _principal(request, "admin" if group in ADMIN_GROUPS else "read")
        return {"ok": True, "group": group, "data": _group_view(group)}

    @app.put(f"{API_PREFIX}/settings/{{group}}")
    async def put_settings_group(request: Request, group: str):
        """#89,级别 A:整组替换(缺省键回默认);写回 ``agent.toml``(**原子写:临时文件 + rename**)+ 热加载;
        不可热加载的项回 ``restart_required:true``;密码类字段**只写不读**(给 ``secret`` 即写 Vault 并回 ``secret_ref``)。

        🔴 ``AgentConfig`` 是 frozen dataclass、进程内**不做半截热改**:本端点把整组值原子落盘 + 落
        ``settings['config.<group>']``,并一律回 ``restart_required``(哪些项真能热加载由各模块自己声明,
        02 §3.4.6 没给清单,见 rulings R6-58 (cv))。``resources`` 组直接写 ``resource_pools``。
        """
        p = _principal(request, "admin")
        if group not in SETTINGS_GROUPS:
            raise ApiError(404, "TARGET_NOT_FOUND", f"未知配置组:{group}")
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError(400, "INVALID_ARGS", "请求体须为对象", reason="bad_body")
        _reject_unknown_keys(group, body)          # 🔴 在**任何**落库动作之前(含下面的 public_domain)
        if group == "api" and "public_domain" in body:
            # 与 `_group_view` 同一把键 ⇒ 写进读得回;空串/None = 清除(P-SET 把域名删掉的动作)。
            pub = body.pop("public_domain")
            pub = str(pub).strip() if isinstance(pub, str) else None
            agent.store.settings_set(API_PUBLIC_DOMAIN_KEY, pub or None, actor=p.actor)
        warnings: list[str] = []
        if group == "resources":
            return {"ok": True, "group": group, **_put_resources(body, p.actor)}
        if group == "mail":
            return await put_settings_mail(request)
        if group == "retention":
            checked = _validate_retention(body)
            body, warnings = checked["value"], checked["warnings"]
        secret_refs = await _stash_secrets(group, body, p.actor)
        agent.store.settings_set(f"config.{group}", body, actor=p.actor)
        written = _write_agent_toml(group, body)
        data = dict(body)
        if group == "api":
            data["public_domain"] = agent.store.settings_get(API_PUBLIC_DOMAIN_KEY)   # 与 GET 同形,表单回填不用再拉一次
        return {"ok": True, "group": group, "data": data, "secret_refs": secret_refs,
                "restart_required": True, "config_written": written, "warnings": warnings}


    # ------------------------------------------------------------------ 邮件 /mail(06 §3.2;命名以 06 为准,C-29)
    #: `[mail.inbound.hmac]` 的发件人短名表在 `settings` 里的键前缀(`mail.hmac.<短名>`);#67/#68/GET 三处同源。
    MAIL_HMAC_PREFIX = "mail.hmac."

    def _mail_or_503():
        if getattr(agent, "mail", None) is None:
            raise ApiError(503, "NOT_READY", "邮件服务未装配", reason="mail_not_wired", retryable=True)
        return agent.mail

    def _mail_route_scopes() -> dict[Any, str]:
        """``route_id → scope``(取值 = 01 §4 ``qt-mail-health-route-{scope}`` 的那一套:
        ``default`` / ``qidian|qq|wechat`` / ``<account_id>``);给 #58/#59/#61 行的 ``route`` 列用。

        邮件服务没装配时回空表(``route`` 全为 ``null``),不因此让列表整个 503。
        """
        try:
            rows = _mail_or_503().ms.routes_list()
        except ApiError:
            return {}
        return {r["id"]: (r["account_id"] or r["channel"] or "default") for r in rows}

    def _hmac_route_id(short: str, routes: list[dict[str, Any]]) -> Optional[str]:
        """短名落在哪条路由的 ``inbound_json.hmac{短名: secret_ref}`` 里(06 §3.1 表);全局短名回 ``None``。

        短名本身**全局唯一、不带 route 维度**(R6-23),这一列只是让 P-SET 知道它挂在哪块卡片上。
        """
        for r in routes:
            try:
                inbound = json.loads(r.get("inbound_json") or "{}")
            except ValueError:
                continue
            if short in (inbound.get("hmac") or {}):
                return r.get("id")
        return None

    def _transport_of(p: Principal) -> str:
        """🔴 承重墙(基线 §11.17 ③):``transport`` **只能取自鉴权上下文**,绝不许调用方在 body 里指定。

        控制台会话 = ``console``、loopback = ``local``、邮件入口 = ``email``(邮件入口永远走不到本端点)。
        写审计时 ``console → 'http'``(``audit_log.transport`` 的 CHECK 只收 local/http/email/system,见 rulings R6-58 (n))。"""
        return "local" if p.transport == "local" else "console"

    @app.get(f"{API_PREFIX}/mail/status")
    async def mail_status(request: Request, route: Optional[str] = None, route_id: Optional[str] = None):
        """#56,级别 R。形状照 02 #56 逐字:不带 ``route_id`` ⇒ ``{enabled, routes:[{route_id, channel, account_id, route,
        inbound, outbound, cleanup}]}``(全部启用路由);带 ⇒ 单条 ``{enabled, route, inbound, outbound, cleanup}``,
        没有这条路由 ⇒ ``404``(此前回 ``data:[]``;02 #56 未写,按全册 `TARGET_NOT_FOUND` 惯例)。
        🔴 信封**顶层平铺 + ``ok``**、不包 ``data``:02 §3.4 通用 R6-55「响应列直接给出字面键集的端点顶层平铺」,
        #56 的响应列正是字面键集(不是 §7 对象名、也不是 C-42 列表)。``?route=`` 是 06 §2.7 的旧参数名,照收。"""
        _principal(request, "read")
        mail = _mail_or_503()
        rid = route_id or route
        rows = mail.status(rid)
        if rid is None:
            return {"ok": True, "enabled": bool(mail.cfg.enabled), "routes": rows}
        if not rows:
            raise ApiError(404, "TARGET_NOT_FOUND", f"路由不存在:{rid}")
        return {"ok": True, **_mail_status_single(mail, rows[0])}

    def _mail_status_single(mail: Any, row: dict[str, Any]) -> dict[str, Any]:
        """02 #56 单条形状;``enabled`` = 全局 ``[mail] enabled`` 且该路由 ``enabled``(停用路由带 id 也查得到)。"""
        r = next((x for x in mail.routes.routes if str(x.id) == row["route_id"]), None)
        return {"enabled": bool(mail.cfg.enabled and (r is None or r.enabled)),
                **{k: row[k] for k in ("route", "inbound", "outbound", "cleanup")}}

    @app.post(f"{API_PREFIX}/mail/fetch")
    async def mail_fetch(request: Request):
        """#57,级别 W:``{route_id?}`` 手动触发一轮收 → ``202 {run_id}``(发件队列由投递器自动排空,单封用 #62)。"""
        p = _principal(request, "write")
        mail = _mail_or_503()
        body = await _json_or_empty(request)
        route_id = body.get("route_id")
        keys = [r.mailbox_key for r in mail.routes.routes if route_id is None or str(r.id) == str(route_id)]
        if route_id is not None and not keys:
            raise ApiError(404, "TARGET_NOT_FOUND", f"路由不存在:{route_id}")
        run_id = ulid(agent.clock())

        async def one_round() -> None:
            out = await asyncio.to_thread(lambda: {k: f.run_once() for k, f in mail.fetchers.items() if k in keys})
            await mail.dispatch(agent.bus)
            log.info("#57 手动取信 run=%s:%s", run_id, out)

        asyncio.create_task(one_round(), name=f"mail-fetch:{run_id}")
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="mail.fetch",
                                 result_code="OK", detail={"run_id": run_id, "route_id": route_id}, now_ms=agent.clock())
        return JSONResponse(status_code=202, content={"ok": True, "run_id": run_id, "mailboxes": sorted(keys)})

    @app.post(f"{API_PREFIX}/mail/test")
    async def mail_test(request: Request):
        """#66,级别 A:``{which:'inbound'|'outbound', route_id?}`` 连通 + 登录测试;
        ``inbound`` 同时报 ``imap_ok``/``pop3_ok`` 两个结论(E-1 回落的前置判断)。

        **只连配置里那一个邮箱**,不发任何邮件;开发容器里后端是 ``FakeImap``/``FakePop3``/``FakeSmtp``。"""
        _principal(request, "admin")
        mail = _mail_or_503()
        body = await _json_or_empty(request)
        which = body.get("which")
        if which not in ("inbound", "outbound"):
            raise ApiError(400, "INVALID_ARGS", "which 须为 inbound|outbound", reason="bad_which",
                           extra={"details": [{"pointer": "/which"}]})
        route_id = body.get("route_id")
        route = next((r for r in mail.routes.routes if route_id is None or str(r.id) == str(route_id)), None)
        if route is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"路由不存在:{route_id}")

        def probe(factory, label: str) -> dict[str, Any]:
            if factory is None:
                return {f"{label}_ok": False, f"{label}_error": "backend_not_wired"}
            try:
                backend = factory(route)
                backend.connect()
                backend.login()
                closer = getattr(backend, "logout", None) or getattr(backend, "close", None)
                if closer is not None:
                    closer()
            except Exception as e:
                return {f"{label}_ok": False, f"{label}_error": repr(e)[:200]}
            return {f"{label}_ok": True}

        if which == "outbound":
            out = await asyncio.to_thread(probe, mail.smtp_factory, "smtp")
        else:
            imap = await asyncio.to_thread(probe, mail.imap_factory, "imap")
            pop3 = await asyncio.to_thread(probe, mail.pop3_factory, "pop3")
            out = {**imap, **pop3}
        return {"ok": True, "which": which, "route_id": route.id, **out}

    # ------------------------------------------------------------------ #103 邮件模板 CRUD(E-4;表 mail_templates)
    #: 🔴 R6-58 (h):02 §3.1 的 CHECK 已改成 ``('ibquote-163-v1','collector-v1','qtrade-v1')``,
    #: ``qtrade-v1`` **直接落库**——原先借用 ``custom`` 格位的映射层(``TEMPLATE_PROFILE_DDL_FALLBACK``)已随之删除。
    #: ``custom`` 这个取值按 R-13 作废,入参给它一律 ``400 profile_custom_removed``。
    TEMPLATE_PROFILES_ALLOWED = ("ibquote-163-v1", "collector-v1", "qtrade-v1")

    def _template_row_view(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "kind": row["kind"], "subject_pattern": row["subject_pattern"],
                "body_fields": json.loads(row["body_fields_json"] or "[]"),
                # R6-58 (h):库里存什么就回什么,不再有格位借用
                "compat_profile": row["compat_profile"],
                "version": row["version"], "builtin": bool(row["builtin"]),
                "created_at": iso8601(row["created_ms"]), "updated_at": iso8601(row["updated_ms"])}

    def _template_or_404(tpl_id: int) -> dict[str, Any]:
        r = agent.store.con.execute("SELECT * FROM mail_templates WHERE id=?", (tpl_id,)).fetchone()
        if r is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"邮件模板不存在:{tpl_id}")
        return dict(r)

    def _template_body(body: dict[str, Any], *, require_all: bool) -> dict[str, Any]:
        for key in ("name", "kind", "subject_pattern"):
            if require_all and (not isinstance(body.get(key), str) or not body[key].strip()):
                raise ApiError(400, "INVALID_ARGS", f"{key} 必填", reason=f"bad_{key}", extra={"details": [{"pointer": f"/{key}"}]})
        if "kind" in body and body["kind"] not in ("inbound", "outbound"):
            raise ApiError(400, "INVALID_ARGS", "kind 须为 inbound|outbound", reason="bad_kind", extra={"details": [{"pointer": "/kind"}]})
        profile = body.get("compat_profile", "qtrade-v1")
        if profile == "custom":
            raise ApiError(400, "INVALID_ARGS", "compat_profile 的 'custom' 已被 R-13 删除,请用 'qtrade-v1'",
                           reason="profile_custom_removed", extra={"details": [{"pointer": "/compat_profile"}]})
        if profile not in TEMPLATE_PROFILES_ALLOWED:
            raise ApiError(400, "INVALID_ARGS", f"compat_profile 须为 {'|'.join(TEMPLATE_PROFILES_ALLOWED)}",
                           reason="bad_profile", extra={"details": [{"pointer": "/compat_profile"}]})
        fields = body.get("body_fields", [])
        if not isinstance(fields, list) or any(not isinstance(x, dict) or not x.get("key") for x in fields):
            raise ApiError(400, "INVALID_ARGS", "body_fields 须为 [{key,label,order,required,empty}]", reason="bad_body_fields",
                           extra={"details": [{"pointer": "/body_fields"}]})
        return {"profile": profile, "fields": fields}

    @app.get(f"{API_PREFIX}/settings/mail/templates")
    async def list_mail_templates(request: Request, kind: Optional[str] = None):
        """#103(列),级别 A。随包三份**内置模板是代码常量、不入表**(06 §3.1 末句),走 ``/templates/defaults``。"""
        _principal(request, "admin")
        sql = "SELECT * FROM mail_templates" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
        rows = [dict(r) for r in agent.store.con.execute(sql, (kind,) if kind else ())]
        return {"ok": True, "data": [_template_row_view(r) for r in rows]}

    @app.post(f"{API_PREFIX}/settings/mail/templates", status_code=201)
    async def create_mail_template(request: Request):
        """#103(建),级别 A。"""
        p = _principal(request, "admin")
        body = await request.json()
        parsed = _template_body(body, require_all=True)
        now = agent.clock()
        stored_profile = parsed["profile"]
        try:
            cur = agent.store.con.execute(
                "INSERT INTO mail_templates(name, kind, subject_pattern, body_fields_json, compat_profile, version, builtin, "
                "created_ms, updated_ms) VALUES (?,?,?,?,?,1,0,?,?)",
                (body["name"].strip(), body["kind"], body["subject_pattern"],
                 json.dumps(parsed["fields"], ensure_ascii=False), stored_profile, now, now))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise ApiError(400, "INVALID_ARGS", f"模板名已存在:{body['name']}", reason="name_taken",
                               extra={"details": [{"pointer": "/name"}]})
            raise
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "mail_templates", "op": "create"}, now_ms=now)
        return {"ok": True, "data": _template_row_view(_template_or_404(int(cur.lastrowid)))}

    @app.put(f"{API_PREFIX}/settings/mail/templates/{{tpl_id}}")
    async def put_mail_template(request: Request, tpl_id: int):
        """#103(改),级别 A:``version`` **必须递增**(不递增 → 400);内置模板不可改。"""
        p = _principal(request, "admin")
        row = _template_or_404(tpl_id)
        if row["builtin"]:
            raise ApiError(409, "NOT_APPLICABLE", "内置模板不可改,请另存一份", reason="builtin_readonly")
        body = await request.json()
        parsed = _template_body(body, require_all=False)
        version = body.get("version")
        if not isinstance(version, int) or version <= int(row["version"]):
            raise ApiError(400, "INVALID_ARGS", f"version 必须递增(当前 {row['version']})", reason="version_not_increasing",
                           extra={"details": [{"pointer": "/version"}]})
        now = agent.clock()
        stored_profile = parsed["profile"]
        agent.store.con.execute(
            "UPDATE mail_templates SET name=?, kind=?, subject_pattern=?, body_fields_json=?, compat_profile=?, version=?, updated_ms=? "
            "WHERE id=?",
            (body.get("name", row["name"]), body.get("kind", row["kind"]), body.get("subject_pattern", row["subject_pattern"]),
             json.dumps(parsed["fields"], ensure_ascii=False), stored_profile, version, now, tpl_id))
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "mail_templates", "op": "put", "id": tpl_id}, now_ms=now)
        return {"ok": True, "data": _template_row_view(_template_or_404(tpl_id))}

    @app.delete(f"{API_PREFIX}/settings/mail/templates/{{tpl_id}}")
    async def delete_mail_template(request: Request, tpl_id: int):
        """#103(删),级别 A:``builtin=1`` 或**被任一 `mail_routes` 引用** → 409。"""
        p = _principal(request, "admin")
        row = _template_or_404(tpl_id)
        if row["builtin"]:
            raise ApiError(409, "NOT_APPLICABLE", "内置模板不可删", reason="builtin_readonly")
        used = agent.store.con.execute(
            "SELECT COUNT(*) FROM mail_routes WHERE outbound_template_id=? OR inbound_template_id=?", (tpl_id, tpl_id)).fetchone()[0]
        if used:
            raise ApiError(409, "RESOURCE_EXHAUSTED", f"模板被 {used} 条邮箱路由引用,先改路由再删", reason="template_in_use",
                           extra={"routes": used})
        agent.store.con.execute("DELETE FROM mail_templates WHERE id=?", (tpl_id,))
        agent.store.insert_audit(kind="api", transport=p.transport, actor=p.actor, action="settings.update",
                                 result_code="OK", detail={"group": "mail_templates", "op": "delete", "id": tpl_id}, now_ms=agent.clock())
        return {"ok": True, "deleted": True, "id": tpl_id}

    @app.get(f"{API_PREFIX}/mail/inbox")
    async def mail_inbox(request: Request, status: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                         q: Optional[str] = None, limit: int = Query(100, ge=1, le=500), cursor: Optional[str] = None):
        """#58,级别 R。**C-42 统一分页**:排序 = ``received_ms`` 降序,游标 G-16。

        🔴 出参走 ``mail_inbox_row_view``(**不是库行原样**):时间列 ISO 8601(00 §6「时间(API/事件/**邮件**)」)、
        列表**不含** ``body_text``(#58 逐字「详情才给」)。``next_cursor`` 仍按**库行**的 ``received_ms`` 算
        (视图里已经没有 ``*_ms`` 了,先算游标再转视图,C-42 翻页行为不变)。
        """
        _principal(request, "read")
        before, since_ms, until_ms = _page_window(cursor, since, until)
        rows = _mail_or_503().ms.inbox_list_page(status=status, since_ms=since_ms, until_ms=until_ms, q=q,
                                                 limit=limit, before=before)
        nxt = _next_cursor(rows, limit, ts_key="received_ms")
        scopes = _mail_route_scopes()
        return {"ok": True, "data": [mail_inbox_row_view(r, route=scopes.get(r.get("route_id"))) for r in rows],
                "next_cursor": nxt}

    @app.get(f"{API_PREFIX}/mail/inbox/{{inbox_id}}")
    async def mail_inbox_get(request: Request, inbox_id: int):
        """#59,级别 R:含解析字段与 ``reason``;**正文只在本端点给**(#58 逐字「详情才给」)。

        与 #58 同一个视图(时间 ISO、``id`` 字符串),只是多带 ``body_text``。
        """
        _principal(request, "read")
        row = _mail_or_503().ms.inbox_get(inbox_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"邮件不存在:{inbox_id}")
        scopes = _mail_route_scopes()
        return {"ok": True, "data": mail_inbox_row_view(row, route=scopes.get(row.get("route_id")), with_body=True)}

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
                          since: Optional[str] = None, until: Optional[str] = None,
                          limit: int = Query(100, ge=1, le=500), cursor: Optional[str] = None):
        """#61,级别 R。**C-42 统一分页**:排序 = ``created_ms`` 降序,游标 G-16。

        🔴 出参走 ``mail_outbox_row_view``(01 §2.7.8 发件队列列 + ISO 时间),库行的正文与投递内部列不下发;
        ``next_cursor`` 同 #58,先按库行 ``created_ms`` 算好再转视图。
        """
        _principal(request, "read")
        before, since_ms, until_ms = _page_window(cursor, since, until)
        rows = _mail_or_503().ms.outbox_list_page(status=status, kind=kind, since_ms=since_ms, until_ms=until_ms,
                                                  limit=limit, before=before)
        nxt = _next_cursor(rows, limit, ts_key="created_ms")
        scopes = _mail_route_scopes()
        return {"ok": True, "data": [mail_outbox_row_view(r, route=scopes.get(r.get("route_id"))) for r in rows],
                "next_cursor": nxt}

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
    async def mail_cleanup_log(request: Request, since: Optional[str] = None, until: Optional[str] = None,
                               limit: int = Query(50, ge=1, le=500), cursor: Optional[str] = None):
        """#65,级别 R。**C-42 统一分页**(02 #65 逐字「分页」):排序 = ``started_ms`` 降序,游标 G-16。

        🔴 出参走 ``mail_cleanup_log_row_view``(此前 ``SELECT *`` 原样透出 `started_ms`/`finished_ms`/`detail_json`),
        且此前收 `limit` 却不回 `next_cursor` —— 超出 `limit` 的轮次被静默丢掉(与 hmac-keys P-5 同型)。
        ``next_cursor`` 先按**库行**的 ``started_ms`` 算,再转视图(视图里已没有 ``*_ms``)。
        """
        _principal(request, "read")
        before, since_ms, until_ms = _page_window(cursor, since, until)
        rows = _mail_or_503().ms.cleanup_log_list_page(since_ms=since_ms, until_ms=until_ms, limit=limit, before=before)
        nxt = _next_cursor(rows, limit, ts_key="started_ms")
        return {"ok": True, "data": [mail_cleanup_log_row_view(r) for r in rows], "next_cursor": nxt}

    @app.get(f"{API_PREFIX}/mail/hmac-keys")
    async def mail_hmac_keys_list(request: Request):
        """**HMAC 发件人短名表(只读)**,级别 A。**全量返回,不分页**。

        🔴 **本端点是短名表的唯一来源**:02 #88 的 `mail` 组逐字写着「`senders[].shortname` **不随本组下发**
        (它属 `[mail.inbound.hmac]`,从 `GET /mail/hmac-keys` 侧取)——两处都下发会让前端拿到两份可能不一致的短名表」
        (R6-58 (ac))。而 02 §3.4 端点表此前只有 #67 `POST` / #68 `DELETE`,**这个 `GET` 没有编号** ⇒ 实现里
        它就成了 `405`,P-SET 的短名表因此恒空。本端点按 (ac) 那句 + 06 §2.3.4/§3.2 的字段补上,**待文档方补登编号**。

        🔴 **绝不回密钥值**:只回 `secret_ref`(`vault://mail/hmac/cmd/<短名>`),明文只在 #67 那一次下发。

        🔴 **不分页(不接受 `limit`,也不回 `next_cursor`)**,理由三条(独立联调 P-5,待文档方随编号一并登记):
        ① C-42「分页/时间参数全端点统一」是对 **§3.4 端点表里的端点**说的,而本端点**至今没有编号**、不在那张表里;
        ② C-42 的游标 G-16 = ``base64url(JSON{"ts_ms", "id"})``,要一个**时间排序列 + 行主键**;短名表存在
           ``settings`` 的 ``mail.hmac.<短名>`` 键值对里,既没有稳定的排序时间列也没有行主键,游标无从构造;
        ③ 短名表 = ``[mail.inbound.hmac]`` 的发件人白名单,一个发件人一把钥(#67),规模天然很小。
        此前「收 `limit` 却不回 `next_cursor`」是最坏的一种:**超出 `limit` 的短名被静默丢掉**,P-SET 的白名单看着
        是全的、实际少几行,而少一行就等于那位发件人的指令邮件全被 ``SENDER_DENIED``。
        """
        _principal(request, "admin")
        try:
            routes = _mail_or_503().ms.routes_list()
        except ApiError:
            routes = []                       # 邮件服务没装配也要能列短名表(短名存在 `settings`,不依赖 mail 服务)
        rows = agent.store.settings_list_prefix(MAIL_HMAC_PREFIX)
        out = []
        for r in rows:
            short = r["key"][len(MAIL_HMAC_PREFIX):]
            val = r["value"] if isinstance(r["value"], dict) else {}
            out.append({
                "short_name": short,
                "sender": val.get("sender"),
                "route_id": _hmac_route_id(short, routes),
                "enabled": True,                      # 吊销(#68)= 删条目 ⇒ 列出来的就是启用中的
                "secret_ref": val.get("secret_ref"),
                "created_at": iso8601(int(val["created_ms"])) if val.get("created_ms") else iso8601(r["updated_ms"]),
            })
        return {"ok": True, "data": out}

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
        if agent.store.settings_get(f"{MAIL_HMAC_PREFIX}{short}"):
            raise ApiError(400, "INVALID_ARGS", f"短名 {short} 已被占用", reason="short_name_taken",
                           extra={"details": [{"pointer": "/short_name"}]})
        secret = ulid() + ulid()
        now = agent.clock()
        await agent.vault.put(ref, secret, scope="mail")
        agent.store.settings_set(f"{MAIL_HMAC_PREFIX}{short}",
                                 {"sender": str(body["sender"]), "secret_ref": f"vault://{ref}", "created_ms": now},
                                 actor=p.actor, now_ms=now)
        return {"ok": True, "short_name": short, "secret_ref": f"vault://{ref}", "secret": secret}   # secret 一次性下发,不再回读

    @app.delete(f"{API_PREFIX}/mail/hmac-keys/{{short_name}}")
    async def mail_hmac_key_delete(request: Request, short_name: str):
        """#68,级别 A:按**短名**定位吊销(不按 sender);不存在 → 404。"""
        p = _principal(request, "admin")
        if not agent.store.settings_get(f"{MAIL_HMAC_PREFIX}{short_name}"):
            raise ApiError(404, "TARGET_NOT_FOUND", f"短名不存在:{short_name}")
        await agent.vault.delete(f"mail/hmac/cmd/{short_name}")
        agent.store.settings_set(f"{MAIL_HMAC_PREFIX}{short_name}", None, actor=p.actor)
        return {"ok": True, "revoked": True, "short_name": short_name}

    @app.get(f"{API_PREFIX}/mail/pending-confirms")
    async def mail_pending_confirms(request: Request):
        """#68b,级别 A:出参**恰八键**(R6-7 定死)。视图在 ``MailStore.pending_confirms``(时间 ISO、``id`` 字符串),
        与 ``confirms.list()`` 同一份,这里不再二次加工。"""
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

    @app.post(f"{API_PREFIX}/mail/templates/{{tpl_id}}/preview")
    async def mail_template_preview(request: Request, tpl_id: str):
        """#104,级别 R:用指定模板渲染一条消息(不入 ``mail_outbox``、不发送)。

        🔴 **路径带 `{id}`**(02 #104 逐字;联调交接第 2 节:此前实现成了无 id 的 `/mail/templates/preview`,
        控制台照 02 调必 404)。``{id}`` = ``mail_templates.id``,``"default"`` = 随包出站模板。
        入参二选一:``{sample_message_id}``(照 02,从库里取一条真消息组装上下文)或 ``{ctx}``(直接给 12 核心占位符的取值)。
        """
        p = _principal(request, "read")
        body = await _json_or_empty(request)
        tpl = cfg.mail.template_out
        if tpl_id not in ("default", "current"):
            if not tpl_id.isdigit():
                raise ApiError(400, "INVALID_ARGS", "模板 id 须为整数或 'default'", reason="bad_template_id",
                               extra={"details": [{"pointer": "/id"}]})
            _template_or_404(int(tpl_id))       # 存在性校验:不存在 → 404,不静默回退到随包模板
        ctx = body.get("ctx")
        warnings: list[str] = []
        fields_used: list[str] = []
        if ctx is None:
            mid = body.get("sample_message_id")
            if not isinstance(mid, str) or not mid:
                raise ApiError(400, "INVALID_ARGS", "须给 sample_message_id 或 ctx", reason="bad_sample",
                               extra={"details": [{"pointer": "/sample_message_id"}]})
            row = agent.store.get_message_full(mid)
            if row is None or not p.allows_account(row["account_id"]):
                raise ApiError(404, "TARGET_NOT_FOUND", f"消息不存在:{mid}")
            view = message_view(row)
            ctx = {"account_id": view["account_id"], "channel": view["channel"], "session": (view["session"] or {}).get("name") or "",
                   "session_id": (view["session"] or {}).get("id") or "", "sender": (view["sender"] or {}).get("name") or "",
                   "ts": view["ts"] or "", "text": view["text"] or "", "message_id": view["id"],
                   "ext_msg_id": view["ext_msg_id"] or "", "dir": view["dir"], "type": view["type"], "state": view["state"]}
            fields_used = sorted(ctx)
        elif not isinstance(ctx, dict):
            raise ApiError(400, "INVALID_ARGS", "ctx 须为对象(12 核心占位符的取值)", reason="bad_ctx",
                           extra={"details": [{"pointer": "/ctx"}]})
        else:
            fields_used = sorted(ctx)
        if tpl_id not in ("default", "current"):
            warnings.append(f"本期渲染恒用随包出站模板({cfg.mail.template_out});表里的模板 id={tpl_id} 只做存在性校验")
        from ..mail.templates import render_message_mail
        rendered = render_message_mail(tpl, ctx)
        return {"ok": True, "id": tpl_id, "subject": rendered.subject, "body_text": rendered.body_text,
                "body_html": rendered.body_html, "fields_used": fields_used, "warnings": warnings}

    @app.get(f"{API_PREFIX}/settings/mail/routes")
    async def mail_routes_list(request: Request):
        """#105(GET 半),级别 A:路由 CRUD;``inbound.secret``/``outbound.secret`` 只写不读。"""
        _principal(request, "admin")
        mail = _mail_or_503()
        status = {row["route_id"]: row for row in mail.status(include_disabled=True)}
        return {"ok": True, "data": [
            mail_route_row_view(r, status=(_mail_status_single(mail, status[str(r["id"])]) if str(r["id"]) in status else None))
            for r in mail.ms.routes_list()]}

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
        _check_route_or_400(mail, ch, aid, body, pointer="")
        row_id = await _upsert_route_or_503(mail, ch, aid, body)
        mail.reload()
        return {"ok": True, "id": str(row_id)}          # 与 GET 行 `id`、#56 `route_id` 同型(字符串)

    # ------------------------------------------------------------------ WS /events(02 §3.4.7)
    @app.websocket(f"{API_PREFIX}/events")
    async def ws_events(ws: WebSocket):
        """§3.4.7 握手:``Authorization`` 头或 ``?token=``(浏览器 WS 不能带头);
        **HMAC 客户端用 ``?app_id&ts&nonce&sig``**(对 ``GET /api/v1/events`` 签)。"""
        transport = "local" if (ws.client and ws.client.host in ("127.0.0.1", "::1")) else "http"
        q = ws.query_params
        p: Optional[Principal] = None
        if q.get("app_id") and q.get("sig") and getattr(agent, "hmac", None) is not None:
            # canonical 的 query 里**不含签名四件套本身**(否则自指);与 HTTP 侧同一套 canonical_string
            rest = "&".join(f"{k}={v}" for k, v in sorted(q.items()) if k not in ("app_id", "ts", "nonce", "sig"))
            try:
                res = await agent.hmac.verify(method="GET", path=f"{API_PREFIX}/events", query=rest, body=b"",
                                              headers={"X-QT-AppId": q["app_id"], "X-QT-Timestamp": q.get("ts", ""),
                                                       "X-QT-Nonce": q.get("nonce", ""), "X-QT-Signature": q["sig"]},
                                              client_ip=ws.client.host if ws.client else None)
                p = res.principal
            except ApiError:
                await _ws_reject(ws, 4401, "HMAC 签名校验未过")
                return
        if p is None:
            token = ""
            auth = ws.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            token = token or q.get("token", "")
            row = agent.store.api_client_by_token(token) if token else None
            if row is None:
                await _ws_reject(ws, 4401, "缺少或无效的令牌")
                return
            p = principal_from_row(row, transport=transport)
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close(code=4400, reason="10 秒内未收到合法的首帧订阅")
            return
        if not isinstance(first, dict) or not isinstance(first.get("subscribe"), dict):   # R6-53:首帧必须是含 subscribe 对象的 JSON
            await ws.close(code=4400, reason="首帧须为 {subscribe:{…}}")
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
