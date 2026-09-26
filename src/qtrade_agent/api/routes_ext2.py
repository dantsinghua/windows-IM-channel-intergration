"""02 §3.4 第二批「文档写了、API 层没注册」的端点(续 `routes_ext.py`)。

收录(编号以 02 §3.4 为准):#8 `POST /accounts/{id}/purge`、#16 `POST /accounts/{id}/logout`、
#27 `PATCH /accounts/{id}/sessions/{sid}`(+ 兄弟端点 #27b `GET`)、#33 `GET /accounts/{id}/screenshot`、
#34 `GET /accounts/{id}/stream`(WS)、#35 `POST /accounts/{id}/stream/input`、#37 `GET /broadcast/{broadcast_id}`、
#50 `GET /messages/{id}/media/{idx}`、#52 `GET /exports/{job_id}[/file]`、#53 `POST /messages/{id}/asr`、
#54 `POST /messages/purge`、#82 `POST /system/drain`、#83 `POST /system/shutdown`。

🔴 **三条纪律**(与上一批 `routes_ext.py` 同口径):

1. **外部副作用一律经可注入后端**:画面流 `agent.stream_backend`、ASR `agent.asr_backend`、文件系统 `agent.fs`、
   优雅停机 `agent.shutdown_hook`。**缺执行体时诚实回 `503 NOT_READY` + 明确 `reason`**,不伪造帧、不假装删过。
2. **不绕开既有横切**:鉴权 / 错误信封 / 审计 / 幂等 / `trace_id` 全部走 `create_api` 传进来的那一套;
   指令类经 `agent.bus.submit`(GATE 与审计都在总线里)。
3. **不往 `store/` 里加方法**(文件所有权约束,同 `maintenance.py` 的做法):本模块要的几条 SQL 走
   `_tx(store)` 薄封装,复用 ``Store`` 自己的事务与写锁。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import struct
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from .. import __version__ as AGENT_VERSION
from ..events import iso8601
from ..ids import ulid
from ..models import Command, CommandOrigin, json_safe
from .auth import ApiError, Principal, principal_from_row, require_account, require_level
from .serialize import session_view

log = logging.getLogger("qtrade.api.ext2")

#: #34 画面流子协议(02 #34 逐字,C-08)
STREAM_SUBPROTOCOL = "qtrade-scrcpy-v1"
#: #34 档位(02 #34 逐字)
STREAM_PROFILES = ("thumb", "focus", "focus15", "thumb10")
#: #35 REST 注入的类型(02 #35 逐字)
STREAM_INPUT_TYPES = ("tap", "swipe", "key", "text")
#: #34 控制帧类型(02 #34 逐字)
STREAM_CONTROL_TYPES = ("touch", "key", "scroll", "text", "pause", "resume", "profile", "pong")
#: 00 §8.1 R-06:`login_required` 下允许的**画面注入类**(不过 GATE,但逐条记 `audit_log.kind='stream_input'`)
LOGIN_PHASE_STATES = ("login_required", "logging_in")
#: #54 清理模式(02 #54 逐字)
MESSAGES_PURGE_MODES = ("all", "text_only")
#: #82 等在途指令的默认上限(02 #82:「等在跑指令 ≤30s」)
DRAIN_WAIT_S = 30.0

#: WS 关闭码(02 §3.4.7 只定义 4401 / 4400;本模块对「通道不适用 / 已有 focus 连接 / 无执行体」另给三个码,
#: 见模块尾「留给文档方」一节 —— **不是** §3.4.7 已有的码,须补登)
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_BAD_FRAME = 4400
WS_CLOSE_NOT_APPLICABLE = 4409          # 通道不支持画面流(QQ 拒绝 / 微信 NOT_APPLICABLE)
WS_CLOSE_CONFLICT = 4410                # 同账号已有 focus* 连接(02 #34「后来者 409」的 WS 落点)
WS_CLOSE_NOT_READY = 4503               # 画面流执行体未装配(本期没有 scrcpy-server)


@contextmanager
def _tx(store) -> Iterator[Any]:
    """薄封装:复用 ``Store`` 自己的事务与写锁(文件所有权约束:不往 ``store/`` 里加方法)。"""
    with store._tx() as c:                     # noqa: SLF001 - 见 docstring
        yield c


class Fs:
    """#8 真删账号目录的执行体协议 —— **可注入**,测试用 `FakeFs`,免得单测真去 `rmtree` 一个目录。"""

    def exists(self, path: str) -> bool:                     # pragma: no cover - 协议声明
        raise NotImplementedError

    def du_mb(self, path: str) -> float:                     # pragma: no cover
        raise NotImplementedError

    def rmtree(self, path: str) -> bool:                     # pragma: no cover
        raise NotImplementedError


class RealFs:
    """缺省实现:真删盘上目录。只会被 #8 用在 ``<accounts_dir>/<account_id>/`` 这一个路径上。"""

    def exists(self, path: str) -> bool:
        return os.path.isdir(path)

    def du_mb(self, path: str) -> float:
        total = 0
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
        return round(total / 1048576, 3)

    def rmtree(self, path: str) -> bool:
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path, ignore_errors=False)
        return True


class StreamBackend:
    """#34 / #35 画面流与输入回注的执行体协议(04 H07 / 02 #34/#35)。

    真机实现 = ``screen_scrcpy.ScrcpyBackend``,由 ``main`` 在随包 scrcpy-server 与 adb 都在时装配;
    没装配(测试 / 开发容器 / 缺 jar)一律 ``503 NOT_READY(stream_backend_missing)`` / WS ``4503``,**不伪造帧**。

    Fake 只要实现下面两个方法即可编程:

    - ``async open(account_id, *, profile) -> session``,``session`` 需有
      ``meta: dict``(首帧 JSON 的 `{codec,width,height,profile,fps,seq0}`)、
      ``frames() -> AsyncIterator[tuple[int, bytes] | dict]``(``(pts_ms, Annex-B NAL)``;8 字节 PTS 头由本模块拼;
      ``dict`` = 原样发给客户端的 JSON,如换档后的新 meta、``{type:'restart'}``)、
      ``async control(msg: dict)``、``async close()``。
    - ``async inject(account_id, msg: dict) -> dict``(#35 的 REST 兜底,与 #34 控制帧同一条 adb input 路径)。
    """

    async def open(self, account_id: str, *, profile: str) -> Any:        # pragma: no cover - 协议声明
        raise NotImplementedError

    async def inject(self, account_id: str, msg: dict[str, Any]) -> dict[str, Any]:   # pragma: no cover
        raise NotImplementedError


class AsrBackend:
    """#53 `voice_to_text` 的执行体协议(00 §11.13 [NOLLM]:**全程序只有这里用多模态**)。

    🔴 缺省未注入 ⇒ ``503 NOT_READY(asr_backend_missing)``;**本模块自己不出网**,是否本地推理由实现方保证。
    协议:``async transcribe(*, path, mime, media_id) -> {"text": str, "confidence": float|None}``。
    """

    async def transcribe(self, *, path: str, mime: Optional[str], media_id: int) -> dict[str, Any]:   # pragma: no cover
        raise NotImplementedError


def _sha8(text: str) -> str:
    """02 §2.9 脱敏表:进审计的 `text` 只留 `sha8:len`(登录密码不许落审计,00 §8.1 R-06)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def register_ext2(app: FastAPI, *, agent, cfg, prefix: str, principal, json_or_empty, caps_by_op) -> None:
    """把本模块的端点挂到 ``app``(注册点见 `api/app.py` 的调用处注释)。"""
    P = prefix

    # ================================================================== 共用件
    def _audit(p: Principal, action: str, *, result_code: str = "OK", detail: Optional[dict[str, Any]] = None,
               account_id: Optional[str] = None, kind: str = "api") -> None:
        try:
            agent.store.insert_audit(kind=kind, transport=p.transport, actor=p.actor, action=action,
                                     account_id=account_id, result_code=result_code, detail=detail or {},
                                     now_ms=agent.clock())
        except Exception as e:       # 审计失败不影响业务(与 create_api 的 middleware 同口径)
            log.warning("审计写入失败 action=%s: %s", action, e)

    def _account_or_404(p: Principal, account_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        """🔴 ``store.get_account`` 自带 ``deleted_ms IS NULL`` 过滤,取软删行必须走 ``get_account_full``
        (#8 purge 的前置恰恰是「已软删」,用错这个函数会恒 404)。"""
        require_account(p, account_id)
        row = agent.store.get_account_full(account_id) if include_deleted else agent.store.get_account(account_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"账号不存在:{account_id}")
        return row

    def _fs() -> Any:
        return getattr(agent, "fs", None) or RealFs()

    def _account_dir(account_id: str) -> str:
        """``<data_dir>/accounts/<id>/`` —— 与 `runtime.data_dir()` 同一个落点,但这里只拼路径、不碰 runtime。"""
        return os.path.join(agent.data_dir, "accounts", account_id)

    async def _submit_op(request: Request, p: Principal, account_id: str, op: str, args: dict[str, Any], *,
                         timeout_ms: Optional[int] = None):
        """把一条**只读类** op 经总线跑掉并返回 `CommandResult`(GATE / 审计 / 幂等都在 `bus.submit` 里)。"""
        cap = caps_by_op.get(op)
        if cap is None:
            raise ApiError(400, "INVALID_ARGS", f"未知能力 op={op!r}", reason="unknown_op")
        require_level(p, "read" if cap["kind"] == "read" else ("admin" if cap["kind"] == "admin" else "write"))
        cmd = Command(account_id=account_id, op=op, args=args, idempotency_key=None, confirm=True,
                      timeout_ms=int(timeout_ms or cfg.bus.default_timeout_ms),
                      origin=CommandOrigin(transport=p.transport, actor=p.actor,
                                           ip=request.client.host if request.client else None),
                      trace_id=ulid(), submitted_at_ms=agent.clock())
        request.state.account_id = account_id
        return await agent.bus.submit(cmd)

    # ================================================================== #8 真删账号数据(danger)
    def _mark_irreversible(job_id: str) -> None:
        """#108:``account_purge`` / ``messages_purge`` **进入不可逆阶段**后不可取消(02 #108 逐字)。

        标记落在 ``jobs.params_json.irreversible_since_ms`` 上(作业表本来就按 kind 自定义 params),
        ``POST /jobs/{job_id}/cancel`` 见到它即回 ``409 NOT_CANCELLABLE``。
        """
        now = agent.clock()
        with _tx(agent.store) as c:
            row = c.execute("SELECT params_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return
            try:
                params = json.loads(row["params_json"] or "{}")
            except ValueError:
                params = {}
            params["irreversible_since_ms"] = now
            c.execute("UPDATE jobs SET params_json=?, updated_ms=? WHERE job_id=?",
                      (json.dumps(params, ensure_ascii=False), now, job_id))

    def _purge_account_rows(account_id: str) -> dict[str, int]:
        """删该账号的 ``messages`` / ``sessions`` / ``cursors``;**媒体只减 refcount**(P-17),``device_profiles`` 行不动。"""
        now = agent.clock()
        deleted = {"messages": 0, "sessions": 0, "cursors": 0, "media_deref": 0}
        with _tx(agent.store) as c:
            rows = c.execute("SELECT id, media_json FROM messages WHERE account_id=?", (account_id,)).fetchall()
            for r in rows:
                try:
                    items = json.loads(r["media_json"] or "[]")
                except ValueError:
                    items = []
                for it in items:
                    mid = it.get("media_id") if isinstance(it, dict) else None
                    if mid is None:
                        continue
                    c.execute("UPDATE media SET ref_count = MAX(0, ref_count - 1), last_ref_ms=? WHERE id=?", (now, int(mid)))
                    deleted["media_deref"] += 1
            deleted["messages"] = c.execute("DELETE FROM messages WHERE account_id=?", (account_id,)).rowcount
            deleted["sessions"] = c.execute("DELETE FROM sessions WHERE account_id=?", (account_id,)).rowcount
            deleted["cursors"] = c.execute("DELETE FROM cursors WHERE owner=?", (account_id,)).rowcount
        return deleted

    @app.post(f"{P}/accounts/{{account_id}}/purge")
    async def purge_account(request: Request, account_id: str):
        """#8,级别 **A**(danger,00 §11.17 ① 九项之一):``{confirm:"<id>"}`` → ``202 {job_id}``。

        🔴 **真删,不可逆**:该账号的 ``messages`` / ``sessions`` / ``cursors`` 库行 + ``accounts/<id>/`` 数据目录
        + Vault 条目;**媒体只减 refcount**(P-17),``device_profiles`` 行不动(serialno/mac 永不复用),
        ``accounts`` 行按 #7「id 不复用」保留为墓碑。**须先 #7 软删**(02 #8 逐字「须先 #7」),否则 ``409``。
        进入不可逆阶段后 #108 取消回 ``409 NOT_CANCELLABLE``。
        """
        p = principal(request, "admin")
        body = await json_or_empty(request)
        row = _account_or_404(p, account_id, include_deleted=True)
        if body.get("confirm") != account_id:
            raise ApiError(400, "INVALID_ARGS", "purge 须带 confirm 且等于账号 id 原文(02 #8 二次确认)",
                           reason="confirm_mismatch", extra={"details": [{"pointer": "/confirm"}]})
        if not row.get("deleted_ms"):
            raise ApiError(409, "NOT_APPLICABLE", "须先软删(#7 DELETE /accounts/{id})再 purge", reason="not_soft_deleted")
        request.state.account_id = account_id
        job_id = agent.store.job_create(kind="account_purge", actor=p.actor, account_id=account_id,
                                        params={"confirm": True})

        async def job_body() -> dict[str, Any]:
            fs = _fs()
            path = _account_dir(account_id)
            freed_mb = await asyncio.to_thread(fs.du_mb, path) if fs.exists(path) else 0.0
            _mark_irreversible(job_id)                       # ← 这一行之后不可取消(#108)
            deleted = await asyncio.to_thread(_purge_account_rows, account_id)
            dir_removed = await asyncio.to_thread(fs.rmtree, path) if fs.exists(path) else False
            vault_removed = False
            vault = getattr(agent, "vault", None)
            ref = row.get("credential_ref")
            if vault is not None and ref:
                try:
                    delete = getattr(vault, "delete", None)
                    if delete is not None:
                        out = delete(ref)
                        vault_removed = bool(await out) if asyncio.iscoroutine(out) else bool(out)
                except Exception as e:                       # Vault 不可达不该让已删的库行回滚,如实记
                    log.warning("#8 purge 删 Vault 条目失败 account=%s ref=%s: %s", account_id, ref, e)
            return {"account_id": account_id, "deleted": deleted, "dir_removed": dir_removed,
                    "vault_removed": vault_removed, "freed_mb": freed_mb}

        agent.spawn_job(job_id, "account_purge", job_body)
        _audit(p, "account.purge", account_id=account_id, detail={"job_id": job_id})
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    # ================================================================== #16 登出
    @app.post(f"{P}/accounts/{{account_id}}/logout")
    async def logout_account(request: Request, account_id: str):
        """#16,级别 W → ``202``。微信 = WinAgent ``POST /wa/v1/wechat/logout``(P-31:先 WM_CLOSE 再结束进程)。

        执行体查找顺序:①适配器自己的 ``logout(acct)``(有就用);②微信回落到 WinAgent 客户端。
        **QQ 通道无「登出」这个概念**(登录态在容器的 `qq_data` 卷里,要换号走 #99/重装)⇒ ``409 NOT_APPLICABLE``;
        **企点本期没有 UI 登出执行体** ⇒ ``503 NOT_READY(logout_backend_missing)``,**不假装登出过**。
        """
        p = principal(request, "write")
        row = _account_or_404(p, account_id)
        request.state.account_id = account_id
        channel = row["channel"]
        if channel == "qq":
            raise ApiError(409, "NOT_APPLICABLE", "QQ 通道没有「登出」概念(登录态在 qq_data 卷里)",
                           reason="channel_no_logout")
        adapter = agent.adapters.get(channel)
        fn = getattr(adapter, "logout", None) if adapter is not None else None
        if fn is not None:
            await fn(account_id)
            _audit(p, "account.logout", account_id=account_id, detail={"channel": channel, "via": "adapter"})
            return JSONResponse(status_code=202, content={"ok": True, "account_id": account_id, "via": "adapter"})
        if channel == "wechat":
            wa = getattr(agent, "winagent", None)
            if wa is None:
                raise ApiError(503, "NOT_READY", "WinAgent 客户端未装配", reason="winagent_missing")
            try:
                status, _body = await wa.request("POST", "/wa/v1/wechat/logout", json={}, timeout_s=15.0, retry=False)
            except Exception as e:
                raise ApiError(503, "NOT_READY", f"WinAgent 不可达:{e}", reason="winagent_offline", retryable=True)
            if status >= 400:
                raise ApiError(503, "NOT_READY", f"WinAgent 回 {status}", reason="winagent_error", retryable=True)
            _audit(p, "account.logout", account_id=account_id, detail={"channel": channel, "via": "winagent"})
            return JSONResponse(status_code=202, content={"ok": True, "account_id": account_id, "via": "winagent"})
        raise ApiError(503, "NOT_READY", f"{channel} 通道的登出执行体本期未装配", reason="logout_backend_missing",
                       needs_human=True)

    # ================================================================== #27 / #27b 单会话
    def _session_or_404(p: Principal, account_id: str, sid: str) -> dict[str, Any]:
        _account_or_404(p, account_id)
        row = agent.store.con.execute("SELECT * FROM sessions WHERE id=? AND account_id=?", (sid, account_id)).fetchone()
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"会话不存在:{sid}")
        return dict(row)

    @app.get(f"{P}/accounts/{{account_id}}/sessions/{{sid}}")
    async def get_session(request: Request, account_id: str, sid: str):
        """#27b(**02 只给了 `PATCH` 半**,同上一批 #79b/#85b/#86b 的做法补一个只读兄弟端点):单会话详情 ``Session``。"""
        p = principal(request, "read")
        request.state.account_id = account_id
        return {"ok": True, "data": session_view(_session_or_404(p, account_id, sid))}

    @app.patch(f"{P}/accounts/{{account_id}}/sessions/{{sid}}")
    async def patch_session(request: Request, account_id: str, sid: str):
        """#27,级别 W:``{muted?, capture_text?, retention_days?, media_policy?}`` → ``Session``。

        ``retention_days > 30`` 按 E-18 与 #22 同口径回 ``400``;键集外的字段一律 ``400 bad_field``
        (静默忽略会让用户以为改过了)。
        """
        p = principal(request, "write")
        body = await json_or_empty(request)
        old = _session_or_404(p, account_id, sid)
        request.state.account_id = account_id
        allowed = {"muted", "capture_text", "retention_days", "media_policy"}
        bad = sorted(set(body) - allowed)
        if bad:
            raise ApiError(400, "INVALID_ARGS", f"不支持的字段:{','.join(bad)}", reason="bad_field",
                           extra={"details": [{"pointer": f"/{k}"} for k in bad]})
        cols: dict[str, Any] = {}
        if "muted" in body:
            if not isinstance(body["muted"], bool):
                raise ApiError(400, "INVALID_ARGS", "muted 须为布尔", reason="bad_muted",
                               extra={"details": [{"pointer": "/muted"}]})
            cols["muted"] = 1 if body["muted"] else 0
        if "capture_text" in body:
            v = body["capture_text"]
            if v is not None and not isinstance(v, bool):
                raise ApiError(400, "INVALID_ARGS", "capture_text 须为布尔或 null(null=跟账号)", reason="bad_capture_text",
                               extra={"details": [{"pointer": "/capture_text"}]})
            cols["capture_text"] = None if v is None else (1 if v else 0)
        if "retention_days" in body:
            v = body["retention_days"]
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v <= 0 or v > 30):
                raise ApiError(400, "INVALID_ARGS", "retention_days 须为 1~30 的整数或 null(E-18)", reason="bad_retention_days",
                               extra={"details": [{"pointer": "/retention_days"}]})
            cols["retention_days"] = v
        if "media_policy" in body:
            v = body["media_policy"]
            if v is not None and not isinstance(v, dict):
                raise ApiError(400, "INVALID_ARGS", "media_policy 须为对象或 null", reason="bad_media_policy",
                               extra={"details": [{"pointer": "/media_policy"}]})
            cols["media_policy_json"] = None if v is None else json.dumps(v, ensure_ascii=False)
        now = agent.clock()
        if cols:
            sets = ", ".join(f"{k}=?" for k in cols)
            with _tx(agent.store) as c:
                c.execute(f"UPDATE sessions SET {sets}, updated_ms=? WHERE id=? AND account_id=?",
                          [*cols.values(), now, sid, account_id])
        _audit(p, "session.patch", account_id=account_id, detail={"session_id": sid, "changed": sorted(cols)})
        return {"ok": True, "data": session_view(_session_or_404(p, account_id, sid) if cols else old)}

    # ================================================================== #33 截图
    @app.get(f"{P}/accounts/{{account_id}}/screenshot")
    async def account_screenshot(request: Request, account_id: str):
        """#33,级别 R:``?region=x,y,w,h&format=png|jpeg`` → **二进制图**;经队列(不插队)。

        🔴 **R6-58 (bb)/(cz)**:适配器契约里 ``data['png']`` 仍是裸 ``bytes``(给本端点与直调方用),
        但**落库与出 JSON 两处一律经 `json_safe`** 换成 ``{__binary__, len}`` 占位;图片本体由本端点直接回流,
        另按 §2.8.2 落 ``media/`` 并把 ``media_id`` / ``sha256`` 放响应头(复盘走那条路)。
        QQ ⇒ ``409 NOT_APPLICABLE``(§3.10 目录 `channels.qq='not_applicable'`)。
        """
        p = principal(request, "read")
        row = _account_or_404(p, account_id)
        fmt = (request.query_params.get("format") or "png").lower()
        if fmt not in ("png", "jpeg"):
            raise ApiError(400, "INVALID_ARGS", "format 须为 png|jpeg", reason="bad_format",
                           extra={"details": [{"pointer": "/format"}]})
        region = request.query_params.get("region")
        args: dict[str, Any] = {}
        if region:
            parts = region.split(",")
            if len(parts) != 4 or not all(x.strip().lstrip("-").isdigit() for x in parts):
                raise ApiError(400, "INVALID_ARGS", "region 须为 x,y,w,h 四个整数", reason="bad_region",
                               extra={"details": [{"pointer": "/region"}]})
            x, y, w, h = (int(v) for v in parts)
            if w <= 0 or h <= 0:
                raise ApiError(400, "INVALID_ARGS", "region 的 w/h 须为正整数", reason="bad_region",
                               extra={"details": [{"pointer": "/region"}]})
            args["region"] = {"x": x, "y": y, "w": w, "h": h}
        if fmt != "png":
            args["format"] = fmt
        if row["channel"] == "qq":
            raise ApiError(409, "NOT_APPLICABLE", "QQ 通道不支持截图(§3.10 目录 channels.qq='not_applicable')",
                           reason="not_applicable")
        res = await _submit_op(request, p, account_id, "screenshot", args)
        if not res.ok:
            code = res.code
            status = {"NOT_APPLICABLE": 409, "UNSUPPORTED": 409, "LOGIN_REQUIRED": 409, "TIMEOUT": 504,
                      "NOT_READY": 503, "TARGET_NOT_FOUND": 404}.get(code, 500 if code == "INTERNAL" else 409)
            msg = res.error.message if res.error is not None else f"截图失败:{code}"
            raise ApiError(status, code, msg, reason=(res.error.reason if res.error is not None else "") or "screenshot_failed",
                           extra={"result": json_safe(res.data)})
        data = res.data or {}
        raw = data.get("png") if isinstance(data.get("png"), (bytes, bytearray)) else None
        if raw is None and isinstance(data.get("png_b64"), str):
            try:
                raw = base64.b64decode(data["png_b64"])
            except ValueError:
                raw = None
        if raw is None:
            raise ApiError(503, "NOT_READY", "适配器没有回图片体(data.png / data.png_b64 都为空)",
                           reason="screenshot_no_image", extra={"result": json_safe(data)})
        mime = "image/jpeg" if fmt == "jpeg" else "image/png"
        headers = {"Cache-Control": "no-store", "X-QT-Trace-Id": res.trace_id or ""}
        media = getattr(agent, "media", None)
        if media is not None:
            try:
                out = await asyncio.to_thread(
                    lambda: media.put_bytes(bytes(raw), kind="image",
                                            origin={"kind": "screenshot", "account_id": account_id,
                                                    "trace_id": res.trace_id}))
                if out.get("media_id") is not None:
                    headers["X-QT-Media-Id"] = str(out["media_id"])
                if out.get("sha256"):
                    headers["X-QT-Sha256"] = str(out["sha256"])
            except Exception as e:                  # 落 media 失败不该让截图取不到(复盘少一条,图还在)
                log.warning("#33 截图落 media 失败 account=%s: %s", account_id, e)
        return Response(content=bytes(raw), media_type=mime, headers=headers)

    # ================================================================== #34 / #35 画面流
    #: 同一账号只允许 1 个 `focus*` 连接(02 #34「后来者 409」);`thumb*` 不限
    focus_holders: dict[str, str] = {}

    def _ws_principal(ws: WebSocket) -> Optional[Principal]:
        """WS 握手鉴权(与 `/events` 同一套):``Authorization`` 头或 ``?token=``;HMAC 走 ``?app_id&ts&nonce&sig``。"""
        transport = "local" if (ws.client and ws.client.host in ("127.0.0.1", "::1")) else "http"
        token = ""
        auth = ws.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or ws.query_params.get("token", "")
        row = agent.store.api_client_by_token(token) if token else None
        if row is None:
            return None
        return principal_from_row(row, transport=transport)

    async def _ws_close(ws: WebSocket, code: int, reason: str, *, accepted: bool = False) -> None:
        """🔴 **先 `accept()` 再 `close(code)`** —— 在 accept 之前 close,客户端只看得到 `1006`(上一批 E-05 的教训)。"""
        if not accepted:
            try:
                await ws.accept(subprotocol=STREAM_SUBPROTOCOL)
            except Exception:
                return
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass

    @app.websocket(f"{P}/accounts/{{account_id}}/stream")
    async def account_stream(ws: WebSocket, account_id: str):
        """#34,级别 R(WS 升级,子协议 ``qtrade-scrcpy-v1``)。

        首帧由服务端发 JSON ``{codec,width,height,profile,fps,seq0}``;此后二进制帧 =
        **8 字节 PTS(毫秒,big-endian uint64)+ Annex-B NAL**;控制帧由客户端发 JSON。

        🔴 执行体没装配 ⇒ 鉴权与参数校验照做,到真要出帧时 ``close(4503, stream_backend_missing)``,
        **一帧都不伪造**;``open`` 失败(拉不起 scrcpy-server)同样 4503。
        """
        p = _ws_principal(ws)
        if p is None:
            await _ws_close(ws, WS_CLOSE_UNAUTHORIZED, "缺少或无效的令牌")
            return
        if not p.can("read") or not p.allows_account(account_id):
            await _ws_close(ws, WS_CLOSE_UNAUTHORIZED, "当前令牌无权访问该账号")
            return
        profile = ws.query_params.get("profile") or "thumb"
        if profile not in STREAM_PROFILES:
            await _ws_close(ws, WS_CLOSE_BAD_FRAME, f"profile 须为 {'|'.join(STREAM_PROFILES)}")
            return
        row = agent.store.get_account(account_id)
        if row is None or row.get("deleted_ms"):
            await _ws_close(ws, WS_CLOSE_BAD_FRAME, f"账号不存在:{account_id}")
            return
        if row["channel"] != "qidian":
            # 02 #34:QQ 拒绝;微信 NOT_APPLICABLE(用 #33 截图轮询)
            await _ws_close(ws, WS_CLOSE_NOT_APPLICABLE, f"{row['channel']} 通道不提供画面流")
            return
        is_focus = profile.startswith("focus")
        conn_id = ulid(agent.clock())
        if is_focus and focus_holders.get(account_id):
            await _ws_close(ws, WS_CLOSE_CONFLICT, "该账号已有 focus 连接(同时只允许 1 个)")
            return
        backend: Optional[StreamBackend] = getattr(agent, "stream_backend", None)
        if backend is None:
            await _ws_close(ws, WS_CLOSE_NOT_READY, "stream_backend_missing:画面流执行体本期未装配")
            return
        try:
            session = await backend.open(account_id, profile=profile)
        except Exception as e:
            log.warning("#34 打开画面流失败 account=%s: %s", account_id, e)
            await _ws_close(ws, WS_CLOSE_NOT_READY, f"画面流打开失败:{e}")
            return
        if is_focus:
            focus_holders[account_id] = conn_id
        await ws.accept(subprotocol=STREAM_SUBPROTOCOL)
        _audit(p, "stream.open", account_id=account_id, detail={"profile": profile, "conn_id": conn_id})
        meta = dict(getattr(session, "meta", None) or {})
        meta.setdefault("profile", profile)
        pump: Optional[asyncio.Task] = None
        try:
            await ws.send_json(meta)                      # 首帧 = 服务端发的 JSON(02 #34 逐字)

            async def pump_frames() -> None:
                async for item in session.frames():
                    if isinstance(item, dict):
                        # 执行体下发的 JSON(换档后的新 meta / 02 #101「已连 WS 收到 {type:'restart'} 后重连」)。
                        # restart 先让出 focus 位,客户端立刻重连才不会撞 4410
                        if item.get("type") == "restart" and is_focus and focus_holders.get(account_id) == conn_id:
                            focus_holders.pop(account_id, None)
                        await ws.send_json(item)
                        continue
                    pts_ms, nal = item
                    await ws.send_bytes(struct.pack(">Q", int(pts_ms)) + bytes(nal))

            pump = asyncio.create_task(pump_frames(), name=f"stream:{account_id}:{conn_id}")
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    await ws.close(code=WS_CLOSE_BAD_FRAME, reason="控制帧须为 JSON")
                    return
                if not isinstance(msg, dict) or msg.get("type") not in STREAM_CONTROL_TYPES:
                    await ws.close(code=WS_CLOSE_BAD_FRAME, reason=f"type 须为 {'|'.join(STREAM_CONTROL_TYPES)}")
                    return
                if msg["type"] == "profile":
                    if msg.get("profile") not in STREAM_PROFILES:
                        await ws.close(code=WS_CLOSE_BAD_FRAME, reason="profile 取值非法")
                        return
                _audit_stream_input(p, account_id, msg, via="ws")
                await session.control(msg)
        except WebSocketDisconnect:
            return
        except RuntimeError:
            return
        finally:
            if pump is not None:
                pump.cancel()
            if is_focus and focus_holders.get(account_id) == conn_id:
                focus_holders.pop(account_id, None)
            try:
                await session.close()
            except Exception:
                pass

    def _audit_stream_input(p: Principal, account_id: str, msg: dict[str, Any], *, via: str) -> None:
        """00 §8.1 R-06 / 02 §2.2.2:画面注入类**逐条留痕**,``text`` 只记 ``sha8:len``(登录密码不许落审计)。"""
        safe = {k: v for k, v in msg.items() if k != "text"}
        if isinstance(msg.get("text"), str):
            safe["text"] = f"{_sha8(msg['text'])}:{len(msg['text'])}"
        _audit(p, f"stream.{msg.get('type')}", account_id=account_id, detail={"via": via, **safe}, kind="stream_input")

    @app.post(f"{P}/accounts/{{account_id}}/stream/input")
    async def stream_input(request: Request, account_id: str):
        """#35,级别 W:``{type:'tap|swipe|key|text', …}`` —— **无 WS 时的 REST 注入兜底**(C-08)。

        00 §8.1 R-06:注入类在 ``login_required`` / ``logging_in`` 下**照常允许**(登录本来就要点滑块、填短信码),
        **不过 GATE**,但逐条记 ``audit_log.kind='stream_input'``、``text`` 只留 ``sha8:len``。
        执行体缺省未注入 ⇒ ``503 NOT_READY(stream_backend_missing)``。
        """
        p = principal(request, "write")
        body = await json_or_empty(request)
        row = _account_or_404(p, account_id)
        request.state.account_id = account_id
        kind = body.get("type")
        if kind not in STREAM_INPUT_TYPES:
            raise ApiError(400, "INVALID_ARGS", f"type 须为 {'|'.join(STREAM_INPUT_TYPES)}", reason="bad_type",
                           extra={"details": [{"pointer": "/type"}]})
        if kind in ("tap", "swipe"):
            need = ("x", "y") if kind == "tap" else ("x", "y", "x2", "y2")
            missing = [k for k in need if not isinstance(body.get(k), (int, float)) or isinstance(body.get(k), bool)]
            if missing:
                raise ApiError(400, "INVALID_ARGS", f"{kind} 须带数字 {'/'.join(need)}", reason="bad_coords",
                               extra={"details": [{"pointer": f"/{k}"} for k in missing]})
        if kind == "key" and not isinstance(body.get("keycode"), (int, str)):
            raise ApiError(400, "INVALID_ARGS", "key 须带 keycode", reason="bad_keycode",
                           extra={"details": [{"pointer": "/keycode"}]})
        dur = body.get("duration_ms")
        if kind == "swipe" and dur is not None and (isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur < 0):
            # 规格外字段(控制台长按兜底用;待 02 #35 登记):缺省 120 ms、上限 5000 ms,由执行体钳
            raise ApiError(400, "INVALID_ARGS", "duration_ms 须为非负数字", reason="bad_duration",
                           extra={"details": [{"pointer": "/duration_ms"}]})
        if kind == "text" and not isinstance(body.get("text"), str):
            raise ApiError(400, "INVALID_ARGS", "text 须带 text 字符串", reason="bad_text",
                           extra={"details": [{"pointer": "/text"}]})
        if row["state"] not in (*LOGIN_PHASE_STATES, "running", "degraded"):
            raise ApiError(409, "NOT_APPLICABLE", f"账号当前 {row['state']},不接受画面注入", reason="bad_state")
        backend: Optional[StreamBackend] = getattr(agent, "stream_backend", None)
        if backend is None:
            _audit_stream_input(p, account_id, body, via="rest_rejected")
            raise ApiError(503, "NOT_READY", "stream_backend_missing:输入回注执行体本期未装配",
                           reason="stream_backend_missing", needs_human=True)
        _audit_stream_input(p, account_id, body, via="rest")
        try:
            out = await backend.inject(account_id, dict(body))
        except ValueError as e:                     # 不认识的键名等:参数问题,不冒 500
            raise ApiError(400, "INVALID_ARGS", str(e), reason="bad_input") from e
        except (LookupError, RuntimeError, OSError) as e:      # 查不到设备 / adb 失败
            raise ApiError(503, "NOT_READY", f"输入回注失败:{e}", reason="inject_failed") from e
        return {"ok": True, "account_id": account_id, "type": kind, "result": json_safe(out or {})}

    # ================================================================== #37 广播作业查询
    @app.get(f"{P}/broadcast/{{broadcast_id}}")
    async def get_broadcast(request: Request, broadcast_id: str):
        """#37,级别 R:一次 #36 群发的汇总。

        落点 = ``settings['broadcast.<broadcast_id>']``(与上一批 #78 自检落 settings 同一做法:
        ``jobs.kind`` 的 CHECK 里没有 broadcast,硬塞会直接违反约束)。**只看得到自己有权的账号**。
        """
        p = principal(request, "read")
        blob = agent.store.settings_get(f"broadcast.{broadcast_id}")
        if not isinstance(blob, dict):
            raise ApiError(404, "TARGET_NOT_FOUND", f"广播不存在:{broadcast_id}")
        results = {aid: r for aid, r in (blob.get("results") or {}).items() if p.allows_account(aid)}
        counts = {"total": len(results), "ok": sum(1 for r in results.values() if r.get("ok"))}
        counts["failed"] = counts["total"] - counts["ok"]
        return {"ok": True, "broadcast_id": broadcast_id, "op": blob.get("op"), "actor": blob.get("actor"),
                "submitted_at": blob.get("submitted_at"), "counts": counts, "results": results}

    # ================================================================== #50 媒体字节流
    @app.get(f"{P}/messages/{{message_id}}/media/{{idx}}")
    async def get_message_media(request: Request, message_id: str, idx: int):
        """#50,级别 R:**二进制**;``lazy`` 未下载则触发下载(阻塞 ≤10 s,否则 ``202`` 让客户端重试);
        ``skipped_oversize`` → ``413``;已按 ``media_days`` 到期删除(``expired``,E-10)→ ``410``。

        ``Content-Type`` 取 ``media.mime``(按字节头识别的那个,不是来源声明的)。懒下载复用既有 ``media.fetch_into``。
        """
        p = principal(request, "read")
        row = agent.store.get_message_full(message_id)
        if row is None or not p.allows_account(row["account_id"]):
            raise ApiError(404, "TARGET_NOT_FOUND", f"消息不存在:{message_id}")
        try:
            items = json.loads(row.get("media_json") or "[]")
        except ValueError:
            items = []
        if idx < 0 or idx >= len(items):
            raise ApiError(404, "TARGET_NOT_FOUND", f"消息 {message_id} 没有第 {idx} 个媒体")
        item = items[idx] if isinstance(items[idx], dict) else {}
        mid = item.get("media_id")
        if mid is None:
            raise ApiError(404, "TARGET_NOT_FOUND", "该媒体还没有 media 行(先用 #96 fetch 触发入库)",
                           reason="media_not_noted")
        info = agent.media.get(int(mid)) or {}
        status = info.get("status")
        if status in ("pending", "failed", None):
            try:
                out = await asyncio.wait_for(agent.media.fetch_into(int(mid)), timeout=10.0)
            except asyncio.TimeoutError:
                return JSONResponse(status_code=202, content={"ok": True, "media_id": int(mid), "state": "pending",
                                                              "hint": "下载中,请稍后重试"})
            status = out.get("status")
            info = agent.media.get(int(mid)) or info
        if status == "skipped_oversize":
            raise ApiError(413, "INVALID_ARGS", "媒体超过大小上限,未下载", reason="skipped_oversize")
        if status == "expired":
            raise ApiError(410, "TARGET_NOT_FOUND", "媒体文件已按保留期删除(E-10)", reason="expired")
        if status != "ready":
            raise ApiError(503, "NOT_READY", f"媒体当前状态 {status},取不到字节", reason="media_not_ready",
                           retryable=True, extra={"fail_reason": info.get("fail_reason")})
        data = await asyncio.to_thread(agent.media.read_file, int(mid))
        if data is None:
            raise ApiError(410, "TARGET_NOT_FOUND", "媒体文件已不在盘上", reason="expired")
        return Response(content=data, media_type=info.get("mime") or "application/octet-stream",
                        headers={"X-QT-Media-Id": str(mid), "X-QT-Sha256": str(info.get("sha256") or "")})

    # ================================================================== #52 导出产物
    def _job_or_404(job_id: str) -> dict[str, Any]:
        row = agent.store.job_get(job_id)
        if row is None:
            raise ApiError(404, "TARGET_NOT_FOUND", f"作业不存在:{job_id}")
        return row

    def _job_result(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(row.get("result_json") or "null") or {}
        except ValueError:
            return {}

    def _require_own_job(p: Principal, row: dict[str, Any]) -> None:
        """**只许取本人作业的产物**:非 admin 只看自己 `actor` 的行;账号级作业另过 `allow_accounts`。"""
        if row.get("account_id") and not p.allows_account(str(row["account_id"])):
            raise ApiError(403, "FORBIDDEN", "当前令牌无权访问该账号的作业", reason="account_not_allowed")
        if not p.can("admin") and row.get("actor") != p.actor:
            raise ApiError(403, "FORBIDDEN", "只能取本人发起的作业产物", reason="job_not_owned")

    @app.get(f"{P}/exports/{{job_id}}")
    async def get_export(request: Request, job_id: str):
        """#52,级别 R:作业状态 ``{status, rows, bytes, download_url, expires_at}``
        (00 §11.21 [JOB]:本端点 = ``GET /jobs/{job_id}`` 的**别名**,只是形状按 02 #52 逐字给导出视角的键)。"""
        p = principal(request, "read")
        row = _job_or_404(job_id)
        _require_own_job(p, row)
        res = _job_result(row)
        has_file = bool(res.get("file_path"))
        return {"ok": True, "job_id": job_id, "kind": row["kind"], "status": row["state"],
                "rows": res.get("rows"), "bytes": res.get("bytes"),
                "download_url": f"{P}/exports/{job_id}/file" if (row["state"] == "succeeded" and has_file) else None,
                "expires_at": iso8601(row["expires_ms"]) if row.get("expires_ms") is not None else None}

    @app.get(f"{P}/exports/{{job_id}}/file")
    async def download_export(request: Request, job_id: str):
        """#52 下载(计 1 行):产物字节流。

        🔴 **路径不得穿越**:只认 ``jobs.result_json.file_path``,且必须落在 ``<data_dir>/exports/`` 之内
        (``realpath`` 比对);不接受任何客户端传来的路径片段。
        """
        p = principal(request, "read")
        row = _job_or_404(job_id)
        _require_own_job(p, row)
        if row["state"] != "succeeded":
            raise ApiError(409, "NOT_APPLICABLE", f"作业尚未成功(当前 {row['state']}),没有产物可下载",
                           reason="job_not_succeeded")
        res = _job_result(row)
        path = res.get("file_path")
        if not isinstance(path, str) or not path:
            raise ApiError(404, "TARGET_NOT_FOUND", "该作业没有产物文件", reason="no_artifact")
        exports_root = os.path.realpath(os.path.join(agent.data_dir, "exports"))
        real = os.path.realpath(path)
        if not (real == exports_root or real.startswith(exports_root + os.sep)):
            log.error("#52 产物路径越界 job=%s path=%s", job_id, path)
            raise ApiError(403, "FORBIDDEN", "产物路径不在导出目录内", reason="path_escape")
        if not os.path.isfile(real):
            raise ApiError(410, "TARGET_NOT_FOUND", "产物已按保留期删除(jobs 随产物 7 天)", reason="expired")
        data = await asyncio.to_thread(lambda: open(real, "rb").read())
        mime = {".jsonl": "application/x-ndjson", ".csv": "text/csv; charset=utf-8", ".zip": "application/zip",
                ".tar": "application/x-tar"}.get(os.path.splitext(real)[1], "application/octet-stream")
        _audit(p, "export.download", detail={"job_id": job_id, "bytes": len(data)})
        return Response(content=data, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(real)}"'})

    # ================================================================== #53 语音转文字
    @app.post(f"{P}/messages/{{message_id}}/asr")
    async def message_asr(request: Request, message_id: str):
        """#53,级别 W:触发 ``voice_to_text``(A.4)→ ``202 {trace_id}``;结果回写 ``asr_text`` / ``asr_state``,
        ``text_source='asr'`` **只在原消息无文本时**才写 ``text``。

        🔴 00 §11.13 [NOLLM]:**全程序只有这里用多模态**。执行体经可注入 ``agent.asr_backend``;
        缺省未注入 ⇒ ``503 NOT_READY(asr_backend_missing)``,**本端点自己不出网**。
        """
        p = principal(request, "write")
        row = agent.store.get_message_full(message_id)
        if row is None or not p.allows_account(row["account_id"]):
            raise ApiError(404, "TARGET_NOT_FOUND", f"消息不存在:{message_id}")
        require_account(p, row["account_id"])
        request.state.account_id = row["account_id"]
        if row["type"] != "voice":
            raise ApiError(409, "NOT_APPLICABLE", f"消息类型 {row['type']} 不是语音", reason="not_voice")
        if row.get("asr_state") == "pending":
            raise ApiError(409, "RESOURCE_EXHAUSTED", "该消息的转写已在进行中", reason="asr_in_progress")
        try:
            items = json.loads(row.get("media_json") or "[]")
        except ValueError:
            items = []
        voice = next((it for it in items if isinstance(it, dict) and it.get("media_id") is not None), None)
        if voice is None:
            raise ApiError(409, "NOT_APPLICABLE", "该语音消息没有可转写的媒体行", reason="no_media")
        backend: Optional[AsrBackend] = getattr(agent, "asr_backend", None)
        if backend is None:
            raise ApiError(503, "NOT_READY", "asr_backend_missing:语音转写执行体本期未装配(00 §11.13 只此一处用多模态)",
                           reason="asr_backend_missing", needs_human=True)
        mid = int(voice["media_id"])
        info = agent.media.get(mid) or {}
        if info.get("status") != "ready" or not info.get("rel_path"):
            raise ApiError(409, "NOT_APPLICABLE", f"媒体当前状态 {info.get('status')},先用 #96 下载", reason="media_not_ready")
        trace_id = ulid(agent.clock())
        _asr_set(message_id, state="pending")

        async def run() -> None:
            try:
                out = await backend.transcribe(path=agent.media.abs_path(str(info["rel_path"])),
                                               mime=info.get("mime"), media_id=mid)
                text = str((out or {}).get("text") or "")
                conf = (out or {}).get("confidence")
                _asr_set(message_id, state="done", text=text, confidence=conf,
                         fill_text=not (row.get("text") or "").strip())
            except Exception as e:
                log.warning("#53 转写失败 message=%s: %s", message_id, e)
                _asr_set(message_id, state="failed")

        asyncio.create_task(run(), name=f"asr:{message_id}")
        _audit(p, "message.asr", account_id=row["account_id"], detail={"message_id": message_id, "trace_id": trace_id})
        return JSONResponse(status_code=202, content={"ok": True, "trace_id": trace_id, "accepted": True,
                                                      "message_id": message_id})

    def _asr_set(message_id: str, *, state: str, text: Optional[str] = None, confidence: Any = None,
                 fill_text: bool = False) -> None:
        """回写 ``asr_state`` / ``asr_text`` / ``asr_confidence``;``fill_text`` 时另把 ``text`` 与 ``text_source='asr'`` 写上。"""
        with _tx(agent.store) as c:
            c.execute("UPDATE messages SET asr_state=?, asr_text=COALESCE(?, asr_text), "
                      "asr_confidence=COALESCE(?, asr_confidence) WHERE id=?",
                      (state, text, float(confidence) if isinstance(confidence, (int, float)) else None, message_id))
            if fill_text and text:
                c.execute("UPDATE messages SET text=?, text_len=?, text_source='asr' WHERE id=?",
                          (text, len(text), message_id))

    # ================================================================== #54 消息清理(danger)
    @app.post(f"{P}/messages/purge")
    async def messages_purge(request: Request):
        """#54,级别 **A**(danger,00 §11.17 ① 九项之一):``{account_id, before, mode:'all|text_only'}`` → ``202 {job_id}``。

        ``text_only`` 只清正文与 FTS、保留元数据与媒体引用;``all`` 删行(媒体只减 refcount,与 §2.8.4 同一路径)。
        🔴 本实现额外要求 ``confirm:true``(总控裁决,与 #8 同口径):**没有 `confirm` 一律 `400` 且一行都不删**。
        02 #54 原句是「二次确认由控制台做」,两者的关系见交接「留给文档方」一节。
        """
        p = principal(request, "admin")
        body = await json_or_empty(request)
        account_id = body.get("account_id")
        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise ApiError(400, "INVALID_ARGS", "account_id 须为非空字符串或省略(省略 = 全部账号)", reason="bad_account_id",
                           extra={"details": [{"pointer": "/account_id"}]})
        if isinstance(account_id, str):
            _account_or_404(p, account_id, include_deleted=True)
            request.state.account_id = account_id
        mode = body.get("mode") or "all"
        if mode not in MESSAGES_PURGE_MODES:
            raise ApiError(400, "INVALID_ARGS", f"mode 须为 {'|'.join(MESSAGES_PURGE_MODES)}", reason="bad_mode",
                           extra={"details": [{"pointer": "/mode"}]})
        before = body.get("before")
        before_ms = _parse_before(before)
        if body.get("confirm") is not True:
            raise ApiError(400, "INVALID_ARGS", "messages/purge 是不可逆清理,须带 confirm:true", reason="confirm_required",
                           extra={"details": [{"pointer": "/confirm"}]})
        job_id = agent.store.job_create(kind="messages_purge", actor=p.actor,
                                        account_id=account_id if isinstance(account_id, str) else None,
                                        params={"mode": mode, "before": before, "account_id": account_id})

        async def job_body() -> dict[str, Any]:
            _mark_irreversible(job_id)                    # ← 这一行之后不可取消(#108)
            return await asyncio.to_thread(_purge_messages_rows, account_id, before_ms, mode)

        agent.spawn_job(job_id, "messages_purge", job_body)
        _audit(p, "messages.purge", account_id=account_id if isinstance(account_id, str) else None,
               detail={"job_id": job_id, "mode": mode, "before": before})
        return JSONResponse(status_code=202, content={"ok": True, "job_id": job_id})

    def _parse_before(before: Any) -> Optional[int]:
        if before is None or before == "":
            return None
        if isinstance(before, int) and not isinstance(before, bool):
            return before
        if isinstance(before, str):
            from datetime import datetime
            try:
                return int(datetime.fromisoformat(before.replace(" ", "+")).timestamp() * 1000)
            except ValueError:
                pass
        raise ApiError(400, "INVALID_ARGS", "before 须为 ISO 8601 时间或 epoch 毫秒", reason="bad_before",
                       extra={"details": [{"pointer": "/before"}]})

    def _purge_messages_rows(account_id: Optional[str], before_ms: Optional[int], mode: str) -> dict[str, Any]:
        """§2.8.4 同一路径:``all`` 删行(媒体减 refcount)、``text_only`` 只清正文(FTS 由触发器跟着走)。"""
        now = agent.clock()
        where, params = [], []
        if isinstance(account_id, str):
            where.append("account_id=?")
            params.append(account_id)
        if before_ms is not None:
            where.append("ts_ms < ?")
            params.append(before_ms)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with _tx(agent.store) as c:
            rows = c.execute(f"SELECT id, media_json FROM messages{cond}", params).fetchall()
            if mode == "text_only":
                n = c.execute(f"UPDATE messages SET text=NULL, text_len=0{cond}", params).rowcount
                return {"mode": mode, "rows": n, "media_deref": 0}
            deref = 0
            for r in rows:
                try:
                    items = json.loads(r["media_json"] or "[]")
                except ValueError:
                    items = []
                for it in items:
                    mid = it.get("media_id") if isinstance(it, dict) else None
                    if mid is None:
                        continue
                    c.execute("UPDATE media SET ref_count = MAX(0, ref_count - 1), last_ref_ms=? WHERE id=?", (now, int(mid)))
                    deref += 1
            n = c.execute(f"DELETE FROM messages{cond}", params).rowcount
        return {"mode": mode, "rows": n, "media_deref": deref}

    # ================================================================== #82 / #83 排空与停机
    @app.post(f"{P}/system/drain")
    async def system_drain(request: Request):
        """#82,级别 A:升级前排空(03 §2.13 第 1 步)——**停接新指令 → 等在跑指令 ≤30 s → 停全部账号容器**,
        ``desired_state`` **保留**(升级后按 §2.6 恢复)→ ``{drained:true}``。

        「停接新指令」由本模块的闸门中间件执行(见 `_drain_gate`):置位后指令类端点一律
        ``503 NOT_READY(draining)``。⚠️ **没有逆操作**:02 没给「取消 drain」的端点,恢复受理靠 Agent 重启
        (升级流程本来就会重启)—— 见交接「留给文档方」。
        """
        p = principal(request, "admin")
        body = await json_or_empty(request)
        try:
            wait_s = float(body.get("timeout_s", DRAIN_WAIT_S))
        except (TypeError, ValueError):
            raise ApiError(400, "INVALID_ARGS", "timeout_s 须为数字", reason="bad_timeout",
                           extra={"details": [{"pointer": "/timeout_s"}]})
        if not (0 < wait_s <= 300):
            raise ApiError(400, "INVALID_ARGS", "timeout_s 须在 (0, 300]", reason="bad_timeout",
                           extra={"details": [{"pointer": "/timeout_s"}]})
        agent.drained = True                                   # ← 闸门置位:此后指令类端点一律 503
        inflight_before = _bus_inflight()
        waited = 0.0
        while _bus_inflight() > 0 and waited < wait_s:
            await asyncio.sleep(0.05)
            waited += 0.05
        inflight_after = _bus_inflight()
        stopped: list[str] = []
        desired: dict[str, str] = {}
        for row in agent.store.list_accounts():
            if row["state"] in ("stopped", "disabled", "created") or row.get("deleted_ms"):
                continue
            desired[row["id"]] = row.get("desired_state") or "stopped"
            try:
                await agent.accounts.stop(row["id"], graceful=True, actor=p.actor)
                await agent.accounts.wait_idle(row["id"])
                stopped.append(row["id"])
            except Exception as e:
                log.warning("#82 drain 停账号 %s 失败: %s", row["id"], e)
        for aid, want in desired.items():
            # 02 #82「`desired_state` 保留」:`accounts.stop` 会把它置 stopped,这里按 drain 前的值还原,
            # 否则升级后 §2.6 恢复时**原本在跑的账号一个都起不回来**。
            agent.store.set_desired_state(aid, want)
        _audit(p, "system.drain", detail={"inflight_before": inflight_before, "inflight_after": inflight_after,
                                          "stopped": stopped, "waited_s": round(waited, 2)})
        return {"ok": True, "drained": True, "inflight": inflight_after, "inflight_before": inflight_before,
                "waited_s": round(waited, 2), "stopped_accounts": stopped}

    def _bus_inflight() -> int:
        """在途指令数 = 各账号队列里排着的 + 正在等确认的(读不到就当 0,不编造)。"""
        bus = getattr(agent, "bus", None)
        if bus is None:
            return 0
        queued = sum(q.qsize() for q in getattr(bus, "_queues", {}).values())
        return queued + len(getattr(bus, "_inflight", {}))

    @app.post(f"{P}/system/shutdown")
    async def system_shutdown(request: Request):
        """#83,级别 A:优雅停机(§2.6);**容器不停**(与 #82 不同)。须 ``confirm:true``。

        执行体 = 可注入的 ``agent.shutdown_hook``(缺省 = ``AgentApp.stop()``);本端点**先把 202 回出去**
        再在后台触发钩子,免得连自己这条响应都被停机掐断。
        """
        p = principal(request, "admin")
        body = await json_or_empty(request)
        if body.get("confirm") is not True:
            raise ApiError(400, "INVALID_ARGS", "优雅停机须带 confirm:true(停机后控制台会断开)",
                           reason="confirm_required", extra={"details": [{"pointer": "/confirm"}]})
        hook = getattr(agent, "shutdown_hook", None) or agent.stop
        _audit(p, "system.shutdown", detail={"hook": getattr(hook, "__name__", type(hook).__name__)})

        async def run() -> None:
            await asyncio.sleep(0)                # 让本请求的响应先写出去
            try:
                out = hook()
                if asyncio.iscoroutine(out):
                    await out
            except Exception as e:
                log.exception("#83 优雅停机失败: %s", e)

        asyncio.create_task(run(), name="system-shutdown")
        return JSONResponse(status_code=202, content={"ok": True, "accepted": True, "stopping": True})

    # ================================================================== #82 的闸门:停接新指令
    #: drain 后仍可用的是只读与运维类;下面这些**新指令**入口一律 503(升级流程要的就是这个)
    DRAIN_BLOCKED = ("/commands", "/send", "/stream/input", "/start", "/restart", "/broadcast/commands")

    @app.middleware("http")
    async def _drain_gate(request: Request, call_next):
        """#82「停接新指令」的落点。未 drain 时**零影响**(直接放行)。"""
        if getattr(agent, "drained", False) and request.method == "POST":
            path = request.url.path
            if path.startswith(P) and any(path.endswith(s) for s in DRAIN_BLOCKED):
                body = {"ok": False, "code": "NOT_READY",
                        "error": {"message": "Agent 正在排空(#82 drain),已停止受理新指令", "reason": "draining",
                                  "retryable": True, "needs_human": False},
                        "trace_id": getattr(request.state, "trace_id", None) or ulid(agent.clock())}
                # 本闸门在 `versions_and_audit` **外层**(后加的 middleware 在外),版本头要自己补齐,
                # 否则控制台的版本协商在 drain 期间会拿到一个没有 `X-QT-Api-Version` 的响应。
                resp = JSONResponse(status_code=503, content=body)
                resp.headers["X-QT-Api-Version"] = cfg.api.api_version
                resp.headers["X-QT-Agent-Version"] = AGENT_VERSION
                return resp
        return await call_next(request)


def job_is_irreversible(row: dict[str, Any]) -> bool:
    """#108:``account_purge`` / ``messages_purge`` 是否已进入**不可逆阶段**(由 `_mark_irreversible` 打标)。

    `api/app.py` 的 ``POST /jobs/{job_id}/cancel`` 用它判 ``409 NOT_CANCELLABLE``(02 #108 逐字)。
    """
    if row.get("kind") not in ("account_purge", "messages_purge"):
        return False
    try:
        params = json.loads(row.get("params_json") or "{}")
    except ValueError:
        return False
    return params.get("irreversible_since_ms") is not None


def note_broadcast(store, *, broadcast_id: str, op: str, actor: str, results: dict[str, Any], now_ms: int) -> None:
    """#36 群发结果的落点,供 #37 查(``settings['broadcast.<id>']``;``jobs.kind`` 的 CHECK 里没有 broadcast)。"""
    try:
        store.settings_set(f"broadcast.{broadcast_id}",
                           {"op": op, "actor": actor, "submitted_at": iso8601(now_ms), "results": results},
                           actor=actor, now_ms=now_ms)
    except Exception as e:      # 群发本身已经成了,汇总落不下来只该少一条查询,不该把 200 变成 500
        log.warning("#36 广播汇总落 settings 失败 broadcast_id=%s: %s", broadcast_id, e)
