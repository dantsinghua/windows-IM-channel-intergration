"""``pipe`` 模块 —— 服务 ⇄ 用户会话代理的内部 IPC(02 §2.4.1,G-06)。

逐字落地的几条:

- **通道**:``\\\\.\\pipe\\qtrade-winagent-user``(与控制台令牌管道**分开**),消息模式,**服务端在服务侧**;
  会话代理启动即连接并发 ``HELLO {session_id, user_sid, version, pid, modules:[wslctl, wechat?]}``,
  服务回 ``WELCOME {ipc_version}``。方向固定「服务 → 会话代理」发请求,会话代理只回响应与心跳。
- **帧格式**:UTF-8 JSON,一帧一条;心跳 ``{id:0, method:"ping"}`` / ``{id:0, ok:true}`` 每 5s;单帧 ≤ 1 MB。
- **超时**:服务按 §2.5 各动作超时**减 1s** 作 ``deadline_ms``;会话代理到点主动返回 ``TIMEOUT``;
  **15s 无心跳 → 服务判会话代理离线**。管道读写一律在线程里(R-09 §2.3.1 ⑤)。
- 🔴 **分级(R2-10 + R3-1)**:控制面探活 ``ping``/``health`` **不经本管道**——由服务直接回答、单次 2s、
  连续 3 次超时才判离线;``user_agent`` 取自**服务维护的心跳状态**。只有数据面才走管道,按各自 ``deadline_ms``。
  **否则拿探活超时会先把 send 打死**(send 本身要 15 s)。本模块因此**不提供**任何「穿管道去问探活」的入口。
- 🔴 **多用户仲裁不是先到先得(R3-12)**:服务对每个来连的会话代理**必须先校验对端进程 SID == 安装用户 SID**,
  非安装用户一律 ``HELLO`` 即拒(``FORBIDDEN {reason:"仅安装用户可控 WSL"}``)——否则任意登录用户的会话代理
  都能抢到 WSL 控制权,那是**提权**。校验通过者里再取**第一个完成 HELLO 的**为持有者(``holder_session_id``),
  其余回 ``BUSY {holder_user}`` 并每 30s 重试;持有者断开后下一位接管。
- **版本**:``HELLO.version`` 主版本须与服务一致,否则回 ``VERSION_MISMATCH`` 并记 ``alert WA_USER_VERSION_MISMATCH``。
- **审计**:每次调度一行 ``wa_audit_log(actor='svc→user', action=method, target, result, trace_id)``。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import alerts as A
from .alerts import AlertBuffer
from .audit import ACTOR_SVC_TO_USER, Audit
from .backends import PipeBackend, PipeConn, PipeFrame
from .config import IpcConfig
from .errors import FORBIDDEN, INTERNAL, TIMEOUT, VERSION_MISMATCH, WaError, user_agent_offline
from .logfmt import get_logger

log = get_logger("pipe")

IPC_VERSION = "1.0"
HELLO = "HELLO"
WELCOME = "WELCOME"
PING = "ping"
HEARTBEAT_ID = 0
BUSY_RETRY_S = 30                        # 02 §2.4.1:非持有者每 30s 重试
FORBIDDEN_REASON = "仅安装用户可控 WSL"   # 逐字


def method_for_path(path: str) -> str:
    """``/wa/v1/wsl/start`` ⇒ ``wsl.start``(02 §2.4.1「与 /wa/v1 一一映射」;只对**纯 user 端点**成立)。

    混合端点(#19/#25/#26/#27/#37/#46)**不是**一一映射,由各模块自己按 R3-15 拆两半,不走本函数。
    """
    p = path[len("/wa/v1/"):] if path.startswith("/wa/v1/") else path.lstrip("/")
    return p.strip("/").replace("/", ".")


def major(version: str) -> str:
    return version.split(".", 1)[0]


@dataclass
class UserSession:
    """一个已握手的会话代理。``holder`` = 当前持有者(唯一被下发请求的那个)。"""
    session_id: str
    user_sid: str
    version: str
    pid: int
    modules: tuple[str, ...]
    conn: PipeConn
    connected_ms: int
    last_heartbeat_ms: int
    holder: bool = False
    reader: Optional[asyncio.Task] = None


class PipeHub:
    """**服务侧**的管道枢纽:握手 / SID 校验 / 仲裁 / 心跳 / 请求下发。"""

    def __init__(self, backend: PipeBackend, cfg: IpcConfig, *, pipe_name: str, install_user_sid: str,
                 version: str, alerts: Optional[AlertBuffer] = None, audit: Optional[Audit] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._backend = backend
        self._cfg = cfg
        self._pipe_name = pipe_name
        self._install_user_sid = install_user_sid
        self._version = version
        self._alerts = alerts
        self._audit = audit
        self._clock = clock
        self.sessions: dict[str, UserSession] = {}
        self.holder_session_id: Optional[str] = None
        self.rejected: list[tuple[str, str]] = []            # (reason, sid) —— 便于测试与排障
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._accept_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- 心跳状态(探活只读它,绝不穿管道)
    @property
    def user_agent_online(self) -> bool:
        """``GET /wa/v1/health.user_agent`` 的真值:**服务维护的心跳状态**(R3-1)。"""
        h = self.holder
        if h is None:
            return False
        return (self._clock() - h.last_heartbeat_ms) <= self._cfg.offline_after_s * 1000

    @property
    def holder(self) -> Optional[UserSession]:
        sid = self.holder_session_id
        return self.sessions.get(sid) if sid else None

    def user_session_view(self) -> Optional[dict[str, Any]]:
        """``GET /wa/v1/health.user_session``(#2:``{sid, user}?``)。"""
        h = self.holder
        return {"sid": h.user_sid, "user": h.session_id, "pid": h.pid, "modules": list(h.modules)} if h else None

    # ---------------------------------------------------------------- 接入 / 握手
    async def start(self) -> None:
        self._accept_task = asyncio.create_task(self._accept_loop(), name="pipe-accept")

    async def stop(self) -> None:
        if self._accept_task:
            self._accept_task.cancel()
        for s in list(self.sessions.values()):
            if s.reader:
                s.reader.cancel()
            await s.conn.close()
        self.sessions.clear()
        self.holder_session_id = None

    async def _accept_loop(self) -> None:
        while True:
            conn = await self._backend.serve(self._pipe_name)
            try:
                await self.handshake(conn)
            except asyncio.CancelledError:
                raise
            except Exception:                                  # 单个连接出问题不影响下一个
                log.exception("握手失败")

    async def handshake(self, conn: PipeConn) -> Optional[UserSession]:
        """收 ``HELLO`` → **先 SID 校验再仲裁** → 回 ``WELCOME`` / ``FORBIDDEN`` / ``BUSY`` / ``VERSION_MISMATCH``。"""
        frame = await conn.recv()
        if frame is None or frame.method != HELLO:
            await conn.close()
            return None
        p = frame.params
        sid = str(p.get("user_sid") or conn.peer_sid or "")
        session_id = str(p.get("session_id") or "")
        # ① 安全校验在前(R3-12):对端进程 SID 必须 == 安装用户 SID
        peer = conn.peer_sid or sid
        if peer != self._install_user_sid:
            self.rejected.append((FORBIDDEN, peer))
            await conn.send(PipeFrame(id=frame.id, ok=False, error={"code": FORBIDDEN, "reason": FORBIDDEN_REASON,
                                                                    "message": FORBIDDEN_REASON}))
            await conn.close()
            log.warning("拒绝非安装用户的会话代理", extra={"op": "pipe.hello", "code": FORBIDDEN, "kv": {"sid": peer}})
            return None
        # ② 版本:主版本不一致 → VERSION_MISMATCH + alert WA_USER_VERSION_MISMATCH
        ver = str(p.get("version") or "")
        if major(ver) != major(self._version):
            self.rejected.append((VERSION_MISMATCH, peer))
            if self._alerts:
                self._alerts.firing(A.WA_USER_VERSION_MISMATCH, subject="host",
                                    evidence={"svc_version": self._version, "user_version": ver})
            await conn.send(PipeFrame(id=frame.id, ok=False,
                                      error={"code": VERSION_MISMATCH, "message": f"会话代理版本 {ver} 与服务 {self._version} 主版本不一致"}))
            await conn.close()
            return None
        now = self._clock()
        s = UserSession(session_id=session_id, user_sid=peer, version=ver, pid=int(p.get("pid") or 0),
                        modules=tuple(p.get("modules") or ()), conn=conn, connected_ms=now, last_heartbeat_ms=now)
        # ③ 仲裁:校验通过者里取第一个完成 HELLO 的为持有者;其余 BUSY + 每 30s 重试
        if self.holder_session_id is not None and self.holder_session_id in self.sessions:
            holder = self.sessions[self.holder_session_id]
            self.rejected.append(("BUSY", peer))
            await conn.send(PipeFrame(id=frame.id, ok=False,
                                      error={"code": "BUSY", "holder_user": holder.session_id,
                                             "retry_after_s": BUSY_RETRY_S,
                                             "message": f"WSL 控制权已被会话 {holder.session_id} 持有"}))
            await conn.close()
            return None
        s.holder = True
        self.sessions[session_id] = s
        self.holder_session_id = session_id
        await conn.send(PipeFrame(id=frame.id, ok=True, result={"type": WELCOME, "ipc_version": IPC_VERSION,
                                                                "svc_version": self._version}))
        s.reader = asyncio.create_task(self._read_loop(s), name=f"pipe-read-{session_id}")
        if self._alerts:
            self._alerts.resolve(A.WA_USER_VERSION_MISMATCH, subject="host")
        log.info("会话代理上线", extra={"op": "pipe.hello", "code": "OK", "kv": {"session": session_id}})
        return s

    async def _read_loop(self, s: UserSession) -> None:
        try:
            while True:
                frame = await s.conn.recv()
                if frame is None:
                    break
                if frame.id == HEARTBEAT_ID:                    # 心跳:记时刻并回 {id:0, ok:true}
                    s.last_heartbeat_ms = self._clock()
                    await s.conn.send(PipeFrame(id=HEARTBEAT_ID, ok=True))
                    continue
                fut = self._pending.pop(frame.id, None)
                if fut is not None and not fut.done():
                    fut.set_result(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("管道读循环异常")
        finally:
            self._drop(s)

    def _drop(self, s: UserSession) -> None:
        """持有者断开后下一位接管(仲裁的「接管」半;本实现里下一位是 ``sessions`` 里最早连上的那个)。"""
        self.sessions.pop(s.session_id, None)
        if self.holder_session_id == s.session_id:
            self.holder_session_id = None
            nxt = min(self.sessions.values(), key=lambda x: x.connected_ms, default=None)
            if nxt is not None:
                nxt.holder = True
                self.holder_session_id = nxt.session_id
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(user_agent_offline())
        self._pending.clear()

    # ---------------------------------------------------------------- 下发请求
    async def call(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout_s: float,
                   trace_id: Optional[str] = None, target: Optional[str] = None) -> Any:
        """服务 → 会话代理。``deadline_ms`` = **该动作超时 − 1s**(02 §2.4.1「超时」行);离线一律 503 NOT_READY。"""
        h = self.holder
        if h is None or not self.user_agent_online:
            if self._audit:
                self._audit.record(actor=ACTOR_SVC_TO_USER, action=method, target=target, result="NOT_READY",
                                   trace_id=trace_id)
            raise user_agent_offline()
        deadline_ms = max(1000, int(timeout_s * 1000) - 1000)
        self._next_id += 1
        fid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[fid] = fut
        await h.conn.send(PipeFrame(id=fid, method=method, params=dict(params or {}), trace_id=trace_id,
                                    deadline_ms=deadline_ms))
        try:
            frame: PipeFrame = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as e:
            self._pending.pop(fid, None)
            if self._audit:
                self._audit.record(actor=ACTOR_SVC_TO_USER, action=method, target=target, result=TIMEOUT, trace_id=trace_id)
            raise WaError(TIMEOUT, f"会话代理在 {timeout_s}s 内未返回({method})", reason="user_agent_timeout") from e
        if frame.ok:
            if self._audit:
                self._audit.record(actor=ACTOR_SVC_TO_USER, action=method, target=target, result="OK", trace_id=trace_id)
            return frame.result
        err = frame.error or {}
        code = str(err.get("code") or INTERNAL)
        if self._audit:
            self._audit.record(actor=ACTOR_SVC_TO_USER, action=method, target=target, result=code, trace_id=trace_id)
        raise WaError(code, str(err.get("message") or "会话代理返回错误"), reason=str(err.get("reason") or ""),
                      stage="user")


# ---------------------------------------------------------------------- 会话代理侧


Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class UserAgentLink:
    """**会话代理侧**的管道客户端:连接 → ``HELLO`` → 处理请求 + 每 ``heartbeat_s`` 发心跳。

    到 ``deadline_ms`` **主动返回 ``TIMEOUT``**(02 §2.4.1「超时」行:会话代理到点自己回,不让服务干等)。
    """

    def __init__(self, backend: PipeBackend, cfg: IpcConfig, *, pipe_name: str, session_id: str, user_sid: str,
                 version: str, pid: int, modules: tuple[str, ...],
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._backend = backend
        self._cfg = cfg
        self._pipe_name = pipe_name
        self.session_id = session_id
        self.user_sid = user_sid
        self.version = version
        self.pid = pid
        self.modules = modules
        self._clock = clock
        self.handlers: dict[str, Handler] = {}
        self.conn: Optional[PipeConn] = None
        self.welcome: Optional[dict[str, Any]] = None
        self._tasks: list[asyncio.Task] = []

    def on(self, method: str, handler: Handler) -> None:
        self.handlers[method] = handler

    async def connect(self) -> dict[str, Any]:
        """返回 ``WELCOME`` 结果;被拒时抛 ``WaError``(``FORBIDDEN``/``BUSY``/``VERSION_MISMATCH``)。"""
        conn = await self._backend.connect(self._pipe_name, peer_sid=self.user_sid)     # type: ignore[call-arg]
        self.conn = conn
        await conn.send(PipeFrame(id=1, method=HELLO, params={
            "session_id": self.session_id, "user_sid": self.user_sid, "version": self.version,
            "pid": self.pid, "modules": list(self.modules)}))
        reply = await conn.recv()
        if reply is None or not reply.ok:
            err = (reply.error if reply else None) or {"code": INTERNAL, "message": "管道握手无响应"}
            raise WaError(str(err.get("code") or INTERNAL), str(err.get("message") or ""), reason=str(err.get("reason") or ""),
                          extra={k: v for k, v in err.items() if k in ("holder_user", "retry_after_s")})
        self.welcome = reply.result
        return reply.result

    async def run(self) -> None:
        """启动请求处理与心跳两个任务(会话代理主循环)。"""
        self._tasks = [asyncio.create_task(self._serve_loop(), name="ua-serve"),
                       asyncio.create_task(self._heartbeat_loop(), name="ua-heartbeat")]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.conn:
            await self.conn.close()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.heartbeat_s)
            if self.conn is None:
                return
            await self.conn.send(PipeFrame(id=HEARTBEAT_ID, method=PING))

    async def _serve_loop(self) -> None:
        assert self.conn is not None
        while True:
            frame = await self.conn.recv()
            if frame is None:
                return
            if frame.id == HEARTBEAT_ID:                          # 服务对心跳的回应,忽略
                continue
            asyncio.create_task(self._dispatch(frame))

    async def _dispatch(self, frame: PipeFrame) -> None:
        assert self.conn is not None
        h = self.handlers.get(frame.method or "")
        if h is None:
            await self.conn.send(PipeFrame(id=frame.id, ok=False,
                                           error={"code": "TARGET_NOT_FOUND", "message": f"未知方法 {frame.method}"}))
            return
        budget = (frame.deadline_ms or 30000) / 1000
        try:
            result = await asyncio.wait_for(h(frame.params), timeout=budget)
        except asyncio.TimeoutError:
            await self.conn.send(PipeFrame(id=frame.id, ok=False,
                                           error={"code": TIMEOUT, "message": f"会话代理超过 deadline_ms={frame.deadline_ms}"}))
            return
        except WaError as e:
            await self.conn.send(PipeFrame(id=frame.id, ok=False,
                                           error={"code": e.code, "message": e.message, "reason": e.reason}))
            return
        except Exception as e:                                    # 任何未预期异常都要变成一帧,不能把管道读死
            await self.conn.send(PipeFrame(id=frame.id, ok=False, error={"code": INTERNAL, "message": repr(e)}))
            return
        await self.conn.send(PipeFrame(id=frame.id, ok=True, result=result))
